"""
Execution layer. Two modes:

PAPER: simulates fills against live Dexscreener prices. No funds at risk.
       Use this to validate the strategy before going anywhere near LIVE.

LIVE:  builds and sends real swap transactions through Jupiter's API,
       signed locally with a keypair loaded from an environment variable.
       Nothing about your key ever leaves your machine in this design -
       it's read from the env, used to sign locally, and the signed
       transaction (not the key) is sent to Jupiter/RPC.

Before ever setting MODE = "LIVE" in config.py:
  1. Run PAPER mode and review trade_log.csv for at least several sessions.
  2. Understand that every number below is an assumption, not a guarantee -
     read config.py and adjust for your own risk tolerance.
  3. Only fund the wallet you point this at with money you've already
     decided you can lose completely.
"""
import os
import csv
import time
import base64
import datetime
import subprocess
import threading
import requests

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"

LOG_PATH = "trade_log.csv"
LOG_FIELDS = [
    "symbol",
    "contract_address",
    "side",
    "state",
    "usd",
    "price",
    "tokens",
    "ts",
    "mode",
    "signature",
    "ts_readable",
    "portfolio_balance",
    "pnl",
    "exit_reason",
    "liquidity_usd",
    "volume_5m",
    "buy_sell_ratio",
    "pair_age_seconds",
]

_log_lock = threading.Lock()

# macOS system sounds — played in the background so fills never wait on audio.
_SOUND_BUY = "/System/Library/Sounds/Glass.aiff"
_SOUND_WIN = "/System/Library/Sounds/Blow.aiff"
_SOUND_LOSS = "/System/Library/Sounds/Bottle.aiff"


def _play_alert(path: str):
    """Play a short system sound without blocking the trading loop."""
    if not os.path.exists(path):
        print("\a", end="", flush=True)
        return
    try:
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        print("\a", end="", flush=True)


def _alert_for_fill(row: dict):
    try:
        import config
        if not getattr(config, "SOUND_ALERTS", True):
            return
    except ImportError:
        pass
    side = row.get("side")
    if side == "BUY":
        _play_alert(_SOUND_BUY)
        return
    if side != "SELL":
        return
    try:
        pnl = float(row.get("pnl") or 0)
    except (TypeError, ValueError):
        pnl = 0.0
    if pnl > 0:
        _play_alert(_SOUND_WIN)
    else:
        _play_alert(_SOUND_LOSS)


def _with_extras(fill: dict, extras: dict = None) -> dict:
    if extras:
        fill.update({k: v for k, v in extras.items() if v is not None and v != ""})
    return fill


def _realized_pnl(side: str, tokens: float, price: float, entry_price: float = None) -> float:
    """USD realized PnL for a fill. Buys are 0; sells are (exit - entry) * tokens."""
    if side != "SELL" or entry_price is None:
        return 0.0
    return round((price - entry_price) * tokens, 2)


def _backfill_pnl(rows: list) -> list:
    """Fill missing pnl on older log rows using FIFO entry prices per mint."""
    open_entries = {}  # key -> (entry_price, remaining_tokens)
    for row in rows:
        side = row.get("side")
        key = row.get("contract_address") or row.get("symbol")
        try:
            tokens = float(row.get("tokens") or 0)
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            if row.get("pnl") in (None, ""):
                row["pnl"] = 0.0
            continue
        if side == "BUY":
            open_entries[key] = (price, tokens)
            if row.get("pnl") in (None, ""):
                row["pnl"] = 0.0
        elif side == "SELL":
            entry_price, remaining = open_entries.get(key, (None, 0.0))
            if row.get("pnl") in (None, ""):
                row["pnl"] = _realized_pnl("SELL", tokens, price, entry_price)
            remaining -= tokens
            if remaining <= 1e-9:
                open_entries.pop(key, None)
            else:
                open_entries[key] = (entry_price, remaining)
    return rows


def _refresh_trade_states(rows: list) -> list:
    """Mark each BUY open until its tokens are fully sold; SELL rows are closed."""
    remaining = {}
    buy_indices = {}
    for i, row in enumerate(rows):
        side = row.get("side")
        key = row.get("contract_address") or row.get("symbol")
        try:
            tokens = float(row.get("tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0.0
        if side == "BUY":
            remaining[key] = remaining.get(key, 0.0) + tokens
            buy_indices.setdefault(key, []).append(i)
            row["state"] = "open"
        elif side == "SELL":
            row["state"] = "closed"
            leftover = remaining.get(key, 0.0) - tokens
            remaining[key] = leftover
            if leftover <= 1e-9:
                remaining[key] = 0.0
                for bi in buy_indices.get(key, []):
                    rows[bi]["state"] = "closed"
                buy_indices[key] = []
    return rows


def read_trade_log() -> list:
    """Snapshot of trade_log.csv for the live viewer."""
    with _log_lock:
        if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
            return []
        with open(LOG_PATH, newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return []
            return list(reader)


def _rewrite_log(rows: list):
    rows = _refresh_trade_states(_backfill_pnl(rows))
    with _log_lock:
        with open(LOG_PATH, "w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=LOG_FIELDS, extrasaction="ignore", restval=""
            )
            writer.writeheader()
            for old in rows:
                writer.writerow(old)
            f.flush()
            os.fsync(f.fileno())


def _log_trade(row: dict):
    """Writes one fill to trade_log.csv. Adds a human-readable local-time
    column ('ts_readable') alongside the raw unix 'ts'. Always rewrites
    the file so BUY rows flip from open to closed when the position exits."""
    row = dict(row)  # don't mutate the caller's dict
    if "ts" in row and "ts_readable" not in row:
        row["ts_readable"] = datetime.datetime.fromtimestamp(row["ts"]).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    if not row.get("state"):
        row["state"] = "open" if row.get("side") == "BUY" else "closed"

    existing_rows = read_trade_log()
    existing_rows.append(row)
    _rewrite_log(existing_rows)
    _alert_for_fill(row)


class PaperExecutor:
    """Simulates fills instantly at the current quoted price. No real orders."""

    def __init__(self, starting_balance_usd: float):
        self.balance_usd = starting_balance_usd

    def buy(self, symbol: str, pair_address: str, usd_amount: float, price: float,
            mint: str = "", extras: dict = None) -> dict:
        tokens = usd_amount / price
        self.balance_usd -= usd_amount
        fill = _with_extras({
            "symbol": symbol, "contract_address": mint,
            "side": "BUY", "state": "open", "usd": usd_amount,
            "price": price, "tokens": tokens, "ts": time.time(), "mode": "PAPER",
            "portfolio_balance": round(self.balance_usd, 2),
            "pnl": _realized_pnl("BUY", tokens, price),
        }, extras)
        _log_trade(fill)
        print(f"[PAPER] BUY {symbol} ${usd_amount:.2f} @ {price:.8f} -> {tokens:.2f} tokens")
        return fill

    def sell(self, symbol: str, pair_address: str, tokens: float, price: float,
             mint: str = "", entry_price: float = None, extras: dict = None) -> dict:
        usd_amount = tokens * price
        self.balance_usd += usd_amount
        pnl = _realized_pnl("SELL", tokens, price, entry_price)
        fill = _with_extras({
            "symbol": symbol, "contract_address": mint,
            "side": "SELL", "state": "closed", "usd": usd_amount,
            "price": price, "tokens": tokens, "ts": time.time(), "mode": "PAPER",
            "portfolio_balance": round(self.balance_usd, 2),
            "pnl": pnl,
        }, extras)
        _log_trade(fill)
        reason = (extras or {}).get("exit_reason", "")
        reason_bit = f" ({reason})" if reason else ""
        print(f"[PAPER] SELL {symbol} {tokens:.2f} tokens @ {price:.8f} "
              f"-> ${usd_amount:.2f} pnl=${pnl:.2f}{reason_bit}")
        return fill


class LiveExecutor:
    """
    Sends real swaps via Jupiter, signed locally with a keypair loaded from
    the SOLANA_PRIVATE_KEY environment variable. The key is never written to
    disk or sent anywhere by this code - it's used in-memory to sign, and
    only the signed transaction is broadcast.

    Requires: pip install solders base58
    """

    def __init__(self, rpc_url: str, slippage_bps: int, priority_fee_lamports: int):
        self.rpc_url = rpc_url
        self.slippage_bps = slippage_bps
        self.priority_fee_lamports = priority_fee_lamports

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
        print(f"[LiveExecutor] loaded wallet {self.pubkey}")

    def _get_quote(self, input_mint: str, output_mint: str, amount_lamports: int) -> dict:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount_lamports,
            "slippageBps": self.slippage_bps,
        }
        r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _build_swap_tx(self, quote: dict) -> str:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": self.pubkey,
            "prioritizationFeeLamports": self.priority_fee_lamports,
            "wrapAndUnwrapSol": True,
        }
        r = requests.post(JUPITER_SWAP_URL, json=payload, timeout=10)
        r.raise_for_status()
        return r.json()["swapTransaction"]  # base64-encoded unsigned tx

    def _sign_and_send(self, swap_tx_b64: str) -> str:
        from solders.transaction import VersionedTransaction
        from solders.message import to_bytes_versioned

        raw_tx = base64.b64decode(swap_tx_b64)
        unsigned_tx = VersionedTransaction.from_bytes(raw_tx)

        signature = self.keypair.sign_message(to_bytes_versioned(unsigned_tx.message))
        signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])

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
        return result["result"]  # transaction signature

    def buy(self, symbol: str, pair_address: str, usd_amount: float, price: float,
             token_mint: str, sol_price_usd: float, extras: dict = None) -> dict:
        amount_lamports = int((usd_amount / sol_price_usd) * 1e9)
        quote = self._get_quote(SOL_MINT, token_mint, amount_lamports)
        swap_tx = self._build_swap_tx(quote)
        sig = self._sign_and_send(swap_tx)

        tokens_out = float(quote["outAmount"]) / (10 ** quote.get("outputDecimals", 6))
        fill = _with_extras({
            "symbol": symbol, "contract_address": token_mint,
            "side": "BUY", "state": "open", "usd": usd_amount, "price": price,
            "tokens": tokens_out, "ts": time.time(), "mode": "LIVE", "signature": sig,
            "pnl": _realized_pnl("BUY", tokens_out, price),
        }, extras)
        _log_trade(fill)
        print(f"[LIVE] BUY {symbol} ${usd_amount:.2f} -> tx {sig}")
        return fill

    def sell(self, symbol: str, pair_address: str, tokens: float, price: float,
              token_mint: str, token_decimals: int, entry_price: float = None,
              extras: dict = None) -> dict:
        amount_raw = int(tokens * (10 ** token_decimals))
        quote = self._get_quote(token_mint, SOL_MINT, amount_raw)
        swap_tx = self._build_swap_tx(quote)
        sig = self._sign_and_send(swap_tx)

        usd_out = tokens * price
        fill = _with_extras({
            "symbol": symbol, "contract_address": token_mint,
            "side": "SELL", "state": "closed", "usd": usd_out, "price": price,
            "tokens": tokens, "ts": time.time(), "mode": "LIVE", "signature": sig,
            "pnl": _realized_pnl("SELL", tokens, price, entry_price),
        }, extras)
        _log_trade(fill)
        print(f"[LIVE] SELL {symbol} {tokens:.4f} tokens -> tx {sig}")
        return fill
