#!/usr/bin/env python3
"""Pre-publish privacy scan for SeismoHub Lite.

Scans every file that `git add -A` would publish (tracked files plus
untracked files that .gitignore does not exclude) and the git identity.
It exits non-zero on any blocking finding.

Blocking checks:
  * secrets: API-key, token and private-key patterns. With --env-file, the
    actual values from those files are also compared in memory. Only the
    variable NAME is ever printed, never the value.
  * local paths: home directories, temp dirs, file-scheme URLs, local folder names
  * e-mail addresses (abstract e-mails are masked by the exporter)
  * PDF material: .pdf files or references, PDF storage folder names, PDF fields
  * personal data: reading-state, notes, takeaway, project, chat and
    retrieval field names
  * data shape: JSON keys must match the exporter's whitelist exactly
  * group-specific AI suggestions, unless --allow-group-followups
  * files: unexpected file types, binaries, oversized files
  * git: the commit identity must be a GitHub noreply address

Review-only checks (reported, not blocking): long hex strings and UUIDs.
These can be harmless (hashes, dataset ids) but are worth a look.

With --db, it also fingerprints private text from the app database (opened
read-only) and confirms none of it appears in the output: Q&A chat messages,
reading notes, takeaways and stored PDF file names.

tools/privacy_allowlist.txt lists exact strings that a human reviewed and
accepted, for example a file name quoted inside a public abstract. A match on
one of those strings is reported as "allowed", not blocked. Each entry must
say why it is safe.

Usage:
    python3 tools/privacy_scan.py [--env-file PATH ...] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = REPO / "tools" / "privacy_allowlist.txt"
MAX_FILE_MB = 25
ALLOWED_SUFFIXES = {".html", ".json", ".py", ".md", ".txt", ""}
ALLOWED_NAMES = {".gitignore", ".nojekyll", "LICENSE"}

# Some literals are assembled from pieces so this file never matches itself.
_U = "Us" + "ers"
LOCAL_PATH_PATTERNS = [
    ("home directory", re.compile(r"/" + _U + r"/[^\s\"'<>]+")),
    ("home directory", re.compile(r"/ho" + r"me/[a-z_][a-z0-9_-]*/")),
    ("temp directory", re.compile(r"/pri" + r"vate/(?:tmp|var)/")),
    ("windows path", re.compile(r"[A-Za-z]:\\\\" + _U + r"\\\\", re.I)),
    ("file URL", re.compile(r"fi" + r"le://", re.I)),
    ("home shortcut", re.compile(r"(?<![\w.])~/" + r"\.?[A-Za-z]")),
    ("local folder name", re.compile("Connections" + " and SWP|seismohub-v2" + "-release", re.I)),
    ("app config dir", re.compile(r"seismo_" + r"harvester", re.I)),
]
SECRET_PATTERNS = [
    ("Google API key", re.compile(r"AI" + r"za[0-9A-Za-z\-_]{35}")),
    ("OpenAI/Anthropic-style key", re.compile(r"\bsk-" + r"(?:ant-)?[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_" + r"[A-Za-z0-9]{30,}|github_" + r"pat_[A-Za-z0-9_]{20,}")),
    ("AWS key id", re.compile(r"\bAK" + r"IA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox" + r"[baprs]-[A-Za-z0-9-]{10,}")),
    ("Bearer token", re.compile(r"Bear" + r"er\s+[A-Za-z0-9\-._~+/]{20,}=*")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIV" + r"ATE KEY-----")),
    ("key assignment", re.compile(
        r"\b(?:api[_-]?key|apikey|secret|access[_-]?token|auth[_-]?token|inst" + r"token|passw(?:or)?d)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9\-_/+]{12,}", re.I)),
]
REVIEW_PATTERNS = [
    ("long hex string", re.compile(r"\b[0-9a-f]{32,}\b", re.I)),
    ("UUID", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PDF_PATTERNS = [
    ("PDF file reference", re.compile(r"[\w\-./]+\.pdf\b", re.I)),
    ("PDF storage folder", re.compile(r"paper_" + r"bundles|/down" + r"loads/|_downloads_" + r"backup")),
    ("PDF field", re.compile(r"\bpdf_(?:path|sha256|url|used)\b")),
]
PERSONAL_FIELDS = re.compile(
    r"\b(?:reading_" + r"notes|reading_intent|user_takeaway_(?:what|why|at)|follow_up_at|last_opened_at|"
    r"liked_at|dismissed_at|archived_at|read_at|is_read|is_liked|is_dismissed|is_archived|"
    r"qa_" + r"messages|user_paper_states|project_papers|project_folder|retrieval_(?:status|source_url|error_reason)|"
    r"embedding_model|analysis_payload)\b")

# Exact data shape written by tools/export_lite.py
INDEX_TOP = {"generated", "count", "abstracts", "papers"}
INDEX_PAPER = {"id", "t", "a", "j", "d", "doi", "url", "src", "tp", "in", "kw", "rel", "tk", "fc", "ab", "af"}
INDEX_FACETS = {"m", "dt", "tg", "r"}
SHARD_ENTRY = {"sum", "sa", "kc", "me", "de", "li", "fig", "tm", "ev", "us", "model"}
FIG_ITEM = {"id", "title", "desc", "interp", "finding", "cap"}


class Report:
    def __init__(self):
        self.block: list[str] = []
        self.review: list[str] = []
        self.info: list[str] = []

    def hit(self, kind: str, where: str, detail: str = "", review: bool = False):
        line = f"{kind}: {where}" + (f"  [{detail}]" if detail else "")
        (self.review if review else self.block).append(line)


def mask(s: str) -> str:
    s = s.strip()
    return s[:4] + "…" + f"({len(s)} chars)" if len(s) > 6 else "…"


def load_allowlist() -> dict[str, str]:
    """Exact strings accepted after human review -> the recorded reason."""
    allowed: dict[str, str] = {}
    if ALLOWLIST_FILE.exists():
        for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
            entry, _, reason = line.partition("#")
            entry = entry.strip()
            if entry:
                allowed[entry] = reason.strip() or "no reason given"
    return allowed


def publish_list() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO, check=True, capture_output=True,
    ).stdout.decode("utf-8")
    return [REPO / p for p in out.split("\0") if p]


def load_env_values(paths: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for p in paths:
        try:
            text = Path(p).expanduser().read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.replace("export ", "").strip()
            val = val.strip().strip("'\"")
            if len(val) >= 8 and not re.fullmatch(r"(?i)true|false|none|null|\d+(\.\d+)?", val):
                values[key] = val
    return values


def db_fingerprints(db: str) -> dict[str, list[str]]:
    """Snippets of private text that must never appear in published files."""
    fp: dict[str, list[str]] = {"chat": [], "notes": [], "pdf": []}
    uri = Path(db).expanduser().resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(qa_messages)")}
        text_col = next((c for c in ("content", "text", "message", "body") if c in cols), None)
        if text_col:
            for (t,) in con.execute(f"SELECT {text_col} FROM qa_messages"):
                t = re.sub(r"\s+", " ", str(t or "")).strip()
                if len(t) >= 40:
                    fp["chat"].append(t[:60])
        for col in ("reading_notes", "user_takeaway_what", "user_takeaway_why", "reading_intent"):
            for (t,) in con.execute(f"SELECT {col} FROM papers WHERE {col} IS NOT NULL AND {col} != ''"):
                t = re.sub(r"\s+", " ", str(t)).strip()
                if len(t) >= 20 and t not in ("{}", "[]"):
                    fp["notes"].append(t[:50])
        pcol = "pdf" + "_path"
        for (t,) in con.execute(f"SELECT {pcol} FROM papers WHERE {pcol} IS NOT NULL AND {pcol} != ''"):
            name = Path(str(t)).name
            if len(name) >= 8:
                fp["pdf"].append(name)
    finally:
        con.close()
    return fp


def check_json_shape(path: Path, rel: str, rep: Report, allow_group: bool):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        rep.hit("invalid JSON", rel, str(e)[:80])
        return
    if path.name == "papers.json":
        if set(data) != INDEX_TOP:
            rep.hit("unexpected top-level keys", rel, ", ".join(sorted(set(data) ^ INDEX_TOP)))
        for p in data.get("papers", []):
            extra = set(p) - INDEX_PAPER
            if extra:
                rep.hit("unexpected paper keys", rel, ", ".join(sorted(extra)))
                break
            if set(p.get("fc", {})) - INDEX_FACETS:
                rep.hit("unexpected facet keys", rel, ", ".join(sorted(set(p["fc"]) - INDEX_FACETS)))
                break
            url = p.get("url", "")
            if url and (not re.match(r"^https?://", url) or re.search(r"//(localhost|127\.|0\.0\.0\.0|\[::1\])", url)):
                rep.hit("non-public link", rel, url[:60])
        rep.info.append(f"{rel}: {len(data.get('papers', []))} papers, keys = whitelist")
    elif path.parent.name == "d":
        allowed = SHARD_ENTRY | ({"fu"} if allow_group else set())
        fig_allowed = FIG_ITEM | ({"rel"} if allow_group else set())
        for pid, entry in data.items():
            if not re.fullmatch(r"[0-9a-f]{16}", pid):
                rep.hit("unexpected shard id", rel, pid[:20])
                break
            extra = set(entry) - allowed
            if extra:
                kind = "group follow-ups present" if "fu" in extra else "unexpected analysis keys"
                rep.hit(kind, rel, ", ".join(sorted(extra)))
                break
            bad_fig = [k for f in entry.get("fig", []) for k in f if k not in fig_allowed]
            if bad_fig:
                rep.hit("unexpected figure keys", rel, ", ".join(sorted(set(bad_fig))))
                break


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-publish privacy scan for SeismoHub Lite")
    ap.add_argument("--env-file", action="append", default=[], help="compare against values in this env file (values are never printed)")
    ap.add_argument("--db", help="app database (opened read-only) for private-text fingerprints")
    ap.add_argument("--allow-group-followups", action="store_true", help="accept exported group follow-up suggestions")
    args = ap.parse_args()

    rep = Report()
    env_values = load_env_values(args.env_file)
    if args.env_file:
        rep.info.append(f"comparing against {len(env_values)} secret-like values from {len(args.env_file)} env file(s) (values not shown)")
    fps = db_fingerprints(args.db) if args.db else {}
    if fps:
        rep.info.append("db fingerprints: " + ", ".join(f"{k}={len(v)}" for k, v in fps.items()))

    allow = load_allowlist()
    allowed_seen: set[str] = set()
    files = publish_list()
    total = 0
    for path in files:
        rel = str(path.relative_to(REPO))
        size = path.stat().st_size
        total += size
        if path.name not in ALLOWED_NAMES and path.suffix.lower() not in ALLOWED_SUFFIXES:
            rep.hit("unexpected file type", rel)
        if size > MAX_FILE_MB * 1e6:
            rep.hit("file too large", rel, f"{size / 1e6:.1f} MB")
        raw = path.read_bytes()
        if b"\0" in raw[:4096]:
            rep.hit("binary file", rel)
            continue
        text = raw.decode("utf-8", errors="replace")

        def scan(kind, pat, show, allowable=True):
            for m in pat.finditer(text):
                s = m.group(0).strip()
                if allowable and s in allow:
                    allowed_seen.add(f"{kind} in {rel}: {s[:60]} ({allow[s]})")
                    continue
                rep.hit(kind, rel, show(s))
                return  # one blocking hit per check and file is enough

        for name, pat in SECRET_PATTERNS:  # secrets can never be allowlisted
            scan(f"secret pattern ({name})", pat, mask, allowable=False)
        for key, val in env_values.items():
            if val in text:
                rep.hit("value from env file", rel, f"variable {key}")
        for name, pat in LOCAL_PATH_PATTERNS:
            scan(f"local path ({name})", pat, lambda s: s[:50])
        scan("e-mail address", EMAIL, mask)
        for name, pat in PDF_PATTERNS:
            scan(f"PDF reference ({name})", pat, lambda s: s[:60])
        # This file defines the field-name list itself, so it skips only this check.
        m = None if path.resolve() == Path(__file__).resolve() else PERSONAL_FIELDS.search(text)
        if m:
            rep.hit("personal/chat field name", rel, m.group(0))
        for kind, snippets in fps.items():
            found = sum(1 for s in snippets if s in text)
            if found:
                rep.hit(f"private {kind} text from the app database", rel, f"{found} snippet(s)")
        for name, pat in REVIEW_PATTERNS:
            found = pat.findall(text)
            if found:
                rep.hit(name, rel, f"{len(found)}× e.g. {found[0][:12]}…", review=True)
        if path.suffix == ".json":
            check_json_shape(path, rel, rep, args.allow_group_followups)

    # git identity
    def git(*a):
        r = subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True)
        return r.stdout.strip()
    email = git("config", "user.email")
    if not email.endswith("@users.noreply.github.com"):
        rep.hit("git identity", "user.email", "not a GitHub noreply address")
    for line in git("log", "--format=%ae%n%ce").splitlines():
        if line and not line.endswith("@users.noreply.github.com"):
            rep.hit("git history", "commit author/committer", mask(line))
            break

    print(f"files that would be published: {len(files)} ({total / 1e6:.1f} MB)")
    for path in files:
        print("   ", path.relative_to(REPO), f"{path.stat().st_size / 1e3:,.0f} kB")
    for line in rep.info:
        print("info:", line)
    for line in sorted(allowed_seen):
        print("ALLOWED (reviewed):", line)
    for line in rep.review:
        print("REVIEW:", line)
    for line in rep.block:
        print("BLOCK:", line)
    print("RESULT:", "CLEAN" if not rep.block else f"{len(rep.block)} blocking finding(s)")
    return 1 if rep.block else 0


if __name__ == "__main__":
    sys.exit(main())
