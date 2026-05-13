"""Argus training label fetcher — port of ``label_fetcher.py``.

Same flow: query sensor-event-history for cached labels; for unresolved
notifications, fetch Notification + Feedback rows and classify via Claude
Haiku on Bedrock. Writes resolved rows back to history so subsequent runs
hit the cache.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import boto3
import pandas as pd
from boto3.dynamodb.conditions import Key


log = logging.getLogger(__name__)


_BEDROCK_MODEL_ID = "us.anthropic.claude-3-haiku-20240307-v1:0"

_CLASSIFY_SYSTEM = """\
You are a maintenance expert classifying industrial sensor alert feedback.

Rules:
- The structured fields (failureMode, feedbackType) are more reliable than free-text comments.
- If failureMode names a real mechanical failure (e.g. Cavitation, Blockage, Misalignment,
  Corrosion, Looseness, Bearing fault, Overheating, Leakage) -> tp, regardless of the comment.
- If failureMode or feedbackType indicates no fault (e.g. "No failure detected", "False Positive",
  "Normal", "No Failure") -> fp.
- Only return skip if there is genuinely no usable information at all.
- Answer with exactly one word: tp, fp, or skip."""

_CLASSIFY_PROMPT = """\
An industrial sensor fired an alert. An operator reviewed it and submitted feedback.

{fields}
Was this alert a genuine machine failure (tp), a false alarm (fp), \
or is there insufficient information to decide (skip)?

Reply with exactly one of: tp  fp  skip"""


def _region() -> str:
    return (
        os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "ap-southeast-2"
    )


_ssm = None
_dynamodb = None
_bedrock = None


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm", region_name=_region())
    return _ssm


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_region())
    return _dynamodb


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
    return _bedrock


def _get_source_table_names(deployment_env: str) -> Tuple[str, str]:
    notif = f"/f7i/{deployment_env}/argus/notification-table-name"
    feedb = f"/f7i/{deployment_env}/argus/feedback-table-name"
    resp = _get_ssm().get_parameters(Names=[notif, feedb], WithDecryption=False)
    by_name = {p["Name"]: p["Value"] for p in resp["Parameters"]}
    missing = [n for n in (notif, feedb) if n not in by_name]
    if missing:
        raise RuntimeError(f"SSM parameters not found: {missing}")
    return by_name[notif], by_name[feedb]


def _query_notifications(table, sensor_id: str) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {
        "IndexName": "bySensorID",
        "KeyConditionExpression": Key("sensorID").eq(sensor_id),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _query_history(history_table, sensor_id: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    kwargs: dict = {"KeyConditionExpression": Key("sensor_id").eq(sensor_id)}
    while True:
        resp = history_table.query(**kwargs)
        for row in resp.get("Items", []):
            ts = row.get("event_ts")
            if ts:
                rows[ts] = row
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return rows


def _write_history(
    history_table, sensor_id: str, event_ts: str,
    label: str, label_source: str,
    notification: dict, feedback: Optional[dict],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    item: dict = {
        "sensor_id":    sensor_id,
        "event_ts":     event_ts,
        "label":        label,
        "label_source": label_source,
        "alert_type":   str(notification.get("type",        "")),
        "content":      str(notification.get("content",     "")),
        "asset_name":   str(notification.get("assetName",   "")),
        "site_name":    str(notification.get("siteName",    "")),
        "company_name": str(notification.get("companyName", "")),
        "classified_at": now,
    }
    if feedback:
        item["failure_mode"] = str(feedback.get("failureMode") or "")
    if label_source == "llm":
        item["llm_model"] = _BEDROCK_MODEL_ID
    history_table.put_item(Item=item)


def _classify_with_llm(feedback: dict) -> Tuple[str, str]:
    feedback_type = str(feedback.get("feedbackType") or "").strip()
    failure_mode  = str(feedback.get("failureMode")  or "").strip()
    comment       = str(feedback.get("comment")      or "").strip()

    if feedback_type.upper() == "FALSE_POSITIVE":
        return "fp", "feedback_type"

    if not feedback_type and not failure_mode and not comment:
        return "skip", "llm"

    field_lines = []
    if feedback_type:
        field_lines.append(f"feedbackType: {feedback_type}")
    if failure_mode:
        field_lines.append(f"failureMode: {failure_mode}")
    if comment and comment.lower() not in ("no comment provided.", "no comment", ""):
        field_lines.append(f"comment: {comment}")
    fields = "\n".join(field_lines) + "\n\n" if field_lines else ""

    try:
        resp = _get_bedrock().invoke_model(
            modelId=_BEDROCK_MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 5,
                "system": _CLASSIFY_SYSTEM,
                "messages": [{"role": "user", "content": _CLASSIFY_PROMPT.format(fields=fields)}],
            }),
            contentType="application/json",
            accept="application/json",
        )
        body = json.loads(resp["body"].read())
        answer = body["content"][0]["text"].strip().lower().split()[0]
        if answer in ("tp", "fp", "skip"):
            return answer, "llm"
        log.warning("LLM returned unexpected token %r — skip", answer)
        return "skip", "llm"
    except Exception as exc:
        log.warning("LLM classify failed (%s) — skip", exc)
        return "skip", "llm"


def fetch_labels(
    sensor_id: str,
    deployment_env: str,
    sensor_event_history_table: str,
) -> Tuple[List[str], List[str]]:
    """Return (tp_dates_iso, fp_dates_iso) for the given sensor."""
    ddb = _get_dynamodb()
    history_table = ddb.Table(sensor_event_history_table) if sensor_event_history_table else None

    cached: dict[str, dict] = {}
    if history_table:
        try:
            cached = _query_history(history_table, sensor_id)
            log.info("labels: %d cached history rows for sensor %s", len(cached), sensor_id)
        except Exception as exc:
            log.warning("labels: history query failed (%s) — will re-classify all.", exc)

    try:
        notif_name, feedb_name = _get_source_table_names(deployment_env)
    except Exception as exc:
        log.warning("labels: SSM lookup failed (%s) — cached-only.", exc)
        notifications: list[dict] = []
        notif_table = feedback_table = None
        if not cached:
            return [], []
    else:
        notif_table = ddb.Table(notif_name)
        feedback_table = ddb.Table(feedb_name)
        try:
            notifications = _query_notifications(notif_table, sensor_id)
        except Exception as exc:
            log.warning("labels: Notification query failed (%s).", exc)
            notifications = []

    log.info("labels: %d notifications for sensor %s", len(notifications), sensor_id)

    tp_dates: List[pd.Timestamp] = []
    fp_dates: List[pd.Timestamp] = []
    pending = ambiguous = cache_hits = 0

    for notif in notifications:
        ts_raw = notif.get("timestamp")
        if not ts_raw:
            pending += 1
            continue

        try:
            event_ts_dt = pd.Timestamp(ts_raw, tz="UTC")
            event_ts = event_ts_dt.isoformat()
        except Exception:
            log.warning("labels: unparseable timestamp %r", ts_raw)
            pending += 1
            continue

        cached_row = cached.get(event_ts)
        if cached_row and cached_row.get("label") not in (None, "", "pending"):
            label = cached_row["label"]
            cache_hits += 1
        else:
            feedback_id = notif.get("feedbackID")
            if not feedback_id:
                if history_table:
                    _write_history(history_table, sensor_id, event_ts,
                                   "pending", "pending", notif, None)
                pending += 1
                continue

            if feedback_table is None:
                pending += 1
                continue

            try:
                fb_resp = feedback_table.get_item(Key={"id": feedback_id})
            except Exception as exc:
                log.warning("labels: feedback fetch failed %s: %s", feedback_id, exc)
                pending += 1
                continue

            feedback = fb_resp.get("Item")
            if not feedback:
                pending += 1
                continue

            label, label_source = _classify_with_llm(feedback)
            if history_table:
                _write_history(history_table, sensor_id, event_ts,
                               label, label_source, notif, feedback)

        if label == "tp":
            tp_dates.append(event_ts_dt)
        elif label == "fp":
            fp_dates.append(event_ts_dt)
        elif label == "skip":
            ambiguous += 1

    tp_dates.sort()
    fp_dates.sort()

    log.info(
        "labels: sensor=%s notifications=%d TP=%d FP=%d pending=%d ambiguous=%d cache_hits=%d",
        sensor_id, len(notifications), len(tp_dates), len(fp_dates),
        pending, ambiguous, cache_hits,
    )
    return [str(d) for d in tp_dates], [str(d) for d in fp_dates]
