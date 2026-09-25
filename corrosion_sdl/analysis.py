"""Deciding when an open-circuit potential has settled.

This is the one piece of genuine feedback logic in Phase 1. Every other decision
the software makes is scripted; this one is made from live data.

A freshly immersed coupon does not sit at a steady potential. The double layer
forms, the passive film adjusts, and the measured potential drifts -- quickly at
first, then more slowly. Only once it has settled is the value meaningful, and
only then are EIS and PDP measuring what they are supposed to measure, since
both are referenced to it.

A fixed timer cannot capture that, because different coupons settle at different
rates. So instead:

    fit the drift over a trailing window
    stop when |drift| < threshold

Three guards stop a quiet patch of noise being mistaken for convergence:

1. **A full window is required.** Stability cannot be declared until the
   trailing window is completely populated, so a calm first thirty seconds
   cannot end the measurement.
2. **A minimum sample count is required.** A slope through two noisy points
   means nothing.
3. **A minimum elapsed time is required**, set in `OCPParams` and checked here,
   independent of the window.

This module has no hardware dependency, which is why it is fully implemented and
exhaustively testable before anything is delivered.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .types import OCPParams

#: Fewest samples that may be used for a slope fit.
MIN_SAMPLES = 10


@dataclass(frozen=True)
class Stability:
    """A snapshot of the stability calculation at one instant.

    Attributes:
        stable: Whether every condition is currently satisfied.
        drift_mv_per_min: Fitted drift over the window. None if not yet fittable.
        window_s: Time actually spanned by the buffered samples.
        samples: How many samples are in the window.
        reason: Plain-language explanation, recorded in metadata so a reviewer
            can see why the measurement stopped when it did.
    """

    stable: bool
    drift_mv_per_min: float | None
    window_s: float
    samples: int
    reason: str


class OCPStabilityMonitor:
    """Judges whether an open-circuit potential has settled.

    Fed one reading at a time. Passed to the potentiostat as the `should_stop`
    callable, which keeps acquisition timing in the driver and this judgement
    here, where it can be tested without an instrument.

    Args:
        params: Supplies the window, threshold, and minimum duration.

    Example:
        >>> monitor = OCPStabilityMonitor(OCPParams())
        >>> monitor.update(0.0, -0.221)
        False
    """

    def __init__(self, params: OCPParams) -> None:
        self.params = params
        self._samples: deque[tuple[float, float]] = deque()
        self._last: Stability | None = None

    # -- feeding ----------------------------------------------------------- #

    def update(self, elapsed_s: float, potential_v: float) -> bool:
        """Add a reading and return whether the potential is now settled.

        This is the `should_stop` callable handed to the potentiostat.

        Args:
            elapsed_s: Seconds since the measurement began.
            potential_v: Measured potential, in volts.

        Returns:
            True when every stability condition is satisfied.
        """
        self._samples.append((elapsed_s, potential_v))
        cutoff = elapsed_s - self.params.stability_window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        self._last = self.assess(elapsed_s)
        return self._last.stable

    # -- assessing --------------------------------------------------------- #

    def assess(self, now_s: float) -> Stability:
        """Evaluate the current state without adding a sample.

        Args:
            now_s: Current elapsed time, for the minimum-duration check.

        Returns:
            A full assessment, including the fitted drift.
        """
        n = len(self._samples)
        span = self._samples[-1][0] - self._samples[0][0] if n >= 2 else 0.0

        if n < MIN_SAMPLES:
            return Stability(
                False, None, span, n,
                f"only {n} samples; {MIN_SAMPLES} needed to fit a slope",
            )

        # A partly filled window would fit the slope over less history than the
        # criterion asks for, so refuse until it is genuinely full.
        if span < self.params.stability_window_s * 0.98:
            return Stability(
                False, self._drift(), span, n,
                f"window only {span:.0f} s of "
                f"{self.params.stability_window_s:.0f} s required",
            )

        if now_s < self.params.min_duration_s:
            return Stability(
                False, self._drift(), span, n,
                f"elapsed {now_s:.0f} s is below the "
                f"{self.params.min_duration_s:.0f} s minimum",
            )

        drift = self._drift()
        if drift is None:
            return Stability(False, None, span, n, "drift could not be fitted")

        limit = self.params.stability_threshold_mv_per_min
        if abs(drift) < limit:
            return Stability(
                True, drift, span, n,
                f"drift {drift:+.4f} mV/min is within +/-{limit} mV/min",
            )
        return Stability(
            False, drift, span, n,
            f"drift {drift:+.4f} mV/min exceeds +/-{limit} mV/min",
        )

    def _drift(self) -> float | None:
        """Return the least-squares drift over the window, in mV/min.

        Closed-form ordinary least squares. Returns None if the samples are
        degenerate -- all at one timestamp -- which would otherwise divide by
        zero.
        """
        n = len(self._samples)
        if n < 2:
            return None

        mean_t = sum(t for t, _ in self._samples) / n
        mean_v = sum(v for _, v in self._samples) / n
        num = sum((t - mean_t) * (v - mean_v) for t, v in self._samples)
        den = sum((t - mean_t) ** 2 for t, _ in self._samples)
        if den == 0.0:
            return None

        return (num / den) * 1000.0 * 60.0      # V/s -> mV/min

    # -- reporting --------------------------------------------------------- #

    @property
    def last(self) -> Stability | None:
        """Return the most recent assessment, or None before any update."""
        return self._last

    def summary(self) -> dict[str, object]:
        """Return a metadata-ready summary of the final stability state.

        Merged into the OCP measurement's `derived` values, so the record shows
        not just the final potential but the drift that justified stopping.
        """
        a = self._last
        if a is None:
            return {"stability_evaluated": False}
        return {
            "stability_evaluated": True,
            "stable_at_end": a.stable,
            "final_drift_mv_per_min": a.drift_mv_per_min,
            "window_span_s": a.window_s,
            "window_sample_count": a.samples,
            "stability_reason": a.reason,
            "threshold_mv_per_min": self.params.stability_threshold_mv_per_min,
        }

    def reset(self) -> None:
        """Clear all buffered samples, so the monitor can be reused."""
        self._samples.clear()
        self._last = None
