"""The vocabulary of the system.

Small on purpose. Everything here is either an enum or a plain record. No
behaviour beyond validation, and no imports from anywhere else in the package,
so this module can never take part in a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    """Return the current time as timezone-aware UTC.

    Used for every timestamp in the system. Local naive time is avoided because
    a 30-hour run crosses midnight, and twice a year it would cross a
    daylight-saving boundary and produce timestamps that go backwards.
    """
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #

class MeasurementType(str, Enum):
    """The three electrochemical measurements, in the order they must run."""

    OCP = "OCP"   #: Open circuit potential. Non-destructive. Always first.
    EIS = "EIS"   #: Impedance spectroscopy. Non-destructive. Needs OCP first.
    PDP = "PDP"   #: Potentiodynamic polarisation. DESTRUCTIVE. Always last.


#: The mandated order. PDP is last because it destroys the coupon surface:
#: once it has run, no further measurement on that sample means anything.
MEASUREMENT_ORDER: tuple[MeasurementType, ...] = (
    MeasurementType.OCP,
    MeasurementType.EIS,
    MeasurementType.PDP,
)

#: Column layouts, fixed per measurement type. Every CSV of a given type is
#: shaped identically, whichever instrument produced it.
COLUMNS: dict[MeasurementType, tuple[str, ...]] = {
    MeasurementType.OCP: ("elapsed_s", "potential_v"),
    MeasurementType.EIS: (
        "frequency_hz", "z_real_ohm", "z_imag_ohm", "z_modulus_ohm", "z_phase_deg",
    ),
    MeasurementType.PDP: (
        "elapsed_s", "potential_v", "current_a", "current_density_a_cm2",
    ),
}


class OCPEnd(str, Enum):
    """Why an OCP measurement stopped.

    Recorded in metadata. The two traces look similar on a plot; their validity
    does not. This field is the difference.
    """

    STABLE = "STABILITY_REACHED"    #: Drift criterion satisfied. Good data.
    TIMEOUT = "MAX_WAIT_TIMEOUT"    #: Ran out of time, still drifting.
    FAULT = "FAULT"


# --------------------------------------------------------------------------- #
# Cell lifecycle
# --------------------------------------------------------------------------- #

class CellState(str, Enum):
    """Where a cell is in its lifecycle.

    The station moves a cell through these in order, and every operation checks
    the state before acting. This is what stops the software filling a cell
    that is not clamped shut.

        EMPTY -> LOADED -> CLOSED -> FILLED -> DRAINED -> OPEN -> EMPTY
    """

    EMPTY = "EMPTY"       #: No sample. Clamp open. Ready to receive a coupon.
    LOADED = "LOADED"     #: Coupon placed, clamp still open.
    CLOSED = "CLOSED"     #: Clamp shut, seal confirmed by switch. Dry.
    FILLED = "FILLED"     #: Electrolyte in the cell. Measurements may run.
    DRAINED = "DRAINED"   #: Liquid removed. Clamp still shut.
    OPEN = "OPEN"         #: Clamp released, coupon still sitting in the cell.


class Activity(str, Enum):
    """Things that may happen on the station, only one at a time.

    See `hardware/arbiter.py`. Everything here either moves, vibrates, or
    switches current, and all of those corrupt an electrochemical measurement.
    """

    ARM_A = "ARM_A"                # fluid head arm
    ARM_B = "ARM_B"                # sample handling arm
    PUMP = "PUMP"                  # fill, drain, rinse
    CLAMP = "CLAMP"                # cell open/close motor
    MEASUREMENT = "MEASUREMENT"    # potentiostat acquiring


# --------------------------------------------------------------------------- #
# Measurement parameters
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OCPParams:
    """Settings for an open-circuit-potential measurement.

    OCP does not stop on a timer. It stops when the potential has settled --
    when the drift over a trailing window falls below a threshold -- or when
    the maximum wait is reached, whichever comes first.

    Attributes:
        sample_interval_s: Seconds between readings.
        stability_window_s: Trailing window the drift is fitted over.
        stability_threshold_mv_per_min: Drift below this counts as settled.
        min_duration_s: Never declare stability before this much time. Must be
            at least as long as the window, otherwise stability could be called
            before a full window of data exists.
        max_duration_s: Give up after this long. Stops one stubborn sample
            stalling an overnight run forever.
    """

    sample_interval_s: float = 1.0
    stability_window_s: float = 600.0
    stability_threshold_mv_per_min: float = 0.2
    min_duration_s: float = 600.0
    max_duration_s: float = 3600.0

    def __post_init__(self) -> None:
        if self.sample_interval_s <= 0:
            raise ValueError("sample_interval_s must be positive")
        if self.stability_window_s <= 0:
            raise ValueError("stability_window_s must be positive")
        if self.min_duration_s < self.stability_window_s:
            raise ValueError(
                "min_duration_s must be >= stability_window_s, or stability "
                "could be declared before a full window of data exists"
            )
        if self.max_duration_s <= self.min_duration_s:
            raise ValueError("max_duration_s must exceed min_duration_s")


@dataclass(frozen=True)
class EISParams:
    """Settings for impedance spectroscopy.

    The DC bias is not set here. EIS is always run at the open-circuit
    potential measured moments earlier, and `Station.measure_eis` fills it in
    automatically from the OCP result.

    Attributes:
        start_frequency_hz: Highest frequency. The sweep runs high to low.
        end_frequency_hz: Lowest frequency.
        points_per_decade: Sweep density.
        ac_amplitude_mv: RMS amplitude of the AC perturbation.
        settle_time_s: Pause after applying the bias, before sweeping.
    """

    start_frequency_hz: float = 100_000.0
    end_frequency_hz: float = 0.01
    points_per_decade: int = 10
    ac_amplitude_mv: float = 10.0
    settle_time_s: float = 5.0

    def __post_init__(self) -> None:
        if self.start_frequency_hz <= self.end_frequency_hz:
            raise ValueError("start_frequency_hz must exceed end_frequency_hz")
        if self.end_frequency_hz <= 0:
            raise ValueError("end_frequency_hz must be positive")
        if self.points_per_decade < 1:
            raise ValueError("points_per_decade must be at least 1")

    def point_count(self) -> int:
        """Return how many frequencies the sweep will visit."""
        import math
        decades = math.log10(self.start_frequency_hz / self.end_frequency_hz)
        return int(round(decades * self.points_per_decade)) + 1


@dataclass(frozen=True)
class PDPParams:
    """Settings for potentiodynamic polarisation.

    This measurement destroys the coupon. It always runs last, and the sample
    cannot be re-tested afterwards.

    Potentials are given relative to OCP; `Station.measure_pdp` converts them
    to absolute values using the measured open-circuit potential.

    Attributes:
        start_v_vs_ocp: Sweep start relative to OCP (negative = cathodic).
        end_v_vs_ocp: Sweep end relative to OCP (positive = anodic).
        scan_rate_mv_per_s: Sweep rate.
        current_limit_a: Compliance limit.
        sample_period_s: Acquisition period.
    """

    start_v_vs_ocp: float = -0.25
    end_v_vs_ocp: float = 1.0
    scan_rate_mv_per_s: float = 0.167
    current_limit_a: float = 0.1
    sample_period_s: float = 1.0

    def __post_init__(self) -> None:
        if self.start_v_vs_ocp >= self.end_v_vs_ocp:
            raise ValueError("start_v_vs_ocp must be below end_v_vs_ocp")
        if self.scan_rate_mv_per_s <= 0:
            raise ValueError("scan_rate_mv_per_s must be positive")

    def duration_s(self) -> float:
        """Return the nominal sweep duration in seconds."""
        span_mv = (self.end_v_vs_ocp - self.start_v_vs_ocp) * 1000.0
        return span_mv / self.scan_rate_mv_per_s


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass
class Measurement:
    """The data and provenance of one completed measurement.

    Attributes:
        type: Which measurement this is.
        cell: Which cell it was taken in.
        sample_id: Which coupon it was taken on.
        columns: Column names for `rows`, fixed by `COLUMNS`.
        rows: The acquired data, one tuple per point.
        started_at: UTC time acquisition began.
        finished_at: UTC time acquisition ended.
        requested: Parameters as asked for.
        applied: Parameters as the instrument reported them back. Instruments
            silently clamp out-of-range requests; storing both makes that
            visible instead of invisible.
        derived: Scalars computed from the trace -- final potential,
            termination reason, drift, and so on.
    """

    type: MeasurementType
    cell: int
    sample_id: str
    columns: tuple[str, ...]
    rows: list[tuple[float, ...]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    requested: dict[str, Any] = field(default_factory=dict)
    applied: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)

    @property
    def point_count(self) -> int:
        """Return the number of acquired data points."""
        return len(self.rows)

    @property
    def duration_s(self) -> float | None:
        """Return acquisition duration, or None if not finished."""
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def check_shape(self) -> None:
        """Raise if any row does not match the declared column count."""
        width = len(self.columns)
        for i, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(
                    f"{self.type.value} row {i} has {len(row)} values "
                    f"but {width} columns are declared"
                )


@dataclass
class SampleRun:
    """The outcome of one coupon, start to finish.

    Attributes:
        sample_id: The coupon.
        cell: Which cell it ran in.
        material: Alloy designation.
        area_cm2: Exposed area set by the O-ring aperture.
        electrolyte: Description of the solution used.
        measurements: Completed measurements, in execution order.
        started_at: When the coupon was loaded.
        finished_at: When it was unloaded, or when the run failed.
        ok: Whether the run completed successfully.
        failure: Explanation when `ok` is False.
        run_dir: Directory the results were written to.
    """

    sample_id: str
    cell: int
    material: str = "Ti"
    area_cm2: float = 1.0
    electrolyte: str = ""
    measurements: list[Measurement] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    ok: bool = False
    failure: str = ""
    run_dir: Any = None

    def types_done(self) -> tuple[MeasurementType, ...]:
        """Return the measurement types completed, in order."""
        return tuple(m.type for m in self.measurements)

    def consumed(self) -> bool:
        """Return whether PDP has run, meaning the coupon is destroyed."""
        return MeasurementType.PDP in self.types_done()
