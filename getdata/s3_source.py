# -*- coding: utf-8 -*-
"""
s3_source.py — S3 Drive 의 .pkl 을 읽어 DataFrame 으로 돌려준다

[흐름]
    Spotfire  ->  S3 (multi_report/*.pkl)  ->  이 모듈  ->  build_f3 / get_move
                                                              -> 전처리 결과만 DB 적재

raw 자체는 DB 에 넣지 않는다. bdq 로 받던 자리를 그대로 대체하는 역할이다.

[설정]  .env  (getdata/db_common.py 가 읽는 것과 같은 파일)
    S3_ACCESS_KEY_ID=
    S3_SECRET_ACCESS_KEY=
    S3_ENDPOINT_URL=http://s3.dataplatform.samsungds.net:9020
    S3_BUCKET=RND_FABMODELING
    S3_PREFIX=multi_report/

[사용]
    python getdata/s3_source.py --list         S3 에 뭐가 있는지 확인
    python getdata/s3_source.py --peek LOT     한 테이블 미리보기
    python getdata/s3_source.py --check        8개 전부 읽어 행수/컬럼 점검

[코드에서]
    import s3_source
    df = s3_source.read_table("PFR1_KFR7_LOT")     # DataFrame (컬럼 소문자)
    frames = s3_source.read_all()                   # {이름: DataFrame}
"""

from __future__ import annotations

import argparse
import io
import json
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
# Oracle 이 대문자로 주는 컬럼명을 소문자로 통일한다(기존 전처리가 소문자 기준).
LOWER_COLUMNS = True

MANIFEST = "_manifest.json"     # Spotfire 가 업로드 완료 후 마지막에 쓰는 파일

# 로컬 캐시.
#   LastModified 로 판별하면 안 된다. Spotfire 가 30분마다 8개를 전부 재업로드하므로
#   내용이 같아도 시각은 매번 바뀌어 캐시가 무력화된다.
#   ETag(=업로드 내용의 MD5)를 쓴다. head_object 로 헤더만 받아 비교하므로
#   본문을 내려받지 않고 판별된다. pandas.to_pickle 은 같은 DataFrame 에 대해
#   바이트가 재현되므로, 내용이 안 바뀌면 ETag 도 같다.
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s3cache")
USE_CACHE = True




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


def read_manifest():
    """마지막 업로드 회차 정보. 없으면 None.

    Spotfire 가 8개를 다 올린 뒤 마지막에 쓰므로, 이 파일이 있으면
    그 회차는 완결된 것이다(중간에 읽어 서로 다른 시점이 섞이는 것을 막는다).
    """
    bucket, prefix = _bucket_prefix()
    try:
        obj = client().get_object(Bucket=bucket, Key=f"{prefix}{MANIFEST}")
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return None


def _cache_path(key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, key.replace("/", "_"))


def _fetch_bytes(bucket, key):
    """ETag(내용 해시)를 보고 바뀐 것만 내려받는다.

    head_object 는 헤더만 받으므로 1.7GB 파일이라도 판별 비용이 거의 없다.
    ETag 가 같으면 본문을 아예 요청하지 않는다.
    """
    cli = client()
    if not USE_CACHE:
        return cli.get_object(Bucket=bucket, Key=key)["Body"].read(), False

    path = _cache_path(key)
    meta = path + ".meta"

    try:
        head = cli.head_object(Bucket=bucket, Key=key)
        # multipart 업로드면 '<md5>-<파트수>' 형태. 그대로 비교하면 된다.
        tag = str(head.get("ETag", "")).strip('"')
        size = str(head.get("ContentLength", ""))
        remote = f"{tag}|{size}"
    except Exception:
        remote = ""          # head 실패 시엔 캐시를 믿지 않고 받는다

    if remote and os.path.exists(path) and os.path.exists(meta):
        with open(meta, encoding="utf-8") as f:
            if f.read().strip() == remote:
                with open(path, "rb") as fh:
                    return fh.read(), True          # 캐시 적중 (본문 미요청)

    data = cli.get_object(Bucket=bucket, Key=key)["Body"].read()
    if remote:
        with open(path, "wb") as fh:
            fh.write(data)
        with open(meta, "w", encoding="utf-8") as f:
            f.write(remote)
    return data, False


def read_table(name, bucket=None, prefix=None):
    """S3 의 pkl 을 DataFrame 으로. 바뀌지 않았으면 로컬 캐시를 쓴다."""
    b, p = _bucket_prefix()
    bucket = bucket or b
    prefix = prefix if prefix is not None else p
    key = f"{prefix}{name}.{EXT}"

    data, cached = _fetch_bytes(bucket, key)
    if cached:
        print(f"[S3] {name} 캐시 사용", flush=True)
    buf = io.BytesIO(data)
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


def main():
    ap = argparse.ArgumentParser(description="S3 의 pkl 확인/미리보기")
    ap.add_argument("--list", action="store_true", help="S3 오브젝트 목록만 출력")
    ap.add_argument("--peek", metavar="NAME",
                    help="한 테이블만 읽어 미리보기(적재 안 함). 부분 이름 가능")
    ap.add_argument("--check", action="store_true",
                    help="8개 전부 읽어 행수/컬럼 점검")
    ap.add_argument("--manifest", action="store_true",
                    help="마지막 업로드 회차 정보만 출력")
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

    if args.manifest:
        m = read_manifest()
        print(json.dumps(m, ensure_ascii=False, indent=2) if m
              else "(매니페스트 없음 - Spotfire 가 아직 새 버전으로 올리지 않음)")
        return

    if args.check:
        frames = read_all()
        print()
        for k, v in frames.items():
            print(f"  {k:30s} {len(v):>9,}행 {v.shape[1]:>3}컬럼  "
                  f"{', '.join(list(v.columns)[:6])}...")
        missing = [t for t in TABLES if t not in frames]
        if missing:
            print("\n읽지 못한 테이블:", missing)
        return

    print("옵션이 필요합니다: --list / --peek NAME / --check")


if __name__ == "__main__":
    main()
