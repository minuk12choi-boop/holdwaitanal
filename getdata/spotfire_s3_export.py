# -*- coding: utf-8 -*-
"""
spotfire_s3_export.py — Spotfire 데이터 함수용 S3 자동 적재

[사용법]
  Spotfire > Tools > Register Data Functions
    - Type        : Python script (스크립트 언어를 Python 으로)
    - Script      : 이 파일 내용을 그대로 붙여넣기
    - Input Parameters : 아래 8개를 Table 로 등록. 이름을 정확히 맞출 것.
          PFR1_KFR7_LOT
          PFR1_KFR7_MATERIALWORKSTATUS
          PFR1_KFR7_STEP_PATH
          PFR1_KFR7_TIP
          PFR1_KFR7_EQUIPMENT
          PFR1_KFR7_EQP_GROUP
          PFR1_KFR7_HOLD
          PFR1_KFR7_MOVE
    - Output Parameters : upload_log (Table) — 적재 결과 확인용. 생략 가능.
  등록 후 Refresh Function 을 "Automatic" 으로 두면 분석 파일을 열 때마다,
  즉 데이터가 새로 로딩될 때마다 S3 에 올라간다.

[자격증명]
  .env 에서 읽는다. Spotfire 는 작업 폴더가 제각각이라 ENV_CANDIDATES 의
  경로를 순서대로 찾는다. 본인 경로가 다르면 첫 줄만 고치면 된다.
  .env 를 못 찾으면 os.environ 을 본다.

      S3_ACCESS_KEY_ID=
      S3_SECRET_ACCESS_KEY=
      S3_ENDPOINT_URL=http://s3.dataplatform.samsungds.net:9020
      S3_BUCKET=
      S3_PREFIX=

[결과]
  s3://<S3_BUCKET>/<S3_PREFIX><테이블명>.pkl
  python 쪽(build_f3.py / get_move.py)은 같은 이름으로 읽으면 된다.
"""

import io
import os
import datetime as dt

import boto3
import pandas as pd


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
ENV_CANDIDATES = [
    r"D:\PERSONAL_SPACE\SW\python\7_holdwaitanal\.env",
    os.path.join(os.path.expanduser("~"), "7_holdwaitanal", ".env"),
    ".env",
]

TABLE_NAMES = [
    "PFR1_KFR7_LOT",
    "PFR1_KFR7_MATERIALWORKSTATUS",
    "PFR1_KFR7_STEP_PATH",
    "PFR1_KFR7_TIP",
    "PFR1_KFR7_EQUIPMENT",
    "PFR1_KFR7_EQP_GROUP",
    "PFR1_KFR7_HOLD",
    "PFR1_KFR7_MOVE",
]

FMT = "pkl"          # 참고 코드와 동일. 'parquet' / 'csv' 로 바꿔도 된다.


# ---------------------------------------------------------------------------
def load_env():
    """.env 를 찾아 os.environ 에 채운다. 이미 값이 있으면 덮지 않는다."""
    for path in ENV_CANDIDATES:
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        return path
    return None


def make_client():
    key = os.environ.get("S3_ACCESS_KEY_ID", "")
    secret = os.environ.get("S3_SECRET_ACCESS_KEY", "")
    endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    if not (key and secret and endpoint):
        raise RuntimeError(
            "S3 자격증명을 찾지 못했습니다. .env 의 S3_ACCESS_KEY_ID / "
            "S3_SECRET_ACCESS_KEY / S3_ENDPOINT_URL 을 확인하세요. "
            "(찾은 .env: %s)" % (ENV_PATH or "없음"))
    return boto3.client(
        service_name="s3",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        endpoint_url=endpoint,
    )


def to_buffer(df, fmt):
    buf = io.BytesIO()
    if fmt == "pkl":
        df.to_pickle(buf)
    elif fmt == "parquet":
        df.to_parquet(buf, index=False)
    else:
        buf.write(df.to_csv(index=False).encode("utf-8-sig"))
    buf.seek(0)          # 이걸 빼먹으면 0 바이트로 올라간다
    return buf


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
ENV_PATH = load_env()

bucket = os.environ.get("S3_BUCKET", "")
prefix = os.environ.get("S3_PREFIX", "")
if prefix and not prefix.endswith("/"):
    prefix += "/"        # 없으면 폴더가 아니라 파일명에 붙는다

rows = []
started = dt.datetime.now()

try:
    if not bucket:
        raise RuntimeError(".env 에 S3_BUCKET 이 비어 있습니다.")
    client = make_client()

    for name in TABLE_NAMES:
        t0 = dt.datetime.now()
        # Spotfire 는 입력 파라미터를 전역 변수로 넣어준다.
        # 등록하지 않은 테이블이 있어도 전체가 멈추지 않게 개별 처리한다.
        df = globals().get(name)
        if df is None:
            rows.append([name, "SKIP", 0, 0, "입력 파라미터 미등록", t0, dt.datetime.now()])
            continue
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        try:
            key = "%s%s.%s" % (prefix, name, FMT)
            client.upload_fileobj(to_buffer(df, FMT), bucket, key)
            rows.append([name, "OK", len(df), df.shape[1],
                         "s3://%s/%s" % (bucket, key), t0, dt.datetime.now()])
        except Exception as e:
            rows.append([name, "FAIL", len(df), df.shape[1],
                         "%s: %s" % (type(e).__name__, e), t0, dt.datetime.now()])

except Exception as e:
    rows.append(["(전체)", "FAIL", 0, 0, "%s: %s" % (type(e).__name__, e),
                 started, dt.datetime.now()])

upload_log = pd.DataFrame(
    rows, columns=["table", "status", "rows", "cols", "detail", "start", "end"])
upload_log["elapsed_sec"] = (
    upload_log["end"] - upload_log["start"]).dt.total_seconds().round(2)
