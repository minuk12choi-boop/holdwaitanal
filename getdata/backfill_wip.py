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

    filled = fill_gaps(conn, since, now)
    conn.commit()
    conn.close()
    print(f"[WIP] 완료 {total:,}행 (빈 SHIFT 메움 {filled:,}행)  "
          f"{perf_counter() - t0:.1f}s", flush=True)


def fill_gaps(conn, since, now):
    """f3_history 에 없는 SHIFT 를 **가장 가까운 스냅샷**으로 채운다.

    파이프라인이 멈춘 SHIFT 는 f3_history 에 아무것도 안 남는다. 그대로
    두면 히트맵의 그 칸이 영영 빈다. 재공은 몇 시간 사이 크게 변하지
    않으므로, 가장 가까운 시각의 재공으로 메우는 편이 빈칸보다 낫다.

    메운 행은 loaded_at 이 아니라 **snapshot_at 이 그 SHIFT 기준시각**이라
    원본과 구분된다. 나중에 진짜 데이터가 들어오면 그것이 이긴다.
    """
    today = DB.biz_date(dt.datetime.now())
    with conn.cursor() as cur:
        # 이미 채워진 (biz_date, shift)
        cur.execute("SELECT DISTINCT biz_date, shift FROM f3_wip_step "
                    "WHERE biz_date >= %s", (since,))
        have = {(str(a), b) for a, b in cur.fetchall()}
        # 채울 후보가 될 스냅샷(있는 것 전부)
        cur.execute("SELECT DISTINCT snapshot_at, biz_date, shift "
                    "FROM f3_wip_step WHERE biz_date >= %s", (since,))
        # 원본 snapshot_at 값을 그대로 들고 있어야 다시 조회할 때 맞는다.
        #   비교용 datetime 은 따로 만든다.
        src = [(_as_dt(a), str(b), c, a) for a, b, c in cur.fetchall()]
        src = [x for x in src if x[0] is not None]
    if not src:
        print("[WIP] 채울 원본이 없다. 건너뛴다.", flush=True)
        return 0

    want = []
    d = since
    while d <= today:
        for sh in ("GY", "DAY", "SW"):
            if (str(d), sh) not in have:
                want.append((d, sh))
        d += dt.timedelta(days=1)
    if not want:
        return 0

    total = 0
    with conn.cursor() as cur:
        for bd, sh in want:
            at = DB.shift_boundary(bd, sh) if hasattr(DB, "shift_boundary") \
                else _boundary(bd, sh)
            # 가장 가까운 원본 스냅샷
            near = min(src, key=lambda x: abs((x[0] - at).total_seconds()))
            gap_h = abs((near[0] - at).total_seconds()) / 3600.0
            if gap_h > MAX_GAP_HOURS:
                continue          # 너무 멀면 채우지 않는다(엉뚱한 값이 된다)
            cur.execute("DELETE FROM f3_wip_step "
                        "WHERE biz_date=%s AND shift=%s", (bd, sh))
            cur.execute("""
                INSERT INTO f3_wip_step
                  (snapshot_at, biz_date, shift, `line`, prod2, proc_id,
                   step_seq, layer_id, module1, area, wip_qty, lot_cnt,
                   loaded_at)
                SELECT %s, %s, %s, `line`, prod2, proc_id, step_seq,
                       layer_id, module1, area, wip_qty, lot_cnt, %s
                FROM   f3_wip_step WHERE snapshot_at = %s
            """, (at, bd, sh, now, near[3]))
            n = cur.rowcount
            total += n
            print(f"[WIP]   {bd} {sh} <- {near[1]} {near[2]} "
                  f"({gap_h:.1f}시간 차) {n:,}행", flush=True)
    return total


def _as_dt(v):
    """드라이버가 문자열로 줄 수도 있어 datetime 으로 맞춘다."""
    if isinstance(v, dt.datetime):
        return v
    t = str(v or "")[:19]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(t, f)
        except ValueError:
            continue
    return None


# 이 시간을 넘게 떨어진 스냅샷으로는 채우지 않는다. 하루가 넘게 벌어지면
# 그 사이 재공이 크게 달라져 오히려 잘못된 그림이 된다.
MAX_GAP_HOURS = 20

# SHIFT 기준시각. GY 는 전날 22시다.
SHIFT_HOUR = {"GY": 22, "DAY": 6, "SW": 14}


def _boundary(bd, sh):
    h = SHIFT_HOUR.get(sh, 6)
    d = bd - dt.timedelta(days=1) if sh == "GY" else bd
    return dt.datetime.combine(d, dt.time(h))


if __name__ == "__main__":
    main()
