"""Teaching poses: the calibration tool.

Run this once the arms are mounted and the baseplate is fixed::

    python -m corrosion_sdl.teach

It walks the required-pose list in order. For each one you jog the arm by hand
to the right place and press Enter; the current joint angles are captured under
that name and written to `config/poses.json`.

WHY THIS TOOL EXISTS
--------------------
No coordinate appears anywhere in the codebase. Every physical position is a
name -- ``cell_3.place``, ``rack.slot_5.pick`` -- that resolves through the pose
registry. Those names have to come from somewhere, and jogging an arm and
capturing joint angles is both more accurate and far faster than measuring the
baseplate and computing them.

It also means a bumped arm or a re-mounted fixture is fixed by re-teaching the
affected poses. No code changes, no recompilation, no developer involved.

POSE COUNT
----------
For an eight-cell station the list comes to 34 poses. That is a real afternoon
of work, so the tool is built to make it systematic: it shows progress, lets you
skip and return, re-teach individual poses, and it never loses work -- the file
is saved after every capture.

If the cells turn out to sit on a regular pitch, it is worth checking whether
approach poses can be derived from place poses by a fixed joint offset. That
would cut the count by roughly a third.

SAFETY
------
On real hardware the arm's servos are released while jogging, so it can be moved
by hand. Support the arm before releasing them -- it will sag under its own
weight. The tool warns before doing this.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config
from .errors import PoseError
from .hardware.arm import MyCobotArm, RobotArm, SimulatedArm
from .poses import PoseRegistry, required_poses

BAR = "=" * 70


def describe(name: str) -> str:
    """Return a human description of what a pose should look like."""
    if name == "arm_a.park":
        return "Fluid-head arm, fully clear of the baseplate and all cells."
    if name == "arm_b.park":
        return "Sample arm, fully clear of the baseplate and all cells."
    if name.startswith("rack.slot_"):
        slot = name.split("_")[1].split(".")[0]
        return f"Gripper closed around the coupon in rack slot {slot}."
    if ".approach" in name:
        cell = name.split("_")[1].split(".")[0]
        return (
            f"Directly above cell {cell}, high enough to clear its rim "
            "and any neighbouring fixture."
        )
    if ".place" in name:
        cell = name.split("_")[1].split(".")[0]
        return f"Coupon resting on the seat of cell {cell}, ready to release."
    if ".dispense" in name:
        cell = name.split("_")[1].split(".")[0]
        return f"Dispense head positioned over cell {cell}, clear of the rim."
    return "Position the arm as this name describes."


def arm_for(name: str) -> str:
    """Return which arm a pose belongs to."""
    if name.startswith("arm_a") or ".dispense" in name:
        return "arm_a"
    return "arm_b"


def teach(
    registry: PoseRegistry,
    arm_a: RobotArm,
    arm_b: RobotArm,
    names: list[str],
    out_path: Path,
    only_missing: bool = True,
) -> None:
    """Walk the pose list, capturing each one.

    Args:
        registry: The registry being filled in.
        arm_a: Fluid-head arm.
        arm_b: Sample arm.
        names: Every pose name to visit.
        out_path: Where to save. Written after every capture, so nothing is lost.
        only_missing: Skip poses already taught.
    """
    todo = [n for n in names if not (only_missing and registry.has(n))]

    print(BAR)
    print(f"  {len(names)} poses required, {len(todo)} to teach now")
    print(BAR)
    print("\n  Enter  capture the current position")
    print("  s      skip this pose for now")
    print("  q      save and quit\n")

    for i, name in enumerate(todo, start=1):
        arm_name = arm_for(name)
        arm = arm_a if arm_name == "arm_a" else arm_b

        print(f"\n[{i}/{len(todo)}]  {name}")
        print(f"  arm : {arm_name}")
        print(f"  goal: {describe(name)}")

        try:
            answer = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  interrupted")
            break

        if answer == "q":
            break
        if answer == "s":
            print("  skipped")
            continue

        try:
            angles = arm.angles()
        except Exception as exc:      # noqa: BLE001
            print(f"  could not read the arm: {exc}")
            continue

        registry.set(name, angles, note=f"taught via teach.py on {arm_name}")
        registry.save(out_path)
        print(f"  captured {tuple(round(a, 2) for a in angles)}")

    print(f"\n{BAR}")
    gaps = registry.missing(names)
    if gaps:
        print(f"  {len(gaps)} pose(s) still missing:")
        for name in gaps:
            print(f"    {name}")
    else:
        print("  every required pose has been taught")
    print(f"  saved to {out_path}")
    print(BAR)


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Capture arm poses by jogging.")
    parser.add_argument(
        "--output", type=Path, default=Path("./config/poses.json"),
        help="where to write the pose registry",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="practise against simulated arms, without hardware",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="revisit every pose, including ones already taught",
    )
    parser.add_argument("--cells", type=int, default=8)
    parser.add_argument("--slots", type=int, default=8)
    args = parser.parse_args(argv)

    config = Config()
    names = required_poses(args.cells, args.slots)

    if args.output.exists():
        try:
            registry = PoseRegistry.load(args.output)
            print(f"loaded {len(registry)} existing pose(s) from {args.output}")
        except PoseError as exc:
            print(f"could not load {args.output}: {exc}")
            return 1
    else:
        registry = PoseRegistry.empty()
        print(f"starting a new pose file at {args.output}")

    arm_cls = SimulatedArm if args.simulate else MyCobotArm
    arm_a = arm_cls("arm_a", registry, config.station.arm_a_port)
    arm_b = arm_cls("arm_b", registry, config.station.arm_b_port)

    if not args.simulate:
        print("\n  Servos will be released so the arms can be moved by hand.")
        print("  SUPPORT BOTH ARMS BEFORE CONTINUING -- they will sag.")
        try:
            if input("  ready? [y/N] ").strip().lower() != "y":
                return 0
        except (EOFError, KeyboardInterrupt):
            return 0

    try:
        arm_a.connect()
        arm_b.connect()
        if args.simulate:
            arm_a.home()
            arm_b.home()
        teach(registry, arm_a, arm_b, names, args.output, only_missing=not args.all)
    except NotImplementedError as exc:
        print(f"\nthe real arm driver is not implemented yet:\n  {exc}")
        print("\nuse --simulate to practise the workflow.")
        return 1
    finally:
        try:
            arm_a.disconnect()
            arm_b.disconnect()
        except Exception:      # noqa: BLE001
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
