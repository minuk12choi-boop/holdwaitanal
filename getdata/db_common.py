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
# 공용 변환
# ---------------------------------------------------------------------------
def to_datetime(series):
    """어떤 표기로 와도 datetime 으로.

        2026-08-12 13:14:22        ISO
        2026-08-12 오후 1:14:22     한글 오전/오후
        20260812 131422            숫자 + 공백
        20260812131422 / ...0      숫자만 / 밀리초
        (datetime64)               Oracle TO_DATE 결과

    DuckDB 쪽 build_f3.parsed_ts() 와 같은 규칙이다.
    """
    import pandas as pd

    if pd.api.types.is_datetime64_any_dtype(series):
        return series

    s = series.astype("string")
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

    # 1) 한글 오전/오후 -> 숫자 추출로는 못 푼다. 먼저 처리.
    ko = s.str.contains("오전|오후", na=False)
    if ko.any():
        t = (s[ko].str.replace(r"(오전|오후)\s*0?0:", r"\1 12:", regex=True)
                  .str.replace("오전", "AM", regex=False)
                  .str.replace("오후", "PM", regex=False))
        out.loc[ko] = pd.to_datetime(t, format="%Y-%m-%d %p %I:%M:%S",
                                     errors="coerce")

    # 2) 숫자만 뽑아 14자리
    rest = ~ko
    if rest.any():
        d = s[rest].str.replace(r"[^0-9]", "", regex=True).str.slice(0, 14)
        d = d.where(d.str.len() >= 8).str.pad(14, side="right", fillchar="0")
        out.loc[rest] = pd.to_datetime(d, format="%Y%m%d%H%M%S", errors="coerce")

    # 3) 남은 것은 일반 파서로
    miss = out.isna() & s.notna() & (s.str.strip() != "")
    if miss.any():
        out.loc[miss] = pd.to_datetime(s[miss], errors="coerce")
    return out


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
    "hold_reason", "exception_reason", "ftp_reason", "step_desc", "eqpline", "prod1", "prod2", "dept",
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

    # CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블에 컬럼을 늘려주지 않는다.
    # SUMMARY_OUTPUT_COLUMNS 가 바뀌면 기존 데이터를 유지한 채 컬럼만 추가한다.
    for table in ("f3_live", "f3_history"):
        add_missing_columns(conn, table, columns)


def add_missing_columns(conn, table, columns):
    """테이블에 없는 컬럼을 ALTER TABLE 로 추가한다(기존 행은 NULL).

    반환: 추가한 컬럼 목록
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s", (table,))
        have = {r[0].lower() for r in cur.fetchall()}
        if not have:
            return []                      # 테이블 자체가 없음

        added = []
        prev = None
        for c in columns:
            if c.lower() in have:
                prev = c
                continue
            typ = "TEXT" if c in TEXT_COLUMNS else f"VARCHAR({SHORT_LEN})"
            after = f" AFTER `{prev}`" if prev else ""
            cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{c}` {typ} NULL{after}")
            added.append(c)
            prev = c
    conn.commit()
    if added:
        print(f"[DDL] {table} 컬럼 추가: {', '.join(added)}", flush=True)
    return added


def ensure_standard_schema(conn):
    """기준정보: 원인 소분류 규칙.

    hold / exception / ftp 의 '유형' 은 아직 정립 전이라, 사유 문자열에 포함된
    키워드로 유형을 붙이는 방식으로 시작한다. 화면(기준정보)에서 편집한다.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS cause_rules (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          category VARCHAR(32) NOT NULL,      -- hold | exception | ftp
          keyword VARCHAR(128) NOT NULL,      -- 사유에 포함되면 매칭
          label VARCHAR(64) NOT NULL,         -- 화면에 보일 유형명
          sort_no INT NOT NULL DEFAULT 100,
          updated_at DATETIME NULL,
          KEY ix_cr (category, sort_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
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
          query_time VARCHAR(32) NULL,
          KEY ix_ll_snap (snapshot_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # 기존 테이블에 kind 컬럼 보강
        for ddl in (
                "ALTER TABLE f3_load_log ADD COLUMN kind VARCHAR(8) "
                "NOT NULL DEFAULT '조회'",
                "ALTER TABLE f3_load_log ADD COLUMN query_time VARCHAR(32) NULL"):
            try:
                cur.execute(ddl)
            except Exception:
                pass
    conn.commit()


def load_f3_load_log(conn, snapshot_at, load_log, keep=2):
    """스냅샷별 로딩시각 기록. f3_live 와 같은 벌수만 유지한다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM f3_load_log WHERE snapshot_at = %s", (snapshot_at,))
        cur.executemany(
            "INSERT INTO f3_load_log (snapshot_at, table_name, load_start, load_end,"
            " elapsed_sec, row_count, col_count, kind, query_time)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(snapshot_at, r.get("테이블"), r.get("로딩_시작시각"), r.get("로딩_종료시각"),
              r.get("소요_초"), r.get("행수"), r.get("컬럼수"),
              r.get("구분", "조회"), r.get("원천조회시각")) for r in load_log])
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
    """이번 조회가 커버한 (업무일, shift) 만 교체한다.

    업무일 통째로 지우면 안 된다. 2시간 주기로 최근 몇 시간만 조회할 때
    같은 업무일의 이전 shift(이미 적재된 것)까지 날아간다.
    move_daily 는 교체 후 move_shift 에서 다시 합산해 만든다.
    """
    now = dt.datetime.now()
    pairs = sorted({(r.biz_date, r.shift) for r in df_shift.itertuples(index=False)}) \
        if len(df_shift) else []

    with conn.cursor() as cur:
        for bd, sh in pairs:
            cur.execute("DELETE FROM move_shift WHERE biz_date=%s AND shift=%s", (bd, sh))
            cur.execute("DELETE FROM move_lot   WHERE biz_date=%s AND shift=%s", (bd, sh))

        if len(df_shift):
            cur.executemany(
                "INSERT INTO move_shift "
                "(biz_date, shift, sys_line_id, move_qty, lot_cnt, loaded_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [(r.biz_date, r.shift, r.sys_line_id, int(r.move_qty), int(r.lot_cnt), now)
                 for r in df_shift.itertuples(index=False)])

        if df_lot is not None and len(df_lot):
            cur.executemany(
                "INSERT INTO move_lot "
                "(biz_date, shift, sys_line_id, lot_id, move_qty, tkout_cnt, loaded_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [(r.biz_date, r.shift, r.sys_line_id, str(r.lot_id)[:64],
                  int(r.move_qty), int(r.tkout_cnt), now)
                 for r in df_lot.itertuples(index=False)])

        # 영향받은 업무일의 일 집계를 shift 합으로 다시 만든다
        for bd in sorted({b for b, _ in pairs}):
            cur.execute("DELETE FROM move_daily WHERE biz_date=%s", (bd,))
            cur.execute(
                "INSERT INTO move_daily "
                "(biz_date, sys_line_id, move_qty, lot_cnt, loaded_at) "
                "SELECT biz_date, sys_line_id, SUM(move_qty), SUM(lot_cnt), %s "
                "FROM move_shift WHERE biz_date=%s "
                "GROUP BY biz_date, sys_line_id", (now, bd))
    conn.commit()
    return pairs


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
    ensure_f3_schema(conn, SUMMARY_OUTPUT_COLUMNS)   # 없으면 생성, 있으면 컬럼 보강
    ensure_load_log_schema(conn)
    ensure_move_schema(conn)
    ensure_standard_schema(conn)
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
