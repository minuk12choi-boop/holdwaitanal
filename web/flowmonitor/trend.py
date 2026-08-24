"""MOVE 추이 분석.

/main/ 은 지금 이 순간만 본다. 여기서는 f3_move_step · f3_wip_step 을 읽어
**어디서 언제부터 얼마나 줄었는지** 를 가린다.

핵심
  - 기간을 미리 정하지 않는다. 전일 · 3 · 7 · 14 · 30 · 60일 중앙값과
    한꺼번에 견줘 문제의 성격을 가른다.
  - 평소치 비교는 normal_qty(정상 진행분) 로 한다. REWORK 는 따로 본다.
  - 재공을 함께 봐서 '막혔다' 와 '안 넘어온다' 를 가른다.
"""
import datetime as dt

from django.db import connection

# 비교 창. 며칠짜리 문제인지 여기서 갈린다.
WINDOWS = (1, 3, 7, 14, 30, 60)

# 이 아래로 떨어지면 저하로 본다.
DROP = 0.75
# 이 위로 오르면 증가로 본다.
RISE = 1.25
# 이만큼은 움직여야 의미가 있다. 소량 구간의 잡음을 걷는다.
MIN_QTY = 100


def _rows(sql, args):
    with connection.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _table_ok(name):
    try:
        with connection.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {name} LIMIT 1")
            cur.fetchall()
        return True
    except Exception:
        return False


def move_series(line, today, days=61, prod=None, plan=None):
    """(proc_id, layer_id) 별 일자 MOVE. 정상분과 REWORK 를 나눠 담는다."""
    if not _table_ok("f3_move_step"):
        return {}
    w = ["sys_line_id = %s", "biz_date > %s", "biz_date <= %s"]
    a = [line, today - dt.timedelta(days=days), today]
    if prod:
        w.append("prod2 IN (%s)" % ",".join(["%s"] * len(prod)))
        a += list(prod)
    if plan:
        w.append("proc_id IN (%s)" % ",".join(["%s"] * len(plan)))
        a += list(plan)
    rows = _rows(
        "SELECT biz_date, prod2, proc_id, layer_id, "
        "       SUM(normal_qty) AS n, SUM(rework_qty) AS r, "
        "       SUM(move_qty) AS m "
        "FROM   f3_move_step "
        f"WHERE  {' AND '.join(w)} "
        "GROUP  BY biz_date, prod2, proc_id, layer_id", a)

    out = {}
    for r in rows:
        k = (r["prod2"], r["proc_id"], r["layer_id"])
        out.setdefault(k, {})[r["biz_date"]] = {
            "n": int(r["n"] or 0), "r": int(r["r"] or 0),
            "m": int(r["m"] or 0)}
    return out


def wip_series(line, today, days=61, prod=None, plan=None):
    """(proc_id, layer_id) 별 일자 재공. 그날 스냅샷들의 최대와 평균."""
    if not _table_ok("f3_wip_step"):
        return {}
    w = ["`line` = %s", "biz_date > %s", "biz_date <= %s"]
    a = [line, today - dt.timedelta(days=days), today]
    if prod:
        w.append("prod2 IN (%s)" % ",".join(["%s"] * len(prod)))
        a += list(prod)
    if plan:
        w.append("proc_id IN (%s)" % ",".join(["%s"] * len(plan)))
        a += list(plan)
    # 스냅샷별로 먼저 접고, 그 다음 날짜별 최대 · 평균을 낸다.
    rows = _rows(
        "SELECT biz_date, prod2, proc_id, layer_id, "
        "       MAX(q) AS mx, AVG(q) AS av "
        "FROM ( "
        "  SELECT biz_date, prod2, proc_id, layer_id, snapshot_at, "
        "         SUM(wip_qty) AS q "
        "  FROM   f3_wip_step "
        f"  WHERE  {' AND '.join(w)} "
        "  GROUP  BY biz_date, prod2, proc_id, layer_id, snapshot_at "
        ") t GROUP BY biz_date, prod2, proc_id, layer_id", a)

    out = {}
    for r in rows:
        k = (r["prod2"], r["proc_id"], r["layer_id"])
        out.setdefault(k, {})[r["biz_date"]] = {
            "max": int(r["mx"] or 0), "avg": float(r["av"] or 0)}
    return out


def _ratios(hist, today, key="n"):
    """오늘 값을 여러 창의 중앙값과 견준다. 창마다 비율을 낸다."""
    cur = (hist.get(today) or {}).get(key, 0)
    out = {"today": cur}
    for w in WINDOWS:
        base = [v.get(key, 0) for d, v in hist.items()
                if today - dt.timedelta(days=w) <= d < today]
        med = _median(base)
        out[f"w{w}"] = {
            "median": med,
            "ratio": (cur / med) if med else None,
            "days": len(base),
        }
    return out


def _pick_window(rt):
    """가장 뚜렷한 창을 고른다. 비율이 1 에서 가장 멀리 떨어진 것."""
    best, bw = None, None
    for w in WINDOWS:
        r = rt.get(f"w{w}", {}).get("ratio")
        if r is None or rt[f"w{w}"]["days"] < max(2, w // 3):
            continue
        d = abs(1 - r)
        if best is None or d > best:
            best, bw = d, w
    return bw


def _since(hist, today, med, key="n", limit=0.75, back=60):
    """언제부터 기준 아래인가. 거슬러 올라가 처음 떨어진 날을 찾는다."""
    if not med:
        return None, 0
    day = today
    start, run = None, 0
    for _ in range(back):
        v = (hist.get(day) or {}).get(key)
        if v is None:
            day -= dt.timedelta(days=1)
            continue
        if v / med <= limit:
            start, run = day, run + 1
            day -= dt.timedelta(days=1)
            continue
        break
    return start, run


def shape(line, today=None, prod=None, plan=None):
    """구간마다 무슨 일이 있었는지 정리한다. 문구는 여기서 만들지 않는다."""
    today = today or dt.date.today()
    mv = move_series(line, today, prod=prod, plan=plan)
    wp = wip_series(line, today, prod=prod, plan=plan)

    out = []
    for k, hist in mv.items():
        prod2, proc, layer = k
        rt = _ratios(hist, today, "n")
        if rt["today"] < MIN_QTY and not any(
                (rt[f"w{w}"]["median"] or 0) >= MIN_QTY for w in WINDOWS):
            continue                      # 원래 물량이 적은 구간은 건너뛴다
        w = _pick_window(rt)
        if w is None:
            continue
        ratio = rt[f"w{w}"]["ratio"]
        med = rt[f"w{w}"]["median"]
        if ratio is None or DROP < ratio < RISE:
            continue                      # 평소와 다르지 않다

        start, run = _since(hist, today, med, "n",
                            DROP if ratio <= DROP else 1 / RISE)

        wh = wp.get(k, {})
        wnow = (wh.get(today) or {}).get("max", 0)
        wmed = _median([v.get("max", 0) for d, v in wh.items()
                        if today - dt.timedelta(days=w) <= d < today])
        wratio = (wnow / wmed) if wmed else None

        cur = hist.get(today) or {}
        out.append({
            "prod2": prod2, "proc_id": proc, "layer_id": layer,
            "window": w, "ratio": ratio, "median": med,
            "today": rt["today"], "gap": int((med or 0) - rt["today"]),
            "since": start, "run_days": run,
            "rework": cur.get("r", 0), "move": cur.get("m", 0),
            "wip_now": wnow, "wip_median": wmed, "wip_ratio": wratio,
            "kind": ("drop" if ratio <= DROP else "rise"),
        })

    out.sort(key=lambda x: -abs(x["gap"]))
    return out
