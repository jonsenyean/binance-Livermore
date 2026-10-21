# Binance Perpetual Futures · Paper Trading Tool

**Live Market Data + Virtual Funds**: Market data is sourced 100% from real-time Binance feeds, while all trades and capital are simulated locally—no real orders are placed, and no actual funds are involved.
**P&L Calculation Based on Official Binance Rules**: Adheres to official precision standards, tiered maintenance margin requirements, and standard formulas for trading fees and funding rates.

> **Note on Public Release**: This project is intended for the public release of source code, configuration templates, and test code. Before publishing to GitHub, please remove local runtime files such as `baozhengjin.ini`, `trade_log.csv`, `test_out.txt`, `huihua.md`, and any backup files (e.g., `*.bak*`). While the API Key and Secret fields in the current configuration are empty, please ensure that no actual API credentials are ever committed to GitHub. This project is strictly for paper trading purposes and does not constitute investment advice.

## Project Overview

This is a paper trading tool for Binance USDT-margined perpetual futures, built using Python and `asyncio`. It retrieves live public market data from Binance and simulates accounts, order matching, trading fees, funding rates, margin requirements, liquidation prices, and strategy-based P&L locally. It comes with a default "Livermore Key Point" rolling position strategy; no real orders are ever submitted to Binance. Any provided API keys are used solely for optional, read-only data display. ## Portfolio Strategy

| Item | Configuration |
|---|---|
| Total Capital | 1000 USDT (`TOTAL_BALANCE`) — **fixed** (uses the value recorded in `baozhengjin.ini` if available). |
| Trading Pairs | Number requested at startup (defaults to 10 if left blank; `NUM_PAIRS`). Selection criteria: Top 30 by trading volume (`VOLUME_POOL`), excluding assets listed for fewer than 28 days (`POOL_MIN_DAYS`). Ranked by 30-day average hourly volatility; the top N are selected. **ETH is mandatorily included** (`REQUIRE_SYMBOLS`). |
| Capital per Pair | Independent capital pool = Total Capital ÷ Number of Trading Pairs (`PER_PAIR_BALANCE` is calculated based on this). |
| Position Opening | Initial margin per position is fixed at 10 USDT (`TREND_POSITION_MARGIN_USD`). Notional value = Margin × Current Leverage (e.g., 10x leverage = 100 USDT notional). If this is below the exchange's minimum order size for a given pair, it is automatically adjusted to meet that minimum. |
| High-Volatility Window | Based on 30-day 1-hour candlestick data, the system identifies a 4-hour sliding window (`VOLATILITY_WINDOW_HOURS`) with the highest average volatility (in UTC). Automated trading is restricted to this specific time window (`TRADE_ONLY_IN_WINDOW`). |
| Portfolio Risk Control | Limits: ≤6 active pairs, ≤60% margin usage, ≤3000 USDT net directional exposure. A "circuit breaker" (halt) is triggered if the daily (UTC) equity drawdown reaches ≥5%. This applies only to automated strategies; manual commands remain unaffected. |
| Take-Profit / Stop-Loss Targets | Defined at startup (leave blank to disable). If the daily profit target or loss limit is reached, the entire portfolio stops opening new positions. No further entries are permitted until the system is restarted; a prominent, large-text banner notification is displayed. |
| Take-Profit / Stop-Loss | Calculated based on **position margin (principal)**. Take-profit at ≥500% and stop-loss at ≥50% (`TREND_TAKE_PROFIT` / `TREND_STOP_LOSS`), with additional trailing take-profit functionality.

## Features

- **Real-time Market Data**: Utilizes **two independent WebSocket connections**—one for `bookTicker` (Public category) and another for `markPrice@1s` (Market category)—routed through a local proxy with automatic reconnection upon disconnection. As of March 6, 2026, Binance has split the WebSocket endpoints for USDT-margined futures based on data category (the legacy endpoint `stream.binancefuture.com` has been deprecated, and `markPrice` is no longer routed through it). Each connection is monitored by a "market data watchdog": if no updates are received within `WS_STALE_SECONDS`, the connection is forcibly reset and re-established, ensuring automatic recovery from frozen market data. `markPrice` is aggregated into 1-minute candlestick data to facilitate trend analysis.
- **Automated Trading Pair Selection**: Upon startup, the system prompts for the desired number of trading pairs. It then fetches pairs from Binance based on high liquidity (top trading volume) and high volatility, automatically writing them to `jiaoyi.ini` (preserving existing comments) for use in trading. If the fetch fails, the system falls back to the existing list in `jiaoyi.ini` (`FALLBACK_SYMBOLS`).
- **Local Simulated Matching (Binance-compliant)**: Buy orders are matched against the best ask (sell-side), and sell orders against the best bid (buy-side). Precision is rounded according to official Binance specifications (retrieved via `exchangeInfo` at startup).
- **Trend-Following + Floating-Profit Pyramiding Strategy (per trading pair)**: Uses fast/slow 1-minute EMAs to identify trends. Positions are opened with a fixed margin amount (`TREND_POSITION_MARGIN_USD`). If a position is profitable and subsequently experiences a pullback, the floating profit is used to increase the position size. Exits are triggered by take-profit, stop-loss, trailing take-profit, or trend reversal.
- **Livermore Method (Default, `STRATEGY="livermore_roll"`):**
Entry upon breakout of a key pivot point; initial "test" position; pyramiding (adding to the position) as the price hits new highs while in profit; stop-loss at key pivot points; "letting profits run" (see the "Livermore Method" section below for details).
- **Portfolio Risk Governor**: Includes limits on the number of open positions (`RISK_MAX_POSITIONS`), total margin usage (`RISK_MAX_MARGIN_PCT`), and net directional exposure (`RISK_MAX_NET_NOTIONAL_USD` — calculated as long notional value minus short notional value). Also features a daily equity drawdown circuit breaker (`RISK_DAILY_MAX_LOSS_PCT`, which resets automatically at the start of each UTC day). Given the high correlation among cryptocurrencies, this prevents the entire portfolio from being wiped out by a single, unified directional bet. These limits apply only to new positions or additions initiated by the automated strategy; manual commands and the closing of existing positions remain unrestricted.
- **Take-Profit Targets / Stop-Loss/Limit Thresholds**: Upon startup, you will be prompted to enter specific monetary values ​​(press Enter to skip). If your daily profit reaches the target (indicated by a large "Target Achieved" banner) or your daily loss hits the stop-loss threshold (indicated by a large "Stop-Loss Triggered" banner), **all trading pairs will cease opening new positions or adding to existing ones** (this state is locked; no new entries will occur until the tool is restarted, though existing positions can still be closed as usual).
- **Performance Statistics & Trading Logs**: Aggregate statistics—including win rate, profit factor, average profit/loss, and expected value—are available via the `stats` command. Additionally, every closed trade is logged to `trade_log.csv` (encoded in UTF-8-SIG, allowing for direct opening in Excel).
- **Profit/Loss Breakdown**: Detailed P&L for each trading pair (unrealized, realized, and net P&L), along with a portfolio-wide summary.
- **Balance & Earnings Tracking (`baozhengjin.ini`)**: When the tool is closed, the current total margin is saved to `baozhengjin.ini`. Upon the next launch, this value is automatically loaded as the starting capital—ensuring that your balance and earnings persist across sessions rather than resetting upon restart.

## The Livermore Method (Default Strategy)

This strategy implements the core trading principles found in *Reminiscences of a Stock Operator* (notably, it **prohibits adding to a position during a pullback from an unrealized profit**):

| Livermore Principle | Implementation in This Tool |
|---|---|
| **Entry on Breakout of Key Points** | Key points are defined by the high and low prices over the most recent `LIVERMORE_PIVOT_PERIOD` (default: 45) 1-minute candlesticks. A long position is opened when the price breaks above the previous high (plus an adaptive buffer—see "Entry Quality"), and a short position is opened when it breaks below the previous low. The strategy does not attempt to predict; it only enters once a key level is breached. |
| **Moving Average Trend Filter** | `LIVERMORE_REQUIRE_TREND` (set to `True` by default): Requires the breakout to align with the direction of the 1-minute EMA. This filters out false breakouts that run counter to the prevailing trend (note: this may also exclude "sideways-to-breakout" patterns). |
| **Multi-Timeframe Filtering** | `LIVERMORE_USE_HIGHER_TF` (True): Entries are permitted only when the 15-minute (15m) trend aligns with the trade direction. If the 15m timeframe is "flat" (ranging), entries are blocked (controlled by `LIVERMORE_HTF_FLAT_ALLOW`) to reduce the risk of "whipsaws" (false breakouts in both directions) during sideways markets. |
| **Probing Positions** | The initial position is a "probe" sized at `LIVERMORE_PROBE_MARGIN_USD` (40 USD). The philosophy: "Test the waters first; add to the position only once the market confirms your thesis." |
| **Entry Quality** | Breakout buffer is set to `max(0.1%, 0.25 × 15m ATR / Price)` (controlled by `LIVERMORE_BREAKOUT_ATR_MULT`). For highly volatile assets, the buffer automatically widens to filter out false breakouts. A funding rate filter (`LIVERMORE_FUNDING_FILTER`) is also applied: do not go long if the funding rate exceeds +0.03% per 8 hours, and do not go short if it falls below -0.03% (avoiding "crowded" trades). |
| **Risk Budgeting** | Maximum loss per trade is capped at `LIVERMORE_RISK_MAX_LOSS_USD` (25 USD). The notional size of the probe position is calculated as `min(fixed notional, 25 USD / stop-loss distance %)`. A wider stop-loss results in a smaller position size, ensuring the potential loss remains constant regardless of the stop-loss distance—preventing the scenario where a wider stop-loss leads to a larger potential loss. |
| **Pyramiding (Adding to Winners – The Livermore Method)** | Add to positions incrementally in the direction of the trend, but only when the trade is profitable and reaching new highs. Specifically, add a new "tranche" when the price breaks out by an additional `LIVERMORE_ADD_BREAKOUT_PCT` (0.5%) above the previous entry/addition price. The strategy focuses on chasing strength rather than buying dips; it raises the average entry price over time and strictly forbids "averaging down" (adding to a losing position). The number of additions is capped by `TREND_MAX_ADDS`. |
| **Letting Profits Run** | A trailing take-profit mechanism is used, which triggers if profits retreat by 35% from their peak. Exit (`LIVERMORE_TRAIL_GIVEBACK`): Once floating profit reaches +1.5%, a "break-even" mechanism is activated (`LIVERMORE_BREAKEVEN_ACTIVATE_PCT`). The break-even price is set at the entry price ±0.12%—covering the 0.1% round-trip taker fee—ensuring that an exit at the break-even level does not result in a net loss due to trading fees. Exits are managed via trailing take-profit/break-even mechanisms rather than fixed take-profit levels. |
| **Reversal Exit (15m timeframe)** | An exit is triggered if the price falls below the low (for long positions) or rises above the high (for short positions) of the most recently closed 15-minute candle, or if the 15-minute EMA reverses direction (`LIVERMORE_HTF_EXIT_BREAK`). This serves as an earlier exit signal compared to using 1m moving averages. |
| **Stagnation Exit (Correcting a "False Breakout")** | If, 45 minutes after a breakout entry (`LIVERMORE_STAGNATION_MINUTES`), no position-adding (scaling in) has occurred and the peak floating profit has remained below 10% of the margin (`LIVERMORE_STAGNATION_MIN_PNL`), the breakout is deemed a failure. The position is closed proactively, rather than waiting for a stop-loss to be hit. |
| **Rapid Stop-Loss** | Employs a combination of margin-based stop-loss, an ATR-adaptive stop-loss (`LIVERMORE_STOP_ATR_MULT`), and a pivot-point-based stop-loss (`LIVERMORE_USE_PIVOT_STOP`). If the price falls below the designated pivot stop-loss level, the position is closed. The stop-loss level shifts upward alongside the pivot point, but never moves downward. |
| **Patient Waiting** | Entries occur only upon breakouts at key pivot points. After a position is closed, a new entry is permitted only if a **higher** key pivot point is broken (`last_break` guardrail). This is combined with a "cool-down" period to prevent over-trading. Following a loss, there is a 120-minute cool-down; this extends to 320 minutes after two consecutive losses, and caps at 12 hours for three or more consecutive losses—enforcing greater caution as losses accumulate. |

To switch back to the original trend strategy, set `STRATEGY = "trend_pyramid"` in `config.py`.

## Profit & Loss Calculation (Binance Official Standards)

| Item | Formula | Implemented |
|---|---|---|
| Unrealized P&L | Long: `(Mark Price - Entry Price) × Quantity`; Short: Inverse (using Mark Price) | ✅ |
| Realized P&L | `(Exit Price - Entry Price) × Quantity - Opening/Closing Fees` | ✅ |
| Trading Fees | `Notional Value × Fee Rate` (Taker: 0.05% / Maker: 0.02%) | ✅ |
| Funding Fees | `Mark Price × Quantity × Funding Rate` (Longs pay if the rate is positive) | ✅ |
| Initial Margin | `Notional Value / Leverage` (capped by the maximum leverage for the specific tier) | ✅ |
| Maintenance Margin | Determined by Notional Value based on the official tier table (0.4% / 0.5% / 1% / 2.5% / 5%) | ✅ |
| Liquidation Price | Long: `(Entry Notional - Locked Margin) / (Quantity × (1 - MMR))`; Short: Inverse (includes cumulative margin from additional positions; consistent with Binance) | ✅ |
| Order Precision | Quantity rounded down to `step_size`; Price rounded to `tick_size` (per official `exchangeInfo`) | ✅ |
| Minimum Order Size | Based on the official `min_notional` for each trading pair (e.g., BTC: 50, ETH: 20; others fetched dynamically) | ✅ |

## Quick Start

```bash
cd binance_paper_trading
pip install -r requirements.txt
python main.py
```

> No API keys required—this is a local simulation that only reads public market data and does not place real orders.
> If you are in mainland China, a proxy is required: set `WS_PROXY = http://127.0.0.1:7897` (adjust to your specific Clash port). If you are unable to connect or market data fails to update, first run `python check_proxy.py` to diagnose the issue: check whether the proxy is functioning correctly and whether data can be received for the new categories (Public/Market).
