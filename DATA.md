# Data sources

All public, no keys. Checked by hand against live responses on 2026-11-02 before writing `fetch.py`.
Times are unix milliseconds, UTC. Rates are fractions per settlement (0.0001 = 0.01%), not percent.

## Hyperliquid

`POST https://api.hyperliquid.xyz/info`

| body | returns | notes |
|---|---|---|
| `{"type":"fundingHistory","coin":"BTC","startTime":t}` | `[{coin, fundingRate, premium, time}]` | Oldest first from `startTime`, at most 500 rows per call, so I page by last `time` + 1. History starts 2023-05-12 for BTC. |
| `{"type":"predictedFundings"}` | `[[coin, [[venue, {fundingRate, nextFundingTime, fundingIntervalHours}]]]]` | Next rate for HL (`HlPerp`), Binance (`BinPerp`) and Bybit (`BybitPerp`). Not stored yet. |
| `{"type":"candleSnapshot","req":{coin, interval:"1h", startTime, endTime}}` | `[{t, c, ...}]` | Trade candles, only the latest 5000 per request. |
| `{"type":"l2Book","coin":"BTC"}` | `{levels:[bids, asks]}` | 20 levels per side, so a large order can run off the visible book. With `"nSigFigs":4` the levels are price-rounded and reach deeper; I use that only when the fine book runs out. |
| `{"type":"metaAndAssetCtxs"}` | universe + per coin `funding`, `openInterest`, `premium`, `markPx` | Used for checks only. |

- **Interval changed.** BTC settled every 8 hours until 2023-06-08 00:00 UTC and every hour after that.
  The 8h rows carry the 8h rate (0.0001 at the default) and the hourly rows the hourly payment
  (0.0000125 at the default, one eighth of 0.01%). I take the interval from the time since the previous row,
  so both periods convert correctly.
- `fundingRate` is what a long pays per unit of notional at that settlement. `premium` stays on the 8h
  scale in both periods; I do not use it.
- Three hourly settlements are missing for every coin (2023-07-02 20:00, 2023-08-23 20:00, 2024-08-15
  13:00 UTC). The next row then gets spread over 2 hours, which halves that one hour's rate. Too small to matter.
- Timestamps land a few ms to a few hundred ms after the hour. I round to the nearest hour.
- Formula (docs): rate = premium + clamp(0.01% − premium, −0.05%, 0.05%) per 8h, paid hourly as 1/8;
  capped at 4% per hour.
- Rate limit: 1200 weight per minute per IP. `fundingHistory` costs 20 plus 1 per 20 rows returned
  (45 for a full page); `candleSnapshot` 20 plus 1 per 60 candles; `l2Book` 2. `fetch.py` paces itself to
  600 weight per minute.
- Fees, base tier: taker 0.045%, maker 0.015%.

## Binance USDⓈ-M

`GET https://fapi.binance.com/...`

| endpoint | returns | notes |
|---|---|---|
| `/fapi/v1/fundingRate?symbol=BTCUSDT&startTime=t&limit=1000` | `[{symbol, fundingTime, fundingRate, markPrice}]` | Oldest first from `startTime`. Without `startTime` it returns only recent rows, so I always pass it. `markPrice` is the mark at settlement (empty on some early rows). |
| `/fapi/v1/fundingInfo` | per symbol `fundingIntervalHours`, caps | Current settings only. On 2026-11-02: BTC, ETH 8h cap ±0.3%; SOL 8h ±0.375%; HYPE **4h** ±2%. |
| `/fapi/v1/premiumIndex` | current mark, index, `lastFundingRate`, `interestRate` (0.0001 per 8h), `nextFundingTime` | Checks only. |
| `/fapi/v1/klines?interval=1h` | `[openTime, o, h, l, c, ...]` | Up to 1500 per call. |
| `/fapi/v1/depth?limit=1000` | bids, asks | Snapshot. |

- Interval can change per symbol over time. SOL ran on 2h from 2022-11-10 to 2022-11-18 (FTX week), before
  the HL overlap starts. I take it
  from the timestamps, not from `fundingInfo`, which only shows the current setting.
- The payment goes to whoever holds the position at `fundingTime`.
- Fees, VIP 0: taker 0.05%, maker 0.02%.

## Bybit linear (USDT perps)

`GET https://api.bybit.com/v5/market/...`

| endpoint | returns | notes |
|---|---|---|
| `funding/history?category=linear&symbol=BTCUSDT&endTime=t&limit=200` | `{result:{list:[{symbol, fundingRate, fundingRateTimestamp}]}}` | **Newest first**, 200 rows max. Passing only `endTime` works, so I walk backwards. Timestamp is a string. No price. |
| `instruments-info` | `fundingInterval` (minutes), `upperFundingRate` / `lowerFundingRate` | HYPE: 480 min, cap ±0.63% on 2026-11-02. |
| `tickers` | current `fundingRate`, `fundingIntervalHour`, `fundingCap`, OI | BTC cap 0.333%. Checks only. |
| `kline?interval=60` | `[[start, o, h, l, c, ...]]`, newest first | 1000 per call. |
| `orderbook?limit=500` | `{b, a}` | Snapshot. |

- Same payment-at-settlement rule as Binance. SOL ran on 2h from 2022-11-10 to 2022-12-20.
- Fees, non-VIP: taker 0.055%, maker 0.02% (help centre fee table, updated 2026-09).

## Normalisation

Every settlement becomes a per-hour rate: `rate / N`, spread over the N hours before it, where N is the
hours since that venue's previous settlement for the coin, capped at 8. A gap longer than 8 hours is
treated as missing data and only its last 8 hours get a value. Annualised = per-hour rate × 8760.

## Tables in funding.db

- `funding(venue, coin, time, rate, premium, mark)`: venue is `hl`, `bn` or `by`.
- `candles(venue, coin, time, close)`: hourly, `time` is the candle open. Last ~200 days for HL and Bybit;
  Binance from 2023-05-01 for the margin simulation.
- `depth(venue, coin, time, notional, spread_bps, slip_bps)`: spread and the worse side's average fill vs mid for
  a market order of that size. Null when the visible book is too thin.
