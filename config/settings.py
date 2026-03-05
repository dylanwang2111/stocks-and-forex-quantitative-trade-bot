"""
config/settings.py
Central typed configuration loaded from .env
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root or config/
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT / "config" / ".env")


@dataclass
class IbkrConfig:
    host: str = field(default_factory=lambda: os.getenv("IBKR_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("IBKR_PORT", "7497")))
    client_id: int = field(default_factory=lambda: int(os.getenv("IBKR_CLIENT_ID", "1")))
    # Paper trading port: 7497  |  Live trading port: 7496
    account_id: str = field(default_factory=lambda: os.getenv("IBKR_ACCOUNT_ID", ""))

    @property
    def enabled(self) -> bool:
        return bool(self.account_id)

    @property
    def is_paper(self) -> bool:
        return self.port == 7497


@dataclass
class OandaConfig:
    api_key: str = field(default_factory=lambda: os.getenv("OANDA_API_KEY", ""))
    account_id: str = field(default_factory=lambda: os.getenv("OANDA_ACCOUNT_ID", ""))
    environment: str = field(
        default_factory=lambda: os.getenv("OANDA_ENVIRONMENT", "practice")
    )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.account_id)


@dataclass
class GroqConfig:
    api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    model: str = "llama-3.3-70b-versatile"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class GeminiConfig:
    api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    model: str = "gemini-2.0-flash-exp"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class TelegramConfig:
    bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str   = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass
class BotConfig:
    trading_mode: str = field(
        default_factory=lambda: os.getenv("TRADING_MODE", "paper")
    )
    min_confidence: float = field(
        default_factory=lambda: float(os.getenv("MIN_CONFIDENCE", "55"))
    )
    max_positions: int = field(
        default_factory=lambda: int(os.getenv("MAX_POSITIONS", "2"))
    )
    cash_reserve_pct: float = field(
        default_factory=lambda: float(os.getenv("CASH_RESERVE_PCT", "0.30"))
    )
    total_capital: float = field(
        default_factory=lambda: float(os.getenv("TOTAL_CAPITAL", "500"))
    )
    risk_per_trade: float = field(
        default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.01"))
    )
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///trade_bot.db")
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )

    @property
    def cash_reserve(self) -> float:
        return self.total_capital * self.cash_reserve_pct

    @property
    def deployable_capital(self) -> float:
        return self.total_capital - self.cash_reserve


@dataclass
class Settings:
    ibkr: IbkrConfig = field(default_factory=IbkrConfig)
    oanda: OandaConfig = field(default_factory=OandaConfig)
    groq: GroqConfig = field(default_factory=GroqConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    bot: BotConfig = field(default_factory=BotConfig)


# Singleton
settings = Settings()
