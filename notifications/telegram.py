"""
notifications/telegram.py
Telegram Bot notifications for trade events.

All sends are fire-and-forget (background thread) so they never block
the main trading loop. All errors are swallowed — a failed notification
must never crash the bot.

Setup:
  1. Create a bot via @BotFather on Telegram → copy the token
  2. Send /start to your bot, then fetch your chat_id:
       curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
  3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env

Notifications fired:
  • Trade opened       — symbol, direction, tier, entry/stop/TP, risk $
  • Trade closed       — symbol, entry→exit, P&L $, reason
  • Circuit breaker    — reason, what is blocked
  • Portfolio updated  — new universe after weekly selection
  • Daily summary      — open positions, daily P&L, total equity (hourly)
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING

from config.settings import settings

if TYPE_CHECKING:
    from portfolio.watchlist import Instrument

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sends Telegram messages via the Bot API.

    Enabled only when both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
    Every send is dispatched to a daemon thread — callers never wait for
    network I/O.
    """

    _API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    _TIMEOUT  = 10   # seconds per HTTP request

    def __init__(self) -> None:
        self._token   = settings.telegram.bot_token
        self._chat_id = settings.telegram.chat_id
        self._enabled = settings.telegram.enabled

        if self._enabled:
            logger.info("TelegramNotifier ready (chat_id=%s)", self._chat_id)
        else:
            logger.debug(
                "TelegramNotifier disabled "
                "(set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env to enable)"
            )

    # ------------------------------------------------------------------
    # High-level event methods
    # ------------------------------------------------------------------

    def notify_trade_opened(
        self,
        symbol: str,
        direction: str,
        tier: str,
        quantity: float,
        entry_price: float,
        stop_price: float,
        take_profit_price: float,
        risk_dollars: float,
        position_size_usd: float,
    ) -> None:
        direction_emoji = "🟢" if direction == "long" else "🔴"
        arrow = "▲ LONG" if direction == "long" else "▼ SHORT"
        stop_pct  = abs(entry_price - stop_price)  / entry_price * 100
        tp_pct    = abs(take_profit_price - entry_price) / entry_price * 100
        msg = (
            f"{direction_emoji} TRADE OPENED\n"
            f"{symbol} {arrow} | {tier}\n"
            f"Qty:   {quantity:g}\n"
            f"Entry: ${entry_price:,.4f}\n"
            f"Stop:  ${stop_price:,.4f}  (-{stop_pct:.1f}%)\n"
            f"TP:    ${take_profit_price:,.4f}  (+{tp_pct:.1f}%)\n"
            f"Size:  ${position_size_usd:.2f}  |  Risk: ${risk_dollars:.2f}"
        )
        self.send(msg)

    def notify_trade_closed(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        reason: str,
    ) -> None:
        if direction == "long":
            pnl = (exit_price - entry_price) * quantity
        else:
            pnl = (entry_price - exit_price) * quantity
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        if direction == "short":
            pnl_pct = -pnl_pct

        emoji = "✅" if pnl >= 0 else "❌"
        sign  = "+" if pnl >= 0 else ""
        msg = (
            f"{emoji} TRADE CLOSED  [{reason}]\n"
            f"{symbol}  {'LONG' if direction == 'long' else 'SHORT'}\n"
            f"Entry → Exit:  ${entry_price:,.4f} → ${exit_price:,.4f}\n"
            f"P&L:  {sign}${pnl:.2f}  ({sign}{pnl_pct:.2f}%)"
        )
        self.send(msg)

    def notify_circuit_breaker(self, reason: str) -> None:
        msg = (
            f"⚡ CIRCUIT BREAKER TRIPPED\n"
            f"{reason}\n"
            f"New entries blocked until next trading day."
        )
        self.send(msg)

    def notify_portfolio_updated(self, instruments: list) -> None:
        stocks = [i.symbol for i in instruments if i.asset_type == "stock"]
        forex  = [i.symbol for i in instruments if i.asset_type == "forex"]
        lines  = ["🔄 PORTFOLIO UPDATED"]
        if stocks:
            lines.append(f"Stocks ({len(stocks)}): {', '.join(stocks)}")
        if forex:
            lines.append(f"Forex  ({len(forex)}): {', '.join(forex)}")
        self.send("\n".join(lines))

    def notify_pdt_warning(self, used: int, limit: int) -> None:
        remaining = limit - used
        msg = (
            f"⚠️ PDT ALERT: {used}/{limit} day trades used this week.\n"
            f"{'No further stock day trades this week — swing mode only.' if remaining == 0 else f'{remaining} day trade(s) remaining. Next stock trade must be swing if this is the last.'}"
        )
        self.send(msg)

    def notify_event_guard(self, symbol: str, reason: str) -> None:
        msg = (
            f"🚫 BLACKOUT: {symbol}\n"
            f"{reason}\n"
            f"No new entries until blackout lifts."
        )
        self.send(msg)

    def notify_scan_result(self, results: list[dict]) -> None:
        """
        results: list of dicts with keys: symbol, direction, score
        Only shows top signals (score >= 55).
        """
        tradeable = [r for r in results if r.get("score", 0) >= 55]
        if not tradeable:
            return
        lines = ["📡 SCAN RESULT"]
        for r in sorted(tradeable, key=lambda x: x["score"], reverse=True)[:5]:
            arrow = "▲" if r.get("direction") == "long" else "▼"
            lines.append(f"  {r['symbol']:<8} {arrow} {r['score']:.0f}%")
        self.send("\n".join(lines))

    def notify_optimizer_ready(self, approved: int, rejected: int) -> None:
        msg = (
            f"🔬 OPT READY: {approved} change(s) approved, {rejected} rejected.\n"
            f"Open dashboard → Optimizer tab to review and apply."
        )
        self.send(msg)

    def notify_daily_summary(
        self,
        open_positions: int,
        daily_pnl: float,
        total_equity: float,
        trading_mode: str,
        unrealized_pnl: float = 0.0,
        deployed: float = 0.0,
        available_cash: float = 0.0,
    ) -> None:
        total_pnl = daily_pnl + unrealized_pnl
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        def _fmt(v: float) -> str:
            return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"
        msg = (
            f"{pnl_emoji} HOURLY SUMMARY  [{trading_mode.upper()}]\n"
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Open positions: {open_positions}\n"
            f"Deployed:       ${deployed:.2f}\n"
            f"Available cash: ${available_cash:.2f}\n"
            f"Realized P&L:   {_fmt(daily_pnl)}\n"
            f"Unrealized P&L: {_fmt(unrealized_pnl)}\n"
            f"Total P&L:      {_fmt(total_pnl)}\n"
            f"Total equity:   ${total_equity:.2f}"
        )
        self.send(msg)

    # ------------------------------------------------------------------
    # Raw send (fire-and-forget)
    # ------------------------------------------------------------------

    def send(self, message: str) -> None:
        """
        Send a plain-text message. Dispatches to a background thread.
        Safe to call from any thread — never raises, never blocks.
        """
        if not self._enabled:
            return
        t = threading.Thread(
            target=self._send_blocking,
            args=(message,),
            daemon=True,
            name="TelegramSend",
        )
        t.start()

    def _send_blocking(self, message: str) -> None:
        """HTTP POST to Telegram API. Runs in a daemon thread."""
        url  = self._API_URL.format(token=self._token)
        data = json.dumps({
            "chat_id":    self._chat_id,
            "text":       message,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Telegram send failed: HTTP %d", resp.status
                    )
                else:
                    logger.debug("Telegram message sent (%d chars)", len(message))
        except urllib.error.HTTPError as exc:
            logger.warning("Telegram HTTP error: %d %s", exc.code, exc.reason)
        except Exception as exc:
            logger.debug("Telegram send error: %s", exc)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def test_telegram_notifier() -> None:
    """
    Smoke-test: verifies notifier is constructed correctly and that
    send() is a no-op when disabled (no credentials set).
    Only sends a real message if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set.
    """
    import os, time

    print("=== test_telegram_notifier ===")

    notifier = TelegramNotifier()

    if not notifier._enabled:
        print("Notifier disabled (no credentials) — testing no-op path")
        # Should not raise
        notifier.send("this should be silently dropped")
        notifier.notify_trade_opened(
            symbol="TEST", direction="long", tier="SMALL",
            quantity=10, entry_price=1.08, stop_price=1.064,
            take_profit_price=1.112, risk_dollars=1.60,
            position_size_usd=83.16,
        )
        print("PASS: no-op when disabled")
    else:
        print("Credentials found — sending live test message...")
        notifier.send(
            "🤖 Trade Bot test message\n"
            f"Sent at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        time.sleep(2)   # let the background thread finish
        print("PASS: message dispatched (check your Telegram)")

    print("test_telegram_notifier: DONE")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_telegram_notifier()
