"""
Scanner dry-run wrapper — runs the full scan + chart + DNA pipeline,
captures alert payloads, and outputs them as JSON to stdout.
Used when Discord delivery path (webhook/bot) is not configured.
"""
import json, sys, os, time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
WORKSPACE  = os.path.join(SCRIPT_DIR, '..')

CONFIG_PATH  = os.path.join(WORKSPACE, 'config', 'scanner_config.json')
SEEN_PATH    = os.path.join(WORKSPACE, 'data', 'seen_alerts.json')
LOG_PATH     = os.path.join(WORKSPACE, 'data', 'scanner_log.json')
LIBRARY_PATH = os.path.join(WORKSPACE, 'data', 'runner_library.json')

from graveyard_scanner import (
    load_config, load_seen, save_seen, fetch_all_pairs,
    classify_metrics, setup_score, check_smart_money,
    build_embed, log_run, get_runner_library
)
from chart_pattern_classifier import classify_pair, RUG_PATTERNS
from runner_dna_extractor import extract_dna, score_dna_vs_library

def run_scan():
    config          = load_config()
    min_score       = config.get('min_score', 50)
    min_fdv         = config.get('min_fdv', 30_000)
    max_fdv         = config.get('max_fdv', 600_000)
    smart_money_cfg = config.get('smart_money_wallets', {})

    print(f"[{datetime.now().strftime('%H:%M')}] Scanning {24} categories...", flush=True)
    pairs = fetch_all_pairs()
    print(f"[{datetime.now().strftime('%H:%M')}] Fetched {len(pairs)} pairs — running metrics filter...", flush=True)

    seen       = load_seen()
    candidates = []

    for pair in pairs:
        is_runner, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h = classify_metrics(pair, min_fdv, max_fdv)
        if not is_runner:
            continue
        score = setup_score(buy_r, vol24, liq, fdv_liq, pc24)
        if score < min_score:
            continue
        addr = pair.get('pairAddress', '')
        if addr and addr not in seen:
            candidates.append((score, pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h))

    candidates.sort(key=lambda x: -x[0])

    if not candidates:
        print(f"[{datetime.now().strftime('%H:%M')}] No pre-runners above score {min_score}.")
        log_run(0, 0, len(pairs))
        print(json.dumps({"alerts": [], "pairs_scanned": len(pairs), "candidates": 0}))
        return

    print(f"[{datetime.now().strftime('%H:%M')}] {len(candidates)} candidates — chart pattern + DNA check...", flush=True)

    chart_checked = []
    rug_filtered  = 0

    for score, pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h in candidates[:15]:
        addr    = pair.get('pairAddress', '')
        chart_pattern = None
        if age_h >= 2.0:
            try:
                chart_pattern = classify_pair(addr)
                time.sleep(0.25)
            except Exception as e:
                pass

        if chart_pattern and chart_pattern.pattern in RUG_PATTERNS and chart_pattern.confidence > 0.65:
            rug_filtered += 1
            continue

        boosted_score = score
        if chart_pattern and chart_pattern.is_alert_target:
            boost = int(chart_pattern.runner_probability * 15)
            boosted_score = min(100, score + boost)

        # DNA match
        dna_result = None
        try:
            library = get_runner_library()
            if library:
                from chart_pattern_classifier import fetch_candles, analyze_candles
                candles = fetch_candles(addr, resolution="15m", limit=80)
                if candles:
                    dna = extract_dna(candles)
                    if dna:
                        dna_result = score_dna_vs_library(dna, library)
                        if dna_result and dna_result.get("score", 0) >= 55:
                            dna_boost = int((dna_result["score"] - 55) / 10)
                            boosted_score = min(100, boosted_score + dna_boost)
        except Exception:
            pass

        chart_checked.append((boosted_score, pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h, chart_pattern, dna_result))

    chart_checked.sort(key=lambda x: -x[0])
    new_alerts = chart_checked[:5]

    if not new_alerts:
        print(f"[{datetime.now().strftime('%H:%M')}] All candidates filtered by rug pattern.")
        log_run(len(candidates), 0, len(pairs), rug_filtered)
        print(json.dumps({"alerts": [], "pairs_scanned": len(pairs), "candidates": len(candidates), "rug_filtered": rug_filtered}))
        return

    results = []
    for score, pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h, chart_pattern, dna_result in new_alerts:
        mint       = (pair.get('baseToken') or {}).get('address', '')
        smart_hits = check_smart_money(mint, smart_money_cfg) if mint else []
        embed      = build_embed(pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h,
                                 score, chart_pattern, smart_hits, dna_result)
        sym  = (pair.get('baseToken') or {}).get('symbol', '?')
        name = (pair.get('baseToken') or {}).get('name', sym)
        addr = pair.get('pairAddress', '')
        cp   = chart_pattern.pattern if chart_pattern else 'UNKNOWN'
        dna_score = dna_result.get('score', 0) if dna_result else 0
        dna_matches = [m.get('symbol','?') for m in (dna_result.get('matches',[])[:3] if dna_result else [])]

        results.append({
            "symbol":       sym,
            "name":         name,
            "score":        score,
            "chart_pattern": cp,
            "dna_score":    dna_score,
            "dna_matches":  dna_matches,
            "vol24":        vol24,
            "liq":          liq,
            "fdv":          fdv,
            "pc24":         pc24,
            "age_h":        age_h,
            "pair_address": addr,
            "embed":        embed,
            "smart_hits":   smart_hits,
        })
        seen.add(addr)

    save_seen(seen)
    log_run(len(candidates), len(results), len(pairs), rug_filtered)
    # Output RESULT_JSON marker so caller can parse it
    print("RESULT_JSON:" + json.dumps({
        "alerts": results,
        "pairs_scanned": len(pairs),
        "candidates": len(candidates),
        "rug_filtered": rug_filtered
    }))

if __name__ == '__main__':
    run_scan()
