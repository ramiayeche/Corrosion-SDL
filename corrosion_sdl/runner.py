"""Running a batch: eight coupons, unattended, overnight.

The runner is a loop with a failure policy. It reads a list of samples, runs
each through `protocol.run_sample`, and decides what to do when one fails:

    Recoverable          mark it failed, move on. One bad coupon must not end
                         a run that has fourteen hours left in it.

    Fatal                stop. Safe state. Wait for a person. The station's
                         state is no longer trusted.

    3 failures in a row  stop anyway. That is a systematic fault -- a blocked
                         drain, an empty reservoir, a loose connector -- and
                         continuing would consume the rest of the tray to
                         produce nothing.

That last rule is what makes leaving the station running overnight defensible.

SCALE
-----
A full OCP/EIS/PDP sequence takes roughly three to four hours, so eight coupons
is 24 to 32 hours of continuous operation. Nobody is watching for most of it.
Everything in this module exists because of that fact.

THE PHASE 3 SEAM
----------------
`load_batch` reads a JSON file and returns a list of `SampleSpec`. That is the
only place the software decides *which* experiment runs next.

Closing the loop later means replacing that one function with one that consults
an optimiser instead of a file. Nothing else in the package changes -- not the
station, not the protocol, not the storage. The seam is deliberately this small.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Fatal, Recoverable
from .protocol import SampleSpec, run_sample
from .station import Station
from .types import SampleRun


@dataclass
class BatchResult:
    """What happened to a batch.

    Attributes:
        runs: Every run attempted, in order.
        stopped_because: Why the loop ended.
    """

    runs: list[SampleRun] = field(default_factory=list)
    stopped_because: str = ""

    @property
    def succeeded(self) -> int:
        """Return how many coupons completed successfully."""
        return sum(1 for r in self.runs if r.ok)

    @property
    def failed(self) -> int:
        """Return how many coupons failed."""
        return sum(1 for r in self.runs if not r.ok)

    def summary(self) -> str:
        """Return a one-line summary for the operator."""
        return (
            f"{len(self.runs)} attempted, {self.succeeded} succeeded, "
            f"{self.failed} failed -- {self.stopped_because}"
        )


def load_batch(path: Path) -> list[SampleSpec]:
    """Read a batch definition from a JSON file.

    Every entry is validated as it is read, so a typo in the eighth sample stops
    the batch before the first coupon is consumed rather than twelve hours in.

    **This function is the Phase 3 seam.** Replacing it with one that asks an
    optimiser for the next experiment is the entire change required to close the
    loop.

    Args:
        path: Path to the batch file.

    Returns:
        The samples to run, in file order.

    Raises:
        Fatal: If the file is missing, unparseable, or any entry is invalid.
    """
    if not path.exists():
        raise Fatal(f"batch file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Fatal(f"could not read {path}: {exc}") from exc

    entries = raw.get("samples")
    if not isinstance(entries, list) or not entries:
        raise Fatal(f"{path} must contain a non-empty 'samples' array")

    specs: list[SampleSpec] = []
    seen_ids: set[str] = set()
    seen_cells: set[int] = set()

    for i, entry in enumerate(entries):
        try:
            spec = SampleSpec(
                sample_id=str(entry["sample_id"]),
                cell=int(entry["cell"]),
                material=str(entry.get("material", "Ti")),
                area_cm2=float(entry.get("area_cm2", 1.0)),
                electrolyte=str(entry.get("electrolyte", "3.5 wt% NaCl")),
                run_eis=bool(entry.get("run_eis", True)),
                run_pdp=bool(entry.get("run_pdp", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Fatal(f"sample at index {i} is invalid: {exc}") from exc

        if spec.sample_id in seen_ids:
            raise Fatal(f"duplicate sample_id {spec.sample_id!r} at index {i}")
        if spec.cell in seen_cells:
            raise Fatal(
                f"cell {spec.cell} is used twice (index {i}). Each cell holds one "
                "coupon per batch, since a used cell must be rinsed and reloaded."
            )
        seen_ids.add(spec.sample_id)
        seen_cells.add(spec.cell)
        specs.append(spec)

    return specs


def run_batch(
    station: Station,
    specs: list[SampleSpec],
    on_event=None,
) -> BatchResult:
    """Run every sample in `specs`, applying the failure policy.

    Args:
        station: A connected, homed station.
        specs: The samples to run.
        on_event: Optional callable taking a short status string. Kept as a
            callback rather than a logger so the runner makes no assumption
            about how a session is being watched.

    Returns:
        A `BatchResult` describing what happened.
    """
    def emit(message: str) -> None:
        if on_event is not None:
            on_event(message)

    result = BatchResult()
    consecutive_failures = 0
    limit = station.config.protocol.max_consecutive_failures

    emit(f"batch starting: {len(specs)} samples")

    for spec in specs:
        emit(f"{spec.sample_id}: starting on cell {spec.cell}")

        try:
            run = run_sample(station, spec)

        except Fatal as exc:
            result.stopped_because = f"fatal fault: {exc}"
            emit(f"{spec.sample_id}: FATAL - {exc}")
            station.safe_state()
            return result

        except Recoverable as exc:
            # run_sample normally handles these itself; this catches anything
            # that escaped, so one unexpected error cannot end the batch.
            run = SampleRun(sample_id=spec.sample_id, cell=spec.cell)
            run.ok = False
            run.failure = f"{type(exc).__name__}: {exc}"

        result.runs.append(run)

        if run.ok:
            consecutive_failures = 0
            emit(
                f"{spec.sample_id}: complete "
                f"({len(run.measurements)} measurements)"
            )
        else:
            consecutive_failures += 1
            emit(f"{spec.sample_id}: FAILED - {run.failure}")

            if consecutive_failures >= limit:
                result.stopped_because = (
                    f"{consecutive_failures} consecutive failures indicate a "
                    "systematic fault; stopping so the remaining coupons are "
                    "not consumed for nothing"
                )
                emit(result.stopped_because)
                station.safe_state()
                return result

    result.stopped_because = "all samples attempted"
    emit(f"batch finished: {result.summary()}")
    return result
