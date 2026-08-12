# -*- coding: utf-8 -*-
"""
s3_to_db.py — S3 Drive 의 .pkl 을 읽어 DataFrame 으로 만들고 DB 에 적재

[흐름]
    Spotfire  ->  S3 (multi_report/*.pkl)  ->  이 모듈  ->  MySQL
                                                    \\
                                                     -> build_f3.py 가 소비

[설정]  .env  (getdata/db_common.py 가 읽는 것과 같은 파일)
    S3_ACCESS_KEY_ID=
    S3_SECRET_ACCESS_KEY=
    S3_ENDPOINT_URL=http://s3.dataplatform.samsungds.net:9020
    S3_BUCKET=RND_FABMODELING
    S3_PREFIX=multi_report/

[사용]
    python getdata/s3_to_db.py --list          S3 에 뭐가 있는지만 확인
    python getdata/s3_to_db.py --peek LOT      한 테이블만 읽어 미리보기(적재 안 함)
    python getdata/s3_to_db.py                 8개 전부 읽어 raw_* 테이블로 적재
    python getdata/s3_to_db.py --only LOT,TIP  일부만

[읽기만 하고 싶을 때]
    import s3_to_db
    df = s3_to_db.read_table("PFR1_KFR7_LOT")          # DataFrame
    frames = s3_to_db.read_all()                        # {이름: DataFrame}
"""

from __future__ import annotations

import argparse
import io
import os
import pickle
from time import perf_counter

import pandas as pd

import db_common as DB

# Spotfire 가 올리는 8개 테이블. 이름은 raw_of_raw_table.txt 와 동일하다.
TABLES = [
    "PFR1_KFR7_LOT",
    "PFR1_KFR7_MATERIALWORKSTATUS",
    "PFR1_KFR7_STEP_PATH",
    "PFR1_KFR7_TIP",
    "PFR1_KFR7_EQUIPMENT",
    "PFR1_KFR7_EQP_GROUP",
    "PFR1_KFR7_HOLD",
    "PFR1_KFR7_MOVE",
]

EXT = "pkl"
DB_PREFIX = "raw_"          # MySQL 에 적재될 테이블명 접두 (raw_pfr1_kfr7_lot ...)

# Oracle 이 대문자로 주는 컬럼명을 소문자로 통일한다(기존 전처리가 소문자 기준).
LOWER_COLUMNS = True

# DB 적재 대상. STEP_PATH / TIP 은 수백 MB 라 DB 에 넣을 이유가 없다.
# build_f3.py 가 메모리에서 바로 쓰면 되므로 기본은 제외한다.
DB_LOAD_TABLES = [
    "PFR1_KFR7_LOT",
    "PFR1_KFR7_MATERIALWORKSTATUS",
    "PFR1_KFR7_EQUIPMENT",
    "PFR1_KFR7_EQP_GROUP",
    "PFR1_KFR7_HOLD",
    "PFR1_KFR7_MOVE",
]


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------
def client():
    """refer_s3_access.txt 가이드와 동일한 형태의 boto3 client."""
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


def _bucket_prefix():
    DB.load_env()
    bucket = os.environ.get("S3_BUCKET", "")
    prefix = os.environ.get("S3_PREFIX", "")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    if not bucket:
        raise RuntimeError(".env 에 S3_BUCKET 이 필요합니다.")
    return bucket, prefix


def list_objects():
    """버킷/프리픽스 아래 오브젝트 목록. 적재 전 점검용."""
    bucket, prefix = _bucket_prefix()
    cli = client()
    out = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        res = cli.list_objects_v2(**kw)
        for o in res.get("Contents", []):
            if o["Key"].endswith("/"):
                continue
            out.append({"key": o["Key"], "size_mb": round(o["Size"] / 1024 / 1024, 2),
                        "modified": o["LastModified"]})
        if not res.get("IsTruncated"):
            break
        token = res.get("NextContinuationToken")
    return pd.DataFrame(out)


def read_table(name, bucket=None, prefix=None):
    """S3 의 pkl 을 파일로 내려받지 않고 메모리에서 바로 DataFrame 으로."""
    b, p = _bucket_prefix()
    bucket = bucket or b
    prefix = prefix if prefix is not None else p
    key = f"{prefix}{name}.{EXT}"

    obj = client().get_object(Bucket=bucket, Key=key)
    buf = io.BytesIO(obj["Body"].read())
    try:
        df = pd.read_pickle(buf)
    except Exception:
        # pandas 버전 차이로 read_pickle 이 실패하면 표준 pickle 로 재시도
        buf.seek(0)
        df = pickle.load(buf)
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df)
    if LOWER_COLUMNS:
        # Oracle 은 컬럼을 대문자로 돌려준다. 기존 python 전처리가 전부
        # 소문자 기준이라 여기서 한 번에 맞춘다.
        df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def read_all(names=None):
    """{이름: DataFrame}. 실패한 것은 건너뛰고 이유를 출력한다."""
    out = {}
    for name in (names or TABLES):
        t0 = perf_counter()
        try:
            df = read_table(name)
            out[name] = df
            print(f"[S3] {name:30s} {len(df):>9,}행 {df.shape[1]:>3}컬럼 "
                  f"{perf_counter() - t0:6.1f}s", flush=True)
        except Exception as e:
            print(f"[S3] {name:30s} 실패: {type(e).__name__}: {e}", flush=True)
    return out


# ---------------------------------------------------------------------------
# DB 적재
# ---------------------------------------------------------------------------
def _ddl(table, df):
    cols = ",\n  ".join(f"`{c}` TEXT NULL" for c in df.columns)
    return (f"CREATE TABLE IF NOT EXISTS `{table}` (\n  {cols}\n) "
            f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


def load_to_db(conn, name, df, chunk=5000):
    """raw_<name> 테이블을 통째로 교체한다(멱등).

    원본 raw 를 그대로 보관하는 것이 목적이라 전 컬럼 TEXT 로 둔다.
    타입 변환은 build_f3.py 가 담당한다.
    """
    table = (DB_PREFIX + name).lower()
    cols = list(df.columns)
    ph = ", ".join(["%s"] * len(cols))
    collist = ", ".join(f"`{c}`" for c in cols)

    rows = []
    for r in df.itertuples(index=False, name=None):
        rows.append(tuple(None if pd.isna(v) else str(v) for v in r))

    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        cur.execute(_ddl(table, df))
        sql = f"INSERT INTO `{table}` ({collist}) VALUES ({ph})"
        for i in range(0, len(rows), chunk):
            cur.executemany(sql, rows[i:i + chunk])
    conn.commit()
    return table, len(rows)


def main():
    ap = argparse.ArgumentParser(description="S3 의 pkl 을 읽어 DB 에 적재")
    ap.add_argument("--list", action="store_true", help="S3 오브젝트 목록만 출력")
    ap.add_argument("--peek", metavar="NAME",
                    help="한 테이블만 읽어 미리보기(적재 안 함). 부분 이름 가능")
    ap.add_argument("--only", default="", help="쉼표구분. 부분 이름 가능")
    ap.add_argument("--all", action="store_true",
                    help="STEP_PATH / TIP 포함 전부 적재(수백 MB 라 느림)")
    args = ap.parse_args()

    if args.list:
        df = list_objects()
        print(df.to_string(index=False) if len(df) else "(오브젝트 없음)")
        return

    def resolve(token):
        t = token.strip().upper()
        hit = [n for n in TABLES if t == n or t in n]
        return hit

    if args.peek:
        names = resolve(args.peek)
        if not names:
            print(f"'{args.peek}' 에 해당하는 테이블이 없습니다. {TABLES}")
            return
        df = read_table(names[0])
        print(f"\n{names[0]}  {len(df):,}행 {df.shape[1]}컬럼")
        print("컬럼:", list(df.columns))
        print(df.head(10).to_string())
        return

    names = list(DB_LOAD_TABLES)
    if args.all:
        names = list(TABLES)
    if args.only:
        names = [n for tok in args.only.split(",") for n in resolve(tok)]
        if not names:
            print(f"--only 에 해당하는 테이블이 없습니다. {TABLES}")
            return
    skipped = [n for n in TABLES if n not in names]
    if skipped:
        print(f"[DB] 적재 제외: {', '.join(skipped)}  "
              f"(--all 또는 --only 로 포함 가능)", flush=True)

    frames = read_all(names)
    if not frames:
        print("[DB] 적재할 데이터가 없습니다.", flush=True)
        return

    conn = DB.connect()
    try:
        for name, df in frames.items():
            t0 = perf_counter()
            table, n = load_to_db(conn, name, df)
            print(f"[DB] {table:36s} {n:>9,}행 {perf_counter() - t0:6.1f}s",
                  flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
