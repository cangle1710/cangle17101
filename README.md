# Polymarket Smart Copy Trading Bot

A production-quality copy-trading system for [Polymarket](https://polymarket.com)
binary prediction markets. Tracks a configurable set of on-chain wallets,
evaluates every trade against liquidity/score/latency gates, sizes with
fractional Kelly, executes adaptively on the CLOB, and manages exits
independently of the source trader.

> **Status:** dry-run by default. You can run the full pipeline end-to-end
> with simulated fills. Going live requires providing a CLOB order signer
> (see [Going live](#going-live)).

## Design principles

- **Do not blindly copy.** Every signal goes through a filter that rejects
  it if the market has already moved, liquidity is thin, the spread is
  wide, or the source trader's composite score is too low.
- **Prioritize expected value, not win rate.** Sizing is driven by a
  fractional-Kelly formula that estimates edge from the trader's historical
  ROI and composite score, capped to avoid over-confidence.
- **Latency-aware.** The tracker polls every ~2s; execution aborts if the
  market has drifted past the configured slippage tolerance while we were
  placing the order.
- **Risk first.** Daily soft-stop, weekly hard-stop, per-trader drawdown
  and loss-streak cutoffs, global exposure caps, and a capital reserve.
- **Modular.** Every stage is a separate class with a narrow interface, so
  you can swap in a different scorer, a different signer, or a live
  backtesting harness without touching the rest.

## Project structure

```
bot/
├── core/
│   ├── models.py            # TradeSignal, Position, Order, TraderStats, ...
│   ├── config.py            # Typed YAML loader
│   ├── logging_setup.py     # Standard + JSONL decision log
│   ├── http.py              # Async httpx wrapper w/ retries
│   ├── trade_parser.py      # Raw API payload -> TradeSignal
│   ├── wallet_tracker.py    # Polls data-api for each watched wallet
│   ├── trader_scorer.py     # Per-trader stats + composite score
│   ├── signal_filter.py     # Rejects bad/stale signals
│   ├── position_sizer.py    # Fractional Kelly w/ hard caps
│   ├── portfolio_manager.py # Open positions, bankroll, anchors
│   ├── exit_manager.py      # TP/SL/mirror/time exits
│   └── orchestrator.py      # Entry/exit/maintenance async loops
├── execution/
│   ├── clob_client.py       # Polymarket CLOB HTTP wrapper + signer hook
│   └── execution_engine.py  # Adaptive limit-order placement
├── risk/
│   └── risk_manager.py      # Global + per-trader kill-switches
├── data/
│   └── datastore.py         # SQLite persistence
├── backtest/
│   └── backtester.py        # Historical replay scaffold
├── main.py                  # Entry point
└── config.yaml              # Example config
```

## Quick start

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Edit the config

Open `bot/config.yaml` and replace `tracker.wallets` with the list of
addresses you actually want to follow. Everything else has sane defaults;
tune as you go.

### 3. Run (dry-run)

```bash
python -m bot.main --config bot/config.yaml
```

In dry-run mode the `ClobClient` uses a built-in signer that simulates
instant full fills at the requested limit price. The bot still performs
real reads (order book, wallet trades) against the public Polymarket
endpoints, so you get a realistic end-to-end view of what would have been
copied, sized, and filled.

Live output goes to stdout **and** `logs/bot.log`. A machine-readable
decision record goes to `logs/decisions.jsonl` — one JSON object per
copy/reject/exit event.

### 4. Inspect the decision log

```bash
tail -f logs/decisions.jsonl | jq .
```

Every rejection carries a reason code (`thin_liquidity`, `price_moved`,
`low_trader_score`, `daily_soft_stop`, ...). Use this to tune thresholds.

## Going live

Dry-run skips the actual CLOB signing; to submit real orders you need to
swap in a signer. The fastest path is the official
[`py-clob-client`](https://github.com/Polymarket/py-clob-client):

```python
# production_signer.py
from py_clob_client.client import ClobClient as PyClob
from py_clob_client.clob_types import OrderArgs
from py_clob_client.constants import POLYGON

py_clob = PyClob(
    host="https://clob.polymarket.com",
    key=os.environ["POLYGON_PRIVATE_KEY"],
    chain_id=POLYGON,
    funder=os.environ["POLYGON_FUNDER"],
    signature_type=2,
)
py_clob.set_api_creds(py_clob.create_or_derive_api_creds())

async def sign_and_post(order: dict):
    if "cancel_order_id" in order:
        return py_clob.cancel(order["cancel_order_id"])
    resp = py_clob.create_and_post_order(OrderArgs(
        token_id=order["token_id"],
        price=order["price"],
        size=order["size"],
        side=order["side"],
    ))
    return resp
```

Then wire it in:

```python
# bot/main.py (replace the ClobClient construction)
from production_signer import sign_and_post
clob = ClobClient(cfg.execution, http, signer=sign_and_post)
```

and set `execution.dry_run: false` in `config.yaml`.

## Risk controls summary

| Control | Default | Where |
|---|---|---|
| Weekly drawdown hard halt | 30% | `risk.weekly_drawdown_stop_pct` |
| Daily new-entry soft stop | 10% | `risk.daily_soft_stop_pct` |
| Per-trader cutoff | 20% DD or 5 consec. losses | `risk.trader_*` |
| Max per single copy | 3% of bankroll | `sizing.max_pct_per_trade` |
| Max per market | 8% of bankroll | `sizing.max_pct_per_market` |
| Max global exposure | 60% of bankroll | `risk.max_global_exposure_pct` |
| Capital reserve (never deployed) | 10% of starting bankroll | `bankroll.reserve_pct` |
| Kelly fraction | 0.25 | `sizing.kelly_fraction` |
| Max slippage (abort) | 1.5% | `execution.max_slippage_pct` |

## Backtesting

`bot.backtest.Backtester` replays historical trader trades through the
full filter/sizer/risk pipeline. You supply:

- an iterable of `HistoricalTrade` (a `TradeSignal` plus optional
  resolution timestamp and outcome),
- a `book_at(token_id, ts)` function that returns an `OrderBookSnapshot`
  for that instant.

See `bot/backtest/backtester.py` for the interface. The default fill model
assumes an instant fill at the limit price; replace it with a book-walk
simulator for more realism.

## Operations

### Metrics & health

When `observability.enabled` is true (default), the bot exposes a tiny
HTTP server on `127.0.0.1:9090`:

```
GET /metrics   # Prometheus text format: signal counts, rejection reasons,
               # slippage bps histogram, execution latency, equity, open
               # exposure, open positions, halts, trader cutoffs, etc.
GET /healthz   # 200 "ok" if the process is alive
GET /readyz    # 200 if the orchestrator is running + DB is writable,
               # else 503 with the failure reason
```

Scrape with Prometheus and chart in Grafana. A one-shot local stack is
available via `docker-compose up`.

### Admin CLI

```
python -m bot.cli --config bot/config.yaml status
python -m bot.cli --config bot/config.yaml halt --reason "ops maintenance"
python -m bot.cli --config bot/config.yaml resume
python -m bot.cli --config bot/config.yaml cutoff --wallet 0xabc --reason "losing"
python -m bot.cli --config bot/config.yaml uncutoff --wallet 0xabc
python -m bot.cli --config bot/config.yaml positions
python -m bot.cli --config bot/config.yaml traders
python -m bot.cli --config bot/config.yaml replay --file logs/decisions.jsonl
```

All commands talk to the same SQLite state file the bot uses; writes are
picked up on the next maintenance tick (60s) or by checking the file on
every signal (kill-switch file and per-wallet cutoffs are read eagerly).

### Kill-switch file

`safety.kill_switch_file` (default empty / disabled) is a path that, if
it exists on disk, blocks all new entries immediately. The exit loop
keeps working. Use it for emergency pauses without attaching a debugger:

```
touch /var/run/bot.halt   # pause trading
rm /var/run/bot.halt      # resume
```

### Paper vs live banner

On startup the bot prints a loud banner indicating which mode it's in.
When `execution.dry_run: false`, it waits
`safety.live_mode_confirm_delay_seconds` (default 5s) before starting
the pipeline, giving you time to Ctrl+C if it was left off by accident.

### Regression replay

Compare today's code against yesterday's decisions:

```
python -m bot.tools.replay --file logs/decisions.jsonl --config bot/config.yaml
```

Emits agreement counts and per-reason diffs; exits non-zero if any
event's accept/reject outcome changed.

## Extending

- **Different scorer:** subclass `TraderScorer` and override `score()`.
- **Different tracker:** subclass `WalletTracker` and override
  `_poll_wallet()` — e.g., to consume a WebSocket feed or an RPC stream
  of `OrderFilled` events (`_fallback_from_chain` is a stub).
- **Different execution strategy:** subclass `ExecutionEngine.execute()`
  or replace `_compute_limit_price`.

## Data flow

```
WalletTracker ──> TradeParser ──> [dedupe in DataStore]
                                         │
                                         ▼
                                   SignalFilter  (reject on book/score)
                                         │
                                         ▼
                                  PositionSizer  (fractional Kelly)
                                         │
                                         ▼
                                   RiskManager   (global / daily / trader)
                                         │
                                         ▼
                                ExecutionEngine  (adaptive limit orders)
                                         │
                                         ▼
                              PortfolioManager   (opens + tracks)
                                         │
                                         ▼
                               ExitManager loop  (TP/SL/mirror/time)
                                         │
                                         ▼
                                 TraderScorer    (update stats, feed back)
```

## License

Same as the repo license.
