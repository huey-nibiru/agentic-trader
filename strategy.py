"""
The decision engine: given a live position and fresh market data,
decides whether to hold, partially exit, or fully exit.

This encodes the rules from our conversation:
- tiered take-profit ladder
- hard stop-loss
- trailing stop once in profit
- no-new-high timer
- volume-drop exit
- stagnant time cap
"""
import time
from dataclasses import dataclass, field


@dataclass
class Position:
    pair_address: str
    symbol: str
    entry_price: float
    entry_time: float                  # unix timestamp
    size_tokens: float                 # remaining token amount held
    original_size_tokens: float        # for computing % sold vs TP ladder
    mint: str = ""                     # token contract; kept so sells work after restart
    liquidity_usd: float = None        # Dexscreener snapshot at entry (for the trade log)
    volume_5m: float = None
    buy_sell_ratio: float = None
    pair_age_seconds: float = None
    peak_price: float = None
    last_new_high_time: float = None
    tp_ladder_hit: list = field(default_factory=list)  # which TP tiers already executed
    volume_history: list = field(default_factory=list)  # recent 1m volumes for drop detection

    def __post_init__(self):
        if self.peak_price is None:
            self.peak_price = self.entry_price
        if self.last_new_high_time is None:
            self.last_new_high_time = self.entry_time


@dataclass
class Decision:
    action: str          # "HOLD" | "SELL_PARTIAL" | "SELL_ALL"
    fraction: float       # fraction of CURRENT remaining position to sell (0-1)
    reason: str


def evaluate(position: Position, current_price: float, current_volume_1m: float, cfg) -> Decision:
    now = time.time()
    gain_pct = (current_price - position.entry_price) / position.entry_price
    gain_multiple = current_price / position.entry_price

    # --- update peak / new-high tracking ---
    if current_price > position.peak_price:
        position.peak_price = current_price
        position.last_new_high_time = now

    # --- track volume history for drop detection ---
    position.volume_history.append(current_volume_1m)
    lookback = cfg.VOLUME_DROP_LOOKBACK_CANDLES
    position.volume_history = position.volume_history[-(lookback + 1):]

    # 1) HARD STOP LOSS - always checked first, overrides everything
    if gain_pct <= cfg.STOP_LOSS_PCT:
        return Decision("SELL_ALL", 1.0, f"hard stop loss hit ({gain_pct:.1%})")

    # 2) TRAILING STOP - only once we've been in meaningful profit
    if position.peak_price > position.entry_price:
        drawdown_from_peak = (current_price - position.peak_price) / position.peak_price
        if drawdown_from_peak <= cfg.TRAILING_STOP_PCT:
            return Decision("SELL_ALL", 1.0,
                             f"trailing stop hit ({drawdown_from_peak:.1%} from peak)")

    # 3) TAKE-PROFIT LADDER
    for tier_gain, tier_fraction in cfg.TP_LADDER:
        if gain_multiple >= tier_gain and tier_gain not in position.tp_ladder_hit:
            position.tp_ladder_hit.append(tier_gain)
            return Decision("SELL_PARTIAL", tier_fraction,
                             f"TP tier {tier_gain}x hit, selling {tier_fraction:.0%} of remaining")

    # 4) VOLUME DROP - only meaningful once we have enough history
    if len(position.volume_history) > lookback:
        recent_avg = sum(position.volume_history[:-1]) / lookback
        if recent_avg > 0 and current_volume_1m < recent_avg * cfg.VOLUME_DROP_THRESHOLD:
            return Decision("SELL_ALL", 1.0,
                             f"volume dropped to {current_volume_1m:.0f} vs avg {recent_avg:.0f}")

    # 5) NO NEW HIGH IN N MINUTES — only once already up enough that a pause
    #    looks like distribution, not like the trade never started.
    minutes_since_high = (now - position.last_new_high_time) / 60
    min_gain = getattr(cfg, "NO_NEW_HIGH_MIN_GAIN_PCT", 0.0)
    if gain_pct >= min_gain and minutes_since_high >= cfg.NO_NEW_HIGH_MINUTES:
        return Decision("SELL_ALL", 1.0,
                         f"no new high in {minutes_since_high:.1f} min")

    # 6) STAGNANT TIME CAP - hasn't moved meaningfully, don't let capital sit dead
    minutes_held = (now - position.entry_time) / 60
    if minutes_held >= cfg.STAGNANT_TIME_CAP_MINUTES and gain_pct < cfg.STAGNANT_GAIN_PCT:
        return Decision("SELL_ALL", 1.0,
                         f"stagnant: only {gain_pct:.1%} after {minutes_held:.1f} min")

    return Decision("HOLD", 0.0, f"holding, gain={gain_pct:.1%}, peak_dd checked, no trigger")
