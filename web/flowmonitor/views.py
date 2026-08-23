# -*- coding: utf-8 -*-
"""FlowStack 화면 / 지표 API.

패널 순서(blueprint 7.1): MOVE - W/T - 재공 - HOLD율 - WAIT성 진행불가율
공통 x축은 [3개월][4주][7일].
지표 정의는 docs/common_conventions.md 참조.
"""
import datetime as dt

import json

from django.db import connection, ProgrammingError, OperationalError
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from .chartdata import build_panel

# 전체 톤은 진한 남색 계열로 통일한다. 색은 라인 구분이 아니라
# 정보 전달(status)과 강조에만 쓴다.
BRAND = "#2F4B7C"
LINE_TONES = ["#2F4B7C", "#4C7DD1", "#7FB3E8", "#A9C8E8"]

MOVE_LOT_TYPES = ("PP", "PB", "PG")

# 상단 메뉴는 모든 페이지가 공유한다.
MENU = [("FAB현황", "/main/"), ("기준정보", "/master/"),
        ("다운로드", "/downloads/")]


def _fmt_snap(v):
    if v is None or v == "":
        return "-"
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M")
    return str(v)[:16]          # '2026-08-14 15:30:00' -> '2026-08-14 15:30'


def base_ctx(**kw):
    """모든 페이지 공통 컨텍스트. 헤더의 적재시각을 여기서 채운다."""
    snap = None
    try:
        if _table_exists("f3_live"):
            snap = _latest_snapshot()
    except Exception:
        snap = None
    ctx = {"menu": MENU, "snapshot_at": _fmt_snap(snap)}
    ctx.update(kw)
    return ctx
DEFAULT_LINE = "KFR4"          # NRD

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
        "SELECT biz_date, sys_line_id, move_qty FROM f3_move_daily WHERE biz_date >= %s",
        [since], "f3_move_daily")}

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
# 요약카드에서 고른 행을 그대로 재현하는 조건.
# 최상단 필터(prod2/area)를 건드리면 요약카드 자체가 바뀌므로 따로 둔다.
XKEYS = (("x_prod", "prod2"), ("x_mod", "module1"),
         ("x_area", "AREA"), ("x_eqp", None))


def _xfilters(request):
    """요약카드 전용 조건을 읽는다. 없으면 빈 dict."""
    out = {}
    for p, _col in XKEYS:
        v = [x for x in request.GET.get(p, "").split(",") if x]
        if v:
            out[p] = v
    return out


def _fline_ok(r, want):
    """라인 ALL 에서 조각(라인)을 고른 경우. 표와 LOT BALANCE 에도 걸린다."""
    return (not want) or (str(r.get("line") or "-") in want)


def _farea_ok(r, want):
    """AREA 별 재공 차트에서 고른 조건. 표와 LOT BALANCE 에도 걸린다."""
    if not want:
        return True
    return (str(r.get("AREA") or "").strip() or UNCLASSIFIED) in want


def _xrow_ok(r, xf):
    """행이 요약카드 조건에 맞는지."""
    if not xf:
        return True
    for p, col in XKEYS:
        want = xf.get(p)
        if not want:
            continue
        if p == "x_eqp":                       # 설비 ID 는 목록으로 본다
            ids = set(_eqp_ids(r))
            ids.add(str(r.get("eqpgroup") or "").strip())
            if not (ids & set(want)):
                return False
            continue
        if (str(r.get(col) or "").strip() or UNCLASSIFIED) not in want:
            return False
    return True


def _ln_map(d):
    """{status -> {라인 -> 카운터}} 를 화면용으로."""
    return {a: {b: {"qty": int(c["qty"]), "lots": int(c["lots"])}
                for b, c in v.items()} for a, v in (d or {}).items()}


def _dist_rows(d):
    """{키 -> {status -> 카운터}} 를 화면용 목록으로. 매수 많은 순."""
    def tot(v):
        return sum(c["qty"] for c in v.values())

    return [{"name": k, "qty": int(tot(v)),
             "lots": int(sum(c["lots"] for c in v.values())),
             "by_status": {a: {"qty": int(c["qty"]), "lots": int(c["lots"])}
                           for a, c in v.items()}}
            for k, v in sorted(d.items(), key=lambda kv: -tot(kv[1]))]


def _columns_of(name):
    """테이블의 컬럼 이름 집합. 없으면 빈 집합."""
    try:
        with connection.cursor() as cur:
            return {d[0] for d in connection.introspection.get_table_description(
                cur, name)}
    except Exception:
        return set()


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
                " col_count, kind, query_time FROM f3_load_log "
                "WHERE snapshot_at = %s ORDER BY id", [snapshot_at])
            rows = [{"table": r[0], "start": r[1], "end": r[2],
                     "sec": (None if r[3] is None else round(float(r[3]), 2)),
                     "rows": r[4], "cols": r[5], "kind": r[6], "qt": r[7]}
                    for r in cur.fetchall()]
        except Exception:          # kind / query_time 추가 이전 스냅샷
            cur.execute(
                "SELECT table_name, load_start, load_end, elapsed_sec, row_count,"
                " col_count FROM f3_load_log WHERE snapshot_at = %s ORDER BY id",
                [snapshot_at])
            rows = [{"table": r[0], "start": r[1], "end": r[2],
                     "sec": (None if r[3] is None else round(float(r[3]), 2)),
                     "rows": r[4], "cols": r[5], "kind": "조회", "qt": None}
                    for r in cur.fetchall()]

    if not rows:
        return rows, None

    starts = [r["start"] for r in rows if r["start"]]
    ends = [r["end"] for r in rows if r["end"]]
    qts = sorted({r["qt"] for r in rows if r.get("qt")})
    total = {
        "table": "계", "kind": "", "qt": (qts[-1] if qts else None),
        "start": min(starts) if starts else None,
        "end": max(ends) if ends else None,
        "sec": round(sum(r["sec"] or 0 for r in rows), 2),
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

    # 좌측은 S3 원천(매니페스트 기록) 목록. 없으면 예전 방식으로 폴백한다.
    src = [r for r in log if r.get("kind") == "원천"]
    if not src:
        src = [r for r in log if r.get("kind") != "처리"]
    src = sorted(src, key=lambda r: (r.get("qt") or "9999"))

    log = [r for r in log if r.get("kind") != "원천"]   # 우측은 처리 구간만
    qts = [r.get("qt") for r in src if r.get("qt")]
    starts = [r.get("start") for r in src if r.get("start")]
    src_total = ({"n": len(src),
                  "qt_min": (min(qts) if qts else None),
                  "qt_max": (max(qts) if qts else None),
                  "rows": sum(r.get("rows") or 0 for r in src),
                  "start": (min(starts) if starts else None)}
                 if src else None)
    return render(request, "flowmonitor/downloads.html", {
        "sources": src, "src_total": src_total,
        "menu": MENU,
        "snapshot_at": _fmt_snap(sel),
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
                                ("table", "kind", "qt", "start", "end", "sec",
                                 "rows", "cols")}]
    log = pd.DataFrame(log_rows)
    if len(log):
        log.columns = ["테이블", "로딩_시작시각", "로딩_종료시각", "소요_초",
                       "행수", "컬럼수", "구분", "원천조회시각"]

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
    for t in ("f3_move_daily", "f3_move_shift", "f3_live", "f3_history",
              "f3_history_meta", "f3_load_log"):
        if not _table_exists(t):
            info["tables"][t] = "없음"
            continue
        with connection.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            info["tables"][t] = f"{cur.fetchone()[0]:,}행"

    if _table_exists("f3_move_daily"):
        with connection.cursor() as cur:
            cur.execute("SELECT MIN(biz_date), MAX(biz_date), "
                        "COUNT(DISTINCT biz_date), COUNT(DISTINCT sys_line_id) "
                        "FROM f3_move_daily")
            r = cur.fetchone()
            info["samples"]["f3_move_daily"] = {
                "기간": f"{r[0]} ~ {r[1]}", "일수": r[2], "라인수": r[3]}
    if _table_exists("f3_history"):
        with connection.cursor() as cur:
            cur.execute("SELECT biz_date, shift, COUNT(*) FROM f3_history "
                        "GROUP BY biz_date, shift ORDER BY biz_date DESC LIMIT 5")
            info["samples"]["f3_history"] = [
                {"biz_date": str(a), "shift": b, "rows": c} for a, b, c in cur.fetchall()]

    move, wip, missing = _fetch()
    info["조회결과"] = {
        "f3_move_daily 행": len(move), "f3_history 집계 행": len(wip),
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

    lot_type 이 지정되면 f3_move_lot 을 f3_live 의 lot_type 과 연결해 걸러낸다.
    (f3_move_lot 자체에는 lot_type 이 없다)
    """
    if not _table_exists("f3_move_shift"):
        return {}, None
    with connection.cursor() as cur:
        cur.execute("SELECT MAX(biz_date) FROM f3_move_shift")
        bd = cur.fetchone()[0]
        if not bd:
            return {}, None

        if types and _table_exists("f3_move_lot") and _table_exists("f3_live"):
            ph = ",".join(["%s"] * len(types))
            cur.execute(f"""
                SELECT m.sys_line_id, m.shift, SUM(m.move_qty)
                FROM   f3_move_lot m
                JOIN  (SELECT DISTINCT `line`, lot_id, lot_type
                       FROM   f3_live
                       WHERE  snapshot_at = (SELECT MAX(snapshot_at) FROM f3_live)) f
                  ON   f.`line` = m.sys_line_id AND f.lot_id = m.lot_id
                WHERE  m.biz_date = %s AND f.lot_type IN ({ph})
                GROUP  BY m.sys_line_id, m.shift
            """, [bd, *types])
        else:
            cur.execute("SELECT sys_line_id, shift, move_qty FROM f3_move_shift "
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
def num_f(v, default=None):
    """소수 허용 숫자 변환. 경과일처럼 실수 컬럼용."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def calc_wt(mv, lot_id, qty, status):
    """lot 의 W/T.

    MOVE 는 TrackOut 이 끝난 것만 잡히므로, 지금 막 RUN 으로 들어간 lot 은
    실제로는 진행 중인데 W/T=0 으로 보인다. 그 잡음을 걷어내려고
    **MOVE 가 없는 RUN 은 한 스텝(=1)** 으로 본다.
    이렇게 해야 W/T=0 이 '작업 없이 멈춰 있는 재공' 만 남는다.
    """
    if not qty:
        return 0.0
    wt = mv.get(lot_id, 0.0) / qty
    if wt <= 0 and str(status or "").upper() == "RUN":
        return 1.0
    return wt


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
                num(qty), _cause_of(hold, exc, ftp, down, tip, st), st)

        cur.execute("""
            SELECT shift, lot_id, SUM(move_qty)
            FROM   f3_move_lot WHERE biz_date = %s AND sys_line_id = %s
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
    if not (_table_exists("f3_history") and _table_exists("f3_move_lot")):
        return JsonResponse({"ready": False, "reason": "f3_history / f3_move_lot 미적재"})

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
        for lot, (qty, cs, st) in lt.items():
            wt = calc_wt(mv, lot, qty, st)
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
# 드릴다운 표의 전체 컬럼. 이름은 여기서만 바꾼다(모든 프리셋에 함께 적용된다).
LOT_DETAIL_COLS = [
    ("line", "LINE"), ("전산라인", "전산라인"),
    ("dest_line_id", "DEST_LINE"), ("fa_object4", "FA_OBJECT4"),
    ("prod1", "제품군"), ("prod2", "제품"), ("dept", "DEPT"),
    ("lot_id", "LOT"), ("lot_type", "TYPE"), ("qty", "QTY"), ("wt", "W/T"),
    ("lot_status", "상태"), ("proc_id", "PROC"),
    ("module1", "모듈1"), ("module2", "모듈2"),
    ("layer_id", "LAYER"), ("AREA", "AREA"),
    ("step_seq", "STEP"), ("step_desc", "DESC"),
    ("eqpgroup", "EQP그룹"), ("eqpgroup_cham", "CHAM그룹"), ("recipe_id", "RCP"),
    ("cause", "제약원인"), ("down", "설비상태"), ("tip", "TIP"),
    ("hold", "HOLD_일"), ("hold_reason", "HOLD사유"),
    ("exception", "예약제외_일"), ("exception_reason", "예약제외사유"),
    ("ftp", "FTP_일"), ("ftp_reason", "FTP사유"),
    ("마지막이벤트경과_일", "마지막이벤트경과_일"),
    ("스텝도착경과_일", "스텝도착경과_일"),
    ("마지막작업경과_일", "마지막TKOUT경과_일"),
]

# 드릴다운 종류별 기본 컬럼과 순서.
# 여기 없는 컬럼은 체크가 꺼진 채로 시작하고, 다시 켜면 볼 수 있다.
_BASE = ["prod1", "prod2", "lot_id", "lot_type", "qty", "wt", "proc_id",
         "module1", "module2", "layer_id", "AREA", "step_seq", "step_desc",
         "eqpgroup", "eqpgroup_cham", "recipe_id"]
_ELAPSED = ["마지막이벤트경과_일", "스텝도착경과_일", "마지막작업경과_일"]

DRILL_PRESETS = {
    # 재공 구성 막대
    "type": (["line", "전산라인", "dest_line_id", "fa_object4"] + _BASE
             + ["cause", "down", "tip", "hold", "hold_reason",
                "exception", "exception_reason", "ftp", "ftp_reason"] + _ELAPSED),
    # status WAIT / Bottleneck
    "wait": (["line", "cause", "down", "tip"] + _BASE + _ELAPSED),
    # status WAIT(진행불가) / Wait성 진행불가
    "waitng": (["line", "cause", "down", "tip", "exception", "ftp"] + _BASE
               + ["exception_reason", "ftp_reason"] + _ELAPSED),
    # status HOLD / Hold
    "hold": (["line", "cause", "hold"] + _BASE + ["hold_reason"] + _ELAPSED),
    # status RUN
    "run": ["line"] + _BASE,
    # W/T 분포 · W/T0 재공 분포
    "wt": (["wt", "cause", "down", "tip", "hold", "exception", "ftp",
            "line", "전산라인"] + [c for c in _BASE if c != "wt"]
           + ["hold_reason", "exception_reason", "ftp_reason"] + _ELAPSED),
}


def _preset_for(src, status, big, lot_type, wt_range, wt0):
    """어떤 드릴다운인지에 따라 기본 컬럼 묶음을 고른다."""
    if wt_range or wt0:
        return "wt"
    key = status or ""
    if big == "Bottleneck":
        key = "WAIT"
    elif big == "Wait성 진행불가":
        key = "WAIT(진행불가)"
    elif big == "Hold":
        key = "HOLD"
    if key == "WAIT":
        return "wait"
    if key == "WAIT(진행불가)":
        return "waitng"
    if key == "HOLD":
        return "hold"
    if key == "RUN":
        return "run"
    if src == "type" or lot_type:
        return "type"
    return None


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

    if not (_table_exists("f3_history") and _table_exists("f3_move_lot")):
        return JsonResponse({"rows": [], "cols": [], "reason": "미적재"})

    rules = _cause_rules()
    ht = _holdtype_rules()


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
            cur.execute("SELECT lot_id, SUM(move_qty) FROM f3_move_lot "
                        "WHERE biz_date=%s AND sys_line_id=%s GROUP BY lot_id", [bd, line])
        else:
            cur.execute("SELECT lot_id, SUM(move_qty) FROM f3_move_lot "
                        "WHERE biz_date=%s AND sys_line_id=%s AND shift=%s "
                        "GROUP BY lot_id", [bd, line, col])
        mv = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    out = []
    for r in recs:
        qty = num(r.get("qty"))
        wt = calc_wt(mv, r["lot_id"], qty, r.get("lot_status"))
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
            b2, m2, s2 = classify_lot(r, rules, ht)
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

# W/T=0 재공을 '마지막 작업 이후 며칠 지났는가' 로 나눈다.
# 2일이하 = 1일 초과 ~ 2일 이하.
WT0_BINS = [
    {"key": "d1",  "label": "1일 이하",  "lo": None, "hi": 1.0},
    {"key": "d2",  "label": "2일 이하",  "lo": 1.0,  "hi": 2.0},
    {"key": "d3",  "label": "3일 이하",  "lo": 2.0,  "hi": 3.0},
    {"key": "d4",  "label": "4일 이하",  "lo": 3.0,  "hi": 4.0},
    {"key": "d5",  "label": "5일 이하",  "lo": 4.0,  "hi": 5.0},
    {"key": "d7",  "label": "7일 이하",  "lo": 5.0,  "hi": 7.0},
    {"key": "d15", "label": "15일 이하", "lo": 7.0,  "hi": 15.0},
    {"key": "d15p", "label": "15일 초과", "lo": 15.0, "hi": None},
]

# 제품구분이 붙지 않은 lot 도 골라볼 수 있어야 한다.
UNCLASSIFIED = "(미분류)"

EQP_ISSUE = ("DOWN", "PM", "LOCAL")
TOP_N = 5          # 상단 요약 차트에 그릴 개수
SUB_MAX = 15       # 서버가 내려주는 소분류 최대 개수(드릴다운 차트용)


def _wt0_bin(days):
    """마지막작업경과_일 -> 구간 키."""
    if days is None:
        return None
    for b in WT0_BINS:
        lo_ok = b["lo"] is None or days > b["lo"]
        hi_ok = b["hi"] is None or days <= b["hi"]
        if lo_ok and hi_ok:
            return b["key"]
    return None


def _wt_range_key(wt):
    if wt is None:
        return None
    if wt <= 0:
        return "0"
    for r in WT_RANGES[1:-1]:
        if r["lo"] < wt <= r["hi"]:
            return r["key"]
    return "5+"


def _holdtype_rules():
    """f3_std_holdtype 규칙. 없으면 빈 목록.

    반환 순서 = 우선순위. **sort_no 가 절대 기준**이고 위에 있는 것이 먼저다.
    sort_no 가 아직 없는 행(예전에 넣은 것)은 조건이 많은 순으로 뒤에 붙인다.
    비교는 모두 대문자로 한다(사용자가 소문자로 넣어도 걸리게).
    """
    if not _table_exists("f3_std_holdtype"):
        return []
    has_sort = "sort_no" in _columns_of("f3_std_holdtype")
    col = "sort_no" if has_sort else "id"
    with connection.cursor() as cur:
        cur.execute(f"SELECT id, line, type, condition1, condition2, condition3,"
                    f" type_name, {col} AS sort_no FROM f3_std_holdtype")
        rows = [{"id": r[0], "line": r[1], "type": (r[2] or "ALL").upper(),
                 "c": [str(x).upper() for x in (r[3], r[4], r[5]) if x],
                 "name": r[6], "sort_no": r[7]} for r in cur.fetchall()]

    def spec(r):
        return len(r["c"]) + (1 if r["line"] else 0) \
            + (0 if r["type"] == "ALL" else 1)

    # sort_no 가 있으면 그것만 본다. 없는 행은 맨 뒤로.
    BIG = 10 ** 9
    return sorted(rows, key=lambda r: (
        r["sort_no"] if r["sort_no"] is not None else BIG, -spec(r), r["id"]))


# 동시에 걸리면 이 순서로 하나만 쓴다
HOLDTYPE_ORDER = [("HOLD", "hold", "hold_reason"),
                  ("FTP", "ftp", "ftp_reason"),
                  ("예약제외", "exception", "exception_reason")]


def holdtype_of(r, rules):
    """기준정보로 세부 유형을 찾는다. 못 찾으면 None."""
    if not rules:
        return None
    line = str(r.get("line") or "")
    for kind, flag, reason in HOLDTYPE_ORDER:
        if not r.get(flag):
            continue
        text = str(r.get(reason) or "").upper()   # 대소문자 무시
        if not text:
            continue
        for rule in rules:
            if rule["line"] and rule["line"] != line:
                continue
            if rule["type"] not in ("ALL", kind):
                continue
            if all(c in text for c in rule["c"]):
                return rule["name"]
    return None


def _sub_name(r, ht, kind, reason_col, rules, cat):
    """소분류 이름.

    build_f3 가 미리 넣어 둔 cause_detail 을 우선 쓴다(전처리 결과).
    기준정보를 방금 고쳐 아직 반영 전이면 그 자리에서 계산한다.
    """
    pre = r.get("cause_detail")
    if pre:
        return pre
    if ht:
        text = str(r.get(reason_col) or "")
        line = str(r.get("line") or "")
        for rule in ht:
            if rule["line"] and rule["line"] != line:
                continue
            if rule["type"] not in ("ALL", kind):
                continue
            if text and all(c in text for c in rule["c"]):
                return rule["name"]
    return _classify(r.get(reason_col), rules.get(cat, []))


def _cause_rules():
    """기준정보의 소분류 규칙. 없으면 빈 목록(=사유 원문을 그대로 유형으로)."""
    if not _table_exists("f3_cause_rules"):
        return {}
    out = {}
    with connection.cursor() as cur:
        cur.execute("SELECT category, keyword, label FROM f3_cause_rules "
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


# 현스텝 행에서만 의미가 있는 컬럼(연속블록 행에는 값이 없다)
# lot 단위로 접을 때 **현스텝 행의 값**을 써야 하는 컬럼.
# 여기 없으면 전 행의 MIN() 이 되어 연속블록 값이 섞인다.
STEP_SCOPED = ("eqpgroup", "eqpgroup_cham", "down", "tip", "recipe_id",
               "step_seq", "step_desc", "eqp_type", "batch_kind", "eqpline",
               "order_seq", "layer_id", "AREA", "de_rank", "연속", "현스텝",
               "module1", "module2",
               "lot_status", "step_status",
               "hold", "hold_reason", "exception", "exception_reason",
               "ftp", "ftp_reason", "cause_detail")


def _hist_snapshots(line):
    """날짜/shift 선택지. f3_history 의 (biz_date, shift, snapshot_at).

    라인으로 좁히면 목록이 비는 일이 있다(예: KFR4 는 최근 분류라 과거
    스냅샷에 없다). 날짜/shift 는 라인과 무관하므로 전체에서 뽑는다.
    """
    if not _table_exists("f3_history"):
        return []
    with connection.cursor() as cur:
        cur.execute(
            "SELECT biz_date, shift, MAX(snapshot_at) FROM f3_history "
            "GROUP BY biz_date, shift "
            "ORDER BY biz_date DESC, MAX(snapshot_at) DESC LIMIT 60")
        return [{"biz_date": str(r[0]), "shift": r[1],
                 "at": (r[2].strftime("%H:%M") if hasattr(r[2], "strftime")
                        else str(r[2])[11:16])}
                for r in cur.fetchall()]


_ROWS_CACHE = {}          # (스냅샷, 조건) -> rows. 같은 스냅샷이면 재사용한다.
_CLS_CACHE = {}           # lot -> (대분류, 중분류, 소분류). 스냅샷 단위 캐시.


def _summary_rows(line, types, extra=None, biz_date=None, shift=None):
    """현재 스냅샷의 lot 단위 원자료.

    SELECT 목록을 손으로 관리하면 컬럼이 늘 때마다 빠뜨린다(실제로
    경과일/fa_object4 가 통째로 NULL 이었다). LOT_DETAIL_COLS 에서 자동 생성한다.
    """
    # 같은 스냅샷·조건이면 다시 읽지 않는다. 좌측을 누를 때마다
    # 원인 분석이 전체를 재조회해 1초 넘게 걸리던 문제를 없앤다.
    ckey = (line, tuple(types or ()), biz_date, shift,
            tuple(sorted((k, tuple(v or ())) for k, v in (extra or {}).items())))

    # 날짜/shift 를 고르면 과거 스냅샷(f3_history)에서 읽는다.
    hist = bool(biz_date and shift)
    table = "f3_history" if hist else "f3_live"
    if hist:
        if not _table_exists("f3_history"):
            return [], None
        snap = f"{biz_date} {shift}"
    else:
        snap = _latest_snapshot()
        if not snap:
            return [], None
    hit = _ROWS_CACHE.get(ckey)
    if hit and hit[0] == snap:
        return hit[1], snap

    with connection.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} LIMIT 0")
        have = {d[0] for d in cur.description}
    have_l = {c.lower() for c in have}

    want = [c for c, _ in LOT_DETAIL_COLS if c not in ("lot_id", "wt", "cause")]
    for c in ("recipe_id", "step_seq", "step_desc", "lot_type"):
        if c not in want:
            want.append(c)

    sel = []
    for c in want:
        if c not in have:
            continue
        if c == "qty":
            sel.append("MIN(CAST(`qty` AS SIGNED)) AS `qty`")
        elif c in STEP_SCOPED:
            # 현스텝 행이 없는 lot 도 있어 전체 MIN 으로 보완한다.
            sel.append(f"COALESCE(MIN(CASE WHEN `현스텝`='현스텝' THEN `{c}` END),"
                       f" MIN(`{c}`)) AS `{c}`")
        else:
            sel.append(f"MIN(`{c}`) AS `{c}`")

    if hist:
        where = "biz_date = %s AND shift = %s AND `line` = %s"
        params = [biz_date, shift, line]
    else:
        where = "snapshot_at = %s AND `line` = %s"
        params = [snap, line]
    cond = ""
    if types:
        cond = " AND lot_type IN (%s)" % ",".join(["%s"] * len(types))
        params += list(types)
    # lot 단위 속성(제품구분/상태)은 SQL 에서 거른다.
    # 파이썬으로 전부 가져와 거르면 드릴다운마다 전 재공을 훑게 된다.
    for col, vals in (extra or {}).items():
        if not vals or col.lower() not in have_l:
            continue
        vals = list(vals)
        cond += " AND `%s` IN (%s)" % (col, ",".join(["%s"] * len(vals)))
        params += vals

    with connection.cursor() as cur:
        cur.execute(f"""
            SELECT lot_id, {', '.join(sel)}, COUNT(*) AS `_steps`
            FROM   {table}
            WHERE  {where} {cond}
            GROUP  BY lot_id, `line`
        """, params)
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]

    if len(_ROWS_CACHE) > 40:          # 스냅샷이 바뀌면 낡은 것은 버린다
        _ROWS_CACHE.clear()
    _ROWS_CACHE[ckey] = (snap, rows)
    return rows, snap


_MV_CACHE = {}


def _lot_move_map(line=None):
    """당일 lot 별 MOVE (WT 계산용).

    한 스냅샷 안에서는 값이 바뀌지 않는다. 매 요청 다시 읽지 않고 캐시한다.

    lot_id 로만 연결한다. MOVE 의 라인 구분(sys_line_id)은 f3 의 라인 분류와
    체계가 달라(예: KFR7 원천이 NRD-K / NRD 로 갈린다) 그대로 쓰면 재분류된
    lot 의 MOVE 를 놓친다. lot_id 는 라인과 무관하게 유일하므로 조건을 뺀다.
    """
    if not _table_exists("f3_move_lot"):
        return {}
    with connection.cursor() as cur:
        cur.execute("SELECT MAX(biz_date) FROM f3_move_lot")
        bd = cur.fetchone()[0]
        if not bd:
            return {}
        hit = _MV_CACHE.get("bd")
        if hit == bd and "map" in _MV_CACHE:
            return _MV_CACHE["map"]
        cur.execute("SELECT lot_id, SUM(move_qty) FROM f3_move_lot "
                    "WHERE biz_date=%s GROUP BY lot_id", [bd])
        mv = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
        _MV_CACHE.clear()
        _MV_CACHE.update({"bd": bd, "map": mv})
        return mv


def _bucket(d, key, qty, eld=None):
    c = d.setdefault(key, {"lots": 0, "qty": 0, "eld": 0.0, "eln": 0})
    c["lots"] += 1
    c["qty"] += qty
    if eld is not None:
        c["eld"] += eld          # 경과일 합
        c["eln"] += 1            # 값이 있는 lot 수


def classify_lot(r, rules, ht=None):
    """대분류 / 중분류 / 소분류.

    소분류는 기준정보(f3_std_holdtype)로 정한 유형을 우선 쓴다.
    규칙이 없으면 예전처럼 사유 원문을 그대로 쓴다.
    """
    return _classify_lot(r, rules, ht)


def _eqp_ids(r):
    """이 lot 이 걸린 실제 설비 ID 목록.

    설비그룹(eqpgroup)은 묶음 이름이고, 챔버까지 있는 eqpgroup_cham 이
    설비 단위다. 둘 다 없으면 빈 목록.
    """
    out = []
    for col in ("eqpgroup_cham", "eqpgroup"):
        txt = str(r.get(col) or "")
        if not txt:
            continue
        out = [x.strip() for x in txt.split(",") if x.strip()]
        if col == "eqpgroup_cham" and out:
            return out
    return out


def _blocked_eqps(r):
    """설비상태 / TIP 문구에서 막힌 설비·챔버 이름을 뽑는다.

    'DOWN: TBO403-1(0.1일↑)' / 'PREVENT: TBO403-1(6.8일↑)' 형태다.
    """
    out = set()
    for col in ("down", "tip"):
        txt = str(r.get(col) or "")
        if not txt:
            continue
        for part in txt.split(","):
            part = part.split(":", 1)[-1]        # 'DOWN: ' 앞부분 제거
            part = part.split("(", 1)[0]         # 경과일 괄호 제거
            name = part.strip()
            if name:
                out.add(name)
    return out


def _classify_lot(r, rules, ht=None):
    """LOT 1건 -> (대분류, 중분류, 소분류후보리스트).

    소분류가 설비 단위인 경우 lot 이 여러 설비에 걸릴 수 있어 리스트로 준다.
    api_summary 와 api_lots 가 같은 함수를 써야 화면과 드릴다운이 어긋나지 않는다.
    """
    st = r.get("lot_status") or "-"
    eqps = [x.strip() for x in str(r.get("eqpgroup") or "").split(",") if x.strip()]
    # 챔버 단위까지 본다. 일부만 막혔는지 판단하려면 후보 전체를 알아야 한다.
    chams = [x.strip() for x in str(r.get("eqpgroup_cham") or "").split(",")
             if x.strip()]
    cand = chams or eqps

    # 원인은 상태의 하위 분류다. 상태를 먼저 보고 그 안에서 나눈다.
    # (예전에는 hold 플래그만 보고 Hold 로 보내, 상태와 원인이 어긋났다)
    if st == "HOLD":
        return ("Hold", None,
                [_sub_name(r, ht, "HOLD", "hold_reason", rules, "hold")])

    # 가상스텝 판정. 설비그룹 / recipe(ppid) / step 명 중 하나라도 WAIT 이면
    # 실제 설비를 기다리는 게 아니므로 Bottleneck 이 아니다.
    virtual = "WAIT" in " ".join(
        str(r.get(k) or "") for k in
        ("eqpgroup", "eqpgroup_cham", "recipe_id", "ppid",
         "step_seq", "step_desc")).upper()

    if st == "WAIT(진행불가)":
        if r.get("exception"):
            return ("Wait성 진행불가", "예약제외",
                    [_sub_name(r, ht, "예약제외", "exception_reason",
                               rules, "exception")])
        if r.get("ftp"):
            return ("Wait성 진행불가", "FTP",
                    [_sub_name(r, ht, "FTP", "ftp_reason", rules, "ftp")])
        # 진행불가는 갈 길이 없다는 뜻이다. 호환설비이슈로 보내지 않는다.
        if _eqp_status_of(r.get("down")):
            return ("Wait성 진행불가", "설비이슈", eqps or ["(설비미상)"])
        if r.get("tip"):
            return ("Wait성 진행불가", "TIP", eqps or ["(설비미상)"])
        if virtual:
            return ("Wait성 진행불가", "가상스텝 대기", ["가상스텝"])
        return ("Wait성 진행불가", "기타", ["미분류"])

    if st == "WAIT":
        if virtual:
            return ("Wait성 진행불가", "가상스텝 대기", ["가상스텝"])
        # 갈 수 있는 설비가 남아 있는 상태다(WAIT).
        # 중분류는 AREA(공정 구역), 소분류는 설비명으로 본다.
        area = (r.get("AREA") or "").strip() or UNCLASSIFIED
        blocked = _blocked_eqps(r)
        if blocked and cand and not set(cand) <= blocked:
            # 후보 중 일부만 막힌 경우. 문제가 된 설비만 소분류로 둔다.
            return ("Bottleneck", area, sorted(blocked))
        return ("Bottleneck", area, eqps or ["(설비미상)"])

    return (None, None, [])


def _add(node, lots, qty, eld=None, ln=None):
    node["lots"] += lots
    node["qty"] += qty
    if eld is not None:
        node["eld"] = node.get("eld", 0.0) + eld
        node["eln"] = node.get("eln", 0) + 1
    if ln:                       # 라인별 분해(라인 ALL 일 때 누적막대로 쓴다)
        c = node.setdefault("ln", {}).setdefault(ln, {"lots": 0.0, "qty": 0.0})
        c["lots"] += lots
        c["qty"] += qty


def api_summary(request):
    """FAB현황 좌측 summary 뷰 한 벌."""
    line = request.GET.get("line") or DEFAULT_LINE
    raw = request.GET.get("types")
    types = ([t for t in raw.split(",") if t] if raw is not None
             else list(DEFAULT_LOT_TYPES))
    prod1 = [x for x in request.GET.get("prod1", "").split(",") if x]
    prod2 = [x for x in request.GET.get("prod2", "").split(",") if x]
    areas = [x for x in request.GET.get("area", "").split(",") if x]
    # 좌측에서 막대/조각을 고르면 원인 분석만 그 부분집합으로 다시 센다.
    # 같은 필드를 여러 개 고를 수 있으므로(Ctrl) 반드시 목록으로 받는다.
    def flist(name):
        return [x for x in request.GET.get(name, "").split(",") if x]

    f_status = flist("f_status")
    f_wt = flist("f_wt")
    f_wt0 = flist("f_wt0")
    f_type = flist("f_type")
    f_area = flist("f_area")          # AREA 별 재공 차트에서
    bdate = request.GET.get("biz_date", "")
    bshift = request.GET.get("shift", "")

    if not _table_exists("f3_live"):
        return JsonResponse({"ready": False, "reason": "f3_live 미적재"})

    rows, snap = _summary_rows(line, types, biz_date=bdate, shift=bshift)
    if not rows:
        return JsonResponse({"ready": False, "reason": "이 라인의 스냅샷이 없습니다"})

    # lot_type 목록은 필터와 무관하게 전체에서 뽑아야 토글이 유지된다
    all_rows, _ = _summary_rows(line, [])
    all_types = sort_lot_types({(r.get("lot_type") or "-") for r in all_rows})
    # 제품구분 선택지는 lot_type 필터까지만 반영한다(자기 자신은 제외해야 목록이 안 줄어든다)
    def pv(r, k):
        return r.get(k) or UNCLASSIFIED

    # 가상스텝 등 AREA 가 없는 재공도 고를 수 있어야 한다.
    a_opts = sorted({r.get("AREA") for r in rows if r.get("AREA")})
    if any(not r.get("AREA") for r in rows):
        a_opts.append(UNCLASSIFIED)
    p1_opts = sorted({pv(r, "prod1") for r in rows})
    p2_opts = sorted({pv(r, "prod2") for r in rows
                      if not prod1 or pv(r, "prod1") in prod1})
    rows = [r for r in rows
            if (not prod1 or pv(r, "prod1") in prod1)
            and (not prod2 or pv(r, "prod2") in prod2)
            and (not areas or (r.get("AREA") or UNCLASSIFIED) in areas)]
    if not rows:
        return JsonResponse({"ready": False, "reason": "선택한 제품구분에 해당하는 재공이 없습니다",
                             "prod1_options": p1_opts, "prod2_options": p2_opts,
                             "area_options": a_opts},
                            json_dumps_params={"ensure_ascii": False})

    mv = _lot_move_map(line)
    rules = _cause_rules()
    ht = _holdtype_rules()          # 기준정보 유형이 소분류를 대체한다

    # 원인 분류는 lot 마다 같은 결과다. 한 스냅샷 안에서 재사용한다.
    ckey = (snap, id(rules), id(ht))
    if _CLS_CACHE.get("key") != ckey:
        _CLS_CACHE.clear()
        _CLS_CACHE["key"] = ckey
        _CLS_CACHE["map"] = {}
    cls = _CLS_CACHE["map"]

    tot = {"lots": 0, "qty": 0}
    by_type, by_status, by_wt, by_wt0 = {}, {}, {}, {}
    wt0_st, wt0_tot = {}, {}      # W/T 0 재공의 status 분해
    tree = {}

    xf = _xfilters(request)
    # 좌측 차트와 원인 분석도 드릴다운 조건을 그대로 따라야 한다.
    # (예전에는 f_status 만 봐서, 원인/LAYER 로 좁혀도 값이 그대로였다)
    want_layer = [x for x in request.GET.get("layer_id", "").split(",") if x]
    # causes = 대분류>중분류>소분류>설비ID  (뒤는 비어도 된다)
    or_causes = []
    for spec in request.GET.get("causes", "").split("|"):
        if not spec:
            continue
        parts = (spec.split(">") + ["", "", "", ""])[:4]
        or_causes.append(tuple(x.strip() for x in parts))
    c_big = request.GET.get("big", "")
    c_mid = request.GET.get("mid", "")
    c_sub = [x for x in request.GET.get("sub", "").split(",") if x]
    # 원인 조건을 어디서 걸었는지. 'cause' 면 원인 차트 자신이라 그 차트는 뺀다.
    cause_src = request.GET.get("cause_src", "cause")

    def _cause_ok(r):
        """원인 조건에 맞는지. 조건이 없으면 늘 참."""
        if not (or_causes or c_big or c_mid or c_sub):
            return True
        got = cls.get(r["lot_id"])
        if got is None:
            got = classify_lot(r, rules, ht)
            cls[r["lot_id"]] = got
        b2, m2, s2 = got
        if or_causes:
            ids = set(_eqp_ids(r))
            return any((not a or b2 == a)
                       and (not b or (m2 or "") == b)
                       and (not c or c in s2)
                       and (not e or e in ids)
                       for a, b, c, e in or_causes)
        if c_big and b2 != c_big:
            return False
        if c_mid and (m2 or "") != c_mid:
            return False
        if c_sub and not any(x in s2 for x in c_sub):
            return False
        return True

    # 요약카드는 최상단 필터까지만 반영한다. 카드에서 고른 조건(x_*)이
    # 카드 목록 자체를 바꾸면 다음 행을 고를 수 없다.
    ins_rows = []          # 요약카드용. 루프에서 채운다.
    by_area = {}           # AREA -> {status -> 매수}
    wt_line = {}           # W/T 구간 -> {라인 -> 매수}
    type_prod = {}         # 제품 -> {lot_type -> 카운터}  재공막대 펼치기용
    area_ln = {}           # AREA -> {status -> {라인 -> 매수}}
    wt0_ln = {}            # W/T0 구간 -> {status -> {라인 -> 매수}}
    st_ln = {}             # status -> {라인 -> 매수}
    by_line = {}           # LINE -> 매수 (라인 ALL 일 때 쓴다)

    # 차트마다 **자기 필드를 뺀 나머지 조건**으로 센다.
    #   원차트에서 HOLD 를 고르면 재공막대 · W/T · W/T 0 은 HOLD 로 좁혀지고,
    #   원차트 자신은 다른 status 도 계속 보여 다시 고를 수 있다.
    for r in rows:
        # AREA 는 최상단 필터(레벨 1)라 무엇을 그리든 먼저 걸린다.
        if areas and (r.get("AREA") or UNCLASSIFIED) not in areas:
            continue

        q = num(r.get("qty"))
        st = r.get("lot_status") or "-"
        ty = r.get("lot_type") or "-"
        wt = calc_wt(mv, r["lot_id"], q, r.get("lot_status"))
        wk = _wt_range_key(wt)
        b0 = _wt0_bin(num_f(r.get("마지막작업경과_일"))) if wt <= 0 else None

        ok_x = _xrow_ok(r, xf)
        ok_layer = (not want_layer
                    or str(r.get("layer_id") or "").strip() in want_layer)
        ok_cause = _cause_ok(r)
        ok_st = (not f_status) or (st in f_status)
        ok_ty = (not f_type) or (ty in f_type)
        ok_wt = (not f_wt) or (wk in f_wt)
        ok_w0 = (not f_wt0) or (wt <= 0 and b0 in f_wt0)
        ar = str(r.get("AREA") or "").strip() or UNCLASSIFIED
        ok_ar = (not f_area) or (ar in f_area)



        # 요약카드는 카드에서 고른 조건(x_*)만 무시한다.
        if (ok_layer and ok_cause and ok_st and ok_ty and ok_wt and ok_w0
                and ok_ar):
            ins_rows.append(r)

        # 원인 분석과 LOT BALANCE 도 '자기 조건' 은 빼고 센다.
        #   Hold > FLOW금지 를 골라도 Hold 차트에는 다른 사유가 남아야
        #   Ctrl 로 더 고를 수 있다.
        if not ok_x:
            continue
        base = ok_layer and ok_cause

        # 각 차트는 **자기 축을 뺀** 조건으로 센다.
        #   그래야 축 요소가 사라지지 않아 Ctrl 로 더 고를 수 있다.
        #   고른 것은 화면에서 테두리로, 나머지는 흐리게 나타낸다.
        if base and ok_ty and ok_wt and ok_w0 and ok_ar:
            _bucket(by_status, st, q)
            _bucket(st_ln.setdefault(st, {}), str(r.get("line") or "-"), q)
        if base and ok_st and ok_wt and ok_w0 and ok_ar:
            _bucket(by_type, ty, q)
            _bucket(type_prod.setdefault(
                str(r.get("prod2") or "").strip() or UNCLASSIFIED, {}), ty, q)
        if base and ok_st and ok_ty and ok_w0 and ok_ar:
            _bucket(by_wt, wk, q)
            _bucket(wt_line.setdefault(wk, {}),
                    str(r.get("line") or "-"), q)
        if base and ok_st and ok_ty and ok_wt and ok_w0:
            _bucket(by_area.setdefault(ar, {}), st, q)
            _bucket(area_ln.setdefault(ar, {}).setdefault(st, {}),
                    str(r.get("line") or "-"), q)
            if ok_ar:
                _bucket(by_line.setdefault(
                    str(r.get("line") or "-"), {}), st, q)
        if base and ok_st and ok_ty and ok_wt and ok_ar and wt <= 0:
            _bucket(by_wt0, b0, q)
            if b0:
                _bucket(wt0_st.setdefault(b0, {}), st, q)
                _bucket(wt0_ln.setdefault(b0, {}).setdefault(st, {}),
                        str(r.get("line") or "-"), q)
            _bucket(wt0_tot, st, q)

        # 총계는 모든 조건을 반영한다.
        if base and ok_st and ok_ty and ok_wt and ok_w0 and ok_ar:
            tot["lots"] += 1
            tot["qty"] += q

        # 원인 분석은 **원인 차트에서 건 조건만** 뺀다.
        #   요약카드나 다른 곳에서 건 원인 조건은 그대로 반영해야 한다
        #   (B/N 행을 눌렀는데 예약제외가 남으면 안 된다).
        if not (ok_layer and ok_st and ok_ty and ok_wt and ok_w0 and ok_ar):
            continue
        if cause_src != "cause" and not ok_cause:
            continue
        got = cls.get(r["lot_id"])
        if got is None:
            got = classify_lot(r, rules, ht)
            cls[r["lot_id"]] = got
        big, mid, subs = got
        if not big:
            continue
        eld = num_f(r.get("마지막이벤트경과_일"))
        w = 1.0 / len(subs) if subs else 0.0      # 설비 여러 개면 지분으로 나눔
        g = tree.setdefault(big, {"lots": 0.0, "qty": 0.0, "eld": 0.0, "eln": 0,
                                  "mid": {}})
        lnm = str(r.get("line") or "-")
        _add(g, 1, q, eld, lnm)
        mkey = mid or "_"
        m = g["mid"].setdefault(mkey, {"lots": 0.0, "qty": 0.0, "eld": 0.0,
                                       "eln": 0, "sub": {}})
        _add(m, 1, q, eld, lnm)
        # 설비 단위 소분류는 그 아래에 실제 설비 ID 를 한 겹 더 둔다.
        eqp_ids = _eqp_ids(r) if big in ("Bottleneck", "Wait성 진행불가") else []
        for sname in subs:
            sc = m["sub"].setdefault(sname, {"lots": 0.0, "qty": 0.0,
                                             "eld": 0.0, "eln": 0, "eqp": {}})
            sc.setdefault("eqp", {})
            _add(sc, w, q * w, eld, lnm)
            # 소분류 이름이 곧 설비그룹인 경우, 그 그룹의 설비 ID 로 나눈다.
            ids = [x for x in eqp_ids if x.startswith(sname)] or eqp_ids
            if ids and sname != "(설비미상)":
                w2 = w / len(ids)
                for eid in ids:
                    ec = sc["eqp"].setdefault(eid, {"lots": 0.0, "qty": 0.0,
                                                    "eld": 0.0, "eln": 0})
                    _add(ec, w2, q * w2, eld, lnm)

    def node(name, v, extra=None):
        n = v.get("eln") or 0
        d = {"name": name, "lots": round(v["lots"], 1), "qty": int(round(v["qty"])),
             # 경과일 평균(일/lot). 라인차트에 쓴다.
             "elapsed_d": (round(v.get("eld", 0.0) / n, 1) if n else None)}
        if v.get("ln"):                    # 라인 ALL 일 때 누적막대로 쓴다
            d["by_line"] = {a: {"qty": int(round(c["qty"])),
                                "lots": round(c["lots"], 1)}
                            for a, c in v["ln"].items()}
        if extra:
            d.update(extra)
        return d

    def subs_of(m, limit=SUB_MAX):
        """소분류 목록. 화면이 상위 몇 개만 그릴지는 클라이언트가 정한다.

        설비 ID 가 둘 이상이면 그 아래 단계(children)로 붙여 더 파고들게 한다.
        """
        items = sorted(m["sub"].items(), key=lambda kv: (-kv[1]["qty"], kv[0]))
        out = []
        for k, v in items[:limit]:
            extra = {"kind": "sub"}
            eq = v.get("eqp") or {}
            if len(eq) > 1:
                eqs = sorted(eq.items(), key=lambda kv: (-kv[1]["qty"], kv[0]))
                extra["children"] = [node(a, b, {"kind": "eqp"})
                                     for a, b in eqs[:limit]]
            out.append(node(k, v, extra))
        return out

    causes = []
    # 'Wait' 는 갈 수 있는 설비가 남아 있는 경우다(호환설비이슈).
    # 원인 분석 차트는 3칸이라 Bottleneck 쪽에 함께 담는다.
    for big in ("Hold", "Wait성 진행불가", "Bottleneck"):
        g = tree.get(big)
        if not g:
            causes.append({"name": big, "lots": 0, "qty": 0, "children": []})
            continue
        children = []
        for mkey, m in sorted(g["mid"].items(), key=lambda kv: -kv[1]["qty"]):
            if mkey == "_":
                # 중분류가 없는 대분류(Hold / Bottleneck)는 소분류가 바로 자식이다.
                # kind 로 구분해 두지 않으면 드릴다운 필터가 mid/sub 를 혼동한다.
                children = subs_of(m)
            else:
                children.append(node(mkey, m, {"kind": "mid",
                                               "children": subs_of(m)}))
        total_child = (sum(len(m["sub"]) for m in g["mid"].values())
                       if list(g["mid"]) == ["_"] else len(g["mid"]))
        causes.append(node(big, g, {"children": children,
                                    "sub_total": total_child,
                                    "shown": len(children)}))

    # 원차트는 자기 조건을 뺀 집계라 tot(선택분)과 분모가 다르다.
    # 제 합계로 나눠야 100% 가 되지 않는다.
    st_tot = sum(v.get("lots", 0) for v in by_status.values())
    st_lines = _ln_map(st_ln)
    status = [{"name": s, "color": STATUS_COLORS[s],
               "lots": by_status.get(s, {}).get("lots", 0),
               "qty": by_status.get(s, {}).get("qty", 0),
               "by_line": st_lines.get(s, {}),
               "pct": round(by_status.get(s, {}).get("lots", 0)
                            / st_tot * 100, 1) if st_tot else 0}
              for s in STATUS_ORDER]
    blocked = [x for x in status if x["name"] in ("HOLD", "WAIT(진행불가)")]

    return JsonResponse({
        "ready": True, "line": line,
        "snapshot_at": (snap.strftime("%Y-%m-%d %H:%M")
                        if hasattr(snap, "strftime") else str(snap)),
        "types": all_types, "selected_types": types,
        "snapshots": _hist_snapshots(line),
        "biz_date": bdate, "shift": bshift,
        "prod1_options": p1_opts, "prod2_options": p2_opts,
        "area_options": a_opts,
        "selected_prod1": prod1, "selected_prod2": prod2,
        "total": tot,
        "by_lot_type": [{"name": k, **by_type[k]}
                        for k in sorted(by_type, key=lot_type_key)],
        # 재공막대 '제품별 비율 보기'. 재공이 많은 제품이 위로 온다.
        "type_by_prod": [
            {"name": p,
             "lots": int(sum(c["lots"] for c in v.values())),
             "qty": int(sum(c["qty"] for c in v.values())),
             "types": [{"name": k, "lots": int(v[k]["lots"]),
                        "qty": int(v[k]["qty"])}
                       for k in sorted(v, key=lot_type_key)]}
            for p, v in sorted(
                type_prod.items(),
                key=lambda kv: -sum(c["qty"] for c in kv[1].values()))],
        "status": status,
        # 원 우측에 보여 줄 기본값. 범례 항목이 아니라 표시용이다.
        "default": (lambda h: {
            "label": "HOLD",
            "pct": round(h["lots"] / tot["lots"] * 100, 1) if tot["lots"] else 0,
            "lots": h["lots"], "qty": h["qty"],
            "color": STATUS_COLORS["HOLD"],
        })(next((x for x in status if x["name"] == "HOLD"),
                {"lots": 0, "qty": 0})),
        "insights": _build_insights(
            line, types, ins_rows, mv, rules, ht, cls,
            {"qty": sum(num(x.get("qty")) for x in ins_rows)}),
        # AREA 별 재공(status 누적). x축은 매수 순.
        "area_dist": [dict(x, by_line_status=_ln_map(area_ln.get(x["name"])))
                      for x in _dist_rows(by_area)],
        "line_dist": _dist_rows(by_line),
        "wt0_bins": WT0_BINS,
        "wt0_dist": [{"name": b["key"], "label": b["label"],
                      **by_wt0.get(b["key"], {"lots": 0, "qty": 0}),
                      "by_status": {k: {"lots": v["lots"], "qty": v["qty"]}
                                    for k, v in wt0_st.get(b["key"], {}).items()},
                      "by_line_status": _ln_map(wt0_ln.get(b["key"]))}
                     for b in WT0_BINS],
        # 누적막대 계열 순서와 색. 원차트와 같은 색을 쓴다.
        "wt0_status": [{"name": k, "color": STATUS_COLORS.get(k, "#9CA3AF"),
                        "lots": v["lots"], "qty": v["qty"]}
                       for k, v in sorted(
                           wt0_tot.items(),
                           key=lambda kv: STATUS_ORDER.index(kv[0])
                           if kv[0] in STATUS_ORDER else 99)],
        "wt_ranges": WT_RANGES,
        "wt_dist": [{"name": r["key"], "label": r["label"],
                     **by_wt.get(r["key"], {"lots": 0, "qty": 0}),
                     "by_line": {a: {"qty": int(c["qty"]),
                                     "lots": int(c["lots"])}
                                 for a, c in wt_line.get(r["key"], {}).items()}}
                    for r in WT_RANGES],
        "causes": causes,
    }, json_dumps_params={"ensure_ascii": False})


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------


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
                  base_ctx(lines=lines, default_line=DEFAULT_LINE))


def fab_metrics(request):
    """FAB지표. 메뉴에는 노출하지 않지만 URL 로는 접근 가능하다.
    Shift 별 MOVE 와 추이 그래프가 이쪽으로 옮겨왔다."""
    panels = [dict(p, px=int(round(p["h"] * PANEL_BASE_PX)) + PANEL_AXIS_PX)
              for p in PANELS]
    return render(request, "flowmonitor/fab_metrics.html",
                  base_ctx(panels=panels,
                           lines=[{"label": c["label"], "line": c["line"]}
                                  for c in LINE_CARDS]))


# 기준정보 카드 정의. 컬럼을 여기서만 관리하면 화면/저장이 함께 따라간다.
STD_CARDS = [
    {"key": "module", "table": "f3_std_module", "title": "모듈설정",
     "desc": "조건에 맞는 LOT 에 MODULE1 / MODULE2 를 부여한다. "
             "빈 칸은 와일드카드이고, 더 구체적으로 지정된 행이 우선한다.",
     "cols": [
         {"k": "line", "t": "LINE", "type": "select", "opts": "lines", "w": "sm"},
         {"k": "proc_id", "t": "PROC_ID", "w": "md"},
         {"k": "start_layer", "t": "START_LAYER", "w": "sm"},
         {"k": "end_layer", "t": "END_LAYER", "w": "sm"},
         {"k": "start_stepseq", "t": "START_STEPSEQ", "w": "md"},
         {"k": "end_stepseq", "t": "END_STEPSEQ", "w": "md"},
         {"k": "module1", "t": "MODULE1", "req": True, "w": "md"},
         {"k": "module2", "t": "MODULE2", "w": "md"},
     ]},
    {"key": "holdtype", "table": "f3_std_holdtype", "title": "HOLD 유형설정",
     "sortable": True,          # 행을 끌어 적용 순서를 바꾼다
     "desc": "각 유형의 사유 컬럼에 CONDITION 이 모두 포함되면 TYPE_NAME 으로 "
             "분류한다. 대소문자는 구분하지 않는다. "
             "여러 개가 걸리면 **위에 있는 행**이 먼저다. "
             "행 왼쪽 손잡이를 끌어 순서를 바꾼다. "
             "한 행 안에서 동시에 걸리면 HOLD > FTP > 예약제외 순으로 하나만 쓴다.",
     "cols": [
         {"k": "line", "t": "LINE", "type": "select", "opts": "lines", "w": "sm"},
         {"k": "type", "t": "TYPE", "type": "select", "w": "sm",
          "opts": ["ALL", "HOLD", "FTP", "예약제외"], "noblank": True,
          "default": "ALL"},
         {"k": "condition1", "t": "CONDITION1", "w": "lg"},
         {"k": "condition2", "t": "CONDITION2", "w": "lg"},
         {"k": "condition3", "t": "CONDITION3", "w": "lg"},
         {"k": "type_name", "t": "TYPE_NAME", "req": True, "w": "md"},
     ]},
    {"key": "hot", "table": "f3_std_hot", "title": "초HOT 기준설정",
     "desc": "GRADE 와 CONDITION 으로 초HOT 유형을 정한다. "
             "빈 칸은 와일드카드다.",
     "cols": [
         {"k": "line", "t": "LINE", "type": "select", "opts": "lines", "w": "sm"},
         {"k": "grade", "t": "GRADE", "type": "select", "w": "sm",
          "opts": ["G1", "G2", "G3", "G4", "G5"]},
         {"k": "condition_1", "t": "CONDITION_1", "w": "lg"},
         {"k": "condition_2", "t": "CONDITION_2", "w": "lg"},
         {"k": "condition_3", "t": "CONDITION_3", "w": "lg"},
         {"k": "type_name", "t": "TYPE_NAME", "req": True, "w": "md"},
     ]},
    {"key": "plan", "table": "f3_std_plan", "title": "제품별 메인 PLAN",
     "desc": "메인체크가 켜진 PLAN 이 그 제품의 메인이다. "
             "여러 개면 PLAN 명 오름차순이 우선한다. "
             "GROUP 이 같은 숫자면 같은 그룹으로 묶인다. "
             "PLAN 은 f3 의 PROC_ID 와 같은 값이다.",
     "cols": [
         {"k": "line", "t": "LINE", "type": "select", "opts": "lines", "w": "sm"},
         {"k": "prod2", "t": "제품", "type": "search", "opts": "prod2", "w": "md"},
         {"k": "plan", "t": "PLAN(PROC_ID)", "w": "md"},
         {"k": "grp", "t": "GROUP", "type": "number", "w": "sm"},
         {"k": "is_main", "t": "메인체크", "type": "check", "w": "sm"},
     ]},
]


LOT_TYPE_ORDER = ["PP", "PB", "PG", "EG", "EE", "HH"]


def sort_lot_types(vals):
    """lot_type 은 업무 순서가 있다. 나머지는 이름순."""
    def key(v):
        u = str(v).upper()
        return (LOT_TYPE_ORDER.index(u) if u in LOT_TYPE_ORDER
                else len(LOT_TYPE_ORDER), str(v))
    return sorted(vals, key=key)


def _prod2_options():
    """제품(PROD2) 선택지. f3_live 에 적재된 값에서 뽑는다."""
    if not _table_exists("f3_live"):
        return []
    with connection.cursor() as cur:
        cur.execute("SELECT DISTINCT prod2 FROM f3_live "
                    "WHERE prod2 IS NOT NULL AND prod2 <> '' ORDER BY prod2")
        return [r[0] for r in cur.fetchall()]


def _std_rows(table, cols):
    if not _table_exists(table):
        return []
    names = ", ".join(f"`{c['k']}`" for c in cols)
    # 적용 순서가 있는 표(HOLD 유형)는 그 순서대로 보여 준다.
    has_sort = "sort_no" in _columns_of(table)
    order = "sort_no IS NULL, sort_no, id" if has_sort else "id"
    with connection.cursor() as cur:
        cur.execute(f"SELECT id, {names} FROM `{table}` ORDER BY {order}")
        keys = ["id"] + [c["k"] for c in cols]
        return [dict(zip(keys, r)) for r in cur.fetchall()]


def _std_save(card, payload):
    """행 목록을 통째로 교체한다.

    같은 내용이 여러 번 들어와도 결과가 같아야 하므로 중복은 걸러 저장한다.
    """
    cols = [c["k"] for c in card["cols"]]
    req = [c["k"] for c in card["cols"] if c.get("req")]

    clean, seen = [], set()
    for row in payload:
        v = {k: (str(row.get(k) or "").strip() or None) for k in cols}
        for k in ("condition1", "condition2", "condition3"):
            if v.get(k):                     # 대소문자 구분 없이 걸리게
                v[k] = v[k].upper()
        for c in card["cols"]:                         # 비면 기본값으로 채운다
            if c.get("default") and not v.get(c["k"]):
                v[c["k"]] = c["default"]
        if any(not v.get(k) for k in req):
            continue                                   # 필수값 없으면 버린다
        if "module1" in cols and not v.get("module2"):
            v["module2"] = v["module1"]                # 비우면 module1 과 동일
        sig = tuple(v[k] for k in cols)
        if sig in seen:
            continue                                   # 완전히 같은 행은 하나만
        seen.add(sig)
        clean.append(v)

    # 적용 순서가 있는 표는 **화면에 보이는 순서 그대로** 번호를 매긴다.
    # 사용자가 끌어 올린 순서가 곧 우선순위다. 자동으로 다시 섞지 않는다.
    has_sort = "sort_no" in _columns_of(card["table"])

    ph = ", ".join(["%s"] * len(cols))
    names = ", ".join(f"`{k}`" for k in cols)
    with connection.cursor() as cur:
        cur.execute(f"DELETE FROM `{card['table']}`")
        if clean:
            now = dt.datetime.now()
            if has_sort:
                cur.executemany(
                    f"INSERT INTO `{card['table']}` ({names}, sort_no, "
                    f"updated_at) VALUES ({ph}, %s, %s)",
                    [tuple(v[k] for k in cols) + ((i + 1) * 10, now)
                     for i, v in enumerate(clean)])
            else:
                cur.executemany(
                    f"INSERT INTO `{card['table']}` ({names}, updated_at) "
                    f"VALUES ({ph}, %s)",
                    [tuple(v[k] for k in cols) + (now,) for v in clean])
    return len(clean)


def _apply_standards():
    """기준정보를 f3_live 에 즉시 반영한다.

    평상시에는 build_f3 가 미리 계산해 두지만, 규칙을 방금 고쳤을 때
    다음 배치까지 기다리지 않도록 여기서 다시 채운다.
    지금은 HOLD 유형(cause_detail)만 대상이다. 모듈은 layer/step 범위 비교라
    SQL 로 옮기기 어려워 다음 배치에서 반영된다.
    """
    if not _table_exists("f3_live"):
        return 0, 0
    ht = _holdtype_rules()
    with connection.cursor() as cur:
        cur.execute("SELECT MAX(snapshot_at) FROM f3_live")
        row = cur.fetchone()
        snap = row[0] if row else None
        if not snap:
            return 0, 0
        cur.execute(
            "SELECT lot_id, `line`, hold, hold_reason, ftp, ftp_reason,"
            " exception, exception_reason FROM f3_live WHERE snapshot_at=%s",
            [snap])
        names = [d[0] for d in cur.description]
        recs = [dict(zip(names, r)) for r in cur.fetchall()]

        upd, seen = [], {}
        for r in recs:
            v = holdtype_of(r, ht) if ht else None
            if seen.get(r["lot_id"]) == v:
                continue
            seen[r["lot_id"]] = v
            upd.append((v, snap, r["lot_id"]))
        if upd:
            cur.executemany(
                "UPDATE f3_live SET cause_detail=%s "
                "WHERE snapshot_at=%s AND lot_id=%s", upd)
        hit = sum(1 for v in seen.values() if v)
    return hit, len(seen)


@csrf_exempt
@ensure_csrf_cookie
def standards(request):
    """기준정보. 카드별로 행 추가/삭제/저장/검색을 한다.

    csrf_exempt: 사내망 전용 도구이고, 브라우저/프록시 환경에 따라 csrftoken
    쿠키가 잡히지 않아 저장이 403 으로 막히는 일이 반복됐다. 외부 노출이 없는
    화면이라 이 뷰에 한해 검사를 끈다. (다른 뷰는 그대로 보호된다)
    ensure_csrf_cookie 는 정상 환경에서 쿠키를 계속 내려보내기 위해 남겨 둔다.
    """
    msg = ""
    if request.method == "POST" and request.POST.get("card") == "__apply__":
        # 카드별이 아니라 기준정보 전체를 현재 스냅샷에 반영한다.
        hit, tot = _apply_standards()
        msg = f"기준정보를 f3_live 에 반영했습니다. 원인 유형 {hit:,}/{tot:,} LOT"
    elif request.method == "POST":
        key = request.POST.get("card")
        card = next((c for c in STD_CARDS if c["key"] == key), None)
        if card:
            try:
                payload = json.loads(request.POST.get("rows") or "[]")
            except ValueError:
                payload = []
            if not _table_exists(card["table"]):
                msg = (f"{card['table']} 테이블이 없습니다. "
                       f"python getdata/db_common.py --init 을 먼저 실행하세요.")
            else:
                n = _std_save(card, payload)
                msg = f"{card['title']}: {n}행 저장했습니다."

    lines = [c["line"] for c in LINE_CARDS]
    prod2 = _prod2_options()
    cards = []
    for c in STD_CARDS:
        cols = []
        for col in c["cols"]:
            col = dict(col)
            if col.get("opts") == "lines":
                col["opts"] = lines
            elif col.get("opts") == "prod2":
                col["opts"] = prod2
            cols.append(col)
        cards.append({**c, "cols": cols, "rows": _std_rows(c["table"], c["cols"])})

    return render(request, "flowmonitor/standards.html",
                  base_ctx(cards=cards, msg=msg,
                           cards_json=json.dumps(cards, ensure_ascii=False)))


SHIFT_SEQ = [("GY", "22:00"), ("DAY", "06:00"), ("SW", "14:00")]


def _biz_today(now=None):
    """업무일. D 는 D-1 22:00 ~ D 22:00 을 덮는다."""
    now = now or dt.datetime.now()
    d = now.date()
    return (d + dt.timedelta(days=1)) if now.hour >= 22 else d


def _shifts_started(biz_date, now=None):
    """그 업무일에서 **이미 시작된** SHIFT. 적재 여부와 무관하다."""
    now = now or dt.datetime.now()
    d = biz_date if isinstance(biz_date, dt.date) else \
        dt.datetime.strptime(str(biz_date)[:10], "%Y-%m-%d").date()
    starts = [("GY", dt.datetime.combine(d - dt.timedelta(days=1), dt.time(22))),
              ("DAY", dt.datetime.combine(d, dt.time(6))),
              ("SW", dt.datetime.combine(d, dt.time(14)))]
    return [k for k, t in starts if t <= now]


def api_trend(request):
    """SHIFT / 날짜 추이. 막대=재공, 라인=원인 비율.

    x 축은 **시계에 맞춰** 만든다. 적재가 안 된 구간도 자리를 비워 둔다.
      scope=day   그 업무일에서 이미 시작된 SHIFT + 현재
      scope=week  오늘까지 최근 7일(날짜)
    """
    line = request.GET.get("line") or DEFAULT_LINE
    raw = request.GET.get("types")
    types = ([t for t in raw.split(",") if t] if raw is not None
             else list(DEFAULT_LOT_TYPES))
    prod1 = [x for x in request.GET.get("prod1", "").split(",") if x]
    prod2 = [x for x in request.GET.get("prod2", "").split(",") if x]
    areas = [x for x in request.GET.get("area", "").split(",") if x]
    bdate = request.GET.get("biz_date", "")
    drill = [x for x in request.GET.get("drill", "").split("|") if x]
    scope = request.GET.get("scope", "day")
    wshift = request.GET.get("wshift", "GY")

    rules, ht = _cause_rules(), _holdtype_rules()
    today = _biz_today()

    points = []
    if scope == "week":
        for k in range(6, -1, -1):
            d = today - dt.timedelta(days=k)
            points.append({"key": str(d), "label": str(d)[5:],
                           "shift": wshift, "biz_date": str(d)})
    else:
        d = bdate or str(today)
        for sh in _shifts_started(d):
            at = dict(SHIFT_SEQ)[sh]
            points.append({"key": sh, "label": f"{sh}\n{at}",
                           "shift": sh, "biz_date": d})
        if str(d) == str(today):        # 오늘이면 맨 끝에 현재
            snap = _latest_snapshot()
            at = snap.strftime("%H:%M") if hasattr(snap, "strftime") else ""
            points.append({"key": "__now__", "label": f"현재\n{at}".rstrip(),
                           "shift": None})

    return _trend_points(points, line, types, prod1, prod2, drill,
                         rules, ht, scope, wshift)


def _trend_points(points, line, types, prod1, prod2, drill, rules, ht,
                  scope, wshift):
    """각 지점의 재공과 원인 비율을 센다. x축이 SHIFT 든 날짜든 같다."""
    out = []
    for p in points:
        if p["shift"]:
            rows, _ = _summary_rows(line, types,
                                    biz_date=p.get("biz_date"),
                                    shift=p["shift"])
        else:
            rows, _ = _summary_rows(line, types)
        rows = [r for r in rows
                if (not prod1 or (r.get("prod1") or UNCLASSIFIED) in prod1)
                and (not prod2 or (r.get("prod2") or UNCLASSIFIED) in prod2)]

        qty = sum(num(r.get("qty")) for r in rows)
        series = {}
        for r in rows:
            q = num(r.get("qty"))
            big, mid, subs = classify_lot(r, rules, ht)
            if not big:
                continue
            if not drill:
                if big in ("Hold", "Wait성 진행불가"):
                    series[big] = series.get(big, 0) + q
                continue

            # Hold+Wait성 은 둘을 합쳐 소분류 상위를 본다
            if drill[0] == "Hold+Wait성":
                if big not in ("Hold", "Wait성 진행불가"):
                    continue
                key = subs[0] if subs else mid
                if key:
                    series[key] = series.get(key, 0) + q
                continue

            if big != drill[0]:
                continue
            if len(drill) == 1:
                key = mid if (big == "Wait성 진행불가") else (subs[0] if subs else None)
            else:
                if mid != drill[1]:
                    continue
                key = subs[0] if subs else None
            if key:
                series[key] = series.get(key, 0) + q
        out.append({"key": p["key"], "label": p["label"],
                    "qty": qty, "series": series})

    # 라인으로 그릴 계열 (상위 5)
    tot = {}
    for x in out:
        for k, v in x["series"].items():
            tot[k] = tot.get(k, 0) + v
    order = ["Hold", "Wait성 진행불가"] if not drill else \
        [k for k, _ in sorted(tot.items(), key=lambda kv: -kv[1])[:5]]
    order = [k for k in order if k in tot]

    return JsonResponse({
        "points": out, "series": order, "drill": drill,
        # 라인 색. Hold/Wait성 은 status 색을 그대로 쓴다.
        "colors": {"Hold": STATUS_COLORS["HOLD"],
                   "Wait성 진행불가": STATUS_COLORS["WAIT(진행불가)"]},
        "scope": scope, "wshift": wshift,
    }, json_dumps_params={"ensure_ascii": False})


def api_balance(request):
    """LOT BALANCE. x=LAYER, y=재공. status 로 쌓고 PLAN/제품으로 나눈다.

    드릴다운 표와 같은 조건을 받는다(그래야 표와 그림이 맞는다).
    """
    line = request.GET.get("line") or DEFAULT_LINE
    raw = request.GET.get("types")
    types = ([t for t in raw.split(",") if t] if raw is not None
             else list(DEFAULT_LOT_TYPES))

    def multi(name):
        return [x for x in request.GET.get(name, "").split(",") if x]

    prod1, prod2 = multi("prod1"), multi("prod2")
    areas = multi("area")
    bdate = request.GET.get("biz_date", "")
    bshift = request.GET.get("shift", "")
    # 표와 같은 조건
    f_type, f_status = multi("lot_type"), multi("status")
    wt_range, wt0 = multi("wt_range"), multi("wt0")
    big = request.GET.get("big", "")
    mid = request.GET.get("mid", "")
    subs_want = multi("sub")
    or_causes = []
    for spec in request.GET.get("causes", "").split("|"):
        if not spec:
            continue
        parts = (spec.split(">") + ["", "", "", ""])[:4]
        or_causes.append(tuple(x.strip() for x in parts))
    # 서로 다른 대분류를 함께 고른 경우. AND 로는 언제나 0 이라 OR 로 본다.
    #   causes=Hold>>FLOW금지|Wait성 진행불가>설비이슈>
    or_causes = []
    for spec in request.GET.get("causes", "").split("|"):
        if not spec:
            continue
        parts = (spec.split(">") + ["", "", "", ""])[:4]
        or_causes.append(tuple(x.strip() for x in parts))

    by = [x for x in request.GET.get("by", "plan").split(",") if x] or ["plan"]
    gcols = [("prod2" if x == "prod" else "proc_id") for x in by]

    rows, _ = _summary_rows(line, types, biz_date=bdate, shift=bshift)
    mv = _lot_move_map(line)
    rules, ht = _cause_rules(), _holdtype_rules()

    layers, by_plan, total = set(), {}, {}
    grp_lay = {}          # 제품 -> 그 제품의 LAYER 집합
    grp_of = {}           # 줄 이름 -> 어느 제품에 속하는지
    prod_seen, plan_seen = set(), set()
    lay_mod = {}          # LAYER -> 모듈. x축 아래 경계 표시에 쓴다
    xf = _xfilters(request)
    f_area_b = [x for x in request.GET.get("f_area", "").split(",") if x]
    for r in rows:
        if prod1 and (r.get("prod1") or UNCLASSIFIED) not in prod1:
            continue
        if prod2 and (r.get("prod2") or UNCLASSIFIED) not in prod2:
            continue
        if areas and (r.get("AREA") or UNCLASSIFIED) not in areas:
            continue
        if not _xrow_ok(r, xf):
            continue
        if not _farea_ok(r, f_area_b):
            continue
        st = r.get("lot_status") or "-"
        if f_type and (r.get("lot_type") or "-") not in f_type:
            continue
        if f_status and st not in f_status:
            continue
        q = num(r.get("qty"))
        wt = calc_wt(mv, r["lot_id"], q, st)
        if wt_range and _wt_range_key(wt) not in wt_range:
            continue
        if wt0 and (wt > 0
                    or _wt0_bin(num_f(r.get("마지막작업경과_일"))) not in wt0):
            continue
        if big or mid or subs_want or or_causes:
            b2, m2, s2 = classify_lot(r, rules, ht)
            if or_causes:
                ids = set(_eqp_ids(r))
                if not any((not a or b2 == a)
                           and (not b or (m2 or "") == b)
                           and (not c or c in s2)
                           and (not e or e in ids)
                           for a, b, c, e in or_causes):
                    continue
            else:
                if big and b2 != big:
                    continue
                if mid and (m2 or "") != mid:
                    continue
                if subs_want and not any(x in s2 for x in subs_want):
                    continue

        lay = str(r.get("layer_id") or "").strip()
        if not lay:
            continue
        layers.add(lay)
        mod = r.get("module1")
        if mod:
            lay_mod.setdefault(lay, mod)
        _bucket(total.setdefault(lay, {}), st, q)
        key = " · ".join(str(r.get(c) or "-") for c in gcols)
        _bucket(by_plan.setdefault(key, {}).setdefault(lay, {}), st, q)
        # 제품으로 나눌 때는 **그 제품이 실제로 있는 LAYER** 만 축으로 쓴다.
        #   PLAN 까지 나눠도 축은 제품 단위로 묶는다(줄끼리 견줄 수 있게).
        if "prod" in by:
            grp_lay.setdefault(str(r.get("prod2") or "-"), set()).add(lay)
        grp_of[key] = str(r.get("prod2") or "-")
        prod_seen.add(str(r.get("prod2") or "-"))
        plan_seen.add(str(r.get("proc_id") or "-"))

    main = _main_plans(line) if by == ["plan"] else {}
    # 지금 조건에서 실제로 있는 제품·PLAN 만 목록에 올린다.
    #   제품을 고르면 그 제품의 PLAN 만, 반대도 마찬가지다.
    opt_prod = sorted({v for v in prod_seen})
    opt_plan = sorted({v for v in plan_seen})

    def rank(p):
        return (0 if p in main else 1, main.get(p, 0), p)

    xs = sorted(layers)
    sts = [x for x in STATUS_ORDER
           if any(x in total.get(l, {}) for l in xs)]

    def axis_of(key):
        """그 줄이 쓸 x축. 제품으로 나눴으면 그 제품의 LAYER 만."""
        if "prod" not in by:
            return xs
        g = grp_of.get(key)
        keep = grp_lay.get(g)
        return [l for l in xs if (not keep or l in keep)]

    def pack(src, ax):
        return {st: [int(round(src.get(l, {}).get(st, {}).get("qty", 0)))
                     for l in ax] for st in sts}

    def packl(src, ax):
        return {st: [int(round(src.get(l, {}).get(st, {}).get("lots", 0)))
                     for l in ax] for st in sts}

    # 같은 모듈이 이어지는 구간을 묶는다.
    bands, cur = [], None
    for i2, l in enumerate(xs):
        m = lay_mod.get(l)
        if cur and cur["name"] == m:
            cur["to"] = i2
        else:
            cur = {"name": m, "from": i2, "to": i2}
            bands.append(cur)
    bands = [b for b in bands if b["name"]]

    return JsonResponse({
        "layers": xs, "by": ",".join(by), "modules": bands,
        "opt_prod": opt_prod, "opt_plan": opt_plan,
        "status": [{"name": x, "color": STATUS_COLORS.get(x, "#9CA3AF")}
                   for x in sts],
        "total": {"qty": pack(total, xs), "lots": packl(total, xs)},
        "plans": [{"name": p, "main": (p in main),
                   # 줄마다 제 x축을 함께 내려보낸다(제품별 LAYER 가 다르다).
                   "layers": axis_of(p), "group": grp_of.get(p),
                   "qty": pack(by_plan[p], axis_of(p)),
                   "lots": packl(by_plan[p], axis_of(p))}
                  for p in sorted(by_plan, key=rank)],
    }, json_dumps_params={"ensure_ascii": False})


def _main_plans(line):
    """메인 PLAN -> 우선순위. 메인체크가 여럿이면 PLAN 명 오름차순."""
    if not _table_exists("f3_std_plan"):
        return {}
    with connection.cursor() as cur:
        cur.execute("SELECT plan FROM f3_std_plan "
                    "WHERE is_main='Y' AND (line=%s OR line IS NULL OR line='') "
                    "AND plan IS NOT NULL ORDER BY plan", [line])
        return {r[0]: i for i, r in enumerate(cur.fetchall())}


@csrf_exempt
def api_debug_layout(request):
    """브라우저에서 잰 레이아웃 초과 정보를 서버 로그로 찍는다.

    콘솔 복사가 어려워, 화면에서 넘치는 요소를 여기로 보내 터미널에서 본다.
    """
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = {}
    if data.get("kind") == "mark":
        print("\n" + "=" * 70, flush=True)
        print("[MARK] 선택 표시 · 화살표 (값·이름은 보내지 않는다)", flush=True)
        for it in (data.get("items") or [])[:60]:
            print("  %-30s %s" % (it.get("sel"), it.get("note")), flush=True)
        print("=" * 70 + "\n", flush=True)
        return JsonResponse({"ok": True})

    if data.get("kind") == "boot":
        print("\n" + "=" * 70, flush=True)
        print("[BOOT] 첫 화면 순서 (값·이름은 보내지 않는다)", flush=True)
        for it in (data.get("items") or [])[:40]:
            print("  %-40s %s" % (it.get("sel"), it.get("note")), flush=True)
        print("=" * 70 + "\n", flush=True)
        return JsonResponse({"ok": True})

    if data.get("kind") == "overlap":
        print("\n" + "=" * 70, flush=True)
        print("[LAYOUT] 카드 · 차트 자리 (값·이름은 보내지 않는다)", flush=True)
        for it in (data.get("items") or [])[:60]:
            print("  %-46s %s" % (it.get("sel"), it.get("note")), flush=True)
        print("=" * 70 + "\n", flush=True)
        return JsonResponse({"ok": True})

    print("\n" + "=" * 70, flush=True)
    print("[LAYOUT] viewport=%s  scrollWidth=%s  초과=%spx"
          % (data.get("vw"), data.get("sw"),
             (data.get("sw") or 0) - (data.get("vw") or 0)), flush=True)
    for it in (data.get("items") or [])[:25]:
        print("  %6spx  %-38s w=%-7s parent=%s"
              % (it.get("over"), it.get("sel"), it.get("w"),
                 it.get("parent")), flush=True)
    print("=" * 70 + "\n", flush=True)
    return JsonResponse({"ok": True})


def _ins_path(r, kind):
    """카드별 계층 경로. 카드 성격에 맞춰 다르게 판다."""
    if kind == "lot":
        # LOT 자체에 걸린 제약: 제품 > 모듈 > LAYER
        mod = str(r.get("module1") or "").strip()
        lay = str(r.get("layer_id") or "").strip()
        out = [str(r.get("prod2") or "").strip() or UNCLASSIFIED]
        if mod:
            out.append(mod)
        if lay:
            out.append(lay)
        return out
    # 설비에 걸린 제약: AREA > 설비 ID
    area = str(r.get("AREA") or "").strip() or UNCLASSIFIED
    eqps = _eqp_ids(r) or [str(r.get("eqpgroup") or "").strip()]
    return [area, (eqps[0] if eqps else UNCLASSIFIED)]


def _ins_bucket(rows, mv, rules, ht, cls=None, line=""):
    """카드 네 종류로 나누고, 각자의 경로로 5순위까지 센다."""
    from collections import defaultdict

    cards = {
        "lot":  {"title": "LOT 제약 (HOLD · 예약제외 · FTP)", "kind": "lot",
                 "items": defaultdict(lambda: _ins_new())},
        "neck": {"title": "Bottleneck (재공 몰림)", "kind": "eqp",
                 "items": defaultdict(lambda: _ins_new())},
        "eqp":  {"title": "설비 이슈 (진행 불가)", "kind": "eqp",
                 "items": defaultdict(lambda: _ins_new())},
        "tip":  {"title": "PREVENT", "kind": "eqp",
                 "items": defaultdict(lambda: _ins_new())},
    }

    for r in rows:
        got = (cls or {}).get(r["lot_id"])
        if got is None:
            got = classify_lot(r, rules, ht)
        big, mid, subs = got
        if not big:
            continue

        card, head, extra = None, None, None
        if big == "Hold":
            card, head = "lot", "HOLD"
        elif big == "Wait성 진행불가" and mid in ("예약제외", "FTP"):
            card, head = "lot", mid
        elif big == "Wait성 진행불가" and mid == "설비이슈":
            # PM / DOWN / LOCAL 같은 설비 상태를 접두로 쓴다.
            card = "eqp"
            head = _eqp_status_of(r.get("down")) or "설비이슈"
        elif big == "Wait성 진행불가" and mid == "TIP":
            card, head = "tip", "PREVENT"
        elif big == "Bottleneck":
            card, head = "neck", "B/N"
            # 호환 그룹 안에 막힌 설비가 있으면 그것 때문에 neck 이 된 것이다.
            blocked = _blocked_eqps(r)
            extra = sorted(blocked) if blocked else None
        if not card:
            continue

        # 전 라인으로 볼 때는 어느 라인 재공인지 밝힌다.
        pre = []
        if line == "*":
            pre = [str(r.get("line") or "-")]
        # 카드 안에 한 종류만 있는 경우(B/N · PREVENT)는 접두를 생략한다.
        mid_head = [] if card in ("neck", "tip") else [head]
        path = tuple(pre + mid_head + _ins_path(r, cards[card]["kind"]))
        it = cards[card]["items"][path]
        q = num(r.get("qty"))
        it["lots"] += 1
        it["qty"] += q
        if extra:
            for e in extra:
                it["blocked"][e] += q
            it["blocked_qty"] += q
        if calc_wt(mv, r["lot_id"], q, r.get("lot_status")) <= 0:
            it["wt0_qty"] += q
            d = num_f(r.get("마지막작업경과_일"))
            if d is not None:
                it["idle"].append(d)
    return cards


def _ins_new():
    from collections import defaultdict
    return {"lots": 0, "qty": 0, "wt0_qty": 0, "idle": [],
            "blocked": defaultdict(int), "blocked_qty": 0}


def _build_insights(line, types, rows, mv, rules, ht, cls, tot, top_n=5):
    """카드 네 장. 각 카드 안에서 매수 순으로 5순위까지."""
    cards = _ins_bucket(rows, mv, rules, ht, cls, line)

    out = []
    total = tot.get("qty") or 0
    for ck, cv in cards.items():
        items = sorted(cv["items"].items(), key=lambda kv: -kv[1]["qty"])
        rank = []
        csum = 0
        for path, v in items[:top_n]:
            idle = v["idle"]
            csum += v["qty"]
            bl = sorted(v["blocked"].items(), key=lambda kv: -kv[1])
            rank.append({
                "path": list(path), "lots": v["lots"], "qty": v["qty"],
                "pct": round(v["qty"] / total * 100, 1) if total else 0,
                "wt0_qty": v["wt0_qty"],
                "idle_avg": round(sum(idle) / len(idle), 1) if idle else None,
                "idle_max": round(max(idle), 1) if idle else None,
                # Bottleneck 전용: 호환 그룹에서 막힌 설비
                "blocked": [{"name": a, "qty": b} for a, b in bl[:3]] or None,
                "blocked_qty": v["blocked_qty"] or None,
            })
        out.append({"key": ck, "title": cv["title"],
                    "qty": sum(v["qty"] for _, v in items),
                    "shown": csum, "rank": rank})
    return out


def _top(d):
    """가장 큰 항목 (이름, 매수). 없으면 None."""
    if not d:
        return None
    k = max(d, key=lambda x: d[x])
    return {"name": k, "qty": int(d[k])}


def api_eqp_wait(request):
    """설비 앞에 선 재공을 **라인 구분 없이** 센다.

    Bottleneck 과 설비이슈는 같은 설비를 여러 라인이 나눠 쓰므로,
    제 라인만 봐서는 그 설비가 왜 막혔는지 알 수 없다.
    화면의 '전 라인' 토글이 이 API 를 부른다.
    """
    types = [t for t in request.GET.get("types", "").split(",") if t]
    kind = request.GET.get("kind", "neck")      # neck | eqp
    want = [x for x in request.GET.get("eqp", "").split(",") if x]
    rules, ht = _cause_rules(), _holdtype_rules()

    out, snap = {}, None
    for card in LINE_CARDS:
        ln = card["line"]
        rows, sp = _summary_rows(ln, types)
        if not rows:
            continue
        snap = snap or sp
        for r in rows:
            big, mid, subs = classify_lot(r, rules, ht)
            if kind == "neck" and big != "Bottleneck":
                continue
            if kind == "eqp" and not (big == "Wait성 진행불가"
                                      and mid == "설비이슈"):
                continue
            ids = _eqp_ids(r) or [str(r.get("eqpgroup") or "").strip()]
            if want and not (set(ids) & set(want)):
                continue
            q = num(r.get("qty"))
            w = q / len(ids) if ids else q
            for eid in (ids or ["(설비미상)"]):
                c = out.setdefault(eid, {})
                c[ln] = c.get(ln, 0) + w

    rows_out = [{"name": k,
                 "qty": int(round(sum(v.values()))),
                 "by_line": {a: int(round(b)) for a, b in v.items()}}
                for k, v in out.items()]
    rows_out.sort(key=lambda x: -x["qty"])
    return JsonResponse({
        "rows": rows_out[:40],
        "lines": [c["line"] for c in LINE_CARDS],
        "labels": {c["line"]: c["label"] for c in LINE_CARDS},
    }, json_dumps_params={"ensure_ascii": False})


def api_lot_steps(request):
    """한 lot 의 모든 스텝(현스텝 + 연속블록).

    표의 재공 수는 lot 단위 집계로 세므로, 여기서 여러 행을 돌려줘도
    카운트에는 영향이 없다. 화면에서 접었다 펴는 상세일 뿐이다.
    """
    line = request.GET.get("line") or DEFAULT_LINE
    lot = request.GET.get("lot_id") or ""
    if not lot or not _table_exists("f3_live"):
        return JsonResponse({"rows": []})

    snap = _latest_snapshot()
    with connection.cursor() as cur:
        cur.execute("SELECT * FROM f3_live LIMIT 0")
        have = {d[0] for d in cur.description}
    want = [c for c, _ in LOT_DETAIL_COLS if c in have]
    for extra in ("현스텝", "연속", "order_seq", "de_rank"):
        if extra in have and extra not in want:
            want.append(extra)

    with connection.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(f'`{c}`' for c in want)} FROM f3_live "
            f"WHERE snapshot_at=%s AND `line`=%s AND lot_id=%s "
            f"ORDER BY CAST(`order_seq` AS SIGNED)"
            if "order_seq" in want else
            f"SELECT {', '.join(f'`{c}`' for c in want)} FROM f3_live "
            f"WHERE snapshot_at=%s AND `line`=%s AND lot_id=%s",
            [snap, line, lot])
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]

    return JsonResponse({"rows": rows,
                         "cols": [{"k": c, "t": t} for c, t in LOT_DETAIL_COLS]},
                        json_dumps_params={"ensure_ascii": False})


def api_lots_live(request):
    """FAB현황 드릴다운. 현재 단면(f3_live) 기준.

    W/T 분포 막대와 원인 분석 노드에서 넘어온 조건을 그대로 적용한다.
    분류는 api_summary 와 동일한 classify_lot 을 쓴다.
    """
    line = request.GET.get("line") or DEFAULT_LINE
    raw = request.GET.get("types")
    types = ([t for t in raw.split(",") if t] if raw is not None
             else list(DEFAULT_LOT_TYPES))
    # Ctrl 다중선택을 위해 쉼표로 여러 값을 받는다.
    def multi(name):
        return [x for x in request.GET.get(name, "").split(",") if x]

    wt_range = multi("wt_range")
    wt0 = multi("wt0")
    prod1 = [x for x in request.GET.get("prod1", "").split(",") if x]
    prod2 = [x for x in request.GET.get("prod2", "").split(",") if x]
    areas = [x for x in request.GET.get("area", "").split(",") if x]
    # 화면에 보이는 컬럼만 실어 보낸다. 응답 크기가 절반 이하로 준다.
    want_cols = [x for x in request.GET.get("cols", "").split(",") if x]
    bdate = request.GET.get("biz_date", "")
    bshift = request.GET.get("shift", "")
    lot_type = multi("lot_type")     # 재공 구성 막대에서
    status = multi("status")         # status 원차트에서
    # LOT BALANCE 막대에서. layer_id 는 현스텝 행의 값이라 SQL WHERE 로
    # 거르면 연속블록 행까지 걸려 필터가 무력해진다. 집계 뒤에 판별한다.
    layer_id = multi("layer_id")
    big = request.GET.get("big", "")
    mid = request.GET.get("mid", "")
    subs_want = multi("sub")
    # 서로 다른 대분류를 함께 고른 경우. AND 로는 언제나 0 이라 OR 로 본다.
    #   causes=Hold>>FLOW금지|Wait성 진행불가>설비이슈>
    or_causes = []
    for spec in request.GET.get("causes", "").split("|"):
        if not spec:
            continue
        parts = (spec.split(">") + ["", "", "", ""])[:4]
        or_causes.append(tuple(x.strip() for x in parts))

    if not _table_exists("f3_live"):
        return JsonResponse({"rows": [], "cols": [], "reason": "f3_live 미적재"})

    # (미분류)가 섞이면 IN 절로 못 거른다. 그 경우만 파이썬에서 처리한다.
    xf = _xfilters(request)
    f_area_x = [x for x in request.GET.get("f_area", "").split(",") if x]
    p1_sql = prod1 if prod1 and UNCLASSIFIED not in prod1 else None
    p2_sql = prod2 if prod2 and UNCLASSIFIED not in prod2 else None
    rows, snap = _summary_rows(line, types, {
        "prod1": p1_sql, "prod2": p2_sql,
        "AREA": areas or None,
        "lot_type": lot_type or None,
        "lot_status": status or None,
    }, biz_date=bdate, shift=bshift)
    mv = _lot_move_map(line)
    rules = _cause_rules()
    ht = _holdtype_rules()

    # lot_id / wt 는 정렬과 식별에 쓰이므로 화면에서 감춰도 항상 실어 보낸다.
    pkey = _preset_for(request.GET.get("src", ""),
                       status[0] if len(status) == 1 else "",
                       big, lot_type[0] if len(lot_type) == 1 else "",
                       wt_range, wt0)
    valid = {c for c, _ in LOT_DETAIL_COLS}
    preset = [c for c in DRILL_PRESETS.get(pkey, [])
              if c in valid] if pkey else None

    keep = (set(want_cols) | {"lot_id", "wt"}) if want_cols else None
    send = [(c, t) for c, t in LOT_DETAIL_COLS if keep is None or c in keep]
    if not send:
        send = list(LOT_DETAIL_COLS)
    send_keys = [c for c, _ in send]

    out = []
    tot_qty = 0
    for r in rows:
        q = num(r.get("qty"))
        # lot_type/status 와 (미분류) 없는 제품구분은 위 SQL 에서 이미 걸렀다
        if prod1 and not p1_sql and (r.get("prod1") or UNCLASSIFIED) not in prod1:
            continue
        if prod2 and not p2_sql and (r.get("prod2") or UNCLASSIFIED) not in prod2:
            continue
        wt = calc_wt(mv, r["lot_id"], q, r.get("lot_status"))
        if wt_range and _wt_range_key(wt) not in wt_range:
            continue
        if wt0:
            if wt > 0 or _wt0_bin(num_f(r.get("마지막작업경과_일"))) not in wt0:
                continue
        b2, m2, s2 = classify_lot(r, rules, ht)
        if not _xrow_ok(r, xf):
            continue
        if layer_id and str(r.get("layer_id") or "").strip() not in layer_id:
            continue
        if not _farea_ok(r, f_area_x):
            continue
        if or_causes:
            ids = set(_eqp_ids(r))
            if not any((not a or b2 == a)
                       and (not b or (m2 or "") == b)
                       and (not c or c in s2)
                       and (not e or e in ids)
                       for a, b, c, e in or_causes):
                continue
        else:
            if big and b2 != big:
                continue
            if mid and (m2 or "") != mid:
                continue
            if subs_want and not any(x in s2 for x in subs_want):
                continue
        rec = dict(r)
        rec["wt"] = round(wt, 2)
        cause = " / ".join(x for x in (b2, m2) if x)
        detail = r.get("cause_detail") or holdtype_of(r, ht)
        rec["cause"] = f"{cause}({detail})" if (cause and detail) else cause
        row = {c: rec.get(c) for c in send_keys}
        row["_steps"] = int(r.get("_steps") or 1)
        out.append(row)
        tot_qty += q

    out.sort(key=lambda x: (x["wt"], str(x["lot_id"])))
    return JsonResponse({
        "rows": out,
        "cols": [{"k": c, "t": t} for c, t in send],
        "all_cols": [{"k": c, "t": t} for c, t in LOT_DETAIL_COLS],
        "preset": preset,
        # 필터 목록 정렬 힌트. 없는 값은 이름순으로 뒤에 붙인다.
        "order": {"lot_type": LOT_TYPE_ORDER,
                  "proc_id": list(_main_plans(line).keys())},
        "status_colors": STATUS_COLORS,
        "lots": len(out), "qty": tot_qty,
        "line": line, "snapshot_at": str(snap) if snap else "",
    }, json_dumps_params={"ensure_ascii": False})
