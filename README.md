# Polymarket Smart Copy Trading Bot

A production-grade copy-trading system for [Polymarket](https://polymarket.com)
binary prediction markets, with a full operator dashboard. Watches a
configurable set of on-chain wallets (or a synthetic source for offline
testing), evaluates every trade through a tunable risk pipeline, sizes
with fractional Kelly, executes adaptively on the CLOB, and manages
exits independently of the source trader.

> **Status:** paper-mode by default. The full pipeline runs end-to-end
> with simulated fills. Going live requires a CLOB order signer (see
> _Going live_) and explicitly setting `execution.dry_run: false` in
> the YAML — there is no UI button that can lift that ceiling.

## What it does

```
┌─────────────────────┐   ┌───────────┐   ┌─────────┐   ┌──────┐   ┌───────────┐   ┌──────────┐
│ Signal Source       │ → │ Filter    │ → │ Sizer   │ → │ Risk │ → │ Execution │ → │ Portfolio│
│ (poll | ws | demo)  │   │ (book/sc) │   │ (Kelly) │   │      │   │ (CLOB)    │   │ + Exits  │
└─────────────────────┘   └───────────┘   └─────────┘   └──────┘   └───────────┘   └──────────┘
                                                                                          ↓
                                                                                  ┌──────────────┐
                                                                                  │ Trader Stats │
                                                                                  │ (per-cat)    │
                                                                                  └──────────────┘
```

Every signal lands in a JSONL decision journal so you can replay the
whole pipeline post-hoc and explain why each trade happened (or didn't).
A FastAPI + React dashboard visualises live state, exposes every admin
action the CLI does, and lets you flip runtime modes (paper/live,
smart/blind) without touching files.

## Design principles

- **Don't blindly copy.** Filter for liquidity, score traders Bayesianly,
  and detect adverse selection so size shrinks on the flow you're being
  picked off on.
- **Risk-first.** Kelly fraction, per-trade caps, per-market caps,
  correlation-group caps, daily/weekly drawdown stops, kill-switch file,
  per-wallet cutoffs. All before a single share is bought.
- **Latency matters.** Polling is fine for backtesting; for live you want
  the WebSocket source.
- **Observable.** Prometheus `/metrics`, a JSONL decision journal, a SQLite
  audit log of admin actions, and a dashboard that surfaces it all.
- **Modular.** Every layer is a swappable class. Want a different scorer?
  Sub-class `TraderScorer`. Different signer? Inject one. Different signal
  source? Implement two methods.

## Quick start (paper mode, offline)

```bash
git clone <this repo> && cd <this repo>

# 1) install deps
pip install -r requirements.txt
pip install -r dashboard/requirements.txt
(cd dashboard/web && npm install && npm run build)

# 2) flip demo on so the bot actually has signals to react to
#    (set demo.enabled: true in bot/config.yaml; default is off)

# 3) run the bot
python -m bot.main --config bot/config.yaml &

# 4) run the dashboard
DASHBOARD_API_KEY=$(openssl rand -hex 24) \
DASHBOARD_BOT_DB_PATH=state/bot_state.sqlite \
DASHBOARD_BOT_CONFIG_PATH=bot/config.yaml \
DASHBOARD_DECISIONS_LOG_PATH=logs/decisions.jsonl \
DASHBOARD_STATIC_DIR=dashboard/web/dist \
uvicorn dashboard.app.main:app --host 127.0.0.1 --port 8080 &

# 5) open http://127.0.0.1:8080 and paste your API key
```

Or via Docker compose:

```bash
DASHBOARD_API_KEY=$(openssl rand -hex 24) docker compose up -d
# bot:        127.0.0.1:9090   (Prometheus metrics)
# prometheus: 127.0.0.1:9091
# grafana:    127.0.0.1:3000   (anon Admin)
# dashboard:  127.0.0.1:8080
```

## Running modes

Three orthogonal switches. The YAML is the **ceiling**; runtime overrides
can only restrict, never escalate.

| Switch | Default | Where | What it does |
|---|---|---|---|
| `execution.dry_run` | `true` | YAML | Hard ceiling on whether real orders can be signed. `true` = paper-only forever. |
| `tracker.source` | `poll` | YAML | `poll` = data-API polling (~2 s). `websocket` = sub-100 ms CLOB stream. |
| `demo.enabled` | `false` | YAML | Synthetic signal/book source for fully offline runs. |
| `kv_state['execution_mode']` | absent | runtime (dashboard `POST /api/execution_mode`) | Force paper at runtime. Refused if YAML pins paper (cannot escalate). |
| `kv_state['copy_mode']` | absent (smart) | runtime (dashboard `POST /api/copy_mode`) | `smart` = use per-(trader, category) score + adverse-selection drift penalty. `blind` = naive 1:1 copier; ignores both. |
| `kv_state['global_halt_reason']` | absent | runtime (dashboard `POST /api/halt`) | Stops new entries; existing positions continue to be exited. |
| `safety.kill_switch_file` | empty | YAML / `touch` | If the file path exists on disk, every entry is blocked immediately. |

The bot reloads runtime overrides on its 60 s maintenance tick (and the
kill-switch file is checked on every signal). All changes take effect
without a restart.

## The smart-vs-blind toggle (the differentiator)

The dashboard's **Controls → Copy mode** panel toggles between two
distinct behaviours:

- **SMART (default)** — the `PositionSizer` consults two extra signals:
  1. `TraderScorer.score(wallet, category=...)` — per-(trader, market category)
     Bayesian shrinkage. Same trader has different edge in different markets;
     a 0%-WR-on-macro / 75%-WR-on-sports trader gets sized down only on
     macro signals, not all signals. Categories come from
     `risk.correlation_groups` in the YAML.
  2. `AdverseSelectionObserver.drift_penalty(wallet, token)` — rolling
     mean of post-fill drift converted into edge units, subtracted from
     `implied_edge`. Persistent picked-off flow shrinks itself.

- **BLIND** — the bot reverts to global trader score with no
  category split, no drift penalty. Useful as the unfiltered baseline
  when A/B-ing the smart layer's incremental edge.

You flip between them from the dashboard at any time. The bot picks up
the change on the next maintenance tick. Both modes share the same
filter/risk/exit infrastructure — only the sizer changes.

## Three latency / quality edges (recently shipped)

1. **WebSocket signal source** (`bot/core/websocket_tracker.py`).
   `tracker.source: websocket` swaps the 2 s data-API poll for a CLOB
   user-channel subscription with sub-100 ms message latency. Includes
   reconnect with exponential backoff (max 30 s), pluggable parser, and
   pluggable connector for tests. The single biggest edge in copy
   trading.

2. **Adverse-selection feedback loop**
   (`bot/core/enhancements.py:AdverseSelectionObserver`). Used to be
   observation-only; now produces a `drift_penalty(wallet, token)` that
   the sizer subtracts in SMART mode. Saturates at `max_penalty`
   (default 0.05 edge units) once mean drift hits `penalty_bps_scale`
   (default 100 bps). Closes the loop without operator intervention.

3. **Per-(trader, category) Bayesian shrinkage**
   (`bot/core/trader_scorer.py:_category_score`). Combines a trader's
   global Beta-posterior with per-category history; a trader who's
   brilliant on sports doesn't get sized up on macro and vice versa.
   Shrinks toward the global prior when category data is sparse.

## Risk controls (in evaluation order)

1. **Kill-switch file** — file presence on disk → every entry blocked.
2. **Global halt** — operator-set in `kv_state` or auto-tripped by drawdown.
3. **Per-trader cutoff** — set by the risk manager on consecutive losses,
   max-drawdown breach, or operator action.
4. **Daily soft stop** — no new entries after `daily_soft_stop_pct` daily DD.
5. **Weekly hard stop** — `weekly_drawdown_stop_pct` trips the global halt.
6. **Per-trade cap** — `sizing.max_pct_per_trade` of bankroll.
7. **Per-market cap** — `sizing.max_pct_per_market` of bankroll.
8. **Correlation-group cap** — `risk.max_pct_per_correlation_group`.
9. **Max open positions / max global exposure** — global notional limits.

The sizer's internal `nonpositive_kelly`, `dust`, and `slippage_abort`
gates filter further before an order goes out.

## Demo mode (offline test loop)

Polymarket has no testnet. To exercise the full pipeline without any
network access — local development, demos, sandboxed environments — set
`demo.enabled: true`. The `WalletTracker` emits synthetic TradeSignals
from configured demo wallets/markets at the configured rate, the
`ClobClient` serves synthetic order books for those tokens, and (since
`dry_run` is paper-locked by default) fills are simulated locally.
Trader stats are auto-seeded with positive history so the sizer
produces non-zero positions; otherwise every signal is rejected with
`nonpositive_kelly`.

```yaml
demo:
  enabled: true
  signals_per_minute: 30.0
  sell_probability: 0.20
  wallets:
    - "0xdemo000000000000000000000000000000000001"
  markets:
    - {market_id: "demo-trump-2028", token_id: "demo-tok-trump-yes", price: 0.42, outcome: "YES", liquidity: 30000}
```

In a fresh demo run the dashboard sees 5+ open positions and 80+ decisions in 30 seconds.

## Operator surfaces

### Dashboard pages

| Path | Purpose |
|---|---|
| `/` (Overview) | KPIs, equity sparkline, halt banner, paper/live banner, copy-mode banner, dry-run badge |
| `/positions` | Open + closed positions, sortable, wallet filter |
| `/traders` | Ranked trader stats with composite scores and cutoff badges |
| `/decisions` | Live-tailing JSONL feed; filter by event type |
| `/replay` | Mirror of `python -m bot.cli replay`: histogram of events + reject reasons |
| `/controls` | Halt/resume, trader cutoff/uncutoff, paper/live, smart/blind toggles |
| `/config` | Read-only filtered view of every YAML knob currently in effect |

### REST API

All under `/api/*`. All require `X-API-Key` except `/api/health`. Rate-limited (10 fail/60s/IP → 429).

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | Public liveness; reports `db_ok` / `decisions_log_ok` |
| `/api/summary` | GET | KPIs, halt state, dry-run flag, equity, exposure |
| `/api/summary/equity_series` | GET | Mark-to-market equity history (downsampled) |
| `/api/positions` | GET | `?status=open\|closed\|all&wallet=...` |
| `/api/traders` | GET | `?sort=score\|roi\|pnl\|trades` |
| `/api/decisions` | GET | `?since_offset=N&type=copied\|rejected\|exit\|...` |
| `/api/replay` | POST | Summarise a decisions log file |
| `/api/halt` | POST/DELETE | Set/clear `kv_state['global_halt_reason']` |
| `/api/cutoff` | POST | `{wallet, reason}` — block new signals from a wallet |
| `/api/cutoff/{wallet}` | DELETE | Lift cutoff |
| `/api/execution_mode` | GET/POST/DELETE | paper/live runtime override |
| `/api/copy_mode` | GET/POST/DELETE | smart/blind runtime override |
| `/api/config` | GET | Read-only YAML config |

### Admin CLI

```bash
python -m bot.cli --config bot/config.yaml status
python -m bot.cli --config bot/config.yaml halt --reason "ops maintenance"
python -m bot.cli --config bot/config.yaml resume
python -m bot.cli --config bot/config.yaml cutoff --wallet 0xabc --reason "5 consec losses"
python -m bot.cli --config bot/config.yaml uncutoff --wallet 0xabc
python -m bot.cli --config bot/config.yaml positions
python -m bot.cli --config bot/config.yaml traders
python -m bot.cli --config bot/config.yaml replay --file logs/decisions.jsonl
```

Every CLI command has a dashboard equivalent. They write to the same
SQLite tables; the bot picks both up on the maintenance tick.

### Metrics & health

`127.0.0.1:9090` exposes Prometheus text format on `/metrics`, plus
`/healthz` and `/readyz`. Notable metrics: `bot_signals_copied_total`,
`bot_signals_rejected_total{reason}`, `bot_slippage_bps`,
`bot_execution_latency_seconds`, `bot_adverse_drift_bps`,
`bot_equity_usdc`, `bot_open_exposure_usdc`, `bot_global_halted`.

A Grafana stack ships in `docker-compose.yml` (`grafana:3000`).

## Going live

The bot ships with a deterministic dry-run signer. Real CLOB signing
requires `py_clob_client` (or your own EIP-712 signer) and these env
vars:

```
POLYGON_PRIVATE_KEY=0x...      # wallet that will sign orders
CLOB_API_KEY=...               # derived via py_clob_client.create_api_key
CLOB_API_SECRET=...
CLOB_API_PASSPHRASE=...
```

Then in `bot/config.yaml`:

```yaml
execution:
  dry_run: false                # the YAML ceiling. UI cannot lift this.
```

On the next start, the bot prints a loud `LIVE TRADING` banner and waits
`safety.live_mode_confirm_delay_seconds` (default 5 s) before starting
the pipeline so you can `Ctrl+C` if it was a mistake.

You can flip back to paper at runtime from the dashboard's Controls
page; the YAML stays at `dry_run: false` but the runtime override
forces paper. Switching from live → paper is always allowed; the other
direction needs a YAML edit + restart.

## Project layout

```
bot/
  main.py                      Entry point; wires every layer together
  cli.py                       Admin CLI (status, halt, cutoff, replay, ...)
  core/
    config.py                  Typed dataclass config + YAML loader
    models.py                  TradeSignal, Position, Order, OrderBookSnapshot, ...
    wallet_tracker.py          Polling signal source (data-api)
    websocket_tracker.py       WebSocket signal source (sub-second latency)
    signal_filter.py           Liquidity / spread / score / age gates
    position_sizer.py          Fractional Kelly with caps + smart mode hooks
    trader_scorer.py           Composite + Bayesian + per-category shrinkage
    portfolio_manager.py       Open/close, equity, anchors, hydrate
    exit_manager.py            TP / SL / mirror / time exits
    orchestrator.py            entry_loop + exit_loop + maintenance_loop
    enhancements.py            SignalAggregator + AdverseSelectionObserver
    logging_setup.py           DecisionLogger (JSONL audit trail)
    http.py                    Tiny httpx wrapper
  execution/
    clob_client.py             ClobClient (paper signer + live signer)
    execution_engine.py        Limit-order placement with reposts
  risk/
    risk_manager.py            All gates + refresh_external_state
  data/
    datastore.py               SQLite persistence (positions, traders, kv, ...)
  observability/
    pipeline_metrics.py        Prometheus metric definitions
    server.py                  Stdlib HTTP server (loopback by default)
  backtest/
    backtester.py              Offline replay through filter/sizer/risk

dashboard/
  app/
    main.py                    FastAPI factory + lifespan + middleware
    config.py                  pydantic-settings (DASHBOARD_* env)
    deps.py                    require_api_key (with rate-limit lockout)
    db.py                      SQLite open + write_tx + audit
    schemas.py                 Pydantic request/response models
    scoring.py                 Read-only thin wrapper over bot.core.trader_scorer
    routers/                   One FastAPI router per concern
  web/                         React + Vite + TS SPA, built into dist/
  tests/                       Pytest suite for the dashboard

tests/                         Pytest suite for the bot (240+ tests)
```

## Backtesting

`bot.backtest.Backtester` replays historical trader trades through the
full filter/sizer/risk pipeline. You supply:

- an iterable of `HistoricalTrade` (a `TradeSignal` plus optional
  resolution timestamp and outcome), and
- a `book_at(token_id, ts)` callable that returns an `OrderBookSnapshot`
  for that instant.

See `bot/backtest/backtester.py` for the interface. The default fill
model assumes an instant fill at the limit price; replace it with a
book-walk simulator for more realism.

## Observability

- **Decision journal**: `logs/decisions.jsonl`. Every `copied`,
  `rejected`, `exit`, `signal_cluster`, `adverse_selection_check`, and
  `stuck_signal_recovered` event with full structured fields. Audit
  trail for everything the bot decides.
- **Prometheus**: `127.0.0.1:9090/metrics`.
- **Dashboard audit log**: `state/dashboard_audit.sqlite`. Every admin
  action (halt, cutoff, mode change) with timestamp + actor + payload.
- **Replay tool**: `python -m bot.tools.replay --file logs/decisions.jsonl
  --config bot/config.yaml` re-evaluates each decision against the
  current code; non-zero exit if any verdict changed.

## Testing

`pytest` runs the full suite (325+ tests across `tests/` and
`dashboard/tests/`). Marked tests:

- `pytest -m throughput` — performance / latency floors.
- `pytest -m e2e` — end-to-end paths.

CI (`.github/workflows/test.yml`) installs both `requirements.txt` and
`dashboard/requirements.txt`, runs the unified suite, and lints with
`ruff` over `bot/`, `tests/`, and `dashboard/`.

## Extending

- **Different scorer:** subclass `TraderScorer` and override `score()`.
- **Different tracker:** implement an async `stream() -> AsyncIterator[TradeSignal]`
  and a `stop()`. See `WebsocketSignalSource` for a complete example.
- **Different signer:** any `Callable[[dict], Awaitable[dict]]` will do.
  See `_dry_run_signer` for the contract.
- **Different exit logic:** subclass `ExitManager`.

Each layer is wired in `bot/main.py:_amain` so a fork can replace any
single piece without touching the rest.

## License

See `LICENSE`. The example demo wallets and markets are illustrative
only and trade no real capital.
