"""
Thin client around the public Dexscreener API.
Docs: https://docs.dexscreener.com/api/reference
"""
import time
import requests

BASE_URL = "https://api.dexscreener.com"


class DexscreenerClient:
    def __init__(self, session: requests.Session = None):
        self.session = session or requests.Session()

    def get_pair(self, chain_id: str, pair_address: str) -> dict:
        """Fetch live data for a single pair (price, volume, liquidity, txns)."""
        url = f"{BASE_URL}/latest/dex/pairs/{chain_id}/{pair_address}"
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        return pairs[0] if pairs else None

    def search_pairs(self, query: str) -> list:
        """Search pairs by token symbol/name/address."""
        url = f"{BASE_URL}/latest/dex/search"
        r = self.session.get(url, params={"q": query}, timeout=10)
        r.raise_for_status()
        return r.json().get("pairs", [])

    def get_token_pairs(self, chain_id: str, token_address: str) -> list:
        """Get all pairs for a given token address."""
        url = f"{BASE_URL}/token-pairs/v1/{chain_id}/{token_address}"
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        return r.json() or []

    def poll_pair(self, chain_id: str, pair_address: str, interval: int = 5):
        """Generator that yields fresh pair data every `interval` seconds."""
        while True:
            try:
                yield self.get_pair(chain_id, pair_address)
            except requests.RequestException as e:
                print(f"[dexscreener] poll error: {e}")
                yield None
            time.sleep(interval)


def passes_entry_filters(pair: dict, cfg) -> tuple[bool, str]:
    """Check a candidate pair against config entry filters. Returns (ok, reason)."""
    if not pair:
        return False, "no data"

    liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
    if liquidity < cfg.MIN_LIQUIDITY_USD:
        return False, f"liquidity ${liquidity:.0f} < min ${cfg.MIN_LIQUIDITY_USD}"

    created_at_ms = pair.get("pairCreatedAt")
    if created_at_ms:
        age_sec = (time.time() * 1000 - created_at_ms) / 1000
        if age_sec < cfg.MIN_PAIR_AGE_SECONDS:
            return False, f"too new ({age_sec:.0f}s old)"
        if age_sec > cfg.MAX_PAIR_AGE_SECONDS:
            return False, f"too old ({age_sec:.0f}s old)"

    vol_5m = (pair.get("volume") or {}).get("m5", 0) or 0
    if vol_5m < cfg.MIN_VOLUME_5M_USD:
        return False, f"5m volume ${vol_5m:.0f} < min ${cfg.MIN_VOLUME_5M_USD}"

    txns_5m = (pair.get("txns") or {}).get("m5", {})
    buys, sells = txns_5m.get("buys", 0), txns_5m.get("sells", 0)
    ratio = buys / sells if sells > 0 else float("inf")
    if ratio < cfg.MIN_BUY_SELL_RATIO_5M:
        return False, f"buy/sell ratio {ratio:.2f} < min {cfg.MIN_BUY_SELL_RATIO_5M}"

    return True, "ok"


def entry_snapshot(pair: dict) -> dict:
    """Dexscreener fields to log at buy (and copy onto the matching sell)."""
    if not pair:
        return {}
    liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
    vol_5m = (pair.get("volume") or {}).get("m5", 0) or 0
    txns_5m = (pair.get("txns") or {}).get("m5", {}) or {}
    buys = txns_5m.get("buys", 0) or 0
    sells = txns_5m.get("sells", 0) or 0
    if sells > 0:
        ratio = round(buys / sells, 2)
    elif buys > 0:
        ratio = buys
    else:
        ratio = 0
    age = None
    created_at_ms = pair.get("pairCreatedAt")
    if created_at_ms:
        age = round((time.time() * 1000 - created_at_ms) / 1000, 1)
    snap = {
        "liquidity_usd": round(float(liquidity), 2),
        "volume_5m": round(float(vol_5m), 2),
        "buy_sell_ratio": ratio,
    }
    if age is not None:
        snap["pair_age_seconds"] = age
    return snap
