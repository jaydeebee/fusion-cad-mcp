"""Fusion 360 API docs scraper.

Crawls help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/*.htm starting from
a seed list (Index.htm + What's New + known UM pages + every API page in the
help site's table of contents), follows every internal .htm link, converts each
page to markdown, and writes one .md per page. corpus.jsonl is then rebuilt from
pages/, so a run only ever adds or refreshes pages and never discards earlier
ones. Also writes a manifest and the crawl frontier that --resume picks back up.

Usage:
    py -3 scraper.py --i-accept-autodesk-terms
    py -3 scraper.py --limit 20 --i-accept-autodesk-terms
    py -3 scraper.py --resume --i-accept-autodesk-terms

Politeness:
    1 req/sec by default; --rate to override (seconds between requests).
    Retries 3x with backoff on 5xx; on 404 tries a known alternate slug, else
    logs and skips.
    Requires explicit local-cache consent before fetching Autodesk Help pages.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md_convert

BASE = "https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/"

# lxml is faster and more forgiving on Autodesk's markup, but bs4 only fails on
# it at first parse, long after the crawl has started. Resolve it up front and
# fall back to the stdlib parser.
try:
    import lxml  # noqa: F401

    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

# Defaults to the directory the resolver checks first, so a plain
# `corpus build` lands where the server looks. Overridable for the dev corpus.
DEFAULT_OUT_DIR = Path.home() / ".fusion-cad" / "corpus"

OUT_DIR = DEFAULT_OUT_DIR
PAGES_DIR = OUT_DIR / "pages"
CORPUS_PATH = OUT_DIR / "corpus.jsonl"
MANIFEST_PATH = OUT_DIR / "manifest.json"
FRONTIER_PATH = OUT_DIR / "frontier.json"
LOG_PATH = OUT_DIR / "scraper.log"

# Save the frontier every N pages so a crash or kill loses at most this much
# crawl progress.
FRONTIER_SAVE_EVERY = 250


def configure(out_dir: Path) -> None:
    """Point the scraper at a different output directory.

    The paths are module-level constants used throughout, so rebind them all
    together rather than threading a directory through every function.
    """
    global OUT_DIR, PAGES_DIR, CORPUS_PATH, MANIFEST_PATH, FRONTIER_PATH, LOG_PATH
    OUT_DIR = Path(out_dir).expanduser().resolve()
    PAGES_DIR = OUT_DIR / "pages"
    CORPUS_PATH = OUT_DIR / "corpus.jsonl"
    MANIFEST_PATH = OUT_DIR / "manifest.json"
    FRONTIER_PATH = OUT_DIR / "frontier.json"
    LOG_PATH = OUT_DIR / "scraper.log"
    OUT_DIR.mkdir(parents=True, exist_ok=True)


DEFAULT_USER_AGENT = "fusion-cad-mcp-corpus-builder/0.1 (local cache; contact configurable)"
PREVIEW_MARKER = "This functionality is provided as a preview"
INTRODUCED_RE = re.compile(r"Introduced in version\s+([^\n\r]+)", re.IGNORECASE)

TERMS_NOTICE = """\
This command fetches Autodesk Help pages and builds a local corpus cache on this machine.
It does not grant redistribution rights and does not publish Autodesk documentation.
Continue only if you have reviewed Autodesk Terms of Use / Acceptable Use and accept
responsibility for this user-initiated local cache build.
"""

# Seed pages: starting points for the crawl.
SEEDS = [
    "Index.htm",
    "WhatsNew.htm",
    # User Manual entry points (probed and confirmed):
    "BasicConcepts_UM.htm",
    "CustomFeatures_UM.htm",
]

# The help site's table of contents. Most User Manual pages and the sample list
# are reachable only from here, not by following links from Index.htm.
TOC_URL = "https://help.autodesk.com/view/fusion360/ENU/data/toctree.json"
TOC_API_PREFIX = "/cloudhelp/ENU/Fusion-360-API/files/"

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }
)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url: str, *, retries: int = 3, backoff: float = 2.0) -> str | None:
    """GET with retries. Returns body text or None on permanent failure."""
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code >= 500:
                time.sleep(backoff * (attempt + 1))
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            log(f"  WARN {url}: {e} (attempt {attempt + 1})")
            time.sleep(backoff * (attempt + 1))
    return None


HTM_LINK_RE = re.compile(r"""href\s*=\s*['\"]([^'\"]+\.htm)(?:#[^'\"]*)?['\"]""", re.IGNORECASE)

# Autodesk's "Derived from:" links on inherited members embed the C++ header
# folder, e.g. adsk.core.Materials_Property_id.htm for the page that actually
# lives at core_Property_id.htm. Every such link 404s.
HEADER_DIR_SLUG_RE = re.compile(r"^adsk\.([a-z]+)\.[A-Za-z0-9]+_(.+\.htm)$")

API_NAMESPACES = ("core", "fusion", "cam", "drawing", "electron", "volume")
NS_SLUG_RE = re.compile(r"^(" + "|".join(API_NAMESPACES) + r")_(.+\.htm)$")


def canonical_slug(slug: str) -> str:
    """Map a known-broken link slug to the slug Autodesk actually serves."""
    m = HEADER_DIR_SLUG_RE.match(slug)
    return f"{m.group(1)}_{m.group(2)}" if m else slug


def canonical_url(url: str) -> str:
    head, _, slug = url.rpartition("/")
    return f"{head}/{canonical_slug(slug)}"


def fallback_url(url: str) -> str | None:
    """Where a 404'd page most likely lives, or None if there's no guess.

    Two other link shapes on Autodesk's pages 404:
      - classes inherited from core, linked under the subclass's namespace
        (fusion_Base.htm, cam_EventArgs.htm -> core_Base.htm, core_EventArgs.htm)
      - sample links missing the _Sample suffix (MaterialSample.htm ->
        MaterialSample_Sample.htm)
    A fallback never yields another fallback, so a miss costs one extra request.
    """
    head, _, slug = url.rpartition("/")
    m = NS_SLUG_RE.match(slug)
    if m:
        return f"{head}/core_{m.group(2)}" if m.group(1) != "core" else None
    if not slug.endswith("_Sample.htm"):
        return f"{head}/{slug[:-4]}_Sample.htm"
    return None


def extract_links(html: str) -> list[str]:
    """Pull all internal .htm links from a page (relative or absolute under our base)."""
    out = []
    for m in HTM_LINK_RE.finditer(html):
        href = m.group(1)
        # Absolutize
        absurl = urljoin(BASE, href)
        # Same-folder scope only
        if absurl.startswith(BASE):
            # Drop fragment, normalize
            absurl = canonical_url(absurl.split("#", 1)[0])
            out.append(absurl)
    return out


def toc_seeds() -> list[str]:
    """API page URLs listed in the help site's table of contents.

    Empty when the TOC can't be fetched or parsed; the static SEEDS still run.
    """
    text = fetch(TOC_URL)
    if text is None:
        log(f"WARN could not fetch {TOC_URL}; crawling from static seeds only")
        return []
    try:
        books = json.loads(text)["books"]
    except (ValueError, KeyError, TypeError) as e:
        log(f"WARN unreadable TOC {TOC_URL}: {e}")
        return []

    out: dict[str, None] = {}
    stack = list(books)
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        ln = node.get("ln") or ""
        if TOC_API_PREFIX in ln:
            slug = ln.rsplit("/", 1)[-1].split("#", 1)[0]
            out[BASE + canonical_slug(slug)] = None
        stack.extend(node.get("children") or [])
    log(f"TOC: {len(out)} API pages listed")
    return list(out)


def title_from_html(soup: BeautifulSoup) -> str:
    t = soup.find("title")
    return t.get_text(strip=True) if t else ""


def main_content(soup: BeautifulSoup) -> BeautifulSoup:
    """Return the meaningful content tag. Autodesk help pages put content in <body>."""
    body = soup.find("body") or soup
    # Strip scripts, styles, navigation cruft
    for tag in body.find_all(["script", "style"]):
        tag.decompose()
    return body


def classify(slug: str) -> tuple[str, str]:
    """Return (namespace, kind) for a page slug like 'MeshBody_calculateCollisionsWithRay.htm'."""
    name = slug[:-4] if slug.endswith(".htm") else slug
    if name.endswith("_UM"):
        return ("user-manual", "manual")
    if "_" in name:
        parent, member = name.split("_", 1)
        # Heuristic: if member looks like a method/property (camelCase), it's a member page
        kind = (
            "member"
            if member and (member[0].islower() or member in {"classType", "objectType"})
            else "object"
        )
        return (parent, kind)
    return (name, "object")


HEADER_DIR_LINK_RE = re.compile(r"\badsk\.([a-z]+)\.[A-Za-z0-9]+_([^\s()<>\"'#]+\.htm)")


def fix_links(body_md: str) -> str:
    """Point header-folder "Derived from:" links at the page that exists."""
    return HEADER_DIR_LINK_RE.sub(r"\1_\2", body_md)


def make_record(url: str, slug: str, title: str, namespace: str, kind: str, body_md: str) -> dict:
    introduced_match = INTRODUCED_RE.search(body_md)
    return {
        "url": url,
        "slug": slug,
        "title": title,
        "namespace": namespace,
        "kind": kind,
        "is_preview": PREVIEW_MARKER in body_md or " Object Preview" in title,
        "introduced": introduced_match.group(1).strip() if introduced_match else None,
        "body_md": body_md,
    }


def page_to_record(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, HTML_PARSER)
    title = title_from_html(soup)
    body = main_content(soup)
    body_html = str(body)
    body_md = fix_links(md_convert(body_html, heading_style="ATX", bullets="-").strip())
    slug = url.rsplit("/", 1)[-1]
    namespace, kind = classify(slug)
    return make_record(url, slug, title, namespace, kind, body_md)


def write_page(record: dict) -> Path:
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    fname = record["slug"].replace(".htm", ".md")
    path = PAGES_DIR / fname
    front = (
        "---\n"
        f"url: {record['url']}\n"
        f"slug: {record['slug']}\n"
        f"title: {record['title']!r}\n"
        f"namespace: {record['namespace']}\n"
        f"kind: {record['kind']}\n"
        "---\n\n"
    )
    path.write_text(front + record["body_md"], encoding="utf-8")
    return path


def read_page(path: Path) -> dict | None:
    """Turn a page written by write_page back into its corpus record."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    front, sep, body_md = text[4:].partition("\n---\n\n")
    if not sep:
        return None
    fields = dict(line.split(": ", 1) for line in front.splitlines() if ": " in line)
    try:
        title = ast.literal_eval(fields.get("title", "''"))
        return make_record(
            fields["url"],
            fields["slug"],
            title,
            fields["namespace"],
            fields["kind"],
            fix_links(body_md),
        )
    except (KeyError, ValueError, SyntaxError):
        return None


def rebuild_corpus() -> int:
    """Regenerate corpus.jsonl from every page on disk. Returns the record count.

    pages/ is the source of truth, so a re-crawl updates the corpus in place and
    an interrupted run never leaves it holding only the pages fetched so far.
    Written atomically.
    """
    count = 0
    tmp = CORPUS_PATH.with_name(CORPUS_PATH.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for path in sorted(PAGES_DIR.glob("*.md")):
            try:
                record = read_page(path)
            except OSError as e:
                log(f"  WARN unreadable page {path.name}: {e}")
                continue
            if record is None:
                log(f"  WARN malformed page {path.name}, left out of corpus")
                continue
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    tmp.replace(CORPUS_PATH)
    return count


def already_scraped() -> set[str]:
    """For --resume: collect slugs we've already written."""
    if not PAGES_DIR.exists():
        return set()
    return {p.stem + ".htm" for p in PAGES_DIR.glob("*.md")}


def save_frontier(queue: deque[str] | list[str], seen: set[str]) -> None:
    """Persist the pending queue and visited set so --resume can continue.

    Written atomically: a truncated frontier would silently shrink the crawl.
    """
    payload = {"pending": list(queue), "seen": sorted(seen)}
    tmp = FRONTIER_PATH.with_name(FRONTIER_PATH.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(FRONTIER_PATH)


def load_frontier() -> tuple[list[str], set[str]] | None:
    """Read a saved frontier. None when absent or unreadable."""
    if not FRONTIER_PATH.exists():
        return None
    try:
        data = json.loads(FRONTIER_PATH.read_text(encoding="utf-8"))
        return list(data.get("pending", [])), set(data.get("seen", []))
    except (OSError, ValueError) as e:
        log(f"WARN unreadable frontier {FRONTIER_PATH.name}: {e}")
        return None


# Link targets in converted pages: [text](Foo.htm), [text](Foo.htm "title"),
# and <Foo.htm> autolinks.
MD_LINK_RE = re.compile(
    r"""\(\s*([^()\s]+\.htm)(?:\#[^()\s]*)?(?:\s+"[^"]*")?\s*\)"""
    r"""|<\s*([^<>\s]+\.htm)(?:\#[^<>\s]*)?\s*>""",
    re.IGNORECASE,
)


def extract_links_from_markdown(text: str) -> list[str]:
    """Pull internal .htm links out of an already-converted page."""
    out = []
    for m in MD_LINK_RE.finditer(text):
        href = m.group(1) or m.group(2)
        absurl = urljoin(BASE, href).split("#", 1)[0]
        if absurl.startswith(BASE):
            out.append(canonical_url(absurl))
    return out


def rebuild_frontier_from_pages() -> list[str]:
    """Re-derive the crawl frontier from the pages already on disk.

    Raw HTML is not kept, so the converted markdown is the only surviving record
    of each page's outbound links. Used when resuming a corpus built before the
    frontier was persisted.
    """
    links: list[str] = []
    known: set[str] = set()
    if not PAGES_DIR.exists():
        return links
    for path in sorted(PAGES_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            log(f"  WARN unreadable page {path.name}: {e}")
            continue
        for link in extract_links_from_markdown(text):
            if link not in known:
                known.add(link)
                links.append(link)
    return links


def run(limit: int | None, rate: float, resume: bool) -> dict:
    """Crawl until the queue drains. Returns the manifest."""
    PAGES_DIR.mkdir(parents=True, exist_ok=True)

    # A fresh run re-fetches every page, overwriting each in place; pages it
    # doesn't reach stay on disk. Only the crawl position is reset.
    if not resume:
        FRONTIER_PATH.unlink(missing_ok=True)

    seen: set[str] = set()
    queue: deque[str] = deque()
    already: set[str] = set()
    frontier_source = "seeds"
    restored = 0

    if resume:
        already = already_scraped()
        log(f"Resume: {len(already)} pages already scraped, skipping")
        saved = load_frontier()
        if saved is not None:
            pending, seen = saved
            queue.extend(pending)
            frontier_source = "saved"
            restored = len(queue)
            log(f"Resume: restored frontier, {restored} URLs pending")
        elif already:
            # Corpus predates frontier persistence. Raw HTML is not kept, so
            # re-derive the links from the converted pages rather than re-fetch.
            queue.extend(rebuild_frontier_from_pages())
            frontier_source = "pages"
            restored = len(queue)
            log(f"Resume: no saved frontier, rebuilt {restored} URLs from pages on disk")

    for url in [BASE + s for s in SEEDS] + toc_seeds():
        if url not in seen:
            queue.append(url)

    scraped = 0
    failed = 0
    # Popped but not yet accounted for. Requeued if the crawl dies mid-page,
    # otherwise the URL sits in `seen` with nothing on disk and resume skips it.
    inflight: str | None = None
    try:
        while queue:
            if limit and scraped >= limit:
                log(f"Limit {limit} reached, stopping")
                break
            url = queue.popleft()
            if url in seen:
                continue
            slug = url.rsplit("/", 1)[-1]
            if slug in already:
                seen.add(url)
                continue

            inflight = url
            seen.add(url)
            log(f"[{scraped + 1}] GET {slug}")
            html = fetch(url)
            if html is None:
                alt = fallback_url(url)
                if alt is None:
                    log(f"  SKIP {slug}: 404 or permanent failure")
                    failed += 1
                elif alt not in seen:
                    log(f"  404 {slug}, trying {alt.rsplit('/', 1)[-1]}")
                    queue.appendleft(alt)
                inflight = None
                time.sleep(rate)
                continue

            try:
                record = page_to_record(url, html)
                write_page(record)
                scraped += 1
            except Exception as e:
                log(f"  ERROR parsing {slug}: {e}")
                failed += 1

            # Enqueue newly discovered links
            for link in extract_links(html):
                if link not in seen:
                    queue.append(link)
            inflight = None

            if scraped and scraped % FRONTIER_SAVE_EVERY == 0:
                save_frontier(queue, seen)

            time.sleep(rate)
    finally:
        # Also runs on Ctrl-C, so an interrupted crawl stays resumable.
        if inflight is not None:
            queue.appendleft(inflight)
            seen.discard(inflight)
        save_frontier(queue, seen)
        records = rebuild_corpus()
        log(f"Corpus rebuilt from pages/: {records} records")

    manifest = {
        "scraped": scraped,
        "failed": failed,
        "seen": len(seen),
        "queue_remaining": len(queue),
        "pages_on_disk": len(already),
        "records": records,
        "frontier_source": frontier_source,
        "frontier_restored": restored,
        "completed": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"DONE scraped={scraped} failed={failed} seen={len(seen)} queue_remaining={len(queue)}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Parse args, gate on terms acceptance, crawl. Returns an exit code.

    Callable as a library so `fusion-cad-mcp corpus build` and direct execution
    share one implementation, including the terms gate.
    """
    ap = argparse.ArgumentParser(prog="fusion-cad-mcp corpus build")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N pages (smoke test)")
    ap.add_argument("--rate", type=float, default=1.0, help="Seconds between requests")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Continue a prior crawl: skip pages already on disk and restore the pending queue",
    )
    ap.add_argument("--contact", default="", help="Optional contact string for the User-Agent")
    ap.add_argument("--user-agent", default="", help="Override the default User-Agent")
    ap.add_argument(
        "--i-accept-autodesk-terms",
        action="store_true",
        help="Confirm this user-initiated local cache build complies with Autodesk terms",
    )
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.i_accept_autodesk_terms:
        print(TERMS_NOTICE, file=sys.stderr)
        # isatty() reports True in some non-interactive contexts, where input()
        # raises EOFError. Catch it and print the same instruction.
        if not sys.stdin.isatty():
            print("ERROR: pass --i-accept-autodesk-terms in non-interactive runs.", file=sys.stderr)
            return 2
        try:
            answer = input("Type YES to build the local corpus cache: ")
        except (EOFError, KeyboardInterrupt):
            print(
                "\nERROR: no interactive input available. "
                "Pass --i-accept-autodesk-terms to confirm.",
                file=sys.stderr,
            )
            return 2
        if answer.strip() != "YES":
            print("Cancelled.", file=sys.stderr)
            return 130

    if args.user_agent:
        SESSION.headers["User-Agent"] = args.user_agent
    elif args.contact:
        SESSION.headers["User-Agent"] = (
            f"fusion-cad-mcp-corpus-builder/0.1 (local cache; contact {args.contact})"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        manifest = run(args.limit, args.rate, args.resume)
    except KeyboardInterrupt:
        log("Interrupted by user")
        return 130

    # A resume that scrapes nothing and recovers no frontier lost the crawl
    # rather than finishing it. Fail loudly instead of claiming success.
    if (
        args.resume
        and manifest["scraped"] == 0
        and manifest["pages_on_disk"]
        and manifest["frontier_source"] != "saved"
        and manifest["frontier_restored"] == 0
    ):
        print(
            "WARNING: resume found no pending work and scraped nothing; the crawl "
            "frontier could not be restored.\nThe corpus contains only the "
            f"{manifest['pages_on_disk']} pages already on disk and is very likely "
            "incomplete.\nRe-run without --resume to rebuild it.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
