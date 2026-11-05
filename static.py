"""Checks on the static trade: short HL, long the CEX, held the whole time.

1. Is the mean gap distinguishable from zero once autocorrelation is allowed for? Moving-block bootstrap
   on daily means, 30-day blocks, overall and per year.
2. When did it pay? Mean gap per quarter.
3. Can the hedge actually be held? The two legs sit on different venues, so a rally drains the short leg's
   margin while the long leg's grows. I simulate both legs hour by hour on Binance closes at 2x, 3x and 5x
   per leg. When a leg's margin falls to half its starting level, I move collateral across venues to even
   them out and resize both legs back to target leverage, paying taker fees on the resize."""
import numpy as np
import pandas as pd

from analyze import CEXS, COINS, HOUR, TAKER, YEAR_H, db, hourly, show

LEVERAGE = [2, 3, 5]
MAINT = 0.02          # maintenance margin, share of notional. Both venues ask 0.4-1.5% at this size; I round up
BOOT, BLOCK = 2000, 30
rng = np.random.default_rng(0)


def ci(daily):
    """2.5% and 97.5% bounds of the mean of a daily series, moving-block bootstrap."""
    x = daily.values
    n = len(x)
    if n < 2 * BLOCK:
        return np.nan, np.nan
    starts = rng.integers(0, n - BLOCK + 1, size=(BOOT, -(-n // BLOCK)))
    idx = (starts[:, :, None] + np.arange(BLOCK)).reshape(BOOT, -1)[:, :n]
    return tuple(np.percentile(x[idx].mean(1), [2.5, 97.5]))


def closes(coin):
    """Binance hourly closes indexed by the hour they end at, same convention as hourly()."""
    c = pd.read_sql("SELECT time, close FROM candles WHERE venue='bn' AND coin=?", db, params=(coin,))
    return pd.Series(c.close.values, index=(c.time / HOUR).round().astype(int) + 1)


def hold(df, lev, fee):
    """df: hourly px, hl and cex rates. Returns final equity / starting capital, rebalances, worst leg margin."""
    px, rh, rc = df.px.values, df.hl.values, df.cex.values
    q = 1 / px[0]                                  # $1 notional per leg
    e_s = e_l = 1 / lev                            # margin on the short HL leg and the long CEX leg
    cap = e_s + e_l
    e_s -= fee / 2
    e_l -= fee / 2
    rebal, worst = 0, 1 / lev
    for i in range(1, len(px)):
        d = q * (px[i] - px[i - 1])
        n = q * px[i]
        e_s += -d + n * rh[i]                      # short receives HL funding when the rate is positive
        e_l += d - n * rc[i]                       # long pays CEX funding when the rate is positive
        m = min(e_s, e_l) / n
        worst = min(worst, m)
        if m <= MAINT:
            return np.nan, rebal, m                # liquidated before I could react
        if m < 0.5 / lev:
            tot = e_s + e_l
            q_new = tot / 2 * lev / px[i]
            tot -= abs(q_new - q) * px[i] * fee    # fee is per unit of notional on both legs together
            q, e_s, e_l = q_new, tot / 2, tot / 2
            rebal += 1
    return (e_s + e_l - q * px[-1] * fee) / cap, rebal, worst


def main():
    cis, quarterly, sims = [], {}, []
    for coin in COINS:
        hl, px = hourly("hl", coin), closes(coin)
        for cex, name in CEXS.items():
            df = pd.concat([hl, hourly(cex, coin), px], axis=1, keys=["hl", "cex", "px"]).dropna()
            gap = (df.hl - df.cex) * YEAR_H * 100
            when = pd.to_datetime(gap.index * HOUR, unit="ms")
            day = gap.groupby(when.floor("D")).agg(["mean", "count"])
            daily = day["mean"][day["count"] == 24]
            lo, hi = ci(daily)
            cis.append(dict(coin=coin, cex=name, period="all", days=len(daily), mean=daily.mean(), lo=lo, hi=hi))
            for y, d in daily.groupby(daily.index.year):
                lo, hi = ci(d)
                cis.append(dict(coin=coin, cex=name, period=y, days=len(d), mean=d.mean(), lo=lo, hi=hi))
            quarterly[f"{coin} {name}"] = gap.groupby(when.to_period("Q")).mean()

            years = len(df) / YEAR_H
            fee = (TAKER["hl"] + TAKER[cex]) / 100
            for lev in LEVERAGE:
                mult, rebal, worst = hold(df, lev, fee)
                sims.append(dict(coin=coin, cex=name, lev=lev, years=years, naive_apr=gap.mean() * lev / 2,
                                 sim_apr=(mult ** (1 / years) - 1) * 100, rebalances=rebal,
                                 per_year=rebal / years, worst_margin=worst * 100))

    show("Mean gap, % a year, with 95% block-bootstrap interval (30-day blocks)", cis, "{:.1f}")
    print("\n== Mean gap by quarter, % a year")
    print(pd.DataFrame(quarterly).to_string(float_format="{:.1f}".format))
    show("Static hedge held throughout, return on capital (both legs' margin), % a year. naive = mean gap x lev/2; "
         "sim = hourly margin simulation with rebalancing and taker fees; worst_margin = lowest leg margin, % of "
         "notional; NaN sim = liquidated", sims)


if __name__ == "__main__":
    main()
