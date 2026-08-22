"""
Persist open positions, paper cash, and daily realized PnL across restarts.

Primary store is bot_state.json (written after every fill and after each
position-management pass). If that file is missing on startup, holdings
are rebuilt from unmatched BUY rows in trade_log.csv.
"""
import csv
import json
import os
from datetime import date

from typing import Optional

import requests

from strategy import Position

STATE_PATH = "bot_state.json"


def _position_to_dict(pos: Position) -> dict:
    return {
        "pair_address": pos.pair_address,
        "symbol": pos.symbol,
        "mint": pos.mint or "",
        "entry_price": pos.entry_price,
        "entry_time": pos.entry_time,
        "size_tokens": pos.size_tokens,
        "original_size_tokens": pos.original_size_tokens,
        "peak_price": pos.peak_price,
        "last_new_high_time": pos.last_new_high_time,
        "tp_ladder_hit": list(pos.tp_ladder_hit),
        "volume_history": list(pos.volume_history),
        "liquidity_usd": pos.liquidity_usd,
        "volume_5m": pos.volume_5m,
        "buy_sell_ratio": pos.buy_sell_ratio,
        "pair_age_seconds": pos.pair_age_seconds,
    }


def _position_from_dict(d: dict) -> Position:
    return Position(
        pair_address=d["pair_address"],
        symbol=d["symbol"],
        mint=d.get("mint") or "",
        entry_price=float(d["entry_price"]),
        entry_time=float(d["entry_time"]),
        size_tokens=float(d["size_tokens"]),
        original_size_tokens=float(d["original_size_tokens"]),
        peak_price=float(d["peak_price"]) if d.get("peak_price") is not None else None,
        last_new_high_time=(
            float(d["last_new_high_time"]) if d.get("last_new_high_time") is not None else None
        ),
        tp_ladder_hit=list(d.get("tp_ladder_hit") or []),
        volume_history=list(d.get("volume_history") or []),
        liquidity_usd=d.get("liquidity_usd"),
        volume_5m=d.get("volume_5m"),
        buy_sell_ratio=d.get("buy_sell_ratio"),
        pair_age_seconds=d.get("pair_age_seconds"),
    )


def save_state(open_positions: dict, realized_pnl_usd: float, paper_balance_usd=None):
    payload = {
        "session_date": date.today().isoformat(),
        "realized_pnl_usd": realized_pnl_usd,
        "paper_balance_usd": paper_balance_usd,
        "open_positions": [_position_to_dict(p) for p in open_positions.values()],
    }
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, STATE_PATH)


def load_state() -> Optional[dict]:
    if not os.path.exists(STATE_PATH) or os.path.getsize(STATE_PATH) == 0:
        return None
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[state] could not read {STATE_PATH}: {e}", flush=True)
        return None

    positions = {}
    for raw in data.get("open_positions") or []:
        pos = _position_from_dict(raw)
        positions[pos.pair_address] = pos

    realized = float(data.get("realized_pnl_usd") or 0)
    saved_day = data.get("session_date")
    if saved_day and saved_day != date.today().isoformat():
        print(f"[state] new calendar day (was {saved_day}); resetting daily realized PnL",
              flush=True)
        realized = 0.0

    return {
        "open_positions": positions,
        "realized_pnl_usd": realized,
        "paper_balance_usd": data.get("paper_balance_usd"),
        "source": STATE_PATH,
    }


def recover_from_trade_log(dexscreener_client, chain_id: str = "solana") -> Optional[dict]:
    """Rebuild leftover longs from trade_log.csv when bot_state.json is missing."""
    from executor import LOG_PATH

    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        return None

    with open(LOG_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None

    lots = {}  # mint -> open lot
    paper_balance = None
    realized = 0.0

    for row in rows:
        mint = (row.get("contract_address") or "").strip()
        side = row.get("side")
        try:
            tokens = float(row.get("tokens") or 0)
            price = float(row.get("price") or 0)
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if row.get("portfolio_balance") not in (None, ""):
            try:
                paper_balance = float(row["portfolio_balance"])
            except (TypeError, ValueError):
                pass
        if row.get("pnl") not in (None, ""):
            try:
                realized += float(row["pnl"])
            except (TypeError, ValueError):
                pass

        if not mint:
            continue
        if side == "BUY":
            lots[mint] = {
                "symbol": row.get("symbol") or "?",
                "mint": mint,
                "entry_price": price,
                "entry_time": ts,
                "size_tokens": tokens,
                "original_size_tokens": tokens,
                "liquidity_usd": row.get("liquidity_usd"),
                "volume_5m": row.get("volume_5m"),
                "buy_sell_ratio": row.get("buy_sell_ratio"),
                "pair_age_seconds": row.get("pair_age_seconds"),
            }
        elif side == "SELL" and mint in lots:
            lots[mint]["size_tokens"] -= tokens
            if lots[mint]["size_tokens"] <= 1e-9:
                del lots[mint]

    if not lots and paper_balance is None:
        return None

    positions = {}
    for mint, lot in lots.items():
        try:
            pairs = dexscreener_client.get_token_pairs(chain_id, mint)
        except requests.RequestException as e:
            print(f"[state] Dexscreener lookup failed for {lot['symbol']} ({mint[:8]}...): {e}",
                  flush=True)
            continue
        pair = (pairs or [None])[0]
        pair_address = (pair or {}).get("pairAddress")
        if not pair_address:
            print(f"[state] no Dexscreener pair for open {lot['symbol']} — skipped", flush=True)
            continue
        def _opt_float(val):
            try:
                return float(val) if val not in (None, "") else None
            except (TypeError, ValueError):
                return None

        positions[pair_address] = Position(
            pair_address=pair_address,
            symbol=lot["symbol"],
            mint=mint,
            entry_price=lot["entry_price"],
            entry_time=lot["entry_time"],
            size_tokens=lot["size_tokens"],
            original_size_tokens=lot["original_size_tokens"],
            liquidity_usd=_opt_float(lot.get("liquidity_usd")),
            volume_5m=_opt_float(lot.get("volume_5m")),
            buy_sell_ratio=_opt_float(lot.get("buy_sell_ratio")),
            pair_age_seconds=_opt_float(lot.get("pair_age_seconds")),
        )

    return {
        "open_positions": positions,
        "realized_pnl_usd": realized,
        "paper_balance_usd": paper_balance,
        "source": LOG_PATH,
    }
