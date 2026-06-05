# PolyEdge Strategy and Math

This document is the implementation spec for the first version of the bot.
The code must follow these rules before any live trading is enabled.

## Objective

Build a configurable bot for Polymarket's rolling crypto Up/Down markets.
The current default target is BTC 15-minute. The bot estimates the fair
probability that the configured asset finishes Up at
expiry, compares that probability to the executable CLOB bid/ask, and trades
only when the edge survives fees, spread, slippage, latency, model error, and
risk limits.

This is not an LLM prediction strategy. It is a short-horizon probability,
execution, and risk-management system.

## Market Definition

The target market is:

```text
Asset: BTC
Horizon: 15 minutes
Outcomes: Up, Down
Resolution concept: Chainlink BTC/USD end price compared with start price
```

The bot must discover each rolling market from official Polymarket metadata
and must never trade a market until these fields are known:

```text
condition_id
up_token_id
down_token_id
start_ts
end_ts
start_price
tick_size
minimum_order_size
neg_risk
resolution_source
```

If any critical field is missing, the market is `OBSERVE_ONLY`.

## Price Feeds

The primary v1 resolution-aligned input is Polymarket RTDS Chainlink BTC/USD.
Polymarket documents this no-auth WebSocket stream for crypto prices:

```text
wss://ws-live-data.polymarket.com
topic: crypto_prices_chainlink
symbol: btc/usd
```

The bot treats this as the closest free public approximation to the Chainlink
Data Streams source used by BTC 15-minute market rules. Direct authenticated
Chainlink Data Streams remains the later pro path.

The minimum feed set is:

```text
Polymarket RTDS Chainlink btc/usd
Polymarket RTDS Binance btcusdt
Binance BTCUSDT book ticker
Coinbase BTC-USD ticker
Optional direct authenticated Chainlink Data Streams latest report
```

The reference price object carries its own staleness flag. A stale or divergent
price cannot produce a live order.

`S_now` for fair value is:

```text
Polymarket RTDS Chainlink btc/usd
```

Binance and Coinbase are cross-checks. If the Chainlink RTDS price diverges
from fresh Binance/Coinbase proxy prices by more than the configured threshold,
the bot pauses trading.

Default divergence threshold:

```text
0.0015 = 0.15%
```

## Probability Model

The first model is intentionally simple and auditable. It models BTC log
returns over the remaining market time:

```text
d log(S_t) = mu dt + sigma dW_t
```

Where:

```text
S_now      = current BTC reference price
S_start    = market start/reference price
tau        = seconds remaining / seconds per year
mu         = annualized short-horizon log-drift estimate
sigma      = annualized realized volatility estimate
Phi        = standard normal CDF
```

For a market that resolves Up when the end price is greater than or equal to
the start price:

```text
q_up = P(S_T >= S_start)
```

Under the log-return model:

```text
log(S_T / S_now) ~ Normal(mu * tau, sigma^2 * tau)
```

So:

```text
q_up = Phi((log(S_now / S_start) + mu * tau) / (sigma * sqrt(tau)))
```

For this bot:

```text
q_down = 1 - q_up
```

The model clamps probabilities to a conservative range:

```text
0.001 <= q <= 0.999
```

This prevents numerical extremes from creating oversized positions near
expiry.

## Volatility Estimate

The v1 volatility estimator uses exponentially weighted log returns from fresh
Polymarket RTDS Chainlink BTC/USD ticks only. Binance/Coinbase proxy updates
are used for cross-checks, but they must not update sigma. Replaying the same
Chainlink tick through repeated composite reference updates would add false
zero-return samples and push volatility toward the floor.

For consecutive prices:

```text
r_i = log(S_i / S_{i-1})
dt_i = source_ts_i - source_ts_{i-1}
```

EWMA variance update:

```text
var_ewma = lambda * var_ewma + (1 - lambda) * (r_i^2 / dt_i)
```

Annualized volatility:

```text
sigma = sqrt(var_ewma * seconds_per_year)
```

Defaults:

```text
lambda = 0.94
sigma_floor = 0.20 annualized
sigma_cap = 3.00 annualized
```

The floor prevents the model from becoming overconfident during quiet periods.
The cap prevents one bad tick from exploding the estimate.

The deduplication key is:

```text
(source, source_ts, price)
```

where `source` defaults to `polymarket_rtds_chainlink_btc_usd` for the BTC
15-minute configuration.

## Drift Estimate

The v1 live model defaults to:

```text
mu = 0
```

Directional drift and order-flow signals can be added later only after replay
tests show they improve post-cost PnL.

Candidate future drift inputs:

```text
CEX trade imbalance
CEX order-book imbalance
Perp basis and funding
Chainlink versus CEX lag
Short-term trend over 30-120 seconds
```

Any drift module must output a bounded annualized `mu` and must be recorded in
the replay log.

## Polymarket Fee Math

Polymarket crypto markets charge taker fees and no maker fees under the
documented crypto formula:

```text
taker_fee = shares * 0.07 * price * (1 - price)
maker_fee = 0
```

Per share:

```text
taker_fee_per_share = 0.07 * price * (1 - price)
```

Examples:

```text
buy at 0.50: fee = 0.0175, break-even q = 0.5175 before spread/slippage
buy at 0.52: fee = 0.017472, break-even q = 0.537472 before spread/slippage
buy at 0.60: fee = 0.0168, break-even q = 0.6168 before spread/slippage
```

The bot must calculate fees at the actual intended execution price.

## Expected Value

For a taker buy of Up:

```text
EV_up_taker = q_up - ask_up - fee(ask_up) - slippage - model_error
```

For a taker buy of Down:

```text
EV_down_taker = q_down - ask_down - fee(ask_down) - slippage - model_error
```

For a maker bid on Up:

```text
EV_up_maker = q_up - bid_up - adverse_selection_buffer - model_error
```

For a maker bid on Down:

```text
EV_down_maker = q_down - bid_down - adverse_selection_buffer - model_error
```

For a sell of a held Up position:

```text
EV_sell_up = sell_price - q_up - adverse_selection_buffer
```

The first version only opens long outcome positions. It does not short by
selling shares it does not own.

## Model Error Buffer

The bot subtracts a configurable model error buffer from every apparent edge.

Default:

```text
model_error_buffer = 0.01 probability points
```

Increase the buffer when:

```text
Chainlink source is unavailable and CEX proxy is used
feed latency is elevated
time to expiry is low
volatility is high
Polymarket book is thin
```

## Maker-First Strategy

The default strategy is to quote as a maker with post-only limit orders.
Implementation uses post-only GTC plus local TTL-managed cancel/replace. It
does not use GTD unless the execution adapter supplies a valid exchange
expiration timestamp.

For Up:

```text
fair_up = q_up
quote_bid_up = floor_to_tick(fair_up - maker_margin)
```

For Down:

```text
fair_down = q_down
quote_bid_down = floor_to_tick(fair_down - maker_margin)
```

The order is valid only if:

```text
quote_price > best_bid
quote_price < best_ask
quote_price >= min_price
quote_price <= max_price
expected_edge >= maker_min_edge
```

Default maker parameters:

```text
maker_margin = 0.015
maker_min_edge = 0.01
order_ttl_seconds = 10
```

The order manager tracks desired maker quotes by:

```text
market_id
token_id
side
```

Rules:

```text
same desired quote already resting -> hold
price/size/order kind changed -> cancel all market quotes, then place desired quotes
local TTL expired -> cancel all market quotes, then place desired quotes
no maker edge while quote is resting -> cancel all market quotes
stale feed, kill switch, close window, or risk breach -> cancel all market quotes
```

In paper mode, `cancel_all` cancels the bot's tracked resting paper orders for
that market. In live mode, cancel/replace is scoped:

```text
tracked live order ids -> cancelOrders([...])
no tracked ids but condition_id available -> cancelMarketOrders({ market: condition_id })
no scope available -> reject cancellation unless ALLOW_EMERGENCY_ACCOUNT_CANCEL=true
```

Account-wide live cancellation is reserved for an explicit emergency gate and
should only be used with a dedicated wallet.

## Taker Strategy

Taker orders are disabled by default and only allowed when explicitly enabled
for research or a later approved live phase.

For a taker buy of Up:

```text
net_edge = q_up - ask_up - taker_fee_per_share(ask_up) - slippage - model_error
```

For a taker buy of Down:

```text
net_edge = q_down - ask_down - taker_fee_per_share(ask_down) - slippage - model_error
```

Default taker threshold:

```text
enable_taker_orders = false
taker_min_edge = 0.03
```

Allowed order types:

```text
FAK for partial fill with remainder cancelled
FOK for all-or-none execution
```

The worst-price limit must be the observed ask plus a small slippage allowance
for buys. The bot must not submit a taker order from stale book data.

Decision `size` is always share quantity in paper and replay. For live CLOB
FAK/FOK market BUY orders, Polymarket expects `amount` as quote dollars, so the
execution adapter sends:

```text
amount = price * share_size
```

For live CLOB market SELL orders, `amount` remains share quantity. Taker
decisions record `quote_amount` so the replay log can audit this conversion.

## Position Sizing

The bot starts with fixed tiny size. Kelly sizing is documented but not enabled
for v1 live trading.

Binary outcome expected edge:

```text
edge = q - price - costs
```

For a binary contract paying 1 dollar if correct, bought at price `p`:

```text
profit_if_win = 1 - p - costs
loss_if_lose = p + costs
```

The Kelly fraction for decimal payoff is:

```text
b = profit_if_win / loss_if_lose
kelly_fraction = (b * q - (1 - q)) / b
```

The bot uses:

```text
size = min(configured_base_size, market_depth_limited_size, risk_limited_size)
```

Risk limits:

```text
max_position_per_market
max_total_position
max_order_size
max_daily_loss
max_open_orders
```

## Staleness and Latency Rules

No live orders are allowed if any of these are true:

```text
reference price stale
Polymarket RTDS Chainlink price diverges too far from cross-check feeds
Polymarket book stale
market metadata stale
current time >= end_ts - final_no_trade_seconds
WebSocket reconnecting
clock skew above threshold
unresolved kill switch
live trading not explicitly enabled
non-restricted jurisdiction not explicitly confirmed
```

Default:

```text
max_reference_age_ms = 1500
max_book_age_ms = 1500
final_no_trade_seconds = 30
reference_divergence_pause_threshold = 0.0015
```

## Start Price Capture

The market metadata confirms the Chainlink resolution source but may not embed
the starting BTC/USD price. The bot captures the start price from the first
fresh Polymarket RTDS Chainlink `btc/usd` tick at or immediately after
`market.start_ts`.

Default capture window:

```text
start_price_capture_grace_seconds = 5
```

If the bot starts late and misses this window, the market remains
`OBSERVE_ONLY`. This avoids fabricating a start price from Binance/Coinbase or
from a late Chainlink tick.

## Backtesting Rules

A valid backtest must replay event time, not bar closes.

It must simulate:

```text
Polymarket L2 book updates
CEX/Chainlink reference prices
queue position for maker orders
partial fills
cancellation latency
taker fees
spread and slippage
market close behavior
settlement outcome
```

Invalid backtests:

```text
Using midpoint fills
Ignoring taker fees
Assuming maker orders always fill
Using final result data in the signal
Ignoring stale feeds
Ignoring cancel latency
```

The current replay command is:

```bash
polyedge backtest --path data/events.jsonl
```

The first implementation is intentionally conservative:

```text
FAK/FOK decisions fill immediately at decision price and pay taker fees.
Post-only maker decisions fill only if a later book ask touches the bid price.
Settlement uses the first RTDS Chainlink btc/usd tick at or after market end,
or the closest tick inside the configured settlement window.
```

This is a useful research backtester, not yet a queue-accurate exchange
simulator. Queue position and trade-print-based maker fills should be added
after enough CLOB trade data is recorded.

## Live Readiness Checklist

Live execution is blocked until all are true:

```text
ALLOW_LIVE=true
CONFIRM_NON_RESTRICTED_LOCATION=true
PRIVATE_KEY configured
FUNDER configured if required
Polymarket API credentials derivable
wallet balances checked
allowances checked
kill switch endpoint tested
paper trading profitable after costs
replay log can reproduce decisions
max risk limits configured
exact resolution source available or explicit override accepted
```

## Initial Success Criteria

The first milestone is not profit. It is reliable observation and replay.

Milestone 1:

```text
Discover configured crypto Up/Down markets
Maintain Up and Down order books
Track reference price
Compute q_up/q_down
Emit paper trade decisions
Record all inputs and outputs
Expose health/status API
```

Milestone 2:

```text
Run paper trading for at least 1,000 markets
Backtest from raw recorded data
Compare paper fills against conservative replay fills
Estimate net EV after fees and slippage
```

Milestone 3:

```text
Enable tiny-size live maker-only trading
Keep taker orders disabled unless net edge exceeds threshold
Audit every order against replayable inputs
```
