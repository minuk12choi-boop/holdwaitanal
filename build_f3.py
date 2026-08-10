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
                   OVER (PARTITION BY lot_id, line_id)             AS max_transn_time,
               SUM(CASE WHEN wip_attribute = 'FLOWLEVEL' THEN 1 ELSE NULL END)
                   OVER (PARTITION BY lot_id, step_seq, line_id)   AS flowlevel_cnt
        FROM   FAB.M_LOT_TRANSN_HIST
        WHERE  lot_transn_type = 'ModifyAttr'
          AND  wip_attribute IN ('GRADE')
          AND  line_id IN ('PFR1', 'KFR7')
    ) h
    WHERE  flowlevel_cnt IS NULL
      AND  max_transn_time = lot_transn_time
),

g AS (
    SELECT DISTINCT
           line_id,
           lot_id,
           new_attr_value AS grade
    FROM   t1
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
	NULL as fa_object4
    FROM        MOS_KH_SMI.SMICDC_P3NRD_MC_LOT m
    JOIN        m0
      ON        m.lot_id          = m0.lot_id
     AND        m.last_event_date = m0.max_event_date
    LEFT JOIN   g
      ON        m.lot_id  = g.lot_id
     AND        g.line_id = 'PFR1'
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
    LEFT JOIN   g
      ON        m.lot_id  = g.lot_id
     AND        g.line_id = 'KFR7'
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

# ── TrackInPrevent (Tip) : 실제 사용 컬럼만 조회 ─────────────────────────
kfr7_tip_query = """
SELECT process, step, ppid, eqpid, chamberid,
       type, checkcount, tkin_count, updated, eventtime
FROM   MOS_KH_SMI.SMICDC_NRDK_TRACKINPREVENT
WHERE  owner IN ('LEVEL1', 'PHOTO_LEVEL1')
"""

pfr1_tip_query = """
SELECT process, step, ppid, eqpid, chamberid,
       type, checkcount, tkin_count, updated, eventtime
FROM   MOS_KH_SMI.SMICDC_P3NRD_TRACKINPREVENT
WHERE  owner IN ('LEVEL1', 'PHOTO_LEVEL1')
"""

# ── Equipment : line_id + eqp_id 별 최신 적재분만 ────────────────────────
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

# ── 설비그룹 : line_id + eqp_group_name + eqp_id 별 최신 적재분만 ────────
eqp_group_query = """
SELECT line_id, eqp_group_name, eqp_id
FROM (
    SELECT g.line_id, g.eqp_group_name, g.eqp_id, g.impala_insert_time,
           MAX(g.impala_insert_time)
               OVER (PARTITION BY g.line_id, g.eqp_group_name, g.eqp_id)
               AS max_impala_insert_time
    FROM   MOS_KH_SMI.SMIMES_MI_EQP_GROUP_LIST g
    WHERE  g.line_id IN ('KFR7', 'PFR1')
) x
WHERE x.impala_insert_time = x.max_impala_insert_time
"""

# ── Step Path : 실제 사용 컬럼만 조회 (조건절 없이 생테이블) ─────────────
kfr7_step_path_query = """
SELECT lot_id, order_seq, proc_id, step_seq, step_desc, step_level,
       step_skip_yn, delay_step_type, delay_time_mins, layer_id,
       eqp_type, eqp_group_id, recipe_id, ext_1st_vals, tkin_type_detail
FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_PATH
"""

pfr1_step_path_query = """
SELECT lot_id, order_seq, proc_id, step_seq, step_desc, step_level,
       step_skip_yn, delay_step_type, delay_time_mins, layer_id,
       eqp_type, eqp_group_id, recipe_id, ext_1st_vals, tkin_type_detail
FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_PATH
"""

# =====================================================================
# Tip 전처리 (기존 Oracle tip_table_pfr1 / tip_table_kfr7 의 t~final 로직을
# pandas 로 재현. 사내 15분 호출제한 회피를 위해 SQL 은 생테이블만 조회하고
# 결합/연산은 python 단에서 수행한다.)
# =====================================================================
BATCH_KINDS = ('BATCH_FURNACE', 'BATCH_WET')
EQP_ISSUE_STATUS = ('LOCAL', 'PM', 'DOWN')


def _lower_cols(df):
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    return out


def _to_datetime(series):
    """'yyyymmdd hh24:mi:ss' 계열 문자열 → datetime. 구분자 유무에 관계없이 파싱."""
    s = series.astype('string').str.replace(r'[^0-9]', '', regex=True).str.slice(0, 14)
    s = s.where(s.str.len() >= 8).str.pad(14, side='right', fillchar='0')
    return pd.to_datetime(s, format='%Y%m%d%H%M%S', errors='coerce')


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
        'lot_type': '-',
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

# Oracle `step_skip_yn <> 'Y'` 는 NULL 행을 제외한다(NULL <> 'Y' 는 UNKNOWN).
# 재현 구현들은 NULL 을 포함해 왔다. 원본과 맞추려면 True 로 둔다.
EXCLUDE_NULL_STEP_SKIP_YN = True

# batch_kind 는 EQP 단위 값이라 한 설비그룹에 batch/비batch 설비가 섞이면
# 같은 lot·step 이 여러 행으로 갈라진다. eqpline 과 동일하게 step 단위로 합친다.
AGGREGATE_BATCH_KIND = True

# 원본테이블 검증 시트 설정
#   시트 1장당 행수 상한. Excel 한계(1,048,576)보다 낮게 잡아야 안전하다.
RAW_SHEET_ROWS_PER_SHEET = 500_000
#   테이블별 총 기록 행수. None = 전량.
#   step_path / tip 은 수백만 행이라 전량 기록 시 파일이 수 GB 가 되고 저장에만
#   수십 분이 걸린다. 필요할 때만 숫자를 올릴 것.
RAW_SHEET_MAX_ROWS = {
    "lot": None,
    "equipment": None,
    "eqp_group": None,
    "hold": None,
    "KFR7_step_path": 200_000,
    "PFR1_step_path": 200_000,
    "KFR7_tip": 200_000,
    "PFR1_tip": 200_000,
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

    tm_cols = """ms.ms_row_id, t0.wc_mask,
                 t0.lot_type AS rule_lot_type, t0.process AS rule_process,
                 t0.step AS rule_step, t0.ppid AS rule_ppid,
                 CAST(t0.eqpid AS VARCHAR) AS eqpid, CAST(t0.eqpcham AS VARCHAR) AS eqpcham,
                 CAST(t0.prevent AS VARCHAR) AS prevent,
                 CAST(t0.type_body AS VARCHAR) AS type_body,
                 CAST(t0.type_cham AS VARCHAR) AS type_cham,
                 CAST(t0.tip_eventtime AS TIMESTAMP) AS tip_eventtime,
                 CAST(t0.eqpissue AS VARCHAR) AS eqpissue,
                 CAST(t0.body_eqp_status AS VARCHAR) AS body_eqp_status,
                 CAST(t0.cham_eqp_status AS VARCHAR) AS cham_eqp_status,
                 CAST(t0.eqpissuetime AS TIMESTAMP) AS eqpissuetime,
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
            CAST(tm.type_cham AS VARCHAR) AS type_cham, CAST(tm.tip_eventtime AS TIMESTAMP) AS tip_eventtime,
            COALESCE(CAST(tm.eqpissue AS VARCHAR),
                     CASE WHEN ms.body_status IN ('LOCAL','PM','DOWN')
                          THEN CAST(ms.body_status AS VARCHAR) END)    AS eqpissue,
            COALESCE(CAST(tm.body_eqp_status AS VARCHAR),
                     CAST(ms.body_status AS VARCHAR))                  AS body_eqp_status,
            CAST(tm.cham_eqp_status AS VARCHAR)                        AS cham_eqp_status,
            COALESCE(CAST(tm.eqpissuetime AS TIMESTAMP),
                     CAST(ms.s_eqp_status_change_time AS TIMESTAMP))   AS eqpissuetime,
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

    # tip 조인 검증표: 어떤 rule 이 어떤 lot/step 에 어떤 근거로 붙었는지
    tip_match = con.execute("""
        SELECT ms.line, ms.lot_id, ms.lot_type, ms.order_seq, ms.step_seq,
               ms.proc_id, ms.recipe_id, ms.eqp_id,
               tm.match_type, tm.wc_mask,
               tm.rule_lot_type, tm.rule_process, tm.rule_step, tm.rule_ppid,
               tm.eqpid AS rule_eqpid,
               tm.prevent, tm.type_body, tm.type_cham, tm.tip_eventtime,
               tm.eqpissue, tm.eqpissuetime
        FROM t_matches tm
        JOIN ms_joined ms ON ms.ms_row_id = tm.ms_row_id
        ORDER BY ms.line, ms.lot_id, ms.order_seq, tm.match_type
    """).df()
    return df_f3, tip_match


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


def diagnose_eqp_group_mix(df_eqp_group, df_eqp):
    """한 설비그룹 안에 batch 설비와 single 설비가 섞여 있는지 점검한다.

    실제 공정상 불가능한 조합이므로, 나온다면 원인은 둘 중 하나다.
      (a) 원천 데이터 자체가 그렇게 등록돼 있다
      (b) eqp_group 의 eqp_id 가 equipment 에 없어 batch_kind 가 NULL 이 됐다
    두 경우를 구분할 수 있도록 matched 여부를 함께 표기한다.
    """
    eg = _lower_cols(df_eqp_group)
    eg = eg[~eg["eqp_id"].str.contains("OFF", na=False)]
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


def save_workbook(path, df_f3, load_log, raw_samples, dup_rows, dup_cols, mixed_groups,
                  extra_sheets=None):
    """f3 + 진단 + 로딩시각 + 원본테이블 시트를 하나의 엑셀 파일로 저장.

    원본테이블이 시트 한계를 넘으면 name_1, name_2 ... 로 나눠 적재한다.
    """
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
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

        for name, df in (extra_sheets or {}).items():
            raw_samples[name] = df

        for name, df in raw_samples.items():
            safe = _excel_safe(df)
            n = len(safe)
            if n <= RAW_SHEET_ROWS_PER_SHEET:
                safe.to_excel(xw, sheet_name=_sheet_name(name), index=False)
                continue
            for i in range(0, n, RAW_SHEET_ROWS_PER_SHEET):
                part = i // RAW_SHEET_ROWS_PER_SHEET + 1
                safe.iloc[i:i + RAW_SHEET_ROWS_PER_SHEET].to_excel(
                    xw, sheet_name=_sheet_name(f"{name}_{part}"), index=False)


# ---------------------------------------------------------------------------
def main():
    from bigdataquery import getData

    load_log = []
    raw_samples = {}

    def fetch(name, sql, keep_sample=True):
        start = dt.datetime.now()
        t0 = perf_counter()
        print(f"[QUERY] {name} 조회 시작 {start:%Y-%m-%d %H:%M:%S}", flush=True)
        df = getData(param=sql, convert_type=True, verbose=True)
        end = dt.datetime.now()
        secs = perf_counter() - t0
        n = len(df)
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
        })
        print(f"[QUERY] {name} rows={n:,} cols={df.shape[1]} {secs:.1f}s", flush=True)
        if keep_sample:
            # 원본 전체를 들고 있으면 메모리가 터지므로 시트 기록분만 복사해 둔다.
            raw_samples[name] = (df if cap is None else df.head(cap)).copy()
        return df

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"

    with timer("소형 원천 조회"):
        df_lot = fetch("lot", lot_query)
        df_eqp = fetch("equipment", eqp_query)
        df_eqp_group = fetch("eqp_group", eqp_group_query)
        df_hold = fetch("hold", hold_query)

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
    con.register("m", _lower_cols(df_lot))
    con.register("s", s)
    con.register("t", t)
    for k, v in holds.items():
        con.register(k, v)

    with timer("f3 생성"):
        df_f3, tip_match = build_f3(con)
    n_tip = int(df_f3["tip"].notna().sum())
    print(f"[TIP] f3 tip 값 있는 행 = {n_tip:,} / {len(df_f3):,}", flush=True)
    print(f"[ROWS] f3 = {len(df_f3):,}  (lot {df_f3['lot_id'].nunique():,}개)", flush=True)

    dup_rows, dup_cols = diagnose_duplicates(df_f3)
    if len(dup_rows):
        print(f"[DUP] (line, lot_id, order_seq) 중복 {len(dup_rows):,}행 / "
              f"유발 컬럼: {dup_cols}", flush=True)
    else:
        print("[DUP] lot/step 중복 없음", flush=True)

    mixed = diagnose_eqp_group_mix(df_eqp_group, df_eqp)
    if len(mixed):
        n_grp = mixed[["line_id", "eqp_group_name"]].drop_duplicates().shape[0]
        n_unmatched = int((mixed["구분"] == "미매칭").sum())
        print(f"[MIX] batch/single 혼재 설비그룹 {n_grp:,}개 "
              f"(그중 equipment 미매칭 설비 {n_unmatched:,}건)", flush=True)
    else:
        print("[MIX] batch/single 혼재 설비그룹 없음", flush=True)

    with timer("엑셀 저장"):
        path = os.path.join(os.getcwd(), f"f3_{stamp}.xlsx")
        save_workbook(path, df_f3, load_log, raw_samples, dup_rows, dup_cols, mixed,
                      {"t_rules(전처리된 tip)": t, "tip_join_검증": tip_match,
                       "s(step_scope)": s})
    print(f"saved: {path} rows={len(df_f3):,}", flush=True)

if __name__ == "__main__":
    main()
