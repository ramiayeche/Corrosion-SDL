"""The cell clamp: seals a coupon against the O-ring, and confirms it.

Each cell has a motor that presses the coupon against an O-ring, and a switch
that reports whether the clamp is seated. The O-ring aperture is what defines
the exposed area -- one square centimetre -- so the seal is not merely about
keeping liquid in.

Two things depend on the switch, and both are serious.

**Leaks.** Filling an unsealed cell floods the baseplate with electrolyte,
across live electrode connections. Never fill on an assumed seal.

**Exposed area.** A coupon that is seated crookedly, or is short of the seat,
exposes an area other than the intended 1 cm². Every current density derived
from that run is then wrong by an unknown factor -- and, once again, the run
looks perfectly successful. The switch is the only thing standing between a
badly seated coupon and a plausible, wrong dataset.

So `is_closed()` reads the switch, and `close()` refuses to report success
without it. `ClampError` is Fatal for the same reason `RelayError` is: the
failure mode is silent, and silent failures are the expensive ones.

WHEN THE HARDWARE ARRIVES
-------------------------
Fill in the five stubs in `MotorClamp`. The switch polling logic is the part to
get right -- see the notes there.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..errors import ClampError


class CellClamp(ABC):
    """Opens and closes the cell clamps, with switch confirmation.

    Args:
        cell_count: How many cells have clamps.
        timeout_s: How long to wait for the switch to confirm before failing.
    """

    def __init__(self, cell_count: int, timeout_s: float = 15.0) -> None:
        self.cell_count = cell_count
        self.timeout_s = timeout_s
        self._connected = False

    @abstractmethod
    def connect(self) -> None:
        """Open a session with the clamp controller."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the session. Must be safe to call twice and must not raise."""

    @abstractmethod
    def _drive(self, cell: int, close: bool) -> None:
        """Drive the clamp motor for `cell` and wait for it to finish."""

    @abstractmethod
    def is_closed(self, cell: int) -> bool:
        """Read the seat switch for `cell`.

        Must read the physical switch. Returning a remembered state defeats the
        purpose of the module.

        Returns:
            True if the switch reports the clamp seated.
        """

    @abstractmethod
    def stop_all(self) -> None:
        """Stop every clamp motor. Must not raise; runs on the fault path."""

    # -- public API -------------------------------------------------------- #

    def close(self, cell: int) -> None:
        """Clamp a cell shut and confirm the seal with the switch.

        Args:
            cell: Which cell to clamp.

        Raises:
            ClampError: If the switch does not confirm within the timeout.
        """
        self._check_cell(cell)
        self._drive(cell, close=True)
        if not self.is_closed(cell):
            raise ClampError(
                f"cell {cell} clamp was driven closed but the seat switch did "
                f"not confirm within {self.timeout_s:.0f} s. The coupon may be "
                "mis-seated or absent. Refusing to fill: an unsealed cell leaks "
                "electrolyte, and a mis-seated coupon has the wrong exposed area."
            )

    def open(self, cell: int) -> None:
        """Release a clamp.

        The caller is responsible for having drained the cell first;
        `Station.open_cell` enforces that through the cell state machine.

        Args:
            cell: Which cell to release.

        Raises:
            ClampError: If the switch still reports the clamp seated.
        """
        self._check_cell(cell)
        self._drive(cell, close=False)
        if self.is_closed(cell):
            raise ClampError(
                f"cell {cell} clamp was driven open but the seat switch still "
                "reports it closed. The coupon cannot be removed safely."
            )

    def assert_closed(self, cell: int) -> None:
        """Raise unless the switch confirms `cell` is sealed.

        Called before filling, and again before measuring.

        Raises:
            ClampError: If the switch does not confirm the seal.
        """
        if not self.is_closed(cell):
            raise ClampError(
                f"cell {cell} is not confirmed sealed by its seat switch"
            )

    def _check_cell(self, cell: int) -> None:
        """Raise if `cell` is out of range."""
        if not 1 <= cell <= self.cell_count:
            raise ClampError(
                f"cell {cell} is out of range; cells are 1..{self.cell_count}"
            )


# --------------------------------------------------------------------------- #
# Real hardware
# --------------------------------------------------------------------------- #

class MotorClamp(CellClamp):
    """Motor-driven clamps with seat switches.

    Stubbed pending hardware.

    IMPLEMENTATION NOTES
    --------------------
    * Drive the motor until the switch closes, then stop. Do not drive for a
      fixed time and hope: coupon thickness varies, and over-driving either
      crushes the coupon or stalls the motor.
    * Always impose a stall timeout. A motor driving against an obstruction will
      happily burn itself out.
    * Debounce the switch. Mechanical switches bounce for a few milliseconds,
      and a single instantaneous read during clamping will sometimes report a
      seal that is not yet made.
    * Consider reading the switch a second time a moment after the motor stops.
      A clamp that seats and then relaxes is a different fault from one that
      never seats, and it is worth being able to tell them apart.
    * `stop_all` must de-energise unconditionally, including mid-travel.
    """

    def connect(self) -> None:
        """Open the clamp controller interface and read every switch."""
        raise NotImplementedError(
            "MotorClamp.connect: open the controller interface, read all seat "
            "switches to establish initial state, set self._connected = True."
        )

    def disconnect(self) -> None:
        """Stop all motors and close the interface. Must not raise."""
        raise NotImplementedError(
            "MotorClamp.disconnect: stop all motors, close the handle. "
            "Swallow exceptions."
        )

    def _drive(self, cell: int, close: bool) -> None:
        """Drive one clamp motor until its switch changes or it times out."""
        raise NotImplementedError(
            "MotorClamp._drive: energise the motor in the requested direction; "
            "poll the debounced switch until it reaches the target state or "
            "self.timeout_s elapses; de-energise in all cases."
        )

    def is_closed(self, cell: int) -> bool:
        """Read the debounced seat switch for one cell."""
        raise NotImplementedError(
            "MotorClamp.is_closed: read the seat switch for this cell, "
            "debounced. Must be a physical read, not remembered state."
        )

    def stop_all(self) -> None:
        """De-energise every clamp motor. Must not raise."""
        raise NotImplementedError(
            "MotorClamp.stop_all: de-energise every clamp motor unconditionally."
        )


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #

class SimulatedClamp(CellClamp):
    """Clamps that exist only in software.

    The simulated switch follows the motor faithfully, so the confirmation path
    in `close` and `open` is genuinely exercised. `fail_next_close` forces a
    switch failure, which is how the leak-prevention guard gets tested without
    spilling anything.
    """

    def __init__(self, cell_count: int, timeout_s: float = 15.0) -> None:
        super().__init__(cell_count, timeout_s)
        self._closed: dict[int, bool] = {c: False for c in range(1, cell_count + 1)}
        self.log: list[str] = []
        #: Set to make the next close fail its switch confirmation.
        self.fail_next_close = False

    def connect(self) -> None:
        """Open the simulated session with every clamp released."""
        self._connected = True

    def disconnect(self) -> None:
        """Close the simulated session."""
        self._connected = False

    def _drive(self, cell: int, close: bool) -> None:
        """Move the simulated clamp and its switch together."""
        if close and self.fail_next_close:
            self.fail_next_close = False
            self.log.append(f"cell {cell} close FAILED (simulated)")
            return                              # switch stays open
        self._closed[cell] = close
        self.log.append(f"cell {cell} {'closed' if close else 'opened'}")

    def is_closed(self, cell: int) -> bool:
        """Return the simulated switch state."""
        self._check_cell(cell)
        return self._closed[cell]

    def stop_all(self) -> None:
        """No-op: no simulated motor is running."""
        self.log.append("all clamp motors stopped")
