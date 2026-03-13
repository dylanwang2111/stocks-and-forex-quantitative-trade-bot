# Operations

Day-to-day operating guide for running, monitoring, and maintaining the bot.

---

## Starting the Bot

Always log to a date-stamped file:

```bash
python main.py --mode paper >> logs/paper_$(date +%Y%m%d).log 2>&1 &
echo "PID: $!"
```

Verify it started:
```bash
ps aux | grep "python main.py" | grep -v grep
tail -20 logs/paper_YYYYMMDD.log
```

---

## Stopping the Bot

**Always check for open positions before stopping:**

```bash
# Check open positions
sqlite3 trade_bot.db "SELECT id, symbol, direction, entry_price, quantity FROM trades WHERE status='open';"

# Stop the bot
kill <PID>
```

Open positions are NOT closed on shutdown — they persist in the DB and are restored on the next start.

---

## Restarting the Bot

```bash
# 1. Find current PID
ps aux | grep "python main.py" | grep -v grep

# 2. Kill it
kill <PID>
sleep 3

# 3. Restart with log
python main.py --mode paper >> logs/paper_$(date +%Y%m%d).log 2>&1 &

# 4. Verify positions restored
grep "restore_from_db" logs/paper_$(date +%Y%m%d).log | tail -5
```

Expected output after restart:
```
restore_from_db: reloaded GDXJ short entry=133.2100 stop=136.0468 tp=127.5363
restore_from_db: reloaded XLE long entry=57.6000 stop=56.9140 tp=58.9720
Orchestrator.start(): restored 2 open position(s) from DB
```

---

## Monitoring Logs

```bash
# Follow live log
tail -f logs/paper_YYYYMMDD.log

# Check scan cycle summaries
grep "Cycle #" logs/paper_YYYYMMDD.log

# Check signal scores
grep "scan:" logs/paper_YYYYMMDD.log | tail -20

# Check for errors
grep -E "ERROR|CRITICAL|WARNING" logs/paper_YYYYMMDD.log | tail -30

# Check for trades
grep -E "Trade opened|Trade closed|TRADE" logs/paper_YYYYMMDD.log
```

---

## Scan Cycle Log Format

Every 15 minutes, per-instrument scores are logged at INFO level:

```
scan: NVDA  | dir=long  bull=66.7 bear= 0.0 score=66.7 tier=SMALL
scan: EURUSD | dir=short bull= 0.0 bear=55.6 score=55.6 tier=SMALL
scan: XLE   | dir=long  bull=44.4 bear=11.1 score=44.4 tier=NO_TRADE
scan: CVX blocked by CorrelationGuard — CVX is correlated with open position XLE
```

Cycle summary:
```
--- Cycle #9 complete | 62.1s | open=3 daily_pnl=0.00 cb_consecutive=0 skipped=False ---
```

Fields: cycle number, elapsed time, open positions, daily realized P&L, consecutive loss count, whether cycle was skipped (circuit breaker).

---

## Checking Portfolio State

```bash
# Open positions
sqlite3 trade_bot.db "
SELECT symbol, direction, entry_price, quantity, status
FROM trades
WHERE status='open'
ORDER BY entry_time;
"

# Today's closed trades
sqlite3 trade_bot.db "
SELECT symbol, direction, entry_price, exit_price, pnl_usd, exit_reason
FROM trades
WHERE status='closed'
AND date(exit_time) = date('now')
ORDER BY exit_time;
"

# Daily P&L
sqlite3 trade_bot.db "
SELECT ROUND(SUM(pnl_usd), 2) as daily_pnl
FROM trades
WHERE status='closed'
AND date(entry_time) = date('now');
"

# All-time P&L
sqlite3 trade_bot.db "
SELECT ROUND(SUM(pnl_usd), 2) as total_pnl, COUNT(*) as trades
FROM trades
WHERE status='closed';
"
```

---

## Manually Closing a Position

If a position needs to be closed manually (e.g. before a weekend):

```bash
# 1. Get the trade ID
sqlite3 trade_bot.db "SELECT id, symbol, direction, entry_price FROM trades WHERE status='open';"

# 2. Mark it closed in the DB (use current price as exit)
sqlite3 trade_bot.db "
UPDATE trades
SET status='closed',
    exit_price=<CURRENT_PRICE>,
    exit_time=datetime('now'),
    exit_reason='manual',
    pnl_usd=(<CURRENT_PRICE> - entry_price) * quantity,
    pnl_pct=((<CURRENT_PRICE> - entry_price) / entry_price) * 100
WHERE id=<TRADE_ID>;
"

# 3. Restart the bot — the closed position won't be restored
```

For a short position, P&L = (entry_price - exit_price) × quantity.

---

## Checking Active Universe

```bash
# See which instruments are currently being scanned
python3 -c "
from portfolio.watchlist import get_universe_snapshot
for i in get_universe_snapshot():
    print(f'{i.symbol:12} {i.broker:6} {i.asset_type}')
"
```

---

## Troubleshooting

### No trades after several days

1. Check signal scores in logs — are instruments reaching SMALL tier (≥55)?
   ```bash
   grep "tier=SMALL\|tier=MEDIUM\|tier=LARGE\|tier=FULL" logs/paper_YYYYMMDD.log | tail -20
   ```
2. Check if circuit breaker is active:
   ```bash
   grep "CIRCUIT BREAKER\|skipped=True" logs/paper_YYYYMMDD.log | tail -5
   ```
3. Check if correlation guard is blocking everything:
   ```bash
   grep "CorrelationGuard blocked" logs/paper_YYYYMMDD.log | tail -10
   ```
4. Check if max positions is already reached:
   ```bash
   sqlite3 trade_bot.db "SELECT COUNT(*) FROM trades WHERE status='open';"
   ```
5. Check if market is open (scans outside US hours yield no stock data):
   - US stocks: 13:30–20:00 UTC (9:30am–4pm ET)
   - Forex: 24/5 (closed Sat–Sun)

### Unrealized P&L shows $0 or wrong value

This means price fetch failed during the snapshot. Check:
```bash
grep "save_snapshot.*could not fetch\|save_snapshot.*empty price" logs/paper_YYYYMMDD.log
```

If IBKR is disconnected during snapshot time, prices fall back to yfinance. If both fail, unrealized is excluded and a WARNING is logged.

### Bot crashes on startup

```bash
# Check the last error
grep -E "ERROR|CRITICAL|Traceback" logs/paper_YYYYMMDD.log | tail -20
```

Common causes:
- `IBKR_CLIENT_ID already in use` — another process is connected; change `IBKR_CLIENT_ID` in `.env`
- `No module named X` — run `pip install -r requirements.txt`
- `Database locked` — another bot instance is running; kill it first

### Telegram notifications not arriving

1. Verify token and chat_id:
   ```bash
   python3 -c "
   from notifications.telegram import TelegramNotifier
   n = TelegramNotifier()
   n.send('test message')
   import time; time.sleep(2)
   "
   ```
2. Check if notifier is enabled:
   ```bash
   grep "TelegramNotifier" logs/paper_YYYYMMDD.log | head -3
   ```
3. Confirm bot has not been blocked by Telegram (send a message to the bot first)

---

## Routine Maintenance

### Weekly
- Review Telegram summaries — is P&L trending in the right direction?
- Check log for repeated WARNING/ERROR patterns
- Verify IBKR and OANDA connections are stable

### Monthly
- Review all closed trades: win rate, average P&L, exit reasons
- Compare actual performance to backtest expectations
- Consider running `python main.py --mode optimize` if performance degrades
- Check disk space (logs and DB grow over time)

### After Major Market Events
- Check if circuit breaker was tripped
- Review which instruments the portfolio agent selected
- Consider temporarily raising `MIN_CONFIDENCE` for cautious entry during high volatility
