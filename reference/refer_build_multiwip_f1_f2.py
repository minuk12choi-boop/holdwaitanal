# -*- coding: utf-8 -*-
r"""
Spotfire export CSV 4개(m/s/t/h)를 DuckDB로 조합하여 검증용 f1/f2/f3 결과를 생성한다.

Windows PowerShell 패키지 설치:
    pip install duckdb pandas openpyxl pyarrow

Windows PowerShell 실행 예:
    cd D:\PERSONAL_SPACE\SW\python
    python D:\PERSONAL_SPACE\SW\python\5_multiwip\build_multiwip_f1_f2.py

주의:
- 입력 CSV는 Excel로 열어 검증하지 않고 Python/DuckDB에서 직접 전체 행을 읽는다.
- 결과 Excel은 openpyxl 시트 한계를 피하기 위해 1,000,000행 단위로 분할한다.
- Parquet은 대용량 원본 검증 및 추후 DB 적재 전환을 쉽게 하기 위해 항상 저장한다.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from contextlib import contextmanager
from time import perf_counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import duckdb
    import pandas as pd
except ImportError as exc:
    print("[ERROR] 필수 패키지가 없습니다. Windows PowerShell에서 아래 명령으로 설치하세요.")
    print("pip install duckdb pandas openpyxl pyarrow")
    raise


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
EXCEL_DIR = OUTPUT_DIR / "excel"
PARQUET_DIR = OUTPUT_DIR / "parquet"
LOG_DIR = OUTPUT_DIR / "log"

CSV_ENCODING = "utf-16"
EXCEL_MAX_ROWS_PER_SHEET = 1_000_000
EXCEL_CSV_FALLBACK_ROWS = 5_000_000

# 입력 CSV 파일명(고정) — 웹 업로드 페이지와 파이프라인이 공유하는 canonical 이름.
INPUT_FILENAMES = {
    "m": "rndplan_mc_lot.csv",
    "s": "rndplan_step.csv",
    "t": "memory_tip.csv",
    "h": "memory_hold.csv",
}

# 입력 파일 디렉터리: 환경변수 MULTIWIP_INPUT_DIR가 있으면 그 폴더를, 없으면 기존 Documents 경로를 사용한다.
# (기본 동작은 그대로 유지되며, 웹 업로드 기능은 이 환경변수로만 경로를 바꿔 끼운다.)
_DEFAULT_INPUT_DIR = Path(r"C:\Users\minuk12.choi\Documents")
INPUT_DIR = Path(os.environ.get("MULTIWIP_INPUT_DIR") or _DEFAULT_INPUT_DIR)

INPUT_FILES = {alias: INPUT_DIR / name for alias, name in INPUT_FILENAMES.items()}

DATE_CHECK_TARGETS = {
    "m": ["START_DATE", "LAST_TKOUT_DATE", "STEP_ARRIVE_DATE", "LAST_EVENT_DATE"],
    "h": ["HOLD_DATE"],
    "t": ["TIP_EVENTTIME", "EQPISSUETIME"],
    "f1": ["TIP_EVENTTIME", "EQPISSUETIME", "hold_date", "exception_date", "ftp_date"],
}

SUMMARY_OUTPUT_COLUMNS = [
    "lot_inform",
    "line",
    "현재위치",
    "전산라인",
    "투입라인",
    "lot_id",
    "carr_id",
    "grade",
    "lot_type",
    "lot_level",
    "qty",
    "bay",
    "sendfab",
    "투입경과_일",
    "마지막이벤트경과_일",
    "스텝도착경과_일",
    "lot_status",
    "step_status",
    "proc_id",
    "de_rank",
    "연속",
    "AREA",
    "layer_id",
    "현스텝",
    "order_seq",
    "step_seq",
    "step_desc",
    "recipe_id",
    "eqp_type",
    "batch_kind",
    "eqpline",
    "eqpgroup",
    "eqpgroup_cham",
    "tip",
    "down",
    "hold",
    "hold_reason",
    "exception",
    "exception_reason",
    "ftp",
    "ftp_reason",
]

BLOCKED_SUMMARY_COLUMNS = {"eqpid", "eqpcham"}



TODO_ITEMS = [
    "s.eqp_status_change_time 요청이 있었으나 현재 s CSV 필수 컬럼/실제 로드 컬럼에서 확인되지 않으면 fallback 적용 불가합니다. EQPISSUETIME fallback을 적용하려면 rndplan_step.csv에 EQP_STATUS_CHANGE_TIME 컬럼이 추가되어야 합니다.",
    "t 정확 매칭과 wildcard 매칭 중복 제거 기준은 요청 기준 컬럼으로 구현했으나, 실제 데이터에서 동일 기준 내 복수 이벤트가 의미 있는 중복이면 추가 키 확인이 필요합니다.",
    "f2/f3 eqpline은 LINE, LOT_ID, ORDER_SEQ 기준 고유 EQPLINE 값을 숫자 우선 정렬 후 concatenate합니다. 현스텝이 연속공정일 때 DE_RANK 전체 기준으로 바꿀지는 업무 확인이 필요합니다.",
    "hold/exception/ftp가 동일 LOT/STEP에 여러 건 존재할 경우 현재 left join 결과가 행을 늘릴 수 있습니다. 최신 1건만 사용할지 전체 이력을 유지할지 확인이 필요합니다.",
    "TIP/PREVENT 동일 설비의 서로 다른 경과일 이벤트는 실제 별도 이력인지 확정 전까지 설비명+경과일 기준으로 distinct 처리합니다.",
]


@dataclass
class RunStats:
    input_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    validations: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    date_failures: dict[str, dict[str, Any]] = field(default_factory=dict)
    todo_items: list[str] = field(default_factory=lambda: list(TODO_ITEMS))




@contextmanager
def timer(label: str):
    start = perf_counter()
    print(f"[TIMER] {label} start")
    try:
        yield
    finally:
        print(f"[TIMER] {label} elapsed={perf_counter() - start:.3f}s")

def log_table_count(con, table_name: str, stats, key: str | None = None) -> int:
    count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    stats.counts[key or f"{table_name}_rows_logged"] = count
    print(f"[ROWS] {table_name} rows={count:,}")
    return count

def sql_text(value: str | Path) -> str:
    """DuckDB SQL 문자열 리터럴을 안전하게 만든다."""
    return str(value).replace("'", "''")


def ensure_output_dirs() -> None:
    """결과 저장 폴더를 생성한다."""
    for directory in (OUTPUT_DIR, EXCEL_DIR, PARQUET_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def csv_row_count(path: Path, encoding: str = CSV_ENCODING) -> int | None:
    """CSV 전체 행 수를 헤더 제외 기준으로 계산한다."""
    if not path.exists():
        return None
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        total = sum(1 for _ in reader)
    return max(total - 1, 0)


def csv_data_line_count(path: Path, encoding: str = CSV_ENCODING) -> int | None:
    """CSV의 데이터 행 수(헤더 제외)를 센다. ignore_errors로 누락된 행 탐지에 사용한다.

    csv.reader를 쓰므로 값 안의 개행도 1행으로 올바르게 계산된다.
    """
    return csv_row_count(path, encoding)


def file_size_mb(path: Path) -> float | None:
    """파일 크기를 MB 단위로 반환한다."""
    if not path.exists():
        return None
    return round(path.stat().st_size / (1024 * 1024), 2)


def normalize_empty_expr(column_name: str) -> str:
    """빈 문자열을 NULL처럼 다루기 위한 SQL 표현식을 만든다."""
    return f"NULLIF(TRIM(CAST({column_name} AS VARCHAR)), '')"


def quote_ident(identifier: str) -> str:
    """DuckDB 식별자를 안전하게 큰따옴표로 감싼다."""
    return '"' + identifier.replace('"', '""') + '"'


def prefixed_column_list(columns: list[str], table_alias: str | None = None, indent: str = "            ") -> str:
    """명시적 SELECT 컬럼 목록을 만든다."""
    prefix = f"{table_alias}." if table_alias else ""
    return ",\n".join(f"{indent}{prefix}{quote_ident(column)}" for column in columns)


def summary_export_query(table_name: str) -> str:
    """f2/f3 저장에 사용할 최종 테이블 기준 명시적 컬럼 SELECT를 만든다."""
    columns = prefixed_column_list(SUMMARY_OUTPUT_COLUMNS)
    return f"""
        SELECT
{columns}
        FROM {table_name}
        ORDER BY line, lot_id, TRY_CAST(order_seq AS BIGINT) NULLS LAST, order_seq, eqpgroup_cham
    """


def summary_distinct_query(source_table_name: str) -> str:
    """summary 최종 출력 컬럼 projection 후 DISTINCT 하는 SELECT를 만든다."""
    columns = prefixed_column_list(SUMMARY_OUTPUT_COLUMNS)
    return f"""
        SELECT DISTINCT
{columns}
        FROM {source_table_name}
    """


def record_distinct_counts(
    con: duckdb.DuckDBPyConnection,
    stats: RunStats,
    table_name: str,
    before_table_name: str,
    after_table_name: str,
) -> None:
    """최종 산출물 distinct 전/후 row 수와 제거 row 수를 기록한다."""
    before_count = con.execute(f"SELECT COUNT(*) FROM {before_table_name}").fetchone()[0]
    after_count = con.execute(f"SELECT COUNT(*) FROM {after_table_name}").fetchone()[0]
    removed_count = before_count - after_count
    stats.counts[f"{table_name}_rows_before_distinct"] = before_count
    stats.counts[f"{table_name}_rows_after_distinct"] = after_count
    stats.counts[f"{table_name}_duplicate_rows_removed"] = removed_count
    print(
        f"[INFO] {table_name} 중복 제거 전 row 수 = {before_count:,}, "
        f"중복 제거 후 row 수 = {after_count:,}, 제거된 중복 row 수 = {removed_count:,}"
    )


def describe_query_columns(con: duckdb.DuckDBPyConnection, query: str) -> list[str]:
    """DuckDB DESCRIBE 결과에서 컬럼명을 순서대로 반환한다."""
    return [row[0] for row in con.execute(f"DESCRIBE {query}").fetchall()]


def validate_no_blocked_summary_columns(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    stats: RunStats,
    columns: list[str] | None = None,
) -> None:
    """f2/f3 최종 컬럼에 eqpid/eqpcham이 노출되지 않는지 확인한다."""
    is_table_schema_check = columns is None
    checked_columns = table_columns(con, table_name) if is_table_schema_check else columns
    if is_table_schema_check:
        stats.validations[f"{table_name}_final_columns"] = checked_columns
    print(f"[INFO] {table_name} 최종 컬럼 목록: {', '.join(checked_columns)}")
    blocked = [column for column in checked_columns if column.lower() in BLOCKED_SUMMARY_COLUMNS]
    if blocked:
        message = f"[ERROR] {table_name} 최종 컬럼에 eqpid/eqpcham이 남아 있습니다"
        stats.validations[f"{table_name}_blocked_columns"] = blocked
        print(message)
        raise RuntimeError(f"{message}: {', '.join(blocked)}")
    stats.validations[f"{table_name}_blocked_columns"] = []


def validate_summary_export_columns(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """저장용 SELECT 컬럼 목록도 실제로 eqpid/eqpcham을 포함하지 않는지 확인한다."""
    for table_name in ("f2", "f3"):
        query = summary_export_query(table_name)
        columns = describe_query_columns(con, query)
        stats.validations[f"{table_name}_export_columns"] = columns
        print(f"[INFO] {table_name} 저장 SELECT 컬럼 목록: {', '.join(columns)}")
        validate_no_blocked_summary_columns(con, table_name, stats, columns)


def validate_parquet_columns(con: duckdb.DuckDBPyConnection, table_name: str, parquet_path: Path, stats: RunStats) -> None:
    """저장된 f2/f3 Parquet 컬럼 목록을 확인한다."""
    columns = describe_query_columns(con, f"SELECT * FROM read_parquet('{sql_text(parquet_path)}')")
    stats.validations[f"{table_name}_parquet_columns"] = columns
    print(f"[INFO] {table_name} Parquet 컬럼 목록: {', '.join(columns)}")
    validate_no_blocked_summary_columns(con, table_name, stats, columns)


def table_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
    """DuckDB table/view의 실제 로드 컬럼명을 순서대로 반환한다."""
    return [
        row[0]
        for row in con.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],
        ).fetchall()
    ]


def find_column_case_insensitive(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    candidates: list[str],
) -> str | None:
    """후보 컬럼명을 대소문자 무시로 실제 DuckDB 컬럼 목록에서 찾는다."""
    columns = table_columns(con, table_name)
    lookup = {column.upper(): column for column in columns}
    for candidate in candidates:
        found = lookup.get(candidate.upper())
        if found is not None:
            return found
    return None


def parsed_timestamp_expr(column_name: str) -> str:
    """한국어 오전/오후 날짜 문자열을 TIMESTAMP로 변환하는 표현식.

    성능: 이전에는 값 하나당 REGEXP_EXTRACT를 10회 이상 호출했으나,
    '오전/오후'를 AM/PM으로 치환한 뒤 TRY_STRPTIME 1회로 처리한다.
    (기존 표현식과 10,000건 이상 무작위/경계값 비교로 동등성 검증, 약 2.5배 빠름)
    - '오전 12시'->00시, '오후 12시'->12시는 %p/%I가 그대로 처리한다.
    - '오전/오후 00:xx'(비표준 표기)는 12로 정규화해 기존 regex 동작과 결과를 맞춘다.
    - 그 외 형식(24시간제 등)은 TRY_CAST fallback으로 처리한다.
    """
    normalized_expr = (
        "REPLACE(REPLACE("
        "REGEXP_REPLACE(TRIM(CAST(" + column_name + " AS VARCHAR)), "
        "'(오전|오후) 0?0:', '\\1 12:')"
        ", '오전', 'AM'), '오후', 'PM')"
    )
    return (
        "COALESCE("
        "TRY_STRPTIME(" + normalized_expr + ", '%Y-%m-%d %p %I:%M:%S'), "
        "TRY_CAST(" + column_name + " AS TIMESTAMP))"
    )


def elapsed_days_number_expr(column_name: str) -> str:
    """현재시각 기준 경과일을 소수점 한 자리 '숫자'로 만든다.

    기존에는 '3.2일↑' 문자열이라 정렬/집계/필터가 불가능했다. 단위 표기는 웹에서 붙인다.
    NULL은 산술에서 자연히 전파되므로 CASE 래핑이 없어도 되고, 그 덕에 날짜 파싱이
    2회에서 1회로 줄어든다.
    """
    parsed_expr = parsed_timestamp_expr(column_name)
    return f"ROUND((EPOCH(CURRENT_TIMESTAMP) - EPOCH({parsed_expr})) / 86400.0, 1)"


def elapsed_days_expr(column_name: str) -> str:
    """tip/down 등 사람이 읽는 라벨 안에 들어갈 경과일 문자열('3.2일↑')을 만든다.

    FORMAT은 NULL을 그대로 전파하므로 기존 CASE 래핑과 결과가 같다.
    """
    return "FORMAT('{:.1f}', " + elapsed_days_number_expr(column_name) + ") || '일↑'"


def load_csv_to_duckdb(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """입력 CSV를 DuckDB table로 적재한다."""
    print("[START] 입력 CSV 로드 시작")
    for alias, path in INPUT_FILES.items():
        stats.input_info[alias] = {
            "path": str(path),
            "size_mb": file_size_mb(path),
            # rows는 DuckDB 적재 후 COUNT(*)로 채운다(파이썬 전수 스캔 1패스 제거).
            "rows": None,
            "exists": path.exists(),
        }
        if not path.exists():
            raise FileNotFoundError(f"입력 파일을 찾을 수 없습니다: {path}")

        con.execute(f"DROP TABLE IF EXISTS {alias}")
        con.execute(
            f"""
            CREATE TABLE {alias} AS
            SELECT *
            FROM read_csv_auto(
                '{sql_text(path)}',
                header = true,
                all_varchar = true,
                encoding = '{CSV_ENCODING}',
                sample_size = -1,
                ignore_errors = true
            )
            """
        )
        row_count = con.execute(f"SELECT COUNT(*) FROM {alias}").fetchone()[0]
        stats.counts[f"{alias}_rows"] = row_count
        stats.input_info[alias]["rows"] = row_count
        stats.input_info[alias]["duckdb_rows"] = row_count
        # ignore_errors=true로 조용히 누락된 행이 있는지 확인한다(원본 라인 수와 비교).
        raw_lines = csv_data_line_count(path)
        stats.input_info[alias]["csv_data_lines"] = raw_lines
        skipped = (raw_lines - row_count) if raw_lines is not None else None
        stats.input_info[alias]["skipped_rows"] = skipped
        if skipped:
            msg = f"{alias}: CSV 데이터 라인 {raw_lines:,}건 중 {skipped:,}건이 파싱 실패로 누락되었습니다({path.name})."
            print(f"[WARN] {msg}")
            stats.validations[f"{alias}_skipped_rows"] = skipped
            if msg not in stats.todo_items:
                stats.todo_items.append(msg)
        else:
            stats.validations[f"{alias}_skipped_rows"] = 0
        print(f"[OK] {alias} rows = {row_count:,}")


def build_h_views(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """hold 원천을 h1/h2/h3로 분리한다."""
    h_filters = {
        "h1": "ITEM_TYPE IN ('HOLD LOT', 'FUTUREHOLD')",
        "h2": "ITEM_TYPE = 'EXCEPTION'",
        "h3": "ITEM_TYPE = 'FTkinPvLot'",
    }
    for view_name, condition in h_filters.items():
        con.execute(f"DROP TABLE IF EXISTS {view_name}")
        con.execute(f"CREATE TABLE {view_name} AS SELECT * FROM h WHERE {condition}")
        stats.counts[f"{view_name}_rows"] = con.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]


def build_f1(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """m 기준으로 s/t/h를 조합하여 raw 조합 결과 f1을 생성한다."""
    s_eqp_status_change_time_col = find_column_case_insensitive(
        con,
        "s",
        ["EQP_STATUS_CHANGE_TIME", "eqp_status_change_time"],
    )
    s_area_col = find_column_case_insensitive(con, "s", ["AREA", "area"])
    s_eqp_status_change_time_expr = (
        f"s.{quote_ident(s_eqp_status_change_time_col)}"
        if s_eqp_status_change_time_col is not None
        else "CAST(NULL AS VARCHAR)"
    )
    s_area_expr = f"s.{quote_ident(s_area_col)}" if s_area_col is not None else "CAST(NULL AS VARCHAR)"
    eqpissuetime_expr = (
        "COALESCE(tm.EQPISSUETIME, ms.s_EQP_STATUS_CHANGE_TIME)"
        if s_eqp_status_change_time_col is not None
        else "tm.EQPISSUETIME"
    )
    stats.validations["eqpissuetime_fallback_column_exists"] = s_eqp_status_change_time_col is not None
    stats.validations["eqpissuetime_fallback_column_name"] = s_eqp_status_change_time_col
    stats.validations["eqpissuetime_fallback_available"] = s_eqp_status_change_time_col is not None
    if s_eqp_status_change_time_col is None:
        message = (
            "s.eqp_status_change_time 요청이 있었으나 현재 s CSV 필수 컬럼/실제 로드 컬럼에서 "
            "확인되지 않아 fallback 적용 불가. EQPISSUETIME fallback을 적용하려면 "
            "rndplan_step.csv에 EQP_STATUS_CHANGE_TIME 컬럼이 추가되어야 함"
        )
        print(f"[WARN] {message}")
        stats.validations["eqpissuetime_fallback_message"] = message
    else:
        print(f"[OK] EQPISSUETIME fallback 컬럼 확인: s.{s_eqp_status_change_time_col}")

    stats.validations["s_area_column_exists"] = s_area_col is not None
    stats.validations["s_area_column_name"] = s_area_col
    print(f"[INFO] s AREA 컬럼 존재 여부 = {s_area_col is not None}")
    print(f"[INFO] s AREA 컬럼 실제 사용명 = {s_area_col}")
    if s_area_col is None:
        area_message = "s AREA 컬럼이 없어 AREA는 null로 출력됨. rndplan_step.csv에 AREA 컬럼 추가 후 값이 채워짐"
        print(f"[WARN] {area_message}")
        stats.validations["s_area_fallback_message"] = area_message
        if area_message not in stats.todo_items:
            stats.todo_items.append(area_message)
    else:
        stats.validations["s_area_fallback_message"] = "AREA 컬럼 사용 가능"

    con.execute("DROP TABLE IF EXISTS m_base")
    con.execute(
        """
        CREATE TABLE m_base AS
        SELECT ROW_NUMBER() OVER () AS m_row_id, *
        FROM m
        """
    )

    con.execute("DROP TABLE IF EXISTS ms_joined")
    con.execute(
        f"""
        CREATE TABLE ms_joined AS
        SELECT
            ROW_NUMBER() OVER () AS ms_row_id,
            m.m_row_id,
            m.LOT_INFORM, m.LINE, m.CUR_LINE_ID, m.SYS_LINE_ID, m.ORIGIN_LINE_ID,
            m.LOT_ID, m.CARR_ID, m.GRADE, m.LOT_TYPE, m.LOT_LEVEL, m.CUR_QTY,
            m.BAY_NAME, m.SENDFAB,
            m.START_DATE, m.LAST_EVENT_DATE, m.STEP_ARRIVE_DATE,
            m.STATUS, m.PROC_ID AS m_PROC_ID,
            m.ORDER_SEQ AS m_ORDER_SEQ, m.STEP_SEQ AS m_STEP_SEQ,
            s.PROC_ID, s.ORDER_SEQ, s.DE_RANK, s."연속", {s_area_expr} AS AREA,
            s.LAYER_ID, s.STEP_LEVEL,
            s.EIN, s.STEP_SEQ, s.STEP_DESC, s.EQP_TYPE, s.RECIPE_ID, s.EQP_ID,
            s.BATCH_KIND, s.EQPLINE, s.BODY_STATUS,
            {s_eqp_status_change_time_expr} AS s_EQP_STATUS_CHANGE_TIME,
            CASE WHEN m.ORDER_SEQ = s.ORDER_SEQ THEN '현스텝' END AS "현스텝"
        FROM m_base m
        LEFT JOIN s
          ON m.LINE = s.LINE
         AND m.LOT_ID = s.LOT_ID
        """
    )

    con.execute("DROP TABLE IF EXISTS t0")
    con.execute(
        """
        CREATE TABLE t0 AS
        SELECT *
        FROM t
        WHERE COALESCE(NULLIF(TRIM(PROCESS), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(STEP), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(PPID), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(EQPID), ''), '-') = '-'
        """
    )

    con.execute("DROP TABLE IF EXISTS t_matches_raw")
    con.execute(
        """
        CREATE TABLE t_matches_raw AS
        SELECT
            ms.ms_row_id,
            t.LINE AS t_LINE, t.PROCESS, t.STEP, t.PPID, t.EQPID, t.EQPCHAM,
            t.CHAMBERID, t.LOT_TYPE AS t_LOT_TYPE, t.BATCH_KIND AS t_BATCH_KIND,
            t.PREVENT, t.TYPE_BODY, t.TYPE_CHAM, t.TIP_EVENTTIME,
            t.EQPISSUE, t.BODY_EQP_STATUS, t.CHAM_EQP_STATUS, t.EQPISSUETIME,
            t.EQPLINE AS t_EQPLINE,
            '정확' AS match_type
        FROM ms_joined ms
        INNER JOIN t
          ON ms.LINE = t.LINE
         AND ms.LOT_TYPE = t.LOT_TYPE
         AND ms.PROC_ID = t.PROCESS
         AND ms.STEP_SEQ = t.STEP
         AND ms.RECIPE_ID = t.PPID
         AND ms.EQP_ID = t.EQPID

        UNION ALL

        SELECT
            ms.ms_row_id,
            t0.LINE AS t_LINE, t0.PROCESS, t0.STEP, t0.PPID, t0.EQPID, t0.EQPCHAM,
            t0.CHAMBERID, t0.LOT_TYPE AS t_LOT_TYPE, t0.BATCH_KIND AS t_BATCH_KIND,
            t0.PREVENT, t0.TYPE_BODY, t0.TYPE_CHAM, t0.TIP_EVENTTIME,
            t0.EQPISSUE, t0.BODY_EQP_STATUS, t0.CHAM_EQP_STATUS, t0.EQPISSUETIME,
            t0.EQPLINE AS t_EQPLINE,
            'wildcard' AS match_type
        FROM ms_joined ms
        INNER JOIN t0
          ON ms.LINE = t0.LINE
         AND ms.LOT_TYPE = t0.LOT_TYPE
         AND (COALESCE(NULLIF(TRIM(t0.PROCESS), ''), '-') = '-' OR ms.PROC_ID = t0.PROCESS)
         AND (COALESCE(NULLIF(TRIM(t0.STEP), ''), '-') = '-' OR ms.STEP_SEQ = t0.STEP)
         AND (COALESCE(NULLIF(TRIM(t0.PPID), ''), '-') = '-' OR ms.RECIPE_ID = t0.PPID)
         AND (COALESCE(NULLIF(TRIM(t0.EQPID), ''), '-') = '-' OR ms.EQP_ID = t0.EQPID)
        """
    )

    con.execute("DROP TABLE IF EXISTS t_matches")
    con.execute(
        """
        CREATE TABLE t_matches AS
        SELECT * EXCLUDE (rn)
        FROM (
            SELECT
                tmr.*,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        tmr.ms_row_id, ms.LINE, ms.LOT_ID, ms.ORDER_SEQ, ms.STEP_SEQ, ms.RECIPE_ID, ms.EQP_ID,
                        tmr.EQPCHAM, tmr.PREVENT, tmr.EQPISSUE, tmr.TIP_EVENTTIME, tmr.EQPISSUETIME
                    ORDER BY CASE WHEN tmr.match_type = '정확' THEN 0 ELSE 1 END
                ) AS rn
            FROM t_matches_raw tmr
            INNER JOIN ms_joined ms ON tmr.ms_row_id = ms.ms_row_id
        )
        WHERE rn = 1
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_base")
    con.execute(
        f"""
        CREATE TABLE f1_base AS
        SELECT
            ms.*,
            tm.EQPID AS t_EQPID,
            tm.EQPCHAM,
            tm.PREVENT,
            tm.TYPE_BODY,
            tm.TYPE_CHAM,
            tm.TIP_EVENTTIME,
            COALESCE(tm.EQPISSUE, CASE WHEN ms.BODY_STATUS IN ('LOCAL', 'PM', 'DOWN') THEN ms.BODY_STATUS END) AS eqpissue,
            COALESCE(tm.BODY_EQP_STATUS, ms.BODY_STATUS) AS body_eqp_status,
            tm.CHAM_EQP_STATUS,
            tm.EQPISSUETIME AS t_EQPISSUETIME,
            {eqpissuetime_expr} AS EQPISSUETIME,
            COALESCE(ms.EQP_ID, tm.EQPID) AS eqpid,
            COALESCE(tm.EQPCHAM, ms.EQP_ID) AS eqpcham,
            h1.HOLD_USER AS hold,
            h1.HOLD_REASON AS hold_reason,
            h1.HOLD_DATE AS hold_date,
            h2.HOLD_USER AS exception,
            h2.HOLD_REASON AS exception_reason,
            h2.HOLD_DATE AS exception_date,
            h3.HOLD_USER AS ftp,
            h3.HOLD_REASON AS ftp_reason,
            h3.HOLD_DATE AS ftp_date,
            CASE
                WHEN tm.PREVENT = 'PREVENT'
                  OR tm.EQPISSUE IS NOT NULL
                  OR h2.HOLD_USER IS NOT NULL
                  OR h3.HOLD_USER IS NOT NULL
                  OR ms.BODY_STATUS IN ('LOCAL', 'PM', 'DOWN')
                THEN 'ISSUE'
            END AS issue_step
        FROM ms_joined ms
        LEFT JOIN t_matches tm ON ms.ms_row_id = tm.ms_row_id
        LEFT JOIN h1
          ON ms.LINE = h1.LINE_ID
         AND ms.LOT_ID = h1.LOT_ID
         AND ms.STEP_SEQ = h1.STEP_SEQ
        LEFT JOIN h2
          ON ms.LINE = h2.LINE_ID
         AND ms.LOT_ID = h2.LOT_ID
         AND ms.STEP_SEQ = h2.STEP_SEQ
        LEFT JOIN h3
          ON ms.LINE = h3.LINE_ID
         AND ms.LOT_ID = h3.LOT_ID
         AND ms.STEP_SEQ = h3.STEP_SEQ
        """
    )

    if s_eqp_status_change_time_col is not None:
        stats.validations["eqpissuetime_fallback_filled_rows"] = con.execute(
            """
            SELECT COUNT(*)
            FROM f1_base
            WHERE NULLIF(TRIM(CAST(t_EQPISSUETIME AS VARCHAR)), '') IS NULL
              AND NULLIF(TRIM(CAST(s_EQP_STATUS_CHANGE_TIME AS VARCHAR)), '') IS NOT NULL
              AND NULLIF(TRIM(CAST(EQPISSUETIME AS VARCHAR)), '') IS NOT NULL
            """
        ).fetchone()[0]
    else:
        stats.validations["eqpissuetime_fallback_filled_rows"] = 0
    stats.validations["eqpissuetime_null_after_fallback_rows"] = con.execute(
        """
        SELECT COUNT(*)
        FROM f1_base
        WHERE NULLIF(TRIM(CAST(EQPISSUETIME AS VARCHAR)), '') IS NULL
        """
    ).fetchone()[0]

    con.execute("DROP TABLE IF EXISTS f1_counts")
    con.execute(
        """
        CREATE TABLE f1_counts AS
        SELECT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            COUNT(DISTINCT eqpcham) AS path_count,
            COUNT(DISTINCT CASE WHEN issue_step IS NOT NULL THEN eqpcham END) AS issue_count
        FROM f1_base
        GROUP BY LINE, LOT_ID, ORDER_SEQ
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_groups")
    con.execute(
        """
        CREATE TABLE f1_groups AS
        SELECT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            STRING_AGG(DISTINCT eqpid, ', ' ORDER BY eqpid) FILTER (WHERE eqpid IS NOT NULL) AS eqpgroup,
            STRING_AGG(DISTINCT eqpcham, ', ' ORDER BY eqpcham) FILTER (WHERE eqpcham IS NOT NULL) AS eqpgroup_cham_raw
        FROM f1_base
        GROUP BY LINE, LOT_ID, ORDER_SEQ
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_status_base")
    con.execute(
        """
        CREATE TABLE f1_status_base AS
        SELECT
            fb.*,
            COALESCE(fc.issue_count, 0) AS issue_count,
            COALESCE(fc.path_count, 0) AS path_count,
            CASE
                -- STEP_STATUS는 개별 STEP_SEQ 행 상태만 산출한다.
                -- HOLD/FTP/EXCEPTION 등 LOT 단위 EXCLUSION은 현스텝에서만 STEP_STATUS에 반영하고 후속 STEP에 복사하지 않는다.
                WHEN fb."현스텝" = '현스텝' AND fb.STATUS = 'HOLD' THEN 'HOLD'
                WHEN fb."현스텝" = '현스텝' AND (fb.hold IS NOT NULL OR fb.exception IS NOT NULL OR fb.ftp IS NOT NULL) THEN 'WAIT(진행불가)'
                WHEN fb.STATUS = 'WAIT' AND COALESCE(fc.path_count, 0) > 0 AND COALESCE(fc.issue_count, 0) > 0 AND COALESCE(fc.issue_count, 0) >= COALESCE(fc.path_count, 0)
                THEN 'WAIT(진행불가)'
                WHEN fb."현스텝" IS DISTINCT FROM '현스텝' AND fb.STATUS IN ('HOLD', 'RUN') THEN 'WAIT'
                ELSE fb.STATUS
            END AS step_status,
            CASE
                WHEN fb."현스텝" = '현스텝' AND (fb.hold IS NOT NULL OR fb.exception IS NOT NULL OR fb.ftp IS NOT NULL OR fb.STATUS = 'HOLD') THEN 1
                ELSE 0
            END AS current_exclusion_step_flag,
            CASE
                WHEN fb."현스텝" IS DISTINCT FROM '현스텝' AND (fb.hold IS NOT NULL OR fb.exception IS NOT NULL OR fb.ftp IS NOT NULL) THEN 1
                ELSE 0
            END AS non_current_exclusion_input_flag,
            fg.eqpgroup,
            fg.eqpgroup_cham_raw,
            COALESCE(NULLIF(TRIM(CAST(fg.eqpgroup_cham_raw AS VARCHAR)), ''), fg.eqpgroup) AS eqpgroup_cham
        FROM f1_base fb
        LEFT JOIN f1_counts fc
          ON fb.LINE = fc.LINE
         AND fb.LOT_ID = fc.LOT_ID
         AND fb.ORDER_SEQ = fc.ORDER_SEQ
        LEFT JOIN f1_groups fg
          ON fb.LINE = fg.LINE
         AND fb.LOT_ID = fg.LOT_ID
         AND fb.ORDER_SEQ = fg.ORDER_SEQ
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_current")
    con.execute(
        """
        CREATE TABLE f1_current AS
        SELECT
            LINE,
            LOT_ID,
            MAX(ORDER_SEQ) FILTER (WHERE "현스텝" = '현스텝') AS current_order_seq,
            MAX(DE_RANK) FILTER (WHERE "현스텝" = '현스텝') AS current_de_rank,
            MAX(NULLIF(TRIM(CAST("연속" AS VARCHAR)), '')) FILTER (WHERE "현스텝" = '현스텝') AS current_continuous,
            CASE
                WHEN MAX(CASE WHEN "현스텝" = '현스텝' AND step_status = 'HOLD' THEN 1 ELSE 0 END) > 0 THEN 'HOLD'
                WHEN MAX(CASE WHEN "현스텝" = '현스텝' AND step_status = 'WAIT(진행불가)' THEN 1 ELSE 0 END) > 0 THEN 'WAIT(진행불가)'
                WHEN MAX(CASE WHEN "현스텝" = '현스텝' AND step_status = 'WAIT' THEN 1 ELSE 0 END) > 0 THEN 'WAIT'
                WHEN MAX(CASE WHEN "현스텝" = '현스텝' AND step_status = 'RUN' THEN 1 ELSE 0 END) > 0 THEN 'RUN'
                ELSE MAX(step_status) FILTER (WHERE "현스텝" = '현스텝')
            END AS current_step_status,
            MAX(current_exclusion_step_flag) FILTER (WHERE "현스텝" = '현스텝') AS current_exclusion_step_flag
        FROM f1_status_base
        GROUP BY LINE, LOT_ID
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_blocked_rank")
    con.execute(
        """
        CREATE TABLE f1_blocked_rank AS
        SELECT LINE, LOT_ID, DE_RANK, COUNT(*) AS blocked_rows
        FROM f1_status_base
        WHERE step_status = 'WAIT(진행불가)'
        GROUP BY LINE, LOT_ID, DE_RANK
        """
    )

    con.execute("DROP TABLE IF EXISTS f1")
    con.execute("DROP TABLE IF EXISTS f1_raw")
    con.execute(
        f"""
        CREATE TABLE f1_raw AS
        SELECT
            fsb.LOT_INFORM,
            fsb.LINE,
            fsb.CUR_LINE_ID,
            fsb.SYS_LINE_ID,
            fsb.ORIGIN_LINE_ID,
            fsb.LOT_ID,
            fsb.CARR_ID,
            fsb.GRADE,
            fsb.LOT_TYPE,
            fsb.LOT_LEVEL,
            fsb.CUR_QTY,
            fsb.BAY_NAME,
            fsb.SENDFAB,
            {elapsed_days_number_expr('fsb.START_DATE')} AS "투입경과_일",
            {elapsed_days_number_expr('fsb.LAST_EVENT_DATE')} AS "마지막이벤트경과_일",
            {elapsed_days_number_expr('fsb.STEP_ARRIVE_DATE')} AS "스텝도착경과_일",
            fsb.issue_step,
            fsb.issue_count,
            fsb.path_count,
            fsb."현스텝",
            fsb.step_status,
            CASE
                WHEN COALESCE(fc.current_exclusion_step_flag, 0) > 0 AND fc.current_step_status = 'HOLD' THEN 'HOLD'
                WHEN COALESCE(fc.current_exclusion_step_flag, 0) > 0 THEN 'WAIT(진행불가)'
                WHEN fc.current_step_status = 'WAIT'
                 AND fc.current_continuous IS NOT NULL
                 AND COALESCE(fbr.blocked_rows, 0) > 0
                -- TODO: CHILDEQP raw 추가 시 ':' / ';' child path 생존 여부까지 반영한다.
                THEN 'WAIT(진행불가)'
                ELSE fc.current_step_status
            END AS lot_status,
            fsb.PROC_ID,
            fsb.ORDER_SEQ,
            fsb.DE_RANK,
            fsb."연속",
            fsb.AREA,
            fsb.LAYER_ID,
            fsb.STEP_LEVEL,
            fsb.EIN,
            fsb.STEP_SEQ,
            fsb.STEP_DESC,
            fsb.EQP_TYPE,
            fsb.RECIPE_ID,
            fsb.BATCH_KIND,
            fsb.EQPLINE,
            fsb.eqpid,
            fsb.eqpcham,
            fsb.PREVENT,
            fsb.TYPE_BODY,
            fsb.TYPE_CHAM,
            fsb.TIP_EVENTTIME,
            fsb.eqpissue,
            fsb.body_eqp_status,
            fsb.CHAM_EQP_STATUS,
            fsb.EQPISSUETIME,
            fsb.eqpgroup,
            fsb.eqpgroup_cham,
            fsb.hold,
            fsb.hold_reason,
            fsb.hold_date,
            fsb.exception,
            fsb.exception_reason,
            fsb.exception_date,
            fsb.ftp,
            fsb.ftp_reason,
            fsb.ftp_date
        FROM f1_status_base fsb
        LEFT JOIN f1_current fc
          ON fsb.LINE = fc.LINE
         AND fsb.LOT_ID = fc.LOT_ID
        LEFT JOIN f1_blocked_rank fbr
          ON fc.LINE = fbr.LINE
         AND fc.LOT_ID = fbr.LOT_ID
         AND fc.current_de_rank = fbr.DE_RANK
        """
    )

    stats.validations["f1_eqpgroup_cham_null_blank_rows_before_fill"] = con.execute(
        """
        SELECT COUNT(*)
        FROM f1_status_base
        WHERE NULLIF(TRIM(CAST(eqpgroup_cham_raw AS VARCHAR)), '') IS NULL
        """
    ).fetchone()[0]
    stats.validations["f1_eqpgroup_cham_null_blank_rows_after_fill"] = con.execute(
        """
        SELECT COUNT(*)
        FROM f1_raw
        WHERE NULLIF(TRIM(CAST(eqpgroup_cham AS VARCHAR)), '') IS NULL
        """
    ).fetchone()[0]
    print(
        "[INFO] eqpgroup_cham 보완 전 null/blank row 수 - f1 = "
        f"{stats.validations['f1_eqpgroup_cham_null_blank_rows_before_fill']:,}"
    )
    print(
        "[INFO] eqpgroup_cham 보완 후 null/blank row 수 - f1 = "
        f"{stats.validations['f1_eqpgroup_cham_null_blank_rows_after_fill']:,}"
    )

    con.execute("DROP TABLE IF EXISTS f1_final")
    con.execute("CREATE TABLE f1_final AS SELECT DISTINCT * FROM f1_raw")
    # f1은 f1_final의 순수 복사였다 -> VIEW로 대체해 동일 데이터를 두 번 저장하지 않는다.
    con.execute("DROP TABLE IF EXISTS f1")
    con.execute("DROP VIEW IF EXISTS f1")
    con.execute("CREATE VIEW f1 AS SELECT * FROM f1_final")

    record_distinct_counts(con, stats, "f1", "f1_raw", "f1_final")
    stats.counts["f1_rows"] = stats.counts["f1_rows_after_distinct"]
    print(f"[OK] f1 rows = {stats.counts['f1_rows']:,}")


def build_f2(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """f1 전체 path를 유지한 summary 전체본 f2를 생성한다."""
    con.execute("DROP TABLE IF EXISTS f2_current")
    con.execute(
        """
        CREATE TABLE f2_current AS
        SELECT
            LINE,
            LOT_ID,
            MAX(ORDER_SEQ) FILTER (WHERE "현스텝" = '현스텝') AS current_order_seq,
            MAX(DE_RANK) FILTER (WHERE "현스텝" = '현스텝') AS current_de_rank,
            MAX(NULLIF(TRIM(CAST("연속" AS VARCHAR)), '')) FILTER (WHERE "현스텝" = '현스텝') AS current_continuous
        FROM f1
        GROUP BY LINE, LOT_ID
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_keep")
    con.execute(
        """
        CREATE TABLE f2_keep AS
        SELECT
            f1.*,
            fc.current_order_seq,
            fc.current_de_rank,
            fc.current_continuous
        FROM f1
        LEFT JOIN f2_current fc
          ON f1.LINE = fc.LINE
         AND f1.LOT_ID = fc.LOT_ID
        """
    )

    # [변경] tip/down은 이전에 '현스텝(또는 현 연속블록)' 범위에서만 산출한 뒤
    # LINE+LOT_ID 단위 문자열로 요약하여 그 LOT의 모든 step 행에 복사했다.
    # 이제 각 STEP(LINE+LOT_ID+ORDER_SEQ) 단위로 산출/부착하여, 이슈가 실제 발생한
    # 스텝에만 표시되고 현스텝이 아니어도 값이 있으면 표시되도록 한다.
    # 이에 따라 current_step_scope 기반 범위 제한 단계가 제거되었다.
    con.execute("DROP TABLE IF EXISTS f2_tip_scope")
    con.execute("CREATE TABLE f2_tip_scope AS SELECT * FROM f2_keep")

    con.execute("DROP TABLE IF EXISTS f2_down_scope")
    con.execute("CREATE TABLE f2_down_scope AS SELECT * FROM f2_keep")

    con.execute("DROP TABLE IF EXISTS f2_tip_parts")
    con.execute(
        f"""
        CREATE TABLE f2_tip_parts AS
        SELECT DISTINCT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            CASE
                WHEN TYPE_BODY = 'PREVENT' THEN eqpid
                WHEN TYPE_CHAM = 'PREVENT' THEN eqpcham
                ELSE COALESCE(eqpid, eqpcham)
            END AS eqp_name,
            {elapsed_days_expr('TIP_EVENTTIME')} AS elapsed_days_text
        FROM f2_tip_scope
        WHERE PREVENT = 'PREVENT'
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_tip_summary")
    con.execute(
        """
        CREATE TABLE f2_tip_summary AS
        SELECT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            'PREVENT: ' || STRING_AGG(
                DISTINCT eqp_name || COALESCE('(' || elapsed_days_text || ')', ''),
                ', '
                ORDER BY eqp_name || COALESCE('(' || elapsed_days_text || ')', '')
            ) AS tip
        FROM f2_tip_parts
        WHERE eqp_name IS NOT NULL
        GROUP BY LINE, LOT_ID, ORDER_SEQ
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_down_parts")
    con.execute(
        f"""
        CREATE TABLE f2_down_parts AS
        SELECT DISTINCT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            CASE
                WHEN body_eqp_status IN ('LOCAL', 'DOWN', 'PM') THEN eqpid
                WHEN CHAM_EQP_STATUS IN ('LOCAL', 'DOWN', 'PM') THEN eqpcham
                ELSE COALESCE(eqpcham, eqpid)
            END AS eqp_name,
            CASE
                WHEN body_eqp_status IN ('LOCAL', 'DOWN', 'PM') THEN body_eqp_status
                WHEN CHAM_EQP_STATUS IN ('LOCAL', 'DOWN', 'PM') THEN CHAM_EQP_STATUS
                ELSE eqpissue
            END AS issue_group,
            {elapsed_days_expr('EQPISSUETIME')} AS elapsed_days_text
        FROM f2_down_scope
        WHERE eqpissue IS NOT NULL
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_down_grouped")
    con.execute(
        """
        CREATE TABLE f2_down_grouped AS
        SELECT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            issue_group,
            issue_group || ': ' || STRING_AGG(
                DISTINCT eqp_name || COALESCE('(' || elapsed_days_text || ')', ''),
                ', '
                ORDER BY eqp_name || COALESCE('(' || elapsed_days_text || ')', '')
            ) AS down_part
        FROM f2_down_parts
        WHERE eqp_name IS NOT NULL AND issue_group IS NOT NULL
        GROUP BY LINE, LOT_ID, ORDER_SEQ, issue_group
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_down_summary")
    con.execute(
        """
        CREATE TABLE f2_down_summary AS
        SELECT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            STRING_AGG(
                down_part,
                ' / '
                ORDER BY CASE issue_group WHEN 'LOCAL' THEN 1 WHEN 'PM' THEN 2 WHEN 'DOWN' THEN 3 ELSE 4 END, issue_group
            ) AS down
        FROM f2_down_grouped
        GROUP BY LINE, LOT_ID, ORDER_SEQ
        """
    )

    stats.validations["tip_scope_rows"] = con.execute("SELECT COUNT(*) FROM f2_tip_scope WHERE PREVENT = 'PREVENT'").fetchone()[0]
    stats.validations["down_scope_rows"] = con.execute("SELECT COUNT(*) FROM f2_down_scope WHERE eqpissue IS NOT NULL").fetchone()[0]
    stats.validations["tip_not_null_lots"] = con.execute("SELECT COUNT(DISTINCT LOT_ID) FROM f2_tip_summary WHERE tip IS NOT NULL").fetchone()[0]
    stats.validations["down_not_null_lots"] = con.execute("SELECT COUNT(DISTINCT LOT_ID) FROM f2_down_summary WHERE down IS NOT NULL").fetchone()[0]
    # [변경] tip/down이 STEP 단위가 되었으므로, 값이 붙은 'step 수'를 함께 기록한다.
    stats.validations["tip_not_null_steps"] = con.execute("SELECT COUNT(*) FROM f2_tip_summary WHERE tip IS NOT NULL").fetchone()[0]
    stats.validations["down_not_null_steps"] = con.execute("SELECT COUNT(*) FROM f2_down_summary WHERE down IS NOT NULL").fetchone()[0]
    # 현스텝이 아닌 스텝에 붙은 이슈 건수(과거에는 LOT 단위 복사로 전 스텝에 퍼졌던 값)
    stats.validations["issue_rows_on_non_current_steps"] = con.execute(
        """
        SELECT COUNT(*)
        FROM f2_keep
        WHERE COALESCE("현스텝", '') <> '현스텝'
          AND (PREVENT = 'PREVENT' OR eqpissue IS NOT NULL)
        """
    ).fetchone()[0]

    con.execute("DROP TABLE IF EXISTS f2_eqpline_values")
    con.execute(
        """
        CREATE TABLE f2_eqpline_values AS
        SELECT DISTINCT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            NULLIF(TRIM(CAST(EQPLINE AS VARCHAR)), '') AS eqpline_value
        FROM f1
        WHERE NULLIF(TRIM(CAST(EQPLINE AS VARCHAR)), '') IS NOT NULL
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_eqpline_summary")
    con.execute(
        """
        CREATE TABLE f2_eqpline_summary AS
        SELECT
            LINE,
            LOT_ID,
            ORDER_SEQ,
            STRING_AGG(
                eqpline_value,
                ', '
                ORDER BY
                    CASE WHEN TRY_CAST(eqpline_value AS DOUBLE) IS NULL THEN 1 ELSE 0 END,
                    TRY_CAST(eqpline_value AS DOUBLE),
                    eqpline_value
            ) AS eqpline
        FROM f2_eqpline_values
        GROUP BY LINE, LOT_ID, ORDER_SEQ
        -- TODO: 현스텝이 연속공정이면 DE_RANK 전체 기준 unique concatenate가 맞는지 업무 확인이 필요합니다.
        """
    )

    con.execute("DROP TABLE IF EXISTS f2")
    con.execute("DROP TABLE IF EXISTS f2_calc")
    con.execute(
        f"""
        CREATE TABLE f2_calc AS
        SELECT
            fk.LOT_INFORM AS lot_inform,
            fk.LINE AS line,
            fk.CUR_LINE_ID AS "현재위치",
            fk.SYS_LINE_ID AS "전산라인",
            fk.ORIGIN_LINE_ID AS "투입라인",
            fk.LOT_ID AS lot_id,
            fk.CARR_ID AS carr_id,
            fk.GRADE AS grade,
            fk.LOT_TYPE AS lot_type,
            fk.LOT_LEVEL AS lot_level,
            fk.CUR_QTY AS qty,
            fk.BAY_NAME AS bay,
            fk.SENDFAB AS sendfab,
            fk."투입경과_일",
            fk."마지막이벤트경과_일",
            fk."스텝도착경과_일",
            fk.lot_status,
            fk.step_status,
            fk.PROC_ID AS proc_id,
            fk.DE_RANK AS de_rank,
            fk."연속",
            fk.AREA AS AREA,
            fk.LAYER_ID AS layer_id,
            fk."현스텝",
            fk.ORDER_SEQ AS order_seq,
            fk.STEP_SEQ AS step_seq,
            fk.STEP_DESC AS step_desc,
            fk.RECIPE_ID AS recipe_id,
            fk.EQP_TYPE AS eqp_type,
            fk.BATCH_KIND AS batch_kind,
            fes.eqpline,
            fk.eqpgroup,
            COALESCE(NULLIF(TRIM(CAST(fk.eqpgroup_cham AS VARCHAR)), ''), fk.eqpgroup) AS eqpgroup_cham,
            ts.tip,
            ds.down,
            CASE
                WHEN fk.hold IS NOT NULL THEN {elapsed_days_number_expr('fk.hold_date')}
            END AS hold,
            fk.hold_reason,
            CASE
                WHEN fk.exception IS NOT NULL THEN {elapsed_days_number_expr('fk.exception_date')}
            END AS exception,
            fk.exception_reason,
            CASE
                WHEN fk.ftp IS NOT NULL THEN {elapsed_days_number_expr('fk.ftp_date')}
            END AS ftp,
            fk.ftp_reason
        FROM f2_keep fk
        LEFT JOIN f2_tip_summary ts
          ON fk.LINE = ts.LINE
         AND fk.LOT_ID = ts.LOT_ID
         AND fk.ORDER_SEQ = ts.ORDER_SEQ
        LEFT JOIN f2_down_summary ds
          ON fk.LINE = ds.LINE
         AND fk.LOT_ID = ds.LOT_ID
         AND fk.ORDER_SEQ = ds.ORDER_SEQ
        LEFT JOIN f2_eqpline_summary fes
          ON fk.LINE = fes.LINE
         AND fk.LOT_ID = fes.LOT_ID
         AND fk.ORDER_SEQ = fes.ORDER_SEQ
        """
    )

    stats.validations["f2_eqpgroup_cham_null_blank_rows_after_fill"] = con.execute(
        """
        SELECT COUNT(*)
        FROM f2_calc
        WHERE NULLIF(TRIM(CAST(eqpgroup_cham AS VARCHAR)), '') IS NULL
        """
    ).fetchone()[0]
    print(
        "[INFO] eqpgroup_cham 보완 후 null/blank row 수 - f2 = "
        f"{stats.validations['f2_eqpgroup_cham_null_blank_rows_after_fill']:,}"
    )

    con.execute("DROP TABLE IF EXISTS f2_final")
    con.execute(f"CREATE TABLE f2_final AS {summary_distinct_query('f2_calc')}")
    con.execute("DROP TABLE IF EXISTS f2")
    con.execute(f"CREATE TABLE f2 AS {summary_export_query('f2_final')}")

    record_distinct_counts(con, stats, "f2", "f2_calc", "f2_final")
    stats.counts["f2_rows"] = stats.counts["f2_rows_after_distinct"]
    validate_no_blocked_summary_columns(con, "f2", stats)
    print(f"[OK] f2 rows = {stats.counts['f2_rows']:,}")


def build_f3(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """f2에서 현재 보기용 row만 남긴 summary 결과 f3를 생성한다."""
    con.execute("DROP TABLE IF EXISTS f3_current")
    con.execute(
        """
        CREATE TABLE f3_current AS
        SELECT
            line,
            lot_id,
            MAX(order_seq) FILTER (WHERE "현스텝" = '현스텝') AS current_order_seq,
            MAX(de_rank) FILTER (WHERE "현스텝" = '현스텝') AS current_de_rank,
            MAX(NULLIF(TRIM(CAST("연속" AS VARCHAR)), '')) FILTER (WHERE "현스텝" = '현스텝') AS current_continuous
        FROM f2
        GROUP BY line, lot_id
        """
    )

    f3_columns = prefixed_column_list(SUMMARY_OUTPUT_COLUMNS, "f2")
    con.execute("DROP TABLE IF EXISTS f3_base")
    con.execute(
        f"""
        CREATE TABLE f3_base AS
        SELECT
{f3_columns}
        FROM f2
        LEFT JOIN f3_current fc
          ON f2.line = fc.line
         AND f2.lot_id = fc.lot_id
        WHERE f2."현스텝" = '현스텝'
           OR (fc.current_continuous IS NOT NULL AND f2.de_rank = fc.current_de_rank)
        """
    )

    stats.validations["f3_eqpgroup_cham_null_blank_rows_after_fill"] = con.execute(
        """
        SELECT COUNT(*)
        FROM f3_base
        WHERE NULLIF(TRIM(CAST(eqpgroup_cham AS VARCHAR)), '') IS NULL
        """
    ).fetchone()[0]
    print(
        "[INFO] eqpgroup_cham 보완 후 null/blank row 수 - f3 = "
        f"{stats.validations['f3_eqpgroup_cham_null_blank_rows_after_fill']:,}"
    )

    con.execute("DROP TABLE IF EXISTS f3_final")
    con.execute(f"CREATE TABLE f3_final AS {summary_distinct_query('f3_base')}")
    con.execute("DROP TABLE IF EXISTS f3")
    con.execute(f"CREATE TABLE f3 AS {summary_export_query('f3_final')}")

    record_distinct_counts(con, stats, "f3", "f3_base", "f3_final")
    stats.counts["f3_rows"] = stats.counts["f3_rows_after_distinct"]
    validate_no_blocked_summary_columns(con, "f3", stats)
    print(f"[OK] f3 rows = {stats.counts['f3_rows']:,}")


def export_parquet(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """f1/f2/f3를 Parquet으로 저장한다."""
    outputs = {
        "f1_parquet": PARQUET_DIR / "f1_result.parquet",
        "f2_parquet": PARQUET_DIR / "f2_result.parquet",
        "f3_parquet": PARQUET_DIR / "f3_result.parquet",
    }
    con.execute(
        f"""
        COPY (
            SELECT *
            FROM f1_final
            ORDER BY LINE, LOT_ID, TRY_CAST(ORDER_SEQ AS BIGINT) NULLS LAST, ORDER_SEQ, COALESCE(eqpcham, '')
        ) TO '{sql_text(outputs['f1_parquet'])}' (FORMAT PARQUET)
        """
    )
    validate_summary_export_columns(con, stats)
    con.execute(
        f"""
        COPY (
            {summary_export_query("f2")}
        ) TO '{sql_text(outputs['f2_parquet'])}' (FORMAT PARQUET)
        """
    )
    con.execute(
        f"""
        COPY (
            {summary_export_query("f3")}
        ) TO '{sql_text(outputs['f3_parquet'])}' (FORMAT PARQUET)
        """
    )
    validate_parquet_columns(con, "f2", outputs["f2_parquet"], stats)
    validate_parquet_columns(con, "f3", outputs["f3_parquet"], stats)
    stats.outputs.update({key: str(path) for key, path in outputs.items()})
    print("[OK] parquet 저장 완료")


def relation_to_dataframe(con: duckdb.DuckDBPyConnection, query: str, limit: int, offset: int) -> pd.DataFrame:
    """쿼리 결과 일부를 pandas DataFrame으로 가져온다."""
    return con.execute(f"SELECT * FROM ({query}) AS q LIMIT {limit} OFFSET {offset}").fetchdf()


def write_csv_chunks(con: duckdb.DuckDBPyConnection, query: str, output_path: Path, row_count: int, rows_per_file: int) -> list[str]:
    """Excel 저장이 비현실적으로 큰 경우 CSV를 분할 저장한다."""
    csv_paths: list[str] = []
    base = output_path.with_suffix("")
    chunk_count = math.ceil(row_count / rows_per_file)
    for chunk_index in range(chunk_count):
        offset = chunk_index * rows_per_file
        chunk_path = base.parent / f"{base.name}_part{chunk_index + 1:03d}.csv"
        con.execute(
            f"""
            COPY (
                SELECT * FROM ({query}) AS q
                LIMIT {rows_per_file} OFFSET {offset}
            ) TO '{sql_text(chunk_path)}' (HEADER, DELIMITER ',')
            """
        )
        csv_paths.append(str(chunk_path))
    return csv_paths


def write_excel_safely(
    con: duckdb.DuckDBPyConnection,
    query: str,
    output_path: Path,
    sheet_base_name: str,
    max_rows_per_sheet: int = EXCEL_MAX_ROWS_PER_SHEET,
) -> dict[str, Any]:
    """Excel 행 제한을 고려하여 한 파일 내 여러 시트 또는 CSV 분할로 안전하게 저장한다."""
    row_count = con.execute(f"SELECT COUNT(*) FROM ({query}) AS q").fetchone()[0]
    result: dict[str, Any] = {"path": str(output_path), "rows": row_count, "csv_paths": []}

    if row_count > EXCEL_CSV_FALLBACK_ROWS:
        result["csv_paths"] = write_csv_chunks(con, query, output_path, row_count, max_rows_per_sheet)
        result["warning"] = "Excel 저장이 비현실적으로 큰 행 수라 CSV 분할 저장으로 대체했습니다."
        return result

    sheet_count = max(1, math.ceil(row_count / max_rows_per_sheet))
    target_path = output_path
    try:
        with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
            for sheet_index in range(sheet_count):
                offset = sheet_index * max_rows_per_sheet
                limit = min(max_rows_per_sheet, row_count - offset) if row_count else max_rows_per_sheet
                df = relation_to_dataframe(con, query, limit, offset)
                sheet_name = f"{sheet_base_name}_{sheet_index + 1:03d}" if sheet_count > 1 else sheet_base_name
                df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_path = output_path.with_name(f"{output_path.stem}_{timestamp}{output_path.suffix}")
        print(f"[WARN] Excel 파일이 열려 있어 기본 경로에 저장하지 못했습니다. timestamp suffix 파일명으로 재시도합니다: {target_path}")
        with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
            for sheet_index in range(sheet_count):
                offset = sheet_index * max_rows_per_sheet
                limit = min(max_rows_per_sheet, row_count - offset) if row_count else max_rows_per_sheet
                df = relation_to_dataframe(con, query, limit, offset)
                sheet_name = f"{sheet_base_name}_{sheet_index + 1:03d}" if sheet_count > 1 else sheet_base_name
                df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
        result["path"] = str(target_path)
        result["warning"] = "Excel 파일이 열려 있어 timestamp suffix 파일명으로 재시도 저장했습니다."
    result["sheet_count"] = sheet_count
    return result


def export_excel_safely(con: duckdb.DuckDBPyConnection, stats: RunStats, include_f1: bool = False) -> None:
    """f2/f3를 Excel 검증 파일로 저장한다. f1은 명시 옵션에서만 포함한다."""
    print("[WARN] Excel export는 대량 데이터에서 매우 느립니다. 운영 적재에서는 사용하지 마십시오.")
    export_targets = {}
    if include_f1:
        export_targets["f1"] = (
            "SELECT * FROM f1_final ORDER BY LINE, LOT_ID, TRY_CAST(ORDER_SEQ AS BIGINT) NULLS LAST, ORDER_SEQ, COALESCE(eqpcham, '')",
            EXCEL_DIR / "f1_result.xlsx",
        )
    else:
        print("[SKIP] f1 Excel export는 대량 데이터 병목 방지를 위해 기본 제외합니다. f2/f3만 저장합니다.")
    export_targets.update({
        "f2": (
            summary_export_query("f2"),
            EXCEL_DIR / "f2_result.xlsx",
        ),
        "f3": (
            summary_export_query("f3"),
            EXCEL_DIR / "f3_result.xlsx",
        ),
    })
    validate_summary_export_columns(con, stats)
    for name, (query, path) in export_targets.items():
        result = write_excel_safely(con, query, path, name)
        stats.outputs[f"{name}_excel"] = result["path"]
        if result.get("csv_paths"):
            stats.outputs[f"{name}_csv_parts"] = ", ".join(result["csv_paths"])
    print("[OK] excel 저장 완료")


def collect_date_failures(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """날짜 파싱 성공/실패 건수와 실패 샘플 값을 수집한다."""
    for table_name, columns in DATE_CHECK_TARGETS.items():
        for column in columns:
            exists = con.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = ? AND column_name = ?
                """,
                [table_name, column],
            ).fetchone()[0]
            if not exists:
                continue

            parsed_expr = parsed_timestamp_expr(column)
            total_count = con.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE {normalize_empty_expr(column)} IS NOT NULL"
            ).fetchone()[0]
            success_count = con.execute(
                f"""
                SELECT COUNT(*)
                FROM {table_name}
                WHERE {normalize_empty_expr(column)} IS NOT NULL
                  AND {parsed_expr} IS NOT NULL
                """
            ).fetchone()[0]
            fail_count = con.execute(
                f"""
                SELECT COUNT(*)
                FROM {table_name}
                WHERE {normalize_empty_expr(column)} IS NOT NULL
                  AND {parsed_expr} IS NULL
                """
            ).fetchone()[0]
            sample_sql = f"""
                SELECT DISTINCT {column}
                FROM {table_name}
                WHERE {normalize_empty_expr(column)} IS NOT NULL
                  AND {parsed_expr} IS NULL
                LIMIT 20
            """
            samples = [row[0] for row in con.execute(sample_sql).fetchall()]
            stats.date_failures[f"{table_name}.{column}"] = {
                "total": total_count,
                "success": success_count,
                "count": fail_count,
                "samples": samples,
            }

    stats.validations["korean_datetime_parse_examples"] = (
        "한국어 오전/오후 파싱 규칙 적용: "
        "2026-04-06 오후 10:11:08 -> 2026-04-06 22:11:08, "
        "2026-05-30 오전 8:23:55 -> 2026-05-30 08:23:55"
    )


def collect_validations(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    """로그에 남길 검증 포인트를 수집한다."""
    stats.validations["m_lot_distinct"] = con.execute("SELECT COUNT(DISTINCT LOT_ID) FROM m").fetchone()[0]

    table_specs = {
        "f1": {
            "lot_col": "LOT_ID",
            "current_col": '"현스텝"',
            "tip_expr": "TIP_EVENTTIME IS NOT NULL",
            "down_expr": "eqpissue IS NOT NULL",
            "hold_col": "hold",
            "exception_col": "exception",
            "ftp_col": "ftp",
        },
        "f2": {
            "lot_col": "lot_id",
            "current_col": '"현스텝"',
            "tip_expr": "tip IS NOT NULL",
            "down_expr": "down IS NOT NULL",
            "hold_col": "hold",
            "exception_col": "exception",
            "ftp_col": "ftp",
        },
        "f3": {
            "lot_col": "lot_id",
            "current_col": '"현스텝"',
            "tip_expr": "tip IS NOT NULL",
            "down_expr": "down IS NOT NULL",
            "hold_col": "hold",
            "exception_col": "exception",
            "ftp_col": "ftp",
        },
    }

    for table_name, spec in table_specs.items():
        lot_col = spec["lot_col"]
        current_col = spec["current_col"]
        stats.validations[f"{table_name}_lot_distinct"] = con.execute(
            f"SELECT COUNT(DISTINCT {lot_col}) FROM {table_name}"
        ).fetchone()[0]
        stats.validations[f"{table_name}_current_rows"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {current_col} = '현스텝'"
        ).fetchone()[0]
        stats.validations[f"{table_name}_non_current_rows"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {current_col} IS NULL OR {current_col} <> '현스텝'"
        ).fetchone()[0]
        stats.validations[f"{table_name}_lot_status_counts"] = con.execute(
            f"SELECT lot_status, COUNT(*) FROM {table_name} GROUP BY lot_status ORDER BY lot_status"
        ).fetchall()
        stats.validations[f"{table_name}_step_status_counts"] = con.execute(
            f"SELECT step_status, COUNT(*) FROM {table_name} GROUP BY step_status ORDER BY step_status"
        ).fetchall()
        stats.validations[f"{table_name}_lot_status_multi_value_lot_count"] = con.execute(
            f"SELECT COUNT(*) FROM (SELECT {lot_col} FROM {table_name} GROUP BY {lot_col} HAVING COUNT(DISTINCT lot_status) > 1)"
        ).fetchone()[0]
        total_rows_for_ratio = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] or 0
        same_status_rows = con.execute(f"SELECT COUNT(*) FROM {table_name} WHERE COALESCE(step_status, '') = COALESCE(lot_status, '')").fetchone()[0] or 0
        stats.validations[f"{table_name}_step_lot_status_same_row_ratio"] = round(same_status_rows / total_rows_for_ratio * 100, 2) if total_rows_for_ratio else 0
        stats.validations[f"{table_name}_non_current_exclusion_copied_step_status_rows"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE ({current_col} IS NULL OR {current_col} <> '현스텝') AND ({spec['hold_col']} IS NOT NULL OR {spec['exception_col']} IS NOT NULL OR {spec['ftp_col']} IS NOT NULL) AND (step_status = 'HOLD' OR step_status = 'WAIT(진행불가)')"
        ).fetchone()[0]
        stats.validations[f"{table_name}_wait_block_tip_down_origin_rows"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE step_status = 'WAIT(진행불가)' AND ({spec['tip_expr']} OR {spec['down_expr']})"
        ).fetchone()[0]
        stats.validations[f"{table_name}_wait_block_exclusion_origin_rows"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE step_status = 'WAIT(진행불가)' AND ({spec['hold_col']} IS NOT NULL OR {spec['exception_col']} IS NOT NULL OR {spec['ftp_col']} IS NOT NULL)"
        ).fetchone()[0]
        stats.validations[f"{table_name}_tip_not_null"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {spec['tip_expr']}"
        ).fetchone()[0]
        stats.validations[f"{table_name}_down_not_null"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {spec['down_expr']}"
        ).fetchone()[0]
        stats.validations[f"{table_name}_hold_not_null"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {spec['hold_col']} IS NOT NULL"
        ).fetchone()[0]
        stats.validations[f"{table_name}_exception_not_null"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {spec['exception_col']} IS NOT NULL"
        ).fetchone()[0]
        stats.validations[f"{table_name}_ftp_not_null"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {spec['ftp_col']} IS NOT NULL"
        ).fetchone()[0]
        stats.validations[f"{table_name}_AREA_not_null"] = con.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {quote_ident('AREA')} IS NOT NULL"
        ).fetchone()[0]
        print(
            f"[INFO] AREA not null row 수 - {table_name} = "
            f"{stats.validations[f'{table_name}_AREA_not_null']:,}"
        )
        for elapsed_col in ("투입경과_일", "마지막이벤트경과_일", "스텝도착경과_일"):
            stats.validations[f"{table_name}_{elapsed_col}_not_null"] = con.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE {quote_ident(elapsed_col)} IS NOT NULL"
            ).fetchone()[0]

    stats.validations["f1_issue_step_not_null"] = con.execute("SELECT COUNT(*) FROM f1 WHERE issue_step IS NOT NULL").fetchone()[0]
    stats.validations["f3_continuous_extension_rows"] = stats.validations.get("f3_non_current_rows", 0)
    stats.validations["f1_has_non_current_rows"] = stats.validations.get("f1_non_current_rows", 0) > 0
    stats.validations["f2_has_non_current_rows"] = stats.validations.get("f2_non_current_rows", 0) > 0
    stats.validations["f3_outside_current_or_continuous_rows"] = con.execute(
        """
        SELECT COUNT(*)
        FROM f3
        LEFT JOIN f3_current fc
          ON f3.line = fc.line
         AND f3.lot_id = fc.lot_id
        WHERE NOT (
            f3."현스텝" = '현스텝'
            OR (fc.current_continuous IS NOT NULL AND f3.de_rank = fc.current_de_rank)
        )
        """
    ).fetchone()[0]
    stats.validations["f3_only_current_or_continuous"] = stats.validations["f3_outside_current_or_continuous_rows"] == 0

    for table_name in ("f2", "f3"):
        summary_key_rows = con.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT
                    line,
                    lot_id,
                    order_seq,
                    recipe_id,
                    eqpgroup,
                    eqpgroup_cham
                FROM {table_name}
                GROUP BY line, lot_id, order_seq, recipe_id, eqpgroup, eqpgroup_cham
            ) AS summary_keys
            """
        ).fetchone()[0]
        total_rows = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        stats.validations[f"{table_name}_summary_key_distinct_rows"] = summary_key_rows
        stats.validations[f"{table_name}_summary_key_duplicate_suspect_rows"] = total_rows - summary_key_rows

    stats.validations["order_seq_sort_rule"] = "ORDER_SEQ 정렬은 TRY_CAST(order_seq AS BIGINT) NULLS LAST, 원본 order_seq 보조 기준으로 수행"
    for table_name, column_name in (("f1", "ORDER_SEQ"), ("f2", "order_seq"), ("f3", "order_seq")):
        stats.validations[f"{table_name}_order_seq_cast_fail_count"] = con.execute(
            f"""
            SELECT COUNT(DISTINCT {column_name})
            FROM {table_name}
            WHERE {normalize_empty_expr(column_name)} IS NOT NULL
              AND TRY_CAST({column_name} AS BIGINT) IS NULL
            """
        ).fetchone()[0]
        stats.validations[f"{table_name}_order_seq_cast_fail_samples"] = [
            row[0]
            for row in con.execute(
                f"""
                SELECT DISTINCT {column_name}
                FROM {table_name}
                WHERE {normalize_empty_expr(column_name)} IS NOT NULL
                  AND TRY_CAST({column_name} AS BIGINT) IS NULL
                LIMIT 20
                """
            ).fetchall()
        ]


def format_status_counts(rows: list[tuple[Any, int]]) -> list[str]:
    """상태별 건수를 markdown 목록으로 변환한다."""
    if not rows:
        return ["  - 없음"]
    return [f"  - {status if status is not None else '(NULL)'}: {count:,}" for status, count in rows]


def write_run_log(stats: RunStats) -> Path:
    """실행 결과 markdown 로그를 저장한다."""
    log_path = LOG_DIR / f"build_multiwip_f1_f2_f3_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines: list[str] = ["# build_multiwip_f1_f2_f3 실행 로그", ""]

    lines.extend([
        "## 결과 정의",
        "- f1 = raw 조합 전체",
        "- f2 = 전체 path 유지 summary 전체본",
        "- f3 = 현재 보기용 summary, 현스텝 또는 현스텝 연속공정 관련 step만 유지",
        "",
    ])

    lines.extend(["## 입력 파일"])
    for alias in ("m", "s", "t", "h"):
        info = stats.input_info.get(alias, {})
        lines.append(f"- {alias} 경로: `{info.get('path')}`")
        lines.append(f"  - 파일 크기(MB): {info.get('size_mb')}")
        lines.append(f"  - 행 수(csv/duckdb): {info.get('rows')} / {info.get('duckdb_rows')}")
    lines.append("")

    lines.extend([
        "## 중간/최종 행 수",
        f"- m rows: {stats.counts.get('m_rows', 0):,}",
        f"- s rows: {stats.counts.get('s_rows', 0):,}",
        f"- t rows: {stats.counts.get('t_rows', 0):,}",
        f"- h rows: {stats.counts.get('h_rows', 0):,}",
        f"- h1 rows: {stats.counts.get('h1_rows', 0):,}",
        f"- h2 rows: {stats.counts.get('h2_rows', 0):,}",
        f"- h3 rows: {stats.counts.get('h3_rows', 0):,}",
        f"- f1 rows: {stats.counts.get('f1_rows', 0):,}",
        f"- f2 rows: {stats.counts.get('f2_rows', 0):,}",
        f"- f3 rows: {stats.counts.get('f3_rows', 0):,}",
        "",
        "## 최종 산출물 중복 제거 검증",
        "- distinct 기준: f1은 최종 출력 전체 컬럼, f2/f3는 SUMMARY_OUTPUT_COLUMNS 전체 컬럼",
        f"- f1 중복 제거 전 row 수: {stats.counts.get('f1_rows_before_distinct', 0):,}",
        f"- f1 중복 제거 후 row 수: {stats.counts.get('f1_rows_after_distinct', 0):,}",
        f"- f1 제거된 중복 row 수: {stats.counts.get('f1_duplicate_rows_removed', 0):,}",
        f"- f2 중복 제거 전 row 수: {stats.counts.get('f2_rows_before_distinct', 0):,}",
        f"- f2 중복 제거 후 row 수: {stats.counts.get('f2_rows_after_distinct', 0):,}",
        f"- f2 제거된 중복 row 수: {stats.counts.get('f2_duplicate_rows_removed', 0):,}",
        f"- f3 중복 제거 전 row 수: {stats.counts.get('f3_rows_before_distinct', 0):,}",
        f"- f3 중복 제거 후 row 수: {stats.counts.get('f3_rows_after_distinct', 0):,}",
        f"- f3 제거된 중복 row 수: {stats.counts.get('f3_duplicate_rows_removed', 0):,}",
        "",
        "## distinct LOT_ID",
        f"- m distinct LOT_ID: {stats.validations.get('m_lot_distinct', 0):,}",
        f"- f1 distinct LOT_ID: {stats.validations.get('f1_lot_distinct', 0):,}",
        f"- f2 distinct LOT_ID: {stats.validations.get('f2_lot_distinct', 0):,}",
        f"- f3 distinct LOT_ID: {stats.validations.get('f3_lot_distinct', 0):,}",
        "",
        "## 현스텝/비현스텝 행 수",
        f"- f1 현스텝 row 수: {stats.validations.get('f1_current_rows', 0):,}",
        f"- f1 비현스텝 row 수: {stats.validations.get('f1_non_current_rows', 0):,}",
        f"- f2 현스텝 row 수: {stats.validations.get('f2_current_rows', 0):,}",
        f"- f2 비현스텝 row 수: {stats.validations.get('f2_non_current_rows', 0):,}",
        f"- f3 현스텝 row 수: {stats.validations.get('f3_current_rows', 0):,}",
        f"- f3 연속공정 확장 row 수: {stats.validations.get('f3_continuous_extension_rows', 0):,}",
        "",
        "## 핵심 검증",
        f"- f1 비현스텝 row 수 > 0: {stats.validations.get('f1_has_non_current_rows')}",
        f"- f2 비현스텝 row 수 > 0: {stats.validations.get('f2_has_non_current_rows')}",
        f"- f3에 현스텝/연속공정 외 row 혼입 없음: {stats.validations.get('f3_only_current_or_continuous')}",
        f"- f3 현스텝/연속공정 외 row 수: {stats.validations.get('f3_outside_current_or_continuous_rows', 0):,}",
        "",
        "## f2/f3 summary key 중복 의심 검증",
        "- summary key: line, lot_id, order_seq, recipe_id, eqpgroup, eqpgroup_cham",
        f"- f2 rows: {stats.counts.get('f2_rows', 0):,}",
        f"- f3 rows: {stats.counts.get('f3_rows', 0):,}",
        f"- f2 distinct summary key rows: {stats.validations.get('f2_summary_key_distinct_rows', 0):,}",
        f"- f3 distinct summary key rows: {stats.validations.get('f3_summary_key_distinct_rows', 0):,}",
        f"- f2 중복 의심 rows: {stats.validations.get('f2_summary_key_duplicate_suspect_rows', 0):,}",
        f"- f3 중복 의심 rows: {stats.validations.get('f3_summary_key_duplicate_suspect_rows', 0):,}",
        "",
        "## AREA 컬럼 검증",
        f"- s AREA 컬럼 존재 여부: {stats.validations.get('s_area_column_exists')}",
        f"- s AREA 컬럼 실제 사용명: {stats.validations.get('s_area_column_name')}",
        f"- s AREA fallback 메시지: {stats.validations.get('s_area_fallback_message')}",
        f"- AREA not null row 수 - f1: {stats.validations.get('f1_AREA_not_null', 0):,}",
        f"- AREA not null row 수 - f2: {stats.validations.get('f2_AREA_not_null', 0):,}",
        f"- AREA not null row 수 - f3: {stats.validations.get('f3_AREA_not_null', 0):,}",
        "",
        "## eqpgroup_cham 보완 검증",
        f"- eqpgroup_cham 보완 전 null/blank row 수 - f1: {stats.validations.get('f1_eqpgroup_cham_null_blank_rows_before_fill', 0):,}",
        f"- eqpgroup_cham 보완 후 null/blank row 수 - f1: {stats.validations.get('f1_eqpgroup_cham_null_blank_rows_after_fill', 0):,}",
        f"- eqpgroup_cham 보완 후 null/blank row 수 - f2: {stats.validations.get('f2_eqpgroup_cham_null_blank_rows_after_fill', 0):,}",
        f"- eqpgroup_cham 보완 후 null/blank row 수 - f3: {stats.validations.get('f3_eqpgroup_cham_null_blank_rows_after_fill', 0):,}",
        "",
        "## f2/f3 최종 및 저장 컬럼 검증",
        f"- f2 최종 컬럼: {', '.join(stats.validations.get('f2_final_columns', []))}",
        f"- f3 최종 컬럼: {', '.join(stats.validations.get('f3_final_columns', []))}",
        f"- f2 저장 SELECT 컬럼: {', '.join(stats.validations.get('f2_export_columns', []))}",
        f"- f3 저장 SELECT 컬럼: {', '.join(stats.validations.get('f3_export_columns', []))}",
        f"- f2 Parquet 컬럼: {', '.join(stats.validations.get('f2_parquet_columns', []))}",
        f"- f3 Parquet 컬럼: {', '.join(stats.validations.get('f3_parquet_columns', []))}",
        f"- f2 eqpid/eqpcham 잔존 컬럼: {stats.validations.get('f2_blocked_columns', [])}",
        f"- f3 eqpid/eqpcham 잔존 컬럼: {stats.validations.get('f3_blocked_columns', [])}",
        "",
        "## issue_step",
        f"- f1 issue_step not null 행 수: {stats.validations.get('f1_issue_step_not_null', 0):,}",
        "",
        "## EQPISSUETIME fallback 검증",
        f"- EQPISSUETIME fallback 컬럼 존재 여부: {stats.validations.get('eqpissuetime_fallback_column_exists')}",
        f"- s.eqp_status_change_time 사용 가능 여부: {stats.validations.get('eqpissuetime_fallback_available')}",
        f"- 실제 fallback 컬럼명: {stats.validations.get('eqpissuetime_fallback_column_name')}",
        f"- t.EQPISSUETIME null 중 fallback으로 채워진 row 수: {stats.validations.get('eqpissuetime_fallback_filled_rows', 0):,}",
        f"- fallback 후 EQPISSUETIME null row 수: {stats.validations.get('eqpissuetime_null_after_fallback_rows', 0):,}",
        f"- fallback 메시지: {stats.validations.get('eqpissuetime_fallback_message', 'fallback 적용 가능')}",
        "",
        "## TIP/DOWN 집계 범위 검증",
        f"- tip/down 부착 단위: STEP(LINE+LOT_ID+ORDER_SEQ)",
        f"- tip 집계 대상 row 수: {stats.validations.get('tip_scope_rows', 0):,}",
        f"- down 집계 대상 row 수: {stats.validations.get('down_scope_rows', 0):,}",
        f"- tip not null lot 수: {stats.validations.get('tip_not_null_lots', 0):,}",
        f"- down not null lot 수: {stats.validations.get('down_not_null_lots', 0):,}",
        f"- 현스텝이 비연속인데 현스텝 order_seq 외 step의 prevent/eqpissue 의심 row 수: {stats.validations.get('non_continuous_out_of_scope_issue_rows', 0):,}",
        "- TIP/DOWN 집계 조건: current_연속 IS NULL이면 order_seq = current_order_seq, current_연속 IS NOT NULL이면 de_rank = current_de_rank",
    ])

    for table_name in ("f1", "f2", "f3"):
        lines.append("")
        lines.append(f"## {table_name} summary 컬럼 검증")
        lines.append(f"- {table_name} lot_status별 건수:")
        lines.extend(format_status_counts(stats.validations.get(f"{table_name}_lot_status_counts", [])))
        lines.append(f"- {table_name} step_status별 건수:")
        lines.extend(format_status_counts(stats.validations.get(f"{table_name}_step_status_counts", [])))
        lines.append(f"- {table_name} 동일 lot_id 내 LOT_STATUS unique count > 1 LOT 수: {stats.validations.get(f'{table_name}_lot_status_multi_value_lot_count', 0):,}")
        lines.append(f"- {table_name} STEP_STATUS와 LOT_STATUS가 동일한 row 비율: {stats.validations.get(f'{table_name}_step_lot_status_same_row_ratio', 0)}%")
        lines.append(f"- {table_name} 현스텝이 아닌 row에서 EXCLUSION/HOLD/FTP/EXCEPTION이 STEP_STATUS로 복사된 의심 row 수: {stats.validations.get(f'{table_name}_non_current_exclusion_copied_step_status_rows', 0):,}")
        lines.append(f"- {table_name} STEP_STATUS WAIT(진행불가) 중 TIP/DOWN 원인 row 수: {stats.validations.get(f'{table_name}_wait_block_tip_down_origin_rows', 0):,}")
        lines.append(f"- {table_name} STEP_STATUS WAIT(진행불가) 중 EXCLUSION 원인 row 수: {stats.validations.get(f'{table_name}_wait_block_exclusion_origin_rows', 0):,}")
        lines.append(f"- {table_name} tip not null 건수: {stats.validations.get(f'{table_name}_tip_not_null', 0):,}")
        lines.append(f"- {table_name} down not null 건수: {stats.validations.get(f'{table_name}_down_not_null', 0):,}")
        lines.append(f"- {table_name} hold not null 건수: {stats.validations.get(f'{table_name}_hold_not_null', 0):,}")
        lines.append(f"- {table_name} exception not null 건수: {stats.validations.get(f'{table_name}_exception_not_null', 0):,}")
        lines.append(f"- {table_name} ftp not null 건수: {stats.validations.get(f'{table_name}_ftp_not_null', 0):,}")
        lines.append(f"- {table_name} AREA not null row 수: {stats.validations.get(f'{table_name}_AREA_not_null', 0):,}")
        lines.append(f"- {table_name} 투입경과_일 not null row 수: {stats.validations.get(f'{table_name}_투입경과_일_not_null', 0):,}")
        lines.append(f"- {table_name} 마지막이벤트경과_일 not null row 수: {stats.validations.get(f'{table_name}_마지막이벤트경과_일_not_null', 0):,}")
        lines.append(f"- {table_name} 스텝도착경과_일 not null row 수: {stats.validations.get(f'{table_name}_스텝도착경과_일_not_null', 0):,}")

    lines.extend(["", "## 저장 결과"])
    for name in ("f1", "f2", "f3"):
        lines.append(f"- {name} Excel 저장 경로: `{stats.outputs.get(f'{name}_excel')}`")
        lines.append(f"- {name} Parquet 저장 경로: `{stats.outputs.get(f'{name}_parquet')}`")
        if stats.outputs.get(f"{name}_csv_parts"):
            lines.append(f"- {name} CSV 분할 저장 경로: `{stats.outputs.get(f'{name}_csv_parts')}`")
    lines.append("")

    lines.append("## 날짜 파싱 검증")
    lines.append(f"- {stats.validations.get('korean_datetime_parse_examples')}")
    for key, value in stats.date_failures.items():
        lines.append(
            f"- {key}: 전체 {value.get('total', 0):,}건 / "
            f"파싱 성공 {value.get('success', 0):,}건 / 파싱 실패 {value.get('count', 0):,}건"
        )
        if value["samples"]:
            lines.append(f"  - 실패 샘플: {', '.join(map(str, value['samples'][:20]))}")
    lines.append("")

    lines.append("## ORDER_SEQ 숫자 정렬 검증")
    lines.append(f"- {stats.validations.get('order_seq_sort_rule')}")
    for table_name in ("f1", "f2", "f3"):
        lines.append(f"- {table_name} 숫자 변환 실패 ORDER_SEQ 건수: {stats.validations.get(f'{table_name}_order_seq_cast_fail_count', 0):,}")
        samples = stats.validations.get(f"{table_name}_order_seq_cast_fail_samples", [])
        if samples:
            lines.append(f"  - 샘플: {', '.join(map(str, samples[:20]))}")
    lines.append("")

    lines.append("## TODO")
    for item in stats.todo_items:
        lines.append(f"- {item}")
    lines.append("")

    log_path.write_text("\n".join(lines), encoding="utf-8")
    stats.outputs["log"] = str(log_path)
    return log_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="f1/f2/f3 생성")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--export-excel", action="store_true", help="검증용 Excel export 실행(느림)")
    group.add_argument("--no-export-excel", action="store_true", help="검증용 Excel export 생략(운영 기본값)")
    parser.add_argument("--export-excel-include-f1", action="store_true", help="Excel export 시 f1까지 포함(매우 느림)")
    return parser.parse_args(argv)


def _env_export_excel_enabled() -> bool:
    return str(os.environ.get("MULTIWIP_EXPORT_EXCEL") or "").strip().lower() in {"1", "true", "y", "yes", "on"}


def main(argv: list[str] | None = None) -> None:
    """전체 f1/f2/f3 생성 흐름을 실행한다."""
    args = parse_args(argv)
    export_excel = bool(args.export_excel or (_env_export_excel_enabled() and not args.no_export_excel))
    ensure_output_dirs()
    stats = RunStats()
    con = duckdb.connect(database=":memory:")
    # DuckDB 튜닝: 스레드/메모리 상한과 spill 경로를 환경변수로 조정할 수 있게 한다.
    # (기본값은 DuckDB 자동 설정을 따르므로 미설정 시 기존 동작과 동일하다)
    for pragma_env, pragma_sql in (
        ("MULTIWIP_DUCKDB_THREADS", "SET threads = {}"),
        ("MULTIWIP_DUCKDB_MEMORY_LIMIT", "SET memory_limit = '{}'"),
        ("MULTIWIP_DUCKDB_TEMP_DIR", "SET temp_directory = '{}'"),
    ):
        value = os.environ.get(pragma_env)
        if value:
            try:
                con.execute(pragma_sql.format(value))
                print(f"[INFO] {pragma_env}={value} 적용")
            except Exception as exc:
                print(f"[WARN] {pragma_env} 적용 실패({value}): {exc}")
    try:
        with timer("원천 m/s/t/h 조회 시간"):
            load_csv_to_duckdb(con, stats)
        with timer("h hold/exception/ftp view 생성 시간"):
            build_h_views(con, stats)
        with timer("f1 생성 + LOT_STATUS/STEP_STATUS + eqpgroup_cham 보정 시간"):
            build_f1(con, stats)
            log_table_count(con, "f1_raw", stats)
            log_table_count(con, "f1", stats)
        with timer("f2 생성 시간"):
            build_f2(con, stats)
            log_table_count(con, "f2", stats)
        with timer("f3 생성 시간"):
            build_f3(con, stats)
            log_table_count(con, "f3", stats)
        with timer("date validation 시간"):
            collect_date_failures(con, stats)
        with timer("parquet 저장 시간"):
            export_parquet(con, stats)
        if export_excel:
            with timer("excel 저장 시간"):
                export_excel_safely(con, stats, include_f1=args.export_excel_include_f1)
        else:
            print("[SKIP] excel 저장은 기본 비활성화되어 건너뜁니다. 필요 시 --export-excel 또는 MULTIWIP_EXPORT_EXCEL=1 사용")
        with timer("validation 수집 시간"):
            collect_validations(con, stats)
        with timer("run log 저장 시간"):
            write_run_log(stats)
        print("[DONE] 실행 완료")
    finally:
        con.close()


if __name__ == "__main__":
    main()
