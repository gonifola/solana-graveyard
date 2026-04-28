#!/usr/bin/env python3
"""
Solana Graveyard - Pattern Miner
Harvests thousands of Solana tokens from DexScreener + Pump.fun,
mines statistical pattern clusters, outputs a discovered pattern library.

Run:  python3 scripts/pattern_miner.py
Out:  data/discovered_patterns.json
      data/token_dataset.json
"""

import json, time, math, os, random, sys
from collections import defaultdict
from datetime import datetime, timezone
import requests

DS_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={q}"
DS_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/{addrs}"
PF_COINS  = "https://frontend-api.pump.fun/coins?offset={offset}&limit=50&sort=created_timestamp&order=DESC"

OUT_DATASET  = "data/token_dataset.json"
OUT_PATTERNS = "data/discovered_patterns.json"

SEARCH_TERMS = [
    "sol","pump","moon","doge","pepe","cat","dog","baby","inu",
    "ai","bonk","wif","meme","gem","king","fire","rocket",
    "trump","elon","chad","frog","bear","bull","whale",
    "fun","game","win","bet","fast","turbo","power","ultra",
    "dark","ghost","soul","new","next","big","mini","hyper",
    "safe","real","alpha","sigma","based","raydium","jupiter",
    "cash","rich","launch","coin","token","defi",
    "red","blue","green","gold","black","magic","cyber","meta",
    "ape","shark","lion","tiger","dragon","bird","eagle","wolf",
    "run","rise","fly","dump","rekt","gm","wagmi","420","69",
]

K_CLUSTERS = 14
MIN_LIQ    = 200
MIN_VOL    = 50

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_search(query):
    try:
        r = requests.get(DS_SEARCH.format(q=query), timeout=12)
        r.raise_for_status()
        return [p for p in (r.json().get("pairs") or []) if (p.get("chainId") or "") == "solana"]
    except:
        return []

def fetch_pumpfun(offset):
    try:
        r = requests.get(PF_COINS.format(offset=offset), timeout=12)
        r.raise_for_status()
        return r.json() or []
    except:
        return []

def enrich_mints(mints):
    results = []
    for i in range(0, len(mints), 30):
        batch = mints[i:i+30]
        try:
            r = requests.get(DS_TOKENS.format(addrs=",".join(batch)), timeout=12)
            r.raise_for_status()
            results.extend([p for p in (r.json().get("pairs") or []) if (p.get("chainId") or "") == "solana"])
        except:
            pass
        time.sleep(0.4)
    return results

# ── Features ──────────────────────────────────────────────────────────────────

def extract(pair):
    pc  = pair.get("priceChange") or {}
    v   = pair.get("volume")      or {}
    liq = pair.get("liquidity")   or {}
    txn = pair.get("txns")        or {}

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    age_ms = pair.get("pairCreatedAt") or 0
    age_h  = max((now_ms - age_ms) / 3_600_000, 0.01) if age_ms else 0.01

    vol24   = float(v.get("h24")  or 0)
    liq_usd = float(liq.get("usd") or 0)
    fdv     = float(pair.get("fdv") or 0)
    mc      = float(pair.get("marketCap") or 0)

    def br(b):
        buys  = float((txn.get(b) or {}).get("buys")  or 0)
        sells = float((txn.get(b) or {}).get("sells") or 0)
        tot   = buys + sells
        return buys / tot if tot > 0 else 0.5

    def tc(b):
        t = txn.get(b) or {}
        return float((t.get("buys") or 0) + (t.get("sells") or 0))

    dex = (pair.get("dexId") or "").lower()
    return {
        "age_h":         age_h,
        "vol24":         vol24,
        "liq_usd":       liq_usd,
        "fdv":           fdv,
        "mc":            mc,
        "buy_ratio_5m":  br("m5"),
        "buy_ratio_1h":  br("h1"),
        "buy_ratio_6h":  br("h6"),
        "buy_ratio_24h": br("h24"),
        "fdv_liq":  min(fdv / liq_usd, 1000) if liq_usd > 0 else 999,
        "vol_liq":  min(vol24 / liq_usd, 50) if liq_usd > 0 else 0,
        "pc_5m":    max(min(float(pc.get("m5")  or 0), 1000), -100),
        "pc_1h":    max(min(float(pc.get("h1")  or 0), 1000), -100),
        "pc_6h":    max(min(float(pc.get("h6")  or 0), 2000), -100),
        "pc_24h":   max(min(float(pc.get("h24") or 0), 5000), -100),
        "txns_5m":  tc("m5"),
        "txns_1h":  tc("h1"),
        "txns_24h": tc("h24"),
        "is_pump":  1.0 if "pump" in dex else 0.0,
        "is_ray":   1.0 if "raydium" in dex else 0.0,
    }

def label(f):
    p = f["pc_24h"]
    if p >= 500: return "moonshot"
    if p >= 100: return "big_runner"
    if p >= 30:  return "runner"
    if p >= -20: return "stable"
    if p >= -60: return "dump"
    return "rug"

def to_vec(f):
    def ln(x):       return math.log1p(max(x, 0))
    def clp(x,lo,hi): return max(lo, min(hi, x))
    return [
        clp(ln(f["age_h"])   / 8,  0, 1),
        f["buy_ratio_5m"],
        f["buy_ratio_1h"],
        f["buy_ratio_24h"],
        clp(ln(f["vol24"])   / 16, 0, 1),
        clp(ln(f["liq_usd"]) / 16, 0, 1),
        clp(ln(f["fdv"])     / 20, 0, 1),
        clp(f["fdv_liq"] / 200,    0, 1),
        clp(f["vol_liq"] / 10,     0, 1),
        clp((f["pc_5m"]  + 100) / 1100, 0, 1),
        clp((f["pc_1h"]  + 100) / 1100, 0, 1),
        clp((f["pc_6h"]  + 100) / 2100, 0, 1),
        clp((f["pc_24h"] + 100) / 5100, 0, 1),
        clp(f["txns_5m"]  / 100,  0, 1),
        clp(f["txns_1h"]  / 500,  0, 1),
        clp(f["txns_24h"] / 2000, 0, 1),
        f["is_pump"],
        f["is_ray"],
    ]

# ── K-Means ───────────────────────────────────────────────────────────────────

def dist2(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b))

def kmeans(vecs, k, iters=60, restarts=3):
    best = (float("inf"), None, None)
    n = len(vecs)
    for _ in range(restarts):
        cents = random.sample(vecs, k)
        assign = [0] * n
        for it in range(iters):
            na = [min(range(k), key=lambda j: dist2(v, cents[j])) for v in vecs]
            if na == assign and it > 5:
                break
            assign = na
            cents = [[sum(col)/len(m) for col in zip(*m)] if (m := [vecs[i] for i in range(n) if assign[i]==j]) else cents[j] for j in range(k)]
        inertia = sum(dist2(vecs[i], cents[assign[i]]) for i in range(n))
        if inertia < best[0]:
            best = (inertia, assign, cents)
    return best[1], best[2]

# ── Auto-label ────────────────────────────────────────────────────────────────

def auto_label(fa, rr, rugr, msr):
    age  = fa.get("age_h", 0)
    br1  = fa.get("buy_ratio_1h", 0.5)
    br24 = fa.get("buy_ratio_24h", 0.5)
    fdvl = fa.get("fdv_liq", 50)
    voll = fa.get("vol_liq", 0.5)
    pc24 = fa.get("pc_24h", 0)
    pc1  = fa.get("pc_1h", 0)
    liq  = fa.get("liq_usd", 0)
    pump = fa.get("is_pump", 0)
    txn1 = fa.get("txns_1h", 0)

    if msr  > 0.20:                      return "Moonshot Zone"
    if rr   > 0.45 and age < 12 and br1 > 0.62: return "Early Momentum"
    if rr   > 0.38 and br1 > 0.65:      return "Buy Pressure Surge"
    if rr   > 0.30 and voll > 1.5 and age < 24: return "Volume Breakout"
    if rr   > 0.30 and pc1 > 15:        return "Breakout Attempt"
    if rugr > 0.65 and age < 6:          return "Instant Rug Zone"
    if rugr > 0.55:                      return "Dump and Abandon"
    if pc24 < -40 and voll < 0.1:        return "Ghost / Dead"
    if age  > 96 and liq > 30000 and abs(pc24) < 25: return "Stable Survivor"
    if pump > 0.6 and age < 48 and rr < 0.20:   return "Pump.fun Fade"
    if fdvl > 80 and rugr > 0.30:        return "Whale Trap"
    if br24 < 0.35 and pc24 < -20:       return "Slow Bleed"
    if txn1 < 5 and age > 24:            return "Forgotten Launch"
    if age  < 6 and br1 > 0.55:          return "High-Risk New Launch"
    return "Mixed Signals"

LABEL_EMOJI = {
    "Moonshot Zone": "🚀", "Early Momentum": "🌱", "Buy Pressure Surge": "⚡",
    "Volume Breakout": "🔥", "Breakout Attempt": "📈", "Instant Rug Zone": "💀",
    "Dump and Abandon": "🪦", "Ghost / Dead": "👻", "Stable Survivor": "🏦",
    "Pump.fun Fade": "🎯", "Whale Trap": "🐋", "Slow Bleed": "📉",
    "Forgotten Launch": "💤", "High-Risk New Launch": "🎲", "Mixed Signals": "📊",
}

ALPHA_LABELS = {"Moonshot Zone", "Early Momentum", "Buy Pressure Surge", "Volume Breakout", "Breakout Attempt"}

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)
    random.seed(42)

    print("\n" + "=" * 60)
    print("  SOLANA GRAVEYARD PATTERN MINER")
    print("=" * 60)

    # Phase 1: DexScreener harvest
    print(f"\n[1/5] DexScreener harvest ({len(SEARCH_TERMS)} queries)...")
    seen = {}
    for i, term in enumerate(SEARCH_TERMS):
        for p in fetch_search(term):
            addr = p.get("pairAddress")
            if addr and addr not in seen:
                seen[addr] = p
        sys.stdout.write(f"\r  {i+1}/{len(SEARCH_TERMS)} queries  |  {len(seen)} unique tokens   ")
        sys.stdout.flush()
        time.sleep(0.25)
    print(f"\n  Done: {len(seen)} tokens")

    # Phase 2: Pump.fun harvest
    print(f"\n[2/5] Pump.fun harvest...")
    pf_mints = []
    for page in range(10):
        coins = fetch_pumpfun(page * 50)
        if not coins:
            break
        pf_mints.extend(c.get("mint") for c in coins if c.get("mint"))
        time.sleep(0.3)
    print(f"  {len(pf_mints)} pump.fun mints, enriching...")
    for p in enrich_mints(pf_mints[:300]):
        addr = p.get("pairAddress")
        if addr and addr not in seen:
            seen[addr] = p
    print(f"  Total: {len(seen)} tokens")

    # Phase 3: Feature extraction
    print(f"\n[3/5] Extracting features...")
    records, skipped = [], 0
    for addr, pair in seen.items():
        try:
            f = extract(pair)
            if f["liq_usd"] < MIN_LIQ or f["vol24"] < MIN_VOL:
                skipped += 1; continue
            if f["age_h"] < 0.05 or f["age_h"] > 8760:
                skipped += 1; continue
            records.append({
                "pairAddress": addr,
                "name":    (pair.get("baseToken") or {}).get("name", "?"),
                "symbol":  (pair.get("baseToken") or {}).get("symbol", "?"),
                "dex":     pair.get("dexId", ""),
                "url":     pair.get("url", ""),
                "features": f, "outcome": label(f), "vec": to_vec(f),
            })
        except:
            skipped += 1
    print(f"  {len(records)} quality tokens  ({skipped} filtered)")

    if len(records) < 30:
        print("  Not enough data — DexScreener may be rate-limiting. Retry in a few minutes.")
        sys.exit(1)

    # Phase 4: K-means
    k = min(K_CLUSTERS, len(records) // 15)
    print(f"\n[4/5] K-means (k={k}, {len(records)} tokens, 3 restarts)...")
    vecs = [r["vec"] for r in records]
    assignments, centroids = kmeans(vecs, k=k, iters=60, restarts=3)
    for i, r in enumerate(records):
        r["cluster"] = assignments[i]
    print("  Done")

    # Phase 5: Pattern analysis
    print(f"\n[5/5] Building pattern library...")
    clusters = defaultdict(list)
    for r in records:
        clusters[r["cluster"]].append(r)

    patterns = []
    for cid, members in clusters.items():
        n = len(members)
        oc = defaultdict(int)
        for m in members:
            oc[m["outcome"]] += 1

        rr   = (oc["runner"] + oc["big_runner"] + oc["moonshot"]) / n
        msr  = oc["moonshot"] / n
        rugr = (oc["rug"] + oc["dump"]) / n
        alpha = round(rr - rugr * 0.6, 3)

        fkeys = list(members[0]["features"].keys())
        fa = {fk: round(sum(m["features"].get(fk,0) for m in members) / n, 3) for fk in fkeys}

        raw_label = auto_label(fa, rr, rugr, msr)
        emoji = LABEL_EMOJI.get(raw_label, "📊")
        is_alpha = raw_label in ALPHA_LABELS

        top5 = sorted(members, key=lambda x: -x["features"]["pc_24h"])[:5]

        # Human-readable defining features
        keys = []
        avg_age = fa["age_h"]
        if avg_age < 12:   keys.append(f"Young (avg {avg_age:.1f}h old)")
        elif avg_age > 72: keys.append(f"Aged (avg {avg_age:.0f}h old)")
        if fa["buy_ratio_1h"] > 0.60: keys.append(f"Strong buy pressure ({fa['buy_ratio_1h']*100:.0f}% buys/1h)")
        if fa["buy_ratio_1h"] < 0.40: keys.append(f"Sell pressure ({fa['buy_ratio_1h']*100:.0f}% buys/1h)")
        if fa["fdv_liq"] < 30:  keys.append(f"Lean FDV/Liq ({fa['fdv_liq']:.1f}x)")
        if fa["fdv_liq"] > 100: keys.append(f"Bloated FDV/Liq ({fa['fdv_liq']:.0f}x)")
        if fa["vol_liq"] > 1.5: keys.append(f"High vol/liq ratio ({fa['vol_liq']:.1f}x)")
        if fa["pc_24h"] > 50:   keys.append(f"24h momentum +{fa['pc_24h']:.0f}%")
        if fa["pc_24h"] < -40:  keys.append(f"24h down {fa['pc_24h']:.0f}%")

        patterns.append({
            "cluster_id":    cid,
            "label":         f"{emoji} {raw_label}",
            "raw_label":     raw_label,
            "is_alpha":      is_alpha,
            "sample_count":  n,
            "outcome_dist":  {o: round(oc[o]/n, 3) for o in ["moonshot","big_runner","runner","stable","dump","rug"]},
            "runner_rate":   round(rr, 3),
            "moonshot_rate": round(msr, 3),
            "rug_rate":      round(rugr, 3),
            "alpha_score":   alpha,
            "feature_avg":   fa,
            "centroid":      [round(x, 4) for x in centroids[cid]],
            "defining_features": keys,
            "top_examples":  [{"name": m["name"], "symbol": m["symbol"],
                               "outcome": m["outcome"], "url": m["url"],
                               "pc_24h": round(m["features"]["pc_24h"], 1)} for m in top5],
        })

    patterns.sort(key=lambda x: -x["alpha_score"])
    for i, p in enumerate(patterns):
        p["rank"] = i + 1

    # Save dataset
    dataset = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "token_count": len(records),
        "tokens": [{"pairAddress": r["pairAddress"], "name": r["name"], "symbol": r["symbol"],
                    "cluster": r["cluster"], "outcome": r["outcome"],
                    "features": {k: round(v,3) for k,v in r["features"].items()}} for r in records]
    }
    with open(OUT_DATASET, "w") as f:
        json.dump(dataset, f)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "token_count": len(records),
        "cluster_count": len(patterns),
        "patterns": patterns,
    }
    with open(OUT_PATTERNS, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*60}")
    print(f"  DONE — {len(records)} tokens → {len(patterns)} patterns")
    print(f"{'='*60}")
    print(f"\n{'#':<4} {'Pattern':<28} {'N':<6} {'Runners':<10} {'Rugs':<10} {'Alpha'}")
    print("-" * 62)
    for p in patterns[:12]:
        print(f"#{p['rank']:<3} {p['label']:<28} {p['sample_count']:<6} "
              f"{p['runner_rate']*100:.0f}%{'':<7} {p['rug_rate']*100:.0f}%{'':<7} {p['alpha_score']:.3f}")

    alpha_patterns = [p for p in patterns if p["is_alpha"]]
    if alpha_patterns:
        best = alpha_patterns[0]
        print(f"\n  Top alpha pattern: {best['label']}")
        print(f"  Runner rate: {best['runner_rate']*100:.0f}%  |  Rug rate: {best['rug_rate']*100:.0f}%")
        print(f"  Defining: {', '.join(best['defining_features'][:3])}")

if __name__ == "__main__":
    main()
