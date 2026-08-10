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

# blueprint 7.3 권장 상대 높이
PANELS = [
    {"key": "move",    "title": "MOVE",              "unit": "매",   "h": 1.2},
    {"key": "wt",      "title": "W/T",               "unit": "회",   "h": 1.0},
    {"key": "wip",     "title": "재공",               "unit": "매",   "h": 1.2},
    {"key": "hold",    "title": "HOLD율",             "unit": "%",   "h": 0.8,
     "basis": True},
    {"key": "blocked", "title": "WAIT성 진행불가율",    "unit": "%",   "h": 0.8,
     "basis": True},
]


def _fetch():
    """일별 MOVE / 재공 원자료.

    재공은 업무일 시작 스냅샷(GY)을 쓴다. lot 단위로 접은 뒤 집계한다
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
            if "doesn't exist" in str(e) or "1146" in str(e):
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
                SELECT biz_date, `line`, lot_id,
                       MIN(CAST(qty AS SIGNED)) AS qty,
                       MIN(lot_status)          AS lot_status
                FROM   f3_history
                WHERE  shift = 'GY' AND biz_date >= %s
                  AND  lot_type IN ({types})
                GROUP  BY biz_date, `line`, lot_id
            ) t
            GROUP BY biz_date, `line`
        """, [since, *MOVE_LOT_TYPES], "f3_history")}

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
                  f"getdata/build_f3.py · getdata/get_move.py 를 실행하세요.")
    return JsonResponse({"panels": panels,
                         "order": [p["key"] for p in PANELS],
                         "basis": basis, "notice": notice})


def flowstack(request):
    return render(request, "flowmonitor/flowstack.html",
                  {"menu": [("FlowStack", "/"), ("상세", "#"), ("Lot Balance", "#")],
                   "panels": PANELS})
