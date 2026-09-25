"""Robot arms.

`RobotArm` is the interface the rest of the system uses. `MyCobotArm` is the
real implementation (stubbed, pending hardware). `SimulatedArm` is complete and
is what makes the whole station runnable today.

The important design decision is in the signature of `move_to`: it takes a
**pose name**, never joint angles. Positions live in the registry and nowhere
else, so no caller anywhere in this package contains a coordinate.

WHEN THE HARDWARE ARRIVES
-------------------------
Fill in the five stubbed methods in `MyCobotArm` using `pymycobot`. Nothing else
in the package changes -- `Station` already talks to this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..errors import ArmError
from ..poses import PoseRegistry


class RobotArm(ABC):
    """One robot arm.

    Args:
        name: Which arm this is, for log and error messages, e.g. ``"arm_a"``.
        poses: The shared pose registry.
        port: Serial port the arm is attached to.
        move_timeout_s: How long to wait for a move before calling it failed.
    """

    def __init__(
        self,
        name: str,
        poses: PoseRegistry,
        port: str,
        move_timeout_s: float = 60.0,
    ) -> None:
        self.name = name
        self.poses = poses
        self.port = port
        self.move_timeout_s = move_timeout_s
        self._connected = False
        self._homed = False
        self._at: str | None = None

    # -- state ------------------------------------------------------------- #

    @property
    def is_connected(self) -> bool:
        """Return whether a session with the arm is open."""
        return self._connected

    @property
    def is_homed(self) -> bool:
        """Return whether the arm has established a reference position."""
        return self._homed

    @property
    def at(self) -> str | None:
        """Return the name of the last pose reached, or None."""
        return self._at

    def assert_ready(self) -> None:
        """Raise unless the arm is connected and homed.

        Raises:
            ArmError: If the arm is not ready to be commanded. Moving an
                un-homed arm means moving on unknown coordinates.
        """
        if not self._connected:
            raise ArmError(f"{self.name} is not connected")
        if not self._homed:
            raise ArmError(
                f"{self.name} has not been homed; no move may be commanded "
                "until a reference position is established"
            )

    # -- interface --------------------------------------------------------- #

    @abstractmethod
    def connect(self) -> None:
        """Open a session with the arm and verify it responds."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the session. Must be safe to call twice and must not raise."""

    @abstractmethod
    def home(self) -> None:
        """Establish the reference position."""

    @abstractmethod
    def move_to(self, pose_name: str) -> None:
        """Move to a taught pose and block until it has physically arrived.

        Blocking until arrival is essential. The arbiter is released as soon as
        this returns, so if it returned while the arm was still moving, a
        measurement could begin mid-motion.

        Args:
            pose_name: Name of a pose in the registry.

        Raises:
            PoseError: If the pose has not been taught.
            ArmError: If the arm does not arrive within the timeout.
        """

    @abstractmethod
    def angles(self) -> tuple[float, ...]:
        """Return the current joint angles in degrees."""

    @abstractmethod
    def stop(self) -> None:
        """Halt motion immediately. Must not raise; runs on the fault path."""

    @abstractmethod
    def gripper(self, closed: bool) -> None:
        """Open or close the gripper.

        Only meaningful on the sample-handling arm. The fluid-head arm may
        implement this as a no-op.

        Args:
            closed: True to grip, False to release.
        """


# --------------------------------------------------------------------------- #
# Real hardware
# --------------------------------------------------------------------------- #

class MyCobotArm(RobotArm):
    """Elephant Robotics myCobot 280 M5.

    Every method below is a stub. The bodies are deliberately unwritten until
    the arm is on the bench, because guessing at a vendor API produces code that
    looks finished and does not run.

    IMPLEMENTATION NOTES
    --------------------
    The `pymycobot` package provides `MyCobot(port, baudrate)`. The calls that
    matter here:

        mc = MyCobot(self.port, 115200)
        mc.send_angles(list_of_6_angles, speed)   # speed 0-100
        mc.get_angles()                           # -> list of 6 floats
        mc.is_moving()                            # -> 0 or 1
        mc.set_gripper_state(state, speed)        # 0 open, 1 closed
        mc.release_all_servos()

    Four things to get right when filling these in:

    1. `send_angles` returns immediately. It does NOT wait for arrival. Poll
       `is_moving()` until it clears, then verify `get_angles()` is within
       tolerance of the target. Raise `ArmError` on timeout. This is the whole
       point of the blocking contract.
    2. The M5 has no true homing switch. "Homing" is a move to a known safe
       configuration, followed by confirming `get_angles()` agrees. If it does
       not, the arm has lost steps and must not be trusted.
    3. Serial communication is not reliable under load. Wrap calls in a short
       retry, and treat a persistent failure as `ArmError`, which is Fatal.
    4. Use a conservative speed. The default is faster than is comfortable near
       filled cells, and a spilled cell costs a coupon.
    """

    def connect(self) -> None:
        """Open the serial session and confirm the arm responds.

        Should construct `MyCobot(self.port, 115200)`, read `get_angles()` to
        confirm the link, and set `self._connected = True`.
        """
        raise NotImplementedError(
            "MyCobotArm.connect: construct pymycobot.MyCobot on self.port, "
            "verify with get_angles(), set self._connected = True."
        )

    def disconnect(self) -> None:
        """Close the serial session. Must not raise."""
        raise NotImplementedError(
            "MyCobotArm.disconnect: close the serial handle, set "
            "self._connected = False. Swallow any exception -- this runs in "
            "finally blocks and on the fault path."
        )

    def home(self) -> None:
        """Move to the known safe configuration and verify arrival."""
        raise NotImplementedError(
            "MyCobotArm.home: send_angles to the safe configuration, wait for "
            "is_moving() to clear, verify get_angles() matches, then set "
            "self._homed = True."
        )

    def move_to(self, pose_name: str) -> None:
        """Move to a taught pose and block until arrival is confirmed."""
        raise NotImplementedError(
            "MyCobotArm.move_to: resolve self.poses.get(pose_name).angles, "
            "send_angles(), poll is_moving() until clear or timeout, verify "
            "get_angles() is within tolerance, then set self._at = pose_name."
        )

    def angles(self) -> tuple[float, ...]:
        """Return current joint angles from `get_angles()`."""
        raise NotImplementedError("MyCobotArm.angles: return tuple(mc.get_angles()).")

    def stop(self) -> None:
        """Halt immediately. Must not raise."""
        raise NotImplementedError(
            "MyCobotArm.stop: call mc.stop() or release servos. Never raise."
        )

    def gripper(self, closed: bool) -> None:
        """Drive the gripper and wait for it to finish."""
        raise NotImplementedError(
            "MyCobotArm.gripper: mc.set_gripper_state(1 if closed else 0, speed), "
            "then pause for the gripper to complete its travel."
        )


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #

class SimulatedArm(RobotArm):
    """An arm that exists only in software.

    Complete and behaviourally faithful: it refuses to move before homing,
    resolves poses through the real registry, and tracks where it is. It simply
    arrives instantly and never drops anything.

    This is what lets the entire station, protocol, and data pipeline be
    demonstrated and tested before any hardware is delivered.
    """

    #: Joint angles the simulated arm reports before its first move.
    REST = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._angles: tuple[float, ...] = self.REST
        self._gripper_closed = False
        self.move_log: list[str] = []   #: Every pose visited, for inspection.

    def connect(self) -> None:
        """Open the simulated session."""
        self._connected = True

    def disconnect(self) -> None:
        """Close the simulated session. Safe to call repeatedly."""
        self._connected = False
        self._homed = False

    def home(self) -> None:
        """Establish the simulated reference position."""
        if not self._connected:
            raise ArmError(f"{self.name} is not connected")
        self._angles = self.REST
        self._homed = True
        self._at = None
        self.move_log.append("<home>")

    def move_to(self, pose_name: str) -> None:
        """Resolve the pose and arrive instantly.

        The pose lookup is real, so a missing or mis-typed pose fails here
        exactly as it would on hardware.
        """
        self.assert_ready()
        pose = self.poses.get(pose_name)
        self._angles = pose.angles
        self._at = pose_name
        self.move_log.append(pose_name)

    def angles(self) -> tuple[float, ...]:
        """Return the simulated joint angles."""
        return self._angles

    def stop(self) -> None:
        """No-op: the simulated arm is never in motion."""
        return None

    def gripper(self, closed: bool) -> None:
        """Record the simulated gripper state."""
        self.assert_ready()
        self._gripper_closed = closed
        self.move_log.append(f"<gripper {'close' if closed else 'open'}>")

    @property
    def gripper_closed(self) -> bool:
        """Return whether the simulated gripper is closed."""
        return self._gripper_closed
