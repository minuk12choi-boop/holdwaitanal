# -*- coding: utf-8 -*-
"""
s3_upload.py — S3 Drive 적재

참고 코드와 동일한 방식(boto3 + endpoint_url + pickle 업로드)이되,
자격증명은 코드에 박지 않고 .env 에서 읽는다.

.env 에 아래를 넣는다.

    S3_ACCESS_KEY_ID=...
    S3_SECRET_ACCESS_KEY=...
    S3_ENDPOINT_URL=http://s3.dataplatform.samsungds.net:9020
    S3_BUCKET=FAB_MODELING
    S3_PREFIX=PM/NRD_PM/InputData_RealTime(NRD)/

사용:
    import s3_upload
    s3_upload.upload_frames({"f3": df_f3, "move_daily": df_move})

    # 단독 실행: DB 에 적재된 최신 스냅샷을 S3 로 올린다
    python getdata/s3_upload.py
"""

from __future__ import annotations

import io
import os

import db_common as DB


def s3_client():
    """boto3 클라이언트. 자격증명이 없으면 명확히 알려준다."""
    import boto3

    DB.load_env()
    key = os.environ.get("S3_ACCESS_KEY_ID", "")
    secret = os.environ.get("S3_SECRET_ACCESS_KEY", "")
    endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    if not (key and secret and endpoint):
        raise RuntimeError(
            ".env 에 S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_ENDPOINT_URL "
            "이 필요합니다.")
    return boto3.client(
        service_name="s3",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        endpoint_url=endpoint,
    )


def upload_frames(frames, bucket=None, prefix=None, fmt="pkl"):
    """{이름: DataFrame} 을 S3 에 올린다.

    fmt: 'pkl'(참고 코드와 동일) | 'parquet' | 'csv'
    반환: 업로드한 object key 목록
    """
    DB.load_env()
    bucket = bucket or os.environ.get("S3_BUCKET", "")
    prefix = prefix if prefix is not None else os.environ.get("S3_PREFIX", "")
    if not bucket:
        raise RuntimeError(".env 에 S3_BUCKET 이 필요합니다.")

    cli = s3_client()
    keys = []
    for name, df in frames.items():
        buf = io.BytesIO()
        if fmt == "pkl":
            df.to_pickle(buf)
        elif fmt == "parquet":
            df.to_parquet(buf, index=False)
        else:
            buf.write(df.to_csv(index=False).encode("utf-8-sig"))
        buf.seek(0)

        key = f"{prefix}{name}.{fmt}"
        cli.upload_fileobj(buf, bucket, key)
        keys.append(key)
        print(f"[S3] uploaded s3://{bucket}/{key} rows={len(df):,}", flush=True)
    return keys


# ---------------------------------------------------------------------------
def _latest_frames():
    """DB 에 적재된 최신 스냅샷/집계를 DataFrame 으로 읽어온다."""
    import pandas as pd

    conn = DB.connect()
    out = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(snapshot_at) FROM f3_live")
            snap = cur.fetchone()[0]
        if snap:
            out["f3_live"] = pd.read_sql(
                "SELECT * FROM f3_live WHERE snapshot_at = %s", conn, params=(snap,))
        for t in ("move_daily", "move_shift", "move_lot"):
            try:
                out[t] = pd.read_sql(f"SELECT * FROM {t}", conn)
            except Exception:
                pass
    finally:
        conn.close()
    return out


def main():
    frames = _latest_frames()
    if not frames:
        print("[S3] 올릴 데이터가 없습니다.", flush=True)
        return
    upload_frames(frames)


if __name__ == "__main__":
    main()
