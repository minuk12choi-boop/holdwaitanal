# -*- coding: utf-8 -*-
"""
build_f3.py — f3 직행 파이프라인

기존 refer_build_multiwip_f1_f2.py 는
    Oracle → Spotfire CSV export → DuckDB(f1 → f2 → f3)
구조였고, 이를 Impala 직접조회로 옮긴 v5 는 f1 에만 4,282초가 걸렸다.

원인은 조회도 조인도 아니라 **f3 가 쓰지도 않는 행을 1,800만 개 만든 뒤 버린 것**이다.
    f1 18,024,925 → f2 3,748,947 → f3 8,271

f3 의 범위 조건은
    현스텝(m.order_seq = s.order_seq)  OR  de_rank = 현스텝의 de_rank
뿐이고, 이 판정에 필요한 값(lot_id / order_seq / de_rank)은 **설비그룹 전개 이전**
StepPath 단계에서 이미 확정된다.

따라서 이 모듈은 StepPath 를 받자마자 f3 범위로 좁힌 뒤(약 8천 행) 설비그룹을
전개한다. 이후 f1 → f2 → f3 계산식은 참조 코드의 SQL 을 그대로 사용하므로
결과 동일성이 보장되며, 대상 행이 작아 수 초 안에 끝난다.

실행:
    python build_f3.py
"""

from __future__ import annotations

import datetime as dt
import os
from contextlib import contextmanager
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd

from get_data import (
    lot_query,
    kfr7_tip_query,
    pfr1_tip_query,
    eqp_query,
    eqp_group_query,
    kfr7_step_path_query,
    pfr1_step_path_query,
    build_tip,
    _build_equipment,
    _lower_cols,
    _drop_null_keys,
    _int_str,
    _excel_safe,
)

LINES = ("KFR7", "PFR1")

# Oracle `step_skip_yn <> 'Y'` 는 NULL 행을 제외한다(NULL <> 'Y' 는 UNKNOWN).
# 재현 구현들은 NULL 을 포함해 왔다. 원본과 맞추려면 True 로 둔다.
EXCLUDE_NULL_STEP_SKIP_YN = True

# hold 원천에서 제외할 status_seq. '2' = 조치완료 (기존 Oracle 쿼리 기준)
HOLD_EXCLUDE_STATUS_SEQ = ("2",)

HOLD_ITEM_TYPES = {
    "h1": ("HOLD LOT", "FUTUREHOLD"),
    "h2": ("EXCEPTION",),
    "h3": ("FTkinPvLot",),
}

SUMMARY_OUTPUT_COLUMNS = [
    "lot_inform", "line", "현재위치", "전산라인", "투입라인", "lot_id", "carr_id",
    "grade", "lot_type", "lot_level", "qty", "bay", "sendfab",
    "투입경과_일", "마지막이벤트경과_일", "스텝도착경과_일",
    "lot_status", "step_status", "proc_id", "de_rank", "연속", "AREA", "layer_id",
    "현스텝", "order_seq", "step_seq", "step_desc", "recipe_id", "eqp_type",
    "batch_kind", "eqpline", "eqpgroup", "eqpgroup_cham",
    "tip", "down", "hold", "hold_reason", "exception", "exception_reason",
    "ftp", "ftp_reason",
]

hold_query = """
SELECT line_id, item_type, status_seq, lot_id, step_seq,
       hold_user_name, issue_reason_cont, issue_date
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN ('KFR4', 'KFR7', 'PFR1')
"""


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
@contextmanager
def timer(label: str):
    t0 = perf_counter()
    print(f"[TIMER] {label} start", flush=True)
    yield
    print(f"[TIMER] {label} elapsed={perf_counter() - t0:.3f}s", flush=True)


def q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def col_list(alias: str | None = None, indent: str = "            ") -> str:
    prefix = f"{alias}." if alias else ""
    return ",\n".join(f"{indent}{prefix}{q(c)}" for c in SUMMARY_OUTPUT_COLUMNS)


def parsed_ts(column: str) -> str:
    """'오전/오후' 표기를 포함한 문자열을 TIMESTAMP 로. (참조 코드와 동일)"""
    norm = (
        "REPLACE(REPLACE(REGEXP_REPLACE(TRIM(CAST(" + column + " AS VARCHAR)), "
        "'(오전|오후) 0?0:', '\\1 12:'), '오전', 'AM'), '오후', 'PM')"
    )
    return (
        f"COALESCE(TRY_STRPTIME({norm}, '%Y-%m-%d %p %I:%M:%S'), "
        f"TRY_CAST({column} AS TIMESTAMP))"
    )


def elapsed_days_num(column: str) -> str:
    return f"ROUND((EPOCH(CURRENT_TIMESTAMP) - EPOCH({parsed_ts(column)})) / 86400.0, 1)"


def elapsed_days_text(column: str) -> str:
    return "FORMAT('{:.1f}', " + elapsed_days_num(column) + ") || '일↑'"


# ---------------------------------------------------------------------------
# 1. StepPath → f3 범위로 선축약  (이 파이프라인의 핵심)
# ---------------------------------------------------------------------------
def narrow_step_to_scope(df_path, df_lot, line):
    """StepPath 원천에서 f3 가 실제로 필요로 하는 행만 남긴다.

    남기는 행:
      1) 현스텝            : order_seq = 재공의 현재 order_seq
      2) 현 연속블록       : delay_step_type IN ('S','Y') 이고 de_rank = 현스텝 de_rank

    de_rank 는 lot 전체 경로의 S 누적개수이므로 현재 위치 이전 S/Y 행도 계산에
    필요하다. 그 행들은 계산에만 쓰고 결과에서는 버린다.
    """
    path = _lower_cols(df_path)
    path["order_seq"] = pd.to_numeric(path["order_seq"], errors="coerce")

    m = _lower_cols(df_lot)
    m = m[m["line"].eq(line)][["lot_id", "order_seq"]].copy()
    m["order_seq"] = pd.to_numeric(m["order_seq"], errors="coerce")
    m = m.dropna(subset=["order_seq"]).drop_duplicates()

    lot_ids = set(m["lot_id"].dropna())
    path = path[path["lot_id"].isin(lot_ids)]

    # --- de_rank : S/Y 행만으로 계산 (경로 전체 기준) ---
    sy = path[path["delay_step_type"].isin(["S", "Y"])][
        ["lot_id", "order_seq", "delay_step_type"]
    ].copy()
    sy = sy.sort_values(["lot_id", "order_seq"], kind="mergesort")
    sy["_s"] = sy["delay_step_type"].eq("S").astype(int)
    sy["de_rank"] = sy.groupby("lot_id", dropna=False)["_s"].cumsum()
    sy["de_rank"] = sy.groupby(["lot_id", "order_seq"], dropna=False)["de_rank"].transform("max")
    de = sy[["lot_id", "order_seq", "de_rank"]].drop_duplicates()

    # --- step_skip_yn 필터 (Oracle NULL 의미 반영) ---
    skip = path["step_skip_yn"]
    keep = skip.ne("Y") & (skip.notna() if EXCLUDE_NULL_STEP_SKIP_YN else True)
    path = path[keep]

    # --- 현스텝 확정 ---
    cur = path.merge(m.rename(columns={"order_seq": "_cur"}), on="lot_id", how="inner")
    cur = cur[cur["order_seq"].eq(cur["_cur"])].drop(columns=["_cur"])
    cur = cur.merge(de, on=["lot_id", "order_seq"], how="left")

    cur_rank = cur.loc[cur["de_rank"].notna(), ["lot_id", "de_rank"]].drop_duplicates()
    cur_rank = cur_rank.rename(columns={"de_rank": "_cur_rank"})

    # --- 현 연속블록 : 현스텝 이후이면서 de_rank 가 같은 S/Y 행 ---
    blk = path.merge(m.rename(columns={"order_seq": "_cur"}), on="lot_id", how="inner")
    blk = blk[blk["order_seq"] > blk["_cur"]].drop(columns=["_cur"])
    blk = blk.merge(de, on=["lot_id", "order_seq"], how="inner")
    blk = blk.merge(cur_rank, on="lot_id", how="inner")
    blk = blk[blk["de_rank"].eq(blk["_cur_rank"])].drop(columns=["_cur_rank"])

    scope = pd.concat([cur, blk], ignore_index=True).drop_duplicates(
        subset=["lot_id", "order_seq"]
    )

    delay_mins = pd.to_numeric(scope["delay_time_mins"], errors="coerce")
    cont = np.select(
        [scope["delay_step_type"].eq("S"), scope["delay_step_type"].eq("Y")],
        ["연속첫", "연속(" + _int_str(np.trunc(delay_mins)).fillna("") + ")"],
        default=None,
    )
    detail = scope["tkin_type_detail"].where(scope["tkin_type_detail"].ne("-"))

    return pd.DataFrame({
        "line": line,
        "lot_id": scope["lot_id"],
        "proc_id": scope["proc_id"],
        "order_seq": scope["order_seq"],
        "de_rank": scope["de_rank"],
        "연속": cont,
        "layer_id": scope["layer_id"],
        "step_level": _int_str(scope["step_level"]),
        "ein": detail.fillna(scope["ext_1st_vals"]),
        "step_seq": scope["step_seq"],
        "step_desc": scope["step_desc"],
        "eqp_type": scope["eqp_type"],
        "eqp_group_raw": scope["eqp_group_id"],
        "recipe_id": scope["recipe_id"],
    })


def expand_with_equipment(scope, df_eqp, df_eqp_group, line):
    """축약된 scope 에만 설비그룹·설비를 전개한다(전체 경로 전개 없음)."""
    eg = _lower_cols(df_eqp_group)
    eg = eg[eg["line_id"].eq(line) & ~eg["eqp_id"].str.contains("OFF", na=False)]
    eg = eg[["line_id", "eqp_group_name", "eqp_id"]].drop_duplicates()
    eg = _drop_null_keys(eg, ["line_id", "eqp_group_name"])

    out = scope.merge(
        eg, left_on=["line", "eqp_group_raw"], right_on=["line_id", "eqp_group_name"],
        how="left",
    ).drop(columns=["line_id", "eqp_group_name"])

    e = _build_equipment(df_eqp, line)
    e = _drop_null_keys(
        e[["eqp_id", "batch_kind", "eqpline", "eqp_status",
           "eqp_status_change_time"]].drop_duplicates(),
        ["eqp_id"],
    )
    out = out.merge(e, on="eqp_id", how="left")
    out = out.rename(columns={"eqp_status": "body_status"})
    out["AREA"] = pd.NA          # 참조 파이프라인과 동일하게 미제공
    return out


# ---------------------------------------------------------------------------
# 2. hold
# ---------------------------------------------------------------------------
def build_hold(df_hold):
    """FAB_ISSUE_LOT → h1/h2/h3.

    기존 Oracle: status_seq <> '2' 필터 후
      (line_id, lot_id, step_seq, item_type) 별 최신 hold_date 1건 →
      item_type 그룹별로 (line_id, lot_id, step_seq) 당 1건만 남김.
    """
    h = _lower_cols(df_hold)
    h = h[~h["status_seq"].isin(HOLD_EXCLUDE_STATUS_SEQ)]
    h = h.rename(columns={
        "hold_user_name": "hold_user",
        "issue_reason_cont": "hold_reason",
        "issue_date": "hold_date",
    })
    h = h[["line_id", "item_type", "lot_id", "step_seq",
           "hold_user", "hold_reason", "hold_date"]].drop_duplicates()
    h["hold_date"] = pd.to_datetime(h["hold_date"], errors="coerce")

    key = ["line_id", "lot_id", "step_seq", "item_type"]
    h["_max"] = h.groupby(key, dropna=False)["hold_date"].transform("max")
    h = h[h["hold_date"].eq(h["_max"])].drop(columns=["_max"])

    out = {}
    for name, types in HOLD_ITEM_TYPES.items():
        sub = h[h["item_type"].isin(types)]
        # Oracle 의 rownum + max(r) 는 (line_id, item_type, lot_id, step_seq)
        # 정렬 기준 마지막 1건을 고르는 것과 같다.
        sub = sub.sort_values(["line_id", "item_type", "lot_id", "step_seq"],
                              kind="mergesort")
        out[name] = sub.drop_duplicates(
            subset=["line_id", "lot_id", "step_seq"], keep="last"
        ).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# 3. f1 → f2 → f3  (참조 코드 계산식 그대로. 대상 행이 작아 즉시 완료)
# ---------------------------------------------------------------------------
def build_f3(con):
    con.execute("""
        CREATE OR REPLACE TABLE ms_joined AS
        SELECT
            ROW_NUMBER() OVER () AS ms_row_id,
            m.lot_inform, m.line, m.cur_line_id, m.sys_line_id, m.origin_line_id,
            m.lot_id, m.carr_id, m.grade, m.lot_type, m.lot_level, m.cur_qty,
            m.bay_name, m.sendfab,
            m.start_date, m.last_event_date, m.step_arrive_date,
            m.status, m.order_seq AS m_order_seq,
            s.proc_id, s.order_seq, s.de_rank, s."연속", s.AREA,
            s.layer_id, s.step_level, s.ein, s.step_seq, s.step_desc,
            s.eqp_type, s.recipe_id, s.eqp_id, s.batch_kind, s.eqpline,
            s.body_status, s.eqp_status_change_time AS s_eqp_status_change_time,
            CASE WHEN m.order_seq = s.order_seq THEN '현스텝' END AS "현스텝"
        FROM m
        LEFT JOIN s ON m.line = s.line AND m.lot_id = s.lot_id
    """)

    con.execute("""
        CREATE OR REPLACE TABLE t0 AS
        SELECT * FROM t
        WHERE COALESCE(NULLIF(TRIM(process), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(step), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(ppid), ''), '-') = '-'
           OR COALESCE(NULLIF(TRIM(eqpid), ''), '-') = '-'
    """)

    con.execute("""
        CREATE OR REPLACE TABLE t_matches_raw AS
        SELECT ms.ms_row_id, t.eqpid, t.eqpcham, t.prevent, t.type_body, t.type_cham,
               t.tip_eventtime, t.eqpissue, t.body_eqp_status, t.cham_eqp_status,
               t.eqpissuetime, '정확' AS match_type
        FROM ms_joined ms
        INNER JOIN t
          ON ms.line = t.line AND ms.lot_type = t.lot_type
         AND ms.proc_id = t.process AND ms.step_seq = t.step
         AND ms.recipe_id = t.ppid AND ms.eqp_id = t.eqpid
        UNION ALL
        SELECT ms.ms_row_id, t0.eqpid, t0.eqpcham, t0.prevent, t0.type_body, t0.type_cham,
               t0.tip_eventtime, t0.eqpissue, t0.body_eqp_status, t0.cham_eqp_status,
               t0.eqpissuetime, 'wildcard' AS match_type
        FROM ms_joined ms
        INNER JOIN t0
          ON ms.line = t0.line AND ms.lot_type = t0.lot_type
         AND (COALESCE(NULLIF(TRIM(t0.process), ''), '-') = '-' OR ms.proc_id = t0.process)
         AND (COALESCE(NULLIF(TRIM(t0.step), ''), '-') = '-' OR ms.step_seq = t0.step)
         AND (COALESCE(NULLIF(TRIM(t0.ppid), ''), '-') = '-' OR ms.recipe_id = t0.ppid)
         AND (COALESCE(NULLIF(TRIM(t0.eqpid), ''), '-') = '-' OR ms.eqp_id = t0.eqpid)
    """)

    con.execute("""
        CREATE OR REPLACE TABLE t_matches AS
        SELECT * EXCLUDE (rn) FROM (
            SELECT tmr.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY tmr.ms_row_id, tmr.eqpcham, tmr.prevent,
                                    tmr.eqpissue, tmr.tip_eventtime, tmr.eqpissuetime
                       ORDER BY CASE WHEN tmr.match_type = '정확' THEN 0 ELSE 1 END
                   ) AS rn
            FROM t_matches_raw tmr
        ) WHERE rn = 1
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE f1_base AS
        SELECT
            ms.*,
            tm.eqpcham, tm.prevent, tm.type_body, tm.type_cham, tm.tip_eventtime,
            COALESCE(tm.eqpissue,
                     CASE WHEN ms.body_status IN ('LOCAL','PM','DOWN')
                          THEN ms.body_status END)                    AS eqpissue,
            COALESCE(tm.body_eqp_status, ms.body_status)               AS body_eqp_status,
            tm.cham_eqp_status,
            COALESCE(tm.eqpissuetime, ms.s_eqp_status_change_time)     AS eqpissuetime,
            COALESCE(ms.eqp_id, tm.eqpid)                              AS eqpid,
            COALESCE(tm.eqpcham, ms.eqp_id)                            AS eqpcham2,
            h1.hold_user AS hold, h1.hold_reason AS hold_reason, h1.hold_date AS hold_date,
            h2.hold_user AS exception, h2.hold_reason AS exception_reason,
            h2.hold_date AS exception_date,
            h3.hold_user AS ftp, h3.hold_reason AS ftp_reason, h3.hold_date AS ftp_date,
            CASE WHEN tm.prevent = 'PREVENT' OR tm.eqpissue IS NOT NULL
                   OR h2.hold_user IS NOT NULL OR h3.hold_user IS NOT NULL
                   OR ms.body_status IN ('LOCAL','PM','DOWN')
                 THEN 'ISSUE' END                                      AS issue_step
        FROM ms_joined ms
        LEFT JOIN t_matches tm ON ms.ms_row_id = tm.ms_row_id
        LEFT JOIN h1 ON ms.line = h1.line_id AND ms.lot_id = h1.lot_id AND ms.step_seq = h1.step_seq
        LEFT JOIN h2 ON ms.line = h2.line_id AND ms.lot_id = h2.lot_id AND ms.step_seq = h2.step_seq
        LEFT JOIN h3 ON ms.line = h3.line_id AND ms.lot_id = h3.lot_id AND ms.step_seq = h3.step_seq
    """)
    con.execute('ALTER TABLE f1_base RENAME COLUMN eqpcham2 TO eqpcham_final')

    con.execute("""
        CREATE OR REPLACE TABLE f1_counts AS
        SELECT line, lot_id, order_seq,
               COUNT(DISTINCT eqpcham_final) AS path_count,
               COUNT(DISTINCT CASE WHEN issue_step IS NOT NULL THEN eqpcham_final END) AS issue_count
        FROM f1_base GROUP BY line, lot_id, order_seq
    """)

    con.execute("""
        CREATE OR REPLACE TABLE f1_groups AS
        SELECT line, lot_id, order_seq,
               STRING_AGG(DISTINCT eqpid, ', ' ORDER BY eqpid)
                   FILTER (WHERE eqpid IS NOT NULL) AS eqpgroup,
               STRING_AGG(DISTINCT eqpcham_final, ', ' ORDER BY eqpcham_final)
                   FILTER (WHERE eqpcham_final IS NOT NULL) AS eqpgroup_cham_raw
        FROM f1_base GROUP BY line, lot_id, order_seq
    """)

    con.execute("""
        CREATE OR REPLACE TABLE f1_status_base AS
        SELECT fb.*,
               COALESCE(fc.issue_count, 0) AS issue_count,
               COALESCE(fc.path_count, 0)  AS path_count,
               CASE
                   WHEN fb."현스텝" = '현스텝' AND fb.status = 'HOLD' THEN 'HOLD'
                   WHEN fb."현스텝" = '현스텝'
                    AND (fb.hold IS NOT NULL OR fb.exception IS NOT NULL OR fb.ftp IS NOT NULL)
                        THEN 'WAIT(진행불가)'
                   WHEN fb.status = 'WAIT' AND COALESCE(fc.path_count,0) > 0
                    AND COALESCE(fc.issue_count,0) > 0
                    AND COALESCE(fc.issue_count,0) >= COALESCE(fc.path_count,0)
                        THEN 'WAIT(진행불가)'
                   WHEN fb."현스텝" IS DISTINCT FROM '현스텝'
                    AND fb.status IN ('HOLD','RUN') THEN 'WAIT'
                   ELSE fb.status
               END AS step_status,
               CASE WHEN fb."현스텝" = '현스텝'
                     AND (fb.hold IS NOT NULL OR fb.exception IS NOT NULL
                          OR fb.ftp IS NOT NULL OR fb.status = 'HOLD')
                    THEN 1 ELSE 0 END AS current_exclusion_step_flag
        FROM f1_base fb
        LEFT JOIN f1_counts fc
          ON fb.line = fc.line AND fb.lot_id = fc.lot_id AND fb.order_seq = fc.order_seq
    """)

    con.execute("""
        CREATE OR REPLACE TABLE f1_current AS
        SELECT line, lot_id,
               MAX(current_exclusion_step_flag) AS current_exclusion_step_flag,
               MAX(de_rank)   FILTER (WHERE "현스텝" = '현스텝') AS current_de_rank,
               MAX(NULLIF(TRIM(CAST("연속" AS VARCHAR)), ''))
                   FILTER (WHERE "현스텝" = '현스텝')            AS current_continuous,
               CASE
                   WHEN MAX(CASE WHEN "현스텝"='현스텝' AND step_status='HOLD' THEN 1 ELSE 0 END) > 0 THEN 'HOLD'
                   WHEN MAX(CASE WHEN "현스텝"='현스텝' AND step_status='WAIT(진행불가)' THEN 1 ELSE 0 END) > 0 THEN 'WAIT(진행불가)'
                   WHEN MAX(CASE WHEN "현스텝"='현스텝' AND step_status='WAIT' THEN 1 ELSE 0 END) > 0 THEN 'WAIT'
                   WHEN MAX(CASE WHEN "현스텝"='현스텝' AND step_status='RUN'  THEN 1 ELSE 0 END) > 0 THEN 'RUN'
                   ELSE MAX(step_status) FILTER (WHERE "현스텝" = '현스텝')
               END AS current_step_status
        FROM f1_status_base GROUP BY line, lot_id
    """)

    con.execute("""
        CREATE OR REPLACE TABLE f1_blocked_rank AS
        SELECT line, lot_id, de_rank, COUNT(*) AS blocked_rows
        FROM f1_status_base WHERE step_status = 'WAIT(진행불가)'
        GROUP BY line, lot_id, de_rank
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE f1 AS
        SELECT
            fsb.* EXCLUDE (status),
            {elapsed_days_num('fsb.start_date')}       AS "투입경과_일",
            {elapsed_days_num('fsb.last_event_date')}  AS "마지막이벤트경과_일",
            {elapsed_days_num('fsb.step_arrive_date')} AS "스텝도착경과_일",
            fc.current_de_rank, fc.current_continuous,
            fg.eqpgroup,
            COALESCE(NULLIF(TRIM(CAST(fg.eqpgroup_cham_raw AS VARCHAR)), ''), fg.eqpgroup)
                AS eqpgroup_cham,
            CASE
                WHEN COALESCE(fc.current_exclusion_step_flag,0) > 0
                 AND fc.current_step_status = 'HOLD' THEN 'HOLD'
                WHEN COALESCE(fc.current_exclusion_step_flag,0) > 0 THEN 'WAIT(진행불가)'
                WHEN fc.current_step_status = 'WAIT'
                 AND fc.current_continuous IS NOT NULL
                 AND COALESCE(fbr.blocked_rows,0) > 0 THEN 'WAIT(진행불가)'
                ELSE fc.current_step_status
            END AS lot_status
        FROM f1_status_base fsb
        LEFT JOIN f1_current fc ON fsb.line = fc.line AND fsb.lot_id = fc.lot_id
        LEFT JOIN f1_groups  fg ON fsb.line = fg.line AND fsb.lot_id = fg.lot_id
                               AND fsb.order_seq = fg.order_seq
        LEFT JOIN f1_blocked_rank fbr ON fsb.line = fbr.line AND fsb.lot_id = fbr.lot_id
                               AND fsb.de_rank = fbr.de_rank
    """)

    # ---- tip / down / eqpline 요약 (step 단위) ----
    con.execute(f"""
        CREATE OR REPLACE TABLE tip_summary AS
        SELECT line, lot_id, order_seq,
               'PREVENT: ' || STRING_AGG(DISTINCT label, ', ' ORDER BY label) AS tip
        FROM (
            SELECT DISTINCT line, lot_id, order_seq,
                   (CASE WHEN type_body = 'PREVENT' THEN eqpid
                         WHEN type_cham = 'PREVENT' THEN eqpcham_final
                         ELSE COALESCE(eqpid, eqpcham_final) END)
                   || COALESCE('(' || {elapsed_days_text('tip_eventtime')} || ')', '') AS label
            FROM f1 WHERE prevent = 'PREVENT'
              AND COALESCE(CASE WHEN type_body='PREVENT' THEN eqpid
                                WHEN type_cham='PREVENT' THEN eqpcham_final
                                ELSE COALESCE(eqpid, eqpcham_final) END, '') <> ''
        ) GROUP BY line, lot_id, order_seq
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE down_summary AS
        SELECT line, lot_id, order_seq,
               STRING_AGG(down_part, ' / ' ORDER BY
                   CASE issue_group WHEN 'LOCAL' THEN 1 WHEN 'PM' THEN 2
                                    WHEN 'DOWN' THEN 3 ELSE 4 END, issue_group) AS down
        FROM (
            SELECT line, lot_id, order_seq, issue_group,
                   issue_group || ': ' || STRING_AGG(DISTINCT label, ', ' ORDER BY label) AS down_part
            FROM (
                SELECT DISTINCT line, lot_id, order_seq,
                       (CASE WHEN body_eqp_status IN ('LOCAL','DOWN','PM') THEN body_eqp_status
                             WHEN cham_eqp_status IN ('LOCAL','DOWN','PM') THEN cham_eqp_status
                             ELSE eqpissue END) AS issue_group,
                       (CASE WHEN body_eqp_status IN ('LOCAL','DOWN','PM') THEN eqpid
                             WHEN cham_eqp_status IN ('LOCAL','DOWN','PM') THEN eqpcham_final
                             ELSE COALESCE(eqpcham_final, eqpid) END)
                       || COALESCE('(' || {elapsed_days_text('eqpissuetime')} || ')', '') AS label
                FROM f1 WHERE eqpissue IS NOT NULL
            ) WHERE issue_group IS NOT NULL
            GROUP BY line, lot_id, order_seq, issue_group
        ) GROUP BY line, lot_id, order_seq
    """)

    con.execute("""
        CREATE OR REPLACE TABLE eqpline_summary AS
        SELECT line, lot_id, order_seq,
               STRING_AGG(v, ', ' ORDER BY
                   CASE WHEN TRY_CAST(v AS DOUBLE) IS NULL THEN 1 ELSE 0 END,
                   TRY_CAST(v AS DOUBLE), v) AS eqpline
        FROM (
            SELECT DISTINCT line, lot_id, order_seq,
                   NULLIF(TRIM(CAST(eqpline AS VARCHAR)), '') AS v
            FROM f1 WHERE NULLIF(TRIM(CAST(eqpline AS VARCHAR)), '') IS NOT NULL
        ) GROUP BY line, lot_id, order_seq
    """)

    # ---- f3 : 현스텝 + 현 연속블록만 ----
    con.execute(f"""
        CREATE OR REPLACE TABLE f3_calc AS
        SELECT
            f.lot_inform, f.line,
            f.cur_line_id AS "현재위치", f.sys_line_id AS "전산라인",
            f.origin_line_id AS "투입라인",
            f.lot_id, f.carr_id, f.grade, f.lot_type, f.lot_level,
            f.cur_qty AS qty, f.bay_name AS bay, f.sendfab,
            f."투입경과_일", f."마지막이벤트경과_일", f."스텝도착경과_일",
            f.lot_status, f.step_status, f.proc_id, f.de_rank, f."연속",
            f.AREA, f.layer_id, f."현스텝", f.order_seq, f.step_seq, f.step_desc,
            f.recipe_id, f.eqp_type, f.batch_kind,
            es.eqpline, f.eqpgroup, f.eqpgroup_cham,
            ts.tip, ds.down,
            CASE WHEN f.hold      IS NOT NULL THEN {elapsed_days_num('f.hold_date')}      END AS hold,
            f.hold_reason,
            CASE WHEN f.exception IS NOT NULL THEN {elapsed_days_num('f.exception_date')} END AS exception,
            f.exception_reason,
            CASE WHEN f.ftp       IS NOT NULL THEN {elapsed_days_num('f.ftp_date')}       END AS ftp,
            f.ftp_reason
        FROM f1 f
        LEFT JOIN tip_summary  ts ON f.line=ts.line AND f.lot_id=ts.lot_id AND f.order_seq=ts.order_seq
        LEFT JOIN down_summary ds ON f.line=ds.line AND f.lot_id=ds.lot_id AND f.order_seq=ds.order_seq
        LEFT JOIN eqpline_summary es ON f.line=es.line AND f.lot_id=es.lot_id AND f.order_seq=es.order_seq
        WHERE f."현스텝" = '현스텝'
           OR (f.current_continuous IS NOT NULL AND f.de_rank = f.current_de_rank)
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE f3 AS
        SELECT DISTINCT
{col_list('f3_calc')}
        FROM f3_calc
    """)
    return con.execute(f"""
        SELECT
{col_list()}
        FROM f3
        ORDER BY line, lot_id, TRY_CAST(order_seq AS BIGINT) NULLS LAST,
                 order_seq, eqpgroup_cham
    """).df()


# ---------------------------------------------------------------------------
def main():
    from bigdataquery import getData

    def fetch(name, sql):
        with timer(f"getData: {name}"):
            df = getData(param=sql, convert_type=True, verbose=True)
        print(f"[ROWS] {name} = {len(df):,}", flush=True)
        return df

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"

    with timer("소형 원천 조회"):
        df_lot = fetch("lot", lot_query)
        df_eqp = fetch("equipment", eqp_query)
        df_eqp_group = fetch("eqp_group", eqp_group_query)
        df_hold = fetch("hold", hold_query)

    # StepPath 는 라인별로 받아 즉시 f3 범위로 좁히고 원본을 해제한다.
    s_parts = []
    for line, sql in (("KFR7", kfr7_step_path_query), ("PFR1", pfr1_step_path_query)):
        df_path = fetch(f"{line}_step_path", sql)
        with timer(f"{line} f3 범위 축약"):
            scope = narrow_step_to_scope(df_path, df_lot, line)
        del df_path
        print(f"[ROWS] {line} scope(step) = {len(scope):,}", flush=True)
        with timer(f"{line} 설비그룹 전개"):
            s_parts.append(expand_with_equipment(scope, df_eqp, df_eqp_group, line))
    s = pd.concat(s_parts, ignore_index=True)
    print(f"[ROWS] s = {len(s):,}", flush=True)

    t_parts = []
    for line, sql in (("KFR7", kfr7_tip_query), ("PFR1", pfr1_tip_query)):
        df_tip = fetch(f"{line}_tip", sql)
        with timer(f"{line} tip 전처리"):
            t_parts.append(build_tip(df_tip, df_eqp, line))
        del df_tip
    t = pd.concat(t_parts, ignore_index=True)
    print(f"[ROWS] t = {len(t):,}", flush=True)

    with timer("hold 전처리"):
        holds = build_hold(df_hold)
    for k, v in holds.items():
        print(f"[ROWS] {k} = {len(v):,}", flush=True)

    con = duckdb.connect()
    m = _lower_cols(df_lot)
    con.register("m", m)
    con.register("s", s)
    con.register("t", t)
    for k, v in holds.items():
        con.register(k, v)

    with timer("f3 생성"):
        df_f3 = build_f3(con)
    print(f"[ROWS] f3 = {len(df_f3):,}", flush=True)

    with timer("저장"):
        path = os.path.join(os.getcwd(), f"f3_{stamp}.xlsx")
        _excel_safe(df_f3).to_excel(path, index=False)
    print(f"saved: {path} rows={len(df_f3):,}", flush=True)


if __name__ == "__main__":
    main()
