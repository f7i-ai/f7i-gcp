"""vertex-completion-bridge — Vertex CustomJob terminal-state → AWS.

For each terminal-state Vertex CustomJob:

  1. Pull the job ID out of the log entry that fired the Pub/Sub trigger.
  2. Describe the job for authoritative state + the output URI Vertex wrote.
  3. Read metrics.json from the output URI (written by the training shim).
  4. Copy model.tar.gz from the output URI into an AWS S3 bucket — same
     shape SageMaker would have produced for the predict pipeline.
  5. Mint a Google OIDC token, exchange for STS creds, PutEvents to
     EventBridge with state + metrics + s3_model_uri in the detail.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Optional, Tuple
from urllib.parse import urlparse

import boto3
import functions_framework
import google.auth.transport.requests
from google.auth import compute_engine
from google.cloud import aiplatform_v1, storage


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


_TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}


# ── AWS auth (OIDC → STS → boto3 Session) ─────────────────────────────────────

def _mint_google_oidc_token(audience: str) -> str:
    request = google.auth.transport.requests.Request()
    credentials = compute_engine.IDTokenCredentials(
        request,
        target_audience=audience,
        use_metadata_identity_endpoint=True,
    )
    credentials.refresh(request)
    return credentials.token


def _aws_session() -> boto3.Session:
    role_arn = os.environ["AWS_ROLE_ARN"]
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    token = _mint_google_oidc_token("https://sts.amazonaws.com")
    sts = boto3.client("sts", region_name=region)
    resp = sts.assume_role_with_web_identity(
        RoleArn=role_arn,
        RoleSessionName="vertex-completion-bridge",
        WebIdentityToken=token,
        DurationSeconds=3600,
    )
    creds = resp["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


# ── GCS helpers ───────────────────────────────────────────────────────────────

def _parse_gs_uri(uri: str) -> Tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "gs":
        raise ValueError(f"Expected gs:// URI, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _read_metrics(gcs: storage.Client, output_uri: str) -> Optional[dict]:
    bucket_name, prefix = _parse_gs_uri(output_uri.rstrip("/"))
    blob = gcs.bucket(bucket_name).blob(f"{prefix}/metrics.json")
    if not blob.exists():
        log.info("No metrics.json at %s/metrics.json", output_uri)
        return None
    return json.loads(blob.download_as_text())


def _copy_model_to_s3(
    gcs: storage.Client,
    s3_client,
    output_uri: str,
    bucket: str,
    prefix: str,
    job_id: str,
) -> Optional[str]:
    """Stream model.tar.gz from GCS to S3 in chunks. Returns the S3 URI."""
    bucket_name, gcs_prefix = _parse_gs_uri(output_uri.rstrip("/"))
    blob = gcs.bucket(bucket_name).blob(f"{gcs_prefix}/model.tar.gz")
    if not blob.exists():
        log.info("No model.tar.gz at %s/model.tar.gz", output_uri)
        return None

    s3_key = f"{prefix.rstrip('/')}/{job_id}/model.tar.gz"
    log.info("Streaming gs://%s/%s/model.tar.gz → s3://%s/%s",
             bucket_name, gcs_prefix, bucket, s3_key)
    # Download to memory — model.tar.gz from a small lstm-vae run is ~MB-scale.
    data = blob.download_as_bytes()
    s3_client.put_object(Bucket=bucket, Key=s3_key, Body=data,
                         ContentType="application/gzip")
    return f"s3://{bucket}/{s3_key}"


# ── Vertex job lookup ─────────────────────────────────────────────────────────

def _extract_job_id(log_entry: dict) -> Optional[str]:
    labels = (log_entry.get("resource") or {}).get("labels") or {}
    job_id = labels.get("job_id") or labels.get("resource_id")
    if job_id:
        return str(job_id)
    resource_name = (log_entry.get("protoPayload") or {}).get("resourceName") or ""
    if "/customJobs/" in resource_name:
        return resource_name.rsplit("/customJobs/", 1)[1].split("/")[0]
    return None


def _describe_job(project: str, location: str, job_id: str):
    client = aiplatform_v1.JobServiceClient(
        client_options={"api_endpoint": f"{location}-aiplatform.googleapis.com"}
    )
    return client.get_custom_job(
        name=f"projects/{project}/locations/{location}/customJobs/{job_id}"
    )


# ── Handler ───────────────────────────────────────────────────────────────────

@functions_framework.cloud_event
def handle(cloud_event):
    raw = cloud_event.data["message"]["data"]
    decoded = base64.b64decode(raw).decode("utf-8")
    try:
        log_entry = json.loads(decoded)
    except json.JSONDecodeError:
        log.warning("Pub/Sub payload was not JSON: %r", decoded[:200])
        return

    project = os.environ["GCP_PROJECT_ID"]
    location = os.environ.get("VERTEX_LOCATION", "australia-southeast1")
    s3_bucket = os.environ.get("AWS_MODEL_S3_BUCKET")
    s3_prefix = os.environ.get("AWS_MODEL_S3_PREFIX", "vertex-trainer")

    job_id = _extract_job_id(log_entry)
    if not job_id:
        log.info("No job_id in log entry — ignoring")
        return

    try:
        job = _describe_job(project, location, job_id)
    except Exception as exc:
        log.error("Failed to describe job %s: %s", job_id, exc)
        return

    state = aiplatform_v1.JobState(job.state).name
    if state not in _TERMINAL_STATES:
        log.info("Job %s state=%s is non-terminal — ignoring", job_id, state)
        return

    end_time    = job.end_time.isoformat()    if job.end_time    else None
    create_time = job.create_time.isoformat() if job.create_time else None
    output_uri  = (
        job.job_spec.base_output_directory.output_uri_prefix
        if job.job_spec and job.job_spec.base_output_directory
        else None
    )
    error_message = job.error.message if job.error and job.error.message else None

    # Vertex writes outputs to <base_output_dir>/model/ when AIP_MODEL_DIR is used.
    model_output_uri = f"{output_uri.rstrip('/')}/model" if output_uri else None

    gcs = storage.Client(project=project)
    metrics: Optional[dict] = None
    s3_model_uri: Optional[str] = None

    if state == "JOB_STATE_SUCCEEDED" and model_output_uri and s3_bucket:
        try:
            metrics = _read_metrics(gcs, model_output_uri)
        except Exception as exc:
            log.error("Failed to read metrics: %s", exc)
        try:
            s3 = _aws_session().client("s3")
            s3_model_uri = _copy_model_to_s3(
                gcs, s3, model_output_uri, s3_bucket, s3_prefix, job_id,
            )
        except Exception as exc:
            log.error("Failed to copy model to S3: %s", exc)

    event_detail = {
        "job_name":      job.display_name,
        "resource_name": job.name,
        "job_id":        job_id,
        "state":         state,
        "create_time":   create_time,
        "end_time":      end_time,
        "output_uri":    output_uri,
        "model_uri":     model_output_uri,
        "s3_model_uri":  s3_model_uri,
        "metrics":       metrics,
        "error":         error_message,
    }

    log.info("Forwarding to EventBridge: %s",
             json.dumps({k: v for k, v in event_detail.items() if k != "metrics"}))

    events = _aws_session().client("events")
    resp = events.put_events(Entries=[{
        "Source":       "gcp.vertex-ai",
        "DetailType":   "VertexTrainingJobStateChange",
        "Detail":       json.dumps(event_detail),
        "EventBusName": os.environ["AWS_EVENTBRIDGE_BUS"],
    }])

    if resp.get("FailedEntryCount", 0) > 0:
        log.error("EventBridge rejected: %s", resp)
    else:
        log.info("EventBridge accepted (event_id=%s)", resp["Entries"][0].get("EventId"))
