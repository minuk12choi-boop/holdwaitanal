query = """
WITH

m AS (
    SELECT 'KFR4' AS line, sys_line_id, cur_line_id, origin_line_id, lot_id, last_event_date
    FROM   MOS_KH_SMI.SMICDC_NRD_MC_LOT
    UNION ALL
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

tp AS (
    SELECT ml.lot_id,
           MAX(tp.pathseq) AS max_pathseq
    FROM (
        SELECT lot_id, step_seq
        FROM   MOS_KH_SMI.SMICDC_NRD_MC_LOT
        WHERE  lot_status_seg IN ('Hold', 'Active')
    ) ml
    JOIN   MOS_KH_SMI.SMICDC_NRD_TODOPLAN tp
      ON   ml.lot_id   = tp.lotid
     AND   ml.step_seq = tp.stepseq
    GROUP  BY ml.lot_id
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
           TO_TIMESTAMP(m.start_date,       'yyyyMMdd HH:mm:ss')    AS start_date,
           TO_TIMESTAMP(m.last_tkout_date,  'yyyyMMdd HH:mm:ss')    AS last_tkout_date,
           TO_TIMESTAMP(m.step_arrive_date, 'yyyyMMdd HH:mm:ss')    AS step_arrive_date,
           TO_TIMESTAMP(m.last_event_date,  'yyyyMMdd HH:mm:ss')    AS last_event_date
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
           TO_TIMESTAMP(m.start_date,       'yyyyMMdd HH:mm:ss')    AS start_date,
           TO_TIMESTAMP(m.last_tkout_date,  'yyyyMMdd HH:mm:ss')    AS last_tkout_date,
           TO_TIMESTAMP(m.step_arrive_date, 'yyyyMMdd HH:mm:ss')    AS step_arrive_date,
           TO_TIMESTAMP(m.last_event_date,  'yyyyMMdd HH:mm:ss')    AS last_event_date
    FROM        MOS_KH_SMI.SMICDC_NRD_MC_LOT m
    JOIN        m0
      ON        m.lot_id          = m0.lot_id
     AND        m.last_event_date = m0.max_event_date
    LEFT JOIN   tp
      ON        m.lot_id = tp.lot_id
    WHERE       m.lot_status_seg IN ('Active', 'Hold')

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
           TO_TIMESTAMP(m.start_date,       'yyyyMMdd HH:mm:ss')    AS start_date,
           TO_TIMESTAMP(m.last_tkout_date,  'yyyyMMdd HH:mm:ss')    AS last_tkout_date,
           TO_TIMESTAMP(m.step_arrive_date, 'yyyyMMdd HH:mm:ss')    AS step_arrive_date,
           TO_TIMESTAMP(m.last_event_date,  'yyyyMMdd HH:mm:ss')    AS last_event_date
    FROM        MOS_KH_SMI.SMICDC_NRDK_MC_LOT m
    JOIN        m0
      ON        m.lot_id          = m0.lot_id
     AND        m.last_event_date = m0.max_event_date
    LEFT JOIN   g
      ON        m.lot_id  = g.lot_id
     AND        g.line_id = 'KFR7'
    WHERE       m.lot_status_seg IN ('Active', 'Hold')
),

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
            FROM   MOS_KH_SMI.SMICDC_NRD_STEPRULE
        ) sr0
        JOIN (
            SELECT comments, fromseq, seq, inserttime
            FROM   MOS_KH_SMI.SMICDC_NRD_STEPCOMMENTS
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
