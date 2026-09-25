"""Writing results to local disk.

One rule governs the whole module:

    **The CSV holds numbers. The JSON holds everything needed to read them.**

A potential trace with no reference electrode is not a measurement -- potentials
only exist relative to a reference. A current trace with no exposed area cannot
become a current density. A run with no timestamp cannot be matched to a lab
notebook. None of that belongs in a CSV, and all of it is mandatory, so each
measurement gets a sidecar.

Layout on disk::

    data/
      2026-09-11_143022_TI-004_cell3/
        run_manifest.json        what happened to this coupon
        00_OCP.csv
        00_OCP.metadata.json
        01_EIS.csv
        01_EIS.metadata.json
        02_PDP.csv
        02_PDP.metadata.json

What that buys:

* One directory per coupon -- a sample's whole history is one copyable unit.
* Numeric prefixes keep execution order visible in a plain directory listing,
  so the fact that PDP ran last can be checked without opening anything.
* Sidecars sit beside their CSV, so the two cannot be separated by a careless
  copy.
* Plain text throughout, readable in fifteen years with no special software.
  Corrosion data is compared across years, so a proprietary binary format would
  be a slow-acting mistake.

Everything here is fully implemented. It has no hardware dependency, and an
unattended run that cannot save its results is destroying coupons for nothing --
so `StorageError` is Fatal.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from .config import StationConfig, StorageConfig
from .errors import StorageError
from .types import Measurement, SampleRun, utc_now

#: Bumped when the sidecar layout changes incompatibly. Recorded in every file,
#: so old data stays readable after the schema moves on.
SCHEMA_VERSION = "2.0.0"


class DataStore:
    """Writes measurements, sidecars, and manifests to the local filesystem.

    Args:
        config: Output location and formatting.
        station: Station description, for the electrode details recorded in
            every sidecar.
        software_version: Recorded in every file.
    """

    def __init__(
        self,
        config: StorageConfig,
        station: StationConfig,
        software_version: str = "2.0.0",
    ) -> None:
        self.config = config
        self.station = station
        self.software_version = software_version

    # -- run directories --------------------------------------------------- #

    def start_run(self, run: SampleRun) -> Path:
        """Create and return the directory for one coupon's run.

        The name is ``{date}_{time}_{sample}_cell{n}``, which is unique per run,
        sorts chronologically, and is self-describing in a directory listing.

        Args:
            run: The run about to begin.

        Returns:
            Path to the created directory.

        Raises:
            StorageError: If the directory cannot be created, or already exists
                with contents. Refusing to reuse a populated directory is
                deliberate: a repeated run ID must never silently overwrite an
                earlier coupon's data, because after PDP that data cannot be
                reproduced.
        """
        stamp = (run.started_at or utc_now()).strftime("%Y-%m-%d_%H%M%S")
        path = self.config.data_root / f"{stamp}_{run.sample_id}_cell{run.cell}"

        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"could not create {path}: {exc}") from exc

        if any(path.iterdir()):
            raise StorageError(
                f"{path} already exists and is not empty; refusing to overwrite "
                "previously acquired data"
            )
        return path

    # -- measurements ------------------------------------------------------ #

    def write_measurement(
        self,
        run_dir: Path,
        run: SampleRun,
        measurement: Measurement,
        index: int,
    ) -> tuple[Path, Path]:
        """Write one measurement as a CSV plus its JSON sidecar.

        The CSV is written first, the sidecar second. If the process dies
        between them, an orphaned CSV is obvious and diagnosable. The reverse
        order would leave a sidecar describing data that does not exist, which
        is worse because it looks complete.

        Args:
            run_dir: Directory for this run.
            run: The run in progress.
            measurement: The completed measurement.
            index: Position within the run, starting at 0.

        Returns:
            Paths to the CSV and the sidecar.

        Raises:
            StorageError: If either file cannot be written.
        """
        measurement.check_shape()

        stem = f"{index:02d}_{measurement.type.value}"
        csv_path = run_dir / f"{stem}.csv"
        meta_path = run_dir / f"{stem}.metadata.json"

        self._write_csv(csv_path, measurement)
        self._write_json(
            meta_path, self._metadata(run, measurement, index, csv_path.name)
        )
        return csv_path, meta_path

    def _write_csv(self, path: Path, m: Measurement) -> None:
        """Write the numeric data of one measurement."""
        fmt = self.config.float_format
        try:
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(m.columns)
                for row in m.rows:
                    writer.writerow([fmt % value for value in row])
                fh.flush()
                if self.config.fsync:
                    os.fsync(fh.fileno())
        except OSError as exc:
            raise StorageError(f"could not write {path}: {exc}") from exc

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        """Write a JSON document."""
        try:
            with path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
                fh.write("\n")
                fh.flush()
                if self.config.fsync:
                    os.fsync(fh.fileno())
        except OSError as exc:
            raise StorageError(f"could not write {path}: {exc}") from exc

    def _metadata(
        self,
        run: SampleRun,
        m: Measurement,
        index: int,
        csv_name: str,
    ) -> dict[str, Any]:
        """Assemble the sidecar for one measurement.

        Three fields carry more weight than they look:

        * ``instrument.is_simulation`` -- mandatory, so synthetic data can never
          be mistaken for measured data, six months later by someone who did not
          run it.
        * ``applied`` versus ``requested`` -- instruments silently clamp
          out-of-range settings, and storing both makes that visible.
        * ``derived.termination_reason`` -- distinguishes an OCP that converged
          from one that timed out. The traces look alike; their validity does
          not.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "sample_id": run.sample_id,
            "cell": run.cell,
            "measurement_type": m.type.value,
            "sequence_index": index,
            "csv_filename": csv_name,
            "columns": list(m.columns),
            "row_count": m.point_count,

            "sample": {
                "sample_id": run.sample_id,
                "material": run.material,
                "exposed_area_cm2": run.area_cm2,
            },
            "cell_setup": {
                "cell": run.cell,
                "reference_electrode": self.station.reference_electrode,
                "counter_electrode": self.station.counter_electrode,
                "electrolyte": run.electrolyte,
                "exposed_area_cm2": run.area_cm2,
            },
            "instrument": m.applied.get("_instrument", {}),
            "timing": {
                "started_at_utc": m.started_at.isoformat() if m.started_at else "",
                "finished_at_utc": m.finished_at.isoformat() if m.finished_at else "",
                "duration_s": m.duration_s,
            },
            "requested_parameters": _clean(m.requested),
            "applied_parameters": _clean(m.applied),
            "derived": _clean(m.derived),
            "software_version": self.software_version,
        }

    # -- manifest ---------------------------------------------------------- #

    def write_manifest(self, run_dir: Path, run: SampleRun) -> Path:
        """Write the run-level summary.

        Written on every outcome, success or failure, so an aborted run still
        leaves a record of what happened and how far it got.

        Comparing ``measurements`` against what was requested is the quick
        audit: any shortfall means something stopped early, and ``failure`` says
        what.

        Args:
            run_dir: Directory for this run.
            run: The finished or failed run.

        Returns:
            Path to the manifest.
        """
        payload = {
            "schema_version": SCHEMA_VERSION,
            "sample_id": run.sample_id,
            "cell": run.cell,
            "material": run.material,
            "exposed_area_cm2": run.area_cm2,
            "electrolyte": run.electrolyte,
            "reference_electrode": self.station.reference_electrode,
            "counter_electrode": self.station.counter_electrode,
            "ok": run.ok,
            "failure": run.failure,
            "sample_consumed": run.consumed(),
            "measurements": [
                {
                    "index": i,
                    "type": m.type.value,
                    "csv_filename": f"{i:02d}_{m.type.value}.csv",
                    "row_count": m.point_count,
                    "duration_s": m.duration_s,
                    "derived": _clean(m.derived),
                }
                for i, m in enumerate(run.measurements)
            ],
            "started_at_utc": run.started_at.isoformat() if run.started_at else "",
            "finished_at_utc": run.finished_at.isoformat() if run.finished_at else "",
            "software_version": self.software_version,
            "written_at_utc": utc_now().isoformat(),
        }
        path = run_dir / "run_manifest.json"
        self._write_json(path, payload)
        return path

    def write_batch_summary(self, runs: list[SampleRun], path: Path) -> Path:
        """Write a summary of a whole batch, for the operator's morning review.

        Args:
            runs: Every run attempted.
            path: Where to write the summary.

        Returns:
            The path written.
        """
        payload = {
            "schema_version": SCHEMA_VERSION,
            "written_at_utc": utc_now().isoformat(),
            "total": len(runs),
            "succeeded": sum(1 for r in runs if r.ok),
            "failed": sum(1 for r in runs if not r.ok),
            "consumed": sum(1 for r in runs if r.consumed()),
            "runs": [
                {
                    "sample_id": r.sample_id,
                    "cell": r.cell,
                    "ok": r.ok,
                    "failure": r.failure,
                    "measurements": [m.type.value for m in r.measurements],
                    "run_dir": str(r.run_dir) if r.run_dir else "",
                }
                for r in runs
            ],
        }
        self._write_json(path, payload)
        return path


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Drop private keys (leading underscore) from a mapping before writing."""
    return {k: v for k, v in d.items() if not k.startswith("_")}
