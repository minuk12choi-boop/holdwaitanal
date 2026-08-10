# -*- coding: utf-8 -*-
"""FlowStack 화면 / 지표 API.

패널 순서(blueprint 7.1): MOVE - W/T - 재공 - HOLD율 - WAIT성 진행불가율
공통 x축은 [3개월][4주][7일].
지표 정의는 docs/common_conventions.md 참조.
"""
import datetime as dt

from django.db import connection
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
    {"key": "hold",    "title": "HOLD율",             "unit": "%",   "h": 0.8},
    {"key": "blocked", "title": "WAIT성 진행불가율",    "unit": "%",   "h": 0.8},
]


def _fetch():
    """일별 MOVE / 재공 / HOLD / 진행불가 원자료.

    재공은 업무일 시작 스냅샷(GY)을 쓴다. lot 단위로 접은 뒤 매수를 합산한다
    (f3 는 lot 당 현스텝 + 연속블록 행이 있어 그대로 더하면 중복된다).
    """
    since = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)
    types = ",".join(["%s"] * len(MOVE_LOT_TYPES))

    with connection.cursor() as cur:
        cur.execute(
            "SELECT biz_date, sys_line_id, move_qty FROM move_daily "
            "WHERE biz_date >= %s", [since])
        move = {(r[0], r[1]): float(r[2] or 0) for r in cur.fetchall()}

        cur.execute(f"""
            SELECT biz_date, `line`,
                   SUM(qty)                                            AS wip_qty,
                   SUM(CASE WHEN lot_status = 'HOLD' THEN qty ELSE 0 END)          AS hold_qty,
                   SUM(CASE WHEN lot_status = 'WAIT(진행불가)' THEN qty ELSE 0 END) AS blocked_qty
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
        """, [since, *MOVE_LOT_TYPES])
        wip = {(r[0], r[1]): (float(r[2] or 0), float(r[3] or 0), float(r[4] or 0))
               for r in cur.fetchall()}
    return move, wip


def _panels():
    move, wip = _fetch()
    keys = set(move) | set(wip)

    d_move, d_wt, d_wip, d_hold, d_blk = {}, {}, {}, {}, {}
    for k in keys:
        mv = move.get(k)
        w, h, b = wip.get(k, (None, None, None))
        if mv is not None:
            d_move[k] = mv
        if w:
            d_wip[k] = w
            d_hold[k] = h / w * 100
            d_blk[k] = b / w * 100
            if mv is not None:
                d_wt[k] = mv / w

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
    return out


def api_flowstack(request):
    return JsonResponse({"panels": _panels(), "order": [p["key"] for p in PANELS]})


def flowstack(request):
    return render(request, "flowmonitor/flowstack.html",
                  {"menu": [("FlowStack", "/"), ("상세", "#"), ("Lot Balance", "#")],
                   "panels": PANELS})
