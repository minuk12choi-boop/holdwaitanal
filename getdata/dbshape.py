"""데이터 '모양' 점검기. 값은 내보내지 않는다.

[파일명 주의] inspect.py 로 두면 **파이썬 표준 모듈 inspect 를 가린다.**
같은 폴더에서 다른 스크립트를 돌릴 때 numpy·pandas 가 통째로 깨진다.
표준 모듈과 겹치는 이름(inspect, types, json, code ...)은 쓰지 않는다.

값 자체는 사내 자료라 밖으로 낼 수 없다. 그래도 **모양**은 낼 수 있다.
NULL 비율 · 고유값 수 · 길이 · 표기 형태 같은 것은 값이 아니다.
이 출력만 있으면 조인이 왜 안 붙는지, 어느 컬럼이 비는지 판단할 수 있다.

실행
    python getdata/dbshape.py f3_live
    python getdata/dbshape.py f3_live --cols order_seq,proc_id,step_seq
    python getdata/dbshape.py f3_move_step --group sys_line_id
    python getdata/dbshape.py f3_live --key line,lot_id,order_seq

내보내는 것 / 내보내지 않는 것
    낸다     행 수 · NULL 비율 · 고유값 수 · 길이 최소/최대 · 표기 형태
             (숫자만/영문만/섞임) · 앞뒤 공백 여부 · 대소문자 섞임 여부
    안 낸다  실제 값, 값의 일부, 값을 짐작할 수 있는 어떤 것도
"""
from __future__ import annotations

import argparse
import re

import db_common as DB


def _shape(v):
    """값 하나를 '모양' 문자로 바꾼다. 값은 남기지 않는다."""
    s = str(v)
    if re.fullmatch(r"\d+", s):
        return "숫자"
    if re.fullmatch(r"[A-Za-z]+", s):
        return "영문"
    if re.fullmatch(r"[A-Za-z0-9]+", s):
        return "영숫자"
    if re.fullmatch(r"[가-힣\s]+", s):
        return "한글"
    return "섞임"


def _cols_of(cur, table):
    cur.execute(f"SELECT * FROM `{table}` LIMIT 0")
    return [d[0] for d in cur.description]


def profile(cur, table, cols, where=""):
    """컬럼마다 모양을 잰다."""
    w = f" WHERE {where}" if where else ""
    cur.execute(f"SELECT COUNT(*) FROM `{table}`{w}")
    total = cur.fetchone()[0]
    print(f"\n[표] {table}  {total:,}행{('  (' + where + ')') if where else ''}")
    if not total:
        return
    print(f"  {'컬럼':22s} {'NULL%':>7s} {'빈값%':>7s} {'고유':>9s} "
          f"{'길이':>9s}  형태")
    print("  " + "-" * 74)

    for c in cols:
        cur.execute(
            f"SELECT COUNT(*), "
            f"       SUM(CASE WHEN `{c}` IS NULL THEN 1 ELSE 0 END), "
            f"       SUM(CASE WHEN `{c}` IS NOT NULL "
            f"            AND TRIM(CAST(`{c}` AS CHAR))='' THEN 1 ELSE 0 END), "
            f"       COUNT(DISTINCT `{c}`), "
            f"       MIN(CHAR_LENGTH(CAST(`{c}` AS CHAR))), "
            f"       MAX(CHAR_LENGTH(CAST(`{c}` AS CHAR))) "
            f"FROM `{table}`{w}")
        n, nul, emp, uniq, lmin, lmax = cur.fetchone()
        nul, emp = int(nul or 0), int(emp or 0)

        # 형태는 표본 200개만 본다(값은 즉시 버린다).
        cur.execute(f"SELECT `{c}` FROM `{table}`{w} "
                    f"{'AND' if where else 'WHERE'} `{c}` IS NOT NULL "
                    f"LIMIT 200")
        kinds, pad, mixed = set(), False, False
        for (v,) in cur.fetchall():
            s = str(v)
            kinds.add(_shape(s))
            if s != s.strip():
                pad = True
            if s != s.upper() and s != s.lower():
                mixed = True

        flag = []
        if pad:
            flag.append("앞뒤공백")
        if mixed:
            flag.append("대소문자섞임")
        rng = f"{lmin or 0}~{lmax or 0}"
        print(f"  {c:22s} {nul / n * 100:6.1f}% {emp / n * 100:6.1f}% "
              f"{uniq:9,} {rng:>9s}  {'/'.join(sorted(kinds)) or '-'}"
              f"{('  [' + ' '.join(flag) + ']') if flag else ''}")


def keycheck(cur, table, keys, where=""):
    """이 컬럼들이 행을 유일하게 가르는지 본다."""
    w = f" WHERE {where}" if where else ""
    k = ", ".join(f"`{x}`" for x in keys)
    # COUNT(DISTINCT a, b) 는 MySQL 전용이다. 부분질의로 세면 어디서나 된다.
    cur.execute(f"SELECT COUNT(*) FROM `{table}`{w}")
    n = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM "
                f"(SELECT 1 FROM `{table}`{w} GROUP BY {k}) t")
    u = cur.fetchone()[0]
    print(f"\n[키] ({', '.join(keys)})  행 {n:,} · 조합 {u:,}")
    if n == u:
        print("  유일하다.")
        return
    print(f"  **중복 {n - u:,}행.** 이 키로 GROUP BY 하면 서로 다른 행이 뭉친다.")
    # NULL 이 섞이면 조인이 통째로 실패한다. 그것만 따로 센다.
    for x in keys:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`{w} "
                    f"{'AND' if where else 'WHERE'} `{x}` IS NULL")
        c = cur.fetchone()[0]
        if c:
            print(f"  {x} 가 NULL 인 행 {c:,} "
                  f"-> SQL 에서 NULL=NULL 은 참이 아니라 조인이 안 붙는다.")


def groupcheck(cur, table, col, where=""):
    """값별 건수. 값 자체는 감추고 순위와 비율만 낸다."""
    w = f" WHERE {where}" if where else ""
    cur.execute(f"SELECT COUNT(*) FROM `{table}`{w}")
    total = cur.fetchone()[0] or 1
    cur.execute(f"SELECT COUNT(*) c FROM `{table}`{w} "
                f"GROUP BY `{col}` ORDER BY c DESC LIMIT 15")
    rows = cur.fetchall()
    print(f"\n[분포] {col}  상위 {len(rows)}개 (값은 감춤)")
    for i, (c,) in enumerate(rows, 1):
        bar = "#" * max(1, int(c / total * 40))
        print(f"  {i:2d}위 {c:9,}  {c / total * 100:5.1f}%  {bar}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table")
    ap.add_argument("--cols", default="", help="쉼표로 구분. 비우면 전부")
    ap.add_argument("--key", default="", help="유일성을 볼 컬럼들")
    ap.add_argument("--group", default="", help="값별 건수를 볼 컬럼")
    ap.add_argument("--where", default="", help="조건(선택)")
    a = ap.parse_args()

    conn = DB.connect()
    try:
        with conn.cursor() as cur:
            have = _cols_of(cur, a.table)
            cols = [c for c in (a.cols.split(",") if a.cols else have)
                    if c and c in have]
            miss = [c for c in a.cols.split(",") if c and c not in have]
            if miss:
                print(f"[!] 없는 컬럼: {', '.join(miss)}")
            profile(cur, a.table, cols, a.where)
            if a.key:
                keycheck(cur, a.table, [x for x in a.key.split(",") if x],
                         a.where)
            if a.group:
                groupcheck(cur, a.table, a.group, a.where)
    finally:
        conn.close()
    print("\n(값은 하나도 출력하지 않았습니다. 이 결과는 그대로 공유해도 됩니다.)")


if __name__ == "__main__":
    main()
