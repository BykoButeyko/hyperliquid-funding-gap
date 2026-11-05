# Plan

## Question

Hyperliquid perps pay funding every hour. Binance and Bybit pay every 8 hours (4 hours on some symbols). If
the rates differ for long enough, you can short the perp where funding is high, long it where funding is
low, stay flat on price, and collect the difference. I want to know how big that difference is for BTC,
ETH, SOL and HYPE, how long it lasts, and how much is left after the costs of actually holding the trade.

## Data

- Funding history from all three venues, as far back as each API goes (see `DATA.md`).
- Hourly closes from all three venues for the last ~200 days, for the price gap between venues.
- Order book snapshots for slippage at $100k, $1M and $5M per leg.
- Binance mark price at each settlement, which gives a full-history volatility series.

`fetch.py` loads everything into `funding.db` and can be rerun to top it up.

## Method

1. Turn every settlement into a per-hour rate: a payment covering N hours is spread over those N hours,
   with N read from the time since the previous settlement on that venue. This handles HL's early 8h
   period, Binance's 4h symbols and interval changes without hard-coding any of them.
2. Gap = HL rate minus CEX rate, per hour, annualised. Positive means short HL, long the CEX.
3. Distribution: mean, median, tails, how often the gap is basically zero (both venues at the default
   0.01% per 8h).
4. Persistence: autocorrelation at 8h, 1 day and 7 days on 8h block averages, and a half-life from an
   AR(1) fit. Block averages because CEX rates are flat inside each 8h window, which would inflate
   short-lag autocorrelation on hourly data.
5. Cost-adjusted carry: a simple rule. Enter after a settled 8h block shows a gap above a threshold, hold
   while the gap stays in my favour, leave when it flips. Costs per round trip: both venues' fees in and
   out (taker or maker scenario), plus walk-the-book slippage from the latest snapshot in the taker case.
   The signal only uses settled data. Benchmark: hold short HL, long CEX for the whole span with one round
   trip, to see whether switching adds anything over a static position.
6. Regimes: mean |gap| by tercile of 7-day realised volatility.
7. Holding throughout (`static.py`): a moving-block bootstrap on daily means (30-day blocks) for intervals
   that allow for autocorrelation, overall and per year; the mean per quarter; and an hourly simulation of
   both legs' margin at 2x, 3x and 5x. When a leg's margin falls to half its starting level I move
   collateral to even the legs out and resize both back to target leverage, paying taker fees on the resize.
8. Basis: HL vs CEX price gap from hourly closes, and how much it moves over a week. This is the price
   risk of a trade that is flat on paper but carries two different contracts.

## Funding mechanics that bias a naive comparison

- All three use the same shape: premium plus a clamped interest term of 0.01% per 8h. So the ~11% a year
  "baseline" that HL longs pay is also there on Binance and Bybit. It cancels in the gap. Comparing HL
  funding against zero, instead of against the CEX, would overstate the edge by about 11% a year.
- HL samples the premium every few seconds and settles hourly. CEXs average over the window and settle once.
  An hourly snapshot of HL against the last CEX print mixes one hour against eight. I compare accrued
  rates over the same hours instead.
- CEXs pay only to positions open at the settlement time. Spreading the payment over the window assumes
  the position is held throughout, which is true for this trade but not for someone timing settlements.
- Caps differ: HL caps at 4% per hour; Binance at 0.3% per 8h for BTC and ETH, 0.375% for SOL and 2% for
  HYPE; Bybit HYPE at 0.63%. In stress the capped venue lags, so the gap in those hours is partly a cap
  artefact that you cannot always trade into.

## What would be interesting either way

- Gap persists for days and clears realistic taker costs: the venues are not well arbitraged, worth
  checking who is on the other side and what capacity looks like.
- Gap is mostly noise around zero and dies within hours: the market is efficiently arbitraged, and the
  cost table shows how tight that equilibrium is. That is a clean negative result.
- Gap only shows up in volatile weeks: the edge is really a payment for carrying basis and liquidation risk
  when it is highest, which changes how to read any positive net number.

## Next

- Open interest history and a series of book snapshots instead of one, for capacity.
- Predicted funding (`predictedFundings`) as the entry signal instead of the last settled block.
- Mark price candles with highs and lows for the margin simulation, instead of Binance closes.
- Compare 2026 return on capital with stablecoin lending rates, not only Treasury bills.
