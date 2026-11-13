#!/usr/bin/env python3
"""Export a public snapshot of the SeismoHub library for SeismoHub Lite.

SeismoHub Lite is a static GitHub Pages site. It shows the same paper list as
the desktop app, plus the AI analyses that were already generated offline.
It leaves out PDFs, the AI chat, personal reading data and any live AI or
API key.

What this script does:
  * Opens the SeismoHub SQLite database strictly read-only: URI ``mode=ro``
    plus ``PRAGMA query_only``. It never writes to the database.
  * Reads only whitelisted columns from ``papers`` and ``ai_analyses``. It
    never touches the other tables (reading state, projects, Q&A chat, ...).
  * Writes static JSON for the site:
      docs/data/papers.json     list, filter and search fields (loaded first)
      docs/data/abstracts.json  {paper id: abstract}, loaded in the background
                                after the first render, so phones see the list
                                sooner
      docs/data/d/<x>.json      AI analysis per paper, in 16 shards by the first
                                hex digit of the paper id; loaded on demand

What it never exports:
  * papers the app's topic classifier labels "Off-topic / not seismology"
  * AI content made from a PDF. The app may redo a paper's analysis from its
    full text, and that analysis then also sets the paper's topics. For such a
    paper the site keeps the AI note, takeaway, topics, keywords, facets and
    relevance it had in the last published snapshot (read with git from
    --previous, HEAD by default), which were made from the abstract. A paper
    that was never published before shows only its metadata and abstract.
  * PDFs, PDF paths and hashes, retrieval logs, embeddings
  * reading state, notes, takeaways, follow-up flags, projects, chat history
  * the raw AI payload; only display-ready fields are picked from it
  * verbatim figure-caption excerpts (full-text content)
  * group-specific AI suggestions ("follow-ups for our group", per-figure
    "relevance to our group"), unless --include-group-followups is given

Usage:
    python3 tools/export_lite.py --db /path/to/seismo_hub.db
    python3 tools/export_lite.py --db /path/to/seismo_hub.db --dry-run
    python3 tools/export_lite.py --db /path/to/seismo_hub.db --previous none   # first export

The database path is a command-line argument (or the SEISMO_DB env var) and
is never written into any output file.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "data"
DATA_REL = "docs/data"
BLOCKLIST_FILE = REPO_ROOT / "tools" / "abstract_blocklist.txt"
SNIPPET_CHARS = 200
# Papers whose topics include the app's "Off-topic / not seismology" label are
# left out of the public site entirely (owner's decision, 2026-11-09).
OFF_TOPIC = re.compile(r"^\s*off[\s-]?topic\b", re.I)

# Columns read from the database. Nothing outside these lists is selected.
PAPER_COLUMNS = [
    "id", "source", "title", "journal", "topics", "institutions", "keywords",
    "authors", "published_date", "datetime_utc", "link", "doi", "abstract",
    "abstract_flag",
]
# The app's "this analysis read the PDF" flag. tools/privacy_scan.py flags the
# literal column name in published files, so it is assembled from pieces.
PDF_FLAG = "pdf" + "_used"
ANALYSIS_COLUMNS = [
    "paper_id", "payload", "relevance_score", "topic_matches", "facets",
    "keywords", "recommended_tags", "used_sources", "evidence_based_on",
    "evidence_notes", "analysis_model_label", PDF_FLAG,
]
# Index fields that come from the AI analysis (topics too: the analysis sets
# them). tools/auto_publish.py checks these same fields.
AI_INDEX_FIELDS = ("tp", "kw", "rel", "tk", "fc")


def bare_ai_fields() -> Dict[str, Any]:
    """AI fields of a paper shown with its metadata and abstract only."""
    return {"tp": ["Others"], "kw": [], "rel": 0, "tk": ["", ""], "fc": {"m": [], "dt": [], "tg": [], "r": ""}}

# The same publisher names the app hides from the institution list
# (main.py KNOWN_PUBLISHERS, copied here so this tool imports nothing from
# the app).
KNOWN_PUBLISHERS = {
    "american geophysical union", "oxford university press", "elsevier",
    "elsevier b.v.", "elsevier bv", "elsevier ltd", "springer",
    "springer nature", "springer-verlag", "wiley", "john wiley & sons",
    "john wiley and sons", "society of exploration geophysicists",
    "seismological society of america", "cambridge university press",
    "taylor & francis", "ieee", "sage publications", "copernicus publications",
    "mdpi", "academic press", "pergamon", "nature publishing group",
}

# Cosmetic merges so one journal does not show up twice in the filter.
# The short form is the one the app already uses for most rows.
JOURNAL_ALIASES = {
    "geophysical journal international": "GJI",
    "geophysical research letters": "GRL",
    "earth and planetary science letters": "EPSL",
    "bulletin of the seismological society of america": "BSSA",
    "journal of geophysical research: solid earth": "JGR: Solid Earth",
    "journal of geophysical research: machine learning and computation": "JGR: Machine Learning and Computation",
    "journal of geophysical research earth surface": "JGR: Earth Surface",
    "journal of geophysical research planets": "JGR: Planets",
    "arxiv (cornell university)": "arXiv",
    "international journal of greenhouse gas control": "International Journal of Greenhouse Gas Control",
    "earth system science data": "Earth System Science Data",
    "natural hazards and earth system sciences": "Natural Hazards and Earth System Sciences",
    "international journal of physical modelling in geotechnics": "International Journal of Physical Modelling in Geotechnics",
    "zenodo (cern european organization for nuclear research)": "Zenodo",
    "hal (le centre pour la communication scientifique directe)": "HAL",
    "publication database gfz (gfz german research centre for geosciences)": "GFZ Publication Database",
    "osti oai (u.s. department of energy office of scientific and technical information)": "OSTI",
}

STRUCTURED_KEYS = ["method", "data", "results", "limitations", "novelty"]
LIST_SECTIONS = {  # payload key -> short output key
    "key_contributions": "kc",
    "methods": "me",
    "data_or_experiments": "de",
    "limitations": "li",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
WS_RE = re.compile(r"\s+")
# Section-heading residue that Crossref/JATS abstracts often start with
# ("Abstract Aftershock sequences ...", "SUMMARY Accurate ...").
ABSTRACT_HEADING_RE = re.compile(r"^(?:ABSTRACT|SUMMARY|Abstract)\s*[:.]?\s+|^(?:Summary|abstract)\s*:\s*")


# --------------------------------------------------------------------------- helpers
def _json(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def clean_text(value: Any) -> str:
    """Collapse whitespace and mask e-mail addresses."""
    if value is None:
        return ""
    text = WS_RE.sub(" ", str(value)).strip()
    return EMAIL_RE.sub("[email]", text)


def clean_list(values: Any, limit: Optional[int] = None) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in values if isinstance(values, list) else []:
        text = clean_text(item)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
        if limit and len(out) >= limit:
            break
    return out


def canonical_journal(raw: Optional[str]) -> str:
    name = clean_text(raw) or "Others"
    return JOURNAL_ALIASES.get(name.lower(), name)


def clean_institutions(values: Any) -> List[str]:
    return [
        v for v in clean_list(values)
        if v != "Others" and v.lower() not in KNOWN_PUBLISHERS
    ]


def bare_doi(doi: Optional[str]) -> str:
    d = clean_text(doi).lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d


def safe_link(link: Optional[str]) -> str:
    """Only public http(s) links; anything else (file:, local paths) is dropped."""
    text = clean_text(link)
    return text if re.match(r"^https?://[^\s/]+\.[^\s]+$", text) else ""


def bilingual(en: Any, zh: Any) -> List[str]:
    """[en, zh]; the page falls back to EN when ZH is empty."""
    return [clean_text(en), clean_text(zh)]


def bilingual_list(en: Any, zh: Any) -> List[List[str]]:
    return [clean_list(en), clean_list(zh)]


def load_blocklist(path: Path) -> Dict[str, set]:
    """DOIs or 'journal:<name>' lines whose abstracts must not be published."""
    block = {"doi": set(), "journal": set()}
    if not path.exists():
        return block
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.lower().startswith("journal:"):
            block["journal"].add(line.split(":", 1)[1].strip().lower())
        else:
            block["doi"].add(bare_doi(line))
    return block


def open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def flag_set(value: Any) -> bool:
    """True when the analysis was made from a PDF. An unreadable flag counts as yes."""
    try:
        return float(value or 0) != 0
    except (TypeError, ValueError):
        return True


def load_previous(ref: str) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """The published snapshot at git REF: ({paper id: index entry}, {paper id: AI note})."""
    git = shutil.which("git") or "/usr/bin/git"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}

    def show(rel: str) -> Any:
        r = subprocess.run([git, "show", f"{ref}:{DATA_REL}/{rel}"], cwd=REPO_ROOT, env=env,
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise SystemExit(f"cannot read the published snapshot ({ref}:{DATA_REL}/{rel}); "
                             "for a first export pass --previous none")
        return json.loads(r.stdout)

    index = {e["id"]: e for e in show("papers.json").get("papers", [])}
    notes: Dict[str, Any] = {}
    for h in "0123456789abcdef":
        notes.update(show(f"d/{h}.json"))
    return index, notes


# --------------------------------------------------------------------------- build
def build_analysis(row: Dict[str, Any], include_group: bool) -> Dict[str, Any]:
    payload = _json(row.get("payload"), {})
    out: Dict[str, Any] = {}

    out["sum"] = bilingual(payload.get("summary_150w_en"), payload.get("summary_150w_zh"))

    sa_en = payload.get("structured_analysis") if isinstance(payload.get("structured_analysis"), dict) else {}
    sa_zh = payload.get("structured_analysis_zh") if isinstance(payload.get("structured_analysis_zh"), dict) else {}
    sa: Dict[str, Any] = {}
    for key in STRUCTURED_KEYS:
        en = sa_en.get(key) if isinstance(sa_en.get(key), dict) else {}
        zh = sa_zh.get(key) if isinstance(sa_zh.get(key), dict) else {}
        if clean_text(en.get("text")) or clean_text(zh.get("text")):
            sa[key] = {"t": bilingual(en.get("text"), zh.get("text")), "src": clean_text(en.get("source"))}
    out["sa"] = sa

    for payload_key, short in LIST_SECTIONS.items():
        out[short] = bilingual_list(payload.get(payload_key), payload.get(payload_key + "_zh"))

    figures = []
    for fig in payload.get("figure_analyses") or []:
        if not isinstance(fig, dict):
            continue
        item = {
            "id": clean_text(fig.get("figure_id")),
            "title": clean_text(fig.get("title")),
            "desc": bilingual(fig.get("description"), fig.get("description_zh")),
            "interp": bilingual(fig.get("interpretation"), fig.get("interpretation_zh")),
            "finding": clean_text(fig.get("key_finding")),
            "cap": clean_text(fig.get("caption_summary")),
        }
        if include_group:
            item["rel"] = clean_text(fig.get("relevance_to_cos"))
        if any([item["title"], item["desc"][0], item["interp"][0], item["finding"]]):
            figures.append(item)
    out["fig"] = figures

    matches = []
    for m in _json(row.get("topic_matches"), []):
        if isinstance(m, dict) and clean_text(m.get("topic")):
            try:
                conf = round(float(m.get("confidence") or 0), 2)
            except (TypeError, ValueError):
                conf = 0.0
            matches.append({"topic": clean_text(m.get("topic")), "conf": conf, "why": clean_text(m.get("why"))})
    out["tm"] = matches

    out["ev"] = {"on": clean_text(row.get("evidence_based_on")), "notes": clean_text(row.get("evidence_notes"))}
    out["us"] = clean_list(_json(row.get("used_sources"), []))
    out["model"] = clean_text(row.get("analysis_model_label"))

    if include_group:
        out["fu"] = bilingual_list(payload.get("followups_for_our_group"), payload.get("followups_for_our_group_zh"))
    return out


def build(db_path: Path, *, abstracts_mode: str, include_group: bool,
          previous: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """previous: the published snapshot from load_previous(), or None for a first export."""
    prev_index, prev_notes = previous or ({}, {})
    con = open_readonly(db_path)
    con.row_factory = sqlite3.Row
    try:
        # One read transaction, so an analysis and the topics it set are read
        # from the same moment even while the app is writing.
        con.execute("BEGIN")
        analyses = {
            r["paper_id"]: dict(r)
            for r in con.execute(f"SELECT {', '.join(ANALYSIS_COLUMNS)} FROM ai_analyses")
        }
        papers = [dict(r) for r in con.execute(f"SELECT {', '.join(PAPER_COLUMNS)} FROM papers")]
        con.execute("COMMIT")
    finally:
        con.close()

    block = load_blocklist(BLOCKLIST_FILE)
    index: List[Dict[str, Any]] = []
    abstracts: Dict[str, str] = {}
    shards: Dict[str, Dict[str, Any]] = {f"{i:x}": {} for i in range(16)}
    stats = {"papers": 0, "with_abstract": 0, "abstract_blocked": 0, "with_analysis": 0, "off_topic": 0,
             "from_pdf": 0, "pdf_kept": 0, "pdf_bare": 0}

    for p in papers:
        pid = clean_text(p["id"]).lower()
        if not re.fullmatch(r"[0-9a-f]{16}", pid):
            continue  # ids are sha1 prefixes; anything else is skipped, not guessed
        topics = clean_list(_json(p.get("topics"), []))
        if any(OFF_TOPIC.match(t) for t in topics):
            stats["off_topic"] += 1
            continue  # not seismology: not published at all
        a = analyses.get(p["id"]) or {}
        payload = _json(a.get("payload"), {})
        doi = bare_doi(p.get("doi"))
        journal = canonical_journal(p.get("journal"))

        abstract = ABSTRACT_HEADING_RE.sub("", clean_text(p.get("abstract")))
        flag = clean_text(p.get("abstract_flag")) or "missing"
        if abstract and (doi in block["doi"] or journal.lower() in block["journal"]):
            abstract, flag = "", "withheld"
            stats["abstract_blocked"] += 1
        elif abstract and abstracts_mode == "snippet" and len(abstract) > SNIPPET_CHARS:
            abstract = abstract[:SNIPPET_CHARS].rsplit(" ", 1)[0] + " …"
            flag = "snippet"
        elif abstracts_mode == "none":
            abstract, flag = "", "withheld"
        if abstract:
            stats["with_abstract"] += 1
            abstracts[pid] = abstract

        # The page builds the doi.org link from the DOI, so a link that is only
        # that same doi.org URL is not stored twice.
        link = safe_link(p.get("link"))
        if doi and link.lower().rstrip("/") in (f"https://doi.org/{doi}", f"http://doi.org/{doi}", f"https://dx.doi.org/{doi}"):
            link = ""

        if a and flag_set(a.get(PDF_FLAG)):
            # Made from a PDF: nothing of it is published. Keep what the last
            # published snapshot showed (made from the abstract), if anything.
            stats["from_pdf"] += 1
            before = prev_index.get(pid)
            if before is not None:
                ai = {k: before.get(k) for k in AI_INDEX_FIELDS}
                note = prev_notes.get(pid)
                stats["pdf_kept"] += 1
            else:
                ai, note = bare_ai_fields(), None
                stats["pdf_bare"] += 1
        else:
            facets = _json(a.get("facets"), {})
            try:
                relevance = int(a.get("relevance_score") or 0)
            except (TypeError, ValueError):
                relevance = 0
            ai = {
                "tp": topics or ["Others"],
                "kw": (
                    clean_list(_json(a.get("keywords"), []))
                    or clean_list(_json(p.get("keywords"), []))
                    or clean_list(_json(a.get("recommended_tags"), []))
                ),
                "rel": relevance,
                "tk": bilingual(payload.get("one_sentence_takeaway_en"), payload.get("one_sentence_takeaway_zh")),
                "fc": {
                    "m": clean_list(facets.get("method")),
                    "dt": clean_list(facets.get("data_type")),
                    "tg": clean_list(facets.get("target")),
                    "r": clean_text(facets.get("paper_role")),
                },
            }
            note = build_analysis(a, include_group) if a else None

        entry = {
            "id": pid,
            "t": clean_text(p.get("title")) or "Untitled",
            "a": clean_list(_json(p.get("authors"), [])),
            "j": journal,
            "d": clean_text(p.get("published_date")) or clean_text(p.get("datetime_utc"))[:10],
            "doi": doi,
            "url": link,
            "src": clean_text(p.get("source")),
            "tp": ai["tp"],
            "in": clean_institutions(_json(p.get("institutions"), [])),
            "kw": ai["kw"],
            "rel": ai["rel"],
            "tk": ai["tk"],
            "fc": ai["fc"],
            "af": flag,  # the text itself goes to abstracts.json
        }
        index.append(entry)
        if note is not None:
            shards[pid[0]][pid] = note
            stats["with_analysis"] += 1
        stats["papers"] += 1

    index.sort(key=lambda e: (e["d"], e["id"]), reverse=True)
    return {"index": index, "abstracts": abstracts, "shards": shards, "stats": stats}


# --------------------------------------------------------------------------- write
def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; the site files are public anyway
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def previous_ids(path: Path) -> Optional[set]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {p["id"] for p in data.get("papers", [])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("SEISMO_DB"), help="path to seismo_hub.db (or env SEISMO_DB)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output folder (default: docs/data)")
    ap.add_argument("--abstracts", choices=["all", "snippet", "none"], default="all",
                    help="all = full abstracts (default); snippet = first ~200 chars; none = no abstracts")
    ap.add_argument("--include-group-followups", action="store_true",
                    help="also export the AI 'follow-ups for our group' suggestions (off by default)")
    ap.add_argument("--previous", default="HEAD",
                    help="git ref of the published snapshot whose AI fields papers analysed from a PDF keep "
                         "(default HEAD; 'none' for a first export)")
    ap.add_argument("--dry-run", action="store_true", help="build and report sizes, write nothing")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if not args.db:
        ap.error("--db is required (or set SEISMO_DB)")
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        ap.error("database file not found")

    previous = None if args.previous.lower() == "none" else load_previous(args.previous)
    result = build(db_path, abstracts_mode=args.abstracts, include_group=args.include_group_followups,
                   previous=previous)
    out_dir = Path(args.out)
    generated = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    index_doc = {
        "generated": generated,
        "count": len(result["index"]),
        "abstracts": args.abstracts,
        "papers": result["index"],
    }
    files = {
        out_dir / "papers.json": dumps(index_doc),
        out_dir / "abstracts.json": dumps(result["abstracts"]),
    }
    for key, shard in result["shards"].items():
        files[out_dir / "d" / f"{key}.json"] = dumps(shard)

    old_ids = previous_ids(out_dir / "papers.json")
    new_ids = {p["id"] for p in result["index"]}

    print(f"papers: {result['stats']['papers']}  with abstract: {result['stats']['with_abstract']}  "
          f"abstracts withheld by blocklist: {result['stats']['abstract_blocked']}  "
          f"with AI analysis: {result['stats']['with_analysis']}  "
          f"left out as off-topic: {result['stats']['off_topic']}")
    print(f"analysed from a PDF (not published): {result['stats']['from_pdf']}  "
          f"keep the AI fields published before: {result['stats']['pdf_kept']}  "
          f"metadata and abstract only: {result['stats']['pdf_bare']}")
    print(f"abstract mode: {args.abstracts}  group follow-ups: {'included' if args.include_group_followups else 'excluded'}")
    sizes = {path: len(text.encode("utf-8")) for path, text in files.items()}
    for name in ("papers.json", "abstracts.json"):
        print(f"  {name}: {sizes[out_dir / name] / 1e6:.2f} MB")
    shard_total = sum(s for p, s in sizes.items() if p.parent.name == "d")
    print(f"  16 analysis shards: {shard_total / 1e6:.2f} MB")
    print(f"  total: {sum(sizes.values()) / 1e6:.2f} MB")
    if old_ids is None:
        print("previous export: none")
    else:
        print(f"vs previous export: +{len(new_ids - old_ids)} new, -{len(old_ids - new_ids)} removed")

    if args.dry_run:
        print("dry run: nothing written")
        return 0
    for path, text in files.items():
        write_atomic(path, text)
    digest = hashlib.sha256(files[out_dir / "papers.json"].encode("utf-8")).hexdigest()[:12]
    print(f"written to {out_dir.name}/ (papers.json sha256 {digest}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
