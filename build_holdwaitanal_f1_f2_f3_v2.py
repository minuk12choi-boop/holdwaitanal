# -*- coding: utf-8 -*-
r"""
7_holdwaitanal 프로젝트에서 사내 DB 원천을 직접 조회하여 중간 CSV 없이 f1/f2/f3를 생성한다.

핵심 원칙
- TrackInPrevent / StepPath는 사용자가 지정한 넓은 조회 범위를 유지한다.
- 대형 조회 결과는 한 번만 받아 즉시 DuckDB 작업 DB에 적재한 뒤 pandas 객체를 해제한다.
- Equipment / EqpGroup / Lot은 한 번만 조회하여 Tip·Step에서 공통 재사용한다.
- h(HOLD/EXCEPTION/FTP)는 이번 버전에서 제외하고 최종 컬럼은 NULL로 유지한다.
- AREA도 이번 버전에서는 NULL로 유지한다.
- 최종 결과는 f1/f2/f3 Parquet과 검증 로그로 저장한다.

필수 패키지
    pip install duckdb pandas pyarrow openpyxl

실행 예
    cd D:\\PERSONAL_SPACE\\SW\\python\\7_holdwaitanal
    python build_holdwaitanal_f1_f2_f3.py --smoke-test
    python build_holdwaitanal_f1_f2_f3.py --rebuild
    python build_holdwaitanal_f1_f2_f3.py --rebuild --export-excel

주의
- DuckDB는 별도 프로그램/서버가 아니라 Python 패키지다.
- getData가 대형 결과를 항상 pandas DataFrame 전체로 반환한다면, 각 단일 조회 순간의
  메모리 피크는 피할 수 없다. 대신 여러 대형 DataFrame이 동시에 살아 있는 상황은 막는다.
"""

from __future__ import annotations

import argparse
import gc
import inspect
import json
import math
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

import duckdb
import pandas as pd

try:
    from bigdataquery import getData
except ImportError as exc:
    print("[ERROR] bigdataquery.getData를 불러오지 못했습니다.")
    raise


# =============================================================================
# 경로 / 실행 설정
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PARQUET_DIR = OUTPUT_DIR / "parquet"
EXCEL_DIR = OUTPUT_DIR / "excel"
LOG_DIR = OUTPUT_DIR / "log"
TEMP_DIR = OUTPUT_DIR / "duckdb_temp"
WORK_DB_PATH = OUTPUT_DIR / "holdwaitanal_work.duckdb"
SMOKE_DB_PATH = OUTPUT_DIR / "holdwaitanal_smoke.duckdb"

SMOKE_TIP_ROWS_PER_LINE = 5_000
SMOKE_STEP_ROWS_PER_LINE = 20_000
SMOKE_LOT_COUNT_PER_LINE = 20

EXCEL_MAX_ROWS_PER_SHEET = 1_000_000
EXCEL_CSV_FALLBACK_ROWS = 5_000_000

BATCH_KINDS = ("BATCH_FURNACE", "BATCH_WET")
EQP_ISSUE_STATUS = ("LOCAL", "PM", "DOWN")

SUMMARY_OUTPUT_COLUMNS = [
    "lot_inform", "line", "현재위치", "전산라인", "투입라인", "lot_id", "carr_id",
    "grade", "lot_type", "lot_level", "qty", "bay", "sendfab", "투입경과_일",
    "마지막이벤트경과_일", "스텝도착경과_일", "lot_status", "step_status", "proc_id",
    "de_rank", "연속", "AREA", "layer_id", "현스텝", "order_seq", "step_seq",
    "step_desc", "recipe_id", "eqp_type", "batch_kind", "eqpline", "eqpgroup",
    "eqpgroup_cham", "tip", "down", "hold", "hold_reason", "exception",
    "exception_reason", "ftp", "ftp_reason",
]

BLOCKED_SUMMARY_COLUMNS = {"eqpid", "eqpcham"}


# =============================================================================
# 원천 SQL
# =============================================================================
LOT_QUERY = r"""
WITH
m AS (
    SELECT 'PFR1' AS line, sys_line_id, cur_line_id, origin_line_id, lot_id, last_event_date
    FROM MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
    UNION ALL
    SELECT 'KFR7' AS line, sys_line_id, cur_line_id, origin_line_id, lot_id, last_event_date
    FROM MOS_KH_SMI.SMICDC_NRDK_MC_LOT
),
c AS (
    SELECT lot_id, MAX(last_event_date) AS max_event_date
    FROM m
    GROUP BY lot_id
),
m0 AS (
    SELECT m.line, m.lot_id, c.max_event_date
    FROM m
    JOIN c ON m.lot_id = c.lot_id
    WHERE m.last_event_date = c.max_event_date
),
t1 AS (
    SELECT line_id, lot_id, new_attr_value
    FROM (
        SELECT line_id, lot_id, new_attr_value, lot_transn_time,
               MAX(lot_transn_time) OVER (PARTITION BY lot_id, line_id) AS max_transn_time,
               SUM(CASE WHEN wip_attribute = 'FLOWLEVEL' THEN 1 ELSE NULL END)
                   OVER (PARTITION BY lot_id, step_seq, line_id) AS flowlevel_cnt
        FROM FAB.M_LOT_TRANSN_HIST
        WHERE lot_transn_type = 'ModifyAttr'
          AND wip_attribute IN ('GRADE')
          AND line_id IN ('PFR1', 'KFR7')
    ) h
    WHERE flowlevel_cnt IS NULL
      AND max_transn_time = lot_transn_time
),
g AS (
    SELECT DISTINCT line_id, lot_id, new_attr_value AS grade
    FROM t1
),
m1 AS (
    SELECT 'PFR1' AS line,
           m.cur_line_id, m.sys_line_id, m.origin_line_id, m.lot_id, m.carr_id,
           m.lot_type,
           CASE WHEN COALESCE(g.grade, '-') <> '-' THEN CONCAT('G', g.grade) END AS grade,
           CAST(CAST(m.lot_level AS BIGINT) AS STRING) AS lot_level,
           m.cur_qty, m.bay_name,
           CASE WHEN m.lot_status_seg = 'Hold' THEN 'HOLD' ELSE m.step_status_seg END AS status,
           m.proc_id,
           CAST(CAST(m.order_seq AS BIGINT) AS STRING) AS order_seq,
           m.step_seq,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.start_date, '[^0-9]', ''), 1, 14), 14, '0'), 'yyyyMMddHHmmss') AS start_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.last_tkout_date, '[^0-9]', ''), 1, 14), 14, '0'), 'yyyyMMddHHmmss') AS last_tkout_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.step_arrive_date, '[^0-9]', ''), 1, 14), 14, '0'), 'yyyyMMddHHmmss') AS step_arrive_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.last_event_date, '[^0-9]', ''), 1, 14), 14, '0'), 'yyyyMMddHHmmss') AS last_event_date,
           NULL AS fa_object4
    FROM MOS_KH_SMI.SMICDC_P3NRD_MC_LOT m
    JOIN m0 ON m.lot_id = m0.lot_id AND m.last_event_date = m0.max_event_date
    LEFT JOIN g ON m.lot_id = g.lot_id AND g.line_id = 'PFR1'
    WHERE m.lot_status_seg IN ('Active', 'Hold')
      AND m.order_seq IS NOT NULL

    UNION ALL

    SELECT 'KFR7' AS line,
           m.cur_line_id, m.sys_line_id, m.origin_line_id, m.lot_id, m.carr_id,
           m.lot_type,
           CASE WHEN COALESCE(g.grade, '-') <> '-' THEN CONCAT('G', g.grade) END AS grade,
           CAST(CAST(m.lot_level AS BIGINT) AS STRING) AS lot_level,
           m.cur_qty, m.bay_name,
           CASE WHEN m.lot_status_seg = 'Hold' THEN 'HOLD' ELSE m.step_status_seg END AS status,
           m.proc_id,
           CAST(CAST(m.order_seq AS BIGINT) AS STRING) AS order_seq,
           m.step_seq,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.start_date, '[^0-9]', ''), 1, 14), 14, '0'), 'yyyyMMddHHmmss') AS start_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.last_tkout_date, '[^0-9]', ''), 1, 14), 14, '0'), 'yyyyMMddHHmmss') AS last_tkout_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.step_arrive_date, '[^0-9]', ''), 1, 14), 14, '0'), 'yyyyMMddHHmmss') AS step_arrive_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.last_event_date, '[^0-9]', ''), 1, 14), 14, '0'), 'yyyyMMddHHmmss') AS last_event_date,
           w.fa_object4
    FROM MOS_KH_SMI.SMICDC_NRDK_MC_LOT m
    JOIN m0 ON m.lot_id = m0.lot_id AND m.last_event_date = m0.max_event_date
    LEFT JOIN g ON m.lot_id = g.lot_id AND g.line_id = 'KFR7'
    LEFT JOIN MOS_KH_SMI.SMICDC_NRDK_MATERIALWORKSTATUS w ON m.lot_id = w.lotid
    WHERE m.lot_status_seg IN ('Active', 'Hold')
),
co AS (
    SELECT line, lot_id, lot_inform
    FROM (
        SELECT 'PFR1' AS line, lot_id, step_comment AS lot_inform,
               update_date AS cmt_time,
               MAX(update_date) OVER (PARTITION BY lot_id) AS max_cmt_time
        FROM MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_COMMENT
        WHERE parent_order_seq = 0 AND comment_type = 'LOT'
    ) p1
    WHERE max_cmt_time = cmt_time

    UNION ALL

    SELECT line, lot_id, lot_inform
    FROM (
        SELECT 'KFR7' AS line, lot_id, step_comment AS lot_inform,
               update_date AS cmt_time,
               MAX(update_date) OVER (PARTITION BY lot_id) AS max_cmt_time
        FROM MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_COMMENT
        WHERE parent_order_seq = 0 AND comment_type = 'LOT'
    ) k7
    WHERE max_cmt_time = cmt_time
)
SELECT DISTINCT
       m1.*,
       CASE
           WHEN m1.origin_line_id <> m1.sys_line_id THEN 'OLD_SEND'
           WHEN m1.origin_line_id = m1.sys_line_id AND m1.sys_line_id <> m1.cur_line_id THEN 'NEW_SEND'
       END AS sendfab,
       co.lot_inform
FROM m1
LEFT JOIN co ON m1.line = co.line AND m1.lot_id = co.lot_id
WHERE m1.line = m1.sys_line_id
  AND m1.lot_type IN ('PP', 'PB', 'PG', 'TT')
  AND m1.cur_line_id NOT IN ('CHTV')
"""

KFR7_TIP_QUERY = r"""
SELECT process, step, ppid, eqpid, chamberid,
       type, checkcount, tkin_count, updated, eventtime
FROM MOS_KH_SMI.SMICDC_NRDK_TRACKINPREVENT
WHERE owner IN ('LEVEL1', 'PHOTO_LEVEL1')
"""

PFR1_TIP_QUERY = r"""
SELECT process, step, ppid, eqpid, chamberid,
       type, checkcount, tkin_count, updated, eventtime
FROM MOS_KH_SMI.SMICDC_P3NRD_TRACKINPREVENT
WHERE owner IN ('LEVEL1', 'PHOTO_LEVEL1')
"""

EQP_QUERY = r"""
SELECT line_id, origin_line_id, batch_kind, eqp_id,
       eqp_status, tool_kind, eqp_status_change_time
FROM (
    SELECT e.line_id, e.origin_line_id, e.batch_kind, e.eqp_id,
           e.eqp_status, e.tool_kind, e.eqp_status_change_time,
           e.impala_insert_time,
           MAX(e.impala_insert_time) OVER (PARTITION BY e.line_id, e.eqp_id) AS max_impala_insert_time
    FROM MOS_KH_SMI.SMIMES_MI_EQUIPMENT e
    WHERE e.line_id IN ('KFR7', 'PFR1')
) x
WHERE x.impala_insert_time = x.max_impala_insert_time
"""

EQP_GROUP_QUERY = r"""
SELECT line_id, eqp_group_name, eqp_id
FROM (
    SELECT g.line_id, g.eqp_group_name, g.eqp_id, g.impala_insert_time,
           MAX(g.impala_insert_time)
               OVER (PARTITION BY g.line_id, g.eqp_group_name, g.eqp_id) AS max_impala_insert_time
    FROM MOS_KH_SMI.SMIMES_MI_EQP_GROUP_LIST g
    WHERE g.line_id IN ('KFR7', 'PFR1')
) x
WHERE x.impala_insert_time = x.max_impala_insert_time
"""

KFR7_STEP_PATH_QUERY = r"""
SELECT lot_id, order_seq, proc_id, step_seq, step_desc, step_level,
       step_skip_yn, delay_step_type, delay_time_mins, layer_id,
       eqp_type, eqp_group_id, recipe_id, ext_1st_vals, tkin_type_detail
FROM MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_PATH
"""

PFR1_STEP_PATH_QUERY = r"""
SELECT lot_id, order_seq, proc_id, step_seq, step_desc, step_level,
       step_skip_yn, delay_step_type, delay_time_mins, layer_id,
       eqp_type, eqp_group_id, recipe_id, ext_1st_vals, tkin_type_detail
FROM MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_PATH
"""


# =============================================================================
# 공통 유틸
# =============================================================================
@dataclass
class RunStats:
    counts: dict[str, int] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    validations: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@contextmanager
def timer(stats: RunStats, label: str):
    start = perf_counter()
    print(f"[TIMER] {label} start")
    try:
        yield
    finally:
        elapsed = perf_counter() - start
        stats.timings[label] = elapsed
        print(f"[TIMER] {label} elapsed={elapsed:.3f}s")


def ensure_dirs() -> None:
    for path in (OUTPUT_DIR, PARQUET_DIR, EXCEL_DIR, LOG_DIR, TEMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def sql_text(value: str | Path) -> str:
    return str(value).replace("'", "''")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def normalize_column_token(name: str) -> str:
    """대소문자, _, -, 공백 차이를 무시한 컬럼 비교 토큰."""
    return re.sub(r"[_\-\s]+", "", str(name).strip().lower())


def canonicalize_dataframe_columns(df: pd.DataFrame, expected: Iterable[str], table_name: str) -> pd.DataFrame:
    """비슷한 컬럼명을 expected 이름으로 정규화한다.

    대용량 원천에서 df.copy()는 메모리를 거의 한 번 더 사용하므로 하지 않는다.
    이 파이프라인은 조회 직후 DataFrame을 staging하고 폐기하므로 컬럼명만 제자리 변경한다.
    """
    token_map: dict[str, list[str]] = {}
    for column in df.columns:
        token_map.setdefault(normalize_column_token(column), []).append(str(column))

    rename_map: dict[str, str] = {}
    for target in expected:
        candidates = token_map.get(normalize_column_token(target), [])
        if len(candidates) > 1:
            raise RuntimeError(f"{table_name}: {target} 후보 컬럼이 여러 개입니다: {candidates}")
        if len(candidates) == 1:
            rename_map[candidates[0]] = target.lower()

    if rename_map:
        df.rename(columns=rename_map, inplace=True)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def inspect_getdata_capabilities(stats: RunStats) -> None:
    info: dict[str, Any] = {
        "object_type": str(type(getData)),
        "module": getattr(getData, "__module__", None),
    }
    try:
        info["signature"] = str(inspect.signature(getData))
    except Exception as exc:
        info["signature_error"] = repr(exc)
    try:
        source = inspect.getsource(getData)
        info["source_keywords"] = {
            word: (word in source)
            for word in ("chunksize", "chunk_size", "batch_size", "fetchmany", "arrow", "iterator", "yield")
        }
    except Exception as exc:
        info["source_error"] = repr(exc)
    stats.validations["getData_capabilities"] = info


def get_data(query: str, label: str, stats: RunStats) -> pd.DataFrame:
    print(f"[QUERY] {label} 조회 시작")
    result = getData(param=query, convert_type=True, verbose=True)
    stats.validations[f"{label}_return_type"] = str(type(result))
    # 사전 점검에서 현재 환경의 getData 반환형이 pandas.DataFrame으로 확인되었다.
    # 예상과 달라질 경우 묵시적으로 변환하지 않고 즉시 중단해 메모리 복사를 방지한다.
    if not isinstance(result, pd.DataFrame):
        raise TypeError(f"{label}: getData 반환값이 pandas DataFrame이 아닙니다: {type(result)}")
    print(f"[QUERY] {label} rows={len(result):,}, cols={len(result.columns):,}")
    stats.counts[f"raw_{label}_rows"] = len(result)
    return result


def stage_dataframe(
    con: duckdb.DuckDBPyConnection,
    df: pd.DataFrame,
    table_name: str,
    stats: RunStats,
    expected_columns: Iterable[str],
    select_columns: Iterable[str] | None = None,
    replace: bool = True,
) -> None:
    """DataFrame을 DuckDB에 즉시 적재하고 원본 pandas 메모리는 호출부에서 해제한다."""
    normalized = canonicalize_dataframe_columns(df, expected_columns, table_name)
    missing = [c.lower() for c in expected_columns if c.lower() not in normalized.columns]
    if missing:
        raise RuntimeError(f"{table_name}: 필수 컬럼 누락: {missing}; 실제 컬럼={list(normalized.columns)}")

    con.register("_stage_df", normalized)
    try:
        if replace:
            con.execute(f"DROP TABLE IF EXISTS {quote_ident(table_name)}")
        columns = list(select_columns or [c.lower() for c in expected_columns])
        select_sql = ", ".join(quote_ident(c) for c in columns)
        con.execute(
            f"CREATE TABLE {quote_ident(table_name)} AS SELECT {select_sql} FROM _stage_df"
        )
    finally:
        con.unregister("_stage_df")
    count = con.execute(f"SELECT COUNT(*) FROM {quote_ident(table_name)}").fetchone()[0]
    stats.counts[f"{table_name}_rows"] = count
    print(f"[STAGE] {table_name} rows={count:,}")


def query_table_count(con: duckdb.DuckDBPyConnection, table_name: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {quote_ident(table_name)}").fetchone()[0]


def table_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
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


def parsed_timestamp_expr(column_name: str) -> str:
    return (
        f"COALESCE(TRY_CAST({column_name} AS TIMESTAMP), "
        f"TRY_STRPTIME(REGEXP_REPLACE(TRIM(CAST({column_name} AS VARCHAR)), '[^0-9]', ''), '%Y%m%d%H%M%S'))"
    )


def elapsed_days_number_expr(column_name: str) -> str:
    parsed = parsed_timestamp_expr(column_name)
    return f"ROUND((EPOCH(CURRENT_TIMESTAMP) - EPOCH({parsed})) / 86400.0, 1)"


def elapsed_days_text_expr(column_name: str) -> str:
    return f"FORMAT('{{:.1f}}', {elapsed_days_number_expr(column_name)}) || '일↑'"


def prefixed_column_list(columns: list[str], alias: str | None = None, indent: str = "            ") -> str:
    prefix = f"{alias}." if alias else ""
    return ",\n".join(f"{indent}{prefix}{quote_ident(c)}" for c in columns)


def summary_export_query(table_name: str) -> str:
    columns = prefixed_column_list(SUMMARY_OUTPUT_COLUMNS)
    return f"""
        SELECT
{columns}
        FROM {table_name}
        ORDER BY line, lot_id, TRY_CAST(order_seq AS BIGINT) NULLS LAST, order_seq, eqpgroup_cham
    """

def limited_query(query: str, limit: int) -> str:
    """원본 SQL의 의미를 건드리지 않고 외부 SELECT에서 행 수만 제한한다."""
    return f"SELECT * FROM ({query.strip().rstrip(';')}) smoke_q LIMIT {int(limit)}"


def smoke_step_query(query: str, lot_ids: list[str], limit: int) -> str:
    """스모크 테스트에서 실제 재공 LOT과 겹치는 StepPath만 소량 조회한다."""
    if not lot_ids:
        return limited_query(query, limit)
    quoted = ", ".join("'" + str(v).replace("'", "''") + "'" for v in lot_ids)
    base = query.strip().rstrip(';')
    return (
        f"SELECT * FROM ({base}) smoke_step "
        f"WHERE CAST(lot_id AS STRING) IN ({quoted}) LIMIT {int(limit)}"
    )


def pick_smoke_lot_ids(df_lot: pd.DataFrame) -> dict[str, list[str]]:
    """라인별 실제 LOT_ID를 골라 StepPath 스모크 조회에 사용한다."""
    cols = {normalize_column_token(c): c for c in df_lot.columns}
    line_col = cols.get(normalize_column_token("line"))
    lot_col = cols.get(normalize_column_token("lot_id"))
    if line_col is None or lot_col is None:
        return {"KFR7": [], "PFR1": []}
    out: dict[str, list[str]] = {}
    for line in ("KFR7", "PFR1"):
        values = (
            df_lot.loc[df_lot[line_col].astype(str).eq(line), lot_col]
            .dropna().astype(str).drop_duplicates().head(SMOKE_LOT_COUNT_PER_LINE).tolist()
        )
        out[line] = values
    return out


# =============================================================================
# 원천 조회 및 staging
# =============================================================================
def load_sources(con: duckdb.DuckDBPyConnection, stats: RunStats, smoke_test: bool = False) -> None:
    # Lot은 약 4천 행 수준이므로 smoke에서도 전체 조회해 실제 LOT_ID를 확보한다.
    df = get_data(LOT_QUERY, "lot", stats)
    smoke_lot_ids = pick_smoke_lot_ids(df) if smoke_test else {"KFR7": [], "PFR1": []}
    if smoke_test:
        stats.validations["smoke_lot_ids"] = smoke_lot_ids
    stage_dataframe(
        con, df, "lot_raw", stats,
        expected_columns=[
            "line", "cur_line_id", "sys_line_id", "origin_line_id", "lot_id", "carr_id",
            "lot_type", "grade", "lot_level", "cur_qty", "bay_name", "status", "proc_id",
            "order_seq", "step_seq", "start_date", "last_tkout_date", "step_arrive_date",
            "last_event_date", "fa_object4", "sendfab", "lot_inform",
        ],
    )
    del df
    gc.collect()

    # 공통 dimension은 작고 downstream 매칭률에 중요하므로 smoke에서도 전체 조회한다.
    df = get_data(EQP_QUERY, "equipment", stats)
    stage_dataframe(
        con, df, "equipment_raw", stats,
        expected_columns=[
            "line_id", "origin_line_id", "batch_kind", "eqp_id", "eqp_status",
            "tool_kind", "eqp_status_change_time",
        ],
    )
    del df
    gc.collect()

    df = get_data(EQP_GROUP_QUERY, "eqp_group", stats)
    stage_dataframe(
        con, df, "eqp_group_raw", stats,
        expected_columns=["line_id", "eqp_group_name", "eqp_id"],
    )
    del df
    gc.collect()

    for line, query, table in (
        ("KFR7", KFR7_TIP_QUERY, "tip_kfr7_raw"),
        ("PFR1", PFR1_TIP_QUERY, "tip_pfr1_raw"),
    ):
        actual_query = limited_query(query, SMOKE_TIP_ROWS_PER_LINE) if smoke_test else query
        df = get_data(actual_query, f"{line}_tip", stats)
        stage_dataframe(
            con, df, table, stats,
            expected_columns=[
                "process", "step", "ppid", "eqpid", "chamberid", "type",
                "checkcount", "tkin_count", "updated", "eventtime",
            ],
        )
        del df
        gc.collect()

    for line, query, table in (
        ("KFR7", KFR7_STEP_PATH_QUERY, "step_kfr7_raw"),
        ("PFR1", PFR1_STEP_PATH_QUERY, "step_pfr1_raw"),
    ):
        actual_query = (
            smoke_step_query(query, smoke_lot_ids.get(line, []), SMOKE_STEP_ROWS_PER_LINE)
            if smoke_test else query
        )
        df = get_data(actual_query, f"{line}_step_path", stats)
        stage_dataframe(
            con, df, table, stats,
            expected_columns=[
                "lot_id", "order_seq", "proc_id", "step_seq", "step_desc", "step_level",
                "step_skip_yn", "delay_step_type", "delay_time_mins", "layer_id",
                "eqp_type", "eqp_group_id", "recipe_id", "ext_1st_vals", "tkin_type_detail",
            ],
        )
        del df
        gc.collect()


# =============================================================================
# m / equipment / eqp_group 정규화
# =============================================================================
def build_dimensions(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    con.execute("DROP TABLE IF EXISTS m")
    con.execute(
        """
        CREATE TABLE m AS
        SELECT
            CAST(line AS VARCHAR) AS LINE,
            CAST(cur_line_id AS VARCHAR) AS CUR_LINE_ID,
            CAST(sys_line_id AS VARCHAR) AS SYS_LINE_ID,
            CAST(origin_line_id AS VARCHAR) AS ORIGIN_LINE_ID,
            CAST(lot_id AS VARCHAR) AS LOT_ID,
            CAST(carr_id AS VARCHAR) AS CARR_ID,
            CAST(lot_type AS VARCHAR) AS LOT_TYPE,
            CAST(grade AS VARCHAR) AS GRADE,
            CAST(lot_level AS VARCHAR) AS LOT_LEVEL,
            CAST(cur_qty AS VARCHAR) AS CUR_QTY,
            CAST(bay_name AS VARCHAR) AS BAY_NAME,
            CAST(status AS VARCHAR) AS STATUS,
            CAST(proc_id AS VARCHAR) AS PROC_ID,
            CAST(order_seq AS VARCHAR) AS ORDER_SEQ,
            CAST(step_seq AS VARCHAR) AS STEP_SEQ,
            CAST(start_date AS VARCHAR) AS START_DATE,
            CAST(last_tkout_date AS VARCHAR) AS LAST_TKOUT_DATE,
            CAST(step_arrive_date AS VARCHAR) AS STEP_ARRIVE_DATE,
            CAST(last_event_date AS VARCHAR) AS LAST_EVENT_DATE,
            CAST(sendfab AS VARCHAR) AS SENDFAB,
            CAST(lot_inform AS VARCHAR) AS LOT_INFORM
        FROM lot_raw
        """
    )

    con.execute("DROP TABLE IF EXISTS equipment")
    con.execute(
        """
        CREATE TABLE equipment AS
        SELECT DISTINCT
            CAST(line_id AS VARCHAR) AS line_id,
            CAST(origin_line_id AS VARCHAR) AS eqpline,
            CAST(batch_kind AS VARCHAR) AS batch_kind,
            CAST(eqp_id AS VARCHAR) AS eqp_id,
            CAST(eqp_status AS VARCHAR) AS eqp_status,
            CAST(tool_kind AS VARCHAR) AS tool_kind,
            CAST(eqp_status_change_time AS VARCHAR) AS eqp_status_change_time
        FROM equipment_raw
        WHERE tool_kind IN ('EQP', 'CHAMBER')
        """
    )

    con.execute("DROP TABLE IF EXISTS eqp_group")
    con.execute(
        """
        CREATE TABLE eqp_group AS
        SELECT DISTINCT
            CAST(line_id AS VARCHAR) AS line_id,
            CAST(eqp_group_name AS VARCHAR) AS eqp_group_name,
            CAST(eqp_id AS VARCHAR) AS eqp_id
        FROM eqp_group_raw
        WHERE COALESCE(CAST(eqp_id AS VARCHAR), '') NOT ILIKE '%OFF%'
        """
    )

    for name in ("m", "equipment", "eqp_group"):
        stats.counts[f"{name}_rows"] = query_table_count(con, name)

    # 키 중복 검증
    stats.validations["equipment_duplicate_line_eqp_keys"] = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT line_id, eqp_id
            FROM equipment
            GROUP BY line_id, eqp_id
            HAVING COUNT(*) > 1
        ) x
        """
    ).fetchone()[0]
    stats.validations["eqp_group_duplicate_keys"] = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT line_id, eqp_group_name, eqp_id
            FROM eqp_group
            GROUP BY line_id, eqp_group_name, eqp_id
            HAVING COUNT(*) > 1
        ) x
        """
    ).fetchone()[0]


# =============================================================================
# Tip 전처리: pandas 대신 DuckDB SQL로 수행
# =============================================================================
def build_tip(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    con.execute("DROP TABLE IF EXISTS tip_raw_union")
    con.execute(
        """
        CREATE TABLE tip_raw_union AS
        SELECT 'KFR7' AS line, * FROM tip_kfr7_raw
        UNION ALL
        SELECT 'PFR1' AS line, * FROM tip_pfr1_raw
        """
    )

    con.execute("DROP TABLE IF EXISTS tip_t")
    con.execute(
        """
        CREATE TABLE tip_t AS
        SELECT
            CAST(line AS VARCHAR) AS line,
            CAST(process AS VARCHAR) AS process,
            CAST(step AS VARCHAR) AS step,
            CAST(ppid AS VARCHAR) AS ppid,
            CAST(eqpid AS VARCHAR) AS eqpid,
            '-' AS lot_type,
            CASE WHEN chamberid IS NULL OR CAST(chamberid AS VARCHAR) IN ('-', 'MAIN')
                 THEN 'MAIN' ELSE CAST(chamberid AS VARCHAR) END AS chamberid,
            CASE
                WHEN type = 'DOING' THEN 'DOING'
                WHEN type = 'PREVENT' AND COALESCE(TRY_CAST(checkcount AS DOUBLE), 0) = 0 THEN 'PREVENT'
                WHEN type = 'PREVENT' AND COALESCE(TRY_CAST(tkin_count AS DOUBLE), 0) >= COALESCE(TRY_CAST(checkcount AS DOUBLE), 0) THEN 'PREVENT'
                WHEN type = 'PREVENT' AND COALESCE(TRY_CAST(tkin_count AS DOUBLE), 0) < COALESCE(TRY_CAST(checkcount AS DOUBLE), 0) THEN 'DOING'
            END AS prevent,
            CASE
                WHEN chamberid IS NOT NULL AND CAST(chamberid AS VARCHAR) NOT IN ('MAIN', '-')
                THEN CAST(eqpid AS VARCHAR) || '-' || CAST(chamberid AS VARCHAR)
                ELSE CAST(eqpid AS VARCHAR)
            END AS ee,
            COALESCE(CAST(eventtime AS VARCHAR), CAST(updated AS VARCHAR)) AS eventtime
        FROM tip_raw_union
        """
    )

    con.execute("DROP TABLE IF EXISTS tip_ttt")
    con.execute(
        """
        CREATE TABLE tip_ttt AS
        SELECT * EXCLUDE (doing_count, group_count, prevent_max_time)
        FROM (
            SELECT
                t.*,
                SUM(CASE WHEN prevent = 'DOING' THEN 1 ELSE 0 END)
                    OVER (PARTITION BY line, process, step, ppid, ee, lot_type) AS doing_count,
                COUNT(ee)
                    OVER (PARTITION BY line, process, step, ppid, ee, lot_type) AS group_count,
                MAX(eventtime)
                    OVER (PARTITION BY line, process, step, ppid, ee, lot_type, prevent) AS prevent_max_time
            FROM tip_t t
        ) x
        WHERE (group_count > 1 AND doing_count > 0 AND prevent = 'DOING' AND prevent_max_time = eventtime)
           OR (group_count > 1 AND doing_count = 0 AND prevent_max_time = eventtime)
           OR group_count = 1
        """
    )

    # equipment 차원은 line+eqp_id 기준으로만 결합한다.
    con.execute("DROP TABLE IF EXISTS tip_te")
    con.execute(
        """
        CREATE TABLE tip_te AS
        SELECT
            t.*,
            e.batch_kind,
            e.eqpline
        FROM tip_ttt t
        LEFT JOIN (
            SELECT DISTINCT line_id, eqp_id, batch_kind, eqpline
            FROM equipment
        ) e
          ON t.line = e.line_id
         AND t.ee = e.eqp_id
        """
    )

    con.execute("DROP TABLE IF EXISTS tip_main")
    con.execute("CREATE TABLE tip_main AS SELECT * FROM tip_te WHERE chamberid IN ('MAIN', '-') OR chamberid IS NULL")
    con.execute("DROP TABLE IF EXISTS tip_cham")
    con.execute("CREATE TABLE tip_cham AS SELECT * FROM tip_te WHERE chamberid NOT IN ('MAIN', '-') AND chamberid IS NOT NULL")

    con.execute("DROP TABLE IF EXISTS tip_tee")
    con.execute(
        """
        CREATE TABLE tip_tee AS
        SELECT
            a.line, a.process, a.step, a.ppid, a.eqpid,
            CASE WHEN COALESCE(a.batch_kind, '-') IN ('BATCH_FURNACE', 'BATCH_WET')
                 THEN NULL ELSE b.chamberid END AS chamberid,
            CASE WHEN COALESCE(a.batch_kind, '-') IN ('BATCH_FURNACE', 'BATCH_WET')
                 THEN a.eqpid ELSE COALESCE(b.ee, a.eqpid) END AS eqpcham,
            COALESCE(b.lot_type, a.lot_type) AS lot_type,
            a.batch_kind,
            CASE WHEN a.prevent = 'PREVENT' OR b.prevent = 'PREVENT' THEN 'PREVENT' ELSE 'DOING' END AS prevent,
            a.prevent AS type_body,
            b.prevent AS type_cham,
            CASE WHEN COALESCE(a.batch_kind, '-') IN ('BATCH_FURNACE', 'BATCH_WET')
                 THEN a.eventtime ELSE COALESCE(b.eventtime, a.eventtime) END AS eventtime,
            COALESCE(a.eqpline, b.eqpline) AS eqpline
        FROM tip_main a
        LEFT JOIN tip_cham b
          ON a.line = b.line
         AND a.process = b.process
         AND a.step = b.step
         AND a.ppid = b.ppid
         AND a.eqpid = b.eqpid
        """
    )

    con.execute("DROP TABLE IF EXISTS equipment_status")
    con.execute(
        """
        CREATE TABLE equipment_status AS
        WITH body AS (
            SELECT line_id, eqp_id, eqp_status, eqp_status_change_time
            FROM equipment
            WHERE tool_kind = 'EQP'
        ),
        cham AS (
            SELECT
                line_id,
                eqp_id,
                CASE
                    WHEN POSITION('-' IN eqp_id) > 0 THEN SPLIT_PART(eqp_id, '-', 1)
                    WHEN POSITION('_' IN eqp_id) > 0 THEN SPLIT_PART(eqp_id, '_', 1)
                END AS body_id,
                eqp_status,
                eqp_status_change_time
            FROM equipment
            WHERE tool_kind = 'CHAMBER'
        )
        SELECT DISTINCT
            b.line_id,
            COALESCE(c.eqp_id, b.eqp_id) AS eqpcham,
            b.eqp_status AS body_eqp_status,
            c.eqp_status AS cham_eqp_status,
            b.eqp_status_change_time AS body_status_change_time,
            c.eqp_status_change_time AS cham_status_change_time
        FROM body b
        LEFT JOIN cham c
          ON b.line_id = c.line_id
         AND b.eqp_id = c.body_id
        """
    )

    con.execute("DROP TABLE IF EXISTS t")
    con.execute(
        """
        CREATE TABLE t AS
        SELECT DISTINCT
            x.line AS LINE,
            x.process AS PROCESS,
            x.step AS STEP,
            x.ppid AS PPID,
            x.eqpid AS EQPID,
            x.eqpcham AS EQPCHAM,
            x.chamberid AS CHAMBERID,
            x.lot_type AS LOT_TYPE,
            x.batch_kind AS BATCH_KIND,
            x.prevent AS PREVENT,
            x.type_body AS TYPE_BODY,
            x.type_cham AS TYPE_CHAM,
            CASE WHEN x.prevent = 'PREVENT' THEN x.eventtime END AS TIP_EVENTTIME,
            CASE
                WHEN es.body_eqp_status IN ('LOCAL', 'PM', 'DOWN') THEN es.body_eqp_status
                WHEN es.cham_eqp_status IN ('LOCAL', 'PM', 'DOWN') THEN es.cham_eqp_status
            END AS EQPISSUE,
            es.body_eqp_status AS BODY_EQP_STATUS,
            es.cham_eqp_status AS CHAM_EQP_STATUS,
            CASE
                WHEN es.body_eqp_status IN ('LOCAL', 'PM', 'DOWN') THEN es.body_status_change_time
                WHEN es.cham_eqp_status IN ('LOCAL', 'PM', 'DOWN') THEN es.cham_status_change_time
            END AS EQPISSUETIME,
            x.eqpline AS EQPLINE
        FROM tip_tee x
        LEFT JOIN equipment_status es
          ON x.line = es.line_id
         AND x.eqpcham = es.eqpcham
        """
    )

    stats.counts["t_rows"] = query_table_count(con, "t")
    stats.validations["tip_main_rows"] = query_table_count(con, "tip_main")
    stats.validations["tip_cham_rows"] = query_table_count(con, "tip_cham")
    stats.validations["tip_tee_rows"] = query_table_count(con, "tip_tee")
    stats.validations["tip_join_amplification"] = round(
        stats.validations["tip_tee_rows"] / max(stats.validations["tip_main_rows"], 1), 4
    )


# =============================================================================
# Step 전처리: 대형 StepPath를 DuckDB 안에서 LOT 대상 제한 후 결합
# =============================================================================
def build_step(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    con.execute("DROP TABLE IF EXISTS step_path")
    con.execute(
        """
        CREATE TABLE step_path AS
        SELECT 'KFR7' AS line, * FROM step_kfr7_raw
        UNION ALL
        SELECT 'PFR1' AS line, * FROM step_pfr1_raw
        """
    )

    # 대상 lot/order_seq는 약 4천 건 수준이라 먼저 축약한다.
    con.execute("DROP TABLE IF EXISTS current_lot")
    con.execute(
        """
        CREATE TABLE current_lot AS
        SELECT DISTINCT
            LINE AS line,
            LOT_ID AS lot_id,
            TRY_CAST(ORDER_SEQ AS BIGINT) AS current_order_seq
        FROM m
        WHERE TRY_CAST(ORDER_SEQ AS BIGINT) IS NOT NULL
        """
    )

    # DE_RANK는 대상 lot에 대해서만 계산한다.
    con.execute("DROP TABLE IF EXISTS step_rank")
    con.execute(
        """
        CREATE TABLE step_rank AS
        SELECT DISTINCT
            p.line,
            CAST(p.lot_id AS VARCHAR) AS lot_id,
            TRY_CAST(p.order_seq AS BIGINT) AS order_seq_num,
            SUM(CASE WHEN p.delay_step_type = 'S' THEN 1 ELSE 0 END)
                OVER (
                    PARTITION BY p.line, p.lot_id
                    ORDER BY TRY_CAST(p.order_seq AS BIGINT)
                    RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS de_rank
        FROM step_path p
        INNER JOIN current_lot m
          ON CAST(p.line AS VARCHAR) = m.line
         AND CAST(p.lot_id AS VARCHAR) = m.lot_id
        WHERE p.delay_step_type IN ('S', 'Y')
          AND TRY_CAST(p.order_seq AS BIGINT) IS NOT NULL
        """
    )

    con.execute("DROP TABLE IF EXISTS step_candidate")
    con.execute(
        """
        CREATE TABLE step_candidate AS
        SELECT
            CAST(p.line AS VARCHAR) AS line,
            CAST(p.lot_id AS VARCHAR) AS lot_id,
            CAST(p.proc_id AS VARCHAR) AS proc_id,
            TRY_CAST(p.order_seq AS BIGINT) AS order_seq_num,
            CAST(TRY_CAST(p.order_seq AS BIGINT) AS VARCHAR) AS order_seq,
            r.de_rank,
            CAST(p.delay_step_type AS VARCHAR) AS delay_step_type,
            CASE
                WHEN p.delay_step_type = 'S' THEN '연속첫'
                WHEN p.delay_step_type = 'Y' THEN '연속(' || CAST(CAST(TRUNC(TRY_CAST(p.delay_time_mins AS DOUBLE)) AS BIGINT) AS VARCHAR) || ')'
            END AS "연속",
            CAST(NULL AS VARCHAR) AS AREA,
            CAST(p.layer_id AS VARCHAR) AS layer_id,
            CAST(TRY_CAST(p.step_level AS BIGINT) AS VARCHAR) AS step_level,
            COALESCE(NULLIF(CAST(p.tkin_type_detail AS VARCHAR), '-'), CAST(p.ext_1st_vals AS VARCHAR)) AS ein,
            CAST(p.step_seq AS VARCHAR) AS step_seq,
            CAST(p.step_desc AS VARCHAR) AS step_desc,
            CAST(p.eqp_type AS VARCHAR) AS eqp_type,
            CAST(p.eqp_group_id AS VARCHAR) AS eqp_group_raw,
            CAST(p.recipe_id AS VARCHAR) AS recipe_id
        FROM step_path p
        INNER JOIN current_lot m
          ON CAST(p.line AS VARCHAR) = m.line
         AND CAST(p.lot_id AS VARCHAR) = m.lot_id
        LEFT JOIN step_rank r
          ON CAST(p.line AS VARCHAR) = r.line
         AND CAST(p.lot_id AS VARCHAR) = r.lot_id
         AND TRY_CAST(p.order_seq AS BIGINT) = r.order_seq_num
        WHERE COALESCE(CAST(p.step_skip_yn AS VARCHAR), '') <> 'Y'
          AND TRY_CAST(p.order_seq AS BIGINT) >= m.current_order_seq
        """
    )

    # 설비그룹 확장은 업무상 정상 증가다. 조인 전후 행 수를 기록한다.
    stats.validations["step_candidate_rows_before_eqp_group"] = query_table_count(con, "step_candidate")

    con.execute("DROP TABLE IF EXISTS step_with_group")
    con.execute(
        """
        CREATE TABLE step_with_group AS
        SELECT
            s.*,
            g.eqp_id
        FROM step_candidate s
        LEFT JOIN eqp_group g
          ON s.line = g.line_id
         AND s.eqp_group_raw = g.eqp_group_name
        """
    )
    stats.validations["step_rows_after_eqp_group"] = query_table_count(con, "step_with_group")
    stats.validations["step_eqp_group_amplification"] = round(
        stats.validations["step_rows_after_eqp_group"] /
        max(stats.validations["step_candidate_rows_before_eqp_group"], 1), 4
    )

    con.execute("DROP TABLE IF EXISTS s")
    con.execute(
        """
        CREATE TABLE s AS
        SELECT DISTINCT
            s.line AS LINE,
            s.lot_id AS LOT_ID,
            s.proc_id AS PROC_ID,
            s.order_seq AS ORDER_SEQ,
            CAST(s.de_rank AS VARCHAR) AS DE_RANK,
            s."연속" AS "연속",
            s.AREA AS AREA,
            s.layer_id AS LAYER_ID,
            s.step_level AS STEP_LEVEL,
            s.ein AS EIN,
            s.step_seq AS STEP_SEQ,
            s.step_desc AS STEP_DESC,
            s.eqp_type AS EQP_TYPE,
            s.recipe_id AS RECIPE_ID,
            s.eqp_id AS EQP_ID,
            e.batch_kind AS BATCH_KIND,
            e.eqpline AS EQPLINE,
            e.eqp_status AS BODY_STATUS,
            e.eqp_status_change_time AS EQP_STATUS_CHANGE_TIME
        FROM step_with_group s
        LEFT JOIN equipment e
          ON s.line = e.line_id
         AND s.eqp_id = e.eqp_id
        """
    )
    stats.counts["s_rows"] = query_table_count(con, "s")


# =============================================================================
# f1/f2/f3 생성
# =============================================================================
def build_f1(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    con.execute("DROP TABLE IF EXISTS m_base")
    con.execute("CREATE TABLE m_base AS SELECT ROW_NUMBER() OVER () AS m_row_id, * FROM m")

    con.execute("DROP TABLE IF EXISTS ms_joined")
    con.execute(
        """
        CREATE TABLE ms_joined AS
        SELECT
            ROW_NUMBER() OVER () AS ms_row_id,
            m.m_row_id,
            m.LOT_INFORM, m.LINE, m.CUR_LINE_ID, m.SYS_LINE_ID, m.ORIGIN_LINE_ID,
            m.LOT_ID, m.CARR_ID, m.GRADE, m.LOT_TYPE, m.LOT_LEVEL, m.CUR_QTY,
            m.BAY_NAME, m.SENDFAB, m.START_DATE, m.LAST_EVENT_DATE, m.STEP_ARRIVE_DATE,
            m.STATUS, m.PROC_ID AS m_PROC_ID, m.ORDER_SEQ AS m_ORDER_SEQ, m.STEP_SEQ AS m_STEP_SEQ,
            s.PROC_ID, s.ORDER_SEQ, s.DE_RANK, s."연속", s.AREA, s.LAYER_ID, s.STEP_LEVEL,
            s.EIN, s.STEP_SEQ, s.STEP_DESC, s.EQP_TYPE, s.RECIPE_ID, s.EQP_ID,
            s.BATCH_KIND, s.EQPLINE, s.BODY_STATUS, s.EQP_STATUS_CHANGE_TIME,
            CASE WHEN m.ORDER_SEQ = s.ORDER_SEQ THEN '현스텝' END AS "현스텝"
        FROM m_base m
        LEFT JOIN s
          ON m.LINE = s.LINE
         AND m.LOT_ID = s.LOT_ID
        """
    )

    # exact/wildcard 원천을 만들되 최종 우선순위는 ROW_NUMBER로 제어한다.
    con.execute("DROP TABLE IF EXISTS t0")
    con.execute(
        """
        CREATE TABLE t0 AS
        SELECT * FROM t
        WHERE COALESCE(NULLIF(TRIM(PROCESS), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(STEP), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(PPID), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(EQPID), ''), '-') = '-'
        """
    )

    con.execute("DROP TABLE IF EXISTS t_matches")
    con.execute(
        """
        CREATE TABLE t_matches AS
        WITH candidates AS (
            SELECT
                ms.ms_row_id,
                t.*,
                0 AS priority
            FROM ms_joined ms
            INNER JOIN t
              ON ms.LINE = t.LINE
             AND (t.LOT_TYPE = '-' OR ms.LOT_TYPE = t.LOT_TYPE)
             AND ms.PROC_ID = t.PROCESS
             AND ms.STEP_SEQ = t.STEP
             AND ms.RECIPE_ID = t.PPID
             AND ms.EQP_ID = t.EQPID

            UNION ALL

            SELECT
                ms.ms_row_id,
                t0.*,
                1 AS priority
            FROM ms_joined ms
            INNER JOIN t0
              ON ms.LINE = t0.LINE
             AND (t0.LOT_TYPE = '-' OR ms.LOT_TYPE = t0.LOT_TYPE)
             AND (COALESCE(NULLIF(TRIM(t0.PROCESS), ''), '-') = '-' OR ms.PROC_ID = t0.PROCESS)
             AND (COALESCE(NULLIF(TRIM(t0.STEP), ''), '-') = '-' OR ms.STEP_SEQ = t0.STEP)
             AND (COALESCE(NULLIF(TRIM(t0.PPID), ''), '-') = '-' OR ms.RECIPE_ID = t0.PPID)
             AND (COALESCE(NULLIF(TRIM(t0.EQPID), ''), '-') = '-' OR ms.EQP_ID = t0.EQPID)
        )
        SELECT * EXCLUDE (priority, rn)
        FROM (
            SELECT
                c.*,
                ROW_NUMBER() OVER (
                    PARTITION BY ms_row_id, EQPCHAM, PREVENT, EQPISSUE, TIP_EVENTTIME, EQPISSUETIME
                    ORDER BY priority
                ) AS rn
            FROM candidates c
        ) x
        WHERE rn = 1
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_base")
    con.execute(
        """
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
            COALESCE(tm.EQPISSUETIME, ms.EQP_STATUS_CHANGE_TIME) AS EQPISSUETIME,
            COALESCE(ms.EQP_ID, tm.EQPID) AS eqpid,
            COALESCE(tm.EQPCHAM, ms.EQP_ID) AS eqpcham,
            CAST(NULL AS VARCHAR) AS hold,
            CAST(NULL AS VARCHAR) AS hold_reason,
            CAST(NULL AS VARCHAR) AS hold_date,
            CAST(NULL AS VARCHAR) AS exception,
            CAST(NULL AS VARCHAR) AS exception_reason,
            CAST(NULL AS VARCHAR) AS exception_date,
            CAST(NULL AS VARCHAR) AS ftp,
            CAST(NULL AS VARCHAR) AS ftp_reason,
            CAST(NULL AS VARCHAR) AS ftp_date,
            CASE
                WHEN tm.PREVENT = 'PREVENT'
                  OR tm.EQPISSUE IS NOT NULL
                  OR ms.BODY_STATUS IN ('LOCAL', 'PM', 'DOWN')
                THEN 'ISSUE'
            END AS issue_step
        FROM ms_joined ms
        LEFT JOIN t_matches tm ON ms.ms_row_id = tm.ms_row_id
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_counts")
    con.execute(
        """
        CREATE TABLE f1_counts AS
        SELECT LINE, LOT_ID, ORDER_SEQ,
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
        SELECT LINE, LOT_ID, ORDER_SEQ,
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
                WHEN fb."현스텝" = '현스텝' AND fb.STATUS = 'HOLD' THEN 'HOLD'
                WHEN fb.STATUS = 'WAIT' AND COALESCE(fc.path_count, 0) > 0
                     AND COALESCE(fc.issue_count, 0) >= COALESCE(fc.path_count, 0)
                THEN 'WAIT(진행불가)'
                WHEN fb."현스텝" IS DISTINCT FROM '현스텝' AND fb.STATUS IN ('HOLD', 'RUN') THEN 'WAIT'
                ELSE fb.STATUS
            END AS step_status,
            fg.eqpgroup,
            COALESCE(NULLIF(TRIM(fg.eqpgroup_cham_raw), ''), fg.eqpgroup) AS eqpgroup_cham
        FROM f1_base fb
        LEFT JOIN f1_counts fc
          ON fb.LINE = fc.LINE AND fb.LOT_ID = fc.LOT_ID AND fb.ORDER_SEQ = fc.ORDER_SEQ
        LEFT JOIN f1_groups fg
          ON fb.LINE = fg.LINE AND fb.LOT_ID = fg.LOT_ID AND fb.ORDER_SEQ = fg.ORDER_SEQ
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_current")
    con.execute(
        """
        CREATE TABLE f1_current AS
        SELECT
            LINE, LOT_ID,
            MAX(ORDER_SEQ) FILTER (WHERE "현스텝" = '현스텝') AS current_order_seq,
            MAX(DE_RANK) FILTER (WHERE "현스텝" = '현스텝') AS current_de_rank,
            MAX(NULLIF(TRIM("연속"), '')) FILTER (WHERE "현스텝" = '현스텝') AS current_continuous,
            CASE
                WHEN MAX(CASE WHEN "현스텝" = '현스텝' AND step_status = 'HOLD' THEN 1 ELSE 0 END) > 0 THEN 'HOLD'
                WHEN MAX(CASE WHEN "현스텝" = '현스텝' AND step_status = 'WAIT(진행불가)' THEN 1 ELSE 0 END) > 0 THEN 'WAIT(진행불가)'
                WHEN MAX(CASE WHEN "현스텝" = '현스텝' AND step_status = 'WAIT' THEN 1 ELSE 0 END) > 0 THEN 'WAIT'
                WHEN MAX(CASE WHEN "현스텝" = '현스텝' AND step_status = 'RUN' THEN 1 ELSE 0 END) > 0 THEN 'RUN'
                ELSE MAX(step_status) FILTER (WHERE "현스텝" = '현스텝')
            END AS current_step_status
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

    con.execute("DROP TABLE IF EXISTS f1_raw")
    con.execute(
        f"""
        CREATE TABLE f1_raw AS
        SELECT
            fsb.LOT_INFORM, fsb.LINE, fsb.CUR_LINE_ID, fsb.SYS_LINE_ID, fsb.ORIGIN_LINE_ID,
            fsb.LOT_ID, fsb.CARR_ID, fsb.GRADE, fsb.LOT_TYPE, fsb.LOT_LEVEL, fsb.CUR_QTY,
            fsb.BAY_NAME, fsb.SENDFAB,
            {elapsed_days_number_expr('fsb.START_DATE')} AS "투입경과_일",
            {elapsed_days_number_expr('fsb.LAST_EVENT_DATE')} AS "마지막이벤트경과_일",
            {elapsed_days_number_expr('fsb.STEP_ARRIVE_DATE')} AS "스텝도착경과_일",
            fsb.issue_step, fsb.issue_count, fsb.path_count, fsb."현스텝", fsb.step_status,
            CASE
                WHEN fc.current_step_status = 'WAIT'
                 AND fc.current_continuous IS NOT NULL
                 AND COALESCE(br.blocked_rows, 0) > 0
                THEN 'WAIT(진행불가)'
                ELSE fc.current_step_status
            END AS lot_status,
            fsb.PROC_ID, fsb.ORDER_SEQ, fsb.DE_RANK, fsb."연속", fsb.AREA,
            fsb.LAYER_ID, fsb.STEP_LEVEL, fsb.EIN, fsb.STEP_SEQ, fsb.STEP_DESC,
            fsb.EQP_TYPE, fsb.RECIPE_ID, fsb.BATCH_KIND, fsb.EQPLINE,
            fsb.eqpid, fsb.eqpcham, fsb.PREVENT, fsb.TYPE_BODY, fsb.TYPE_CHAM,
            fsb.TIP_EVENTTIME, fsb.eqpissue, fsb.body_eqp_status, fsb.CHAM_EQP_STATUS,
            fsb.EQPISSUETIME, fsb.eqpgroup, fsb.eqpgroup_cham,
            fsb.hold, fsb.hold_reason, fsb.hold_date,
            fsb.exception, fsb.exception_reason, fsb.exception_date,
            fsb.ftp, fsb.ftp_reason, fsb.ftp_date
        FROM f1_status_base fsb
        LEFT JOIN f1_current fc ON fsb.LINE = fc.LINE AND fsb.LOT_ID = fc.LOT_ID
        LEFT JOIN f1_blocked_rank br
          ON fc.LINE = br.LINE AND fc.LOT_ID = br.LOT_ID AND fc.current_de_rank = br.DE_RANK
        """
    )

    con.execute("DROP TABLE IF EXISTS f1_final")
    con.execute("CREATE TABLE f1_final AS SELECT DISTINCT * FROM f1_raw")
    con.execute("DROP VIEW IF EXISTS f1")
    con.execute("CREATE VIEW f1 AS SELECT * FROM f1_final")

    stats.counts["f1_rows_before_distinct"] = query_table_count(con, "f1_raw")
    stats.counts["f1_rows"] = query_table_count(con, "f1_final")
    stats.counts["f1_duplicate_rows_removed"] = (
        stats.counts["f1_rows_before_distinct"] - stats.counts["f1_rows"]
    )


def build_f2(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    con.execute("DROP TABLE IF EXISTS f2_tip_parts")
    con.execute(
        f"""
        CREATE TABLE f2_tip_parts AS
        SELECT DISTINCT
            LINE, LOT_ID, ORDER_SEQ,
            CASE
                WHEN TYPE_BODY = 'PREVENT' THEN eqpid
                WHEN TYPE_CHAM = 'PREVENT' THEN eqpcham
                ELSE COALESCE(eqpid, eqpcham)
            END AS eqp_name,
            {elapsed_days_text_expr('TIP_EVENTTIME')} AS elapsed_days_text
        FROM f1
        WHERE PREVENT = 'PREVENT'
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_tip_summary")
    con.execute(
        """
        CREATE TABLE f2_tip_summary AS
        SELECT LINE, LOT_ID, ORDER_SEQ,
               'PREVENT: ' || STRING_AGG(
                    DISTINCT eqp_name || COALESCE('(' || elapsed_days_text || ')', ''),
                    ', ' ORDER BY eqp_name || COALESCE('(' || elapsed_days_text || ')', '')
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
            LINE, LOT_ID, ORDER_SEQ,
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
            {elapsed_days_text_expr('EQPISSUETIME')} AS elapsed_days_text
        FROM f1
        WHERE eqpissue IS NOT NULL
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_down_grouped")
    con.execute(
        """
        CREATE TABLE f2_down_grouped AS
        SELECT LINE, LOT_ID, ORDER_SEQ, issue_group,
               issue_group || ': ' || STRING_AGG(
                    DISTINCT eqp_name || COALESCE('(' || elapsed_days_text || ')', ''),
                    ', ' ORDER BY eqp_name || COALESCE('(' || elapsed_days_text || ')', '')
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
        SELECT LINE, LOT_ID, ORDER_SEQ,
               STRING_AGG(
                    down_part, ' / '
                    ORDER BY CASE issue_group WHEN 'LOCAL' THEN 1 WHEN 'PM' THEN 2 WHEN 'DOWN' THEN 3 ELSE 4 END,
                             issue_group
               ) AS down
        FROM f2_down_grouped
        GROUP BY LINE, LOT_ID, ORDER_SEQ
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_eqpline_values")
    con.execute(
        """
        CREATE TABLE f2_eqpline_values AS
        SELECT DISTINCT
            LINE, LOT_ID, ORDER_SEQ,
            NULLIF(TRIM(CAST(EQPLINE AS VARCHAR)), '') AS eqpline_value
        FROM f1
        WHERE NULLIF(TRIM(CAST(EQPLINE AS VARCHAR)), '') IS NOT NULL
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_eqpline_summary")
    con.execute(
        """
        CREATE TABLE f2_eqpline_summary AS
        SELECT LINE, LOT_ID, ORDER_SEQ,
               STRING_AGG(
                   eqpline_value, ', '
                   ORDER BY CASE WHEN TRY_CAST(eqpline_value AS DOUBLE) IS NULL THEN 1 ELSE 0 END,
                            TRY_CAST(eqpline_value AS DOUBLE),
                            eqpline_value
               ) AS eqpline
        FROM f2_eqpline_values
        GROUP BY LINE, LOT_ID, ORDER_SEQ
        """
    )

    con.execute("DROP TABLE IF EXISTS f2_calc")
    con.execute(
        f"""
        CREATE TABLE f2_calc AS
        SELECT
            f.LOT_INFORM AS lot_inform,
            f.LINE AS line,
            f.CUR_LINE_ID AS "현재위치",
            f.SYS_LINE_ID AS "전산라인",
            f.ORIGIN_LINE_ID AS "투입라인",
            f.LOT_ID AS lot_id,
            f.CARR_ID AS carr_id,
            f.GRADE AS grade,
            f.LOT_TYPE AS lot_type,
            f.LOT_LEVEL AS lot_level,
            f.CUR_QTY AS qty,
            f.BAY_NAME AS bay,
            f.SENDFAB AS sendfab,
            f."투입경과_일", f."마지막이벤트경과_일", f."스텝도착경과_일",
            f.lot_status, f.step_status,
            f.PROC_ID AS proc_id, f.DE_RANK AS de_rank, f."연속", f.AREA,
            f.LAYER_ID AS layer_id, f."현스텝", f.ORDER_SEQ AS order_seq,
            f.STEP_SEQ AS step_seq, f.STEP_DESC AS step_desc, f.RECIPE_ID AS recipe_id,
            f.EQP_TYPE AS eqp_type, f.BATCH_KIND AS batch_kind,
            e.eqpline, f.eqpgroup,
            COALESCE(NULLIF(TRIM(f.eqpgroup_cham), ''), f.eqpgroup) AS eqpgroup_cham,
            t.tip, d.down,
            CAST(NULL AS DOUBLE) AS hold,
            CAST(NULL AS VARCHAR) AS hold_reason,
            CAST(NULL AS DOUBLE) AS exception,
            CAST(NULL AS VARCHAR) AS exception_reason,
            CAST(NULL AS DOUBLE) AS ftp,
            CAST(NULL AS VARCHAR) AS ftp_reason
        FROM f1 f
        LEFT JOIN f2_tip_summary t ON f.LINE = t.LINE AND f.LOT_ID = t.LOT_ID AND f.ORDER_SEQ = t.ORDER_SEQ
        LEFT JOIN f2_down_summary d ON f.LINE = d.LINE AND f.LOT_ID = d.LOT_ID AND f.ORDER_SEQ = d.ORDER_SEQ
        LEFT JOIN f2_eqpline_summary e ON f.LINE = e.LINE AND f.LOT_ID = e.LOT_ID AND f.ORDER_SEQ = e.ORDER_SEQ
        """
    )

    columns = prefixed_column_list(SUMMARY_OUTPUT_COLUMNS)
    con.execute("DROP TABLE IF EXISTS f2_final")
    con.execute(f"CREATE TABLE f2_final AS SELECT DISTINCT\n{columns}\nFROM f2_calc")
    con.execute("DROP TABLE IF EXISTS f2")
    con.execute(f"CREATE TABLE f2 AS {summary_export_query('f2_final')}")

    stats.counts["f2_rows_before_distinct"] = query_table_count(con, "f2_calc")
    stats.counts["f2_rows"] = query_table_count(con, "f2")
    stats.counts["f2_duplicate_rows_removed"] = (
        stats.counts["f2_rows_before_distinct"] - stats.counts["f2_rows"]
    )


def build_f3(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    con.execute("DROP TABLE IF EXISTS f3_current")
    con.execute(
        """
        CREATE TABLE f3_current AS
        SELECT line, lot_id,
               MAX(order_seq) FILTER (WHERE "현스텝" = '현스텝') AS current_order_seq,
               MAX(de_rank) FILTER (WHERE "현스텝" = '현스텝') AS current_de_rank,
               MAX(NULLIF(TRIM("연속"), '')) FILTER (WHERE "현스텝" = '현스텝') AS current_continuous
        FROM f2
        GROUP BY line, lot_id
        """
    )

    f2_cols = prefixed_column_list(SUMMARY_OUTPUT_COLUMNS, "f2")
    con.execute("DROP TABLE IF EXISTS f3_base")
    con.execute(
        f"""
        CREATE TABLE f3_base AS
        SELECT
{f2_cols}
        FROM f2
        LEFT JOIN f3_current fc ON f2.line = fc.line AND f2.lot_id = fc.lot_id
        WHERE f2."현스텝" = '현스텝'
           OR (fc.current_continuous IS NOT NULL AND f2.de_rank = fc.current_de_rank)
        """
    )

    columns = prefixed_column_list(SUMMARY_OUTPUT_COLUMNS)
    con.execute("DROP TABLE IF EXISTS f3_final")
    con.execute(f"CREATE TABLE f3_final AS SELECT DISTINCT\n{columns}\nFROM f3_base")
    con.execute("DROP TABLE IF EXISTS f3")
    con.execute(f"CREATE TABLE f3 AS {summary_export_query('f3_final')}")

    stats.counts["f3_rows_before_distinct"] = query_table_count(con, "f3_base")
    stats.counts["f3_rows"] = query_table_count(con, "f3")
    stats.counts["f3_duplicate_rows_removed"] = (
        stats.counts["f3_rows_before_distinct"] - stats.counts["f3_rows"]
    )


# =============================================================================
# 검증 / 저장
# =============================================================================
def validate_summary_columns(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    for table_name in ("f2", "f3"):
        cols = table_columns(con, table_name)
        stats.validations[f"{table_name}_columns"] = cols
        blocked = [c for c in cols if c.lower() in BLOCKED_SUMMARY_COLUMNS]
        stats.validations[f"{table_name}_blocked_columns"] = blocked
        if blocked:
            raise RuntimeError(f"{table_name}에 금지 컬럼이 남았습니다: {blocked}")


def collect_row_change_validations(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    stats.validations["lot_distinct"] = con.execute("SELECT COUNT(DISTINCT LOT_ID) FROM m").fetchone()[0]
    for table in ("f1", "f2", "f3"):
        lot_col = "LOT_ID" if table == "f1" else "lot_id"
        stats.validations[f"{table}_lot_distinct"] = con.execute(
            f"SELECT COUNT(DISTINCT {lot_col}) FROM {table}"
        ).fetchone()[0]

    stats.validations["f3_outside_scope_rows"] = con.execute(
        """
        SELECT COUNT(*)
        FROM f3
        LEFT JOIN f3_current fc ON f3.line = fc.line AND f3.lot_id = fc.lot_id
        WHERE NOT (
            f3."현스텝" = '현스텝'
            OR (fc.current_continuous IS NOT NULL AND f3.de_rank = fc.current_de_rank)
        )
        """
    ).fetchone()[0]

    # 증폭 원인 샘플
    stats.validations["eqp_group_large_groups"] = con.execute(
        """
        SELECT line_id, eqp_group_name, COUNT(DISTINCT eqp_id) AS eqp_count
        FROM eqp_group
        GROUP BY line_id, eqp_group_name
        HAVING COUNT(DISTINCT eqp_id) > 1
        ORDER BY eqp_count DESC
        LIMIT 20
        """
    ).fetchall()

    stats.validations["tip_multi_chamber_keys"] = con.execute(
        """
        SELECT line, process, step, ppid, eqpid, COUNT(*) AS chamber_rows
        FROM tip_cham
        GROUP BY line, process, step, ppid, eqpid
        HAVING COUNT(*) > 1
        ORDER BY chamber_rows DESC
        LIMIT 20
        """
    ).fetchall()


def export_parquet(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    paths = {
        "f1_parquet": PARQUET_DIR / "f1_result.parquet",
        "f2_parquet": PARQUET_DIR / "f2_result.parquet",
        "f3_parquet": PARQUET_DIR / "f3_result.parquet",
    }
    con.execute(
        f"""
        COPY (
            SELECT * FROM f1_final
            ORDER BY LINE, LOT_ID, TRY_CAST(ORDER_SEQ AS BIGINT) NULLS LAST, ORDER_SEQ, COALESCE(eqpcham, '')
        ) TO '{sql_text(paths['f1_parquet'])}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    con.execute(f"COPY ({summary_export_query('f2')}) TO '{sql_text(paths['f2_parquet'])}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.execute(f"COPY ({summary_export_query('f3')}) TO '{sql_text(paths['f3_parquet'])}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    stats.outputs.update({k: str(v) for k, v in paths.items()})


def relation_to_dataframe(con: duckdb.DuckDBPyConnection, query: str, limit: int, offset: int) -> pd.DataFrame:
    return con.execute(f"SELECT * FROM ({query}) q LIMIT {limit} OFFSET {offset}").fetchdf()


def export_excel(con: duckdb.DuckDBPyConnection, stats: RunStats) -> None:
    for name, query in (
        ("f2", summary_export_query("f2")),
        ("f3", summary_export_query("f3")),
    ):
        row_count = con.execute(f"SELECT COUNT(*) FROM ({query}) q").fetchone()[0]
        if row_count > EXCEL_CSV_FALLBACK_ROWS:
            stats.warnings.append(f"{name}: {row_count:,}행으로 Excel 저장 생략")
            continue
        path = EXCEL_DIR / f"{name}_result.xlsx"
        sheet_count = max(1, math.ceil(row_count / EXCEL_MAX_ROWS_PER_SHEET))
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for idx in range(sheet_count):
                offset = idx * EXCEL_MAX_ROWS_PER_SHEET
                limit = min(EXCEL_MAX_ROWS_PER_SHEET, row_count - offset) if row_count else 1
                df = relation_to_dataframe(con, query, limit, offset)
                sheet_name = name if sheet_count == 1 else f"{name}_{idx + 1:03d}"
                df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
                del df
                gc.collect()
        stats.outputs[f"{name}_excel"] = str(path)


def write_log(stats: RunStats) -> Path:
    path = LOG_DIR / f"build_multiwip_direct_{datetime.now():%Y%m%d_%H%M%S}.md"
    lines = [
        "# MultiWIP 직접 조회·전처리 실행 로그",
        "",
        "## 실행 구조",
        "- 중간 CSV 없음",
        "- 원천 테이블별 1회 조회",
        "- 대형 DataFrame 즉시 DuckDB 적재 후 해제",
        "- h 제외: hold/exception/ftp 컬럼은 NULL",
        "- AREA는 NULL",
        "",
        "## 주요 행 수",
    ]
    for key in sorted(stats.counts):
        lines.append(f"- {key}: {stats.counts[key]:,}")

    lines.extend(["", "## 행 수 차이 해석"])
    lines.append(
        "- step_candidate → step_with_group 증가는 하나의 설비그룹에 복수 EQP가 포함되어 path가 펼쳐지는 업무상 정상 증가일 수 있습니다."
    )
    lines.append(
        "- tip_main → tip_tee 증가는 본체 1개에 복수 챔버가 연결되어 생기는 정상 증가일 수 있습니다."
    )
    lines.append(
        "- f1/f2/f3 DISTINCT 감소는 최종 출력 컬럼이 완전히 동일한 행 제거입니다. 제거 건수가 크면 원천 최신시각 동률, 다대다 조인, 출력에서 빠진 이력 키를 확인해야 합니다."
    )
    lines.append(
        "- 기존 CSV 방식과 행 수가 다르면 아래 증폭률·중복키·샘플을 함께 비교하여 정상 감소/증가인지 판정해야 합니다."
    )

    lines.extend(["", "## 검증"])
    for key, value in sorted(stats.validations.items()):
        lines.append(f"- {key}: `{json.dumps(value, ensure_ascii=False, default=str)}`")

    lines.extend(["", "## 실행시간"])
    for key, value in stats.timings.items():
        lines.append(f"- {key}: {value:.3f}초")

    lines.extend(["", "## 출력"])
    for key, value in stats.outputs.items():
        lines.append(f"- {key}: `{value}`")

    if stats.warnings:
        lines.extend(["", "## 경고"])
        for warning in stats.warnings:
            lines.append(f"- {warning}")

    path.write_text("\n".join(lines), encoding="utf-8")
    stats.outputs["log"] = str(path)
    return path


# =============================================================================
# main
# =============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="7_holdwaitanal DB 직접 조회 기반 f1/f2/f3 생성")
    parser.add_argument("--export-excel", action="store_true", help="f2/f3 검증용 Excel 저장")
    parser.add_argument("--rebuild", action="store_true", help="기존 작업 DB 삭제 후 전체 재조회")
    parser.add_argument(
        "--smoke-test", action="store_true",
        help="대형 원천은 소량만 조회해 lot→tip/step→f1→f2→f3 전체 경로를 검증",
    )
    return parser.parse_args(argv)


def configure_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = '{sql_text(TEMP_DIR)}'")
    con.execute("SET preserve_insertion_order = false")

    threads = os.environ.get("HOLDWAITANAL_DUCKDB_THREADS")
    memory_limit = os.environ.get("HOLDWAITANAL_DUCKDB_MEMORY_LIMIT")
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if memory_limit:
        con.execute(f"SET memory_limit = '{sql_text(memory_limit)}'")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    ensure_dirs()
    db_path = SMOKE_DB_PATH if args.smoke_test else WORK_DB_PATH

    # smoke는 항상 새 작업 DB로 실행해 본 실행 DB와 섞이지 않게 한다.
    if args.smoke_test and db_path.exists():
        db_path.unlink()
    elif args.rebuild and db_path.exists():
        db_path.unlink()

    stats = RunStats()
    stats.validations["run_mode"] = "smoke_test" if args.smoke_test else "full"
    inspect_getdata_capabilities(stats)
    con = duckdb.connect(str(db_path))
    configure_duckdb(con)

    try:
        with timer(stats, "원천 조회 및 staging"):
            load_sources(con, stats, smoke_test=args.smoke_test)
        with timer(stats, "공통 dimension 생성"):
            build_dimensions(con, stats)
        with timer(stats, "Tip 전처리"):
            build_tip(con, stats)
        with timer(stats, "Step 전처리"):
            build_step(con, stats)
        with timer(stats, "f1 생성"):
            build_f1(con, stats)
        with timer(stats, "f2 생성"):
            build_f2(con, stats)
        with timer(stats, "f3 생성"):
            build_f3(con, stats)
        with timer(stats, "검증"):
            validate_summary_columns(con, stats)
            collect_row_change_validations(con, stats)

        if args.smoke_test:
            # smoke에서는 본 결과 Parquet/Excel을 덮어쓰지 않는다.
            print("[SMOKE PASS] f1/f2/f3 생성 및 검증 단계까지 완료")
        else:
            with timer(stats, "Parquet 저장"):
                export_parquet(con, stats)
            if args.export_excel:
                with timer(stats, "Excel 저장"):
                    export_excel(con, stats)

        with timer(stats, "로그 저장"):
            log_path = write_log(stats)
        print(f"[DONE] 작업 DB: {db_path}")
        print(f"[DONE] 로그: {log_path}")
    finally:
        con.close()
        gc.collect()


if __name__ == "__main__":
    main()
