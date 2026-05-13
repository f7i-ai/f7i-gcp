"""Standalone LSTM-VAE unsupervised anomaly detection — SageMaker entry point.

Completely independent of the RL-LLM-VAE pipeline. Run directly as a SageMaker
PyTorch job for comparison against the consensus-bootstrapped RL approach.

Architecture
------------
  Encoder : LSTM(n_features, hidden) → last hidden → Linear → (μ, log σ²)
  Decoder : Linear(latent) → repeat seq_len → LSTM(hidden, n_features) → output
  Loss    : β-VAE ELBO  (MSE reconstruction + β·KLD)

Anomaly detection
-----------------
  Both the MSE threshold and the Mahalanobis covariance statistics are
  calibrated on the held-out 15 % validation split — never on the train
  split — so thresholds reflect genuine out-of-sample reconstruction quality.

  MSE threshold  : mean(val_errors) + sigma * std(val_errors)
  Mahalanobis    : μ_r, Σ⁻¹ fit on val per-feature squared residuals;
                   alert threshold = 99th percentile of val Mah² scores.

Outputs (saved to SM_MODEL_DIR)
--------------------------------
  lstm_vae.pt             — model weights
  scaler.pkl              — fitted MinMaxScaler
  threshold.json          — {"threshold": float, "mean": float, "std": float,
                              "sigma": float, calibrated_on: "validation"}
  mahalanobis_stats.json  — μ_r, Σ⁻¹, mah_threshold, val_scores_sorted
  history.json            — per-epoch training loss
  config.json             — all hyperparameters
"""
import argparse
import json
import os
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.preprocessing import MinMaxScaler
from torch.utils.data import DataLoader, TensorDataset

# ── Paths injected by SageMaker ───────────────────────────────────────────────
MODEL_DIR   = Path(os.environ.get("SM_MODEL_DIR",            "/opt/ml/model"))
TRAIN_CHAN  = Path(os.environ.get("SM_CHANNEL_TRAIN",        "data"))
VALID_CHAN  = Path(os.environ.get("SM_CHANNEL_VALIDATION",   "/opt/ml/input/data/validation"))
LABELS_CHAN = Path(os.environ.get("SM_CHANNEL_LABELS",       "data"))

SENSOR_COLS = [
    "temperature",
    "velocity_total_crest",
    "velocity_x_rms",
    "velocity_y_rms",
    "velocity_z_rms",
]

SIGMA_THRESHOLD = 2.0   # anomaly threshold: mean + SIGMA * std of validation errors


# ─────────────────────────────────────────────────────────────────────────────
#  Model
# ─────────────────────────────────────────────────────────────────────────────

class LSTMEncoder(nn.Module):
    def __init__(self, n_features: int, hidden: int, latent: int, n_layers: int = 2):
        super().__init__()
        self.lstm  = nn.LSTM(n_features, hidden, n_layers,
                             batch_first=True, dropout=0.1 if n_layers > 1 else 0.0)
        self.fc_mu     = nn.Linear(hidden, latent)
        self.fc_logvar = nn.Linear(hidden, latent)

    def forward(self, x):
        # x: (B, T, F)
        _, (h, _) = self.lstm(x)
        h_last = h[-1]                      # last layer hidden: (B, hidden)
        return self.fc_mu(h_last), self.fc_logvar(h_last)


class LSTMDecoder(nn.Module):
    def __init__(self, n_features: int, hidden: int, latent: int,
                 seq_len: int, n_layers: int = 2):
        super().__init__()
        self.seq_len = seq_len
        self.fc_in   = nn.Linear(latent, hidden)
        self.lstm    = nn.LSTM(hidden, hidden, n_layers,
                               batch_first=True, dropout=0.1 if n_layers > 1 else 0.0)
        self.fc_out  = nn.Linear(hidden, n_features)

    def forward(self, z):
        # z: (B, latent)
        h = torch.relu(self.fc_in(z))                    # (B, hidden)
        h = h.unsqueeze(1).repeat(1, self.seq_len, 1)    # (B, T, hidden)
        out, _ = self.lstm(h)                             # (B, T, hidden)
        return torch.sigmoid(self.fc_out(out))            # (B, T, F) in [0,1]


class LSTMVAE(nn.Module):
    def __init__(self, n_features: int, hidden: int = 64,
                 latent: int = 16, seq_len: int = 25, n_layers: int = 2):
        super().__init__()
        self.encoder = LSTMEncoder(n_features, hidden, latent, n_layers)
        self.decoder = LSTMDecoder(n_features, hidden, latent, seq_len, n_layers)

    def reparameterise(self, mu, logvar):
        if self.training:
            std = torch.exp(0.5 * logvar)
            return mu + std * torch.randn_like(std)
        return mu

    def forward(self, x):
        mu, logvar = self.encoder(x)
        z = self.reparameterise(mu, logvar)
        return self.decoder(z), mu, logvar

    @torch.no_grad()
    def reconstruction_error(self, x: torch.Tensor) -> torch.Tensor:
        """Mean MSE over time steps and features — (B,) scalar per window."""
        self.eval()
        recon, _, _ = self(x)
        return ((x - recon) ** 2).mean(dim=(1, 2))   # (B,)


def elbo_loss(recon, x, mu, logvar, beta: float = 0.1) -> torch.Tensor:
    recon_loss = nn.functional.mse_loss(recon, x, reduction="sum")
    kld        = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
    return (recon_loss + beta * kld) / x.size(0)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_csv(channel: Path):
    """Return the training CSV — prefer files with 'train' in the name."""
    csvs = [f for f in channel.iterdir() if f.suffix == ".csv"]
    if not csvs:
        raise FileNotFoundError(f"No CSV found in {channel}")
    train_csvs = [f for f in csvs if "train" in f.name.lower() and "validation" not in f.name.lower()]
    return train_csvs[0] if train_csvs else csvs[0]


def _load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    missing = [c for c in SENSOR_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV missing columns: {missing}")
    return df


def _make_windows(data: np.ndarray, seq_len: int) -> np.ndarray:
    """Return (N, seq_len, n_features) float32 array."""
    return np.stack([data[i: i + seq_len] for i in range(len(data) - seq_len)],
                    axis=0).astype(np.float32)


def _point_adjust(labels: np.ndarray, preds: np.ndarray) -> np.ndarray:
    """Standard point-adjust: if any alert in a contiguous anomaly segment, flag all."""
    adjusted = preds.copy()
    in_anom  = False
    start    = 0
    for i, lbl in enumerate(labels):
        if lbl == 1 and not in_anom:
            in_anom = True
            start = i
        elif lbl == 0 and in_anom:
            if preds[start:i].any():
                adjusted[start:i] = 1
            in_anom = False
    if in_anom and preds[start:].any():
        adjusted[start:] = 1
    return adjusted


# ─────────────────────────────────────────────────────────────────────────────
#  Training
# ─────────────────────────────────────────────────────────────────────────────

def train_lstm_vae(
    windows: np.ndarray,           # (N, T, F) — normalised
    n_features: int,
    seq_len: int,
    hidden: int,
    latent: int,
    n_layers: int,
    beta: float,
    lr: float,
    epochs: int,
    patience: int,
    batch_size: int,
    device: torch.device,
) -> tuple:
    tensor  = torch.FloatTensor(windows).to(device)
    loader  = DataLoader(TensorDataset(tensor), batch_size=batch_size, shuffle=True)
    model   = LSTMVAE(n_features, hidden, latent, seq_len, n_layers).to(device)
    opt     = torch.optim.Adam(model.parameters(), lr=lr)
    sched   = torch.optim.lr_scheduler.ReduceLROnPlateau(
                  opt, mode="min", factor=0.5, patience=10, min_lr=1e-5)

    best_loss  = float("inf")
    no_improve = 0
    best_state = None
    history    = {"epoch": [], "loss": [], "lr": []}

    for epoch in range(1, epochs + 1):
        model.train()
        total = 0.0
        for (batch,) in loader:
            opt.zero_grad()
            recon, mu, logvar = model(batch)
            loss = elbo_loss(recon, batch, mu, logvar, beta)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total += loss.item()
        avg  = total / len(loader)
        cur_lr = opt.param_groups[0]["lr"]
        sched.step(avg)

        history["epoch"].append(epoch)
        history["loss"].append(avg)
        history["lr"].append(cur_lr)

        if epoch % 10 == 0 or epoch == 1:
            print(f"LSTM-VAE Epoch {epoch:4d}/{epochs}  loss={avg:.4f}  lr={cur_lr:.2e}")

        if avg < best_loss:
            best_loss  = avg
            no_improve = 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            no_improve += 1

        if no_improve >= patience:
            print(f"LSTM-VAE converged at epoch {epoch} "
                  f"(patience={patience}  best_loss={best_loss:.4f})")
            break

    if best_state is not None:
        model.load_state_dict(best_state)

    return model, history, best_loss


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="LSTM-VAE standalone unsupervised training")
    p.add_argument("--sensor-id",   default="unknown")
    p.add_argument("--seq-len",     type=int,   default=25)
    p.add_argument("--hidden",      type=int,   default=64)
    p.add_argument("--latent",      type=int,   default=16)
    p.add_argument("--n-layers",    type=int,   default=2)
    p.add_argument("--beta",        type=float, default=0.1)
    p.add_argument("--lr",          type=float, default=1e-3)
    p.add_argument("--epochs",      type=int,   default=300)
    p.add_argument("--patience",    type=int,   default=30)
    p.add_argument("--batch-size",  type=int,   default=32)
    p.add_argument("--sigma",       type=float, default=2.0,
                   help="Threshold = mean + sigma*std of validation reconstruction errors")
    p.add_argument("--alert-win-days", type=int, default=7)
    p.add_argument("--model-dir",   default=str(MODEL_DIR))
    p.add_argument("--train",       default=str(TRAIN_CHAN))
    p.add_argument("--validation",  default=str(VALID_CHAN))
    p.add_argument("--labels",      default=str(LABELS_CHAN))
    return p.parse_args()


def main():
    args   = _parse_args()
    t0     = time.time()
    outdir = Path(args.model_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    seq_len = args.seq_len

    print(f"\n{'='*60}")
    print(f"LSTM-VAE standalone training  sensor={args.sensor_id}")
    print(f"  hidden={args.hidden}  latent={args.latent}  "
          f"n_layers={args.n_layers}  beta={args.beta}")
    print(f"  seq_len={seq_len}  lr={args.lr}  epochs={args.epochs}  "
          f"patience={args.patience}  sigma={args.sigma}")
    print(f"  device={device}")
    print(f"{'='*60}\n")

    with open(outdir / "config.json", "w") as f:
        json.dump(vars(args), f, indent=2)

    # ── Load data ────────────────────────────────────────────────────────────
    train_csv = _find_csv(Path(args.train))
    df = _load_csv(train_csv)
    print(f"Loaded {len(df)} rows from {train_csv.name}  "
          f"({df['timestamp'].min()} → {df['timestamp'].max()})")

    # ── Scale ────────────────────────────────────────────────────────────────
    raw    = df[SENSOR_COLS].values.astype(np.float32)
    scaler = MinMaxScaler()
    scaled = scaler.fit_transform(raw)       # (N_rows, F) in [0,1]
    with open(outdir / "scaler.pkl", "wb") as f:
        pickle.dump(scaler, f)
    print(f"Scaler fitted on {len(scaled)} rows  features={SENSOR_COLS}")

    # ── Windows ──────────────────────────────────────────────────────────────
    windows = _make_windows(scaled, seq_len)   # (N_win, T, F)
    n_win, _, n_feat = windows.shape
    print(f"Windows: {n_win} × {seq_len} × {n_feat}")

    # ── Train ─────────────────────────────────────────────────────────────────
    model, history, best_loss = train_lstm_vae(
        windows    = windows,
        n_features = n_feat,
        seq_len    = seq_len,
        hidden     = args.hidden,
        latent     = args.latent,
        n_layers   = args.n_layers,
        beta       = args.beta,
        lr         = args.lr,
        epochs     = args.epochs,
        patience   = args.patience,
        batch_size = args.batch_size,
        device     = device,
    )
    torch.save(model.state_dict(), outdir / "lstm_vae.pt")
    with open(outdir / "history.json", "w") as f:
        json.dump(history, f)

    # ── Training reconstruction errors (logged only, not used for thresholds) ──
    model.eval()
    tensor       = torch.FloatTensor(windows).to(device)
    train_errors = model.reconstruction_error(tensor).cpu().numpy()   # (N_win,)
    np.save(outdir / "reconstruction_errors.npy", train_errors)
    print(f"\nTraining reconstruction errors (reference only)  "
          f"mean={train_errors.mean():.6f}  std={train_errors.std():.6f}")

    # ── Validation residuals — used for ALL thresholds ────────────────────────
    # Both the MSE threshold and the Mahalanobis stats are calibrated on the
    # held-out validation split (15 %) so thresholds reflect out-of-sample
    # reconstruction quality, not the optimistically low training errors.
    val_csv     = _find_csv(Path(args.validation))
    df_val      = _load_csv(val_csv)
    raw_val     = df_val[SENSOR_COLS].values.astype(np.float32)
    val_windows = _make_windows(scaler.transform(raw_val), seq_len)
    # Timestamps aligned to the end of each validation window (for label mapping)
    val_win_ts  = df_val["timestamp"].iloc[seq_len:]
    print(f"Validation channel: {len(df_val)} rows → {len(val_windows)} windows")

    val_tensor = torch.FloatTensor(val_windows).to(device)
    with torch.no_grad():
        val_recon, _, _ = model(val_tensor)         # (N_val, T, F)
    # Squared mean residual per feature over time → (N_val, F)
    val_residuals = ((val_tensor - val_recon)**2).mean(dim=1).cpu().numpy()

    # MSE threshold from validation errors
    val_errors = val_residuals.mean(axis=1)          # (N_val,) scalar per window
    err_mean   = float(val_errors.mean())
    err_std    = float(val_errors.std())
    threshold  = err_mean + args.sigma * err_std
    print(f"Validation reconstruction errors  mean={err_mean:.6f}  "
          f"std={err_std:.6f}  threshold(σ={args.sigma})={threshold:.6f}")

    threshold_info = {
        "threshold":    threshold,
        "mean":         err_mean,
        "std":          err_std,
        "sigma":        args.sigma,
        "n_windows":    int(len(val_windows)),
        "n_train_windows": n_win,
        "sensor_id":    args.sensor_id,
        "algo":         "lstm-vae",
        "calibrated_on": "validation",
    }
    with open(outdir / "threshold.json", "w") as f:
        json.dump(threshold_info, f, indent=2)

    # ── Mahalanobis residual statistics (same validation residuals) ───────────
    mu_r  = val_residuals.mean(axis=0)                        # (F,)
    cov_r = np.cov(val_residuals.T) + 1e-4 * np.eye(n_feat)  # (F, F) ridge
    cov_inv    = np.linalg.inv(cov_r)                         # raises if singular
    d          = val_residuals - mu_r                         # (N_val, F)
    mah_scores = np.einsum("ni,ij,nj->n", d, cov_inv, d)     # (N_val,)
    mah_mean      = float(mah_scores.mean())
    mah_std       = float(mah_scores.std() + 1e-9)
    # Empirical 99th percentile of validation Mahalanobis scores.
    # Ties the alert threshold directly to a 1 % false-positive rate on
    # healthy data — no chi-squared or Gaussian assumption required.
    mah_threshold     = float(np.percentile(mah_scores, 99))
    val_scores_sorted = np.sort(mah_scores).tolist()   # empirical CDF lookup at inference
    mah_stats  = {
        "mu_r":              mu_r.tolist(),
        "sigma_inv":         cov_inv.tolist(),
        "mah_threshold":     mah_threshold,
        "val_scores_sorted": val_scores_sorted,
        "mah_mean":          mah_mean,
        "mah_std":           mah_std,
        "n_val":             int(len(val_windows)),
    }
    with open(outdir / "mahalanobis_stats.json", "w") as f:
        json.dump(mah_stats, f, indent=2)
    print(f"Mahalanobis stats  n_val={len(val_windows)}"
          f"  mah_mean={mah_mean:.4f}  mah_std={mah_std:.4f}"
          f"  mah_threshold={mah_threshold:.4f}  (sigma={args.sigma})")
    print(f"METRIC_MAH_MEAN: {mah_mean:.6f}")
    print(f"METRIC_MAH_STD: {mah_std:.6f}")
    print(f"METRIC_MAH_THRESHOLD: {mah_threshold:.6f}")

    # ── Evaluate against human labels if labels.json present ────────────────
    # All evaluation is done on val_errors (validation split) vs the
    # validation-calibrated threshold.  Using train_errors here would be wrong
    # because the model was trained on that data — its reconstruction errors
    # are systematically lower than out-of-sample errors, making the
    # validation-calibrated threshold appear too strict and producing
    # misleading F1 / alert_pct metrics.
    labels_path = Path(args.labels) / "labels.json"
    if labels_path.exists():
        with open(labels_path) as f:
            label_data = json.load(f)
        tp_dates = [pd.Timestamp(d, tz="UTC") for d in label_data.get("tp_dates", [])]
        fp_dates = [pd.Timestamp(d, tz="UTC") for d in label_data.get("fp_dates", [])]
        alert_win = pd.Timedelta(days=args.alert_win_days)

        if tp_dates:
            n_val_win = len(val_windows)
            human_labels = np.zeros(n_val_win, dtype=np.int32)
            for dt in tp_dates:
                mask = ((val_win_ts >= dt - alert_win) & (val_win_ts <= dt)).values
                human_labels[mask] = 1
            for dt in fp_dates:
                mask = ((val_win_ts >= dt - alert_win) & (val_win_ts <= dt + alert_win)).values
                human_labels[mask] = 0

            predictions = (val_errors >= threshold).astype(np.int32)
            f1   = f1_score(human_labels, predictions, zero_division=0)
            f1pa = f1_score(human_labels, _point_adjust(human_labels, predictions),
                            zero_division=0)
            prec = precision_score(human_labels, predictions, zero_division=0)
            rec  = recall_score(human_labels, predictions, zero_division=0)
            alert_pct = predictions.mean()
            print(f"\nEvaluation vs human labels ({len(tp_dates)} TP events, on validation split)")
            print(f"  F1={f1:.4f}  F1_PA={f1pa:.4f}  "
                  f"Prec={prec:.4f}  Rec={rec:.4f}  Alert%={alert_pct:.2%}")
            print(f"METRIC_F1: {f1:.6f}")
            print(f"METRIC_F1_PA: {f1pa:.6f}")
            print(f"METRIC_PRECISION: {prec:.6f}")
            print(f"METRIC_RECALL: {rec:.6f}")
            print(f"METRIC_ALERT_PCT: {alert_pct:.6f}")
        else:
            # No human labels — report anomaly rate on validation split
            predictions = (val_errors >= threshold).astype(np.int32)
            alert_pct   = predictions.mean()
            print(f"\nNo human labels found — unsupervised metrics only (validation split)")
            print(f"  Alert%={alert_pct:.2%}  threshold={threshold:.6f}")
            print(f"METRIC_ALERT_PCT: {alert_pct:.6f}")
    else:
        predictions = (val_errors >= threshold).astype(np.int32)
        alert_pct   = predictions.mean()
        print(f"\nNo labels.json in labels channel — alert%={alert_pct:.2%} (validation split)")
        print(f"METRIC_ALERT_PCT: {alert_pct:.6f}")

    np.save(outdir / "predictions.npy", predictions)

    elapsed = (time.time() - t0) / 60
    print(f"\nMETRIC_BEST_LOSS: {best_loss:.6f}")
    print(f"METRIC_THRESHOLD: {threshold:.6f}")
    print(f"Total training time: {elapsed:.1f} min")
    print(f"Artifacts saved → {outdir}")


if __name__ == "__main__":
    main()
