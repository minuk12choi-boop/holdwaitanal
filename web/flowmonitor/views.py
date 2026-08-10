# -*- coding: utf-8 -*-
"""FlowStack 화면 / 지표 API.

패널 순서(blueprint 7.1): MOVE - W/T - 재공 - HOLD율 - WAIT성 진행불가율
공통 x축은 [3개월][4주][7일].
지표 정의는 docs/common_conventions.md 참조.
"""
import datetime as dt

from django.db import connection, ProgrammingError, OperationalError
from django.http import JsonResponse
from django.shortcuts import render

from .chartdata import build_panel

LINE_COLORS = {"KFR7": "#2563EB", "PFR1": "#059669",
               "KFR4": "#EA580C", "P3R3": "#D19A00"}

MOVE_LOT_TYPES = ("PP", "PB", "PG")
LOOKBACK_DAYS = 140

# blueprint 7.3 권장 상대 높이. 템플릿에는 계산된 px 로 넘긴다.
PANEL_BASE_PX = 150
PANELS = [
    {"key": "move",    "title": "MOVE",              "unit": "매",   "h": 1.2},
    {"key": "wt",      "title": "W/T",               "unit": "매/매", "h": 1.0},
    {"key": "wip",     "title": "재공",               "unit": "매",   "h": 1.2},
    {"key": "hold",    "title": "HOLD율",             "unit": "%",   "h": 0.8,
     "basis": True},
    {"key": "blocked", "title": "WAIT성 진행불가율",    "unit": "%",   "h": 0.8,
     "basis": True},
]


def _fetch():
    """일별 MOVE / 재공 원자료.

    재공은 업무일 대표 스냅샷(GY 우선, 없으면 DAY > SW)을 쓴다. lot 단위로 접은 뒤 집계한다
    (f3 는 lot 당 현스텝 + 연속블록 행이 있어 그대로 더하면 중복된다).
    HOLD/WAIT 는 매수(qty)와 lot 수를 모두 담아 화면에서 전환할 수 있게 한다.
    """
    since = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)
    types = ",".join(["%s"] * len(MOVE_LOT_TYPES))
    missing = []

    def run(sql, params, table):
        """적재 전이라 테이블이 없을 수 있다. 500 대신 빈 결과로 처리한다."""
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except (ProgrammingError, OperationalError) as e:
            msg = str(e).lower()
            if ("doesn't exist" in msg or "1146" in msg
                    or "no such table" in msg or "does not exist" in msg):
                missing.append(table)
                return []
            raise

    move = {(r[0], r[1]): float(r[2] or 0) for r in run(
        "SELECT biz_date, sys_line_id, move_qty FROM move_daily WHERE biz_date >= %s",
        [since], "move_daily")}

    wip = {(r[0], r[1]): tuple(float(x or 0) for x in r[2:]) for r in run(f"""
            SELECT biz_date, `line`,
                   SUM(qty)                                                        AS wip_qty,
                   COUNT(*)                                                        AS wip_lot,
                   SUM(CASE WHEN lot_status = 'HOLD' THEN qty ELSE 0 END)          AS hold_qty,
                   SUM(CASE WHEN lot_status = 'HOLD' THEN 1 ELSE 0 END)            AS hold_lot,
                   SUM(CASE WHEN lot_status = 'WAIT(진행불가)' THEN qty ELSE 0 END) AS blocked_qty,
                   SUM(CASE WHEN lot_status = 'WAIT(진행불가)' THEN 1 ELSE 0 END)   AS blocked_lot
            FROM (
                SELECT h.biz_date, h.`line`, h.lot_id,
                       MIN(CAST(h.qty AS SIGNED)) AS qty,
                       MIN(h.lot_status)          AS lot_status
                FROM   f3_history h
                JOIN (
                    -- 업무일 대표 스냅샷: GY 우선, 없으면 DAY, 그것도 없으면 SW.
                    -- 적재 초기라 GY 가 아직 없는 날에도 값이 나오게 한다.
                    SELECT biz_date,
                           SUBSTRING_INDEX(GROUP_CONCAT(
                               shift ORDER BY FIELD(shift,'GY','DAY','SW')), ',', 1) AS shift
                    FROM   f3_history
                    WHERE  biz_date >= %s
                    GROUP  BY biz_date
                ) pick
                  ON h.biz_date = pick.biz_date AND h.shift = pick.shift
                WHERE  h.biz_date >= %s
                  AND  h.lot_type IN ({types})
                GROUP  BY h.biz_date, h.`line`, h.lot_id
            ) t
            GROUP BY biz_date, `line`
        """, [since, since, *MOVE_LOT_TYPES], "f3_history")}

    return move, wip, missing


def _panels(basis="qty"):
    """basis: 'qty' = 매수 기준(기본), 'lot' = Lot 수 기준.

    HOLD율 / WAIT성 진행불가율의 분모만 바뀐다. MOVE / W/T / 재공은
    정의상 매수 기준이므로 영향받지 않는다(docs/common_conventions.md).
    """
    move, wip, missing = _fetch()
    keys = set(move) | set(wip)
    i = 0 if basis == "qty" else 1        # (wip_qty, wip_lot, hold_qty, hold_lot, ...)

    d_move, d_wt, d_wip, d_hold, d_blk = {}, {}, {}, {}, {}
    for k in keys:
        mv = move.get(k)
        row = wip.get(k)
        if mv is not None:
            d_move[k] = mv
        if not row:
            continue
        w_qty, w_lot, h_qty, h_lot, b_qty, b_lot = row
        base = (w_qty, w_lot)[i]
        if w_qty:
            d_wip[k] = w_qty
            if mv is not None:
                d_wt[k] = mv / w_qty
        if base:
            d_hold[k] = (h_qty, h_lot)[i] / base * 100
            d_blk[k] = (b_qty, b_lot)[i] / base * 100

    out = {}
    for p in PANELS:
        daily, dec = {
            "move": (d_move, 0), "wt": (d_wt, 1), "wip": (d_wip, 0),
            "hold": (d_hold, 1), "blocked": (d_blk, 1),
        }[p["key"]]
        data = build_panel(daily, decimals=dec)
        for ds in data["datasets"]:
            ds["borderColor"] = LINE_COLORS.get(ds["label"], "#6B7280")
            ds["backgroundColor"] = ds["borderColor"]
            ds["spanGaps"] = False
            ds["tension"] = 0.25
            ds["pointRadius"] = 2
        data.update(key=p["key"], title=p["title"], unit=p["unit"], height=p["h"])
        out[p["key"]] = data
    return out, missing


def api_flowstack(request):
    basis = request.GET.get("basis", "qty")
    if basis not in ("qty", "lot"):
        basis = "qty"
    panels, missing = _panels(basis)
    notice = ""
    if missing:
        notice = (f"아직 적재되지 않은 테이블: {', '.join(sorted(set(missing)))}. "
                  f"python getdata/db_common.py --init 로 테이블을 만든 뒤 "
                  f"getdata/build_f3.py 를 실행하세요. "
                  f"이미 실행했다면 로그에 [ERROR] DB 적재 실패 가 있는지 확인하세요.")
    return JsonResponse({"panels": panels,
                         "order": [p["key"] for p in PANELS],
                         "basis": basis, "notice": notice})


def flowstack(request):
    panels = [dict(p, px=int(round(p["h"] * PANEL_BASE_PX))) for p in PANELS]
    return render(request, "flowmonitor/flowstack.html",
                  {"menu": [("FAB현황", "/"), ("다운로드", "/downloads/")],
                   "panels": panels})


# ---------------------------------------------------------------------------
# 다운로드 (재공Raw)
# ---------------------------------------------------------------------------
def _table_exists(name):
    return name in connection.introspection.table_names()


def _snapshots():
    """f3_live(실시간 2벌) + f3_history(누적) 목록."""
    out = []
    if _table_exists("f3_live"):
        with connection.cursor() as cur:
            cur.execute("SELECT snapshot_at, COUNT(*) FROM f3_live "
                        "GROUP BY snapshot_at ORDER BY snapshot_at DESC")
            for snap, n in cur.fetchall():
                out.append({"kind": "live", "snapshot_at": snap, "rows": n,
                            "biz_date": "", "shift": "실시간"})
    if _table_exists("f3_history_meta"):
        with connection.cursor() as cur:
            cur.execute("SELECT biz_date, shift, snapshot_at, row_count "
                        "FROM f3_history_meta ORDER BY biz_date DESC, "
                        "FIELD(shift,'SW','DAY','GY') LIMIT 60")
            for bd, sh, snap, n in cur.fetchall():
                out.append({"kind": "history", "snapshot_at": snap, "rows": n,
                            "biz_date": bd, "shift": sh})
    return out


def _load_log(snapshot_at):
    """(행 목록, 합계행). 합계행은 소요초 합 + 전체 시작/종료 + 실제 경과."""
    if not _table_exists("f3_load_log"):
        return [], None
    with connection.cursor() as cur:
        cur.execute(
            "SELECT table_name, load_start, load_end, elapsed_sec, row_count, col_count "
            "FROM f3_load_log WHERE snapshot_at = %s ORDER BY id", [snapshot_at])
        rows = [{"table": r[0], "start": r[1], "end": r[2], "sec": r[3],
                 "rows": r[4], "cols": r[5]} for r in cur.fetchall()]

    if not rows:
        return rows, None

    starts = [r["start"] for r in rows if r["start"]]
    ends = [r["end"] for r in rows if r["end"]]
    total = {
        "table": "계",
        "start": min(starts) if starts else None,
        "end": max(ends) if ends else None,
        "sec": round(sum(r["sec"] or 0 for r in rows), 3),
        "rows": sum(r["rows"] or 0 for r in rows),
        "cols": None,
    }
    if total["start"] and total["end"]:
        total["wall"] = round((total["end"] - total["start"]).total_seconds(), 1)
    return rows, total


def downloads(request):
    snaps = _snapshots()
    sel = request.GET.get("snapshot") or (snaps[0]["snapshot_at"].strftime(
        "%Y-%m-%d %H:%M:%S") if snaps else "")
    log, total = _load_log(sel) if sel else ([], None)
    return render(request, "flowmonitor/downloads.html", {
        "menu": [("FAB현황", "/"), ("다운로드", "/downloads/")],
        "snapshots": snaps, "selected": sel, "load_log": log, "load_total": total,
    })


def download_wip_raw(request):
    """재공Raw 엑셀. f3_live 또는 f3_history 의 한 스냅샷을 그대로 내려준다."""
    import io

    import pandas as pd
    from django.http import HttpResponse

    snap = request.GET.get("snapshot")
    kind = request.GET.get("kind", "live")
    if not snap:
        return HttpResponse("snapshot 파라미터가 필요합니다.", status=400)

    table = "f3_history" if kind == "history" else "f3_live"
    with connection.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE snapshot_at = %s", [snap])
        cols = [c[0] for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    drop = [c for c in ("id", "snapshot_at", "biz_date", "shift") if c in df.columns]
    meta = df[drop].head(1) if drop else pd.DataFrame()
    df = df.drop(columns=drop)

    log_rows, log_total = _load_log(snap)
    if log_total:
        log_rows = log_rows + [{k: log_total.get(k) for k in
                                ("table", "start", "end", "sec", "rows", "cols")}]
    log = pd.DataFrame(log_rows)
    if len(log):
        log.columns = ["테이블", "로딩_시작시각", "로딩_종료시각", "소요_초", "행수", "컬럼수"]

    buf = io.BytesIO()
    try:
        import xlsxwriter  # noqa: F401
        engine = "xlsxwriter"
    except ImportError:
        engine = "openpyxl"
    with pd.ExcelWriter(buf, engine=engine) as xw:
        df.to_excel(xw, sheet_name="재공Raw", index=False)
        if len(log):
            log.to_excel(xw, sheet_name="로딩시각", index=False)
        if len(meta):
            meta.to_excel(xw, sheet_name="스냅샷정보", index=False)

    stamp = str(snap).replace("-", "").replace(":", "").replace(" ", "_")
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="wip_raw_{stamp}.xlsx"'
    return resp


def api_health(request):
    """적재 상태 진단. 차트가 비었을 때 원인을 바로 알기 위한 것."""
    info = {"tables": {}, "samples": {}}
    for t in ("move_daily", "move_shift", "f3_live", "f3_history",
              "f3_history_meta", "f3_load_log"):
        if not _table_exists(t):
            info["tables"][t] = "없음"
            continue
        with connection.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            info["tables"][t] = f"{cur.fetchone()[0]:,}행"

    if _table_exists("move_daily"):
        with connection.cursor() as cur:
            cur.execute("SELECT MIN(biz_date), MAX(biz_date), "
                        "COUNT(DISTINCT biz_date), COUNT(DISTINCT sys_line_id) "
                        "FROM move_daily")
            r = cur.fetchone()
            info["samples"]["move_daily"] = {
                "기간": f"{r[0]} ~ {r[1]}", "일수": r[2], "라인수": r[3]}
    if _table_exists("f3_history"):
        with connection.cursor() as cur:
            cur.execute("SELECT biz_date, shift, COUNT(*) FROM f3_history "
                        "GROUP BY biz_date, shift ORDER BY biz_date DESC LIMIT 5")
            info["samples"]["f3_history"] = [
                {"biz_date": str(a), "shift": b, "rows": c} for a, b, c in cur.fetchall()]

    move, wip, missing = _fetch()
    info["조회결과"] = {
        "move_daily 행": len(move), "f3_history 집계 행": len(wip),
        "없는 테이블": missing,
        "LOOKBACK_DAYS": LOOKBACK_DAYS,
        "조회 시작일": str(dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)),
    }
    panels, _ = _panels("qty")
    info["패널별 데이터포인트"] = {
        k: sum(1 for ds in v["datasets"] for x in ds["data"] if x is not None)
        for k, v in panels.items()}
    return JsonResponse(info, json_dumps_params={"ensure_ascii": False, "indent": 2})
