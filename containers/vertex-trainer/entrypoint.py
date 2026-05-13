"""Vertex CustomJob entrypoint shim.

Translates the Vertex contract (channel URIs as env vars + AIP_MODEL_DIR
output path) to the SageMaker contract the training script was written
for (/opt/ml/input/data/<channel>/ + /opt/ml/model + SM_* env vars), runs
the training script unmodified, then packages the model directory into
model.tar.gz and uploads it plus a metrics.json (parsed from stdout)
to AIP_MODEL_DIR — same shape SageMaker would write to its S3 output.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import urlparse

from google.cloud import storage


# ── Vertex → SageMaker channel mapping ────────────────────────────────────────

CHANNEL_DIRS = {
    "TRAIN":      "/opt/ml/input/data/train",
    "VALIDATION": "/opt/ml/input/data/validation",
    "LABELS":     "/opt/ml/input/data/labels",
}
SM_ENV_BY_CHANNEL = {
    "TRAIN":      "SM_CHANNEL_TRAIN",
    "VALIDATION": "SM_CHANNEL_VALIDATION",
    "LABELS":     "SM_CHANNEL_LABELS",
}
MODEL_DIR = Path("/opt/ml/model")


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "gs":
        raise ValueError(f"Expected gs:// URI, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _download_uri(client: storage.Client, gs_uri: str, dest_dir: Path) -> None:
    """Download a single GCS object (or all blobs under a prefix) into dest_dir."""
    bucket_name, key = _parse_gs_uri(gs_uri)
    bucket = client.bucket(bucket_name)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if gs_uri.endswith("/"):
        # Treat as prefix — download every blob under it.
        for blob in client.list_blobs(bucket_name, prefix=key):
            rel = Path(blob.name[len(key):])
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(target))
            print(f"[shim] {gs_uri}{rel} → {target}")
    else:
        blob = bucket.blob(key)
        target = dest_dir / Path(key).name
        blob.download_to_filename(str(target))
        print(f"[shim] {gs_uri} → {target}")


def _stage_channels(client: storage.Client) -> None:
    for channel, dest in CHANNEL_DIRS.items():
        env_key = f"INPUT_{channel}_URI"
        uri = os.environ.get(env_key)
        if not uri:
            print(f"[shim] {env_key} not set — skipping channel")
            continue
        _download_uri(client, uri, Path(dest))
        os.environ[SM_ENV_BY_CHANNEL[channel]] = dest


_HERE = Path(__file__).resolve().parent
_TRAINING_SCRIPT = _HERE / "sagemaker_rl" / "lstm_vae_train.py"


def _run_training(extra_args: list[str]) -> str:
    """Run the training script, tee its stdout to a buffer for metric parsing."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["SM_MODEL_DIR"] = str(MODEL_DIR)

    cmd = ["python", str(_TRAINING_SCRIPT), *extra_args]
    print(f"[shim] launching: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        captured.append(line)
    proc.wait()
    if proc.returncode != 0:
        sys.exit(proc.returncode)
    return "".join(captured)


_METRIC_RE = re.compile(r"^METRIC_([A-Z0-9_]+):\s*(-?[0-9.eE+-]+)\s*$", re.MULTILINE)


def _build_metrics(stdout_text: str) -> dict:
    """Extract METRIC_<NAME>: <value> lines and merge with config.json + threshold.json."""
    metrics: dict = {
        m.group(1).lower(): float(m.group(2))
        for m in _METRIC_RE.finditer(stdout_text)
    }

    for fname in ("config.json", "threshold.json", "mahalanobis_stats.json"):
        path = MODEL_DIR / fname
        if path.exists():
            try:
                metrics.setdefault(fname.replace(".json", ""), json.loads(path.read_text()))
            except json.JSONDecodeError:
                pass
    return metrics


def _tar_model_dir(tar_path: Path) -> None:
    with tarfile.open(tar_path, mode="w:gz") as tar:
        for entry in MODEL_DIR.iterdir():
            if entry.name == tar_path.name:
                continue
            tar.add(entry, arcname=entry.name)


def _upload_outputs(client: storage.Client, metrics: dict) -> None:
    output_dir = os.environ.get("AIP_MODEL_DIR")
    if not output_dir:
        print("[shim] AIP_MODEL_DIR unset — skipping upload (running locally?)")
        return

    bucket_name, key_prefix = _parse_gs_uri(output_dir)
    bucket = client.bucket(bucket_name)

    tar_path = MODEL_DIR / "model.tar.gz"
    _tar_model_dir(tar_path)

    metrics_path = MODEL_DIR / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2, default=str))

    for path in (tar_path, metrics_path):
        blob_name = f"{key_prefix.rstrip('/')}/{path.name}"
        bucket.blob(blob_name).upload_from_filename(str(path))
        print(f"[shim] uploaded {path} → gs://{bucket_name}/{blob_name}")


def main() -> None:
    extra_args = sys.argv[1:]
    client = storage.Client()
    _stage_channels(client)
    stdout_text = _run_training(extra_args)
    metrics = _build_metrics(stdout_text)
    _upload_outputs(client, metrics)
    print("[shim] done")


if __name__ == "__main__":
    main()
