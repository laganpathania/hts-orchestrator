# OQ report: hts-orchestrator 0.1-demo

**Verdict: PASS** (23 of 23 OQ cases passed, 0 review problem(s))

Intended use: Plan, check and run (in simulation) an unattended 384-well 3D spheroid compound screen with iDOT, Opentrons Flex, a robot arm and a luminescence reader, and produce a traceable data package. Simulated devices only; no GxP use.

This system and all instruments are simulated. The report shows the software behaves as specified; it is not a GxP validation.

## Installation record

- python: 3.13.16
- platform: Linux-6.18.44-fc-v153-x86_64-with-glibc2.39
- spec_sha256: 0d2f9e321060abf61d534a97aea66749a0363453aa9a0872fc1d00faacf12dc6
- code_and_documents_sha256: 179f75f76626ffa12e7edf1638db713bed77abb24b4c73bcf0c3f6338b1504a7
- executed_at: 2026-10-26T15:50:26+00:00
- packages: {'numpy': '1.26.4', 'scipy': '1.14.1', 'lxml': '6.1.3', 'yaml': '6.0.3', 'opentrons': '10.1.0', 'pytest': '9.1.1'}

## Document review

Every requirement and risk is covered, every code is provoked, every referenced test exists.

## Risk assessment

| Risk | Failure | S | RPN before | RPN after | Level after | Controls |
|---|---|---|---|---|---|---|
| R-01 | Compound dosed twice or not at all after an interrupted dispense | 5 | 60 (high) | 10 | low | E201, E202, V030, resume with only the short wells |
| R-02 | Plate lost or dropped in transit and the run carries on | 5 | 40 (medium) | 5 | low | E302, K009, K010, C001, C002, D051 |
| R-03 | Plate placed in the wrong place or out of order | 4 | 36 (medium) | 4 | low | K003, K004, K005, K006, K007, K012, K013, K014 |
| R-04 | Cells wait too long before dosing or reagent is added too late | 4 | 48 (high) | 8 | low | V040, V041, V042, D040, D041, D042, window check before cells are committed |
| R-05 | Incubation time off target | 4 | 36 (medium) | 8 | low | V043, D043 |
| R-06 | Incubator temperature excursion during the 72 h | 4 | 32 (medium) | 8 | low | E401, D052 |
| R-07 | Lysis or shaking too short for 3D spheroids | 4 | 48 (high) | 8 | low | V044, E701, shake restarts in full |
| R-08 | Edge effect from evaporation over 72 h | 3 | 36 (medium) | 12 | low | V013, V050, V051, V052, edge policy and stratified controls |
| R-09 | DMSO concentration toxic to cells | 4 | 24 (medium) | 4 | low | V001, V002 |
| R-10 | Tips, reservoir liquid or iDOT source run out in the middle of a step | 3 | 36 (medium) | 6 | low | V020, V021, V030, V060, E101, E102, E202 |
| R-11 | Run state or spec changed between day 0 and day 3 | 5 | 40 (medium) | 5 | low | spec hash check, state hash chain, custody chain check on resume |
| R-12 | Audit trail edited or deleted | 5 | 50 (high) | 5 | low | hash-chained event log, hash-chained custody log, package verification |
| R-13 | Nobody available when a fault needs a person | 4 | 48 (high) | 8 | low | pause with saved reason, exit code 2, resume only with a named person, D050 |
| R-14 | Wrong plate in the reader or a reader file for another plate | 5 | 40 (medium) | 5 | low | E602, barcode check on import |
| R-15 | Retry loop that never ends | 3 | 18 (low) | 3 | low | max_retries, max_fixes, E001 |
| R-16 | Simulated data mistaken for real data | 5 | 30 (medium) | 5 | low | simulated flag in package, simulation truth kept out of the record |
| R-17 | Generated instrument program does not load or does the wrong thing | 4 | 36 (medium) | 4 | low | Opentrons simulate, worklist vs plate map check, protocol hashes in the package |
| R-18 | Quarantined plate used by mistake | 4 | 24 (medium) | 4 | low | Q001, Q002, K008, K009 |

## OQ cases

| Case | Title | Result |
|---|---|---|
| OQ-01 | Spec limits block an unworkable experiment | pass |
| OQ-02 | Findings say what, where and who | pass |
| OQ-03 | Time windows are predicted before the run | pass |
| OQ-04 | Windows are enforced during the run | pass |
| OQ-05 | Plate map completeness and balance | pass |
| OQ-06 | Plate map and files are reproducible | pass |
| OQ-07 | Instrument programs load and match the plan | pass |
| OQ-08 | Lysis chemistry limits | pass |
| OQ-09 | Plate custody rules | pass |
| OQ-10 | Lost plate | pass |
| OQ-11 | Quarantine and release | pass |
| OQ-12 | Device fault model | pass |
| OQ-13 | Retries and fixes are limited | pass |
| OQ-14 | Shortages and resuming half-done steps | pass |
| OQ-15 | Incubator excursion | pass |
| OQ-16 | Wrong plate at the reader | pass |
| OQ-17 | Persistence across the 72 h wait | pass |
| OQ-18 | Audit trail integrity | pass |
| OQ-19 | Data package content | pass |
| OQ-20 | AnIML export | pass |
| OQ-21 | Analysis against known truth | pass |
| OQ-22 | Command line outcomes | pass |
| OQ-23 | Scenario runner catches wrong behaviour | pass |

## Traceability (requirement -> case)

| URS | Requirement | Risks | Case | Codes | Result |
|---|---|---|---|---|---|
| URS-01 | Experiment spec is checked before any run | R-09 R-10 | OQ-01 | V001 V002 V010 V011 V012 V020 V021 V030 V060 | pass |
| URS-01 | Experiment spec is checked before any run | R-09 R-10 | OQ-02 |  | pass |
| URS-01 | Experiment spec is checked before any run | R-09 R-10 | OQ-14 | E101 E102 E202 | pass |
| URS-02 | Time windows are predicted before the run and enforced during it | R-04 R-05 | OQ-03 | V040 V041 V042 V043 V045 | pass |
| URS-02 | Time windows are predicted before the run and enforced during it | R-04 R-05 | OQ-04 | D040 D041 D042 D043 | pass |
| URS-03 | Plate map is complete, unique and balanced | R-08 | OQ-05 | V013 V014 V050 V051 V052 | pass |
| URS-04 | Plate map is reproducible | R-17 | OQ-06 |  | pass |
| URS-05 | Instrument programs are generated and proven loadable | R-17 | OQ-07 |  | pass |
| URS-06 | Assay chemistry limits are respected | R-07 | OQ-08 | V044 | pass |
| URS-07 | A plate is always in exactly one known place | R-02 R-03 | OQ-09 | K001 K003 K004 K005 K006 K007 K008 K009 K010 K011 K012 K013 K014 K015 | pass |
| URS-08 | A lost plate never goes unnoticed | R-02 | OQ-10 | E302 C001 C002 D051 K009 | pass |
| URS-09 | Plates can be quarantined and released only by a person | R-18 | OQ-11 | Q001 Q002 K008 K009 | pass |
| URS-10 | Faults are handled by policy | R-01 R-06 R-07 R-10 R-13 R-15 | OQ-12 | E001 E103 E201 E301 E402 E501 E601 E701 | pass |
| URS-10 | Faults are handled by policy | R-01 R-06 R-07 R-10 R-13 R-15 | OQ-13 | D050 | pass |
| URS-10 | Faults are handled by policy | R-01 R-06 R-07 R-10 R-13 R-15 | OQ-14 | E101 E102 E202 | pass |
| URS-10 | Faults are handled by policy | R-01 R-06 R-07 R-10 R-13 R-15 | OQ-15 | E401 D052 | pass |
| URS-11 | Half-done steps resume without repeating work | R-01 R-10 | OQ-14 | E101 E102 E202 | pass |
| URS-12 | The 72 h wait survives a restart and cannot be tampered with | R-05 R-11 | OQ-17 |  | pass |
| URS-13 | The audit trail is attributable, ordered and tamper-evident | R-11 R-12 | OQ-18 |  | pass |
| URS-14 | Deviations are recorded where the data are | R-04 R-05 R-06 R-13 | OQ-04 | D040 D041 D042 D043 | pass |
| URS-14 | Deviations are recorded where the data are | R-04 R-05 R-06 R-13 | OQ-15 | E401 D052 | pass |
| URS-14 | Deviations are recorded where the data are | R-04 R-05 R-06 R-13 | OQ-19 |  | pass |
| URS-15 | The data package is complete and verifiable | R-12 R-16 R-17 | OQ-19 |  | pass |
| URS-16 | Results are exported in AnIML |  | OQ-20 |  | pass |
| URS-17 | Analysis is checked against known truth |  | OQ-21 |  | pass |
| URS-18 | Wrong data cannot enter by accident | R-14 | OQ-16 | E602 | pass |
| URS-19 | Run outcomes are unambiguous for automation | R-13 | OQ-22 |  | pass |
| URS-20 | Failure behaviour is regression-tested |  | OQ-23 |  | pass |

## Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Author | | | |
| Reviewer | | | |
| Approver | | | |
