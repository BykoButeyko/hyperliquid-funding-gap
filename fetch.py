"""Pull funding history, hourly closes and one order book snapshot from Hyperliquid, Binance and Bybit
into funding.db. Safe to rerun: each series continues from the last row it already has.

I run this from my own machine, not the VPS, because the VPS IP already uses most of the
Hyperliquid request budget (1200 weight per minute per IP)."""
import sqlite3
import time

import requests

COINS = ["BTC", "ETH", "SOL", "HYPE"]
HL = "https://api.hyperliquid.xyz/info"
BN = "https://fapi.binance.com"
BY = "https://api.bybit.com"
START = 1_546_300_800_000        # 2019-01-01, earlier than any of these markets
HOUR = 3_600_000
BN_PX_FROM = 1_682_899_200_000   # 2023-05-01: Binance hourly closes from here cover the whole HL overlap
HL_WEIGHT_PER_MIN = 600          # half the HL limit, so my other tools on this IP keep working
BOOK_SIZES = [100_000, 1_000_000, 5_000_000]   # USD notional for the slippage snapshot

session = requests.Session()
_next = [0.0]


def hl(body, weight):
    """POST /info, paced by request weight. fundingHistory costs 20 + 1 per 20 rows (500 rows -> 45)."""
    wait = _next[0] - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _next[0] = max(_next[0], time.monotonic()) + 60 * weight / HL_WEIGHT_PER_MIN
    for attempt in range(5):
        r = session.post(HL, json=body, timeout=30)
        if r.status_code == 200:
            return r.json()
        time.sleep(2 ** attempt * 5)
    r.raise_for_status()


def get(url, params):
    for attempt in range(5):
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        time.sleep(2 ** attempt * 5)
    r.raise_for_status()


def setup(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS funding (venue TEXT, coin TEXT, time INTEGER, rate REAL, premium REAL, mark REAL,
                                        PRIMARY KEY (venue, coin, time));
    CREATE TABLE IF NOT EXISTS candles (venue TEXT, coin TEXT, time INTEGER, close REAL,
                                        PRIMARY KEY (venue, coin, time));
    CREATE TABLE IF NOT EXISTS depth (venue TEXT, coin TEXT, time INTEGER, notional REAL, spread_bps REAL,
                                      slip_bps REAL, PRIMARY KEY (venue, coin, time, notional));
    """)


def last(db, table, venue, coin):
    t = db.execute(f"SELECT max(time) FROM {table} WHERE venue=? AND coin=?", (venue, coin)).fetchone()[0]
    return t + 1 if t else START


def funding_hl(db, coin):
    start = last(db, "funding", "hl", coin)
    while True:
        rows = hl({"type": "fundingHistory", "coin": coin, "startTime": start}, 20 + 25)
        db.executemany("INSERT OR IGNORE INTO funding VALUES ('hl',?,?,?,?,NULL)",
                       [(coin, r["time"], float(r["fundingRate"]), float(r["premium"])) for r in rows])
        db.commit()
        if len(rows) < 500:
            return
        start = rows[-1]["time"] + 1


def funding_bn(db, coin):
    start = last(db, "funding", "bn", coin)
    while True:
        rows = get(BN + "/fapi/v1/fundingRate", {"symbol": coin + "USDT", "startTime": start, "limit": 1000})
        db.executemany("INSERT OR IGNORE INTO funding VALUES ('bn',?,?,?,NULL,?)",
                       [(coin, r["fundingTime"], float(r["fundingRate"]), float(r.get("markPrice") or "nan")) for r in rows])
        db.commit()
        if not rows:
            return
        start = rows[-1]["fundingTime"] + 1
        time.sleep(0.5)


def funding_by(db, coin):
    # Bybit pages newest first, 200 rows, so walk backwards from now to what I already have.
    stop, end = last(db, "funding", "by", coin), int(time.time() * 1000)
    while end > stop:
        rows = get(BY + "/v5/market/funding/history",
                   {"category": "linear", "symbol": coin + "USDT", "endTime": end, "limit": 200})["result"]["list"]
        rows = [(coin, int(r["fundingRateTimestamp"]), float(r["fundingRate"])) for r in rows]
        db.executemany("INSERT OR IGNORE INTO funding VALUES ('by',?,?,?,NULL,NULL)", rows)
        db.commit()
        if len(rows) < 200:
            return
        end = min(r[1] for r in rows) - 1
        time.sleep(0.2)


def candles(db, coin):
    # Hourly closes. HL serves at most 5000 candles, so the price gap between venues covers ~200 days.
    # Binance goes back to the start of HL history, for the margin check on a hedge held the whole time.
    since = max(last(db, "candles", "hl", coin), int(time.time() * 1000) - 5000 * HOUR)
    now = int(time.time() * 1000)
    rows = hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": since, "endTime": now}},
              20 + 5000 // 60)
    db.executemany("INSERT OR IGNORE INTO candles VALUES ('hl',?,?,?)", [(coin, r["t"], float(r["c"])) for r in rows])
    first = db.execute("SELECT min(time) FROM candles WHERE venue='bn' AND coin=?", (coin,)).fetchone()[0]
    t = BN_PX_FROM if first is None or first > BN_PX_FROM else last(db, "candles", "bn", coin)
    while t < now:
        rows = get(BN + "/fapi/v1/klines", {"symbol": coin + "USDT", "interval": "1h", "startTime": t, "limit": 1500})
        db.executemany("INSERT OR IGNORE INTO candles VALUES ('bn',?,?,?)", [(coin, r[0], float(r[4])) for r in rows])
        if not rows:
            break
        t = rows[-1][0] + HOUR
    t = since
    while t < now:
        rows = get(BY + "/v5/market/kline", {"category": "linear", "symbol": coin + "USDT", "interval": "60",
                                             "start": t, "end": t + 999 * HOUR, "limit": 1000})["result"]["list"]
        db.executemany("INSERT OR IGNORE INTO candles VALUES ('by',?,?,?)",
                       [(coin, int(r[0]), float(r[4])) for r in rows])
        t += 1000 * HOUR
    db.commit()


def slip(levels, notional, mid):
    """Average fill vs mid in bps for a market order of `notional` USD walking `levels` [(px, sz)]."""
    left, cost, qty = notional, 0.0, 0.0
    for px, sz in levels:
        take = min(left, px * sz)
        cost += take
        qty += take / px
        left -= take
        if left <= 0:
            return abs(cost / qty / mid - 1) * 1e4
    return None   # book shallower than the order


def depth(db, coin):
    now = int(time.time() * 1000)
    def hl_book(**agg):
        b = hl({"type": "l2Book", "coin": coin, **agg}, 2)["levels"]
        return [(float(x["px"]), float(x["sz"])) for x in b[0]], [(float(x["px"]), float(x["sz"])) for x in b[1]]
    books = {"hl": hl_book()}
    coarse = hl_book(nSigFigs=4)   # HL shows 20 levels; price-rounded levels reach deeper, a bit pessimistic
    b = get(BN + "/fapi/v1/depth", {"symbol": coin + "USDT", "limit": 1000})
    books["bn"] = ([(float(p), float(s)) for p, s in b["bids"]], [(float(p), float(s)) for p, s in b["asks"]])
    b = get(BY + "/v5/market/orderbook", {"category": "linear", "symbol": coin + "USDT", "limit": 500})["result"]
    books["by"] = ([(float(p), float(s)) for p, s in b["b"]], [(float(p), float(s)) for p, s in b["a"]])
    for venue, (bids, asks) in books.items():
        mid = (bids[0][0] + asks[0][0]) / 2
        spread = (asks[0][0] - bids[0][0]) / mid * 1e4
        for n in BOOK_SIZES:
            s = [slip(bids, n, mid), slip(asks, n, mid)]
            if venue == "hl" and None in s:
                s = [slip(coarse[0], n, mid), slip(coarse[1], n, mid)]
            db.execute("INSERT OR REPLACE INTO depth VALUES (?,?,?,?,?,?)",
                       (venue, coin, now, n, spread, None if None in s else max(s)))
    db.commit()


if __name__ == "__main__":
    db = sqlite3.connect("funding.db")
    setup(db)
    for coin in COINS:
        for f in (funding_hl, funding_bn, funding_by, candles, depth):
            f(db, coin)
        print(coin, dict(db.execute("SELECT venue, count(*) FROM funding WHERE coin=? GROUP BY venue", (coin,))))
