# Paper Desk — cloud edition

Fake-money paper-trading bot. `pd_engine.py` fetches real crypto (Crypto.com,
no key needed) and stock (Financial Modeling Prep) prices, runs one trading
cycle, and writes the result to `state.json`. `index.html` is a static
dashboard (meant for GitHub Pages) that reads `state.json` and renders
equity, positions, trade log, and a per-cycle activity log explaining what
the bot did (or didn't do) and why.

## One-time setup

1. **Get a free FMP API key**: sign up at
   https://financialmodelingprep.com/developer/docs — the free tier is
   enough for 30 stock symbols on an hourly schedule.
2. **Enable GitHub Pages**: repo Settings → Pages → Source: "Deploy from a
   branch" → Branch: `main`, folder `/ (root)` → Save. GitHub gives you a
   URL like `https://cruzlucas49.github.io/paper-desk-bot/` — that's your
   dashboard, viewable anytime from any device.
3. **Create the Claude Code Routine** (claude.ai/code/routines, or run
   `/schedule` inside a Claude Code session in this repo):
   - Repo: this one (`cruzlucas49/paper-desk-bot`)
   - Trigger: scheduled, hourly (`0 * * * *`) — that's the minimum interval
     for cloud routines
   - Environment variable: `FMP_API_KEY` = (the key from step 1) — set this
     in the routine's cloud environment config, not in the prompt itself
   - Prompt: paste the block below exactly

```
Run one Paper Desk trading cycle in this repo, fully unattended, no
questions, no confirmation:

1. Run: python3 pd_engine.py
2. If state.json changed, commit and push directly to the main branch:
   git add state.json && git commit -m "Trading cycle $(date -u +%Y-%m-%dT%H:%M:%SZ)" && git push origin main
3. Do NOT open a pull request and do NOT create a claude/-prefixed branch —
   this is an automated data sync, not a code change that needs review.
   Push straight to main every time.
4. Report back only the one-line summary that pd_engine.py printed to
   stdout. Nothing else.
```

## Files

- `pd_engine.py` — the trading engine. Safe to run manually any time:
  `python3 pd_engine.py` (reads/writes `state.json` in the current
  directory).
- `state.json` — current bot state (cash, positions, trade log, cycle log).
  Starts fresh at $1,000, no positions.
- `index.html` — the dashboard. Pure static HTML/CSS/JS, no build step,
  fetches `state.json` from the same folder every 60s.

## Notes

- Crypto data needs no key (Crypto.com's public ticker endpoint).
- If `FMP_API_KEY` isn't set, the engine still runs — it just skips stocks
  and trades crypto only, and says so in `errors`.
- Position sizing is ~8% of equity per trade, capped at 12 concurrent
  positions, with stop-loss/take-profit/score-based exits and self-tuning
  thresholds after every 6+ closed trades. See `pd_engine.py` for exact
  logic.
