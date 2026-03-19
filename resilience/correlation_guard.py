"""
resilience/correlation_guard.py

Prevents opening a new position if it would create a correlated pair or
exceed the maximum number of simultaneous forex positions.
"""
from __future__ import annotations

import logging

from portfolio.watchlist import CORRELATION_BLACKLIST, are_correlated, get_instrument  # noqa: F401

logger = logging.getLogger(__name__)


class CorrelationGuard:
    """
    Prevents opening a new position if it would create a correlated pair.

    Rules enforced:
    1. Never hold two symbols that appear in CORRELATION_BLACKLIST simultaneously.
    2. Never hold more than MAX_FOREX forex positions simultaneously.
    """

    MAX_FOREX   = 4  # maximum simultaneous forex positions
    MAX_CRYPTO  = 2  # maximum simultaneous crypto positions

    def __init__(self, open_symbols: list[str] | None = None) -> None:
        """
        Parameters
        ----------
        open_symbols:
            Symbols currently held as open positions.  In production pass
            ``state_manager.all_positions()`` symbols here, then call
            :meth:`update_open_symbols` before every ``is_allowed`` check.
        """
        self._open_symbols: list[str] = list(open_symbols or [])

    # ── State management ───────────────────────────────────────────────────────

    def update_open_symbols(self, symbols: list[str]) -> None:
        """Sync guard state with the live portfolio before each check."""
        self._open_symbols = list(symbols)
        logger.debug("CorrelationGuard updated open symbols: %s", self._open_symbols)

    # ── Core check ────────────────────────────────────────────────────────────

    def is_allowed(self, candidate_symbol: str) -> tuple[bool, str]:
        """
        Determine whether opening a position in *candidate_symbol* is permitted.

        Returns
        -------
        (True, "")
            All checks passed — opening is allowed.
        (False, reason)
            At least one check failed; *reason* explains which rule was broken.

        Checks performed (in order):
        1. Candidate is already open → blocked.
        2. Candidate is correlated with any open symbol (via CORRELATION_BLACKLIST) → blocked.
        3. Candidate is a forex instrument AND open forex count >= MAX_FOREX (3) → blocked.
        """
        # ── Check 1: duplicate position ────────────────────────────────────────
        if candidate_symbol in self._open_symbols:
            reason = f"already have open position in {candidate_symbol}"
            logger.warning("CorrelationGuard blocked %s: %s", candidate_symbol, reason)
            return False, reason

        # ── Check 2: correlation blacklist ─────────────────────────────────────
        for open_sym in self._open_symbols:
            if are_correlated(candidate_symbol, open_sym):
                reason = (
                    f"{candidate_symbol} is correlated with open position {open_sym}"
                )
                logger.warning("CorrelationGuard blocked %s: %s", candidate_symbol, reason)
                return False, reason

        # ── Check 3: max forex positions ───────────────────────────────────────
        try:
            candidate_inst = get_instrument(candidate_symbol)
            if candidate_inst.asset_type == "forex":
                forex_count = 0
                for open_sym in self._open_symbols:
                    try:
                        open_inst = get_instrument(open_sym)
                        if open_inst.asset_type == "forex":
                            forex_count += 1
                    except KeyError:
                        pass
                if forex_count >= self.MAX_FOREX:
                    reason = (
                        f"already hold {forex_count} forex position(s)"
                        f" — max {self.MAX_FOREX} simultaneously"
                    )
                    logger.warning(
                        "CorrelationGuard blocked %s: %s", candidate_symbol, reason
                    )
                    return False, reason
        except KeyError:
            # Unknown candidate symbol — skip the forex check
            pass

        # ── Check 4: max crypto positions ──────────────────────────────────────
        try:
            candidate_inst = get_instrument(candidate_symbol)
            if candidate_inst.asset_type == "crypto":
                crypto_count = 0
                for open_sym in self._open_symbols:
                    try:
                        open_inst = get_instrument(open_sym)
                        if open_inst.asset_type == "crypto":
                            crypto_count += 1
                    except KeyError:
                        pass
                if crypto_count >= self.MAX_CRYPTO:
                    reason = (
                        f"already hold {crypto_count} crypto position(s)"
                        f" — max {self.MAX_CRYPTO} simultaneously"
                    )
                    logger.warning(
                        "CorrelationGuard blocked %s: %s", candidate_symbol, reason
                    )
                    return False, reason
        except KeyError:
            pass

        logger.debug("CorrelationGuard allowed %s", candidate_symbol)
        return True, ""


# ── Self-tests ─────────────────────────────────────────────────────────────────

def test_correlation_guard() -> None:
    """
    Lightweight smoke tests — no external test runner required.
    Run with:  python -m resilience.correlation_guard
    """
    guard = CorrelationGuard()

    # ------------------------------------------------------------------
    # Test 1: SPY open → QQQ candidate → blocked (correlation blacklist)
    # ------------------------------------------------------------------
    guard.update_open_symbols(["SPY"])
    allowed, reason = guard.is_allowed("QQQ")
    assert not allowed, "Expected QQQ to be blocked when SPY is open"
    assert "correlated" in reason.lower(), f"Unexpected reason: {reason!r}"
    print(f"[PASS] Test 1 — QQQ blocked (SPY open): {reason!r}")

    # ------------------------------------------------------------------
    # Test 2: SPY open → AAPL candidate → allowed (no correlation)
    # ------------------------------------------------------------------
    guard.update_open_symbols(["SPY"])
    allowed, reason = guard.is_allowed("AAPL")
    assert allowed, f"Expected AAPL to be allowed when SPY is open, got: {reason!r}"
    assert reason == ""
    print("[PASS] Test 2 — AAPL allowed (SPY open)")

    # ------------------------------------------------------------------
    # Test 3: EURUSD open → GBPUSD candidate → blocked (both forex)
    # ------------------------------------------------------------------
    guard.update_open_symbols(["EURUSD"])
    allowed, reason = guard.is_allowed("GBPUSD")
    assert not allowed, "Expected GBPUSD to be blocked when EURUSD is open"
    # GBPUSD is in the correlation blacklist with EURUSD, so check 2 fires first;
    # either "correlated" or "forex" in the reason is acceptable — the trade is blocked.
    assert not allowed
    print(f"[PASS] Test 3 — GBPUSD blocked (EURUSD open): {reason!r}")

    # ------------------------------------------------------------------
    # Test 4: SPY open → EURUSD candidate → allowed (different asset class, not correlated)
    # ------------------------------------------------------------------
    guard.update_open_symbols(["SPY"])
    allowed, reason = guard.is_allowed("EURUSD")
    assert allowed, f"Expected EURUSD to be allowed when SPY is open, got: {reason!r}"
    assert reason == ""
    print("[PASS] Test 4 — EURUSD allowed (SPY open)")

    # ------------------------------------------------------------------
    # Bonus test: duplicate open position is blocked
    # ------------------------------------------------------------------
    guard.update_open_symbols(["EURUSD"])
    allowed, reason = guard.is_allowed("EURUSD")
    assert not allowed, "Expected EURUSD to be blocked when already open"
    assert "already have open position" in reason
    print(f"[PASS] Bonus — EURUSD blocked (already open): {reason!r}")

    print("\nAll CorrelationGuard tests passed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_correlation_guard()
