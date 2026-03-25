"""
FastLoop Trading — Live Dashboard
Runs as a Flask web server on PORT (Railway injects this).
Reads trade records from database.py (PostgreSQL or local JSONL fallback).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

ROOT        = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>FastLoop Trader</title>
  <meta http-equiv="refresh" content="60">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: monospace; background: #0d1117; color: #e6edf3; padding: 20px; }
    h1   { color: #58a6ff; margin-bottom: 6px; font-size: 1.4em; }
    .sub { color: #8b949e; font-size: 0.85em; margin-bottom: 20px; }
    .sub a { color: #58a6ff; text-decoration: none; }
    .stats { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
    .card  { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
             padding: 14px 20px; min-width: 140px; }
    .card .label { color: #8b949e; font-size: 0.78em; text-transform: uppercase; letter-spacing: 1px; }
    .card .value { font-size: 1.6em; font-weight: bold; margin-top: 4px; }
    .green  { color: #3fb950; }
    .red    { color: #f85149; }
    .yellow { color: #d29922; }
    .blue   { color: #58a6ff; }
    .grey   { color: #8b949e; }
    table { width: 100%; border-collapse: collapse; font-size: 0.82em; }
    th { background: #161b22; color: #8b949e; text-align: left;
         padding: 8px 12px; border-bottom: 1px solid #30363d; font-size: 0.78em; text-transform: uppercase; }
    td { padding: 7px 12px; border-bottom: 1px solid #21262d; vertical-align: top; }
    tr:hover td { background: #161b22; }
    .badge { border-radius: 4px; padding: 2px 7px; font-size: 0.75em; font-weight: bold; display: inline-block; }
    .badge-paper { background: #1f3a5f; color: #58a6ff; }
    .badge-live  { background: #3d1a1a; color: #f85149; }
    .badge-skip  { background: #252a30; color: #8b949e; }
    .section { color: #8b949e; font-size: 0.78em; text-transform: uppercase;
               letter-spacing: 1px; margin: 28px 0 12px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
  </style>
</head>
<body>
  <h1>⚡ FastLoop Trader</h1>
  <div class="sub">
    Auto-refreshes every 60s &nbsp;|&nbsp;
    Last updated: {{ now }} &nbsp;|&nbsp;
    <a href="/settings">Settings</a> &nbsp;|&nbsp;
    <a href="/api/trades">API</a>
  </div>

  <!-- Stats row -->
  <div class="stats">
    <div class="card">
      <div class="label">Mode</div>
      <div class="value">
        <span class="badge {{ 'badge-live' if mode == 'live' else 'badge-paper' }}">
          {{ mode.upper() }}
        </span>
      </div>
    </div>
    <div class="card">
      <div class="label">Total Runs</div>
      <div class="value blue">{{ stats.total_runs }}</div>
    </div>
    <div class="card">
      <div class="label">Trades</div>
      <div class="value green">{{ stats.total_trades }}</div>
    </div>
    <div class="card">
      <div class="label">Skipped</div>
      <div class="value grey">{{ stats.total_skips }}</div>
    </div>
    <div class="card">
      <div class="label">Trade Rate</div>
      <div class="value {{ 'green' if stats.trade_rate >= 30 else 'yellow' }}">{{ stats.trade_rate }}%</div>
    </div>
    <div class="card">
      <div class="label">Runs Today</div>
      <div class="value">{{ stats.runs_today }}</div>
    </div>
    <div class="card">
      <div class="label">Daily Spent</div>
      <div class="value blue">${{ "%.2f"|format(stats.daily_spent) }}</div>
    </div>
    <div class="card">
      <div class="label">Live Trades</div>
      <div class="value {{ 'green' if stats.live_trades > 0 else 'grey' }}">{{ stats.live_trades }}</div>
    </div>
  </div>

  <!-- Trade Log -->
  <div class="section">Recent Cycles (last 200)</div>
  {% if records %}
  <table>
    <thead>
      <tr>
        <th>Time (UTC)</th>
        <th>Asset</th>
        <th>Status</th>
        <th>Side</th>
        <th>YES Price</th>
        <th>Momentum %</th>
        <th>Divergence</th>
        <th>Size $</th>
        <th>Shares</th>
        <th>Source</th>
        <th>Reason</th>
        <th>Expires</th>
        <th>Market</th>
      </tr>
    </thead>
    <tbody>
    {% for r in records %}
      <tr>
        <td>{{ r.timestamp[:16].replace("T", " ") }}</td>
        <td>{{ r.asset }}</td>
        <td>
          <span class="badge badge-{{ r.status }}">{{ r.status.upper() }}</span>
        </td>
        <td class="{{ 'green' if r.get('side') == 'YES' else ('red' if r.get('side') == 'NO' else '') }}">
          {{ r.get("side", "—") }}
        </td>
        <td>{{ "%.3f"|format(r.yes_price) if r.yes_price is not none else "—" }}</td>
        <td class="{{ 'green' if r.get('momentum_pct', 0) >= 0 else 'red' }}">
          {{ "%+.3f"|format(r.momentum_pct) if r.momentum_pct is not none else "—" }}
        </td>
        <td>{{ "%.3f"|format(r.divergence) if r.divergence is not none else "—" }}</td>
        <td class="{{ 'blue' if r.get('size_usd') else '' }}">
          {{ "$%.2f"|format(r.size_usd) if r.get("size_usd") else "—" }}
        </td>
        <td>{{ "%.1f"|format(r.shares) if r.get("shares") else "—" }}</td>
        <td class="grey">{{ r.get("source_used", "—") }}</td>
        <td class="grey" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="{{ r.get('reason', '') }}">
          {{ r.get("reason", "—")[:50] }}{% if r.get("reason", "")|length > 50 %}…{% endif %}
        </td>
        <td class="{{ 'red' if r.get('seconds_to_expiry', 999) < 60 else '' }}">
          {{ r.seconds_to_expiry ~ "s" if r.get("seconds_to_expiry") is not none else "—" }}
        </td>
        <td class="grey" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="{{ r.get('market', '') }}">
          {{ r.get("market", "—")[:45] }}{% if r.get("market", "")|length > 45 %}…{% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p style="color:#8b949e; padding:20px 0;">
      No trade records yet — waiting for the first cron cycle to complete.
    </p>
  {% endif %}
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_stats():
    try:
        from database import get_stats
        return get_stats()
    except Exception as e:
        print(f"  [WARN] Failed to load stats: {e}")
        return {
            "total_runs": 0, "total_trades": 0, "live_trades": 0,
            "paper_trades": 0, "total_skips": 0, "trade_rate": 0.0,
            "daily_spent": 0.0, "runs_today": 0, "records": [],
        }


def _load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    stats  = _load_stats()
    mode   = os.environ.get("TRADING_MODE", "paper")

    # Normalise record fields for template (None-safe)
    records = []
    for r in stats.get("records", []):
        records.append({
            "timestamp":        r.get("timestamp", ""),
            "asset":            r.get("asset", "?"),
            "status":           r.get("status", "skip"),
            "side":             r.get("side"),
            "yes_price":        r.get("yes_price"),
            "momentum_pct":     r.get("momentum_pct"),
            "divergence":       r.get("divergence"),
            "size_usd":         r.get("size_usd"),
            "shares":           r.get("shares"),
            "source_used":      r.get("source_used"),
            "reason":           r.get("reason", ""),
            "seconds_to_expiry":r.get("seconds_to_expiry"),
            "market":           r.get("market", ""),
        })

    # Convert to a simple namespace-like object for Jinja `r.key` access
    class Row(dict):
        def __getattr__(self, k):
            return self.get(k)

    records = [Row(r) for r in records]

    class Stats(dict):
        def __getattr__(self, k):
            return self.get(k, 0)

    return render_template_string(
        TEMPLATE,
        stats   = Stats(stats),
        records = records,
        mode    = mode,
        now     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.route("/api/trades")
def api_trades():
    try:
        from database import load_trades
        return jsonify(load_trades(500))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/settings")
def settings_page():
    cfg = _load_config()

    labels = {
        "entry_threshold":   ("Entry Threshold",   "Min price divergence from 50¢ to trigger a trade"),
        "min_momentum_pct":  ("Min Momentum %",    "Minimum BTC/ETH/SOL % move required"),
        "max_position":      ("Max Position $",    "Maximum USD per trade"),
        "signal_source":     ("Signal Source",     "CEX price feed (binance or coingecko)"),
        "lookback_minutes":  ("Lookback Minutes",  "Price history window for momentum calculation"),
        "min_time_remaining":("Min Time Remaining","Skip markets with less than this many seconds left"),
        "asset":             ("Asset",             "Asset to trade: BTC, ETH, or SOL"),
        "window":            ("Market Window",     "Fast market duration: 5m or 15m"),
        "volume_confidence": ("Volume Confidence", "Weight signal by Binance volume ratio"),
        "daily_budget":      ("Daily Budget $",    "Maximum USD spend per calendar day (UTC)"),
    }

    rows = ""
    for key, (label, hint) in labels.items():
        val = cfg.get(key, "—")
        rows += f"<tr><td>{label}</td><td class='val'>{val}</td><td class='hint'>{hint}</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>FastLoop Settings</title>
    <style>
      body{{font-family:monospace;background:#0d1117;color:#e6edf3;padding:20px;}}
      h1{{color:#58a6ff;margin-bottom:4px;}} .sub{{color:#8b949e;font-size:.85em;margin-bottom:20px;}}
      a{{color:#58a6ff;text-decoration:none;}}
      table{{border-collapse:collapse;width:100%;max-width:820px;}}
      th{{background:#161b22;color:#8b949e;text-align:left;padding:8px 14px;
          border-bottom:1px solid #30363d;font-size:.78em;text-transform:uppercase;}}
      td{{padding:8px 14px;border-bottom:1px solid #21262d;vertical-align:top;}}
      .val{{color:#3fb950;font-weight:bold;}} .hint{{color:#8b949e;font-size:.82em;}}
    </style></head><body>
    <h1>⚡ FastLoop Settings</h1>
    <div class="sub"><a href="/">← Back to dashboard</a> &nbsp;|&nbsp;
    Override any value via Railway Variables or <code>--set KEY=VALUE</code>.</div>
    <table><thead><tr><th>Setting</th><th>Current Value</th><th>Description</th></tr></thead>
    <tbody>{rows}</tbody></table>
    </body></html>"""


@app.route("/health")
def health():
    return "ok", 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def start_dashboard():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_dashboard()
