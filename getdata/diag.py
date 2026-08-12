# -*- coding: utf-8 -*-
"""
diag.py — S3 raw 원천 진단

Oracle 전환 후 f3 결과가 Impala 시절과 크게 달라졌을 때, 어느 단계에서
벌어지는지 확인한다.

    python getdata/diag.py hold      HOLD 분포 (status_seq / item_type / 중복)
    python getdata/diag.py lot       LOT 분포 (lot_type / status / 라인)
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


if __name__ == "__main__":
    main()
