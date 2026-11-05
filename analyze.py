"""First pass over funding.db: size, persistence and cost-adjusted value of the Hyperliquid vs CEX funding gap.

Everything is turned into a per-hour rate first. A settlement that covers N hours is spread evenly over
those N hours, with N taken from the gap to the previous settlement on that venue (HL was 8-hourly at
launch and is hourly now; Binance moved some symbols to 4h). Rates are shown annualised (x 8760, in %).
gap = HL - CEX. A positive gap means HL longs pay more, so the carry trade is short HL, long CEX."""
import sqlite3

import numpy as np
import pandas as pd

HOUR = 3_600_000
YEAR_H = 24 * 365
COINS = ["BTC", "ETH", "SOL", "HYPE"]
CEXS = {"bn": "Binance", "by": "Bybit"}
# Base tier fees in percent of notional, checked on each venue's fee page in Nov 2026.
TAKER = {"hl": 0.045, "bn": 0.05, "by": 0.055}
MAKER = {"hl": 0.015, "bn": 0.02, "by": 0.02}
SIZE = 100_000        # USD per leg; slippage comes from the order book snapshot at this size
ENTRY = [10, 20, 40]  # enter when the last 8h block's gap is above this, % a year
EXIT = 0              # leave when the gap in my favour drops to this or below

db = sqlite3.connect("funding.db")
pd.set_option("display.width", 200)


def hourly(venue, coin):
    """Per-hour funding rate indexed by hour number (unix hours)."""
    f = pd.read_sql("SELECT time, rate FROM funding WHERE venue=? AND coin=? ORDER BY time", db,
                    params=(venue, coin))
    if f.empty:
        return pd.Series(dtype=float)
    h = (f.time / HOUR).round().astype(int)
    n = h.diff().fillna(8 if venue != "hl" else 1).clip(1, 8).astype(int)   # gaps over 8h are missing data
    hours = np.concatenate([np.arange(e - k + 1, e + 1) for e, k in zip(h, n)])
    return pd.Series(np.repeat((f.rate / n).values, n.values), index=hours).groupby(level=0).last()


def marks(coin):
    f = pd.read_sql("SELECT time, mark FROM funding WHERE venue='bn' AND coin=? AND mark IS NOT NULL", db,
                    params=(coin,))
    m = pd.Series(f.mark.values, index=(f.time / HOUR).round().astype(int))
    return m[m.index % 8 == 0]          # keep the 00/08/16 UTC prints so 4h symbols use the same grid


def blocks(s):
    """8h block means ending at 00/08/16 UTC, only blocks with all 8 hours present. Hour index h is the
    hour ending at h, so block k holds hours 8k+1..8k+8 and lines up with one CEX settlement."""
    g = s.groupby((s.index - 1) // 8)
    return g.mean()[g.count() == 8]


def half_life(b):
    x = b.dropna()
    phi = np.corrcoef(x[:-1], x[1:])[0, 1]
    return 8 * np.log(0.5) / np.log(phi) if 0 < phi < 1 else np.nan


def depth_cost(venue, coin):
    """Bps paid vs mid for one $SIZE market order, median over the snapshots taken so far."""
    r = db.execute("SELECT slip_bps FROM depth WHERE venue=? AND coin=? AND notional=? AND slip_bps IS NOT NULL",
                   (venue, coin, SIZE)).fetchall()
    return np.median([x[0] for x in r]) if r else np.nan


def backtest(b, entry, cost_pct):
    """b: 8h block gap, per-hour rate. Signal uses the block that just settled, so no look-ahead.
    Returns trades, mean hold in days, gross and net carry in % of one leg's notional."""
    trades, side, held, gross, start = [], 0, 0, 0.0, None
    idx = b.index.values
    for i in range(len(b) - 1):
        if idx[i + 1] != idx[i] + 1:          # hole in the data: book the trade so far and reset
            if side:
                trades.append((held, gross))
            side = 0
            continue
        g = b.iloc[i] * YEAR_H * 100
        if side == 0 and abs(g) > entry:
            side, held, gross = np.sign(g), 0, 0.0
        elif side and side * g <= EXIT:
            trades.append((held, gross))
            side = 0
        if side:
            gross += side * b.iloc[i + 1] * 8 * 100
            held += 1
    if side:
        trades.append((held, gross))
    if not trades:
        return 0, np.nan, 0.0, 0.0
    t = np.array(trades)
    return len(t), t[:, 0].mean() / 3, t[:, 1].sum(), t[:, 1].sum() - cost_pct * len(t)


def show(title, rows, fmt="{:.2f}"):
    print(f"\n== {title}")
    print(pd.DataFrame(rows).to_string(index=False, float_format=fmt.format))


def main():
    dist, persist, bt, regime, basis, yearly = [], [], [], [], [], {}
    for coin in COINS:
        hl = hourly("hl", coin)
        m = marks(coin)
        vol = np.log(m).diff().rolling(21).std() * np.sqrt(3 * 365) * 100   # 7d realised vol, % a year
        for cex, name in CEXS.items():
            c = hourly(cex, coin)
            both = pd.concat([hl, c], axis=1, keys=["hl", "cex"]).dropna()
            if both.empty:
                continue
            gap = both.hl - both.cex
            a = gap * YEAR_H * 100
            years = len(both) / YEAR_H
            span = f"{pd.to_datetime(both.index[0] * HOUR, unit='ms'):%Y-%m-%d}..{pd.to_datetime(both.index[-1] * HOUR, unit='ms'):%Y-%m-%d}"
            dist.append(dict(coin=coin, cex=name, span=span, hl_apr=both.hl.mean() * YEAR_H * 100,
                             cex_apr=both.cex.mean() * YEAR_H * 100, gap_mean=a.mean(), gap_med=a.median(),
                             p05=a.quantile(.05), p95=a.quantile(.95), abs_mean=a.abs().mean(),
                             hl_pos=(a > 0).mean() * 100, near_zero=(a.abs() < 1).mean() * 100))

            yearly[f"{coin} {name}"] = a.groupby(pd.to_datetime(a.index * HOUR, unit="ms").year).mean()

            b = blocks(gap)
            ab = b * YEAR_H * 100
            persist.append(dict(coin=coin, cex=name, blocks=len(b), ac_8h=ab.autocorr(1), ac_1d=ab.autocorr(3),
                                ac_7d=ab.autocorr(21), half_life_h=half_life(ab),
                                sd_hourly=a.std(), sd_8h=ab.std(), sd_1d=ab.groupby(ab.index // 3).mean().std()))

            slip = depth_cost("hl", coin) + depth_cost(cex, coin)
            costs = {"taker": 2 * (TAKER["hl"] + TAKER[cex]) + 2 * slip / 100, "maker": 2 * (MAKER["hl"] + MAKER[cex])}
            for k, cost in costs.items():   # benchmark: short HL, long CEX the whole time, one round trip
                gross = gap.sum() * 100
                bt.append(dict(coin=coin, cex=name, entry="always", fills=k, rt_cost=cost, trades=1,
                               hold_d=len(gap) / 24, gross=gross, net=gross - cost, net_apr=(gross - cost) / years))
            for e in ENTRY:
                for k, cost in costs.items():
                    n, hold, gross, net = backtest(b, e, cost)
                    bt.append(dict(coin=coin, cex=name, entry=e, fills=k, rt_cost=cost, trades=n,
                                   hold_d=hold, gross=gross, net=net, net_apr=net / years))

            v = vol.reindex(b.index * 8 + 8).values
            r = pd.DataFrame({"abs_gap": ab.abs().values, "vol": v}).dropna()
            if len(r) > 100:
                r["tercile"] = pd.qcut(r.vol, 3, labels=["calm", "mid", "volatile"])
                row = r.groupby("tercile", observed=True).abs_gap.mean()
                regime.append(dict(coin=coin, cex=name, **row.to_dict(),
                                   vol_cut=f"{r.vol.quantile(1/3):.0f}/{r.vol.quantile(2/3):.0f}"))

            px = pd.read_sql("SELECT venue, time, close FROM candles WHERE coin=? AND venue IN ('hl',?)", db,
                             params=(coin, cex)).pivot(index="time", columns="venue", values="close").dropna()
            if len(px):
                bp = (px.hl / px[cex] - 1) * 1e4
                basis.append(dict(coin=coin, cex=name, hours=len(bp), mean_bps=bp.mean(), sd_bps=bp.std(),
                                  sd_7d_change_bps=bp.diff(168).std()))


    show("Funding gap HL - CEX, hourly, % a year (hl_pos and near_zero are % of hours)", dist, "{:.1f}")
    show("Persistence of the gap (8h blocks, % a year); half-life from AR(1) on 8h blocks", persist)
    show(f"Carry backtest per ${SIZE:,} leg: gross/net in % of one leg over the whole span, net_apr per year. "
         f"Capital = one leg's notional (both legs at 2x)", bt)
    print("\n== Mean gap by calendar year, % a year")
    print(pd.DataFrame(yearly).T.to_string(float_format="{:.1f}".format))
    show("Mean |gap| (% a year) by 7d realised volatility tercile of the Binance mark price", regime, "{:.1f}")
    show("Price gap HL vs CEX from hourly closes (bps); sd_7d_change is the basis risk over a week's hold", basis, "{:.1f}")
    print("\n== Order book snapshot: half spread + walk cost vs mid for one market order (bps)")
    print(pd.read_sql("SELECT venue, coin, notional, round(spread_bps,2) spread, round(slip_bps,2) slip FROM depth "
                      "WHERE time IN (SELECT max(time) FROM depth GROUP BY venue, coin) ORDER BY coin, notional, venue",
                      db).to_string(index=False))


if __name__ == "__main__":
    main()
