# Configuration

All configuration is loaded from `.env` into typed dataclass settings (`config/settings.py`). Never hardcode values — always use `.env`.

---

## Setup

```bash
cp config/.env.example .env
# Edit .env with your values
```

---

## Full .env Reference

```env
# ── Trading Mode ─────────────────────────────────────────────────────────────
TRADING_MODE=paper           # paper | live

# ── Capital ──────────────────────────────────────────────────────────────────
TOTAL_CAPITAL=2000
IBKR_CAPITAL=1500            # Stocks broker pool
OANDA_CAPITAL=500            # Forex broker pool
CASH_RESERVE_PCT=0.30        # 30% never deployed
RISK_PER_TRADE=0.03          # 3% of broker pool per trade

# ── Trading Parameters ───────────────────────────────────────────────────────
MIN_CONFIDENCE=55            # Entry threshold (SMALL tier)
MAX_POSITIONS=2              # Max concurrent open positions
SWING_HOLDING_DAYS=5         # Force-close after this many days

# ── IBKR ─────────────────────────────────────────────────────────────────────
IBKR_ACCOUNT_ID=             # Your paper/live account number
IBKR_HOST=172.26.128.1       # IBGateway host (WSL: use Windows IP)
IBKR_PORT=4002               # 4002 = IBGateway paper, 7497 = TWS paper, 7496 = TWS live
IBKR_CLIENT_ID=1

# ── OANDA ────────────────────────────────────────────────────────────────────
OANDA_API_KEY=
OANDA_ACCOUNT_ID=
OANDA_ENVIRONMENT=practice   # practice | live

# ── LLM (Cat8 macro signal) ───────────────────────────────────────────────────
GROQ_API_KEY=                # Free tier at console.groq.com
GEMINI_API_KEY=              # Free tier at aistudio.google.com

# ── Notifications ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL=sqlite:///trade_bot.db   # SQLite (default)
# DATABASE_URL=postgresql://user:pass@localhost/tradebot  # PostgreSQL (prod)

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL=INFO
```

---

## Settings Classes

All settings are accessed via the singleton `settings` object:

```python
from config.settings import settings

settings.bot.trading_mode      # "paper" | "live"
settings.bot.total_capital     # float
settings.bot.min_confidence    # int (55)
settings.bot.max_positions     # int (2)
settings.ibkr.host             # str
settings.ibkr.port             # int
settings.oanda.api_key         # str
settings.telegram.enabled      # bool (True if token + chat_id both set)
```

### `BotConfig`

| Field | Default | Description |
|-------|---------|-------------|
| `trading_mode` | `"paper"` | `"paper"` or `"live"` |
| `total_capital` | `2000` | Total account balance |
| `ibkr_capital` | `1500` | IBKR broker pool |
| `oanda_capital` | `500` | OANDA broker pool |
| `cash_reserve_pct` | `0.30` | Fraction never deployed |
| `risk_per_trade` | `0.03` | Max risk per trade (fraction of broker pool) |
| `min_confidence` | `55` | Minimum score to enter (SMALL tier) |
| `max_positions` | `2` | Max simultaneous positions |
| `swing_holding_days` | `5` | Force-close after N days |

### Helper Methods

```python
settings.bot.broker_capital("ibkr")    # → 1500.0
settings.bot.broker_capital("oanda")   # → 500.0
settings.bot.deployable_capital        # → total_capital × (1 - cash_reserve_pct)
```

---

## Capital Model

```
broker_capital(broker)       = IBKR_CAPITAL or OANDA_CAPITAL
cash_reserve(broker)         = broker_capital × CASH_RESERVE_PCT
deployable(broker)           = broker_capital × (1 - CASH_RESERVE_PCT)
max_risk_per_trade(broker)   = broker_capital × RISK_PER_TRADE
available_cash(broker)       = deployable - Σ(open_position_cost_basis)
```

**Example with defaults:**

| | IBKR | OANDA |
|-|------|-------|
| Pool | $1,500 | $500 |
| Reserve (30%) | $450 | $150 |
| Deployable | $1,050 | $350 |
| Max risk/trade (3%) | $33 | $21 |

---

## IBKR Port Reference

| Mode | Application | Port |
|------|-------------|------|
| Paper | IBGateway | 4002 |
| Paper | TWS | 7497 |
| Live | IBGateway | 4001 |
| Live | TWS | 7496 |

The `.env` default uses IBGateway paper (4002). If running TWS, change to 7497.

---

## Running Without Brokers

Both IBKR and OANDA credentials are optional. In paper mode:
- Stocks: Synthetic fills at `entry_price` (no IBKR connection needed)
- Forex: Synthetic fills at `entry_price` (no OANDA connection needed)
- Data: Falls back to yfinance when broker data sources are unavailable

Set `TRADING_MODE=paper` and leave broker fields empty to run fully disconnected.

---

## LLM Configuration

Cat8 (macro/news signal) requires at least one LLM key. Priority order:

1. **Groq** (preferred — faster, free tier): `GROQ_API_KEY`
2. **Gemini** (fallback): `GEMINI_API_KEY`

If neither is set, Cat8 returns a neutral vote (0) and the bot continues without macro intelligence.

The optimization pipeline (`--mode optimize`) requires `GEMINI_API_KEY`.
