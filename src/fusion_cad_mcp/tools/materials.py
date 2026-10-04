"""Group 6 / material tools.

- list_materials: find material names in the loaded libraries and the design
- set_material: assign a physical material to a body or a component

Material names are not in the API docs; they come from whichever libraries the
user's Fusion has loaded ("Aluminum 6061", "Steel", ...). Names are also not
unique across libraries. So set_material matches a name exactly, and on a miss
returns near-name suggestions rather than guessing.

Lookup order for set_material: materials already in the design, then each
loaded library in order. Pass `library` to search only that library.
"""

from __future__ import annotations

from ..adapter import FusionAdapter
from ..envelope import Envelope, parse_stdout_json

# Shared by both scripts: enumerate (source, material) pairs, design first.
# A library whose materials can't be read is reported, not fatal.
_SOURCES_BLOCK = """
DESIGN_SOURCE = "<design>"


def material_sources(app, design, library):
    out = []
    skipped = []
    if design is not None and library in (None, DESIGN_SOURCE):
        out.append((DESIGN_SOURCE, design.materials))
    libs = app.materialLibraries
    for i in range(libs.count):
        lib = libs.item(i)
        if library is not None and lib.name != library:
            continue
        try:
            out.append((lib.name, lib.materials))
        except Exception as e:
            skipped.append({"library": lib.name, "error": str(e)})
    return out, skipped


def iter_materials(sources):
    for source, mats in sources:
        for i in range(mats.count):
            yield source, mats.item(i)


def stems(text):
    # First 6 chars of each word, so "aluminium" still finds "Aluminum 6061".
    return [w[:6] for w in text.lower().replace("-", " ").split() if len(w) >= 3]


def entry(source, m):
    return {"library": source, "name": m.name, "id": m.id}
"""


def build_list_materials(
    name_filter: str | None = None, library: str | None = None, limit: int = 50
) -> str:
    """Script listing materials whose name contains name_filter (case-insensitive)."""
    return f"""
import adsk.core
import adsk.fusion
import json

NAME_FILTER = {name_filter!r}
LIBRARY = {library!r}
LIMIT = {int(limit)!r}
{_SOURCES_BLOCK}

def run(_ctx):
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    libs = app.materialLibraries
    library_names = [libs.item(i).name for i in range(libs.count)]
    if LIBRARY is not None and LIBRARY not in library_names and LIBRARY != DESIGN_SOURCE:
        print(json.dumps({{"ok": False, "error": "library_not_found", "library": LIBRARY,
                           "libraries": library_names}}))
        return
    sources, skipped = material_sources(app, design, LIBRARY)
    needle = (NAME_FILTER or "").lower()
    matches = []
    total = 0
    for source, m in iter_materials(sources):
        if needle and needle not in m.name.lower():
            continue
        total += 1
        if len(matches) < LIMIT:
            matches.append(entry(source, m))
    print(json.dumps({{
        "ok": True,
        "materials": matches,
        "count": len(matches),
        "total_matched": total,
        "truncated": total > len(matches),
        "libraries": library_names,
        "skipped_libraries": skipped,
    }}))
""".strip()


def build_set_material(
    material: str,
    body_name: str | None = None,
    component_name: str | None = None,
    library: str | None = None,
) -> str:
    """Script assigning `material` (exact name or id) to one body or component."""
    return f"""
import adsk.core
import adsk.fusion
import json

MATERIAL = {material!r}
BODY_NAME = {body_name!r}
COMPONENT_NAME = {component_name!r}
LIBRARY = {library!r}
{_SOURCES_BLOCK}

def find_material(sources):
    found = None
    others = []
    for source, m in iter_materials(sources):
        if m.name == MATERIAL or m.id == MATERIAL:
            if found is None:
                found = (source, m)
            elif m.id != found[1].id:
                others.append(entry(source, m))
    return found, others


def suggestions(sources, limit=10):
    # Shortest names first: the plain grade ("Aluminum") before its alloys.
    keys = stems(MATERIAL)
    out = [entry(s, m) for s, m in iter_materials(sources) if any(k in m.name.lower() for k in keys)]
    out.sort(key=lambda e: len(e["name"]))
    return out[:limit]


def find_targets(design):
    comps = design.allComponents
    hits = []
    for i in range(comps.count):
        comp = comps.item(i)
        if COMPONENT_NAME is not None:
            if comp.name == COMPONENT_NAME:
                hits.append((comp.name, comp))
            continue
        for j in range(comp.bRepBodies.count):
            b = comp.bRepBodies.item(j)
            if b.name == BODY_NAME:
                hits.append((comp.name + " / " + b.name, b))
    return hits


def run(_ctx):
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        print(json.dumps({{"ok": False, "error": "no_active_design"}}))
        return

    kind = "component" if COMPONENT_NAME is not None else "body"
    target_name = COMPONENT_NAME if COMPONENT_NAME is not None else BODY_NAME
    targets = find_targets(design)
    if not targets:
        print(json.dumps({{"ok": False, "error": kind + "_not_found", "name": target_name}}))
        return
    if len(targets) > 1:
        print(json.dumps({{"ok": False, "error": "ambiguous_" + kind, "name": target_name,
                           "paths": [p for p, _ in targets]}}))
        return
    path, target = targets[0]

    sources, skipped = material_sources(app, design, LIBRARY)
    found, others = find_material(sources)
    if found is None:
        print(json.dumps({{"ok": False, "error": "material_not_found", "material": MATERIAL,
                           "library": LIBRARY, "suggestions": suggestions(sources),
                           "skipped_libraries": skipped}}))
        return
    source, mat = found

    before = target.material.name if target.material else None
    target.material = mat
    after = target.material.name if target.material else None
    print(json.dumps({{
        "ok": after == mat.name,
        "error": None if after == mat.name else "material_not_applied",
        "target": path,
        "kind": kind,
        "before": before,
        "after": after,
        "material_id": mat.id,
        "library": source,
        "other_matches": others,
    }}))
""".strip()


# ---------- run wrappers ----------


def _run(adapter: FusionAdapter, script: str, label: str) -> Envelope:
    env = adapter.execute_script(script)
    if not env.ok:
        return env
    parsed = parse_stdout_json(env)
    if parsed is None:
        return Envelope(ok=False, error=f"{label}_parse_failed", message=env.message)
    if parsed.get("ok") is False:
        return Envelope(
            ok=False,
            error=parsed.get("error") or f"{label}_failed",
            message=env.message,
            result=parsed,
        )
    return Envelope(ok=True, message=env.message, result=parsed)


def list_materials(
    adapter: FusionAdapter,
    name_filter: str | None = None,
    library: str | None = None,
    limit: int = 50,
) -> Envelope:
    if not isinstance(limit, int) or limit < 1:
        return Envelope(ok=False, error="invalid_limit", message="limit must be a positive int")
    return _run(adapter, build_list_materials(name_filter, library, limit), "list_materials")


def set_material(
    adapter: FusionAdapter,
    material: str,
    body_name: str | None = None,
    component_name: str | None = None,
    library: str | None = None,
) -> Envelope:
    if not material or not isinstance(material, str):
        return Envelope(
            ok=False, error="invalid_material", message="material must be a non-empty string"
        )
    if (body_name is None) == (component_name is None):
        return Envelope(
            ok=False,
            error="invalid_target",
            message="pass exactly one of body_name or component_name",
        )
    return _run(
        adapter,
        build_set_material(material, body_name, component_name, library),
        "set_material",
    )


__all__ = [
    "build_list_materials",
    "build_set_material",
    "list_materials",
    "set_material",
]
