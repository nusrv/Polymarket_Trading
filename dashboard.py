"""
FastLoop Trading — Live Dashboard
Runs as a persistent Flask web server (PORT injected by Railway).
Reads from PostgreSQL via database.py and paper_portfolio.py.
"""

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
  Auto-refreshes every 60s &nbsp;|&nbsp; {now} &nbsp;|&nbsp;
  <a href="/">Dashboard</a> &nbsp;|&nbsp;
  <a href="/portfolio">Portfolio</a> &nbsp;|&nbsp;
  <a href="/results">Results Log</a> &nbsp;|&nbsp;
  <a href="/analyze">&#128200; Analyze</a> &nbsp;|&nbsp;
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

  {% if bot_stale %}
  <div style="background:#3d1a1a;border:1px solid #f85149;border-radius:6px;padding:10px 16px;margin-bottom:16px;color:#f85149;font-size:0.88em">
    &#9888; Bot has not run in {{ bot_stale_mins }} minutes (last run: {{ last_run_ts }}).
    Check the Railway cron job or scheduler.
  </div>
  {% endif %}

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
      <div class="label">Open Exposure</div>
      <div class="value blue">${{ "%.2f"|format(pf.open_cost) }}</div>
    </div>
    <div class="card">
      <div class="label">Traded All-Time</div>
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
        <th>Entered ({{ tz_offset }})</th>
        <th>Expires ({{ tz_offset }})</th>
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
        <td class="grey">{{ p.entered_at | fmt_ts }}</td>
        <td class="grey">{{ p.end_time | fmt_ts if p.end_time else "—" }}</td>
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
        <th>Time ({{ tz_offset }})</th>
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
        <td class="grey">{{ r.timestamp | fmt_ts }}</td>
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
  <style>
    {{ css | safe }}
    .fund-box { background:#161b22;border:1px solid #30363d;border-radius:8px;padding:18px 24px;margin-bottom:24px;max-width:520px; }
    .fund-box h3 { margin:0 0 14px;font-size:1em;color:#8b949e; }
    .fund-row { display:flex;gap:10px;align-items:center;margin-bottom:10px; }
    .fund-row label { color:#8b949e;font-size:0.85em;width:90px;flex-shrink:0; }
    .fund-row input, .fund-row select {
      background:#0d1117;border:1px solid #30363d;color:#e6edf3;
      padding:6px 10px;border-radius:5px;font-family:monospace;font-size:0.9em;width:160px;
    }
    .fund-row input[type=text] { width:200px; }
    .btn-dep  { background:#238636;color:#fff;border:none;padding:7px 18px;border-radius:6px;cursor:pointer;font-size:0.9em; }
    .btn-dep:hover { background:#2ea043; }
    .btn-with { background:#b62324;color:#fff;border:none;padding:7px 18px;border-radius:6px;cursor:pointer;font-size:0.9em; }
    .btn-with:hover { background:#cf3232; }
    .filter-bar { display:flex;gap:6px;margin-bottom:16px;align-items:center;flex-wrap:wrap; }
    .filter-bar button { background:#161b22;border:1px solid #30363d;color:#8b949e;
      padding:5px 14px;border-radius:5px;cursor:pointer;font-size:0.82em;font-family:monospace; }
    .filter-bar button.active { background:#1f6feb;border-color:#1f6feb;color:#fff; }
    .filter-bar input[type=date] { background:#161b22;border:1px solid #30363d;color:#e6edf3;
      padding:4px 8px;border-radius:5px;font-family:monospace;font-size:0.82em; }
  </style>
</head>
<body>
  {{ nav | safe }}

  <h2>Paper Portfolio</h2>

  <!-- Summary cards -->
  <div class="cards">
    <div class="card">
      <div class="label">Starting Capital</div>
      <div class="value white">${{ "%.2f"|format(pf.starting_balance) }}</div>
    </div>
    {% if pf.net_funding > 0 %}
    <div class="card">
      <div class="label">Net Funded</div>
      <div class="value {{ 'green' if pf.net_funding >= 0 else 'red' }}">${{ "%+.2f"|format(pf.net_funding) }}</div>
    </div>
    {% endif %}
    <div class="card">
      <div class="label">Portfolio Value</div>
      <div class="value {{ 'green' if pf.portfolio_value >= (pf.starting_balance + pf.net_funding) else 'red' }}">
        ${{ "%.2f"|format(pf.portfolio_value) }}
      </div>
    </div>
    <div class="card">
      <div class="label">Realized P&L</div>
      <div class="value {{ 'pnl-pos' if pf.resolved_pnl >= 0 else 'pnl-neg' }}">
        ${{ "%+.2f"|format(pf.resolved_pnl) }}
      </div>
    </div>
    <div class="card">
      <div class="label">Return</div>
      <div class="value {{ 'green' if pf.return_pct >= 0 else 'red' }}">
        {{ "%+.2f"|format(pf.return_pct) }}%
      </div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value {{ 'green' if pf.win_rate >= 50 else ('yellow' if pf.win_rate > 0 else 'grey') }}">
        {{ "%.1f"|format(pf.win_rate) }}%
      </div>
    </div>
    <div class="card">
      <div class="label">Won / Lost</div>
      <div class="value white">
        <span class="green">{{ pf.won_count }}</span> /
        <span class="red">{{ pf.lost_count }}</span>
      </div>
    </div>
    <div class="card">
      <div class="label">Open Exposure</div>
      <div class="value blue">${{ "%.2f"|format(pf.open_cost) }}</div>
    </div>
    <div class="card">
      <div class="label">Traded All-Time</div>
      <div class="value grey">${{ "%.2f"|format(pf.total_invested) }}</div>
    </div>
  </div>

  <!-- Fund / Withdraw wallet -->
  <div class="fund-box">
    <h3>Fund Wallet</h3>
    <form method="POST" action="/portfolio/fund" style="display:contents">
      <div class="fund-row">
        <label>Type</label>
        <select name="tx_type">
          <option value="deposit">Deposit</option>
          <option value="withdrawal">Withdrawal</option>
        </select>
      </div>
      <div class="fund-row">
        <label>Amount ($)</label>
        <input type="number" name="amount" min="0.01" step="0.01" placeholder="e.g. 50.00" required>
      </div>
      <div class="fund-row">
        <label>Note</label>
        <input type="text" name="note" placeholder="optional label">
      </div>
      <div style="display:flex;gap:10px;margin-top:6px">
        <button type="submit" name="action" value="deposit"   class="btn-dep">+ Deposit</button>
        <button type="submit" name="action" value="withdrawal" class="btn-with">- Withdraw</button>
      </div>
    </form>
    {% if fund_msg %}
    <p style="margin:10px 0 0;color:#3fb950;font-size:0.85em">{{ fund_msg }}</p>
    {% endif %}
  </div>

  <!-- Transaction history -->
  {% if transactions %}
  <h3 style="margin:0 0 10px;font-size:1em;color:#8b949e">Funding History</h3>
  <table style="max-width:600px;margin-bottom:28px">
    <thead>
      <tr>
        <th>Time ({{ tz_offset }})</th>
        <th>Type</th>
        <th>Amount</th>
        <th>Note</th>
      </tr>
    </thead>
    <tbody>
    {% for t in transactions %}
      <tr>
        <td class="grey">{{ t.timestamp | fmt_ts }}</td>
        <td><span class="badge {{ 'badge-won' if t.type == 'deposit' else 'badge-lost' }}">
          {{ t.type.upper() }}</span>
        </td>
        <td class="{{ 'pnl-pos' if t.type == 'deposit' else 'pnl-neg' }}">
          {{ '+' if t.type == 'deposit' else '-' }}${{ "%.2f"|format(t.amount) }}
        </td>
        <td class="grey">{{ t.note or '—' }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <!-- Positions with time filter -->
  <h3 style="margin:0 0 10px;font-size:1em;color:#8b949e">Trade History</h3>
  <div class="filter-bar">
    <button class="active" onclick="filterRows(this,'all')">All</button>
    <button onclick="filterRows(this,'1d')">Day</button>
    <button onclick="filterRows(this,'7d')">7 Days</button>
    <button onclick="filterRows(this,'30d')">1 Month</button>
    <span style="color:#8b949e;font-size:0.82em">Custom:</span>
    <input type="date" id="from_date" onchange="filterCustom()">
    <span style="color:#8b949e;font-size:0.82em">to</span>
    <input type="date" id="to_date" onchange="filterCustom()">
  </div>

  {% if positions %}
  <table id="pos-table">
    <thead>
      <tr>
        <th>Market</th>
        <th>Side</th>
        <th>Entry Price</th>
        <th>Shares</th>
        <th>Cost</th>
        <th>P&L</th>
        <th>Status</th>
        <th>Entered ({{ tz_offset }})</th>
        <th>Resolved ({{ tz_offset }})</th>
      </tr>
    </thead>
    <tbody>
    {% for p in positions %}
      {% set pnl = p.pnl_usd or 0 %}
      <tr data-ts="{{ p.entered_at }}">
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
        <td class="grey">{{ p.entered_at | fmt_ts }}</td>
        <td class="grey">{{ p.resolved_at | fmt_ts if p.resolved_at else "—" }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p class="grey" style="padding:20px 0">No positions yet.</p>
  {% endif %}

<script>
function filterRows(btn, range) {
  document.querySelectorAll('.filter-bar button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('from_date').value = '';
  document.getElementById('to_date').value = '';
  const now = new Date();
  let cutoff = null;
  if (range === '1d')  cutoff = new Date(now - 86400000);
  if (range === '7d')  cutoff = new Date(now - 7*86400000);
  if (range === '30d') cutoff = new Date(now - 30*86400000);
  document.querySelectorAll('#pos-table tbody tr').forEach(row => {
    if (!cutoff) { row.style.display = ''; return; }
    const ts = new Date(row.dataset.ts);
    row.style.display = ts >= cutoff ? '' : 'none';
  });
}
function filterCustom() {
  document.querySelectorAll('.filter-bar button').forEach(b => b.classList.remove('active'));
  const from = document.getElementById('from_date').value;
  const to   = document.getElementById('to_date').value;
  const fromDt = from ? new Date(from) : null;
  const toDt   = to   ? new Date(to + 'T23:59:59') : null;
  document.querySelectorAll('#pos-table tbody tr').forEach(row => {
    const ts = new Date(row.dataset.ts);
    const show = (!fromDt || ts >= fromDt) && (!toDt || ts <= toDt);
    row.style.display = show ? '' : 'none';
  });
}
</script>
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
          {% elif s.type == 'tz_select' %}
            <select name="{{ s.key }}" style="width:300px">
              {% for val, lbl in s.options %}
              <option value="{{ val }}" {{ 'selected' if s.current == val }}>{{ lbl }}</option>
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
            "starting_balance": 100.0, "net_funding": 0.0, "total_invested": 0.0, "open_cost": 0.0,
            "open_count": 0, "won_count": 0, "lost_count": 0, "expired_count": 0,
            "resolved_pnl": 0.0, "portfolio_value": 100.0,
            "return_pct": 0.0, "win_rate": 0.0, "positions": [], "transactions": [],
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


def _get_tz():
    """Return configured display timezone (default: Asia/Amman = UTC+3)."""
    tz_name = _load_config().get("display_tz", "Asia/Amman")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        return timezone.utc


def _now_local():
    """Current datetime in the display timezone."""
    return datetime.now(_get_tz())


def _fmt_ts(ts_str, fmt="%Y-%m-%d %H:%M"):
    """Convert a UTC ISO timestamp string to the display timezone."""
    if not ts_str or ts_str == "—":
        return "—"
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_get_tz()).strftime(fmt)
    except Exception:
        return str(ts_str)[:16].replace("T", " ")


@app.template_filter("fmt_ts")
def fmt_ts_filter(ts_str):
    return _fmt_ts(ts_str)


def _tz_label():
    """Short label for the display timezone, e.g. 'Asia/Amman'."""
    return _load_config().get("display_tz", "Asia/Amman")


def _tz_offset_label():
    """UTC offset string for the display timezone, e.g. 'UTC+3'."""
    try:
        tz = _get_tz()
        total_mins = int(datetime.now(tz).utcoffset().total_seconds() / 60)
        sign = "+" if total_mins >= 0 else "-"
        h, m = divmod(abs(total_mins), 60)
        return f"UTC{sign}{h}:{m:02d}" if m else f"UTC{sign}{h}"
    except Exception:
        return "UTC"


# Major IANA timezones for the settings dropdown (value, display label)
TZ_OPTIONS = [
    ("Pacific/Midway",       "UTC-11 — Midway Island"),
    ("Pacific/Honolulu",     "UTC-10 — Honolulu (Hawaii)"),
    ("America/Anchorage",    "UTC-9  — Anchorage (Alaska)"),
    ("America/Los_Angeles",  "UTC-8  — Los Angeles / Vancouver"),
    ("America/Denver",       "UTC-7  — Denver / Phoenix"),
    ("America/Chicago",      "UTC-6  — Chicago / Mexico City"),
    ("America/New_York",     "UTC-5  — New York / Toronto"),
    ("America/Halifax",      "UTC-4  — Halifax (Canada)"),
    ("America/Sao_Paulo",    "UTC-3  — São Paulo (Brazil)"),
    ("Atlantic/South_Georgia","UTC-2 — South Georgia"),
    ("Atlantic/Azores",      "UTC-1  — Azores"),
    ("UTC",                  "UTC+0  — UTC / Reykjavik"),
    ("Europe/London",        "UTC+0  — London (UK)"),
    ("Europe/Paris",         "UTC+1  — Paris / Berlin / Rome"),
    ("Europe/Athens",        "UTC+2  — Athens / Cairo / Kyiv"),
    ("Europe/Istanbul",      "UTC+3  — Istanbul (Turkey)"),
    ("Asia/Riyadh",          "UTC+3  — Riyadh (Saudi Arabia)"),
    ("Asia/Amman",           "UTC+3  — Amman (Jordan)"),
    ("Asia/Baghdad",         "UTC+3  — Baghdad (Iraq)"),
    ("Asia/Tehran",          "UTC+3:30 — Tehran (Iran)"),
    ("Asia/Dubai",           "UTC+4  — Dubai (UAE)"),
    ("Asia/Kabul",           "UTC+4:30 — Kabul (Afghanistan)"),
    ("Asia/Karachi",         "UTC+5  — Karachi (Pakistan)"),
    ("Asia/Kolkata",         "UTC+5:30 — Mumbai / New Delhi (India)"),
    ("Asia/Kathmandu",       "UTC+5:45 — Kathmandu (Nepal)"),
    ("Asia/Dhaka",           "UTC+6  — Dhaka (Bangladesh)"),
    ("Asia/Yangon",          "UTC+6:30 — Yangon (Myanmar)"),
    ("Asia/Bangkok",         "UTC+7  — Bangkok / Jakarta"),
    ("Asia/Singapore",       "UTC+8  — Singapore / Kuala Lumpur"),
    ("Asia/Shanghai",        "UTC+8  — Beijing / Shanghai (China)"),
    ("Asia/Tokyo",           "UTC+9  — Tokyo (Japan)"),
    ("Australia/Adelaide",   "UTC+9:30 — Adelaide (Australia)"),
    ("Australia/Sydney",     "UTC+10 — Sydney / Melbourne"),
    ("Pacific/Guadalcanal",  "UTC+11 — Solomon Islands"),
    ("Pacific/Auckland",     "UTC+12 — Auckland (New Zealand)"),
]


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

    positions = pf.get("positions", [])
    open_pos  = [p for p in positions if p.get("status") == "open"]

    # Bot staleness check — warn if no run in > 10 minutes
    bot_stale      = False
    bot_stale_mins = 0
    last_run_ts    = "—"
    all_records = stats.get("records", [])
    if all_records:
        last_ts_str = all_records[0].get("timestamp", "")
        if last_ts_str:
            try:
                last_dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_mins = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                if age_mins > 10:
                    bot_stale      = True
                    bot_stale_mins = int(age_mins)
                    last_run_ts    = _fmt_ts(last_ts_str)
            except Exception:
                pass

    return render_template_string(
        MAIN_TEMPLATE,
        css            = CSS,
        nav            = NAV.format(now=_now_local().strftime("%Y-%m-%d %H:%M") + " (" + _tz_label() + ")"),
        stats          = _Obj(stats),
        pf             = _Obj(pf),
        mode           = mode,
        records        = records,
        open_positions = open_pos,
        tz_offset      = _tz_offset_label(),
        bot_stale      = bot_stale,
        bot_stale_mins = bot_stale_mins,
        last_run_ts    = last_run_ts,
    )


@app.route("/portfolio")
def portfolio():
    pf        = _load_portfolio()
    positions = list(reversed(pf.get("positions", [])))
    txns      = pf.get("transactions", [])

    class _Tx(dict):
        def __getattr__(self, k):
            return self.get(k, "")

    return render_template_string(
        PORTFOLIO_TEMPLATE,
        css          = CSS,
        nav          = NAV.format(now=_now_local().strftime("%Y-%m-%d %H:%M") + " (" + _tz_label() + ")"),
        pf           = _Obj(pf),
        positions    = positions,
        transactions = [_Tx(t) for t in txns],
        tz_offset    = _tz_offset_label(),
        fund_msg     = request.args.get("msg", ""),
    )


@app.route("/portfolio/fund", methods=["POST"])
def portfolio_fund():
    from paper_portfolio import add_transaction
    tx_type = request.form.get("action") or request.form.get("tx_type", "deposit")
    try:
        amount = float(request.form.get("amount", 0))
    except ValueError:
        amount = 0.0
    note = request.form.get("note", "").strip()

    if amount <= 0:
        return redirect("/portfolio?msg=Invalid+amount")
    if tx_type not in ("deposit", "withdrawal"):
        tx_type = "deposit"

    add_transaction(tx_type, amount, note)
    label = "Deposited" if tx_type == "deposit" else "Withdrawn"
    return redirect(f"/portfolio?msg={label}+%24{amount:.2f}+successfully")


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    saved = False

    if request.method == "POST":
        cfg   = _load_config()
        types = {
            "entry_threshold": float, "yes_entry_threshold": float, "min_momentum_pct": float,
            "max_position": float, "signal_source": str, "lookback_minutes": int,
            "min_time_remaining": int, "asset": str, "window": str,
            "volume_confidence": lambda v: v.lower() in ("true", "1", "yes"),
            "daily_budget": float, "display_tz": str,
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
        dict(key="entry_threshold",    label="NO Entry Threshold",  type="number", step="0.01",
             env_var="SIMMER_SPRINT_ENTRY",        hint="Min divergence from 50¢ for NO trades (e.g. 0.05 = 5¢)"),
        dict(key="yes_entry_threshold", label="YES Entry Threshold", type="number", step="0.01",
             env_var="SIMMER_SPRINT_YES_ENTRY",    hint="Min divergence from 50¢ for YES trades — keep higher than NO (e.g. 0.10)"),
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
        dict(key="display_tz",         label="Display Timezone",    type="tz_select",
             options=TZ_OPTIONS,
             env_var="DISPLAY_TZ",                 hint="Timezone used for all timestamps in the dashboard"),
    ]

    for s in SETTINGS_DEF:
        s["current"] = cfg.get(s["key"], "—")

    return render_template_string(
        SETTINGS_TEMPLATE,
        css       = CSS,
        extra_css = SETTINGS_EXTRA_CSS,
        nav       = NAV.format(now=_now_local().strftime("%Y-%m-%d %H:%M") + " (" + _tz_label() + ")"),
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
  <div style="display:flex;gap:6px;margin-bottom:14px;align-items:center;flex-wrap:wrap">
    <button class="flt-btn active" onclick="flt(this,'all')">All</button>
    <button class="flt-btn" onclick="flt(this,'1d')">Day</button>
    <button class="flt-btn" onclick="flt(this,'7d')">7 Days</button>
    <button class="flt-btn" onclick="flt(this,'30d')">1 Month</button>
    <span style="color:#8b949e;font-size:0.82em">Custom:</span>
    <input type="date" id="rf" onchange="fltC()" style="background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:4px 8px;border-radius:5px;font-family:monospace;font-size:0.82em">
    <span style="color:#8b949e;font-size:0.82em">to</span>
    <input type="date" id="rt" onchange="fltC()" style="background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:4px 8px;border-radius:5px;font-family:monospace;font-size:0.82em">
  </div>
  <style>
    .flt-btn{background:#161b22;border:1px solid #30363d;color:#8b949e;padding:5px 14px;border-radius:5px;cursor:pointer;font-size:0.82em;font-family:monospace}
    .flt-btn.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
  </style>
  {% if resolved %}
  <table id="res-tbl">
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
        <th>Entered ({{ tz_offset }})</th>
        <th>Resolved ({{ tz_offset }})</th>
      </tr>
    </thead>
    <tbody>
    {% for p in resolved %}
      {% set pnl = p.pnl_usd or 0 %}
      {% set payout = (p.shares if p.status == 'won' else 0)|round(2) %}
      <tr data-ts="{{ p.entered_at }}">
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
        <td class="grey">{{ p.entered_at | fmt_ts }}</td>
        <td class="grey">{{ p.resolved_at | fmt_ts if p.resolved_at else "—" }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  <script>
  function flt(btn,r){
    document.querySelectorAll('.flt-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('rf').value='';document.getElementById('rt').value='';
    const now=new Date();let cut=null;
    if(r==='1d')cut=new Date(now-86400000);
    if(r==='7d')cut=new Date(now-7*86400000);
    if(r==='30d')cut=new Date(now-30*86400000);
    document.querySelectorAll('#res-tbl tbody tr').forEach(row=>{
      row.style.display=(!cut||new Date(row.dataset.ts)>=cut)?'':'none';
    });
  }
  function fltC(){
    document.querySelectorAll('.flt-btn').forEach(b=>b.classList.remove('active'));
    const f=document.getElementById('rf').value,t=document.getElementById('rt').value;
    const fd=f?new Date(f):null,td=t?new Date(t+'T23:59:59'):null;
    document.querySelectorAll('#res-tbl tbody tr').forEach(row=>{
      const ts=new Date(row.dataset.ts);
      row.style.display=((!fd||ts>=fd)&&(!td||ts<=td))?'':'none';
    });
  }
  </script>
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
      <tr>
        <th>Time ({{ tz_offset }})</th>
        <th>Asset</th>
        <th>YES Price</th>
        <th>Momentum%</th>
        <th>Expires</th>
        <th>Markets Found</th>
        <th>Nearest (s)</th>
        <th>Reason</th>
      </tr>
    </thead>
    <tbody>
    {% for r in skips %}
      <tr>
        <td class="grey">{{ r.get('timestamp','') | fmt_ts }}</td>
        <td>{{ r.get('asset','—') }}</td>
        <td>{{ "$%.3f"|format(r.yes_price) if r.get('yes_price') is not none else "—" }}</td>
        <td class="{{ 'green' if (r.get('momentum_pct') or 0) >= 0 else 'red' }}">
          {{ "%+.3f"|format(r.momentum_pct) if r.get('momentum_pct') is not none else "—" }}
        </td>
        <td class="grey">{{ r.seconds_to_expiry|int ~ "s" if r.get('seconds_to_expiry') is not none else "—" }}</td>
        <td class="{{ 'yellow' if r.get('markets_found', 1) == 0 else 'grey' }}">
          {{ r.get('markets_found', '—') }}
        </td>
        <td class="{{ 'yellow' if r.get('nearest_market_secs') is not none and r.get('nearest_market_secs') > 300 else 'grey' }}">
          {{ r.get('nearest_market_secs', '—') }}
        </td>
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
        nav            = NAV.format(now=_now_local().strftime("%Y-%m-%d %H:%M") + " (" + _tz_label() + ")"),
        resolved       = resolved,
        won_count      = len(won),
        lost_count     = len(lost),
        win_rate       = win_rate,
        total_pnl      = total_pnl,
        avg_win        = avg_win,
        avg_loss       = avg_loss,
        skips          = list(reversed(skips[:200])),
        skip_breakdown = skip_breakdown,
        tz_offset      = _tz_offset_label(),
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


@app.route("/analyze")
def analyze_page():
    try:
        from analytics import get_full_analysis
        data = get_full_analysis()
    except Exception as e:
        tb = traceback.format_exc()
        return f"<pre style='background:#0d1117;color:#f85149;padding:20px;font-family:monospace'>Analytics load error:\n{tb}</pre>", 500

    ANALYZE_EXTRA_CSS = """
    .rec-card { background:#161b22; border:1px solid #30363d; border-radius:8px;
                padding:16px 20px; margin-bottom:12px; }
    .rec-card.high   { border-left:3px solid #3fb950; }
    .rec-card.medium { border-left:3px solid #d29922; }
    .rec-card.low    { border-left:3px solid #8b949e; }
    .rec-title { font-weight:bold; color:#e6edf3; margin-bottom:6px; font-size:1em; }
    .rec-reason { color:#8b949e; font-size:0.85em; margin-bottom:8px; }
    .rec-impact { color:#58a6ff; font-size:0.82em; margin-bottom:10px; }
    .apply-btn { background:#238636; color:#fff; border:none; padding:5px 14px;
                 border-radius:5px; cursor:pointer; font-family:monospace; font-size:0.82em; }
    .apply-btn:hover { background:#2ea043; }
    .change-old { color:#f85149; text-decoration:line-through; }
    .change-new { color:#3fb950; font-weight:bold; }
    .best { background:#1a3d1a !important; }
    """

    def _tbl(rows, col_label="Range"):
        if not rows:
            return "<p class='grey' style='padding:10px 0'>Not enough data yet.</p>"
        max_wr = max((r["win_rate"] for r in rows), default=0)
        h = f"<table><thead><tr><th>{col_label}</th><th>Trades</th><th>Won</th><th>Lost</th>"
        h += "<th>Win Rate</th><th>Total P&L</th><th>Avg P&L</th></tr></thead><tbody>"
        for r in rows:
            best = ' class="best"' if r["win_rate"] == max_wr and max_wr > 0 else ""
            wr_cls = "green" if r["win_rate"] >= 55 else ("yellow" if r["win_rate"] >= 45 else "red")
            pnl_cls = "pnl-pos" if r["total_pnl"] >= 0 else "pnl-neg"
            apnl_cls = "pnl-pos" if r["avg_pnl"] >= 0 else "pnl-neg"
            h += f"<tr{best}><td>{r['range']}</td><td class='blue'>{r['trades']}</td>"
            h += f"<td class='green'>{r['won']}</td><td class='red'>{r['lost']}</td>"
            h += f"<td class='{wr_cls}'>{r['win_rate']}%</td>"
            h += f"<td class='{pnl_cls}'>${r['total_pnl']:+.2f}</td>"
            h += f"<td class='{apnl_cls}'>${r['avg_pnl']:+.2f}</td></tr>"
        h += "</tbody></table>"
        return h

    def _whatif_tbl(rows, param_label, current_val):
        if not rows:
            return "<p class='grey' style='padding:10px 0'>Not enough data yet.</p>"
        max_wr = max((r["win_rate"] for r in rows if r["trades"] >= 3), default=0)
        h = f"<table><thead><tr><th>{param_label}</th><th>Qualifying Trades</th>"
        h += "<th>Filtered Out</th><th>Win Rate</th><th>Total P&L</th>"
        h += "<th>vs Current</th></tr></thead><tbody>"
        for r in rows:
            is_current = abs(r["threshold"] - current_val) < 0.001
            best = ' class="best"' if r["win_rate"] == max_wr and r["trades"] >= 3 and max_wr > 0 else ""
            cur_label = " ← current" if is_current else ""
            wr_cls = "green" if r["win_rate"] >= 55 else ("yellow" if r["win_rate"] >= 45 else ("red" if r["win_rate"] > 0 else "grey"))
            pnl_cls = "pnl-pos" if r["total_pnl"] >= 0 else "pnl-neg"
            h += f"<tr{best}><td><b>≥ {r['threshold']}</b>{cur_label}</td>"
            h += f"<td class='blue'>{r['trades']}</td>"
            h += f"<td class='grey'>{r['filtered_out']}</td>"
            h += f"<td class='{wr_cls}'>{r['win_rate']}%</td>"
            h += f"<td class='{pnl_cls}'>${r['total_pnl']:+.2f}</td>"
            if is_current:
                h += "<td class='grey'>—</td>"
            else:
                best_in_whatif = next((x for x in rows if x["win_rate"] == max_wr and x["trades"] >= 3), None)
                if best_in_whatif and not is_current:
                    r_wr        = r["win_rate"]
                    r_threshold = r["threshold"]
                    r_reason    = f"What-if analysis: win rate {r_wr}% at threshold {r_threshold}"
                    param_key   = "min_momentum_pct" if "momentum" in param_label.lower() else "entry_threshold"
                    h += "<td><form method='POST' action='/apply-setting' style='display:inline'>"
                    h += f"<input type='hidden' name='param' value='{param_key}'>"
                    h += f"<input type='hidden' name='value' value='{r_threshold}'>"
                    h += f"<input type='hidden' name='reason' value='{r_reason}'>"
                    h += "<button type='submit' class='apply-btn'>Apply</button></form></td>"
                else:
                    h += "<td>—</td>"
            h += "</tr>"
        h += "</tbody></table>"
        return h

    recs_html = ""
    for rec in data["recommendations"]:
        conf  = rec.get("confidence", "low")
        title = f"{rec['parameter']}: {rec['current']} → {rec['suggested']}" if rec["parameter"] and rec["suggested"] else "Observation"
        recs_html += f"<div class='rec-card {conf}'>"
        recs_html += f"<div class='rec-title'>{title}</div>"
        recs_html += f"<div class='rec-reason'>{rec['reason']}</div>"
        if rec.get("impact"):
            recs_html += f"<div class='rec-impact'>{rec['impact']}</div>"
        if rec["parameter"] and rec["suggested"] and rec["parameter"] not in ("side_filter",):
            recs_html += f"""<form method='POST' action='/apply-setting' style='display:inline'>
              <input type='hidden' name='param' value='{rec["parameter"]}'>
              <input type='hidden' name='value' value='{rec["suggested"]}'>
              <input type='hidden' name='reason' value='{rec["reason"][:120]}'>
              <button type='submit' class='apply-btn'>&#10003; Apply This Change</button>
            </form>"""
        recs_html += "</div>"

    changelog_html = ""
    for c in data["change_log"]:
        ts = _fmt_ts(c.get("timestamp", ""))
        src_cls = "green" if c.get("source") == "recommended" else "blue"
        changelog_html += f"""<tr>
          <td class='grey'>{ts}</td>
          <td class='yellow'>{c.get('parameter','?')}</td>
          <td><span class='change-old'>{c.get('old_value','?')}</span></td>
          <td><span class='change-new'>{c.get('new_value','?')}</span></td>
          <td class='{src_cls}'>{c.get('source','manual')}</td>
          <td class='grey'>{c.get('reason','')[:80]}</td>
        </tr>"""

    cfg  = data["current_config"]
    sa   = data.get("skip_analysis", {})

    # Build skip analysis section HTML
    _sa_color = "red" if sa.get("consecutive_recent", 0) >= 10 else "yellow"
    _no_mkt_color = "red" if sa.get("no_market_pct", 0) >= 70 else "yellow"
    skip_cards_html = f"""
<div class="cards">
  <div class="card">
    <div class="label">Total Skips</div>
    <div class="value grey">{sa.get('total_skips', 0)}</div>
  </div>
  <div class="card">
    <div class="label">Skip Rate</div>
    <div class="value yellow">{sa.get('skip_rate', 0)}%</div>
  </div>
  <div class="card">
    <div class="label">Consecutive Now</div>
    <div class="value {_sa_color}">{sa.get('consecutive_recent', 0)}</div>
  </div>
  <div class="card">
    <div class="label">No Market %</div>
    <div class="value {_no_mkt_color}">{sa.get('no_market_pct', 0)}%</div>
  </div>
  <div class="card">
    <div class="label">Avg Nearest (s)</div>
    <div class="value grey">{sa.get('avg_nearest_secs') or '—'}</div>
  </div>
</div>"""

    # Skip reason breakdown table
    skip_reason_rows = ""
    for reason, count in sa.get("by_reason", []):
        total_skip = sa.get("total_skips", 1) or 1
        pct = round(count / total_skip * 100, 1)
        color = "red" if reason == "no tradeable markets" else "yellow" if pct >= 20 else "grey"
        skip_reason_rows += f"<tr><td class='grey'>{reason}</td><td class='{color}'>{count}</td><td class='{color}'>{pct}%</td></tr>"

    skip_html = skip_cards_html + (
        f"<table style='max-width:500px;margin-top:12px'><thead><tr><th>Skip Reason</th><th>Count</th><th>%</th></tr></thead><tbody>{skip_reason_rows}</tbody></table>"
        if skip_reason_rows else "<p class='grey' style='padding:10px 0'>No skips recorded yet.</p>"
    )

    # Alert if "no tradeable markets" dominates
    no_mkt_pct = sa.get("no_market_pct", 0)
    consecutive = sa.get("consecutive_recent", 0)
    skip_alert = ""
    if no_mkt_pct >= 50:
        skip_alert = f"""<div style="background:#3d1a1a;border:1px solid #f85149;border-radius:6px;
          padding:10px 16px;margin-bottom:16px;color:#f85149;font-size:0.88em">
          &#9888; {no_mkt_pct}% of skips are "no tradeable markets" — the bot cannot find live {cfg.get('window','5m')} markets
          during many of its cron runs. Consider switching to the 15m window (more availability)
          or verify Polymarket has active fast markets during your bot's running hours.
          {f'Currently <b>{consecutive}</b> consecutive skips.' if consecutive >= 5 else ''}
        </div>"""

    tmpl = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>FastLoop Analysis</title>
  <meta http-equiv="refresh" content="120">
  <style>{{{{ css | safe }}}}{{{{ extra_css | safe }}}}</style>
</head>
<body>
  {{{{ nav | safe }}}}

  {skip_alert}

  <h2>Skip / Market Availability Analysis</h2>
  {skip_html}

  <h2>Overall Performance</h2>
  <div class="cards">
    <div class="card">
      <div class="label">Resolved Trades</div>
      <div class="value blue">{data['total_resolved']}</div>
    </div>
    <div class="card">
      <div class="label">Overall Win Rate</div>
      <div class="value {'green' if data['overall_win_rate'] >= 50 else 'red'}">{data['overall_win_rate']}%</div>
    </div>
    <div class="card">
      <div class="label">Enriched Records</div>
      <div class="value grey">{data['total_enriched']}</div>
    </div>
    <div class="card">
      <div class="label">Min Momentum</div>
      <div class="value yellow">{cfg.get('min_momentum_pct', '?')}%</div>
    </div>
    <div class="card">
      <div class="label">Entry Threshold</div>
      <div class="value yellow">{cfg.get('entry_threshold', '?')}</div>
    </div>
  </div>

  <h2>Recommendations</h2>
  {recs_html if recs_html else "<p class='grey'>No recommendations yet.</p>"}

  <h2>Performance by Momentum Strength</h2>
  <p class="grey" style="font-size:0.82em;margin-bottom:10px">Highlighted row = best win rate. Use what-if table below to find optimal threshold.</p>
  {_tbl(data['by_momentum'], 'Momentum Range')}

  <h2>Performance by Divergence</h2>
  {_tbl(data['by_divergence'], 'Divergence Range')}

  <h2>Performance by Side (YES vs NO)</h2>
  {_tbl(data['by_side'], 'Trade Side')}

  <h2>Performance by YES Price at Entry</h2>
  {_tbl(data['by_yes_price'], 'YES Price Range')}

  <h2>What-If: Min Momentum Threshold</h2>
  <p class="grey" style="font-size:0.82em;margin-bottom:10px">Shows how win rate changes if you only trade when momentum exceeds each threshold. Current: <b>{cfg.get('min_momentum_pct', 0.03)}%</b></p>
  {_whatif_tbl(data['whatif_momentum'], 'Min Momentum %', float(cfg.get('min_momentum_pct', 0.03)))}

  <h2>What-If: Entry Threshold (Divergence)</h2>
  <p class="grey" style="font-size:0.82em;margin-bottom:10px">Shows how win rate changes if you only trade when price divergence exceeds each value. Current: <b>{cfg.get('entry_threshold', 0.05)}</b></p>
  {_whatif_tbl(data['whatif_divergence'], 'Entry Threshold', float(cfg.get('entry_threshold', 0.05)))}

  <h2>Settings Change Log</h2>
  {'<table><thead><tr><th>Time (' + _tz_offset_label() + ')</th><th>Parameter</th><th>Old Value</th><th>New Value</th><th>Source</th><th>Reason</th></tr></thead><tbody>' + changelog_html + '</tbody></table>' if changelog_html else "<p class='grey' style='padding:10px 0'>No changes recorded yet.</p>"}

</body>
</html>"""

    try:
        return render_template_string(
            tmpl,
            css       = CSS,
            extra_css = ANALYZE_EXTRA_CSS,
            nav       = NAV.format(now=_now_local().strftime("%Y-%m-%d %H:%M") + " (" + _tz_label() + ")"),
        )
    except Exception as e:
        tb = traceback.format_exc()
        return f"<pre style='background:#0d1117;color:#f85149;padding:20px;font-family:monospace'>Analyze render error:\n{tb}</pre>", 500


@app.route("/apply-setting", methods=["POST"])
def apply_setting():
    param  = request.form.get("param", "").strip()
    value  = request.form.get("value", "").strip()
    reason = request.form.get("reason", "Applied from analysis page").strip()

    ALLOWED = {
        "min_momentum_pct":  float,
        "entry_threshold":   float,
        "max_position":      float,
        "daily_budget":      float,
        "lookback_minutes":  int,
        "min_time_remaining":int,
        "asset":             str,
        "window":            str,
        "signal_source":     str,
        "volume_confidence": lambda v: v.lower() in ("true","1","yes"),
    }

    if param not in ALLOWED:
        return f"<p style='color:#f85149;font-family:monospace;padding:20px'>Unknown parameter: {param}</p>", 400

    try:
        cast      = ALLOWED[param]
        new_value = cast(value)
    except Exception:
        return f"<p style='color:#f85149;font-family:monospace;padding:20px'>Invalid value for {param}: {value}</p>", 400

    cfg      = _load_config()
    old_value = cfg.get(param, "not set")
    cfg[param] = new_value
    _save_config(cfg)

    try:
        from analytics import log_setting_change
        log_setting_change(param, old_value, new_value, reason, source="recommended")
    except Exception:
        pass

    return redirect("/analyze")


@app.route("/health")
def health():
    return "ok", 200


@app.route("/admin/reset-portfolio", methods=["POST"])
def reset_portfolio_route():
    from paper_portfolio import reset_portfolio
    balance = float(request.form.get("balance", 100.0))
    reset_portfolio(starting_balance=balance)
    return f"Portfolio reset to ${balance:.2f}", 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def start_dashboard():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_dashboard()
