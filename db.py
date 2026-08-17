"""SQLite persistence.

Two write paths, deliberately different:

  latest   -- the FULL option chain for every stock, wiped and rewritten each
              cycle. This is the "database of all FnO scrips and their chains"
              view: complete, but only current.

  history  -- only the in-delta-band rows, appended every cycle and retained
              for RETENTION_DAYS. Keeping the full chain in history would add
              ~1.8M rows/day for data nobody queries; the band is what the
              return-per-delta question is actually about.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, List, Optional

import config

RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    n_stocks      INTEGER,
    n_contracts   INTEGER,
    n_priced      INTEGER,
    n_margined    INTEGER,
    duration_sec  REAL,
    status        TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS latest (
    tradingsymbol TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    expiry        TEXT NOT NULL,
    dte           INTEGER,
    strike        REAL,
    opt_type      TEXT,
    lot_size      INTEGER,
    spot          REAL,
    future        REAL,
    bid           REAL,
    ask           REAL,
    mid           REAL,
    ltp           REAL,
    spread_pct    REAL,
    oi            INTEGER,
    oi_lots       REAL,
    volume        INTEGER,
    iv            REAL,
    delta         REAL,
    abs_delta     REAL,
    gamma         REAL,
    theta         REAL,
    vega          REAL,
    delta_bucket  REAL,
    margin        REAL,
    span          REAL,
    exposure      REAL,
    credit        REAL,
    return_pct    REAL,
    liq_flag      TEXT,
    px_status     TEXT,
    quality       TEXT,
    in_band       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_latest_name   ON latest(name);
CREATE INDEX IF NOT EXISTS idx_latest_bucket ON latest(delta_bucket, opt_type, expiry);
CREATE INDEX IF NOT EXISTS idx_latest_ret    ON latest(return_pct);

CREATE TABLE IF NOT EXISTS history (
    snapshot_id   INTEGER NOT NULL,
    ts            TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    name          TEXT NOT NULL,
    expiry        TEXT,
    dte           INTEGER,
    strike        REAL,
    opt_type      TEXT,
    lot_size      INTEGER,
    future        REAL,
    mid           REAL,
    iv            REAL,
    delta         REAL,
    abs_delta     REAL,
    delta_bucket  REAL,
    margin        REAL,
    credit        REAL,
    return_pct    REAL,
    oi            INTEGER,
    liq_flag      TEXT,
    quality       TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_snap   ON history(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_hist_symbol ON history(tradingsymbol, ts);
CREATE INDEX IF NOT EXISTS idx_hist_bucket ON history(name, delta_bucket, opt_type);

CREATE TABLE IF NOT EXISTS stock_meta (
    name        TEXT PRIMARY KEY,
    company     TEXT,
    spot        REAL,
    prev_close  REAL,
    day_change  REAL,
    day_pct     REAL,
    day_open    REAL,
    day_high    REAL,
    day_low     REAL,
    ts          TEXT
);

CREATE TABLE IF NOT EXISTS events (
    name        TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    event_type  TEXT,
    purpose     TEXT,
    detail      TEXT,
    source      TEXT,
    confidence  TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (name, event_date, event_type, source)
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);
CREATE INDEX IF NOT EXISTS idx_events_name ON events(name, event_date);

CREATE TABLE IF NOT EXISTS strategy_cache (
    name       TEXT NOT NULL,
    expiry     TEXT NOT NULL,
    stance     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    computed_at TEXT,
    PRIMARY KEY (name, expiry, stance)
);
CREATE INDEX IF NOT EXISTS idx_strat_name ON strategy_cache(name);

CREATE TABLE IF NOT EXISTS watchlist (
    name     TEXT PRIMARY KEY,
    added_at TEXT,
    note     TEXT,
    sort_idx INTEGER
);

CREATE TABLE IF NOT EXISTS underlyings (
    name        TEXT NOT NULL,
    expiry      TEXT NOT NULL,
    spot        REAL,
    future      REAL,
    basis_pct   REAL,
    lot_size    INTEGER,
    ts          TEXT,
    PRIMARY KEY (name, expiry)
);
"""

LATEST_COLUMNS = [
    "tradingsymbol", "name", "expiry", "dte", "strike", "opt_type", "lot_size",
    "spot", "future", "bid", "ask", "mid", "ltp", "spread_pct", "oi", "oi_lots",
    "volume", "iv", "delta", "abs_delta", "gamma", "theta", "vega",
    "delta_bucket", "margin", "span", "exposure", "credit", "return_pct",
    "liq_flag", "px_status", "quality", "in_band",
]

HISTORY_COLUMNS = [
    "snapshot_id", "ts", "tradingsymbol", "name", "expiry", "dte", "strike",
    "opt_type", "lot_size", "future", "mid", "iv", "delta", "abs_delta",
    "delta_bucket", "margin", "credit", "return_pct", "oi", "liq_flag",
    "quality",
]


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Lightweight migration: these tables hold only transient snapshot data
        # (rewritten every cycle), so recreating them on a schema change is
        # cheaper and safer than patching columns in place.
        for table, cols in (("latest", LATEST_COLUMNS),
                            ("history", HISTORY_COLUMNS),
                            ("stock_meta", ["name", "company", "spot", "prev_close",
                                            "day_change", "day_pct", "day_open",
                                            "day_high", "day_low", "ts"])):
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            missing = [c for c in cols if c not in have]
            if missing:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                conn.executescript(SCHEMA)


def start_snapshot(ts: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO snapshots (ts, status) VALUES (?, 'running')", (ts,))
        return cur.lastrowid


def finish_snapshot(snapshot_id: int, **fields) -> None:
    allowed = {"n_stocks", "n_contracts", "n_priced", "n_margined",
               "duration_sec", "status", "note"}
    sets, vals = [], []
    for key, val in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            vals.append(val)
    if not sets:
        return
    vals.append(snapshot_id)
    with connect() as conn:
        conn.execute(f"UPDATE snapshots SET {', '.join(sets)} WHERE id = ?", vals)


class RefusedWrite(RuntimeError):
    """Raised instead of replacing good data with nothing."""


def write_latest(rows: List[dict], min_rows: int = 1) -> None:
    """Atomically replace the full-chain table.

    This DELETEs before inserting, so an empty `rows` would silently destroy the
    entire dataset. That is exactly what happened when an expired Kite token
    made every quote batch fail: four "successful" cycles wiped the table. The
    table is now only ever replaced by a non-empty result.
    """
    if len(rows) < min_rows:
        raise RefusedWrite(
            f"refusing to replace `latest` with {len(rows)} rows "
            f"(minimum {min_rows}) -- keeping the previous snapshot")
    placeholders = ", ".join("?" for _ in LATEST_COLUMNS)
    sql = f"INSERT INTO latest ({', '.join(LATEST_COLUMNS)}) VALUES ({placeholders})"
    payload = [[r.get(c) for c in LATEST_COLUMNS] for r in rows]
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM latest")
        conn.executemany(sql, payload)


def write_history(rows: List[dict]) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" for _ in HISTORY_COLUMNS)
    sql = f"INSERT INTO history ({', '.join(HISTORY_COLUMNS)}) VALUES ({placeholders})"
    payload = [[r.get(c) for c in HISTORY_COLUMNS] for r in rows]
    with connect() as conn:
        conn.executemany(sql, payload)


def write_underlyings(rows: List[dict]) -> None:
    if not rows:
        return
    sql = ("INSERT INTO underlyings (name, expiry, spot, future, basis_pct, lot_size, ts) "
           "VALUES (?, ?, ?, ?, ?, ?, ?) "
           "ON CONFLICT(name, expiry) DO UPDATE SET "
           "spot=excluded.spot, future=excluded.future, basis_pct=excluded.basis_pct, "
           "lot_size=excluded.lot_size, ts=excluded.ts")
    with connect() as conn:
        conn.executemany(sql, [
            (r["name"], r["expiry"], r.get("spot"), r.get("future"),
             r.get("basis_pct"), r.get("lot_size"), r.get("ts")) for r in rows
        ])


def write_stock_meta(rows: List[dict]) -> None:
    if not rows:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT INTO stock_meta (name, company, spot, prev_close, day_change, "
            "day_pct, day_open, day_high, day_low, ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET company=excluded.company, "
            "spot=excluded.spot, prev_close=excluded.prev_close, "
            "day_change=excluded.day_change, day_pct=excluded.day_pct, "
            "day_open=excluded.day_open, day_high=excluded.day_high, "
            "day_low=excluded.day_low, ts=excluded.ts",
            [(r["name"], r.get("company"), r.get("spot"), r.get("prev_close"),
              r.get("day_change"), r.get("day_pct"), r.get("day_open"),
              r.get("day_high"), r.get("day_low"), r.get("ts")) for r in rows])


def write_events(rows: List[dict]) -> None:
    """Replace the calendar. Refuses an empty write for the same reason
    write_latest does: a failed fetch must not erase a good calendar."""
    if not rows:
        raise RefusedWrite("refusing to replace `events` with 0 rows")
    from datetime import datetime as _dt
    ts = _dt.now().isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM events")
        conn.executemany(
            "INSERT OR REPLACE INTO events (name, event_date, event_type, purpose,"
            " detail, source, confidence, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            [(r["name"], r["event_date"], r.get("event_type"), r.get("purpose"),
              r.get("detail"), r.get("source"), r.get("confidence"), ts)
             for r in rows])


def write_strategies(rows: List[dict]) -> None:
    """Upsert priced structures. Partial writes are fine -- each row stands
    alone, so a run that only manages half the universe still helps."""
    if not rows:
        return
    from datetime import datetime as _dt
    ts = _dt.now().isoformat(timespec="seconds")
    with connect() as conn:
        conn.executemany(
            "INSERT INTO strategy_cache (name, expiry, stance, payload, computed_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(name, expiry, stance) DO UPDATE SET "
            "payload=excluded.payload, computed_at=excluded.computed_at",
            [(r["name"], r["expiry"], r["stance"], r["payload"], ts) for r in rows])


def read_strategies(name: Optional[str] = None,
                    expiry: Optional[str] = None) -> List[dict]:
    where, params = [], []
    if name:
        where.append("name = ?"); params.append(name)
    if expiry:
        where.append("expiry = ?"); params.append(expiry)
    sql = "SELECT * FROM strategy_cache"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def watchlist_get() -> List[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT name FROM watchlist ORDER BY COALESCE(sort_idx, 1e9), name")]


def watchlist_add(names: List[str]) -> List[str]:
    from datetime import datetime as _dt
    ts = _dt.now().isoformat(timespec="seconds")
    with connect() as conn:
        start = conn.execute(
            "SELECT COALESCE(MAX(sort_idx), 0) FROM watchlist").fetchone()[0] or 0
        for i, n in enumerate(names, 1):
            conn.execute(
                "INSERT INTO watchlist (name, added_at, sort_idx) VALUES (?,?,?) "
                "ON CONFLICT(name) DO NOTHING", (n.strip().upper(), ts, start + i))
    return watchlist_get()


def watchlist_remove(names: List[str]) -> List[str]:
    with connect() as conn:
        conn.executemany("DELETE FROM watchlist WHERE name = ?",
                         [(n.strip().upper(),) for n in names])
    return watchlist_get()


def watchlist_set(names: List[str]) -> List[str]:
    """Replace the whole list, preserving the given order."""
    from datetime import datetime as _dt
    ts = _dt.now().isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("DELETE FROM watchlist")
        conn.executemany(
            "INSERT INTO watchlist (name, added_at, sort_idx) VALUES (?,?,?)",
            [(n.strip().upper(), ts, i) for i, n in enumerate(names)])
    return watchlist_get()


def prune(days: int = RETENTION_DAYS) -> int:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM history WHERE ts < datetime('now', ?)", (f"-{days} days",))
        conn.execute(
            "DELETE FROM snapshots WHERE ts < datetime('now', ?)", (f"-{days} days",))
        return cur.rowcount


def last_snapshot() -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


if __name__ == "__main__":
    init()
    with connect() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("initialised", config.DB_PATH)
    print("tables:", tables)
