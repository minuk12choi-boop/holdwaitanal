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
import sys
from contextlib import contextmanager
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd


# TrackInPrevent 는 lot_type 을 갖지 않는다. 원본 Oracle 쿼리대로 전 행을
#   '-' (= 와일드카드, lot_type 을 따지지 않음) 로 둔다.
#   나중에 실제 lot_type 컬럼이 확인되면 그 컬럼명을 넣으면 된다.
TIP_LOT_TYPE_COLUMN = None


lot_query = """
WITH

m AS (
    SELECT 'PFR1' AS line, sys_line_id, cur_line_id, origin_line_id, lot_id, last_event_date
    FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
    UNION ALL
    SELECT 'KFR7' AS line, sys_line_id, cur_line_id, origin_line_id, lot_id, last_event_date
    FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT
),

c AS (
    SELECT lot_id,
           MAX(last_event_date) AS max_event_date
    FROM   m
    GROUP  BY lot_id
),

m0 AS (
    SELECT m.line,
           m.lot_id,
           c.max_event_date
    FROM   m
    JOIN   c
      ON   m.lot_id = c.lot_id
    WHERE  m.last_event_date = c.max_event_date
),

-- GRADE: 라인별 MC_LOT_ATTR 에서 직접 읽는다.
--   기존에는 FAB.M_LOT_TRANSN_HIST 의 최신 ModifyAttr 이력을 썼으나,
--   현재 값은 MC_LOT_ATTR 에 그대로 있으므로 이력 스캔이 불필요하다.
--   조인 기준: MC_LOT.object_id = MC_LOT_ATTR.parent_object_id
g_pfr1 AS (
    SELECT parent_object_id,
           attr_value AS grade
    FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_ATTR
    WHERE  attr_name = 'GRADE'
),

g_kfr7 AS (
    SELECT parent_object_id,
           attr_value AS grade
    FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT_ATTR
    WHERE  attr_name = 'GRADE'
),

m1 AS (
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
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.start_date, '[^0-9]', ''), 1, 14), 14, '0'),
                        'yyyyMMddHHmmss')    AS start_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.last_tkout_date, '[^0-9]', ''), 1, 14), 14, '0'),
                        'yyyyMMddHHmmss')    AS last_tkout_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.step_arrive_date, '[^0-9]', ''), 1, 14), 14, '0'),
                        'yyyyMMddHHmmss')    AS step_arrive_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.last_event_date, '[^0-9]', ''), 1, 14), 14, '0'),
                        'yyyyMMddHHmmss')    AS last_event_date,
           w.fa_object4
    FROM        MOS_KH_SMI.SMICDC_P3NRD_MC_LOT m
    JOIN        m0
      ON        m.lot_id          = m0.lot_id
     AND        m.last_event_date = m0.max_event_date
    LEFT JOIN   g_pfr1 g
      ON        m.object_id = g.parent_object_id
    LEFT JOIN   MOS_KH_SMI.SMICDC_P3NRD_MATERIALWORKSTATUS w
      ON        m.lot_id = w.lotid
    WHERE       m.lot_status_seg IN ('Active', 'Hold')
      AND       m.order_seq IS NOT NULL

    UNION ALL

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
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.start_date, '[^0-9]', ''), 1, 14), 14, '0'),
                        'yyyyMMddHHmmss')    AS start_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.last_tkout_date, '[^0-9]', ''), 1, 14), 14, '0'),
                        'yyyyMMddHHmmss')    AS last_tkout_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.step_arrive_date, '[^0-9]', ''), 1, 14), 14, '0'),
                        'yyyyMMddHHmmss')    AS step_arrive_date,
           TO_TIMESTAMP(RPAD(SUBSTR(REGEXP_REPLACE(m.last_event_date, '[^0-9]', ''), 1, 14), 14, '0'),
                        'yyyyMMddHHmmss')    AS last_event_date,
	w.fa_object4
    FROM        MOS_KH_SMI.SMICDC_NRDK_MC_LOT m
    JOIN        m0
      ON        m.lot_id          = m0.lot_id
     AND        m.last_event_date = m0.max_event_date
    LEFT JOIN   g_kfr7 g
      ON        m.object_id = g.parent_object_id
     LEFT JOIN MOS_KH_SMI.SMICDC_NRDK_MATERIALWORKSTATUS w
      ON        m.lot_id         = w.lotid
    WHERE       m.lot_status_seg IN ('Active', 'Hold')
),

co AS (
    SELECT line, lot_id, lot_inform
    FROM (
        SELECT 'PFR1'                                               AS line,
               lot_id,
               step_comment                                         AS lot_inform,
               update_date                                          AS cmt_time,
               MAX(update_date) OVER (PARTITION BY lot_id)          AS max_cmt_time
        FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_COMMENT
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
        FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_COMMENT
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
  AND       m1.lot_type IN ('PP', 'PG', 'EG')
  AND       m1.cur_line_id NOT IN ('CHTV')
"""

# 진단용. 위 세 조건을 빼고 통째로 받는다. 그래야 '어디서 빠졌는지' 를
# 셀 수 있다. 조건을 SQL 에 두면 걸러진 lot 은 애초에 오지 않아
# 비교 대상조차 되지 못한다.
lot_query_raw = lot_query.replace(
    """WHERE       m1.line = m1.sys_line_id
  AND       m1.lot_type IN ('PP', 'PG', 'EG')
  AND       m1.cur_line_id NOT IN ('CHTV')""", "")

# ── TrackInPrevent (Tip) : 실제 사용 컬럼만 조회 ─────────────────────────
kfr7_tip_query = """
SELECT process, step, ppid, eqpid, chamberid, {LOT_TYPE_COL}
       type, checkcount, tkin_count, updated, eventtime
FROM   MOS_KH_SMI.SMICDC_NRDK_TRACKINPREVENT
WHERE  owner IN ('LEVEL1', 'PHOTO_LEVEL1')
"""

pfr1_tip_query = """
SELECT process, step, ppid, eqpid, chamberid, {LOT_TYPE_COL}
       type, checkcount, tkin_count, updated, eventtime
FROM   MOS_KH_SMI.SMICDC_P3NRD_TRACKINPREVENT
WHERE  owner IN ('LEVEL1', 'PHOTO_LEVEL1')
"""

# ── Equipment : line_id + eqp_id 별 최신 적재분만 ────────────────────────
_LOT_TYPE_SEL = f"{TIP_LOT_TYPE_COLUMN}," if TIP_LOT_TYPE_COLUMN else ""
kfr7_tip_query = kfr7_tip_query.replace("{LOT_TYPE_COL}", _LOT_TYPE_SEL)
pfr1_tip_query = pfr1_tip_query.replace("{LOT_TYPE_COL}", _LOT_TYPE_SEL)

eqp_query = """
SELECT line_id, origin_line_id, batch_kind, eqp_id,
       eqp_status, tool_kind, eqp_status_change_time
FROM (
    SELECT e.line_id, e.origin_line_id, e.batch_kind, e.eqp_id,
           e.eqp_status, e.tool_kind, e.eqp_status_change_time,
           e.impala_insert_time,
           MAX(e.impala_insert_time)
               OVER (PARTITION BY e.line_id, e.eqp_id) AS max_impala_insert_time
    FROM   MOS_KH_SMI.SMIMES_MI_EQUIPMENT e
    WHERE  e.line_id IN ('KFR7', 'PFR1')
) x
WHERE x.impala_insert_time = x.max_impala_insert_time
"""

# ── 설비그룹 : line_id + eqp_group_name 별 최신 스냅샷만 ─────────────────
#   파티션 키에 eqp_id 를 넣으면 안 된다. 그렇게 하면 그룹에서 이미 빠진 설비도
#   '자기 자신의 최신 행'으로 살아남아, 옛 구성원과 현 구성원이 섞인다.
#   (실측 예: WSH403_WSH405_WSO411_WSO414 그룹에 6/27 자 MMC404/MMC407 이 잔존)
#   그룹 단위로 최신 적재시각을 잡아야 그 시점의 구성원만 남는다.
eqp_group_query = """
SELECT line_id, eqp_group_name, eqp_id
FROM (
    SELECT g.line_id, g.eqp_group_name, g.eqp_id, g.impala_insert_time,
           MAX(g.impala_insert_time)
               OVER (PARTITION BY g.line_id, g.eqp_group_name)
               AS max_impala_insert_time
    FROM   MOS_KH_SMI.SMIMES_MI_EQP_GROUP_LIST g
    WHERE  g.line_id IN ('KFR7', 'PFR1')
) x
WHERE x.impala_insert_time = x.max_impala_insert_time
"""

# ── Step Path : 실제 사용 컬럼만 조회 (조건절 없이 생테이블) ─────────────
kfr7_step_path_query = """
SELECT p.lot_id, p.order_seq, p.proc_id, p.step_seq, p.step_desc, p.step_level,
       p.step_skip_yn, p.delay_step_type, p.delay_time_mins, p.layer_id,
       p.eqp_type, p.eqp_group_id, p.recipe_id, p.ext_1st_vals, p.tkin_type_detail
FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_PATH p
JOIN   (SELECT lot_id, order_seq FROM MOS_KH_SMI.SMICDC_NRDK_MC_LOT
        WHERE lot_status_seg IN ('Active', 'Hold')) c
  ON   p.lot_id = c.lot_id
WHERE  p.order_seq >= c.order_seq
    OR p.delay_step_type IN ('S', 'Y')
"""

pfr1_step_path_query = """
SELECT p.lot_id, p.order_seq, p.proc_id, p.step_seq, p.step_desc, p.step_level,
       p.step_skip_yn, p.delay_step_type, p.delay_time_mins, p.layer_id,
       p.eqp_type, p.eqp_group_id, p.recipe_id, p.ext_1st_vals, p.tkin_type_detail
FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_PATH p
JOIN   (SELECT lot_id, order_seq FROM MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
        WHERE lot_status_seg IN ('Active', 'Hold')) c
  ON   p.lot_id = c.lot_id
WHERE  p.order_seq >= c.order_seq
    OR p.delay_step_type IN ('S', 'Y')
"""

# =====================================================================
# Tip 전처리 (기존 Oracle tip_table_pfr1 / tip_table_kfr7 의 t~final 로직을
# pandas 로 재현. 사내 15분 호출제한 회피를 위해 SQL 은 생테이블만 조회하고
# 결합/연산은 python 단에서 수행한다.)
# =====================================================================
BATCH_KINDS = ('BATCH_FURNACE', 'BATCH_WET')
EQP_ISSUE_STATUS = ('LOCAL', 'PM', 'DOWN')


def _cache_path(name, biz_date):
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}_{biz_date:%Y%m%d}.csv")


def cached_daily(name, biz_date, producer):
    """업무일(22시 기준) 단위 캐시.

    설비그룹처럼 하루에 한 번만 바뀌면 충분한 테이블용. 같은 업무일 안에서는
    파일에서 읽고, 날짜가 바뀌면 새로 조회한다. 오래된 캐시는 지운다.
    """
    path = _cache_path(name, biz_date)
    if os.path.exists(path):
        df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
        print(f"[CACHE] {name} 재사용 ({os.path.basename(path)}) rows={len(df):,}",
              flush=True)
        return df, True

    df = producer()
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[CACHE] {name} 새로 저장 ({os.path.basename(path)}) rows={len(df):,}",
          flush=True)

    d = os.path.dirname(path)
    for f in os.listdir(d):
        if f.startswith(name + "_") and f != os.path.basename(path):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    return df, False


def _lower_cols(df):
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    return out


def _to_datetime(series):
    """문자열이든 datetime 이든 datetime 으로. (db_common.to_datetime 위임)

    Oracle 쿼리에서 TO_DATE 로 변환해 주는 컬럼이 늘어나고 있어, 문자열만
    가정하면 깨진다. 두 경우를 모두 받는다.
    """
    import db_common as _DB
    return _DB.to_datetime(series)


def expand_group_name(name):  # noqa: C901
    """설비그룹명에 축약 표기된 구성설비를 복원한다.

    'MEB405_413'                        -> {MEB405, MEB413}
    'MMC403_404_406_407_408'            -> {MMC403, MMC404, MMC406, MMC407, MMC408}
    'MOLP704_731_MOVP321_MOVP323_MOLP722'
        -> {MOLP704, MOLP731, MOVP321, MOVP323, MOLP722}
    숫자만 있는 토큰은 직전에 나온 알파벳 접두어를 이어받는다.
    """
    out, prefix, last_num = set(), "", ""
    for tok in str(name).split("_"):
        if not tok:
            continue
        head = "".join(c for c in tok if not c.isdigit())
        if head:
            prefix = head
            last_num = "".join(c for c in tok if c.isdigit())
            out.add(tok)
        elif prefix:
            out.add(prefix + tok)
            # 'DTDD707_8' 처럼 뒷자리만 줄여 쓴 경우: 직전 번호의 끝자리를 치환
            if last_num and len(tok) < len(last_num):
                out.add(prefix + last_num[:len(last_num) - len(tok)] + tok)
        else:
            out.add(tok)
    return out


def _drop_null_keys(df, keys):
    """Oracle 의 '=' 는 NULL 과 매칭되지 않으므로, 우측 테이블에서 키가 NULL 인
    행을 제거해 pandas merge(NaN==NaN 매칭)와의 차이를 없앤다."""
    mask = pd.Series(True, index=df.index)
    for k in keys:
        mask &= df[k].notna()
    return df[mask]


def _build_equipment(df_eqp, line):
    """e CTE: line/tool_kind 필터 + 상태변경시각 형변환."""
    e = _lower_cols(df_eqp)
    e = e[e['line_id'].eq(line) & e['tool_kind'].isin(['EQP', 'CHAMBER'])]
    e = e[['line_id', 'origin_line_id', 'batch_kind', 'eqp_id',
           'eqp_status', 'tool_kind', 'eqp_status_change_time']].copy()
    e = e.rename(columns={'origin_line_id': 'eqpline'})
    e['eqp_status_change_time'] = _to_datetime(e['eqp_status_change_time'])
    return e.drop_duplicates()


def _tip_lot_type(t):
    """TrackInPrevent 의 lot_type. 컬럼이 없으면 '-'(와일드카드)로 폴백한다."""
    col = TIP_LOT_TYPE_COLUMN
    if col and col in t.columns:
        return t[col].astype('string').fillna('-').replace('', '-')
    if col:
        print(f"[WARN] TrackInPrevent 에 '{col}' 컬럼이 없어 lot_type 을 "
              f"'-'(와일드카드)로 처리합니다. TIP_LOT_TYPE_COLUMN 확인 필요.", flush=True)
    return '-'


def prefilter_tip(df_tip, s_scope):
    """s(f3 범위) 와 매칭될 가능성이 없는 TrackInPrevent 행을 미리 버린다.

    rule 이 매칭되려면 process / step / ppid / eqpid 각각이 '-'(와일드카드)이거나
    s 에 존재하는 값이어야 한다. 이는 필요조건이므로 여기서 버려도 매칭 손실이 없다.
    (조합까지 보지는 않으므로 남는 행이 모두 매칭된다는 뜻은 아니다)

    필터 기준 컬럼(process, step, ppid, eqpid)은 이후 _build_ttt / _build_tee 의
    파티션·조인 키를 모두 결정하므로, 같은 파티션의 행은 통째로 남거나 통째로
    사라진다. 즉 윈도우 연산 결과가 왜곡되지 않는다.
    """
    t = _lower_cols(df_tip)
    mask = np.ones(len(t), dtype=bool)
    for col, scol in (("process", "proc_id"), ("step", "step_seq"),
                      ("ppid", "recipe_id"), ("eqpid", "eqp_id")):
        allowed = pd.Index(pd.unique(s_scope[scol].dropna()))
        v = t[col]
        mask &= (v.isna() | v.isin(["-", ""]) | v.isin(allowed)).to_numpy()
    return t[mask]


def _build_t(df_tip, line):
    """t CTE: prevent 판정 / ee(설비-챔버) 조합 / eventtime 보정."""
    t = _lower_cols(df_tip)

    raw_cham = t['chamberid']
    typ = t['type']
    chk = pd.to_numeric(t['checkcount'], errors='coerce').fillna(0)
    tkin = pd.to_numeric(t['tkin_count'], errors='coerce').fillna(0)

    prevent = np.select(
        [typ.eq('DOING'),
         typ.eq('PREVENT') & chk.eq(0),
         typ.eq('PREVENT') & tkin.ge(chk),
         typ.eq('PREVENT') & tkin.lt(chk)],
        ['DOING', 'PREVENT', 'PREVENT', 'DOING'],
        default=None,
    )

    has_cham = raw_cham.notna() & ~raw_cham.isin(['MAIN', '-'])
    ee = np.where(has_cham,
                  t['eqpid'].astype('string') + '-' + raw_cham.astype('string'),
                  t['eqpid'].astype('string'))

    out = pd.DataFrame({
        'line': line,
        'process': t['process'],
        'step': t['step'],
        'ppid': t['ppid'],
        'eqpid': t['eqpid'],
        'lot_type': _tip_lot_type(t),
        'chamberid': raw_cham.where(~raw_cham.isin(['-', 'MAIN']), 'MAIN'),
        'prevent': pd.Series(prevent, index=t.index, dtype='object'),
        'ee': pd.Series(ee, index=t.index, dtype='object'),
        'eventtime': t['eventtime'].fillna(t['updated']),
    })
    return out.drop_duplicates().reset_index(drop=True)


def _build_ttt(t):
    """ttt CTE: (line,process,step,ppid,ee,lot_type) 그룹 내 DOING 우선 + 최신건만."""
    grp = ['line', 'process', 'step', 'ppid', 'ee', 'lot_type']
    df = t.copy()
    df['_doing'] = df['prevent'].eq('DOING').astype(int)

    g = df.groupby(grp, dropna=False)
    df['c'] = g['_doing'].transform('sum')
    df['cc'] = g['ee'].transform('count')
    df['m'] = df.groupby(grp + ['prevent'], dropna=False)['eventtime'].transform('max')

    keep = (
        (df['cc'].gt(1) & df['c'].gt(0) & df['prevent'].eq('DOING') & df['m'].eq(df['eventtime']))
        | (df['cc'].gt(1) & df['c'].eq(0) & df['m'].eq(df['eventtime']))
        | df['cc'].eq(1)
    )
    return df[keep].drop(columns=['_doing', 'c', 'cc', 'm']).reset_index(drop=True)


def _build_tee(ttt, e):
    """te(설비정보 결합) → tee(본체 a + 챔버 b 결합)."""
    e_dim = _drop_null_keys(
        e[['line_id', 'eqp_id', 'batch_kind', 'eqpline']].drop_duplicates(),
        ['line_id', 'eqp_id'],
    )
    te = ttt.merge(e_dim, left_on=['line', 'ee'], right_on=['line_id', 'eqp_id'],
                   how='left').drop(columns=['line_id', 'eqp_id'])

    is_main = te['chamberid'].fillna('-').isin(['MAIN', '-'])
    a = te[is_main].copy()
    b = te[~is_main].copy()

    keys = ['line', 'process', 'step', 'ppid', 'eqpid']
    b = _drop_null_keys(b, keys)
    m = a.merge(b, on=keys, how='left', suffixes=('_a', '_b'))

    is_batch = m['batch_kind_a'].fillna('-').isin(BATCH_KINDS)

    eqpcham = m['ee_b'].mask(is_batch, m['eqpid']).fillna(m['eqpid'])
    eventtime = m['eventtime_b'].mask(is_batch, m['eventtime_a']).fillna(m['eventtime_a'])

    return pd.DataFrame({
        'line': m['line'],
        'process': m['process'],
        'step': m['step'],
        'ppid': m['ppid'],
        'eqpid': m['eqpid'],
        'chamberid': m['chamberid_b'].mask(is_batch, np.nan),
        'eqpcham': eqpcham,
        'lot_type': m['lot_type_b'].fillna(m['lot_type_a']),
        'batch_kind': m['batch_kind_a'],
        'prevent': np.where(m['prevent_a'].eq('PREVENT') | m['prevent_b'].eq('PREVENT'),
                            'PREVENT', 'DOING'),
        'type_body': m['prevent_a'],
        'type_cham': m['prevent_b'],
        'eventtime': eventtime,
        'eqpline': m['eqpline_a'].fillna(m['eqpline_b']),
    })


def _build_es(e):
    """es CTE: 본체(EQP) 기준으로 챔버(CHAMBER) 상태를 붙인다."""
    a = e[e['tool_kind'].eq('EQP')][
        ['line_id', 'eqp_id', 'eqp_status', 'eqp_status_change_time']].copy()
    b = e[e['tool_kind'].eq('CHAMBER')][
        ['line_id', 'eqp_id', 'eqp_status', 'eqp_status_change_time']].copy()

    eid = b['eqp_id'].astype('string')
    # Oracle: nvl(substr(id,1,instr(id,'-')-1), substr(id,1,instr(id,'_')-1))
    body = eid.str.split('-').str[0].where(eid.str.contains('-', na=False))
    body = body.fillna(eid.str.split('_').str[0].where(eid.str.contains('_', na=False)))
    b['body'] = body
    b = _drop_null_keys(b, ['line_id', 'body'])

    m = a.merge(b, left_on=['line_id', 'eqp_id'], right_on=['line_id', 'body'],
                how='left', suffixes=('_body', '_cham'))

    out = pd.DataFrame({
        'line_id': m['line_id'],
        'eqpcham': m['eqp_id_cham'].fillna(m['eqp_id_body']),
        'body_eqp_status': m['eqp_status_body'],
        'cham_eqp_status': m['eqp_status_cham'],
        'body_status_change_time': m['eqp_status_change_time_body'],
        'cham_status_change_time': m['eqp_status_change_time_cham'],
    })
    return out.drop_duplicates()


def build_tip(df_tip, df_eqp, line):
    """생테이블 2종(TrackInPrevent, Equipment) → 기존 tip 결과 포맷."""
    e = _build_equipment(df_eqp, line)
    t = _build_t(df_tip, line)
    ttt = _build_ttt(t)
    tee = _build_tee(ttt, e)
    es = _drop_null_keys(_build_es(e), ['line_id', 'eqpcham'])

    f = tee.merge(es, left_on=['line', 'eqpcham'], right_on=['line_id', 'eqpcham'],
                  how='left').drop(columns=['line_id'])

    body_issue = f['body_eqp_status'].isin(EQP_ISSUE_STATUS)
    cham_issue = f['cham_eqp_status'].isin(EQP_ISSUE_STATUS)

    out = pd.DataFrame({
        'line': f['line'],
        'process': f['process'],
        'step': f['step'],
        'ppid': f['ppid'],
        'eqpid': f['eqpid'],
        'eqpcham': f['eqpcham'],
        'chamberid': f['chamberid'],
        'lot_type': f['lot_type'],
        'batch_kind': f['batch_kind'],
        'prevent': f['prevent'],
        'type_body': f['type_body'],
        'type_cham': f['type_cham'],
        'tip_eventtime': f['eventtime'].where(f['prevent'].eq('PREVENT')),
        'eqpissue': np.select([body_issue, cham_issue],
                              [f['body_eqp_status'], f['cham_eqp_status']], default=None),
        'body_eqp_status': f['body_eqp_status'],
        'cham_eqp_status': f['cham_eqp_status'],
        'eqpissuetime': np.select([body_issue, cham_issue],
                                  [f['body_status_change_time'], f['cham_status_change_time']],
                                  default=np.datetime64('NaT')),
        'eqpline': f['eqpline'],
    })
    return out.drop_duplicates().reset_index(drop=True)


def _excel_safe(df):
    """tz-aware datetime 은 Excel 로 저장할 수 없어 tz 를 제거한다."""
    out = df.copy()
    for col in out.columns:
        if isinstance(out[col].dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_localize(None)
    return out


def _int_str(series):
    """Oracle TO_CHAR(number) 대응. 소수점 표기('3.0') 방지."""
    n = pd.to_numeric(series, errors='coerce')
    return n.astype('Float64').astype('Int64').astype('string')


LINES = ("KFR7", "PFR1")

# KFR7 원천에는 NRD-K 와 NRD 가 함께 들어온다. 목적지 라인으로 가른다.
#   KFR7 + dest_line_id IN ('KFR7A','KFR7B') -> KFR7 (NRD-K)
#   KFR7 + dest_line_id IN ('KFR7C','KFR7D') -> KFR4 (NRD)
# 그 외 dest_line_id 는 기존 분류를 그대로 둔다.
DEST_LINE_MAP = {
    "KFR7": {"KFR7A": "KFR7", "KFR7B": "KFR7",
             "KFR7C": "KFR4", "KFR7D": "KFR4"},
}


# 특정 lot 이 어디서 빠지는지 단계마다 남긴다.
#   실행:  set TRACE_LOT=7DDWG17.1  후 build_f3 실행
#   또는:  python getdata/build_f3.py --trace-lot 7DDWG17.1
TRACE_LOT = os.environ.get("TRACE_LOT", "").strip()
for _i, _a in enumerate(sys.argv):
    if _a == "--trace-lot" and _i + 1 < len(sys.argv):
        TRACE_LOT = sys.argv[_i + 1].strip()

# 원천에는 있는데 최종 f3 에 없는 lot 을 전부 뽑아 엑셀로 남긴다.
#   실행:  set TRACE_DROP=1   또는  python getdata/build_f3.py --trace-drop
#   결과:  getdata/out/dropped_lots_YYYYmmdd_HHMM.xlsx
TRACE_DROP = (os.environ.get("TRACE_DROP", "").strip() not in ("", "0")
              or "--trace-drop" in sys.argv)


def dump_dropped(df_lot, df_f3, path=None, base=None):
    """원천에는 있는데 최종 f3 에 없는 lot 을 전부 뽑아 엑셀로 남긴다.

    df_lot  거르지 않은 원천(진단 모드) 또는 실제 사용분
    base    실제로 파이프라인에 들어간 lot. 이것과 비교해 SQL 단계에서
            걸러진 것과 그 뒤에서 빠진 것을 나눈다.
    """
    m = _lower_cols(df_lot).copy()
    if "lot_id" not in m.columns:
        print("[DROP] lot_id 없음", flush=True)
        return None
    m["lot_id"] = m["lot_id"].astype("string")

    keep = set()
    if df_f3 is not None and len(df_f3):
        keep = set(_lower_cols(df_f3)["lot_id"].astype("string"))
    used = None
    if base is not None and len(base):
        used = set(_lower_cols(base)["lot_id"].astype("string"))

    out = m[~m["lot_id"].isin(keep)].drop_duplicates(subset=["lot_id"]).copy()
    print(f"[DROP] 원천 {m['lot_id'].nunique():,} · f3 {len(keep):,} · "
          f"빠짐 {len(out):,}", flush=True)
    if used is not None:
        gone = len(set(m["lot_id"]) - used)
        print(f"[DROP]   조회 단계에서 {gone:,} · 그 뒤 단계에서 "
              f"{len(out) - gone:,}", flush=True)
    if out.empty:
        return None

    def _why(r):
        lot = r.get("lot_id")
        line = str(r.get("line") or "")
        sysl = str(r.get("sys_line_id") or "")
        cur = str(r.get("cur_line_id") or "")
        lt = str(r.get("lot_type") or "")
        # SQL 조건에 먼저 걸리는 것부터 본다.
        if sysl and line != sysl:
            return f"라인 불일치(line={line} sys={sysl})"
        if lt and lt not in ("PP", "PG", "EG"):
            return f"lot_type 제외({lt})"
        if cur == "CHTV":
            return "cur_line_id = CHTV"
        if not str(r.get("proc_id") or "").strip():
            return "proc_id 없음"
        if not str(r.get("step_seq") or "").strip():
            return "step_seq 없음"
        # 여기까지 통과했는데 없다면 조인 단계에서 빠진 것이다.
        if used is not None and lot not in used:
            return "원천 조회 단계에서 제외"
        return "StepPath 에서 스텝을 못 찾음"

    out["탈락사유"] = out.apply(_why, axis=1)

    cols = [c for c in ("line", "sys_line_id", "cur_line_id", "dest_line_id",
                        "origin_line_id", "lot_id", "lot_type", "status",
                        "proc_id", "step_seq", "order_seq", "prod2", "qty",
                        "탈락사유") if c in out.columns]
    out = out[cols].sort_values(["탈락사유", "lot_id"], kind="mergesort")

    if path is None:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(
            d, f"dropped_lots_{dt.datetime.now():%Y%m%d_%H%M}.xlsx")

    try:
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            out.to_excel(w, sheet_name="빠진 lot", index=False)
            (out.groupby("탈락사유").size().rename("건수").reset_index()
                .sort_values("건수", ascending=False)
                .to_excel(w, sheet_name="사유별 집계", index=False))
    except Exception as e:
        path = path.replace(".xlsx", ".csv")
        out.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"[DROP] 엑셀 실패({type(e).__name__}) - CSV 로 저장", flush=True)

    print(f"[DROP] -> {path}", flush=True)
    for why, n in out["탈락사유"].value_counts().items():
        print(f"[DROP]   {why:34s} {n:>6,}", flush=True)
    return path


def relabel_lines(f3, df_lot):
    """완성된 f3 의 line 만 dest_line_id 기준으로 다시 매긴다.

    조인·집계는 전부 원천 라인(KFR7)으로 끝낸 뒤 마지막에 라벨만 바꾼다.
    중간에 나누면 step_path/tip/equipment 조인을 라인별로 다시 짜야 해서
    복잡해지고, 결과는 어차피 같다.
    """
    lot = _lower_cols(df_lot)
    trace_lot("relabel 입력 f3", f3)
    if "dest_line_id" not in lot.columns:
        print("[LINE] dest_line_id 없음 - 재분류 생략", flush=True)
        return f3

    dest = (lot[["lot_id", "line", "dest_line_id"]]
            .dropna(subset=["lot_id"]).drop_duplicates(subset=["lot_id"]))
    dest["_d"] = dest["dest_line_id"].astype("string").str.strip().str.upper()

    tgt = {}
    for _, r in dest.iterrows():
        m = DEST_LINE_MAP.get(str(r["line"]))
        if m and r["_d"] in m:
            tgt[r["lot_id"]] = m[r["_d"]]
    if not tgt:
        return f3

    out = f3.copy()
    before = out["line"].astype(str)
    new = out["lot_id"].map(tgt).fillna(before)
    moved = int((new != before).sum())
    out["line"] = new
    if moved:
        cnt = new[new != before].value_counts()
        print(f"[LINE] dest_line_id 로 재분류 {moved:,}행  "
              + ", ".join(f"{k}:{v:,}" for k, v in cnt.items()), flush=True)
    return out

# Oracle `step_skip_yn <> 'Y'` 는 NULL 행을 제외한다(NULL <> 'Y' 는 UNKNOWN).
# 재현 구현들은 NULL 을 포함해 왔다. 원본과 맞추려면 True 로 둔다.
EXCLUDE_NULL_STEP_SKIP_YN = True

# batch_kind 는 EQP 단위 값이라 한 설비그룹에 batch/비batch 설비가 섞이면
# 같은 lot·step 이 여러 행으로 갈라진다. eqpline 과 동일하게 step 단위로 합친다.
AGGREGATE_BATCH_KIND = True

# 결과를 DB(f3_live / f3_history)에 적재할지.
LOAD_TO_DB = True

# 원천 조달 경로.
#   "s3"  : Spotfire 가 S3 에 올린 raw 를 읽는다(현행)
#   "bdq" : 기존 bigdataquery 로 Impala 를 직접 조회한다(폴백)
SOURCE = "s3"

# S3 테이블명 -> 기존 fetch() 이름 매핑.
# 이름만 바꿔 끼우면 이후 전처리는 손댈 필요가 없다.
# 같은 회차를 두 번 처리하지 않는다.
#   Spotfire 30분 주기 + build_f3 30분 주기가 엇갈리면, 아직 갱신 안 된 S3 를
#   다시 읽어 같은 스냅샷을 중복 적재하게 된다. 매니페스트의 finished_at 을
#   직전 처리분과 비교해 새 회차일 때만 진행한다.
SKIP_IF_NOT_FRESH = True
_FRESH_MARK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cache", "last_manifest.txt")

S3_MAP = {
    "lot": "PFR1_KFR7_LOT",
    "materialworkstatus": "PFR1_KFR7_MATERIALWORKSTATUS",
    "ssps_prod_name": "PFR1_KFR7_SSPS_PROD_NAME",
    "equipment": "PFR1_KFR7_EQUIPMENT",
    "eqp_group": "PFR1_KFR7_EQP_GROUP",
    "hold": "PFR1_KFR7_HOLD",
    "KFR7_step_path": "PFR1_KFR7_STEP_PATH",
    "PFR1_step_path": "PFR1_KFR7_STEP_PATH",
    "KFR7_tip": "PFR1_KFR7_TIP",
    "PFR1_tip": "PFR1_KFR7_TIP",
    # FabPlan : PFR1 재공 중 order_seq 가 비어 있는 lot 의 스텝 경로
    "fab_step": "PFR1_FABPLAN_STEP",
    "fab_pems": "PFR1_FABPLAN_NEWEINECNSPEC",
    "fab_sel": "PFR1_FABPLAN_SELECTCONNECTSPEC",
    "fab_skiprule": "PFR1_FABPLAN_SKIPRULE",
    "fab_engr": "PFR1_ENGR_LOT_PPID",
}

# FabPlan 을 처리할지. 원천이 아직 안 올라왔으면 자동으로 건너뛴다.
FABPLAN = True

# 엑셀 파일 저장 여부. 2시간마다 적재하므로 평상시엔 끈다.
#   웹의 [다운로드] 메뉴에서 '재공Raw' 를 받으면 되고,
#   손으로 확인하고 싶을 때만 True 로 바꿔 실행한다.
SAVE_EXCEL = False           # 결과 파일 f3_<시각>.xlsx
SAVE_RAW_EXCEL = False       # 원본검증 파일 raw_<시각>.xlsx

# 설비그룹명으로 구성설비를 역추정해 경고하는 진단. 그룹명 표기가 일정하지 않아
# ('DTDD707_8' -> DTDD708, 'MCDD701_MSV_CD', 'NRDWAIT' 등) 오탐이 많다.
# 스냅샷 처리를 다시 의심할 때만 켠다.
WARN_STRAY_GROUP_MEMBER = False

# 원본테이블 검증 시트 설정
#   시트 1장당 행수 상한. Excel 한계(1,048,576)보다 낮게 잡아야 안전하다.
RAW_SHEET_ROWS_PER_SHEET = 200_000
#   테이블별 총 기록 행수. None = 전량.
#   step_path / tip 은 수백만 행이라 전량 기록 시 파일이 수 GB 가 되고 저장에만
#   수십 분이 걸린다. 필요할 때만 숫자를 올릴 것.
RAW_SHEET_MAX_ROWS = {
    "lot": None,
    "equipment": None,
    "eqp_group": None,
    "hold": None,
    "KFR7_step_path": 100_000,
    "PFR1_step_path": 100_000,
    "KFR7_tip": 100_000,
    "PFR1_tip": 100_000,
    "t_rules(전처리된 tip)": 200_000,
    "tip_join_검증": 200_000,
    "eqpgroup_출처추적": 200_000,
    "s(step_scope)": 200_000,
}

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
    "투입경과_일", "마지막이벤트경과_일", "스텝도착경과_일", "마지막작업경과_일",
    "lot_status", "step_status", "proc_id", "de_rank", "연속", "AREA", "layer_id",
    "현스텝", "order_seq", "step_seq", "step_desc", "recipe_id", "eqp_type",
    "batch_kind", "eqpline", "eqpgroup", "eqpgroup_cham",
    "tip", "down", "hold", "hold_reason", "exception", "exception_reason",
    "ftp", "ftp_reason", "fa_object4", "prod1", "prod2", "dept", "dest_line_id",
    "module1", "module2", "cause_detail",
]

# hold 는 생테이블 조회에 4분이 걸리는데, 실제로 쓰이는 건 현재 재공(mc_lot)에
# 해당하는 lot 뿐이다. 이 테이블만은 서버(Impala)에서 미리 걸러 가져온다.
#   - version_desc 는 적재 ID(스냅샷 식별자)다. 전역 MAX 가 현재 시점 스냅샷.
#     반드시 최신 version_desc 로 먼저 좁힌 뒤 status_seq 를 걸러야 한다.
#     (순서를 바꾸면 최신 스냅샷의 행이 전부 '2' 일 때 옛 스냅샷이 딸려온다)
#   - status_seq <> '2' (조치완료 제외. '0' 발의 / '1' 조치중 / '3' 조치불가는 유지)
#   - 현재 재공(Active/Hold) 과 line_id + lot_id 로 조인되는 건만
# 느려지거나 문제가 생기면 HOLD_SERVER_SIDE_FILTER = False 로 되돌린다.
HOLD_SERVER_SIDE_FILTER = True

hold_query_raw = """
SELECT line_id, item_type, status_seq, lot_id, step_seq,
       hold_user_name, issue_reason_cont, issue_date, version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN ('KFR4', 'KFR7', 'PFR1')
"""

hold_query_joined = """
WITH cur AS (
    SELECT 'PFR1' AS line_id, lot_id
    FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
    WHERE  lot_status_seg IN ('Active', 'Hold')
    UNION ALL
    SELECT 'KFR7' AS line_id, lot_id
    FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT
    WHERE  lot_status_seg IN ('Active', 'Hold')
),
h AS (
    SELECT line_id, item_type, status_seq, lot_id, step_seq,
           hold_user_name, issue_reason_cont, issue_date,
           version_desc,
           MAX(version_desc) OVER () AS max_version_desc
    FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
    WHERE  line_id IN ('KFR4', 'KFR7', 'PFR1')
)
SELECT h.line_id, h.item_type, h.status_seq, h.lot_id, h.step_seq,
       h.hold_user_name, h.issue_reason_cont, h.issue_date
FROM   h
JOIN   (SELECT DISTINCT line_id, lot_id FROM cur) c
  ON   h.line_id = c.line_id
 AND   h.lot_id  = c.lot_id
WHERE  h.version_desc = h.max_version_desc
  AND  h.status_seq <> '2'
"""

# 벤치 결과(bench_loading): 조인만 서버에서 하고 나머지를 python 에서 거는 편이
# 가장 빨랐다(h7 8.2s vs h10 13.9s). version_desc/status_seq 필터는 build_hold 가
# 동일하게 적용하므로 결과는 같다.
hold_query_join_only = """
SELECT h.line_id, h.item_type, h.status_seq, h.lot_id, h.step_seq,
       h.hold_user_name, h.issue_reason_cont, h.issue_date, h.version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT h
JOIN   (SELECT DISTINCT line_id, lot_id FROM (
            SELECT 'PFR1' AS line_id, lot_id
            FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
            WHERE  lot_status_seg IN ('Active', 'Hold')
            UNION ALL
            SELECT 'KFR7' AS line_id, lot_id
            FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT
            WHERE  lot_status_seg IN ('Active', 'Hold')
        ) z) c
  ON   h.line_id = c.line_id AND h.lot_id = c.lot_id
WHERE  h.line_id IN ('KFR7', 'PFR1')
"""

# version_desc 는 'YYYYMMDD-HHMMSS' 형태의 적재 ID. 최신값을 찾을 때
# 최근 N일로 한정하면 전수 스캔을 피할 수 있다.
HOLD_VERSION_LOOKBACK_DAYS = 7

# hold 조회에서 mc_lot 조인을 생략할지. 생략해도 결과는 같다(재공 밖 lot 은
# 어차피 f1 조인에서 버려진다). 조인이 mc_lot 두 테이블 전수 스캔을 유발한다.
HOLD_SKIP_MCLOT_JOIN = True

hold_max_query_bounded = """
SELECT MAX(version_desc) AS mv
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN ('KFR7', 'PFR1')
  AND  version_desc >= '{LO}'
"""

hold_max_query = """
SELECT MAX(version_desc) AS mv
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN ('KFR7', 'PFR1')
"""

# 벤치(bench_loading) 최속안. version_desc 를 리터럴로 박으면 파티션 프루닝이
# 걸려 조인/윈도우 방식보다 일관되게 빠르고 전송량도 174,632 -> 581 행으로 준다.
# mc_lot 조인은 두 라인 테이블을 통째로 스캔하면서 1,743행 -> 589행 정도만 줄인다.
# version_desc 리터럴이 이미 대부분을 걷어내므로 조인은 비용 대비 이득이 없다.
# lot 범위 제한은 python 에서 df_lot 으로 처리한다(무료).
hold_query_no_join = """
SELECT line_id, item_type, status_seq, lot_id, step_seq,
       hold_user_name, issue_reason_cont, issue_date, version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN ('KFR7', 'PFR1')
  AND  version_desc = '{MV}'
  AND  status_seq <> '2'
"""

hold_query_two_step = """
SELECT h.line_id, h.item_type, h.status_seq, h.lot_id, h.step_seq,
       h.hold_user_name, h.issue_reason_cont, h.issue_date, h.version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT h
JOIN   (SELECT DISTINCT line_id, lot_id FROM (
            SELECT 'PFR1' AS line_id, lot_id
            FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
            WHERE  lot_status_seg IN ('Active', 'Hold')
            UNION ALL
            SELECT 'KFR7' AS line_id, lot_id
            FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT
            WHERE  lot_status_seg IN ('Active', 'Hold')
        ) z) c
  ON   h.line_id = c.line_id AND h.lot_id = c.lot_id
WHERE  h.line_id IN ('KFR7', 'PFR1')
  AND  h.status_seq <> '2'
  AND  h.version_desc = '{MV}'
"""

hold_query = hold_query_join_only if HOLD_SERVER_SIDE_FILTER else hold_query_raw


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
    """어떤 표기로 와도 TIMESTAMP 로.

    원천/쿼리 변경에 따라 같은 컬럼이 여러 형태로 온다. 전부 받아야 한다.
        2026-08-12 13:14:22        ISO
        2026-08-12 오후 1:14:22     한글 오전/오후
        20260812 131422            숫자 + 공백
        20260812131422             숫자만
        202608121314220            숫자 + 밀리초
        (TIMESTAMP 타입)            Oracle TO_DATE 결과

    방식: 숫자만 뽑아 14자리로 맞춰 파싱하는 경로를 먼저 시도하고,
    안 되면 문자열 파서 -> TRY_CAST 순으로 폴백한다.
    """
    txt = f"TRIM(CAST({column} AS VARCHAR))"
    # 숫자만 추출 -> 8자리 이상이면 14자리로 우측 0 채움
    digits = f"REGEXP_REPLACE({txt}, '[^0-9]', '', 'g')"
    d14 = (f"CASE WHEN LENGTH({digits}) >= 8 "
           f"THEN RPAD(SUBSTR({digits}, 1, 14), 14, '0') END")
    ampm = (
        "REPLACE(REPLACE(REGEXP_REPLACE(" + txt + ", "
        "'(오전|오후) 0?0:', '\\1 12:'), '오전', 'AM'), '오후', 'PM')"
    )
    # 순서 주의: 한글 오전/오후를 먼저 본다. 숫자 추출이 먼저 걸리면
    # '2026-08-12 오후 1:14:22' 가 20260812114220 으로 뭉개진다.
    return (
        f"COALESCE("
        f"  CASE WHEN {txt} LIKE '%오전%' OR {txt} LIKE '%오후%'"
        f"       THEN TRY_STRPTIME({ampm}, '%Y-%m-%d %p %I:%M:%S') END,"
        f"  TRY_STRPTIME({d14}, '%Y%m%d%H%M%S'),"
        f"  TRY_CAST({column} AS TIMESTAMP))"
    )


# 경과일 계산 기준 시각. DuckDB 의 CURRENT_TIMESTAMP 는 UTC 라서,
# KST 로 기록된 원천 시각과 비교하면 최근 9시간 이내 값이 미래로 보여
# 경과일이 음수가 된다. 파이프라인 시작 시각(로컬)을 리터럴로 박아 쓴다.
NOW_SQL = "CAST(now() AS TIMESTAMP)"


def set_now(ts):
    """경과일 기준 시각을 고정한다(=snapshot_at). 실행 중 값이 흔들리지 않는다."""
    global NOW_SQL
    NOW_SQL = "TIMESTAMP '" + ts.strftime("%Y-%m-%d %H:%M:%S") + "'"


def elapsed_days_num(column: str) -> str:
    return (f"ROUND(GREATEST((EPOCH({NOW_SQL}) - EPOCH({parsed_ts(column)}))"
            f" / 86400.0, 0), 1)")


def elapsed_days_text(column: str) -> str:
    return "FORMAT('{:.1f}', " + elapsed_days_num(column) + ") || '일↑'"


# 설비그룹 첫 글자로 공정 AREA 를 정한다.
AREA_MAP = {
    "E": "ETCH", "P": "PHOTO", "M": "METRO", "I": "IMP", "D": "DIFF",
    "W": "CLN", "F": "IMP", "T": "CVD", "S": "METAL", "C": "CMP",
}


def area_of(series):
    """설비그룹 -> AREA. 첫 글자로 가른다. 모르는 글자는 비워 둔다."""
    if series is None:
        return pd.NA
    s = pd.Series(series).astype("string").str.strip().str.upper()
    return s.str[0].map(AREA_MAP)


PROD_COLS = ("prod1", "prod2", "dept")
MODULE_COLS = ("module1", "module2")

# 동시에 걸리면 이 순서로 하나만 쓴다
HOLDTYPE_ORDER = (("HOLD", "hold", "hold_reason"),
                  ("FTP", "ftp", "ftp_reason"),
                  ("예약제외", "exception", "exception_reason"))


def holdtype_rules():
    """f3_std_holdtype 규칙. 구체적인 것 먼저, 같으면 저장 순서."""
    rows = _std_table("f3_std_holdtype",
                      ["id", "line", "type", "condition1", "condition2",
                       "condition3", "type_name"])
    out = []
    for r in rows:
        out.append({"id": r["id"], "line": r["line"],
                    "type": (r["type"] or "ALL").upper(),
                    "c": [x for x in (r["condition1"], r["condition2"],
                                      r["condition3"]) if x],
                    "name": r["type_name"]})

    def spec(r):
        return len(r["c"]) + (1 if r["line"] else 0) + (0 if r["type"] == "ALL" else 1)

    return sorted(out, key=lambda r: (-spec(r), r["id"]))


def holdtype_of(row, rules):
    """한 행의 세부 유형. 못 찾으면 None."""
    line = str(row.get("line") or "")
    for kind, flag, reason in HOLDTYPE_ORDER:
        if not row.get(flag):
            continue
        text = str(row.get(reason) or "")
        if not text:
            continue
        for rule in rules:
            if rule["line"] and rule["line"] != line:
                continue
            if rule["type"] not in ("ALL", kind):
                continue
            if all(c in text for c in rule["c"]):
                return rule["name"]
    return None


def attach_holdtype(f3):
    """기준정보로 cause_detail(세부 원인 유형)을 채운다."""
    out = f3.copy()
    out["cause_detail"] = pd.NA
    rules = holdtype_rules()
    if not rules:
        return out

    recs = out[["line", "hold", "hold_reason", "ftp", "ftp_reason",
                "exception", "exception_reason"]].to_dict("records")
    vals = [holdtype_of(r, rules) for r in recs]
    out["cause_detail"] = pd.Series(vals, index=out.index, dtype="object")
    n = int(out["cause_detail"].notna().sum())
    print(f"[STD] holdtype {n:,}/{len(out):,}행 매칭 (규칙 {len(rules)}건)", flush=True)
    return out


def _std_table(name, cols):
    """기준정보 테이블을 읽는다. 없으면 빈 목록."""
    conn = None
    try:
        import db_common as DB
        conn = DB.connect()
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(cols)} FROM {name}")
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        print(f"[STD] {name} 읽기 생략: {type(e).__name__}", flush=True)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _as_num(v):
    """앞자리 0 이 붙거나 빠져도 같은 값으로 보도록 숫자로 바꾼다.

    f3 의 layer 는 '050' 처럼 0 이 채워져 오고, 기준정보에는 '50' 이나 '20'
    처럼 들어오는 경우가 많다. 문자열로 비교하면 '050' < '20' 이 되어 어긋난다.
    숫자로 못 바꾸면 None 을 돌려주고, 그때만 문자열로 비교한다.
    """
    if v is None or (isinstance(v, float) and v != v):
        return None
    t = str(v).strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _in_range(v, lo, hi):
    """lo~hi 범위 판정. 경계는 포함하고, 비어 있으면 와일드카드.

    값과 경계가 모두 숫자면 숫자로, 하나라도 아니면 문자열로 비교한다.
    """
    if v is None or (isinstance(v, float) and v != v):
        return lo in (None, "") and hi in (None, "")

    nv, nlo, nhi = _as_num(v), _as_num(lo), _as_num(hi)
    use_num = nv is not None and \
        (lo in (None, "") or nlo is not None) and \
        (hi in (None, "") or nhi is not None)

    if use_num:
        if nlo is not None and nv < nlo:
            return False
        if nhi is not None and nv > nhi:
            return False
        return True

    t = str(v).strip()
    if lo not in (None, "") and t < str(lo).strip():
        return False
    if hi not in (None, "") and t > str(hi).strip():
        return False
    return True


def attach_module(f3):
    """기준정보(f3_std_module)로 module1 / module2 를 채운다.

    빈 칸은 와일드카드다. 여러 행이 맞으면 **더 구체적으로 지정된 행**
    (빈 칸이 적은 행)이 이긴다.
    """
    out = f3.copy()
    for c in MODULE_COLS:
        out[c] = pd.NA

    rules = _std_table("f3_std_module",
                       ["line", "proc_id", "start_layer", "end_layer",
                        "start_stepseq", "end_stepseq", "module1", "module2"])
    if not rules:
        return out

    def spec(r):                       # 지정된 조건 개수 = 구체성
        return sum(1 for k in ("line", "proc_id", "start_layer", "end_layer",
                               "start_stepseq", "end_stepseq") if r.get(k))

    rules.sort(key=spec)               # 덜 구체적인 것부터 덮어써 구체적인 것이 남는다
    hit_total = 0
    for r in rules:
        m = pd.Series(True, index=out.index)
        if r.get("line"):
            m &= out["line"].astype(str).eq(str(r["line"]))
        if r.get("proc_id"):
            m &= out["proc_id"].astype(str).eq(str(r["proc_id"]))
        if r.get("start_layer") or r.get("end_layer"):
            m &= out["layer_id"].map(
                lambda v: _in_range(v, r.get("start_layer"), r.get("end_layer")))
        if r.get("start_stepseq") or r.get("end_stepseq"):
            m &= out["step_seq"].map(
                lambda v: _in_range(v, r.get("start_stepseq"), r.get("end_stepseq")))
        if not m.any():
            continue
        out.loc[m, "module1"] = r["module1"]
        out.loc[m, "module2"] = r.get("module2") or r["module1"]
        hit_total = int(out["module1"].notna().sum())

    print(f"[STD] module {hit_total:,}/{len(out):,}행 매칭 (규칙 {len(rules)}건)",
          flush=True)
    return out


def attach_std_product(lot):
    """기준정보 제품구분(f3_std_product) 을 먼저 적용한다.

    lot_id 의 1~5 번째 글자와 PLAN 이 **모두** 맞으면 그 이름을 쓴다.
    비운 칸은 따지지 않는다. 조건이 많이 맞는(구체적인) 규칙이 이긴다.
    여기서 정해진 lot 은 SSPS 제품명을 덮어쓰지 않는다.
    """
    rules = _std_table("f3_std_product",
                       ["lot_char1", "lot_char2", "lot_char3", "lot_char4",
                        "lot_char5", "proc_id", "product_name"])
    if not rules:
        return lot

    up = lot["lot_id"].astype("string").str.upper()
    plan = lot.get("proc_id", pd.Series("", index=lot.index))
    plan = plan.astype("string").str.strip().str.upper().fillna("")
    ch = {i: up.str.slice(i - 1, i) for i in range(1, 6)}

    # 조건 수가 많은 규칙이 이긴다. 적은 것부터 덮어써 마지막에 큰 것이 남는다.
    def _n(r):
        return sum(1 for k in ("lot_char1", "lot_char2", "lot_char3",
                               "lot_char4", "lot_char5", "proc_id")
                   if str(r.get(k) or "").strip())

    hit_n = pd.Series(-1, index=lot.index)
    name = pd.Series(pd.NA, index=lot.index, dtype="string")
    for r in sorted(rules, key=_n):
        nm = str(r.get("product_name") or "").strip()
        if not nm:
            continue
        m = pd.Series(True, index=lot.index)
        for i in range(1, 6):
            want = str(r.get(f"lot_char{i}") or "").strip().upper()
            if want:
                m &= ch[i].eq(want)
        wp = str(r.get("proc_id") or "").strip().upper()
        if wp:
            m &= plan.eq(wp)
        m &= hit_n.le(_n(r))
        if m.any():
            name = name.mask(m, nm)
            hit_n = hit_n.mask(m, _n(r))

    got = name.notna()
    if got.any():
        lot.loc[got, "prod2"] = name[got]
        print(f"[JOIN] 기준정보 제품구분 적용 {int(got.sum()):,}행", flush=True)
    return lot


def attach_prod(df_lot, df_prod):
    """SSPS_PROD_NAME 을 lot 에 붙인다.

    조인 조건
        line = line_id
        lot_type = lot_type
        LEFT(lot_id, LEN(id)) = id       <- id 길이가 2~4 로 제각각이다

    같은 lot 에 길이가 다른 규칙이 함께 맞으면 **더 긴(구체적인) 쪽**을 쓴다.
    """
    lot = _lower_cols(df_lot).copy()
    for c in PROD_COLS:
        lot[c] = pd.NA
    if df_prod is None or not len(df_prod):
        print("[JOIN] 제품구분 원천 없음", flush=True)
        return lot

    p = _lower_cols(df_prod).copy()
    lcol = "line_id" if "line_id" in p.columns else "line"
    need = [lcol, "lot_type", "id"] + [c for c in PROD_COLS if c in p.columns]
    p = p[need].dropna(subset=[lcol, "lot_type", "id"])
    p["id"] = p["id"].astype(str).str.strip()
    p = p[p["id"] != ""]
    p["_len"] = p["id"].str.len()

    key = ["line", "lot_type"]
    lot["_ltype"] = lot["lot_type"].astype(str)
    filled = 0
    # 짧은 규칙부터 붙이고 긴 규칙으로 덮어써서, 긴 쪽이 이기게 한다
    for n in sorted(p["_len"].unique()):
        sub = (p[p["_len"] == n]
               .rename(columns={lcol: "line"})
               .drop_duplicates(subset=key + ["id"]))
        lot["_pfx"] = lot["lot_id"].astype(str).str.slice(0, int(n))
        merged = lot.merge(sub, how="left", left_on=key + ["_pfx"],
                           right_on=key + ["id"], suffixes=("", "_n"))
        for c in PROD_COLS:
            src = c + "_n"
            if src in merged.columns:
                lot[c] = merged[src].where(merged[src].notna(), lot[c].values).values
        filled = int(lot["prod1"].notna().sum())

    lot = lot.drop(columns=[c for c in ("_pfx", "_ltype") if c in lot.columns])
    print(f"[JOIN] 제품구분 {filled:,}/{len(lot):,} lot 매칭", flush=True)
    return lot


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


# ---------------------------------------------------------------------------
# 1-2. FabPlan 스텝 경로
#   PFR1 재공 중 order_seq 가 비어 있는 lot 은 StepPath 로 스텝이 붙지 않는다.
#   대신 공정 정의(STEP) 를 직접 훑어 현스텝부터 연속끝까지를 만든다.
#
#   흐름
#     1) lot 의 현스텝 위치를 STEP 안에서 찾는다
#     2) 사전지정(ENGR PPID) 으로 recipe / 설비를 제한한다
#     3) EIN / ECN / RCS 규제를 붙인다
#     4) SKIP 을 판정한다
#     5) delaytime 으로 연속을 매긴다
#     6) 현스텝 ~ 연속끝만 남긴다
#   결과 컬럼은 narrow_step_to_scope 와 같게 맞춘다.
# ---------------------------------------------------------------------------
FAB_SENTINEL = {"1000000020", "1000000000"}   # 이 delaytime 은 연속이 아니다
FAB_METRO = {"METRO", "MI"}


def _fab_str(sr):
    """비교용 문자열. NaN 과 '-' 는 빈 문자열로 본다."""
    out = sr.astype("string").fillna("").str.strip()
    return out.mask(out.isin(["-", "nan", "None"]), "")


def _fab_step_table(df_step):
    """STEP 원천을 공정별 순번이 붙은 표로 만든다."""
    st = _lower_cols(df_step)
    for c in ("processid", "stepseq", "stepseq_type", "areaname", "eqptype",
              "layerid", "descript", "recipeid", "delaytime", "skiprule"):
        if c not in st.columns:
            st[c] = pd.NA
    st = st[["processid", "stepseq", "stepseq_type", "areaname", "eqptype",
             "layerid", "descript", "recipeid", "delaytime", "skiprule"]].copy()
    for c in ("processid", "stepseq", "stepseq_type", "areaname", "skiprule"):
        st[c] = _fab_str(st[c])
    # stepseq 가 R 로 시작하면 메인으로 본다(참고 구현과 같은 규칙).
    st.loc[st["stepseq"].str.startswith("R"), "stepseq_type"] = "메인"
    st = st.sort_values(["processid", "stepseq"], kind="mergesort")
    st = st.reset_index(drop=True)
    st["_ord"] = st.groupby("processid", sort=False).cumcount()
    return st


def _fab_delay_norm(sr):
    """delaytime 을 문자열로 고른다. 12.0 과 12 가 갈리지 않게 한다."""
    n = pd.to_numeric(sr, errors="coerce")
    out = pd.Series("", index=sr.index, dtype="object")
    ok = n.notna()
    out[ok] = np.where(n[ok] == np.trunc(n[ok]),
                       np.trunc(n[ok]).astype("int64").astype(str),
                       n[ok].astype(str))
    # 숫자가 아니면 원문을 쓴다.
    raw = _fab_str(sr)
    out[~ok] = raw[~ok]
    return out


def _fab_effective_delay(g):
    """METRO / MI 는 뒤쪽 '메인·비METRO' 스텝의 delaytime 을 물려받는다."""
    d = g["_delay"].to_numpy(dtype=object)
    is_metro = g["_metro"].to_numpy()
    is_mnm = g["_main_non_metro"].to_numpy()
    out = d.copy()
    nxt = ""
    for i in range(len(d) - 1, -1, -1):
        if is_mnm[i] and d[i]:
            nxt = d[i]
        if is_metro[i]:
            out[i] = nxt
    return pd.Series(out, index=g.index)


def _fab_continuous(df):
    """delaytime 으로 연속 구간을 매긴다.

    연속첫  구간 바로 앞의 '메인·비METRO' 이면서 core 가 아닌 스텝
    연속    구간 안의 core 스텝(마지막 포함)
    f3 는 '연속끝' 을 따로 두지 않는다. de_rank 로 구간을 가른다.
    """
    df = df.copy()
    df["연속"] = None
    order = df.groupby("lot_id", sort=False).indices

    core = df["_core"].to_numpy()
    mnm = df["_main_non_metro"].to_numpy()
    lab = np.array([None] * len(df), dtype=object)

    for _, idx in order.items():
        n = len(idx)
        i = 0
        while i < n:
            if not core[idx[i]]:
                i += 1
                continue
            j = i
            while j + 1 < n and core[idx[j + 1]]:
                j += 1
            start = i
            prev = i - 1
            if prev >= 0 and mnm[idx[prev]] and not core[idx[prev]]:
                lab[idx[prev]] = "연속첫"
                start = prev
            for p in range(start, j + 1):
                if lab[idx[p]] == "연속첫":
                    continue
                lab[idx[p]] = "연속"
            i = j + 1

    df["연속"] = lab
    return df


def _fab_skip(df):
    """SKIP 판정. 사전지정이 있으면 어떤 이유로도 SKIP 하지 않는다."""
    lot = df["lot_id"]
    # lot_id 의 '.' 바로 앞 한 글자(ff), 그 앞 한 글자(tt) 가 SKIP 키다.
    dot = lot.str.find(".")
    ff_ch = [l[p - 1:p] if p >= 1 else "" for l, p in zip(lot, dot)]
    tt_ch = [l[p - 2:p - 1] if p >= 2 else "" for l, p in zip(lot, dot)]

    def _hit(lst, tok):
        if not tok:
            return False
        s = str(lst or "").strip()
        if s in ("", "-"):
            return False
        return tok in [x.strip() for x in s.split(",")]

    id_skip = pd.Series(
        [_hit(a, b) or _hit(c, d)
         for a, b, c, d in zip(df["_ff"], ff_ch, df["_tt"], tt_ch)],
        index=df.index)

    hot = _fab_str(df["hot_lot_level"])
    cat = _fab_str(df["_category"])
    hot_skip = (hot != "") & pd.Series(
        [h in c for h, c in zip(hot, cat)], index=df.index)

    rule_skip = df["skiprule"].eq("100")
    pre = df["사전지정"].eq("O")

    # PEMS 점프: START / CONNECT 는 nextstepseq 까지 건너뛴다.
    ct = _fab_str(df["connecttype"]).str.upper()
    ns = _fab_str(df["nextstepseq"])
    trig = ct.isin(["START", "CONNECT"]) & ns.ne("")

    jump = pd.Series(False, index=df.index)
    tgt_hit = pd.Series(False, index=df.index)
    for _, idx in df.groupby("lot_id", sort=False).indices.items():
        steps = df["stepseq"].to_numpy()[idx]
        pos = {st: k for k, st in enumerate(steps)}
        tset = {t for t in ns.to_numpy()[idx] if t}
        if tset:
            tgt_hit.iloc[idx] = np.isin(steps, list(tset))
        for k, gi in enumerate(idx):
            if not trig.iloc[gi]:
                continue
            t = ns.iloc[gi]
            end = pos.get(t, len(idx))
            for p in range(k + 1, max(k + 1, end)):
                if pre.iloc[idx[p]]:
                    continue
                jump.iloc[idx[p]] = True

    # 비메인은 진행 지정이 없으면 건너뛴다.
    designated = ct.isin(["START", "CONNECT"]) | tgt_hit
    etc_skip = df["stepseq_type"].eq("기타") & (~pre) & (~designated)

    skip = (jump | hot_skip | rule_skip | id_skip | etc_skip) & (~pre)
    df = df.copy()
    df["SKIP"] = np.where(skip, "O", "")
    return df


def fabplan_scope(df_step, df_pems, df_sel, df_skiprule, df_engr,
                  df_lot, line="PFR1"):
    """FabPlan lot 의 스텝 경로를 narrow_step_to_scope 와 같은 모양으로 만든다."""
    m = _lower_cols(df_lot)
    m = m[m["line"].eq(line)].copy()
    # order_seq 가 비어 있는 lot 만 FabPlan 이다.
    m["order_seq"] = pd.to_numeric(m.get("order_seq"), errors="coerce")
    m = m[m["order_seq"].isna()]
    need = ["lot_id", "proc_id", "step_seq", "lot_level"]
    for c in need:
        if c not in m.columns:
            m[c] = pd.NA
    m = m[need].dropna(subset=["lot_id", "proc_id", "step_seq"]).drop_duplicates()
    print(f"[FABPLAN] 대상 lot {len(m):,}", flush=True)
    if m.empty:
        return pd.DataFrame()

    m = m.rename(columns={"lot_level": "hot_lot_level"})
    for c in ("lot_id", "proc_id", "step_seq"):
        m[c] = _fab_str(m[c])

    st = _fab_step_table(df_step)
    # SKIP 규칙(ff / tt) 을 스텝에 붙인다.
    sk = _lower_cols(df_skiprule)
    if not sk.empty and "skiprule" in sk.columns:
        sk = sk[["skiprule", "lotid_ld", "descript"]].drop_duplicates("skiprule")
        sk["skiprule"] = _fab_str(sk["skiprule"])
        st = st.merge(sk.rename(columns={"lotid_ld": "_ff", "descript": "_tt"}),
                      on="skiprule", how="left")
    else:
        st["_ff"] = pd.NA
        st["_tt"] = pd.NA
    if "category" in _lower_cols(df_step).columns:
        st["_category"] = _fab_str(_lower_cols(df_step)["category"])
    else:
        st["_category"] = ""

    # --- 1) 현스텝 위치를 찾아 그 뒤 구간만 붙인다 ---------------------------
    cur = m.merge(st[["processid", "stepseq", "_ord"]],
                  left_on=["proc_id", "step_seq"],
                  right_on=["processid", "stepseq"], how="inner")
    print(f"[FABPLAN] STEP 에서 현스텝 찾음 {len(cur):,} "
          f"(STEP 원천 {len(st):,}행)", flush=True)
    if cur.empty:
        # 못 찾으면 여기서 끝난다. PLAN·STEPSEQ 표기가 다른지 본다.
        print(f"[FABPLAN] lot 쪽 예시 {m[['proc_id','step_seq']].head(3).values.tolist()}",
              flush=True)
        print(f"[FABPLAN] STEP 쪽 예시 {st[['processid','stepseq']].head(3).values.tolist()}",
              flush=True)
        return pd.DataFrame()
    cur = cur[["lot_id", "proc_id", "step_seq", "hot_lot_level", "_ord"]]
    cur = cur.rename(columns={"_ord": "_cur_ord"})

    # 연속은 길어야 수십 스텝이다. 넉넉히 뒤로 이만큼만 본다.
    WIN = 100
    rows = cur.merge(st, left_on="proc_id", right_on="processid", how="inner")
    rows = rows[(rows["_ord"] >= rows["_cur_ord"])
                & (rows["_ord"] < rows["_cur_ord"] + WIN)].copy()
    if rows.empty:
        return pd.DataFrame()
    rows = rows.sort_values(["lot_id", "_ord"], kind="mergesort")
    rows = rows.reset_index(drop=True)

    # --- 2) 사전지정 : recipe 와 설비를 함께 제한한다 ------------------------
    eg = _lower_cols(df_engr)
    if not eg.empty and "lotid" in eg.columns:
        eg = eg[["lotid", "processid", "stepseq", "eqpid", "newppid"]].copy()
        for c in ("lotid", "processid", "stepseq"):
            eg[c] = _fab_str(eg[c])
        eg = eg.drop_duplicates(["lotid", "processid", "stepseq"])
        rows = rows.merge(
            eg.rename(columns={"lotid": "lot_id", "processid": "proc_id",
                               "eqpid": "_pre_eqp", "newppid": "_pre_ppid"}),
            on=["lot_id", "proc_id", "stepseq"], how="left")
    else:
        rows["_pre_eqp"] = pd.NA
        rows["_pre_ppid"] = pd.NA
    has_pre = _fab_str(rows["_pre_ppid"]).ne("")
    rows["사전지정"] = np.where(has_pre, "O", "")

    # --- 3) EIN / ECN / RCS 규제 -------------------------------------------
    rows = _fab_join_pems(rows, df_pems, df_sel)

    # --- 4) SKIP -----------------------------------------------------------
    rows["skiprule"] = _fab_str(rows["skiprule"])
    rows = _fab_skip(rows)

    # --- 5) 연속 -----------------------------------------------------------
    rows["_delay"] = _fab_delay_norm(rows["delaytime"])
    rows["_metro"] = rows["areaname"].isin(FAB_METRO)
    rows["_main_non_metro"] = rows["stepseq_type"].eq("메인") & (~rows["_metro"])
    # groupby.apply 는 반환 모양이 상황따라 달라진다. 조각을 직접 모은다.
    eff = pd.Series("", index=rows.index, dtype=object)
    for _, idx in rows.groupby("lot_id", sort=False).indices.items():
        g = rows.iloc[idx]
        eff.iloc[idx] = _fab_effective_delay(g).to_numpy()
    rows["_eff"] = eff
    rows["_core"] = (rows["_eff"].ne("") & (~rows["_eff"].isin(FAB_SENTINEL))
                     & rows["SKIP"].ne("O"))
    rows = _fab_continuous(rows)

    # --- 6) 현스텝 ~ 연속끝만 남긴다 ----------------------------------------
    keep = []
    for _, idx in rows.groupby("lot_id", sort=False).indices.items():
        sub = rows.iloc[idx]
        # 현스텝이 SKIP 이면 그 뒤 첫 비SKIP 을 현스텝으로 삼는다.
        ok = sub.index[sub["SKIP"].ne("O")]
        if len(ok) == 0:
            keep.append(sub.index[0])
            continue
        head = ok[0]
        keep.append(head)
        if rows.at[head, "연속"] in ("연속첫", "연속"):
            for ii in ok[1:]:
                if rows.at[ii, "연속"] in ("연속첫", "연속"):
                    keep.append(ii)
                else:
                    break
    out = rows.loc[sorted(set(keep))].copy()

    # --- 결과를 narrow_step_to_scope 모양으로 --------------------------------
    #   de_rank : 연속첫 누적 개수. 기존과 같은 뜻이 되게 맞춘다.
    out = out.sort_values(["lot_id", "_ord"], kind="mergesort")
    first = out["연속"].eq("연속첫").astype(int)
    out["de_rank"] = first.groupby(out["lot_id"]).cumsum()

    # recipe : 사전지정 > PEMS > 원래 값
    rec = _fab_str(out["recipeid"])
    pem = _fab_str(out.get("pems_ppid", pd.Series("", index=out.index)))
    pre = _fab_str(out["_pre_ppid"])
    out["recipe_id"] = np.where(pre.ne(""), pre,
                                np.where(pem.ne(""), pem, rec))

    # 설비 : 사전지정이 있으면 그 설비만, PEMS 지정이 있으면 그 목록만
    grp = _fab_str(out.get("eqpgroup", pd.Series("", index=out.index)))
    lim = _fab_str(out["_pre_eqp"])
    pe = _fab_str(out.get("pems_chamberids", pd.Series("", index=out.index)))
    pe = pe.mask(pe.eq(""), _fab_str(
        out.get("pems_eqpids", pd.Series("", index=out.index))))
    eqp_raw = np.where(lim.ne(""), lim, np.where(pe.ne(""), pe, grp))

    return pd.DataFrame({
        "line": line,
        "lot_id": out["lot_id"].to_numpy(),
        "proc_id": out["proc_id"].to_numpy(),
        # order_seq 는 FabPlan 판별 기준이라 비워 둔다. 대신 공정 내 순번을 쓴다.
        "order_seq": pd.NA,
        "de_rank": out["de_rank"].to_numpy(),
        "연속": out["연속"].to_numpy(),
        "layer_id": out["layerid"].to_numpy(),
        "step_level": pd.NA,
        "ein": out.get("적용PEMSNO", pd.Series(pd.NA, index=out.index)).to_numpy(),
        "step_seq": out["stepseq"].to_numpy(),
        "step_desc": out["descript"].to_numpy(),
        "eqp_type": out["eqptype"].to_numpy(),
        "eqp_group_raw": eqp_raw,
        "recipe_id": out["recipe_id"].to_numpy(),
        "_fab_cur": out["step_seq"].to_numpy(),   # 현스텝 표시에 쓴다
    })


def _fab_join_pems(rows, df_pems, df_sel):
    """EIN / ECN / RCS 를 붙인다. 여럿이면 연결 규칙으로 하나를 고른다."""
    cols = ["적용PEMSNO", "connecttype", "nextstepseq",
            "pems_eqpids", "pems_chamberids", "pems_ppid"]
    B = _lower_cols(df_pems)
    if B.empty:
        for c in cols:
            rows[c] = pd.NA
        return rows
    for c in ("processid", "pems_type", "ecnrule", "lotids", "einecnno",
              "stepseq", "connecttype", "nextstepseq", "pems_eqpids",
              "pems_chamberids", "pems_ppid"):
        if c not in B.columns:
            B[c] = pd.NA
        B[c] = _fab_str(B[c]) if B[c].dtype == object else B[c]
    for c in ("processid", "pems_type", "ecnrule", "lotids", "einecnno",
              "stepseq"):
        B[c] = _fab_str(B[c])
    B = B.reset_index(drop=True)
    B["_b"] = np.arange(len(B))

    A = rows.reset_index(drop=True).copy()
    A["_aid"] = np.arange(len(A))
    # lot_id 의 '.' 바로 앞 한 글자. ECN 규칙 키다.
    A["_pre"] = A["lot_id"].str.extract(r"(.)\.", expand=False).fillna("")

    parts = []
    ecn = B[B["pems_type"].isin(["ECN", "RCS"])]
    allr = ecn[ecn["ecnrule"].eq("-") | ecn["ecnrule"].eq("")]
    if not allr.empty:
        parts.append(A.merge(allr, left_on=["proc_id", "stepseq"],
                             right_on=["processid", "stepseq"], how="inner"))
    spec = ecn[~(ecn["ecnrule"].eq("-") | ecn["ecnrule"].eq(""))].copy()
    if not spec.empty:
        spec["_k"] = spec["ecnrule"].str.replace(" ", "", regex=False).str.split(",")
        spec = spec.explode("_k")
        spec["_k"] = _fab_str(spec["_k"])
        parts.append(A.merge(spec, left_on=["proc_id", "stepseq", "_pre"],
                             right_on=["processid", "stepseq", "_k"],
                             how="inner"))
    ein = B[B["pems_type"].eq("EIN")].copy()
    if not ein.empty:
        ein["_l"] = ein["lotids"].str.split(",")
        ein = ein.explode("_l")
        ein["_l"] = _fab_str(ein["_l"])
        parts.append(A.merge(ein, left_on=["proc_id", "stepseq", "lot_id"],
                             right_on=["processid", "stepseq", "_l"],
                             how="inner"))

    if not parts:
        for c in cols:
            rows[c] = pd.NA
        return rows

    cand = pd.concat(parts, ignore_index=True)
    cand = cand.sort_values(["_aid", "_b"], kind="mergesort")

    C = _lower_cols(df_sel)
    sel = {}
    if not C.empty and "firsteinecnno" in C.columns:
        for a, b, c in zip(_fab_str(C["firsteinecnno"]),
                           _fab_str(C["nexteinecnno"]),
                           _fab_str(C["selecteinecnno"])):
            sel[(a, b)] = c

    def _pick(g):
        if len(g) == 1:
            return g.iloc[0]
        w = g.iloc[0]
        for i in range(1, len(g)):
            c = g.iloc[i]
            got = sel.get((w["einecnno"], c["einecnno"]))
            if got is None:
                got = sel.get((c["einecnno"], w["einecnno"]))
            if got == c["einecnno"]:
                w = c
        return w

    # groupby.apply 는 키를 인덱스로 올리기도 한다. 조각을 직접 고른다.
    take = ["_aid", "einecnno", "connecttype", "nextstepseq",
            "pems_eqpids", "pems_chamberids", "pems_ppid"]
    picked = pd.DataFrame(
        [ _pick(cand.iloc[idx])[take] for _, idx in
          cand.groupby("_aid", sort=False).indices.items() ],
        columns=take)
    picked = picked.rename(columns={"einecnno": "적용PEMSNO"})
    out = A.merge(picked, on="_aid", how="left").drop(columns=["_aid", "_pre"])
    return out


def expand_with_equipment(scope, df_eqp, df_eqp_group, line):
    """축약된 scope 에만 설비그룹·설비를 전개한다(전체 경로 전개 없음)."""
    eg = _lower_cols(df_eqp_group)
    eg = eg[eg["line_id"].eq(line)]
    eg = eg[["line_id", "eqp_group_name", "eqp_id"]].drop_duplicates()
    eg = _drop_null_keys(eg, ["line_id", "eqp_group_name"])

    # 그룹명이 구성설비를 축약 나열한 형태일 때, 복원 결과에 없는 설비가 섞여
    # 있으면 옛 스냅샷 잔존을 의심해야 한다(파티션 키 오류의 대표 증상).
    named = eg["eqp_group_name"].str.contains("_", na=False)
    if WARN_STRAY_GROUP_MEMBER and named.any():
        chk = eg[named]
        stray = [(n, e) for n, e in zip(chk["eqp_group_name"], chk["eqp_id"])
                 if e.upper() not in {x.upper() for x in expand_group_name(n)}]
        if stray:
            print(f"[WARN] {line} 설비그룹명에서 복원되지 않는 구성설비 "
                  f"{len(stray):,}건 (옛 스냅샷 잔존 의심). 예: {stray[:3]}", flush=True)

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
    out["AREA"] = area_of(out.get("eqp_group_raw", out.get("eqp_id")))
    return out


# ---------------------------------------------------------------------------
# 2. hold
# ---------------------------------------------------------------------------
def build_hold(df_hold, lot_ids=None):
    """FAB_ISSUE_LOT → h1/h2/h3.

    기존 Oracle: status_seq <> '2' 필터 후
      (line_id, lot_id, step_seq, item_type) 별 최신 hold_date 1건 →
      item_type 그룹별로 (line_id, lot_id, step_seq) 당 1건만 남김.
    """
    h = _lower_cols(df_hold)

    # version_desc 는 적재 ID(스냅샷). 최신 스냅샷으로 먼저 좁힌 뒤 status_seq 를 건다.
    # 최신 스냅샷에 없는 lot 의 issue 는 이미 조치완료되어 사라진 것이므로
    # 옛 스냅샷에서 되살리지 않는다.
    # (서버측 필터를 쓰면 이미 적용돼 있고, 생테이블 폴백이면 여기서 적용된다)
    if "version_desc" in h.columns:
        latest = h["version_desc"].max()
        before = len(h)
        h = h[h["version_desc"].eq(latest)]
        print(f"[HOLD] 최신 version_desc={latest} 적용: {before:,} -> {len(h):,}행",
              flush=True)

    h = h[~h["status_seq"].isin(HOLD_EXCLUDE_STATUS_SEQ)]
    if lot_ids is not None:
        # 서버에서 mc_lot 조인을 생략했으므로 여기서 재공 lot 으로 좁힌다.
        before = len(h)
        h = h[h["lot_id"].isin(lot_ids)]
        print(f"[HOLD] 재공 lot 한정: {before:,} -> {len(h):,}행", flush=True)
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
            m.start_date, m.last_event_date, m.step_arrive_date, m.last_tkout_date,
            m.fa_object4, m.prod1, m.prod2, m.dept, m.dest_line_id,
            m.status, m.order_seq AS m_order_seq, m.step_seq AS m_step_seq,
            s.proc_id, s.order_seq, s.de_rank, s."연속", s.AREA,
            s.layer_id, s.step_level, s.ein, s.step_seq, s.step_desc,
            s.eqp_type, s.recipe_id, s.eqp_group_raw, s.eqp_id, s.batch_kind, s.eqpline,
            s.body_status, s.eqp_status_change_time AS s_eqp_status_change_time,
            -- FabPlan(order_seq 없음)은 step_seq 로 현스텝을 가린다.
            CASE
              WHEN m.order_seq IS NOT NULL AND s.order_seq IS NOT NULL
               AND m.order_seq = s.order_seq THEN '현스텝'
              WHEN m.order_seq IS NULL AND s.order_seq IS NULL
               AND m.step_seq = s.step_seq THEN '현스텝'
            END AS "현스텝"
        FROM m
        LEFT JOIN s ON m.line = s.line AND m.lot_id = s.lot_id
    """)

    # Tip rule 의 '-' 는 와일드카드(해당 키를 따지지 않음) 표기다.
    # lot_type 도 예외가 아니다. 원본 Oracle tip 쿼리는 lot_type 을 항상 '-' 로
    # 내보내는데, 조인을 ms.lot_type = t.lot_type 등호로 걸면 lot 의 실제
    # lot_type('PP','PG'...)과 절대 일치하지 않아 tip 이 전부 비게 된다.
    # 또한 (t.col='-' OR ms.col=t.col) 형태는 non-equi 조인이라 nested loop 으로
    # 떨어지므로, 와일드카드 조합(mask)별로 나눠 순수 equi-join 으로 처리한다.
    WC = [("lot_type", "lot_type"), ("process", "proc_id"), ("step", "step_seq"),
          ("ppid", "recipe_id"), ("eqpid", "eqp_id")]

    wc_expr = " + ".join(
        f"CASE WHEN COALESCE(NULLIF(TRIM(CAST({tc} AS VARCHAR)), ''), '-') = '-' "
        f"THEN {1 << i} ELSE 0 END"
        for i, (tc, _) in enumerate(WC)
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE t0 AS
        SELECT *, ({wc_expr}) AS wc_mask FROM t
    """)

    tm_cols = f"""ms.ms_row_id, t0.wc_mask,
                 t0.lot_type AS rule_lot_type, t0.process AS rule_process,
                 t0.step AS rule_step, t0.ppid AS rule_ppid,
                 CAST(t0.eqpid AS VARCHAR) AS eqpid, CAST(t0.eqpcham AS VARCHAR) AS eqpcham,
                 CAST(t0.prevent AS VARCHAR) AS prevent,
                 CAST(t0.type_body AS VARCHAR) AS type_body,
                 CAST(t0.type_cham AS VARCHAR) AS type_cham,
                 {parsed_ts('t0.tip_eventtime')} AS tip_eventtime,
                 CAST(t0.eqpissue AS VARCHAR) AS eqpissue,
                 CAST(t0.body_eqp_status AS VARCHAR) AS body_eqp_status,
                 CAST(t0.cham_eqp_status AS VARCHAR) AS cham_eqp_status,
                 {parsed_ts('t0.eqpissuetime')} AS eqpissuetime,
                 CASE WHEN t0.wc_mask = 0 THEN '정확' ELSE 'wildcard' END AS match_type"""

    con.execute(f"CREATE OR REPLACE TABLE t_matches_raw AS {' '.join(['SELECT', tm_cols, 'FROM ms_joined ms JOIN t0 ON FALSE'])}")

    masks = [r[0] for r in con.execute(
        "SELECT DISTINCT wc_mask FROM t0 ORDER BY wc_mask").fetchall()]
    for mask in masks:
        on = ["ms.line = t0.line"]
        for i, (tc, mc) in enumerate(WC):
            if not (mask >> i) & 1:
                on.append(f"ms.{mc} = t0.{tc}")
        n_rules = con.execute(
            "SELECT COUNT(*) FROM t0 WHERE wc_mask = ?", [mask]).fetchone()[0]
        wild = [tc for i, (tc, _) in enumerate(WC) if (mask >> i) & 1]
        print(f"[TIP] wc_mask={mask:02d} rules={n_rules:,} wildcard={wild or '없음(정확매칭)'}",
              flush=True)
        con.execute(f"""
            INSERT INTO t_matches_raw
            SELECT {tm_cols}
            FROM ms_joined ms
            JOIN (SELECT * FROM t0 WHERE wc_mask = {mask}) t0
              ON {' AND '.join(on)}
        """)
    print(f"[TIP] 매칭 후보 = "
          f"{con.execute('SELECT COUNT(*) FROM t_matches_raw').fetchone()[0]:,}행", flush=True)

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
            CAST(tm.prevent AS VARCHAR) AS prevent, CAST(tm.type_body AS VARCHAR) AS type_body,
            CAST(tm.type_cham AS VARCHAR) AS type_cham, {parsed_ts('tm.tip_eventtime')} AS tip_eventtime,
            COALESCE(CAST(tm.eqpissue AS VARCHAR),
                     CASE WHEN ms.body_status IN ('LOCAL','PM','DOWN')
                          THEN CAST(ms.body_status AS VARCHAR) END)    AS eqpissue,
            COALESCE(CAST(tm.body_eqp_status AS VARCHAR),
                     CAST(ms.body_status AS VARCHAR))                  AS body_eqp_status,
            CAST(tm.cham_eqp_status AS VARCHAR)                        AS cham_eqp_status,
            COALESCE({parsed_ts('tm.eqpissuetime')},
                     {parsed_ts('ms.s_eqp_status_change_time')})   AS eqpissuetime,
            COALESCE(CAST(ms.eqp_id AS VARCHAR), CAST(tm.eqpid AS VARCHAR))   AS eqpid,
            COALESCE(CAST(tm.eqpcham AS VARCHAR), CAST(ms.eqp_id AS VARCHAR)) AS eqpcham2,
            h1.hold_user AS hold, h1.hold_reason AS hold_reason, h1.hold_date AS hold_date,
            h2.hold_user AS exception, h2.hold_reason AS exception_reason,
            h2.hold_date AS exception_date,
            h3.hold_user AS ftp, h3.hold_reason AS ftp_reason, h3.hold_date AS ftp_date,
            CASE WHEN CAST(tm.prevent AS VARCHAR) = 'PREVENT' OR tm.eqpissue IS NOT NULL
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
                   FILTER (WHERE eqpcham_final IS NOT NULL) AS eqpgroup_cham_raw,
               STRING_AGG(DISTINCT CAST(batch_kind AS VARCHAR), ', ' ORDER BY CAST(batch_kind AS VARCHAR))
                   FILTER (WHERE batch_kind IS NOT NULL) AS batch_kind_agg
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
                   -- 가상스텝은 설비를 기다리는 게 아니라 진행이 막힌 것이다.
                   -- 설비그룹 / recipe / step 어디든 WAIT 가 들어가거나,
                   -- 설비그룹이 NRDSEND · NRDMEAS 처럼 실제 설비가 아니면 가상스텝.
                   WHEN fb."현스텝" = '현스텝' AND fb.status = 'WAIT'
                    AND (UPPER(COALESCE(fb.eqp_group_raw, '')) LIKE '%WAIT%'
                      OR UPPER(COALESCE(fb.eqp_id, '')) LIKE '%WAIT%'
                      OR UPPER(COALESCE(fb.recipe_id, '')) LIKE '%WAIT%'
                      OR UPPER(COALESCE(fb.step_seq, '')) LIKE '%WAIT%'
                      OR UPPER(COALESCE(fb.step_desc, '')) LIKE '%WAIT%'
                      OR UPPER(TRIM(COALESCE(fb.eqp_group_raw, '')))
                         IN ('NRDSEND', 'NRDMEAS'))
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
            {elapsed_days_num('fsb.last_tkout_date')}   AS "마지막작업경과_일",
            fc.current_de_rank, fc.current_continuous,
            fg.eqpgroup, fg.batch_kind_agg,
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
                   (CASE WHEN type_body = 'PREVENT' THEN CAST(eqpid AS VARCHAR)
                         WHEN type_cham = 'PREVENT' THEN CAST(eqpcham_final AS VARCHAR)
                         ELSE CAST(COALESCE(eqpid, eqpcham_final) AS VARCHAR) END)
                   || COALESCE('(' || {elapsed_days_text('tip_eventtime')} || ')', '') AS label
            FROM f1 WHERE prevent = 'PREVENT'
              AND COALESCE(CAST(CASE WHEN type_body='PREVENT' THEN eqpid
                                     WHEN type_cham='PREVENT' THEN eqpcham_final
                                     ELSE COALESCE(eqpid, eqpcham_final) END AS VARCHAR), '') <> ''
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
                       (CASE WHEN body_eqp_status IN ('LOCAL','DOWN','PM') THEN CAST(body_eqp_status AS VARCHAR)
                             WHEN cham_eqp_status IN ('LOCAL','DOWN','PM') THEN CAST(cham_eqp_status AS VARCHAR)
                             ELSE CAST(eqpissue AS VARCHAR) END) AS issue_group,
                       (CASE WHEN body_eqp_status IN ('LOCAL','DOWN','PM') THEN CAST(eqpid AS VARCHAR)
                             WHEN cham_eqp_status IN ('LOCAL','DOWN','PM') THEN CAST(eqpcham_final AS VARCHAR)
                             ELSE CAST(COALESCE(eqpcham_final, eqpid) AS VARCHAR) END)
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
            f."마지막작업경과_일", f.fa_object4, f.prod1, f.prod2, f.dept,
            f.dest_line_id,
            CAST(NULL AS VARCHAR) AS module1,
            CAST(NULL AS VARCHAR) AS module2,
            CAST(NULL AS VARCHAR) AS cause_detail,
            f.lot_status, f.step_status, f.proc_id, f.de_rank, f."연속",
            f.AREA, f.layer_id, f."현스텝", f.order_seq, f.step_seq, f.step_desc,
            f.recipe_id, f.eqp_type, {'f.batch_kind_agg' if AGGREGATE_BATCH_KIND else 'CAST(f.batch_kind AS VARCHAR)'} AS batch_kind,
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
    df_f3 = con.execute(f"""
        SELECT
{col_list()}
        FROM f3
        ORDER BY line, lot_id, TRY_CAST(order_seq AS BIGINT) NULLS LAST,
                 order_seq, eqpgroup_cham
    """).df()

    # tip 조인 검증표
    #   매칭된 행만 담으면 "안 붙은 lot" 을 검색해도 안 나와 검증이 불가능하다.
    #   f3 범위의 (lot, step, eqp) 전 행을 LEFT JOIN 으로 남기고 매칭여부를 표기한다.
    tip_match = con.execute("""
        SELECT ms.line, ms.lot_id, ms.lot_type, ms.order_seq, ms.step_seq,
               ms.proc_id, ms.recipe_id, ms.eqp_group_raw, ms.eqp_id,
               CASE WHEN tm.ms_row_id IS NULL THEN '미매칭' ELSE '매칭' END AS 매칭여부,
               tm.match_type, tm.wc_mask,
               tm.rule_lot_type, tm.rule_process, tm.rule_step, tm.rule_ppid,
               tm.eqpid AS rule_eqpid,
               tm.prevent, tm.type_body, tm.type_cham, tm.tip_eventtime,
               tm.eqpissue, tm.eqpissuetime
        FROM ms_joined ms
        LEFT JOIN t_matches tm ON ms.ms_row_id = tm.ms_row_id
        ORDER BY ms.line, ms.lot_id, ms.order_seq, ms.eqp_id
    """).df()

    # eqpgroup 출처 추적표
    #   eqpgroup = STRING_AGG(DISTINCT eqpid) 이고 eqpid = COALESCE(설비그룹 eqp_id,
    #   tip rule 의 eqpid) 다. 즉 설비그룹에 매칭되는 설비가 없으면 tip rule 이
    #   물고 온 설비가 eqpgroup 에 그대로 들어간다. 이 표로 어느 쪽에서 온 값인지
    #   행 단위로 확인할 수 있다.
    eqpgroup_trace = con.execute("""
        SELECT fb.line, fb.lot_id, fb.order_seq, fb.proc_id, fb.step_seq,
               fb.recipe_id, fb.eqp_group_raw,
               fb.eqpid                                    AS eqpgroup_구성설비,
               fb.eqp_id                                   AS 설비그룹에서_온값,
               CASE WHEN fb.eqp_id IS NOT NULL THEN '설비그룹'
                    WHEN fb.eqpid  IS NOT NULL THEN 'tip rule'
                    ELSE '없음' END                        AS 출처,
               fb.eqpcham_final, fb.batch_kind, fb.body_status,
               fb.prevent, fb.eqpissue,
               fg.eqpgroup                                 AS 최종_eqpgroup
        FROM f1_base fb
        LEFT JOIN f1_groups fg
          ON fb.line = fg.line AND fb.lot_id = fg.lot_id
         AND fb.order_seq = fg.order_seq
        ORDER BY fb.line, fb.lot_id, fb.order_seq, fb.eqpid
    """).df()

    return df_f3, tip_match, eqpgroup_trace


# ---------------------------------------------------------------------------
# 진단 / 저장
# ---------------------------------------------------------------------------
def diagnose_duplicates(df_f3):
    """(line, lot_id, order_seq) 가 같은데 여러 행으로 나온 건을 찾고,
    어떤 컬럼 때문에 갈라졌는지 알려준다."""
    key = ["line", "lot_id", "order_seq"]
    dup = df_f3[df_f3.duplicated(subset=key, keep=False)]
    if dup.empty:
        return dup, []
    varying = []
    for c in df_f3.columns:
        if c in key:
            continue
        if dup.groupby(key, dropna=False)[c].nunique(dropna=False).max() > 1:
            varying.append(c)
    return dup.sort_values(key, kind="mergesort"), varying


def diagnose_eqp_group_mix(df_eqp_group, df_eqp, used_groups=None):
    """한 설비그룹 안에 batch 설비와 single 설비가 섞여 있는지 점검한다.

    실제 공정상 불가능한 조합이므로, 나온다면 원인은 둘 중 하나다.
      (a) 원천 데이터 자체가 그렇게 등록돼 있다
      (b) eqp_group 의 eqp_id 가 equipment 에 없어 batch_kind 가 NULL 이 됐다
    두 경우를 구분할 수 있도록 matched 여부를 함께 표기한다.
    """
    eg = _lower_cols(df_eqp_group)
    eg = eg[~eg["eqp_id"].str.contains("OFF", na=False)]
    if used_groups is not None:
        # f3 결과에 실제로 등장한 설비그룹만 본다. 쓰이지도 않는 그룹까지 넣으면
        # 검증에 노이즈가 된다.
        eg = eg[eg["eqp_group_name"].isin(set(used_groups))]
    eg = eg[["line_id", "eqp_group_name", "eqp_id"]].drop_duplicates()

    eq = _lower_cols(df_eqp)
    eq = eq[["line_id", "eqp_id", "batch_kind", "tool_kind", "eqp_status"]].drop_duplicates()

    j = eg.merge(eq, on=["line_id", "eqp_id"], how="left")
    j["batch_kind_표기"] = j["batch_kind"].fillna("(equipment 미존재)")
    j["구분"] = np.where(
        j["batch_kind"].isin(BATCH_KINDS), "BATCH",
        np.where(j["batch_kind"].isna(), "미매칭", "SINGLE"))

    key = ["line_id", "eqp_group_name"]
    kinds = j.groupby(key, dropna=False)["구분"].nunique()
    mixed = kinds[kinds > 1].index
    out = j.set_index(key).loc[mixed].reset_index() if len(mixed) else j.iloc[0:0]
    return out.sort_values(key + ["eqp_id"], kind="mergesort")


def _sheet_name(name):
    bad = set('[]:*?/\\')
    clean = "".join(ch for ch in str(name) if ch not in bad)
    return clean[:31] or "sheet"


def _excel_writer(path):
    """xlsxwriter 가 있으면 그것을 쓴다. openpyxl 은 전 셀을 객체로 들고 있어
    수십만 행에서 수 분이 걸리고 MemoryError 도 난다."""
    try:
        import xlsxwriter  # noqa: F401
        # constant_memory 옵션은 쓰지 않는다. pandas 의 셀 기록 순서와 맞지 않아
        # 헤더만 남고 데이터가 통째로 유실된다(실측 확인).
        return pd.ExcelWriter(path, engine="xlsxwriter")
    except ImportError:
        print("[WARN] xlsxwriter 미설치 → openpyxl 사용(느림). "
              "pip install xlsxwriter 권장", flush=True)
        return pd.ExcelWriter(path, engine="openpyxl")


def _write_sheets(xw, sheets):
    """행수가 시트 한계를 넘으면 name_1, name_2 ... 로 나눠 적재."""
    for name, df in sheets.items():
        if df is None or not len(df):
            continue
        safe = _excel_safe(df)
        cap = RAW_SHEET_MAX_ROWS.get(name)
        if cap is not None and len(safe) > cap:
            safe = safe.head(cap)
        n = len(safe)
        if n <= RAW_SHEET_ROWS_PER_SHEET:
            safe.to_excel(xw, sheet_name=_sheet_name(name), index=False)
            continue
        for i in range(0, n, RAW_SHEET_ROWS_PER_SHEET):
            part = i // RAW_SHEET_ROWS_PER_SHEET + 1
            safe.iloc[i:i + RAW_SHEET_ROWS_PER_SHEET].to_excel(
                xw, sheet_name=_sheet_name(f"{name}_{part}"), index=False)


def save_result_workbook(path, df_f3, load_log, dup_rows, dup_cols, mixed_groups,
                         tip_match, eqpgroup_trace=None):
    """결과·진단 시트만 담은 본 파일. 크기가 작아 실패 위험이 없다."""
    with _excel_writer(path) as xw:
        _excel_safe(df_f3).to_excel(xw, sheet_name="f3", index=False)
        pd.DataFrame(load_log).to_excel(xw, sheet_name="로딩시각", index=False)

        if len(mixed_groups):
            _excel_safe(mixed_groups).to_excel(
                xw, sheet_name="설비그룹_batch혼재", index=False)

        if len(dup_rows):
            note = pd.DataFrame({"중복유발_컬럼": dup_cols or ["(없음)"]})
            note.to_excel(xw, sheet_name="중복진단", index=False)
            _excel_safe(dup_rows).to_excel(
                xw, sheet_name="중복진단", index=False, startrow=len(note) + 2)

        _write_sheets(xw, {"tip_join_검증": tip_match,
                           "eqpgroup_출처추적": eqpgroup_trace})


def save_raw_workbook(path, raw_samples, extra_sheets):
    """원본테이블 검증 파일. openpyxl 은 전 셀을 메모리에 들고 있어 대용량에서
    MemoryError 가 난다. 본 파일과 분리해 두어 실패해도 결과가 보존되게 한다."""
    with _excel_writer(path) as xw:
        _write_sheets(xw, {**(extra_sheets or {}), **raw_samples})


# ---------------------------------------------------------------------------
def main():
    s3_manifest_log = []
    if SOURCE == "bdq":
        from bigdataquery import getData
    else:
        import s3_source

        man = s3_source.read_manifest()
        if not man:
            print("[SKIP] S3 매니페스트가 없습니다. Spotfire 업로드를 먼저 "
                  "확인하세요.", flush=True)
            return

        # 함수를 나눠 올리면 매니페스트가 회차 중간에도 존재한다.
        # 9개가 모두 채워지기 전에는 처리하지 않는다.
        have = set((man.get("tables") or {}).keys())
        missing = [t for t in s3_source.TABLES if t not in have]
        if missing and "--force" not in sys.argv:
            print(f"[SKIP] 이번 회차가 아직 완결되지 않았습니다. "
                  f"미도착 {len(missing)}개: {', '.join(missing)}", flush=True)
            return

        fin = str(man.get("finished_at", ""))
        if SKIP_IF_NOT_FRESH and "--force" not in sys.argv:
            prev = ""
            if os.path.exists(_FRESH_MARK):
                with open(_FRESH_MARK, encoding="utf-8") as f:
                    prev = f.read().strip()
            if fin and fin == prev:
                print(f"[SKIP] 이미 처리한 회차입니다 (finished_at={fin}). "
                      f"Spotfire 가 아직 새로 올리지 않았습니다.", flush=True)
                return
            print(f"[S3] 새 회차 감지 finished_at={fin} "
                  f"(이전 {prev or '없음'})", flush=True)
            os.makedirs(os.path.dirname(_FRESH_MARK), exist_ok=True)
            with open(_FRESH_MARK, "w", encoding="utf-8") as f:
                f.write(fin)

        # 매니페스트에 S3 원천 8개의 조회시각/행수가 있다. 그대로 기록해
        # 다운로드 화면이 '내부 처리 단위' 가 아니라 실제 원천을 보여주게 한다.
        # --force 로 돌려도 남아야 하므로 위 분기 밖에 둔다.
        for tname, meta in (man.get("tables") or {}).items():
            s3_manifest_log.append({
                "테이블": tname, "구분": "원천",
                "로딩_시작시각": None, "로딩_종료시각": None, "소요_초": None,
                "행수": meta.get("rows"), "컬럼수": meta.get("cols"),
                "시트_기록행수": 0, "시트_잘림여부": "N",
                "원천조회시각": meta.get("query_time") or man.get("query_time"),
            })

    load_log = []
    s3_cache = {}          # STEP_PATH / TIP 은 두 라인이 한 파일이라 재사용
    raw_samples = {}

    @contextmanager
    def stage(name):
        """로컬 처리 구간도 기록한다.

        기존에는 getData 호출만 남겨서, 조회 사이의 로컬 연산이 '설명되지 않는
        공백' 으로 보였다(소요합 300초 vs 실제 경과 483초). 이제 계 행의
        소요합과 경과가 맞아떨어진다.
        """
        s0 = dt.datetime.now()
        t0 = perf_counter()
        print(f"[STAGE] {name} 시작", flush=True)
        yield
        secs = perf_counter() - t0
        load_log.append({
            "테이블": name, "구분": "처리",
            "로딩_시작시각": s0, "로딩_종료시각": dt.datetime.now(),
            "소요_초": round(secs, 3), "행수": None, "컬럼수": None,
            "시트_기록행수": 0, "시트_잘림여부": "N",
        })
        print(f"[STAGE] {name} {secs:.1f}s", flush=True)

    def fetch(name, sql, keep_sample=True):
        """원천 1건을 가져온다.

        SOURCE 에 따라 bdq(Impala 직접) 또는 S3(Spotfire 적재분) 를 읽는다.
        어느 쪽이든 컬럼은 소문자로 통일되므로 이후 전처리는 동일하다.
        """
        start = dt.datetime.now()
        t0 = perf_counter()
        print(f"[QUERY] {name} 조회 시작 {start:%Y-%m-%d %H:%M:%S}", flush=True)

        if SOURCE == "bdq":
            df = getData(param=sql, convert_type=True, verbose=True)
        else:
            s3name = S3_MAP.get(name)
            if not s3name:
                raise KeyError(f"S3_MAP 에 '{name}' 이 없습니다.")
            if s3name in s3_cache:
                df = s3_cache[s3name]
                print(f"[QUERY] {name} <- 캐시({s3name})", flush=True)
            else:
                df = s3_source.read_table(s3name)
                s3_cache[s3name] = df
            # 한 파일에 두 라인이 들어 있으면 해당 라인만 남긴다.
            # 라인 구분 컬럼명이 테이블마다 다르다(line / line_id).
            # 못 찾으면 조용히 통과해 다른 라인이 섞이므로 반드시 확인한다.
            line = name.split("_")[0]
            if line in ("KFR7", "PFR1"):
                lcol = next((c for c in ("line", "line_id", "sys_line_id")
                             if c in df.columns), None)
                if lcol is None:
                    raise KeyError(
                        f"{s3name} 에 라인 구분 컬럼이 없습니다 "
                        f"(line / line_id / sys_line_id). 컬럼: {list(df.columns)[:8]}")
                before = len(df)
                df = df[df[lcol].astype(str).str.upper() == line]
                print(f"[QUERY] {name} 라인분리({lcol}) {before:,} -> {len(df):,}",
                      flush=True)

        end = dt.datetime.now()
        secs = perf_counter() - t0
        n = len(df)

        # Oracle 이 언제 조회했는지. S3 적재 시각과 구분해서 보여준다.
        qt = None
        for c in df.columns:
            if str(c).lower() == "query_time":
                try:
                    qt = str(df[c].iloc[0]) if n else None
                except Exception:
                    qt = None
                break
        cap = RAW_SHEET_MAX_ROWS.get(name)
        sample_rows = (n if cap is None else min(n, cap)) if keep_sample else 0
        load_log.append({
            "테이블": name,
            "로딩_시작시각": start,
            "로딩_종료시각": end,
            "소요_초": round(secs, 3),
            "행수": n,
            "컬럼수": df.shape[1],
            "시트_기록행수": sample_rows,
            "시트_잘림여부": "Y" if sample_rows < n else "N",
            "구분": "조회", "원천조회시각": qt,
        })
        print(f"[QUERY] {name} rows={n:,} cols={df.shape[1]} {secs:.1f}s", flush=True)
        if keep_sample:
            # 원본 전체를 들고 있으면 메모리가 터지므로 시트 기록분만 복사해 둔다.
            raw_samples[name] = (df if cap is None else df.head(cap)).copy()
        return df

    # 스냅샷 시각은 '원천을 조회하기 시작한 시점' 이다. 파이프라인이 3분쯤
    # 걸리므로 적재 시점을 쓰면 그만큼 뒤로 밀려 shift 기준시각 판정이 어긋난다.
    run_at = dt.datetime.now().replace(microsecond=0)
    set_now(run_at)          # 경과일 기준 시각 고정 (UTC/로컬 혼선 방지)
    stamp = f"{run_at:%Y%m%d_%H%M%S}"

    with timer("소형 원천 조회"):
        # 진단 모드에서는 거르지 않은 원천도 함께 받아 둔다(BDQ 만 가능).
        df_lot_raw = None
        if TRACE_DROP and SOURCE == "bdq":
            df_lot_raw = fetch("lot_raw", lot_query_raw, keep_sample=False)
        df_lot = fetch("lot", lot_query)
        # 원천 그대로의 사본. 뒤 단계에서 줄어든 것을 재려면 이게 있어야 한다.
        #   (조인·필터를 거친 df_lot 과 비교하면 이미 사라진 뒤라 못 잡는다)
        df_lot_src = _lower_cols(df_lot).copy() if TRACE_DROP else None
        # dest_line_id 보정: bdq 경로나 옛 쿼리에는 이 컬럼이 없다.
        # f3 출력 컬럼이라 없으면 SQL 이 깨지므로 빈 값으로 채워 둔다.
        df_lot = _lower_cols(df_lot)
        if "dest_line_id" not in df_lot.columns:
            df_lot["dest_line_id"] = pd.NA
        if SOURCE != "bdq":
            # Oracle 은 datasource 가 달라 fa_object4 를 분리해 받는다.
            # 기존 Impala lot_query 는 이 컬럼을 포함했으므로 여기서 붙여준다.
            df_mws = fetch("materialworkstatus", None)
            with stage("fa_object4 결합"):
                # lot 당 여러 행이 온다(15만행 / 9천 lot). 임의로 첫 행을 남기면
                # 값이 빈 행이 뽑혀 fa_object4 가 통째로 NULL 이 된다.
                # 값이 있는 행을 우선한다.
                m = _lower_cols(df_mws)[["line", "lot_id", "fa_object4"]].copy()
                m["fa_object4"] = m["fa_object4"].replace("", pd.NA)
                m["_has"] = m["fa_object4"].notna()
                m = (m.sort_values("_has", ascending=False)
                       .drop_duplicates(subset=["line", "lot_id"], keep="first")
                       .drop(columns="_has"))
                before = len(df_lot)
                df_lot = _lower_cols(df_lot).merge(m, on=["line", "lot_id"], how="left")
                got = int(df_lot["fa_object4"].notna().sum())
                print(f"[JOIN] fa_object4 {got:,}/{before:,} lot 매칭", flush=True)
            # 제품구분(SSPS_PROD_NAME). a.id 는 lot_id 의 앞 2~4글자다.
            # 길이가 제각각이라 길이별로 나눠 붙이고, 더 구체적인(긴) 것을 우선한다.
            df_prod = fetch("ssps_prod_name", None)
            with stage("제품구분 결합"):
                df_lot = attach_prod(df_lot, df_prod)
                # 기준정보가 우선이다. SSPS 로 채운 뒤 덮어쓴다.
                df_lot = attach_std_product(df_lot)

        df_eqp = fetch("equipment", eqp_query)
        # 설비그룹은 하루에 한 번만 바뀌면 충분하다. 업무일(22시 기준) 단위로
        # 캐시해 두고, 'OFF' 제외까지 마친 상태로 저장해 재사용한다.
        import db_common as _DB
        bd_today = _DB.biz_date(run_at)

        def _load_eqp_group():
            raw = fetch("eqp_group", eqp_group_query)
            n0 = len(raw)
            out = _lower_cols(raw)
            out = out[~out["eqp_id"].str.contains("OFF", case=False, na=False)]
            print(f"[FILTER] eqp_group 'OFF' 설비 제외: {n0:,} -> {len(out):,}",
                  flush=True)
            return out

        df_eqp_group, from_cache = cached_daily("eqp_group", bd_today, _load_eqp_group)
        if from_cache:
            load_log.append({
                "테이블": "eqp_group", "로딩_시작시각": None, "로딩_종료시각": None,
                "소요_초": 0, "행수": len(df_eqp_group),
                "컬럼수": df_eqp_group.shape[1],
                "시트_기록행수": len(df_eqp_group), "시트_잘림여부": "N",
            })
            raw_samples["eqp_group"] = df_eqp_group.copy()

        if SOURCE != "bdq":
            # S3 raw 는 Oracle 쿼리에서 이미 최신 version_desc + status_seq
            # 필터까지 끝난 상태로 온다. 여기서 다시 조회할 것이 없다.
            df_hold = fetch("hold", None)
        elif HOLD_SERVER_SIDE_FILTER:
            # MAX(version_desc) 는 100만행 전수 스캔이라 느릴 수 있다.
            # version_desc 가 'YYYYMMDD-HHMMSS' 형태이므로 최근 며칠로 한정해
            # 먼저 시도하고, 값이 안 나오면 전체 범위로 되돌린다.
            lo = (run_at - dt.timedelta(days=HOLD_VERSION_LOOKBACK_DAYS)
                  ).strftime("%Y%m%d-000000")
            with stage("hold version_desc 조회"):
                mv = getData(param=hold_max_query_bounded.replace("{LO}", lo),
                             convert_type=True, verbose=False).iloc[0, 0]
                if not mv:
                    print("[HOLD] 범위 한정 조회 실패 → 전체 범위로 재시도", flush=True)
                    mv = getData(param=hold_max_query, convert_type=True,
                                 verbose=False).iloc[0, 0]
            print(f"[HOLD] 최신 version_desc = {mv}", flush=True)
            sql = (hold_query_no_join if HOLD_SKIP_MCLOT_JOIN
                   else hold_query_two_step).replace("{MV}", str(mv))
            df_hold = fetch("hold", sql)
        else:
            df_hold = fetch("hold", hold_query)

    # 원천에는 있는데 최종에 없다면 여기 조건 중 하나에 걸린 것이다.
    #   line = sys_line_id · lot_type IN (PP,PG,EG) · cur_line_id <> 'CHTV'
    if TRACE_LOT:
        _t = _lower_cols(df_lot)
        _t = _t[_t["lot_id"].astype("string").str.strip().eq(TRACE_LOT)]
        if len(_t):
            r0 = _t.iloc[0]
            print(f"[TRACE] 원천 lot 조건 점검  "
                  f"line={r0.get('line')} sys={r0.get('sys_line_id')} "
                  f"cur={r0.get('cur_line_id')} dest={r0.get('dest_line_id')} "
                  f"type={r0.get('lot_type')} status={r0.get('status')} "
                  f"order_seq={r0.get('order_seq')} step={r0.get('step_seq')}",
                  flush=True)
        else:
            print("[TRACE] 원천 lot 에 없음 (Oracle 쿼리 단계에서 탈락)",
                  flush=True)
    trace_lot("원천 lot", df_lot)
    s_parts = []
    for line, sql in (("KFR7", kfr7_step_path_query), ("PFR1", pfr1_step_path_query)):
        df_path = fetch(f"{line}_step_path", sql)
        with stage(f"{line} 범위축약"):
            scope = narrow_step_to_scope(df_path, df_lot, line)
        del df_path
        print(f"[ROWS] {line} scope(step) = {len(scope):,}", flush=True)
        trace_lot(f"{line} scope", scope)
        with stage(f"{line} 설비그룹전개"):
            s_parts.append(expand_with_equipment(scope, df_eqp, df_eqp_group, line))

    # FabPlan : PFR1 재공 중 order_seq 가 비어 있는 lot.
    #   StepPath 로는 스텝이 붙지 않아 공정 정의를 직접 훑는다.
    if FABPLAN:
        try:
            fab = {k: fetch(k, None) for k in
                   ("fab_step", "fab_pems", "fab_sel", "fab_skiprule",
                    "fab_engr")}
            for k, v in fab.items():
                print(f"[FABPLAN] {k:14s} {len(v):>8,}행  "
                      f"{list(v.columns)[:6]}", flush=True)
            # 대상 lot 수를 먼저 밝힌다. 0 이면 order_seq 판정을 봐야 한다.
            _m = _lower_cols(df_lot)
            _m = _m[_m["line"].eq("PFR1")]
            _o = pd.to_numeric(_m.get("order_seq"), errors="coerce")
            print(f"[FABPLAN] PFR1 lot {len(_m):,} 중 order_seq 없음 "
                  f"{int(_o.isna().sum()):,}", flush=True)
            with stage("FabPlan 범위축약"):
                fscope = fabplan_scope(fab["fab_step"], fab["fab_pems"],
                                       fab["fab_sel"], fab["fab_skiprule"],
                                       fab["fab_engr"], df_lot, "PFR1")
            del fab
            print(f"[ROWS] FabPlan scope(step) = {len(fscope):,}", flush=True)
            if len(fscope):
                with stage("FabPlan 설비그룹전개"):
                    s_parts.append(expand_with_equipment(
                        fscope.drop(columns=["_fab_cur"]),
                        df_eqp, df_eqp_group, "PFR1"))
        except Exception as e:
            # 원천이 아직 안 올라왔으면 기존 결과만으로 계속 간다.
            print(f"[FABPLAN] 건너뜀: {type(e).__name__}: {e}", flush=True)

    s = pd.concat(s_parts, ignore_index=True)
    print(f"[ROWS] s = {len(s):,}", flush=True)
    trace_lot("s(스텝 전체)", s)

    t_parts = []
    for line, sql in (("KFR7", kfr7_tip_query), ("PFR1", pfr1_tip_query)):
        df_tip = fetch(f"{line}_tip", sql)
        with stage(f"{line} tip 선필터"):
            tip_f = prefilter_tip(df_tip, s[s["line"].eq(line)])
        print(f"[FILTER] {line} tip {len(df_tip):,} -> {len(tip_f):,}", flush=True)
        del df_tip
        with stage(f"{line} tip 전처리"):
            t_parts.append(build_tip(tip_f, df_eqp, line))
        del tip_f
    t = pd.concat(t_parts, ignore_index=True)
    print(f"[ROWS] t = {len(t):,}", flush=True)

    with stage("hold 전처리"):
        holds = build_hold(df_hold, set(_lower_cols(df_lot)["lot_id"].dropna()))
    for k, v in holds.items():
        print(f"[ROWS] {k} = {len(v):,}", flush=True)

    con = duckdb.connect()
    con.register("m", _lower_cols(df_lot))
    con.register("s", s)
    con.register("t", t)
    for k, v in holds.items():
        con.register(k, v)

    with stage("f3 생성"):
        df_f3, tip_match, eqpgroup_trace = build_f3(con)
    trace_lot("f3 생성 직후", df_f3)
    if TRACE_DROP:
        with stage("빠진 lot 정리"):
            dump_dropped(df_lot_raw if df_lot_raw is not None
                         else df_lot_src, df_f3, base=df_lot)

    # 조인·집계는 원천 라인으로 끝내고, 라벨만 마지막에 바꾼다.
    if SOURCE != "bdq":
        with stage("라인 재분류(dest_line_id)"):
            df_f3 = relabel_lines(df_f3, df_lot)

    with stage("모듈 결합(기준정보)"):
        df_f3 = attach_module(df_f3)
    with stage("HOLD 유형 결합(기준정보)"):
        df_f3 = attach_holdtype(df_f3)

    n_tip = int(df_f3["tip"].notna().sum())
    print(f"[TIP] f3 tip 값 있는 행 = {n_tip:,} / {len(df_f3):,}", flush=True)
    print(f"[ROWS] f3 = {len(df_f3):,}  (lot {df_f3['lot_id'].nunique():,}개)", flush=True)

    dup_rows, dup_cols = diagnose_duplicates(df_f3)
    if len(dup_rows):
        print(f"[DUP] (line, lot_id, order_seq) 중복 {len(dup_rows):,}행 / "
              f"유발 컬럼: {dup_cols}", flush=True)
    else:
        print("[DUP] lot/step 중복 없음", flush=True)

    used_groups = set(s["eqp_group_raw"].dropna())
    mixed = diagnose_eqp_group_mix(df_eqp_group, df_eqp, used_groups)
    if len(mixed):
        n_grp = mixed[["line_id", "eqp_group_name"]].drop_duplicates().shape[0]
        n_unmatched = int((mixed["구분"] == "미매칭").sum())
        print(f"[MIX] f3 사용 설비그룹 {len(used_groups):,}개 중 "
              f"batch/single 혼재 {n_grp:,}개 "
              f"(equipment 미매칭 설비 {n_unmatched:,}건)", flush=True)
    else:
        print("[MIX] batch/single 혼재 설비그룹 없음", flush=True)

    # ---- DB 적재 (docs/common_conventions.md 참조) -------------------------
    if LOAD_TO_DB:
        try:
            import db_common as DB
            with timer("DB 적재"):
                conn = DB.connect()
                DB.ensure_f3_schema(conn, SUMMARY_OUTPUT_COLUMNS)
                DB.ensure_load_log_schema(conn)
                snapshot_at = run_at
                DB.load_f3_live(conn, df_f3, SUMMARY_OUTPUT_COLUMNS, snapshot_at)
                DB.load_f3_load_log(conn, snapshot_at,
                                    s3_manifest_log + load_log)
                print(f"[DB] f3_live 갱신 snapshot_at={snapshot_at} "
                      f"rows={len(df_f3):,}", flush=True)
                # 재공 추이는 스냅샷마다 남긴다. f3_history 는 SHIFT 당
                # 하나뿐이라 SHIFT 안의 변화를 볼 수 없다.
                try:
                    _bd = DB.biz_date(snapshot_at)
                    _sh = DB.shift_of(snapshot_at)
                    _n = DB.snap_wip_step(conn, snapshot_at, _bd, _sh)
                    print(f"[DB] f3_wip_step {_n:,}행 (snapshot={snapshot_at})",
                          flush=True)
                except Exception as e:
                    print(f"[DB] f3_wip_step 건너뜀: {type(e).__name__}: {e}",
                          flush=True)

                promoted = DB.promote_to_history(conn, SUMMARY_OUTPUT_COLUMNS)
                if promoted:
                    bd, sh, snap, dist = promoted
                    print(f"[DB] f3_history 적재 biz_date={bd} shift={sh} "
                          f"snapshot={snap} (기준시각과 {dist // 60}분 차)", flush=True)
                else:
                    print("[DB] f3_history 갱신 없음 "
                          "(이미 더 가까운 스냅샷이 적재돼 있음)", flush=True)
                conn.close()
        except Exception:
            # 조용히 넘어가면 '적재했는데 웹에 안 뜬다' 로 이어진다. 크게 알린다.
            import traceback
            print("=" * 70, flush=True)
            print("[ERROR] DB 적재 실패 — 웹에 데이터가 반영되지 않습니다.", flush=True)
            traceback.print_exc()
            print("확인: .env 의 HOLDWAITANAL_DB_* / pymysql 설치 여부", flush=True)
            print("=" * 70, flush=True)

    # ---- 엑셀 저장 (기본 비활성. 필요할 때만 SAVE_EXCEL=True) ---------------
    if SAVE_EXCEL:
        with timer("결과 엑셀 저장"):
            path = os.path.join(os.getcwd(), f"f3_{stamp}.xlsx")
            save_result_workbook(path, df_f3, load_log, dup_rows, dup_cols, mixed,
                                 tip_match, eqpgroup_trace)
        print(f"saved: {path} rows={len(df_f3):,}", flush=True)

    if SAVE_RAW_EXCEL:
        raw_path = os.path.join(os.getcwd(), f"raw_{stamp}.xlsx")
        try:
            with timer("원본검증 엑셀 저장"):
                save_raw_workbook(raw_path, raw_samples,
                                  {"t_rules(전처리된 tip)": t, "s(step_scope)": s})
            print(f"saved: {raw_path}", flush=True)
        except Exception as e:
            print(f"[WARN] 원본검증 파일 저장 실패({type(e).__name__}: {e}). "
                  f"RAW_SHEET_MAX_ROWS 를 줄여 재시도할 것.", flush=True)


if __name__ == "__main__":
    main()
