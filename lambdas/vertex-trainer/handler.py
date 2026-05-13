"""vertex-trainer Lambda — AWS-side bridge to Vertex AI training.

Invoke contract (direct ``aws lambda invoke``):

    {
        "sensor_id":                  "...",                  # required
        "assetchart_table":           "assetchart-...-dev",   # required
        "sensor_event_history_table": "...-sensor-event-history-...",  # required
        "deployment_env":             "dev",                  # optional, default $DEPLOYMENT_ENV
        "algorithm":                  "auto" | "rl" | "lstm_vae"  # optional, default "auto"
    }

The handler:
    1. Reads sensor history from DynamoDB.
    2. Fetches Argus labels via the cached sensor-event-history + Bedrock fallback.
    3. Uploads train/val/test/full CSVs + placeholder llm_scores + labels.json to GCS.
    4. Submits a Vertex AI CustomJob and returns its resource name.

GCP authentication uses Workload Identity Federation — the Lambda execution
role is granted ``iam.workloadIdentityUser`` on a GCP service account via an
AWS-typed Workload Identity Pool. The ``google-auth`` library picks this up
automatically from a credentials JSON staged at ``GOOGLE_APPLICATION_CREDENTIALS``
(written into the package at build time by Terraform).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict


# ── GCP Workload Identity Federation bootstrap ────────────────────────────────
# Terraform renders the external-account config and passes it via env var;
# google-auth requires it on disk pointed to by GOOGLE_APPLICATION_CREDENTIALS.
_WIF_PATH = "/tmp/gcp_wif_config.json"
if "GCP_WIF_CONFIG_JSON" in os.environ and not os.path.exists(_WIF_PATH):
    with open(_WIF_PATH, "w") as _f:
        _f.write(os.environ["GCP_WIF_CONFIG_JSON"])
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _WIF_PATH)

from google.cloud import storage  # noqa: E402

import labels as labels_mod  # noqa: E402
import model_data  # noqa: E402
import vertex_job  # noqa: E402


logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)


def _require(event: Dict[str, Any], key: str) -> Any:
    if key not in event or event[key] in (None, ""):
        raise ValueError(f"Missing required field: {key!r}")
    return event[key]


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    log.info("Received event: %s", json.dumps(event))

    sensor_id           = _require(event, "sensor_id")
    asset_chart_table   = _require(event, "assetchart_table")
    sensor_history_tbl  = _require(event, "sensor_event_history_table")
    deployment_env      = event.get("deployment_env") or os.environ.get("DEPLOYMENT_ENV", "dev")
    algorithm_override  = event.get("algorithm", "auto")

    project        = os.environ["GCP_PROJECT_ID"]
    location       = os.environ["VERTEX_LOCATION"]
    staging_bucket = os.environ["GCS_STAGING_BUCKET"]
    output_bucket  = os.environ.get("GCS_OUTPUT_BUCKET", staging_bucket)
    image_uri      = os.environ["VERTEX_TRAINER_IMAGE"]
    machine_type   = os.environ.get("VERTEX_MACHINE_TYPE", "n1-standard-4")
    service_account = os.environ["VERTEX_TRAINER_SA"]

    # ── 1. Gather sensor data from DynamoDB ───────────────────────────────────
    sensor_rows = model_data.get_sensor_data(asset_chart_table, sensor_id)

    # ── 2. Fetch labels (decides supervised vs unsupervised) ──────────────────
    tp_dates, fp_dates = labels_mod.fetch_labels(
        sensor_id=sensor_id,
        deployment_env=deployment_env,
        sensor_event_history_table=sensor_history_tbl,
    )
    mode = "supervised" if tp_dates else "unsupervised"

    # ── 3. Upload everything to GCS ───────────────────────────────────────────
    gcs_client = storage.Client(project=project)
    channel_uris = model_data.split_and_upload(
        gcs_client=gcs_client,
        sensor_data=sensor_rows,
        sensor_id=sensor_id,
        gcs_bucket=staging_bucket,
    )
    channel_uris["labels"] = model_data.upload_labels(
        gcs_client=gcs_client,
        gcs_bucket=staging_bucket,
        gcs_prefix="",
        sensor_id=sensor_id,
        mode=mode,
        tp_dates=tp_dates,
        fp_dates=fp_dates,
    )

    # ── 4. Launch the Vertex CustomJob ────────────────────────────────────────
    if algorithm_override == "auto":
        algorithm = "lstm_vae" if mode == "unsupervised" else "rl"
    else:
        algorithm = algorithm_override

    if algorithm == "lstm_vae":
        job_name, resource = vertex_job.launch_lstm_vae(
            project=project,
            location=location,
            sensor_id=sensor_id,
            channel_uris=channel_uris,
            output_bucket=output_bucket,
            image_uri=image_uri,
            machine_type=machine_type,
            service_account=service_account,
            staging_bucket=f"gs://{staging_bucket}",
        )
    elif algorithm == "rl":
        job_name, resource = vertex_job.launch_rl(
            project=project,
            location=location,
            sensor_id=sensor_id,
            mode=mode,
            channel_uris=channel_uris,
            output_bucket=output_bucket,
            image_uri=image_uri,
            machine_type=machine_type,
            service_account=service_account,
            staging_bucket=f"gs://{staging_bucket}",
        )
    else:
        raise ValueError(f"Unknown algorithm: {algorithm!r}")

    return {
        "status":      "submitted",
        "sensor_id":   sensor_id,
        "algorithm":   algorithm,
        "mode":        mode,
        "job_name":    job_name,
        "vertex_job":  resource,
        "channel_uris": channel_uris,
    }
