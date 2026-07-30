# -*- coding: utf-8 -*-
"""
xx_impala.py
================================================================================
xx.txt 의 Oracle 쿼리를 Impala(SQL) 문법으로 변환한 모듈.

테이블 매핑 근거는 x.txt <매칭 테이블> 참조.
    smicdc_nrd.mc_lot     (a) -> MOS_KH_SMI.SMICDC_NRD_MC_LOT
    smicdc_p3nrd.mc_lot   (b) -> MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
    smicdc_nrdk.mc_lot    (c) -> MOS_KH_SMI.SMICDC_NRDK_MC_LOT
컬럼은 동일 컬럼명으로 매칭(대소문자 무시). Impala 식별자는 대소문자 구분이 없으므로
소문자로 통일해 작성했다.

x.txt 에 매핑이 명시되지 않은 아래 테이블들은 동일한 명명 규칙
(MOS_KH_SMI.<ORACLE_SCHEMA>_<TABLE>)을 그대로 적용해 추정 기재했다.
실제 이름이 다르면 TABLES 딕셔너리만 수정하면 된다.
    SMICDC_NRD.TODOPLAN             -> MOS_KH_SMI.SMICDC_NRD_TODOPLAN
    smicdc_nrd.steprule             -> MOS_KH_SMI.SMICDC_NRD_STEPRULE
    smicdc_nrd.stepcomments         -> MOS_KH_SMI.SMICDC_NRD_STEPCOMMENTS
    SMICDC_P3NRD.MC_LOT_STEP_COMMENT-> MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_COMMENT
    SMICDC_NRDK.MC_LOT_STEP_COMMENT -> MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_COMMENT
    MI_LOT_TRANSN_HIST_V            -> MOS_KH_SMI.MI_LOT_TRANSN_HIST_V

--------------------------------------------------------------------------------
Oracle -> Impala 주요 변환 내역
--------------------------------------------------------------------------------
 1) Oracle 전용 outer join 표기 `(+)` 는 Impala 미지원.
    -> ANSI `LEFT JOIN ... ON ...` 으로 전환.
      - m1  : m LEFT JOIN g   (lot_id + line_id 상수 조건)
      - m1  : m LEFT JOIN tp  (lot_id)
      - 최종: m1 LEFT JOIN co (line + lot_id)
 2) 콤마(,) 조인 + WHERE 조건 -> 명시적 JOIN/LEFT JOIN 구문으로 재작성.
 3) m0 의 `m.lot_id = c.lot_id(+)` 는 (+) 없는 조건 `m.last_event_date = c.m`
    가 함께 걸려 있어 Oracle에서도 사실상 inner join으로 동작한다.
    -> INNER JOIN 으로 명시.
 4) `select distinct ... max() over ()` 조합은 Impala에서
    "cannot combine SELECT DISTINCT with analytic functions" 오류.
    -> CTE c 를 `GROUP BY lot_id` 집계로 등가 재작성.
 5) TO_CHAR(number)  -> CAST(CAST(x AS BIGINT) AS STRING)
    (lot_level / order_seq / prirt_no 는 x.txt 기준 DOUBLE. STRING으로 바로
     캐스팅하면 '3.0' 형태가 되므로 BIGINT 경유로 Oracle 출력에 맞춤.)
 6) TO_DATE(str,'yyyymmdd hh24:mi:ss')
    -> TO_TIMESTAMP(str,'yyyyMMdd HH:mm:ss')   (원본 컬럼이 STRING 타입)
 7) NVL()  -> COALESCE()          (Impala nvl 도 되지만 표준 함수로 통일)
 8) `'G'||col` 문자열 연결 -> CONCAT('G', col)
 9) UNION ALL 브랜치 타입 정합성: KFR4 의 `NULL as lot_level`
    -> CAST(NULL AS STRING) as lot_level
10) 예약어/함수명과 겹치던 별칭 정리: `SUM` -> flowlevel_cnt, `M` -> max_*
11) 인라인 뷰의 `ORDER BY LOT_ID` 는 Impala에서 LIMIT 없이 사용 불가/무의미.
    -> 제거 (DISTINCT 결과 집합이라 의미 없음)
12) CTE `co` 는 최종 SELECT에서 lot_inform 만 사용하므로
    출력 컬럼을 (line, lot_id, lot_inform) 으로 정리. 결과는 등가.

[원본 로직 그대로 둔 부분 — 의도 확인 필요]
  - CTE t1 의 inner query 는 `WIP_ATTRIBUTE IN ('GRADE')` 로 이미 필터되어
    `SUM(CASE WHEN WIP_ATTRIBUTE='FLOWLEVEL' ...)` 는 항상 NULL 이다.
    따라서 `WHERE flowlevel_cnt IS NULL` 은 항상 참. 원본과 동일하게 유지했다.
"""

from __future__ import annotations

import argparse
import sys

__all__ = ["TABLES", "IMPALA_SQL", "get_query", "run_query"]


# ------------------------------------------------------------------------------
# 테이블 매핑 (필요 시 여기만 수정)
# ------------------------------------------------------------------------------
TABLES = {
    # x.txt 명시 매핑
    "nrd_mc_lot":    "MOS_KH_SMI.SMICDC_NRD_MC_LOT",      # a / KFR4
    "p3nrd_mc_lot":  "MOS_KH_SMI.SMICDC_P3NRD_MC_LOT",    # b / PFR1
    "nrdk_mc_lot":   "MOS_KH_SMI.SMICDC_NRDK_MC_LOT",     # c / KFR7
    # 명명 규칙으로 추정한 매핑
    "lot_transn":    "MOS_KH_SMI.MI_LOT_TRANSN_HIST_V",
    "nrd_todoplan":  "MOS_KH_SMI.SMICDC_NRD_TODOPLAN",
    "nrd_steprule":  "MOS_KH_SMI.SMICDC_NRD_STEPRULE",
    "nrd_stepcomm":  "MOS_KH_SMI.SMICDC_NRD_STEPCOMMENTS",
    "p3nrd_comment": "MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_COMMENT",
    "nrdk_comment":  "MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_COMMENT",
}

# Oracle 'yyyymmdd hh24:mi:ss' 의 Impala 대응 포맷
TS_FMT = "yyyyMMdd HH:mm:ss"


# ------------------------------------------------------------------------------
# 변환된 Impala 쿼리
# ------------------------------------------------------------------------------
_SQL_TEMPLATE = """
WITH

-- 3개 라인 mc_lot 통합 (라인 판별 및 최신 event 산출용)
m AS (
    SELECT 'KFR4' AS line, sys_line_id, cur_line_id, origin_line_id, lot_id, last_event_date
    FROM   {nrd_mc_lot}
    UNION ALL
    SELECT 'PFR1' AS line, sys_line_id, cur_line_id, origin_line_id, lot_id, last_event_date
    FROM   {p3nrd_mc_lot}
    UNION ALL
    SELECT 'KFR7' AS line, sys_line_id, cur_line_id, origin_line_id, lot_id, last_event_date
    FROM   {nrdk_mc_lot}
),

-- [변환] distinct + max() over  ->  group by 집계
c AS (
    SELECT lot_id,
           MAX(last_event_date) AS max_event_date
    FROM   m
    GROUP  BY lot_id
),

-- [변환] `m.lot_id = c.lot_id(+)` + 비(+) 조건 -> 사실상 inner join
m0 AS (
    SELECT m.line,
           m.lot_id,
           c.max_event_date
    FROM   m
    JOIN   c
      ON   m.lot_id = c.lot_id
    WHERE  m.last_event_date = c.max_event_date
),

-- LOT 별 최신 GRADE 속성 변경 이력
t1 AS (
    SELECT line_id,
           lot_id,
           new_attr_value
    FROM (
        SELECT line_id,
               lot_id,
               new_attr_value,
               lot_transn_time,
               MAX(lot_transn_time)
                   OVER (PARTITION BY lot_id, line_id)            AS max_transn_time,
               SUM(CASE WHEN wip_attribute = 'FLOWLEVEL' THEN 1 ELSE NULL END)
                   OVER (PARTITION BY lot_id, step_seq, line_id)  AS flowlevel_cnt
        FROM   {lot_transn}
        WHERE  lot_transn_type = 'ModifyAttr'
          AND  wip_attribute IN ('GRADE')
          AND  line_id IN ('PFR1', 'KFR7')
    ) h
    WHERE  flowlevel_cnt IS NULL
      AND  max_transn_time = lot_transn_time
),

-- [변환] 인라인 뷰 내 ORDER BY 제거
g AS (
    SELECT DISTINCT
           line_id,
           lot_id,
           new_attr_value AS grade
    FROM   t1
),

-- KFR4 전용: TODOPLAN 최대 PATHSEQ
tp_kfr4 AS (
    SELECT ml.lot_id,
           MAX(tp.pathseq) AS max_pathseq
    FROM (
        SELECT lot_id, step_seq
        FROM   {nrd_mc_lot}
        WHERE  lot_status_seg IN ('Hold', 'Active')
    ) ml
    JOIN   {nrd_todoplan} tp
      ON   ml.lot_id   = tp.lotid
     AND   ml.step_seq = tp.stepseq
    GROUP  BY ml.lot_id
),

m1 AS (
    -- ---------------------------------------------------------------- PFR1
    SELECT 'PFR1'                                                   AS line,
           m.cur_line_id,
           m.sys_line_id,
           m.origin_line_id,
           m.lot_id,
           m.carr_id,
           m.lot_type,
           CASE WHEN COALESCE(g.grade, '-') <> '-'
                THEN CONCAT('G', g.grade) END                       AS grade,
           CAST(CAST(m.lot_level AS BIGINT) AS STRING)              AS lot_level,
           m.cur_qty,
           m.bay_name,
           CASE WHEN m.lot_status_seg = 'Hold' THEN 'HOLD'
                ELSE m.step_status_seg END                          AS status,
           m.proc_id,
           CAST(CAST(m.order_seq AS BIGINT) AS STRING)              AS order_seq,
           m.step_seq,
           TO_TIMESTAMP(m.start_date,       '{ts_fmt}')             AS start_date,
           TO_TIMESTAMP(m.last_tkout_date,  '{ts_fmt}')             AS last_tkout_date,
           TO_TIMESTAMP(m.step_arrive_date, '{ts_fmt}')             AS step_arrive_date,
           TO_TIMESTAMP(m.last_event_date,  '{ts_fmt}')             AS last_event_date
    FROM        {p3nrd_mc_lot} m
    JOIN        m0
      ON        m.lot_id          = m0.lot_id
     AND        m.last_event_date = m0.max_event_date
    LEFT JOIN   g
      ON        m.lot_id  = g.lot_id
     AND        g.line_id = 'PFR1'
    WHERE       m.lot_status_seg IN ('Active', 'Hold')
      AND       m.order_seq IS NOT NULL

    UNION ALL

    -- ---------------------------------------------------------------- KFR4
    SELECT 'KFR4'                                                   AS line,
           m.cur_line_id,
           m.sys_line_id,
           m.origin_line_id,
           m.lot_id,
           m.carr_id,
           m.lot_type,
           CASE WHEN m.prirt_no IS NOT NULL
                THEN CONCAT('P', CAST(CAST(m.prirt_no AS BIGINT) AS STRING))
           END                                                      AS grade,
           CAST(NULL AS STRING)                                     AS lot_level,
           m.cur_qty,
           m.bay_name,
           CASE WHEN m.lot_status_seg = 'Hold' THEN 'HOLD'
                ELSE m.step_status_seg END                          AS status,
           m.proc_id,
           CAST(CAST(tp.max_pathseq AS BIGINT) AS STRING)           AS order_seq,
           m.step_seq,
           TO_TIMESTAMP(m.start_date,       '{ts_fmt}')             AS start_date,
           TO_TIMESTAMP(m.last_tkout_date,  '{ts_fmt}')             AS last_tkout_date,
           TO_TIMESTAMP(m.step_arrive_date, '{ts_fmt}')             AS step_arrive_date,
           TO_TIMESTAMP(m.last_event_date,  '{ts_fmt}')             AS last_event_date
    FROM        {nrd_mc_lot} m
    JOIN        m0
      ON        m.lot_id          = m0.lot_id
     AND        m.last_event_date = m0.max_event_date
    LEFT JOIN   tp_kfr4 tp
      ON        m.lot_id = tp.lot_id
    WHERE       m.lot_status_seg IN ('Active', 'Hold')

    UNION ALL

    -- ---------------------------------------------------------------- KFR7
    SELECT 'KFR7'                                                   AS line,
           m.cur_line_id,
           m.sys_line_id,
           m.origin_line_id,
           m.lot_id,
           m.carr_id,
           m.lot_type,
           CASE WHEN COALESCE(g.grade, '-') <> '-'
                THEN CONCAT('G', g.grade) END                       AS grade,
           CAST(CAST(m.lot_level AS BIGINT) AS STRING)              AS lot_level,
           m.cur_qty,
           m.bay_name,
           CASE WHEN m.lot_status_seg = 'Hold' THEN 'HOLD'
                ELSE m.step_status_seg END                          AS status,
           m.proc_id,
           CAST(CAST(m.order_seq AS BIGINT) AS STRING)              AS order_seq,
           m.step_seq,
           TO_TIMESTAMP(m.start_date,       '{ts_fmt}')             AS start_date,
           TO_TIMESTAMP(m.last_tkout_date,  '{ts_fmt}')             AS last_tkout_date,
           TO_TIMESTAMP(m.step_arrive_date, '{ts_fmt}')             AS step_arrive_date,
           TO_TIMESTAMP(m.last_event_date,  '{ts_fmt}')             AS last_event_date
    FROM        {nrdk_mc_lot} m
    JOIN        m0
      ON        m.lot_id          = m0.lot_id
     AND        m.last_event_date = m0.max_event_date
    LEFT JOIN   g
      ON        m.lot_id  = g.lot_id
     AND        g.line_id = 'KFR7'
    WHERE       m.lot_status_seg IN ('Active', 'Hold')
),

-- LOT 별 최신 코멘트
co AS (
    SELECT line, lot_id, lot_inform
    FROM (
        SELECT 'KFR4'                                               AS line,
               sr0.parentlotid                                      AS lot_id,
               sc0.comments                                         AS lot_inform,
               sc0.inserttime                                       AS cmt_time,
               MAX(sc0.inserttime)
                   OVER (PARTITION BY sr0.parentlotid)              AS max_cmt_time
        FROM (
            SELECT parentlotid, ruleseq
            FROM   {nrd_steprule}
        ) sr0
        JOIN (
            SELECT comments, fromseq, seq, inserttime
            FROM   {nrd_stepcomm}
            WHERE  commenttype = 'LOT'
        ) sc0
          ON sr0.ruleseq = sc0.fromseq
    ) k4
    WHERE  max_cmt_time = cmt_time

    UNION ALL

    SELECT line, lot_id, lot_inform
    FROM (
        SELECT 'PFR1'                                               AS line,
               lot_id,
               step_comment                                         AS lot_inform,
               update_date                                          AS cmt_time,
               MAX(update_date) OVER (PARTITION BY lot_id)          AS max_cmt_time
        FROM   {p3nrd_comment}
        WHERE  parent_order_seq = 0
          AND  comment_type = 'LOT'
    ) p1
    WHERE  max_cmt_time = cmt_time

    UNION ALL

    SELECT line, lot_id, lot_inform
    FROM (
        SELECT 'KFR7'                                               AS line,
               lot_id,
               step_comment                                         AS lot_inform,
               update_date                                          AS cmt_time,
               MAX(update_date) OVER (PARTITION BY lot_id)          AS max_cmt_time
        FROM   {nrdk_comment}
        WHERE  parent_order_seq = 0
          AND  comment_type = 'LOT'
    ) k7
    WHERE  max_cmt_time = cmt_time
)

SELECT DISTINCT
       m1.*,
       CASE
           WHEN m1.origin_line_id <> m1.sys_line_id THEN 'OLD_SEND'
           WHEN m1.origin_line_id  = m1.sys_line_id
            AND m1.sys_line_id    <> m1.cur_line_id THEN 'NEW_SEND'
       END                                                          AS sendfab,
       co.lot_inform
FROM        m1
LEFT JOIN   co
  ON        m1.line   = co.line
 AND        m1.lot_id = co.lot_id
WHERE       m1.line = m1.sys_line_id
  AND       m1.lot_type IN ('PP', 'PB', 'PG', 'TT')
  AND       m1.cur_line_id NOT IN ('CHTV')
"""

IMPALA_SQL = _SQL_TEMPLATE.format(ts_fmt=TS_FMT, **TABLES).strip()


# ------------------------------------------------------------------------------
# 헬퍼
# ------------------------------------------------------------------------------
def get_query(tables: dict | None = None, ts_fmt: str = TS_FMT) -> str:
    """테이블 매핑을 덮어써서 쿼리 문자열을 생성한다."""
    merged = dict(TABLES)
    if tables:
        merged.update(tables)
    return _SQL_TEMPLATE.format(ts_fmt=ts_fmt, **merged).strip()


def run_query(host: str, port: int = 21050, database: str = "default",
              sql: str | None = None, as_dataframe: bool = True, **conn_kwargs):
    """
    Impala 에 접속해 쿼리를 실행한다. impyla 필요:  pip install impyla

    as_dataframe=True 이면 pandas.DataFrame, 아니면 (columns, rows) 튜플 반환.
    """
    from impala.dbapi import connect  # type: ignore

    sql = sql or IMPALA_SQL
    with connect(host=host, port=port, database=database, **conn_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()

    if as_dataframe:
        import pandas as pd  # type: ignore
        return pd.DataFrame(rows, columns=columns)
    return columns, rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="xx.txt Oracle 쿼리의 Impala 변환본 출력/실행")
    p.add_argument("--print", dest="do_print", action="store_true",
                   help="변환된 Impala 쿼리를 표준출력으로 출력 (기본 동작)")
    p.add_argument("--out", metavar="PATH", help="쿼리를 .sql 파일로 저장")
    p.add_argument("--run", action="store_true", help="Impala 에 접속해 실행")
    p.add_argument("--host")
    p.add_argument("--port", type=int, default=21050)
    p.add_argument("--database", default="default")
    args = p.parse_args(argv)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(IMPALA_SQL + "\n")
        print(f"saved: {args.out}", file=sys.stderr)

    if args.run:
        if not args.host:
            p.error("--run 사용 시 --host 필요")
        df = run_query(host=args.host, port=args.port, database=args.database)
        print(df.to_string())
        return 0

    if args.do_print or not args.out:
        try:
            print(IMPALA_SQL)
        except BrokenPipeError:  # `| head` 등으로 파이프가 닫힌 경우
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
