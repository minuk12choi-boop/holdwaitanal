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


def diag_lotwt(lot_id):
    """한 lot 이 왜 WT=0 인지 추적한다.

    f3 쪽 값 / f3_move_lot 적재분 / MOVE 원천을 나란히 보여준다.
    """
    import db_common as DB

    if not lot_id:
        print("사용: python getdata/diag.py lotwt 1DFSE01.1")
        return

    _head(f"lot {lot_id} MOVE 매칭 추적")
    conn = DB.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(snapshot_at) FROM f3_live")
            snap = cur.fetchone()[0]
            cur.execute(
                "SELECT `line`, lot_id, lot_status, qty, `마지막작업경과_일` "
                "FROM f3_live WHERE snapshot_at=%s AND lot_id=%s LIMIT 3",
                [snap, lot_id])
            rows = cur.fetchall()
            print(f"[f3_live] snapshot={snap}")
            if rows:
                for r in rows:
                    print(f"   line={r[0]} lot={r[1]!r} status={r[2]} qty={r[3]} "
                          f"마지막작업경과일={r[4]}")
            else:
                print("   해당 lot 없음. lot_id 표기를 확인한다.")

            cur.execute("SELECT MAX(biz_date) FROM f3_move_lot")
            bd = cur.fetchone()[0]
            print(f"\n[f3_move_lot] 화면이 쓰는 업무일 = {bd}")
            cur.execute("SELECT biz_date, shift, sys_line_id, lot_id, move_qty "
                        "FROM f3_move_lot WHERE lot_id=%s "
                        "ORDER BY biz_date DESC, shift LIMIT 10", [lot_id])
            mv = cur.fetchall()
            if mv:
                for r in mv:
                    mark = "  <- 화면 대상" if r[0] == bd else ""
                    print(f"   {r[0]} {r[1]:3s} {r[2]} qty={r[4]}{mark}")
            else:
                print("   적재된 MOVE 가 없다.")

            # lot_id 표기 차이(공백/대소문자) 확인
            cur.execute("SELECT DISTINCT lot_id FROM f3_move_lot "
                        "WHERE REPLACE(UPPER(lot_id),' ','')=%s LIMIT 5",
                        [lot_id.upper().replace(" ", "")])
            same = [r[0] for r in cur.fetchall()]
            if same and same != [lot_id]:
                print(f"\n   표기가 다른 같은 lot: {same}")

            # 웹과 똑같이 계산해 본다
            cur.execute("SELECT MIN(qty) FROM f3_live "
                        "WHERE snapshot_at=%s AND lot_id=%s", [snap, lot_id])
            qrow = cur.fetchone()
            cur.execute("SELECT SUM(move_qty) FROM f3_move_lot "
                        "WHERE biz_date=%s AND lot_id=%s", [bd, lot_id])
            mrow = cur.fetchone()
            try:
                q = int(float(qrow[0])) if qrow and qrow[0] is not None else 0
            except (TypeError, ValueError):
                q = 0
            m = float(mrow[0]) if mrow and mrow[0] is not None else 0.0
            wt = (m / q) if q else 0.0
            print(f"\n[W/T 계산] MOVE {m:g} / 재공 {q} = {wt:.2f}")
            if not q:
                print("   재공 수량이 0 이라 W/T 가 0 이 된다. qty 를 확인한다.")
            elif not m:
                print(f"   업무일 {bd} 에 이 lot 의 MOVE 가 없다.")
            else:
                print("   값이 0 이 아닌데 화면이 0 이면 웹이 옛 코드로 떠 있는 것이다.")
                print("   waitress 를 재시작한다.")
    finally:
        conn.close()

    print("\n[MOVE 원천] 최근 구간을 직접 조회하려면:")
    print("   python getdata/get_move.py --hours 24   (재적재 후 다시 확인)")


def diag_lotst(lot_id):
    """lot 의 상태가 왜 그렇게 매겨졌는지 추적한다.

    f3 는 lot 단위로 하나의 상태를 갖는데, 그 값은 **현스텝 행**에서 온다.
    연속블록 행까지 함께 보여 어느 행이 상태를 정했는지 드러낸다.
    """
    import db_common as DB

    if not lot_id:
        print("사용: python getdata/diag.py lotst 7FBAZ12.1")
        return

    _head(f"lot {lot_id} 상태 판정 추적")
    conn = DB.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(snapshot_at) FROM f3_live")
            snap = cur.fetchone()[0]
            cur.execute(
                "SELECT `현스텝`, `연속`, order_seq, step_seq, layer_id,"
                " lot_status, step_status, `hold`, hold_reason,"
                " `exception`, exception_reason, ftp, ftp_reason,"
                " down, tip, eqpgroup, eqpgroup_cham, recipe_id, step_desc"
                " FROM f3_live WHERE snapshot_at=%s AND lot_id=%s"
                " ORDER BY CAST(order_seq AS SIGNED)", [snap, lot_id])
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]

        if not rows:
            print("해당 lot 이 f3_live 에 없다.")
            return

        print(f"snapshot={snap}   행 {len(rows)}개 (현스텝 + 연속블록)\n")
        for r in rows:
            mark = "★현스텝" if r["현스텝"] == "현스텝" else "  연속  "
            print(f"{mark} order_seq={r['order_seq']} step={r['step_seq']} "
                  f"layer={r['layer_id']}")
            print(f"         lot_status={r['lot_status']!r} "
                  f"step_status={r['step_status']!r}")
            flags = [(k, r[k]) for k in
                     ("hold", "exception", "ftp", "down", "tip") if r[k]]
            print(f"         플래그: {flags or '없음'}")
            for k in ("hold_reason", "exception_reason", "ftp_reason"):
                if r[k]:
                    print(f"         {k}={r[k]}")
            print(f"         eqpgroup={r['eqpgroup']} cham={r['eqpgroup_cham']}")
            print(f"         recipe={r['recipe_id']} desc={r['step_desc']}")
            print()

        # 웹이 lot 단위로 접을 때 쓰는 값(현스텝 행)과 비교한다.
        cur_row = next((r for r in rows if r["현스텝"] == "현스텝"), rows[0])
        others = {r["lot_status"] for r in rows
                  if r["현스텝"] != "현스텝" and r["lot_status"]}
        if others - {cur_row["lot_status"]}:
            print("[참고] 연속블록 행의 상태:", sorted(others))
            print("       웹 표는 현스텝 행 값을 쓴다. 다르면 화면과 대조한다.\n")
        print("-" * 70)
        print("[판정]")
        st = cur_row["lot_status"]
        print(f"  f3 가 매긴 상태 = {st!r}")
        if cur_row["hold"] and st != "HOLD":
            print("  !! hold 플래그가 있는데 상태가 HOLD 가 아니다.")
            print("     f1_status_base 의 CASE 순서상 앞선 조건이 먼저 걸렸다.")
            print("     흔한 원인:")
            print("       - 가상스텝 판정(설비그룹/recipe/step 에 WAIT 포함)")
            print("       - 예약제외/FTP 로 모든 path 가 막힘(issue>=path)")
            txt = " ".join(str(cur_row[k] or "") for k in
                           ("eqpgroup", "eqpgroup_cham", "recipe_id",
                            "step_seq", "step_desc")).upper()
            if "WAIT" in txt:
                print("     -> 가상스텝 조건에 걸렸다(위 값 중 WAIT 포함).")
            else:
                print("     -> 가상스텝은 아니다. path/issue 집계를 확인한다.")
        elif st == "HOLD":
            print("  hold 플래그와 상태가 일치한다.")
        print("\n  원인 분석은 hold 플래그가 있으면 상태와 무관하게 'Hold' 로")
        print("  분류한다(classify_lot). 그래서 상태 색과 원인이 다를 수 있다.")
    finally:
        conn.close()


def diag_tree(line=""):
    """지금 데이터로 만들어지는 **원인 분류 체계 전체**를 찍는다.

    어느 항목에 하위가 있고(=드릴다운 가능) 어디가 말단인지 한눈에 본다.
    웹의 classify_lot 을 그대로 불러 쓰므로 화면과 어긋나지 않는다.
    """
    import os
    web = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web")
    sys.path.insert(0, web)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from flowmonitor import views as V

    line = line or V.DEFAULT_LINE
    _head(f"원인 분류 체계 ({line})")
    rows, snap = V._summary_rows(line, list(V.DEFAULT_LOT_TYPES))
    if not rows:
        print("재공이 없다.")
        return
    rules, ht = V._cause_rules(), V._holdtype_rules()

    tree, tot = {}, {"lots": 0, "qty": 0}
    for r in rows:
        q = V.num(r.get("qty"))
        big, mid, subs = V.classify_lot(r, rules, ht)
        if not big:
            continue
        tot["lots"] += 1
        tot["qty"] += q
        g = tree.setdefault(big, {"lots": 0, "qty": 0, "mid": {}})
        g["lots"] += 1
        g["qty"] += q
        m = g["mid"].setdefault(mid or "(중분류 없음)",
                                {"lots": 0, "qty": 0, "sub": {}})
        m["lots"] += 1
        m["qty"] += q
        for sname in (subs or ["(소분류 없음)"]):
            sname = str(sname).strip() or "(빈 값)"
            sc = m["sub"].setdefault(sname, {"lots": 0, "qty": 0})
            sc["lots"] += 1
            sc["qty"] += q

    print(f"snapshot={snap}   대상 {tot['lots']:,} LOT / {tot['qty']:,} 매\n")
    print("표기:  [드릴]  = 하위가 있어 더 파고들 수 있다")
    print("       [말단]  = 더 쪼갤 수 없다 -> 바로 LOT 표로 간다\n")

    for big in sorted(tree, key=lambda k: -tree[k]["qty"]):
        g = tree[big]
        mids = g["mid"]
        # 중분류가 '(중분류 없음)' 하나뿐이면 대분류 바로 아래가 소분류다.
        flat = len(mids) == 1 and "(중분류 없음)" in mids
        print(f"■ {big}   {g['lots']:,} LOT / {g['qty']:,} 매"
              f"   {'[소분류 직결]' if flat else '[중분류 %d개]' % len(mids)}")
        for mid in sorted(mids, key=lambda k: -mids[k]["qty"]):
            m = mids[mid]
            subs = m["sub"]
            leaf = len(subs) <= 1
            tag = "[말단]" if leaf else f"[드릴] 소분류 {len(subs)}개"
            if not flat:
                print(f"   ├ {mid:<18} {m['lots']:>6,} LOT / "
                      f"{m['qty']:>8,} 매   {tag}")
            top = sorted(subs.items(), key=lambda kv: -kv[1]["qty"])[:8]
            for k, v in top:
                pre = "   │   └" if not flat else "   ├"
                print(f"{pre} {k:<20} {v['lots']:>6,} LOT / {v['qty']:>8,} 매")
            if len(subs) > 8:
                print(f"   │      ... 외 {len(subs) - 8}개")
        print()

    print("-" * 70)
    print("[요약] 드릴다운이 한 번 더 되는 항목")
    for big, g in tree.items():
        for mid, m in g["mid"].items():
            if len(m["sub"]) > 1:
                print(f"   {big} > {mid}  -> 소분류 {len(m['sub'])}개")


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
    if what == "lotwt":
        diag_lotwt(sys.argv[2] if len(sys.argv) > 2 else "")
    if what == "lotst":
        diag_lotst(sys.argv[2] if len(sys.argv) > 2 else "")
    if what == "tree":
        diag_tree(sys.argv[2] if len(sys.argv) > 2 else "KFR4")
    if what in ("dates", "all"):
        diag_dates()


if __name__ == "__main__":
    main()
