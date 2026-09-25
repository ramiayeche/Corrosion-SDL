"""The potentiostat.

`Potentiostat` is the interface. `GamryPotentiostat` is the real implementation,
deliberately left as a stub because the instrument model is not yet confirmed.
`SimulatedPotentiostat` is complete and produces physically plausible data.

This abstraction is the reason an unconfirmed instrument does not block the
project. Every other module in the package is written against the interface
below, so confirming Gamry, Squidstat, PalmSens, or anything else means writing
one subclass. Nothing else changes.

CONTRACTS
---------
1. **Measurements block.** A measure call returns only when acquisition is
   complete. Concurrency is the station's business, not the driver's.
2. **Drivers never write files.** They return `Measurement` objects; `storage.py`
   decides what lands on disk. Acquisition and persistence stay separable and
   independently testable.
3. **The cell is de-energised on every exit path**, including exceptions, so a
   coupon is never left polarised by a crash.

WHEN THE HARDWARE ARRIVES
-------------------------
Fill in the eight stubs in `GamryPotentiostat`. The notes on each say what the
COM interface expects.
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from typing import Any, Callable

from ..errors import InstrumentError
from ..types import (
    COLUMNS,
    EISParams,
    Measurement,
    MeasurementType,
    OCPEnd,
    OCPParams,
    PDPParams,
    utc_now,
)


class Potentiostat(ABC):
    """One potentiostat.

    Args:
        device_id: Serial number or index. None takes the first device found,
            which is fine on a single-instrument bench.
    """

    #: Name recorded in metadata, and used to select this class.
    key: str = "abstract"

    def __init__(self, device_id: str | None = None) -> None:
        self.device_id = device_id
        self._connected = False

    # -- session ----------------------------------------------------------- #

    @property
    def is_connected(self) -> bool:
        """Return whether a session with the instrument is open."""
        return self._connected

    def assert_connected(self) -> None:
        """Raise if no session is open."""
        if not self._connected:
            raise InstrumentError(f"{type(self).__name__} is not connected")

    @abstractmethod
    def connect(self) -> None:
        """Open a session and take control of the instrument."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the session. Must be safe to call twice and must not raise."""

    @abstractmethod
    def identify(self) -> dict[str, Any]:
        """Return instrument identity for the metadata record.

        Returns:
            At minimum ``model``, ``serial_number``, ``firmware_version``,
            ``key``, and ``is_simulation``. Written verbatim into every sidecar
            so results stay traceable to the instrument that produced them.
        """

    @abstractmethod
    def cell_on(self) -> None:
        """Energise the cell (connect WE/RE/CE to the front end)."""

    @abstractmethod
    def cell_off(self) -> None:
        """De-energise the cell. Must not raise; runs on every exit path."""

    # -- measurements ------------------------------------------------------ #

    @abstractmethod
    def measure_ocp(
        self,
        params: OCPParams,
        cell: int,
        sample_id: str,
        should_stop: Callable[[float, float], bool] | None = None,
    ) -> Measurement:
        """Acquire an open-circuit-potential trace.

        Termination is decided by `should_stop`, supplied by the station from
        `analysis.OCPStabilityMonitor`. This inversion is deliberate: the driver
        owns acquisition timing, while the judgement about when a potential has
        settled lives in one testable place outside any driver.

        Args:
            params: Sampling and stability settings.
            cell: Which cell, for the record.
            sample_id: Which coupon, for the record.
            should_stop: Called after each reading with ``(elapsed_s,
                potential_v)``. Returning True ends acquisition early.

        Returns:
            A measurement whose `derived` contains at least
            ``final_potential_v`` and ``termination_reason``.
        """

    @abstractmethod
    def measure_eis(
        self,
        params: EISParams,
        dc_potential_v: float,
        cell: int,
        sample_id: str,
    ) -> Measurement:
        """Acquire an impedance spectrum at a given DC bias.

        Args:
            params: Sweep settings.
            dc_potential_v: Bias, supplied by the station from the measured OCP.
                A driver must never substitute a default here: biasing at an
                arbitrary potential silently changes what is being measured.
            cell: Which cell, for the record.
            sample_id: Which coupon, for the record.

        Returns:
            An EIS measurement with modulus and phase already derived.
        """

    @abstractmethod
    def measure_pdp(
        self,
        params: PDPParams,
        ocp_potential_v: float,
        area_cm2: float,
        cell: int,
        sample_id: str,
    ) -> Measurement:
        """Acquire a potentiodynamic polarisation curve.

        This destroys the coupon surface and cannot be repeated.

        Args:
            params: Sweep settings, relative to OCP.
            ocp_potential_v: Reference potential from the preceding OCP.
            area_cm2: Exposed area, for current density.
            cell: Which cell, for the record.
            sample_id: Which coupon, for the record.

        Returns:
            A PDP measurement including current density.
        """


# --------------------------------------------------------------------------- #
# Real hardware
# --------------------------------------------------------------------------- #

class GamryPotentiostat(Potentiostat):
    """Gamry-class potentiostat.

    Stubbed. The instrument model is not confirmed, and the bodies are left
    unwritten on purpose: guessing at a vendor API produces code that looks
    finished and does not run.

    IMPLEMENTATION NOTES
    --------------------
    Gamry instruments are driven on Windows through the ``GamryCOM`` toolkit via
    ``comtypes``. The community ``pygamry`` wrapper is a thinner surface over the
    same objects.

    * The COM interface is event-driven. Signals are started, and data arrives
      through ``GamryDtaqEvents`` callbacks pumped by ``comtypes``. The blocking
      contract above is met by pumping that loop internally until the dtaq
      completes, so callers see an ordinary synchronous call.
    * Evaluate `should_stop` inside the OCP data callback and halt the dtaq when
      it returns True.
    * Gamry reports impedance as Zreal/Zimag only. Compute modulus and phase
      here, once, so every consumer sees identical values with one sign
      convention.
    * Instruments silently clamp out-of-range settings. Read the applied values
      back after configuring and return them in `Measurement.applied` rather
      than echoing what was requested.
    * Gamry devices are exclusive-access. A stray Gamry Framework window will
      block `connect`, and the resulting error message is unhelpful -- worth
      catching and re-raising with something clearer.

    Suggested dtaqs: ``GamryDtaqOcv`` for OCP, ``GamryReadZ`` for EIS,
    ``GamryDtaqRcv`` for PDP.
    """

    key = "gamry"

    def connect(self) -> None:
        """Open the COM session and take the pstat."""
        raise NotImplementedError(
            "GamryPotentiostat.connect: CreateObject('GamryCOM.GamryDeviceList'), "
            "select by self.device_id or index 0, pstat.Init(), pstat.Open(), "
            "set self._connected = True."
        )

    def disconnect(self) -> None:
        """De-energise the cell and close the session. Must not raise."""
        raise NotImplementedError(
            "GamryPotentiostat.disconnect: cell off, pstat.Close(), set "
            "self._connected = False. Swallow exceptions."
        )

    def identify(self) -> dict[str, Any]:
        """Return model, serial number, and firmware version."""
        raise NotImplementedError(
            "GamryPotentiostat.identify: return model, serial_number, "
            "firmware_version, key='gamry', is_simulation=False."
        )

    def cell_on(self) -> None:
        """Energise via ``pstat.SetCell(GamryCOM.CellOn)``."""
        raise NotImplementedError("GamryPotentiostat.cell_on: pstat.SetCell(CellOn).")

    def cell_off(self) -> None:
        """De-energise via ``pstat.SetCell(GamryCOM.CellOff)``. Must not raise."""
        raise NotImplementedError(
            "GamryPotentiostat.cell_off: pstat.SetCell(CellOff). Swallow any COM "
            "error so the caller's original exception survives."
        )

    def measure_ocp(
        self,
        params: OCPParams,
        cell: int,
        sample_id: str,
        should_stop: Callable[[float, float], bool] | None = None,
    ) -> Measurement:
        """Acquire OCP, ending on the caller's stability criterion."""
        raise NotImplementedError(
            "GamryPotentiostat.measure_ocp: start GamryDtaqOcv at "
            "params.sample_interval_s; in the data callback append "
            "(elapsed_s, potential_v) and evaluate should_stop; halt when it "
            "returns True past min_duration_s, or at max_duration_s; populate "
            "derived with final_potential_v and termination_reason."
        )

    def measure_eis(
        self,
        params: EISParams,
        dc_potential_v: float,
        cell: int,
        sample_id: str,
    ) -> Measurement:
        """Acquire an impedance spectrum with ``GamryReadZ``."""
        raise NotImplementedError(
            "GamryPotentiostat.measure_eis: sweep start->end frequency with "
            "GamryReadZ at dc_potential_v; derive modulus and phase from "
            "Zreal/Zimag before returning."
        )

    def measure_pdp(
        self,
        params: PDPParams,
        ocp_potential_v: float,
        area_cm2: float,
        cell: int,
        sample_id: str,
    ) -> Measurement:
        """Acquire a polarisation curve with ``GamryDtaqRcv``."""
        raise NotImplementedError(
            "GamryPotentiostat.measure_pdp: convert the OCP-relative bounds to "
            "absolute potentials, sweep with GamryDtaqRcv, and record current "
            "density as current_a / area_cm2."
        )


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #

class SimulatedPotentiostat(Potentiostat):
    """A potentiostat that exists only in software.

    The signals are plausible rather than rigorous, shaped so the pipeline is
    exercised properly:

    * **OCP** relaxes exponentially toward a steady potential with noise, so
      drift genuinely falls below threshold after a realistic settling period
      and the stability monitor converges on real data.
    * **EIS** follows a Randles response -- solution resistance, charge transfer
      resistance, double-layer capacitance -- giving a recognisable Nyquist
      semicircle.
    * **PDP** follows Butler-Volmer kinetics about the corrosion potential with
      a passive plateau on the anodic branch, as a passivating alloy such as
      titanium behaves.

    Deterministic given a seed, so tests and demonstrations repeat exactly.

    Args:
        device_id: Ignored.
        seed: Seed for reproducible traces.
        settling_tau_s: Time constant of the simulated OCP relaxation.
        noise_mv: Standard deviation of the simulated potential noise.
    """

    key = "simulated"

    def __init__(
        self,
        device_id: str | None = None,
        seed: int = 20260911,
        settling_tau_s: float = 240.0,
        noise_mv: float = 0.08,
    ) -> None:
        super().__init__(device_id)
        self._rng = random.Random(seed)
        self._tau = settling_tau_s
        self._noise_v = noise_mv / 1000.0
        self._cell_live = False

        # Drawn once per instance, so one seed gives one consistent "sample".
        self._ocp_final_v = -0.220 + self._rng.uniform(-0.03, 0.03)
        self._ocp_offset_v = self._rng.uniform(0.06, 0.12)
        self._r_solution = 12.0 + self._rng.uniform(-2.0, 2.0)
        self._r_ct = 4.5e5 * (1.0 + self._rng.uniform(-0.2, 0.2))
        self._c_dl = 2.2e-5 * (1.0 + self._rng.uniform(-0.15, 0.15))

    # -- session ----------------------------------------------------------- #

    def connect(self) -> None:
        """Open the simulated session."""
        self._connected = True

    def disconnect(self) -> None:
        """Close the simulated session."""
        self._cell_live = False
        self._connected = False

    def identify(self) -> dict[str, Any]:
        """Return synthetic identity, flagged clearly as simulated."""
        return {
            "model": "Simulated Potentiostat",
            "serial_number": "SIM-0001",
            "firmware_version": "0.0.0-sim",
            "key": self.key,
            "is_simulation": True,
        }

    def cell_on(self) -> None:
        """Energise the simulated cell."""
        self.assert_connected()
        self._cell_live = True

    def cell_off(self) -> None:
        """De-energise the simulated cell. Never raises."""
        self._cell_live = False

    # -- measurements ------------------------------------------------------ #

    def measure_ocp(
        self,
        params: OCPParams,
        cell: int,
        sample_id: str,
        should_stop: Callable[[float, float], bool] | None = None,
    ) -> Measurement:
        """Generate an OCP trace that relaxes toward a steady potential."""
        self.assert_connected()
        self.cell_on()
        try:
            started = utc_now()
            rows: list[tuple[float, ...]] = []
            ended = OCPEnd.TIMEOUT

            steps = int(params.max_duration_s / params.sample_interval_s) + 1
            for step in range(steps):
                t = step * params.sample_interval_s
                v = (
                    self._ocp_final_v
                    + self._ocp_offset_v * math.exp(-t / self._tau)
                    + self._rng.gauss(0.0, self._noise_v)
                )
                rows.append((t, v))
                if (
                    should_stop is not None
                    and t >= params.min_duration_s
                    and should_stop(t, v)
                ):
                    ended = OCPEnd.STABLE
                    break

            if not rows:
                raise InstrumentError("OCP produced no data points")

            window_start = rows[-1][0] - params.stability_window_s
            window = [v for t, v in rows if t >= window_start]

            m = Measurement(
                type=MeasurementType.OCP,
                cell=cell,
                sample_id=sample_id,
                columns=COLUMNS[MeasurementType.OCP],
                rows=rows,
                started_at=started,
                finished_at=utc_now(),
                requested=vars(params).copy(),
                applied=vars(params).copy(),
                derived={
                    "final_potential_v": rows[-1][1],
                    "mean_potential_last_window_v": sum(window) / len(window),
                    "termination_reason": ended.value,
                    "elapsed_s": rows[-1][0],
                },
            )
            m.check_shape()
            return m
        finally:
            self.cell_off()

    def measure_eis(
        self,
        params: EISParams,
        dc_potential_v: float,
        cell: int,
        sample_id: str,
    ) -> Measurement:
        """Generate a Randles-like impedance spectrum."""
        self.assert_connected()
        self.cell_on()
        try:
            started = utc_now()
            rows: list[tuple[float, ...]] = []

            n = params.point_count()
            lo, hi = math.log10(params.end_frequency_hz), math.log10(
                params.start_frequency_hz
            )
            for i in range(n):
                frac = i / max(n - 1, 1)
                freq = 10 ** (hi + frac * (lo - hi))     # high to low
                w = 2.0 * math.pi * freq

                denom = 1.0 + (w * self._r_ct * self._c_dl) ** 2
                z_re = self._r_solution + self._r_ct / denom
                z_im = -(w * self._c_dl * self._r_ct ** 2) / denom
                z_re *= 1.0 + self._rng.gauss(0.0, 0.004)
                z_im *= 1.0 + self._rng.gauss(0.0, 0.004)

                rows.append(
                    (freq, z_re, z_im, math.hypot(z_re, z_im),
                     math.degrees(math.atan2(z_im, z_re)))
                )

            m = Measurement(
                type=MeasurementType.EIS,
                cell=cell,
                sample_id=sample_id,
                columns=COLUMNS[MeasurementType.EIS],
                rows=rows,
                started_at=started,
                finished_at=utc_now(),
                requested=vars(params).copy(),
                applied={**vars(params).copy(), "dc_potential_v": dc_potential_v},
                derived={
                    "dc_potential_v": dc_potential_v,
                    "r_solution_ohm_estimate": self._r_solution,
                    "r_ct_ohm_estimate": self._r_ct,
                    "point_count": len(rows),
                },
            )
            m.check_shape()
            return m
        finally:
            self.cell_off()

    def measure_pdp(
        self,
        params: PDPParams,
        ocp_potential_v: float,
        area_cm2: float,
        cell: int,
        sample_id: str,
    ) -> Measurement:
        """Generate a polarisation curve with a passive anodic plateau."""
        self.assert_connected()
        self.cell_on()
        try:
            started = utc_now()
            rows: list[tuple[float, ...]] = []

            e_start = ocp_potential_v + params.start_v_vs_ocp
            e_end = ocp_potential_v + params.end_v_vs_ocp
            rate = params.scan_rate_mv_per_s / 1000.0
            n = max(int(((e_end - e_start) / rate) / params.sample_period_s), 2)

            i_corr = 2.0e-8 * area_cm2
            beta_a, beta_c = 0.12, 0.16
            e_passive = ocp_potential_v + 0.30
            i_passive = 8.0e-7 * area_cm2

            for i in range(n):
                t = i * params.sample_period_s
                e = e_start + rate * t
                eta = e - ocp_potential_v

                i_a = i_corr * (10 ** (eta / beta_a))
                i_c = i_corr * (10 ** (-eta / beta_c))
                if e > e_passive:
                    i_a = i_passive * (1.0 + 0.35 * (e - e_passive))

                current = (i_a - i_c) * (1.0 + self._rng.gauss(0.0, 0.02))
                current = max(min(current, params.current_limit_a),
                              -params.current_limit_a)
                rows.append((t, e, current, current / area_cm2))

            m = Measurement(
                type=MeasurementType.PDP,
                cell=cell,
                sample_id=sample_id,
                columns=COLUMNS[MeasurementType.PDP],
                rows=rows,
                started_at=started,
                finished_at=utc_now(),
                requested=vars(params).copy(),
                applied={
                    **vars(params).copy(),
                    "start_potential_v": e_start,
                    "end_potential_v": e_end,
                    "ocp_reference_v": ocp_potential_v,
                    "area_cm2": area_cm2,
                },
                derived={
                    "i_corr_a_estimate": i_corr,
                    "e_corr_v_estimate": ocp_potential_v,
                    "point_count": len(rows),
                    "sample_consumed": True,
                },
            )
            m.check_shape()
            return m
        finally:
            self.cell_off()
