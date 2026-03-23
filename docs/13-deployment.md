# Deployment

---

## Local Development (Paper Mode)

The simplest setup — no broker connections required.

```bash
# 1. Clone and install
git clone <repo-url> trade-bot
cd trade-bot
pip install -r requirements.txt

# 2. Configure
cp config/.env.example .env
# Edit .env: set TRADING_MODE=paper, optionally add LLM/Telegram keys

# 3. Run
python main.py --mode paper >> logs/paper_$(date +%Y%m%d).log 2>&1 &

# 4. Monitor
tail -f logs/paper_YYYYMMDD.log
```

---

## VPS Setup (Production)

Recommended: Ubuntu 22.04 VPS with 2GB RAM.

### 1. System Dependencies

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git docker.io docker-compose
```

### 2. Clone and Install

```bash
git clone <repo-url> /opt/trade-bot
cd /opt/trade-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment

```bash
cp config/.env.example .env
nano .env
```

Minimum `.env` for paper mode:
```env
TRADING_MODE=paper
TOTAL_CAPITAL=2000
IBKR_CAPITAL=1500
OANDA_CAPITAL=500
GROQ_API_KEY=your_groq_key
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
DATABASE_URL=sqlite:///trade_bot.db
```

### 4. Run as systemd Service

```bash
sudo nano /etc/systemd/system/trade-signet.service
```

```ini
[Unit]
Description=Trade Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/trade-bot
ExecStart=/opt/trade-bot/venv/bin/python main.py --mode paper
Restart=on-failure
RestartSec=30
StandardOutput=append:/opt/trade-bot/logs/paper.log
StandardError=append:/opt/trade-bot/logs/paper.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable trade-signet
sudo systemctl start trade-signet
sudo systemctl status trade-signet
```

---

## Docker

### Build and Run

```bash
# Paper trading bot
docker compose up -d trade-signet

# Dashboard
docker compose up -d dashboard
# Open http://your-server:8050

# View logs
docker compose logs -f trade-signet

# Stop
docker compose down
```

### docker-compose.yml

```yaml
version: '3.8'

services:
  trade-signet:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./trade_bot.db:/app/trade_bot.db
      - ./logs:/app/logs
    command: python main.py --mode paper

  dashboard:
    build: .
    restart: unless-stopped
    env_file: .env
    ports:
      - "8050:8050"
    volumes:
      - ./trade_bot.db:/app/trade_bot.db
    command: python -m uvicorn dashboard_v2:app --host 0.0.0.0 --port 8050
```

### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py", "--mode", "paper"]
```

---

## Broker Setup

### IBKR (Interactive Brokers)

1. Open IB Gateway (not TWS) — lighter and more stable for automated trading
2. Configure API settings in IB Gateway:
   - Enable ActiveX and Socket Clients
   - Socket port: 4002 (paper) or 4001 (live)
   - Allow connections from: `127.0.0.1` (local) or your server IP
3. Set in `.env`:
   ```env
   IBKR_HOST=127.0.0.1   # or Windows host IP from WSL
   IBKR_PORT=4002         # paper: 4002, live: 4001
   IBKR_ACCOUNT_ID=DU123456
   ```

**WSL Note**: From WSL2, IB Gateway runs on Windows. Find the Windows IP:
```bash
cat /etc/resolv.conf | grep nameserver | awk '{print $2}'
# Use this IP as IBKR_HOST
```

### OANDA

1. Open a practice (paper) or live account at oanda.com
2. Generate an API key from the account settings
3. Set in `.env`:
   ```env
   OANDA_API_KEY=your_api_key
   OANDA_ACCOUNT_ID=001-001-12345678-001
   OANDA_ENVIRONMENT=practice   # or live
   ```

---

## Switching to Live Mode

**Warning**: Live mode places real orders. Only switch after thorough paper trading validation.

```bash
# 1. Stop paper bot
sudo systemctl stop trade-signet  # or kill the process

# 2. Update .env
TRADING_MODE=live
IBKR_PORT=4001               # live port
OANDA_ENVIRONMENT=live

# 3. Restart
sudo systemctl start trade-signet
```

The bot will prompt for confirmation on startup in live mode:
```
⚠️  LIVE MODE — real orders will be placed. Confirm? [yes/N]:
```

### Live Mode Checklist

- [ ] Paper traded for at least 2 weeks with satisfactory results
- [ ] Backtest passes all thresholds (Sharpe > 1.2, MaxDD < 20%)
- [ ] IBKR account funded and API permissions enabled
- [ ] OANDA live account active with API key
- [ ] Telegram notifications confirmed working
- [ ] `.env` has correct live ports and environment values
- [ ] Circuit breaker parameters reviewed
- [ ] Position size conservative (reduce `IBKR_CAPITAL` / `OANDA_CAPITAL` to start)

---

## PostgreSQL (Production Database)

For multi-server deployments or better performance:

```bash
# Install PostgreSQL
sudo apt install postgresql postgresql-contrib

# Create database
sudo -u postgres psql
CREATE DATABASE trade_signet;
CREATE USER tradebotuser WITH PASSWORD 'yourpassword';
GRANT ALL PRIVILEGES ON DATABASE trade_signet TO tradebotuser;
\q

# Update .env
DATABASE_URL=postgresql://tradebotuser:yourpassword@localhost/trade_signet
```

The schema is created automatically on first run. No manual migration needed for fresh installs.

---

## Log Rotation

Prevent logs from filling the disk:

```bash
sudo nano /etc/logrotate.d/trade-signet
```

```
/opt/trade-bot/logs/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    dateext
}
```

---

## Updates and Restarts

When deploying code updates:

```bash
cd /opt/trade-bot
git pull

# Check open positions before restarting
sqlite3 trade_bot.db "SELECT symbol, direction, entry_price FROM trades WHERE status='open';"

# Restart (open positions are restored automatically)
sudo systemctl restart trade-signet
```

The bot's `restore_from_db()` mechanism reloads all open positions (with their stop/TP levels) on every startup. No positions are lost during updates.
