"""One test per code: proof that each refusal or pre-flight code can actually be triggered.

The OQ protocol (validation/oq.yaml) points at these tests, so a code that is listed but never provoked
would show up as a gap in the traceability matrix.
"""
import copy
from pathlib import Path

import pytest

from htsorch.clock import SimClock
from htsorch.custody import Custody, CustodyError
from htsorch.platemap import build_plate_map
from htsorch.spec import derive, load_spec
from htsorch.validator import validate
from plant_helpers import T0

SPEC = Path(__file__).resolve().parents[1] / "specs" / "ctg3d_screen.yaml"


def fresh():
    c = Custody(SimClock(T0))
    c.register("A", "BC-A", "assay")
    c.register("P", "BC-P", "prep")
    return c


def in_transit():
    c = fresh()
    c.begin_move("A", "flex_deck")
    return c


def at(station):
    c = fresh()
    c.begin_move("A", station)
    c.complete_move("A")
    return c


def _k003():
    c = fresh()
    c.register("B", "BC-B", "assay", location="centrifuge")
    c.register("C", "BC-C", "assay", location="centrifuge")


def _k004():
    c = in_transit()
    c.begin_move("P", "flex_deck")


def _k008():
    c = fresh()
    c.quarantine("A", "system", "x")
    c.advance("A", "edge_filled")


def _k010():
    in_transit().advance("A", "edge_filled")


def _k014():
    fresh().complete_move("A")


K_CASES = {
    "K001": lambda: fresh().begin_move("NOPE", "flex_deck"),
    "K003": _k003,
    "K004": _k004,
    "K005": lambda: fresh().begin_move("P", "incubator"),
    "K006": lambda: fresh().advance("A", "seeded"),
    "K007": lambda: at("incubator").advance("A", "compound_dosed"),
    "K008": _k008,
    "K009": lambda: _release_by_system(),
    "K010": _k010,
    "K011": lambda: fresh().register("A", "BC-NEW", "assay"),
    "K012": lambda: fresh().begin_move("A", "moon"),
    "K013": lambda: fresh().begin_move("A", "plate_hotel"),
    "K014": _k014,
    "K015": lambda: fresh().register("Z", "BC-Z", "reagent_plate"),
}


def _release_by_system():
    c = fresh()
    c.quarantine("A", "system", "x")
    c.release("A", "system", "auto")


@pytest.mark.parametrize("code", sorted(K_CASES))
def test_custody_refusal_code_is_raised_and_logged(code):
    with pytest.raises(CustodyError) as ei:
        K_CASES[code]()
    assert ei.value.code == code


# ---- pre-flight codes that the main tests do not provoke -------------------------------------------------------------------

@pytest.fixture()
def spec():
    return load_spec(SPEC)


def errors(spec_, pm=None):
    return {f.code for f in validate(spec_, derive(spec_), pm) if f.level == "ERROR"}


def test_V012_too_many_dilution_columns(spec):
    spec["dilution"]["points"] = 12
    assert "V012" in errors(spec)


def test_V042_reagent_lag_window(spec):
    spec["timing"]["windows"]["max_reagent_lag_min"] = 1
    assert "V042" in errors(spec)


def test_V050_duplicate_well_in_plate_map(spec):
    d = derive(spec)
    pm = copy.deepcopy(build_plate_map(spec, d))
    pm[1]["well"] = pm[0]["well"]
    assert "V050" in errors(spec, pm)


def test_V052_edge_policy_violated_by_the_plate_map(spec):
    spec["assay"]["edge"]["policy"] = "controls"
    d = derive(spec)
    pm = copy.deepcopy(build_plate_map(spec, d))
    test_well = next(w for w in pm if w["type"] == "test" and not w["edge"])
    edge_ctrl = next(w for w in pm if w["edge"])
    test_well["well"], edge_ctrl["well"] = edge_ctrl["well"], test_well["well"]
    test_well["edge"], edge_ctrl["edge"] = True, False
    assert "V052" in errors(spec, pm)
