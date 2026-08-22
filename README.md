# Coin Scalp Bot

For automatic new-pair discovery, using liquidity/volume/age as entry filters. Tiered take-profit of (2x/5x/10x). Hard stop-loss, trailing stop, no-new-high timer, volume-drop exit, and a stagnant-position time cap. {Not for aping}

## Files
- `config.py` — every tunable parameter, including `MODE` (`PAPER`/`LIVE`)
- `dexscreener.py` — market data + entry filter checks
- `discovery.py` — continuously scans Dexscreener for new Solana pairs
- `approval.py` — human approval gate (console prompt, or Telegram)
- `strategy.py` — the SL/TP/exit decision engine (fully automatic once a position is open)
- `executor.py` — paper simulation, and live execution via Jupiter + solders signing
- `main.py` — ties discovery → approval → execution → position management together

## Setup
```bash
pip install requests solders base58 websockets
python main.py
```

Runs in **PAPER mode by default** — the real PumpPortal websocket connects
and real Dexscreener maturity checks run, but fills are simulated and
candidates auto-approve, so you can watch the whole pipeline end-to-end
with zero funds at risk. Every trade (paper or live) logs to `trade_log.csv`.

## Discovery: PumpPortal, not Dexscreener's boosted feed

Dexscreener's `token-profiles`/`token-boosts` endpoints for discovery are 
**paid marketing feeds** (tokens whose creators paid for visibility) which is a bad match 
for liquidity-based filters: almost everything on them is a thin, freshly
launched token by design. Discovery now **PumpPortal**
(https://pumpportal.fun), a third-party websocket that
fires the instant a token is created on Pump.fun. Newly-created mints sit
in a pending pool until they cross `MIN_PAIR_AGE_SECONDS`, then get
checked against **real Dexscreener liquidity/volume data** for that mint
(Dexscreener indexes Pump.fun pairs directly) and run through the same
entry filters as before.

Worth knowing: PumpPortal is unofficial and not affiliated with Pump.fun,
has no disclosed team or security audit, and charges a 0.5% fee per trade
executed through its transaction API. It's a widely used, actively
documented tool for this exact purpose, but that's a different level of
trust than Pump.fun's own infrastructure - noting it plainly.

## Going live

1. Run PAPER mode across several sessions first. Read `trade_log.csv` and
   the console output — is discovery finding sane, real candidates? Does
   the SL/TP logic behave the way you expect on real market data?
2. `export SOLANA_PRIVATE_KEY="..."` as an environment variable only.
   Never paste it into a file, never commit it, never share it in chat.
3. `export SOL_USD_PRICE="..."` (a live SOL price — wire this to a real
   feed, e.g. pull it from Dexscreener's SOL/USDC pair, for accurate sizing).
4. Flip `MODE = "LIVE"` in `config.py`.
5. Start the bot. **Discovery and SL/TP management run automatically.**
   Every new buy still stops and asks you first (console prompt by
   default — swap in `TelegramApproval` from `approval.py` if you'd rather
   approve from your phone). Once you approve an entry, exits (partial
   take-profit, stop-loss, trailing stop, etc.) fire automatically without
   further prompts. Live buys/sells route through PumpPortal's
   non-custodial Local Transaction API (`pumpportal.py`), which works
   whether a token is still on its bonding curve or has graduated to
   Raydium/PumpSwap.
6. Start with a fraction of your intended bankroll, not all of it.

## Why buys are gated but sells aren't

New, unvetted pairs are exactly where rugs and honeypots concentrate — a
human glance at liquidity/chart/socials before capital commits is the best
available check the strategy has. Once you're already in a position,
automatic exit management is lower-risk: it's just managing a trade you
knowingly entered, not deciding what to enter next.

## What this bot does NOT do

- **Guarantee anything.** Every threshold in `config.py` is a starting
  assumption based on the strategy we discussed, not a backtested,
  validated edge. Meme coin markets are dominated by rugs, wash trading,
  and adversarial bots with faster infrastructure than a basic script like
  this one. Treat any capital run through this as capital you've already
  decided you can lose completely.
- **Protect you from yourself.** `DAILY_LOSS_LIMIT_USD` and
  `MAX_POSITION_USD` only work if you don't override them mid-session
  because a trade "feels right." Approving every candidate the bot shows
  you defeats the point of the approval step.
