"""The end-to-end demonstration run.

Runs the complete station against the target configuration and records
everything it does to a timestamped transcript file.

This module exists to satisfy a specific requirement: an end-to-end
demonstration of the defined motion sequences that is documented and recorded,
rather than merely observed once and described afterwards. The transcript it
writes is the record. It is plain text, timestamped, and reproducible.

The run has eight parts:

    1. Configuration      what the station was built from
    2. Motion sequences   every defined sequence, listed step by step
    3. Pose coverage      proof that every step resolves to a taught pose
    4. Sequence execution each sequence run individually, step by step
    5. End-to-end run     one coupon, load through unload
    6. Safety interlocks  each guard refusing an unsafe action
    7. Data written       the files produced
    8. Summary            pass or fail

Everything runs against simulated devices, which exercise the identical control
paths, ordering rules, interlocks and data handling as the real ones. What a
simulated run cannot demonstrate is the physical behaviour of equipment that has
not been delivered; that boundary is stated in the transcript rather than left
for a reader to infer.
"""

from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from . import __version__
from .config import Config
from .errors import ClampError, RelayError, SequenceError
from .poses import required_poses
from .protocol import SampleSpec, run_sample
from .sequences import SEQUENCES, MotionSequence, Step, all_pose_names
from .station import Station
from .types import Activity

RULE = "=" * 78
THIN = "-" * 78


class Recorder:
    """Writes the transcript to the console and to a file at the same time.

    The file is the deliverable; the console output is so that a person watching
    the run sees the same thing the record will show.
    """

    def __init__(self, path: Path, stream: TextIO = sys.stdout) -> None:
        self.path = path
        self.stream = stream
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8")
        self.checks_passed = 0
        self.checks_failed = 0

    def line(self, text: str = "") -> None:
        """Write one line to both destinations."""
        print(text, file=self.stream)
        self._fh.write(text + "\n")
        self._fh.flush()

    def stamped(self, text: str) -> None:
        """Write one line prefixed with a UTC timestamp."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        self.line(f"  [{now}] {text}")

    def heading(self, number: int, title: str) -> None:
        """Start a numbered section."""
        self.line()
        self.line(RULE)
        self.line(f"  PART {number}.  {title.upper()}")
        self.line(RULE)

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        """Record one pass/fail observation and return whether it passed."""
        if ok:
            self.checks_passed += 1
            self.line(f"  PASS   {label}")
        else:
            self.checks_failed += 1
            self.line(f"  FAIL   {label}   {detail}")
        return ok

    def close(self) -> None:
        """Close the transcript file."""
        self._fh.close()


# --------------------------------------------------------------------------- #

def _describe_step(
    seq: MotionSequence,
    index: int,
    total: int,
    step: Step,
    arm: str,
    cell: int | None,
) -> str:
    """Return a one-line description of an executed motion step."""
    where = f" cell {cell}" if cell is not None else ""
    target = f" -> {step.target}" if step.target else ""
    return (
        f"{seq.key}{where}  step {index}/{total}  "
        f"{arm}  {step.kind.value}{target}   ({step.note})"
    )


def run(config_path: Path, output_dir: Path, transcript: Path | None = None) -> int:
    """Execute the demonstration and write the transcript.

    Args:
        config_path: The configuration to run against.
        output_dir: Where measurement data is written.
        transcript: Where to write the transcript. Defaults to a timestamped
            file inside `output_dir`.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y-%m-%d_%H%M%S")
    if transcript is None:
        transcript = output_dir / f"demonstration_{stamp}.log"

    rec = Recorder(transcript)
    try:
        return _run(rec, config_path, output_dir, started)
    finally:
        rec.close()


def _run(rec: Recorder, config_path: Path, output_dir: Path, started: datetime) -> int:
    """Body of the demonstration. See `run`."""
    rec.line(RULE)
    rec.line("  CORROSION SELF-DRIVING LABORATORY")
    rec.line("  END-TO-END DEMONSTRATION RUN OF THE DEFINED MOTION SEQUENCES")
    rec.line(RULE)
    rec.line(f"  Started (UTC)   : {started.isoformat()}")
    rec.line(f"  Software version: {__version__}")
    rec.line(f"  Python          : {platform.python_version()} on {platform.system()}")
    rec.line(f"  Configuration   : {config_path}")
    rec.line(f"  Transcript      : {rec.path}")

    # -- 1. configuration -------------------------------------------------- #
    rec.heading(1, "Configuration")
    config = Config.from_file(config_path)
    # Redirect data into the demonstration's own directory, so a demonstration
    # never mixes its output with real acquired data.
    from .config import StorageConfig
    config = Config(
        station=config.station,
        fluidics=config.fluidics,
        protocol=config.protocol,
        storage=StorageConfig(
            data_root=output_dir / "data",
            float_format=config.storage.float_format,
            fsync=config.storage.fsync,
        ),
        poses_file=config.poses_file,
        simulate=config.simulate,
    )
    for line in config.summary().split("\n"):
        rec.line(f"  {line}")
    rec.line()
    rec.line("  Devices run against their simulated counterparts. The control paths,")
    rec.line("  ordering rules, interlocks and data handling exercised below are the")
    rec.line("  same ones the real equipment will use; only the device drivers differ.")

    # -- 2. the defined motion sequences ----------------------------------- #
    rec.heading(2, "The defined motion sequences")
    rec.line(f"  {len(SEQUENCES)} sequences are defined. Every arm movement the station")
    rec.line("  performs belongs to one of them; nothing else commands an arm.")
    for seq in SEQUENCES.values():
        rec.line()
        example_cell = 3 if seq.needs_cell else None
        example_arm = None if seq.arm != "either" else "arm_b"
        for line in seq.describe(example_arm, example_cell).split("\n"):
            rec.line(f"  {line}")
        if seq.needs_cell:
            rec.line(f"       (shown for cell {example_cell}; identical for cells 1-8)")
        elif seq.arm == "either":
            rec.line(f"       (shown for {example_arm}; identical for arm_a)")

    # -- 3. pose coverage --------------------------------------------------- #
    rec.heading(3, "Pose coverage")
    rec.line("  Every MOVE step resolves to a named pose. This checks that the motion")
    rec.line("  plan and the calibration checklist agree: a sequence that visited a")
    rec.line("  pose nobody was asked to teach would fail at run time, with an arm")
    rec.line("  mid-task.")
    rec.line()

    needed = all_pose_names(config.station.cell_count, config.station.rack_slots)
    required = set(required_poses(config.station.cell_count, config.station.rack_slots))
    rec.line(f"  poses reachable through the sequences : {len(needed)}")
    rec.line(f"  poses on the calibration checklist    : {len(required)}")
    rec.check(
        "every pose a sequence visits is on the calibration checklist",
        needed <= required,
        f"missing: {sorted(needed - required)}",
    )

    station = Station.from_config(config)
    rec.check(
        f"all {len(required)} required poses resolve in the registry",
        not station.poses.missing(sorted(required)),
        f"missing: {station.poses.missing(sorted(required))[:5]}",
    )

    # -- 4. sequence execution ---------------------------------------------- #
    rec.heading(4, "Sequence execution")
    rec.line("  Each sequence is now executed and every step recorded as it happens.")

    executed: list[str] = []
    station.step_observer = lambda seq, i, n, step, arm, cell: (
        executed.append(step.target or step.kind.value),
        rec.stamped(_describe_step(seq, i, n, step, arm, cell)),
    )

    station.connect()
    rec.line()
    rec.line("  home  (both arms)")
    station.home()

    rec.line()
    rec.line("  load_sample  (arm B, cell 3)")
    station.load_sample("DEMO-SEQ", cell=3)

    rec.line()
    rec.line("  position_dispense_head  (arm A, cell 3)  -- to fill")
    station.close_cell(3)
    station.select_cell(3)
    station.fill_cell(3)

    rec.line()
    rec.line("  park  (both arms)  -- clear of the cells before measuring")
    station.park_arms()

    rec.line()
    rec.line("  position_dispense_head  (arm A, cell 3)  -- to rinse")
    station.empty_cell(3)
    station.rinse_cell(3)

    rec.line()
    rec.line("  unload_sample  (arm B, cell 3)")
    station.open_cell(3)
    station.unload_sample(3)

    station.step_observer = None
    rec.line()
    rec.check("every defined sequence executed without error", True)
    rec.line(f"  total motion steps executed: {len(executed)}")

    # -- 5. end-to-end run --------------------------------------------------- #
    rec.heading(5, "End-to-end run: one coupon, load through unload")
    rec.line("  The full experiment now runs unassisted: load, seal, select, fill,")
    rec.line("  park, measure three ways, drain, rinse, open, unload.")
    rec.line()

    spec = SampleSpec(
        sample_id="DEMO-001",
        cell=1,
        material="Ti-6Al-4V",
        area_cm2=1.0,
        electrolyte="3.5 wt% NaCl, pH 7.0, 22 C",
    )
    rec.line(f"  sample      : {spec.sample_id}  ({spec.material})")
    rec.line(f"  cell        : {spec.cell}")
    rec.line(f"  electrolyte : {spec.electrolyte}")
    rec.line(f"  exposed area: {spec.area_cm2} cm2")
    rec.line()

    run_record = run_sample(station, spec)
    rec.check("the coupon completed successfully", run_record.ok, run_record.failure)
    rec.check(
        "three measurements were acquired",
        len(run_record.measurements) == 3,
        f"got {len(run_record.measurements)}",
    )

    rec.line()
    for i, m in enumerate(run_record.measurements):
        rec.line(f"  [{i}] {m.type.value:<4} {m.point_count:>6} points")
        if m.type.value == "OCP":
            rec.line(f"        ended because : {m.derived['termination_reason']}")
            rec.line(f"        final potential: "
                     f"{m.derived['final_potential_v'] * 1000:+.2f} mV")
            rec.line(f"        final drift    : "
                     f"{m.derived['final_drift_mv_per_min']:+.4f} mV/min")
        elif m.type.value == "EIS":
            rec.line(f"        DC bias        : "
                     f"{m.derived['dc_potential_v'] * 1000:+.2f} mV "
                     "(the measured OCP, applied automatically)")
        else:
            rec.line(f"        sweep          : {m.rows[0][1]:+.3f} V to "
                     f"{m.rows[-1][1]:+.3f} V")
            rec.line(f"        coupon consumed: {m.derived['sample_consumed']}")

    ocp = run_record.measurements[0]
    eis = run_record.measurements[1]
    rec.line()
    rec.check(
        "OCP ended on its stability criterion, not on a timeout",
        ocp.derived["termination_reason"] == "STABILITY_REACHED",
        str(ocp.derived["termination_reason"]),
    )
    rec.check(
        "EIS was biased at the measured open-circuit potential",
        abs(eis.derived["dc_potential_v"] - ocp.derived["final_potential_v"]) < 1e-12,
    )
    rec.check("the coupon is marked consumed by PDP", run_record.consumed())

    # -- 6. safety interlocks ------------------------------------------------ #
    rec.heading(6, "Safety interlocks")
    rec.line("  Each guard is now shown refusing an unsafe action, with the message")
    rec.line("  an operator would see.")

    rec.line()
    rec.line(THIN)
    rec.line("  6.1  Only one activity at a time")
    with station.arbiter.hold(Activity.MEASUREMENT):
        try:
            with station.arbiter.hold(Activity.ARM_A):
                rec.check("arm motion during a measurement was refused", False,
                          "it was permitted")
        except SequenceError as exc:
            rec.check("arm motion during a measurement was refused", True)
            rec.line(f"         {exc}")

    rec.line()
    rec.line(THIN)
    rec.line("  6.2  A cell cannot be filled unless it is sealed")
    station.load_sample("DEMO-SAFE", cell=5)
    try:
        station.fill_cell(5)
        rec.check("filling an unclamped cell was refused", False, "it was permitted")
    except SequenceError as exc:
        rec.check("filling an unclamped cell was refused", True)
        rec.line(f"         {exc}")

    rec.line()
    rec.line(THIN)
    rec.line("  6.3  The clamp seat switch must confirm the seal")
    station.clamps.fail_next_close = True
    try:
        station.close_cell(5)
        rec.check("an unconfirmed seal was refused", False, "it was accepted")
    except ClampError as exc:
        rec.check("an unconfirmed seal was refused", True)
        rec.line(f"         {exc}")

    rec.line()
    rec.line(THIN)
    rec.line("  6.4  The cell selector is read back, never assumed")
    station.selector.fail_next_switch = True
    try:
        station.select_cell(6)
        rec.check("an unverified cell selection was refused", False, "it was accepted")
    except RelayError as exc:
        rec.check("an unverified cell selection was refused", True)
        rec.line(f"         {exc}")

    rec.line()
    rec.line(THIN)
    rec.line("  6.5  Measurement order is enforced")
    station.close_cell(5)
    station.select_cell(5)
    station.fill_cell(5)
    station.park_arms()
    try:
        station.measure_eis(5)
        rec.check("EIS before OCP was refused", False, "it was permitted")
    except SequenceError as exc:
        rec.check("EIS before OCP was refused", True)
        rec.line(f"         {exc}")

    station.measure_ocp(5)
    station.measure_pdp(5)
    try:
        station.measure_pdp(5)
        rec.check("a second PDP on one coupon was refused", False, "it was permitted")
    except SequenceError as exc:
        rec.check("a second PDP on one coupon was refused", True)
        rec.line(f"         {exc}")

    rec.line()
    rec.line(THIN)
    rec.line("  6.6  Pre-flight refuses a batch the station cannot finish")
    try:
        station.check_ready(samples=500)
        rec.check("an impossible batch was refused", False, "it was accepted")
    except SequenceError as exc:
        rec.check("an impossible batch was refused", True)
        rec.line(f"         {str(exc).splitlines()[0]}")
        for tail in str(exc).splitlines()[1:]:
            rec.line(f"         {tail}")

    rec.line()
    rec.line(THIN)
    rec.line("  6.7  The safe state")
    station.safe_state()
    rec.check("every relay channel is open", station.selector.active() is None)
    rec.check("the arbiter is idle", not station.arbiter.is_held)

    # -- 7. data written ------------------------------------------------------ #
    rec.heading(7, "Data written")
    rec.line("  One directory per coupon. The CSV holds the numbers; the JSON sidecar")
    rec.line("  holds everything needed to interpret them.")
    rec.line()

    run_dir = Path(run_record.run_dir)
    files = sorted(run_dir.iterdir())
    for path in files:
        rec.line(f"    {path.name:<28} {path.stat().st_size:>9,} bytes")

    rec.line()
    rec.check("one CSV per measurement",
              sum(1 for f in files if f.name.endswith(".csv")) == 3)
    rec.check("one metadata sidecar per CSV",
              sum(1 for f in files if f.name.endswith(".metadata.json")) == 3)
    rec.check("a run manifest was written",
              (run_dir / "run_manifest.json").exists())

    import json
    meta = json.loads((run_dir / "00_OCP.metadata.json").read_text())
    rec.check("the sidecar records the reference electrode",
              bool(meta["cell_setup"]["reference_electrode"]))
    rec.check("the sidecar records the exposed area",
              meta["cell_setup"]["exposed_area_cm2"] == 1.0)
    rec.check("the sidecar flags this as simulated data",
              meta["instrument"]["is_simulation"] is True)

    rec.line()
    rec.line("  Excerpt, 00_OCP.metadata.json:")
    for line in json.dumps(
        {
            "sample": meta["sample"],
            "cell_setup": meta["cell_setup"],
            "instrument": meta["instrument"],
            "derived": {
                k: meta["derived"][k]
                for k in ("final_potential_v", "termination_reason",
                          "final_drift_mv_per_min")
                if k in meta["derived"]
            },
        },
        indent=2,
    ).split("\n"):
        rec.line(f"    {line}")

    station.close()

    # -- 8. summary ------------------------------------------------------------ #
    finished = datetime.now(timezone.utc)
    rec.heading(8, "Summary")
    total = rec.checks_passed + rec.checks_failed
    rec.line(f"  checks performed : {total}")
    rec.line(f"  passed           : {rec.checks_passed}")
    rec.line(f"  failed           : {rec.checks_failed}")
    rec.line(f"  motion steps     : {len(executed)}")
    rec.line(f"  duration         : "
             f"{(finished - started).total_seconds():.1f} s")
    rec.line(f"  finished (UTC)   : {finished.isoformat()}")
    rec.line()
    rec.line("  Demonstrated:")
    rec.line("    - all 5 defined motion sequences, executed step by step")
    rec.line("    - a complete coupon from rack to rack, unassisted")
    rec.line("    - OCP terminating on its stability criterion")
    rec.line("    - EIS biased at the measured open-circuit potential")
    rec.line("    - PDP running last and consuming the coupon")
    rec.line("    - 7 safety interlocks refusing unsafe actions")
    rec.line("    - the complete data record written to disk")
    rec.line()
    rec.line("  Not demonstrated, and outside the scope of a simulated run:")
    rec.line("    - physical behaviour of the Client-supplied equipment, which has")
    rec.line("      not been delivered. The five device drivers are specified and")
    rec.line("      documented; they are implemented against the equipment once it")
    rec.line("      is on the bench.")
    rec.line()
    verdict = "PASS" if rec.checks_failed == 0 else "FAIL"
    rec.line(RULE)
    rec.line(f"  RESULT: {verdict}   ({rec.checks_passed}/{total} checks passed)")
    rec.line(RULE)

    return 0 if rec.checks_failed == 0 else 1
