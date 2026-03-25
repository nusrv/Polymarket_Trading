"""
FastLoop Trade Database
=======================
Persists every trade/skip cycle record.

Backends (priority order):
  1. PostgreSQL — when DATABASE_URL env var is set (Railway managed DB)
  2. Local JSONL — data/trades.jsonl (fallback / local dev)

Schema:
  fastloop_trades(id SERIAL, data JSONB, created_at TIMESTAMPTZ)
  One row per bot cycle (paper trade, live trade, or skip).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT        = Path(__file__).parent
DATA_DIR    = ROOT / "data"
TRADES_FILE = DATA_DIR / "trades.jsonl"

DATABASE_URL = os.environ.get("DATABASE_URL")

_BACKEND = "PostgreSQL" if DATABASE_URL else "local JSONL file"
print(f"  [DB] Persistence backend: {_BACKEND}")


# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------

def _pg_conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"  [WARN] PostgreSQL connection failed: {e}")
        return None


def _pg_ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fastloop_trades (
                id         SERIAL PRIMARY KEY,
                data       JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS fastloop_trades_created_idx
            ON fastloop_trades (created_at DESC)
        """)
    conn.commit()


def _pg_insert(record):
    conn = _pg_conn()
    if not conn:
        return False
    try:
        _pg_ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fastloop_trades (data, created_at) VALUES (%s, NOW())",
                (json.dumps(record, default=str),),
            )
        conn.commit()
        return True
    except Exception as e:
        print(f"  [WARN] PostgreSQL insert failed: {e}")
        return False
    finally:
        conn.close()


def _pg_load(limit=500):
    conn = _pg_conn()
    if not conn:
        return None
    try:
        _pg_ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM fastloop_trades ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        print(f"  [WARN] PostgreSQL load failed: {e}")
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# JSONL fallback helpers
# ---------------------------------------------------------------------------

def _jsonl_append(record):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _jsonl_load(limit=500):
    if not TRADES_FILE.exists():
        return []
    try:
        lines = TRADES_FILE.read_text(encoding="utf-8").strip().splitlines()
        rows = []
        for line in reversed(lines[-limit:]):
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_trade(record):
    """Persist one cycle record. Called from fastloop_trader.emit_console_record."""
    _jsonl_append(record)   # always write local file (cache + fallback)
    _pg_insert(record)      # also write PostgreSQL if configured


def load_trades(limit=500):
    """Load recent trade records, newest first."""
    pg = _pg_load(limit)
    if pg is not None:
        return pg
    return _jsonl_load(limit)


def get_stats():
    """Compute summary stats for the dashboard."""
    records = load_trades(1000)
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    total       = len(records)
    trades      = [r for r in records if r.get("status") in ("paper", "live")]
    live_trades = [r for r in records if r.get("status") == "live"]
    skips       = [r for r in records if r.get("status") == "skip"]

    today_live  = [
        r for r in live_trades
        if r.get("timestamp", "").startswith(today)
    ]
    daily_spent = sum(r.get("size_usd", 0) for r in today_live)

    # Runs today = distinct timestamps with today's date prefix
    today_records   = [r for r in records if r.get("timestamp", "").startswith(today)]
    runs_today      = len(today_records)

    trade_rate = round(len(trades) / total * 100, 1) if total else 0.0

    return {
        "total_runs":   total,
        "total_trades": len(trades),
        "live_trades":  len(live_trades),
        "paper_trades": len(trades) - len(live_trades),
        "total_skips":  len(skips),
        "trade_rate":   trade_rate,
        "daily_spent":  round(daily_spent, 2),
        "runs_today":   runs_today,
        "records":      records[:200],
    }
