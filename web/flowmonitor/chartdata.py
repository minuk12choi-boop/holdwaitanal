# -*- coding: utf-8 -*-
"""MOVE 차트용 데이터 구성.

x 축은 [3개월][간극][4주][간극][7일] 이며 당월·당주를 포함한다.
월/주 값은 '기간 총 MOVE / 기간 일수' 다(일별 값의 단순평균이 아님).
주는 일요일~토요일. 자세한 규칙은 docs/common_conventions.md 참조.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

GAP = "__gap__"


def week_start(d: dt.date) -> dt.date:
    """일요일 시작 주의 첫날."""
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def week_label(d: dt.date) -> str:
    """주차 라벨. 1월 1일이 속한 주(일~토)가 W01 이다.

    2026-08-10(월)이 속한 주는 W33 이 된다.
    """
    ws = week_start(d)
    year = (ws + dt.timedelta(days=6)).year   # 토요일이 속한 해 기준
    w1 = week_start(dt.date(year, 1, 1))
    return f"W{((ws - w1).days // 7) + 1:02d}"


def month_range(anchor: dt.date, n=3):
    """당월 포함 최근 n개월의 (년, 월) 목록."""
    out, y, m = [], anchor.year, anchor.month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def build_axis(anchor: dt.date, months=3, weeks=4, days=7):
    """x축 버킷 목록. 각 항목 = (key, label, kind, 포함 날짜 집합)"""
    axis = []
    for y, m in month_range(anchor, months):
        nxt = dt.date(y + (m == 12), (m % 12) + 1, 1)
        dates = set()
        d = dt.date(y, m, 1)
        while d < nxt and d <= anchor:
            dates.add(d)
            d += dt.timedelta(days=1)
        axis.append((f"M{y}-{m:02d}", f"{m}월", "month", dates))

    axis.append((GAP + "1", "", "gap", set()))

    ws = week_start(anchor)
    for i in range(weeks - 1, -1, -1):
        s = ws - dt.timedelta(days=7 * i)
        dates = {s + dt.timedelta(days=k) for k in range(7)}
        dates = {d for d in dates if d <= anchor}
        axis.append((f"W{s:%Y%m%d}", week_label(s), "week", dates))

    axis.append((GAP + "2", "", "gap", set()))

    for i in range(days - 1, -1, -1):
        d = anchor - dt.timedelta(days=i)
        axis.append((f"D{d:%Y%m%d}", f"{d.month}/{d.day}", "day", {d}))
    return axis


def build_series(rows, anchor=None, months=3, weeks=4, days=7):
    """rows: [(biz_date, sys_line_id, move_qty), ...] -> Chart.js 용 dict"""
    if anchor is None:
        anchor = max((r[0] for r in rows), default=dt.date.today())
    axis = build_axis(anchor, months, weeks, days)

    by_line = defaultdict(dict)
    for bd, line, qty in rows:
        by_line[line][bd] = by_line[line].get(bd, 0) + (qty or 0)

    labels = [lb for _, lb, _, _ in axis]
    kinds = [k for _, _, k, _ in axis]
    datasets = []
    for line in sorted(by_line):
        vals = []
        for _, _, kind, dates in axis:
            if kind == "gap" or not dates:
                vals.append(None)
                continue
            total = sum(by_line[line].get(d, 0) for d in dates)
            vals.append(round(total / len(dates), 1) if kind != "day"
                        else by_line[line].get(next(iter(dates)), 0))
        datasets.append({"label": line, "data": vals})
    return {"labels": labels, "kinds": kinds, "datasets": datasets,
            "anchor": anchor.isoformat()}
