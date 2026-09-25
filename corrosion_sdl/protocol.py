"""The experiment: one coupon, start to finish.

This is the shortest file in the package and the most important one to read. If
you want to know what the machine does, read `run_sample` below -- it is the
whole experiment in about twenty lines, in the order things physically happen.

Everything that would normally clutter such a function -- interlocks, state
checks, file writing, passing the OCP result into EIS -- lives inside the
`Station` methods being called. That is what keeps this readable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Fatal, Recoverable
from .station import Station
from .types import SampleRun, utc_now


@dataclass(frozen=True)
class SampleSpec:
    """What to run on one coupon.

    Attributes:
        sample_id: Identifier. Becomes part of the run directory name.
        cell: Which cell to run it in.
        material: Alloy designation, recorded in metadata.
        area_cm2: Exposed area set by the O-ring aperture.
        electrolyte: Description of the solution, recorded in metadata.
        run_eis: Whether to run impedance spectroscopy.
        run_pdp: Whether to run polarisation. This destroys the coupon.
    """

    sample_id: str
    cell: int
    material: str = "Ti"
    area_cm2: float = 1.0
    electrolyte: str = "3.5 wt% NaCl"
    run_eis: bool = True
    run_pdp: bool = True


def run_sample(station: Station, spec: SampleSpec) -> SampleRun:
    """Run one coupon from rack to rack.

    The physical sequence:

        1.  load      arm B moves the coupon from the rack into the cell
        2.  close     the clamp seals it against the O-ring, switch confirms
        3.  select    the relay board connects this cell to the potentiostat
        4.  fill      arm A positions the head, the pump dispenses electrolyte
        5.  park      both arms retreat clear of the baseplate
        6.  OCP       measure until the potential settles
        7.  EIS       measure impedance, biased at that potential
        8.  PDP       measure polarisation -- destroys the coupon
        9.  empty     drain the spent electrolyte to waste
        10. rinse     DI water, then drain again
        11. open      release the clamp
        12. unload    arm B returns the coupon to its rack slot

    Ordering is not merely conventional. Filling before clamping floods the
    baseplate. Measuring before selecting reads a different cell. Running PDP
    before EIS destroys the surface EIS was going to measure. Each of those is
    refused by the `Station` method involved, so a mistake in this file fails
    loudly rather than quietly producing bad data.

    Args:
        station: A connected, homed station.
        spec: What to run.

    Returns:
        The completed `SampleRun`, with `ok` set and `failure` populated if
        something went wrong.

    Raises:
        Fatal: Re-raised after cleanup. The station's state is untrusted and the
            batch must stop.
    """
    cell = spec.cell
    run: SampleRun | None = None

    try:
        # -- prepare ------------------------------------------------------- #
        run = station.load_sample(
            spec.sample_id,
            cell,
            material=spec.material,
            area_cm2=spec.area_cm2,
            electrolyte=spec.electrolyte,
        )
        station.close_cell(cell)      # seal confirmed by switch before any liquid
        station.select_cell(cell)     # relay connects this cell, read back
        station.fill_cell(cell)       # arm positions head, pump dispenses
        station.park_arms()           # arms clear before anything is measured

        # -- measure ------------------------------------------------------- #
        # Order is fixed. OCP establishes the reference potential that both of
        # the others are defined against, and PDP destroys the surface, so it
        # can only ever come last.
        station.measure_ocp(cell)
        if spec.run_eis:
            station.measure_eis(cell)
        if spec.run_pdp:
            station.measure_pdp(cell)

        # -- clean up ------------------------------------------------------ #
        station.empty_cell(cell)
        station.rinse_cell(cell)
        station.open_cell(cell)
        run = station.unload_sample(cell)

        run.ok = True
        return run

    except Recoverable as exc:
        # This coupon failed, but the station is still in a known state. Try to
        # leave the cell clean so the next sample is not affected, then report.
        run = _abandon(station, cell, run, f"{type(exc).__name__}: {exc}")
        return run

    except Fatal:
        # The station's state is not trusted. Do not touch the hardware further
        # beyond the safe state; record what we have and let the runner stop.
        if run is not None:
            run.ok = False
            run.failure = "fatal fault during run"
            run.finished_at = utc_now()
        station.safe_state()
        raise


def _abandon(
    station: Station,
    cell: int,
    run: SampleRun | None,
    reason: str,
) -> SampleRun:
    """Clean up after a recoverable failure and return the failed run.

    Best effort. The cell may be in any state, so each cleanup step is attempted
    independently and failures are ignored -- the point is to leave the station
    usable for the next coupon, not to guarantee a perfect recovery.

    The coupon is deliberately left in the cell if it cannot be removed safely.
    A human can retrieve it; an arm reaching into a cell in an unknown state
    cannot.
    """
    from .types import CellState

    for step in ("empty", "rinse", "open", "unload"):
        try:
            state = station.cell_state(cell)
            if step == "empty" and state is CellState.FILLED:
                station.empty_cell(cell)
            elif step == "rinse" and station.cell_state(cell) is CellState.DRAINED:
                station.rinse_cell(cell)
            elif step == "open" and station.cell_state(cell) is CellState.DRAINED:
                station.open_cell(cell)
            elif step == "unload" and station.cell_state(cell) is CellState.OPEN:
                run = station.unload_sample(cell)
        except Exception:      # noqa: BLE001 - cleanup is best effort
            break

    if run is None:
        run = SampleRun(sample_id="unknown", cell=cell, started_at=utc_now())

    run.ok = False
    run.failure = reason
    run.finished_at = utc_now()
    return run
