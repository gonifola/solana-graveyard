"""
Solana Graveyard — Historical Pattern Archiver
================================================
Scans historical Solana pools via GeckoTerminal (free, no API key) and
classifies each token's chart pattern to build the graveyard archive.

This is the "scan all meme coins ever created" engine.

Data sources:
  • GeckoTerminal /networks/solana/pools — paginated pool list
  • GeckoTerminal /networks/solana/new_pools — recently created pools
  • GeckoTerminal OHLCV endpoint — hourly candles per pool

Output: workspace/data/pattern_archive.json
  {
    "last_updated": "...",
    "total_scanned": 1234,
    "pattern_counts": { "STRAIGHT_RUG": 450, ... },
    "tokens": [
      {
        "pair_address": "...",
        "token_name": "...",
        "token_symbol": "...",
        "pattern": "STRAIGHT_RUG",
        "confidence": 0.88,
        "rug_probability": 0.96,
        "runner_probability": 0.02,
        "peak_retention_pct": 4.2,
        "time_to_peak_mins": 45,
        "scanned_at": "...",
        "dexscreener_url": "..."
      },
      ...
    ]
  }

Usage:
  python graveyard_archiver.py                  # standard run (200 pools)
  python graveyard_archiver.py --pages 10       # scan ~200 more pools (20/page)
  python graveyard_archiver.py --new-only       # only scan new/recent pools
  python graveyard_archiver.py --reset          # clear archive and restart
"""
import json
import time
import urllib.request
import sys
import os
import argparse
from datetime import datetime, timezone
from dataclasses import asdict

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_PATH = os.path.join(SCRIPT_DIR, '..', 'data', 'pattern_archive.json')
sys.path.insert(0, SCRIPT_DIR)

from chart_pattern_classifier import classify_pair

GT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json;version=20230302",
}
GT_BASE = "https://api.geckoterminal.com/api/v2"

# ── GeckoTerminal Pool Fetchers ────────────────────────────────────────────────

def fetch_pools_page(page: int = 1) -> list:
    """Get a page of Solana pools sorted by volume."""
    url = f"{GT_BASE}/networks/solana/pools?page={page}&include=base_token"
    try:
        req = urllib.request.Request(url, headers=GT_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        return data.get("data") or []
    except Exception as e:
        print(f"  Pool page {page} error: {e}")
        return []


def fetch_new_pools(page: int = 1) -> list:
    """Get recently created Solana pools."""
    url = f"{GT_BASE}/networks/solana/new_pools?page={page}&include=base_token"
    try:
        req = urllib.request.Request(url, headers=GT_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        return data.get("data") or []
    except Exception as e:
        print(f"  New pools page {page} error: {e}")
        return []


def fetch_trending_pools() -> list:
    """Get trending Solana pools."""
    url = f"{GT_BASE}/networks/solana/trending_pools?page=1"
    try:
        req = urllib.request.Request(url, headers=GT_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        return data.get("data") or []
    except Exception as e:
        print(f"  Trending pools error: {e}")
        return []


def pool_to_meta(pool_data: dict) -> dict:
    """Extract useful metadata from a GeckoTerminal pool entry."""
    attrs = pool_data.get("attributes") or {}
    rels  = pool_data.get("relationships") or {}
    base_token = (rels.get("base_token") or {}).get("data") or {}

    # Name: "TOKEN / SOL" style
    name   = attrs.get("name") or "Unknown"
    parts  = name.split(" / ")
    symbol = parts[0] if parts else name

    return {
        "pair_address": attrs.get("address") or "",
        "token_name":   name,
        "token_symbol": symbol,
        "created_at":   attrs.get("pool_created_at") or "",
        "dex_id":       (pool_data.get("relationships") or {}).get("dex", {}).get("data", {}).get("id") or "",
    }

# ── Archive I/O ────────────────────────────────────────────────────────────────

def load_archive() -> dict:
    try:
        with open(ARCHIVE_PATH) as f:
            return json.load(f)
    except Exception:
        return {
            "last_updated": "",
            "total_scanned": 0,
            "pattern_counts": {},
            "tokens": []
        }


def save_archive(archive: dict):
    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    with open(ARCHIVE_PATH, 'w') as f:
        json.dump(archive, f, indent=2)


def rebuild_pattern_counts(tokens: list) -> dict:
    counts = {}
    for t in tokens:
        p = t.get("pattern", "UNKNOWN")
        counts[p] = counts.get(p, 0) + 1
    return counts

# ── Main Scan ──────────────────────────────────────────────────────────────────

def run_archive(pages: int = 10, new_only: bool = False, reset: bool = False):
    archive = {} if reset else load_archive()
    if not archive:
        archive = {"last_updated": "", "total_scanned": 0, "pattern_counts": {}, "tokens": []}

    # Build set of already-seen pair addresses
    seen_pairs = {t["pair_address"] for t in archive.get("tokens", [])}
    print(f"Archive: {len(archive.get('tokens', []))} tokens already classified")

    # ── Collect pools to scan ──────────────────────────────────────────────
    all_pools_meta = []

    if not new_only:
        print(f"Fetching {pages} pages of Solana pools from GeckoTerminal...")
        for page in range(1, pages + 1):
            pools = fetch_pools_page(page)
            if not pools:
                print(f"  Page {page}: empty, stopping")
                break
            for p in pools:
                meta = pool_to_meta(p)
                if meta["pair_address"] and meta["pair_address"] not in seen_pairs:
                    all_pools_meta.append(meta)
            print(f"  Page {page}: {len(pools)} pools ({len(all_pools_meta)} new so far)")
            time.sleep(0.3)

    # Always include new + trending
    print("Fetching new + trending pools...")
    for p in fetch_new_pools(1) + fetch_new_pools(2) + fetch_trending_pools():
        meta = pool_to_meta(p)
        if meta["pair_address"] and meta["pair_address"] not in seen_pairs:
            all_pools_meta.append(meta)

    # Deduplicate
    seen_in_batch = set()
    unique_meta   = []
    for m in all_pools_meta:
        if m["pair_address"] not in seen_in_batch:
            seen_in_batch.add(m["pair_address"])
            unique_meta.append(m)

    print(f"\nTotal new pools to classify: {len(unique_meta)}")
    if not unique_meta:
        print("Nothing new to scan.")
        return archive

    # ── Classify each pool ─────────────────────────────────────────────────
    new_tokens = []
    pattern_counts_new = {}

    for i, meta in enumerate(unique_meta, 1):
        addr = meta["pair_address"]
        sym  = meta["token_symbol"][:12]

        try:
            cp = classify_pair(addr)
            record = {
                **meta,
                "pattern":            cp.pattern,
                "confidence":         round(cp.confidence, 3),
                "rug_probability":    round(cp.rug_probability, 3),
                "runner_probability": round(cp.runner_probability, 3),
                "peak_retention_pct": round(cp.peak_retention_pct, 1),
                "time_to_peak_mins":  cp.time_to_peak_mins,
                "candles_analyzed":   cp.candles_analyzed,
                "is_alert_target":    cp.is_alert_target,
                "is_rug":             cp.is_rug,
                "emoji":              cp.emoji,
                "description":        cp.description,
                "dexscreener_url":    f"https://dexscreener.com/solana/{addr}",
                "scanned_at":         datetime.now(timezone.utc).isoformat(),
            }
            new_tokens.append(record)
            pattern_counts_new[cp.pattern] = pattern_counts_new.get(cp.pattern, 0) + 1
            seen_pairs.add(addr)

            print(f"  [{i:3}/{len(unique_meta)}] {sym:<12} {cp.emoji} {cp.pattern:<16} "
                  f"retain={cp.peak_retention_pct:5.1f}% conf={cp.confidence:.2f}")
        except Exception as e:
            print(f"  [{i:3}/{len(unique_meta)}] {sym:<12} ERROR: {e}")

        time.sleep(0.28)  # GeckoTerminal: ~3 req/sec safe

    # ── Update archive ──────────────────────────────────────────────────────
    archive["tokens"] = archive.get("tokens", []) + new_tokens
    # Keep most recent 5000 tokens (avoid unbounded growth)
    archive["tokens"] = archive["tokens"][-5000:]
    archive["total_scanned"] = len(archive["tokens"])
    archive["pattern_counts"] = rebuild_pattern_counts(archive["tokens"])
    archive["last_updated"] = datetime.now(timezone.utc).isoformat()

    save_archive(archive)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"Archive updated: {archive['total_scanned']} total tokens")
    print(f"\nPattern distribution:")
    for pattern, count in sorted(archive["pattern_counts"].items(), key=lambda x: -x[1]):
        pct = count / archive["total_scanned"] * 100
        bar = "█" * int(pct / 2)
        print(f"  {pattern:<16} {count:4d}  ({pct:4.1f}%)  {bar}")

    runners = [t for t in new_tokens if t.get("is_alert_target")]
    if runners:
        print(f"\n🎯 Alert targets found this run:")
        for t in runners:
            print(f"  {t['emoji']} {t['token_symbol']} — {t['pattern']} (runner_prob={t['runner_probability']:.0%})")
            print(f"     {t['dexscreener_url']}")

    return archive


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Solana Graveyard Pattern Archiver")
    parser.add_argument("--pages",    type=int, default=10,     help="GT pool pages to scan (20 pools/page)")
    parser.add_argument("--new-only", action="store_true",      help="Only scan new/trending pools")
    parser.add_argument("--reset",    action="store_true",      help="Clear archive and restart")
    args = parser.parse_args()

    run_archive(pages=args.pages, new_only=args.new_only, reset=args.reset)
