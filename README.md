# hts-orchestrator

Spec-to-protocol platform for an automated high-throughput screen: iDOT nanolitre
compound dispensing, Opentrons Flex liquid handling, robot-arm plate transfers,
a 72 h spheroid incubation and a CellTiter-Glo 3D luminescence readout.

A scientist writes one YAML spec. The platform validates it, builds a seeded random
plate map, and generates an iDOT worklist plus three Opentrons Flex protocols, each
checked with the Opentrons simulator. Nobody edits instrument code.

## Run it

```
pip install pyyaml numpy "scipy>=1.14,<1.15" lxml pytest opentrons
python scripts/fetch_animl_schemas.py        # optional: enables the validated AnIML export
python -m htsorch validate specs/ctg3d_screen.yaml
python -m htsorch build specs/ctg3d_screen.yaml --out out
python -m htsorch run specs/ctg3d_screen.yaml --out out --operator "your.name"
python -m pytest
```

`run` builds the artifacts, runs the workflow on the simulated devices (the engine stops at the 72 h
incubation, saves its state, and is continued with the clock moved to the wake-up) and writes the data
package described below. The workflow engine and its failure scenarios are described under
"Workflow engine" further down; for example:

```
python -m htsorch run specs/ctg3d_screen.yaml --out out --scenario scenarios/lost_plate.yaml
python -m htsorch run specs/ctg3d_screen.yaml --out out --scenario scenarios/nobody_there.yaml    # pauses, exit code 2
python -m htsorch resume out/HTS-CTG3D-001/state.json --operator "A.Operator" --decision retry --until-wake
python -m htsorch run specs/ctg3d_screen.yaml --out out --stop-at-wait    # stops at the incubation; resume later
```

Exit codes: 0 complete (package written), 2 waiting or paused for a person, 3 aborted (no package).
Simple fault injection still works: `--extra-compound-wait-min 55 --extra-incubation-h 3`.

Demo specs (each is the main spec with one change):

| Spec | Result |
|---|---|
| `example_fails_well_volume.yaml` | blocked, V002: 100 uL exceeds plate working volume |
| `example_fails_dmso.yaml` | blocked, V001: 50 nL into 40 uL is 0.125% DMSO, not below the 0.1% limit |
| `example_variable_volume.yaml` | builds: stock series 1:3.5 against a 1:4 final series, so the iDOT volume varies from 39 to 22.9 nL |
| `example_fails_idot_min_volume.yaml` | blocked, V003: volume falls to 12.3 nL, below the 20 nL iDOT minimum |
| `example_fails_reservoir.yaml` | blocked, V020: reagent does not fit the trough |
| `example_fails_2d_lysis.yaml` | blocked, V044: 2 min shake / 10 min incubation is a 2D protocol |
| `example_edge_controls.yaml` | builds: perimeter holds controls only |
| `example_edge_pbs_too_many.yaml` | blocked, V010: 384 conditions, 308 interior wells |
| `example_edge_pbs_timing.yaml` | blocked, V040 and V041: fits the plate, but 1-channel seeding takes so long that the compound wait and the cell hold windows are both missed |

## The experiment

| Item | Value |
|---|---|
| Plate | 1 x 384-well ULA round-bottom, randomized, stratified edge/interior |
| Tests | 20 compounds x 5 concentrations (1:4 series, 10 uM top) x 3 replicates = 300 wells |
| Controls | 42 negative (DMSO solvent) + 42 positive (kill), scattered |
| Compound step | 30 nL per well from the prep plate (iDOT); volume per well is derived from the stock series (minimum 20 nL), identical across the shipped spec |
| Assay volume | 20 uL media + 20 uL cells = 40 uL, final DMSO 0.075% (limit: below 0.1%, warning above 0.05%) |
| Readout | 40 uL CellTiter-Glo 3D (1:1), 80 uL total |

## Edge policy (evaporation over 72 h)

`assay.edge.policy` decides what the 76 perimeter wells hold:

| Policy | Perimeter | Consequence |
|---|---|---|
| `assay` | normal conditions, stratified | all 384 wells usable; edge effects must be handled in analysis |
| `controls` | controls only (neg/pos alternating); extra controls go inside | all 300 test wells sit in the interior; still 8-channel seeding |
| `pbs_fill` | PBS, no cells or compound (unused wells too) | only 308 wells usable; adds `flex_0_edge_fill.py`; seeding switches to the 1-channel pipette |

Why `pbs_fill` seeds with the 1-channel: an 8-channel on a 384-well plate cannot skip single
wells (rows A and P share a pass with interior rows). Under the placeholder timing figures
this makes the cell hold window the limiting step, and the validator says so (V041).
Options are a 96-channel head, a longer validated hold time, or the `controls` policy.
PBS wells still receive reagent at readout (8-channel, all wells) and act as background blanks.

## CellTiter-Glo 3D lysis check

Spheroids need vigorous shaking and a longer incubation than 2D cultures. The spec states
`shake_min`, `incubate_min` and the required minimums (`shake_min_required: 5`,
`incubate_min_required: 25`, to be verified against the vendor protocol). V044 blocks the
build if either is short or if any of the four values is missing. The generated readout
protocol repeats the times, and the engine runs exactly the stated shake and lysis times (a stalled shaker restarts the full shake, never a shortened one).

## Pre-flight checks (V-codes)

DMSO % (V001), iDOT dispense volume floor (V003), well volume (V002), plate coverage (V010), pipette ranges (V011),
layout fit (V012), edge policy (V013/V052), PBS volume (V014), reagent shake and incubation (V044), reservoir volume incl. dead volume (V020), tips (V021),
iDOT source-well volume (V030), timing windows (V040-V043), plate-map integrity (V050/V051),
master-stock volume (V060). Every finding states what happened, what the system did and who must act.

## Workflow engine

`engine.py` runs the screen step by step on the device layer and plate custody described below.
`run` uses it; the saved state file lets another process continue the run.

| What | How |
|---|---|
| Timing windows | Enforced while the run happens. Before each seeding step the engine predicts the compound wait (V040/D040), cell age (V041/D041) and reagent lag (V042/D042) from the current time. If a window cannot be met, a person decides before more cells are committed: continue (a deviation) or abort (plates quarantined). The decision takes time too, and that time counts. |
| Single timing model | `timing.py` serves the validator's estimates and the engine's measurements. On the main spec both give 16.4 min / 12.2 min / 14.1 min (a test asserts they agree). |
| Faults | Looked up in the recovery table (`policy.py`): **retry** (system, up to N times), **operator_fix** (reload tips, refill, reload iDOT source, find the plate, load balance plate, acknowledge an excursion; then the step resumes with only what is left), or **stop** (a person decides). A fix that does not hold pauses the run after 3 tries. |
| Resuming half-done steps | A Flex step stopped by a shortage resumes with the missing tips and liquid only; an iDOT dispense resumes with the short wells only, so nothing is dosed twice. Per-well times follow the real segments, so a pause in the middle of seeding flags only the wells served after it. |
| The 72 h wait | `incubator_store` saves the state with a wake-up time and the process may exit. `resume` loads it, checks the event hash chain, custody, and that the spec has not changed, then continues. |
| Pauses | If nobody is available, the run pauses with the reason saved. `resume --operator NAME --decision retry|abort|continue` continues. Time passing while paused is recorded and ages the cells. |
| Deviations in the data | D040-D043 (windows, incubation), **D050** a person intervened, **D051** plate custody was uncertain, **D052** incubator excursion. Plate flags (C001/C002/Q001/Q002/E401) and the custody log are in the package under `audit/`. |

### Scenario runner

```
python -m htsorch scenarios                 # runs every file in scenarios/; exit 0 if all behave as expected, else 1
python -m htsorch scenarios -v              # also prints what each scenario saw
```

A scenario file (`scenarios/*.yaml`) says what goes wrong, how the person at the bench behaves, and **what must
happen**. A scenario with no `expect` block fails: a scenario that checks nothing proves nothing.

| Key | Meaning |
|---|---|
| `description` | one line, shown in the report |
| `faults` | injected faults (code, device, on the n-th call, how many times, optional detail) |
| `inventory` | **real shortages without code**: `tips: {group: n_left}`, `reservoirs: {name: {fraction: 0.3}}` or `{volume_ul: n}`, `idot_sources: {well: {usable_ul: n}}`. The device then runs out by itself (E101, E102, E202). Unknown names are errors. |
| `operator`, `policy`, `delays` | simulated person (availability, response time, decisions), recovery table overrides, handoff delays |
| `then` | scripted steps after the first stop: `advance_min`, `until_wake`, `wake_delay_h`, `respond: {actor, decision, note}` |
| `expect` | `status` (required: complete / waiting / paused / aborted), and optionally `faults`, `deviations`, `interventions`, `plate_flags`, `alerts`, `paused_reason_contains`, `resumed_steps`, `events_include`, `events_absent`, `package` |

Beyond the scenario's own `expect`, the runner checks invariants on every run: custody and event chains verify,
the gripper is empty, the iDOT never dispenses more than planned (exactly the plan when complete), every fault was
answered, the saved state reloads to the same status, devices are idle and plates complete on a finished run, an
aborted run leaves no active plate, and the data package builds and verifies. Tests prove the runner catches
wrong expectations, double dosing and custody damage.

Shipped scenarios (15):

| Scenario | Result |
|---|---|
| `transient_faults` | completes, no deviations: three instruments hiccup and the system retries |
| `stuck_arm` | arm fails the same handoff four times; retries run out; operator decides to retry; D050 |
| `lost_plate` | plate lost on the way to the centrifuge; operator finds it; flags C001/C002; D050, D051 |
| `incubator_excursion` | E401 at wake-up; operator acknowledges; plate flagged; D050, D052 |
| `late_handoff_abort` | a 50 min delay would break the 60 min window; operator aborts before cells are dispensed |
| `nobody_there`, `nobody_there_then_found` | lost plate with no operator: the run pauses; then someone answers |
| `low_tips`, `short_reagent`, `short_cells_pause`, `idot_source_short` | staged shortages (E101, E102, E202); fix and resume with only what is missing |
| `wrong_plate_abort` | wrong plate found; operator aborts |
| `late_wake_within_tolerance`, `late_wake_beyond_tolerance` | late reagent addition: tolerated, or D043 |
| `stops_at_incubation` | the run stops at the 72 h wait with state saved |

The simulated operator (`policy.SimOperator`) has a response time, can be unavailable (for all faults or
for chosen codes), decides window breaches and stops, and says where a lost plate turns up.

### Device layer and plate custody

Tested on their own and together (`tests/test_devices.py`, `tests/test_custody.py`, `tests/test_lifecycle.py`).

**Devices** (`devices.py`): the Flex, iDOT, arm, incubator, centrifuge, shaker and reader all expose one
call, `device.execute(command, **params)`. The simulated classes can be swapped for real drivers
(vendor API, OPC-UA client) without changing the workflow. They keep real inventories:

| Device | What it tracks | Real (not injected) faults |
|---|---|---|
| Flex | tips per rack, liquid per reservoir above dead volume | E101 tips run out, E102 liquid below dead volume |
| iDOT | volume per source well above dead volume | E202 source well short |
| Incubator | start time per plate (survives a restart) | E402 no free slot |
| Centrifuge | balance plate loaded | E501 no balance plate |
| Reader | plate barcode against the expected plate | E602 wrong plate |

A fault puts the device in `error`; it refuses commands (E001) until `recover()`. The system may clear
**transient** faults itself; **operator** faults raise `RecoveryNotPermitted` until a named person acts.
Every fault costs simulated time (`minutes_lost`), so a retry eats into the timing windows.
Maintenance commands (reload tips, refill) work while a device is in error.

**Fault injection** (`faults.py`): rules fire on the n-th call of `device.command`, for `times` calls
in a row, so a retry after one failure succeeds unless the rule says otherwise:

```python
FaultInjector([{"device": "arm", "command": "move", "code": "E302", "at": 5},
               {"device": "reader", "command": "read", "code": "E601", "times": 2}])
```

Catalogue: E101 E102 E103 (Flex), E201 E202 (iDOT), E301 handoff failed before pick-up, E302 plate lost in
transit (arm), E401 incubator excursion, E402 door/slot, E501 rotor, E601 E602 (reader), E701 shaker.
Each has a kind (transient/operator), a plate risk (none, partial_step, uncertain, environment),
a time cost and a hint, so the recovery policy in the next milestone can be a table.

**Plate custody** (`custody.py`): every plate is in exactly one place, and illegal requests are refused with a
code and logged, not applied.

| Rule | Code |
|---|---|
| unknown plate / duplicate plate or barcode | K001 / K011 |
| station full; gripper already holds a plate | K003 / K004 |
| plate type not allowed at a station (prep plate in the incubator) | K005 |
| step out of order; step at the wrong station | K006 / K007 |
| plate quarantined or discarded | K008 |
| action needs a named person, not the system | K009 |
| plate in the gripper or at an unknown location | K010 |

Moves are two-phase (`begin_move`, then `complete_move`, `abort_move` or `mark_lost`) and
`transfer_plate` ties the arm to custody: a failed handoff before pick-up leaves the plate at its source,
a plate lost in transit becomes `unknown`, gets flag **C001**, and nothing can touch it until a person
confirms where it is (flag **C002**). Quarantine adds **Q001/Q002**. Flags stay with the plate for the data package.

The custody log is hash-chained, and `verify()` also checks the current state against the last log entry
for every plate. A random-sequence test (thousands of requests, most of them illegal) asserts that no
sequence can break the invariants.

## Data package (FAIR-style output of Day 3)

`python -m htsorch run` ends with one folder instead of a bare CSV:

```
package-<experiment>-<run uuid>/
  datapackage.json      identifiers, provenance, timeline, flags, QC, data dictionary, file hashes
  wells.csv             one row per well: raw RLU, % of control, per-well timings, flags
  reader_export.csv     the reader file as ingested (plate barcode and mode are checked)
  analysis/curve_fits.csv
  animl/experiment.animl        AnIML export, validated against the AnIML XSDs (needs the schemas)
  animl/techniques/*.atdd       project-specific Technique Definition for the luminescence read
  inputs/               spec, plate map, iDOT worklist and the Flex protocols that were used
  simulation_truth.json synthetic ground truth, kept out of the main record
```

| FAIR aspect | What the package does |
|---|---|
| Findable | run UUID, experiment id, plate barcodes in `datapackage.json` |
| Accessible | open formats only (JSON, CSV, YAML, Python); `python -c "from htsorch.datapackage import verify_package"` re-checks every hash and the event chain |
| Interoperable | data dictionary with units (UCUM-style: `umol/L`, `min`, `RLU`) |
| Reusable | operator, software versions, protocol and spec hashes, instrument states, limitations; license field is left UNSPECIFIED on purpose |

Flags travel with the data. Pre-flight warnings (for example V001 DMSO, V042 reagent lag) and
run-time deviations (D040 compound-to-cells wait, D041 cell age, D042 reagent lag, D043 incubation)
are listed in `flags` with a plain-language interpretation and the affected wells, and every
`wells.csv` row carries the flag codes that apply to it. A reviewer looking at an odd result can
tell process from biology: each well also has its own `compound_to_cells_min`, `cell_age_min` and
`reagent_offset_min`.

The event log is hash-chained, so editing a recorded step is detected by `verify_package`.

### AnIML export

When the AnIML schemas are present, `run` also writes `animl/experiment.animl`: two plate
samples (with the plate layout), a run-provenance step (run UUID, operator, hashes, simulated
instrument states), one experiment step per process step, the raw luminescence, normalised
data, curve fits, plate QC, and the traceability flags (including their interpretation notes
and affected wells) as results, and an audit-trail entry. Each document is checked against the
published XSDs, and a project-level check confirms the document follows its technique definition.
The export aborts, and leaves no package folder, if validation fails.

What the validation does and does not mean:

- Schemas come from the AnIML project repository (https://github.com/AnIML/schemas), version `0.90`
  with a `core:draft:0.90` namespace. This work makes no claim about the final ASTM E1947 text.
- It is schema validation plus a small conformance check of our own. It is not the official AnIML
  validator, and it does not prove that other AnIML software will read the file as intended.
- The Technique Definition is ours, written for this demo; it is not a community-published one.
- Documents are not digitally signed. Undefined values (for example blank PBS wells) are written as `NaN`.
- Only min, %, uM and RLU carry SI definitions. Volume units are labels only, because the schema
  does not say how factor and exponent combine.
- The schema files are not bundled (no licence file was found in the repository); fetch them with
  `scripts/fetch_animl_schemas.py`. Without them `--animl auto` skips the export and says why,
  `--animl require` fails, and `--animl off` disables it.

Other honest limits: `datapackage.json` is our own JSON layout, not a published standard.
The simulated reader values do not react to delays, pauses or faults: the flags are real record-keeping,
but the synthetic signal is not degraded by them.

## Identify (business case)

`python -m htsorch identify` computes, from the experiment spec and `identify/assumptions.yaml`: the manual hands-on time per plate by step,
an automation-fit score (weighted criteria, five of them computed from the spec), and cost, payback, NPV and the plates per week needed to
pay back in 3 years. Every assumption is marked `source` (with URL), `spec` or `placeholder`; the placeholders are listed in the output so
nobody mistakes them for facts. Compared with everything by hand (compound dosing 98 min against a 60 min window), results on the shipped assumptions: 5.6 h manual vs 1.7 h automated hands-on per plate, fit 86% (strong), payback 3.6 years at 4 plates a week for the full system (break-even 4.6 plates a week) and 1.9 years for Flex and iDOT without the arm (break-even 2.6). A reference case (iDOT dosing, people do the rest) is also reported. The iDOT run time is taken from the spec (`timing.idot_dispense_min`, 15 min, the upper end of 10-15). The payback is very
sensitive to the manual timings and prices, so replace the placeholders with a stopwatch measurement and real quotes.

## Validation package

`validation/` holds a mini validation package: URS (20 requirements), a risk assessment (18 risks, S x O x D before and
after controls), an OQ protocol (23 cases) and a generated traceability matrix and OQ report. `python -m htsorch qualify`
checks that every requirement, risk and error code is covered by a test, runs the tests, and writes `validation/report/`.
See `validation/README.md` for what each document means and what this is not (no real hardware, so not a GxP validation).

## What this is not

- Not run on hardware. Protocols are checked with `opentrons_simulate` only.
- The iDOT worklist is our own representation, not verified against the vendor import format.
- The ULA round-bottom plate uses a stand-in Opentrons labware definition.
- Devices model state, inventories and faults, not instrument physics or vendor protocols; the engine
  never talks to real instruments. The person at the bench is simulated.
- An injected fault that carries no detail (for example E103 or E201) is treated as having done no work,
  so the step repeats in full. Partial progress is only modelled for real shortages (tips, liquid, iDOT source volume).
- Cell prep, the shaker and the reader barcode check are simplified; durations of centrifuge and read are placeholders.
- All reader values, timestamps and instrument states in the data package are simulated.
- Limits marked `VERIFY` in the spec (plate working volume, reagent ratio, pipette minimums,
  iDOT dead volume, shake/incubation time) are placeholders to check against vendor documents.
- Timing figures are estimates, not measurements.
- Practice for qualification-ready work, not GxP validated.

## Demo (`demo/`)

- `index.html`: one-page case study in the style of the author's home page: plain-language overview, workflow flowcharts, an interactive 15-scenario replay, evidence, honest limits and run instructions. Put a screen recording named `hts-orchestrator-demo.mp4` beside it to enable the video card.
- `capture.sh` re-records the real command output into `demo/capture/`; `build_site.py` rebuilds `index.html` from `site_parts/` and that output.

## Next milestones

1. A persisted audit log across runs.
2. Optional: PyLabRobot backend, OPC-UA exposure of device states, LLM-assisted spec drafting.

## Web interface (Streamlit)

```bash
pip install -r requirements.txt      # use Python 3.13
streamlit run streamlit_app.py
```

Three tabs: design and check the spec (live validation, dose table, plate map), run with the operator in the loop
(pick a failure scenario, or take the operator desk yourself), and results (QC, dose-response, plate readout, data-package
download). Free hosting: push this folder to a public GitHub repo and deploy `streamlit_app.py` on Streamlit Community Cloud.
Devices are simulated; the Opentrons simulator is not needed.
