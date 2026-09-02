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
import datetime as dt
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
    "PFR1_KFR7_SSPS_PROD_NAME",
]

# FabPlan 원천. 아직 안 올라온 환경도 있어 **필수 목록과 분리**한다.
#   TABLES 에 넣으면 이 다섯이 없을 때 매 회차가 미완결로 판정돼
#   build_f3 가 통째로 건너뛴다.
FAB_TABLES = [
    "PFR1_FABPLAN_STEP",
    "PFR1_FABPLAN_NEWEINECNSPEC",
    "PFR1_FABPLAN_SELECTCONNECTSPEC",
    "PFR1_FABPLAN_SKIPRULE",
    "PFR1_ENGR_LOT_PPID",
]

# CATEGORY 이력. 아직 안 올라온 환경도 있어 선택 목록에 둔다.
#   없으면 초HOT 은 /master/ 기준정보만으로 정한다.
OPT_TABLES = [
    "PFR1_CATEGORY",
]

# Spotfire 쪽 FMT 와 맞춘다. parquet 권장(pkl 대비 약 6% 크기).
EXT = "parquet"
# Oracle 이 대문자로 주는 컬럼명을 소문자로 통일한다(기존 전처리가 소문자 기준).
LOWER_COLUMNS = True

MANIFEST = "_manifest.json"     # Spotfire 가 업로드 완료 후 마지막에 쓰는 파일


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


def manifest_age(m=None, max_min=90):
    """매니페스트가 얼마나 낡았는지 본다. 표마다 따로 센다.

    Spotfire 쪽은 데이터 함수를 셋(tip · path · rest)으로 나눠 돌린다.
    하나가 실패해도 매니페스트에는 앞 회차 항목이 그대로 남아 ok 수가
    채워져 보인다. 표별 uploaded_at 으로 그것을 가린다.
    """
    m = m if m is not None else read_manifest()
    if not m:
        return {"ok": False, "reason": "매니페스트가 없습니다"}
    now = dt.datetime.now()
    stale, unknown = [], []

    # 조각으로 나눈 뒤에는 옛 이름(PFR1_KFR7_TIP)이 매니페스트에 남아 있다.
    #   그것은 더 이상 갱신되지 않으므로 낡았다고 나오는 게 당연하다.
    #   조각(_0 · _1 · _2)이 하나라도 있으면 옛 이름은 보지 않는다.
    names = set((m.get("tables") or {}).keys())
    skip = {t for t in SPLIT_TABLES
            if any(f"{t}_{i}" in names for i in range(SPLIT_PARTS))}

    for name, v in (m.get("tables") or {}).items():
        if name in skip:
            continue
        at = str((v or {}).get("uploaded_at") or "")
        if not at:
            unknown.append(name)            # 옛 형식(시각 없음)
            continue
        try:
            t = dt.datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            unknown.append(name)
            continue
        mins = (now - t).total_seconds() / 60.0
        if mins > max_min:
            stale.append((name, round(mins)))
    stale.sort(key=lambda x: -x[1])
    return {"ok": not stale, "stale": stale, "unknown": unknown,
            "total": len(m.get("tables") or {}),
            "expected": m.get("total")}


# 조각으로 나눠 올리는 표. Spotfire 가 한 번에 읽다 죽어서 셋으로 갈랐다.
#   PFR1_KFR7_TIP -> PFR1_KFR7_TIP_0 · _1 · _2
#   원본 이름으로 읽으면 조각을 찾아 이어 붙인다.
SPLIT_PARTS = 3
SPLIT_TABLES = ("PFR1_KFR7_TIP", "PFR1_KFR7_STEP_PATH")


def read_table(name, bucket=None, prefix=None):
    """S3 의 표를 읽어 DataFrame 으로. 조각으로 나뉜 표는 이어 붙인다.

    캐시하지 않는다. 라인 데이터는 매 순간 바뀌므로 낡은 값을 재사용하면
    실시간 전환의 의미가 없다. 전송량은 압축(FMT)으로 줄인다.
    """
    if name in SPLIT_TABLES:
        parts, missing = [], []
        for i in range(SPLIT_PARTS):
            try:
                parts.append(_read_one(f"{name}_{i}", bucket, prefix))
            except Exception:
                missing.append(i)
        if parts:
            if missing:
                # 한 조각이라도 빠지면 그만큼 재공이 사라진다. 조용히 넘기면
                # 설비를 못 찾거나 스텝이 비는 것으로만 보여 원인을 못 찾는다.
                print(f"[S3] {name} 조각 {missing} 없음 - 그만큼 빠진다",
                      flush=True)
            df = pd.concat(parts, ignore_index=True)
            # 조각마다 라인별 행 수를 찍는다. 한 조각의 쿼리가 잘못되면
            # 여기서 드러난다(예: 한쪽 라인 조건만 다른 숫자로 남음).
            for i, part in enumerate(parts):
                lc = ""
                for col in ("line_id", "LINE_ID"):
                    if col in part.columns:
                        vc = part[col].value_counts().to_dict()
                        lc = "  " + " · ".join(f"{k} {v:,}"
                                               for k, v in sorted(vc.items()))
                        break
                print(f"[S3]   {name}_{i} {len(part):,}행{lc}", flush=True)
            print(f"[S3] {name} 조각 {len(parts)}개 이어붙임 {len(df):,}행",
                  flush=True)
            return df
        # 조각이 하나도 없으면 옛 방식(한 덩어리)으로 읽는다.

    return _read_one(name, bucket, prefix)


def _read_one(name, bucket=None, prefix=None):
    """조각 하나(또는 나누지 않은 표)를 읽는다."""
    b, p = _bucket_prefix()
    bucket = bucket or b
    prefix = prefix if prefix is not None else p
    key = f"{prefix}{name}.{EXT}"

    data = client().get_object(Bucket=bucket, Key=key)["Body"].read()
    buf = io.BytesIO(data)
    if EXT == "parquet":
        df = pd.read_parquet(buf)
    elif EXT == "csv":
        df = pd.read_csv(buf)
    else:
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
        if not m:
            print("(매니페스트 없음 - Spotfire 가 아직 새 버전으로 올리지 않음)")
            return
        print(f"원천 조회(SYSDATE) : {m.get('query_time', '(없음)')}")
        print(f"업로드 시작        : {m.get('run_at', '')}")
        print(f"업로드 완료        : {m.get('finished_at', '')}")
        print(f"형식 / 테이블      : {m.get('fmt', '')} / "
              f"{m.get('ok', 0)}of{m.get('total', 0)}")
        qs = m.get("query_time_all") or []
        if len(qs) > 1:
            print(f"[주의] 테이블마다 조회 시각이 다릅니다: {qs}")
        print()
        for k, v in (m.get("tables") or {}).items():
            print(f"  {k:30s} {v.get('rows', 0):>10,}행 "
                  f"{v.get('cols', 0):>3}컬럼  {v.get('query_time', '')}")
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
