"""
Approval layer: every discovered candidate is surfaced here for a yes/no
before a single dollar moves. This is the one required checkpoint between
"bot found a token" and "bot bought a token" in LIVE mode.

Two interfaces provided:
  - ConsoleApproval: simplest, prompts in the terminal you're running the bot in
  - TelegramApproval: approve from your phone via a Telegram bot (needs setup)

Both expose the same interface: approve(pair: dict) -> bool
"""
import os
import time
import requests


class ConsoleApproval:
    """Blocks and asks in the terminal. Good for a first run where you're
    watching the bot directly."""

    def __init__(self, timeout_seconds: int = 60):
        self.timeout_seconds = timeout_seconds

    def approve(self, pair: dict) -> bool:
        symbol = (pair.get("baseToken") or {}).get("symbol", "?")
        price = pair.get("priceUsd", "?")
        liq = (pair.get("liquidity") or {}).get("usd", 0)
        vol5m = (pair.get("volume") or {}).get("m5", 0)
        url = pair.get("url", "")

        print("\n" + "=" * 60)
        print(f"CANDIDATE: {symbol}  |  ${price}")
        print(f"Liquidity: ${liq:,.0f}   5m Volume: ${vol5m:,.0f}")
        print(f"Chart: {url}")
        print("=" * 60)
        try:
            resp = input(f"Buy ${os.environ.get('PREVIEW_SIZE', '?')}? [y/N]: ").strip().lower()
        except EOFError:
            return False
        return resp == "y"


class TelegramApproval:
    """
    Sends candidates to a Telegram chat with Approve/Reject, and polls for
    your response. Requires:
      export TELEGRAM_BOT_TOKEN="..."
      export TELEGRAM_CHAT_ID="..."
    (create a bot via @BotFather, then message it once so you have a chat_id)
    """

    def __init__(self, timeout_seconds: int = 120):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not self.token or not self.chat_id:
            raise RuntimeError(
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars to use TelegramApproval."
            )
        self.timeout_seconds = timeout_seconds
        self.api = f"https://api.telegram.org/bot{self.token}"

    def _send(self, text: str) -> int:
        r = requests.post(f"{self.api}/sendMessage",
                           json={"chat_id": self.chat_id, "text": text}, timeout=10)
        r.raise_for_status()
        return r.json()["result"]["message_id"]

    def _get_replies_since(self, after_update_id: int):
        r = requests.get(f"{self.api}/getUpdates",
                          params={"offset": after_update_id + 1, "timeout": 5}, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])

    def approve(self, pair: dict) -> bool:
        symbol = (pair.get("baseToken") or {}).get("symbol", "?")
        price = pair.get("priceUsd", "?")
        liq = (pair.get("liquidity") or {}).get("usd", 0)
        vol5m = (pair.get("volume") or {}).get("m5", 0)
        url = pair.get("url", "")

        text = (f"New candidate: {symbol} @ ${price}\n"
                f"Liquidity: ${liq:,.0f} | 5m Vol: ${vol5m:,.0f}\n"
                f"{url}\n\nReply 'yes {symbol}' within "
                f"{self.timeout_seconds}s to buy, otherwise it's skipped.")
        self._send(text)

        deadline = time.time() + self.timeout_seconds
        last_update_id = 0
        while time.time() < deadline:
            updates = self._get_replies_since(last_update_id)
            for u in updates:
                last_update_id = max(last_update_id, u["update_id"])
                msg_text = (u.get("message") or {}).get("text", "").strip().lower()
                if msg_text == f"yes {symbol.lower()}":
                    return True
            time.sleep(3)
        return False
