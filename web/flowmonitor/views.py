# -*- coding: utf-8 -*-
import datetime as dt

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render

from .chartdata import build_series

LINE_COLORS = {"KFR7": "#2563EB", "PFR1": "#059669",
               "KFR4": "#EA580C", "P3R3": "#D19A00"}


def _move_rows(days_back=140):
    since = dt.date.today() - dt.timedelta(days=days_back)
    with connection.cursor() as cur:
        cur.execute(
            "SELECT biz_date, sys_line_id, move_qty FROM move_daily "
            "WHERE biz_date >= %s ORDER BY biz_date", [since])
        return cur.fetchall()


def api_move(request):
    data = build_series(_move_rows())
    for ds in data["datasets"]:
        ds["borderColor"] = LINE_COLORS.get(ds["label"], "#6B7280")
        ds["backgroundColor"] = ds["borderColor"]
        ds["spanGaps"] = False
        ds["tension"] = 0.25
    return JsonResponse(data)


def flowstack(request):
    return render(request, "flowmonitor/flowstack.html",
                  {"menu": [("FlowStack", "/"), ("상세", "#"), ("Lot Balance", "#")]})
