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


# ---------------------------------------------------------------------------
# 원인 결합
#   MOVE 가 떨어진 구간에 그 시점 무슨 일이 있었는지 붙인다.
#   f3_history 에는 스냅샷마다 상태 · 설비 · HOLD 가 이미 쌓여 있다.
# ---------------------------------------------------------------------------
def cause_at(line, since, today, prod=None, plan=None, layer=None):
    """그 구간에서 잡힌 원인들. 스냅샷 단위로 시작 · 지속을 센다."""
    if not _table_ok("f3_history") or since is None:
        return []
    w = ["`line` = %s", "biz_date >= %s", "biz_date <= %s",
         "`현스텝` = '현스텝'"]
    a = [line, since, today]
    if prod:
        w.append("prod2 = %s")
        a.append(prod)
    if plan:
        w.append("proc_id = %s")
        a.append(plan)
    if layer:
        w.append("layer_id = %s")
        a.append(layer)

    rows = _rows(
        "SELECT biz_date, shift, snapshot_at, lot_status, "
        "       eqpgroup, eqpgroup_cham, AREA, down, tip, hold, hold_reason, "
        "       `연속`, de_rank, step_seq, SUM(qty) AS q, COUNT(*) AS c "
        "FROM   f3_history "
        f"WHERE  {' AND '.join(w)} "
        "GROUP  BY biz_date, shift, snapshot_at, lot_status, eqpgroup, "
        "          eqpgroup_cham, AREA, down, tip, hold, hold_reason, "
        "          `연속`, de_rank, step_seq", a)

    # 원인별로 스냅샷을 모은다.
    box = {}
    for r in rows:
        st = (r["lot_status"] or "").upper()
        eqp = (r["eqpgroup_cham"] or r["eqpgroup"] or "").strip()
        down = (r["down"] or "").strip()
        tip = (r["tip"] or "").strip()
        hold = (r["hold_reason"] or "").strip()

        if down:
            key = ("설비", eqp or (r["AREA"] or "-"), down.split(":")[0].strip())
        elif tip:
            key = ("PREVENT", eqp or (r["AREA"] or "-"), "TIP")
        elif st == "HOLD":
            key = ("HOLD", hold or "(사유없음)", "")
        elif st == "WAIT" and eqp:
            key = ("대기", eqp, "")
        else:
            continue

        b = box.setdefault(key, {"snaps": set(), "qty": 0, "area": r["AREA"],
                                 "first_step": None, "cont_first": 0})
        b["snaps"].add(r["snapshot_at"])
        b["qty"] += int(r["q"] or 0)
        # 연속구간의 첫 스텝에 쌓였는지. 그렇다면 뒤쪽이 진짜 원인이다.
        if str(r.get("연속") or "").startswith("연속첫"):
            b["cont_first"] += int(r["q"] or 0)
        if b["first_step"] is None:
            b["first_step"] = r["step_seq"]

    out = []
    for (kind, name, sub), b in box.items():
        snaps = sorted(b["snaps"])
        out.append({
            "kind": kind, "name": name, "sub": sub, "area": b["area"],
            "qty": b["qty"], "snap_cnt": len(snaps),
            "first_snap": snaps[0] if snaps else None,
            "last_snap": snaps[-1] if snaps else None,
            "cont_first_qty": b["cont_first"],
        })
    out.sort(key=lambda x: -x["qty"])
    return out


def _common_axis(items, key):
    """여러 구간이 한 값으로 모이면 그 값을, 갈라지면 None 을 준다."""
    vs = {x.get(key) for x in items}
    return vs.pop() if len(vs) == 1 else None


def group_findings(rows):
    """구간을 묶는다.

    설비성   AREA 라는 큰 틀로 묶는다(설비 하나가 여러 LAYER 를 때린다).
    LOT성    제품 > PLAN > 모듈 > LAYER 로 갈 수 있는 데까지 세분화한다.
    """
    by = {}
    for r in rows:
        c = (r.get("causes") or [None])[0]
        if c and c["kind"] in ("설비", "PREVENT"):
            k = ("설비", c["area"] or "-", c["name"])
        elif c and c["kind"] == "HOLD":
            k = ("LOT", "HOLD", c["name"])
        elif c and c["kind"] == "대기":
            k = ("설비", c["area"] or "-", c["name"])
        else:
            k = ("미상", r["proc_id"], r["layer_id"])
        by.setdefault(k, []).append(r)

    out = []
    for (grp, a1, a2), items in by.items():
        # 공통 축까지만 이름에 넣는다. 갈라지는 축은 뺀다.
        axes = {
            "prod2": _common_axis(items, "prod2"),
            "proc_id": _common_axis(items, "proc_id"),
            "layer_id": _common_axis(items, "layer_id"),
        }
        sin = [x["since"] for x in items if x["since"]]
        out.append({
            "group": grp, "a1": a1, "a2": a2,
            "axes": axes,
            "items": items,
            "n": len(items),
            "gap": sum(x["gap"] for x in items),
            "ratio": (sum(x["today"] for x in items)
                      / sum(x["median"] or 0 for x in items)
                      if sum(x["median"] or 0 for x in items) else None),
            "since": min(sin) if sin else None,
            "run_days": max((x["run_days"] for x in items), default=0),
            "window": max((x["window"] for x in items), default=7),
            "wip_ratio": max((x["wip_ratio"] or 0 for x in items), default=0),
            "rework": sum(x["rework"] for x in items),
            "causes": (items[0].get("causes") or []),
        })
    out.sort(key=lambda x: -abs(x["gap"]))
    return out


def diagnose(line, today=None, prod=None, plan=None):
    """추이 분석 + 원인 결합. 문구 생성이 이 결과를 받는다."""
    today = today or dt.date.today()
    rows = shape(line, today, prod=prod, plan=plan)
    for r in rows[:40]:                 # 원인 조회는 상위만. 나머지는 미상.
        r["causes"] = cause_at(line, r["since"], today,
                               r["prod2"], r["proc_id"], r["layer_id"])
    for r in rows[40:]:
        r["causes"] = []
    return group_findings(rows)


# ---------------------------------------------------------------------------
# 문구 생성
#   어휘는 고정한다. 감소 · 증가만 쓰고, 수치는 늘 비교 기준과 함께 적는다.
#   다양성은 **어느 주어로 열고, 어떤 항목을 몇 개, 어떤 순서로** 담느냐에서
#   얻는다. 추상 표현으로 늘리지 않는다.
#
#   모든 문장은 주어를 갖는다. '5일째입니다' 처럼 주어 없는 서술은 만들지
#   않는다(무엇이 5일째인지 그 문장 안에서 끝나야 한다).
# ---------------------------------------------------------------------------
def _nf(n):
    return f"{int(round(n)):,}"


def _target(axes, fallback=""):
    """타격 대상 이름. 공통 축까지만 적는다."""
    p = []
    if axes.get("prod2") and axes["prod2"] != "-":
        p.append(f"{axes['prod2']} 제품")
    if axes.get("proc_id") and axes["proc_id"] != "-":
        p.append(f"{axes['proc_id']} PLAN")
    if axes.get("layer_id"):
        p.append(f"LAYER {axes['layer_id']}")
    return " ".join(p) or fallback


def _eqp_name(c):
    """설비 이름. AREA 를 앞에 붙여 어디인지 밝힌다.

    'CVD 의 TCV301' 처럼 한 번만 쓴다. 뒤에서 다시 언급할 때는
    _eqp_short() 로 설비명만 쓴다.
    """
    if not c:
        return ""
    a = (c.get("area") or "").strip()
    return f"{a} 의 {c['name']}" if a else c["name"]


def _eqp_short(c):
    """두 번째 언급부터는 설비명만. AREA 를 되풀이하지 않는다."""
    return c["name"] if c else ""


def _state(c):
    """설비가 어떤 상태인지. 원천 표기를 그대로 쓴다."""
    if not c:
        return ""
    if c["kind"] == "설비":
        return c.get("sub") or "이슈"
    if c["kind"] == "PREVENT":
        return "PREVENT"
    return ""


def _facts(g):
    """문장에 담을 수 있는 사실들. 여기서 고른 것만 쓴다."""
    f = {}
    c = (g.get("causes") or [None])[0]
    tgt = _target(g["axes"])

    if g["since"]:
        f["since"] = f"{g['since'].month}/{g['since'].day}"
    if g["run_days"] >= 2:
        f["run"] = g["run_days"]
    if g["ratio"] is not None:
        f["ratio"] = int(round(g["ratio"] * 100))
    f["gap"] = g["gap"]
    f["window"] = g["window"]
    f["target"] = tgt
    if c:
        f["eqp"] = _eqp_name(c)
        f["eqp_short"] = _eqp_short(c)
        f["state"] = _state(c)
        f["cause_kind"] = c["kind"]
        f["cont"] = c.get("cont_first_qty", 0) > 0
    if g["wip_ratio"] and g["wip_ratio"] >= 1.3:
        f["wip"] = g["wip_ratio"]
    if g["rework"] > 0 and g["gap"] > 0:
        f["rework"] = g["rework"]
    if g["n"] > 1:
        f["spread"] = g["n"]
    return f


def _sent_move(f):
    """MOVE 감소를 말하는 절. 주어는 대상이다."""
    return (f"{f['target']} 의 MOVE 가 {f['window']}일 평균 대비 "
            f"{f['ratio']}%({_nf(f['gap'])}매) 감소했습니다")


def _sent_eqp(f):
    """설비 상태를 말하는 절. 주어는 설비다."""
    s = f"{f['eqp']} 가 {f['state']} 상태입니다"
    if f.get("since") and f.get("run"):
        s = (f"{f['eqp']} 가 {f['since']}부터 {f['run']}일째 "
             f"{f['state']} 상태입니다")
    elif f.get("since"):
        s = f"{f['eqp']} 가 {f['since']}부터 {f['state']} 상태입니다"
    return s


def _sent_wip(f):
    return f"같은 구간의 재공은 평소의 {f['wip']:.1f}배로 증가했습니다"


def _sent_cont(f):
    return (f"재공은 연속구간 첫 스텝에 쌓여 있어, 실제 제약은 같은 구간 "
            f"뒤쪽의 {f['eqp_short']} 입니다")


def _sent_rework(f):
    return (f"같은 구간에서 REWORK 가 {_nf(f['rework'])}매 발생해 설비 가동이 "
            f"정상 진행에 쓰이지 못했습니다")


def _sent_spread(f):
    return f"같은 원인이 {f['spread']}개 구간에 영향을 주고 있습니다"


def phrase(g, seed=0):
    """구간 하나를 문장으로. 같은 데이터면 같은 문장이 나온다."""
    f = _facts(g)
    if not f.get("ratio"):
        return ""

    # 원인이 없으면 확인을 요청한다.
    if not f.get("eqp"):
        return (_sent_move(f) + ". 설비 이슈나 HOLD 가 잡히지 않습니다. "
                "설비 상태를 직접 확인하거나 전산 이슈 여부를 점검해 주세요.")

    # 담을 수 있는 절을 모은다. 그 상황에서 특이한 것만 남는다.
    opt = []
    if f.get("cont"):
        opt.append(_sent_cont(f))
    elif f.get("wip"):
        opt.append(_sent_wip(f))
    if f.get("rework"):
        opt.append(_sent_rework(f))
    if f.get("spread"):
        opt.append(_sent_spread(f))

    # 여는 절을 고른다. 씨앗이 같으면 늘 같은 것이 나온다.
    head = (seed + len(f.get("eqp", "")) + int(f["gap"])) % 3
    if head == 0:
        body = [_sent_eqp(f), _sent_move(f)]
    elif head == 1:
        body = [_sent_move(f), "원인은 " + _sent_eqp(f).replace(" 가 ", " 로, ", 1)]
        body[1] = f"원인은 {f['eqp']} 입니다"
        if f.get("since"):
            body[1] = (f"원인은 {f['since']}부터 {f['state']} 상태인 "
                       f"{f['eqp']} 입니다")
    else:
        body = [_sent_eqp(f), _sent_move(f)]
        body[1] = ("이로 인해 " + _sent_move(f))

    # 딸린 절은 최대 두 개까지. 개수가 달라 길이도 달라진다.
    body += opt[: (1 + (seed + int(f["gap"])) % 2)]
    return ". ".join(body) + "."


def summarize(line, today=None, prod=None, plan=None, seed=0, top=3):
    """상위 몇 개를 문장으로, 나머지는 규모만."""
    gs = diagnose(line, today, prod=prod, plan=plan)
    head = []
    for i, g in enumerate(gs[:top]):
        t = phrase(g, seed + i)
        if t:
            head.append({"text": t, "group": g["group"], "gap": g["gap"]})
    rest = gs[top:]
    return {
        "items": head,
        "rest_n": len(rest),
        "rest_gap": sum(x["gap"] for x in rest),
        "all": gs,
    }
