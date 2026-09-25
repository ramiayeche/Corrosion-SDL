"""Command-line entry point.

    python -m corrosion_sdl verify        check the build against a configuration
    python -m corrosion_sdl sequences     list the defined motion sequences
    python -m corrosion_sdl demonstrate   recorded end-to-end demonstration run
    python -m corrosion_sdl run           run a batch of coupons
    python -m corrosion_sdl teach         capture arm positions
    python -m corrosion_sdl poses         list the required positions

Every command takes ``--config``, which defaults to the target configuration
shipped with the build.

Exit codes are meaningful, so these commands can be used in a build pipeline:
0 means success, 1 means the command found a problem, 2 means it could not run
at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .errors import Fatal, Recoverable, SdlError
from .poses import required_poses
from .runner import load_batch, run_batch
from .sequences import SEQUENCES, all_pose_names
from .station import Station

DEFAULT_CONFIG = Path("config/target_configuration.json")


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def cmd_verify(args: argparse.Namespace) -> int:
    """Check that the build runs against a configuration without fatal error.

    This is the acceptance check for the delivered software. It loads the
    configuration, builds the station, homes it, runs the pre-flight checks,
    proves that every motion sequence resolves to a taught position, runs one
    complete coupon end to end, and confirms the data landed on disk.

    Returns:
        0 if everything passed, 1 otherwise.
    """
    from .protocol import SampleSpec, run_sample

    passed = failed = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  PASS   {label}")
        else:
            failed += 1
            print(f"  FAIL   {label}   {detail}")

    print("=" * 74)
    print(f"  BUILD VERIFICATION   corrosion_sdl {__version__}")
    print(f"  configuration: {args.config}")
    print("=" * 74)

    config = Config.from_file(args.config)
    for line in config.summary().split("\n"):
        print(f"  {line}")
    print()

    if args.output is not None:
        from .config import StorageConfig
        config = Config(
            station=config.station, fluidics=config.fluidics,
            protocol=config.protocol,
            storage=StorageConfig(data_root=args.output,
                                  float_format=config.storage.float_format,
                                  fsync=config.storage.fsync),
            poses_file=config.poses_file, simulate=config.simulate,
        )

    check("configuration loaded and validated", True)

    station = Station.from_config(config)
    check("station assembled from configuration", True)

    required = required_poses(config.station.cell_count, config.station.rack_slots)
    check(f"all {len(required)} required positions resolve",
          not station.poses.missing(required),
          f"missing {station.poses.missing(required)[:3]}")

    reachable = all_pose_names(config.station.cell_count, config.station.rack_slots)
    check("every motion-sequence step maps to a required position",
          reachable <= set(required),
          f"orphaned: {sorted(reachable - set(required))}")

    station.connect()
    check("all device sessions opened", True)

    try:
        station.home()
        check("both arms homed and parked", True)

        notes = station.check_ready(samples=config.station.cell_count)
        check(f"pre-flight passed for {config.station.cell_count} samples", True)
        for note in notes:
            print(f"           {note}")

        record = run_sample(
            station,
            SampleSpec(sample_id="VERIFY-001", cell=1, material="Ti-6Al-4V"),
        )
        check("one coupon ran end to end", record.ok, record.failure)
        check("three measurements acquired", len(record.measurements) == 3,
              f"got {len(record.measurements)}")

        run_dir = Path(record.run_dir) if record.run_dir else None
        check("results written to disk",
              run_dir is not None and (run_dir / "run_manifest.json").exists())

        station.safe_state()
        check("station returned to the safe state",
              station.selector.active() is None and not station.arbiter.is_held)
    finally:
        station.close()

    print()
    print("=" * 74)
    verdict = "PASS" if failed == 0 else "FAIL"
    print(f"  {verdict}   {passed} passed, {failed} failed")
    print("=" * 74)
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# sequences / poses
# --------------------------------------------------------------------------- #

def cmd_sequences(args: argparse.Namespace) -> int:
    """Print every defined motion sequence, step by step."""
    print("=" * 74)
    print(f"  DEFINED MOTION SEQUENCES   ({len(SEQUENCES)} sequences)")
    print("=" * 74)
    print("  Every arm movement the station performs belongs to one of these.")
    print("  Nothing else commands an arm.")
    for seq in SEQUENCES.values():
        print()
        cell = args.cell if seq.needs_cell else None
        arm = None if seq.arm != "either" else "arm_b"
        print(seq.describe(arm, cell))
        if seq.needs_cell:
            print(f"      (shown for cell {cell}; identical for every cell)")
        elif seq.arm == "either":
            print(f"      (shown for {arm}; identical for arm_a)")
    return 0


def cmd_poses(args: argparse.Namespace) -> int:
    """Print the positions that must be taught before the station can run."""
    config = Config.from_file(args.config)
    names = required_poses(config.station.cell_count, config.station.rack_slots)
    print(f"  {len(names)} positions must be taught for an "
          f"{config.station.cell_count}-cell station:")
    print()
    for name in names:
        print(f"    {name}")
    print()
    print("  Capture them with:  python -m corrosion_sdl teach")
    return 0


# --------------------------------------------------------------------------- #
# demonstrate
# --------------------------------------------------------------------------- #

def cmd_demonstrate(args: argparse.Namespace) -> int:
    """Run the recorded end-to-end demonstration."""
    from .demonstration import run as run_demonstration
    return run_demonstration(args.config, args.output, args.transcript)


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def cmd_run(args: argparse.Namespace) -> int:
    """Run a batch of coupons defined in a batch file."""
    config = Config.from_file(args.config)
    specs = load_batch(args.batch)

    print(f"  configuration : {args.config}")
    print(f"  batch         : {args.batch}  ({len(specs)} samples)")
    if config.simulate:
        print("  MODE          : SIMULATED HARDWARE - no physical motion will occur")
    print()

    station = Station.from_config(config)
    station.connect()
    try:
        station.home()
        for note in station.check_ready(samples=len(specs)):
            print(f"  ok  {note}")
        print()
        result = run_batch(station, specs, on_event=lambda m: print(f"  {m}"))
        print()
        print(f"  {result.summary()}")

        summary_path = config.storage.data_root / "batch_summary.json"
        station.store.write_batch_summary(result.runs, summary_path)
        print(f"  summary written to {summary_path}")
        return 0 if result.failed == 0 else 1
    finally:
        station.safe_state()
        station.close()


def cmd_teach(args: argparse.Namespace) -> int:
    """Capture arm positions by jogging."""
    from .teach import main as teach_main
    argv = ["--output", str(args.output_poses)]
    if args.simulate:
        argv.append("--simulate")
    return teach_main(argv)


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""
    p = argparse.ArgumentParser(
        prog="corrosion_sdl",
        description="Control software for the corrosion self-driving laboratory.",
    )
    p.add_argument("--version", action="version",
                   version=f"corrosion_sdl {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_config(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"configuration file (default: {DEFAULT_CONFIG})")

    v = sub.add_parser("verify",
                       help="check the build runs against a configuration")
    add_config(v)
    v.add_argument("--output", type=Path, default=None,
                   help="write verification data here instead of the configured root")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("sequences", help="list the defined motion sequences")
    s.add_argument("--cell", type=int, default=3,
                   help="cell number to show per-cell sequences for (default: 3)")
    s.set_defaults(func=cmd_sequences)

    pz = sub.add_parser("poses", help="list the positions that must be taught")
    add_config(pz)
    pz.set_defaults(func=cmd_poses)

    d = sub.add_parser("demonstrate",
                       help="recorded end-to-end demonstration run")
    add_config(d)
    d.add_argument("--output", type=Path, default=Path("demonstration_output"),
                   help="where to write data and the transcript")
    d.add_argument("--transcript", type=Path, default=None,
                   help="transcript path (default: timestamped, inside --output)")
    d.set_defaults(func=cmd_demonstrate)

    r = sub.add_parser("run", help="run a batch of coupons")
    add_config(r)
    r.add_argument("--batch", type=Path, default=Path("config/batch.example.json"),
                   help="batch definition file")
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("teach", help="capture arm positions by jogging")
    t.add_argument("--output-poses", type=Path, default=Path("config/poses.json"))
    t.add_argument("--simulate", action="store_true",
                   help="practise without hardware")
    t.set_defaults(func=cmd_teach)

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point. Converts package errors into clean messages and exit codes."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  interrupted", file=sys.stderr)
        return 130
    except Fatal as exc:
        print(f"\n  FATAL  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Recoverable as exc:
        print(f"\n  ERROR  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except SdlError as exc:
        print(f"\n  ERROR  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
