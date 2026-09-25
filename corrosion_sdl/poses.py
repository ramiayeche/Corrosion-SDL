"""The pose registry: where every physical position in the system lives.

This module is the boundary between the software and the mechanical build.

**No coordinate appears anywhere else in this package.** Not one joint angle,
not one offset. Code refers to positions only by name:

    arm_b.move_to("cell_3.place")
    arm_a.move_to("cell_3.dispense")

Those names resolve here, through `poses.json` -- a file produced by jogging the
arms and capturing joint angles with `teach.py`.

Why joint angles rather than Cartesian coordinates plus inverse kinematics:

* A taught joint configuration is exactly reproducible. The arm returns to the
  same physical pose every time, with no IK solver choosing a different elbow
  configuration on a later run.
* There is no ambiguity to resolve and no singularity to avoid.
* For a teach-and-repeat station, where every position is fixed furniture,
  interpolation between poses buys nothing.

The trade is that poses cannot be computed -- each must be taught. That is why
`REQUIRED_POSES` exists: it is the checklist handed to whoever does the
teaching, and startup fails loudly if anything on it is missing.

WHEN THE HARDWARE ARRIVES
-------------------------
Nothing in this file changes. Someone runs `teach.py`, jogs each arm to each
position, and captures it. If a baseplate is re-mounted or an arm is knocked,
the affected poses are re-taught -- again with no code change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import PoseError

#: Degrees of freedom on a myCobot 280 M5.
JOINT_COUNT = 6

#: Conservative joint limits in degrees, per axis (myCobot 280 class).
#: Poses outside these are rejected at load time rather than commanded.
#: ADJUST THESE to match the arm's datasheet once the model is fixed.
JOINT_LIMITS: tuple[tuple[float, float], ...] = (
    (-168.0, 168.0),
    (-135.0, 135.0),
    (-145.0, 145.0),
    (-148.0, 148.0),
    (-168.0, 168.0),
    (-180.0, 180.0),
)


@dataclass(frozen=True)
class Pose:
    """A named arm configuration.

    Attributes:
        name: Semantic name, e.g. ``"cell_3.place"``.
        angles: Six joint angles in degrees.
        note: Optional free text from whoever taught it.
    """

    name: str
    angles: tuple[float, ...]
    note: str = ""

    def __post_init__(self) -> None:
        if len(self.angles) != JOINT_COUNT:
            raise PoseError(
                f"pose {self.name!r} has {len(self.angles)} joint angles, "
                f"expected {JOINT_COUNT}"
            )
        for i, (angle, (lo, hi)) in enumerate(zip(self.angles, JOINT_LIMITS)):
            if not lo <= angle <= hi:
                raise PoseError(
                    f"pose {self.name!r} joint {i + 1} is {angle} deg, outside "
                    f"the limit [{lo}, {hi}]"
                )


def required_poses(cell_count: int, rack_slots: int) -> list[str]:
    """Return every pose name the software needs, given the station size.

    This is the interface document with the mechanical build. For an 8-cell
    station it comes to 34 poses:

        Arm B (sample handling)
            rack.slot_N.pick     lift a coupon out of the rack        (8)
            cell_N.approach      hover above the cell, clear of it    (8)
            cell_N.place         lower the coupon onto the seat       (8)
            arm_b.park           clear of everything, measurement-safe (1)

        Arm A (fluid head)
            cell_N.dispense      head positioned over the cell        (8)
            arm_a.park           clear of everything                  (1)

    The approach poses exist so the arm arrives above a cell before descending
    into it, rather than sweeping in sideways across the baseplate.

    Args:
        cell_count: Number of cells.
        rack_slots: Number of rack positions.

    Returns:
        Every required pose name.
    """
    names: list[str] = ["arm_a.park", "arm_b.park"]
    for slot in range(1, rack_slots + 1):
        names.append(f"rack.slot_{slot}.pick")
    for cell in range(1, cell_count + 1):
        names.append(f"cell_{cell}.approach")
        names.append(f"cell_{cell}.place")
        names.append(f"cell_{cell}.dispense")
    return names


class PoseRegistry:
    """Named poses, loaded from disk and validated.

    Args:
        poses: The loaded poses, keyed by name.
    """

    def __init__(self, poses: dict[str, Pose]) -> None:
        self._poses = poses

    # -- loading ----------------------------------------------------------- #

    @classmethod
    def load(cls, path: Path) -> "PoseRegistry":
        """Load and validate a pose file.

        Every pose is checked against the joint limits as it is read, so a
        mis-typed angle is caught here rather than commanded to an arm.

        Args:
            path: Path to ``poses.json``.

        Returns:
            A populated registry.

        Raises:
            PoseError: If the file is missing, unparseable, or any pose is
                malformed or out of limits.
        """
        if not path.exists():
            raise PoseError(
                f"pose file not found: {path}\n"
                "Run `python -m corrosion_sdl.teach` to create one, or start "
                "with config/poses.example.json."
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PoseError(f"could not read {path}: {exc}") from exc

        entries = raw.get("poses", raw)
        if not isinstance(entries, dict):
            raise PoseError(f"{path} must contain an object mapping names to angles")

        poses: dict[str, Pose] = {}
        for name, value in entries.items():
            if name.startswith("_"):
                continue  # comment keys
            if isinstance(value, dict):
                angles, note = value.get("angles", []), value.get("note", "")
            else:
                angles, note = value, ""
            try:
                poses[name] = Pose(name, tuple(float(a) for a in angles), note)
            except (TypeError, ValueError) as exc:
                raise PoseError(f"pose {name!r} is malformed: {exc}") from exc

        return cls(poses)

    @classmethod
    def empty(cls) -> "PoseRegistry":
        """Return a registry with no poses, for tests."""
        return cls({})

    # -- lookup ------------------------------------------------------------ #

    def get(self, name: str) -> Pose:
        """Return the pose called `name`.

        Raises:
            PoseError: If the pose has not been taught. The message lists near
                matches, since the usual cause is a typo or an off-by-one cell
                number.
        """
        if name in self._poses:
            return self._poses[name]
        prefix = name.split(".")[0]
        similar = sorted(n for n in self._poses if n.startswith(prefix))
        hint = f" Poses starting {prefix!r}: {similar}" if similar else ""
        raise PoseError(f"pose {name!r} has not been taught.{hint}")

    def has(self, name: str) -> bool:
        """Return whether `name` has been taught."""
        return name in self._poses

    def names(self) -> list[str]:
        """Return every taught pose name, sorted."""
        return sorted(self._poses)

    def __len__(self) -> int:
        return len(self._poses)

    # -- validation -------------------------------------------------------- #

    def missing(self, required: Iterable[str]) -> list[str]:
        """Return the required poses that have not been taught."""
        return [name for name in required if name not in self._poses]

    def require(self, required: Iterable[str]) -> None:
        """Assert that every required pose exists.

        Called once at startup, before anything moves. The point is that a
        missing pose stops the run at the beginning rather than when an arm is
        already holding a coupon over a filled cell.

        Raises:
            PoseError: Listing everything that is missing, so the person
                teaching them fixes all of it in one pass.
        """
        gaps = self.missing(required)
        if gaps:
            raise PoseError(
                f"{len(gaps)} required pose(s) have not been taught:\n  "
                + "\n  ".join(gaps)
                + "\n\nRun `python -m corrosion_sdl.teach` to capture them."
            )

    # -- writing ----------------------------------------------------------- #

    def set(self, name: str, angles: Iterable[float], note: str = "") -> None:
        """Add or replace a pose. Used by the teach utility."""
        self._poses[name] = Pose(name, tuple(float(a) for a in angles), note)

    def save(self, path: Path) -> None:
        """Write the registry to disk as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": (
                "Joint angles in degrees, six per pose. Produced by teach.py. "
                "Re-teach any pose whose fixture has been moved or bumped."
            ),
            "poses": {
                name: {"angles": list(p.angles), "note": p.note}
                for name, p in sorted(self._poses.items())
            },
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
