# -*- coding: utf-8 -*-
"""
diag.py — S3 raw 원천 진단

Oracle 전환 후 f3 결과가 Impala 시절과 크게 달라졌을 때, 어느 단계에서
벌어지는지 확인한다.

    python getdata/diag.py hold      HOLD 분포 (status_seq / item_type / 중복)
    python getdata/diag.py lot       LOT 분포 (lot_type / status / 라인)
    python getdata/diag.py tip       TIP.process <-> STEP_PATH.proc_id 겹침
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


def main():
    what = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    if what in ("hold", "all"):
        diag_hold()
    if what in ("lot", "all"):
        diag_lot()
    if what in ("tip", "all"):
        diag_tip()


if __name__ == "__main__":
    main()


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
