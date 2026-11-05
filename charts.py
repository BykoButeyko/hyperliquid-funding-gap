"""charts/gap_30d.png: 30-day mean of the HL - Binance funding gap per coin, for the README."""
import os

import matplotlib.pyplot as plt
import pandas as pd

from analyze import COINS, HOUR, YEAR_H, hourly

INK, MUTED, GRID, LINE = "#0b0b0b", "#52514e", "#e4e3df", "#2a78d6"


def main():
    os.makedirs("charts", exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(10, 6), sharex=True, sharey=True, facecolor="#fcfcfb")
    for ax, coin in zip(axes.flat, COINS):
        gap = (hourly("hl", coin) - hourly("bn", coin)).dropna() * YEAR_H * 100
        gap.index = pd.to_datetime(gap.index * HOUR, unit="ms")
        ax.plot(gap.rolling("30D", min_periods=24 * 20).mean(), color=LINE, lw=1.6)
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.set_title(coin, loc="left", fontsize=11, color=INK)
        ax.set_facecolor("#fcfcfb")
        ax.grid(axis="y", color=GRID, lw=0.6)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(colors=MUTED, labelsize=9, length=0)
    for ax in axes[:, 0]:
        ax.set_ylabel("% a year", color=MUTED, fontsize=9)
    fig.suptitle("Funding gap, Hyperliquid minus Binance, 30-day mean", x=0.06, ha="left", fontsize=13, color=INK)
    fig.text(0.06, 0.005, "Above zero: HL longs pay more than Binance longs. Bybit tracks Binance closely.",
             fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    fig.savefig("charts/gap_30d.png", dpi=150)


if __name__ == "__main__":
    main()
