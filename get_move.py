# -*- coding: utf-8 -*-
"""
get_move.py — MOVE 조회 및 적재

정의는 docs/common_conventions.md 참조.
  MOVE      = TrackOut wafer 매수, lot_type IN ('PP','PB','PG')
  Line 구분 = sys_line_id (line_id 는 cur/sys 합집합이라 그대로 쓰면 안 됨)
  구간      = TrackOut 시각(lot_transn_time) 기준
  shift 구간 = GY 22~06 / DAY 06~14 / SW 14~22 (한 업무일 3개 = 22~22)
  업무일    = 22:00 시작 / shift = GY(22) DAY(06) SW(14), 각 8시간

적재 방식
  - move_daily 가 비어 있으면 3개월치 (3개월 전 날짜가 속한 달의 1일부터)
  - 아니면 최근 2일치 (실행 시각이 밀려도 공백이 안 생김)
  - 해당 업무일 구간을 통째로 지우고 다시 넣는다 (중복 비교 없이 멱등)

사용:
    python get_move.py                          # 자동 판단
    python get_move.py --full                   # 3개월치 강제 재적재
    python get_move.py --days 5                 # 최근 N일
    python get_move.py --from 2026-05-01 --to 2026-05-31
    python get_move.py --dry-run                # 적재 없이 집계만 확인
"""

from __future__ import annotations

import argparse
import datetime as dt
from time import perf_counter

import pandas as pd

import db_common as DB

INIT_MONTHS = 3
INCREMENTAL_DAYS = 2
BOUNDARY_SHIFT = {22: "GY", 6: "DAY", 14: "SW"}
TARGET_LINES = ("KFR7", "PFR1")


def move_query(ts_from: dt.datetime, ts_to: dt.datetime) -> str:
    """TrackOut 시각 기준 구간 조회.

    주의: 원본은 tkin_date 로 필터했으나 그러면 오래 걸린 스텝의 TrackOut 이
    누락된다(3일 전 TrackIn -> 어제 TrackOut). 집계 기준과 동일한
    lot_transn_time 으로 자른다.
    """
    f = ts_from.strftime("%Y%m%d %H%M%S")
    t = ts_to.strftime("%Y%m%d %H%M%S")
    return f"""
SELECT
    sys_line_id,
    line_id,
    current_line_id                                    AS cur_line_id,
    lot_id,
    lot_type,
    component_qty                                      AS move,
    ppid,
    process_eqp_id,
    process_id,
    step_seq,
    step_desc,
    eqp_type,
    FROM_UNIXTIME(UNIX_TIMESTAMP(recent_tkout_time, 'yyyyMMdd HHmmss'))
                                                       AS recent_tkout_date,
    tkin_date,
    process_start_date,
    process_finish_date,
    FROM_UNIXTIME(UNIX_TIMESTAMP(lot_transn_time, 'yyyyMMdd HHmmss'))
                                                       AS tkout_date
FROM   FAB.M_LOT_TRANSN_HIST
WHERE  line_id IN ('KFR7', 'PFR1')          -- 파티션 프루닝용(PK)
  AND  sys_line_id IN ('KFR7', 'PFR1')      -- 실제 집계 기준
  AND  lot_transn_type = 'TrackOut'
  AND  lot_type IN ('PP', 'PB', 'PG')
  AND  lot_transn_time >= '{f}'
  AND  lot_transn_time <  '{t}'
"""


def resolve_range(conn, args):
    """조회 구간을 결정한다.

    끝은 '가장 최근에 지난 shift 기준시각'으로 맞춘다. 진행 중인 shift 를 반쯤
    담아두면 다음 실행 때 값이 바뀐다.
    """
    now = dt.datetime.now()
    bounds = DB.shift_boundaries(now, back_hours=26)
    ts_to = bounds[0][0] if bounds else now

    if args.ts_from and args.ts_to:
        return (dt.datetime.combine(args.ts_from, dt.time(22)) - dt.timedelta(days=1),
                dt.datetime.combine(args.ts_to, dt.time(22)))
    if args.days:
        return ts_to - dt.timedelta(days=args.days), ts_to
    if args.full or DB.move_last_biz_date(conn) is None:
        first = (ts_to.date() - dt.timedelta(days=INIT_MONTHS * 30)).replace(day=1)
        return dt.datetime.combine(first, dt.time(22)) - dt.timedelta(days=1), ts_to
    return ts_to - dt.timedelta(days=INCREMENTAL_DAYS), ts_to


def aggregate(df, ts_from, ts_to):
    """이벤트 단위 -> (업무일, shift, sys_line_id) 집계."""
    d = df.copy()
    d.columns = [str(c).lower() for c in d.columns]
    d["tkout_date"] = pd.to_datetime(d["tkout_date"], errors="coerce")
    d = d.dropna(subset=["tkout_date"])
    # line_id 는 cur_line_id / sys_line_id 의 합집합이라 다른 라인이 섞여 온다.
    # MOVE 의 Line 기준은 sys_line_id 이므로 여기서 한 번 더 거른다.
    d = d[d["sys_line_id"].isin(TARGET_LINES)]
    d["move"] = pd.to_numeric(d["move"], errors="coerce").fillna(0)

    rows = []
    boundary = ts_to - dt.timedelta(hours=8)
    while boundary >= ts_from:
        lo, hi = DB.shift_window(boundary)
        shift = BOUNDARY_SHIFT.get(boundary.hour)
        if shift:
            bd = DB.biz_date(boundary)
            chunk = d[(d["tkout_date"] > lo) & (d["tkout_date"] <= hi)]
            for line, g in chunk.groupby("sys_line_id", dropna=True):
                rows.append({"biz_date": bd, "shift": shift, "sys_line_id": line,
                             "move_qty": int(g["move"].sum()),
                             "lot_cnt": int(g["lot_id"].nunique())})
        boundary -= dt.timedelta(hours=8)

    df_shift = pd.DataFrame(rows, columns=["biz_date", "shift", "sys_line_id",
                                           "move_qty", "lot_cnt"])
    if len(df_shift):
        df_daily = (df_shift.groupby(["biz_date", "sys_line_id"], as_index=False)
                    .agg(move_qty=("move_qty", "sum"), lot_cnt=("lot_cnt", "sum")))
    else:
        df_daily = pd.DataFrame(columns=["biz_date", "sys_line_id", "move_qty", "lot_cnt"])
    return df_shift, df_daily


def main():
    ap = argparse.ArgumentParser(description="MOVE 조회/적재")
    ap.add_argument("--full", action="store_true", help=f"{INIT_MONTHS}개월치 재적재")
    ap.add_argument("--days", type=int, default=0, help="최근 N일")
    ap.add_argument("--from", dest="ts_from", type=dt.date.fromisoformat)
    ap.add_argument("--to", dest="ts_to", type=dt.date.fromisoformat)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from bigdataquery import getData

    conn = DB.connect()
    DB.ensure_move_schema(conn)

    ts_from, ts_to = resolve_range(conn, args)
    print(f"[MOVE] 조회구간 {ts_from:%Y-%m-%d %H:%M} ~ {ts_to:%Y-%m-%d %H:%M}", flush=True)

    t0 = perf_counter()
    df = getData(param=move_query(ts_from, ts_to), convert_type=True, verbose=True)
    print(f"[MOVE] 원천 {len(df):,}행 {perf_counter() - t0:.1f}s", flush=True)

    df_shift, df_daily = aggregate(df, ts_from, ts_to)
    print(f"[MOVE] shift 집계 {len(df_shift):,}행 / 일 집계 {len(df_daily):,}행", flush=True)
    if len(df_daily):
        print(df_daily.tail(6).to_string(index=False), flush=True)

    if args.dry_run:
        print("[MOVE] --dry-run: 적재 생략", flush=True)
        conn.close()
        return

    biz_dates = sorted(set(df_shift["biz_date"])) if len(df_shift) else []
    DB.replace_move(conn, df_shift, df_daily, biz_dates)
    print(f"[MOVE] 적재 완료: 업무일 {len(biz_dates)}일 "
          f"({biz_dates[0] if biz_dates else '-'} ~ "
          f"{biz_dates[-1] if biz_dates else '-'})", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
