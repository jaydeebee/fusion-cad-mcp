"""Link-repair, TOC seeding and in-place rebuild tests for the corpus scraper.

Autodesk's pages carry several link shapes that 404 even though the target page
exists under another slug. No network: fetch() is stubbed with a tiny fake site.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

scraper = pytest.importorskip("fusion_cad_mcp.corpus.scraper")

BASE = scraper.BASE


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("adsk.core.Materials_Property_id.htm", "core_Property_id.htm"),
        ("adsk.fusion.SheetMetal_Design_materials.htm", "fusion_Design_materials.htm"),
        ("adsk.cam.CAM_OperationBase_name.htm", "cam_OperationBase_name.htm"),
        ("core_Materials.htm", "core_Materials.htm"),
        ("MaterialSample_Sample.htm", "MaterialSample_Sample.htm"),
    ],
)
def test_canonical_slug(slug, expected):
    assert scraper.canonical_slug(slug) == expected


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("fusion_Base.htm", "core_Base.htm"),
        ("cam_EventArgs.htm", "core_EventArgs.htm"),
        ("MaterialSample.htm", "MaterialSample_Sample.htm"),
        ("SketchArcs_addFillet.htm", "SketchArcs_addFillet_Sample.htm"),
        ("core_FloatSliderListCommandInput.htm", None),
        ("MaterialSample_Sample.htm", None),
    ],
)
def test_fallback_url(slug, expected):
    assert scraper.fallback_url(BASE + slug) == (BASE + expected if expected else None)


def test_fallback_of_a_fallback_is_none():
    """Guessing stops after one hop, so a dead link costs at most two requests."""
    for slug in ("fusion_Nope.htm", "Nope.htm"):
        alt = scraper.fallback_url(BASE + slug)
        assert scraper.fallback_url(alt) is None


def test_extract_links_canonicalizes_header_dir_links():
    html = '<a href="adsk.core.Materials_Property_id.htm#x">Property.id</a>'
    assert scraper.extract_links(html) == [BASE + "core_Property_id.htm"]


def test_fix_links_rewrites_markdown():
    md = "Derived from: [Property.id](adsk.core.Materials_Property_id.htm)"
    assert scraper.fix_links(md) == "Derived from: [Property.id](core_Property_id.htm)"


FAKE_TOC = json.dumps(
    {
        "books": [
            {
                "ln": None,
                "children": [
                    {"ln": "/cloudhelp/ENU/Fusion-GetStarted/files/GS.htm", "children": []},
                    {
                        "ln": "/cloudhelp/ENU/Fusion-360-API/files/Events_UM.htm",
                        "children": [
                            {"ln": "/cloudhelp/ENU/Fusion-360-API/files/Units_UM.htm#a"},
                        ],
                    },
                ],
            }
        ]
    }
)

FAKE_SITE = {
    "toctree.json": FAKE_TOC,
    "Index.htm": (
        "<html><title>Index</title><body>"
        '<a href="adsk.core.Materials_Property_id.htm">derived</a>'
        '<a href="fusion_Base.htm">base</a>'
        '<a href="MaterialSample.htm">sample</a>'
        '<a href="Gone.htm">gone</a>'
        "</body></html>"
    ),
    "core_Property_id.htm": "<html><title>Property.id</title><body>id</body></html>",
    "core_Base.htm": "<html><title>Base</title><body>base</body></html>",
    "MaterialSample_Sample.htm": "<html><title>Sample</title><body>s</body></html>",
    "Events_UM.htm": "<html><title>Events</title><body>e</body></html>",
    "Units_UM.htm": "<html><title>Units</title><body>u</body></html>",
}


@pytest.fixture
def site(tmp_path: Path, monkeypatch):
    scraper.configure(tmp_path)
    monkeypatch.setattr(scraper, "SEEDS", ["Index.htm"])
    requested: list[str] = []

    def fake_fetch(url, **kw):
        slug = url.rsplit("/", 1)[-1]
        requested.append(slug)
        return FAKE_SITE.get(slug)

    monkeypatch.setattr(scraper, "fetch", fake_fetch)
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    return tmp_path, requested


def _slugs(out: Path) -> set[str]:
    return {p.name for p in (out / "pages").glob("*.md")}


def test_toc_seeds_lists_only_api_pages(site):
    assert scraper.toc_seeds() == [BASE + "Events_UM.htm", BASE + "Units_UM.htm"]


def test_crawl_repairs_broken_links_and_seeds_from_toc(site):
    out, requested = site
    m = scraper.run(limit=None, rate=0, resume=False)

    assert _slugs(out) == {
        "Index.md",
        "core_Property_id.md",
        "core_Base.md",
        "MaterialSample_Sample.md",
        "Events_UM.md",
        "Units_UM.md",
    }
    # Header-folder links are rewritten before fetching, never requested.
    assert "adsk.core.Materials_Property_id.htm" not in requested
    # Gone.htm 404s, then its one guess 404s too: one failure, not two.
    assert m["failed"] == 1
    assert requested.count("Gone_Sample.htm") == 1


def test_fresh_run_keeps_earlier_pages_and_corpus(site):
    """A new run, even one stopped early, must not shrink the corpus."""
    out, _ = site
    full = scraper.run(limit=None, rate=0, resume=False)
    assert full["records"] == 6

    partial = scraper.run(limit=1, rate=0, resume=False)
    assert partial["scraped"] == 1
    assert partial["records"] == 6
    lines = (out / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6


def test_corpus_record_round_trips_through_page_file(site):
    out, _ = site
    url = BASE + "core_Thing.htm"
    html = (
        '<html><title>It\'s "quoted"</title><body>'
        '<a href="adsk.core.Materials_Property_id.htm">p</a>'
        "<p>Introduced in version August 2014</p></body></html>"
    )
    record = scraper.page_to_record(url, html)
    path = scraper.write_page(record)
    assert scraper.read_page(path) == record
