"""
spotfire_s3_export_tip_2.py — S3 적재 (s3drive_tip_2)

  TIP 3/6 조각

[등록]
  Tools > Register Data Functions
    Name              : s3drive_tip_2
    Type              : Python script
    Input Parameters  : 아래 1개만 Table 로 등록
                        PFR1_KFR7_TIP_2
    Output Parameters : upload_log_tip_2 (Table)
                        Output handler = Data table
                        Replace existing data table 체크
    Run location      : 일곱(이제 열셋) 함수를 모두 같게 둔다

  [중요] 한 함수에 표를 여러 개 물리면 나눈 뜻이 없다.
  Spotfire 는 스크립트를 돌리기 전에 등록된 입력을 전부
  메모리로 읽는다. 함수마다 제 표 하나씩만 등록한다.

  13개 함수를 모두 등록해야 25개 표가 다 올라간다.
  매니페스트는 서로 병합되므로 순서는 상관없다.
"""
import io
import json
import sys
import datetime as dt

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

# boto3 설치 전 점검용. False 로 두면 업로드하지 않고 환경 정보만 upload_log_tip_2 에
# 남긴다(파이썬 경로 / 설치 명령).
USE_BOTO3 = True
# ────────────────────────────────────────────────────────────────────────────

# 이 데이터 함수가 담당할 테이블.
#   8개를 한 함수에 몰면 Spotfire 가 입력을 pandas 로 넘기는 단계에서
#   메모리/임시디스크가 터진다(_read_inputs 에서 MemoryError).
#   함수를 여러 개로 나누고 각 함수의 TABLE_NAMES 를 아래처럼 줄인다.
#
#     s3drive_big1 : ["PFR1_KFR7_STEP_PATH"]
#     s3drive_big2 : ["PFR1_KFR7_TIP"]
#     s3drive_rest : 나머지 6개
#     s3drive_fab  : FabPlan 5개
#
#   매니페스트는 회차마다 병합되므로 어느 함수가 먼저 끝나든 상관없다.
#   ALL_TABLES 개가 모두 채워졌을 때만 완결로 본다.
# [메모리 부족으로 함수가 죽을 때]
#   Spotfire 는 스크립트를 돌리기 **전에** 등록된 입력을 전부 메모리로
#   읽는다. TIP(1,000만행) + STEP_PATH(700만행) 을 한 함수에 같이 두면
#   그 단계에서 죽는다.
#
#     numpy.core._exceptions._ArrayMemoryError: Unable to allocate ...
#     File "data_function.py", line 343, in _read_inputs
#
#   이때는 **데이터 함수를 나눠 등록한다.** 아래 TABLE_NAMES 만 바꾼
#   사본을 만들고, 각 함수에는 그 표들만 Input Parameter 로 등록한다.
#
#     s3drive_tip    ["PFR1_KFR7_TIP"]
#     s3drive_path   ["PFR1_KFR7_STEP_PATH"]
#     s3drive_rest   나머지 12개
#
#   매니페스트는 아래에서 기존 것을 읽어 병합하므로, 나눠 돌려도
#   마지막 함수가 14개 전부를 담은 완결 표시를 남긴다.
#   ALL_TABLES 는 그대로 14 로 둔다.
TABLE_NAMES = [
    "PFR1_KFR7_TIP_2",
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

# 파이프라인 전체가 기대하는 테이블 수. 함수를 나눠 등록해도 이 값은
# **전체 개수**로 둔다(각 함수의 TABLE_NAMES 길이가 아니다).
#   기존 9 + FabPlan 5 = 14
ALL_TABLES = 25


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
# 진행 로그. Spotfire 가 print 를 보여주지 않아 파일로 남긴다.
# 함수가 도는지, 어디서 멈추는지 확인하는 유일한 수단이다.
#
# [주의] Run location 이 Force Server 면 이 코드는 **서버에서** 돈다.
#   아래 PC 경로는 서버에 없어 아무것도 안 남는다. 그래서 못 쓰면
#   임시 폴더로 물러난다. 서버에서 돌 때도 흔적이 남는다.
TRACE_FILE = r"D:\PERSONAL_SPACE\SW\python\7_holdwaitanal\logs\spotfire_export.log"


def _trace_path():
    import os
    import tempfile
    for cand in (TRACE_FILE,
                 os.path.join(tempfile.gettempdir(), "spotfire_export.log")):
        try:
            d = os.path.dirname(cand)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(cand, "a", encoding="utf-8"):
                pass
            return cand
        except Exception:
            continue
    return None


_TRACE_PATH = _trace_path()


def trace(msg):
    if not _TRACE_PATH:
        return
    try:
        import socket
        who = socket.gethostname()
    except Exception:
        who = "?"
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write("%s  [%s]  %s\n"
                    % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), who, msg))
    except Exception:
        pass


trace("=" * 60)
trace("script start")

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
    trace("client ok  bucket=%s prefix=%s" % (bucket, prefix))

    # ── 1) 입력 점검 (전역 변수 -> DataFrame) ────────────────────────────
    jobs = []
    for name in TABLE_NAMES:
        t0 = dt.datetime.now()
        df = globals().get(name)
        if df is None:
            # 이름이 한 글자라도 다르면 globals 에 없다.
            # 실제로 들어온 DataFrame 이름을 함께 찍어 대조할 수 있게 한다.
            got = sorted(k for k, v in globals().items()
                         if isinstance(v, pd.DataFrame) and not k.startswith("_"))
            rows.append([name, "SKIP", 0, 0, "", 0.0, 0.0,
                         "입력 파라미터 미등록. 들어온 입력: "
                         + (", ".join(got) if got else "(없음)"),
                         t0, dt.datetime.now()])
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
        trace("input  %-30s %9d rows  qt=%s" % (name, len(df), qt))
        df = None

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

    # 순차 실행. 병렬은 실측상 이득이 없고(직렬화 6.5초), Spotfire 임베디드
    # python 에서 boto3 클라이언트를 스레드로 공유하면 멈출 수 있다.
    import gc
    for k in range(len(jobs)):
        r = work(jobs[k])
        rows.append(r)
        trace("upload %-30s %s  ser=%.1fs up=%.1fs" % (r[0], r[1], r[5], r[6]))
        jobs[k] = None          # 올린 DataFrame 은 바로 놓아준다
        gc.collect()

    # 8개를 다 올린 뒤 마지막에 매니페스트를 쓴다.
    #   업로드는 순차라 중간에 읽히면 서로 다른 시점의 파일이 섞인다.
    #   매니페스트가 '마지막에' 생기므로, 읽는 쪽은 이것만 보면
    #   '이번 회차가 완결됐는지' 를 알 수 있다.
    ok = [r for r in rows if r[1] == "OK"]

    # 기존 매니페스트를 읽어 병합한다. 함수를 여러 개로 나눠도
    # 마지막에 도는 함수가 8개 전부를 담은 매니페스트를 남기게 된다.
    # 세 함수가 같은 _manifest.json 을 나눠 쓴다. 방금 올린 것만 갈아 끼우고
    # 나머지는 그대로 둔다. **쓰기 직전에 다시 읽어** 그 사이 다른 함수가
    # 올린 것을 잃지 않게 한다.
    prev = {}
    try:
        obj = client.get_object(Bucket=bucket, Key="%s_manifest.json" % prefix)
        old = json.loads(obj["Body"].read().decode("utf-8"))
        # 같은 회차(=오늘) 것만 이어 붙인다. 날이 바뀌면 새로 시작.
        if str(old.get("run_at", ""))[:10] == started.strftime("%Y-%m-%d"):
            prev = old.get("tables") or {}
    except Exception:
        prev = {}

    now_s = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tables = dict(prev)
    for r in ok:
        # uploaded_at 이 있어야 어느 표가 낡았는지 읽는 쪽에서 가린다.
        tables[r[0]] = {"rows": int(r[2]), "cols": int(r[3]),
                        "query_time": r[4], "uploaded_at": now_s}

    qts = sorted({v.get("query_time") for v in tables.values() if v.get("query_time")})
    manifest = {
        "run_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "query_time": qts[-1] if qts else "",       # 원천 조회 시각(SYSDATE)
        "query_time_all": qts,                      # 테이블별로 다르면 여기서 드러난다
        "fmt": FMT,
        "tables": tables,
        "ok": len(tables),
        "total": ALL_TABLES,
    }
    buf = io.BytesIO(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    client.upload_fileobj(buf, bucket, "%s_manifest.json" % prefix)
    trace("manifest ok  ok=%d/%d" % (len(ok), len(TABLE_NAMES)))
    rows.append(["_manifest.json", "OK", len(ok), 0, manifest["query_time"],
                 0.0, 0.0, "완결 표시. 읽는 쪽은 이 파일로 최신 여부를 판단한다.",
                 started, dt.datetime.now()])

except Exception as e:
    import traceback
    trace("FAILED %s: %s" % (type(e).__name__, e))
    trace(traceback.format_exc())
    rows.append(["(전체)", "FAIL", 0, 0, "", 0.0, 0.0,
                 "%s: %s" % (type(e).__name__, e), started, dt.datetime.now()])

upload_log_tip_2 = pd.DataFrame(
    rows, columns=["table", "status", "rows", "cols", "query_time",
                   "serialize_sec", "upload_sec", "detail", "start", "end"])
upload_log_tip_2["elapsed_sec"] = (
    upload_log_tip_2["end"] - upload_log_tip_2["start"]).dt.total_seconds().round(2)

# 스크립트 전체 소요. 이 값이 작은데 Spotfire 체감이 길면, 병목은 이 스크립트가
# 아니라 Spotfire -> python 데이터 전달(입력 마샬링) 이다.
upload_log_tip_2["script_total_sec"] = round(
    (dt.datetime.now() - started).total_seconds(), 2)
trace("script end  rows=%d  total=%.1fs"
      % (len(upload_log_tip_2), (dt.datetime.now() - started).total_seconds()))
