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

import boto3
from botocore import UNSIGNED
from botocore.config import Config
import functions_framework


# ── OIDC helpers ──────────────────────────────────────────────────────────────

def _get_google_oidc_token(audience: str) -> str:
    """Return a Google OIDC ID token via generateIdToken.

    For AWS, aud must be **sts.amazonaws.com** (register that client_id on the Google
    IAM OIDC provider). IAM trust policies must include StringEquals on
    accounts.google.com:aud — see AWS error “requires a StringEquals condition on an application id”.
    """
    import google.auth
    import google.auth.transport.requests
    import requests

    auth_req = google.auth.transport.requests.Request()
    creds, _ = google.auth.default()
    creds.refresh(auth_req)
    access_token = creds.token

    sa_email = getattr(creds, "service_account_email", None)
    if not sa_email:
        raise RuntimeError("Expected GCP service account credentials (missing service_account_email)")

    url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa_email}:generateIdToken"
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"audience": audience, "includeEmail": False},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"generateIdToken HTTP {r.status_code}: {r.text[:800]}")
    return r.json()["token"]


def _assume_aws_role(role_arn: str, region: str) -> dict:
    """Exchange the Google OIDC token for temporary AWS credentials."""
    oidc_token = _get_google_oidc_token("sts.amazonaws.com")

    # AssumeRoleWithWebIdentity is unsigned. Pin global STS and avoid regional sts.<region>.amazonaws.com,
    # which fails this Google OIDC flow with AccessDenied in practice.
    sts = boto3.client(
        "sts",
        region_name="us-east-1",
        endpoint_url="https://sts.amazonaws.com",
        config=Config(signature_version=UNSIGNED),
    )
    response = sts.assume_role_with_web_identity(
        RoleArn=role_arn,
        RoleSessionName="gcp-aws-bridge",
        WebIdentityToken=oidc_token,
        DurationSeconds=900,
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
