# -*- coding: utf-8 -*-
"""
db_common.py — DB 접속 / 스키마 / 적재 공용 모듈

build_f3.py 와 get_move.py 가 함께 쓴다.
정의(업무일·shift·MOVE·W/T)는 docs/common_conventions.md 참조.
"""

from __future__ import annotations

import datetime as dt
import os

SHIFTS = {"GY": 22, "DAY": 6, "SW": 14}
SHIFT_ORDER = ["GY", "DAY", "SW"]
MOVE_LOT_TYPES = ("PP", "PB", "PG")


# ---------------------------------------------------------------------------
# 업무일 / shift
# ---------------------------------------------------------------------------
def biz_date(ts: dt.datetime) -> dt.date:
    """22:00 이상이면 다음날이 업무일."""
    return (ts + dt.timedelta(days=1)).date() if ts.hour >= 22 else ts.date()


def shift_boundaries(around: dt.datetime, back_hours: int = 26):
    """`around` 이전 `back_hours` 안의 shift 기준시각들을 최신순으로."""
    out = []
    start = around - dt.timedelta(hours=back_hours)
    day = start.date() - dt.timedelta(days=1)
    while day <= around.date():
        for name, hour in SHIFTS.items():
            b = dt.datetime.combine(day, dt.time(hour))
            if start <= b <= around:
                out.append((b, name))
        day += dt.timedelta(days=1)
    return sorted(out, key=lambda x: x[0], reverse=True)


def shift_window(boundary: dt.datetime):
    """shift 의 근무 구간 (boundary, boundary+8h].

        GY  22:00 ~ 06:00
        DAY 06:00 ~ 14:00
        SW  14:00 ~ 22:00

    한 업무일의 3개 shift 합이 정확히 그 업무일(22:00~22:00)이 된다.
    """
    return boundary, boundary + dt.timedelta(hours=8)


def snapshot_shift(boundary: dt.datetime):
    """스냅샷 시각 -> (업무일, shift). 그 shift 가 '시작되는' 시점으로 라벨링한다.

        (D-1) 22:00 -> (D, 'GY')
        D     06:00 -> (D, 'DAY')
        D     14:00 -> (D, 'SW')

    MOVE 도 같은 (업무일, shift) 로 저장되므로 **같은 이름끼리 그대로 짝**이 된다.
        W/T(D, S) = MOVE(D, S) / 재공(D, S 스냅샷)
    즉 분모는 그 조가 시작할 때의 재공, 분자는 그 조가 낸 MOVE 다.
    """
    return biz_date(boundary), {22: "GY", 6: "DAY", 14: "SW"}[boundary.hour]


# ---------------------------------------------------------------------------
# 접속
# ---------------------------------------------------------------------------
def load_env(path=".env"):
    """.env 를 읽어 os.environ 에 채운다(python-dotenv 없어도 동작)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def connect():
    """pymysql 커넥션. .env 의 HOLDWAITANAL_DB_* 사용."""
    import pymysql

    load_env()
    return pymysql.connect(
        host=os.environ.get("HOLDWAITANAL_DB_HOST", "127.0.0.1"),
        user=os.environ.get("HOLDWAITANAL_DB_USER", ""),
        password=os.environ.get("HOLDWAITANAL_DB_PASSWORD", ""),
        port=int(os.environ.get("HOLDWAITANAL_DB_PORT", "3306")),
        database=os.environ.get("HOLDWAITANAL_DB_NAME", "app_db"),
        charset=os.environ.get("HOLDWAITANAL_DB_CHARSET", "utf8mb4"),
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------
def _col_ddl(columns):
    """f3 출력 컬럼을 전부 넉넉한 문자열로. 숫자 판별은 조회 시 캐스팅."""
    return ",\n  ".join(f"`{c}` VARCHAR(512) NULL" for c in columns)


def ensure_f3_schema(conn, columns):
    live = f"""
    CREATE TABLE IF NOT EXISTS f3_live (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      snapshot_at DATETIME NOT NULL,
      {_col_ddl(columns)},
      KEY ix_live_snap (snapshot_at),
      KEY ix_live_lot (`line`, `lot_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    hist = f"""
    CREATE TABLE IF NOT EXISTS f3_history (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      biz_date DATE NOT NULL,
      shift VARCHAR(3) NOT NULL,
      snapshot_at DATETIME NOT NULL,
      {_col_ddl(columns)},
      KEY ix_hist_key (biz_date, shift),
      KEY ix_hist_lot (biz_date, shift, `line`, `lot_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    meta = """
    CREATE TABLE IF NOT EXISTS f3_history_meta (
      biz_date DATE NOT NULL,
      shift VARCHAR(3) NOT NULL,
      snapshot_at DATETIME NOT NULL,
      dist_sec INT NOT NULL,
      row_count INT NOT NULL,
      loaded_at DATETIME NOT NULL,
      PRIMARY KEY (biz_date, shift)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with conn.cursor() as cur:
        for sql in (live, hist, meta):
            cur.execute(sql)
    conn.commit()


def ensure_move_schema(conn):
    shift_tbl = """
    CREATE TABLE IF NOT EXISTS move_shift (
      biz_date DATE NOT NULL,
      shift VARCHAR(3) NOT NULL,
      sys_line_id VARCHAR(32) NOT NULL,
      move_qty BIGINT NOT NULL DEFAULT 0,
      lot_cnt INT NOT NULL DEFAULT 0,
      loaded_at DATETIME NOT NULL,
      PRIMARY KEY (biz_date, shift, sys_line_id),
      KEY ix_ms_line (sys_line_id, biz_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    daily_tbl = """
    CREATE TABLE IF NOT EXISTS move_daily (
      biz_date DATE NOT NULL,
      sys_line_id VARCHAR(32) NOT NULL,
      move_qty BIGINT NOT NULL DEFAULT 0,
      lot_cnt INT NOT NULL DEFAULT 0,
      loaded_at DATETIME NOT NULL,
      PRIMARY KEY (biz_date, sys_line_id),
      KEY ix_md_line (sys_line_id, biz_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with conn.cursor() as cur:
        cur.execute(shift_tbl)
        cur.execute(daily_tbl)
    conn.commit()


# ---------------------------------------------------------------------------
# 적재
# ---------------------------------------------------------------------------
def _rows(df, columns):
    out = []
    for r in df[columns].itertuples(index=False, name=None):
        out.append(tuple(None if _isna(v) else str(v)[:512] for v in r))
    return out


def _isna(v):
    try:
        import pandas as pd
        return pd.isna(v)
    except Exception:
        return v is None


def load_f3_live(conn, df, columns, snapshot_at, keep=2):
    """실시간 테이블에 스냅샷 추가 후, 최근 `keep` 벌만 남긴다."""
    cols = ", ".join(f"`{c}`" for c in columns)
    ph = ", ".join(["%s"] * len(columns))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM f3_live WHERE snapshot_at = %s", (snapshot_at,))
        cur.executemany(
            f"INSERT INTO f3_live (snapshot_at, {cols}) VALUES (%s, {ph})",
            [(snapshot_at,) + r for r in _rows(df, columns)],
        )
        cur.execute(
            "DELETE FROM f3_live WHERE snapshot_at NOT IN "
            "(SELECT * FROM (SELECT DISTINCT snapshot_at FROM f3_live "
            " ORDER BY snapshot_at DESC LIMIT %s) t)", (keep,))
    conn.commit()


def promote_to_history(conn, columns, now=None):
    """직전 shift 기준시각에 가장 가까운 live 스냅샷을 history 로 승격한다.

    이미 적재된 것보다 더 가까운 스냅샷일 때만 교체하므로 몇 번을 돌려도 안전하다.
    반환: (biz_date, shift, snapshot_at, dist_sec) 또는 None
    """
    now = now or dt.datetime.now()
    bounds = shift_boundaries(now)
    if not bounds:
        return None
    boundary, _ = bounds[0]
    bd, shift = snapshot_shift(boundary)

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT snapshot_at FROM f3_live")
        snaps = [r[0] for r in cur.fetchall()]
        if not snaps:
            return None
        snap = min(snaps, key=lambda s: abs((s - boundary).total_seconds()))
        dist = int(abs((snap - boundary).total_seconds()))

        cur.execute("SELECT snapshot_at, dist_sec FROM f3_history_meta "
                    "WHERE biz_date=%s AND shift=%s", (bd, shift))
        prev = cur.fetchone()
        if prev and (prev[0] == snap or prev[1] <= dist):
            return None

        cols = ", ".join(f"`{c}`" for c in columns)
        cur.execute("DELETE FROM f3_history WHERE biz_date=%s AND shift=%s", (bd, shift))
        cur.execute(
            f"INSERT INTO f3_history (biz_date, shift, snapshot_at, {cols}) "
            f"SELECT %s, %s, snapshot_at, {cols} FROM f3_live WHERE snapshot_at=%s",
            (bd, shift, snap))
        n = cur.rowcount
        cur.execute(
            "REPLACE INTO f3_history_meta "
            "(biz_date, shift, snapshot_at, dist_sec, row_count, loaded_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (bd, shift, snap, dist, n, dt.datetime.now()))
    conn.commit()
    return bd, shift, snap, dist


def replace_move(conn, df_shift, df_daily, biz_dates):
    """지정 업무일 구간을 통째로 교체한다(중복 비교 없이 멱등)."""
    now = dt.datetime.now()
    with conn.cursor() as cur:
        if biz_dates:
            lo, hi = min(biz_dates), max(biz_dates)
            cur.execute("DELETE FROM move_shift WHERE biz_date BETWEEN %s AND %s", (lo, hi))
            cur.execute("DELETE FROM move_daily WHERE biz_date BETWEEN %s AND %s", (lo, hi))
        if len(df_shift):
            cur.executemany(
                "INSERT INTO move_shift "
                "(biz_date, shift, sys_line_id, move_qty, lot_cnt, loaded_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [(r.biz_date, r.shift, r.sys_line_id, int(r.move_qty), int(r.lot_cnt), now)
                 for r in df_shift.itertuples(index=False)])
        if len(df_daily):
            cur.executemany(
                "INSERT INTO move_daily "
                "(biz_date, sys_line_id, move_qty, lot_cnt, loaded_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                [(r.biz_date, r.sys_line_id, int(r.move_qty), int(r.lot_cnt), now)
                 for r in df_daily.itertuples(index=False)])
    conn.commit()


def move_last_biz_date(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(biz_date) FROM move_daily")
        return cur.fetchone()[0]
