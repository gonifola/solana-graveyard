"""
Solana Graveyard — Pre-Runner Alert Scanner  (v3 — Runner DNA Aware)
=====================================================================
Runs on schedule, pings Discord when tokens hit pre-runner criteria.

v2: Chart pattern recognition — filters rugs, boosts ORGANIC_RUNNER / ACCUMULATION
v3: Runner DNA similarity — compares new token's early chart against historical
    runner library (data/runner_library.json). Adds "DNA Match" to Discord alerts:
    "73% match — similar to PEPE (23x), WIF (18x)"
    Runners with high DNA similarity get score boost + 🧬 badge.

Config: workspace/config/scanner_config.json
"""
import json
import time
import sys
import os
from datetime import datetime, timezone

CONFIG_PATH  = os.path.join(os.path.dirname(__file__), '..', 'config', 'scanner_config.json')
SEEN_PATH    = os.path.join(os.path.dirname(__file__), '..', 'data', 'seen_alerts.json')
LOG_PATH     = os.path.join(os.path.dirname(__file__), '..', 'data', 'scanner_log.json')
LIBRARY_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'runner_library.json')
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from chart_pattern_classifier import classify_pair, fetch_candles, analyze_candles, RUG_PATTERNS, RUNNER_PATTERNS
from runner_dna_extractor import extract_dna, score_dna_vs_library

# ── Runner Library (lazy-loaded once per scan run) ───────────────────────────
_runner_library = None

def get_runner_library() -> list:
    global _runner_library
    if _runner_library is not None:
        return _runner_library
    try:
        with open(LIBRARY_PATH) as f:
            data = json.load(f)
        _runner_library = data.get("runners", [])
        confirmed = sum(1 for r in _runner_library if r.get("is_confirmed_runner"))
        print(f"  Loaded runner library: {len(_runner_library)} indexed | {confirmed} confirmed runners")
    except Exception:
        _runner_library = []
        print("  Runner library not found — run runner_dna_extractor.py first")
    return _runner_library

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def load_seen():
    try:
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    with open(SEEN_PATH, 'w') as f:
        json.dump(list(seen)[-500:], f)

# ── DexScreener Fetch ─────────────────────────────────────────────────────────

SEARCH_TERMS = [
    "sol", "pump", "fun", "meme", "cat", "dog", "pepe", "bonk",
    "wif", "moon", "ape", "degen", "ai", "gpt", "based", "chad",
    "coin", "token", "new", "launch", "gem", "100x", "1000x", "send",
]

DEXSCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://dexscreener.com/",
}

def fetch_pairs(query):
    import urllib.request
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}&chain=solana"
    try:
        req = urllib.request.Request(url, headers=DEXSCREENER_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            pairs = data.get('pairs') or []
            return [p for p in pairs if (p.get('chainId') or '') == 'solana']
    except Exception:
        return []

def fetch_all_pairs():
    seen_addrs = set()
    results = []
    for term in SEARCH_TERMS:
        for pair in fetch_pairs(term):
            addr = pair.get('pairAddress', '')
            if addr and addr not in seen_addrs:
                seen_addrs.add(addr)
                results.append(pair)
        time.sleep(0.15)
    return results

# ── Point-in-Time Classifier ──────────────────────────────────────────────────

def classify_metrics(pair, min_fdv=30_000, max_fdv=600_000):
    """Returns pre_runner classification + raw metrics.

    min_fdv / max_fdv gate: targets the $30K–$600K MC sweet spot where
    pre-runners are spotted before they run to millions. Tokens above
    max_fdv have typically already moved; below min_fdv are dust/noise.
    """
    txns24  = (pair.get('txns') or {}).get('h24') or {}
    buys    = txns24.get('buys', 0)
    sells   = txns24.get('sells', 0)
    total   = buys + sells
    buy_r   = buys / total if total > 0 else 0.5

    vol24   = float((pair.get('volume') or {}).get('h24') or 0)
    liq     = float((pair.get('liquidity') or {}).get('usd') or 0)
    fdv     = float(pair.get('fdv') or 0)
    pc24    = float((pair.get('priceChange') or {}).get('h24') or 0)
    created = pair.get('pairCreatedAt') or 0
    age_h   = (time.time() * 1000 - created) / 3_600_000

    fdv_liq = fdv / liq if liq > 0 else 9999

    is_pre_runner = (
        buy_r   > 0.58          and
        10 < pc24 < 80          and
        vol24   > 10_000        and
        liq     > 15_000        and
        fdv_liq < 60            and
        1 < age_h < 96          and
        min_fdv < fdv < max_fdv        # ← MC sweet spot gate
    )
    return is_pre_runner, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h

def setup_score(buy_r, vol24, liq, fdv_liq, pc24):
    """0–100 base setup score from metrics."""
    score = 0
    if buy_r   > 0.65: score += 25
    elif buy_r > 0.58: score += 15
    if vol24   > 100_000: score += 25
    elif vol24 > 30_000:  score += 15
    elif vol24 > 10_000:  score += 8
    if liq     > 50_000: score += 20
    elif liq   > 30_000: score += 12
    elif liq   > 15_000: score += 6
    if fdv_liq < 20: score += 20
    elif fdv_liq < 40: score += 12
    elif fdv_liq < 60: score += 6
    if pc24    > 50: score += 10
    elif pc24  > 20: score += 6
    elif pc24  > 10: score += 3
    return min(score, 100)

# ── Smart Money Checker ───────────────────────────────────────────────────────

SOLANA_RPC = "https://api.mainnet-beta.solana.com"

def check_smart_money(mint_address, smart_money_config):
    import urllib.request
    hits = []
    if not smart_money_config or not mint_address:
        return hits
    for label, addresses in smart_money_config.items():
        for wallet_addr in addresses:
            if not wallet_addr or 'ADD_' in wallet_addr or len(wallet_addr) < 32:
                continue
            try:
                payload = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        wallet_addr,
                        {"mint": mint_address},
                        {"encoding": "jsonParsed"}
                    ]
                }).encode('utf-8')
                import urllib.request as ur
                req = ur.Request(SOLANA_RPC, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
                with ur.urlopen(req, timeout=6) as r:
                    data = json.loads(r.read())
                accounts = (data.get('result') or {}).get('value') or []
                for acc in accounts:
                    ui_amount = (acc.get('account', {}).get('data', {})
                                 .get('parsed', {}).get('info', {})
                                 .get('tokenAmount', {}).get('uiAmount') or 0)
                    if ui_amount > 0:
                        hits.append({"label": label,
                                     "wallet_short": wallet_addr[:6] + "..." + wallet_addr[-4:],
                                     "amount": ui_amount})
            except Exception:
                pass
            time.sleep(0.1)
    return hits

# ── Discord Embed Builder ─────────────────────────────────────────────────────

def fmt_num(n):
    if n >= 1_000_000: return f"${n/1_000_000:.2f}M"
    if n >= 1_000:     return f"${n/1_000:.1f}K"
    return f"${n:.0f}"

def build_embed(pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h,
                score, chart_pattern=None, smart_hits=None, dna_result=None):
    name   = (pair.get('baseToken') or {}).get('name', 'Unknown')
    sym    = (pair.get('baseToken') or {}).get('symbol', '?')
    mint   = (pair.get('baseToken') or {}).get('address', '')
    dex    = pair.get('dexId', 'dex')
    price  = pair.get('priceUsd', '?')
    pair_url = f"https://dexscreener.com/solana/{pair.get('pairAddress','')}"
    pc6    = float((pair.get('priceChange') or {}).get('h6') or 0)
    pc1    = float((pair.get('priceChange') or {}).get('h1') or 0)

    # Color
    color = 0x69FF47 if score >= 70 else (0x00E676 if score >= 50 else 0xFFD600)
    if smart_hits:
        color = 0xFFD600

    # Chart pattern line
    cp_line = ""
    cp_badge = ""
    if chart_pattern and chart_pattern.pattern != "UNKNOWN":
        cp_line = f"\n{chart_pattern.emoji} **Chart Pattern:** `{chart_pattern.pattern}` — {chart_pattern.description}"
        cp_badge = f" · {chart_pattern.emoji} {chart_pattern.pattern}"
        if chart_pattern.is_alert_target:
            cp_line += " 🎯"
            color = 0x00FF88  # bright green for confirmed runners

    # Smart money
    smart_line = ""
    if smart_hits:
        labels = list(dict.fromkeys(h["label"] for h in smart_hits))
        smart_line = "\n🤖 **Smart Money:** " + " · ".join(f"`{l}`" for l in labels)

    # Runner DNA match
    dna_line  = ""
    dna_badge = ""
    if dna_result and dna_result.get("score", 0) >= 55:
        dna_score   = dna_result["score"]
        top_matches = dna_result.get("matches", [])[:3]
        match_strs  = []
        for m in top_matches:
            mult = m.get("multiplier", 0)
            sym  = m.get("symbol", "?")
            match_strs.append(f"{sym} ({mult:.0f}x)" if mult else sym)
        match_text = " · ".join(match_strs) if match_strs else "past runners"
        dna_line  = f"\n🧬 **DNA Match: {dna_score:.0f}%** — similar to {match_text}"
        dna_badge = f"  ·  DNA {dna_score:.0f}%"
        if dna_score >= 75:
            color = 0x00FF88  # bright green — strong DNA match

    fields = [
        {"name": "💰 Price",      "value": f"`${price}`",          "inline": True},
        {"name": "📈 24h",        "value": f"`+{pc24:.1f}%`",      "inline": True},
        {"name": "⚡ 1h / 6h",   "value": f"`{pc1:+.1f}% / {pc6:+.1f}%`", "inline": True},
        {"name": "📊 Vol 24h",    "value": f"`{fmt_num(vol24)}`",  "inline": True},
        {"name": "💧 Liquidity",  "value": f"`{fmt_num(liq)}`",    "inline": True},
        {"name": "🔢 FDV/Liq",   "value": f"`{fdv_liq:.1f}x`",    "inline": True},
        {"name": "🟢 Buy Ratio", "value": f"`{buy_r*100:.0f}%`",   "inline": True},
        {"name": "🕐 Age",       "value": f"`{age_h:.1f}h`",       "inline": True},
        {"name": "🏦 DEX",       "value": f"`{dex}`",              "inline": True},
    ]

    if chart_pattern and chart_pattern.pattern != "UNKNOWN":
        fields.append({
            "name": "🔬 Runner Probability",
            "value": f"`{chart_pattern.runner_probability*100:.0f}%`",
            "inline": True
        })

    title_prefix = '🚨' if smart_hits else ('🌱' if not (chart_pattern and chart_pattern.is_alert_target) else '🎯')
    smart_badge  = '  ·  SMART MONEY ✓' if smart_hits else ''
    runner_badge = '  ·  ORGANIC RUNNER ✓' if (chart_pattern and chart_pattern.pattern == 'ORGANIC_RUNNER') else \
                   ('  ·  ACCUMULATION ✓' if (chart_pattern and chart_pattern.pattern == 'ACCUMULATION') else '')

    return {
        "title": f"{title_prefix} PRE-RUNNER{smart_badge}{runner_badge}{dna_badge}: {name} ({sym})",
        "description": (
            f"**Setup Score: {score}/100**\n\n"
            f"Buy pressure building · Healthy liquidity · FDV reasonable"
            f"{cp_line}"
            f"{dna_line}"
            f"{smart_line}\n\n"
            f"[→ DexScreener]({pair_url})"
            + (f"  ·  [Pump.fun](https://pump.fun/{mint})" if mint else "")
        ),
        "color": color,
        "fields": fields,
        "footer": {"text": f"Solana Graveyard Scanner v2 · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
        "url": pair_url,
    }

# ── Send Discord ──────────────────────────────────────────────────────────────

def send_discord(webhook_url, embeds, content=None):
    import urllib.request
    payload = {"embeds": embeds[:10]}
    if content:
        payload["content"] = content
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status in (200, 204)
    except Exception as e:
        print(f"Discord send error: {e}")
        return False

# ── Log ───────────────────────────────────────────────────────────────────────

def log_run(found, alerted, total_scanned, filtered_by_pattern=0):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    try:
        with open(LOG_PATH) as f:
            logs = json.load(f)
    except Exception:
        logs = []
    logs.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "scanned": total_scanned,
        "pre_runners_found": found,
        "filtered_by_rug_pattern": filtered_by_pattern,
        "new_alerts_sent": alerted,
    })
    with open(LOG_PATH, 'w') as f:
        json.dump(logs[-200:], f, indent=2)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config          = load_config()
    webhook_url     = config.get('discord_webhook_url', '')
    min_score       = config.get('min_score', 50)
    min_fdv         = config.get('min_fdv', 30_000)
    max_fdv         = config.get('max_fdv', 600_000)
    smart_money_cfg = config.get('smart_money_wallets', {})

    if not webhook_url or webhook_url == "PASTE_YOUR_WEBHOOK_URL_HERE":
        print("No Discord webhook URL configured. Set in config/scanner_config.json")
        sys.exit(0)

    print(f"[{datetime.now().strftime('%H:%M')}] Scanning {len(SEARCH_TERMS)} categories...")
    pairs = fetch_all_pairs()
    print(f"[{datetime.now().strftime('%H:%M')}] Fetched {len(pairs)} pairs — running metrics filter...")

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

    # Sort by score
    candidates.sort(key=lambda x: -x[0])

    if not candidates:
        print(f"[{datetime.now().strftime('%H:%M')}] No pre-runners above score {min_score}.")
        log_run(0, 0, len(pairs))
        return

    print(f"[{datetime.now().strftime('%H:%M')}] {len(candidates)} metric-passing candidates — running chart pattern check...")

    # ── Chart Pattern Filter & Boost ──────────────────────────────────────
    chart_checked = []
    rug_filtered  = 0

    for score, pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h in candidates[:15]:  # cap chart API calls at 15
        addr = pair.get('pairAddress', '')
        age_h_val = age_h

        chart_pattern = None
        if age_h_val >= 2.0:   # only check chart for tokens >2h old (need enough candles)
            try:
                chart_pattern = classify_pair(addr)
                time.sleep(0.25)  # GeckoTerminal rate limit
            except Exception as e:
                print(f"  Chart API error for {addr[:12]}: {e}")

        # Filter confirmed rug patterns
        if chart_pattern and chart_pattern.pattern in RUG_PATTERNS and chart_pattern.confidence > 0.65:
            print(f"  🚫 Filtered (rug pattern {chart_pattern.pattern}): {(pair.get('baseToken') or {}).get('symbol','?')}")
            rug_filtered += 1
            continue

        # Boost score for confirmed organic runners / accumulation
        boosted_score = score
        if chart_pattern and chart_pattern.is_alert_target:
            boost = int(chart_pattern.runner_probability * 15)
            boosted_score = min(100, score + boost)
            print(f"  🎯 Runner pattern confirmed ({chart_pattern.pattern}): {(pair.get('baseToken') or {}).get('symbol','?')} +{boost} pts → {boosted_score}")

        chart_checked.append((boosted_score, pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h, chart_pattern))

    # Re-sort after boosts
    chart_checked.sort(key=lambda x: -x[0])
    new_alerts = chart_checked[:5]

    if not new_alerts:
        print(f"[{datetime.now().strftime('%H:%M')}] All candidates filtered by rug pattern. No alerts.")
        log_run(len(candidates), 0, len(pairs), rug_filtered)
        return

    print(f"[{datetime.now().strftime('%H:%M')}] Sending {len(new_alerts)} alert(s)...")

    alerted = 0
    for score, pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h, chart_pattern in new_alerts:
        mint       = (pair.get('baseToken') or {}).get('address', '')
        smart_hits = check_smart_money(mint, smart_money_cfg) if mint else []
        if smart_hits:
            labels = list(dict.fromkeys(h["label"] for h in smart_hits))
            print(f"  🤖 Smart money: {', '.join(labels)}")

        embed = build_embed(pair, buy_r, vol24, liq, fdv, pc24, fdv_liq, age_h,
                            score, chart_pattern, smart_hits)
        ok = send_discord(webhook_url, [embed])
        addr = pair.get('pairAddress', '')
        sym  = (pair.get('baseToken') or {}).get('symbol', '?')
        if ok:
            seen.add(addr)
            alerted += 1
            cp_tag = f" [{chart_pattern.pattern}]" if chart_pattern and chart_pattern.pattern != 'UNKNOWN' else ""
            print(f"  ✓ Alerted: {sym} (score {score}){cp_tag}")
        else:
            print(f"  ✗ Failed:  {sym}")
        time.sleep(0.5)

    save_seen(seen)
    log_run(len(candidates), alerted, len(pairs), rug_filtered)
    print(f"[{datetime.now().strftime('%H:%M')}] Done. {alerted} alert(s) sent. {rug_filtered} filtered by rug pattern.")


if __name__ == '__main__':
    main()
