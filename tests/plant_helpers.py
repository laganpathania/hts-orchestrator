"""Shared set-up for device, custody and lifecycle tests."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from htsorch.clock import SimClock
from htsorch.custody import Custody, transfer_plate
from htsorch.devices import build_devices, idot_needs
from htsorch.faults import DeviceFault, FaultInjector
from htsorch.platemap import build_plate_map
from htsorch.spec import derive, load_spec

MAIN = Path(__file__).resolve().parents[1] / "specs" / "ctg3d_screen.yaml"
T0 = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
ASSAY, PREP = "ASSAY-0001", "PREP-0001"


def make_plant(faults=None, spec_path=MAIN, reservoir_load_ul=None):
    spec = load_spec(spec_path)
    d = derive(spec)
    pm = build_plate_map(spec, d)
    clock = SimClock(T0)
    inj = FaultInjector(faults or [])
    devs = build_devices(spec, d, clock, inj, reservoir_load_ul=reservoir_load_ul)
    cust = Custody(clock)
    cust.register(PREP, spec["prep_plate"]["barcode"], "prep")
    cust.register(ASSAY, spec["assay"]["plate"]["barcode"], "assay")
    return SimpleNamespace(spec=spec, d=d, pm=pm, clock=clock, inj=inj, devs=devs, cust=cust, log=[])


def call(plant, dev, command, retries=3, **params):
    """Run a device command; clear transient faults and retry. Operator faults propagate."""
    for attempt in range(retries + 1):
        try:
            res = plant.devs[dev].execute(command, **params)
            plant.log.append(res.as_dict())
            return res
        except DeviceFault as f:
            plant.log.append({"fault": f.as_dict()})
            if f.kind != "transient" or attempt == retries:
                raise
            plant.devs[dev].recover("system")


def move(plant, plate, dst, retries=3):
    minutes = plant.spec["timing"]["arm_transfer_min"]
    for attempt in range(retries + 1):
        try:
            return transfer_plate(plant.cust, plant.devs["arm"], plate, dst, minutes)
        except DeviceFault as f:
            plant.log.append({"fault": f.as_dict()})
            if f.kind != "transient" or attempt == retries:
                raise
            plant.devs["arm"].recover("system")


def run_nominal(plant):
    """Both plates through the whole workflow, one step after another."""
    s, d, c = plant.spec, plant.d, plant.cust
    tm, rg, est = s["timing"], s["assay"]["reagent"], d["est"]
    res = d["reservoir_need_ul"]
    tips = {k: v["needed"] for k, v in d["tips"].items()}

    move(plant, PREP, "flex_deck")
    call(plant, "flex", "run_protocol", protocol="flex_1_dilution.py", minutes=15.0,
         tips={"dilution": tips["dilution"]}, draws={"dmso": res["dmso"]}, plate=PREP)
    c.advance(PREP, "filled")

    if d["edge_policy"] == "pbs_fill":
        move(plant, ASSAY, "flex_deck")
        call(plant, "flex", "run_protocol", protocol="flex_0_edge_fill.py", minutes=est["edge_fill_min"],
             tips={"edge_fill": tips["edge_fill"]}, draws={"pbs": res["pbs"]}, plate=ASSAY)
        c.advance(ASSAY, "edge_filled")
        move(plant, ASSAY, "idot_deck")
    else:
        move(plant, ASSAY, "idot_deck")
    move(plant, PREP, "idot_deck")
    call(plant, "idot", "dispense", needs_nl=idot_needs(plant.pm),
         minutes=tm["idot_dispense_min"], plate=ASSAY)
    c.advance(ASSAY, "compound_dosed")
    c.advance(PREP, "consumed")

    move(plant, ASSAY, "flex_deck")
    call(plant, "flex", "run_protocol", protocol="flex_2_seeding.py (media)", minutes=est["media_min"],
         tips={"seeding": 1}, draws={"media": res["media"]}, plate=ASSAY)
    c.advance(ASSAY, "media_added")
    call(plant, "flex", "run_protocol", protocol="flex_2_seeding.py (cells)", minutes=est["cells_min"],
         tips={"seeding": 1}, draws={"cells": res["cells"]}, plate=ASSAY)
    c.advance(ASSAY, "seeded")

    move(plant, ASSAY, "centrifuge")
    call(plant, "centrifuge", "spin", plate=ASSAY, minutes=3.0)
    c.advance(ASSAY, "centrifuged")
    move(plant, ASSAY, "incubator")
    call(plant, "incubator", "store", plate=ASSAY)
    c.advance(ASSAY, "incubating")

    plant.clock.advance(tm["windows"]["incubation_h"] * 60.0)          # the 72 h wait
    call(plant, "incubator", "check", plate=ASSAY)
    c.advance(ASSAY, "incubated")
    call(plant, "incubator", "retrieve", plate=ASSAY)
    move(plant, ASSAY, "flex_deck")
    call(plant, "flex", "run_protocol", protocol="flex_3_readout.py", minutes=est["reagent_min"],
         tips={"readout": 1}, draws={"reagent": res["reagent"]}, plate=ASSAY)
    c.advance(ASSAY, "reagent_added")
    call(plant, "shaker", "shake", plate=ASSAY, minutes=rg["shake_min"] + rg["incubate_min"])
    c.advance(ASSAY, "lysed")
    move(plant, ASSAY, "reader")
    call(plant, "reader", "read", plate=ASSAY, barcode=s["assay"]["plate"]["barcode"],
         expected_barcode=s["assay"]["plate"]["barcode"], mode="luminescence", minutes=10.0)
    c.advance(ASSAY, "read")
    c.advance(ASSAY, "complete")
    c.advance(PREP, "complete")
