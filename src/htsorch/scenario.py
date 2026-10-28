"""Scenario files: a repeatable set of things that go wrong, how the person at the bench behaves,
what the person does next, and what must come out. `python -m htsorch scenarios` checks them all.

    description: the plate is lost and nobody is there; a person comes an hour later
    inventory:                                 # starting stock (devices.apply_inventory)
      tips: {dilution: 10}                     #   tips left in a rack
      reservoirs: {cells: {fraction: 0.5}}     #   load half of what the run needs ({volume_ul: N} also works)
      idot_sources: {A1: {usable_ul: 0.3}}     #   uL above the dead volume in an iDOT source well
    faults:                                    # fires on the n-th call of device.command (faults.py)
      - {device: arm, command: move, code: E302, at: 5}
    operator: {available: false}               # policy.SimOperator
    policy: {E301: {max_retries: 1}}           # per-code changes to the recovery table
    delays: {compound_wait_min: 0}             # injected handoff delay before seeding
    wake_delay_h: 0                            # how late the orchestrator wakes after the incubation
    then:                                      # what happens after the run first stops (in order)
      - {advance_min: 60}
      - respond: {actor: A.Operator, decision: retry}
    expect:                                    # every key optional except status
      status: complete                         # complete | paused | waiting | aborted
      faults: [E302]                           # fault codes in order of occurrence (exact)
      deviations: [D050, D051]                 # deviation codes in the run record (exact set; [] = none)
      interventions: 1                         # how many times a person had to act
      plate_flags: {ASSAY-0001: [C001, C002]}  # custody flags per plate (exact, in order)
      alerts: {D040: abort}                    # window alerts and the decision taken
      paused_reason_contains: nobody           # for status paused
      resumed_steps: [flex_dilution_series]    # steps that finished after a stop
      events_include: [operator_action]        # step names that must appear in the log
      events_absent: [flex_media]              # step names that must not appear
      package: true                            # build the data package and verify it (default for complete runs)
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .devices import check_inventory
from .faults import FaultInjector
from .policy import SimOperator, resolve_policy

KEYS = {"description", "faults", "operator", "policy", "delays", "wake_delay_h", "inventory", "then", "expect"}
DELAY_KEYS = {"compound_wait_min"}
THEN_KEYS = {"advance_min", "until_wake", "wake_delay_h", "respond"}
STATUSES = ("complete", "paused", "waiting", "aborted")
EXPECT_KEYS = {"status", "faults", "deviations", "interventions", "plate_flags", "alerts", "paused_reason_contains",
               "resumed_steps", "events_include", "events_absent", "package"}


def _list_of_str(name, v):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"expect.{name} must be a list of names")


def parse_expect(exp: dict | None) -> dict | None:
    if exp is None:
        return None
    if not isinstance(exp, dict):
        raise ValueError("expect must be a mapping")
    unknown = set(exp) - EXPECT_KEYS
    if unknown:
        raise ValueError(f"unknown expect key(s): {', '.join(sorted(unknown))} (known: {', '.join(sorted(EXPECT_KEYS))})")
    if exp.get("status") not in STATUSES:
        raise ValueError(f"expect.status is required and must be one of {', '.join(STATUSES)}")
    for k in ("faults", "deviations", "resumed_steps", "events_include", "events_absent"):
        if k in exp:
            _list_of_str(k, exp[k])
    if "interventions" in exp and (not isinstance(exp["interventions"], int) or exp["interventions"] < 0):
        raise ValueError("expect.interventions must be a whole number")
    if "plate_flags" in exp:
        pf = exp["plate_flags"]
        if not isinstance(pf, dict) or not all(isinstance(v, list) for v in pf.values()):
            raise ValueError("expect.plate_flags must map a plate id to a list of flag codes")
    if "alerts" in exp and not isinstance(exp["alerts"], dict):
        raise ValueError("expect.alerts must map a window code to a decision")
    if "package" in exp and not isinstance(exp["package"], bool):
        raise ValueError("expect.package must be true or false")
    return dict(exp)


def parse_then(then: list | None) -> list:
    out = []
    for i, act in enumerate(then or [], 1):
        if not isinstance(act, dict) or not act:
            raise ValueError(f"then[{i}] must be a mapping")
        unknown = set(act) - THEN_KEYS
        if unknown:
            raise ValueError(f"then[{i}]: unknown key(s) {', '.join(sorted(unknown))} (known: {', '.join(sorted(THEN_KEYS))})")
        if "respond" in act:
            r = act["respond"]
            if not isinstance(r, dict) or not r.get("actor") or r["actor"] == "system":
                raise ValueError(f"then[{i}].respond needs the name of a person as 'actor'")
            if r.get("decision") not in (None, "retry", "abort", "continue"):
                raise ValueError(f"then[{i}].respond.decision must be retry, abort or continue")
        out.append(dict(act))
    return out


def parse_scenario(raw: dict | None) -> dict:
    raw = raw or {}
    unknown = set(raw) - KEYS
    if unknown:
        raise ValueError(f"unknown scenario key(s): {', '.join(sorted(unknown))}")
    FaultInjector(raw.get("faults") or [])                 # validates codes, devices, at/times
    resolve_policy(raw.get("policy"))
    check_inventory(raw.get("inventory"))
    op = SimOperator(**(raw.get("operator") or {}))
    delays = raw.get("delays") or {}
    if set(delays) - DELAY_KEYS:
        raise ValueError(f"unknown delay(s): {', '.join(sorted(set(delays) - DELAY_KEYS))}")
    wake = float(raw.get("wake_delay_h", 0.0))
    if wake < 0:
        raise ValueError("wake_delay_h cannot be negative")
    return {"description": raw.get("description", ""), "faults": raw.get("faults") or [], "operator": op,
            "policy": raw.get("policy") or {}, "delays": delays, "wake_delay_h": wake,
            "inventory": raw.get("inventory") or {}, "then": parse_then(raw.get("then")),
            "expect": parse_expect(raw.get("expect"))}


def load_scenario(path) -> dict:
    return parse_scenario(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
