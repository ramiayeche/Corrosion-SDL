# Corrosion SDL — Control Software

Local control software for a two-arm corrosion testing station. Eight flat cells
with fixed electrodes, one potentiostat behind a relay board, pumps for
electrolyte and DI water, and two myCobot arms. Coupons are tested one at a
time, unattended, typically overnight.

**No network code anywhere in this package.** The station runs fully offline.

---

## Quick start

Python 3.11+. No third-party dependencies.

```bash
python selftest.py                        # 151 checks
python -m corrosion_sdl verify            # build check against the target configuration
python -m corrosion_sdl sequences         # list the defined motion sequences
python -m corrosion_sdl demonstrate       # recorded end-to-end demonstration run
python -m corrosion_sdl poses             # the positions that must be taught
python -m corrosion_sdl run --batch ...   # run a batch of coupons
python -m corrosion_sdl teach             # capture arm positions by jogging
```

Everything runs against simulated hardware, so none of it needs a robot, a
potentiostat, or a pump. `verify` is the acceptance check: it loads
`config/target_configuration.json`, builds the station, homes it, runs pre-flight,
proves every motion step maps to a taught position, runs one coupon end to end,
and returns exit code 0.

---

## What the machine does

This is the whole experiment, from `corrosion_sdl/protocol.py`:

```python
station.load_sample("TI-003", cell=3)   # arm B: rack -> cell
station.close_cell(3)                   # clamp seals it, switch confirms
station.select_cell(3)                  # relay connects this cell, read back
station.fill_cell(3)                    # arm A positions head, pump dispenses
station.park_arms()                     # arms retreat clear

station.measure_ocp(3)                  # until the potential settles
station.measure_eis(3)                  # biased at that potential
station.measure_pdp(3)                  # destroys the coupon, so it goes last

station.empty_cell(3)                   # drain to waste
station.rinse_cell(3)                   # DI water, then drain
station.open_cell(3)                    # release the clamp
station.unload_sample(3)                # arm B: cell -> rack
```

No interlock blocks, no file handling, no passing results between measurements.
All of that happens inside those methods, which is the point — it cannot be
forgotten.

---

## The three rules the design exists to guarantee

**1. One thing at a time.** Arms, pumps, clamps, and the potentiostat never
overlap. Motion and relay switching inject noise that corrupts OCP and EIS, so
the arbiter grants exclusive use of the station and every `Station` method takes
it internally.

**2. PDP is last.** It destroys the coupon surface. Enforced at each measurement
method, not by a state machine hidden elsewhere.

**3. Never act on an assumption a sensor could confirm.** The clamp switch
confirms the seal before any liquid is dispensed. The relay board is read back
before any measurement. Both failures are otherwise silent, and silent failures
produce plausible, wrong data.

---

## Layout

```
corrosion_sdl/
├── __main__.py          the command line: verify, sequences, demonstrate, run
├── errors.py            two branches: Recoverable and Fatal
├── types.py             enums and records; the vocabulary
├── config.py            every setting, grouped by who owns it
├── poses.py             the pose registry — no coordinate lives anywhere else
├── sequences.py         the five defined motion sequences
├── analysis.py          OCP stability criterion
├── storage.py           CSV + JSON sidecar + manifest
├── station.py           THE FACADE — the one object workflows call
├── protocol.py          the experiment, readable top to bottom
├── runner.py            batch loop and failure policy
├── teach.py             pose capture utility for the mechanical build
├── demonstration.py     the recorded end-to-end demonstration run
└── hardware/
    ├── arbiter.py       one activity at a time
    ├── arm.py           RobotArm ABC + MyCobot (stub) + Simulated
    ├── potentiostat.py  Potentiostat ABC + Gamry (stub) + Simulated
    ├── pumps.py         three channels, reservoir tracking
    ├── relay.py         cell selector with mandatory read-back
    └── clamp.py         cell clamp with switch-confirmed seal
```

Read `protocol.py` first — twenty lines. Then `station.py`.

---

## Implemented vs stubbed

Anything that could be got right without hardware, was.

**Implemented** — verified by `selftest.py`:

| Component | Why now |
|---|---|
| `Arbiter` | It *is* the safety rule |
| `OCPStabilityMonitor` | The only feedback logic in Phase 1; pure maths |
| `DataStore` | Output format must be agreed before hardware lands |
| `PoseRegistry` | Validation catches a mis-typed angle before it reaches an arm |
| Cell lifecycle in `Station` | All ordering and sequencing guards |
| Reservoir tracking | Makes a 30-hour unattended run defensible |
| All simulated hardware | Makes the system demonstrable today |

**Stubbed** — awaiting hardware, each raising `NotImplementedError` with notes on
what is required:

`MyCobotArm` (5 methods) · `GamryPotentiostat` (8) · `RealPumps` (5) ·
`RelayBoard` (5) · `MotorClamp` (5)

---

## Bringing up real hardware

1. **Teach the poses.** Mount the arms, fix the baseplate, then
   `python -m corrosion_sdl.teach`. It walks all 34 required poses and saves
   after every capture. Practise first with `--simulate`.
2. **Fill in the drivers.** Five classes, listed above. Each carries
   implementation notes in its docstring.
3. **Set `simulate=False`** in the config. Nothing else changes.

If an arm is bumped or a fixture is re-mounted, re-teach the affected poses. No
code change.

---

## Data output

```
data/2026-09-11_143022_TI-004_cell3/
├── run_manifest.json
├── 00_OCP.csv          +  00_OCP.metadata.json
├── 01_EIS.csv          +  01_EIS.metadata.json
└── 02_PDP.csv          +  02_PDP.metadata.json
```

**The CSV holds numbers; the JSON holds everything needed to read them.** A
potential with no reference electrode is not a measurement. Measurements are
written the moment they finish, so a failure during PDP cannot lose the OCP and
EIS data already acquired — the coupon is destroyed either way.

---

## Open questions

1. **Potentiostat model** unconfirmed. The abstraction means this blocks only
   `GamryPotentiostat`, nothing else.
2. **Does draining use the bottom port or arm aspiration?** Implemented as
   bottom-port; arm aspiration adds 8 poses and a one-line change in
   `empty_cell`.
3. **Relay read-back** — if the chosen board cannot report its state, cell
   identity is unverified and must be recorded as such.
4. **Arm reach and repeatability** — 280 mm reach and ±0.5 mm against a
   reference electrode that must sit 2–3 mm from the sample, identically every
   time. Worth checking early.
