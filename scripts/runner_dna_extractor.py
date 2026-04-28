"""
Solana Graveyard — Runner DNA Extractor
========================================
Finds ALL confirmed historical Solana runners via Pump.fun (sorted by
market cap) — every token that actually grew gets indexed. Extracts the
"early-stage DNA" of each runner: what their chart looked like in the
first 30–50% of candles BEFORE the peak.

This builds data/runner_library.json — the reference library used by
the live scanner to ask: "does this new token's early chart look like
the beginning of a past runner?"

Architecture:
  Pump.fun API (sorted by MC) → GeckoTerminal OHLCV → extract DNA
  → store in data/runner_library.json

Scale:
  - 200 pages × 50 tokens = 10K tokens checked
  - 1000 pages × 50 tokens = 50K tokens checked  (run with --pages 1000)
  - For full millions: Birdeye API key needed (deferred)
  - Pump.fun sorted by MC means you catch ALL significant runners first
    — even 10K pages covers every token that ever hit $300K+

DNA Features (per runner, extracted from pre-peak early window):
  - vol_profile:   normalized volume distribution (8 bins)
  - price_profile: normalized price trajectory (8 bins)
  - vol_front_ratio: was volume front-loaded? (low = organic)
  - price_cv_early: price volatility in early window (low = accumulation)
  - vol_accel: was volume accelerating going into the peak?
  - floor_quality: did price hold its base? (high = strong hands)
  - time_to_peak_mins, peak_multiplier, candles_before_peak

Usage:
  python runner_dna_extractor.py                 # 200 pages (~10K tokens)
  python runner_dna_extractor.py --pages 1000    # deep scan (~50K tokens)
  python runner_dna_extractor.py --min-mc 200000 # lower threshold
  python runner_dna_extractor.py --reset         # clear library + restart
"""
import json, time, math, sys, os, argparse
import urllib.request
from datetime import datetime, timezone

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
LIBRARY_PATH = os.path.join(SCRIPT_DIR, '..', 'data', 'runner_library.json')
sys.path.insert(0, SCRIPT_DIR)

from chart_pattern_classifier import fetch_candles, analyze_candles, RUG_PATTERNS

PUMPFUN_BASE = "https://frontend-api-v3.pump.fun"
DS_BASE      = "https://api.dexscreener.com/latest/dex"

DNA_BINS = 8  # normalize all profiles to this many time bins


# ── Pump.fun API ──────────────────────────────────────────────────────────────

def fetch_pumpfun_page(offset: int, limit: int = 50) -> list:
    """Fetch pump.fun coins sorted by market_cap DESC."""
    url = (f"{PUMPFUN_BASE}/coins?sort=market_cap&order=DESC"
           f"&offset={offset}&limit={limit}&includeNsfw=true")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  Pump.fun offset={offset} error: {e}")
        return []


# ── DexScreener fallback ──────────────────────────────────────────────────────

def get_pair_from_token(token_address: str) -> str | None:
    """Fallback: look up pair address via DexScreener if raydium_pool not set."""
    url = f"{DS_BASE}/tokens/{token_address}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        pairs = data.get("pairs") or []
        for p in pairs:
            if p.get("chainId") == "solana" and "raydium" in p.get("dexId", "").lower():
                return p.get("pairAddress")
        for p in pairs:
            if p.get("chainId") == "solana":
                return p.get("pairAddress")
    except Exception:
        pass
    return None


# ── DNA Extraction ────────────────────────────────────────────────────────────

def _resample(values: list, n: int) -> list:
    """Resample a list to exactly n bins via linear interpolation."""
    if not values:
        return [0.0] * n
    if len(values) == n:
        return [float(v) for v in values]
    result = []
    for i in range(n):
        idx  = i * (len(values) - 1) / max(n - 1, 1)
        lo   = int(idx)
        hi   = min(lo + 1, len(values) - 1)
        frac = idx - lo
        result.append(values[lo] * (1 - frac) + values[hi] * frac)
    return result


def _normalize_minmax(values: list) -> list:
    mn, mx = min(values), max(values)
    return [(v - mn) / (mx - mn) if mx != mn else 0.5 for v in values]


def _normalize_vol(values: list) -> list:
    """Volume distribution: normalize so sum = 1."""
    total = sum(values) or 1
    return [v / total for v in values]


def extract_dna(candles: list, resolution_mins: int) -> dict | None:
    """
    Extract early-stage DNA from a token's full OHLCV.
    'Early stage' = first ~40% of candles before the all-time peak.
    Returns a dict of DNA features, or None if insufficient data.
    """
    if not candles or len(candles) < 8:
        return None

    closes  = [float(c[4]) for c in candles]
    highs   = [float(c[2]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    n       = len(candles)

    peak_idx   = highs.index(max(highs))
    peak_price = highs[peak_idx]

    # Need at least 4 candles before peak
    if peak_idx < 4:
        return None

    # Early window: first 40% of pre-peak candles, capped at 20, min 4
    early_end     = max(4, min(20, int(peak_idx * 0.40)))
    early_closes  = closes[:early_end]
    early_volumes = volumes[:early_end]

    # Base price (first close)
    base_price = early_closes[0] or 1.0
    norm_prices = [c / base_price for c in early_closes]

    total_vol       = sum(volumes) or 1
    early_vol_total = sum(early_volumes) or 1

    # vol_front_ratio: low = volume came LATER (organic). High = dev dump.
    vol_front_ratio = early_vol_total / total_vol

    # price CV in early window: low = sideways consolidation
    mean_ep = sum(early_closes) / len(early_closes)
    var_ep  = sum((c - mean_ep) ** 2 for c in early_closes) / len(early_closes)
    price_cv_early = (var_ep ** 0.5) / (mean_ep or 1)

    # Volume acceleration in early window
    half          = max(len(early_volumes) // 2, 1)
    vol_first_h   = sum(early_volumes[:half]) / half
    vol_second_h  = sum(early_volumes[half:]) / max(len(early_volumes) - half, 1)
    vol_accel     = (vol_second_h - vol_first_h) / (vol_first_h or 1)

    # Floor quality: did price hold above its start in the early window?
    price_low     = min(early_closes)
    floor_quality = price_low / (early_closes[0] or 1)

    # Multiplier: launch price → peak
    start_price     = closes[0] or 1
    peak_multiplier = peak_price / start_price

    # Current retention
    current_price = closes[-1]
    retention     = current_price / (peak_price or 1)

    # Normalized profiles resampled to DNA_BINS
    vol_profile   = _resample(_normalize_vol(early_volumes), DNA_BINS)
    price_profile = _resample(_normalize_minmax(norm_prices), DNA_BINS)

    return {
        "vol_profile":       [round(x, 5) for x in vol_profile],
        "price_profile":     [round(x, 5) for x in price_profile],
        "vol_front_ratio":   round(vol_front_ratio, 4),
        "price_cv_early":    round(price_cv_early, 4),
        "vol_accel":         round(vol_accel, 4),
        "floor_quality":     round(floor_quality, 4),
        "time_to_peak_mins": peak_idx * resolution_mins,
        "peak_multiplier":   round(peak_multiplier, 2),
        "retention":         round(retention, 4),
        "candles_before_peak": peak_idx,
        "resolution_mins":   resolution_mins,
    }


# ── Similarity Scoring (used by scanner at runtime) ───────────────────────────

def cosine_sim(a: list, b: list) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x ** 2 for x in a)) or 1e-9
    mag_b = math.sqrt(sum(x ** 2 for x in b)) or 1e-9
    return dot / (mag_a * mag_b)


def score_dna_vs_library(current_dna: dict, runners: list, top_k: int = 3) -> dict:
    """
    Compare a new token's early DNA against the confirmed runner library.
    Returns {"score": 0-100, "matches": [top_k most similar runners]}.
    Used by the live scanner to add "DNA Match" to Discord alerts.
    """
    if not runners or not current_dna:
        return {"score": 0, "matches": []}

    confirmed = [r for r in runners if r.get("is_confirmed_runner") and r.get("dna")]
    if not confirmed:
        return {"score": 0, "matches": []}

    results = []
    for entry in confirmed:
        ref = entry["dna"]
        vol_sim   = cosine_sim(current_dna["vol_profile"],   ref["vol_profile"])
        price_sim = cosine_sim(current_dna["price_profile"], ref["price_profile"])
        feat_sim  = 1.0 - min(1.0, abs(current_dna["vol_front_ratio"] - ref["vol_front_ratio"]) * 2.5)
        con_sim   = 1.0 - min(1.0, abs(current_dna["price_cv_early"]  - ref["price_cv_early"])  * 6.0)
        combined  = vol_sim * 0.35 + price_sim * 0.30 + feat_sim * 0.20 + con_sim * 0.15
        results.append({
            "symbol":     entry.get("token_symbol", "?"),
            "name":       entry.get("token_name",   "?"),
            "pair":       entry.get("pair_address", ""),
            "peak_mc":    entry.get("usd_market_cap", 0),
            "multiplier": ref.get("peak_multiplier", 0),
            "similarity": round(combined * 100, 1),
        })

    results.sort(key=lambda x: -x["similarity"])
    top_matches = results[:top_k]
    avg_top = sum(m["similarity"] for m in top_matches) / len(top_matches) if top_matches else 0

    return {"score": round(avg_top, 1), "matches": top_matches}


# ── Library I/O ───────────────────────────────────────────────────────────────

def load_library() -> dict:
    try:
        with open(LIBRARY_PATH) as f:
            return json.load(f)
    except Exception:
        return {"last_updated": "", "total_indexed": 0, "confirmed_runners": 0, "runners": []}


def save_library(lib: dict):
    os.makedirs(os.path.dirname(LIBRARY_PATH), exist_ok=True)
    with open(LIBRARY_PATH, "w") as f:
        json.dump(lib, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_extractor(pages: int = 200, min_mc: float = 300_000, reset: bool = False):
    lib = {} if reset else load_library()
    if not lib:
        lib = {"last_updated": "", "total_indexed": 0, "confirmed_runners": 0, "runners": []}

    seen_mints = {r["mint"] for r in lib.get("runners", [])}
    print(f"Runner library: {len(lib.get('runners', []))} tokens already indexed")
    print(f"Scanning {pages} pages of Pump.fun (sorted by MC, threshold=${min_mc/1e6:.2f}M)...\n")

    new_entries  = []
    new_runners  = 0
    total_checked = 0

    for page in range(pages):
        offset = page * 50
        coins  = fetch_pumpfun_page(offset)

        if not coins:
            print(f"\n  Page {page + 1}: empty response — stopping early")
            break

        # If the top coin on this page is below threshold, we're done
        # (list is sorted by MC descending, so everything after is smaller)
        top_mc = float(coins[0].get("usd_market_cap") or 0) if coins else 0
        if top_mc < min_mc:
            print(f"\n  Page {page + 1}: top MC ${top_mc/1e3:.0f}K < threshold — stopping")
            break

        for coin in coins:
            mc   = float(coin.get("usd_market_cap") or 0)
            mint = coin.get("mint") or ""

            if mc < min_mc or not mint or mint in seen_mints:
                total_checked += 1
                continue

            symbol = (coin.get("symbol") or "UNKNOWN")[:12]
            name   = coin.get("name") or symbol

            # Pair address: pump.fun gives raydium_pool directly if graduated
            pair_addr = coin.get("raydium_pool") or None
            if not pair_addr:
                # Fallback: DexScreener lookup (costs a request)
                pair_addr = get_pair_from_token(mint)
                time.sleep(0.15)

            if not pair_addr:
                total_checked += 1
                continue

            # Fetch OHLCV + extract DNA
            try:
                candles, res_mins = fetch_candles(pair_addr)
                if len(candles) < 8:
                    total_checked += 1
                    continue

                cp  = analyze_candles(candles, res_mins)
                dna = extract_dna(candles, res_mins)

                if not dna:
                    total_checked += 1
                    continue

                # Confirmed runner: ≥3x from launch, not a straight rug
                is_confirmed = (
                    dna["peak_multiplier"] >= 3.0 and
                    cp.pattern not in {"STRAIGHT_RUG", "ABANDONED"}
                )
                if is_confirmed:
                    new_runners += 1

                record = {
                    "mint":                mint,
                    "pair_address":        pair_addr,
                    "token_symbol":        symbol,
                    "token_name":          name,
                    "usd_market_cap":      mc,
                    "pattern":             cp.pattern,
                    "emoji":               cp.emoji,
                    "is_confirmed_runner": is_confirmed,
                    "dna":                 dna,
                    "dexscreener_url":     f"https://dexscreener.com/solana/{pair_addr}",
                    "indexed_at":          datetime.now(timezone.utc).isoformat(),
                }
                new_entries.append(record)
                seen_mints.add(mint)

                runner_tag = "🏆 RUNNER" if is_confirmed else "   skip"
                print(f"  {runner_tag}  {symbol:<12}  MC=${mc/1e6:.2f}M  "
                      f"{cp.emoji} {cp.pattern:<16}  {dna['peak_multiplier']:.1f}x")

            except Exception as e:
                print(f"  ✗ {symbol:<12}  error: {e}")

            total_checked += 1
            time.sleep(0.28)

        page_runners = sum(1 for e in new_entries[-len(coins):] if e.get("is_confirmed_runner"))
        print(f"  Page {page + 1}/{pages}: {len(coins)} tokens | "
              f"{page_runners} runners | {len(new_entries)} total new | "
              f"{len(lib.get('runners', []))} in library")
        time.sleep(0.3)

    # Update library
    lib["runners"]          = lib.get("runners", []) + new_entries
    lib["total_indexed"]    = len(lib["runners"])
    lib["confirmed_runners"] = sum(1 for r in lib["runners"] if r.get("is_confirmed_runner"))
    lib["last_updated"]     = datetime.now(timezone.utc).isoformat()
    save_library(lib)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Library saved: {lib['total_indexed']} total indexed | "
          f"{lib['confirmed_runners']} confirmed runners")
    print(f"This run: {len(new_entries)} new entries | {new_runners} new confirmed runners")

    top = sorted(
        [r for r in new_entries if r.get("is_confirmed_runner")],
        key=lambda x: -(x.get("usd_market_cap") or 0)
    )[:10]
    if top:
        print(f"\n🏆 Top runners indexed this run:")
        for r in top:
            dna = r.get("dna", {})
            print(f"  {r['emoji']} {r['token_symbol']:<12}  MC=${r['usd_market_cap']/1e6:.2f}M  "
                  f"peak={dna.get('peak_multiplier','?')}x  "
                  f"ttp={dna.get('time_to_peak_mins','?')}min  "
                  f"floor={dna.get('floor_quality','?')}")

    return lib


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Runner DNA Extractor — build historical runner library")
    parser.add_argument("--pages",  type=int,   default=200,     help="Pump.fun pages to scan (50 tokens/page)")
    parser.add_argument("--min-mc", type=float, default=300_000, help="Min USD market cap to consider ($300K default)")
    parser.add_argument("--reset",  action="store_true",         help="Clear library and restart from scratch")
    args = parser.parse_args()

    run_extractor(pages=args.pages, min_mc=args.min_mc, reset=args.reset)
