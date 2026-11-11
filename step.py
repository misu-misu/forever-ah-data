"""One hourly step, run by GitHub Actions: fetch the Horde PvP US auction house, compare with the previous
copy, and save a small record of estimated sales + current market depth.

Inputs : BLIZZARD_ID / BLIZZARD_SECRET env vars; prev.json.gz (previous full copy, kept in the Actions cache)
Outputs: data/YYYY-MM-DD/HHMMSS.json.gz = {"t", "prev_t", "sold": [[item, units, unit_price]],
                                         "book": [[item, low, units_at_low, units_within_5pct, units_listed]]}
         prev.json.gz replaced with the new full copy.

Sales rules per listing id (old copy -> new copy):
  - same id, smaller quantity                                   -> sold the difference (certain)
  - vanished while SHORT (<30 min left)                         -> expired, ignored
  - vanished, no cheaper listing of the same item survived      -> sold (buyers take the cheapest first)
  - vanished, a cheaper listing of the same item survived       -> cancelled (likely an undercut relist)
  - gear only: vanished, and a NEW listing of the same item and quantity appeared at the same price or
    cheaper                                                     -> relisted, not sold (a 5000g item "selling"
    2.5 a day on 2026-11-11 was one seller reposting it). Trade goods skip this: thousands of new stacks of
    20 appear every hour, so it would wipe out real sales.
Rows: [item, quantity, unit_price, time_left, kind] with kind "i" (gear) or "c" (trade goods); old 4-field
copies count as trade goods.
Cancels that pass as sales make the numbers lean HIGH.
"""
import base64, gzip, json, os, sys, urllib.request
from collections import defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path

HOUSE = 113  # Horde PvP US: matched 80% of lowest prices to Mike's in-game scan on 2026-11-10 (other houses 11-21%)
API = "https://us.api.blizzard.com/data/wow/ruleset/1/auctions/{}{}?namespace=dynamic-forever-us"
PREV = Path("prev.json.gz")

def token():
    auth = base64.b64encode(f"{os.environ['BLIZZARD_ID']}:{os.environ['BLIZZARD_SECRET']}".encode()).decode()
    req = urllib.request.Request("https://oauth.battle.net/token", data=b"grant_type=client_credentials",
                                 headers={"Authorization": "Basic " + auth})
    return json.load(urllib.request.urlopen(req, timeout=60))["access_token"]

def fetch():
    tok, rows, stamps = token(), {}, []
    for kind in ("", "/commodities"):
        req = urllib.request.Request(API.format(HOUSE, kind), headers={"Authorization": "Bearer " + tok, "Accept-Encoding": "gzip"})
        resp = urllib.request.urlopen(req, timeout=120); b = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip": b = gzip.decompress(b)
        stamps.append(parsedate_to_datetime(resp.headers["Last-Modified"]).timestamp())
        for a in json.loads(b).get("auctions", []):
            price = a.get("unit_price") or a.get("buyout")
            if price is None: continue  # bid-only listing
            qty = a.get("quantity", 1)
            if "buyout" in a and qty > 1: price //= qty  # gear buyout covers the whole listing
            rows[a["id"]] = [a["item"]["id"], qty, price, a.get("time_left", ""), "c" if kind else "i"]
    # guard: a tiny copy means the feed broke, not that the market emptied
    assert len(rows) > 50000, f"only {len(rows)} listings: feed changed or broke?"
    return max(stamps), rows

def diff(old, new):
    """Yield (item, units_sold, unit_price) for one pair of copies."""
    survivors_min = {}  # item -> cheapest price among listings that survived untouched
    for aid, (item, qty, price, tl, *_) in old.items():
        if aid in new and new[aid][1] == qty:
            survivors_min[item] = min(price, survivors_min.get(item, price))
    fresh = defaultdict(list)  # gear only: (item, quantity) -> prices of listings that are new in this copy
    for aid, r in new.items():
        if aid not in old and r[4:5] == ["i"]: fresh[(r[0], r[1])].append(r[2])
    for aid, (item, qty, price, tl, *kind) in old.items():
        if aid in new:
            if new[aid][1] < qty: yield item, qty - new[aid][1], price
        elif tl != "SHORT" and price <= survivors_min.get(item, price):
            if kind == ["i"] and any(p <= price for p in fresh.get((item, qty), ())): continue  # reposted
            yield item, qty, price

def book(rows):
    by = defaultdict(list)
    for item, qty, price, *_ in rows.values(): by[item].append((price, qty))
    out = []
    for item, l in by.items():
        low = min(p for p, _ in l)
        out.append([item, low, sum(q for p, q in l if p == low), sum(q for p, q in l if p <= low * 1.05), sum(q for _, q in l)])
    return out

def selftest():
    old = {1: [7, 5, 100, "LONG"], 2: [7, 1, 200, "LONG"], 3: [7, 1, 300, "LONG"], 4: [8, 1, 50, "SHORT"], 5: [9, 2, 10, "LONG"]}
    new = {1: [7, 3, 100, "LONG"], 3: [7, 1, 300, "LONG"]}
    assert sorted(diff(old, new)) == [(7, 1, 200), (7, 2, 100), (9, 2, 10)], sorted(diff(old, new))
    assert list(diff({1: [7, 1, 100, "LONG"], 2: [7, 1, 200, "LONG"]}, {1: [7, 1, 100, "LONG"]})) == []  # cancel
    # gear reposted at the same price or cheaper is not a sale; gone with nothing new is
    assert list(diff({1: [7, 1, 500, "LONG", "i"]}, {9: [7, 1, 500, "LONG", "i"]})) == []
    assert list(diff({1: [7, 1, 500, "LONG", "i"]}, {9: [7, 1, 450, "LONG", "i"]})) == []
    assert list(diff({1: [7, 1, 500, "LONG", "i"]}, {9: [7, 1, 600, "LONG", "i"]})) == [(7, 1, 500)]
    assert list(diff({1: [7, 1, 500, "LONG", "i"]}, {9: [7, 2, 500, "LONG", "i"]})) == [(7, 1, 500)]
    # trade goods: a new stack at the same price does not hide a sale
    assert list(diff({1: [7, 20, 5, "LONG", "c"]}, {9: [7, 20, 5, "LONG", "c"]})) == [(7, 20, 5)]
    assert book({1: [7, 2, 100, ""], 2: [7, 3, 104, ""], 3: [7, 1, 200, ""]}) == [[7, 100, 2, 5, 6]]

def main():
    selftest()
    t, rows = fetch()
    prev_t, prev = None, None
    if PREV.exists():
        with gzip.open(PREV, "rt") as f: d = json.load(f)
        prev_t, prev = d["t"], {int(k): v for k, v in d["rows"].items()}
    if prev_t == t:
        print("Blizzard hasn't published a new copy yet"); return
    sold = [list(s) for s in diff(prev, rows)] if prev else []
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(t, timezone.utc)
    out = Path("data") / when.strftime("%Y-%m-%d") / (when.strftime("%H%M%S") + ".json.gz")
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt") as f:
        json.dump({"t": t, "prev_t": prev_t, "sold": sold, "book": book(rows)}, f, separators=(",", ":"))
    with gzip.open(PREV, "wt") as f: json.dump({"t": t, "rows": rows}, f, separators=(",", ":"))
    print(f"saved {out}: {len(rows)} listings, {len(sold)} sales events since previous copy")

if __name__ == "__main__":
    main()
