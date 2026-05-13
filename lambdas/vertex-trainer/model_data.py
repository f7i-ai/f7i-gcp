"""Sensor data gather + split + GCS upload.

Mirrors ``f7i-cdk/.../predict_train_models/consumer.py`` step-for-step:
- Page the AssetChart ``dataBySensorID`` GSI to collect sensor history.
- Build the five-feature row dict, ffill/bfill, split 70/15/15.
- Emit train/validation/test/full CSVs plus placeholder llm_scores.npy and
  empty vae_channel marker — the same channel layout the training scripts
  already read, just sourced from GCS instead of S3.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Dict, List, Optional

import boto3
import numpy as np
import pandas as pd
from boto3.dynamodb.conditions import Key
from google.cloud import storage


log = logging.getLogger(__name__)


SENSOR_COLS = [
    "temperature",
    "velocity_total_crest",
    "velocity_x_rms",
    "velocity_y_rms",
    "velocity_z_rms",
]


# ── Feature extraction ────────────────────────────────────────────────────────

def _parse_velocity_object(vel_raw) -> Optional[Dict[str, float]]:
    if vel_raw is None or (isinstance(vel_raw, float) and pd.isna(vel_raw)):
        return None
    if isinstance(vel_raw, dict):
        v = vel_raw
    else:
        s = str(vel_raw).strip()
        if not s:
            return None
        try:
            v = json.loads(s)
        except json.JSONDecodeError:
            return None
    band = v.get("band10To1000Hz") or v.get("band10to1000Hz") or {}
    tv = band.get("totalVibration") or {}
    total_crest = float(tv["crestFactor"]) if isinstance(tv, dict) and "crestFactor" in tv else None
    axes: Dict[str, float] = {}
    for axis, key in (("x", "xAxis"), ("y", "yAxis"), ("z", "zAxis")):
        ax = band.get(key)
        if isinstance(ax, dict) and "rms" in ax:
            axes[axis] = float(ax["rms"])
    return {
        "velocity_total_crest": total_crest,
        "velocity_x_rms": axes.get("x"),
        "velocity_y_rms": axes.get("y"),
        "velocity_z_rms": axes.get("z"),
    }


def _row_to_feature_dict(row: pd.Series) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for c in SENSOR_COLS:
        if c in row.index and pd.notna(row[c]):
            out[c] = float(row[c])
    if len(out) == len(SENSOR_COLS):
        return out
    pv = _parse_velocity_object(row.get("velocity"))
    if pv:
        for k in SENSOR_COLS:
            if k not in out and pv.get(k) is not None:
                out[k] = pv[k]
    if "temperature" not in out and "temperature" in row.index and pd.notna(row["temperature"]):
        out["temperature"] = float(row["temperature"])
    for k in SENSOR_COLS:
        if k not in out:
            out[k] = 0.0
    return {k: out[k] for k in SENSOR_COLS}


# ── DynamoDB gather ───────────────────────────────────────────────────────────

def get_sensor_data(
    asset_chart_table_name: str,
    sensor_id: str,
    max_queries: int = 1000,
) -> List[dict]:
    """Page through AssetChart's ``dataBySensorID`` GSI and return all rows."""
    table = boto3.resource("dynamodb").Table(asset_chart_table_name)

    all_items: List[dict] = []
    last_evaluated_key = None
    queries = 0

    while queries < max_queries:
        query_params = {
            "IndexName": "dataBySensorID",
            "KeyConditionExpression": Key("sensorId").eq(sensor_id),
            "ScanIndexForward": False,
        }
        if last_evaluated_key:
            query_params["ExclusiveStartKey"] = last_evaluated_key

        response = table.query(**query_params)
        items = response.get("Items", [])
        all_items.extend(items)
        queries += 1

        log.info("Query #%d: %d items (cumulative %d)", queries, len(items), len(all_items))

        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break

    log.info("Sensor %s: collected %d rows across %d queries",
             sensor_id, len(all_items), queries)
    return all_items


# ── GCS upload helpers ────────────────────────────────────────────────────────

def _gcs_upload_bytes(
    gcs_client: storage.Client, bucket: str, key: str,
    data: bytes, content_type: str,
) -> str:
    gcs_client.bucket(bucket).blob(key).upload_from_string(data, content_type=content_type)
    uri = f"gs://{bucket}/{key}"
    log.info("Uploaded %d bytes to %s", len(data), uri)
    return uri


def _gcs_upload_dataframe_csv(
    gcs_client: storage.Client, bucket: str, key: str, df: pd.DataFrame,
) -> str:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return _gcs_upload_bytes(gcs_client, bucket, key, buf.getvalue().encode("utf-8"), "text/csv")


# ── Split + upload ────────────────────────────────────────────────────────────

def split_and_upload(
    gcs_client: storage.Client,
    sensor_data: List[dict],
    sensor_id: str,
    gcs_bucket: str,
    gcs_prefix: str = "",
) -> Dict[str, str]:
    """Split rows 70/15/15 and upload all four CSVs + placeholder RL assets to GCS.

    Returns a channel-name → ``gs://`` URI map matching the SageMaker channels
    the existing training scripts already read: train, validation, test, full,
    llm_scores, vae_channel.
    """
    if not sensor_data:
        raise ValueError(f"No data available for sensor {sensor_id}")

    df = pd.DataFrame(sensor_data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp")

    rows = []
    for _, row in df.iterrows():
        try:
            feat = _row_to_feature_dict(row)
            rows.append({"timestamp": row["timestamp"], **feat})
        except Exception as exc:
            log.warning("Skipping row due to feature parse error: %s", exc)
            continue

    if len(rows) < 100:
        raise ValueError(
            f"Not enough valid rows after feature extraction for sensor {sensor_id}: {len(rows)}"
        )

    df_processed = pd.DataFrame(rows).sort_values("timestamp").ffill().bfill()

    total = len(df_processed)
    train_size = int(total * 0.7)
    val_size = int(total * 0.15)

    train_df = df_processed.iloc[:train_size]
    val_df = df_processed.iloc[train_size:train_size + val_size]
    test_df = df_processed.iloc[train_size + val_size:]

    log.info("Split sizes — train=%d val=%d test=%d full=%d",
             len(train_df), len(val_df), len(test_df), total)

    base = f"{gcs_prefix.rstrip('/') + '/' if gcs_prefix else ''}{sensor_id}"
    uris = {
        "train": _gcs_upload_dataframe_csv(
            gcs_client, gcs_bucket, f"{base}/training/train_data_{sensor_id}.csv", train_df),
        "validation": _gcs_upload_dataframe_csv(
            gcs_client, gcs_bucket, f"{base}/training/validation_data_{sensor_id}.csv", val_df),
        "test": _gcs_upload_dataframe_csv(
            gcs_client, gcs_bucket, f"{base}/test/test_data_{sensor_id}.csv", test_df),
        "full": _gcs_upload_dataframe_csv(
            gcs_client, gcs_bucket, f"{base}/training/full_data_{sensor_id}.csv", df_processed),
    }

    # Placeholder llm_scores + empty vae_channel marker — Phase 1 of the
    # training container replaces llm_scores with real Bedrock-scored values.
    n_win = max(0, len(train_df) - 25)
    placeholder = np.full(n_win, 0.5, dtype=np.float32)
    buf = io.BytesIO()
    np.save(buf, placeholder)
    uris["llm_scores"] = _gcs_upload_bytes(
        gcs_client, gcs_bucket,
        f"{base}/rl_assets/llm_scores.npy", buf.getvalue(),
        "application/octet-stream",
    )
    uris["vae_channel"] = _gcs_upload_bytes(
        gcs_client, gcs_bucket,
        f"{base}/rl_assets/vae_channel/.keep", b"",
        "application/octet-stream",
    )
    return uris


def upload_labels(
    gcs_client: storage.Client,
    gcs_bucket: str,
    gcs_prefix: str,
    sensor_id: str,
    mode: str,
    tp_dates: List[str],
    fp_dates: List[str],
) -> str:
    """Write labels.json into the same rl_assets prefix as llm_scores."""
    base = f"{gcs_prefix.rstrip('/') + '/' if gcs_prefix else ''}{sensor_id}"
    payload = {
        "sensor_id": sensor_id,
        "mode": mode,
        "tp_dates": tp_dates,
        "fp_dates": fp_dates,
    }
    return _gcs_upload_bytes(
        gcs_client, gcs_bucket,
        f"{base}/rl_assets/labels.json",
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )
