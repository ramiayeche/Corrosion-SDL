"""Pumps: electrolyte in, DI water for rinsing, everything out to waste.

Three fluid paths:

    ELECTROLYTE  reservoir -> dispense head -> cell
    DI           reservoir -> dispense head -> cell   (rinsing only)
    WASTE        cell bottom port -> waste bottle

The dispense head is carried by arm A, so a fill or a rinse is always two steps:
`Station` positions the head over the cell, then commands the pump. Draining
goes through the cell's own bottom port and needs no arm.

RESERVOIR TRACKING
------------------
The volume bookkeeping in this module is not decoration. A batch of eight
samples pushes roughly one litre through the station over 24 to 32 hours. Three
things can end that run silently:

* the electrolyte bottle empties, and the pump dispenses air into a cell;
* the DI bottle empties, and cells stop being rinsed between coupons;
* the waste bottle fills, and drainage backs up into the cell.

None of those throw an error on their own. Each produces a run that looks
successful and is worthless, and each wastes every coupon after the point of
failure. So the controller tracks dispensed and collected volume, and
`Station.check_ready` totals the whole batch before starting and refuses to
begin one that cannot finish.

WHEN THE HARDWARE ARRIVES
-------------------------
Fill in the stubs in `RealPumps`. The volume bookkeeping in the base class is
already correct and is inherited -- do not reimplement it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from ..config import FluidicsConfig
from ..errors import ReservoirError


class Channel(str, Enum):
    """Which fluid path a pump command refers to."""

    ELECTROLYTE = "ELECTROLYTE"
    DI = "DI"


class PumpController(ABC):
    """Fill, rinse, and drain, with reservoir bookkeeping.

    Args:
        config: Volumes, rates, and reservoir capacities.

    Attributes:
        config: The fluidics configuration in force.
    """

    def __init__(self, config: FluidicsConfig) -> None:
        self.config = config
        self._used_electrolyte_ml = 0.0
        self._used_di_ml = 0.0
        self._waste_ml = 0.0
        self._connected = False

    # -- session ----------------------------------------------------------- #

    @abstractmethod
    def connect(self) -> None:
        """Open a session with the pump hardware."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the session. Must be safe to call twice and must not raise."""

    @abstractmethod
    def stop_all(self) -> None:
        """Stop every pump immediately. Must not raise; runs on the fault path."""

    # -- operations -------------------------------------------------------- #

    @abstractmethod
    def _dispense(self, channel: Channel, volume_ml: float) -> None:
        """Move `volume_ml` through `channel` into the cell under the head.

        Positioning the head is the station's job, done before this is called.

        Raises:
            PumpError: If the dispense does not complete as commanded.
        """

    @abstractmethod
    def _drain(self, cell: int) -> float:
        """Empty `cell` through its bottom port into the waste bottle.

        Returns:
            The volume actually recovered, in millilitres. Used both for waste
            bookkeeping and as a check: recovering far less than was dispensed
            suggests a blockage or a leak.

        Raises:
            PumpError: If the drain does not complete within the timeout.
        """

    # -- public API -------------------------------------------------------- #

    def dispense(self, channel: Channel, volume_ml: float) -> None:
        """Dispense into the cell currently under the head.

        Checks the reservoir before moving anything, so an empty bottle stops
        the run rather than quietly pumping air.

        Args:
            channel: Which fluid to dispense.
            volume_ml: How much.

        Raises:
            ReservoirError: If the reservoir lacks the requested volume.
            PumpError: If the dispense fails.
        """
        if volume_ml <= 0:
            raise ValueError("volume_ml must be positive")

        remaining = self.remaining(channel)
        if remaining < volume_ml:
            raise ReservoirError(
                f"{channel.value} reservoir has {remaining:.0f} mL remaining but "
                f"{volume_ml:.0f} mL was requested. Refill before continuing -- "
                "dispensing air would invalidate this sample and every one after."
            )

        self._dispense(channel, volume_ml)

        if channel is Channel.ELECTROLYTE:
            self._used_electrolyte_ml += volume_ml
        else:
            self._used_di_ml += volume_ml

    def drain(self, cell: int) -> float:
        """Empty a cell to waste.

        Checks waste capacity first: an overflowing waste bottle is worse than a
        stopped run, because it contaminates the bench and every remaining cell.

        Args:
            cell: Which cell to drain.

        Returns:
            Volume recovered, in millilitres.

        Raises:
            ReservoirError: If the waste bottle lacks headroom.
            PumpError: If the drain fails or times out.
        """
        headroom = self.waste_headroom_ml()
        if headroom < self.config.cell_volume_ml:
            raise ReservoirError(
                f"waste bottle has {headroom:.0f} mL of headroom, less than one "
                f"cell volume ({self.config.cell_volume_ml:.0f} mL). Empty it "
                "before continuing."
            )
        recovered = self._drain(cell)
        self._waste_ml += recovered
        return recovered

    def rinse(self, cell: int, cycles: int | None = None) -> None:
        """Rinse a drained cell with DI water.

        One cycle is dispense DI, then drain it. Repeated `cycles` times.

        The cell must already be empty of electrolyte; `Station.rinse_cell`
        enforces that through the cell state machine.

        Args:
            cell: Which cell to rinse.
            cycles: Number of cycles. Defaults to the configured value.

        Raises:
            ReservoirError: If DI or waste capacity runs short.
            PumpError: If any cycle fails.
        """
        for _ in range(cycles if cycles is not None else self.config.rinse_cycles):
            self.dispense(Channel.DI, self.config.rinse_volume_ml)
            self.drain(cell)

    # -- bookkeeping ------------------------------------------------------- #

    def remaining(self, channel: Channel) -> float:
        """Return the volume left in a reservoir, in millilitres."""
        if channel is Channel.ELECTROLYTE:
            return self.config.electrolyte_reservoir_ml - self._used_electrolyte_ml
        return self.config.di_reservoir_ml - self._used_di_ml

    def waste_headroom_ml(self) -> float:
        """Return the space left in the waste bottle, in millilitres."""
        return self.config.waste_capacity_ml - self._waste_ml

    def levels(self) -> dict[str, float]:
        """Return all three levels, for logging and pre-flight reporting."""
        return {
            "electrolyte_remaining_ml": self.remaining(Channel.ELECTROLYTE),
            "di_remaining_ml": self.remaining(Channel.DI),
            "waste_headroom_ml": self.waste_headroom_ml(),
        }

    def enough_for(self, samples: int) -> tuple[bool, str]:
        """Return whether the reservoirs can serve `samples` complete samples.

        Called by `Station.check_ready` before a batch starts. This is the check
        that turns a 30-hour unattended run from optimistic into defensible.

        Args:
            samples: How many samples the batch contains.

        Returns:
            ``(ok, explanation)``. The explanation is suitable for an operator.
        """
        electrolyte_each, di_each = self.config.volume_per_sample_ml()
        need_e = electrolyte_each * samples
        need_d = di_each * samples
        need_w = self.config.waste_per_sample_ml() * samples

        problems: list[str] = []
        if self.remaining(Channel.ELECTROLYTE) < need_e:
            problems.append(
                f"electrolyte: need {need_e:.0f} mL, "
                f"have {self.remaining(Channel.ELECTROLYTE):.0f} mL"
            )
        if self.remaining(Channel.DI) < need_d:
            problems.append(
                f"DI: need {need_d:.0f} mL, have {self.remaining(Channel.DI):.0f} mL"
            )
        if self.waste_headroom_ml() < need_w:
            problems.append(
                f"waste headroom: need {need_w:.0f} mL, "
                f"have {self.waste_headroom_ml():.0f} mL"
            )

        if problems:
            return False, "; ".join(problems)
        return True, (
            f"{samples} samples need {need_e:.0f} mL electrolyte, {need_d:.0f} mL "
            f"DI, {need_w:.0f} mL waste headroom -- all available"
        )


# --------------------------------------------------------------------------- #
# Real hardware
# --------------------------------------------------------------------------- #

class RealPumps(PumpController):
    """Peristaltic pumps driven over a serial or relay interface.

    Stubbed pending hardware.

    IMPLEMENTATION NOTES
    --------------------
    * Peristaltic pumps meter by run time: ``seconds = volume_ml / rate_ml_per_min
      * 60``. Calibrate the rate per channel by pumping into a measuring
      cylinder -- tubing bore and roller wear both shift it, and it drifts as
      tubing ages.
    * Prime each line before the first dispense of a session, or the first fill
      is short by the dead volume of the tube.
    * `_drain` should run the waste pump until flow stops, or until
      ``config.drain_timeout_s``, then raise `PumpError`. If no flow sensor is
      fitted, run for a fixed calibrated time and return the nominal volume --
      and say so in the return value's docstring, because the bookkeeping is
      then an estimate rather than a measurement.
    * Never leave a pump running on the fault path. `stop_all` must be
      unconditional.
    """

    def connect(self) -> None:
        """Open the pump interface and prime the lines."""
        raise NotImplementedError(
            "RealPumps.connect: open the pump interface, prime each channel, "
            "set self._connected = True."
        )

    def disconnect(self) -> None:
        """Stop everything and close the interface. Must not raise."""
        raise NotImplementedError(
            "RealPumps.disconnect: stop all pumps, close the handle, set "
            "self._connected = False. Swallow exceptions."
        )

    def stop_all(self) -> None:
        """Stop every pump immediately. Must not raise."""
        raise NotImplementedError(
            "RealPumps.stop_all: de-energise every pump channel unconditionally."
        )

    def _dispense(self, channel: Channel, volume_ml: float) -> None:
        """Run the channel pump for the calibrated time."""
        raise NotImplementedError(
            "RealPumps._dispense: run_seconds = volume_ml / rate * 60; energise "
            "the channel; wait; de-energise; raise PumpError on any fault."
        )

    def _drain(self, cell: int) -> float:
        """Run the waste pump until the cell is empty."""
        raise NotImplementedError(
            "RealPumps._drain: run the waste pump until flow stops or "
            "config.drain_timeout_s elapses; raise PumpError on timeout; return "
            "the volume recovered."
        )


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #

class SimulatedPumps(PumpController):
    """Pumps that exist only in software.

    Volume bookkeeping is fully live, so reservoir exhaustion can be exercised
    and demonstrated without wasting a drop.
    """

    def __init__(self, config: FluidicsConfig) -> None:
        super().__init__(config)
        self.log: list[str] = []          #: Every operation, for inspection.
        self._in_cell: dict[int, float] = {}

    def connect(self) -> None:
        """Open the simulated session."""
        self._connected = True

    def disconnect(self) -> None:
        """Close the simulated session."""
        self._connected = False

    def stop_all(self) -> None:
        """No-op: nothing is running."""
        self.log.append("stop_all")

    def _dispense(self, channel: Channel, volume_ml: float) -> None:
        """Record a simulated dispense."""
        self.log.append(f"dispense {channel.value} {volume_ml:.1f} mL")

    def _drain(self, cell: int) -> float:
        """Recover whatever the simulation believes is in the cell."""
        recovered = self._in_cell.pop(cell, self.config.fill_volume_ml)
        self.log.append(f"drain cell {cell} -> {recovered:.1f} mL")
        return recovered

    def note_fill(self, cell: int, volume_ml: float) -> None:
        """Tell the simulation how much liquid is now in a cell."""
        self._in_cell[cell] = self._in_cell.get(cell, 0.0) + volume_ml
