"""f3_history 로 f3_wip_step 과거분을 채운다.

f3_wip_step 은 build_f3 가 도는 시점부터 쌓인다. 그 전 기간은 비어 있어
히트맵의 '재공' 이 오늘치만 나온다. f3_history 에 이미 남아 있는
스냅샷으로 한 번 채워 두면 그 뒤로는 자동으로 이어진다.

실행:  python getdata/backfill_wip.py            (최근 90일)
       python getdata/backfill_wip.py --days 30
"""
from __future__ import annotations

import argparse
import datetime as dt
from time import perf_counter

import db_common as DB


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    since = dt.date.today() - dt.timedelta(days=args.days)
    conn = DB.connect()
    DB.ensure_move_schema(conn)

    t0 = perf_counter()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT snapshot_at, biz_date, shift FROM f3_history "
            "WHERE biz_date >= %s ORDER BY snapshot_at", (since,))
        snaps = cur.fetchall()
    print(f"[WIP] 대상 스냅샷 {len(snaps):,}개 (>= {since})", flush=True)

    now = dt.datetime.now()
    total = 0
    with conn.cursor() as cur:
        for i, (snap, bd, sh) in enumerate(snaps, 1):
            cur.execute("DELETE FROM f3_wip_step WHERE snapshot_at=%s", (snap,))
            # 현스텝 행만 센다. 연속블록은 아직 오지 않은 스텝이다.
            cur.execute("""
                INSERT INTO f3_wip_step
                  (snapshot_at, biz_date, shift, `line`, prod2, proc_id,
                   step_seq, layer_id, module1, area, wip_qty, lot_cnt,
                   loaded_at)
                SELECT %s, %s, %s, `line`,
                       COALESCE(NULLIF(TRIM(prod2), ''), '-'),
                       COALESCE(NULLIF(TRIM(proc_id), ''), '-'),
                       COALESCE(NULLIF(TRIM(step_seq), ''), '-'),
                       MAX(layer_id), MAX(module1), MAX(AREA),
                       SUM(qty), COUNT(DISTINCT lot_id), %s
                FROM   f3_history
                WHERE  snapshot_at = %s AND `현스텝` = '현스텝'
                GROUP  BY `line`,
                          COALESCE(NULLIF(TRIM(prod2), ''), '-'),
                          COALESCE(NULLIF(TRIM(proc_id), ''), '-'),
                          COALESCE(NULLIF(TRIM(step_seq), ''), '-')
            """, (snap, bd, sh, now, snap))
            total += cur.rowcount
            if i % 20 == 0 or i == len(snaps):
                print(f"[WIP] {i}/{len(snaps)}  누적 {total:,}행", flush=True)
                conn.commit()
    conn.commit()
    conn.close()
    print(f"[WIP] 완료 {total:,}행  {perf_counter() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
