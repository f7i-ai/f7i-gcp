"""
aws-bridge Cloud Function
--------------------------
Uploads a small payload to S3 and publishes an event to EventBridge using
temporary AWS credentials obtained via Google OIDC — no static AWS keys.

Auth flow:
  1. GCP metadata server mints an ID token for this function's service account.
  2. AWS STS validates the token via the accounts.google.com OIDC provider.
     AWS substitutes the JWT `azp` claim for `aud` when both are present;
     Google metadata-server tokens set `azp` to the SA's numeric unique ID,
     so the OIDC provider client_id_list and the role trust condition both
     match on that numeric ID (not the audience URL).
  3. AssumeRoleWithWebIdentity returns temporary credentials.

Env vars (set by Terraform):
    AWS_ROLE_ARN         – IAM role to assume
    AWS_S3_BUCKET        – bucket name for the test upload
    AWS_EVENTBRIDGE_BUS  – EventBridge bus name
    AWS_REGION           – AWS region (default ap-southeast-2)

Test call:
    curl -X POST <FUNCTION_URL> \
         -H 'Content-Type: application/json' \
         -d '{"message": "hello from GCP"}'
"""

import datetime
import json
import logging
import os
import urllib.parse

import boto3
import functions_framework
import requests

logger = logging.getLogger(__name__)


def _get_google_oidc_token(audience: str) -> str:
    """Mint a Google ID token via the GCP metadata server."""
    import google.auth.transport.requests
    from google.auth import compute_engine

    req = google.auth.transport.requests.Request()
    creds = compute_engine.IDTokenCredentials(
        req,
        target_audience=audience,
        use_metadata_identity_endpoint=True,
    )
    creds.refresh(req)
    return creds.token


def _assume_aws_role(role_arn: str) -> dict:
    """Exchange the Google OIDC token for temporary AWS credentials."""
    logger.info("Minting Google OIDC token")
    token = _get_google_oidc_token("https://sts.amazonaws.com")
    logger.info("Calling STS AssumeRoleWithWebIdentity for role %s", role_arn)
    body = urllib.parse.urlencode({
        "Action":            "AssumeRoleWithWebIdentity",
        "Version":           "2011-06-15",
        "RoleArn":           role_arn,
        "RoleSessionName":   "gcp-aws-bridge",
        "WebIdentityToken":  token,
        "DurationSeconds":   "900",
    })
    r = requests.post(
        "https://sts.amazonaws.com/",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        timeout=60,
    )
    if not r.ok or "<ErrorResponse" in r.text:
        logger.error("STS error (HTTP %s): %s", r.status_code, r.text[:800])
        raise RuntimeError(f"STS error (HTTP {r.status_code}): {r.text[:800]}")

    def _field(tag: str) -> str:
        import re
        m = re.search(f"<{tag}>([^<]*)</{tag}>", r.text)
        if not m:
            raise RuntimeError(f"Missing {tag} in STS response")
        return m.group(1)

    return {
        "AccessKeyId":     _field("AccessKeyId"),
        "SecretAccessKey": _field("SecretAccessKey"),
        "SessionToken":    _field("SessionToken"),
    }


@functions_framework.http
def handle(request):
    role_arn = os.environ["AWS_ROLE_ARN"]
    bucket   = os.environ["AWS_S3_BUCKET"]
    bus_name = os.environ.get("AWS_EVENTBRIDGE_BUS", "default")
    region   = os.environ.get("AWS_REGION", "ap-southeast-2")

    body    = request.get_json(silent=True) or {}
    message = body.get("message", "hello from GCP aws-bridge fn")
    now     = datetime.datetime.utcnow().isoformat()

    creds = _assume_aws_role(role_arn)
    logger.info("STS credentials obtained")
    kw = dict(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    s3     = boto3.client("s3",     **kw)
    events = boto3.client("events", **kw)

    key     = f"bridge-test/{now}.json"
    payload = json.dumps({"message": message, "timestamp": now, "source": "gcp-aws-bridge"})
    logger.info("Uploading to S3 bucket=%s key=%s", bucket, key)
    s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json")

    logger.info("Publishing to EventBridge bus=%s", bus_name)
    events.put_events(Entries=[{
        "Source":       "gcp.aws-bridge",
        "DetailType":   "BridgeTestEvent",
        "Detail":       json.dumps({"s3_key": key, "message": message}),
        "EventBusName": bus_name,
    }])

    logger.info("Done: s3_key=%s", key)
    return (
        json.dumps({"status": "ok", "s3_key": key, "bus": bus_name, "message": message}),
        200,
        {"Content-Type": "application/json"},
    )
