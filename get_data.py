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
  AND       m1.lot_type IN ('PP', 'PB', 'PG', 'TT')
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

# ── Step 기준 LOT (기존 m CTE) : Active/Hold 재공의 현재 order_seq ──────
lot_base_query = """
SELECT 'PFR1' AS line, lot_id, order_seq
FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
WHERE  lot_status_seg IN ('Active', 'Hold')
UNION ALL
SELECT 'KFR7' AS line, lot_id, order_seq
FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT
WHERE  lot_status_seg IN ('Active', 'Hold')
"""


# =====================================================================
# Tip 전처리 (기존 Oracle tip_table_pfr1 / tip_table_kfr7 의 t~final 로직을
# pandas 로 재현. 사내 15분 호출제한 회피를 위해 SQL 은 생테이블만 조회하고
# 결합/연산은 python 단에서 수행한다.)
# =====================================================================
import datetime as dt
import os

import numpy as np
import pandas as pd

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


# Excel 시트 최대 행수(1,048,576) - 헤더 1행
EXCEL_MAX_ROWS = 1_048_575


def _save_table(df, name, stamp):
    """Excel 로 저장하되, 행수가 시트 한계를 넘으면 CSV 로 대체 저장한다."""
    safe = _excel_safe(df)
    if len(safe) > EXCEL_MAX_ROWS:
        path = os.path.join(os.getcwd(), f"{name}_{stamp}.csv")
        safe.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"saved(CSV / Excel 행 제한 {EXCEL_MAX_ROWS:,} 초과): {path} rows={len(safe):,}")
    else:
        path = os.path.join(os.getcwd(), f"{name}_{stamp}.xlsx")
        safe.to_excel(path, index=False)
        print(f"saved: {path} rows={len(safe):,}")
    return path




# =====================================================================
# Step 전처리 (기존 Oracle step_table_pfr1 / step_table_kfr7 의 m~final 로직)
#   Equipment 는 Tip 에서 이미 불러온 df_eqp 를 그대로 재사용한다.
# =====================================================================
def _build_de_rank(path, lot_ids):
    """r CTE: delay_step_type S/Y 행에 대해 lot 별 누적 'S' 개수."""
    r = path[path['delay_step_type'].isin(['S', 'Y']) & path['lot_id'].isin(lot_ids)]
    r = r[['lot_id', 'order_seq', 'delay_step_type']].copy()
    r = r.sort_values(['lot_id', 'order_seq'], kind='mergesort')
    r['_s'] = r['delay_step_type'].eq('S').astype(int)
    r['de_rank'] = r.groupby('lot_id', dropna=False)['_s'].cumsum()
    # Oracle 의 RANGE 기본동작(동일 order_seq 는 같은 값)에 맞춤
    r['de_rank'] = r.groupby(['lot_id', 'order_seq'], dropna=False)['de_rank'].transform('max')
    return r[['lot_id', 'order_seq', 'de_rank']].drop_duplicates()


def _int_str(series):
    """Oracle TO_CHAR(number) 대응. 소수점 표기('3.0') 방지."""
    n = pd.to_numeric(series, errors='coerce')
    return n.astype('Float64').astype('Int64').astype('string')


def build_step(df_path, df_lot_base, df_eqp, df_eqp_group, line):
    """생테이블(StepPath, MC_LOT, Equipment, EqpGroup) → 기존 step 결과 포맷."""
    path = _lower_cols(df_path)
    m = _lower_cols(df_lot_base)
    m = m[m['line'].eq(line)][['lot_id', 'order_seq']].drop_duplicates()

    r = _build_de_rank(path, set(m['lot_id'].dropna()))

    # p CTE: 재공 현재 위치(order_seq) 이후의 미진행 step 만
    p = path[path['step_skip_yn'].ne('Y')].merge(
        m.rename(columns={'order_seq': 'cur_order_seq'}), on='lot_id', how='inner')
    p = p[p['order_seq'] >= p['cur_order_seq']]
    p = p.merge(r, on=['lot_id', 'order_seq'], how='left')

    delay_mins = pd.to_numeric(p['delay_time_mins'], errors='coerce')
    cont = np.select(
        [p['delay_step_type'].eq('S'), p['delay_step_type'].eq('Y')],
        ['연속첫', '연속(' + _int_str(np.trunc(delay_mins)).fillna('') + ')'],
        default=None,
    )

    detail = p['tkin_type_detail'].where(p['tkin_type_detail'].ne('-'))

    out = pd.DataFrame({
        'line': line,
        'lot_id': p['lot_id'],
        'proc_id': p['proc_id'],
        'order_seq': p['order_seq'],
        'de_rank': p['de_rank'],
        'delay_step_type': p['delay_step_type'],
        '연속': cont,
        'layer_id': p['layer_id'],
        'step_level': _int_str(p['step_level']),
        'ein': detail.fillna(p['ext_1st_vals']),
        'step_seq': p['step_seq'],
        'step_desc': p['step_desc'],
        'eqp_type': p['eqp_type'],
        'eqp_group_raw': p['eqp_group_id'],
        'recipe_id': p['recipe_id'],
    })

    # eqp_group: 'OFF' 포함 설비 제외
    eg = _lower_cols(df_eqp_group)
    eg = eg[eg['line_id'].eq(line) & ~eg['eqp_id'].str.contains('OFF', na=False)]
    eg = eg[['line_id', 'eqp_group_name', 'eqp_id']].drop_duplicates()
    eg = _drop_null_keys(eg, ['line_id', 'eqp_group_name'])

    out = out.merge(eg, left_on=['line', 'eqp_group_raw'],
                    right_on=['line_id', 'eqp_group_name'],
                    how='left').drop(columns=['line_id', 'eqp_group_name'])

    # equipment: Tip 에서 쓰던 것과 동일한 정제본 재사용
    e = _build_equipment(df_eqp, line)
    e = _drop_null_keys(
        e[['line_id', 'eqp_id', 'batch_kind', 'eqpline',
           'eqp_status', 'eqp_status_change_time']].drop_duplicates(),
        ['line_id', 'eqp_id'])

    out = out.merge(e, on=['eqp_id'], how='left', suffixes=('', '_e'))
    out = out.rename(columns={'eqp_status': 'body_status'}).drop(columns=['line_id'])

    return out.sort_values(['lot_id', 'order_seq', 'eqp_id'],
                           na_position='last', kind='mergesort').reset_index(drop=True)


if __name__ == "__main__":
    from bigdataquery import getData

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"

    df_lot = getData(param=lot_query, convert_type=True, verbose=True)
    df_kfr7_tip = getData(param=kfr7_tip_query, convert_type=True, verbose=True)
    df_pfr1_tip = getData(param=pfr1_tip_query, convert_type=True, verbose=True)
    df_eqp = getData(param=eqp_query, convert_type=True, verbose=True)
    df_eqp_group = getData(param=eqp_group_query, convert_type=True, verbose=True)
    df_lot_base = getData(param=lot_base_query, convert_type=True, verbose=True)
    df_kfr7_path = getData(param=kfr7_step_path_query, convert_type=True, verbose=True)
    df_pfr1_path = getData(param=pfr1_step_path_query, convert_type=True, verbose=True)

    tip = pd.concat(
        [build_tip(df_kfr7_tip, df_eqp, 'KFR7'),
         build_tip(df_pfr1_tip, df_eqp, 'PFR1')],
        ignore_index=True,
    )

    step = pd.concat(
        [build_step(df_kfr7_path, df_lot_base, df_eqp, df_eqp_group, 'KFR7'),
         build_step(df_pfr1_path, df_lot_base, df_eqp, df_eqp_group, 'PFR1')],
        ignore_index=True,
    )

    _save_table(df_lot, "lot", stamp)
    _save_table(tip, "tip", stamp)
    _save_table(step, "step", stamp)
