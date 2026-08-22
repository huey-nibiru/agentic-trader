"""
Configuration for the meme coin scalping bot.
Tune these based on the strategy discussed: minute-scale entries,
tiered take-profit, volume-drop / no-new-high exits, hard time cap.
"""

# ============ MODE ============
# PAPER = simulate fills against live prices, no real funds touched.
# LIVE  = sends real swap transactions via Jupiter using your wallet.
# ALWAYS start and validate in PAPER mode for at least a few sessions.
MODE = "PAPER"  # "PAPER" or "LIVE"

# ============ WALLET (LIVE mode only) ============
# NEVER paste a private key directly into this file or any file you
# might share, commit to git, or upload anywhere.
# Set it as an environment variable instead:
#   export SOLANA_PRIVATE_KEY="your_base58_key_here"
# The bot reads it from the environment at runtime - see executor.py.
RPC_URL = "https://api.mainnet-beta.solana.com"  # consider a paid RPC (Helius, QuickNode) for speed

# ============ POSITION SIZING ============
TOTAL_BANKROLL_USD = 150.0
MAX_POSITION_USD = 25.0          # ~17% of bankroll - more offensive; one rug still survivable
MAX_CONCURRENT_POSITIONS = 10     # more tickets in flight so a runner can appear
DAILY_LOSS_LIMIT_PCT = 0.70      # halt if paper_balance_usd falls to this fraction of bankroll or below
DAILY_LOSS_LIMIT_USD = TOTAL_BANKROLL_USD * DAILY_LOSS_LIMIT_PCT

# ============ ENTRY FILTERS ============
MIN_LIQUIDITY_USD = 10000         # skip anything you can't realistically exit
MIN_PAIR_AGE_SECONDS = 90        # skip the first ~90s (deployer/insider dump window)
MAX_PAIR_AGE_SECONDS = 86400     # keep watching a mint for up to 24 hours
MIN_VOLUME_5M_USD = 5000         # needs real trading activity, not a dead chart
MIN_BUY_SELL_RATIO_5M = 1.7      # buys must meaningfully outweigh sells
DISCOVERY_RECHECK_SECONDS = 60   # wait this long before Dexscreener-checking the same pending mint again
DISCOVERY_MAX_CHECKS_PER_PASS = 20  # cap Dexscreener calls per scan so a 24h pool does not rate-limit us

# ============ EXIT RULES ============
# Let winners run: first clip later and smaller; trail the remainder.
# Paper log showed ~+1-11% scratches and fat left-tail dumps - these knobs
# delay take-profit and tighten the hard stop.
TP_LADDER = [
    (2.0, 0.20),   # at 2x, sell 20% of remaining
    (5.0, 0.30),   # at 5x, sell another 30% of remaining
    (10.0, 0.25),  # at 10x, clip a bit more; trailing stop handles the rest
]

STOP_LOSS_PCT = -0.025            # hard stop: exit at -2.5% 
TRAILING_STOP_PCT = -0.25        # once in profit, give spikes room 

NO_NEW_HIGH_MINUTES = 10         # don't clip a runner for a short pause
NO_NEW_HIGH_MIN_GAIN_PCT = 0.50  # only use the no-new-high exit after already +50%
STAGNANT_GAIN_PCT = 0.15         # "meaningful move" threshold for the time-cap rule
STAGNANT_TIME_CAP_MINUTES = 14   # give names more time to expand
VOLUME_DROP_LOOKBACK_CANDLES = 3 # compare current 1m volume to avg of last N candles
VOLUME_DROP_THRESHOLD = 0.5      # exit if volume falls below 50% of recent avg

# ============ EXECUTION ============
SLIPPAGE_BPS = 1500              # 15% - thin pools need wide tolerance to actually fill (Jupiter path)
PUMPPORTAL_SLIPPAGE_PCT = 15     # same idea, PumpPortal's API takes a plain percent, not bps
PUMPPORTAL_PRIORITY_FEE_SOL = 0.005  # priority fee in SOL for PumpPortal trade-local transactions
PRIORITY_FEE_LAMPORTS = 200000   # aggressive priority fee so exits land fast (Jupiter path)
POLL_INTERVAL_SECONDS = 5        # how often to check open-position prices/candles
DISCOVERY_CHECK_INTERVAL_SECONDS = 5  # how often to check pending PumpPortal mints for maturity
HEARTBEAT_INTERVAL_SECONDS = .25   # console status line while waiting for candidates
SOUND_ALERTS = True              # macOS buy/sell chimes (Glass = green sell, Basso = red sell)
LOG_VIEWER_PORT = 8765           # local live trade-log page (http://127.0.0.1:8765/)
