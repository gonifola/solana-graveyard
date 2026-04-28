"""
Solana Graveyard — Chart Pattern Classifier
============================================
Fetches real OHLCV candle data from GeckoTerminal (free, no API key) and
classifies the SHAPE of the chart to identify:

  STRAIGHT_RUG   🔴  Spike in first 30–90 min → immediate crash → never recovers
  SLOW_BLEED     🟠  Gradual decline, volume dying — no floor
  PUMP_DUMP      🔴  Multiple declining peaks, serial manipulation
  DEAD_CAT       ⚪  Initial rug → brief fake bounce → second death
  ORGANIC_RUNNER 🟢  Slow base build → organic breakout  ← ALERT TARGET
  ACCUMULATION   🟡  Sideways base + growing volume      ← ALERT TARGET (early stage)
  MOONSHOT       🚀  Sustained parabolic run, volume holding
  ABANDONED      ⚰️  Zero activity, completely dead token
  UNKNOWN        ❓  Not enough candle data (<5 valid candles)

Data source: GeckoTerminal API — https://www.geckoterminal.com/
  Endpoint: /api/v2/networks/solana/pools/{pool}/ohlcv/{timeframe}

Usage:
  python chart_pattern_classifier.py <pair_address>
  python chart_pattern_classifier.py <pair1> <pair2> ...   (batch)
"""
import json
import time
import urllib.request
import sys
from dataclasses import dataclass, asdict
from typing import List, Tuple, Optional

# ── GeckoTerminal API ─────────────────────────────────────────────────────────
GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_OHLCV = GT_BASE + "/networks/solana/pools/{pool}/ohlcv/{tf}?aggregate={agg}&limit={limit}&currency=usd"

GT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json;version=20230302",
}

# Try hourly candles first (100 candles = ~4 days), then 15-min, then daily
TIMEFRAME_CONFIGS = [
    ("hour",   1,  100),   # 1h candles, 100 bars
    ("minute", 15,  200),  # 15m candles, 200 bars
    ("day",    1,   90),   # 1d candles, 90 bars (longer history)
]

# ── Pattern Metadata ──────────────────────────────────────────────────────────
PATTERN_META = {
    "STRAIGHT_RUG":    ("🔴", "Spiked fast then crashed — classic dev dump"),
    "SLOW_BLEED":      ("🟠", "Gradual price decay, volume death — no floor"),
    "PUMP_DUMP":       ("🔴", "Serial pump & dump — multiple declining peaks"),
    "DEAD_CAT":        ("⚪", "Rug with fake bounce — avoid second entry"),
    "ORGANIC_RUNNER":  ("🟢", "Slow base → organic breakout — high-quality setup"),
    "ACCUMULATION":    ("🟡", "Sideways base, volume building — pre-runner forming"),
    "MOONSHOT":        ("🚀", "Sustained run with held volume — strong hands"),
    "ABANDONED":       ("⚰️", "Zero activity — completely dead token"),
    "UNKNOWN":         ("❓", "Not enough candle data to classify"),
}

RUG_PATTERNS    = {"STRAIGHT_RUG", "SLOW_BLEED", "PUMP_DUMP", "DEAD_CAT", "ABANDONED"}
RUNNER_PATTERNS = {"ORGANIC_RUNNER", "ACCUMULATION"}


@dataclass
class ChartPattern:
    pattern: str
    confidence: float           # 0.0–1.0
    rug_probability: float      # 0.0–1.0
    runner_probability: float   # 0.0–1.0
    peak_retention_pct: float   # current price as % of all-time high on this chart
    time_to_peak_mins: int      # minutes from first candle to peak
    candles_analyzed: int
    resolution_mins: int        # minutes per candle
    emoji: str
    description: str
    is_alert_target: bool       # True = ORGANIC_RUNNER or ACCUMULATION
    is_rug: bool                # True = confirmed rug pattern


# ── Fetch OHLCV ───────────────────────────────────────────────────────────────

def _fetch_gt(pool: str, tf: str, agg: int, limit: int) -> list:
    """Fetch OHLCV from GeckoTerminal. Returns [[ts, o, h, l, c, v], ...] or []."""
    url = GT_OHLCV.format(pool=pool, tf=tf, agg=agg, limit=limit)
    try:
        req = urllib.request.Request(url, headers=GT_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        raw = (data.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        # GT format: [timestamp_s, open, high, low, close, volume]  (already correct)
        return [c for c in raw if len(c) >= 6 and float(c[4]) > 0]
    except Exception:
        return []


def fetch_candles(pair_address: str) -> Tuple[list, int]:
    """
    Try multiple timeframe configs on GeckoTerminal.
    Returns (candles, resolution_mins_per_candle).
    """
    for tf, agg, limit in TIMEFRAME_CONFIGS:
        candles = _fetch_gt(pair_address, tf, agg, limit)
        if len(candles) >= 8:
            res_mins = agg * ({"minute": 1, "hour": 60, "day": 1440}[tf])
            return candles, res_mins
        time.sleep(0.12)
    return [], 60


# ── Chart Analysis ────────────────────────────────────────────────────────────

def _safe_div(a, b, fallback=0.0):
    return a / b if b else fallback


def analyze_candles(candles: list, resolution_mins: int) -> ChartPattern:
    def make(pattern, conf, rug_p, run_p, retain, ttp):
        emoji, desc = PATTERN_META[pattern]
        return ChartPattern(
            pattern=pattern,
            confidence=conf,
            rug_probability=rug_p,
            runner_probability=run_p,
            peak_retention_pct=retain,
            time_to_peak_mins=ttp,
            candles_analyzed=len(candles),
            resolution_mins=resolution_mins,
            emoji=emoji,
            description=desc,
            is_alert_target=pattern in RUNNER_PATTERNS,
            is_rug=pattern in RUG_PATTERNS,
        )

    if not candles or len(candles) < 5:
        return make("UNKNOWN", 0.0, 0.5, 0.2, 0.0, 0)

    closes  = [float(c[4]) for c in candles]
    highs   = [float(c[2]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    n = len(candles)

    current_price = closes[-1]
    peak_price    = max(highs)
    peak_idx      = highs.index(peak_price)
    time_to_peak  = peak_idx * resolution_mins

    peak_retention_pct = _safe_div(current_price, peak_price, 0.0) * 100

    # ── Volume Shape ──────────────────────────────────────────────────────
    total_vol  = sum(volumes) or 1
    early_vol  = _safe_div(sum(volumes[: n // 3]), total_vol)        # first third
    mid_vol    = _safe_div(sum(volumes[n // 3: 2 * n // 3]), total_vol)
    late_vol   = _safe_div(sum(volumes[2 * n // 3:]), total_vol)     # last third
    recent_vol = _safe_div(sum(volumes[-5:]), total_vol)             # last 5 candles

    half_n = n // 2
    vol_first  = _safe_div(sum(volumes[:half_n]), half_n)
    vol_second = _safe_div(sum(volumes[half_n:]), n - half_n)
    vol_trend  = _safe_div(vol_second - vol_first, vol_first + 1e-9)  # positive = growing

    # ── Price Shape ───────────────────────────────────────────────────────
    third = max(n // 3, 1)
    first_avg = _safe_div(sum(closes[:third]), third)
    last_avg  = _safe_div(sum(closes[-third:]), third)
    price_trend = _safe_div(last_avg - first_avg, first_avg)        # positive = up

    mean_close = _safe_div(sum(closes), n)
    variance   = _safe_div(sum((c - mean_close) ** 2 for c in closes), n)
    std_dev    = variance ** 0.5
    price_cv   = _safe_div(std_dev, mean_close)                      # low = sideways

    # ── Post-Peak Drawdown ────────────────────────────────────────────────
    post_peak   = closes[peak_idx:] if peak_idx < n else closes
    min_post    = min(post_peak) if post_peak else current_price
    post_peak_dd = 1.0 - _safe_div(min_post, peak_price)            # 1.0 = total loss

    # ── Local Peak Count (pump/dump detection) ────────────────────────────
    local_peaks = sum(
        1 for i in range(1, n - 1)
        if highs[i] > highs[i - 1] * 1.05 and highs[i] > highs[i + 1] * 1.05
    )

    # ──────────────────────────────────────────────────────────────────────
    # CLASSIFICATION  (order matters — most specific first)
    # ──────────────────────────────────────────────────────────────────────

    # ABANDONED: basically dead
    if total_vol < 500 and peak_retention_pct < 3:
        return make("ABANDONED", 0.92, 0.10, 0.00, peak_retention_pct, time_to_peak)

    # STRAIGHT RUG: fast spike → immediate crash
    if (time_to_peak <= 90 and
            peak_retention_pct < 13 and
            post_peak_dd > 0.82 and
            early_vol > 0.42):
        conf = min(0.96, 0.87 + (0.08 if peak_retention_pct < 5 else 0))
        return make("STRAIGHT_RUG", conf, 0.96, 0.02, peak_retention_pct, time_to_peak)

    # DEAD CAT: rugged + fake bounce
    if (peak_retention_pct < 22 and
            post_peak_dd > 0.65 and
            local_peaks >= 2 and
            price_cv > 0.28):
        return make("DEAD_CAT", 0.76, 0.86, 0.04, peak_retention_pct, time_to_peak)

    # PUMP & DUMP: serial multiple declining peaks, front-loaded volume
    if (local_peaks >= 3 and
            peak_retention_pct < 35 and
            early_vol > late_vol * 2.0):
        return make("PUMP_DUMP", 0.80, 0.89, 0.04, peak_retention_pct, time_to_peak)

    # SLOW BLEED: gradual decline, volume dying
    if (price_trend < -0.30 and
            peak_retention_pct < 45 and
            recent_vol < 0.08 and
            time_to_peak > 60):
        return make("SLOW_BLEED", 0.74, 0.77, 0.07, peak_retention_pct, time_to_peak)

    # MOONSHOT: strong sustained parabolic run
    if (peak_retention_pct > 70 and
            price_trend > 0.50 and
            recent_vol > 0.10 and
            time_to_peak > 120):
        return make("MOONSHOT", 0.82, 0.13, 0.88, peak_retention_pct, time_to_peak)

    # ORGANIC RUNNER: slow base → sustained breakout  ← ALERT TARGET
    if (time_to_peak > 120 and
            peak_retention_pct > 40 and
            early_vol < 0.50 and   # volume NOT front-loaded (no dev dump)
            price_cv > 0.12 and    # actual price movement (not flatlined)
            recent_vol > 0.06):    # still has activity
        conf = min(0.90, 0.72 + (0.12 if peak_retention_pct > 60 else 0))
        return make("ORGANIC_RUNNER", conf, 0.22, 0.80, peak_retention_pct, time_to_peak)

    # ACCUMULATION: sideways with growing volume  ← EARLY STAGE RUNNER — ALERT TARGET
    if (price_cv < 0.28 and
            vol_trend > 0.05 and
            recent_vol > 0.10 and
            peak_retention_pct > 50 and
            time_to_peak > 30):
        return make("ACCUMULATION", 0.70, 0.28, 0.67, peak_retention_pct, time_to_peak)

    # ── Fallback: heuristic probabilities ────────────────────────────────
    rug_p = min(0.85, max(0.10, 1.0 - peak_retention_pct / 100))
    run_p = min(0.70, max(0.05, recent_vol * 2.5))
    return make("UNKNOWN", 0.38, rug_p, run_p, peak_retention_pct, time_to_peak)


# ── Public API ────────────────────────────────────────────────────────────────

def classify_pair(pair_address: str) -> ChartPattern:
    """Classify chart pattern for a single pair address."""
    candles, res = fetch_candles(pair_address)
    return analyze_candles(candles, res)


def classify_batch(pair_addresses: List[str], delay: float = 0.3) -> dict:
    """Classify multiple pairs. Returns {pair_address: ChartPattern}."""
    results = {}
    for addr in pair_addresses:
        results[addr] = classify_pair(addr)
        time.sleep(delay)
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python chart_pattern_classifier.py <pair_address> [pair2 ...]")
        sys.exit(1)

    addrs = sys.argv[1:]
    if len(addrs) == 1:
        cp = classify_pair(addrs[0])
        print(json.dumps(asdict(cp), indent=2))
    else:
        results = classify_batch(addrs)
        for addr, cp in results.items():
            print(f"\n── {addr}")
            d = asdict(cp)
            print(f"  {cp.emoji}  {cp.pattern}  (conf={cp.confidence:.2f}  rug={cp.rug_probability:.2f}  runner={cp.runner_probability:.2f})")
