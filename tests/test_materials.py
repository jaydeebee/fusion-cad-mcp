"""Tier 1a/1c tests for the material tools. No Fusion required.

The lookup and targeting logic lives inside the generated script, so the script
is exec'd against a stub adsk, as in test_doc_state.py.
"""

from __future__ import annotations

import ast
import contextlib
import io as _io
import json
import sys
import types

import pytest

from fusion_cad_mcp.envelope import Envelope
from fusion_cad_mcp.tools import materials as m

# ---------- stub adsk ----------


class _Coll:
    def __init__(self, items):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]


class _Mat:
    def __init__(self, name, id_):
        self.name = name
        self.id = id_


class _Lib:
    def __init__(self, name, mats):
        self.name = name
        self.materials = _Coll(mats)


class _Body:
    def __init__(self, name, material):
        self.name = name
        self.material = material


class _Comp:
    def __init__(self, name, bodies, material=None):
        self.name = name
        self.bRepBodies = _Coll(bodies)
        self.material = material


STEEL = _Mat("Steel", "PM-018")
ALU = _Mat("Aluminum", "PM-002")
ALU_6061 = _Mat("Aluminum 6061", "PM-231")
ALU_ADDITIVE_DUP = _Mat("Aluminum", "PM-999")


def _world(*, components=None, design=True):
    libs = _Coll(
        [
            _Lib("Fusion Material Library", [ALU, ALU_6061, _Mat("Brass", "PM-005")]),
            _Lib("Other Library", [ALU_ADDITIVE_DUP]),
        ]
    )
    comps = components or [
        _Comp("Root", [_Body("Body1", STEEL)]),
        _Comp("Bracket", [_Body("Plate", STEEL), _Body("Pin", STEEL)]),
    ]
    d = None
    if design:
        d = types.SimpleNamespace(materials=_Coll([STEEL]), allComponents=_Coll(comps))
    app = types.SimpleNamespace(materialLibraries=libs, activeProduct=d)
    return app, comps


def _exec(src, app):
    adsk = types.ModuleType("adsk")
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    adsk.core, adsk.fusion = core, fusion
    core.Application = type("Application", (), {"get": staticmethod(lambda: app)})
    fusion.Design = type("Design", (), {"cast": staticmethod(lambda p: p)})

    ns: dict = {}
    buf = _io.StringIO()
    saved = {k: sys.modules.get(k) for k in ("adsk", "adsk.core", "adsk.fusion")}
    sys.modules.update({"adsk": adsk, "adsk.core": core, "adsk.fusion": fusion})
    try:
        exec(compile(src, "<materials>", "exec"), ns)
        with contextlib.redirect_stdout(buf):
            ns["run"](None)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return json.loads(buf.getvalue())


# ---------- generators ----------


@pytest.mark.parametrize(
    "src",
    [
        m.build_list_materials(),
        m.build_list_materials("alu", "Fusion Material Library", 5),
        m.build_set_material("Aluminum", body_name="Body1"),
        m.build_set_material('It\'s "odd"', component_name="Bracket", library="L"),
    ],
)
def test_generated_scripts_parse_and_define_run(src):
    tree = ast.parse(src)
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run"]
    assert len(runs) == 1 and len(runs[0].args.args) == 1


# ---------- list_materials ----------


def test_list_filters_case_insensitively():
    app, _ = _world()
    out = _exec(m.build_list_materials("ALUM"), app)
    assert [(e["library"], e["name"]) for e in out["materials"]] == [
        ("Fusion Material Library", "Aluminum"),
        ("Fusion Material Library", "Aluminum 6061"),
        ("Other Library", "Aluminum"),
    ]
    assert out["libraries"] == ["Fusion Material Library", "Other Library"]


def test_list_includes_design_materials():
    app, _ = _world()
    out = _exec(m.build_list_materials("steel"), app)
    assert out["materials"] == [{"library": "<design>", "name": "Steel", "id": "PM-018"}]


def test_list_limit_truncates():
    app, _ = _world()
    out = _exec(m.build_list_materials(None, None, 2), app)
    assert out["count"] == 2
    assert out["total_matched"] == 5
    assert out["truncated"] is True


def test_list_unknown_library():
    app, _ = _world()
    out = _exec(m.build_list_materials(None, "Nope"), app)
    assert out["error"] == "library_not_found"


def test_list_works_without_a_design():
    app, _ = _world(design=False)
    out = _exec(m.build_list_materials("brass"), app)
    assert out["materials"][0]["name"] == "Brass"


# ---------- set_material ----------


def test_set_body_material_from_library():
    app, comps = _world()
    out = _exec(m.build_set_material("Aluminum 6061", body_name="Pin"), app)
    assert out["ok"] is True
    assert out["target"] == "Bracket / Pin"
    assert (out["before"], out["after"]) == ("Steel", "Aluminum 6061")
    assert out["library"] == "Fusion Material Library"
    assert comps[1].bRepBodies.item(1).material is ALU_6061


def test_set_reports_same_name_in_other_libraries():
    app, _ = _world()
    out = _exec(m.build_set_material("Aluminum", body_name="Body1"), app)
    assert out["material_id"] == "PM-002"
    assert out["other_matches"] == [
        {"library": "Other Library", "name": "Aluminum", "id": "PM-999"}
    ]


def test_set_library_restricts_search():
    app, _ = _world()
    out = _exec(m.build_set_material("Aluminum", body_name="Body1", library="Other Library"), app)
    assert out["material_id"] == "PM-999"


def test_set_by_id():
    app, _ = _world()
    out = _exec(m.build_set_material("PM-231", body_name="Body1"), app)
    assert out["after"] == "Aluminum 6061"


def test_set_component_material():
    app, comps = _world()
    out = _exec(m.build_set_material("Brass", component_name="Bracket"), app)
    assert out["kind"] == "component"
    assert comps[1].material.name == "Brass"


def test_set_miss_suggests_near_names_shortest_first():
    app, _ = _world()
    out = _exec(m.build_set_material("Aluminium", body_name="Body1"), app)
    assert out["error"] == "material_not_found"
    names = [s["name"] for s in out["suggestions"]]
    assert names[0] == "Aluminum"
    assert "Aluminum 6061" in names and "Brass" not in names


def test_set_body_not_found():
    app, _ = _world()
    out = _exec(m.build_set_material("Brass", body_name="Nope"), app)
    assert out == {"ok": False, "error": "body_not_found", "name": "Nope"}


def test_set_ambiguous_body():
    comps = [_Comp("A", [_Body("Body1", STEEL)]), _Comp("B", [_Body("Body1", STEEL)])]
    app, _ = _world(components=comps)
    out = _exec(m.build_set_material("Brass", body_name="Body1"), app)
    assert out["error"] == "ambiguous_body"
    assert out["paths"] == ["A / Body1", "B / Body1"]
    assert comps[0].bRepBodies.item(0).material is STEEL


def test_set_no_design():
    app, _ = _world(design=False)
    out = _exec(m.build_set_material("Brass", body_name="Body1"), app)
    assert out["error"] == "no_active_design"


# ---------- wrapper validation (no adapter call) ----------


class _NoCallAdapter:
    def execute_script(self, _src):
        raise AssertionError("adapter must not be called")


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"material": "", "body_name": "B"}, "invalid_material"),
        ({"material": "Steel"}, "invalid_target"),
        ({"material": "Steel", "body_name": "B", "component_name": "C"}, "invalid_target"),
    ],
)
def test_set_material_rejects_bad_args(kwargs, error):
    env = m.set_material(_NoCallAdapter(), **kwargs)
    assert isinstance(env, Envelope) and env.error == error


def test_list_materials_rejects_bad_limit():
    assert m.list_materials(_NoCallAdapter(), limit=0).error == "invalid_limit"
