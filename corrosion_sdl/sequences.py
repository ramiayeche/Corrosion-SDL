"""The defined motion sequences.

Every arm movement the station performs belongs to one of the five sequences
declared in this module. Nothing else commands an arm.

Why they live here rather than inline in `station.py`:

* **They can be listed.** `python -m corrosion_sdl sequences` prints every
  sequence and every step, so the motion plan can be reviewed on paper before
  an arm is ever powered.
* **They can be checked.** The pose names each sequence needs are derived from
  the sequence itself, so the required-pose list and the motion plan cannot
  drift apart.
* **They can be demonstrated.** The demonstration run executes them by name and
  records each step, which is what makes an end-to-end run auditable.

A sequence is data, not code. It declares an ordered list of steps; the station
executes them under the arbiter. Pose names are templates resolved at execution
time against the arm and cell in use, which is how one definition serves all
eight cells without repeating itself.

WHEN THE HARDWARE ARRIVES
-------------------------
Nothing in this file changes. The pose names resolve to taught joint angles
through the registry, so a different physical layout is a different pose file,
not a different sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StepKind(str, Enum):
    """What a single step of a sequence does."""

    HOME = "HOME"        #: Establish the arm's reference position.
    MOVE = "MOVE"        #: Move to a named pose and block until arrival.
    GRIP = "GRIP"        #: Close the gripper on a coupon.
    RELEASE = "RELEASE"  #: Open the gripper.


@dataclass(frozen=True)
class Step:
    """One step of a motion sequence.

    Attributes:
        kind: What the step does.
        target: Pose-name template for `MOVE` steps, e.g. ``"cell_{cell}.place"``.
            Empty for the other kinds. Placeholders are ``{arm}`` and ``{cell}``.
        note: Why this step exists. Printed in the demonstration transcript, so
            a reviewer can follow the motion plan without reading the code.
    """

    kind: StepKind
    target: str = ""
    note: str = ""

    def resolve(self, arm: str, cell: int | None = None) -> "Step":
        """Return this step with its pose template filled in.

        Args:
            arm: Which arm is executing, e.g. ``"arm_b"``.
            cell: Which cell the sequence is acting on, if any.

        Returns:
            A step whose `target` is a concrete pose name.
        """
        if not self.target:
            return self
        return Step(
            self.kind,
            self.target.format(arm=arm, cell=cell if cell is not None else ""),
            self.note,
        )


@dataclass(frozen=True)
class MotionSequence:
    """A named, ordered set of steps performed by one arm.

    Attributes:
        key: Identifier used on the command line and in transcripts.
        title: Human-readable name.
        arm: Which arm performs it: ``"arm_a"``, ``"arm_b"``, or ``"either"``
            for sequences that are defined once and run on both.
        needs_cell: Whether the sequence is meaningless without a cell number.
        purpose: One sentence on what the sequence accomplishes.
        steps: The steps, in execution order.
    """

    key: str
    title: str
    arm: str
    needs_cell: bool
    purpose: str
    steps: tuple[Step, ...]

    def resolve(self, arm: str | None = None, cell: int | None = None) -> list[Step]:
        """Return the steps with pose templates filled in.

        Args:
            arm: Overrides the declared arm. Required for ``"either"`` sequences.
            cell: The cell to act on.

        Returns:
            Concrete steps, ready to execute.

        Raises:
            ValueError: If the sequence needs a cell and none was given, or an
                ``"either"`` sequence was not told which arm to use.
        """
        which = arm or self.arm
        if which == "either":
            raise ValueError(
                f"sequence {self.key!r} runs on either arm; specify which one"
            )
        if self.needs_cell and cell is None:
            raise ValueError(f"sequence {self.key!r} requires a cell number")
        return [step.resolve(which, cell) for step in self.steps]

    def pose_names(self, arm: str | None = None, cell: int | None = None) -> list[str]:
        """Return the pose names this sequence visits, in order."""
        return [s.target for s in self.resolve(arm, cell) if s.kind is StepKind.MOVE]

    def describe(self, arm: str | None = None, cell: int | None = None) -> str:
        """Return a printable listing of the sequence, for review or transcripts."""
        import textwrap

        which = arm or self.arm
        purpose = textwrap.wrap(self.purpose, width=62)
        lines = [f"{self.key}  ({self.title})", f"  arm     : {which}"]
        for i, chunk in enumerate(purpose):
            lines.append(f"  {'purpose :' if i == 0 else '         '} {chunk}")

        try:
            steps = self.resolve(arm, cell)
        except ValueError:
            steps = list(self.steps)
        lines += [
            f"  {i:>2}. {s.kind.value:<8} {s.target or '-':<24} {s.note}"
            for i, s in enumerate(steps, start=1)
        ]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The five sequences
# --------------------------------------------------------------------------- #

#: Establish an arm's reference position and bring it to its park pose.
#: Run once per arm at the start of every session, before anything else moves.
HOME = MotionSequence(
    key="home",
    title="Home and park",
    arm="either",
    needs_cell=False,
    purpose=(
        "Establish the arm's reference position and bring it to a pose clear of "
        "the baseplate. Nothing else may move until this has run."
    ),
    steps=(
        Step(StepKind.HOME, "", "establish the reference position"),
        Step(StepKind.MOVE, "{arm}.park", "retreat clear of the baseplate"),
    ),
)

#: Move one arm to its park pose. Used before every measurement, so the arms are
#: physically clear of the cells rather than merely stationary above one.
PARK = MotionSequence(
    key="park",
    title="Park",
    arm="either",
    needs_cell=False,
    purpose="Bring the arm clear of every cell before a measurement begins.",
    steps=(
        Step(StepKind.MOVE, "{arm}.park", "retreat clear of the baseplate"),
    ),
)

#: Arm B carries one coupon from its rack slot into a cell.
#: The approach pose is visited on the way in and again on the way out, so the
#: gripper descends vertically into the cell rather than sweeping in sideways
#: across neighbouring cells.
LOAD_SAMPLE = MotionSequence(
    key="load_sample",
    title="Load sample: rack to cell",
    arm="arm_b",
    needs_cell=True,
    purpose="Carry one coupon from its rack slot onto the seat of a cell.",
    steps=(
        Step(StepKind.MOVE, "rack.slot_{cell}.pick", "reach the coupon in the rack"),
        Step(StepKind.GRIP, "", "close on the coupon"),
        Step(StepKind.MOVE, "cell_{cell}.approach", "rise clear, then travel above the cell"),
        Step(StepKind.MOVE, "cell_{cell}.place", "descend vertically onto the cell seat"),
        Step(StepKind.RELEASE, "", "release the coupon onto the seat"),
        Step(StepKind.MOVE, "cell_{cell}.approach", "withdraw vertically, clear of the rim"),
        Step(StepKind.MOVE, "arm_b.park", "retreat clear of the baseplate"),
    ),
)

#: Arm B returns one coupon from a cell to its rack slot. The reverse of
#: `LOAD_SAMPLE`, and it runs only once the clamp has been released.
UNLOAD_SAMPLE = MotionSequence(
    key="unload_sample",
    title="Unload sample: cell to rack",
    arm="arm_b",
    needs_cell=True,
    purpose="Return one coupon from a cell to the rack slot it came from.",
    steps=(
        Step(StepKind.MOVE, "cell_{cell}.approach", "travel above the cell"),
        Step(StepKind.MOVE, "cell_{cell}.place", "descend vertically to the coupon"),
        Step(StepKind.GRIP, "", "close on the coupon"),
        Step(StepKind.MOVE, "cell_{cell}.approach", "withdraw vertically, clear of the rim"),
        Step(StepKind.MOVE, "rack.slot_{cell}.pick", "travel to the rack slot"),
        Step(StepKind.RELEASE, "", "release the coupon into the rack"),
        Step(StepKind.MOVE, "arm_b.park", "retreat clear of the baseplate"),
    ),
)

#: Arm A brings the fluid dispense head over one cell. Always followed by a
#: separate pump operation under its own arbiter hold, so the arm has finished
#: moving before any liquid flows.
POSITION_DISPENSE_HEAD = MotionSequence(
    key="position_dispense_head",
    title="Position dispense head over cell",
    arm="arm_a",
    needs_cell=True,
    purpose=(
        "Bring the fluid head over a cell so a fill or rinse can be dispensed. "
        "The pump runs afterwards, never while this arm is in motion."
    ),
    steps=(
        Step(StepKind.MOVE, "cell_{cell}.dispense", "position the head above the cell"),
    ),
)


#: Every defined sequence, keyed by name. This is the registry the command line
#: and the demonstration run enumerate.
SEQUENCES: dict[str, MotionSequence] = {
    seq.key: seq
    for seq in (HOME, PARK, LOAD_SAMPLE, UNLOAD_SAMPLE, POSITION_DISPENSE_HEAD)
}


def get(key: str) -> MotionSequence:
    """Return the sequence called `key`.

    Raises:
        KeyError: If no such sequence is defined, naming the ones that are.
    """
    if key not in SEQUENCES:
        raise KeyError(f"no sequence named {key!r}; defined: {sorted(SEQUENCES)}")
    return SEQUENCES[key]


def all_pose_names(cell_count: int, rack_slots: int) -> set[str]:
    """Return every pose name reachable through any sequence.

    Used by the self-test to prove that the motion plan and the required-pose
    list agree. If a sequence ever visits a pose that is not on the required
    list, that is a gap between the plan and the calibration checklist, and it
    would surface at run time as a missing pose.
    """
    names: set[str] = set()
    for arm in ("arm_a", "arm_b"):
        names.update(HOME.pose_names(arm))
        names.update(PARK.pose_names(arm))
    for cell in range(1, cell_count + 1):
        names.update(LOAD_SAMPLE.pose_names(cell=cell))
        names.update(UNLOAD_SAMPLE.pose_names(cell=cell))
        names.update(POSITION_DISPENSE_HEAD.pose_names(cell=cell))
    return names
