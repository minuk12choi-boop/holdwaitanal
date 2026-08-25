"""조인이 실제로 붙는지 미리 본다. 값은 내보내지 않는다.

오늘까지 겪은 문제 대부분이 '코드는 도는데 조인이 안 붙는' 것이었다.
    order_seq 가 NULL 이라 NULL=NULL 이 참이 아니어서 안 붙음
    STEPSEQ 표기가 010 과 0010 으로 달라 안 붙음
    대소문자가 달라 안 붙음
붙기 **전에** 몇 %가 맞는지 알면 고치기 전에 원인을 안다.

실행
    python getdata/joincheck.py f3_move_step f3_live lot_id
    python getdata/joincheck.py f3_live f3_move_step \\
        --left line,lot_id --right sys_line_id,lot_id
    python getdata/joincheck.py A B --left proc_id,step_seq \\
        --right process,step --fix upper,strip,lstrip0

--fix 로 무엇을 맞추면 붙는지 시험해 본다.
    upper    대소문자 무시
    strip    앞뒤 공백 제거
    lstrip0  앞자리 0 제거 (010 <-> 10)
    pad4     4자리로 0 채움 (010 -> 0010)
"""
from __future__ import annotations

import argparse

import db_common as DB


def _norm(v, fixes):
    """비교용으로만 다듬는다. 원본은 건드리지 않는다."""
    if v is None:
        return None
    s = str(v)
    if "strip" in fixes:
        s = s.strip()
    if "upper" in fixes:
        s = s.upper()
    if "lstrip0" in fixes:
        s = s.lstrip("0") or "0"
    if "pad4" in fixes:
        s = s.rjust(4, "0")
    return s


def _load(cur, table, cols, where, fixes):
    w = f" WHERE {where}" if where else ""
    sel = ", ".join(f"`{c}`" for c in cols)
    cur.execute(f"SELECT {sel} FROM `{table}`{w}")
    rows = cur.fetchall()
    keys, nulls = [], 0
    for r in rows:
        if any(x is None for x in r):
            nulls += 1
            continue
        keys.append(tuple(_norm(x, fixes) for x in r))
    return rows, keys, nulls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("left")
    ap.add_argument("right")
    ap.add_argument("on", nargs="?", default="",
                    help="양쪽 컬럼명이 같을 때. 쉼표로 여러 개")
    ap.add_argument("--left", dest="lc", default="")
    ap.add_argument("--right", dest="rc", default="")
    ap.add_argument("--lwhere", default="")
    ap.add_argument("--rwhere", default="")
    ap.add_argument("--fix", default="", help="upper,strip,lstrip0,pad4")
    a = ap.parse_args()

    lc = [x for x in (a.lc or a.on).split(",") if x]
    rc = [x for x in (a.rc or a.on).split(",") if x]
    if not lc or len(lc) != len(rc):
        ap.error("조인 컬럼을 양쪽 같은 개수로 주세요")
    fixes = {x for x in a.fix.split(",") if x}

    conn = DB.connect()
    try:
        with conn.cursor() as cur:
            lrows, lkeys, lnull = _load(cur, a.left, lc, a.lwhere, fixes)
            rrows, rkeys, rnull = _load(cur, a.right, rc, a.rwhere, fixes)
    finally:
        conn.close()

    rset = set(rkeys)
    hit = sum(1 for k in lkeys if k in rset)
    lset = set(lkeys)

    print(f"\n[조인] {a.left}({', '.join(lc)}) "
          f"<- {a.right}({', '.join(rc)})")
    if fixes:
        print(f"  맞춤: {', '.join(sorted(fixes))}")
    print(f"  왼쪽 {len(lrows):,}행 · 조합 {len(lset):,}")
    print(f"  오른쪽 {len(rrows):,}행 · 조합 {len(rset):,}")

    if lnull:
        print(f"  [!] 왼쪽 키에 NULL {lnull:,}행 "
              f"-> 이 행은 **절대 안 붙는다**(NULL=NULL 은 참이 아니다)")
    if rnull:
        print(f"  [!] 오른쪽 키에 NULL {rnull:,}행")

    if not lkeys:
        print("  왼쪽에 비교할 행이 없다.")
        return
    pct = hit / len(lkeys) * 100
    print(f"  매칭 {hit:,}/{len(lkeys):,}  ({pct:.1f}%)")

    if pct >= 99:
        print("  거의 다 붙는다.")
        return

    # 왜 안 붙는지 짚어 준다. 값은 내지 않고 '모양' 만 비교한다.
    #   두 가지를 함께 맞춰야 붙는 경우가 흔하므로 조합까지 시험한다.
    print("\n  [안 붙는 이유 짐작]")
    WHY = {"upper": "대소문자가 다르다",
           "strip": "앞뒤 공백이 있다",
           "lstrip0": "앞자리 0 유무가 다르다 (010 vs 10)",
           "pad4": "자릿수가 다르다 (010 vs 0010)"}
    cand = [k for k in WHY if k not in fixes]

    def rate(extra):
        f2 = fixes | set(extra)
        r2 = {tuple(_norm(x, f2) for x in k) for k in rkeys}
        h2 = sum(1 for k in lkeys if tuple(_norm(x, f2) for x in k) in r2)
        return h2 / len(lkeys) * 100

    from itertools import combinations
    best = []
    for n in (1, 2, 3):
        for combo in combinations(cand, n):
            # lstrip0 과 pad4 를 함께 쓰면 서로 상쇄된다.
            if "lstrip0" in combo and "pad4" in combo:
                continue
            p2 = rate(combo)
            if p2 > pct + 1:
                best.append((p2, combo))
    if not best:
        print("    맞춰서 붙는 조합을 못 찾았다. 키 자체가 다를 수 있다.")
        print("    dbshape.py 로 양쪽 컬럼의 길이·형태를 견줘 보세요.")
    else:
        best.sort(key=lambda x: (-x[0], len(x[1])))
        seen = set()
        for p2, combo in best[:4]:
            if combo[0] in seen and len(combo) > 1:
                continue
            seen.add(combo[0])
            why = " + ".join(WHY[c] for c in combo)
            print(f"    --fix {','.join(combo):22s} {p2:5.1f}% "
                  f"({p2 - pct:+.1f}%p)  -> {why}")
    print("\n  왼쪽에만 있는 조합 "
          f"{len(lset - rset):,} · 오른쪽에만 {len(rset - lset):,}")
    print("\n(값은 하나도 출력하지 않았습니다.)")


if __name__ == "__main__":
    main()
