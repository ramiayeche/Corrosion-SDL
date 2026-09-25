"""All settings, in one file.

Grouped by who owns them, so it is obvious who may change what:

    StationConfig   -- the machine. Changes when hardware changes.
    FluidicsConfig  -- volumes and pump behaviour.
    ProtocolConfig  -- how experiments are run. The electrochemist's decisions.
    StorageConfig   -- where data goes.

Validation happens in `__post_init__`, so an impossible configuration fails at
construction rather than at three in the morning.

Note what is absent: there are no URLs, hostnames, API keys, or credentials
anywhere in this file or this package. The station runs fully offline, and that
absence is the requirement, not an oversight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .types import EISParams, OCPParams, PDPParams


@dataclass(frozen=True)
class StationConfig:
    """Physical description of the station.

    Attributes:
        cell_count: How many cells are on the baseplate.
        rack_slots: How many coupon positions the rack holds. Normally equal to
            `cell_count`, since each coupon returns to the slot it came from.
        arm_a_port: Serial port for the fluid-head arm.
        arm_b_port: Serial port for the sample-handling arm.
        relay_port: Serial port for the cell-selector relay board.
        reference_electrode: Description of the RE. Recorded in every metadata
            file, because a potential with no reference is not a measurement.
        counter_electrode: Description of the CE.
        move_timeout_s: How long to wait for an arm move before calling it
            failed.
        clamp_timeout_s: How long to wait for the clamp switch to confirm.
    """

    cell_count: int = 8
    rack_slots: int = 8
    arm_a_port: str = "/dev/ttyUSB0"
    arm_b_port: str = "/dev/ttyUSB1"
    relay_port: str = "/dev/ttyUSB2"
    reference_electrode: str = "Ag/AgCl (saturated KCl)"
    counter_electrode: str = "Platinum mesh"
    move_timeout_s: float = 60.0
    clamp_timeout_s: float = 15.0

    def __post_init__(self) -> None:
        if self.cell_count < 1:
            raise ValueError("cell_count must be at least 1")
        if self.rack_slots < self.cell_count:
            raise ValueError(
                f"rack_slots ({self.rack_slots}) must be at least cell_count "
                f"({self.cell_count}); every coupon needs somewhere to return to"
            )

    def cells(self) -> range:
        """Return the valid cell numbers, 1-based."""
        return range(1, self.cell_count + 1)

    def check_cell(self, cell: int) -> None:
        """Raise if `cell` is not a valid cell number."""
        if cell not in self.cells():
            raise ValueError(
                f"cell {cell} does not exist; valid cells are 1..{self.cell_count}"
            )


@dataclass(frozen=True)
class FluidicsConfig:
    """Volumes, rates, and reservoir capacities.

    The reservoir figures are what make a long unattended run safe. Eight
    samples at 50 mL of electrolyte plus two 40 mL rinses each is roughly one
    litre through the system. A waste bottle that fills at sample six either
    overflows or backs up into the cell, and either way the rest of the batch
    is lost. `Station.check_ready` totals the requirement up front and refuses
    to start a batch that cannot finish.

    Attributes:
        cell_volume_ml: Working volume of one cell.
        fill_volume_ml: Electrolyte dispensed per sample.
        rinse_volume_ml: DI water per rinse cycle.
        rinse_cycles: Rinses after draining. Must be at least one -- carrying
            corrosion products from a finished PDP into the next coupon
            contaminates it.
        dispense_rate_ml_per_min: Pump rate while dispensing.
        drain_timeout_s: Longest a drain may take before it is called a
            blockage.
        settle_time_s: Pause after filling, before measuring, to let the liquid
            come to rest and the double layer begin forming.
        electrolyte_reservoir_ml: Capacity of the electrolyte bottle.
        di_reservoir_ml: Capacity of the DI bottle.
        waste_capacity_ml: Capacity of the waste bottle.
    """

    cell_volume_ml: float = 50.0
    fill_volume_ml: float = 50.0
    rinse_volume_ml: float = 40.0
    rinse_cycles: int = 2
    dispense_rate_ml_per_min: float = 25.0
    drain_timeout_s: float = 120.0
    settle_time_s: float = 60.0
    electrolyte_reservoir_ml: float = 1000.0
    di_reservoir_ml: float = 1000.0
    waste_capacity_ml: float = 2000.0

    def __post_init__(self) -> None:
        if self.fill_volume_ml > self.cell_volume_ml:
            raise ValueError(
                f"fill_volume_ml ({self.fill_volume_ml}) exceeds cell_volume_ml "
                f"({self.cell_volume_ml}); the cell would overflow"
            )
        if self.rinse_cycles < 1:
            raise ValueError(
                "rinse_cycles must be at least 1: a cell must be rinsed before "
                "the next coupon goes in"
            )
        if self.dispense_rate_ml_per_min <= 0:
            raise ValueError("dispense_rate_ml_per_min must be positive")

    def volume_per_sample_ml(self) -> tuple[float, float]:
        """Return (electrolyte, DI) consumed by one complete sample."""
        return self.fill_volume_ml, self.rinse_volume_ml * self.rinse_cycles

    def waste_per_sample_ml(self) -> float:
        """Return the total liquid one sample sends to the waste bottle."""
        electrolyte, di = self.volume_per_sample_ml()
        return electrolyte + di


@dataclass(frozen=True)
class ProtocolConfig:
    """How experiments are run. The electrochemist's decisions.

    Attributes:
        ocp: OCP settings.
        eis: EIS settings.
        pdp: PDP settings.
        abort_on_ocp_timeout: If True, a coupon whose OCP never settles is
            abandoned before EIS and PDP. Default True, because polarising an
            unequilibrated surface consumes the coupon to produce data of
            unknown validity -- which is worse than no data, since it looks
            like a result.
        inter_measurement_delay_s: Quiet pause between measurements.
        max_consecutive_failures: How many samples may fail in a row before the
            runner stops. Three failures is a systematic fault, and carrying on
            would consume the rest of the tray for nothing.
    """

    ocp: OCPParams = field(default_factory=OCPParams)
    eis: EISParams = field(default_factory=EISParams)
    pdp: PDPParams = field(default_factory=PDPParams)
    abort_on_ocp_timeout: bool = True
    inter_measurement_delay_s: float = 10.0
    max_consecutive_failures: int = 3

    def __post_init__(self) -> None:
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1")


@dataclass(frozen=True)
class StorageConfig:
    """Where results are written.

    Attributes:
        data_root: Root of the local data tree.
        float_format: Format applied to every numeric CSV field.
        fsync: Flush each file to disk on close. Slower, but survives a power
            cut partway through an overnight run.
    """

    data_root: Path = Path("./data")
    float_format: str = "%.9g"
    fsync: bool = True


@dataclass(frozen=True)
class Config:
    """Everything, assembled.

    Attributes:
        station: The machine.
        fluidics: Volumes and pumps.
        protocol: How experiments run.
        storage: Where data goes.
        poses_file: Path to the taught joint-angle registry.
        simulate: When True, the station builds simulated hardware instead of
            connecting to real devices. This is what makes the whole system
            runnable and demonstrable before any hardware exists.
    """

    station: StationConfig = field(default_factory=StationConfig)
    fluidics: FluidicsConfig = field(default_factory=FluidicsConfig)
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    poses_file: Path = Path("./config/poses.json")
    simulate: bool = True

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        """Load a configuration from a JSON file.

        This is how the station is pointed at a particular build. The file
        shipped as ``config/target_configuration.json`` describes the Phase 1
        prototype, and is the configuration the delivered build is verified
        against.

        Unknown keys are rejected rather than ignored. A typo in a setting name
        would otherwise load silently and leave the station running on a default
        the operator believed they had changed.

        Args:
            path: Path to the configuration file.

        Returns:
            The assembled configuration.

        Raises:
            ConfigError: If the file is missing, unparseable, or contains a key
                that does not correspond to a setting.
        """
        if not path.exists():
            raise ConfigError(f"configuration file not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"could not read {path}: {exc}") from exc

        def build(kind: type, data: dict, where: str, **extra: Any) -> Any:
            """Construct a settings dataclass, rejecting unknown keys."""
            allowed = {f.name for f in fields(kind)}
            unknown = set(data) - allowed - set(extra)
            if unknown:
                raise ConfigError(
                    f"unknown setting(s) in {where}: {sorted(unknown)}. "
                    f"Valid settings are {sorted(allowed)}."
                )
            values = {k: v for k, v in data.items() if k not in extra}
            try:
                return kind(**values, **extra)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"invalid {where}: {exc}") from exc

        sections = {k: v for k, v in raw.items() if not k.startswith("_")}
        unknown_sections = set(sections) - {
            "station", "fluidics", "protocol", "storage", "poses_file", "simulate"
        }
        if unknown_sections:
            raise ConfigError(
                f"unknown top-level section(s): {sorted(unknown_sections)}"
            )

        protocol_raw = dict(sections.get("protocol", {}))
        measurement_params = {
            "ocp": build(OCPParams, protocol_raw.pop("ocp", {}), "protocol.ocp"),
            "eis": build(EISParams, protocol_raw.pop("eis", {}), "protocol.eis"),
            "pdp": build(PDPParams, protocol_raw.pop("pdp", {}), "protocol.pdp"),
        }

        storage_raw = dict(sections.get("storage", {}))
        if "data_root" in storage_raw:
            storage_raw["data_root"] = Path(storage_raw["data_root"])

        return cls(
            station=build(StationConfig, sections.get("station", {}), "station"),
            fluidics=build(FluidicsConfig, sections.get("fluidics", {}), "fluidics"),
            protocol=build(ProtocolConfig, protocol_raw, "protocol",
                           **measurement_params),
            storage=build(StorageConfig, storage_raw, "storage"),
            poses_file=Path(sections.get("poses_file", "./config/poses.json")),
            simulate=bool(sections.get("simulate", True)),
        )

    def summary(self) -> str:
        """Return a short human-readable description, for startup banners."""
        mode = "SIMULATED HARDWARE" if self.simulate else "REAL HARDWARE"
        return (
            f"{self.station.cell_count} cells, {self.station.rack_slots} rack slots  |  "
            f"{mode}\n"
            f"fill {self.fluidics.fill_volume_ml:.0f} mL, "
            f"rinse {self.fluidics.rinse_cycles}x{self.fluidics.rinse_volume_ml:.0f} mL  |  "
            f"OCP settles below "
            f"{self.protocol.ocp.stability_threshold_mv_per_min} mV/min\n"
            f"poses: {self.poses_file}  |  data: {self.storage.data_root}"
        )

    @classmethod
    def demo(cls) -> "Config":
        """Return a configuration with timings shortened for a demonstration.

        The *logic* is identical -- OCP still has to satisfy the stability
        criterion, it simply has less history to satisfy it over. Nothing about
        the ordering, interlocking, or data handling changes.
        """
        return cls(
            protocol=ProtocolConfig(
                ocp=OCPParams(
                    sample_interval_s=1.0,
                    stability_window_s=30.0,
                    stability_threshold_mv_per_min=2.0,
                    min_duration_s=30.0,
                    max_duration_s=600.0,
                ),
                inter_measurement_delay_s=0.0,
            ),
            fluidics=FluidicsConfig(settle_time_s=0.0),
            simulate=True,
        )
