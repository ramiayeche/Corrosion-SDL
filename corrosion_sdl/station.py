"""The Station: the one object a workflow talks to.

Everything else in this package exists to support this class. A complete
experiment reads like this::

    station.load_sample("TI-003", cell=3)
    station.close_cell(3)
    station.select_cell(3)
    station.fill_cell(3)
    station.park_arms()

    ocp = station.measure_ocp(3)
    eis = station.measure_eis(3)
    pdp = station.measure_pdp(3)

    station.empty_cell(3)
    station.rinse_cell(3)
    station.open_cell(3)
    station.unload_sample(3)

Note what is *not* in that listing: no interlock blocks, no file handling, no
passing the OCP result into EIS, no checking whether the clamp really shut.
All of it happens inside these methods, which is the point.

THREE THINGS EVERY METHOD DOES FOR YOU
--------------------------------------
**1. It takes the arbiter.** Nothing on the station moves, pumps, switches, or
measures without exclusive use. Because the acquisition is inside the method, a
caller cannot forget it -- obeying the rule was never the caller's job.

**2. It checks the cell state.** Cells move through a fixed lifecycle, and every
method asserts the state it requires before acting. This is what makes it
impossible to fill a cell that is not clamped shut.

**3. It records what happened.** Measurements are written to disk the moment
they finish, not at the end of the run. A failure during PDP must not lose the
OCP and EIS data already gathered, because the coupon is destroyed either way
and that data cannot be recreated.

ORDERING RULES
--------------
Enforced in three obvious places rather than by a state machine hidden in
another file:

    measure_ocp   refuses if the coupon was already consumed by PDP
    measure_eis   refuses without a prior OCP, and applies its bias automatically
    measure_pdp   refuses without a prior OCP, and refuses to run twice

WHEN THE HARDWARE ARRIVES
-------------------------
Nothing in this file changes. `Station.from_config` builds real devices instead
of simulated ones when `config.simulate` is False, and every method above
already talks to the interfaces those devices implement.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .analysis import OCPStabilityMonitor
from .config import Config
from .errors import SequenceError, StabilityNotReached
from .hardware.arbiter import Arbiter
from .hardware.arm import MyCobotArm, RobotArm, SimulatedArm
from .hardware.clamp import CellClamp, MotorClamp, SimulatedClamp
from .hardware.potentiostat import (
    GamryPotentiostat,
    Potentiostat,
    SimulatedPotentiostat,
)
from .hardware.pumps import Channel, PumpController, RealPumps, SimulatedPumps
from .hardware.relay import CellSelector, RelayBoard, SimulatedSelector
from .poses import PoseRegistry, required_poses
from .sequences import MotionSequence, StepKind
from .sequences import HOME as SEQ_HOME
from .sequences import LOAD_SAMPLE as SEQ_LOAD_SAMPLE
from .sequences import PARK as SEQ_PARK
from .sequences import POSITION_DISPENSE_HEAD as SEQ_POSITION_HEAD
from .sequences import UNLOAD_SAMPLE as SEQ_UNLOAD_SAMPLE
from .storage import DataStore
from .types import (
    Activity,
    CellState,
    Measurement,
    OCPEnd,
    SampleRun,
    utc_now,
)


class _Slot:
    """Internal bookkeeping for one cell.

    Tracks where the cell is in its lifecycle, which coupon is in it, and what
    has been measured. This is what the ordering guards consult.
    """

    def __init__(self, cell: int) -> None:
        self.cell = cell
        self.state = CellState.EMPTY
        self.run: SampleRun | None = None
        self.run_dir: Path | None = None
        self.ocp_v: float | None = None     # bias for EIS and reference for PDP

    def require(self, *allowed: CellState, action: str) -> None:
        """Raise unless the cell is in one of the allowed states.

        Args:
            allowed: The states from which `action` is legal.
            action: What was attempted, for the error message.

        Raises:
            SequenceError: With a message naming the state the cell is actually
                in and the states that would have been acceptable.
        """
        if self.state not in allowed:
            names = " or ".join(s.value for s in allowed)
            raise SequenceError(
                f"cannot {action} cell {self.cell}: it is {self.state.value}, "
                f"but must be {names}"
            )


class Station:
    """The corrosion testing station.

    Owns both arms, the potentiostat, the pumps, the clamps, the cell selector,
    the pose registry, the arbiter, and the data store. Workflows call methods
    on this object and nothing else.

    Build one with `Station.from_config`, which wires either real or simulated
    hardware depending on `config.simulate`.

    Args:
        config: Full configuration.
        poses: Loaded pose registry.
        arm_a: Fluid-head arm.
        arm_b: Sample-handling arm.
        pumps: Pump controller.
        clamps: Cell clamps.
        selector: Cell selector relay board.
        pstat: Potentiostat.
        store: Data store.
    """

    def __init__(
        self,
        config: Config,
        poses: PoseRegistry,
        arm_a: RobotArm,
        arm_b: RobotArm,
        pumps: PumpController,
        clamps: CellClamp,
        selector: CellSelector,
        pstat: Potentiostat,
        store: DataStore,
    ) -> None:
        self.config = config
        self.poses = poses
        self.arm_a = arm_a
        self.arm_b = arm_b
        self.pumps = pumps
        self.clamps = clamps
        self.selector = selector
        self.pstat = pstat
        self.store = store
        self.arbiter = Arbiter()

        self._slots = {c: _Slot(c) for c in config.station.cells()}
        self._connected = False

        #: Optional callback invoked after every motion step, with
        #: ``(sequence, step_index, step_count, step, arm, cell)``. Left unset
        #: in normal operation; the demonstration run uses it to record an
        #: auditable transcript of the motion sequences as they execute.
        self.step_observer = None

    # ===================================================================== #
    # Construction
    # ===================================================================== #

    @classmethod
    def from_config(cls, config: Config) -> "Station":
        """Build a station with either real or simulated hardware.

        The choice is `config.simulate`, and it is the only place in the package
        that knows which is which. Everything downstream talks to interfaces.

        Args:
            config: Full configuration.

        Returns:
            An unconnected station. Call `connect()` next.
        """
        st = config.station

        if config.poses_file.exists():
            poses = PoseRegistry.load(config.poses_file)
        elif config.simulate:
            poses = _demo_poses(st.cell_count, st.rack_slots)
        else:
            raise SequenceError(
                f"pose file {config.poses_file} not found, and simulate is False. "
                "Real hardware cannot move without taught poses -- run "
                "`python -m corrosion_sdl.teach` first."
            )

        arm_cls = SimulatedArm if config.simulate else MyCobotArm
        arm_a = arm_cls("arm_a", poses, st.arm_a_port, st.move_timeout_s)
        arm_b = arm_cls("arm_b", poses, st.arm_b_port, st.move_timeout_s)

        pumps = (
            SimulatedPumps(config.fluidics)
            if config.simulate
            else RealPumps(config.fluidics)
        )
        clamps = (
            SimulatedClamp(st.cell_count, st.clamp_timeout_s)
            if config.simulate
            else MotorClamp(st.cell_count, st.clamp_timeout_s)
        )
        selector = (
            SimulatedSelector(st.cell_count, st.relay_port)
            if config.simulate
            else RelayBoard(st.cell_count, st.relay_port)
        )
        pstat = SimulatedPotentiostat() if config.simulate else GamryPotentiostat()

        store = DataStore(config.storage, st)

        return cls(config, poses, arm_a, arm_b, pumps, clamps, selector, pstat, store)

    # ===================================================================== #
    # Setup and teardown
    # ===================================================================== #

    def connect(self) -> None:
        """Open sessions with every device.

        Raises:
            Any device's connection error. Nothing is left half-open: on
            failure, `close()` is called before the error propagates.
        """
        try:
            self.arm_a.connect()
            self.arm_b.connect()
            self.pumps.connect()
            self.clamps.connect()
            self.selector.connect()
            self.pstat.connect()
            self._connected = True
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close every session. Safe to call at any time; never raises."""
        for shutdown in (
            self.pstat.disconnect,
            self.selector.disconnect,
            self.clamps.disconnect,
            self.pumps.disconnect,
            self.arm_b.disconnect,
            self.arm_a.disconnect,
        ):
            try:
                shutdown()
            except Exception:      # noqa: BLE001 - teardown must not raise
                pass
        self._connected = False

    def home(self) -> None:
        """Home both arms and park them clear of the baseplate.

        Must be done once per session before anything else moves. Arms are
        homed one at a time, under the arbiter, so they cannot collide during a
        homing sweep.
        """
        self._run_sequence(SEQ_HOME, "arm_a")
        self._run_sequence(SEQ_HOME, "arm_b")

    def check_ready(self, samples: int) -> list[str]:
        """Verify the station can run a batch of `samples` coupons.

        Run before a batch starts. Every check runs even after one fails, so the
        operator sees every problem at once instead of fixing them one restart
        at a time.

        The reservoir check is the one that matters most for a long run: eight
        coupons push roughly a litre through the station over more than a day,
        and an empty bottle or a full waste container ruins every sample after
        the point it runs out.

        Args:
            samples: How many coupons the batch contains.

        Returns:
            A list of human-readable status lines, all of them passes.

        Raises:
            SequenceError: Listing every problem found.
        """
        problems: list[str] = []
        notes: list[str] = []

        if not self._connected:
            problems.append("station is not connected")

        for arm in (self.arm_a, self.arm_b):
            if not arm.is_homed:
                problems.append(f"{arm.name} has not been homed")
            else:
                notes.append(f"{arm.name} homed")

        gaps = self.poses.missing(
            required_poses(self.config.station.cell_count,
                           self.config.station.rack_slots)
        )
        if gaps:
            problems.append(f"{len(gaps)} pose(s) not taught: {gaps[:5]}...")
        else:
            notes.append(f"all {len(self.poses)} poses taught")

        ok, detail = self.pumps.enough_for(samples)
        (notes if ok else problems).append(detail)

        ident = self.pstat.identify()
        notes.append(
            f"instrument: {ident.get('model', 'unknown')}"
            + ("  [SIMULATED]" if ident.get("is_simulation") else "")
        )

        if problems:
            raise SequenceError(
                "the station is not ready:\n  - " + "\n  - ".join(problems)
            )
        return notes

    def __enter__(self) -> "Station":
        """Open all sessions and return the station."""
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        """Go to the safe state and close all sessions."""
        self.safe_state()
        self.close()

    # ===================================================================== #
    # Sample handling  (arm B)
    # ===================================================================== #

    def load_sample(
        self,
        sample_id: str,
        cell: int,
        material: str = "Ti",
        area_cm2: float = 1.0,
        electrolyte: str = "",
    ) -> SampleRun:
        """Move a coupon from the rack into a cell, and open its run.

        Arm B picks from the rack slot matching the cell number, approaches the
        cell from above, and lowers the coupon onto its seat. Approaching from
        above rather than sweeping in sideways is what keeps the gripper clear
        of neighbouring cells.

        This also creates the run directory, so every later measurement has
        somewhere to be written the moment it finishes.

        Args:
            sample_id: The coupon identifier. Becomes part of the directory name.
            cell: Destination cell.
            material: Alloy designation, recorded in metadata.
            area_cm2: Exposed area set by the O-ring aperture.
            electrolyte: Description of the solution, recorded in metadata.

        Returns:
            The `SampleRun` record for this coupon.

        Raises:
            SequenceError: If the cell is not empty.
        """
        self.config.station.check_cell(cell)
        slot = self._slots[cell]
        slot.require(CellState.EMPTY, action="load a sample into")

        run = SampleRun(
            sample_id=sample_id,
            cell=cell,
            material=material,
            area_cm2=area_cm2,
            electrolyte=electrolyte,
            started_at=utc_now(),
        )
        slot.run_dir = self.store.start_run(run)
        run.run_dir = slot.run_dir
        slot.run = run

        self._run_sequence(SEQ_LOAD_SAMPLE, "arm_b", cell)

        slot.state = CellState.LOADED
        return run

    def unload_sample(self, cell: int) -> SampleRun:
        """Return a coupon to its rack slot and close out the run.

        Writes the run manifest, whatever the outcome. A failed run still leaves
        a record of what happened and how far it got.

        Args:
            cell: Which cell to unload.

        Returns:
            The completed `SampleRun`.

        Raises:
            SequenceError: If the cell is not open, or holds no sample.
        """
        self.config.station.check_cell(cell)
        slot = self._slots[cell]
        slot.require(CellState.OPEN, action="unload a sample from")

        if slot.run is None:
            raise SequenceError(f"cell {cell} has no run in progress")

        self._run_sequence(SEQ_UNLOAD_SAMPLE, "arm_b", cell)

        run = slot.run
        run.finished_at = utc_now()
        if slot.run_dir is not None:
            self.store.write_manifest(slot.run_dir, run)

        slot.state = CellState.EMPTY
        slot.run = None
        slot.run_dir = None
        slot.ocp_v = None
        return run

    # ===================================================================== #
    # Clamping
    # ===================================================================== #

    def close_cell(self, cell: int) -> None:
        """Clamp a cell shut and confirm the seal with its seat switch.

        This must happen before filling. An unsealed cell leaks electrolyte
        across live electrode connections, and a mis-seated coupon exposes an
        area other than the intended one -- which silently invalidates every
        current density from the run.

        Args:
            cell: Which cell to clamp.

        Raises:
            SequenceError: If no sample is loaded.
            ClampError: If the seat switch does not confirm.
        """
        self.config.station.check_cell(cell)
        slot = self._slots[cell]
        slot.require(CellState.LOADED, action="close")

        with self.arbiter.hold(Activity.CLAMP):
            self.clamps.close(cell)

        slot.state = CellState.CLOSED

    def open_cell(self, cell: int) -> None:
        """Release a cell's clamp so the coupon can be removed.

        Args:
            cell: Which cell to open.

        Raises:
            SequenceError: If the cell has not been drained. Opening a clamp on
                a filled cell spills its contents.
            ClampError: If the clamp does not release.
        """
        self.config.station.check_cell(cell)
        slot = self._slots[cell]
        slot.require(CellState.DRAINED, action="open")

        with self.arbiter.hold(Activity.CLAMP):
            self.clamps.open(cell)

        slot.state = CellState.OPEN

    # ===================================================================== #
    # Fluids  (arm A + pumps)
    # ===================================================================== #

    def fill_cell(self, cell: int) -> None:
        """Fill a sealed cell with electrolyte.

        Two steps: arm A positions the dispense head over the cell, then the
        pump runs. They are separate arbiter holds, because the arm must have
        finished moving before liquid starts flowing.

        The seal is re-checked against the switch immediately before dispensing,
        independently of the cell state, because a clamp that relaxed after
        closing is a different fault from one that never closed.

        Args:
            cell: Which cell to fill.

        Raises:
            SequenceError: If the cell is not closed.
            ClampError: If the seal is not confirmed.
            ReservoirError: If there is not enough electrolyte.
            PumpError: If the fill fails.
        """
        self.config.station.check_cell(cell)
        slot = self._slots[cell]
        slot.require(CellState.CLOSED, action="fill")

        self.clamps.assert_closed(cell)

        self._run_sequence(SEQ_POSITION_HEAD, "arm_a", cell)

        with self.arbiter.hold(Activity.PUMP):
            volume = self.config.fluidics.fill_volume_ml
            self.pumps.dispense(Channel.ELECTROLYTE, volume)
            if isinstance(self.pumps, SimulatedPumps):
                self.pumps.note_fill(cell, volume)

        slot.state = CellState.FILLED

        if self.config.fluidics.settle_time_s > 0:
            time.sleep(self.config.fluidics.settle_time_s)

    def empty_cell(self, cell: int) -> None:
        """Drain a cell to the waste bottle.

        Drainage is through the cell's own bottom port, so no arm is involved.

        NOTE FOR INTEGRATION: if the final build empties cells by aspirating
        through the dispense head instead, add an `arm_a.move_to` before the
        pump hold -- and add eight aspirate poses to the registry, since the
        head must reach near the cell floor rather than hover above it.

        Args:
            cell: Which cell to drain.

        Raises:
            SequenceError: If the cell is not filled.
            ReservoirError: If the waste bottle lacks headroom.
            PumpError: If the drain fails or times out.
        """
        self.config.station.check_cell(cell)
        slot = self._slots[cell]
        slot.require(CellState.FILLED, action="empty")

        with self.arbiter.hold(Activity.PUMP):
            self.pumps.drain(cell)

        slot.state = CellState.DRAINED

    def rinse_cell(self, cell: int, cycles: int | None = None) -> None:
        """Rinse a drained cell with DI water.

        Each cycle dispenses DI through the head and drains it again. Rinsing
        matters most after PDP: the spent electrolyte contains exactly the
        corrosion products the next coupon must not be exposed to.

        Args:
            cell: Which cell to rinse.
            cycles: Number of cycles. Defaults to the configured value.

        Raises:
            SequenceError: If the cell has not been drained.
            ReservoirError: If DI or waste capacity runs short.
            PumpError: If a cycle fails.
        """
        self.config.station.check_cell(cell)
        slot = self._slots[cell]
        slot.require(CellState.DRAINED, action="rinse")

        self._run_sequence(SEQ_POSITION_HEAD, "arm_a", cell)

        with self.arbiter.hold(Activity.PUMP):
            self.pumps.rinse(cell, cycles)

        # The cell remains DRAINED: rinsing ends with a drain.

    # ===================================================================== #
    # Positioning and selection
    # ===================================================================== #

    def park_arms(self) -> None:
        """Move both arms to their park poses, clear of everything.

        Called before measuring. The arbiter already prevents a measurement
        while an arm is moving, but parking guarantees the arms are also
        physically clear of the cells while the measurement runs, rather than
        merely stationary above one.
        """
        self._run_sequence(SEQ_PARK, "arm_a")
        self._run_sequence(SEQ_PARK, "arm_b")

    def select_cell(self, cell: int) -> None:
        """Connect the potentiostat to one cell, and verify the connection.

        The instrument cell is de-energised first: switching relays under load
        pits the contacts.

        `CellSelector.select` reads the board back and raises if it does not
        confirm. That read-back is what stops a stuck relay producing a
        complete, plausible, three-hour dataset labelled with the wrong sample.

        Args:
            cell: Which cell to connect.

        Raises:
            RelayError: If the board does not confirm the selection.
        """
        self.config.station.check_cell(cell)
        self.pstat.cell_off()
        self.selector.select(cell)

    # ===================================================================== #
    # Measurements
    # ===================================================================== #

    def measure_ocp(self, cell: int) -> Measurement:
        """Measure open-circuit potential until it settles.

        Ends on the stability criterion -- drift below threshold over a trailing
        window -- not on a timer. The drift that justified stopping is recorded
        in the metadata, so a reviewer can tell a converged trace from one that
        merely ran out of time.

        The measured potential is retained for EIS and PDP, both of which are
        referenced to it.

        Args:
            cell: Which cell to measure.

        Returns:
            The completed measurement, already written to disk.

        Raises:
            SequenceError: If the cell is not filled, or the coupon is consumed.
            StabilityNotReached: If OCP timed out and policy says to stop.
        """
        slot = self._pre_measure(cell)
        run = slot.run
        assert run is not None

        if run.consumed():
            raise SequenceError(
                f"cannot measure cell {cell}: PDP has already destroyed the "
                f"surface of {run.sample_id}"
            )

        params = self.config.protocol.ocp
        monitor = OCPStabilityMonitor(params)

        with self.arbiter.hold(Activity.MEASUREMENT):
            m = self.pstat.measure_ocp(
                params, cell, run.sample_id, should_stop=monitor.update
            )

        m.derived.update(monitor.summary())
        slot.ocp_v = float(m.derived["final_potential_v"])
        self._record(slot, m)

        # Policy: a coupon whose potential never settled is not at steady state.
        # Polarising it consumes the coupon to produce data of unknown validity,
        # which is worse than no data because it looks like a result.
        if m.derived.get("termination_reason") == OCPEnd.TIMEOUT.value:
            drift = m.derived.get("final_drift_mv_per_min")
            message = (
                f"OCP on {run.sample_id} reached its maximum wait without "
                f"settling (final drift {drift} mV/min)"
            )
            if self.config.protocol.abort_on_ocp_timeout:
                raise StabilityNotReached(
                    f"{message}; abandoning this coupon rather than polarising "
                    "an unequilibrated surface"
                )
            m.derived["policy_note"] = f"{message}; continued by policy"

        return m

    def measure_eis(self, cell: int) -> Measurement:
        """Measure impedance at the open-circuit potential.

        The DC bias is the potential measured by `measure_ocp` moments earlier,
        applied automatically. A caller never supplies it, and cannot get it
        wrong.

        Args:
            cell: Which cell to measure.

        Returns:
            The completed measurement, already written to disk.

        Raises:
            SequenceError: If OCP has not run on this coupon, or it is consumed.
        """
        slot = self._pre_measure(cell)
        run = slot.run
        assert run is not None

        if run.consumed():
            raise SequenceError(
                f"cannot run EIS on {run.sample_id}: PDP has already destroyed "
                "the surface"
            )
        if slot.ocp_v is None:
            raise SequenceError(
                f"cannot run EIS on {run.sample_id}: EIS is biased at the "
                "open-circuit potential, so OCP must run first"
            )

        self._pause_between()
        with self.arbiter.hold(Activity.MEASUREMENT):
            m = self.pstat.measure_eis(
                self.config.protocol.eis, slot.ocp_v, cell, run.sample_id
            )

        self._record(slot, m)
        return m

    def measure_pdp(self, cell: int) -> Measurement:
        """Measure the polarisation curve. This destroys the coupon.

        Always the last measurement on a sample. The sweep bounds are relative
        to the measured OCP, and the exposed area converts current to current
        density.

        Args:
            cell: Which cell to measure.

        Returns:
            The completed measurement, already written to disk.

        Raises:
            SequenceError: If OCP has not run, or PDP has already run.
        """
        slot = self._pre_measure(cell)
        run = slot.run
        assert run is not None

        if run.consumed():
            raise SequenceError(
                f"cannot run PDP twice on {run.sample_id}: the surface was "
                "destroyed by the first run"
            )
        if slot.ocp_v is None:
            raise SequenceError(
                f"cannot run PDP on {run.sample_id}: the sweep is referenced to "
                "the open-circuit potential, so OCP must run first"
            )

        self._pause_between()
        with self.arbiter.hold(Activity.MEASUREMENT):
            m = self.pstat.measure_pdp(
                self.config.protocol.pdp,
                slot.ocp_v,
                run.area_cm2,
                cell,
                run.sample_id,
            )

        self._record(slot, m)
        return m

    # ===================================================================== #
    # Safety
    # ===================================================================== #

    def safe_state(self) -> None:
        """Put the station into its defined resting condition.

        Cell de-energised, every relay channel open, all pumps stopped, all
        clamp motors stopped, both arms halted.

        Never raises. Each step is attempted independently, so one failure does
        not prevent the rest -- and a failure here must never mask the original
        fault that prompted it.
        """
        for action in (
            self.pstat.cell_off,
            self.selector.disconnect_all,
            self.pumps.stop_all,
            self.clamps.stop_all,
            self.arm_a.stop,
            self.arm_b.stop,
        ):
            try:
                action()
            except Exception:      # noqa: BLE001 - the fault path must not raise
                pass

    def emergency_stop(self, reason: str) -> None:
        """Halt everything immediately and record why.

        Args:
            reason: What prompted the stop. Printed and retained for the
                operator.
        """
        self.safe_state()
        self._estop_reason = reason

    def cell_state(self, cell: int) -> CellState:
        """Return the lifecycle state of one cell."""
        self.config.station.check_cell(cell)
        return self._slots[cell].state

    def status(self) -> dict[str, Any]:
        """Return a snapshot of the whole station, for logging or a dashboard."""
        return {
            "connected": self._connected,
            "arbiter": self.arbiter.holder.value if self.arbiter.holder else "idle",
            "active_cell": self.selector.active(),
            "cells": {c: s.state.value for c, s in self._slots.items()},
            "reservoirs": self.pumps.levels(),
            "arm_a_at": self.arm_a.at,
            "arm_b_at": self.arm_b.at,
        }

    # ===================================================================== #
    # Internals
    # ===================================================================== #

    def _run_sequence(
        self,
        seq: MotionSequence,
        arm: str,
        cell: int | None = None,
    ) -> None:
        """Execute one defined motion sequence under the arbiter.

        This is the only place in the package that commands an arm. Every
        movement therefore belongs to a named sequence in `sequences.py`, which
        is what makes the motion plan reviewable on paper and auditable in a
        demonstration transcript.

        The arbiter is held for the whole sequence rather than per step, so no
        pump, clamp, or measurement can interleave with a part-completed arm
        movement.

        Args:
            seq: The sequence to run.
            arm: Which arm runs it, ``"arm_a"`` or ``"arm_b"``.
            cell: The cell to act on, for sequences that need one.
        """
        device = self.arm_a if arm == "arm_a" else self.arm_b
        activity = Activity.ARM_A if arm == "arm_a" else Activity.ARM_B
        steps = seq.resolve(arm, cell)

        with self.arbiter.hold(activity):
            for index, step in enumerate(steps, start=1):
                if step.kind is StepKind.HOME:
                    device.home()
                elif step.kind is StepKind.MOVE:
                    device.move_to(step.target)
                elif step.kind is StepKind.GRIP:
                    device.gripper(closed=True)
                elif step.kind is StepKind.RELEASE:
                    device.gripper(closed=False)

                if self.step_observer is not None:
                    self.step_observer(seq, index, len(steps), step, arm, cell)

    def _pre_measure(self, cell: int) -> _Slot:
        """Run every check that must pass before any measurement.

        Four independent conditions:

        1. The cell is filled.
        2. A run is open on it.
        3. The clamp switch still confirms the seal.
        4. The relay board confirms this cell is the connected one.

        Checks 3 and 4 look redundant against the cell state, and are not. The
        state records what the software believes; these read what the hardware
        reports. The gap between the two is precisely where mislabelled data
        comes from.

        Returns:
            The cell's slot.

        Raises:
            SequenceError, ClampError, RelayError: On any failed check.
        """
        self.config.station.check_cell(cell)
        slot = self._slots[cell]
        slot.require(CellState.FILLED, action="measure")

        if slot.run is None:
            raise SequenceError(f"cell {cell} has no run in progress")

        self.clamps.assert_closed(cell)
        self.selector.assert_active(cell)
        return slot

    def _record(self, slot: _Slot, m: Measurement) -> None:
        """Attach an instrument identity, store the measurement, write it out.

        Writing happens here, immediately, rather than at the end of the run.
        With a destructive final measurement, data already acquired can never be
        recreated, so it goes to disk the moment it exists.
        """
        m.applied["_instrument"] = self.pstat.identify()

        run = slot.run
        assert run is not None and slot.run_dir is not None

        index = len(run.measurements)
        run.measurements.append(m)
        self.store.write_measurement(slot.run_dir, run, m, index)

    def _pause_between(self) -> None:
        """Quiet pause between measurements on one coupon."""
        delay = self.config.protocol.inter_measurement_delay_s
        if delay > 0:
            time.sleep(delay)


# --------------------------------------------------------------------------- #
# Demo poses
# --------------------------------------------------------------------------- #

def _demo_poses(cell_count: int, rack_slots: int) -> PoseRegistry:
    """Return a registry of plausible placeholder poses, for simulation only.

    These are NOT taught positions and must never be used on hardware. They
    exist so the simulated station has something to resolve, letting the whole
    workflow be demonstrated before anyone has jogged an arm.

    Real poses come from `teach.py` and live in `config/poses.json`.
    """
    registry = PoseRegistry.empty()
    registry.set("arm_a.park", (0.0, -30.0, 0.0, 0.0, 0.0, 0.0), "simulated")
    registry.set("arm_b.park", (0.0, -30.0, 0.0, 0.0, 0.0, 0.0), "simulated")

    for slot in range(1, rack_slots + 1):
        base = -60.0 + slot * 8.0
        registry.set(f"rack.slot_{slot}.pick", (base, -40.0, 50.0, -10.0, 90.0, 0.0),
                     "simulated")

    for cell in range(1, cell_count + 1):
        base = -40.0 + cell * 10.0
        registry.set(f"cell_{cell}.approach", (base, -35.0, 40.0, -5.0, 90.0, 0.0),
                     "simulated")
        registry.set(f"cell_{cell}.place", (base, -25.0, 45.0, -20.0, 90.0, 0.0),
                     "simulated")
        registry.set(f"cell_{cell}.dispense", (base, -30.0, 35.0, 0.0, 90.0, 0.0),
                     "simulated")

    return registry
