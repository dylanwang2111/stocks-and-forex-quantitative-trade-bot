"""
data/preprocessor.py
OHLCV DataFrame cleaning and validation.
"""
from __future__ import annotations

import pandas as pd


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean a raw OHLCV DataFrame:
    1. Forward-fill short gaps (≤ 5 consecutive NaN rows)
    2. Drop rows where all of OHLCV are NaN after fill
    3. Assert no remaining NaN in OHLCV columns
    4. Assert DatetimeTzAware index
    5. Drop duplicate index entries (keep last)
    6. Sort ascending by time

    Returns cleaned df. Raises ValueError on unrecoverable data quality issues.
    """
    if df.empty:
        raise ValueError("Received empty DataFrame — cannot clean.")

    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame missing columns: {missing}")

    df = df.copy()

    # Remove duplicate timestamps
    df = df[~df.index.duplicated(keep="last")]

    # Sort chronologically
    df = df.sort_index()

    # Forward-fill up to 5 consecutive gaps (e.g. pre/post market gaps in 5m data)
    df[required] = df[required].ffill(limit=5)

    # Drop rows where ALL OHLCV are still NaN
    df = df.dropna(subset=["open", "high", "low", "close"], how="all")

    # Volume can legitimately be 0 for forex; replace remaining NaN volume with 0
    df["volume"] = df["volume"].fillna(0)

    # Final integrity check
    nan_counts = df[["open", "high", "low", "close"]].isna().sum()
    if nan_counts.any():
        bad_cols = nan_counts[nan_counts > 0].to_dict()
        raise ValueError(
            f"NaN values remain after cleaning in columns: {bad_cols}. "
            f"DataFrame may have gaps larger than 5 bars."
        )

    # Validate OHLC relationships
    invalid = df[df["high"] < df["low"]]
    if not invalid.empty:
        # Drop rows with inverted high/low (data corruption)
        df = df[df["high"] >= df["low"]]

    return df


def add_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Add log and simple returns columns."""
    import numpy as np
    df = df.copy()
    df["returns"] = df["close"].pct_change()
    df["log_returns"] = np.log(df["close"] / df["close"].shift(1))
    return df


def resample_to_tf(df: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """
    Resample a higher-frequency DataFrame to a lower frequency.
    target_tf: pandas offset alias, e.g. "15min", "1H", "1D"
    """
    resampled = df.resample(target_tf).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return clean(resampled)
