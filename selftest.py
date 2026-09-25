#!/usr/bin/env python3
"""Self-test: verify that every rule the architecture promises actually holds.

Run with ``python selftest.py``. No test framework needed, so it works anywhere
Python 3.11 does.

Each check corresponds to a rule stated in the documentation. A failure here is
not a failing unit test -- it is a broken guarantee.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from corrosion_sdl.analysis import OCPStabilityMonitor              # noqa: E402
from corrosion_sdl.config import (                                  # noqa: E402
    Config,
    FluidicsConfig,
    ProtocolConfig,
    StationConfig,
    StorageConfig,
)
from corrosion_sdl.errors import (                                  # noqa: E402
    ClampError,
    Fatal,
    PoseError,
    Recoverable,
    RelayError,
    ReservoirError,
    SequenceError,
)
from corrosion_sdl.hardware.arbiter import Arbiter                  # noqa: E402
from corrosion_sdl.hardware.pumps import Channel, SimulatedPumps    # noqa: E402
from corrosion_sdl.poses import PoseRegistry, required_poses        # noqa: E402
from corrosion_sdl.protocol import SampleSpec, run_sample           # noqa: E402
from corrosion_sdl.runner import load_batch, run_batch              # noqa: E402
from corrosion_sdl.station import Station                           # noqa: E402
from corrosion_sdl.types import (                                   # noqa: E402
    MEASUREMENT_ORDER,
    Activity,
    CellState,
    EISParams,
    MeasurementType,
    OCPParams,
    PDPParams,
)

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one check."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def raises(name: str, exc_type: type[BaseException], fn) -> None:
    """Assert that `fn` raises `exc_type`."""
    try:
        fn()
    except exc_type:
        check(name, True)
    except Exception as exc:      # noqa: BLE001
        check(name, False, f"raised {type(exc).__name__}: {exc}")
    else:
        check(name, False, "did not raise")


def section(title: str) -> None:
    """Print a section heading."""
    print(f"\n{title}\n{'-' * len(title)}")


def fresh_station(tmp: Path, cells: int = 8) -> Station:
    """Build a connected, homed simulated station writing into `tmp`."""
    config = Config(
        station=StationConfig(cell_count=cells, rack_slots=cells),
        protocol=Config.demo().protocol,
        fluidics=FluidicsConfig(settle_time_s=0.0),
        storage=StorageConfig(data_root=tmp),
        poses_file=Path("./does-not-exist.json"),
        simulate=True,
    )
    station = Station.from_config(config)
    station.connect()
    station.home()
    return station


# =========================================================================== #
section("1. Arbiter: only one activity at a time")

arb = Arbiter()
with arb.hold(Activity.MEASUREMENT):
    raises("arm refused during measurement", SequenceError,
           lambda: arb.hold(Activity.ARM_A).__enter__())
    raises("pump refused during measurement", SequenceError,
           lambda: arb.hold(Activity.PUMP).__enter__())
    raises("clamp refused during measurement", SequenceError,
           lambda: arb.hold(Activity.CLAMP).__enter__())
check("released after the block", not arb.is_held)

with arb.hold(Activity.ARM_A):
    raises("measurement refused during arm motion", SequenceError,
           lambda: arb.hold(Activity.MEASUREMENT).__enter__())
    raises("arm B refused during arm A motion", SequenceError,
           lambda: arb.hold(Activity.ARM_B).__enter__())

arb2 = Arbiter()
with arb2.hold(Activity.ARM_A):
    with arb2.hold(Activity.ARM_A):
        pass
    check("re-entrant for the same activity", arb2.is_held)
check("fully released after nesting", not arb2.is_held)


def boom() -> None:
    with arb2.hold(Activity.PUMP):
        raise RuntimeError("boom")


try:
    boom()
except RuntimeError:
    pass
check("released even when the body raises", not arb2.is_held)


# =========================================================================== #
section("2. Error hierarchy")

check("ClampError is Fatal", issubclass(ClampError, Fatal))
check("RelayError is Fatal", issubclass(RelayError, Fatal))
check("PoseError is Fatal", issubclass(PoseError, Fatal))
check("ReservoirError is Fatal", issubclass(ReservoirError, Fatal))
check("SequenceError is Fatal", issubclass(SequenceError, Fatal))
from corrosion_sdl.errors import InstrumentError, PumpError          # noqa: E402
check("PumpError is Recoverable", issubclass(PumpError, Recoverable))
check("InstrumentError is Recoverable", issubclass(InstrumentError, Recoverable))
check("Fatal and Recoverable are disjoint",
      not issubclass(Fatal, Recoverable) and not issubclass(Recoverable, Fatal))


# =========================================================================== #
section("3. Configuration validation")

raises("fill volume above cell volume rejected", ValueError,
       lambda: FluidicsConfig(cell_volume_ml=50.0, fill_volume_ml=80.0))
raises("zero rinse cycles rejected", ValueError,
       lambda: FluidicsConfig(rinse_cycles=0))
raises("min_duration below stability window rejected", ValueError,
       lambda: OCPParams(stability_window_s=600.0, min_duration_s=300.0))
raises("max_duration below min_duration rejected", ValueError,
       lambda: OCPParams(min_duration_s=600.0, max_duration_s=500.0))
raises("inverted EIS sweep rejected", ValueError,
       lambda: EISParams(start_frequency_hz=1.0, end_frequency_hz=100.0))
raises("inverted PDP sweep rejected", ValueError,
       lambda: PDPParams(start_v_vs_ocp=1.0, end_v_vs_ocp=-0.25))
raises("fewer rack slots than cells rejected", ValueError,
       lambda: StationConfig(cell_count=8, rack_slots=4))


# =========================================================================== #
section("4. Poses")

names = required_poses(8, 8)
check("34 poses required for an 8-cell station", len(names) == 34, str(len(names)))
check("includes both park poses",
      "arm_a.park" in names and "arm_b.park" in names)
check("includes a pick for every rack slot",
      all(f"rack.slot_{i}.pick" in names for i in range(1, 9)))
check("includes approach, place and dispense for every cell",
      all(f"cell_{i}.{k}" in names
          for i in range(1, 9)
          for k in ("approach", "place", "dispense")))

reg = PoseRegistry.empty()
reg.set("cell_1.place", (0, 0, 0, 0, 0, 0))
check("a taught pose resolves", reg.get("cell_1.place").angles == (0, 0, 0, 0, 0, 0))
raises("an untaught pose raises", PoseError, lambda: reg.get("cell_2.place"))
raises("wrong joint count rejected", PoseError,
       lambda: reg.set("bad", (0, 0, 0)))
raises("out-of-limit angle rejected", PoseError,
       lambda: reg.set("bad", (999, 0, 0, 0, 0, 0)))
check("missing() lists gaps", len(reg.missing(names)) == len(names) - 1)
raises("require() raises listing gaps", PoseError, lambda: reg.require(names))


# =========================================================================== #
section("5. OCP stability logic")

params = OCPParams(
    sample_interval_s=1.0, stability_window_s=60.0,
    stability_threshold_mv_per_min=0.2,
    min_duration_s=60.0, max_duration_s=600.0,
)

m = OCPStabilityMonitor(params)
check("not stable with one sample", not m.update(0.0, -0.220))

m = OCPStabilityMonitor(params)
settled_at = None
for i in range(200):
    if m.update(float(i), -0.220):
        settled_at = i
        break
check("a flat signal settles once the window fills",
      settled_at is not None and settled_at >= 60, f"settled at t={settled_at}")

m = OCPStabilityMonitor(params)
drifted = any(m.update(float(i), -0.220 + 8.333e-5 * i) for i in range(300))
check("a steadily drifting signal never settles", not drifted)
a = m.assess(299.0)
check("drift is measured accurately (5.0 mV/min injected)",
      a.drift_mv_per_min is not None and abs(a.drift_mv_per_min - 5.0) < 0.05,
      f"measured {a.drift_mv_per_min}")

m = OCPStabilityMonitor(params)
for i in range(30):
    m.update(float(i), -0.220)
a = m.assess(30.0)
check("a partial window refuses to settle", not a.stable)
check("and explains why", "window only" in a.reason, a.reason)


# =========================================================================== #
section("6. Pumps and reservoirs")

pumps = SimulatedPumps(FluidicsConfig())
pumps.connect()
ok, detail = pumps.enough_for(8)
check("8 samples fit the default reservoirs", ok, detail)
ok, _ = pumps.enough_for(100)
check("100 samples do not", not ok)

small = SimulatedPumps(FluidicsConfig(electrolyte_reservoir_ml=30.0))
small.connect()
raises("dispensing beyond the reservoir raises", ReservoirError,
       lambda: small.dispense(Channel.ELECTROLYTE, 50.0))

full = SimulatedPumps(FluidicsConfig(waste_capacity_ml=10.0))
full.connect()
raises("draining without waste headroom raises", ReservoirError,
       lambda: full.drain(1))

tracked = SimulatedPumps(FluidicsConfig())
tracked.connect()
before = tracked.remaining(Channel.ELECTROLYTE)
tracked.dispense(Channel.ELECTROLYTE, 50.0)
check("dispensing decrements the reservoir",
      abs(tracked.remaining(Channel.ELECTROLYTE) - (before - 50.0)) < 1e-9)


# =========================================================================== #
section("7. Cell lifecycle is enforced")

with tempfile.TemporaryDirectory() as tmp:
    st = fresh_station(Path(tmp))

    check("a new cell is EMPTY", st.cell_state(1) is CellState.EMPTY)
    raises("cannot close an empty cell", SequenceError, lambda: st.close_cell(1))
    raises("cannot fill an empty cell", SequenceError, lambda: st.fill_cell(1))
    raises("cannot measure an empty cell", SequenceError, lambda: st.measure_ocp(1))

    st.load_sample("TI-A", cell=1)
    check("after loading, LOADED", st.cell_state(1) is CellState.LOADED)
    raises("cannot fill before clamping", SequenceError, lambda: st.fill_cell(1))
    raises("cannot unload before opening", SequenceError,
           lambda: st.unload_sample(1))

    st.close_cell(1)
    check("after clamping, CLOSED", st.cell_state(1) is CellState.CLOSED)
    raises("cannot open a cell that was never drained", SequenceError,
           lambda: st.open_cell(1))

    st.select_cell(1)
    st.fill_cell(1)
    check("after filling, FILLED", st.cell_state(1) is CellState.FILLED)
    raises("cannot rinse a filled cell", SequenceError, lambda: st.rinse_cell(1))

    st.empty_cell(1)
    check("after draining, DRAINED", st.cell_state(1) is CellState.DRAINED)
    st.rinse_cell(1)
    check("still DRAINED after rinsing", st.cell_state(1) is CellState.DRAINED)

    st.open_cell(1)
    check("after opening, OPEN", st.cell_state(1) is CellState.OPEN)
    st.unload_sample(1)
    check("after unloading, EMPTY again", st.cell_state(1) is CellState.EMPTY)

    st.close()


# =========================================================================== #
section("8. Clamp switch must confirm the seal")

with tempfile.TemporaryDirectory() as tmp:
    st = fresh_station(Path(tmp))
    st.load_sample("TI-B", cell=2)
    st.clamps.fail_next_close = True
    raises("a clamp that does not seat raises", ClampError,
           lambda: st.close_cell(2))
    check("the cell stays LOADED after a failed clamp",
          st.cell_state(2) is CellState.LOADED)
    st.close()


# =========================================================================== #
section("9. Relay board is read back, never assumed")

with tempfile.TemporaryDirectory() as tmp:
    st = fresh_station(Path(tmp))
    st.select_cell(4)
    check("the board confirms the selected cell", st.selector.active() == 4)

    st.selector.fail_next_switch = True
    raises("an unconfirmed selection raises", RelayError,
           lambda: st.select_cell(5))

    st.select_cell(3)
    raises("assert_active rejects the wrong cell", RelayError,
           lambda: st.selector.assert_active(7))
    st.close()


# =========================================================================== #
section("10. Measurement order")

check("mandated order is OCP, EIS, PDP",
      MEASUREMENT_ORDER == (MeasurementType.OCP, MeasurementType.EIS,
                            MeasurementType.PDP))

with tempfile.TemporaryDirectory() as tmp:
    st = fresh_station(Path(tmp))
    st.load_sample("TI-C", cell=1)
    st.close_cell(1)
    st.select_cell(1)
    st.fill_cell(1)
    st.park_arms()

    raises("EIS before OCP refused", SequenceError, lambda: st.measure_eis(1))
    raises("PDP before OCP refused", SequenceError, lambda: st.measure_pdp(1))

    ocp = st.measure_ocp(1)
    check("OCP produced data", ocp.point_count > 0)
    check("OCP ended on stability, not timeout",
          ocp.derived["termination_reason"] == "STABILITY_REACHED",
          str(ocp.derived["termination_reason"]))
    check("OCP recorded the drift that justified stopping",
          ocp.derived["final_drift_mv_per_min"] is not None)

    eis = st.measure_eis(1)
    check("EIS produced data", eis.point_count > 0)
    check("EIS was biased at the measured OCP",
          abs(eis.derived["dc_potential_v"]
              - ocp.derived["final_potential_v"]) < 1e-12)
    check("EIS sweeps high to low frequency", eis.rows[0][0] > eis.rows[-1][0])
    check("EIS imaginary part is negative (capacitive)",
          all(r[2] <= 0 for r in eis.rows))

    pdp = st.measure_pdp(1)
    check("PDP produced data", pdp.point_count > 0)
    check("PDP marked the coupon consumed",
          pdp.derived["sample_consumed"] is True)
    check("PDP current density equals current / area",
          abs(pdp.rows[0][3] - pdp.rows[0][2] / 1.0) < 1e-12)
    check("PDP sweep starts below the OCP",
          pdp.rows[0][1] < ocp.derived["final_potential_v"])

    raises("OCP refused on a consumed coupon", SequenceError,
           lambda: st.measure_ocp(1))
    raises("EIS refused on a consumed coupon", SequenceError,
           lambda: st.measure_eis(1))
    raises("PDP refused twice on one coupon", SequenceError,
           lambda: st.measure_pdp(1))
    st.close()


# =========================================================================== #
section("11. Storage")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    st = fresh_station(root)
    run = run_sample(st, SampleSpec("TI-D", cell=1, material="Ti-6Al-4V"))
    st.close()

    check("the run succeeded", run.ok, run.failure)
    check("three measurements completed", len(run.measurements) == 3)

    run_dir = Path(run.run_dir)
    files = sorted(p.name for p in run_dir.iterdir())
    check("one CSV per measurement",
          sum(1 for f in files if f.endswith(".csv")) == 3)
    check("one sidecar per CSV",
          sum(1 for f in files if f.endswith(".metadata.json")) == 3)
    check("a manifest was written", "run_manifest.json" in files)
    check("files are ordered by execution",
          files[0].startswith("00_OCP") and "02_PDP.csv" in files)

    lines = (run_dir / "00_OCP.csv").read_text().strip().split("\n")
    check("CSV header is correct", lines[0] == "elapsed_s,potential_v")
    check("CSV row count matches the measurement",
          len(lines) - 1 == run.measurements[0].point_count)

    import json
    meta = json.loads((run_dir / "00_OCP.metadata.json").read_text())
    check("sidecar records the reference electrode",
          meta["cell_setup"]["reference_electrode"] != "")
    check("sidecar records the exposed area",
          meta["cell_setup"]["exposed_area_cm2"] == 1.0)
    check("sidecar flags simulated data",
          meta["instrument"]["is_simulation"] is True)
    check("sidecar records why OCP stopped",
          meta["derived"]["termination_reason"] == "STABILITY_REACHED")

    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    check("manifest records the coupon as consumed",
          manifest["sample_consumed"] is True)
    check("manifest lists all three measurements",
          len(manifest["measurements"]) == 3)


# =========================================================================== #
section("12. Batch runner")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    batch_file = root / "batch.json"
    batch_file.write_text(
        '{"samples": ['
        '{"sample_id": "A", "cell": 1},'
        '{"sample_id": "B", "cell": 2, "run_pdp": false}'
        ']}'
    )
    specs = load_batch(batch_file)
    check("batch file parsed", len(specs) == 2)
    check("run_pdp honoured", specs[1].run_pdp is False)

    dup_cell = root / "dup.json"
    dup_cell.write_text(
        '{"samples": [{"sample_id": "A", "cell": 1},'
        '{"sample_id": "B", "cell": 1}]}'
    )
    raises("duplicate cell rejected", Fatal, lambda: load_batch(dup_cell))

    dup_id = root / "dupid.json"
    dup_id.write_text(
        '{"samples": [{"sample_id": "A", "cell": 1},'
        '{"sample_id": "A", "cell": 2}]}'
    )
    raises("duplicate sample id rejected", Fatal, lambda: load_batch(dup_id))
    raises("missing batch file rejected", Fatal,
           lambda: load_batch(root / "nope.json"))

    st = fresh_station(root / "out")
    result = run_batch(st, specs)
    st.close()
    check("both samples ran", len(result.runs) == 2)
    check("both succeeded", result.succeeded == 2, result.summary())
    check("PDP skipped where requested",
          len(result.runs[1].measurements) == 2)
    check("first coupon consumed, second not",
          result.runs[0].consumed() and not result.runs[1].consumed())


# =========================================================================== #
section("13. Safe state and pre-flight")

with tempfile.TemporaryDirectory() as tmp:
    st = fresh_station(Path(tmp))
    st.select_cell(2)
    st.safe_state()
    check("safe state opens every relay channel", st.selector.active() is None)
    check("safe state leaves the arbiter idle", not st.arbiter.is_held)

    notes = st.check_ready(samples=8)
    check("pre-flight passes for 8 samples", len(notes) > 0)
    raises("pre-flight refuses an impossible batch", SequenceError,
           lambda: st.check_ready(samples=500))
    st.close()

    unhomed = Station.from_config(
        Config(storage=StorageConfig(data_root=Path(tmp) / "x"),
               poses_file=Path("./nope.json"), simulate=True)
    )
    unhomed.connect()
    raises("pre-flight refuses un-homed arms", SequenceError,
           lambda: unhomed.check_ready(samples=1))
    unhomed.close()


# =========================================================================== #
section("14. Motion sequences are defined, complete and reachable")

from corrosion_sdl import sequences as seqmod                        # noqa: E402
from corrosion_sdl.sequences import SEQUENCES, StepKind, all_pose_names  # noqa: E402

check("five sequences are defined", len(SEQUENCES) == 5, str(len(SEQUENCES)))
check("every sequence is registered under its own key",
      all(key == seq.key for key, seq in SEQUENCES.items()))
check("every sequence has at least one step",
      all(len(s.steps) >= 1 for s in SEQUENCES.values()))
check("every sequence states its purpose",
      all(s.purpose.strip() for s in SEQUENCES.values()))
check("every step carries an explanatory note",
      all(step.note.strip() for s in SEQUENCES.values() for step in s.steps))

# The motion plan and the calibration checklist must agree. A sequence visiting
# a pose nobody was asked to teach would fail at run time, mid-task.
reachable = all_pose_names(8, 8)
required_set = set(required_poses(8, 8))
check("every pose a sequence visits is on the required-pose list",
      reachable <= required_set, f"orphans: {sorted(reachable - required_set)}")
check("the sequences reach every required pose",
      required_set <= reachable, f"unreached: {sorted(required_set - reachable)}")

load = SEQUENCES["load_sample"]
steps = load.resolve(cell=3)
check("load_sample resolves cell placeholders",
      steps[0].target == "rack.slot_3.pick", steps[0].target)
check("load_sample grips before it moves to the cell",
      steps[1].kind is StepKind.GRIP)
check("load_sample releases at the cell seat",
      steps[3].target == "cell_3.place" and steps[4].kind is StepKind.RELEASE)
check("load_sample ends parked", steps[-1].target == "arm_b.park")
check("load_sample approaches the cell from above, in and out",
      steps[2].target == "cell_3.approach" and steps[5].target == "cell_3.approach")

unload = SEQUENCES["unload_sample"]
check("unload_sample reverses load_sample",
      unload.resolve(cell=3)[-2].kind is StepKind.RELEASE)

raises("a per-cell sequence refuses to resolve without a cell", ValueError,
       lambda: SEQUENCES["load_sample"].resolve())
raises("an either-arm sequence refuses to resolve without an arm", ValueError,
       lambda: SEQUENCES["home"].resolve())
raises("an unknown sequence name raises", KeyError,
       lambda: seqmod.get("no_such_sequence"))

check("home establishes the reference before moving",
      SEQUENCES["home"].resolve("arm_a")[0].kind is StepKind.HOME)
check("park is a single move to the park pose",
      SEQUENCES["park"].resolve("arm_a")[0].target == "arm_a.park")
check("position_dispense_head targets the dispense pose",
      SEQUENCES["position_dispense_head"].resolve(cell=5)[0].target
      == "cell_5.dispense")
check("describe() renders without error",
      "load_sample" in SEQUENCES["load_sample"].describe(cell=1))

# The executed motion must match the declared sequence, step for step.
with tempfile.TemporaryDirectory() as tmp:
    st = fresh_station(Path(tmp))
    seen: list[tuple[str, str]] = []
    st.step_observer = lambda sq, i, n, step, arm, cell: seen.append(
        (sq.key, step.target or step.kind.value)
    )
    st.load_sample("TI-SEQ", cell=2)
    expected = [
        ("load_sample", s.target or s.kind.value)
        for s in SEQUENCES["load_sample"].resolve("arm_b", 2)
    ]
    check("the arm executes load_sample exactly as declared",
          seen == expected, f"{seen} != {expected}")
    st.step_observer = None
    st.close()


# =========================================================================== #
section("15. Configuration loading")

from corrosion_sdl.errors import ConfigError                          # noqa: E402

TARGET_CONFIG = Path(__file__).resolve().parent / "config" / "target_configuration.json"
check("the target configuration file ships with the build", TARGET_CONFIG.exists())

target = Config.from_file(TARGET_CONFIG)
check("the target configuration loads", target.station.cell_count == 8,
      str(target.station.cell_count))
check("it describes an 8-slot rack", target.station.rack_slots == 8)
check("it selects simulated hardware", target.simulate is True)
check("its OCP threshold is 0.2 mV/min",
      target.protocol.ocp.stability_threshold_mv_per_min == 0.2)
check("its PDP is destructive-last by configuration",
      target.protocol.pdp.end_v_vs_ocp > target.protocol.pdp.start_v_vs_ocp)
check("nested measurement parameters are typed, not dicts",
      isinstance(target.protocol.ocp, OCPParams))
check("data_root is a Path", isinstance(target.storage.data_root, Path))
check("summary() renders", "cells" in target.summary())

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    bad = root / "bad.json"
    bad.write_text('{"station": {"cell_kount": 8}}')
    raises("a mis-typed setting name is rejected, not ignored", ConfigError,
           lambda: Config.from_file(bad))

    bad2 = root / "bad2.json"
    bad2.write_text('{"stations": {}}')
    raises("an unknown top-level section is rejected", ConfigError,
           lambda: Config.from_file(bad2))

    bad3 = root / "bad3.json"
    bad3.write_text('{"fluidics": {"cell_volume_ml": 10, "fill_volume_ml": 99}}')
    raises("an impossible configuration is rejected at load", ConfigError,
           lambda: Config.from_file(bad3))

    bad4 = root / "bad4.json"
    bad4.write_text("{ not json")
    raises("unparseable JSON is rejected", ConfigError,
           lambda: Config.from_file(bad4))

    raises("a missing configuration file is rejected", ConfigError,
           lambda: Config.from_file(root / "absent.json"))

    ok = root / "ok.json"
    ok.write_text('{"station": {"cell_count": 4, "rack_slots": 4}, "simulate": true}')
    loaded = Config.from_file(ok)
    check("a partial configuration fills the rest from defaults",
          loaded.station.cell_count == 4 and loaded.fluidics.fill_volume_ml == 50.0)


# =========================================================================== #
section("16. The build runs against the target configuration")

with tempfile.TemporaryDirectory() as tmp:
    cfg = Config.from_file(TARGET_CONFIG)
    # Same configuration, but without the real-time waits and writing into a
    # temporary directory, so the check is fast and leaves nothing behind.
    cfg = Config(
        station=cfg.station,
        fluidics=FluidicsConfig(**{**vars(cfg.fluidics), "settle_time_s": 0.0}),
        protocol=ProtocolConfig(
            ocp=cfg.protocol.ocp, eis=cfg.protocol.eis, pdp=cfg.protocol.pdp,
            abort_on_ocp_timeout=cfg.protocol.abort_on_ocp_timeout,
            inter_measurement_delay_s=0.0,
            max_consecutive_failures=cfg.protocol.max_consecutive_failures,
        ),
        storage=StorageConfig(data_root=Path(tmp)),
        poses_file=Path("./does-not-exist.json"),
        simulate=True,
    )
    st = Station.from_config(cfg)
    st.connect()
    st.home()
    st.check_ready(samples=8)
    record = run_sample(st, SampleSpec("TARGET-001", cell=1, material="Ti-6Al-4V"))
    st.safe_state()
    st.close()

    check("a coupon completes under the target protocol", record.ok, record.failure)
    check("OCP reached stability under the 0.2 mV/min criterion",
          record.measurements[0].derived["termination_reason"] == "STABILITY_REACHED",
          str(record.measurements[0].derived["termination_reason"]))
    check("OCP honoured the 600 s minimum duration",
          record.measurements[0].derived["elapsed_s"] >= 600.0,
          str(record.measurements[0].derived["elapsed_s"]))
    check("EIS swept the configured decades",
          record.measurements[1].point_count == cfg.protocol.eis.point_count())
    check("all three measurements were written",
          len(list(Path(record.run_dir).glob("*.csv"))) == 3)


# =========================================================================== #
print(f"\n{'=' * 60}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAILED else 0)
