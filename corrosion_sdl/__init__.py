"""Corrosion self-driving laboratory: local control software.

Two myCobot arms, eight flat cells with fixed electrodes, one potentiostat
behind a relay board, and pumps for electrolyte, DI water, and waste. Coupons
are tested one at a time, unattended, typically overnight.

RUNNING IT
----------
    python -m corrosion_sdl verify        check the build against a configuration
    python -m corrosion_sdl sequences     list the defined motion sequences
    python -m corrosion_sdl demonstrate   recorded end-to-end demonstration run

WHERE TO START READING
----------------------
`protocol.py`. It is twenty lines and it is the entire experiment:

    station.load_sample(...)     arm B places the coupon
    station.close_cell(...)      clamp seals it, switch confirms
    station.select_cell(...)     relay connects this cell
    station.fill_cell(...)       arm A positions the head, pump dispenses
    station.park_arms()          arms retreat
    station.measure_ocp(...)     until the potential settles
    station.measure_eis(...)     biased at that potential
    station.measure_pdp(...)     destroys the coupon, so it goes last
    station.empty_cell(...)      drain to waste
    station.rinse_cell(...)      DI water
    station.open_cell(...)       release the clamp
    station.unload_sample(...)   back to the rack

Then `station.py`, which is the one object those calls go to.

THE THREE RULES THE DESIGN EXISTS TO GUARANTEE
----------------------------------------------
1. **One thing at a time.** Arms, pumps, clamps, and the potentiostat never
   overlap. Motion and switching inject noise that corrupts OCP and EIS, so the
   arbiter grants exclusive use and every station method takes it internally.

2. **PDP is last.** It destroys the coupon surface. The ordering is enforced at
   each measurement method, not by a state machine somewhere else.

3. **Never act on an assumption a sensor could confirm.** The clamp switch
   confirms the seal before any liquid is dispensed. The relay board is read
   back before any measurement. Both failures are silent otherwise, and silent
   failures produce plausible, wrong data.

FULLY LOCAL
-----------
There is no network code anywhere in this package. No endpoints, no credentials,
no telemetry. That absence is a requirement, not an oversight.
"""

__version__ = "2.0.0"

from .config import (
    Config,
    FluidicsConfig,
    ProtocolConfig,
    StationConfig,
    StorageConfig,
)
from .protocol import SampleSpec, run_sample
from .sequences import SEQUENCES, MotionSequence
from .runner import BatchResult, load_batch, run_batch
from .station import Station
from .types import (
    MEASUREMENT_ORDER,
    CellState,
    EISParams,
    Measurement,
    MeasurementType,
    OCPParams,
    PDPParams,
    SampleRun,
)

__all__ = [
    "MEASUREMENT_ORDER",
    "SEQUENCES",
    "BatchResult",
    "CellState",
    "Config",
    "EISParams",
    "FluidicsConfig",
    "Measurement",
    "MeasurementType",
    "MotionSequence",
    "OCPParams",
    "PDPParams",
    "ProtocolConfig",
    "SampleRun",
    "SampleSpec",
    "Station",
    "StationConfig",
    "StorageConfig",
    "__version__",
    "load_batch",
    "run_batch",
    "run_sample",
]
