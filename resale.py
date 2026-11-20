"""Resale price list for VendorSnipe's FLIP deals, from the hourly cloud records (repo forever-ah-data).
Usage: python resale.py   -> writes ResalePrices.lua (OUT). Built online every hour by .github/workflows/prices.yml
(2026-11-20: the owner's PC ran hot building it locally); the PC only downloads the finished file into the add-on.
On the PC, ../ah/resale.py loads this file and points OUT and SAVED at the game folders.

Per item, over the last WINDOW_DAYS of records:
  p = normal price: the LOWEST of four medians (2026-11-12: prices are still falling, and a list priced off
      old asking prices bought into the fall): all hourly cheapest prices, the last RECENT of them, and the
      prices of the sales in the last RECENT hours (when 2+ sold), and the prices of every sale in the window
      (when SLOW_MIN+ sold: an asking price nobody pays is not the normal price). One bad hour can't move a median.
  d = units sold per day, counting only sales at or under 1.25 x p (a "sale" far above the normal price is
      almost surely a cancelled listing, the old 5000g relist case)
  r = sale rate: sold / (sold + ran out unsold), from records that count the unsold ones; -1 = not known yet
  n = units listed right now
  low = cheapest price in the last hourly record (0 = not listed). owner, 2026-11-12: every Bargain Watch row
      on his screen was a price that had already sat there for 1-3 hours (Globe of Water, Demonsage, Golden
      Scale Coif...). A listing at or above this was already on the market an hour ago: not a fresh bargain.
Only items with sales in at least MIN_SALE_HOURS different hours are written. The add-on only uses the list
once it covers READY_HOURS (it checks `hours` itself), so a thin first day can't start flips.

Safety lock (owner, 2026-11-12): `ok` is written true only when replay() shows the list would have worked.
owner, 2026-11-20 ("a) Profit in hand"; vendoring unsold stock "seems silly"): the old test (resold at the normal
price within 12 hours) passed only 22% and kept scan flips off. Now each test buy is sold the one shared way
(exits.follow: relist at the cheapest until sold, relist fees counted), and only buys with MATURE_H+ hours left
in the records count: ok = MIN_TESTED+ of them and the gold already back >= the gold spent (unsold stock counts
nothing). Nov 20: 2,832 test buys, 3,231g spent, -71g so far, 1,140g unsold -> OFF. The add-on's scan flips stay
off while ok is false; Bargain Watch does not use it.

TSM sale rates (owner, 2026-11-13, "2a"): TradeSkillMaster's free public Forever file (US region: sale rate,
sold per day, 14-day market value) is written as VendorSnipeTSM, for the add-on's second "does it sell" check. One region figure for
all rulesets and factions together. Downloaded at most once an hour; a copy older than TSM_MAX_AGE is not used.
"""
import re, calendar, csv, gzip, io, json, statistics, subprocess, time, urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE   # the records: data/<day>/<time>.json.gz next to this file
OUT = HERE / "ResalePrices.lua"
WINDOW_DAYS, MIN_SALE_HOURS, SALE_CAP, SLOW_MIN, RECENT = 7, 3, 1.25, 3, 6
MIN_TESTED, MATURE_H, REPLAY_EVERY = 20, 72, 3   # replay: rebuild the list every 3rd record
FALL_RATIO, FALL_MIN = 0.85, 6   # falling: last day's middle cheapest price under 85% of the day before's (6+ hours each)
REPOST_HOURS = 6   # a lone stack that vanishes and is back within this long was reposted, not sold
PCT, MIN_PROFIT, DAY_SHARE = 20, 500, 0.1   # the add-on's FLIP_PCT, FLIP_MIN, FLIP_DAY_SHARE
# owner, 2026-11-20 ("Yes, plan it" / "Add it, execute"): the fresh-dip numbers, x[10]-x[12] in the add-on.
# Steady price = the price the cheapest listing sat at or above in DIP_HELD of the hours (DIP_MIN_HOURS+ seen).
# Proof our buyers pay it: DIP_MIN_UNITS+ real sales at DIP_NEAR x it or more, on DIP_MIN_DAYS+ days (UTC).
# Replay 2026-11-20 (strategies_replay.py, relist until sold): +42% on 451g over ~6 days.
DIP_HELD, DIP_MIN_HOURS, DIP_NEAR, DIP_MIN_UNITS, DIP_MIN_DAYS, DIP_LOW = 0.9, 72, 0.9, 5, 4, 0.75
net = lambda ah: int((ah - 1) * 0.95)       # after the 5% cut, listed 1c under
base = lambda item: int(str(item).split(":")[0])   # "9815:12745" -> 9815
TSM_URL = "https://public-data.tradeskillmaster.com/forever/us/region/items.csv"
TSM_CACHE, TSM_MAX_AGE = HERE / "tsm_items.csv", 3 * 86400
REPLAY_CACHE, CACHE_KEEP = HERE / "replay_cache.pkl", 3000   # replay()'s saved answers, newest used kept

def tsm_parse(text, keep):
    """{item: [sale rate, sold per day, 14-day market value in copper]} for the items in keep, from TSM's items.csv"""
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        i = int(row["itemId"])
        if i in keep: out[i] = [float(row["saleRate"] or 0), float(row["soldPerDay"] or 0), int(float(row["marketValue"] or 0))]
    return out

def tsm_date(text):
    """Audit 2026-11-15: the newest updatedAt in TSM's items.csv as a unix time (0: none). A fresh download can
    hold old data (all rows were 45h old), so the add-on is told the data's own age, not the download's."""
    ds = [d for d in ((r.get("updatedAt") or "") for r in csv.DictReader(io.StringIO(text))) if d.endswith("Z")]
    return calendar.timegm(time.strptime(max(ds), "%Y-%m-%dT%H:%M:%SZ")) if ds else 0

def tsm(keep):
    """TSM's rates for keep, or None when there is no copy younger than TSM_MAX_AGE (the add-on then skips it)"""
    if not TSM_CACHE.exists() or time.time() - TSM_CACHE.stat().st_mtime > 3600:
        try:
            req = urllib.request.Request(TSM_URL, headers={"User-Agent": "Mozilla/5.0 (generic client)"})
            with urllib.request.urlopen(req, timeout=30) as r: body = r.read()
            if body.startswith(b"itemId,"): TSM_CACHE.write_bytes(body)
        except Exception as e:
            print("TSM file not downloaded:", e)
    if not TSM_CACHE.exists() or time.time() - TSM_CACHE.stat().st_mtime > TSM_MAX_AGE: return None
    return tsm_parse(TSM_CACHE.read_text(encoding="utf-8"), keep)

def drop_reposts(recs):
    """owner, 2026-11-13 (Steaming Stag Steak, "Yes, both"): one seller kept pulling his only stack of 2 and posting
    it again at a new price (50s -> 1g 1c -> 95s -> 2g 95s); each pull counted as 2 sold. The hourly step only
    catches a repost in the same hour at the same or a lower price. Here, with every record in hand: an item's
    "sales" in one hour are all dropped when they were all at ONE price (one seller's batch; buyers sweeping a
    busy market take several price levels), took HALF or more of what was listed the hour before, and the
    listed count is back to at least that level at that record or any within REPOST_HOURS.
    owner, 2026-11-13 ("fix the repost gap too"): the first version needed the batch to be the WHOLE listing, so
    the Hoof-Shaped Foot Pedal seller (11 of 12 gone in one hour, 12 back the next) still counted as 11 sold.
    Checked against TSM's region sales (real sales its users record), 2026-11-13, rank agreement with our
    per-item counts: no repost rule 0.773, whole-listing rule 0.780, half rule without the one-price test 0.776
    (it dropped 15% of all units, busy trade goods too), this rule 0.781.
    Busy markets (a few units gone out of many) are untouched, so they keep their real sales. Leans "not
    sold", like the hourly step: undercounting makes the add-on buy less."""
    recs = sorted(recs, key=lambda r: r["t"])
    n = [{row[0]: row[4] for row in r["book"]} for r in recs]
    out = []
    for k, r in enumerate(recs):
        gone, prices = defaultdict(int), defaultdict(set)
        for s in r["sold"]: gone[s[0]] += s[1]; prices[s[0]].add(s[2])
        def back(item):
            before = n[k - 1].get(item, 0) if k else 0
            return before and len(prices[item]) == 1 and gone[item] * 2 >= before and any(
                n[j].get(item, 0) >= before for j in range(k, len(recs)) if recs[j]["t"] <= r["t"] + REPOST_HOURS * 3600)
        drop = {item for item in gone if back(item)}
        out.append(dict(r, sold=[s for s in r["sold"] if s[0] not in drop]))
    return out

SAVED = None   # the add-on's saved file (own take-backs). Online: none, so they count as sales (measured 2026-11-20:
               # 217 take-backs changed no price, only 14 fast sellers' sales a day, all far above every limit)

def own_takebacks(text):
    """the owner's take-backs from the add-on's saved file: [(time, item key, units, price each)]."""
    m = re.search(r'\n\["tblog"\] = \{\n(.*?)\n\},?\n', text, re.S)
    out = []
    for t, k, q, p in re.findall(r'"(\d+)\|([\d:\-]+)\|(\d+)\|(\d+)"', m.group(1) if m else ""):
        out.append((int(t), int(k) if k.isdigit() else k, int(q), int(p)))
    return out

def drop_own(recs, tbs):
    """owner, 2026-11-18 ("Build both"): the records count a listing he took back as sold (109 of his 180 take-backs
    that day matched a "sale" of the same units at his price), so items he kept taking back looked busier and
    dearer. Each take-back removes one matching sale (same item, same units, his price +-1c) from the record that
    covers its time."""
    recs = [dict(r, sold=list(r["sold"])) for r in recs]
    for t, k, q, p in tbs:
        for r in recs:
            if r.get("prev_t") is not None and r["prev_t"] < t <= r["t"]:
                for i, s in enumerate(r["sold"]):
                    if s[0] == k and s[1] == q and abs(s[2] - p) <= 1: del r["sold"][i]; break
                break
    return recs

def trend(day1, day2):
    """owner, 2026-11-14 ("stop showing falling prices"; Guardian Stone 17g -> 15g -> 11g -> 10g, Gift of Arthas 16g ->
    1g 11s, both shown as bargains off yesterday's price): the middle cheapest price of the last 24h over that of
    the 24h before. Under FALL_RATIO = still falling: no bargain. 1 = not enough hours to tell (never blocks)."""
    if len(day1) < FALL_MIN or len(day2) < FALL_MIN: return 1
    return round(statistics.median(day1) / statistics.median(day2), 2)

REAL_OVER, REAL_ROW_CAP = 1.05, 20   # a real sale: gone at <= 1.05 x the last cheapest; one row counts 20 units at most

def wmedian(rows):
    """middle price of (units, price) rows, each unit counted once; 0 = none"""
    if not rows: return 0
    rows, half, n = sorted(rows, key=lambda r: r[1]), sum(u for u, _ in rows) / 2, 0
    for u, price in rows:
        n += u
        if n >= half: return int(price)

def dip(lows, real, last48, hours):
    """[steady price, real sales a day at DIP_NEAR x it or more, hours of the last 48 at DIP_LOW x it or under];
    [0, 0, 0] when there are too few hours or too little proof that buyers pay it"""
    if len(lows) < DIP_MIN_HOURS or not hours: return [0, 0, 0]
    n = sorted(lows)[int(len(lows) * (1 - DIP_HELD) + 1e-9)]
    hi = [(t, u) for t, p, u in real if p >= DIP_NEAR * n]
    units = sum(u for _, u in hi)
    if units < DIP_MIN_UNITS or len({time.strftime("%Y-%m-%d", time.gmtime(t)) for t, _ in hi}) < DIP_MIN_DAYS: return [0, 0, 0]
    return [int(n), round(units * 24 / hours, 2), sum(1 for x in last48 if x <= DIP_LOW * n)]

def build(recs, now):
    cut = now - WINDOW_DAYS * 86400
    recs = sorted((r for r in recs if r["t"] >= cut), key=lambda r: r["t"])
    lows, sold, sale_hours, gone = defaultdict(list), defaultdict(list), defaultdict(int), defaultdict(int)
    day1, day2 = defaultdict(list), defaultdict(list)   # cheapest prices in the last 24h, and the 24h before
    listed, lastlow, hours = {}, {}, 0.0
    # owner, 2026-11-18 ("build 1-3"): real sales = a listing gone at no more than REAL_OVER x the cheapest price
    # an hour before (bought off the bottom, not a high ask pulled). Their middle price and count a day feed the
    # add-on's green-gear proof and busy-item price (x[8], x[9]).
    prevlow, realsold, realt = {}, defaultdict(list), defaultdict(list)
    for r in recs:
        if r["prev_t"] is not None:
            for item, units, price in r["sold"]:
                if item in prevlow and price <= prevlow[item] * REAL_OVER:
                    realsold[item].append((min(units, REAL_ROW_CAP), price)); realt[item].append((r["t"], price, min(units, REAL_ROW_CAP)))
        prevlow = {row[0]: row[1] for row in r["book"]}
        for item, low, *_ in r["book"]:
            lows[item].append(low)
            age = now - r["t"]
            if age < 86400: day1[item].append(low)
            elif age < 2 * 86400: day2[item].append(low)
        if r["prev_t"] is None: continue
        hours += (r["t"] - r["prev_t"]) / 3600
        hit = set()
        for item, units, price in r["sold"]:
            sold[item].append((units, price, "expired" in r)); hit.add(item)   # flag: this record counts unsold ones
        for item in hit: sale_hours[item] += 1
        for item, units in r.get("expired", []): gone[item] += units
    if recs:
        for item, low, at_low, within5, n in recs[-1]["book"]: listed[item] = n; lastlow[item] = low
    items = {}
    recent = defaultdict(list)   # sale prices in the last RECENT records
    for r in recs[-RECENT:]:
        if r["prev_t"] is not None:
            for item, units, price in r["sold"]: recent[item].append(price)
    for item, l in lows.items():
        if sale_hours[item] < MIN_SALE_HOURS or not hours: continue
        p = min(statistics.median(l), statistics.median(l[-RECENT:]))
        if len(recent[item]) >= 2: p = min(p, statistics.median(recent[item]))
        # owner, 2026-11-13 ("a, go"): Craftsman's Writ: Green Lens showed as a 99s bargain "resells at 1g 40s": 1g 40s
        # was the asking price all day (5-12 listed); its 7 sales in 3 days were 25s-1g 1s. With under 2 sales in
        # the last RECENT hours only asking prices counted. Now the median of every sale in the window caps it too.
        allp = [price for _, price, _ in sold[item]]
        if len(allp) >= SLOW_MIN: p = min(p, statistics.median(allp))
        p = int(p)
        real = [(u, f) for u, price, f in sold[item] if price <= p * SALE_CAP]
        d = sum(u for u, _ in real) * 24 / hours
        if d < 1 and sum(u for u, _ in real) < SLOW_MIN: continue   # slow sellers kept if 3+ sold (owner, 2026-11-12)
        counted = sum(u for u, f in real if f)
        rate = round(counted / (counted + gone[item]), 2) if counted + gone[item] else -1
        rs = realsold[item]
        items[item] = [p, round(d, 1), rate, listed.get(item, 0), sum(u for u, _ in real), lastlow.get(item, 0), trend(day1[item], day2[item]),
                       wmedian(rs), round(sum(u for u, _ in rs) * 24 / hours, 1)] + dip(l, realt[item], day1[item] + day2[item], hours)
    # an item id that has variant entries ("9815:12745", from 2026-11-12) also has older records that mixed
    # all its variants under the plain id: that mixed price is not trusted, so the plain id is dropped
    for v in [k for k in items if isinstance(k, str)]: items.pop(int(v.split(":")[0]), None)
    return {"hours": round(hours, 1), "t": int(now), "items": items}

def replay(recs, cache=None):
    """Would the list have worked? Every REPLAY_EVERY records: build the list from the records up to then only,
    and find the items the add-on would buy (cheapest PCT% under the price after the cut, MIN_PROFIT more,
    sells 1+ a day, and only a few at that price: a pile means the price moved, not a dip). Each item counts
    once, at its first buy, if MATURE_H+ hours of records follow it. The units at the cheapest price are sold by
    exits.follow (never above the normal price). Returns (buys tested, gold spent, gold back so far).
    2026-11-20 (PC running hot): rebuilding the list at every 3rd record took 4+ minutes and grew every day.
    cache (a dict, kept between runs by write()) holds each record's would-buy list under a fingerprint of this
    file and of every record up to it: a changed rule or any changed earlier record recomputes it, so a cached
    answer is always the answer a full run would give (selftest checks both ways)."""
    import exits, hashlib
    recs = sorted(recs, key=lambda r: r["t"])
    ix = exits.Index(recs)
    tested, spent, back, seen = 0, 0, 0, set()
    fp = hashlib.sha256(Path(__file__).read_bytes())
    for k, now in enumerate(recs):
        if now["t"] > recs[-1]["t"] - MATURE_H * 3600: break
        fp.update(json.dumps(now, sort_keys=True).encode())
        if k < RECENT or (k - RECENT) % REPLAY_EVERY: continue
        key = fp.hexdigest()
        cands = cache.pop(key, None) if cache is not None else None   # re-added below: newest last
        if cands is None:
            items, cands = build(recs[:k + 1], now["t"])["items"], []
            for item, low, at_low, *_ in now["book"]:
                if item not in items: continue
                p, perday = items[item][0], items[item][1]
                if items[item][6] < FALL_RATIO: continue   # falling: the add-on does not offer it
                buy = max(1, int(perday * DAY_SHARE))
                if perday < 1 or low * (100 + PCT) > 100 * net(p) or net(p) - low < MIN_PROFIT or at_low > buy: continue
                cands.append((item, low, at_low, p))
        if cache is not None: cache[key] = cands
        for item, low, at_low, p in cands:
            if item in seen: continue
            seen.add(item); tested += 1; spent += low * at_low
            back += exits.follow(ix, item, at_low, low, p, k)[0]
    return tested, spent, back

def locked(tested, spent, back):
    """The safety lock: True = resale stays OFF (too few test buys, or they have not brought their gold back yet)."""
    return tested < MIN_TESTED or back < spent

def suffixes(recs, now):
    """owner, 2026-11-14 ("lets trade all that sell at least OK"): how well each random-stat version ("of the Owl" =
    its lowest bonus id) sells on our server, over every item that carries it: {bonus: [sold a day, share of
    listings that sell]}. Only records that count unsold listings ("expired") are used, so the share is fair."""
    cut = now - WINDOW_DAYS * 86400
    sold, gone, hours = defaultdict(int), defaultdict(int), 0.0
    for r in recs:
        if r["t"] < cut or r["prev_t"] is None or "expired" not in r: continue
        hours += (r["t"] - r["prev_t"]) / 3600
        for item, units, _ in r["sold"]:
            if isinstance(item, str) and ":" in item: sold[min(int(b) for b in item.split(":")[1].split("-"))] += units
        for item, units in r["expired"]:
            if isinstance(item, str) and ":" in item: gone[min(int(b) for b in item.split(":")[1].split("-"))] += units
    if not hours: return {}
    return {b: [round(n * 24 / hours, 1), round(n / (n + gone[b]), 2)] for b, n in sold.items()}

def market(recs, now):
    """Every item listed in the last 24h (owner, 2026-11-11: the bag tooltip showed the cheapest listing of the
    last scan, often the very deal he just bought, so it said nothing about what the item sells for).
    [item] = {typical price = median of the hourly cheapest prices, units sold in 24h at <= 1.25 x that, hours seen}"""
    recs = [r for r in recs if r["t"] >= now - 86400]
    lows, sold = defaultdict(list), defaultdict(list)
    for r in recs:
        for item, low, *_ in r["book"]: lows[base(item)].append(low)
        if r["prev_t"] is not None:
            for item, units, price in r["sold"]: sold[base(item)].append((units, price))
    out = {}
    for item, l in lows.items():
        p = int(statistics.median(l))
        # 2026-11-13: an asking price nobody pays is not the typical price (same rule as build)
        allp = [pr for _, pr in sold[item]]
        if len(allp) >= SLOW_MIN: p = min(p, int(statistics.median(allp)))
        out[item] = [p, sum(u for u, pr in sold[item] if pr <= p * SALE_CAP), len(l)]
    return out

def lua(data):
    k = lambda i: f'["{i}"]' if isinstance(i, str) else f"[{i}]"
    rows = ",\n".join(f"{k(i)}={{{','.join(str(x) for x in v)}}}" for i, v in sorted(data["items"].items(), key=lambda kv: str(kv[0])))
    return (f"-- made by resale.py {time.strftime('%Y-%m-%d %H:%M')}: [item] or [\"item:variant\"] = {{normal price, sold per day, sale rate, listed, units sold in the window, cheapest at the last hourly record, last day's price / the day before's, middle real-sale price, real sales a day, steady price, real sales a day near it, hours cheap of the last 48 (0,0,0 = no fresh-dip proof)}}\n"
            f"-- safety lock: {data.get('replay', 'not replayed')}\n"
            f"VendorSnipeResale = {{hours = {data['hours']}, t = {data['t']}, ok = {'true' if data.get('ok') else 'false'}, items = {{\n{rows}\n}}}}\n"
            + "-- [item] = {typical price over the last 24h, units sold in 24h, hours seen}\nVendorSnipeMarket = {"
            + ",".join(f"[{i}]={{{p},{s},{h}}}" for i, (p, s, h) in sorted(data.get("market", {}).items())) + "}\n"
            + "-- TSM's US Forever region data: [item] = {sale rate, sold per day, 14-day market value}; nil = no fresh copy\nVendorSnipeTSM = "
            + ("nil" if data.get("tsm") is None else "{" + ",".join(f"[{i}]={{{','.join(str(x) for x in v)}}}" for i, v in sorted(data["tsm"].items())) + "}") + "\n"
            + f"-- when TSM last updated that data (unix time, 0 = unknown)\nVendorSnipeTSMDate = {data.get('tsm_t', 0)}\n"
            + "-- random-stat versions on our server: [lowest bonus id] = {sold a day over all items with it, share of listings that sell}\nVendorSnipeSuffix = {"
            + ",".join(f"[{b}]={{{d},{sh}}}" for b, (d, sh) in sorted(data.get("suffix", {}).items())) + "}\n")

def selftest():
    h, D = 3600, 86400
    # fresh dip: 96 hours at 1000 cheapest, 8 at 500 (92% held -> steady 1000); 1 real sale at 950 on each of 5 days
    lows = [1000] * 96 + [500] * 8
    real = [(d * D + 100, 950, 1) for d in range(5)]
    assert dip(lows, real, [1000] * 40 + [500] * 8, 104) == [1000, round(5 * 24 / 104, 2), 8], dip(lows, real, [500] * 8, 104)
    assert dip(lows, real[:4] + [(3 * D + 200, 950, 1)], [], 104)[0] == 1000, "5 units on 4 days: proof enough"
    assert dip(lows, real[:4], [], 104) == [0, 0, 0], "4 units: under 5"
    assert dip(lows, real[:3] + [(2 * D + 5, 950, 9)], [], 104) == [0, 0, 0], "12 units but only 3 days: no proof"
    assert dip(lows, [(t, 899, u) for t, _, u in real], [], 104) == [0, 0, 0], "sales under 90% of the steady price prove nothing"
    assert dip(lows[:71], real, [], 71) == [0, 0, 0], "under 72 hours seen"
    assert dip(lows, real, [750, 751, 700], 104)[2] == 2, "cheap = 25%+ under the steady price: 750 and 700 count, 751 not"
    assert dip([1000] * 80 + [500] * 20, real, [], 100)[0] == 500, "held 1000 only 80% of the hours: steady is 500"
    book = lambda low: [[7, low, 1, 1, 5]]
    recs = [{"t": 0, "prev_t": None, "sold": [], "book": book(100)}]
    for i in range(1, 7):   # 6 hours, item 7 sells 2 a hour at 100, plus one "sale" at 5000 (a relist)
        recs.append({"t": i * h, "prev_t": (i - 1) * h, "sold": [[7, 2, 100]] + ([[7, 1, 5000]] if i == 3 else []),
                     "expired": [[7, 1]] if i == 6 else [], "book": book(100 if i != 2 else 9999)})
    d = build(recs, 6 * h)
    p, perday, rate, n, units, low, _, rmed, rday = d["items"][7][:9]
    # real sales: all at 100 with 100 cheapest an hour before, except hour 3 (cheapest 9999 at hour 2: the 5000
    # "sale" is under 1.05 x 9999, so it counts there); 13 rows of 2 or 1 units in 6h
    assert rmed == 100 and rday == 52.0, (rmed, rday)
    assert wmedian([(1, 50), (5, 100), (1, 900)]) == 100 and wmedian([]) == 0 and wmedian([(1, 10), (1, 20)]) == 10
    # a listing gone at over 1.05 x the cheapest an hour before (a high ask pulled) is not a real sale; one row of
    # 50 units counts 20 (one big buyer can't set the middle price alone)
    rr = [{"t": i * h, "prev_t": (i - 1) * h if i else None, "book": [[1, 100, 1, 1, 9]],
           "sold": [[1, 1, 105], [1, 1, 106]] + ([[1, 50, 90]] if i == 1 else []) if i else []} for i in range(4)]
    x = build(rr, 3 * h)["items"][1]
    assert x[7:9] == [90, 184.0], x   # 3 at 105 (the 106s left out) + 20 of the 50 at 90 = 23 units in 3h
    assert low == 100, low                      # the cheapest price in the last record
    assert d["hours"] == 6 and p == 100, d      # the 9999 hour doesn't move the median
    assert perday == 48.0, perday               # 12 units in 6 h; the 5000 one is left out
    assert rate == round(12 / 13, 2), rate      # 12 real sales vs 1 unsold
    assert 7 not in build(recs[:3], 2 * h)["items"]   # sales in only 2 hours: not written
    assert "[7]={100,48.0," in lua(d) and units == 12
    # variants: "7:55" has its own price; the plain 7 (mixed variants) is dropped; strings are quoted for Lua
    v = [dict(r, book=r["book"] + [["7:55", 900, 1, 1, 1]], sold=r["sold"] + ([["7:55", 1, 900]] if r["prev_t"] is not None else [])) for r in recs]
    dv = build(v, 6 * h)
    assert "7:55" in dv["items"] and 7 not in dv["items"], dv["items"]
    assert '["7:55"]={900,' in lua(dv), lua(dv)
    assert market(v, 6 * h)[7][1] == 12 + 6, "the bag tooltip folds variants into the plain id"
    # a slow seller: 3 sold in 6 days is kept, 2 is not
    slow = [{"t": i * 86400, "prev_t": (i - 1) * 86400 if i else None, "sold": [[8, 1, 50]] if i in (1, 3, 5) else [],
             "book": [[8, 50, 1, 1, 1]]} for i in range(7)]
    assert build(slow, 6 * 86400)["items"][8][4] == 3
    assert 8 not in build(slow[:5], 6 * 86400)["items"]
    m = market(recs, 6 * h)
    assert m[7] == [100, 12, 7], m[7]
    # versions: "of the Owl" = lowest bonus id, summed over every item with it; share = sold / (sold + unsold)
    sx = [{"t": 0, "prev_t": None, "sold": [], "book": []}] + [{"t": i * h, "prev_t": (i - 1) * h, "expired": [["9:55", 1]] if i == 1 else [],
          "sold": [["7:55", 2, 900], ["8:60-55", 1, 900], [7, 5, 100]], "book": []} for i in range(1, 7)]
    sf = suffixes(sx + [{"t": 7 * h, "prev_t": 6 * h, "sold": [["7:55", 50, 1]], "book": []}], 7 * h)   # no "expired": left out
    assert sf == {55: [72.0, 0.95]}, sf     # 18 sold in 6 h (7:55 and 8:55-60), 1 unsold
    assert "VendorSnipeSuffix = {[55]={72.0,0.95}}" in lua({**d, "suffix": sf}), lua({**d, "suffix": sf})   # 7 hourly records; the 9999 hour and the 5000 "sale" don't count
    assert "VendorSnipeMarket = {[7]={100,12,7}}" in lua({**d, "market": m})
    # normal price: recent sales pull it down while the price is falling
    fall = [{"t": i * h, "prev_t": (i - 1) * h if i else None, "sold": [[6, 1, 100 if i < 7 else 60]] if i else [],
             "book": [[6, 100 if i < 7 else 70, 1, 1, 1]]} for i in range(13)]
    assert build(fall, 12 * h)["items"][6][0] == 60, build(fall, 12 * h)["items"][6]
    # asked 1g 40s all along, but its sales (one an hour, so never 2 in the last RECENT hours) were 25s-1g 1s:
    # the normal price is the middle sale, 1g (Craftsman's Writ: Green Lens, 2026-11-13)
    sales = [2500, 5000, 5000, 10000, 10000, 10100, 10100]
    writ = [{"t": i * h, "prev_t": (i - 1) * h if i else None, "sold": [[4, 1, sales[(i - 1) // 3]]] if i and i % 3 == 1 else [],
             "book": [[4, 14000, 5, 5, 10]]} for i in range(22)]
    assert build(writ, 21 * h)["items"][4][0] == 10000, build(writ, 21 * h)["items"][4]
    assert market(writ, 21 * h)[4][0] == 10000, market(writ, 21 * h)[4]   # the bag tooltip too
    # replay: item 5 sells 1 an hour at 1g; at record 9 (a replay hour) it is listed at 50s
    def mk(at_low, resold, hours=90, rival=True):
        return [{"t": i * h, "prev_t": (i - 1) * h if i else None,
                 "sold": [[5, 1, 10000]] if i and (i <= 9 or resold) else [],
                 "book": [[5, 5000 if i == 9 else 10000, at_low if i == 9 else 1, 1, 5]] if i <= 9 or rival else []} for i in range(hours)]
    import exits
    assert replay(mk(1, True)) == (1, 5000, int(int(9999 * 0.95) * (1 - exits.FEE_SHARE))), replay(mk(1, True))   # resold 1c under the next seller
    one = int(int(9999 * 0.95) * (1 - exits.FEE_SHARE))
    assert replay(mk(2, True)) == (1, 10000, int(2 * int(9999 * 0.95) * (1 - exits.FEE_SHARE))), "2 at the cheapest: both bought, both resold"
    assert replay(mk(1, True, rival=False)) == (1, 5000, one), "no rival: listed 1c under the normal price, never above it"
    assert replay(mk(1, False)) == (1, 5000, 0), "no sales after the buy: no gold back (never dumped, never valued)"
    twice = mk(1, True); twice[12]["book"] = [[5, 5000, 1, 1, 5]]
    assert replay(twice)[0] == 1, "cheap again 3 hours later: still one test buy (counted at its first buy)"
    c = {}
    assert replay(mk(1, True), c) == replay(mk(1, True)) and c, "first run with a cache: same answer, cache filled"
    assert replay(mk(1, True), {k: [] for k in c}) == (0, 0, 0), "a cached answer is used, not recomputed"
    assert replay(mk(9, True), dict(c)) == (0, 0, 0), "an earlier record changed: recomputed, not the stale cache"
    assert replay(mk(9, True)) == (0, 0, 0), "9 listed cheap but we'd buy 2: a pile, not bought"
    assert replay(mk(1, True, 9 + 72))[0] == 0, "71 hours of records after the buy: too soon to judge"
    assert replay(mk(1, True, 9 + 72 + 1))[0] == 1, "72 hours after it: judged"
    assert locked(19, 100, 200) and locked(20, 100, 99) and not locked(20, 100, 100), "20+ tested and the gold back, or it stays off"
    assert "ok = false" in lua(d) and "ok = true" in lua({**d, "ok": True})
    assert "VendorSnipeTSM = nil" in lua(d) and "VendorSnipeTSM = {[7]={0.5,2.0}}" in lua({**d, "tsm": {7: [0.5, 2.0]}})
    assert tsm_date("itemId,updatedAt\n1,2026-11-13T08:38:44Z\n2,0\n") == 1794559124 and tsm_date("itemId,updatedAt\n") == 0
    assert "VendorSnipeTSMDate = 0" in lua(d) and "VendorSnipeTSMDate = 5" in lua({**d, "tsm_t": 5})
    t = tsm_parse("itemId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt\n7,X,1,1,1,0.65,1.3,z\n8,Y,1,1,1,,,z\n", {7, 8, 9})
    assert t == {7: [0.65, 1.3, 1], 8: [0.0, 0.0, 1]}, t
    # falling: a day at 10g then a day at 8g -> 0.8; steady -> 1; too few hours -> 1 (never blocks)
    assert trend([80000] * 6, [100000] * 6) == 0.8 and trend([100] * 24, [100] * 24) == 1.0 and trend([1] * 5, [9] * 24) == 1
    fall2 = [{"t": i * h, "prev_t": (i - 1) * h if i else None, "sold": [[2, 1, 100000 if i < 24 else 80000]] if i else [],
              "book": [[2, 100000 if i < 24 else 80000, 1, 1, 5]]} for i in range(48)]
    assert build(fall2, 47 * h)["items"][2][6] == 0.8, build(fall2, 47 * h)["items"][2]
    # reposts: the real steak pattern (2 listed, 2 "sold", 2 listed again at once or 4h later) is not a sale;
    # a busy market (5 listed, 2 sold, 5 again) and a lone stack that stays gone keep their sales
    def rp(books, sold_at):
        return [{"t": i * h, "prev_t": (i - 1) * h if i else None, "sold": [[3, 2, 5000]] if i in sold_at else [],
                 "book": [[3, 5000, b, b, b]] if b else []} for i, b in enumerate(books)]
    left = lambda recs: sum(len(r["sold"]) for r in drop_reposts(recs))
    assert left(rp([2, 2, 0, 0, 0, 0, 0, 0, 0], [1])) == 0, "same hour, back at once"
    assert left(rp([2, 0, 0, 0, 0, 2], [1])) == 0, "back 4 hours later"
    assert left(rp([2, 0, 0, 0, 0, 0, 0, 0, 2], [1])) == 1, "back after REPOST_HOURS: a new seller, counted"
    assert left(rp([5, 2, 2], [1])) == 1, "busy market (5 listed before): kept"
    assert left(rp([2, 0, 0], [1])) == 1, "gone and stays gone: sold"
    assert left(rp([4, 2, 3], [1])) == 1, "only part came back: counted"
    assert left(rp([2, 3, 3], [1])) == 0, "all came back and more: reposted"
    # the Foot Pedal gap: 11 of 12 gone in one hour (two entries), 12 back the next hour
    ped = rp([12, 1, 12], [])
    ped[1]["sold"] = [[3, 10, 18099], [3, 1, 18099]]
    assert left(ped) == 0, "most of a busy listing gone and back: reposted"
    ped[1]["sold"] = [[3, 5, 18099]]; ped[1]["book"] = [[3, 18099, 7, 7, 7]]
    assert left(ped) == 1, "5 of 12 gone: under half, real sales kept"
    ped[1]["sold"] = [[3, 10, 18099], [3, 1, 18500]]; ped[1]["book"] = [[3, 18099, 1, 1, 1]]
    assert left(ped) == 2, "bought at two price levels: buyers sweeping, real sales kept"
    # his own take-backs are not sales: one matching sale removed per take-back, in the record that covers it
    tbs = own_takebacks('x\n["tblog"] = {\n"150|7|20|1419", -- [1]\n"150|9:55|1|500", -- [2]\n},\n')
    assert tbs == [(150, 7, 20, 1419), (150, "9:55", 1, 500)], tbs
    rr = [{"t": 100, "prev_t": None, "sold": [[7, 20, 1419]]},
          {"t": 200, "prev_t": 100, "sold": [[8, 20, 1419], [7, 15, 1419], [7, 20, 1420], [7, 20, 1419], [7, 20, 1419], ["9:55", 1, 501]]}]
    out = drop_own(rr, tbs)
    assert out[0]["sold"] == [[7, 20, 1419]] and out[1]["sold"] == [[8, 20, 1419], [7, 15, 1419], [7, 20, 1419], [7, 20, 1419]], out   # other units/item kept; one removed per take-back
    assert len(rr[1]["sold"]) == 6, "the records passed in are not changed"
    assert drop_own(rr, [(150, 7, 20, 1430)])[1]["sold"] == rr[1]["sold"], "another price: a real sale, kept"

def write():
    """Build from the records already on disk (the caller pulls them) and write the add-on file."""
    selftest()
    recs = []
    for f in sorted(REPO.glob("data/*/*.json.gz")):
        with gzip.open(f, "rt") as fh: recs.append(json.load(fh))
    recs = drop_reposts(recs)
    if SAVED and SAVED.exists(): recs = drop_own(recs, own_takebacks(SAVED.read_text(encoding="utf-8", errors="replace")))
    data = build(recs, time.time())
    data["market"] = market(recs, time.time())
    data["suffix"] = suffixes(recs, time.time())
    data["tsm"] = tsm({base(i) for i in data["items"]})
    data["tsm_t"] = tsm_date(TSM_CACHE.read_text(encoding="utf-8")) if data["tsm"] is not None else 0
    import pickle
    try: cache = pickle.loads(REPLAY_CACHE.read_bytes())
    except Exception: cache = {}   # none yet, or unreadable: a full run (about 5 minutes) rebuilds it
    tested, spent, back = replay(recs, cache)
    for key in list(cache)[:max(0, len(cache) - CACHE_KEEP)]: del cache[key]   # oldest unused first
    REPLAY_CACHE.write_bytes(pickle.dumps(cache))
    data["ok"] = not locked(tested, spent, back)
    data["replay"] = (f"{tested} test buys with 3+ days to sell: {spent / 1e4:.0f}g spent, {back / 1e4:.0f}g back so far "
                      f"({(back - spent) / 1e4:+.0f}g; needs {MIN_TESTED}+ buys and the gold back)")
    # written whole, then swapped in: it runs every hour on its own (2026-11-12), and a /reload in the middle of
    # a write must never load half a file
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(lua(data), encoding="utf-8")
    tmp.replace(OUT)
    t = "no TSM file" if data["tsm"] is None else f"TSM rates for {len(data['tsm'])}"
    return f"{len(data['items'])} items, {data['hours']} hours of records, {t}, {data['replay']}, resale {'ON' if data['ok'] else 'OFF'} -> {OUT}"

if __name__ == "__main__":
    subprocess.run(["git", "-C", str(REPO), "pull", "-q"], check=False)
    print(write())
