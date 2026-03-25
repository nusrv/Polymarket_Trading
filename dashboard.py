"""
FastLoop Trading — Live Dashboard
Runs as a persistent Flask web server (PORT injected by Railway).
Reads from PostgreSQL via database.py and paper_portfolio.py.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request, redirect, url_for

app = Flask(__name__)

ROOT        = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"

# ---------------------------------------------------------------------------
# CSS (shared across pages)
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; background: #0d1117; color: #e6edf3; padding: 20px; font-size: 14px; }
h1   { color: #58a6ff; margin-bottom: 6px; font-size: 1.4em; }
h2   { color: #8b949e; font-size: 0.85em; text-transform: uppercase; letter-spacing: 1px;
       margin: 28px 0 12px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
.sub { color: #8b949e; font-size: 0.85em; margin-bottom: 20px; }
.sub a { color: #58a6ff; text-decoration: none; }
.sub a:hover { text-decoration: underline; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 8px; }
.card  { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
         padding: 14px 20px; min-width: 130px; }
.card .label { color: #8b949e; font-size: 0.75em; text-transform: uppercase; letter-spacing: 1px; }
.card .value { font-size: 1.55em; font-weight: bold; margin-top: 5px; }
.green  { color: #3fb950; }
.red    { color: #f85149; }
.yellow { color: #d29922; }
.blue   { color: #58a6ff; }
.grey   { color: #8b949e; }
.white  { color: #e6edf3; }
table { width: 100%; border-collapse: collapse; font-size: 0.82em; }
th { background: #161b22; color: #8b949e; text-align: left;
     padding: 8px 12px; border-bottom: 1px solid #30363d; font-size: 0.78em; text-transform: uppercase; white-space: nowrap; }
td { padding: 7px 12px; border-bottom: 1px solid #21262d; vertical-align: top; }
tr:hover td { background: #161b22; }
.badge { border-radius: 4px; padding: 2px 7px; font-size: 0.75em; font-weight: bold; display: inline-block; white-space: nowrap; }
.badge-paper   { background: #1f3a5f; color: #58a6ff; }
.badge-live    { background: #3d1a1a; color: #f85149; }
.badge-skip    { background: #252a30; color: #8b949e; }
.badge-yes     { background: #1a3d1a; color: #3fb950; }
.badge-no      { background: #3d1a1a; color: #f85149; }
.badge-open    { background: #1f3a5f; color: #58a6ff; }
.badge-won     { background: #1a3d1a; color: #3fb950; }
.badge-lost    { background: #3d1a1a; color: #f85149; }
.badge-expired { background: #252a30; color: #8b949e; }
.pnl-pos { color: #3fb950; font-weight: bold; }
.pnl-neg { color: #f85149; font-weight: bold; }
.ellipsis { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
"""

NAV = """
<h1>&#9889; FastLoop Trader</h1>
<div class="sub">
  Auto-refreshes every 60s &nbsp;|&nbsp; Last updated: {now} &nbsp;|&nbsp;
  <a href="/">Dashboard</a> &nbsp;|&nbsp;
  <a href="/portfolio">Portfolio</a> &nbsp;|&nbsp;
  <a href="/results">Results Log</a> &nbsp;|&nbsp;
  <a href="/settings">Settings</a> &nbsp;|&nbsp;
  <a href="/api/trades" target="_blank">API</a>
</div>
"""

# ---------------------------------------------------------------------------
# Main dashboard template
# ---------------------------------------------------------------------------

MAIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>FastLoop Trader</title>
  <meta http-equiv="refresh" content="60">
  <style>{{ css | safe }}</style>
</head>
<body>
  {{ nav | safe }}

  <!-- ── BOT STATS ─────────────────────────── -->
  <h2>Bot Activity</h2>
  <div class="cards">
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
      <div class="value {{ 'green' if stats.trade_rate >= 30 else 'yellow' }}">
        {{ stats.trade_rate }}%
      </div>
    </div>
    <div class="card">
      <div class="label">Runs Today</div>
      <div class="value">{{ stats.runs_today }}</div>
    </div>
    <div class="card">
      <div class="label">Daily Spent</div>
      <div class="value blue">${{ "%.2f"|format(stats.daily_spent) }}</div>
    </div>
  </div>

  <!-- ── PAPER PORTFOLIO SUMMARY ───────────── -->
  <h2>Paper Portfolio — ${{ "%.0f"|format(pf.starting_balance) }} Starting Balance</h2>
  <div class="cards">
    <div class="card">
      <div class="label">Portfolio Value</div>
      <div class="value {{ 'green' if pf.portfolio_value >= pf.starting_balance else 'red' }}">
        ${{ "%.2f"|format(pf.portfolio_value) }}
      </div>
    </div>
    <div class="card">
      <div class="label">Return</div>
      <div class="value {{ 'green' if pf.return_pct >= 0 else 'red' }}">
        {{ "%+.2f"|format(pf.return_pct) }}%
      </div>
    </div>
    <div class="card">
      <div class="label">Realized P&L</div>
      <div class="value {{ 'pnl-pos' if pf.resolved_pnl >= 0 else 'pnl-neg' }}">
        ${{ "%+.2f"|format(pf.resolved_pnl) }}
      </div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value {{ 'green' if pf.win_rate >= 50 else 'red' }}">
        {{ "%.1f"|format(pf.win_rate) }}%
      </div>
    </div>
    <div class="card">
      <div class="label">Won / Lost</div>
      <div class="value">
        <span class="green">{{ pf.won_count }}</span> /
        <span class="red">{{ pf.lost_count }}</span>
      </div>
    </div>
    <div class="card">
      <div class="label">Open</div>
      <div class="value blue">{{ pf.open_count }}</div>
    </div>
    <div class="card">
      <div class="label">Total Invested</div>
      <div class="value grey">${{ "%.2f"|format(pf.total_invested) }}</div>
    </div>
  </div>

  <!-- ── OPEN POSITIONS ────────────────────── -->
  {% if open_positions %}
  <h2>Open Positions ({{ open_positions|length }})</h2>
  <table>
    <thead>
      <tr>
        <th>Market</th>
        <th>Side</th>
        <th>Entry Price</th>
        <th>Shares</th>
        <th>Cost</th>
        <th>Entered</th>
        <th>Expires</th>
      </tr>
    </thead>
    <tbody>
    {% for p in open_positions %}
      <tr>
        <td class="ellipsis" title="{{ p.market }}">{{ p.market[:55] }}{% if p.market|length > 55 %}…{% endif %}</td>
        <td><span class="badge badge-{{ p.side.lower() }}">{{ p.side }}</span></td>
        <td>${{ "%.3f"|format(p.entry_price) }}</td>
        <td>{{ "%.1f"|format(p.shares) }}</td>
        <td class="blue">${{ "%.2f"|format(p.cost_usd) }}</td>
        <td class="grey">{{ p.entered_at[:16].replace("T"," ") }}</td>
        <td class="grey">{{ p.end_time[:16].replace("T"," ") if p.end_time else "—" }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <!-- ── RECENT RESOLVED ───────────────────── -->
  {% if resolved_positions %}
  <h2>Recent Resolved Positions</h2>
  <table>
    <thead>
      <tr>
        <th>Market</th>
        <th>Side</th>
        <th>Entry</th>
        <th>Shares</th>
        <th>Cost</th>
        <th>P&L</th>
        <th>Result</th>
        <th>Resolved</th>
      </tr>
    </thead>
    <tbody>
    {% for p in resolved_positions %}
      {% set pnl = p.pnl_usd or 0 %}
      <tr>
        <td class="ellipsis" title="{{ p.market }}">{{ p.market[:55] }}{% if p.market|length > 55 %}…{% endif %}</td>
        <td><span class="badge badge-{{ p.side.lower() }}">{{ p.side }}</span></td>
        <td>${{ "%.3f"|format(p.entry_price) }}</td>
        <td>{{ "%.1f"|format(p.shares) }}</td>
        <td>${{ "%.2f"|format(p.cost_usd) }}</td>
        <td class="{{ 'pnl-pos' if pnl >= 0 else 'pnl-neg' }}">${{ "%+.2f"|format(pnl) }}</td>
        <td><span class="badge badge-{{ p.status }}">{{ p.status.upper() }}</span></td>
        <td class="grey">{{ (p.resolved_at or "")[:16].replace("T"," ") }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <!-- ── CYCLE LOG ─────────────────────────── -->
  <h2>Cycle Log (last 200 runs)</h2>
  {% if records %}
  <table>
    <thead>
      <tr>
        <th>Time (UTC)</th>
        <th>Asset</th>
        <th>Status</th>
        <th>Side</th>
        <th>YES Price</th>
        <th>Momentum%</th>
        <th>Divergence</th>
        <th>Size</th>
        <th>Shares</th>
        <th>Expires</th>
        <th>Reason / Market</th>
      </tr>
    </thead>
    <tbody>
    {% for r in records %}
      <tr>
        <td class="grey">{{ r.timestamp[:16].replace("T"," ") }}</td>
        <td>{{ r.asset }}</td>
        <td><span class="badge badge-{{ r.status }}">{{ r.status.upper() }}</span></td>
        <td>
          {% if r.side %}<span class="badge badge-{{ r.side.lower() }}">{{ r.side }}</span>{% else %}—{% endif %}
        </td>
        <td>{{ "$%.3f"|format(r.yes_price) if r.yes_price is not none else "—" }}</td>
        <td class="{{ 'green' if (r.momentum_pct or 0) >= 0 else 'red' }}">
          {{ "%+.3f"|format(r.momentum_pct) if r.momentum_pct is not none else "—" }}
        </td>
        <td>{{ "%.3f"|format(r.divergence) if r.divergence is not none else "—" }}</td>
        <td>{{ "$%.2f"|format(r.size_usd) if r.size_usd else "—" }}</td>
        <td>{{ "%.1f"|format(r.shares) if r.shares else "—" }}</td>
        <td class="{{ 'red' if (r.seconds_to_expiry or 999) < 60 else 'grey' }}">
          {{ r.seconds_to_expiry|int ~ "s" if r.seconds_to_expiry is not none else "—" }}
        </td>
        <td class="ellipsis grey" title="{{ r.reason or r.market or '' }}">
          {% if r.status == 'skip' %}
            {{ r.reason or "—" }}
          {% else %}
            {{ (r.market or "")[:50] }}{% if (r.market or "")|length > 50 %}…{% endif %}
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p class="grey" style="padding:20px 0">
      No records yet — waiting for the first cron cycle to complete.
    </p>
  {% endif %}
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Portfolio page template
# ---------------------------------------------------------------------------

PORTFOLIO_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>FastLoop Portfolio</title>
  <meta http-equiv="refresh" content="60">
  <style>{{ css | safe }}</style>
</head>
<body>
  {{ nav | safe }}

  <h2>Paper Portfolio — All Positions</h2>
  <div class="cards">
    <div class="card">
      <div class="label">Starting Balance</div>
      <div class="value white">${{ "%.2f"|format(pf.starting_balance) }}</div>
    </div>
    <div class="card">
      <div class="label">Portfolio Value</div>
      <div class="value {{ 'green' if pf.portfolio_value >= pf.starting_balance else 'red' }}">
        ${{ "%.2f"|format(pf.portfolio_value) }}
      </div>
    </div>
    <div class="card">
      <div class="label">Return</div>
      <div class="value {{ 'green' if pf.return_pct >= 0 else 'red' }}">
        {{ "%+.2f"|format(pf.return_pct) }}%
      </div>
    </div>
    <div class="card">
      <div class="label">Realized P&L</div>
      <div class="value {{ 'pnl-pos' if pf.resolved_pnl >= 0 else 'pnl-neg' }}">
        ${{ "%+.2f"|format(pf.resolved_pnl) }}
      </div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value {{ 'green' if pf.win_rate >= 50 else ('yellow' if pf.win_rate > 0 else 'grey') }}">
        {{ "%.1f"|format(pf.win_rate) }}%
      </div>
    </div>
    <div class="card">
      <div class="label">Open</div>
      <div class="value blue">{{ pf.open_count }}</div>
    </div>
    <div class="card">
      <div class="label">Won</div>
      <div class="value green">{{ pf.won_count }}</div>
    </div>
    <div class="card">
      <div class="label">Lost</div>
      <div class="value red">{{ pf.lost_count }}</div>
    </div>
    <div class="card">
      <div class="label">Expired</div>
      <div class="value grey">{{ pf.expired_count }}</div>
    </div>
    <div class="card">
      <div class="label">Total Invested</div>
      <div class="value grey">${{ "%.2f"|format(pf.total_invested) }}</div>
    </div>
  </div>

  {% if positions %}
  <table>
    <thead>
      <tr>
        <th>Market</th>
        <th>Side</th>
        <th>Entry Price</th>
        <th>Shares</th>
        <th>Cost</th>
        <th>P&L</th>
        <th>Status</th>
        <th>Entered</th>
        <th>Resolved</th>
      </tr>
    </thead>
    <tbody>
    {% for p in positions %}
      {% set pnl = p.pnl_usd or 0 %}
      <tr>
        <td class="ellipsis" title="{{ p.market }}">
          {{ p.market[:55] }}{% if p.market|length > 55 %}…{% endif %}
        </td>
        <td><span class="badge badge-{{ p.side.lower() }}">{{ p.side }}</span></td>
        <td>${{ "%.3f"|format(p.entry_price) }}</td>
        <td>{{ "%.1f"|format(p.shares) }}</td>
        <td class="blue">${{ "%.2f"|format(p.cost_usd) }}</td>
        <td class="{{ 'pnl-pos' if pnl > 0 else ('pnl-neg' if pnl < 0 else 'grey') }}">
          {% if p.status == 'open' %}—
          {% else %}${{ "%+.2f"|format(pnl) }}{% endif %}
        </td>
        <td><span class="badge badge-{{ p.status }}">{{ p.status.upper() }}</span></td>
        <td class="grey">{{ p.entered_at[:16].replace("T"," ") }}</td>
        <td class="grey">{{ (p.resolved_at or "—")[:16].replace("T"," ") }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p class="grey" style="padding:20px 0">
      No positions yet — they appear after the first paper trade executes.
    </p>
  {% endif %}
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Settings page template
# ---------------------------------------------------------------------------

SETTINGS_EXTRA_CSS = """
input[type=text],input[type=number],select {
  background:#161b22;border:1px solid #30363d;color:#e6edf3;
  padding:5px 10px;border-radius:4px;font-family:monospace;font-size:0.9em;width:120px;
}
.btn { background:#238636;color:#fff;border:none;padding:7px 18px;
       border-radius:6px;cursor:pointer;font-family:monospace;font-size:0.9em; }
.btn:hover { background:#2ea043; }
.saved { color:#3fb950;font-size:0.85em;margin-left:10px; }
.hint  { color:#8b949e;font-size:0.80em; }
.env-var { color:#d29922;font-size:0.78em; }
"""

SETTINGS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>FastLoop Settings</title>
  <style>
    {{ css | safe }}
    {{ extra_css | safe }}
  </style>
</head>
<body>
  {{ nav | safe }}

  <h2>Trading Parameters</h2>
  <p class="hint" style="margin-bottom:16px">
    Changes here write to <code>config.json</code> for the current session.
    To persist across deploys, set the Railway Variable shown in each row.
  </p>

  {% if saved %}
  <p style="color:#3fb950;margin-bottom:14px">✓ Settings saved.</p>
  {% endif %}

  <form method="POST" action="/settings">
  <table style="max-width:860px">
    <thead>
      <tr>
        <th>Parameter</th>
        <th>Current Value</th>
        <th>New Value</th>
        <th>Railway Variable</th>
        <th>Description</th>
      </tr>
    </thead>
    <tbody>
    {% for s in settings %}
      <tr>
        <td>{{ s.label }}</td>
        <td class="green">{{ s.current }}</td>
        <td>
          {% if s.type == 'bool' %}
            <select name="{{ s.key }}">
              <option value="true"  {{ 'selected' if s.current == True or s.current == 'true' }}>true</option>
              <option value="false" {{ 'selected' if s.current == False or s.current == 'false' }}>false</option>
            </select>
          {% elif s.type == 'choice' %}
            <select name="{{ s.key }}">
              {% for opt in s.options %}
              <option value="{{ opt }}" {{ 'selected' if s.current == opt }}>{{ opt }}</option>
              {% endfor %}
            </select>
          {% else %}
            <input type="{{ 'number' if s.type == 'number' else 'text' }}"
                   name="{{ s.key }}" value="{{ s.current }}"
                   step="{{ s.step or 'any' }}">
          {% endif %}
        </td>
        <td class="env-var">{{ s.env_var }}</td>
        <td class="hint">{{ s.hint }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  <br>
  <button type="submit" class="btn">Save Settings</button>
  </form>
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


def _load_portfolio():
    try:
        from paper_portfolio import get_summary
        return get_summary()
    except Exception as e:
        print(f"  [WARN] Failed to load portfolio: {e}")
        return {
            "starting_balance": 50.0, "total_invested": 0.0, "open_cost": 0.0,
            "open_count": 0, "won_count": 0, "lost_count": 0, "expired_count": 0,
            "resolved_pnl": 0.0, "portfolio_value": 50.0,
            "return_pct": 0.0, "win_rate": 0.0, "positions": [],
        }


def _load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_config(updates):
    cfg = _load_config()
    cfg.update(updates)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _normalize_records(raw_records):
    """Convert raw dicts to safe attribute-accessible Row objects for Jinja."""
    class Row(dict):
        def __getattr__(self, k):
            return self.get(k)
    return [Row(r) for r in raw_records]


class _Obj(dict):
    """Dict with attribute access for Jinja templates."""
    def __getattr__(self, k):
        return self.get(k, 0)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    stats   = _load_stats()
    pf      = _load_portfolio()
    mode    = os.environ.get("TRADING_MODE", "paper")
    records = _normalize_records(stats.get("records", []))

    positions    = pf.get("positions", [])
    open_pos     = [p for p in positions if p.get("status") == "open"]
    resolved_pos = [p for p in positions if p.get("status") in ("won", "lost")][:20]

    return render_template_string(
        MAIN_TEMPLATE,
        css              = CSS,
        nav              = NAV.format(now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        stats            = _Obj(stats),
        pf               = _Obj(pf),
        mode             = mode,
        records          = records,
        open_positions   = open_pos,
        resolved_positions = list(reversed(resolved_pos)),
    )


@app.route("/portfolio")
def portfolio():
    pf        = _load_portfolio()
    positions = list(reversed(pf.get("positions", [])))

    return render_template_string(
        PORTFOLIO_TEMPLATE,
        css       = CSS,
        nav       = NAV.format(now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        pf        = _Obj(pf),
        positions = positions,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    saved = False

    if request.method == "POST":
        cfg   = _load_config()
        types = {
            "entry_threshold": float, "min_momentum_pct": float,
            "max_position": float, "signal_source": str, "lookback_minutes": int,
            "min_time_remaining": int, "asset": str, "window": str,
            "volume_confidence": lambda v: v.lower() in ("true", "1", "yes"),
            "daily_budget": float,
        }
        updates = {}
        for key, cast in types.items():
            val = request.form.get(key)
            if val is not None:
                try:
                    updates[key] = cast(val)
                except Exception:
                    pass
        _save_config(updates)
        saved = True

    cfg = _load_config()

    SETTINGS_DEF = [
        dict(key="entry_threshold",    label="Entry Threshold",     type="number", step="0.01",
             env_var="SIMMER_SPRINT_ENTRY",        hint="Min price divergence from 50¢ to trigger a trade (e.g. 0.05 = 5¢)"),
        dict(key="min_momentum_pct",   label="Min Momentum %",      type="number", step="0.01",
             env_var="SIMMER_SPRINT_MOMENTUM",     hint="Min BTC/ETH/SOL % move required (e.g. 0.03 = 0.03%)"),
        dict(key="max_position",       label="Max Position $",      type="number", step="1",
             env_var="SIMMER_SPRINT_MAX_POSITION", hint="Maximum USD per trade"),
        dict(key="daily_budget",       label="Daily Budget $",      type="number", step="5",
             env_var="SIMMER_SPRINT_DAILY_BUDGET", hint="Max USD spend per calendar day (UTC)"),
        dict(key="lookback_minutes",   label="Lookback Minutes",    type="number", step="1",
             env_var="SIMMER_SPRINT_LOOKBACK",     hint="Price history window for momentum (minutes)"),
        dict(key="min_time_remaining", label="Min Time Remaining",  type="number", step="5",
             env_var="SIMMER_SPRINT_MIN_TIME",     hint="Skip markets with less than N seconds left"),
        dict(key="asset",              label="Asset",               type="choice",
             options=["BTC","ETH","SOL"],
             env_var="SIMMER_SPRINT_ASSET",        hint="Asset to trade"),
        dict(key="window",             label="Market Window",       type="choice",
             options=["5m","15m"],
             env_var="SIMMER_SPRINT_WINDOW",       hint="Fast market duration"),
        dict(key="signal_source",      label="Signal Source",       type="choice",
             options=["binance","coingecko"],
             env_var="SIMMER_SPRINT_SIGNAL",       hint="CEX price feed for momentum"),
        dict(key="volume_confidence",  label="Volume Confidence",   type="bool",
             env_var="SIMMER_SPRINT_VOL_CONF",     hint="Weight signal by Binance volume ratio"),
    ]

    for s in SETTINGS_DEF:
        s["current"] = cfg.get(s["key"], "—")

    return render_template_string(
        SETTINGS_TEMPLATE,
        css       = CSS,
        extra_css = SETTINGS_EXTRA_CSS,
        nav       = NAV.format(now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        settings  = SETTINGS_DEF,
        saved     = saved,
    )


@app.route("/results")
def results_page():
    pf       = _load_portfolio()
    stats    = _load_stats()
    positions = pf.get("positions", [])

    resolved = [p for p in reversed(positions) if p.get("status") in ("won", "lost", "expired")]
    skips    = [r for r in stats.get("records", []) if r.get("status") == "skip"]

    won      = [p for p in resolved if p.get("status") == "won"]
    lost     = [p for p in resolved if p.get("status") == "lost"]
    total_pnl = sum(p.get("pnl_usd") or 0 for p in resolved)
    win_rate  = round(len(won) / len(resolved) * 100, 1) if resolved else 0.0

    avg_win  = round(sum(p.get("pnl_usd",0) for p in won)  / len(won),  2) if won  else 0.0
    avg_loss = round(sum(p.get("pnl_usd",0) for p in lost) / len(lost), 2) if lost else 0.0

    skip_reasons = {}
    for r in skips:
        reason = r.get("reason", "unknown")
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    skip_breakdown = sorted(skip_reasons.items(), key=lambda x: -x[1])

    tmpl = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>FastLoop Results</title>
  <meta http-equiv="refresh" content="60">
  <style>{{ css | safe }}</style>
</head>
<body>
  {{ nav | safe }}

  <h2>Results Summary</h2>
  <div class="cards">
    <div class="card">
      <div class="label">Resolved Trades</div>
      <div class="value blue">{{ resolved|length }}</div>
    </div>
    <div class="card">
      <div class="label">Won</div>
      <div class="value green">{{ won_count }}</div>
    </div>
    <div class="card">
      <div class="label">Lost</div>
      <div class="value red">{{ lost_count }}</div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value {{ 'green' if win_rate >= 50 else 'red' }}">{{ win_rate }}%</div>
    </div>
    <div class="card">
      <div class="label">Total P&L</div>
      <div class="value {{ 'pnl-pos' if total_pnl >= 0 else 'pnl-neg' }}">${{ "%+.2f"|format(total_pnl) }}</div>
    </div>
    <div class="card">
      <div class="label">Avg Win</div>
      <div class="value green">${{ "%+.2f"|format(avg_win) }}</div>
    </div>
    <div class="card">
      <div class="label">Avg Loss</div>
      <div class="value red">${{ "%+.2f"|format(avg_loss) }}</div>
    </div>
    <div class="card">
      <div class="label">Skipped</div>
      <div class="value grey">{{ skips|length }}</div>
    </div>
  </div>

  <h2>Resolved Positions</h2>
  {% if resolved %}
  <table>
    <thead>
      <tr>
        <th>Market</th>
        <th>Side</th>
        <th>Entry Price</th>
        <th>Shares</th>
        <th>Cost</th>
        <th>Payout</th>
        <th>P&L</th>
        <th>Result</th>
        <th>Entered</th>
        <th>Resolved</th>
      </tr>
    </thead>
    <tbody>
    {% for p in resolved %}
      {% set pnl = p.pnl_usd or 0 %}
      {% set payout = (p.shares if p.status == 'won' else 0)|round(2) %}
      <tr>
        <td class="ellipsis" title="{{ p.market }}">{{ p.market[:55] }}{% if p.market|length > 55 %}…{% endif %}</td>
        <td><span class="badge badge-{{ p.side.lower() }}">{{ p.side }}</span></td>
        <td>${{ "%.3f"|format(p.entry_price) }}</td>
        <td>{{ "%.1f"|format(p.shares) }}</td>
        <td class="blue">${{ "%.2f"|format(p.cost_usd) }}</td>
        <td class="{{ 'green' if p.status == 'won' else 'grey' }}">
          ${{ "%.2f"|format(payout) if p.status == 'won' else '0.00' }}
        </td>
        <td class="{{ 'pnl-pos' if pnl > 0 else 'pnl-neg' }}">${{ "%+.2f"|format(pnl) }}</td>
        <td><span class="badge badge-{{ p.status }}">{{ p.status.upper() }}</span></td>
        <td class="grey">{{ p.entered_at[:16].replace("T"," ") }}</td>
        <td class="grey">{{ (p.resolved_at or "—")[:16].replace("T"," ") }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p class="grey" style="padding:20px 0">No resolved positions yet.</p>
  {% endif %}

  <h2>Skip Breakdown</h2>
  {% if skip_breakdown %}
  <table style="max-width:500px">
    <thead><tr><th>Reason</th><th>Count</th></tr></thead>
    <tbody>
    {% for reason, count in skip_breakdown %}
      <tr>
        <td class="grey">{{ reason }}</td>
        <td class="yellow">{{ count }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p class="grey" style="padding:20px 0">No skips recorded yet.</p>
  {% endif %}

  <h2>All Skip Records</h2>
  {% if skips %}
  <table>
    <thead>
      <tr><th>Time (UTC)</th><th>Asset</th><th>YES Price</th><th>Momentum%</th><th>Expires</th><th>Reason</th></tr>
    </thead>
    <tbody>
    {% for r in skips %}
      <tr>
        <td class="grey">{{ r.get('timestamp','')[:16].replace('T',' ') }}</td>
        <td>{{ r.get('asset','—') }}</td>
        <td>{{ "$%.3f"|format(r.yes_price) if r.get('yes_price') is not none else "—" }}</td>
        <td class="{{ 'green' if (r.get('momentum_pct') or 0) >= 0 else 'red' }}">
          {{ "%+.3f"|format(r.momentum_pct) if r.get('momentum_pct') is not none else "—" }}
        </td>
        <td class="grey">{{ r.seconds_to_expiry|int ~ "s" if r.get('seconds_to_expiry') is not none else "—" }}</td>
        <td class="grey">{{ r.get('reason','—') }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</body>
</html>"""

    return render_template_string(
        tmpl,
        css            = CSS,
        nav            = NAV.format(now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        resolved       = resolved,
        won_count      = len(won),
        lost_count     = len(lost),
        win_rate       = win_rate,
        total_pnl      = total_pnl,
        avg_win        = avg_win,
        avg_loss       = avg_loss,
        skips          = list(reversed(skips[:200])),
        skip_breakdown = skip_breakdown,
    )


@app.route("/api/trades")
def api_trades():
    try:
        from database import load_trades
        return jsonify(load_trades(500))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio")
def api_portfolio():
    try:
        from paper_portfolio import get_summary
        return jsonify(get_summary())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
