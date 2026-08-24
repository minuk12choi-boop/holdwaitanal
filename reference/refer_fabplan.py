from bigdataquery import *
import bigdataquery as bdq
import pandas as pd
import pymysql
from datetime import datetime
from my_def import *


def smicdc_merge():

    my_convert_type = True
    my_verbose = True

    tkinprevent = """
                WITH
                t as (SELECT 
                        t.process, 
                        t.step, 
                        t.ppid, 
                        t.eqpid, 
                        t.chamberid, 
                        IFNULL(t.checkcount,0) as checkcount, 
                        IFNULL(t.tkin_count,0) as tkin_count, 
                        t.type,
                        (case when t.chamberid is not null and t.chamberid not in ('MAIN', '-') then concat(t.eqpid,'-',chamberid) else t.eqpid end) as ee
                    FROM mos_kh_smi.smicdc_p3nrd_trackinprevent t
                    INNER JOIN (SELECT
                                DISTINCT m.proc_id
                                FROM mos_kh_smi.smicdc_p3nrd_mc_lot m
                                WHERE m.lot_status_seg IN ('Active', 'Hold')
                                    AND m.sys_line_id = 'PFR1'
                                    AND m.lot_level IS NULL) mm
                    ON t.process = mm.proc_id
                    ORDER BY t.eqpid, t.chamberid
                    ),

                te as (SELECT 
                        t.process, 
                        t.step, 
                        t.ppid, 
                        t.ee,
                        t.eqpid, 
                        t.chamberid, 
                        t.checkcount, 
                        t.tkin_count, 
                        t.type,
                        e.batch_kind,
                        e.eqp_status,
                        e.origin_line_id as eqp_line
                    FROM t
                    LEFT JOIN (SELECT
                                eqp_id,
                                eqp_status, 
                                batch_kind,
                                origin_line_id
                                FROM fab.m_mi_equipment where line_id = 'PFR1') e
                        ON t.ee = e.eqp_id
                    ORDER BY process, step, ppid, ee)

                SELECT 
                    IFNULL(case when ifnull(a.batch_kind, '-') in ('BATCH_FURNACE', 'BATCH_WET') then a.eqpid else b.ee end, a.eqpid) as eqpcham,
                    a.process,
                    a.step,
                    a.ppid,
                    a.eqpid,
                    b.chamberid,
                    a.batch_kind,
                    a.eqp_status as eqpstatus_body,
                    b.eqp_status as eqpstatus_cham,
                    a.checkcount as checkcount_body,
                    a. tkin_count as tkincount_body,
                    b.checkcount as checkcount_cham,
                    b. tkin_count as tkincount_cham,
                    a.type as type_body,
                    b.type as type_cham,
                    (case when b.eqp_status is null then a.eqp_status
                        when b.chamberid is not null and a.eqp_status in ('PM','LOCAL','DOWN') then a.eqp_status
                        else b.eqp_status
                        end) as eqpstatus,
                    (case when a.type='PREVENT' and a.checkcount<=a.tkin_count or b.type='PREVENT' and b.checkcount<=b.tkin_count then 'PREVENT'
                        else 'DOING'
                        end) as prevent,
                    
                    IFNULL(a.eqp_line, b.eqp_line) as eqpline
                FROM (SELECT * FROM te WHERE chamberid in ('MAIN', '-') or chamberid is null) a
                LEFT JOIN (SELECT * FROM te WHERE chamberid not in ('MAIN', '-') and chamberid is not null) b
                    ON a.process = b.process
                        AND a.step = b.step
                        AND a.ppid = b.ppid
                        AND a.eqpid = b.eqpid
                ORDER BY process, step, ppid, eqpid, chamberid
                """

    step = """
                SELECT  
                    s.processid,
                    s.category,
                    s.skiprule,
                    s.subarea as areaname,
                    s.eqptype,
                    s.layerid,
                    CASE WHEN SUBSTRING(s.stepseq, 3) REGEXP '[^0-9]' THEN '기타' ELSE '메인' END AS stepseq_type,
                    s.stepseq,
                    s.descript,
                    s.recipeid,
                    s.delaytime,
                    s.n2_delay_time_mins,
                    k.lotid_ld as ff,
                    k.descript as tt
                FROM mos_kh_smi.smicdc_p3nrd_step s
                LEFT JOIN (SELECT skiprule, lotid_ld, descript FROM mos_kh_smi.smicdc_p3nrd_skiprule) k
                ON s.skiprule = k.skiprule
                WHERE s.revstate = 'Active'
                ORDER BY s.processid, s.stepseq
            """

    mclot = """
            SELECT
                m.lot_id,
                m.lot_type,
                (case when m.lot_status_seg = 'Hold' then 'HOLD' else m.step_status_seg end) as status,
                m.proc_id,
                m.step_seq,
                m.cur_qty,
                m.carr_id,
                IFNULL(m.hot_lot_level, '-') as hot_lot_level,
                m.last_event_date,
                m.cur_line_id
            FROM mos_kh_smi.smicdc_p3nrd_mc_lot m
            LEFT JOIN (SELECT
                    processid, revstate
                    FROM mos_kh_smi.smicdc_p3nrd_processplan
                    WHERE revstate = 'Active' AND (IFNULL(version, 'm') = 'N' or SUBSTR(processid,1,1) = 'R') ) p
            ON m.proc_id = p.processid
            WHERE m.lot_status_seg IN ('Active', 'Hold')
            AND m.sys_line_id = 'PFR1'
            AND p.revstate IS NOT NULL

            """
    
    pems = """
            SELECT 
                type_ as pems_type,
                einecnno,
                processid,
                connecttype,
                stepseq,
                nextstepseq,
                lotids,
                eqpids as pems_eqpids,
                chamberids as pems_chamberids,
                ppid as pems_ppid,
                comment_ as pems_comment,
                issueuser as pems_user,
                issuedate as pems_date,
                ecnrule
            FROM 
                mos_kh_smi.smicdc_p3nrd_neweinecnspec SE
            WHERE 
                NOW() <= SE.ENDTIME
                AND SE.STARTTIME <= NOW()
                AND (SE.TOTALCOUNT = 0 OR (SE.TKINCOUNT < SE.TOTALCOUNT AND SE.TYPE_ IN ('EIN', 'RCS')) OR SE.TYPE_ = 'ECN')
                AND SE.HOLDFLAG <> 'Y'
            """

    engrlotppid = """ SELECT lotid, processid, stepseq, eqpid, newppid, userid, updated FROM mos_kh_smi.smicdc_p3nrd_engr_lot_ppid """

    selectconnectspec = """ SELECT firsteinecnno, nexteinecnno, selecteinecnno FROM mos_kh_smi.smicdc_p3nrd_selectconnectspec WHERE selecteinecnno<>'-' """


    ############## get data
    with timer("getData: tkinprevent"):
        df_tkinprevent = getData(
                            param = tkinprevent,
                            # user_name = 'minuk12.choi',
                            convert_type = my_convert_type,
                            verbose = my_verbose
                            )
    with timer("getData: mclot"):
        df_mclot = getData(
                            param = mclot,
                            # user_name = 'minuk12.choi',
                            convert_type = my_convert_type,
                            verbose = my_verbose
                            )
    with timer("getData: pems"):
        df_pems = getData(
                        param = pems,
                        # user_name = 'minuk12.choi',
                        convert_type = my_convert_type,
                        verbose = my_verbose
                        )
    with timer("getData: step"):
        df_step= getData(
                            param = step,
                            # user_name = 'minuk12.choi',
                            convert_type = my_convert_type,
                            verbose = my_verbose
                            )
    with timer("getData: engrlotppid"):
        df_engrlotppid = getData(
                        param = engrlotppid,
                        # user_name = 'minuk12.choi',
                        convert_type = my_convert_type,
                        verbose = my_verbose
                        )
    with timer("getData: selectconnectspec"):
        df_selectconnectspec = getData(
                        param = selectconnectspec,
                        # user_name = 'minuk12.choi',
                        convert_type = my_convert_type,
                        verbose = my_verbose
                        )

    with timer("getData: join_step_window_by_index"):
        df_mcstep = join_step_window_by_index(df_mclot, df_step, window=100)
    with timer("getData: join_engrlot_ppid_override_recipeid"):
        df_mse = join_engrlot_ppid_override_recipeid(df_mcstep, df_engrlotppid)
    with timer("getData: join_pems_with_rules"):
        df_msep = join_pems_with_rules(df_mse, df_pems, df_selectconnectspec)
    with timer("getData: build_df_msep_skip"):
        df_msep_skip = build_df_msep_skip(df_msep)
    with timer("getData: add_continuous_col_v9"):
        df_msep_skip_c = add_continuous_col_v9(df_msep_skip)
    with timer("getData: keep_steps_like_finalize"):
        df_msep_skip_cs = keep_steps_like_finalize(df_msep_skip_c)
    with timer("getData: join_tkinprevent_with_issues"):
        df_msep_skip_ct = join_tkinprevent_with_issues(df_msep_skip_cs, df_tkinprevent)
    with timer("getData: finalize_dashboard_rows"):
        df_final = finalize_dashboard_rows(df_msep_skip_ct)

    return df_final

smicdc_merge()
