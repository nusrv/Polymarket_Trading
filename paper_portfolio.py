"""
FastLoop Paper Portfolio Tracker
=================================
Simulates a $50 starting balance trading FastLoop paper trades.
Resolves positions via Polymarket Gamma API (same approach as AI Bot).

Persistence backends (priority order):
  1. PostgreSQL — when DATABASE_URL env var is set (Railway managed DB)
  2. Local JSON  — data/paper_portfolio.json (fallback / local dev)

Position lifecycle: open → won | lost | expired
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

ROOT           = Path(__file__).parent
PORTFOLIO_FILE = ROOT / "data" / "paper_portfolio.json"
GAMMA_API      = "https://gamma-api.polymarket.com"

STARTING_BALANCE = float(os.environ.get("PAPER_BALANCE", "50.0"))
DATABASE_URL     = os.environ.get("DATABASE_URL")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url, timeout=8):
    req = Request(url, headers={"User-Agent": "fastloop-trader/1.0"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


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
        print(f"  [WARN] Portfolio PG connection failed: {e}")
        return None


def _pg_ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fastloop_portfolio (
                id         INTEGER PRIMARY KEY,
                data       JSONB NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


def _pg_load():
    conn = _pg_conn()
    if not conn:
        return None
    try:
        _pg_ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM fastloop_portfolio WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"  [WARN] Portfolio PG load failed: {e}")
        return None
    finally:
        conn.close()


def _pg_save(data):
    conn = _pg_conn()
    if not conn:
        return False
    try:
        _pg_ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fastloop_portfolio (id, data, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE
                    SET data = EXCLUDED.data, updated_at = NOW()
                """,
                (json.dumps(data, default=str),),
            )
        conn.commit()
        return True
    except Exception as e:
        print(f"  [WARN] Portfolio PG save failed: {e}")
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_portfolio():
    pg = _pg_load()
    if pg is not None:
        return pg
    if PORTFOLIO_FILE.exists():
        try:
            return json.loads(PORTFOLIO_FILE.read_text())
        except Exception:
            pass
    data = {
        "starting_balance": STARTING_BALANCE,
        "positions":        [],
        "resolved_pnl_usd": 0.0,
        "created_at":       datetime.now(timezone.utc).isoformat(),
    }
    save_portfolio(data)
    return data


def save_portfolio(data):
    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_FILE.write_text(json.dumps(data, indent=2, default=str))
    _pg_save(data)


# ---------------------------------------------------------------------------
# Add position
# ---------------------------------------------------------------------------

def add_position(market, side, yes_token_id, entry_yes_price, shares, cost_usd, end_time_iso):
    """
    Record a paper trade position.
    Called after a successful paper or live trade in fastloop_trader.py.
    """
    portfolio = load_portfolio()

    # Deduplicate by market + side (don't double-record same cycle)
    for p in portfolio["positions"]:
        if p["market"] == market and p["side"] == side and p["status"] == "open":
            return

    entry_price = entry_yes_price if side == "YES" else round(1.0 - entry_yes_price, 4)

    portfolio["positions"].append({
        "market":          market,
        "side":            side,
        "yes_token_id":    yes_token_id,
        "entry_price":     entry_price,
        "entry_yes_price": entry_yes_price,
        "shares":          round(shares, 4),
        "cost_usd":        round(cost_usd, 4),
        "entered_at":      datetime.now(timezone.utc).isoformat(),
        "end_time":        end_time_iso,
        "status":          "open",
        "pnl_usd":         None,
        "resolved_at":     None,
    })
    save_portfolio(portfolio)


# ---------------------------------------------------------------------------
# Resolution check via Gamma API
# ---------------------------------------------------------------------------

def _check_resolution(yes_token_id):
    """
    Returns 1.0 if YES won, 0.0 if NO won, None if not yet resolved.
    """
    if not yes_token_id:
        return None
    data = _get(f"{GAMMA_API}/markets?clob_token_ids={yes_token_id}&limit=5")
    if not data:
        return None

    markets = data if isinstance(data, list) else data.get("markets", [])
    for m in markets:
        is_closed = (
            m.get("closed") or m.get("resolved")
            or m.get("resolution")
            or m.get("umaResolutionStatus") == "resolved"
        )
        if not is_closed:
            continue

        raw = m.get("clobTokenIds") or m.get("clob_token_ids") or "[]"
        if isinstance(raw, str):
            try:
                tokens = json.loads(raw)
            except Exception:
                tokens = []
        else:
            tokens = list(raw)

        if yes_token_id not in tokens:
            continue

        outcome_raw = m.get("outcomePrices")
        if outcome_raw:
            try:
                prices = json.loads(outcome_raw) if isinstance(outcome_raw, str) else outcome_raw
                floats = [float(p) for p in prices]
                if max(floats) < 0.5:
                    return None   # not yet resolved
                return float(floats[0])   # YES token outcome (1.0=YES won, 0.0=NO won)
            except Exception:
                pass

        res = m.get("resolution")
        if res in (1, "1", 1.0):
            return 1.0

    return None


def _lookup_token_by_question(question):
    """
    Find YES CLOB token ID from Gamma API by market question text.
    Used when Simmer SDK markets don't include polymarket_token_id.
    Tries multiple query strategies.
    """
    if not question:
        return None

    def _extract_token(markets):
        for m in markets:
            if m.get("question", "").strip().lower() != question.strip().lower():
                continue
            raw = m.get("clobTokenIds") or m.get("clob_token_ids") or "[]"
            if isinstance(raw, str):
                try:
                    tokens = json.loads(raw)
                except Exception:
                    tokens = []
            else:
                tokens = list(raw)
            if tokens:
                return tokens[0]
        return None

    # Strategy 1: exact question filter
    data = _get(f"{GAMMA_API}/markets?question={quote(question)}&limit=10")
    if data:
        markets = data if isinstance(data, list) else data.get("markets", [])
        result = _extract_token(markets)
        if result:
            return result

    # Strategy 2: keyword search (first 40 chars avoids URL length issues)
    keywords = quote(question[:40])
    data = _get(f"{GAMMA_API}/markets?q={keywords}&closed=true&limit=50")
    if data:
        markets = data if isinstance(data, list) else data.get("markets", [])
        result = _extract_token(markets)
        if result:
            return result

    # Strategy 3: fetch recent closed crypto markets and match locally
    data = _get(f"{GAMMA_API}/markets?tag=crypto&closed=true&order=endDate&ascending=false&limit=200")
    if data:
        markets = data if isinstance(data, list) else data.get("markets", [])
        result = _extract_token(markets)
        if result:
            return result

    return None


# ---------------------------------------------------------------------------
# Refresh open positions
# ---------------------------------------------------------------------------

def refresh_positions():
    """
    Check resolution for all expired open positions.
    Call this at dashboard load or on each bot cycle.
    """
    portfolio = load_portfolio()
    now       = datetime.now(timezone.utc)
    changed   = False

    for pos in portfolio["positions"]:
        # Re-examine open positions and previously-unresolved expired ones
        if pos["status"] == "open":
            # Check if end_time has passed
            end_time_str = pos.get("end_time")
            if end_time_str:
                try:
                    end_dt = datetime.fromisoformat(str(end_time_str).replace("Z", "+00:00"))
                    if end_dt > now:
                        continue   # still live
                except Exception:
                    pass
        elif pos["status"] == "expired" and pos.get("pnl_usd") == 0.0:
            pass  # previously stuck — retry resolution
        else:
            continue

        # Market expired — query resolution
        yes_token_id = pos.get("yes_token_id")
        if not yes_token_id:
            yes_token_id = _lookup_token_by_question(pos.get("market", ""))
            if yes_token_id:
                pos["yes_token_id"] = yes_token_id  # cache so future cycles skip the lookup
                changed = True
        yes_resolution = _check_resolution(yes_token_id)

        if yes_resolution is not None:
            # yes_resolution: 1.0 = YES won, 0.0 = NO won
            our_side_won = (pos["side"] == "YES" and yes_resolution == 1.0) or \
                           (pos["side"] == "NO"  and yes_resolution == 0.0)

            if our_side_won:
                pnl = round(pos["shares"] * 1.0 - pos["cost_usd"], 4)
                pos["status"] = "won"
            else:
                pnl = round(-pos["cost_usd"], 4)
                pos["status"] = "lost"

            prev_pnl = pos.get("pnl_usd") or 0.0  # subtract previously recorded value (e.g. 0.0 from expired)
            pos["pnl_usd"]    = pnl
            pos["resolved_at"] = now.isoformat()
            portfolio["resolved_pnl_usd"] = round(
                portfolio.get("resolved_pnl_usd", 0.0) + pnl - prev_pnl, 4
            )
        else:
            # Expired but resolution not available yet — mark expired
            pos["status"]  = "expired"
            pos["pnl_usd"] = 0.0
            pos["resolved_at"] = now.isoformat()

        changed = True

    if changed:
        save_portfolio(portfolio)

    return portfolio


# ---------------------------------------------------------------------------
# Summary stats for dashboard
# ---------------------------------------------------------------------------

def get_summary():
    portfolio = refresh_positions()
    positions = portfolio.get("positions", [])

    open_pos    = [p for p in positions if p["status"] == "open"]
    won_pos     = [p for p in positions if p["status"] == "won"]
    lost_pos    = [p for p in positions if p["status"] == "lost"]
    expired_pos = [p for p in positions if p["status"] == "expired"]

    total_invested  = sum(p["cost_usd"] for p in positions)
    won_payout      = sum(p["shares"] for p in won_pos)      # $1/share
    resolved_pnl    = portfolio.get("resolved_pnl_usd", 0.0)
    open_cost       = sum(p["cost_usd"] for p in open_pos)

    # Portfolio value = cash not yet deployed + payouts from wins + open cost at risk
    # Simplified: starting_balance + resolved_pnl - open_cost_at_risk + open_cost
    portfolio_value = STARTING_BALANCE + resolved_pnl
    return_pct = round(resolved_pnl / STARTING_BALANCE * 100, 2) if STARTING_BALANCE else 0.0

    win_rate = round(len(won_pos) / (len(won_pos) + len(lost_pos)) * 100, 1) \
               if (won_pos or lost_pos) else 0.0

    return {
        "starting_balance": portfolio.get("starting_balance", STARTING_BALANCE),
        "total_invested":   round(total_invested, 2),
        "open_cost":        round(open_cost, 2),
        "open_count":       len(open_pos),
        "won_count":        len(won_pos),
        "lost_count":       len(lost_pos),
        "expired_count":    len(expired_pos),
        "resolved_pnl":     round(resolved_pnl, 2),
        "portfolio_value":  round(portfolio_value, 2),
        "return_pct":       return_pct,
        "win_rate":         win_rate,
        "positions":        list(reversed(positions[-80:])),
    }


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def reset_portfolio(starting_balance=None):
    balance = starting_balance or STARTING_BALANCE
    data = {
        "starting_balance": balance,
        "positions":        [],
        "resolved_pnl_usd": 0.0,
        "created_at":       datetime.now(timezone.utc).isoformat(),
    }
    save_portfolio(data)
    print(f"  [PAPER] Portfolio reset. Starting balance: ${balance:.2f}")
    return data


if __name__ == "__main__":
    s = get_summary()
    print(f"\nFastLoop Paper Portfolio")
    print(f"  Starting balance: ${s['starting_balance']:.2f}")
    print(f"  Total invested:   ${s['total_invested']:.2f}")
    print(f"  Open positions:   {s['open_count']}")
    print(f"  Won:              {s['won_count']}")
    print(f"  Lost:             {s['lost_count']}")
    print(f"  Realized P&L:     ${s['resolved_pnl']:+.2f}")
    print(f"  Portfolio value:  ${s['portfolio_value']:.2f}")
    print(f"  Return:           {s['return_pct']:+.2f}%")
    print(f"  Win rate:         {s['win_rate']:.1f}%")
