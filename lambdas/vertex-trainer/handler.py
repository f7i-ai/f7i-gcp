"""vertex-trainer Lambda — generate a synthetic dataset, upload to GCS,
and submit a Vertex AI CustomJob that runs the real lstm_vae_train.py.

This is a prototype: the dataset is generated in-memory (~250 rows of
synthetic 5-channel sensor signal — enough to clear the lstm_vae script's
seq_len=25 windowing and produce a real model). All the real I/O contract
(channel layout, hyperparameters, output shape) matches what the AWS-side
SageMaker training job uses today, so the resulting model.tar.gz is
shaped identically once it lands in S3.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import random
import time
from typing import Any, Dict, List


# ── GCP Workload Identity Federation bootstrap ────────────────────────────────
_WIF_PATH = "/tmp/gcp_wif_config.json"
if "GCP_WIF_CONFIG_JSON" in os.environ and not os.path.exists(_WIF_PATH):
    with open(_WIF_PATH, "w") as _f:
        _f.write(os.environ["GCP_WIF_CONFIG_JSON"])
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _WIF_PATH)

from google.cloud import aiplatform, storage  # noqa: E402


logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)


SENSOR_COLS = [
    "temperature",
    "velocity_total_crest",
    "velocity_x_rms",
    "velocity_y_rms",
    "velocity_z_rms",
]


# ── Synthetic dataset generation ──────────────────────────────────────────────

def _generate_csv(n_rows: int, sensor_id: str, seed: int = 0) -> str:
    """Produce a CSV string with the same schema lstm_vae_train.py expects.

    Signal model: a slow sinusoidal baseline + Gaussian noise per channel,
    with each channel offset/scaled to mimic the real sensor magnitude
    ranges (so the MinMaxScaler in training has something non-trivial to fit).
    """
    rng = random.Random(seed + hash(sensor_id) % 10_000)
    base_ts = 1_700_000_000  # arbitrary epoch seconds
    rows: List[str] = ["timestamp," + ",".join(SENSOR_COLS)]

    for i in range(n_rows):
        # 1-min sample cadence
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base_ts + i * 60))
        phase = i / 25.0  # ~25-step period
        temp     = 22.0 + 1.5 * math.sin(phase)             + rng.gauss(0, 0.1)
        v_crest  = 1.40 + 0.05 * math.sin(phase + 0.3)      + rng.gauss(0, 0.02)
        v_x      = 0.30 + 0.02 * math.sin(phase + 0.6)      + rng.gauss(0, 0.005)
        v_y      = 0.40 + 0.03 * math.sin(phase + 0.9)      + rng.gauss(0, 0.005)
        v_z      = 0.50 + 0.02 * math.sin(phase + 1.2)      + rng.gauss(0, 0.005)
        rows.append(f"{ts_iso},{temp:.4f},{v_crest:.4f},{v_x:.4f},{v_y:.4f},{v_z:.4f}")

    return "\n".join(rows) + "\n"


# ── GCS upload ────────────────────────────────────────────────────────────────

def _upload(client: storage.Client, bucket: str, key: str, data: bytes, content_type: str) -> str:
    client.bucket(bucket).blob(key).upload_from_string(data, content_type=content_type)
    return f"gs://{bucket}/{key}"


# ── Handler ───────────────────────────────────────────────────────────────────

_DEFAULT_HPS = {
    "sensor-id":      "proto-sensor",
    "seq-len":        "25",
    "hidden":         "64",
    "latent":         "16",
    "n-layers":       "2",
    "beta":           "0.1",
    "lr":             "0.001",
    "epochs":         "30",     # short for proto — real jobs use 300
    "patience":       "10",
    "batch-size":     "32",
    "sigma":          "3.0",
    "alert-win-days": "7",
}


def _hps_to_args(hps: Dict[str, str]) -> List[str]:
    args: List[str] = []
    for k, v in hps.items():
        args.extend([f"--{k}", str(v)])
    return args


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    log.info("Received event: %s", json.dumps(event))

    project        = os.environ["GCP_PROJECT_ID"]
    location       = os.environ["VERTEX_LOCATION"]
    staging_bucket = os.environ["GCS_STAGING_BUCKET"]
    image_uri      = os.environ["VERTEX_TRAINER_IMAGE"]
    machine_type   = os.environ.get("VERTEX_MACHINE_TYPE", "n1-standard-4")
    service_account = os.environ["VERTEX_TRAINER_SA"]

    sensor_id   = event.get("sensor_id", "proto-sensor")
    n_rows      = int(event.get("n_rows", 250))
    hps         = {**_DEFAULT_HPS, **event.get("hyperparameters", {}), "sensor-id": sensor_id}

    run_id = str(int(time.time()))
    base = f"jobs/{run_id}"

    # ── 1. Generate train + validation CSVs + empty labels.json ──────────────
    train_csv      = _generate_csv(n_rows,           sensor_id, seed=1)
    validation_csv = _generate_csv(max(n_rows // 4, 60), sensor_id, seed=2)
    labels_json    = json.dumps({"sensor_id": sensor_id, "mode": "unsupervised",
                                 "tp_dates": [], "fp_dates": []})

    gcs = storage.Client(project=project)
    train_uri      = _upload(gcs, staging_bucket, f"{base}/train/train.csv", train_csv.encode(),      "text/csv")
    validation_uri = _upload(gcs, staging_bucket, f"{base}/validation/validation.csv", validation_csv.encode(), "text/csv")
    labels_uri     = _upload(gcs, staging_bucket, f"{base}/labels/labels.json", labels_json.encode(), "application/json")
    log.info("Channels uploaded: train=%s validation=%s labels=%s",
             train_uri, validation_uri, labels_uri)

    # ── 2. Submit Vertex CustomJob ────────────────────────────────────────────
    aiplatform.init(project=project, location=location, staging_bucket=f"gs://{staging_bucket}")

    job_name    = f"vertex-trainer-{sensor_id}-{run_id[-8:]}"
    base_output = f"gs://{staging_bucket}/output/{job_name}/"

    job = aiplatform.CustomJob(
        display_name=job_name,
        worker_pool_specs=[{
            "machine_spec":  {"machine_type": machine_type},
            "replica_count": 1,
            "container_spec": {
                "image_uri": image_uri,
                "args":      _hps_to_args(hps),
                "env": [
                    {"name": "INPUT_TRAIN_URI",      "value": train_uri},
                    {"name": "INPUT_VALIDATION_URI", "value": validation_uri},
                    {"name": "INPUT_LABELS_URI",     "value": labels_uri},
                ],
            },
        }],
        base_output_dir=base_output,
        staging_bucket=f"gs://{staging_bucket}",
    )
    job.submit(service_account=service_account)
    log.info("Submitted CustomJob %s (resource=%s)", job_name, job.resource_name)

    return {
        "status":      "submitted",
        "job_name":    job_name,
        "vertex_job":  job.resource_name,
        "channels":    {"train": train_uri, "validation": validation_uri, "labels": labels_uri},
        "output_uri":  base_output,
        "image_uri":   image_uri,
        "n_rows":      n_rows,
        "hyperparameters": hps,
    }
