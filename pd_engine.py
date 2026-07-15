#!/usr/bin/env python3
"""
Paper Desk trading engine — cloud/repo edition.

Self-contained: fetches its own live market data (no MCP tools needed), reads
state.json from the current directory, runs one trading cycle, writes
state.json back. Meant to be run every cycle by a Claude Code Routine, then
git-committed and pushed straight to main.

Requires one environment variable:
  FMP_API_KEY  — free key from https://financialmodelingprep.com/developer/docs

Crypto data comes from Crypto.com's public ticker endpoint (no auth needed).
"""

import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ---------- constants ----------
STABLE_SYMBOLS = {
    "USDC", "USDT", "DAI", "TUSD", "FDUSD", "PYUSD", "USDP", "GUSD", "USDD", "EUR", "EURT", "USTC", "USDE", "USD",
}
STOCK_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD", "NFLX", "JPM",
    "V", "MA", "DIS", "KO", "PEP", "WMT", "XOM", "CVX", "JNJ", "UNH",
    "HD", "NKE", "INTC", "CRM", "ADBE", "PYPL", "UBER", "COIN", "SHOP", "PLTR",
]
CRYPTO_UNIVERSE_SIZE = 50
STARTING_CASH = 1000
MAX_POSITIONS = 12
MIN_HELD_PER_CLASS = 5
MIN_TRADE_DOLLARS = 10
CYCLE_LOG_MAX = 200

DEFAULT_PARAMS = {
    "buyThreshold": 68,
    "sellThreshold": 35,
    "riskPerTradePct": 0.08,
    "stopLossPct": -8,
    "takeProfitPct": 18,
    "momentumWeight": 0.55,
}
PARAM_BOUNDS = {
    "buyThreshold": (55, 85),
    "sellThreshold": (20, 45),
    "riskPerTradePct": (0.03, 0.15),
    "stopLossPct": (-15, -4),
    "takeProfitPct": (8, 30),
    "momentumWeight": (0.3, 0.8),
}
ADAPT_WINDOW = 10
ADAPT_MIN_TRADES = 6
ROTATION_MARGIN = 10
MAX_ROTATIONS_PER_CYCLE = 3

UA = "Mozilla/5.0 (paper-desk-bot cloud routine)"


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def now_ms():
    return int(time.time() * 1000)


def to_float(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def clamp_param(key, val):
    lo, hi = PARAM_BOUNDS[key]
    return clamp(val, lo, hi)


def min_max_norm(val, lo, hi):
    if hi == lo:
        return 50
    return clamp(((val - lo) / (hi - lo)) * 100, 0, 100)


# ---------- market data fetch ----------

def fetch_crypto_raw():
    return http_get_json("https://api.crypto.com/exchange/v1/public/get-tickers")


def fetch_stock_raw(api_key):
    symbols = ",".join(STOCK_WATCHLIST)
    url = f"https://financialmodelingprep.com/stable/quote?symbol={symbols}&apikey={api_key}"
    return http_get_json(url)


def _crypto_symbol(name):
    """Raw endpoint uses e.g. 'BTC_USDT', 'BTC_USD', or perps like 'WALUSD-PERP'."""
    if not isinstance(name, str):
        return None
    if name.endswith("_USDT"):
        return name[:-5]
    if name.endswith("_USD"):
        return name[:-4]
    return None


def extract_crypto_rows(raw):
    if isinstance(raw, dict) and isinstance(raw.get("result"), dict) and isinstance(raw["result"].get("data"), list):
        lst = raw["result"]["data"]
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        lst = raw["data"]
    elif isinstance(raw, list):
        lst = raw
    else:
        raise ValueError("Unexpected Crypto.com tickers response shape")

    by_symbol = {}
    for t in lst:
        name = t.get("i")
        if not isinstance(name, str) or "PERP" in name:
            continue
        symbol = _crypto_symbol(name)
        if not symbol or symbol in STABLE_SYMBOLS:
            continue
        price = to_float(t.get("k"))
        change_raw = to_float(t.get("c"))
        vol_value = to_float(t.get("vv"))
        if not price or not vol_value or vol_value <= 0:
            continue
        row = {
            "id": f"crypto-{symbol}",
            "cls": "crypto",
            "symbol": symbol,
            "name": symbol,
            "price": price,
            "changePct": change_raw * 100 if change_raw is not None else None,
            "volume": vol_value,
        }
        # dedupe BTC_USD vs BTC_USDT etc — keep whichever has more $ volume
        existing = by_symbol.get(symbol)
        if not existing or (row["volume"] or 0) > (existing["volume"] or 0):
            by_symbol[symbol] = row

    rows = list(by_symbol.values())
    rows.sort(key=lambda r: r["volume"] or 0, reverse=True)
    return rows[:CRYPTO_UNIVERSE_SIZE]


def map_stock_row(q):
    price = to_float(q.get("price"))
    change_pct = to_float(q.get("changePercentage"))
    if change_pct is None:
        change_pct = to_float(q.get("changesPercentage"))
    volume = to_float(q.get("volume"))
    symbol = q.get("symbol")
    return {
        "id": f"stock-{symbol}",
        "cls": "stock",
        "symbol": symbol,
        "name": q.get("name") or symbol,
        "price": price,
        "changePct": change_pct,
        "volume": volume,
    }


def extract_stock_rows(raw):
    if isinstance(raw, list):
        lst = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        lst = raw["data"]
    else:
        raise ValueError("Unexpected FMP quote response shape")

    rows = []
    for q in lst:
        if q and q.get("symbol") and q.get("price"):
            rows.append(map_stock_row(q))
    return rows


def score_group(rows, momentum_weight):
    if not rows:
        return rows
    mw = 0.55 if momentum_weight is None else momentum_weight
    changes = [r["changePct"] for r in rows if r["changePct"] is not None]
    volumes = [r["volume"] for r in rows if r["volume"] is not None]
    c_min = min(changes + [0])
    c_max = max(changes + [0])
    v_min = min(volumes + [0])
    v_max = max(volumes + [1])
    out = []
    for r in rows:
        momentum = min_max_norm(r["changePct"], c_min, c_max) if r["changePct"] is not None else 40
        vol_rank = min_max_norm(r["volume"], v_min, v_max) if r["volume"] is not None else 40
        score = round(momentum * mw + vol_rank * (1 - mw))
        row = dict(r)
        row["score"] = score
        row["momentum"] = momentum
        row["volRank"] = vol_rank
        out.append(row)
    return out


def fresh_state():
    return {
        "cash": STARTING_CASH,
        "startingCash": STARTING_CASH,
        "positions": {},
        "tradeLog": [],
        "closedTrades": [],
        "equityHistory": [],
        "cycleLog": [],
        "params": dict(DEFAULT_PARAMS),
        "universeSize": 0,
        "errors": [],
        "fetchedAt": None,
    }


def compute_equity(state):
    pos_value = sum(p["qty"] * (p.get("price") or p["avgCost"]) for p in state["positions"].values())
    return state["cash"] + pos_value


def log_trade(state, entry):
    entry = dict(entry)
    entry["ts"] = now_ms()
    state["tradeLog"].insert(0, entry)
    del state["tradeLog"][60:]


def evaluate_positions(state, by_id):
    for pid, pos in state["positions"].items():
        live = by_id.get(pid)
        if live and live.get("price") is not None:
            pos["price"] = live["price"]
            pos["score"] = live["score"]
            pos["changePct"] = live["changePct"]
            pos["stale"] = False
        else:
            pos["stale"] = True
        history = pos.setdefault("priceHistory", [])
        if pos.get("price") is not None:
            history.append({"t": now_ms(), "price": pos["price"]})
        if len(history) > 500:
            del history[: len(history) - 500]


def sell_position(state, pid, reason, events):
    pos = state["positions"].get(pid)
    if not pos or pos.get("price") is None:
        return
    proceeds = pos["qty"] * pos["price"]
    cost = pos["qty"] * pos["avgCost"]
    pnl = proceeds - cost
    pnl_pct = (pnl / cost * 100) if cost > 0 else 0
    state["cash"] += proceeds
    log_trade(state, {
        "action": "SELL", "cls": pos["cls"], "symbol": pos["symbol"], "qty": pos["qty"],
        "price": pos["price"], "value": proceeds, "reason": reason, "pnl": pnl, "pnlPct": pnl_pct,
    })
    state["closedTrades"].append({
        "pnl": pnl, "pnlPct": pnl_pct, "ts": now_ms(),
        "momentumAtEntry": pos.get("momentumAtEntry"), "volRankAtEntry": pos.get("volRankAtEntry"),
    })
    if len(state["closedTrades"]) > 200:
        state["closedTrades"] = state["closedTrades"][-200:]
    events.append(f"Sold {pos['symbol']} — {reason}, {pnl_pct:+.1f}% (${pnl:+.2f})")
    del state["positions"][pid]


def check_exits(state, events):
    p = state["params"]
    to_sell = []
    for pid, pos in state["positions"].items():
        if pos.get("price") is None:
            continue
        pnl_pct = ((pos["price"] - pos["avgCost"]) / pos["avgCost"]) * 100
        reason = None
        if pnl_pct <= p["stopLossPct"]:
            reason = "stop-loss"
        elif pnl_pct >= p["takeProfitPct"]:
            reason = "take-profit"
        elif not pos.get("stale") and pos.get("score") is not None and pos["score"] <= p["sellThreshold"]:
            reason = "score-exit"
        if reason:
            to_sell.append((pid, reason))
    for pid, reason in to_sell:
        sell_position(state, pid, reason, events)


def check_entries(state, all_scored, events):
    held_ids = set(state["positions"].keys())
    open_slots = MAX_POSITIONS - len(held_ids)
    if open_slots <= 0:
        return

    held_counts = {"crypto": 0, "stock": 0}
    for pos in state["positions"].values():
        held_counts[pos["cls"]] = held_counts.get(pos["cls"], 0) + 1

    def build_queue(cls):
        held = held_counts.get(cls, 0)
        bar = max(50, state["params"]["buyThreshold"] - 15) if held < MIN_HELD_PER_CLASS else state["params"]["buyThreshold"]
        cands = [
            r for r in all_scored
            if r["cls"] == cls and r["id"] not in held_ids and r["score"] >= bar
            and r["changePct"] is not None and r["changePct"] > 0 and r["price"] is not None and r["price"] > 0
        ]
        cands.sort(key=lambda r: r["score"], reverse=True)
        return cands

    queues = {"crypto": build_queue("crypto"), "stock": build_queue("stock")}
    order = ["stock", "crypto"] if held_counts["stock"] <= held_counts["crypto"] else ["crypto", "stock"]

    progressed = True
    while open_slots > 0 and progressed:
        progressed = False
        for cls in order:
            if open_slots <= 0:
                break
            q = queues[cls]
            if not q:
                continue
            cand = q.pop(0)
            equity = compute_equity(state)
            alloc = min(equity * state["params"]["riskPerTradePct"], state["cash"])
            if alloc < MIN_TRADE_DOLLARS:
                continue
            qty = alloc / cand["price"]
            state["cash"] -= alloc
            entry_ts = now_ms()
            state["positions"][cand["id"]] = {
                "cls": cand["cls"], "symbol": cand["symbol"], "name": cand["name"], "qty": qty,
                "avgCost": cand["price"], "price": cand["price"], "score": cand["score"],
                "changePct": cand["changePct"], "stale": False,
                "momentumAtEntry": cand["momentum"], "volRankAtEntry": cand["volRank"],
                "entryTs": entry_ts,
                "priceHistory": [{"t": entry_ts, "price": cand["price"]}],
            }
            log_trade(state, {
                "action": "BUY", "cls": cand["cls"], "symbol": cand["symbol"], "qty": qty,
                "price": cand["price"], "value": alloc, "reason": "score-entry",
            })
            events.append(f"Bought {cand['symbol']} — score {cand['score']}, ${alloc:.2f}")
            held_counts[cls] = held_counts.get(cls, 0) + 1
            open_slots -= 1
            progressed = True


def check_rotations(state, all_scored, events):
    if len(state["positions"]) < MAX_POSITIONS:
        return

    held_ids = set(state["positions"].keys())
    candidates = [
        r for r in all_scored
        if r["id"] not in held_ids and r["changePct"] is not None and r["changePct"] > 0
        and r["price"] is not None and r["price"] > 0
    ]
    if not candidates:
        return
    candidates.sort(key=lambda r: r["score"], reverse=True)

    rotations = 0
    for cand in candidates:
        if rotations >= MAX_ROTATIONS_PER_CYCLE:
            break
        held_same_class = [
            (pid, pos) for pid, pos in state["positions"].items()
            if pos["cls"] == cand["cls"] and pos.get("score") is not None and not pos.get("stale")
        ]
        if not held_same_class:
            continue
        weakest_pid, weakest_pos = min(held_same_class, key=lambda kv: kv[1]["score"])
        if cand["score"] - weakest_pos["score"] >= ROTATION_MARGIN:
            sell_position(state, weakest_pid, "rotation", events)
            rotations += 1


def adapt_strategy(state, events):
    recent = state["closedTrades"][-ADAPT_WINDOW:]
    if len(recent) < ADAPT_MIN_TRADES:
        return

    wins = [t for t in recent if t["pnl"] > 0]
    losses = [t for t in recent if t["pnl"] <= 0]
    win_rate = len(wins) / len(recent)

    p = state["params"]
    before = dict(p)

    if win_rate >= 0.6:
        p["buyThreshold"] = clamp_param("buyThreshold", p["buyThreshold"] - 1.5)
        p["riskPerTradePct"] = clamp_param("riskPerTradePct", p["riskPerTradePct"] + 0.01)
    elif win_rate <= 0.4:
        p["buyThreshold"] = clamp_param("buyThreshold", p["buyThreshold"] + 1.5)
        p["riskPerTradePct"] = clamp_param("riskPerTradePct", p["riskPerTradePct"] - 0.01)

    if losses:
        avg_loss_pct = sum(t["pnlPct"] for t in losses) / len(losses)
        p["stopLossPct"] = clamp_param("stopLossPct", (p["stopLossPct"] + avg_loss_pct * 1.1) / 2)
    if wins:
        avg_win_pct = sum(t["pnlPct"] for t in wins) / len(wins)
        p["takeProfitPct"] = clamp_param("takeProfitPct", (p["takeProfitPct"] + avg_win_pct * 1.1) / 2)

    if wins:
        avg_momentum = sum(t.get("momentumAtEntry") or 50 for t in wins) / len(wins)
        avg_vol_rank = sum(t.get("volRankAtEntry") or 50 for t in wins) / len(wins)
        target = p["momentumWeight"] + 0.02 if avg_momentum >= avg_vol_rank else p["momentumWeight"] - 0.02
        p["momentumWeight"] = clamp_param("momentumWeight", target)

    p["sellThreshold"] = clamp_param("sellThreshold", min(p["sellThreshold"], p["buyThreshold"] - 25))

    if round(before["buyThreshold"], 1) != round(p["buyThreshold"], 1):
        events.append(
            f"Tuned strategy — win rate {win_rate*100:.0f}% over last {len(recent)} trades, "
            f"buyThreshold {before['buyThreshold']:.1f} -> {p['buyThreshold']:.1f}"
        )


def run_trading_cycle(state, all_scored):
    events = []
    by_id = {r["id"]: r for r in all_scored}
    evaluate_positions(state, by_id)
    check_exits(state, events)
    adapt_strategy(state, events)
    check_rotations(state, all_scored, events)
    check_entries(state, all_scored, events)
    return events


def build_note(state, all_scored, events):
    crypto_n = sum(1 for r in all_scored if r["cls"] == "crypto")
    stock_n = sum(1 for r in all_scored if r["cls"] == "stock")
    held_ids = set(state["positions"].keys())
    unheld = [r for r in all_scored if r["id"] not in held_ids]
    scan_txt = f"Scanned {len(all_scored)} assets ({crypto_n} crypto + {stock_n} stocks)."
    if not unheld:
        return scan_txt
    best = max(unheld, key=lambda r: r["score"])
    bar = state["params"]["buyThreshold"]
    if events:
        return f"{scan_txt} Best unheld candidate now: {best['symbol']} (score {best['score']})."
    return (
        f"{scan_txt} Held {len(state['positions'])} positions, no changes this cycle. "
        f"Best unheld candidate: {best['symbol']} (score {best['score']}, needs >= {bar:.0f} to buy)."
    )


def sanitize(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def main():
    state_path = sys.argv[1] if len(sys.argv) > 1 else "state.json"

    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    else:
        state = fresh_state()
    for k, v in fresh_state().items():
        if k not in state:
            state[k] = v
    if not state.get("params"):
        state["params"] = dict(DEFAULT_PARAMS)

    errors = []

    crypto_rows = []
    try:
        crypto_raw = fetch_crypto_raw()
        crypto_rows = extract_crypto_rows(crypto_raw)
    except Exception as e:
        errors.append(f"Crypto feed failed: {e} — using last known prices.")

    stock_rows = []
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        errors.append("FMP_API_KEY not set — skipping stock feed, using last known prices.")
    else:
        try:
            stock_raw = fetch_stock_raw(api_key)
            stock_rows = extract_stock_rows(stock_raw)
        except Exception as e:
            errors.append(f"Stock feed failed: {e} — using last known prices.")

    scored_crypto = score_group(crypto_rows, state["params"]["momentumWeight"])
    scored_stocks = score_group(stock_rows, state["params"]["momentumWeight"])
    all_scored = scored_crypto + scored_stocks

    events = run_trading_cycle(state, all_scored)
    note = build_note(state, all_scored, events)

    equity = compute_equity(state)
    state["equityHistory"].append({"t": now_ms(), "equity": equity})
    if len(state["equityHistory"]) > 500:
        state["equityHistory"] = state["equityHistory"][-500:]

    state["errors"] = errors
    state["universeSize"] = len(all_scored)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["fetchedAt"] = fetched_at

    state.setdefault("cycleLog", []).insert(0, {
        "ts": now_ms(),
        "fetchedAt": fetched_at,
        "equity": round(equity, 2),
        "cash": round(state["cash"], 2),
        "events": events,
        "note": note,
        "errors": errors,
    })
    del state["cycleLog"][CYCLE_LOG_MAX:]

    state = sanitize(state)

    with open(state_path, "w") as f:
        json.dump(state, f, separators=(",", ":"), allow_nan=False)

    crypto_held = sum(1 for p in state["positions"].values() if p["cls"] == "crypto")
    stock_held = sum(1 for p in state["positions"].values() if p["cls"] == "stock")
    realized = sum(t["pnl"] for t in state["closedTrades"])
    summary = (
        f"equity=${equity:.2f} cash=${state['cash']:.2f} "
        f"positions={crypto_held}crypto+{stock_held}stock realizedPnl=${realized:.2f} "
        f"universe={state['universeSize']} events={len(events)} errors={len(errors)}"
    )
    print(summary)
    for e in events:
        print("  " + e)
    if not events:
        print("  " + note)


if __name__ == "__main__":
    main()
