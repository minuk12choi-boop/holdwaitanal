# -*- coding: utf-8 -*-
"""
diag.py — S3 raw 원천 진단

Oracle 전환 후 f3 결과가 Impala 시절과 크게 달라졌을 때, 어느 단계에서
벌어지는지 확인한다.

    python getdata/diag.py hold      HOLD 분포 (status_seq / item_type / 중복)
    python getdata/diag.py lot       LOT 분포 (lot_type / status / 라인)
    python getdata/diag.py tip       TIP.process <-> STEP_PATH.proc_id 겹침
    python getdata/diag.py wt        W/T=0 이 많은 이유 (f3_move_lot <-> f3 lot 매칭)
    python getdata/diag.py dates     날짜 컬럼 원본 표기 확인
    python getdata/diag.py all
"""

from __future__ import annotations

import sys

import pandas as pd

import s3_source

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 60)


def _head(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def diag_hold():
    df = s3_source.read_table("PFR1_KFR7_HOLD")
    _head(f"HOLD  {len(df):,}행 {df.shape[1]}컬럼")
    print("컬럼:", list(df.columns))

    for col in ("line_id", "item_type", "status_seq", "version_desc"):
        if col in df.columns:
            print(f"\n[{col}] 분포")
            print(df[col].astype(str).value_counts().head(15).to_string())

    if "lot_id" in df.columns:
        n_lot = df["lot_id"].nunique()
        print(f"\nlot_id 고유 = {n_lot:,}  (행 {len(df):,} / lot당 평균 "
              f"{len(df) / n_lot:.1f}행)")
        key = [c for c in ("item_type", "lot_id", "step_seq") if c in df.columns]
        if key:
            dup = df.duplicated(subset=key).sum()
            print(f"({', '.join(key)}) 중복 = {dup:,}행")
            if dup:
                print("→ 같은 lot 에 이슈가 여러 건. 스냅샷이 누적본일 수 있다.")

    # Impala 시절과의 비교 기준
    print("\n[참고] Impala 시절 수치")
    print("  raw 1,027행 → 재공 lot 한정 717행 → h1 395 / h2 147 / h3 49")
    print("  현재      7,219행 →           5,083행 → h1 3,613 / h2 621 / h3 247")


def diag_lot():
    df = s3_source.read_table("PFR1_KFR7_LOT")
    _head(f"LOT  {len(df):,}행 {df.shape[1]}컬럼")
    for col in ("line", "sys_line_id", "lot_type", "status"):
        if col in df.columns:
            print(f"\n[{col}] 분포")
            print(df[col].astype(str).value_counts().head(12).to_string())
    if "lot_id" in df.columns:
        print(f"\nlot_id 고유 = {df['lot_id'].nunique():,} / 행 {len(df):,}")
        d = df.duplicated(subset=["lot_id"]).sum()
        print(f"lot_id 중복 = {d:,}행" + ("  → 라인 간 중복이 남아 있다." if d else ""))


def diag_tip():
    """TIP.process 와 STEP_PATH.proc_id 가 라인별로 겹치는지 확인.

    Spotfire 에서 두 테이블을 조인해 TIP 을 줄일 때, 이 겹침이 없으면
    필요한 규칙이 통째로 사라진다.
    """
    tip = s3_source.read_table("PFR1_KFR7_TIP")
    sp = s3_source.read_table("PFR1_KFR7_STEP_PATH")
    _head(f"TIP {len(tip):,}행  vs  STEP_PATH {len(sp):,}행")

    lt = "line_id" if "line_id" in tip.columns else "line"
    ls = "line_id" if "line_id" in sp.columns else "line"

    print(f"\n[TIP.{lt}] 분포")
    print(tip[lt].astype(str).value_counts().to_string())
    print(f"\n[STEP_PATH.{ls}] 분포")
    print(sp[ls].astype(str).value_counts().to_string())

    print("\n[TIP process 값 유형]")
    for ln in sorted(tip[lt].astype(str).unique()):
        t = tip[tip[lt].astype(str) == ln]
        wild = int((t["process"].astype(str) == "-").sum())
        print(f"  {ln}: 전체 {len(t):,}  와일드카드('-') {wild:,}  실값 {len(t)-wild:,}")

    print("\n[process <-> proc_id 겹침]")
    for ln in sorted(set(tip[lt].astype(str)) | set(sp[ls].astype(str))):
        tp = set(tip.loc[tip[lt].astype(str) == ln, "process"].astype(str)) - {"-"}
        spp = set(sp.loc[sp[ls].astype(str) == ln, "proc_id"].astype(str))
        both = tp & spp
        print(f"  {ln}: TIP 고유 process {len(tp):,} / "
              f"STEP_PATH 고유 proc_id {len(spp):,} / 겹침 {len(both):,}")
        if tp and not both:
            print("      -> 겹치는 값이 없다. 조인 키가 잘못됐거나 값 형식이 다르다.")
            print(f"      TIP 예시  : {sorted(tp)[:5]}")
            print(f"      STEP 예시 : {sorted(spp)[:5]}")


def diag_dates():
    """날짜 계열 컬럼이 실제로 어떤 표기로 오는지 본다.

    표기가 바뀌면 파싱이 조용히 실패해 경과일이 NULL 이 된다.
    """
    targets = {
        "PFR1_KFR7_LOT": ["start_date", "last_tkout_date", "step_arrive_date",
                          "last_event_date", "query_time"],
        "PFR1_KFR7_EQUIPMENT": ["eqp_status_change_time"],
        "PFR1_KFR7_TIP": ["updated", "eventtime"],
        "PFR1_KFR7_MOVE": ["tkout_date", "recent_tkout_date"],
        "PFR1_KFR7_HOLD": ["issue_date"],
    }
    for name, cols in targets.items():
        try:
            df = s3_source.read_table(name)
        except Exception as e:
            print(f"  {name}: 읽기 실패 {e}")
            continue
        _head(f"{name}  {len(df):,}행")
        for c in cols:
            if c not in df.columns:
                print(f"  {c:24s} (컬럼 없음)")
                continue
            v = df[c].dropna().astype(str)
            ex = list(v.head(3))
            print(f"  {c:24s} dtype={str(df[c].dtype):16s} 예시={ex}")


def diag_wt():
    """W/T = 0 이 많은 원인 추적.

    W/T = f3_move_lot 의 lot 별 MOVE / 재공 매수.
    lot_id 가 매칭되지 않으면 MOVE 가 0 으로 잡혀 전부 WT=0 이 된다.
    """
    import db_common as DB

    conn = DB.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(snapshot_at) FROM f3_live")
            snap = cur.fetchone()[0]
            cur.execute("SELECT DISTINCT lot_id FROM f3_live WHERE snapshot_at=%s",
                        [snap])
            f3lots = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT MAX(biz_date) FROM f3_move_lot")
            bd = cur.fetchone()[0]
            cur.execute("SELECT lot_id, SUM(move_qty) FROM f3_move_lot "
                        "WHERE biz_date=%s GROUP BY lot_id", [bd])
            mv = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
    finally:
        conn.close()

    _head("W/T 진단")
    print(f"f3_live 스냅샷      : {snap}   lot {len(f3lots):,}개")
    print(f"f3_move_lot 업무일     : {bd}      lot {len(mv):,}개")
    hit = f3lots & set(mv)
    print(f"두 쪽 모두 있는 lot : {len(hit):,}")
    print(f"MOVE 가 없는 lot    : {len(f3lots - set(mv)):,}  -> 이들이 WT=0 이 된다")
    if f3lots and not hit:
        print("\n  겹치는 lot_id 가 하나도 없다. lot_id 형식이 다를 수 있다.")
        print("  f3_live  예시:", sorted(f3lots)[:5])
        print("  f3_move_lot 예시:", sorted(mv)[:5])


def main():
    what = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    if what in ("hold", "all"):
        diag_hold()
    if what in ("lot", "all"):
        diag_lot()
    if what in ("tip", "all"):
        diag_tip()
    if what in ("wt", "all"):
        diag_wt()
    if what in ("dates", "all"):
        diag_dates()


if __name__ == "__main__":
    main()
