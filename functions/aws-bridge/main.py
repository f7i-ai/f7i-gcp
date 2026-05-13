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
import base64
import os
import re
import urllib.parse

import boto3
import functions_framework
import requests


# ── OIDC helpers ──────────────────────────────────────────────────────────────

def _get_google_oidc_token(audience: str) -> str:
    """Mint a Google ID token via the GCP metadata server (Compute Engine identity endpoint).

    This is the standard path for Cloud Functions / Cloud Run — the token is issued
    directly for the workload's service account and verified by Google's OIDC JWKS.
    """
    import google.auth.transport.requests
    from google.auth import compute_engine

    request = google.auth.transport.requests.Request()
    credentials = compute_engine.IDTokenCredentials(
        request,
        target_audience=audience,
        use_metadata_identity_endpoint=True,
    )
    credentials.refresh(request)
    return credentials.token


def _sts_xml_field(xml: str, tag: str) -> str | None:
    m = re.search(f"<{tag}>([^<]*)</{tag}>", xml)
    return m.group(1) if m else None


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

def _decode_jwt_claims(token: str) -> dict:
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p.encode("ascii")))
    except Exception as exc:
        return {"decode_error": str(exc)}


@functions_framework.http
def handle(request):
    role_arn = os.environ["AWS_ROLE_ARN"]
    bucket   = os.environ["AWS_S3_BUCKET"]
    bus_name = os.environ.get("AWS_EVENTBRIDGE_BUS", "default")
    region   = os.environ.get("AWS_REGION", "ap-southeast-2")

    body    = request.get_json(silent=True) or {}
    message = body.get("message", "hello from GCP aws-bridge fn")
    now     = datetime.datetime.utcnow().isoformat()

    # ── Step 1: mint Google OIDC token and expose its claims ──────────────────
    try:
        oidc_token  = _get_google_oidc_token("https://sts.amazonaws.com")
        jwt_claims  = _decode_jwt_claims(oidc_token)
        token_error = None
    except Exception as exc:
        return (json.dumps({"step": "mint_token", "error": str(exc)}),
                500, {"Content-Type": "application/json"})

    # ── Step 2: call STS and expose the raw response ──────────────────────────
    sts_body = urllib.parse.urlencode({
        "Action": "AssumeRoleWithWebIdentity",
        "Version": "2011-06-15",
        "RoleArn": role_arn,
        "RoleSessionName": "gcp-aws-bridge",
        "WebIdentityToken": oidc_token,
        "DurationSeconds": "900",
    })
    sts_resp = requests.post(
        "https://sts.amazonaws.com/",
        data=sts_body.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        timeout=60,
    )
    sts_text = sts_resp.text
    err_code = _sts_xml_field(sts_text, "Code")
    err_msg  = _sts_xml_field(sts_text, "Message")

    if err_code or "<ErrorResponse" in sts_text:
        # Return full diagnostic payload — not a 500 — so curl shows it
        return (json.dumps({
            "step":       "assume_role",
            "sts_status": sts_resp.status_code,
            "sts_error":  err_code,
            "sts_message": err_msg,
            "sts_raw":    sts_text[:2000],
            "jwt_iss":    jwt_claims.get("iss"),
            "jwt_aud":    jwt_claims.get("aud"),
            "jwt_sub":    jwt_claims.get("sub"),
            "jwt_email":  jwt_claims.get("email"),
            "role_arn":   role_arn,
        }), 200, {"Content-Type": "application/json"})

    ak = _sts_xml_field(sts_text, "AccessKeyId")
    sk = _sts_xml_field(sts_text, "SecretAccessKey")
    st = _sts_xml_field(sts_text, "SessionToken")
    if not (ak and sk and st):
        return (json.dumps({"step": "parse_sts", "sts_raw": sts_text[:2000]}),
                500, {"Content-Type": "application/json"})

    creds = {"AccessKeyId": ak, "SecretAccessKey": sk, "SessionToken": st}
    s3, events = _aws_clients(creds, region)

    # ── Step 3: S3 upload ─────────────────────────────────────────────────────
    key = f"bridge-test/{now}.json"
    payload = json.dumps({"message": message, "timestamp": now, "source": "gcp-aws-bridge"})
    s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json")

    # ── Step 4: EventBridge ───────────────────────────────────────────────────
    events.put_events(Entries=[{
        "Source":       "gcp.aws-bridge",
        "DetailType":   "BridgeTestEvent",
        "Detail":       json.dumps({"s3_key": key, "message": message}),
        "EventBusName": bus_name,
    }])

    return (json.dumps({"status": "ok", "s3_key": key, "bus": bus_name, "message": message}),
            200, {"Content-Type": "application/json"})
