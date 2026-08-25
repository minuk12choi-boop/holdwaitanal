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
  - f3_move_daily 가 비어 있으면 3개월치 (3개월 전 날짜가 속한 달의 1일부터)
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

# 원천 조달 경로. build_f3.py 와 동일한 규칙.
#   "s3"  : Spotfire 가 올린 PFR1_KFR7_MOVE 를 읽는다
#   "bdq" : 기존 bigdataquery 로 Impala 조회
SOURCE = "s3"
S3_TABLE = "PFR1_KFR7_MOVE"

INIT_MONTHS = 3

# 2시간 주기 실행 기준 증분 조회 폭(시간).
#   진행 중인 shift(최대 8h) + 직전 shift(8h) 를 덮고도 남는 여유를 둔다.
#   실행이 몇 시간 밀려도 공백이 생기지 않는다. (교체 적재라 겹쳐도 무해)
# 증분 조회 창. 실행 주기(30분)보다 넉넉하되 과하지 않게.
#   시작은 이 값 이전이 속한 shift 의 시작으로 맞춰지므로,
#   실제 창은 최소 이 값 ~ 최대 (이 값 + 8시간) 이 된다.
#   1 이면 진행 중 shift 전체(최대 8시간)를 다시 읽는다. 그것으로 충분하다.
INCREMENTAL_HOURS = 1
BOUNDARY_SHIFT = {22: "GY", 6: "DAY", 14: "SW"}
TARGET_LINES = ("KFR7", "PFR1")


def _s(sr):
    """비교용 문자열."""
    return sr.astype("string").fillna("").str.strip()


def mark_rework(d):
    """REWORK 여부를 매긴다. 세 가지 중 하나면 REWORK 다.

      1) rework_yn = 'Y'
      2) step_seq 가 'RW' 로 시작
      3) 같은 lot · PLAN · step_seq · ppid 의 두 번째 이후 진행
    """
    d = d.sort_values(["lot_id", "tkout_date"], kind="mergesort").copy()
    yn = _s(d.get("rework_yn", pd.Series("", index=d.index))).str.upper().eq("Y")
    rw = _s(d["step_seq"]).str.upper().str.startswith("RW")
    dup = d.groupby(
        [_s(d["lot_id"]), _s(d["process_id"]), _s(d["step_seq"]),
         _s(d.get("ppid", pd.Series("", index=d.index)))],
        sort=False).cumcount().gt(0)
    d["is_rework"] = yn | rw | dup
    return d


def attach_layer(d):
    """LAYER 를 붙인다.

    정상 스텝은 step_seq 의 3~5 번째 글자가 곧 LAYER 다.
    REWORK 스텝은 그 자리에 LAYER 가 없으므로, 같은 lot 에서
    **직전에 나온 정상 LAYER** 를 물려받는다.
    RW 가 몇 개 이어져도 ffill 한 번으로 해결된다.
    """
    ss = _s(d["step_seq"])
    lay = ss.str.slice(2, 5)
    # RW 로 시작하거나 세 글자가 안 되면 값이 없는 것으로 본다.
    bad = ss.str.upper().str.startswith("RW") | lay.str.len().lt(3)
    lay = lay.mask(bad, pd.NA)

    d = d.sort_values(["lot_id", "tkout_date"], kind="mergesort").copy()
    d["layer_id"] = lay.reindex(d.index)
    g = d.groupby("lot_id", sort=False)["layer_id"]
    d["layer_id"] = g.ffill()
    # lot 의 첫 스텝이 RW 면 앞이 비어 있다. 뒤쪽 값으로 한 번 더 채운다.
    d["layer_id"] = d.groupby("lot_id", sort=False)["layer_id"].bfill()
    d["layer_id"] = d["layer_id"].fillna("(미상)")
    return d


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
    rework_yn,
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


def shift_start_at_or_before(ts):
    """ts 가 속한 shift 의 시작 시각."""
    for h in (22, 14, 6):
        b = ts.replace(hour=h, minute=0, second=0, microsecond=0)
        if b <= ts:
            return b
    return (ts - dt.timedelta(days=1)).replace(hour=22, minute=0,
                                               second=0, microsecond=0)


def _append_load_log(conn, rows):
    """f3_load_log 에 MOVE 구간을 덧붙인다.

    build_f3 가 이미 그 스냅샷 행을 써 둔 뒤라 자기 행만 지우고 INSERT 한다.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(snapshot_at) FROM f3_live")
        r = cur.fetchone()
        snap = r[0] if r else None
        if not snap:
            return
        names = tuple(x[0] for x in rows)
        ph = ",".join(["%s"] * len(names))
        cur.execute(f"DELETE FROM f3_load_log WHERE snapshot_at=%s "
                    f"AND table_name IN ({ph})", (snap,) + names)
        cur.executemany(
            "INSERT INTO f3_load_log (snapshot_at, table_name, load_start, load_end,"
            " elapsed_sec, row_count, col_count, kind, query_time)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL)",
            [(snap, n, st, en, round((en - st).total_seconds(), 2), rc, cc, kind)
             for n, st, en, rc, cc, kind in rows])
    conn.commit()


def resolve_range(conn, args):
    """조회 구간을 결정한다.

    끝은 '지금'이다. 진행 중인 shift 도 부분 집계해 둔다(다음 실행에서 교체).
    시작은 INCREMENTAL_HOURS 이전이 속한 shift 의 시작 시각으로 맞춰
    구간이 shift 경계에서 잘리지 않게 한다.
    """
    now = dt.datetime.now().replace(second=0, microsecond=0)

    if args.ts_from and args.ts_to:
        return (dt.datetime.combine(args.ts_from, dt.time(22)) - dt.timedelta(days=1),
                dt.datetime.combine(args.ts_to, dt.time(22)))
    if args.hours:
        return shift_start_at_or_before(now - dt.timedelta(hours=args.hours)), now
    if args.days:
        return shift_start_at_or_before(now - dt.timedelta(days=args.days)), now
    if args.full or DB.move_last_biz_date(conn) is None:
        first = (now.date() - dt.timedelta(days=INIT_MONTHS * 30)).replace(day=1)
        return dt.datetime.combine(first, dt.time(22)) - dt.timedelta(days=1), now
    return shift_start_at_or_before(now - dt.timedelta(hours=INCREMENTAL_HOURS)), now


def aggregate(df, ts_from, ts_to):
    """이벤트 단위 -> (업무일, shift, sys_line_id) 집계."""
    d = df.copy()
    d.columns = [str(c).lower() for c in d.columns]

    # tkout_date 는 원천에 따라 타입이 다르다.
    #   Oracle : TO_DATE 로 변환돼 datetime 으로 옴
    #   bdq    : FROM_UNIXTIME 결과가 문자열로 올 수 있음
    # DB.to_datetime 이 양쪽을 모두 받는다.
    d["tkout_date"] = DB.to_datetime(d["tkout_date"])
    d = d.dropna(subset=["tkout_date"])
    # line_id 는 cur_line_id / sys_line_id 의 합집합이라 다른 라인이 섞여 온다.
    # MOVE 의 Line 기준은 sys_line_id 이므로 여기서 한 번 더 거른다.
    d = d[d["sys_line_id"].isin(TARGET_LINES)]
    d["move"] = pd.to_numeric(d["move"], errors="coerce").fillna(0)
    d = mark_rework(d)
    d = attach_layer(d)

    rows, lot_rows, step_rows = [], [], []
    boundary = shift_start_at_or_before(ts_to)
    while boundary >= ts_from:
        lo, hi = DB.shift_window(boundary)
        hi = min(hi, ts_to)                 # 진행 중인 shift 는 지금까지만
        shift = BOUNDARY_SHIFT.get(boundary.hour)
        if shift and hi > lo:
            bd = DB.biz_date(boundary)
            chunk = d[(d["tkout_date"] > lo) & (d["tkout_date"] <= hi)]
            for line, g in chunk.groupby("sys_line_id", dropna=True):
                rows.append({"biz_date": bd, "shift": shift, "sys_line_id": line,
                             "move_qty": int(g["move"].sum()),
                             "lot_cnt": int(g["lot_id"].nunique())})
                # lot 단위 (WT 계산용)
                for lot, gl in g.groupby("lot_id", dropna=True):
                    lot_rows.append({"biz_date": bd, "shift": shift,
                                     "sys_line_id": line, "lot_id": lot,
                                     "move_qty": int(gl["move"].sum()),
                                     "tkout_cnt": int(len(gl))})
                # 스텝 단위 (추이 분석용). 정상 / rework 를 나눠 담는다.
                gg = g.copy()
                gg["_p"] = _s(gg.get("prod2", pd.Series("-", index=gg.index)))
                gg["_p"] = gg["_p"].mask(gg["_p"].eq(""), "-")
                gg["_n"] = gg["move"].where(~gg["is_rework"], 0)
                gg["_r"] = gg["move"].where(gg["is_rework"], 0)
                # PK 는 (제품, PLAN, step_seq) 다. layer_id 를 키에 넣으면
                # 같은 REWORK 스텝이 lot 마다 다른 LAYER 를 물려받아
                # 같은 PK 로 여러 행이 생겨 충돌한다.
                # LAYER 는 대표값(가장 많이 나온 것) 하나만 남긴다.
                agg = (gg.groupby(["_p", "process_id", "step_seq"],
                                  dropna=False, as_index=False)
                         .agg(move_qty=("move", "sum"),
                              normal_qty=("_n", "sum"),
                              rework_qty=("_r", "sum"),
                              lot_cnt=("lot_id", "nunique")))
                lay = (gg.groupby(["_p", "process_id", "step_seq"],
                                  dropna=False)["layer_id"]
                         .agg(lambda x: x.mode().iat[0] if len(x.mode()) else None)
                         .reset_index().rename(columns={"layer_id": "layer_id"}))
                agg = agg.merge(lay, on=["_p", "process_id", "step_seq"],
                                how="left")
                for _, a in agg.iterrows():
                    step_rows.append({
                        "biz_date": bd, "shift": shift, "sys_line_id": line,
                        "prod2": a["_p"] or "-",
                        "proc_id": str(a["process_id"] or "-"),
                        "step_seq": str(a["step_seq"] or "-"),
                        "layer_id": a["layer_id"],
                        "module1": None, "area": None,
                        "move_qty": int(a["move_qty"]),
                        "normal_qty": int(a["normal_qty"]),
                        "rework_qty": int(a["rework_qty"]),
                        "lot_cnt": int(a["lot_cnt"])})
        boundary -= dt.timedelta(hours=8)

    df_shift = pd.DataFrame(rows, columns=["biz_date", "shift", "sys_line_id",
                                           "move_qty", "lot_cnt"])
    df_lot = pd.DataFrame(lot_rows, columns=["biz_date", "shift", "sys_line_id",
                                             "lot_id", "move_qty", "tkout_cnt"])
    df_step = pd.DataFrame(step_rows, columns=[
        "biz_date", "shift", "sys_line_id", "prod2", "proc_id", "step_seq",
        "layer_id", "module1", "area",
        "move_qty", "normal_qty", "rework_qty", "lot_cnt"])
    if len(df_shift):
        df_daily = (df_shift.groupby(["biz_date", "sys_line_id"], as_index=False)
                    .agg(move_qty=("move_qty", "sum"), lot_cnt=("lot_cnt", "sum")))
    else:
        df_daily = pd.DataFrame(columns=["biz_date", "sys_line_id", "move_qty", "lot_cnt"])
    return df_shift, df_daily, df_lot, df_step


def main():
    ap = argparse.ArgumentParser(description="MOVE 조회/적재")
    # run_pipeline.bat 이 걸러내지 못한 경우를 대비해 무해하게 흡수한다.
    ap.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--f3-only", dest="f3_only", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--move-only", dest="move_only", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--full", action="store_true", help=f"{INIT_MONTHS}개월치 재적재")
    ap.add_argument("--hours", type=int, default=0,
                    help="최근 N시간 (shift 시작으로 정렬). 예: --hours 6")
    ap.add_argument("--days", type=int, default=0, help="최근 N일")
    ap.add_argument("--from", dest="ts_from", type=dt.date.fromisoformat)
    ap.add_argument("--to", dest="ts_to", type=dt.date.fromisoformat)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if SOURCE == "bdq":
        from bigdataquery import getData
    else:
        import s3_source

    conn = DB.connect()
    DB.ensure_move_schema(conn)

    ts_from, ts_to = resolve_range(conn, args)
    print(f"[MOVE] 조회구간 {ts_from:%Y-%m-%d %H:%M} ~ {ts_to:%Y-%m-%d %H:%M}", flush=True)

    t_start = dt.datetime.now()
    t0 = perf_counter()
    if SOURCE == "bdq":
        df = getData(param=move_query(ts_from, ts_to), convert_type=True, verbose=True)
    else:
        # S3 raw 는 Oracle 쿼리에서 이미 기간이 잘려 있다. 여기서는 조회 구간에
        # 맞춰 한 번 더 거른다(구간을 좁혀 돌릴 때 대비).
        df = s3_source.read_table(S3_TABLE)
        if "tkout_date" in df.columns:
            t = DB.to_datetime(df["tkout_date"])
            keep = (t >= ts_from) & (t < ts_to)
            df = df[keep.fillna(False)]
    t_fetched = dt.datetime.now()
    print(f"[MOVE] 원천 {len(df):,}행 {perf_counter() - t0:.1f}s", flush=True)

    df_shift, df_daily, df_lot, df_step = aggregate(df, ts_from, ts_to)
    print(f"[MOVE] shift 집계 {len(df_shift):,}행 / 일 집계 {len(df_daily):,}행 "
          f"/ lot 단위 {len(df_lot):,}행 / 스텝 단위 {len(df_step):,}행",
          flush=True)
    if len(df_step):
        rw = int(df_step["rework_qty"].sum())
        mv = int(df_step["move_qty"].sum()) or 1
        print(f"[MOVE] REWORK {rw:,}매 ({rw / mv * 100:.1f}%)", flush=True)
    if len(df_daily):
        print(df_daily.tail(6).to_string(index=False), flush=True)

    if args.dry_run:
        print("[MOVE] --dry-run: 적재 생략", flush=True)
        conn.close()
        return

    biz_dates = sorted(set(df_shift["biz_date"])) if len(df_shift) else []
    pairs = DB.replace_move(conn, df_shift, df_daily, biz_dates, df_lot,
                            df_step)
    print(f"[MOVE] 적재 완료: {len(pairs)}개 (업무일,shift) 교체", flush=True)
    for bd, sh in pairs[-6:]:
        print(f"        {bd} {sh}", flush=True)

    # 다운로드 화면의 '처리 구간' 에 MOVE 도 보이게 한다.
    # build_f3 와 다른 프로세스라 f3_live 의 최신 스냅샷에 붙인다.
    try:
        _append_load_log(conn, [
            ("move 조회", t_start, t_fetched, len(df), df.shape[1], "조회"),
            ("move 적재", t_fetched, dt.datetime.now(),
             len(df_lot), len(df_shift), "처리"),
        ])
    except Exception as e:
        print(f"[MOVE] load_log 기록 실패(무시): {e}", flush=True)

    conn.close()


if __name__ == "__main__":
    main()
