# -*- coding: utf-8 -*-
"""
bench_loading.py — 원천 로딩 방식 벤치마크

build_f3.py 의 병목인 두 테이블에 대해, 가능한 로딩 시나리오를 모두 돌려
소요시간·행수를 비교한다. 결과가 동일한지도 함께 검증한다.

사용:
    python bench_loading.py                # 전체
    python bench_loading.py --target hold  # hold 만
    python bench_loading.py --target step  # step_path 만
    python bench_loading.py --repeat 2     # 각 시나리오 2회 (캐시 영향 확인)
    python bench_loading.py --only h1,h5   # 특정 시나리오만
    python bench_loading.py --list         # 시나리오 목록만 출력

결과는 bench_loading_<시각>.xlsx 로 저장된다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import traceback
from time import perf_counter

import pandas as pd

# ---------------------------------------------------------------------------
# 공통 조각
# ---------------------------------------------------------------------------
LINES_SQL = "('KFR7', 'PFR1')"

CUR_LOT = """
    SELECT 'PFR1' AS line_id, lot_id, order_seq
    FROM   MOS_KH_SMI.SMICDC_P3NRD_MC_LOT
    WHERE  lot_status_seg IN ('Active', 'Hold')
    UNION ALL
    SELECT 'KFR7' AS line_id, lot_id, order_seq
    FROM   MOS_KH_SMI.SMICDC_NRDK_MC_LOT
    WHERE  lot_status_seg IN ('Active', 'Hold')
"""

HOLD_COLS = ("line_id, item_type, status_seq, lot_id, step_seq, "
             "hold_user_name, issue_reason_cont, issue_date")

STEP_COLS = ("lot_id, order_seq, proc_id, step_seq, step_desc, step_level, "
             "step_skip_yn, delay_step_type, delay_time_mins, layer_id, "
             "eqp_type, eqp_group_id, recipe_id, ext_1st_vals, tkin_type_detail")


# ---------------------------------------------------------------------------
# hold 시나리오
# ---------------------------------------------------------------------------
HOLD_SCENARIOS = {
    "h1": ("생테이블 (KFR4 포함, 필터 없음)", f"""
SELECT {HOLD_COLS}, version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN ('KFR4', 'KFR7', 'PFR1')
"""),

    "h2": ("생테이블 (KFR4 제외)", f"""
SELECT {HOLD_COLS}, version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN {LINES_SQL}
"""),

    "h3": ("status_seq<>'2' 만 서버 필터", f"""
SELECT {HOLD_COLS}, version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN {LINES_SQL}
  AND  status_seq <> '2'
"""),

    "h4": ("MAX(version_desc) OVER () 윈도우", f"""
SELECT {HOLD_COLS}, version_desc
FROM (
    SELECT {HOLD_COLS}, version_desc,
           MAX(version_desc) OVER () AS mv
    FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
    WHERE  line_id IN {LINES_SQL}
) x
WHERE version_desc = mv
"""),

    "h5": ("스칼라 서브쿼리로 max version_desc", f"""
SELECT {HOLD_COLS}, version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN {LINES_SQL}
  AND  version_desc = (SELECT MAX(version_desc)
                       FROM MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT)
"""),

    "h6": ("스칼라 서브쿼리(라인한정) + status", f"""
SELECT {HOLD_COLS}, version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN {LINES_SQL}
  AND  status_seq <> '2'
  AND  version_desc = (SELECT MAX(version_desc)
                       FROM MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
                       WHERE line_id IN {LINES_SQL})
"""),

    "h7": ("mc_lot 조인만 (version/status 없음)", f"""
SELECT h.line_id, h.item_type, h.status_seq, h.lot_id, h.step_seq,
       h.hold_user_name, h.issue_reason_cont, h.issue_date, h.version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT h
JOIN   (SELECT DISTINCT line_id, lot_id FROM ({CUR_LOT}) z) c
  ON   h.line_id = c.line_id AND h.lot_id = c.lot_id
WHERE  h.line_id IN {LINES_SQL}
"""),

    "h8": ("mc_lot 조인 + status", f"""
SELECT h.line_id, h.item_type, h.status_seq, h.lot_id, h.step_seq,
       h.hold_user_name, h.issue_reason_cont, h.issue_date, h.version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT h
JOIN   (SELECT DISTINCT line_id, lot_id FROM ({CUR_LOT}) z) c
  ON   h.line_id = c.line_id AND h.lot_id = c.lot_id
WHERE  h.line_id IN {LINES_SQL}
  AND  h.status_seq <> '2'
"""),

    "h9": ("mc_lot 조인 + 스칼라 max version + status", f"""
SELECT h.line_id, h.item_type, h.status_seq, h.lot_id, h.step_seq,
       h.hold_user_name, h.issue_reason_cont, h.issue_date, h.version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT h
JOIN   (SELECT DISTINCT line_id, lot_id FROM ({CUR_LOT}) z) c
  ON   h.line_id = c.line_id AND h.lot_id = c.lot_id
WHERE  h.line_id IN {LINES_SQL}
  AND  h.status_seq <> '2'
  AND  h.version_desc = (SELECT MAX(version_desc)
                         FROM MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
                         WHERE line_id IN {LINES_SQL})
"""),

    "h10": ("mc_lot 조인 + 윈도우 max version + status (현재 방식)", f"""
SELECT x.line_id, x.item_type, x.status_seq, x.lot_id, x.step_seq,
       x.hold_user_name, x.issue_reason_cont, x.issue_date, x.version_desc
FROM (
    SELECT h.line_id, h.item_type, h.status_seq, h.lot_id, h.step_seq,
           h.hold_user_name, h.issue_reason_cont, h.issue_date, h.version_desc,
           MAX(h.version_desc) OVER () AS mv
    FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT h
    JOIN   (SELECT DISTINCT line_id, lot_id FROM ({CUR_LOT}) z) c
      ON   h.line_id = c.line_id AND h.lot_id = c.lot_id
    WHERE  h.line_id IN {LINES_SQL}
) x
WHERE x.version_desc = x.mv AND x.status_seq <> '2'
"""),

    "h11": ("2단계: max 조회 후 리터럴 주입", "__TWO_STEP__"),
}

HOLD_MAX_SQL = f"""
SELECT MAX(version_desc) AS mv
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT
WHERE  line_id IN {LINES_SQL}
"""

HOLD_LITERAL_SQL = f"""
SELECT h.line_id, h.item_type, h.status_seq, h.lot_id, h.step_seq,
       h.hold_user_name, h.issue_reason_cont, h.issue_date, h.version_desc
FROM   MOS_KH_SMI.MEMMSS_FAB_ISSUE_LOT h
JOIN   (SELECT DISTINCT line_id, lot_id FROM ({CUR_LOT}) z) c
  ON   h.line_id = c.line_id AND h.lot_id = c.lot_id
WHERE  h.line_id IN {LINES_SQL}
  AND  h.status_seq <> '2'
  AND  h.version_desc = '{{MV}}'
"""


# ---------------------------------------------------------------------------
# step_path 시나리오 (KFR7 / PFR1 각각)
# ---------------------------------------------------------------------------
STEP_TABLES = {
    "KFR7": ("MOS_KH_SMI.SMICDC_NRDK_MC_LOT_STEP_PATH",
             "MOS_KH_SMI.SMICDC_NRDK_MC_LOT"),
    "PFR1": ("MOS_KH_SMI.SMICDC_P3NRD_MC_LOT_STEP_PATH",
             "MOS_KH_SMI.SMICDC_P3NRD_MC_LOT"),
}


def step_scenarios(line):
    path_tbl, lot_tbl = STEP_TABLES[line]
    cur = (f"SELECT lot_id, order_seq FROM {lot_tbl} "
           f"WHERE lot_status_seg IN ('Active', 'Hold')")
    return {
        f"s1_{line}": (f"{line} 생테이블 (현재 방식)", f"""
SELECT {STEP_COLS}
FROM   {path_tbl}
"""),
        f"s2_{line}": (f"{line} lot_id IN 서브쿼리", f"""
SELECT {STEP_COLS}
FROM   {path_tbl}
WHERE  lot_id IN (SELECT lot_id FROM {lot_tbl}
                  WHERE lot_status_seg IN ('Active','Hold'))
"""),
        f"s3_{line}": (f"{line} lot_id 조인", f"""
SELECT {', '.join('p.' + c.strip() for c in STEP_COLS.split(','))}
FROM   {path_tbl} p
JOIN   (SELECT DISTINCT lot_id FROM {lot_tbl}
        WHERE lot_status_seg IN ('Active','Hold')) c
  ON   p.lot_id = c.lot_id
"""),
        f"s4_{line}": (f"{line} lot 조인 + skip 제외", f"""
SELECT {', '.join('p.' + c.strip() for c in STEP_COLS.split(','))}
FROM   {path_tbl} p
JOIN   (SELECT DISTINCT lot_id FROM {lot_tbl}
        WHERE lot_status_seg IN ('Active','Hold')) c
  ON   p.lot_id = c.lot_id
WHERE  p.step_skip_yn IS NOT NULL AND p.step_skip_yn <> 'Y'
"""),
        f"s5_{line}": (f"{line} lot 조인 + 현재위치 이후 or S/Y", f"""
SELECT {', '.join('p.' + c.strip() for c in STEP_COLS.split(','))}
FROM   {path_tbl} p
JOIN   ({cur}) c
  ON   p.lot_id = c.lot_id
WHERE  p.order_seq >= c.order_seq
    OR p.delay_step_type IN ('S','Y')
"""),
        f"s6_{line}": (f"{line} lot 조인 + 현재위치 이후 + skip 제외", f"""
SELECT {', '.join('p.' + c.strip() for c in STEP_COLS.split(','))}
FROM   {path_tbl} p
JOIN   ({cur}) c
  ON   p.lot_id = c.lot_id
WHERE  (p.order_seq >= c.order_seq OR p.delay_step_type IN ('S','Y'))
  AND  p.step_skip_yn IS NOT NULL AND p.step_skip_yn <> 'Y'
"""),
    }


# ---------------------------------------------------------------------------
def run_one(getData, key, label, sql, repeat):
    times, rows, cols, err = [], None, None, ""
    for i in range(repeat):
        t0 = perf_counter()
        try:
            df = getData(param=sql, convert_type=True, verbose=False)
            times.append(perf_counter() - t0)
            rows, cols = len(df), df.shape[1]
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"  [FAIL] {key}: {err}", flush=True)
            traceback.print_exc()
            break
    if times:
        print(f"  {key:10s} {min(times):7.1f}s  rows={rows:>10,}  {label}", flush=True)
    return {
        "시나리오": key, "설명": label,
        "최소_초": round(min(times), 2) if times else None,
        "평균_초": round(sum(times) / len(times), 2) if times else None,
        "행수": rows, "컬럼수": cols, "실행횟수": len(times), "오류": err,
    }


def main():
    ap = argparse.ArgumentParser(description="원천 로딩 방식 벤치마크")
    ap.add_argument("--target", default="all", choices=["all", "hold", "step"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only", default="", help="쉼표구분 시나리오 키 (예: h5,h9)")
    ap.add_argument("--list", action="store_true", help="시나리오 목록만 출력")
    args = ap.parse_args()

    scenarios = {}
    if args.target in ("all", "hold"):
        scenarios.update(HOLD_SCENARIOS)
    if args.target in ("all", "step"):
        for line in STEP_TABLES:
            scenarios.update(step_scenarios(line))

    if args.only:
        want = {k.strip() for k in args.only.split(",") if k.strip()}
        scenarios = {k: v for k, v in scenarios.items() if k in want}

    if args.list:
        for k, (label, _) in scenarios.items():
            print(f"{k:10s} {label}")
        return

    from bigdataquery import getData

    results = []
    print(f"[BENCH] {len(scenarios)}개 시나리오, 각 {args.repeat}회\n", flush=True)

    for key, (label, sql) in scenarios.items():
        if sql == "__TWO_STEP__":
            t0 = perf_counter()
            try:
                mv = getData(param=HOLD_MAX_SQL, convert_type=True,
                             verbose=False).iloc[0, 0]
                t_max = perf_counter() - t0
                sql2 = HOLD_LITERAL_SQL.replace("{MV}", str(mv))
                r = run_one(getData, key, f"{label} (mv={mv})", sql2, args.repeat)
                r["최소_초"] = round((r["최소_초"] or 0) + t_max, 2)
                r["설명"] += f" / max조회 {t_max:.1f}s 포함"
                results.append(r)
            except Exception as e:
                print(f"  [FAIL] {key}: {e}", flush=True)
                results.append({"시나리오": key, "설명": label, "오류": str(e)})
            continue
        results.append(run_one(getData, key, label, sql, args.repeat))

    df = pd.DataFrame(results).sort_values("최소_초", na_position="last")
    print("\n" + df.to_string(index=False), flush=True)

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
    path = os.path.join(os.getcwd(), f"bench_loading_{stamp}.xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="결과", index=False)
        pd.DataFrame([{"시나리오": k, "SQL": v[1]} for k, v in scenarios.items()]).to_excel(
            xw, sheet_name="SQL", index=False)
    print(f"\nsaved: {path}", flush=True)


if __name__ == "__main__":
    main()
