"""
Integration with PumpPortal (https://pumpportal.fun) - a documented,
free, third-party API for Pump.fun. Two pieces:

1. PumpPortalDiscovery: connects to the free public websocket and gets
   notified the instant a new token is created on Pump.fun - this is a
   genuinely fresh discovery source, unlike the boosted/profile feeds
   in discovery.py which surface paid-promotion tokens (usually already
   thin and heavily marketed, not organically fresh).

   Newly-created mints are held in a pending pool until they cross
   MIN_PAIR_AGE_SECONDS, then checked against REAL Dexscreener liquidity/
   volume data (Dexscreener indexes Pump.fun bonding-curve pairs directly)
   and run through the same passes_entry_filters() used elsewhere - so
   the filtering logic you already reviewed doesn't change, only the
   source of candidates does.

2. PumpPortalExecutor: builds swap transactions via PumpPortal's
   "Local Transaction API" (non-custodial - PumpPortal returns an
   UNSIGNED transaction, you sign it locally with your own key, exactly
   like the Jupiter path in executor.py). Uses pool="auto" so it works
   whether a token is still on the bonding curve or has graduated to
   Raydium/PumpSwap - Jupiter alone cannot route bonding-curve-only
   tokens, which is why this exists as a separate executor.

PumpPortal is an unofficial, third-party service (not affiliated with
Pump.fun) with no disclosed team or security audit - noting that
plainly since it's relevant to how much to trust it with real funds.
It takes a 0.5% fee per trade executed through its Local Transaction API.

Docs referenced: https://pumpportal.fun/data-api/real-time/
                 https://pumpportal.fun/local-trading-api/trading-api/
"""
import os
import time
import json
import base64
import asyncio
import threading
import queue as pyqueue

import requests
import websockets

from executor import _log_trade

WS_URL = "wss://pumpportal.fun/api/data"
TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"


# ============ DISCOVERY ============

class PumpPortalDiscovery:
    def __init__(self, dexscreener_client, chain_id: str = "solana"):
        self.client = dexscreener_client
        self.chain_id = chain_id
        self.pending = {}   # mint -> creation_time
        self.seen_pairs = set()
        self._lock = threading.Lock()
        self.tokens_seen_count = 0

    async def _listen(self):
        while True:
            try:
                async with websockets.connect(WS_URL) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    print("[pumpportal] connected, subscribed to new token events")
                    async for message in ws:
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        mint = data.get("mint")
                        if mint and data.get("txType") == "create":
                            with self._lock:
                                self.pending[mint] = time.time()
                                self.tokens_seen_count += 1
                            symbol = data.get("symbol", "?")
                            print(f"[pumpportal] new token: {symbol} ({mint[:8]}...) "
                                  f"- watching, total pending: {len(self.pending)}")
            except Exception as e:
                print(f"[pumpportal] websocket error: {e} - reconnecting in 5s")
                await asyncio.sleep(5)

    def start_background_listener(self):
        """Runs the websocket listener forever in a dedicated thread."""
        def _run():
            asyncio.run(self._listen())
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def check_matured_candidates(self, cfg) -> list[dict]:
        """
        Call periodically from the main loop. Checks any pending mints that
        have crossed MIN_PAIR_AGE_SECONDS against real Dexscreener data and
        returns the ones that pass entry filters. Drops mints older than
        MAX_PAIR_AGE_SECONDS (missed window, no longer worth checking).

        Always prints a summary line so the scan is visibly alive even when
        nothing matures or passes this pass.
        """
        from dexscreener import passes_entry_filters

        now = time.time()
        candidates = []
        rejection_counts = {}

        with self._lock:
            mints_to_check = [
                m for m, created in self.pending.items()
                if now - created >= cfg.MIN_PAIR_AGE_SECONDS
            ]
            dropped = 0
            for mint in list(self.pending.keys()):
                if now - self.pending[mint] > cfg.MAX_PAIR_AGE_SECONDS:
                    del self.pending[mint]
                    dropped += 1
            pending_count = len(self.pending)

        for mint in mints_to_check:
            with self._lock:
                self.pending.pop(mint, None)
            try:
                pairs = self.client.get_token_pairs(self.chain_id, mint)
            except requests.RequestException as e:
                rejection_counts[f"fetch error"] = rejection_counts.get("fetch error", 0) + 1
                continue
            if not pairs:
                rejection_counts["no pair data yet"] = rejection_counts.get("no pair data yet", 0) + 1
                continue
            for pair in pairs:
                pair_address = pair.get("pairAddress")
                if not pair_address or pair_address in self.seen_pairs:
                    continue
                ok, reason = passes_entry_filters(pair, cfg)
                if ok:
                    self.seen_pairs.add(pair_address)
                    candidates.append(pair)
                else:
                    key = reason.split("(")[0].split("$")[0].strip()
                    rejection_counts[key] = rejection_counts.get(key, 0) + 1

        status = (f"[pumpportal/scan] pending={pending_count} "
                  f"checked_this_pass={len(mints_to_check)} "
                  f"matured_candidates={len(candidates)} "
                  f"total_tokens_seen={self.tokens_seen_count}")
        if dropped:
            status += f" dropped_expired={dropped}"
        if rejection_counts:
            status += f" rejected={rejection_counts}"
        print(status)

        return candidates


# ============ EXECUTION ============

class PumpPortalExecutor:
    """
    Non-custodial execution via PumpPortal's Local Transaction API.
    Same signing pattern as executor.LiveExecutor: your private key is
    loaded from SOLANA_PRIVATE_KEY, used only to sign locally, and only
    the signed transaction is broadcast - never the key.
    """

    def __init__(self, rpc_url: str, slippage_pct: float, priority_fee_sol: float):
        self.rpc_url = rpc_url
        self.slippage_pct = slippage_pct
        self.priority_fee_sol = priority_fee_sol

        raw_key = os.environ.get("SOLANA_PRIVATE_KEY")
        if not raw_key:
            raise RuntimeError(
                "SOLANA_PRIVATE_KEY not set in environment. "
                "Set it with: export SOLANA_PRIVATE_KEY='your_base58_key' "
                "Never hardcode it in a file."
            )

        from solders.keypair import Keypair
        import base58

        self.keypair = Keypair.from_bytes(base58.b58decode(raw_key))
        self.pubkey = str(self.keypair.pubkey())
        print(f"[PumpPortalExecutor] loaded wallet {self.pubkey}")

    def _get_unsigned_tx(self, action: str, mint: str, amount, denominated_in_sol: bool) -> bytes:
        payload = {
            "publicKey": self.pubkey,
            "action": action,               # "buy" or "sell"
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "slippage": self.slippage_pct,
            "priorityFee": self.priority_fee_sol,
            "pool": "auto",                 # routes to bonding curve or Raydium/PumpSwap automatically
        }
        r = requests.post(TRADE_LOCAL_URL, data=payload, timeout=15)
        r.raise_for_status()
        return r.content  # raw serialized unsigned transaction bytes

    def _sign_and_send(self, raw_tx_bytes: bytes) -> str:
        from solders.transaction import VersionedTransaction

        unsigned_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        signed_tx = VersionedTransaction(unsigned_tx.message, [self.keypair])

        send_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(bytes(signed_tx)).decode("utf-8"),
                {"encoding": "base64", "skipPreflight": False, "maxRetries": 2},
            ],
        }
        r = requests.post(self.rpc_url, json=send_payload, timeout=15)
        r.raise_for_status()
        result = r.json()
        if "error" in result:
            raise RuntimeError(f"RPC error sending transaction: {result['error']}")
        return result["result"]

    def buy(self, symbol: str, mint: str, sol_amount: float, price: float) -> dict:
        raw_tx = self._get_unsigned_tx("buy", mint, sol_amount, denominated_in_sol=True)
        sig = self._sign_and_send(raw_tx)
        tokens_est = (sol_amount / price) if price else 0
        fill = {"symbol": symbol, "side": "BUY", "usd": sol_amount * price,
                "price": price, "tokens": tokens_est, "ts": time.time(),
                "mode": "LIVE-PUMPPORTAL", "signature": sig}
        _log_trade(fill)
        print(f"[LIVE/PumpPortal] BUY {symbol} {sol_amount} SOL -> tx {sig}")
        return fill

    def sell(self, symbol: str, mint: str, tokens: float, price: float) -> dict:
        raw_tx = self._get_unsigned_tx("sell", mint, tokens, denominated_in_sol=False)
        sig = self._sign_and_send(raw_tx)
        fill = {"symbol": symbol, "side": "SELL", "usd": tokens * price,
                "price": price, "tokens": tokens, "ts": time.time(),
                "mode": "LIVE-PUMPPORTAL", "signature": sig}
        _log_trade(fill)
        print(f"[LIVE/PumpPortal] SELL {symbol} {tokens:.4f} tokens -> tx {sig}")
        return fill