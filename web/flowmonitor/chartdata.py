# -*- coding: utf-8 -*-
"""FlowStack 차트용 데이터 구성.

x축은 [3개월][간극][4주][간극][7일] 이며 당월·당주를 포함한다.
월/주 값은 **그 기간에 속한 일별 값의 평균**이다(총합÷일수가 아님).
주는 일요일~토요일, 1월 1일이 속한 주가 W01.
자세한 규칙은 docs/common_conventions.md 참조.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

GAP = "__gap__"


def week_start(d: dt.date) -> dt.date:
    """일요일 시작 주의 첫날."""
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def week_label(d: dt.date) -> str:
    """1월 1일이 속한 주(일~토)가 W01."""
    ws = week_start(d)
    year = (ws + dt.timedelta(days=6)).year
    w1 = week_start(dt.date(year, 1, 1))
    return f"W{((ws - w1).days // 7) + 1:02d}"


def month_range(anchor: dt.date, n=3):
    out, y, m = [], anchor.year, anchor.month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def build_axis(anchor: dt.date, months=3, weeks=4, days=7):
    """x축 버킷. 각 항목 = (label, kind, 포함 날짜 집합)"""
    axis = []
    for y, m in month_range(anchor, months):
        nxt = dt.date(y + (m == 12), (m % 12) + 1, 1)
        d, dates = dt.date(y, m, 1), set()
        while d < nxt and d <= anchor:
            dates.add(d)
            d += dt.timedelta(days=1)
        axis.append((f"{m}월", "month", dates))

    axis.append(("", "gap", set()))

    ws = week_start(anchor)
    for i in range(weeks - 1, -1, -1):
        s = ws - dt.timedelta(days=7 * i)
        dates = {s + dt.timedelta(days=k) for k in range(7) if s + dt.timedelta(days=k) <= anchor}
        axis.append((week_label(s), "week", dates))

    axis.append(("", "gap", set()))

    for i in range(days - 1, -1, -1):
        d = anchor - dt.timedelta(days=i)
        axis.append((f"{d.month}/{d.day}", "day", {d}))
    return axis


def build_panel(daily, anchor=None, decimals=0, months=3, weeks=4, days=7):
    """일별 값 -> 패널 데이터.

    daily : {(date, line): value}
    월/주 값은 그 기간 일별 값의 **평균**이다.
    """
    if anchor is None:
        anchor = max((d for d, _ in daily), default=dt.date.today())
    axis = build_axis(anchor, months, weeks, days)

    by_line = defaultdict(dict)
    for (d, line), v in daily.items():
        if v is not None:
            by_line[line][d] = v

    datasets = []
    for line in sorted(by_line):
        vals = []
        for _, kind, dates in axis:
            if kind == "gap" or not dates:
                vals.append(None)
                continue
            got = [by_line[line][d] for d in dates if d in by_line[line]]
            v = (sum(got) / len(got)) if got else None
            vals.append(None if v is None else
                        (round(v, decimals) if decimals else int(round(v))))
        datasets.append({"label": line, "data": vals})

    return {"labels": [l for l, _, _ in axis],
            "kinds": [k for _, k, _ in axis],
            "datasets": datasets,
            "anchor": anchor.isoformat()}
