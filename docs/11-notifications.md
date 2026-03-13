# Notifications

Telegram alerts are sent for all significant trading events. All sends are fire-and-forget (background thread) and never block the trading loop. A failed notification never crashes the bot.

---

## Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram → copy the token
2. Send `/start` to your bot, then fetch your chat_id:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
3. Set in `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=your_token_here
   TELEGRAM_CHAT_ID=your_chat_id_here
   ```

If either value is missing, the notifier is disabled and all notification calls become no-ops.

---

## Event Types

### Trade Opened

Fired immediately when a position is entered.

```
🟢 TRADE OPENED
NVDA ▲ LONG | SMALL
Qty:   0.5247
Entry: $875.4200
Stop:  $857.2300  (-2.1%)
TP:    $911.6200  (+4.1%)
Size:  $459.35  |  Risk: $9.54
```

Fields: symbol, direction emoji, tier, quantity, entry/stop/TP prices, position size USD, risk dollars.

---

### Trade Closed

Fired when a position exits (any reason).

```
✅ TRADE CLOSED  [take_profit]
NVDA  LONG
Entry → Exit:  $875.4200 → $911.6200
P&L:  +$18.97  (+4.13%)
```

```
❌ TRADE CLOSED  [stop_loss]
EURUSD  SHORT
Entry → Exit:  $1.1501 → $1.1519
P&L:  -$0.07  (-0.16%)
```

Exit reasons: `stop_loss`, `take_profit`, `signal_exit`, `time_exit`.

---

### Hourly Summary

Fired at the top of every hour (HH:00 UTC).

```
📈 HOURLY SUMMARY  [PAPER]
2026-03-13 16:00 UTC
Open positions: 3
Deployed:       $416.58
Available cash: $1183.42
Realized P&L:   +$0.00
Unrealized P&L: +$8.17
Total P&L:      +$8.17
Total equity:   $1600.00
```

- `Deployed`: Capital tied up in open positions
- `Available cash`: Deployable capital minus deployed
- `Realized P&L`: Closed trades today (UTC)
- `Unrealized P&L`: Mark-to-market on open positions (fetched fresh at snapshot time)
- `Total equity`: Available cash + deployed capital

---

### Portfolio Updated

Fired after each weekly or daily universe selection.

```
🔄 PORTFOLIO UPDATED
Stocks (6): CVX, VLO, GLD, JNJ, WMT, COST
Forex  (2): USDJPY, USDCHF
```

---

### Circuit Breaker

Fired when the circuit breaker trips.

```
⚡ CIRCUIT BREAKER TRIPPED
Daily loss exceeded 3.0% of capital.
New entries blocked until next trading day.
```

---

### PDT Warning

Fired when approaching or hitting the Pattern Day Trader limit.

```
⚠️ PDT ALERT: 3/3 day trades used this week.
No further stock day trades this week — swing mode only.
```

```
⚠️ PDT ALERT: 2/3 day trades used this week.
1 day trade(s) remaining. Next stock trade must be swing if this is the last.
```

---

### Event Blackout

Fired when a trade is blocked due to earnings or FOMC.

```
🚫 BLACKOUT: NVDA
Earnings announcement in 90 minutes.
No new entries until blackout lifts.
```

---

### Scan Result

Fired after each scan cycle when there are tradeable signals (score ≥ 55).

```
📡 SCAN RESULT
  NVDA     ▲ 72%
  EURUSD   ▼ 66%
  CVX      ▲ 58%
```

Shows top 5 tradeable signals sorted by score. Only fires if at least one signal qualifies.

---

### Optimizer Ready

Fired when the optimization pipeline completes.

```
🔬 OPT READY: 2 change(s) approved, 1 rejected.
Open dashboard → Optimizer tab to review and apply.
```

---

## Implementation Details

All notification methods live in `TelegramNotifier` (`notifications/telegram.py`).

```python
notifier = TelegramNotifier()

# High-level event methods
notifier.notify_trade_opened(symbol, direction, tier, quantity, entry_price, ...)
notifier.notify_trade_closed(symbol, direction, entry_price, exit_price, quantity, reason)
notifier.notify_daily_summary(open_positions, daily_pnl, total_equity, trading_mode, ...)
notifier.notify_circuit_breaker(reason)
notifier.notify_portfolio_updated(instruments)
notifier.notify_pdt_warning(used, limit)
notifier.notify_event_guard(symbol, reason)
notifier.notify_scan_result(results)
notifier.notify_optimizer_ready(approved, rejected)

# Raw send (any custom message)
notifier.send("custom message text")
```

Every method ultimately calls `send()`, which dispatches a daemon thread:

```python
def send(self, message: str) -> None:
    if not self._enabled:
        return
    t = threading.Thread(target=self._send_blocking, args=(message,), daemon=True)
    t.start()
```

The HTTP POST to `api.telegram.org` has a 10-second timeout. Errors are logged at WARNING/DEBUG but never propagated.
