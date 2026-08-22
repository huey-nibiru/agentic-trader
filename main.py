"""
Full loop:
  1. PumpPortalDiscovery listens on a free websocket for the instant a new
     token is created on Pump.fun (fully automatic, runs in the background).
  2. Once a token crosses MIN_PAIR_AGE_SECONDS, it's checked against real
     Dexscreener liquidity/volume data and the same entry filters used
     throughout this project.
  3. Each candidate that passes is shown to you via the approval layer -
     nothing is bought without your yes (LIVE mode). PAPER mode
     auto-approves so you can watch the whole pipeline end-to-end without
     any real funds moving.
  4. Approved buys open a Position, managed automatically from then on by
     the SL/TP engine in strategy.py.

LIVE-mode buys/sells execute via PumpPortal's Local Transaction API
(non-custodial - you sign locally, see pumpportal.py), since it can route
to a token's bonding curve OR Raydium/PumpSwap once graduated. PAPER mode
simulates fills instead - no funds touched.

Usage:
    python main.py
"""
import os
import time
import atexit

import config
from dexscreener import DexscreenerClient, entry_snapshot
from pumpportal import PumpPortalDiscovery, PumpPortalExecutor
from strategy import Position, evaluate
from executor import PaperExecutor
from approval import ConsoleApproval
from state import load_state, save_state, recover_from_trade_log


class AutoApproval:
    """PAPER mode only - lets the full pipeline run end-to-end with zero
    funds at risk. LIVE mode always uses a real human-approval implementation."""
    def approve(self, pair: dict) -> bool:
        symbol = (pair.get("baseToken") or {}).get("symbol", "?")
        print(f"[paper-auto-approve] {symbol}")
        return True


def build_executor():
    if config.MODE == "PAPER":
        return PaperExecutor(starting_balance_usd=config.TOTAL_BANKROLL_USD)
    elif config.MODE == "LIVE":
        return PumpPortalExecutor(
            rpc_url=config.RPC_URL,
            slippage_pct=config.PUMPPORTAL_SLIPPAGE_PCT,
            priority_fee_sol=config.PUMPPORTAL_PRIORITY_FEE_SOL,
        )
    raise ValueError(f"Unknown MODE: {config.MODE}")


def build_approval():
    if config.MODE == "PAPER":
        return AutoApproval()
    # LIVE mode: always require a real human approval implementation.
    # Swap this for TelegramApproval() (see approval.py) to approve from your phone.
    return ConsoleApproval(timeout_seconds=60)


def main():
    os.environ["PREVIEW_SIZE"] = str(config.MAX_POSITION_USD)

    print(f"=== Meme bot starting | mode={config.MODE} ===")
    if config.MODE == "LIVE":
        print("!! LIVE MODE: real funds via PumpPortal. Every buy requires your explicit approval. !!")

    client = DexscreenerClient()
    scanner = PumpPortalDiscovery(client, chain_id="solana")
    executor = build_executor()
    approval = build_approval()

    scanner.start_background_listener()

    open_positions: dict[str, Position] = {}
    realized_pnl_usd = 0.0
    last_discovery_check = 0.0
    last_position_poll = 0.0

    restored = load_state() or recover_from_trade_log(client)
    if restored:
        open_positions.update(restored["open_positions"])
        realized_pnl_usd = restored["realized_pnl_usd"]
        paper_bal = restored.get("paper_balance_usd")
        if paper_bal is not None and hasattr(executor, "balance_usd"):
            executor.balance_usd = paper_bal
        held = ", ".join(p.symbol for p in open_positions.values()) or "(none)"
        cash = getattr(executor, "balance_usd", None)
        cash_bit = f" | paper cash ${cash:.2f}" if cash is not None else ""
        print(f"[state] restored from {restored['source']}: "
              f"{len(open_positions)} open ({held}) | "
              f"realized PnL ${realized_pnl_usd:.2f}{cash_bit}", flush=True)

    def persist():
        save_state(
            open_positions,
            realized_pnl_usd,
            getattr(executor, "balance_usd", None),
        )

    atexit.register(persist)
    persist()

    print("[main] PumpPortal discovery running in background, entering management loop...\n", flush=True)

    while True:
        if realized_pnl_usd <= -config.DAILY_LOSS_LIMIT_USD:
            print(f"[STOP] Daily loss limit hit (${realized_pnl_usd:.2f}). Halting.")
            break

        now = time.time()

        # --- check for newly matured discovery candidates ---
        if (len(open_positions) < config.MAX_CONCURRENT_POSITIONS
                and now - last_discovery_check >= config.DISCOVERY_CHECK_INTERVAL_SECONDS):
            last_discovery_check = now
            candidates = scanner.check_matured_candidates(config)
            for pair in candidates:
                if len(open_positions) >= config.MAX_CONCURRENT_POSITIONS:
                    break
                pair_address = pair.get("pairAddress")
                mint = (pair.get("baseToken") or {}).get("address")
                symbol = (pair.get("baseToken") or {}).get("symbol", "?")
                price = float(pair.get("priceUsd", 0) or 0)
                if not price or pair_address in open_positions:
                    continue

                if not approval.approve(pair):
                    print(f"[skip] {symbol} not approved")
                    continue

                stats = entry_snapshot(pair)
                if config.MODE == "LIVE":
                    sol_price_usd = float(os.environ.get("SOL_USD_PRICE", "0")) or None
                    if not sol_price_usd:
                        print("[error] SOL_USD_PRICE not set - cannot size LIVE buy, skipping")
                        continue
                    sol_amount = config.MAX_POSITION_USD / sol_price_usd
                    fill = executor.buy(symbol, mint, sol_amount, price, extras=stats)
                else:
                    fill = executor.buy(symbol, pair_address, config.MAX_POSITION_USD, price,
                                        mint=mint, extras=stats)

                open_positions[pair_address] = Position(
                    pair_address=pair_address,
                    symbol=symbol,
                    mint=mint or "",
                    entry_price=price,
                    entry_time=time.time(),
                    size_tokens=fill["tokens"],
                    original_size_tokens=fill["tokens"],
                    liquidity_usd=stats.get("liquidity_usd"),
                    volume_5m=stats.get("volume_5m"),
                    buy_sell_ratio=stats.get("buy_sell_ratio"),
                    pair_age_seconds=stats.get("pair_age_seconds"),
                )
                persist()

        # --- manage open positions ---
        if open_positions and now - last_position_poll >= config.POLL_INTERVAL_SECONDS:
            last_position_poll = now
            for pair_address in list(open_positions.keys()):
                pair = client.get_pair("solana", pair_address)
                if not pair:
                    continue
                price = float(pair.get("priceUsd", 0) or 0)
                volume_1m = (pair.get("volume") or {}).get("m5", 0) / 5
                pos = open_positions[pair_address]
                mint = (pair.get("baseToken") or {}).get("address") or pos.mint

                decision = evaluate(pos, price, volume_1m, config)
                print(f"[{pos.symbol}] price={price:.8f} gain={(price/pos.entry_price - 1):.1%} "
                      f"-> {decision.action} ({decision.reason})", flush=True)

                if decision.action == "HOLD":
                    continue

                sell_tokens = pos.size_tokens * decision.fraction
                sell_extras = {
                    "exit_reason": decision.reason,
                    "liquidity_usd": pos.liquidity_usd,
                    "volume_5m": pos.volume_5m,
                    "buy_sell_ratio": pos.buy_sell_ratio,
                    "pair_age_seconds": pos.pair_age_seconds,
                }
                if config.MODE == "LIVE":
                    fill = executor.sell(pos.symbol, mint, sell_tokens, price,
                                         entry_price=pos.entry_price, extras=sell_extras)
                else:
                    fill = executor.sell(pos.symbol, pair_address, sell_tokens, price,
                                         mint=mint, entry_price=pos.entry_price,
                                         extras=sell_extras)

                cost_basis = (sell_tokens / pos.original_size_tokens) * \
                             (pos.entry_price * pos.original_size_tokens)
                realized_pnl_usd += fill["usd"] - cost_basis
                pos.size_tokens -= sell_tokens

                if decision.action == "SELL_ALL" or pos.size_tokens <= 0:
                    del open_positions[pair_address]
                    print(f"[closed] {pos.symbol} | session realized PnL: ${realized_pnl_usd:.2f}", flush=True)
            persist()

        snap = scanner.status_snapshot()
        print(f"[heartbeat] pending={snap['pending']} open={len(open_positions)} "
              f"seen={snap['tokens_seen']} pnl=${realized_pnl_usd:.2f}", flush=True)
        time.sleep(config.HEARTBEAT_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
