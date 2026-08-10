# -*- coding: utf-8 -*-
"""
bench_processing.py — 전처리(로컬 연산) 방식 벤치마크

build_f3.py 의 로컬 구간 중 시간이 걸리는 곳을 구현 방식별로 비교한다.
로그 기준 로컬 구간:
    KFR7 f3 범위 축약   16.3s
    KFR7 tip 선필터      6.5s
    PFR1 f3 범위 축약    3.2s
    PFR1 tip 선필터      3.1s
    f3 생성              1.3s
                        -------
                        약 30s

각 시나리오는 **결과가 현행과 동일한지 검증**한 뒤 시간을 비교한다.
동일하지 않으면 시간과 무관하게 채택하지 않는다.

사용:
    python bench_processing.py                 # 전체
    python bench_processing.py --repeat 3
    python bench_processing.py --stage narrow  # narrow / prefilter / tip
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from time import perf_counter

import duckdb
import numpy as np
import pandas as pd

import build_f3 as B


def timed(fn, repeat):
    times, out = [], None
    for _ in range(repeat):
        t0 = perf_counter()
        out = fn()
        times.append(perf_counter() - t0)
    return min(times), sum(times) / len(times), out


def frame_key(df, cols):
    """비교용 정규화: 지정 컬럼만 문자열화해 정렬."""
    d = df[cols].copy()
    for c in cols:
        d[c] = d[c].astype("string").fillna("\u2205")
    return d.sort_values(cols, kind="mergesort").reset_index(drop=True)


# ---------------------------------------------------------------------------
# narrow_step_to_scope 대안
# ---------------------------------------------------------------------------
def narrow_duckdb(df_path, df_lot, line):
    """동일 로직을 DuckDB SQL 로. pandas groupby/merge 대신 윈도우+조인."""
    con = duckdb.connect()
    m = B._lower_cols(df_lot)
    m = m[m["line"].eq(line)][["lot_id", "order_seq"]].copy()
    m["order_seq"] = pd.to_numeric(m["order_seq"], errors="coerce")
    m = m.dropna(subset=["order_seq"]).drop_duplicates()
    con.register("p_raw", B._lower_cols(df_path))
    con.register("m", m)

    skip = ("p.step_skip_yn IS NOT NULL AND p.step_skip_yn <> 'Y'"
            if B.EXCLUDE_NULL_STEP_SKIP_YN else "COALESCE(p.step_skip_yn,'') <> 'Y'")

    return con.execute(f"""
        WITH p AS (
            SELECT *, TRY_CAST(order_seq AS DOUBLE) AS os
            FROM p_raw
            WHERE lot_id IN (SELECT lot_id FROM m)
        ),
        de AS (
            SELECT lot_id, os,
                   MAX(rk) AS de_rank
            FROM (
                SELECT lot_id, os,
                       SUM(CASE WHEN delay_step_type='S' THEN 1 ELSE 0 END)
                           OVER (PARTITION BY lot_id ORDER BY os
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rk
                FROM p WHERE delay_step_type IN ('S','Y')
            ) GROUP BY lot_id, os
        ),
        kept AS (SELECT p.* FROM p WHERE {skip}),
        cur AS (
            SELECT k.*, de.de_rank
            FROM kept k JOIN m ON k.lot_id = m.lot_id AND k.os = m.order_seq
            LEFT JOIN de ON de.lot_id = k.lot_id AND de.os = k.os
        ),
        blk AS (
            SELECT k.*, de.de_rank
            FROM kept k JOIN m ON k.lot_id = m.lot_id AND k.os > m.order_seq
            JOIN de ON de.lot_id = k.lot_id AND de.os = k.os
            JOIN (SELECT DISTINCT lot_id, de_rank FROM cur WHERE de_rank IS NOT NULL) cr
              ON cr.lot_id = k.lot_id AND cr.de_rank = de.de_rank
        ),
        u AS (SELECT * FROM cur UNION ALL SELECT * FROM blk)
        SELECT DISTINCT ON (lot_id, os)
            '{line}' AS line, lot_id, proc_id, os AS order_seq, de_rank,
            CASE WHEN delay_step_type='S' THEN '연속첫'
                 WHEN delay_step_type='Y'
                 THEN '연속(' || COALESCE(CAST(CAST(TRUNC(delay_time_mins) AS BIGINT) AS VARCHAR),'') || ')'
            END AS "연속",
            layer_id,
            CAST(CAST(step_level AS BIGINT) AS VARCHAR) AS step_level,
            COALESCE(NULLIF(tkin_type_detail,'-'), ext_1st_vals) AS ein,
            step_seq, step_desc, eqp_type, eqp_group_id AS eqp_group_raw, recipe_id
        FROM u
    """).df()


# ---------------------------------------------------------------------------
# prefilter_tip 대안
# ---------------------------------------------------------------------------
def prefilter_merge(df_tip, s_scope):
    """isin 4회 대신 categorical 화 후 numpy 마스크."""
    t = B._lower_cols(df_tip)
    out = np.ones(len(t), dtype=bool)
    for col, scol in (("process", "proc_id"), ("step", "step_seq"),
                      ("ppid", "recipe_id"), ("eqpid", "eqp_id")):
        allowed = pd.Index(pd.unique(s_scope[scol].dropna()))
        v = t[col]
        out &= (v.isna() | v.isin(["-", ""]) | v.isin(allowed)).to_numpy()
    return t[out]


def prefilter_duckdb(df_tip, s_scope):
    con = duckdb.connect()
    con.register("t", B._lower_cols(df_tip))
    con.register("sc", s_scope[["proc_id", "step_seq", "recipe_id", "eqp_id"]])
    return con.execute("""
        SELECT t.* FROM t
        WHERE (t.process IS NULL OR t.process IN ('-','')
               OR t.process IN (SELECT DISTINCT proc_id FROM sc WHERE proc_id IS NOT NULL))
          AND (t.step IS NULL OR t.step IN ('-','')
               OR t.step IN (SELECT DISTINCT step_seq FROM sc WHERE step_seq IS NOT NULL))
          AND (t.ppid IS NULL OR t.ppid IN ('-','')
               OR t.ppid IN (SELECT DISTINCT recipe_id FROM sc WHERE recipe_id IS NOT NULL))
          AND (t.eqpid IS NULL OR t.eqpid IN ('-','')
               OR t.eqpid IN (SELECT DISTINCT eqp_id FROM sc WHERE eqp_id IS NOT NULL))
    """).df()


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="전처리 방식 벤치마크")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--stage", default="all",
                    choices=["all", "narrow", "prefilter", "tip"])
    args = ap.parse_args()

    from bigdataquery import getData

    def fetch(name, sql):
        t0 = perf_counter()
        df = getData(param=sql, convert_type=True, verbose=False)
        print(f"[LOAD] {name} {len(df):,}행 {perf_counter()-t0:.1f}s", flush=True)
        return df

    results = []
    df_lot = fetch("lot", B.lot_query)
    df_eqp = fetch("equipment", B.eqp_query)
    df_grp = fetch("eqp_group", B.eqp_group_query)

    for line, path_sql, tip_sql in (
            ("KFR7", B.kfr7_step_path_query, B.kfr7_tip_query),
            ("PFR1", B.pfr1_step_path_query, B.pfr1_tip_query)):

        df_path = fetch(f"{line}_step_path", path_sql)

        if args.stage in ("all", "narrow"):
            base_t, base_avg, base = timed(
                lambda: B.narrow_step_to_scope(df_path, df_lot, line), args.repeat)
            results.append({"단계": f"narrow_{line}", "방식": "현행(pandas)",
                            "최소_초": round(base_t, 2), "평균_초": round(base_avg, 2),
                            "행수": len(base), "결과동일": "기준"})
            print(f"  narrow_{line:5s} pandas {base_t:6.2f}s rows={len(base):,}", flush=True)

            try:
                d_t, d_avg, d_out = timed(
                    lambda: narrow_duckdb(df_path, df_lot, line), args.repeat)
                cols = ["lot_id", "order_seq", "de_rank", "step_seq", "eqp_group_raw"]
                same = frame_key(base, cols).equals(frame_key(d_out, cols))
                results.append({"단계": f"narrow_{line}", "방식": "duckdb",
                                "최소_초": round(d_t, 2), "평균_초": round(d_avg, 2),
                                "행수": len(d_out), "결과동일": "Y" if same else "N"})
                print(f"  narrow_{line:5s} duckdb {d_t:6.2f}s rows={len(d_out):,} "
                      f"동일={same}", flush=True)
            except Exception as e:
                results.append({"단계": f"narrow_{line}", "방식": "duckdb",
                                "오류": f"{type(e).__name__}: {e}"})
                print(f"  [FAIL] narrow_{line} duckdb: {e}", flush=True)
        else:
            base = B.narrow_step_to_scope(df_path, df_lot, line)

        scope = B.expand_with_equipment(base, df_eqp, df_grp, line)
        del df_path

        if args.stage in ("all", "prefilter", "tip"):
            df_tip = fetch(f"{line}_tip", tip_sql)

            if args.stage in ("all", "prefilter"):
                p_t, p_avg, p_base = timed(
                    lambda: B.prefilter_tip(df_tip, scope), args.repeat)
                results.append({"단계": f"prefilter_{line}", "방식": "현행(isin)",
                                "최소_초": round(p_t, 2), "평균_초": round(p_avg, 2),
                                "행수": len(p_base), "결과동일": "기준"})
                print(f"  prefilter_{line:5s} isin   {p_t:6.2f}s rows={len(p_base):,}",
                      flush=True)

                for name, fn in (("numpy mask", prefilter_merge),
                                 ("duckdb", prefilter_duckdb)):
                    try:
                        a_t, a_avg, a_out = timed(lambda: fn(df_tip, scope), args.repeat)
                        cols = ["process", "step", "ppid", "eqpid", "chamberid"]
                        same = frame_key(p_base, cols).equals(frame_key(a_out, cols))
                        results.append({"단계": f"prefilter_{line}", "방식": name,
                                        "최소_초": round(a_t, 2), "평균_초": round(a_avg, 2),
                                        "행수": len(a_out), "결과동일": "Y" if same else "N"})
                        print(f"  prefilter_{line:5s} {name:10s} {a_t:6.2f}s "
                              f"rows={len(a_out):,} 동일={same}", flush=True)
                    except Exception as e:
                        results.append({"단계": f"prefilter_{line}", "방식": name,
                                        "오류": f"{type(e).__name__}: {e}"})
                        print(f"  [FAIL] prefilter_{line} {name}: {e}", flush=True)
                tip_f = p_base
            else:
                tip_f = B.prefilter_tip(df_tip, scope)

            if args.stage in ("all", "tip"):
                t_t, t_avg, t_out = timed(
                    lambda: B.build_tip(tip_f, df_eqp, line), args.repeat)
                results.append({"단계": f"build_tip_{line}", "방식": "현행(pandas)",
                                "최소_초": round(t_t, 2), "평균_초": round(t_avg, 2),
                                "행수": len(t_out), "결과동일": "기준"})
                print(f"  build_tip_{line:5s} pandas {t_t:6.2f}s rows={len(t_out):,}",
                      flush=True)
            del df_tip

    df = pd.DataFrame(results)
    print("\n" + df.to_string(index=False), flush=True)

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
    path = os.path.join(os.getcwd(), f"bench_processing_{stamp}.xlsx")
    df.to_excel(path, index=False)
    print(f"\nsaved: {path}", flush=True)


if __name__ == "__main__":
    main()
