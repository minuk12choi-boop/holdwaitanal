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

# 상단 현황 카드. 좌측부터 고정 순서.
#   label = 화면 표기, line = f3/move 의 실제 라인 코드
LINE_CARDS = [
    {"label": "NRD",   "line": "KFR4", "head": "#2F4B7C"},
    {"label": "NRD-P", "line": "PFR1", "head": "#2FA8D8"},
    {"label": "NRD-K", "line": "KFR7", "head": "#8CC63F"},
    {"label": "NRD-V", "line": "KFR6", "head": "#F97C4F"},
]

STATUS_ORDER = ["RUN", "WAIT", "HOLD", "WAIT(진행불가)"]
STATUS_COLORS = {
    "RUN": "#2563EB",            # 파랑
    "WAIT": "#16A34A",           # 초록
    "HOLD": "#DC2626",           # 빨강
    "WAIT(진행불가)": "#EAB308",   # 노랑
}

# Top5 설비의 '대기랏' 판정 기준. 진행 중(RUN)이 아니라 설비를 기다리는 상태.
WAITING_STATUS = ("WAIT", "WAIT(진행불가)")
LOOKBACK_DAYS = 140

# blueprint 7.3 권장 상대 높이. 템플릿에는 계산된 px 로 넘긴다.
PANEL_BASE_PX = 150
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
     "fmt": "i", "basis": True},
    {"key": "blocked", "title": "WAIT성 진행불가율",    "unit": "%",    "h": 0.8,
     "fmt": "i", "basis": True},
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
        data.update(key=p["key"], title=p["title"], unit=p["unit"],
                    height=p["h"], fmt=p.get("fmt", "f1"))
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


def _move_by_shift():
    """가장 최근 업무일의 라인별 shift MOVE."""
    if not _table_exists("move_shift"):
        return {}, None
    with connection.cursor() as cur:
        cur.execute("SELECT MAX(biz_date) FROM move_shift")
        bd = cur.fetchone()[0]
        if not bd:
            return {}, None
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
    snap = _latest_snapshot()
    lots = _lot_rows(snap) if snap else []
    move, move_bd = _move_by_shift()

    # line -> lot_type -> 집계. ALL_TYPES 버킷도 함께 채운다.
    agg = {}
    types_by_line = {}
    for line, lot_id, lot_type, status, qty, eqpgroup in lots:
        lt = (lot_type or "-").strip()
        types_by_line.setdefault(line, set()).add(lt)
        st = status or "-"
        q = int(qty or 0)

        # 설비그룹은 n개 설비로 엮여 있으면 각 설비에 1/n LOT, 1/n 매로 나눠 계상한다.
        # (모든 설비에 1랏씩 계상하면 합계가 실제 대기량보다 부풀려진다)
        eqps = [x.strip() for x in str(eqpgroup or "").split(",") if x.strip()]
        w = 1.0 / len(eqps) if eqps else 0.0

        for bucket in (ALL_TYPES, lt):
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
        top = sorted(d["eqp"].items(), key=lambda kv: (-kv[1]["lots"], kv[0]))[:5]
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
            "top_eqp": [{"name": k, "lots": round(v["lots"], 1),
                         "qty": int(round(v["qty"]))} for k, v in top],
        }

    cards = []
    for c in LINE_CARDS:
        d = agg.get(c["line"])
        if not d:
            cards.append({**c, "ready": False})
            continue
        types = [ALL_TYPES] + sorted(t for t in types_by_line.get(c["line"], ()))
        mv = move.get(c["line"], {})
        cards.append({
            **c, "ready": True, "types": types,
            "by_type": {t: pack(d[t]) for t in types if t in d},
            "move": {"GY": mv.get("GY", 0), "DAY": mv.get("DAY", 0),
                     "SW": mv.get("SW", 0),
                     "total": sum(mv.get(k, 0) for k in ("GY", "DAY", "SW"))},
        })

    return JsonResponse({
        "cards": cards,
        "snapshot_at": (snap.strftime("%Y-%m-%d %H:%M")
                        if hasattr(snap, "strftime") else (str(snap) if snap else None)),
        "move_biz_date": str(move_bd) if move_bd else None,
    }, json_dumps_params={"ensure_ascii": False})
