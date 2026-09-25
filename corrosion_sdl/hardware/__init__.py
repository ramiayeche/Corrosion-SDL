"""Hardware interfaces and their simulated counterparts.

Every device follows the same three-part pattern:

    an abstract base class   the interface the station uses
    a real implementation    stubbed until the hardware arrives
    a simulated one          complete, so the system runs today

That pattern is why the unconfirmed potentiostat and the undelivered arms do not
block the rest of the project. The station talks only to the interfaces, so
filling in a real implementation changes exactly one file.

The arbiter lives here too, because it governs all of them.
"""

from .arbiter import Arbiter
from .arm import MyCobotArm, RobotArm, SimulatedArm
from .clamp import CellClamp, MotorClamp, SimulatedClamp
from .potentiostat import (
    GamryPotentiostat,
    Potentiostat,
    SimulatedPotentiostat,
)
from .pumps import Channel, PumpController, RealPumps, SimulatedPumps
from .relay import CellSelector, RelayBoard, SimulatedSelector

__all__ = [
    "Arbiter",
    "CellClamp",
    "CellSelector",
    "Channel",
    "GamryPotentiostat",
    "MotorClamp",
    "MyCobotArm",
    "Potentiostat",
    "PumpController",
    "RealPumps",
    "RelayBoard",
    "RobotArm",
    "SimulatedArm",
    "SimulatedClamp",
    "SimulatedPotentiostat",
    "SimulatedPumps",
    "SimulatedSelector",
]
