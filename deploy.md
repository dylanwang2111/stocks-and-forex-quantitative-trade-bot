# Deployment Guide

## Quick Start (Docker)

1. Copy `.env.example` to `.env` and fill in your API keys
2. Build and start:
   ```bash
   docker compose up -d tradebot
   ```
3. View logs:
   ```bash
   docker compose logs -f tradebot
   ```
4. Start dashboard (optional):
   ```bash
   docker compose up -d dashboard
   # Open http://your-server-ip:8501
   ```

## VPS Setup (Ubuntu 22.04)

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Clone/upload your project
git clone <your-repo> trade-bot
cd trade-bot
cp .env.example .env
nano .env  # fill in your keys

# Start
docker compose up -d tradebot
```

## Switching to Live Mode

1. Set `TRADING_MODE=live` in `.env`
2. Ensure IBKR Gateway is running (port 4001 for live, 4002 for paper)
3. Restart: `docker compose restart tradebot`

## Useful Commands

```bash
docker compose ps                    # status
docker compose logs -f tradebot      # live logs
docker compose restart tradebot      # restart
docker compose down                  # stop all
docker exec -it trade-bot python3 main.py --mode validate   # run backtest
```

## Data Persistence

Trade data is stored in a Docker volume `trade_data` mounted at `/data/trade_bot.db`.
Back it up with: `docker cp trade-bot:/data/trade_bot.db ./backup.db`

```bash
mkdir -p logs
nohup python main.py --mode paper --log-level INFO > logs/paper_$(date +%Y%m%d).log 2>&1 &
echo "Bot PID: $!"
```