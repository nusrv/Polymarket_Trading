#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles. Falls back to Coinbase if Binance fails.

This version adds console JSON logging:
- one JSON record per cron run
- paper trades and skips are both logged to stdout
- easy to copy from Railway logs into your dashboard
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

sys.stdout.reconfigure(line_buffering=True)

try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False

        def log_trade(*args, **kwargs):
            pass


CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "env": "SIMMER_SPRINT_ENTRY", "type": float},
    "min_momentum_pct": {"default": 0.5, "env": "SIMMER_SPRINT_MOMENTUM", "type": float},
    "max_position": {"default": 5.0, "env": "SIMMER_SPRINT_MAX_POSITION", "type": float},
    "signal_source": {"default": "binance", "env": "SIMMER_SPRINT_SIGNAL", "type": str},
    "lookback_minutes": {"default": 5, "env": "SIMMER_SPRINT_LOOKBACK", "type": int},
    "min_time_remaining": {"default": 0, "env": "SIMMER_SPRINT_MIN_TIME", "type": int},
    "asset": {"default": "BTC", "env": "SIMMER_SPRINT_ASSET", "type": str},
    "window": {"default": "5m", "env": "SIMMER_SPRINT_WINDOW", "type": str},
    "volume_confidence": {"default": True, "env": "SIMMER_SPRINT_VOL_CONF", "type": bool},
    "daily_budget": {"default": 10.0, "env": "SIMMER_SPRINT_DAILY_BUDGET", "type": float},
}

TRADE_SOURCE = "sdk:fastloop"
SKILL_SLUG = "polymarket-fast-loop"
SMART_SIZING_PCT = 0.05
MIN_SHARES_PER_ORDER = 5
MAX_SPREAD_PCT = 0.10

ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}

COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
}

WINDOW_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}

from simmer_sdk.skill import load_config, update_config, get_config_path


def _env_bool(name):
    val = os.environ.get(name)
    if val is None:
        return None
    return val.lower() in ("true", "1", "yes", "y", "on")


def _env_alias_override(cfg, key, aliases, cast):
    for env_name in aliases:
        raw = os.environ.get(env_name)
        if raw not in (None, ""):
            try:
                if cast == bool:
                    cfg[key] = _env_bool(env_name)
                else:
                    cfg[key] = cast(raw)
                return
            except Exception:
                pass


cfg = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-fast-loop")

_env_alias_override(cfg, "entry_threshold", [
    "SIMMER_FASTLOOP_ENTRY_THRESHOLD",
    "SIMMER_SPRINT_ENTRY",
], float)
_env_alias_override(cfg, "min_momentum_pct", [
    "SIMMER_FASTLOOP_MOMENTUM_THRESHOLD",
    "SIMMER_SPRINT_MOMENTUM",
], float)
_env_alias_override(cfg, "max_position", [
    "SIMMER_FASTLOOP_MAX_POSITION_USD",
    "SIMMER_SPRINT_MAX_POSITION",
], float)
_env_alias_override(cfg, "signal_source", [
    "SIMMER_FASTLOOP_SIGNAL_SOURCE",
    "SIMMER_SPRINT_SIGNAL",
], str)
_env_alias_override(cfg, "lookback_minutes", [
    "SIMMER_FASTLOOP_LOOKBACK_MINUTES",
    "SIMMER_SPRINT_LOOKBACK",
], int)
_env_alias_override(cfg, "min_time_remaining", [
    "SIMMER_FASTLOOP_MIN_TIME_REMAINING",
    "SIMMER_SPRINT_MIN_TIME",
], int)
_env_alias_override(cfg, "asset", [
    "SIMMER_FASTLOOP_ASSET",
    "SIMMER_SPRINT_ASSET",
], str)
_env_alias_override(cfg, "window", [
    "SIMMER_FASTLOOP_WINDOW",
    "SIMMER_SPRINT_WINDOW",
], str)
_env_alias_override(cfg, "volume_confidence", [
    "SIMMER_FASTLOOP_VOLUME_CONFIDENCE",
    "SIMMER_FASTLOOP_VOL_CONFIDENCE",
    "SIMMER_SPRINT_VOL_CONF",
], bool)
_env_alias_override(cfg, "daily_budget", [
    "SIMMER_FASTLOOP_DAILY_BUDGET_USD",
    "SIMMER_SPRINT_DAILY_BUDGET",
], float)

ENTRY_THRESHOLD = cfg["entry_threshold"]
MIN_MOMENTUM_PCT = cfg["min_momentum_pct"]
MAX_POSITION_USD = cfg["max_position"]
AUTOMATON_MAX = os.environ.get("AUTOMATON_MAX_BET")
if AUTOMATON_MAX:
    MAX_POSITION_USD = min(MAX_POSITION_USD, float(AUTOMATON_MAX))
SIGNAL_SOURCE = cfg["signal_source"]
LOOKBACK_MINUTES = cfg["lookback_minutes"]
ASSET = cfg["asset"].upper()
WINDOW = cfg["window"]
VOLUME_CONFIDENCE = cfg["volume_confidence"]
DAILY_BUDGET = cfg["daily_budget"]

CONFIGURED_MIN_TIME = cfg["min_time_remaining"]
if CONFIGURED_MIN_TIME > 0:
    MIN_TIME_REMAINING = CONFIGURED_MIN_TIME
else:
    MIN_TIME_REMAINING = max(30, WINDOW_SECONDS.get(WINDOW, 300) // 10)

POLY_FEE_RATE = 0.25
POLY_FEE_EXPONENT = 2


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def emit_console_record(record):
    payload = {
        "timestamp": _now_iso(),
        "asset": ASSET,
        "window": WINDOW,
        "entry_threshold": ENTRY_THRESHOLD,
        "min_momentum_pct": MIN_MOMENTUM_PCT,
        "max_position_usd": MAX_POSITION_USD,
        "lookback_minutes": LOOKBACK_MINUTES,
        "min_time_remaining": MIN_TIME_REMAINING,
        "volume_confidence": VOLUME_CONFIDENCE,
        "daily_budget": DAILY_BUDGET,
        **record,
    }
    print("PAPER_TRADE_JSON::" + json.dumps(payload, ensure_ascii=False))



def _get_spend_path(skill_file):
    from pathlib import Path
    return Path(skill_file).parent / "daily_spend.json"


def _load_daily_spend(skill_file):
    spend_path = _get_spend_path(skill_file)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if spend_path.exists():
        try:
            with open(spend_path) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "spent": 0.0, "trades": 0}


def _save_daily_spend(skill_file, spend_data):
    spend_path = _get_spend_path(skill_file)
    with open(spend_path, "w") as f:
        json.dump(spend_data, f, indent=2)


_client = None


def get_client(live=True):
    global _client
    if _client is None:
        try:
            from simmer_sdk import SimmerClient
        except ImportError:
            print("Error: simmer-sdk not installed. Run: pip install simmer-sdk")
            sys.exit(1)

        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            print("Error: SIMMER_API_KEY environment variable not set")
            sys.exit(1)

        venue = os.environ.get("TRADING_VENUE", "polymarket")
        _client = SimmerClient(api_key=api_key, venue=venue, live=live)
    return _client


def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "simmer-fastloop-market/1.0"

        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            return {"error": error_body.get("detail", str(e)), "status_code": e.code}
        except Exception:
            return {"error": str(e), "status_code": e.code}
    except URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


CLOB_API = "https://clob.polymarket.com"


def _lookup_fee_rate(token_id):
    result = _api_request(f"{CLOB_API}/fee-rate?token_id={quote(str(token_id))}", timeout=5)
    if not result or not isinstance(result, dict) or result.get("error"):
        return 0
    try:
        return int(float(result.get("base_fee") or 0))
    except (ValueError, TypeError):
        return 0


def fetch_live_midpoint(token_id):
    result = _api_request(f"{CLOB_API}/midpoint?token_id={quote(str(token_id))}", timeout=5)
    if not result or not isinstance(result, dict) or result.get("error"):
        return None
    try:
        return float(result["mid"])
    except (KeyError, ValueError, TypeError):
        return None


def fetch_live_prices(clob_token_ids):
    if not clob_token_ids:
        return None
    return fetch_live_midpoint(clob_token_ids[0])


def fetch_orderbook_summary(clob_token_ids):
    if not clob_token_ids:
        return None
    yes_token = clob_token_ids[0]
    result = _api_request(f"{CLOB_API}/book?token_id={quote(str(yes_token))}", timeout=5)
    if not result or not isinstance(result, dict):
        return None

    bids = result.get("bids", [])
    asks = result.get("asks", [])
    if not bids or not asks:
        return None

    try:
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2
        spread_pct = spread / mid if mid > 0 else 0

        bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids[:5])
        ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:5])

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": spread_pct,
            "bid_depth_usd": bid_depth,
            "ask_depth_usd": ask_depth,
        }
    except (KeyError, ValueError, IndexError, TypeError):
        return None


def _parse_resolves_at(resolves_at_str):
    try:
        s = resolves_at_str.replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_fast_market_end_time(question):
    import re
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None

    try:
        from zoneinfo import ZoneInfo
        date_str = match.group(1)
        time_str = match.group(2)
        year = datetime.now(timezone.utc).year
        dt_str = f"{date_str} {year} {time_str}"
        dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p")
        et = ZoneInfo("America/New_York")
        return dt.replace(tzinfo=et).astimezone(timezone.utc)
    except Exception:
        return None


def _discover_via_gamma(asset="BTC", window="5m"):
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=500&closed=false&tag=crypto&order=endDate&ascending=true"
    )
    result = _api_request(url)
    if not result or (isinstance(result, dict) and result.get("error")):
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        matches_window = f"-{window}-" in slug
        if any(p in q for p in patterns) and matches_window:
            if m.get("closed", False):
                continue
            end_time = _parse_fast_market_end_time(m.get("question", ""))
            clob_tokens_raw = m.get("clobTokenIds", "[]")
            if isinstance(clob_tokens_raw, str):
                try:
                    clob_tokens = json.loads(clob_tokens_raw)
                except (json.JSONDecodeError, ValueError):
                    clob_tokens = []
            else:
                clob_tokens = clob_tokens_raw or []

            markets.append({
                "question": m.get("question", ""),
                "slug": slug,
                "end_time": end_time,
                "clob_token_ids": clob_tokens,
                "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                "source": "gamma",
                "is_live_now": None,
            })
    return markets


def _remaining_seconds(market, now=None):
    now = now or datetime.now(timezone.utc)
    end_time = market.get("end_time")
    if not end_time:
        return None
    return (end_time - now).total_seconds()


def _infer_market_live(market, now=None):
    now = now or datetime.now(timezone.utc)
    remaining = _remaining_seconds(market, now)
    if remaining is None:
        return False, None
    window_seconds = WINDOW_SECONDS.get(WINDOW, 300)
    return 0 < remaining <= window_seconds, remaining


def _dedupe_markets(markets):
    seen = {}
    for m in markets:
        key = (m.get("question"), str(m.get("end_time")))
        if key not in seen:
            seen[key] = m
        else:
            if seen[key].get("source") != "simmer" and m.get("source") == "simmer":
                seen[key] = m
    return list(seen.values())


def _focus_markets_near_now(markets, window):
    now = datetime.now(timezone.utc)
    window_seconds = WINDOW_SECONDS.get(window, 300)

    clean = [m for m in markets if m.get("end_time") is not None]
    clean.sort(key=lambda m: m["end_time"])

    near_now = []
    for m in clean:
        remaining = (m["end_time"] - now).total_seconds()
        if -window_seconds <= remaining <= (window_seconds * 24):
            near_now.append(m)

    if near_now:
        return near_now
    return clean[:150]


def discover_fast_market_markets(asset="BTC", window="5m"):
    markets = []

    try:
        client = get_client()
        sdk_markets = client.get_fast_markets(asset=asset, window=window, limit=300)
        if sdk_markets:
            for m in sdk_markets:
                end_time = _parse_resolves_at(m.resolves_at) if getattr(m, "resolves_at", None) else None
                clob_tokens = [m.polymarket_token_id] if getattr(m, "polymarket_token_id", None) else []
                if getattr(m, "polymarket_no_token_id", None):
                    clob_tokens.append(m.polymarket_no_token_id)

                markets.append({
                    "question": m.question,
                    "market_id": m.id,
                    "end_time": end_time,
                    "clob_token_ids": clob_tokens,
                    "is_live_now": getattr(m, "is_live_now", None),
                    "spread_cents": getattr(m, "spread_cents", None),
                    "liquidity_tier": getattr(m, "liquidity_tier", None),
                    "external_price_yes": getattr(m, "external_price_yes", None),
                    "fee_rate_bps": getattr(m, "fee_rate_bps", 0),
                    "source": "simmer",
                })
    except Exception as e:
        print(f"  ⚠️  Simmer fast-markets API failed ({e})")

    gamma_markets = _discover_via_gamma(asset, window)
    markets.extend(gamma_markets)

    markets = _dedupe_markets(markets)
    markets = _focus_markets_near_now(markets, window)

    print("\nDEBUG MARKET SAMPLE:")
    for m in markets[:20]:
        print({
            "question": m.get("question"),
            "end_time": str(m.get("end_time")),
            "is_live_now": m.get("is_live_now"),
            "source": m.get("source"),
        })

    return markets


def find_best_fast_market(markets):
    now = datetime.now(timezone.utc)
    live_candidates = []
    near_future_candidates = []

    for m in markets:
        inferred_live, remaining = _infer_market_live(m, now)
        simmer_live = m.get("is_live_now")
        live_now = simmer_live is True or inferred_live

        if remaining is None:
            continue

        if live_now and remaining > MIN_TIME_REMAINING:
            live_candidates.append((remaining, m))
        elif MIN_TIME_REMAINING < remaining <= (WINDOW_SECONDS.get(WINDOW, 300) * 2):
            near_future_candidates.append((remaining, m))

    if live_candidates:
        live_candidates.sort(key=lambda x: x[0])
        return live_candidates[0][1]

    if near_future_candidates:
        near_future_candidates.sort(key=lambda x: x[0])
        return near_future_candidates[0][1]

    return None


def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&limit={lookback_minutes}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None

    try:
        candles = result
        if len(candles) < 2:
            return None

        price_then = float(candles[0][1])
        price_now = float(candles[-1][4])
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = volumes[-1]
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
            "source_used": "binance",
        }
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def get_coinbase_momentum(asset="BTC", lookback_minutes=5):
    product = COINBASE_PRODUCTS.get(asset, "BTC-USD")
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(minutes=lookback_minutes + 1)

    url = (
        f"https://api.exchange.coinbase.com/products/{product}/candles"
        f"?granularity=60"
        f"&start={quote(start_dt.isoformat())}"
        f"&end={quote(end_dt.isoformat())}"
    )

    headers = {"User-Agent": "simmer-fastloop-market/1.0"}
    result = _api_request(url, headers=headers)
    if not result or isinstance(result, dict):
        return None

    try:
        candles = sorted(result, key=lambda x: x[0])
        if len(candles) < 2:
            return None

        price_then = float(candles[0][3])
        price_now = float(candles[-1][4])
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = volumes[-1]
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
            "source_used": "coinbase",
        }
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def get_momentum(asset="BTC", source="binance", lookback=5):
    if source == "binance":
        symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
        data = get_binance_momentum(symbol, lookback)
        if data:
            return data
        print("  ⚠️  Binance failed — falling back to Coinbase")
        return get_coinbase_momentum(asset, lookback)

    if source == "coinbase":
        return get_coinbase_momentum(asset, lookback)

    if source == "coingecko":
        print("  ⚠️  CoinGecko candle path not implemented here — using Coinbase instead")
        return get_coinbase_momentum(asset, lookback)

    print(f"  ⚠️  Unknown signal source '{source}' — using Coinbase fallback")
    return get_coinbase_momentum(asset, lookback)


def import_fast_market_market(slug):
    url = f"https://polymarket.com/event/{slug}"
    try:
        result = get_client().import_market(url)
    except Exception as e:
        return None, str(e)

    if not result:
        return None, "No response from import endpoint"
    if result.get("error"):
        return None, result.get("error", "Unknown error")

    status = result.get("status")
    market_id = result.get("market_id")

    if status == "resolved":
        alternatives = result.get("active_alternatives", [])
        if alternatives:
            return None, f"Market resolved. Try alternative: {alternatives[0].get('id')}"
        return None, "Market resolved, no alternatives found"

    if status in ("imported", "already_exists"):
        return market_id, None

    return None, f"Unexpected status: {status}"


def get_portfolio():
    try:
        return get_client().get_portfolio()
    except Exception as e:
        return {"error": str(e)}


def get_positions():
    try:
        positions = get_client().get_positions()
        from dataclasses import asdict
        return [asdict(p) for p in positions]
    except Exception:
        return []


def execute_trade(market_id, side, amount):
    try:
        result = get_client().trade(
            market_id=market_id,
            side=side,
            amount=amount,
            source=TRADE_SOURCE,
            skill_slug=SKILL_SLUG,
        )
        return {
            "success": result.success,
            "trade_id": result.trade_id,
            "shares_bought": result.shares_bought,
            "shares": result.shares_bought,
            "error": result.error,
            "simulated": result.simulated,
        }
    except Exception as e:
        return {"error": str(e)}


def calculate_position_size(max_size, smart_sizing=False):
    if not smart_sizing:
        return max_size
    portfolio = get_portfolio()
    if not portfolio or portfolio.get("error"):
        return max_size
    balance = portfolio.get("balance_usdc", 0)
    if balance <= 0:
        return max_size
    return min(balance * SMART_SIZING_PCT, max_size)


def run_fast_market_strategy(dry_run=True, positions_only=False, show_config=False,
                             smart_sizing=False, quiet=False):
    def log(msg, force=False):
        if not quiet or force:
            print(msg)

    def log_skip(reason, **extra):
        emit_console_record({"status": "skip", "reason": reason, **extra})

    log("⚡ Simmer FastLoop Trading Skill")
    log("=" * 50)

    if dry_run:
        log("\n  [PAPER MODE] Trades will be simulated with real prices. Use --live for real trades.")

    log(f"\n⚙️  Configuration:")
    log(f"  Asset:            {ASSET}")
    log(f"  Window:           {WINDOW}")
    log(f"  Entry threshold:  {ENTRY_THRESHOLD} (min divergence from 50¢)")
    log(f"  Min momentum:     {MIN_MOMENTUM_PCT}% (min price move)")
    log(f"  Max position:     ${MAX_POSITION_USD:.2f}")
    log(f"  Signal source:    {SIGNAL_SOURCE}")
    log(f"  Lookback:         {LOOKBACK_MINUTES} minutes")
    log(f"  Min time left:    {MIN_TIME_REMAINING}s")
    log(f"  Volume weighting: {'✓' if VOLUME_CONFIDENCE else '✗'}")
    daily_spend = _load_daily_spend(__file__)
    log(f"  Daily budget:     ${DAILY_BUDGET:.2f} (${daily_spend['spent']:.2f} spent today, {daily_spend['trades']} trades)")

    if show_config:
        config_path = get_config_path(__file__)
        log(f"\n  Config file: {config_path}")
        return

    get_client(live=not dry_run)

    if positions_only:
        log("\n📊 Sprint Positions:")
        positions = get_positions()
        fast_positions = [p for p in positions if "up or down" in (p.get("question", "") or "").lower()]
        if not fast_positions:
            log("  No open fast market positions")
        else:
            for pos in fast_positions:
                log(f"  • {pos.get('question', 'Unknown')[:60]}")
        return

    log(f"\n🔍 Discovering {ASSET} fast markets...")
    markets = discover_fast_market_markets(ASSET, WINDOW)
    log(f"  Found {len(markets)} active fast markets")

    if markets:
        sample = next((m for m in markets if m.get("clob_token_ids")), None)
        if sample and sample.get("fee_rate_bps", 0) == 0:
            fee = _lookup_fee_rate(sample["clob_token_ids"][0])
            if fee > 0:
                log(f"  Fee rate for {WINDOW} markets: {fee} bps ({fee/100:.0f}%)")
                for m in markets:
                    m["fee_rate_bps"] = fee

    best = find_best_fast_market(markets)
    if not best:
        now = datetime.now(timezone.utc)
        for m in markets[:40]:
            remaining = _remaining_seconds(m, now)
            if remaining is None:
                log(f"  Skipped: {m['question'][:50]}... (no end_time available)")
            elif m.get("is_live_now") is False:
                log(f"  Skipped: {m['question'][:50]}... (not live yet; {remaining:.0f}s until expiry)")
            else:
                log(f"  Skipped: {m['question'][:50]}... ({remaining:.0f}s remaining)")
        log(f"  No live tradeable markets among {len(markets)} found — waiting for next window")
        print(f"📊 Summary: No tradeable markets (0/{len(markets)} live with enough time)")
        log_skip("no tradeable markets", markets_found=len(markets))
        return

    end_time = best.get("end_time")
    remaining = (end_time - datetime.now(timezone.utc)).total_seconds() if end_time else 0
    log(f"\n🎯 Selected: {best['question']}")
    log(f"  Expires in: {remaining:.0f}s")

    clob_tokens = best.get("clob_token_ids", [])
    live_price = fetch_live_prices(clob_tokens) if clob_tokens else None
    if live_price is None:
        print("📊 Summary: No trade (CLOB price unavailable)")
        log_skip(
            "CLOB price unavailable",
            market=best.get("question"),
            seconds_to_expiry=remaining,
        )
        return

    market_yes_price = live_price
    log(f"  Current YES price: ${market_yes_price:.3f} (live CLOB)")

    fee_rate_bps = best.get("fee_rate_bps", 0)
    if fee_rate_bps > 0:
        _p = market_yes_price if market_yes_price <= 0.5 else (1 - market_yes_price)
        _eff = POLY_FEE_RATE * (_p * (1 - _p)) ** POLY_FEE_EXPONENT
        log(f"  Fee rate:         {_eff:.2%} effective at current price (feeRateBps={fee_rate_bps})")

    log(f"\n📈 Fetching {ASSET} price signal ({SIGNAL_SOURCE})...")
    momentum = get_momentum(ASSET, SIGNAL_SOURCE, LOOKBACK_MINUTES)
    if not momentum:
        log("  ❌ Failed to fetch price data", force=True)
        log_skip(
            "failed to fetch price data",
            market=best.get("question"),
            seconds_to_expiry=remaining,
            yes_price=market_yes_price,
        )
        return

    log(f"  Source used: {momentum.get('source_used', SIGNAL_SOURCE)}")
    log(f"  Price: ${momentum['price_now']:,.2f} (was ${momentum['price_then']:,.2f})")
    log(f"  Momentum: {momentum['momentum_pct']:+.3f}%")
    log(f"  Direction: {momentum['direction']}")
    if VOLUME_CONFIDENCE:
        log(f"  Volume ratio: {momentum['volume_ratio']:.2f}x avg")

    log("\n🧠 Analyzing...")

    momentum_pct = abs(momentum["momentum_pct"])
    direction = momentum["direction"]

    if momentum_pct < MIN_MOMENTUM_PCT:
        print(f"📊 Summary: No trade (momentum too weak: {momentum_pct:.3f}%)")
        log_skip(
            "momentum too weak",
            market=best.get("question"),
            seconds_to_expiry=remaining,
            yes_price=market_yes_price,
            momentum_pct=momentum["momentum_pct"],
            source_used=momentum.get("source_used"),
        )
        return

    if direction == "up":
        side = "yes"
        divergence = 0.50 + ENTRY_THRESHOLD - market_yes_price
        trade_rationale = f"{ASSET} up {momentum['momentum_pct']:+.3f}% but YES only ${market_yes_price:.3f}"
    else:
        side = "no"
        divergence = market_yes_price - (0.50 - ENTRY_THRESHOLD)
        trade_rationale = f"{ASSET} down {momentum['momentum_pct']:+.3f}% but YES still ${market_yes_price:.3f}"

    if VOLUME_CONFIDENCE and momentum["volume_ratio"] < 0.5:
        print("📊 Summary: No trade (low volume)")
        log_skip(
            "low volume",
            market=best.get("question"),
            side=side.upper(),
            seconds_to_expiry=remaining,
            yes_price=market_yes_price,
            momentum_pct=momentum["momentum_pct"],
            divergence=divergence,
            volume_ratio=momentum["volume_ratio"],
            source_used=momentum.get("source_used"),
        )
        return

    if divergence <= 0:
        print("📊 Summary: No trade (market already priced in)")
        log_skip(
            "market already priced in",
            market=best.get("question"),
            side=side.upper(),
            seconds_to_expiry=remaining,
            yes_price=market_yes_price,
            momentum_pct=momentum["momentum_pct"],
            divergence=divergence,
            source_used=momentum.get("source_used"),
        )
        return

    position_size = calculate_position_size(MAX_POSITION_USD, smart_sizing)
    price = market_yes_price if side == "yes" else (1 - market_yes_price)

    remaining_budget = DAILY_BUDGET - daily_spend["spent"]
    if remaining_budget <= 0:
        print("📊 Summary: No trade (daily budget exhausted)")
        log_skip(
            "daily budget exhausted",
            market=best.get("question"),
            side=side.upper(),
            seconds_to_expiry=remaining,
            yes_price=market_yes_price,
            momentum_pct=momentum["momentum_pct"],
            divergence=divergence,
            source_used=momentum.get("source_used"),
        )
        return
    if position_size > remaining_budget:
        position_size = remaining_budget
    if position_size < 0.50:
        print("📊 Summary: No trade (remaining budget too small)")
        log_skip(
            "remaining budget too small",
            market=best.get("question"),
            side=side.upper(),
            seconds_to_expiry=remaining,
            yes_price=market_yes_price,
            momentum_pct=momentum["momentum_pct"],
            divergence=divergence,
            source_used=momentum.get("source_used"),
        )
        return

    if price > 0:
        min_cost = MIN_SHARES_PER_ORDER * price
        if min_cost > position_size:
            print("📊 Summary: No trade (position too small)")
            log_skip(
                "position too small",
                market=best.get("question"),
                side=side.upper(),
                seconds_to_expiry=remaining,
                yes_price=market_yes_price,
                momentum_pct=momentum["momentum_pct"],
                divergence=divergence,
                size_usd=position_size,
                source_used=momentum.get("source_used"),
            )
            return

    log(f"  ✅ Signal: {side.upper()} — {trade_rationale}", force=True)
    log(f"  Divergence: {divergence:.3f}", force=True)

    if best.get("market_id"):
        market_id = best["market_id"]
    else:
        market_id, import_error = import_fast_market_market(best["slug"])
        if not market_id:
            print(f"📊 Summary: No trade (import failed: {import_error})")
            log_skip(
                f"import failed: {import_error}",
                market=best.get("question"),
                side=side.upper(),
                seconds_to_expiry=remaining,
                yes_price=market_yes_price,
                momentum_pct=momentum["momentum_pct"],
                divergence=divergence,
                source_used=momentum.get("source_used"),
            )
            return

    tag = "SIMULATED" if dry_run else "LIVE"
    log(f"  Executing {side.upper()} trade for ${position_size:.2f} ({tag})...", force=True)
    result = execute_trade(market_id, side, position_size)

    if result and result.get("success"):
        shares = result.get("shares_bought") or result.get("shares") or 0
        log(f"  ✅ {'[PAPER] ' if result.get('simulated') else ''}Bought {shares:.1f} {side.upper()} shares @ ${price:.3f}", force=True)

        emit_console_record({
            "status": "paper" if result.get("simulated") else "live",
            "market": best.get("question"),
            "side": side.upper(),
            "yes_price": market_yes_price,
            "momentum_pct": momentum["momentum_pct"],
            "divergence": divergence,
            "size_usd": position_size,
            "shares": shares,
            "reason": "paper trade" if result.get("simulated") else "live trade",
            "source_used": momentum.get("source_used"),
            "seconds_to_expiry": remaining,
        })

        if not result.get("simulated"):
            daily_spend["spent"] += position_size
            daily_spend["trades"] += 1
            _save_daily_spend(__file__, daily_spend)

        if result.get("trade_id") and JOURNAL_AVAILABLE and not result.get("simulated"):
            confidence = min(0.9, 0.5 + divergence + (momentum_pct / 100))
            log_trade(
                trade_id=result["trade_id"],
                source=TRADE_SOURCE,
                skill_slug=SKILL_SLUG,
                thesis=trade_rationale,
                confidence=round(confidence, 2),
                asset=ASSET,
                momentum_pct=round(momentum["momentum_pct"], 3),
                volume_ratio=round(momentum["volume_ratio"], 2),
                signal_source=SIGNAL_SOURCE,
            )

        print("\n📊 Summary:")
        print(f"  Sprint: {best['question'][:50]}")
        print(f"  Signal: {direction} {momentum_pct:.3f}% | YES ${market_yes_price:.3f}")
        print(f"  Action: {'PAPER' if dry_run else 'TRADED'}")
    else:
        error = result.get("error", "Unknown error") if result else "No response"
        log(f"  ❌ Trade failed: {error}", force=True)
        log_skip(
            f"trade failed: {error}",
            market=best.get("question"),
            side=side.upper(),
            yes_price=market_yes_price,
            momentum_pct=momentum["momentum_pct"],
            divergence=divergence,
            size_usd=position_size,
            source_used=momentum.get("source_used"),
            seconds_to_expiry=remaining,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer FastLoop Trading Skill")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="(Default) Show opportunities without trading")
    parser.add_argument("--positions", action="store_true", help="Show current fast market positions")
    parser.add_argument("--config", action="store_true", help="Show current config")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Update config (e.g., --set entry_threshold=0.08)")
    parser.add_argument("--smart-sizing", action="store_true", help="Use portfolio-based position sizing")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only output on trades/errors")
    args = parser.parse_args()

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set format: {item}. Use KEY=VALUE")
                sys.exit(1)
            key, val = item.split("=", 1)
            if key in CONFIG_SCHEMA:
                type_fn = CONFIG_SCHEMA[key].get("type", str)
                try:
                    if type_fn == bool:
                        updates[key] = val.lower() in ("true", "1", "yes")
                    else:
                        updates[key] = type_fn(val)
                except ValueError:
                    print(f"Invalid value for {key}: {val}")
                    sys.exit(1)
            else:
                print(f"Unknown config key: {key}")
                sys.exit(1)

        update_config(updates, __file__)
        print(f"✅ Config updated: {json.dumps(updates)}")
        sys.exit(0)

    dry_run = not args.live

    run_fast_market_strategy(
        dry_run=dry_run,
        positions_only=args.positions,
        show_config=args.config,
        smart_sizing=args.smart_sizing,
        quiet=args.quiet,
    )
