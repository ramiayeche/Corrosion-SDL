"""The cell selector: connects the potentiostat to exactly one cell.

Each of the eight cells has its own fixed working, reference, and counter
electrodes. One potentiostat serves all of them, so a relay board decides which
cell is electrically connected at any moment.

Two rules, both non-negotiable.

**Break before make.** Every channel is opened before one is closed. Two closed
channels would put two cells in parallel across the instrument front end and
produce a measurement belonging to neither.

**Read back, never assume.** `active()` interrogates the hardware rather than
returning the last command sent. This is the single most important line of
defence in the module, and the reason `RelayError` is Fatal rather than
Recoverable: a relay that fails to switch does not crash anything. It produces a
complete, plausible, three-hour dataset labelled with the wrong sample. Nobody
notices, and the error propagates into whatever conclusion the data supports.
A stopped run is far cheaper.

WHEN THE HARDWARE ARRIVES
-------------------------
Fill in the four stubs in `RelayBoard`. If the board cannot report its own state,
see the note in `active()` -- that limitation must be recorded in the data, not
silently ignored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..errors import RelayError


class CellSelector(ABC):
    """Connects the potentiostat to one cell at a time.

    Args:
        cell_count: How many cells the board can address.
        port: Serial port of the relay board.
    """

    def __init__(self, cell_count: int, port: str) -> None:
        self.cell_count = cell_count
        self.port = port
        self._connected = False

    @abstractmethod
    def connect(self) -> None:
        """Open a session with the relay board and open every channel."""

    @abstractmethod
    def disconnect(self) -> None:
        """Open every channel and close the session. Must not raise."""

    @abstractmethod
    def _switch(self, cell: int) -> None:
        """Open every channel, then close the one for `cell`.

        Must be break-before-make.
        """

    @abstractmethod
    def _read_active(self) -> int | None:
        """Read which cell the hardware reports as connected.

        Returns:
            The connected cell number, or None if no channel is closed.
        """

    @abstractmethod
    def disconnect_all(self) -> None:
        """Open every channel, leaving no cell connected.

        The correct resting state between samples: a cell left connected to an
        idle instrument can pass stray current through the coupon. Must not
        raise; runs on the fault path.
        """

    # -- public API -------------------------------------------------------- #

    def select(self, cell: int) -> None:
        """Connect `cell`, then confirm it by reading the hardware back.

        Args:
            cell: Which cell to connect, 1-based.

        Raises:
            RelayError: If the cell number is invalid, or if the hardware does
                not confirm the requested cell.
        """
        if not 1 <= cell <= self.cell_count:
            raise RelayError(
                f"cell {cell} is out of range; the board addresses 1..{self.cell_count}"
            )

        self._switch(cell)

        confirmed = self._read_active()
        if confirmed != cell:
            raise RelayError(
                f"cell {cell} was selected but the board reports "
                f"{confirmed if confirmed is not None else 'nothing'} connected. "
                "Refusing to measure: an unverified selection risks labelling a "
                "three-hour dataset with the wrong sample."
            )

    def active(self) -> int | None:
        """Return the connected cell, read from the hardware.

        NOTE FOR INTEGRATION: if the chosen relay board has no read-back
        capability, `_read_active` may return the last commanded channel -- but
        that must then be recorded in the metadata as unverified, so a reader
        knows the cell identity was assumed rather than confirmed. Do not
        silently pretend it was verified.
        """
        return self._read_active()

    def assert_active(self, cell: int) -> None:
        """Raise unless `cell` is the connected cell.

        Called immediately before every measurement, as an independent check
        that the right sample is about to be measured.

        Raises:
            RelayError: If a different cell, or no cell, is connected.
        """
        current = self._read_active()
        if current != cell:
            raise RelayError(
                f"expected cell {cell} to be connected, but the board reports "
                f"{current if current is not None else 'nothing'}"
            )


# --------------------------------------------------------------------------- #
# Real hardware
# --------------------------------------------------------------------------- #

class RelayBoard(CellSelector):
    """A USB or serial multi-channel relay board.

    Stubbed pending hardware.

    IMPLEMENTATION NOTES
    --------------------
    * Break before make, always. Open all channels, pause a few milliseconds for
      the contacts to physically separate, then close the target. Relay actuation
      is mechanical and takes real time.
    * Switching under load pits the contacts. `Station` de-energises the
      potentiostat cell before calling `select`, and that ordering must be
      preserved.
    * Prefer a board with state read-back. If the chosen board cannot report its
      channels, either add a sense line or accept that cell identity is
      unverified -- and record it as such (see `active()`).
    * Settle time after switching matters more than it looks: the electrode
      double layer is disturbed by the connection, so allow a pause before
      measuring.
    """

    def connect(self) -> None:
        """Open the board interface and open every channel."""
        raise NotImplementedError(
            "RelayBoard.connect: open the serial/USB handle, open all channels, "
            "set self._connected = True."
        )

    def disconnect(self) -> None:
        """Open all channels and close the interface. Must not raise."""
        raise NotImplementedError(
            "RelayBoard.disconnect: open all channels, close the handle. "
            "Swallow exceptions."
        )

    def _switch(self, cell: int) -> None:
        """Open every channel, pause, then close the channel for `cell`."""
        raise NotImplementedError(
            "RelayBoard._switch: open all channels, sleep for contact "
            "separation, close channel (cell - 1)."
        )

    def _read_active(self) -> int | None:
        """Read the board's channel state back."""
        raise NotImplementedError(
            "RelayBoard._read_active: query channel states and return the "
            "single closed channel as a 1-based cell number, or None."
        )

    def disconnect_all(self) -> None:
        """Open every channel. Must not raise."""
        raise NotImplementedError(
            "RelayBoard.disconnect_all: open every channel unconditionally."
        )


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #

class SimulatedSelector(CellSelector):
    """A relay board that exists only in software.

    Models real read-back honestly: `_read_active` returns genuine internal
    state, so the verification path in `select` is actually exercised rather
    than trivially satisfied.
    """

    def __init__(self, cell_count: int, port: str = "") -> None:
        super().__init__(cell_count, port)
        self._active: int | None = None
        self.log: list[str] = []
        #: Set this to make the next switch fail, for testing the read-back path.
        self.fail_next_switch = False

    def connect(self) -> None:
        """Open the simulated session with all channels open."""
        self._connected = True
        self._active = None

    def disconnect(self) -> None:
        """Close the simulated session."""
        self._active = None
        self._connected = False

    def _switch(self, cell: int) -> None:
        """Open all channels, then close one."""
        self._active = None                       # break
        if self.fail_next_switch:                 # simulated stuck relay
            self.fail_next_switch = False
            self.log.append(f"switch to {cell} FAILED (simulated)")
            return
        self._active = cell                       # make
        self.log.append(f"switch to cell {cell}")

    def _read_active(self) -> int | None:
        """Return the genuine simulated channel state."""
        return self._active

    def disconnect_all(self) -> None:
        """Open every simulated channel."""
        self._active = None
        self.log.append("all channels open")
