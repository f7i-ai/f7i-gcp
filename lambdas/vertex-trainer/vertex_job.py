"""Vertex AI CustomJob launcher — mirror of the SageMaker training-job factory.

The training container (built separately, pushed to Artifact Registry) is
expected to read the per-channel ``gs://`` URIs from env vars and write its
final model + metrics to GCS at ``base_output_dir``. Hyperparameter contracts
match ``consumer.py`` so the existing scripts run unchanged once the channel-
mounting is translated by the container entrypoint.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Tuple

from google.cloud import aiplatform


log = logging.getLogger(__name__)


# ── Hyperparameters (verbatim from consumer.py) ───────────────────────────────

def lstm_vae_hyperparameters(sensor_id: str) -> Dict[str, str]:
    return {
        "sensor-id":      sensor_id,
        "seq-len":        "25",
        "hidden":         "64",
        "latent":         "16",
        "n-layers":       "2",
        "beta":           "0.1",
        "lr":             "0.001",
        "epochs":         "300",
        "patience":       "30",
        "batch-size":     "32",
        "sigma":          "3.0",
        "alert-win-days": "7",
    }


def rl_hyperparameters(sensor_id: str, mode: str = "unsupervised") -> Dict[str, str]:
    return {
        "sensor-id":          sensor_id,
        "mode":               mode,
        "max-episodes":       "500",
        "patience":           "50",
        "fresh":              "1",
        "exp-name":           "prod",
        "sequence_length":    "25",
        "input_size":         "6",
        "dynamic-lambda":     "1",
        "lambda-target":      "8000",
        "lambda-alpha":       "0.01",
        "lambda-min":         "0.1",
        "lambda-max":         "5.0",
        "smooth-k":           "7",
        "alert-win-days":     "7",
        "fp-cooloff-days":    "2",
        "vae-label-smooth-k": "3",
        "implicit-neg":       "1",
        "no-human-seed":      "1",
        "pre-event-only":     "0",
        "alert-cost":         "-0.5",
    }


# ── Submission ────────────────────────────────────────────────────────────────

def _hps_to_args(hps: Dict[str, str]) -> list[str]:
    """Render hyperparameters as ``--key value`` CLI args (argparse-friendly)."""
    args: list[str] = []
    for k, v in hps.items():
        args.extend([f"--{k}", str(v)])
    return args


def _submit_custom_job(
    *,
    project: str,
    location: str,
    job_name: str,
    image_uri: str,
    entry_script: str,
    hyperparameters: Dict[str, str],
    channel_uris: Dict[str, str],
    base_output_uri: str,
    machine_type: str,
    service_account: str,
    staging_bucket: str,
) -> str:
    """Submit a single-replica CustomJob and return its resource name."""
    aiplatform.init(
        project=project,
        location=location,
        staging_bucket=staging_bucket,
    )

    env = [
        {"name": "TRAINING_ENTRY_SCRIPT", "value": entry_script},
        {"name": "AIP_MODEL_DIR",         "value": f"{base_output_uri.rstrip('/')}/model/"},
    ]
    # Channel URIs as env vars so the container entrypoint can stage them into
    # the SageMaker-style /opt/ml/input/data/{channel} paths the scripts expect.
    for channel, uri in channel_uris.items():
        env.append({"name": f"INPUT_{channel.upper()}_URI", "value": uri})

    worker_pool_specs = [{
        "machine_spec":  {"machine_type": machine_type},
        "replica_count": 1,
        "container_spec": {
            "image_uri": image_uri,
            "args":      _hps_to_args(hyperparameters),
            "env":       env,
        },
    }]

    job = aiplatform.CustomJob(
        display_name=job_name,
        worker_pool_specs=worker_pool_specs,
        base_output_dir=base_output_uri,
        staging_bucket=staging_bucket,
    )
    job.submit(service_account=service_account)
    log.info("Submitted Vertex CustomJob %s (resource=%s)", job_name, job.resource_name)
    return job.resource_name


def launch_lstm_vae(
    *,
    project: str,
    location: str,
    sensor_id: str,
    channel_uris: Dict[str, str],
    output_bucket: str,
    image_uri: str,
    machine_type: str,
    service_account: str,
    staging_bucket: str,
) -> Tuple[str, str]:
    job_name = f"argus-unsup-{sensor_id}-{str(int(time.time()))[-8:]}"
    base_output = f"gs://{output_bucket}/output/{sensor_id}/{job_name}/"
    resource = _submit_custom_job(
        project=project,
        location=location,
        job_name=job_name,
        image_uri=image_uri,
        entry_script="lstm_vae_train.py",
        hyperparameters=lstm_vae_hyperparameters(sensor_id),
        channel_uris=channel_uris,
        base_output_uri=base_output,
        machine_type=machine_type,
        service_account=service_account,
        staging_bucket=staging_bucket,
    )
    return job_name, resource


def launch_rl(
    *,
    project: str,
    location: str,
    sensor_id: str,
    mode: str,
    channel_uris: Dict[str, str],
    output_bucket: str,
    image_uri: str,
    machine_type: str,
    service_account: str,
    staging_bucket: str,
) -> Tuple[str, str]:
    job_name = f"argus-semi-{sensor_id}-{str(int(time.time()))[-8:]}"
    base_output = f"gs://{output_bucket}/output/{sensor_id}/{job_name}/"
    resource = _submit_custom_job(
        project=project,
        location=location,
        job_name=job_name,
        image_uri=image_uri,
        entry_script="train_argus.py",
        hyperparameters=rl_hyperparameters(sensor_id, mode=mode),
        channel_uris=channel_uris,
        base_output_uri=base_output,
        machine_type=machine_type,
        service_account=service_account,
        staging_bucket=staging_bucket,
    )
    return job_name, resource
