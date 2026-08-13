# -*- coding: utf-8 -*-
r"""
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
  Spotfire 는 .env 를 읽지 못하므로 아래 [S3 설정] 블록에 값을 직접 넣는다.

  주의: 이 파일을 그대로 git 에 올리면 키가 노출된다.
        아래 값을 채운 뒤에는 저장소에 커밋하지 말고 Spotfire 에만 붙여넣을 것.
        (저장소의 파일은 빈 값으로 유지한다)

[키 관리]
  이 파일은 저장소에도 있으므로 **빈 값 그대로 커밋**한다.
  값을 채운 사본은 Spotfire 데이터 함수 안에만 둔다.
  실수 방지용 pre-commit 훅이 있다. 한 번만 켜두면 된다.

      git config core.hooksPath .githooks

[느릴 때 어디를 볼 것인가]
  upload_log 에 구간이 나뉘어 찍힌다.

      serialize_sec     DataFrame -> parquet 변환
      upload_sec        S3 전송
      script_total_sec  이 스크립트 전체

  실측(5.1M + 9.9M 행 기준) 직렬화는 약 7초, 전송은 100MB 남짓이다.
  script_total_sec 이 10~30초인데 Spotfire 체감이 몇 분이라면, 병목은 이
  스크립트가 아니라 **Spotfire -> python 입력 전달(마샬링)** 이다.
  그 구간은 스크립트로 줄일 수 없고, 입력 테이블 수/컬럼 수를 줄여야 한다.

[boto3 설치]
  Spotfire 의 Python 은 별도 환경이라 boto3 가 없을 수 있다.
  (ModuleNotFoundError: No module named 'boto3')

  Spotfire Analyst 설치 폴더의 python 으로 설치한다. 보통 아래 경로다.

      "C:\Program Files\TIBCO\Spotfire\<버전>\Modules\Python Interpreter_<...>\python.exe" -m pip install boto3

  정확한 경로는 이 스크립트를 한 번 돌려 확인할 수 있다. 아래 USE_BOTO3 를
  False 로 두면 업로드를 건너뛰고 python 실행 경로와 설치 명령을 upload_log 에
  찍어준다.

  사내망에서 pip 이 막히면 사내 미러를 쓴다.

      python.exe -m pip install boto3 --index-url <사내 pypi 미러>

[결과]
  s3://<S3_BUCKET>/<S3_PREFIX><테이블명>.pkl
  python 쪽(build_f3.py / get_move.py)은 같은 이름으로 읽으면 된다.
"""

import io
import json
import sys
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

try:
    import boto3
    BOTO3_ERR = ""
except ImportError as _e:          # Spotfire 환경에 없을 수 있다
    boto3 = None
    BOTO3_ERR = str(_e)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
# ─── S3 설정 ────────────────────────────────────────────────────────────────
# Spotfire 에 붙여넣기 전에 아래 5개를 채운다.
S3_ACCESS_KEY_ID     = "AKIA2DBBF18BDEA535F3"
S3_SECRET_ACCESS_KEY = "vyyKvOA5PJIN8BJTMM3brzIL0oRbNVSzjrLg+vLr"
S3_ENDPOINT_URL      = "http://s3.dataplatform.samsungds.net:9020"
S3_BUCKET            = "RND_FABMODELING"
S3_PREFIX            = "multi_report/"          # 예) "multi_report/"  (끝 '/' 는 자동 보정)

# boto3 설치 전 점검용. False 로 두면 업로드하지 않고 환경 정보만 upload_log 에
# 남긴다(파이썬 경로 / 설치 명령).
USE_BOTO3 = True
# ────────────────────────────────────────────────────────────────────────────

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

# 저장 형식.
#   "parquet" 권장. 컬럼형 + snappy 압축이라 pkl 대비 약 6% 크기.
#             (STEP_PATH+TIP 1,781MB -> 약 100MB. 실측)
#             pyarrow 설치 필요:
#               "<Spotfire python.exe>" -m pip install pyarrow
#   "pkl"     무압축. pyarrow 없이 동작하지만 전송량이 크다.
#   "csv"     타입이 문자열로 뭉개진다. 확인용으로만.
#
# 데이터는 매 회차 전량 갱신한다(캐시하지 않는다). 라인 데이터는 매 순간
# 바뀌므로 낡은 값을 재사용하면 실시간 전환의 의미가 없다.
# 전송량은 주기를 늦추는 대신 압축으로 줄인다.
FMT = "parquet"

# 업로드 동시 실행 수. 전송은 네트워크 대기라 겹치면 이득이 있다.
# (직렬화 자체는 실측 6.5초라 병목이 아니다. 1 로 두면 순차 실행)
WORKERS = 4


# ---------------------------------------------------------------------------
def env_report():
    """boto3 가 없거나 점검 모드일 때 무엇을 해야 하는지 알려준다."""
    exe = sys.executable or "(python 경로 확인 불가)"
    return ('Spotfire python = %s  |  설치 명령: "%s" -m pip install boto3' % (exe, exe))


def make_client():
    if boto3 is None:
        raise RuntimeError("boto3 가 없습니다 (%s). %s" % (BOTO3_ERR, env_report()))
    if not (S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY and S3_ENDPOINT_URL):
        raise RuntimeError(
            "스크립트 상단 [S3 설정] 의 S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / "
            "S3_ENDPOINT_URL 을 채우세요.")
    return boto3.client(
        service_name="s3",
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        endpoint_url=S3_ENDPOINT_URL,
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
bucket = S3_BUCKET
prefix = S3_PREFIX
if prefix and not prefix.endswith("/"):
    prefix += "/"        # 없으면 폴더가 아니라 파일명에 붙는다

rows = []
seen = {}            # 같은 데이터가 여러 이름으로 들어오는지 확인용
started = dt.datetime.now()

try:
    if boto3 is None:
        raise RuntimeError("boto3 가 없습니다 (%s). %s" % (BOTO3_ERR, env_report()))
    if not USE_BOTO3:
        raise RuntimeError("USE_BOTO3=False (점검 모드). " + env_report())
    if not bucket:
        raise RuntimeError("스크립트 상단 [S3 설정] 의 S3_BUCKET 을 채우세요.")
    client = make_client()

    # ── 1) 입력 점검 (전역 변수 -> DataFrame) ────────────────────────────
    jobs = []
    for name in TABLE_NAMES:
        t0 = dt.datetime.now()
        df = globals().get(name)
        if df is None:
            rows.append([name, "SKIP", 0, 0, "", 0.0, 0.0,
                         "입력 파라미터 미등록", t0, dt.datetime.now()])
            continue
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        # 원천 조회 시각(SYSDATE). 업로드 시각만으로는 재조회 여부를 알 수 없다.
        qt = ""
        for c in df.columns:
            if str(c).lower() == "query_time":
                try:
                    qt = str(df[c].iloc[0]) if len(df) else ""
                except Exception:
                    qt = ""
                break

        cols_preview = ", ".join(map(str, df.columns[:6]))
        if len(df.columns) > 6:
            cols_preview += ", ..."

        sig = "%d|%d|%s" % (len(df), df.shape[1], ",".join(map(str, df.columns[:5])))
        dup = seen.get(sig)
        seen.setdefault(sig, name)
        if dup:
            rows.append([name, "DUP", len(df), df.shape[1], qt, 0.0, 0.0,
                         "'%s' 과(와) 내용이 같습니다. Input Parameter 매핑 확인. | %s"
                         % (dup, cols_preview), t0, dt.datetime.now()])
            continue

        jobs.append((name, df, qt))

    # ── 2) 직렬화 + 업로드 (병렬) ────────────────────────────────────────
    def work(job):
        name, df, qt = job
        t0 = dt.datetime.now()
        try:
            s0 = dt.datetime.now()
            buf = to_buffer(df, FMT)
            ser = (dt.datetime.now() - s0).total_seconds()
            size_mb = len(buf.getvalue()) / 1024.0 / 1024.0

            u0 = dt.datetime.now()
            key = "%s%s.%s" % (prefix, name, FMT)
            client.upload_fileobj(buf, bucket, key)
            up = (dt.datetime.now() - u0).total_seconds()

            return [name, "OK", len(df), df.shape[1], qt,
                    round(ser, 2), round(up, 2),
                    "%.1f MB  s3://%s/%s" % (size_mb, bucket, key),
                    t0, dt.datetime.now()]
        except Exception as e:
            return [name, "FAIL", len(df), df.shape[1], qt, 0.0, 0.0,
                    "%s: %s" % (type(e).__name__, e), t0, dt.datetime.now()]

    if WORKERS > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            rows.extend(ex.map(work, jobs))
    else:
        rows.extend(work(j) for j in jobs)

    # 8개를 다 올린 뒤 마지막에 매니페스트를 쓴다.
    #   업로드는 순차라 중간에 읽히면 서로 다른 시점의 파일이 섞인다.
    #   매니페스트가 '마지막에' 생기므로, 읽는 쪽은 이것만 보면
    #   '이번 회차가 완결됐는지' 를 알 수 있다.
    ok = [r for r in rows if r[1] == "OK"]
    qts = sorted({r[4] for r in ok if r[4]})
    manifest = {
        "run_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "query_time": qts[-1] if qts else "",       # 원천 조회 시각(SYSDATE)
        "query_time_all": qts,                      # 테이블별로 다르면 여기서 드러난다
        "fmt": FMT,
        "tables": {r[0]: {"rows": int(r[2]), "cols": int(r[3]), "query_time": r[4]}
                   for r in ok},
        "ok": len(ok),
        "total": len(TABLE_NAMES),
    }
    buf = io.BytesIO(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    client.upload_fileobj(buf, bucket, "%s_manifest.json" % prefix)
    rows.append(["_manifest.json", "OK", len(ok), 0, manifest["query_time"],
                 0.0, 0.0, "완결 표시. 읽는 쪽은 이 파일로 최신 여부를 판단한다.",
                 started, dt.datetime.now()])

except Exception as e:
    rows.append(["(전체)", "FAIL", 0, 0, "", 0.0, 0.0,
                 "%s: %s" % (type(e).__name__, e), started, dt.datetime.now()])

upload_log = pd.DataFrame(
    rows, columns=["table", "status", "rows", "cols", "query_time",
                   "serialize_sec", "upload_sec", "detail", "start", "end"])
upload_log["elapsed_sec"] = (
    upload_log["end"] - upload_log["start"]).dt.total_seconds().round(2)

# 스크립트 전체 소요. 이 값이 작은데 Spotfire 체감이 길면, 병목은 이 스크립트가
# 아니라 Spotfire -> python 데이터 전달(입력 마샬링) 이다.
upload_log["script_total_sec"] = round(
    (dt.datetime.now() - started).total_seconds(), 2)
