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
def find_env(start=None):
    """이 파일 위치에서 위로 올라가며 .env 를 찾는다.

    getdata/ 에서 실행하든 web/ 에서 실행하든 저장소 루트의 .env 를 찾도록.
    """
    d = os.path.dirname(os.path.abspath(start or __file__))
    for _ in range(5):
        cand = os.path.join(d, ".env")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def load_env(path=None):
    """.env 를 읽어 os.environ 에 채운다(python-dotenv 없어도 동작)."""
    path = path or find_env()
    if not path or not os.path.exists(path):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
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
# 길이가 긴 컬럼은 TEXT 로. 전 컬럼을 VARCHAR(512) 로 잡으면
#   - 행 크기 41 x 512 x 4 = 83,968 bytes > 65,535 (InnoDB 한계)
#   - 인덱스 (line, lot_id) 4,096 bytes > 3,072
# 두 한계에 모두 걸린다.
TEXT_COLUMNS = {
    "lot_inform", "eqpgroup", "eqpgroup_cham", "tip", "down",
    "hold_reason", "exception_reason", "ftp_reason", "step_desc", "eqpline",
}
SHORT_LEN = 128          # 그 외 컬럼 (128 x 4 = 512 bytes)
IDX_PREFIX = {"line": 16, "lot_id": 64}


def _col_ddl(columns):
    out = []
    for c in columns:
        typ = "TEXT" if c in TEXT_COLUMNS else f"VARCHAR({SHORT_LEN})"
        out.append(f"`{c}` {typ} NULL")
    return ",\n  ".join(out)


def _idx(cols):
    """인덱스 컬럼에 접두 길이를 붙인다(키 길이 3,072 bytes 한계 회피)."""
    parts = []
    for c in cols:
        n = IDX_PREFIX.get(c)
        parts.append(f"`{c}`({n})" if n else c)
    return ", ".join(parts)


def ensure_f3_schema(conn, columns):
    live_idx = _idx(["line", "lot_id"])
    hist_idx = "biz_date, shift, " + live_idx
    live = f"""
    CREATE TABLE IF NOT EXISTS f3_live (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      snapshot_at DATETIME NOT NULL,
      {_col_ddl(columns)},
      KEY ix_live_snap (snapshot_at),
      KEY ix_live_lot ({live_idx})
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
      KEY ix_hist_lot ({hist_idx})
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


def ensure_load_log_schema(conn):
    """원천 테이블별 로딩 시작/종료 시각. 웹 다운로드 화면에서 보여준다."""
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS f3_load_log (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          snapshot_at DATETIME NOT NULL,
          table_name VARCHAR(64) NOT NULL,
          load_start DATETIME NULL,
          load_end DATETIME NULL,
          elapsed_sec DOUBLE NULL,
          row_count BIGINT NULL,
          col_count INT NULL,
          kind VARCHAR(8) NOT NULL DEFAULT '조회',
          KEY ix_ll_snap (snapshot_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # 기존 테이블에 kind 컬럼 보강
        try:
            cur.execute("ALTER TABLE f3_load_log "
                        "ADD COLUMN kind VARCHAR(8) NOT NULL DEFAULT '조회'")
        except Exception:
            pass
    conn.commit()


def load_f3_load_log(conn, snapshot_at, load_log, keep=2):
    """스냅샷별 로딩시각 기록. f3_live 와 같은 벌수만 유지한다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM f3_load_log WHERE snapshot_at = %s", (snapshot_at,))
        cur.executemany(
            "INSERT INTO f3_load_log (snapshot_at, table_name, load_start, load_end,"
            " elapsed_sec, row_count, col_count, kind)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [(snapshot_at, r.get("테이블"), r.get("로딩_시작시각"), r.get("로딩_종료시각"),
              r.get("소요_초"), r.get("행수"), r.get("컬럼수"),
              r.get("구분", "조회")) for r in load_log])
        cur.execute(
            "DELETE FROM f3_load_log WHERE snapshot_at NOT IN "
            "(SELECT * FROM (SELECT DISTINCT snapshot_at FROM f3_load_log "
            " ORDER BY snapshot_at DESC LIMIT %s) t)", (keep,))
    conn.commit()


def ensure_move_schema(conn):
    """move_shift / move_daily / move_lot.

    move_lot 은 lot 단위 MOVE. WT(= MOVE/재공매수) 를 lot 별로 계산하려면
    집계본만으로는 부족해서 따로 둔다. Low WT 분석의 기반.
    """
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
    lot_tbl = """
    CREATE TABLE IF NOT EXISTS move_lot (
      biz_date DATE NOT NULL,
      shift VARCHAR(3) NOT NULL,
      sys_line_id VARCHAR(32) NOT NULL,
      lot_id VARCHAR(64) NOT NULL,
      move_qty BIGINT NOT NULL DEFAULT 0,
      tkout_cnt INT NOT NULL DEFAULT 0,
      loaded_at DATETIME NOT NULL,
      PRIMARY KEY (biz_date, shift, sys_line_id, lot_id),
      KEY ix_ml_day (biz_date, sys_line_id),
      KEY ix_ml_lot (lot_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with conn.cursor() as cur:
        cur.execute(shift_tbl)
        cur.execute(daily_tbl)
        cur.execute(lot_tbl)
    conn.commit()


# ---------------------------------------------------------------------------
# 적재
# ---------------------------------------------------------------------------
def _rows(df, columns):
    """VARCHAR 컬럼은 정의 길이에 맞춰 자른다(TEXT 는 자르지 않음)."""
    limits = [None if c in TEXT_COLUMNS else SHORT_LEN for c in columns]
    out = []
    for r in df[columns].itertuples(index=False, name=None):
        out.append(tuple(None if _isna(v) else
                         (str(v) if lim is None else str(v)[:lim])
                         for v, lim in zip(r, limits)))
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


def replace_move(conn, df_shift, df_daily, biz_dates, df_lot=None):
    """지정 업무일 구간을 통째로 교체한다(중복 비교 없이 멱등)."""
    now = dt.datetime.now()
    with conn.cursor() as cur:
        if biz_dates:
            lo, hi = min(biz_dates), max(biz_dates)
            cur.execute("DELETE FROM move_shift WHERE biz_date BETWEEN %s AND %s", (lo, hi))
            cur.execute("DELETE FROM move_daily WHERE biz_date BETWEEN %s AND %s", (lo, hi))
            cur.execute("DELETE FROM move_lot WHERE biz_date BETWEEN %s AND %s", (lo, hi))
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
        if df_lot is not None and len(df_lot):
            cur.executemany(
                "INSERT INTO move_lot "
                "(biz_date, shift, sys_line_id, lot_id, move_qty, tkout_cnt, loaded_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [(r.biz_date, r.shift, r.sys_line_id, str(r.lot_id)[:64],
                  int(r.move_qty), int(r.tkout_cnt), now)
                 for r in df_lot.itertuples(index=False)])
    conn.commit()


def move_last_biz_date(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(biz_date) FROM move_daily")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# 스키마 초기화 (실행 진입점은 이것 하나뿐. 평상시엔 라이브러리로만 쓴다)
#     python getdata/db_common.py --init
# ---------------------------------------------------------------------------
def _init_schema():
    from build_f3 import SUMMARY_OUTPUT_COLUMNS

    conn = connect()
    ensure_f3_schema(conn, SUMMARY_OUTPUT_COLUMNS)
    ensure_load_log_schema(conn)
    ensure_move_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        names = sorted(r[0] for r in cur.fetchall())
    conn.close()
    print("생성/확인 완료. 현재 테이블:")
    for n in names:
        print(f"  - {n}")


if __name__ == "__main__":
    import sys
    if "--init" in sys.argv:
        _init_schema()
    else:
        print("db_common 은 공용 라이브러리입니다. 스케줄러에 등록하지 마세요.\n"
              "테이블만 미리 만들려면:  python getdata/db_common.py --init")
