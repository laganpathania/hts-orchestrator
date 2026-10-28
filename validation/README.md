# Validation package (mini)

**What validation means:** documented proof that a system does what it is meant to do, every time, and a
way to show an auditor how you know. In a pharma lab the questions are: *What was it supposed to do?*
*What could go wrong, and how bad is it?* *What did you test, and what happened?* *Can you trace every
requirement to a test and every risk to a control?*

This folder answers those questions for the hts-orchestrator demo.

| Document | Question it answers | File |
|---|---|---|
| URS (User Requirement Specification) | What must the system do? | `urs.yaml` |
| Risk assessment (FMEA) | What can go wrong, how bad, how likely, how would we notice, what is the control? | `risk.yaml` |
| OQ protocol (Operational Qualification) | Which tests prove it, what counts as a pass? | `oq.yaml` |
| Traceability matrix | Is every requirement and risk tested? | generated: `report/traceability.csv` |
| OQ report | What happened when the tests ran, on what, and who signs? | generated: `report/OQ_report.md` |

The trace goes: **requirement (URS) -> risk (R) -> control (a code such as E302 or V040) -> OQ case -> test or scenario -> result.**

## Run it

```
python -m htsorch qualify                 # review the documents against the code, run all OQ cases, write validation/report/
python -m htsorch qualify --review-only   # only the paper check (seconds)
```

Exit code 0 only if the review finds no problem and every case passes.

## What the paper check refuses

- a requirement or risk that no OQ case covers;
- a code (V, E, K, D, C or Q) that the code base can emit but no case provokes (nothing is left untested by omission);
- a control in the risk assessment that is a code but is not provoked by a case covering that risk;
- a test or scenario named in the protocol that does not exist;
- a residual risk that is high, or higher than before the controls.

## How the code-level evidence works

`tests/test_oq_codes.py` triggers each custody refusal (K001-K015) and several pre-flight codes
on purpose; the scenarios provoke the D-codes and E-codes in whole runs. The OQ protocol points at those tests.

## What this is not

- Not a GxP validation: the instruments are simulated, so there is no installation or performance
  qualification on real hardware (IQ here is an environment record, PQ does not apply).
- The risk ratings (1-5) are the author's judgement and are meant to be challenged in a review.
- The sign-off table in the report is blank on purpose: a person signs, not the software.
- Specification limits that are marked VERIFY in the spec still have to be confirmed against vendor documents.
