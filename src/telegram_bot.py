"""
Telegram Bot for trade alerts and daily summaries.
Uses python-telegram-bot for async communication.
"""
import asyncio
from config.settings import settings


class TelegramNotifier:
    def __init__(self):
        self.bot = None
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.enabled = bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)
        if self.enabled:
            from telegram import Bot
            self.bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

    def send_message(self, text: str):
        """Send text message to configured chat."""
        if not self.enabled:
            print(f"[TELEGRAM] {text}")
            return

        try:
            asyncio.get_running_loop().create_task(self._send(text))
        except RuntimeError:
            print(f"[TELEGRAM] {text}")

    async def _send(self, text: str):
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
            )
        except Exception as e:
            print(f"Telegram error: {e}")

    async def send_daily_summary_async(self, summary_text: str):
        """Async wrapper for daily summary."""
        await self._send(f"<b>📊 Daily Summary</b>\n\n{summary_text}")


telegram = TelegramNotifier()