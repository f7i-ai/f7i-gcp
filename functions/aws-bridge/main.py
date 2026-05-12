"""
aws-bridge Cloud Function (dev/test)
-------------------------------------
Invoked via HTTP. Uploads a small payload to S3 and publishes an event to
EventBridge using temporary AWS credentials obtained via Google OIDC — no
static AWS keys required.

Env vars (set by Terraform):
    AWS_ROLE_ARN         – IAM role to assume
    AWS_S3_BUCKET        – bucket name for test upload
    AWS_EVENTBRIDGE_BUS  – EventBridge bus name
    AWS_REGION           – AWS region (default ap-southeast-2)

Test call:
    curl -X POST <FUNCTION_URL> \
         -H 'Content-Type: application/json' \
         -d '{"message": "hello from GCP"}'
"""

import datetime
import json
import os
import urllib.request
import urllib.parse

import boto3
import functions_framework
import google.auth.transport.requests
import google.oauth2.id_token


# ── OIDC helpers ──────────────────────────────────────────────────────────────

def _get_google_oidc_token() -> str:
    """Return a Google-signed OIDC token for this runtime's service account."""
    audience = "sts.amazonaws.com"
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, audience)


def _assume_aws_role(role_arn: str, region: str) -> dict:
    """Exchange the Google OIDC token for temporary AWS credentials."""
    oidc_token = _get_google_oidc_token()

    sts = boto3.client("sts", region_name=region)
    response = sts.assume_role_with_web_identity(
        RoleArn=role_arn,
        RoleSessionName="gcp-aws-bridge",
        WebIdentityToken=oidc_token,
        DurationSeconds=900,  # 15 min — enough for a single invocation
    )
    return response["Credentials"]


def _aws_clients(creds: dict, region: str) -> tuple:
    """Build boto3 S3 + EventBridge clients from temporary credentials."""
    kwargs = dict(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    return boto3.client("s3", **kwargs), boto3.client("events", **kwargs)


# ── Main handler ──────────────────────────────────────────────────────────────

@functions_framework.http
def handle(request):
    role_arn   = os.environ["AWS_ROLE_ARN"]
    bucket     = os.environ["AWS_S3_BUCKET"]
    bus_name   = os.environ.get("AWS_EVENTBRIDGE_BUS", "default")
    region     = os.environ.get("AWS_REGION", "ap-southeast-2")

    body = request.get_json(silent=True) or {}
    message = body.get("message", "hello from GCP aws-bridge fn")
    now = datetime.datetime.utcnow().isoformat()

    # 1. Get temporary AWS creds via Google OIDC
    creds = _assume_aws_role(role_arn, region)
    s3, events = _aws_clients(creds, region)

    # 2. Upload a small JSON file to S3
    key = f"bridge-test/{now}.json"
    payload = json.dumps({"message": message, "timestamp": now, "source": "gcp-aws-bridge"})
    s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json")

    # 3. Publish an event to EventBridge
    #    (S3 will also fire its own notification via the bucket notification config)
    events.put_events(
        Entries=[{
            "Source":       "gcp.aws-bridge",
            "DetailType":   "BridgeTestEvent",
            "Detail":       json.dumps({"s3_key": key, "message": message}),
            "EventBusName": bus_name,
        }]
    )

    result = {
        "status":  "ok",
        "s3_key":  key,
        "bus":     bus_name,
        "message": message,
    }
    return (json.dumps(result), 200, {"Content-Type": "application/json"})
