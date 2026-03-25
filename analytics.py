"""
FastLoop Analytics Engine
==========================
Joins trade records with portfolio outcomes to find what's working and what's not.
Generates specific, data-driven settings recommendations.
Tracks every settings change so you can see what was applied and when.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT        = Path(__file__).parent
DATA_DIR    = ROOT / "data"
CHANGES_FILE = DATA_DIR / "settings_changes.jsonl"
DATABASE_URL = os.environ.get("DATABASE_URL")


# ---------------------------------------------------------------------------
# PostgreSQL helpers for change log
# ---------------------------------------------------------------------------

def _pg_conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    except Exception:
        return None


def _pg_ensure_changes_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fastloop_settings_changes (
                id         SERIAL PRIMARY KEY,
                data       JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


def _pg_log_change(record):
    conn = _pg_conn()
    if not conn:
        return
    try:
        _pg_ensure_changes_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fastloop_settings_changes (data, created_at) VALUES (%s, NOW())",
                (json.dumps(record, default=str),),
            )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _pg_load_changes(limit=100):
    conn = _pg_conn()
    if not conn:
        return None
    try:
        _pg_ensure_changes_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM fastloop_settings_changes ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------

def log_setting_change(param, old_val, new_val, reason, source="manual"):
    record = {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "parameter":  param,
        "old_value":  old_val,
        "new_value":  new_val,
        "reason":     reason,
        "source":     source,  # "manual" | "recommended"
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHANGES_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    _pg_log_change(record)


def get_change_log(limit=50):
    pg = _pg_load_changes(limit)
    if pg is not None:
        return pg
    if not CHANGES_FILE.exists():
        return []
    try:
        lines = CHANGES_FILE.read_text().strip().splitlines()
        return [json.loads(l) for l in reversed(lines[-limit:])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Data joining
# ---------------------------------------------------------------------------

def get_enriched_trades():
    """
    Join DB trade records with portfolio positions by (market, side).
    Returns only records where outcome is known (won/lost).
    """
    try:
        from database import load_trades
        from paper_portfolio import load_portfolio
    except Exception:
        return []

    records   = load_trades(2000)
    portfolio = load_portfolio()
    positions = portfolio.get("positions", [])

    # Build lookup by (market, side) — take the most recent position per key
    pos_lookup = {}
    for p in positions:
        key = (p.get("market"), p.get("side"))
        pos_lookup[key] = p

    enriched = []
    for r in records:
        if r.get("status") not in ("paper", "live"):
            continue
        key    = (r.get("market"), r.get("side"))
        pos    = pos_lookup.get(key)
        status = pos.get("status") if pos else "open"

        enriched.append({
            "market":       r.get("market", ""),
            "side":         r.get("side", "?"),
            "momentum_pct": abs(r.get("momentum_pct") or 0),
            "divergence":   r.get("divergence") or 0,
            "yes_price":    r.get("yes_price") or 0.5,
            "size_usd":     r.get("size_usd") or 0,
            "shares":       r.get("shares") or 0,
            "timestamp":    r.get("timestamp", ""),
            "status":       status,
            "pnl_usd":      pos.get("pnl_usd") or 0 if pos else 0,
            "cost_usd":     pos.get("cost_usd") or r.get("size_usd") or 0,
        })

    return enriched


def _resolved(enriched):
    return [r for r in enriched if r["status"] in ("won", "lost")]


# ---------------------------------------------------------------------------
# Breakdown analysis
# ---------------------------------------------------------------------------

def _bucket_stats(items):
    if not items:
        return {"trades": 0, "won": 0, "lost": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
    won      = [r for r in items if r["status"] == "won"]
    total_pnl = sum(r["pnl_usd"] for r in items)
    return {
        "trades":    len(items),
        "won":       len(won),
        "lost":      len(items) - len(won),
        "win_rate":  round(len(won) / len(items) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl":   round(total_pnl / len(items), 2),
    }


def analyze_by_momentum(enriched):
    resolved = _resolved(enriched)
    buckets  = [
        (0.0,  0.05, "< 0.05%"),
        (0.05, 0.10, "0.05 – 0.10%"),
        (0.10, 0.20, "0.10 – 0.20%"),
        (0.20, 0.50, "0.20 – 0.50%"),
        (0.50, 999,  "> 0.50%"),
    ]
    rows = []
    for lo, hi, label in buckets:
        subset = [r for r in resolved if lo <= r["momentum_pct"] < hi]
        if not subset:
            continue
        row = _bucket_stats(subset)
        row["range"] = label
        rows.append(row)
    return rows


def analyze_by_divergence(enriched):
    resolved = _resolved(enriched)
    buckets  = [
        (0.0,  0.03, "< 0.03"),
        (0.03, 0.06, "0.03 – 0.06"),
        (0.06, 0.10, "0.06 – 0.10"),
        (0.10, 0.20, "0.10 – 0.20"),
        (0.20, 999,  "> 0.20"),
    ]
    rows = []
    for lo, hi, label in buckets:
        subset = [r for r in resolved if lo <= r["divergence"] < hi]
        if not subset:
            continue
        row = _bucket_stats(subset)
        row["range"] = label
        rows.append(row)
    return rows


def analyze_by_side(enriched):
    resolved = _resolved(enriched)
    rows = []
    for side in ("YES", "NO"):
        subset = [r for r in resolved if r["side"] == side]
        if not subset:
            continue
        row = _bucket_stats(subset)
        row["range"] = side
        rows.append(row)
    return rows


def analyze_by_yes_price(enriched):
    resolved = _resolved(enriched)
    buckets  = [
        (0.0,  0.40, "< 0.40 (strong NO)"),
        (0.40, 0.48, "0.40 – 0.48"),
        (0.48, 0.52, "0.48 – 0.52 (near 50¢)"),
        (0.52, 0.60, "0.52 – 0.60"),
        (0.60, 1.01, "> 0.60 (strong YES)"),
    ]
    rows = []
    for lo, hi, label in buckets:
        subset = [r for r in resolved if lo <= r["yes_price"] < hi]
        if not subset:
            continue
        row = _bucket_stats(subset)
        row["range"] = label
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# What-if simulations
# ---------------------------------------------------------------------------

def whatif_momentum(enriched):
    resolved    = _resolved(enriched)
    thresholds  = [0.01, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50]
    total_trades = len(resolved)
    rows = []
    for t in thresholds:
        subset = [r for r in resolved if r["momentum_pct"] >= t]
        if not subset:
            rows.append({"threshold": t, "trades": 0, "filtered_out": total_trades,
                         "win_rate": 0.0, "total_pnl": 0.0})
            continue
        won       = [r for r in subset if r["status"] == "won"]
        total_pnl = sum(r["pnl_usd"] for r in subset)
        rows.append({
            "threshold":    t,
            "trades":       len(subset),
            "filtered_out": total_trades - len(subset),
            "win_rate":     round(len(won) / len(subset) * 100, 1),
            "total_pnl":    round(total_pnl, 2),
        })
    return rows


def whatif_divergence(enriched):
    resolved    = _resolved(enriched)
    thresholds  = [0.01, 0.03, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]
    total_trades = len(resolved)
    rows = []
    for t in thresholds:
        subset = [r for r in resolved if r["divergence"] >= t]
        if not subset:
            rows.append({"threshold": t, "trades": 0, "filtered_out": total_trades,
                         "win_rate": 0.0, "total_pnl": 0.0})
            continue
        won       = [r for r in subset if r["status"] == "won"]
        total_pnl = sum(r["pnl_usd"] for r in subset)
        rows.append({
            "threshold":    t,
            "trades":       len(subset),
            "filtered_out": total_trades - len(subset),
            "win_rate":     round(len(won) / len(subset) * 100, 1),
            "total_pnl":    round(total_pnl, 2),
        })
    return rows


# ---------------------------------------------------------------------------
# Recommendations engine
# ---------------------------------------------------------------------------

def generate_recommendations(enriched, current_config):
    resolved = _resolved(enriched)
    recs     = []

    if len(resolved) < 4:
        return [{"parameter": None, "current": None, "suggested": None,
                 "reason": f"Need at least 4 resolved trades to generate recommendations ({len(resolved)} so far).",
                 "impact": None, "confidence": "low"}]

    current_wr = len([r for r in resolved if r["status"] == "won"]) / len(resolved) * 100

    # ── Momentum threshold ────────────────────────────────────────────────
    best_momentum_wr, best_momentum_t = 0, None
    for t in [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30]:
        subset = [r for r in resolved if r["momentum_pct"] >= t]
        if len(subset) < 3:
            continue
        wr = len([r for r in subset if r["status"] == "won"]) / len(subset) * 100
        if wr > best_momentum_wr:
            best_momentum_wr, best_momentum_t = wr, t

    cur_mom = float(current_config.get("min_momentum_pct", 0.03))
    if best_momentum_t and best_momentum_t != cur_mom and best_momentum_wr > current_wr + 5:
        filtered_count = len([r for r in resolved if r["momentum_pct"] >= best_momentum_t])
        direction = "Raise" if best_momentum_t > cur_mom else "Lower"
        recs.append({
            "parameter": "min_momentum_pct",
            "current":   cur_mom,
            "suggested": best_momentum_t,
            "reason":    f"{direction} momentum threshold from {cur_mom}% to {best_momentum_t}%. "
                         f"Win rate improves from {current_wr:.1f}% → {best_momentum_wr:.1f}% "
                         f"on {filtered_count} qualifying trades.",
            "impact":    f"{filtered_count}/{len(resolved)} resolved trades pass this threshold",
            "confidence": "high" if filtered_count >= 5 else "medium",
        })

    # ── Divergence threshold ──────────────────────────────────────────────
    best_div_wr, best_div_t = 0, None
    for t in [0.01, 0.03, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15]:
        subset = [r for r in resolved if r["divergence"] >= t]
        if len(subset) < 3:
            continue
        wr = len([r for r in subset if r["status"] == "won"]) / len(subset) * 100
        if wr > best_div_wr:
            best_div_wr, best_div_t = wr, t

    cur_div = float(current_config.get("entry_threshold", 0.05))
    if best_div_t and best_div_t != cur_div and best_div_wr > current_wr + 5:
        filtered_count = len([r for r in resolved if r["divergence"] >= best_div_t])
        direction = "Raise" if best_div_t > cur_div else "Lower"
        recs.append({
            "parameter": "entry_threshold",
            "current":   cur_div,
            "suggested": best_div_t,
            "reason":    f"{direction} entry threshold from {cur_div} to {best_div_t}. "
                         f"Win rate improves from {current_wr:.1f}% → {best_div_wr:.1f}%.",
            "impact":    f"{filtered_count}/{len(resolved)} resolved trades pass this threshold",
            "confidence": "high" if filtered_count >= 5 else "medium",
        })

    # ── Side performance ──────────────────────────────────────────────────
    yes_trades = [r for r in resolved if r["side"] == "YES"]
    no_trades  = [r for r in resolved if r["side"] == "NO"]
    if yes_trades and no_trades:
        yes_wr = len([r for r in yes_trades if r["status"] == "won"]) / len(yes_trades) * 100
        no_wr  = len([r for r in no_trades  if r["status"] == "won"]) / len(no_trades) * 100
        gap    = abs(yes_wr - no_wr)
        if gap >= 20:
            weak_side   = "YES" if yes_wr < no_wr else "NO"
            strong_wr   = max(yes_wr, no_wr)
            weak_wr     = min(yes_wr, no_wr)
            recs.append({
                "parameter": "side_filter",
                "current":   "both sides",
                "suggested": f"{weak_side} trades need tighter filters",
                "reason":    f"{weak_side} win rate ({weak_wr:.1f}%) vs "
                             f"{'NO' if weak_side == 'YES' else 'YES'} ({strong_wr:.1f}%). "
                             f"Consider raising entry_threshold for {weak_side} trades specifically.",
                "impact":    f"Tightening {weak_side} filters could improve overall win rate by ~{gap/2:.1f}%",
                "confidence": "medium",
            })

    # ── YES price range ───────────────────────────────────────────────────
    near50 = [r for r in resolved if 0.45 <= r["yes_price"] <= 0.55]
    far50  = [r for r in resolved if r["yes_price"] < 0.40 or r["yes_price"] > 0.60]
    if near50 and far50 and len(near50) >= 3 and len(far50) >= 3:
        near_wr = len([r for r in near50 if r["status"] == "won"]) / len(near50) * 100
        far_wr  = len([r for r in far50  if r["status"] == "won"]) / len(far50)  * 100
        if far_wr > near_wr + 15:
            recs.append({
                "parameter": "entry_threshold",
                "current":   cur_div,
                "suggested": round(cur_div + 0.02, 2),
                "reason":    f"Trades where YES is far from 50¢ win {far_wr:.1f}% vs {near_wr:.1f}% near 50¢. "
                             f"A higher entry_threshold naturally filters out the near-50¢ trades.",
                "impact":    f"Targets trades where market has stronger mismatch signal",
                "confidence": "medium",
            })

    if not recs:
        recs.append({
            "parameter": None, "current": None, "suggested": None,
            "reason":    f"Current settings are performing well (win rate: {current_wr:.1f}%). "
                         "Collect more trades for sharper analysis.",
            "impact":    None, "confidence": "high",
        })

    return recs


# ---------------------------------------------------------------------------
# Full analysis bundle
# ---------------------------------------------------------------------------

def get_full_analysis():
    enriched = get_enriched_trades()
    resolved = _resolved(enriched)

    try:
        config_file = ROOT / "config.json"
        config = json.loads(config_file.read_text()) if config_file.exists() else {}
    except Exception:
        config = {}

    total_wr = round(
        len([r for r in resolved if r["status"] == "won"]) / len(resolved) * 100, 1
    ) if resolved else 0.0

    return {
        "total_enriched":     len(enriched),
        "total_resolved":     len(resolved),
        "overall_win_rate":   total_wr,
        "by_momentum":        analyze_by_momentum(enriched),
        "by_divergence":      analyze_by_divergence(enriched),
        "by_side":            analyze_by_side(enriched),
        "by_yes_price":       analyze_by_yes_price(enriched),
        "whatif_momentum":    whatif_momentum(enriched),
        "whatif_divergence":  whatif_divergence(enriched),
        "recommendations":    generate_recommendations(enriched, config),
        "change_log":         get_change_log(20),
        "current_config":     config,
    }
