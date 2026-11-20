"""The ONE way every backtest sells what it bought (owner, 2026-11-20: vendoring whatever is unsold after 2 days
"seems silly"; "re-do everything we've considered"). Never dump to the vendor, never value unsold stock at a later
asking price nobody paid.

the owner's real selling: list 1c under the cheapest other seller (never above the target price), take back and relist
when undercut, until it sells. Replayed here on the hourly records:
  - each hour our ask = min(cap - 1, cheapest other - 1). While the cheapest still looks like the pile we bought
    (<= 1.02 x what we paid) the cheapest OTHER is unknown, so the ask stays where it was.
  - a real sale between our ask and 1.05 x it (bought off the bottom) takes our units first (we are the cheapest).
  - relist fees: FEE_SHARE of the gold back. His own record: 47g of fees lost on 1,964g of flip sales (2.4%).
Result: gold back (after the 5% cut and fees), units sold, units left at the end of the records, our last ask.
Units left are reported apart: "so far" never counts them; "if the rest sell" values them at our last ask.
"""
from collections import defaultdict

CUT, FEE_SHARE, OVER, ROW_CAP = 0.95, 0.025, 1.05, 20

class Index:
    """per item: the cheapest price and the sales of every hourly record, so a replay can follow one item fast"""
    def __init__(self, recs):
        self.t = [r["t"] for r in recs]
        self.low = defaultdict(dict)     # item -> {record index: cheapest price}
        self.sold = defaultdict(list)    # item -> [(record index, units, price)]
        for i, r in enumerate(recs):
            for b in r["book"]: self.low[b[0]][i] = b[1]
            for it, u, p in r["sold"]: self.sold[it].append((i, u, p))

def follow(ix, item, n, paid, cap, start):
    """sell n units bought at `paid` (each) in record `start`, never above cap - 1c.
    -> (back, sold, left, ask, done): done = the record where the last unit sold (len(records) if some are left);
    a replay must not buy the same item again before then (it would sell into the same buyers twice)"""
    ask, sold, back = cap - 1, 0, 0
    lows, sales = ix.low.get(item, {}), [s for s in ix.sold.get(item, []) if s[0] > start]
    si = 0
    for i in range(start + 1, len(ix.t)):
        low = lows.get(i)
        if low is not None and low > paid * 1.02: ask = min(cap - 1, low - 1)
        while si < len(sales) and sales[si][0] == i:
            _, u, p = sales[si]; si += 1
            if ask <= p <= ask * OVER and sold < n:
                u = min(u, ROW_CAP, n - sold); sold += u; back += int(ask * CUT) * u
        if sold >= n: return int(back * (1 - FEE_SHARE)), sold, 0, ask, i
    return int(back * (1 - FEE_SHARE)), sold, n - sold, ask, len(ix.t)

def selftest():
    recs = [{"t": 0, "book": [[7, 500, 4, 4, 4]], "sold": []},
            {"t": 1, "book": [[7, 900, 1, 1, 1]], "sold": [[7, 2, 899]]},
            {"t": 2, "book": [[7, 700, 1, 1, 1]], "sold": [[7, 5, 700], [7, 3, 2000]]}]
    ix = Index(recs)
    b, s, l, a, d = follow(ix, 7, 4, 500, 1000, 0)
    assert (s, l, a, d) == (4, 0, 699, 2), "2 sold at 8s99c, then undercut: follow down to 6s99c and sell 2 more"
    assert b == int((int(899 * CUT) * 2 + int(699 * CUT) * 2) * (1 - FEE_SHARE)), "after the cut and relist fees"
    assert follow(ix, 7, 4, 500, 1000, 1)[0] == int(int(699 * CUT) * 4 * (1 - FEE_SHARE)), "only sales after the buy count"
    recs[1]["book"] = [[7, 505, 1, 1, 1]]; recs[1]["sold"] = [[7, 1, 999]]
    b, s, l, a, d = follow(Index(recs), 7, 1, 500, 1000, 0)
    assert s == 1 and l == 0 and d == 1, "our own pile still shows: the ask stays at the cap and sells there"
    assert follow(Index([recs[0], {"t": 1, "book": [], "sold": [[7, 2, 5000]]}]), 7, 1, 500, 1000, 0)[1] == 0, \
        "gone far above our ask: a pulled listing, not a sale"
    assert follow(ix, 7, 9, 500, 1000, 0)[4] == 3, "units left: never done"

if __name__ == "__main__":
    selftest(); print("exits OK")
