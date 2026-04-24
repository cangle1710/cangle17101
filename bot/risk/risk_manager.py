"""Global and per-trader risk controls.

Checks, in order:
  1. Kill-switches (tripped by prior events) — both global and per-trader.
  2. Per-trader drawdown / consecutive-loss cutoffs.
  3. Weekly drawdown stop (absolute halt).
  4. Daily soft stop (no new entries; existing positions still managed).
  5. Global exposure and open-position caps.

The RiskManager is *read-only* with respect to positions and equity. The
PortfolioManager feeds it state. Decisions come back as allow/deny plus a
reason code.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..core.config import RiskConfig
from ..core.models import TraderStats

log = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86_400
_SECONDS_PER_WEEK = 604_800


@dataclass
class RiskSnapshot:
    bankroll: float  # deployable capital
    current_equity: float  # realized + unrealized relative to start
    start_of_day_equity: float
    start_of_week_equity: float
    open_exposure: float  # USDC in open positions
    open_positions: int


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    detail: dict

    @classmethod
    def allow(cls, **detail) -> "RiskDecision":
        return cls(True, "allowed", detail)

    @classmethod
    def deny(cls, reason: str, **detail) -> "RiskDecision":
        return cls(False, reason, detail)


class RiskManager:
    def __init__(self, config: RiskConfig, *, kill_switch_file: Optional[str] = None):
        self._cfg = config
        self._global_halt = False
        self._halt_reason: Optional[str] = None
        # wallet -> reason when cut off
        self._trader_cutoffs: dict[str, str] = {}
        # Path checked on every entry; if it exists, trading is paused.
        # Note: this is *not* a persisted global_halt (which requires
        # operator action in the DB to clear). It's a soft pause controlled
        # from outside the process.
        self._kill_switch_file = kill_switch_file or None

    def hydrate(
        self,
        *,
        global_halt_reason: Optional[str] = None,
        cutoffs: Optional[dict[str, str]] = None,
    ) -> None:
        """Restore persisted state on startup."""
        if global_halt_reason:
            self._global_halt = True
            self._halt_reason = global_halt_reason
            log.warning("Restored global halt: %s", global_halt_reason)
        for w, r in (cutoffs or {}).items():
            self._trader_cutoffs[w.lower()] = r
            log.warning("Restored trader cutoff %s: %s", w, r)

    # ----- state mutation -----

    def trip_global(self, reason: str) -> None:
        if not self._global_halt:
            log.error("GLOBAL RISK HALT: %s", reason)
        self._global_halt = True
        self._halt_reason = reason

    def cutoff_trader(self, wallet: str, reason: str) -> None:
        wallet = wallet.lower()
        if wallet not in self._trader_cutoffs:
            log.warning("Cutting off trader %s: %s", wallet, reason)
        self._trader_cutoffs[wallet] = reason

    def reset_trader(self, wallet: str) -> None:
        self._trader_cutoffs.pop(wallet.lower(), None)

    # ----- state read -----

    def trader_is_cutoff(self, wallet: str) -> bool:
        return wallet.lower() in self._trader_cutoffs

    def cutoff_count(self) -> int:
        return len(self._trader_cutoffs)

    def cutoffs(self) -> dict[str, str]:
        return dict(self._trader_cutoffs)

    @property
    def global_halted(self) -> bool:
        return self._global_halt

    # ----- evaluation of new trader state -----

    def evaluate_trader_stats(self, stats: TraderStats) -> Optional[str]:
        """If the trader's rolling stats trip a cutoff, record it and
        return the reason; else return None."""
        if stats.consecutive_losses >= self._cfg.trader_consecutive_loss_cutoff:
            reason = f"{stats.consecutive_losses}_consec_losses"
            self.cutoff_trader(stats.wallet, reason)
            return reason
        if stats.max_drawdown >= self._cfg.trader_drawdown_cutoff_pct:
            reason = f"dd_{stats.max_drawdown:.2%}"
            self.cutoff_trader(stats.wallet, reason)
            return reason
        return None

    def evaluate_portfolio(self, snap: RiskSnapshot) -> None:
        """Trip global halts based on portfolio-level drawdowns."""
        # Weekly drawdown stop: compare equity to start-of-week equity.
        if snap.start_of_week_equity > 0:
            week_dd = (snap.start_of_week_equity - snap.current_equity) / snap.start_of_week_equity
            if week_dd >= self._cfg.weekly_drawdown_stop_pct:
                self.trip_global(f"weekly_dd_{week_dd:.2%}")

    # ----- entry gate -----

    def kill_switch_active(self) -> bool:
        if not self._kill_switch_file:
            return False
        import os
        return os.path.exists(self._kill_switch_file)

    def check_entry(
        self,
        *,
        wallet: str,
        proposed_notional: float,
        snap: RiskSnapshot,
        group: Optional[str] = None,
        group_exposure: float = 0.0,
    ) -> RiskDecision:
        if self.kill_switch_active():
            return RiskDecision.deny(
                "kill_switch_file", path=self._kill_switch_file,
            )
        if self._global_halt:
            return RiskDecision.deny(
                "global_halt", halt_reason=self._halt_reason,
            )

        if self.trader_is_cutoff(wallet):
            return RiskDecision.deny(
                "trader_cutoff",
                trader_reason=self._trader_cutoffs[wallet.lower()],
            )

        # Daily soft stop: block new entries only.
        if snap.start_of_day_equity > 0:
            day_dd = (snap.start_of_day_equity - snap.current_equity) / snap.start_of_day_equity
            if day_dd >= self._cfg.daily_soft_stop_pct:
                return RiskDecision.deny("daily_soft_stop", day_dd=day_dd)

        if snap.open_positions >= self._cfg.max_open_positions:
            return RiskDecision.deny(
                "too_many_positions",
                open=snap.open_positions,
                cap=self._cfg.max_open_positions,
            )

        if snap.bankroll > 0:
            projected_exposure = snap.open_exposure + proposed_notional
            max_exposure = snap.bankroll * self._cfg.max_global_exposure_pct
            if projected_exposure > max_exposure:
                return RiskDecision.deny(
                    "global_exposure_cap",
                    projected=projected_exposure, cap=max_exposure,
                )

        # Correlation-group cap: if the caller has classified this signal
        # into a group with other open positions, ensure projected
        # per-group notional stays below the configured fraction.
        if group is not None and snap.bankroll > 0:
            group_cap = snap.bankroll * self._cfg.max_pct_per_correlation_group
            projected_group = group_exposure + proposed_notional
            if projected_group > group_cap:
                return RiskDecision.deny(
                    "correlation_group_cap",
                    group=group, projected=projected_group, cap=group_cap,
                )

        return RiskDecision.allow()


def start_of_day(ts: Optional[float] = None) -> float:
    ts = ts if ts is not None else time.time()
    return ts - (ts % _SECONDS_PER_DAY)


def start_of_week(ts: Optional[float] = None) -> float:
    ts = ts if ts is not None else time.time()
    # align to Monday UTC
    t = time.gmtime(ts)
    day_offset = t.tm_wday  # 0 = Monday
    midnight = ts - (ts % _SECONDS_PER_DAY)
    return midnight - day_offset * _SECONDS_PER_DAY
