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

# 전체 톤은 진한 남색 계열로 통일한다. 색은 라인 구분이 아니라
# 정보 전달(status)과 강조에만 쓴다.
BRAND = "#2F4B7C"
LINE_TONES = ["#2F4B7C", "#4C7DD1", "#7FB3E8", "#A9C8E8"]

MOVE_LOT_TYPES = ("PP", "PB", "PG")

# 상단 현황 카드. 좌측부터 고정 순서.
#   label = 화면 표기, line = f3/move 의 실제 라인 코드
LINE_CARDS = [
    {"label": "NRD",   "line": "KFR4"},
    {"label": "NRD-P", "line": "PFR1"},
    {"label": "NRD-K", "line": "KFR7"},
    {"label": "NRD-V", "line": "KFR6"},
]

STATUS_ORDER = ["RUN", "WAIT", "HOLD", "WAIT(진행불가)"]
# 계열은 유지하되 채도를 낮춰 눈이 편하게 한다.
STATUS_COLORS = {
    "RUN": "#6699CC",            # 파랑
    "WAIT": "#84C09A",           # 초록
    "HOLD": "#F08080",           # 빨강
    "WAIT(진행불가)": "#E8C46A",   # 노랑
}
ACCENT = "#6A5ACD"               # 소제목 / 합계 강조

# Top5 설비의 '대기랏' 판정 기준. 진행 중(RUN)이 아니라 설비를 기다리는 상태.
WAITING_STATUS = ("WAIT", "WAIT(진행불가)")

# lot_type 정렬: PP > PB > PG > EG, 그 외는 뒤에 오름차순
LOT_TYPE_ORDER = ["PP", "PB", "PG", "EG"]
DEFAULT_LOT_TYPES = ["PP", "PG"]      # 카드 기본 선택


def lot_type_key(t):
    return (LOT_TYPE_ORDER.index(t), "") if t in LOT_TYPE_ORDER else (99, t)


# Low WT 분석
SHIFT_COLS = ["GY", "DAY", "SW"]
def wt_bins(max_wt):
    """WT=0 을 항상 유지하고, 그 위를 0.5 폭으로 나눈다.

    기준을 직접 입력해도(예: 2.3) 구간이 자동으로 만들어진다.
    기준을 넓혀도 WT=0 을 다른 구간과 합치지 않는다.
    """
    bins = [{"key": "0", "label": "WT = 0", "lo": None, "hi": 0.0}]
    lo = 0.0
    while lo < max_wt - 1e-9:
        hi = min(round(lo + 0.5, 3), max_wt)
        bins.append({"key": f"{lo:g}-{hi:g}",
                     "label": f"{lo:g} < WT ≤ {hi:g}", "lo": lo, "hi": hi})
        lo = hi
    return bins
# 원인 대분류. 위에서부터 우선순위. 겹치면 하나에만 귀속한다.
WT_CAUSES = ["Hold", "Wait성 진행불가", "설비", "TIP", "기타/미분류"]
LOOKBACK_DAYS = 140

# blueprint 7.3 권장 상대 높이. 템플릿에는 계산된 px 로 넘긴다.
PANEL_BASE_PX = 150
PANEL_AXIS_PX = 26      # 모든 패널에 x축 라벨이 붙으므로 그만큼 더 준다
PANELS = [
    # fmt: 차트에 직접 찍는 레이블 표기법
    #   k  = 1000 단위 + 소수 1자리 (13,300매 -> 13.3k)
    #   f1 = 소수 1자리
    #   i  = 정수
    # 툴팁(커서)에는 항상 원래 값이 그대로 나온다.
    {"key": "move",    "title": "MOVE",              "unit": "매",    "h": 1.2,
     "fmt": "k"},
    {"key": "wt",      "title": "W/T",               "unit": "매/매", "h": 1.0,
     "fmt": "f1"},
    {"key": "wip",     "title": "재공",               "unit": "매",    "h": 1.2,
     "fmt": "k"},
    {"key": "hold",    "title": "HOLD율",             "unit": "%",    "h": 0.8,
     "fmt": "i", "basis": True, "status": "HOLD"},
    {"key": "blocked", "title": "WAIT성 진행불가율",    "unit": "%",    "h": 0.8,
     "fmt": "i", "basis": True, "status": "WAIT(진행불가)"},
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
                           MIN(CASE shift WHEN 'GY' THEN '1GY' WHEN 'DAY' THEN '2DAY'
                                          ELSE '3SW' END) AS shift
                    FROM   f3_history
                    WHERE  biz_date >= %s
                    GROUP  BY biz_date
                ) pick
                  ON h.biz_date = pick.biz_date
                 AND h.shift = SUBSTR(pick.shift, 2)
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
        for k, ds in enumerate(data["datasets"]):
            ds["borderColor"] = LINE_TONES[k % len(LINE_TONES)]
            ds["backgroundColor"] = ds["borderColor"]
            ds["spanGaps"] = False
            ds["tension"] = 0.25
            ds["pointRadius"] = 2
        data.update(key=p["key"], title=p["title"], unit=p["unit"],
                    height=p["h"], fmt=p.get("fmt", "f1"),
                    status=p.get("status", ""))
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
    panels = [dict(p, px=int(round(p["h"] * PANEL_BASE_PX)) + PANEL_AXIS_PX)
              for p in PANELS]
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
        try:
            cur.execute(
                "SELECT table_name, load_start, load_end, elapsed_sec, row_count,"
                " col_count, kind FROM f3_load_log WHERE snapshot_at = %s ORDER BY id",
                [snapshot_at])
            rows = [{"table": r[0], "start": r[1], "end": r[2], "sec": r[3],
                     "rows": r[4], "cols": r[5], "kind": r[6]} for r in cur.fetchall()]
        except Exception:          # kind 컬럼 추가 이전 스냅샷
            cur.execute(
                "SELECT table_name, load_start, load_end, elapsed_sec, row_count,"
                " col_count FROM f3_load_log WHERE snapshot_at = %s ORDER BY id",
                [snapshot_at])
            rows = [{"table": r[0], "start": r[1], "end": r[2], "sec": r[3],
                     "rows": r[4], "cols": r[5], "kind": "조회"} for r in cur.fetchall()]

    if not rows:
        return rows, None

    starts = [r["start"] for r in rows if r["start"]]
    ends = [r["end"] for r in rows if r["end"]]
    total = {
        "table": "계", "kind": "",
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
                                ("table", "kind", "start", "end", "sec", "rows", "cols")}]
    log = pd.DataFrame(log_rows)
    if len(log):
        log.columns = ["테이블", "로딩_시작시각", "로딩_종료시각", "소요_초",
                       "행수", "컬럼수", "구분"]

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


# ---------------------------------------------------------------------------
# 상단 현황 카드 (라인별 현재 단면)
# ---------------------------------------------------------------------------
def _latest_snapshot():
    if not _table_exists("f3_live"):
        return None
    with connection.cursor() as cur:
        cur.execute("SELECT MAX(snapshot_at) FROM f3_live")
        return cur.fetchone()[0]


def _lot_rows(snapshot_at):
    """lot 단위로 접은 현재 단면.

    (line, lot_id, lot_type, lot_status, qty, eqpgroup)
    f3 는 lot 당 현스텝 + 연속블록 행이 있어 그대로 쓰면 중복된다.
    설비는 현스텝 행의 eqpgroup 을 쓴다.
    """
    with connection.cursor() as cur:
        cur.execute("""
            SELECT `line`, lot_id,
                   MIN(lot_type)                                     AS lot_type,
                   MIN(lot_status)                                   AS lot_status,
                   MIN(CAST(qty AS SIGNED))                          AS qty,
                   MIN(CASE WHEN `현스텝` = '현스텝' THEN eqpgroup END) AS eqpgroup
            FROM   f3_live
            WHERE  snapshot_at = %s
            GROUP  BY `line`, lot_id
        """, [snapshot_at])
        return cur.fetchall()


def _move_by_shift(types=None):
    """가장 최근 업무일의 라인별 shift MOVE.

    lot_type 이 지정되면 move_lot 을 f3_live 의 lot_type 과 연결해 걸러낸다.
    (move_lot 자체에는 lot_type 이 없다)
    """
    if not _table_exists("move_shift"):
        return {}, None
    with connection.cursor() as cur:
        cur.execute("SELECT MAX(biz_date) FROM move_shift")
        bd = cur.fetchone()[0]
        if not bd:
            return {}, None

        if types and _table_exists("move_lot") and _table_exists("f3_live"):
            ph = ",".join(["%s"] * len(types))
            cur.execute(f"""
                SELECT m.sys_line_id, m.shift, SUM(m.move_qty)
                FROM   move_lot m
                JOIN  (SELECT DISTINCT `line`, lot_id, lot_type
                       FROM   f3_live
                       WHERE  snapshot_at = (SELECT MAX(snapshot_at) FROM f3_live)) f
                  ON   f.`line` = m.sys_line_id AND f.lot_id = m.lot_id
                WHERE  m.biz_date = %s AND f.lot_type IN ({ph})
                GROUP  BY m.sys_line_id, m.shift
            """, [bd, *types])
        else:
            cur.execute("SELECT sys_line_id, shift, move_qty FROM move_shift "
                        "WHERE biz_date = %s", [bd])
        out = {}
        for line, sh, qty in cur.fetchall():
            out.setdefault(line, {})[sh] = int(qty or 0)
    return out, bd


ALL_TYPES = "전체"


def _blank():
    return {"status": {}, "eqp": {}, "lots": 0, "qty": 0}


def api_status(request):
    want = [t for t in request.GET.get("types", "").split(",") if t]
    only = request.GET.get("line", "")          # 지정 시 그 라인 카드만 반환
    snap = _latest_snapshot()
    lots = _lot_rows(snap) if snap else []
    move, move_bd = _move_by_shift(want)

    # line -> lot_type -> 집계. ALL_TYPES 버킷도 함께 채운다.
    agg = {}
    types_by_line = {}
    for line, lot_id, lot_type, status, qty, eqpgroup in lots:
        lt = (lot_type or "-").strip()
        types_by_line.setdefault(line, set()).add(lt)
        st = status or "-"
        q = num(qty)

        # 설비그룹은 n개 설비로 엮여 있으면 각 설비에 1/n LOT, 1/n 매로 나눠 계상한다.
        # (모든 설비에 1랏씩 계상하면 합계가 실제 대기량보다 부풀려진다)
        eqps = [x.strip() for x in str(eqpgroup or "").split(",") if x.strip()]
        w = 1.0 / len(eqps) if eqps else 0.0

        buckets = [ALL_TYPES, lt]
        if want and lt in want:
            buckets.append("_SEL_")
        for bucket in buckets:
            d = agg.setdefault(line, {}).setdefault(bucket, _blank())
            d["lots"] += 1
            d["qty"] += q
            c = d["status"].setdefault(st, {"lots": 0, "qty": 0})
            c["lots"] += 1
            c["qty"] += q
            if st in WAITING_STATUS and eqps:
                for e in eqps:
                    ec = d["eqp"].setdefault(e, {"lots": 0.0, "qty": 0.0})
                    ec["lots"] += w
                    ec["qty"] += q * w

    def pack(d):
        tot_l, tot_q = d["lots"], d["qty"]
        status = []
        for st in STATUS_ORDER:
            v = d["status"].get(st, {"lots": 0, "qty": 0})
            status.append({
                "name": st, "color": STATUS_COLORS[st],
                "lots": v["lots"], "qty": v["qty"],
                "pct": round(v["lots"] / tot_l * 100, 1) if tot_l else 0,
            })
        blocked = [x for x in status if x["name"] in ("HOLD", "WAIT(진행불가)")]
        # LOT 기준 상위 5와 매 기준 상위 5는 구성이 다를 수 있다.
        # 단순 재정렬이 아니라 각 기준으로 다시 뽑는다.
        def top_by(metric):
            items = sorted(d["eqp"].items(),
                           key=lambda kv: (-kv[1][metric], kv[0]))[:5]
            return [{"name": k, "lots": round(v["lots"], 1),
                     "qty": int(round(v["qty"]))} for k, v in items]
        return {
            "total": {"lots": tot_l, "qty": tot_q},
            "status": status,
            "default": {
                "label": "HOLD+진행불가",
                "pct": round(sum(x["lots"] for x in blocked) / tot_l * 100, 1)
                       if tot_l else 0,
                "lots": sum(x["lots"] for x in blocked),
                "qty": sum(x["qty"] for x in blocked),
            },
            "top_eqp": {"lots": top_by("lots"), "qty": top_by("qty")},
        }

    cards = []
    for c in LINE_CARDS:
        if only and c["line"] != only:
            continue
        d = agg.get(c["line"])
        if not d:
            cards.append({**c, "ready": False})
            continue
        types = sorted(types_by_line.get(c["line"], ()), key=lot_type_key)
        mv = move.get(c["line"], {})
        sel = d.get("_SEL_") if want else d.get(ALL_TYPES)
        cards.append({
            **c, "ready": True, "types": types,
            "default_types": [t for t in DEFAULT_LOT_TYPES if t in types],
            "sel": pack(sel) if sel else pack(_blank()),
            "by_lot_type": [
                {"name": t, "lots": d[t]["lots"], "qty": d[t]["qty"]}
                for t in types if t in d and (not want or t in want)],
            "selected_types": want,
            "move": {"GY": mv.get("GY", 0), "DAY": mv.get("DAY", 0),
                     "SW": mv.get("SW", 0),
                     "total": sum(mv.get(k, 0) for k in ("GY", "DAY", "SW"))},
        })

    now = dt.datetime.now()
    cur_shift = "GY" if (now.hour >= 22 or now.hour < 6) else (
        "DAY" if now.hour < 14 else "SW")

    return JsonResponse({
        "cards": cards,
        "current_shift": cur_shift,
        "snapshot_at": (snap.strftime("%Y-%m-%d %H:%M")
                        if hasattr(snap, "strftime") else (str(snap) if snap else None)),
        "move_biz_date": str(move_bd) if move_bd else None,
    }, json_dumps_params={"ensure_ascii": False})


# ---------------------------------------------------------------------------
# Low WT 분석
#   WT = 그 구간의 lot MOVE / 재공 매수.  MOVE 기록이 없으면 WT = 0.
#   원인은 Hold > Wait성 진행불가(exception/ftp) > 설비(down) > TIP(prevent)
#   순으로 하나에만 귀속한다(중복 카운트 없음).
# ---------------------------------------------------------------------------
def num(v, default=0):
    """f3 는 전 컬럼이 문자열이라 qty 가 '20.0' 처럼 올 수 있다.
    int('20.0') 은 ValueError 이므로 float 을 거쳐 변환한다."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _cause_of(hold, exception, ftp, down, tip, lot_status=None):
    """겹치면 위에서부터 하나에만 귀속. lot_status='HOLD' 도 Hold 로 본다
    (MEMMSS issue 기록이 없어도 상태가 HOLD 면 Hold 다)."""
    if hold or (lot_status or "") == "HOLD":
        return "Hold"
    if exception or ftp:
        return "Wait성 진행불가"
    if down:
        return "설비"
    if tip:
        return "TIP"
    return "기타/미분류"


def _bin_of(wt, max_wt):
    if wt is None or wt > max_wt + 1e-9:
        return None
    for b in wt_bins(max_wt):
        if b["lo"] is None:
            if wt <= 0:
                return b["key"]
        elif b["lo"] < wt <= b["hi"] + 1e-9:
            return b["key"]
    return None


def _wt_source(biz_date, line):
    """(구간명 -> {lot_id: (qty, cause)}), (구간명 -> {lot_id: move})"""
    types = ",".join(["%s"] * len(MOVE_LOT_TYPES))
    lots, moves = {}, {}

    with connection.cursor() as cur:
        cur.execute(f"""
            SELECT shift, lot_id,
                   MIN(CAST(qty AS SIGNED)) AS qty,
                   MIN(hold) AS hold, MIN(exception) AS exception, MIN(ftp) AS ftp,
                   MIN(CASE WHEN `현스텝`='현스텝' THEN down END) AS down,
                   MIN(CASE WHEN `현스텝`='현스텝' THEN tip END)  AS tip,
                   MIN(lot_status) AS lot_status
            FROM   f3_history
            WHERE  biz_date = %s AND `line` = %s AND lot_type IN ({types})
            GROUP  BY shift, lot_id
        """, [biz_date, line, *MOVE_LOT_TYPES])
        for sh, lot, qty, hold, exc, ftp, down, tip, st in cur.fetchall():
            lots.setdefault(sh, {})[lot] = (
                num(qty), _cause_of(hold, exc, ftp, down, tip, st))

        cur.execute("""
            SELECT shift, lot_id, SUM(move_qty)
            FROM   move_lot WHERE biz_date = %s AND sys_line_id = %s
            GROUP  BY shift, lot_id
        """, [biz_date, line])
        for sh, lot, mv in cur.fetchall():
            moves.setdefault(sh, {})[lot] = float(mv or 0)

    # 전체: 업무일 대표 스냅샷(GY 우선) 의 lot + 하루 전체 MOVE
    rep = next((s for s in ("GY", "DAY", "SW") if s in lots), None)
    if rep:
        lots["전체"] = lots[rep]
    day_move = {}
    for sh in SHIFT_COLS:
        for lot, mv in moves.get(sh, {}).items():
            day_move[lot] = day_move.get(lot, 0) + mv
    moves["전체"] = day_move
    return lots, moves


def api_lowwt(request):
    line = request.GET.get("line", "")
    max_wt = float(request.GET.get("max_wt", 0) or 0)
    if not (_table_exists("f3_history") and _table_exists("move_lot")):
        return JsonResponse({"ready": False, "reason": "f3_history / move_lot 미적재"})

    with connection.cursor() as cur:
        cur.execute("SELECT MAX(biz_date) FROM f3_history")
        bd = cur.fetchone()[0]
    if not bd:
        return JsonResponse({"ready": False, "reason": "f3_history 비어 있음"})

    lots, moves = _wt_source(bd, line)
    cols = ["전체"] + SHIFT_COLS
    bins = wt_bins(max_wt)

    summary, dist, cause = {}, {}, {}
    for col in cols:
        lt = lots.get(col, {})
        mv = moves.get(col, {})
        base = len(lt)
        low_lots, bin_cnt, cause_cnt = 0, {b["key"]: 0 for b in bins}, {}
        for lot, (qty, cs) in lt.items():
            wt = (mv.get(lot, 0.0) / qty) if qty else 0.0
            key = _bin_of(wt, max_wt)
            if key is None:
                continue
            low_lots += 1
            bin_cnt[key] += 1
            cause_cnt.setdefault(key, {}).setdefault(cs, 0)
            cause_cnt[key][cs] += 1
        summary[col] = {"base": base, "low": low_lots, "has": base > 0,
                        "rate": round(low_lots / base * 100, 1) if base else 0}
        dist[col] = bin_cnt
        cause[col] = cause_cnt

    return JsonResponse({
        "ready": True, "biz_date": str(bd), "line": line, "max_wt": max_wt,
        "cols": cols, "bins": bins, "causes": WT_CAUSES,
        "summary": summary, "dist": dist, "cause": cause,
    }, json_dumps_params={"ensure_ascii": False})


# ---------------------------------------------------------------------------
# Drill-down: Low WT lot 상세
#   f3_history 에 실제로 있는 컬럼만 내보낸다(없는 컬럼을 만들지 않는다).
# ---------------------------------------------------------------------------
LOT_DETAIL_COLS = [
    ("lot_id", "LOT"), ("lot_type", "TYPE"), ("qty", "매"),
    ("wt", "WT"), ("lot_status", "상태"),
    ("proc_id", "PROC"), ("step_seq", "STEP"), ("step_desc", "STEP명"),
    ("eqpgroup", "설비그룹"), ("cause", "원인"),
    ("hold", "HOLD"), ("hold_reason", "HOLD사유"),
    ("exception", "EXC"), ("exception_reason", "EXC사유"),
    ("ftp", "FTP"), ("ftp_reason", "FTP사유"),
    ("down", "설비상태"), ("tip", "TIP"),
    ("마지막이벤트경과_일", "마지막이벤트경과(일)"),
    ("스텝도착경과_일", "스텝도착경과(일)"),
]


def api_lots(request):
    """LOT 상세. 현황과 추이 양쪽에서 같은 형식으로 쓴다.

    line     라인 코드
    col      전체 | GY | DAY | SW
    biz_date 미지정이면 최신 업무일
    bin/cause/max_wt  Low WT 에서 넘어올 때만
    status   lot_status 필터 (FlowStack HOLD/WAIT 패널에서)
    """
    line = request.GET.get("line", "")
    col = request.GET.get("col", "전체")
    bin_key = request.GET.get("bin", "")
    cause = request.GET.get("cause", "")
    status = request.GET.get("status", "")
    wt_range = request.GET.get("wt_range", "")     # FAB현황 W/T 분포에서
    big = request.GET.get("big", "")               # 원인 대분류
    mid = request.GET.get("mid", "")               # 중분류
    sub = request.GET.get("sub", "")               # 소분류(설비명 또는 유형)
    req_date = request.GET.get("biz_date", "")
    max_wt = float(request.GET.get("max_wt", 0) or 0)

    if not (_table_exists("f3_history") and _table_exists("move_lot")):
        return JsonResponse({"rows": [], "cols": [], "reason": "미적재"})

    rules = _cause_rules()

    if req_date:
        bd = req_date
    else:
        with connection.cursor() as cur:
            cur.execute("SELECT MAX(biz_date) FROM f3_history")
            bd = cur.fetchone()[0]
    if not bd:
        return JsonResponse({"rows": [], "cols": [], "reason": "데이터 없음"})

    # 대상 shift: 전체면 업무일 대표 스냅샷
    types = ",".join(["%s"] * len(MOVE_LOT_TYPES))
    with connection.cursor() as cur:
        if col == "전체":
            cur.execute("SELECT shift FROM f3_history WHERE biz_date=%s AND `line`=%s "
                        "GROUP BY shift ORDER BY CASE shift WHEN 'GY' THEN 1 "
                        "WHEN 'DAY' THEN 2 ELSE 3 END LIMIT 1", [bd, line])
            r = cur.fetchone()
            shift = r[0] if r else "GY"
        else:
            shift = col

        need = [c for c, _ in LOT_DETAIL_COLS if c not in ("lot_id", "wt", "cause")]
        for extra in ("recipe_id",):
            if extra not in need:
                need.append(extra)
        sel = ", ".join(f"MIN(`{c}`) AS `{c}`" for c in need)
        cur.execute(f"""
            SELECT lot_id, {sel}
            FROM   f3_history
            WHERE  biz_date=%s AND `line`=%s AND shift=%s AND lot_type IN ({types})
            GROUP  BY lot_id
        """, [bd, line, shift, *MOVE_LOT_TYPES])
        names = [d[0] for d in cur.description]
        recs = [dict(zip(names, r)) for r in cur.fetchall()]

        if col == "전체":
            cur.execute("SELECT lot_id, SUM(move_qty) FROM move_lot "
                        "WHERE biz_date=%s AND sys_line_id=%s GROUP BY lot_id", [bd, line])
        else:
            cur.execute("SELECT lot_id, SUM(move_qty) FROM move_lot "
                        "WHERE biz_date=%s AND sys_line_id=%s AND shift=%s "
                        "GROUP BY lot_id", [bd, line, col])
        mv = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    out = []
    for r in recs:
        qty = num(r.get("qty"))
        wt = (mv.get(r["lot_id"], 0.0) / qty) if qty else 0.0
        if bin_key and _bin_of(wt, max_wt) != bin_key:
            continue
        if wt_range and _wt_range_key(wt) != wt_range:
            continue
        if status and (r.get("lot_status") or "") != status:
            continue
        cs = _cause_of(r.get("hold"), r.get("exception"), r.get("ftp"),
                       r.get("down"), r.get("tip"), r.get("lot_status"))
        if cause and cs != cause:
            continue

        if big or mid or sub:
            b2, m2, s2 = classify_lot(r, rules)
            if big and b2 != big:
                continue
            if mid and (m2 or "") != mid:
                continue
            if sub and sub not in s2:
                continue
        r["wt"] = round(wt, 2)
        r["cause"] = cs
        out.append({c: r.get(c) for c, _ in LOT_DETAIL_COLS})

    out.sort(key=lambda x: (x["wt"], str(x["lot_id"])))
    return JsonResponse({
        "rows": out, "cols": [{"k": c, "t": t} for c, t in LOT_DETAIL_COLS],
        "biz_date": str(bd), "line": line, "col": col,
        "bin": bin_key, "cause": cause or "전체", "status": status,
    }, json_dumps_params={"ensure_ascii": False})


# ---------------------------------------------------------------------------
# FAB현황 (단일 라인) — 재공 / status / WT 분포 / 원인 분석
# ---------------------------------------------------------------------------
# 2-4. WT 분포 구간. WT=0 을 맨 위에 두고 아래로 갈수록 정상에 가깝다.
WT_RANGES = [
    {"key": "0",   "label": "WT = 0",       "lo": None, "hi": 0.0},
    {"key": "0-1", "label": "0 < WT ≤ 1",   "lo": 0.0,  "hi": 1.0},
    {"key": "1-2", "label": "1 < WT ≤ 2",   "lo": 1.0,  "hi": 2.0},
    {"key": "2-3", "label": "2 < WT ≤ 3",   "lo": 2.0,  "hi": 3.0},
    {"key": "3-4", "label": "3 < WT ≤ 4",   "lo": 3.0,  "hi": 4.0},
    {"key": "4-5", "label": "4 < WT ≤ 5",   "lo": 4.0,  "hi": 5.0},
    {"key": "5+",  "label": "WT > 5",       "lo": 5.0,  "hi": None},
]

EQP_ISSUE = ("DOWN", "PM", "LOCAL")
TOP_N = 5


def _wt_range_key(wt):
    if wt is None:
        return None
    if wt <= 0:
        return "0"
    for r in WT_RANGES[1:-1]:
        if r["lo"] < wt <= r["hi"]:
            return r["key"]
    return "5+"


def _cause_rules():
    """기준정보의 소분류 규칙. 없으면 빈 목록(=사유 원문을 그대로 유형으로)."""
    if not _table_exists("cause_rules"):
        return {}
    out = {}
    with connection.cursor() as cur:
        cur.execute("SELECT category, keyword, label FROM cause_rules "
                    "ORDER BY category, sort_no, id")
        for cat, kw, label in cur.fetchall():
            out.setdefault(cat, []).append((kw, label))
    return out


def _classify(text, rules, fallback="미분류"):
    t = (text or "").strip()
    for kw, label in rules:
        if kw and kw in t:
            return label
    return t[:40] if t else fallback


def _eqp_status_of(down):
    for s in EQP_ISSUE:
        if s in (down or ""):
            return s
    return None


def _summary_rows(line, types):
    """현재 스냅샷의 lot 단위 원자료 (현스텝 기준)."""
    snap = _latest_snapshot()
    if not snap:
        return [], None
    cond, params = "", [snap, line]
    if types:
        cond = " AND lot_type IN (%s)" % ",".join(["%s"] * len(types))
        params += list(types)
    with connection.cursor() as cur:
        cur.execute(f"""
            SELECT lot_id,
                   MIN(lot_type) AS lot_type,
                   MIN(CAST(qty AS SIGNED)) AS qty,
                   MIN(lot_status) AS lot_status,
                   MIN(CASE WHEN `현스텝`='현스텝' THEN eqpgroup END)  AS eqpgroup,
                   MIN(CASE WHEN `현스텝`='현스텝' THEN down END)      AS down,
                   MIN(CASE WHEN `현스텝`='현스텝' THEN tip END)       AS tip,
                   MIN(CASE WHEN `현스텝`='현스텝' THEN recipe_id END) AS recipe_id,
                   MIN(CASE WHEN `현스텝`='현스텝' THEN step_seq END)  AS step_seq,
                   MIN(CASE WHEN `현스텝`='현스텝' THEN step_desc END) AS step_desc,
                   MIN(hold) AS hold, MIN(hold_reason) AS hold_reason,
                   MIN(exception) AS exception, MIN(exception_reason) AS exception_reason,
                   MIN(ftp) AS ftp, MIN(ftp_reason) AS ftp_reason
            FROM   f3_live
            WHERE  snapshot_at = %s AND `line` = %s {cond}
            GROUP  BY lot_id
        """, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()], snap


def _lot_move_map(line):
    """당일 lot 별 MOVE (WT 계산용)."""
    if not _table_exists("move_lot"):
        return {}
    with connection.cursor() as cur:
        cur.execute("SELECT MAX(biz_date) FROM move_lot")
        bd = cur.fetchone()[0]
        if not bd:
            return {}
        cur.execute("SELECT lot_id, SUM(move_qty) FROM move_lot "
                    "WHERE biz_date=%s AND sys_line_id=%s GROUP BY lot_id", [bd, line])
        return {r[0]: float(r[1] or 0) for r in cur.fetchall()}


def _bucket(d, key, qty):
    c = d.setdefault(key, {"lots": 0, "qty": 0})
    c["lots"] += 1
    c["qty"] += qty


def classify_lot(r, rules):
    """LOT 1건 -> (대분류, 중분류, 소분류후보리스트).

    소분류가 설비 단위인 경우 lot 이 여러 설비에 걸릴 수 있어 리스트로 준다.
    api_summary 와 api_lots 가 같은 함수를 써야 화면과 드릴다운이 어긋나지 않는다.
    """
    st = r.get("lot_status") or "-"
    eqps = [x.strip() for x in str(r.get("eqpgroup") or "").split(",") if x.strip()]

    if r.get("hold") or st == "HOLD":
        return ("Hold", None,
                [_classify(r.get("hold_reason"), rules.get("hold", []))])

    virtual = "WAIT" in " ".join(
        str(r.get(k) or "") for k in ("recipe_id", "step_seq", "step_desc")).upper()

    if st == "WAIT(진행불가)" or r.get("exception") or r.get("ftp") \
            or _eqp_status_of(r.get("down")) or r.get("tip"):
        if r.get("exception"):
            return ("Wait성 진행불가", "예약제외",
                    [_classify(r.get("exception_reason"), rules.get("exception", []))])
        if r.get("ftp"):
            return ("Wait성 진행불가", "FTP",
                    [_classify(r.get("ftp_reason"), rules.get("ftp", []))])
        eqp_st = _eqp_status_of(r.get("down"))
        if eqp_st:
            return ("Wait성 진행불가", "설비이슈", eqps or ["(설비미상)"])
        if r.get("tip"):
            return ("Wait성 진행불가", "TIP", eqps or ["(설비미상)"])
        if virtual:
            return ("Wait성 진행불가", "가상스텝 대기", ["가상스텝"])
        return ("Wait성 진행불가", "기타", ["미분류"])

    if st == "WAIT":
        if virtual:
            return ("Wait성 진행불가", "가상스텝 대기", ["가상스텝"])
        return ("Bottleneck", None, eqps or ["(설비미상)"])

    return (None, None, [])


def _add(node, lots, qty):
    node["lots"] += lots
    node["qty"] += qty


def api_summary(request):
    """FAB현황 좌측 summary 뷰 한 벌."""
    line = request.GET.get("line") or DEFAULT_LINE
    raw = request.GET.get("types")
    types = ([t for t in raw.split(",") if t] if raw is not None
             else list(DEFAULT_LOT_TYPES))

    if not _table_exists("f3_live"):
        return JsonResponse({"ready": False, "reason": "f3_live 미적재"})

    rows, snap = _summary_rows(line, types)
    if not rows:
        return JsonResponse({"ready": False, "reason": "이 라인의 스냅샷이 없습니다"})

    # lot_type 목록은 필터와 무관하게 전체에서 뽑아야 토글이 유지된다
    all_rows, _ = _summary_rows(line, [])
    all_types = sorted({(r.get("lot_type") or "-") for r in all_rows}, key=lot_type_key)

    mv = _lot_move_map(line)
    rules = _cause_rules()

    tot = {"lots": 0, "qty": 0}
    by_type, by_status, by_wt = {}, {}, {}
    tree = {}

    for r in rows:
        q = num(r.get("qty"))
        st = r.get("lot_status") or "-"
        tot["lots"] += 1
        tot["qty"] += q
        _bucket(by_type, r.get("lot_type") or "-", q)
        _bucket(by_status, st, q)
        _bucket(by_wt, _wt_range_key((mv.get(r["lot_id"], 0.0) / q) if q else 0.0), q)

        big, mid, subs = classify_lot(r, rules)
        if not big:
            continue
        w = 1.0 / len(subs) if subs else 0.0      # 설비 여러 개면 지분으로 나눔
        g = tree.setdefault(big, {"lots": 0.0, "qty": 0.0, "mid": {}})
        _add(g, 1, q)
        mkey = mid or "_"
        m = g["mid"].setdefault(mkey, {"lots": 0.0, "qty": 0.0, "sub": {}})
        _add(m, 1, q)
        for sname in subs:
            sc = m["sub"].setdefault(sname, {"lots": 0.0, "qty": 0.0})
            _add(sc, w, q * w)

    def node(name, v, extra=None):
        d = {"name": name, "lots": round(v["lots"], 1), "qty": int(round(v["qty"]))}
        if extra:
            d.update(extra)
        return d

    def subs_of(m, limit=TOP_N):
        items = sorted(m["sub"].items(), key=lambda kv: (-kv[1]["qty"], kv[0]))
        return [node(k, v) for k, v in items[:limit]]

    causes = []
    for big in ("Hold", "Wait성 진행불가", "Bottleneck"):
        g = tree.get(big)
        if not g:
            causes.append({"name": big, "lots": 0, "qty": 0, "children": []})
            continue
        children = []
        for mkey, m in sorted(g["mid"].items(), key=lambda kv: -kv[1]["qty"]):
            if mkey == "_":
                children = subs_of(m, 12)          # 중분류 없는 대분류
            else:
                children.append(node(mkey, m, {"children": subs_of(m)}))
        causes.append(node(big, g, {"children": children}))

    status = [{"name": s, "color": STATUS_COLORS[s],
               "lots": by_status.get(s, {}).get("lots", 0),
               "qty": by_status.get(s, {}).get("qty", 0),
               "pct": round(by_status.get(s, {}).get("lots", 0) / tot["lots"] * 100, 1)
                      if tot["lots"] else 0}
              for s in STATUS_ORDER]
    blocked = [x for x in status if x["name"] in ("HOLD", "WAIT(진행불가)")]

    return JsonResponse({
        "ready": True, "line": line,
        "snapshot_at": (snap.strftime("%Y-%m-%d %H:%M")
                        if hasattr(snap, "strftime") else str(snap)),
        "types": all_types, "selected_types": types,
        "total": tot,
        "by_lot_type": [{"name": k, **by_type[k]}
                        for k in sorted(by_type, key=lot_type_key)],
        "status": status,
        "default": {
            "label": "HOLD+진행불가",
            "pct": round(sum(x["lots"] for x in blocked) / tot["lots"] * 100, 1)
                   if tot["lots"] else 0,
            "lots": sum(x["lots"] for x in blocked),
            "qty": sum(x["qty"] for x in blocked)},
        "wt_ranges": WT_RANGES,
        "wt_dist": [{"name": r["key"], "label": r["label"],
                     **by_wt.get(r["key"], {"lots": 0, "qty": 0})}
                    for r in WT_RANGES],
        "causes": causes,
    }, json_dumps_params={"ensure_ascii": False})


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------
MENU = [("FAB현황", "/"), ("기준정보", "/standards/"), ("다운로드", "/downloads/")]
DEFAULT_LINE = "KFR7"          # NRD-K


def fab_status(request):
    lines = [{"label": c["label"], "line": c["line"]} for c in LINE_CARDS]
    ready = set()
    if _table_exists("f3_live"):
        snap = _latest_snapshot()
        if snap:
            with connection.cursor() as cur:
                cur.execute("SELECT DISTINCT `line` FROM f3_live WHERE snapshot_at=%s",
                            [snap])
                ready = {r[0] for r in cur.fetchall()}
    for x in lines:
        x["ready"] = x["line"] in ready
    return render(request, "flowmonitor/fab_status.html",
                  {"menu": MENU, "lines": lines, "default_line": DEFAULT_LINE})


def fab_metrics(request):
    """FAB지표. 메뉴에는 노출하지 않지만 URL 로는 접근 가능하다.
    Shift 별 MOVE 와 추이 그래프가 이쪽으로 옮겨왔다."""
    panels = [dict(p, px=int(round(p["h"] * PANEL_BASE_PX)) + PANEL_AXIS_PX)
              for p in PANELS]
    return render(request, "flowmonitor/fab_metrics.html",
                  {"menu": MENU, "panels": panels,
                   "lines": [{"label": c["label"], "line": c["line"]}
                             for c in LINE_CARDS]})


def standards(request):
    """기준정보: 원인 소분류 규칙 편집."""
    import db_common as DB

    msg = ""
    if request.method == "POST":
        conn = DB.connect()
        DB.ensure_standard_schema(conn)
        with conn.cursor() as cur:
            if request.POST.get("delete"):
                cur.execute("DELETE FROM cause_rules WHERE id=%s",
                            [request.POST["delete"]])
                msg = "삭제했습니다."
            else:
                cur.execute(
                    "INSERT INTO cause_rules (category, keyword, label, sort_no,"
                    " updated_at) VALUES (%s,%s,%s,%s,NOW())",
                    [request.POST.get("category", "hold"),
                     request.POST.get("keyword", "").strip(),
                     request.POST.get("label", "").strip() or "미지정",
                     int(request.POST.get("sort_no") or 100)])
                msg = "추가했습니다."
        conn.commit()
        conn.close()

    rules = []
    if _table_exists("cause_rules"):
        with connection.cursor() as cur:
            cur.execute("SELECT id, category, keyword, label, sort_no "
                        "FROM cause_rules ORDER BY category, sort_no, id")
            rules = [{"id": r[0], "category": r[1], "keyword": r[2],
                      "label": r[3], "sort_no": r[4]} for r in cur.fetchall()]
    return render(request, "flowmonitor/standards.html",
                  {"menu": MENU, "rules": rules, "msg": msg,
                   "categories": ["hold", "exception", "ftp"]})


def api_lots_live(request):
    """FAB현황 드릴다운. 현재 단면(f3_live) 기준.

    W/T 분포 막대와 원인 분석 노드에서 넘어온 조건을 그대로 적용한다.
    분류는 api_summary 와 동일한 classify_lot 을 쓴다.
    """
    line = request.GET.get("line") or DEFAULT_LINE
    raw = request.GET.get("types")
    types = ([t for t in raw.split(",") if t] if raw is not None
             else list(DEFAULT_LOT_TYPES))
    wt_range = request.GET.get("wt_range", "")
    big = request.GET.get("big", "")
    mid = request.GET.get("mid", "")
    sub = request.GET.get("sub", "")

    if not _table_exists("f3_live"):
        return JsonResponse({"rows": [], "cols": [], "reason": "f3_live 미적재"})

    rows, snap = _summary_rows(line, types)
    mv = _lot_move_map(line)
    rules = _cause_rules()

    out = []
    for r in rows:
        q = num(r.get("qty"))
        wt = (mv.get(r["lot_id"], 0.0) / q) if q else 0.0
        if wt_range and _wt_range_key(wt) != wt_range:
            continue
        b2, m2, s2 = classify_lot(r, rules)
        if big and b2 != big:
            continue
        if mid and (m2 or "") != mid:
            continue
        if sub and sub not in s2:
            continue
        rec = dict(r)
        rec["wt"] = round(wt, 2)
        rec["cause"] = " / ".join(x for x in (b2, m2) if x)
        out.append({c: rec.get(c) for c, _ in LOT_DETAIL_COLS})

    out.sort(key=lambda x: (x["wt"], str(x["lot_id"])))
    return JsonResponse({
        "rows": out, "cols": [{"k": c, "t": t} for c, t in LOT_DETAIL_COLS],
        "line": line, "snapshot_at": str(snap) if snap else "",
    }, json_dumps_params={"ensure_ascii": False})
