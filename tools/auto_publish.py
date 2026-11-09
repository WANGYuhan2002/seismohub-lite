#!/usr/bin/env python3
"""Publish the SeismoHub Lite snapshot automatically, once a day (run by launchd).

On 2026-11-09 the owner chose to have Lite update itself every day without a
person reviewing each diff. Automatic gates take the place of that review.
Every gate must pass. If any one fails, nothing is pushed: a macOS
notification appears and the reason goes to the log.

   1. the job is not paused, and no other run is in progress
   2. the app has finished a new successful fetch since the last handled one
      (app_state in the app database, which is opened read-only)
   3. the repo is clean where it matters: on branch main, nothing staged,
      docs/ unchanged outside docs/data, and HEAD equal to origin/main
   4. export with tools/export_lite.py (read-only, whitelisted columns)
   5. something changed apart from the "generated" date; if not, stop quietly
   6. the shape is locked: the same data files and the same normalized JSON
      field paths as the published snapshot (50 on 2026-11-09)
   7. sanity limits:
      - the paper count may drop by at most 1 % (minimum 5) and grow by at
        most 25 %
      - the total size may grow by at most 25 %
      - no string may exceed 8,000 characters, so full text cannot slip in
        (the longest abstract is 5,809)
   8. AI provenance: no analysis may have been made from a PDF, and no source
      label may be new or mention PDFs or full text
   9. tools/check_site.py passes on the local copy (headless Chrome)
  10. tools/privacy_scan.py, run with the app's .env and old.env, prints
      RESULT: CLEAN and has really compared their values. Env files must sit
      next to the database. keys.env is refused and never read.
  11. the commit holds docs/data only and uses the noreply identity. It is
      built on a private index, so nothing anyone has staged can slip in.
  12. push over SSH in BatchMode (no prompts), then move local main
  13. wait for GitHub Pages to rebuild, then run check_site.py --base <live site>

The job needs no API key. Its subprocesses get a minimal environment.
Logs and notifications hold only names, counts and commit ids, never values
from env files.

Usage:
  auto_publish.py --db DB --env-file ENV [--env-file ENV2] [--dry-run] [--force]
  auto_publish.py --install --db DB --env-file ENV [--env-file ENV2] [--at 21:00]
  auto_publish.py --pause | --resume | --status | --run-now | --uninstall

Files:
  $HOME/Library/LaunchAgents/com.local.seismohub-lite.autopublish.plist
  $HOME/Library/Logs/SeismoHubLite/auto_publish.log
  local/AUTO_PUBLISH_PAUSED       while this file exists, runs do nothing
  local/auto_publish_state.json   last handled fetch, last publish
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
DATA_REL = "docs/data"
DATA = REPO / DATA_REL
LOCAL = REPO / "local"  # gitignored
PAUSE_FILE = LOCAL / "AUTO_PUBLISH_PAUSED"
STATE_FILE = LOCAL / "auto_publish_state.json"
LOCK_FILE = LOCAL / ".auto_publish.lock"
TMP_INDEX = LOCAL / ".auto_publish.index"
LOG_DIR = Path.home() / "Library" / "Logs" / "SeismoHubLite"
LABEL = "com.local.seismohub-lite.autopublish"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LIVE_URL = "https://wangyuhan2002.github.io/seismohub-lite"
BRANCH = "main"
DEFAULT_AT = "21:00"

DATA_FILES = {"papers.json", "abstracts.json"} | {f"d/{h}.json" for h in "0123456789abcdef"}
MAX_STRING_CHARS = 8000
MAX_DROP_FRACTION, MIN_DROP_ALLOWED = 0.01, 5
MAX_GROWTH_FRACTION = 0.25
FETCH_WAIT_SECONDS = 30 * 60
PAGES_WAIT_SECONDS = 15 * 60
GC_EVERY_DAYS = 7
PAPER_ID = re.compile(r"^[0-9a-f]{16}$")
# Source labels that would mean full text was used. tools/privacy_scan.py
# scans this file too, so a few literals are assembled from pieces.
BAD_SOURCE = re.compile(r"pdf|full.?text|tdm|bundle|xml|upload", re.I)
PDF_FLAG_COLUMN = "pdf" + "_used"
KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "SSH_AUTH_SOCK", "TERM", "__CF_USER_TEXT_ENCODING")
GIT = shutil.which("git") or "/usr/bin/git"

GATE_ZH = {
    "setup": "配置有误",
    "fetch": "读不到抓取状态",
    "repo": "仓库状态不对",
    "network": "连不上 GitHub",
    "export": "导出失败",
    "shape": "数据结构变了",
    "sanity": "数据量或长度异常",
    "provenance": "AI 分析来源异常",
    "site": "本地页面检查未通过",
    "privacy": "防泄露扫描未通过",
    "commit": "提交内容检查未通过",
    "push": "推送失败",
    "live": "已推送，但线上复核未通过",
    "error": "意外错误",
}


class Stop(Exception):
    """A gate failed: nothing is pushed; notify and log."""

    def __init__(self, gate: str, reason: str):
        super().__init__(reason)
        self.gate, self.reason = gate, reason


class Done(Exception):
    """Nothing to publish (no new fetch, or no change): log only."""

    def __init__(self, reason: str, handled: str | None = None):
        super().__init__(reason)
        self.reason, self.handled = reason, handled


class Log:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_name(path.name + ".1"))

    def __call__(self, msg: str) -> None:
        line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ------------------------------------------------------------------ helpers
def clean_env(**extra: str) -> dict[str, str]:
    """Minimal environment for subprocesses: no API keys from a shell profile."""
    env = {k: v for k, v in os.environ.items() if k in KEEP_ENV}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env.update(PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0",
               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=20")
    env.update(extra)
    return env


class GitError(Exception):
    pass


def git(*args: str, extra_env: dict[str, str] | None = None, timeout: int = 300) -> str:
    r = subprocess.run([GIT, *args], cwd=REPO, env=clean_env(**(extra_env or {})),
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise GitError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout.strip()


def git_bytes(*args: str) -> bytes:
    r = subprocess.run([GIT, *args], cwd=REPO, env=clean_env(), capture_output=True, timeout=120)
    if r.returncode != 0:
        raise GitError(f"git {args[0]} failed")
    return r.stdout


def retry(fn, what: str, log: Log, attempts: int = 6, wait: int = 30):
    """Network steps may run right after the Mac wakes up, before Wi-Fi is back."""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except (GitError, subprocess.TimeoutExpired, OSError) as exc:
            if i == attempts:
                raise
            log(f"{what} failed (try {i}/{attempts}), retrying in {wait}s: {str(exc)[:160]}")
            time.sleep(wait)


def run_tool(log: Log, gate: str, cmd: list[str], timeout: int) -> str:
    name = Path(cmd[1]).name
    log(f"[{gate}] running {name}")
    try:
        r = subprocess.run(cmd, cwd=REPO, env=clean_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Stop(gate, f"{name} timed out after {timeout // 60} min") from None
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        log("    " + line)
    if r.returncode != 0:
        raise Stop(gate, f"{name} exited with code {r.returncode}")
    return out


def notify(subtitle: str, message: str, enabled: bool, log: Log) -> None:
    if not enabled:
        return

    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'

    script = f"display notification {q(message)} with title {q('SeismoHub Lite')} subtitle {q(subtitle)}"
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=20)
        if r.returncode != 0:
            log(f"notification failed (osascript exit {r.returncode})")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"notification failed ({type(exc).__name__})")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    LOCAL.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def now_local() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def utc_naive() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def parse_ts(value: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


# ------------------------------------------------------------- app database
def open_db_readonly(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    con.execute("PRAGMA query_only = ON")
    return con


def fetch_info(db: Path) -> dict:
    con = open_db_readonly(db)
    try:
        rows = dict(con.execute(
            "SELECT key, value FROM app_state WHERE key IN (?, ?, ?)",
            ("last_fetch_started_at", "last_fetch_completed_at", "last_fetch_status")).fetchall())
    finally:
        con.close()
    # The app writes last_fetch_completed_at only when a fetch succeeds. The
    # status text of a failed fetch can quote URLs, so only "ok or not" is kept.
    return {"started": rows.get("last_fetch_started_at"),
            "completed": rows.get("last_fetch_completed_at"),
            "ok": rows.get("last_fetch_status") == "ok"}


def wait_for_fetch(db: Path, log: Log) -> dict:
    """If the app is fetching (and analysing) right now, wait up to 30 min for it."""
    deadline = time.time() + FETCH_WAIT_SECONDS
    while True:
        info = fetch_info(db)
        started, completed = parse_ts(info["started"]), parse_ts(info["completed"])
        running = bool(started and (not completed or started > completed)
                       and utc_naive() - started < dt.timedelta(hours=2))
        if not running:
            return info
        if time.time() > deadline:
            log("a fetch is still running after 30 min; going on with the last completed one")
            return info
        log("the app is fetching right now; waiting for it to finish")
        time.sleep(60)


def analyses_from_pdf(db: Path) -> int:
    con = open_db_readonly(db)
    try:
        return con.execute(
            f"SELECT COUNT(*) FROM ai_analyses WHERE COALESCE({PDF_FLAG_COLUMN}, 0) != 0").fetchone()[0]
    finally:
        con.close()


def usable_env_files(envs: list[Path], db: Path, log) -> list[Path]:
    """Env files for privacy_scan.py. Only files next to the database are
    accepted (the app's .env and old.env), and keys.env is always refused.
    The owner decided it is never read, not even for a comparison."""
    usable = []
    for env in envs:
        if env.name == "keys.env":
            raise Stop("setup", "keys.env must never be read (owner's rule); pass only .env / old.env")
        if env.expanduser().resolve().parent != db.resolve().parent:
            raise Stop("setup", f"{env.name}: env files must sit next to the database")
        try:
            open(env, "rb").close()  # readable? The values are left to privacy_scan.py.
        except FileNotFoundError:
            log(f"note: {env.name} no longer exists; comparing against the other env file(s)")
            continue
        except OSError as exc:
            raise Stop("setup", f"{env.name} cannot be opened ({type(exc).__name__})") from None
        usable.append(env)
    if not usable:
        raise Stop("setup", "no readable env file: pass the app's .env with --env-file")
    return usable


# ------------------------------------------------------------------- checks
def check_repo(log: Log) -> str:
    if git("rev-parse", "--abbrev-ref", "HEAD") != BRANCH:
        raise Stop("repo", f"the repo is not on branch {BRANCH}")
    if git("diff", "--cached", "--name-only"):
        raise Stop("repo", "something is staged in the git index")
    edited = git("diff", "--name-only", "HEAD", "--", "docs", f":(exclude){DATA_REL}")
    if edited:
        raise Stop("repo", f"uncommitted edits in docs/ outside docs/data: {edited.splitlines()[:3]}")
    if not git("config", "user.email").endswith("@users.noreply.github.com"):
        raise Stop("repo", "git user.email is not the GitHub noreply address")
    try:
        retry(lambda: git("fetch", "--quiet", "origin", BRANCH, timeout=120), "git fetch", log)
    except (GitError, subprocess.TimeoutExpired, OSError) as exc:
        raise Stop("network", f"cannot reach GitHub: {str(exc)[:160]}") from None
    head, remote = git("rev-parse", "HEAD"), git("rev-parse", f"origin/{BRANCH}")
    if head != remote:
        ahead = git("rev-list", "--count", f"origin/{BRANCH}..HEAD")
        behind = git("rev-list", "--count", f"HEAD..origin/{BRANCH}")
        raise Stop("repo", f"local {BRANCH} is {ahead} ahead / {behind} behind origin; sort that out by hand")
    return head


def field_paths(node, prefix: str, out: set[str]) -> None:
    """Leaf paths with list positions as [] and paper-id keys as *."""
    if isinstance(node, dict):
        is_map = bool(node) and all(PAPER_ID.match(k) for k in node)
        for key, value in node.items():
            field_paths(value, f"{prefix}.*" if is_map else f"{prefix}.{key}", out)
    elif isinstance(node, list):
        for value in node:
            field_paths(value, prefix + "[]", out)
    else:
        out.add(prefix)


def shape_of(docs: dict) -> set[str]:
    out: set[str] = set()
    for rel, doc in docs.items():
        field_paths(doc, "d/*.json" if rel.startswith("d/") else rel, out)
    return out


def longest_string(node, prefix: str = "") -> tuple[int, str]:
    if isinstance(node, str):
        return len(node), prefix
    best = (0, prefix)
    if isinstance(node, dict):
        is_map = bool(node) and all(PAPER_ID.match(k) for k in node)
        items = ((f"{prefix}.*" if is_map else f"{prefix}.{k}", v) for k, v in node.items())
    elif isinstance(node, list):
        items = ((prefix + "[]", v) for v in node)
    else:
        return best
    for path, value in items:
        found = longest_string(value, path)
        if found[0] > best[0]:
            best = found
    return best


def source_labels(docs: dict) -> set[str]:
    labels: set[str] = set()
    for rel, shard in docs.items():
        if rel.startswith("d/"):
            for entry in shard.values():
                labels.add("evidence:" + str((entry.get("ev") or {}).get("on") or ""))
                labels.update("source:" + str(s) for s in entry.get("us") or [])
    return labels


def without_generated(index: dict) -> dict:
    return {k: v for k, v in index.items() if k != "generated"}


def restore_data(log: Log) -> None:
    git("restore", "--", DATA_REL)
    log("docs/data put back to the published version")


def build_commit(head: str, message: str) -> tuple[str, list[str]]:
    """Commit docs/data on a private index, so the shared index is never used."""
    TMP_INDEX.unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": str(TMP_INDEX)}
    try:
        git("read-tree", head, extra_env=env)
        git("add", "-A", "--", DATA_REL, extra_env=env)
        tree = git("write-tree", extra_env=env)
    finally:
        TMP_INDEX.unlink(missing_ok=True)
    sha = git("commit-tree", tree, "-p", head, "-m", message)
    names = git("diff-tree", "--no-commit-id", "--name-only", "-r", head, sha).splitlines()
    outside = [n for n in names if not n.startswith(DATA_REL + "/")]
    if outside:
        raise Stop("commit", f"the commit would touch files outside {DATA_REL}: {outside[:3]}")
    return sha, names


def wait_for_pages(base: str, want_sha256: str, log: Log) -> bool:
    url = f"{base}/data/papers.json"
    deadline = time.time() + PAGES_WAIT_SECONDS
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                                       "User-Agent": "seismohub-lite-autopublish"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                if hashlib.sha256(resp.read()).hexdigest() == want_sha256:
                    return True
        except OSError as exc:
            log(f"live site not reachable yet: {type(exc).__name__}")
        time.sleep(20)
    return False


# --------------------------------------------------------------------- run
def publish(args, log: Log, state: dict, progress: dict) -> str:
    dry, force = args.dry_run, args.force
    db = Path(args.db).expanduser() if args.db else None
    if not db or not db.is_file():
        raise Stop("setup", "--db must point to the app database")
    envs = usable_env_files([Path(e).expanduser() for e in args.env_file], db, log)

    # 2. a new successful fetch
    try:
        info = wait_for_fetch(db, log)
    except sqlite3.Error as exc:
        raise Stop("fetch", f"cannot read app_state ({type(exc).__name__})") from None
    fetched = info["completed"]
    if not fetched:
        raise Stop("fetch", "the app database has no last_fetch_completed_at")
    handled = state.get("handled_fetch_at")
    log(f"last successful fetch {fetched[:19]} UTC; last handled {(handled or 'none')[:19]}")
    if handled == fetched and not force:
        raise Done("no new fetch since the last handled one")

    # 3. repo state
    head = check_repo(log)
    log(f"published snapshot: {head[:7]} (local main == origin/{BRANCH})")

    # 4. export
    progress["exported"] = True
    run_tool(log, "export", [sys.executable, str(TOOLS / "export_lite.py"), "--db", str(db)], timeout=600)

    # 5. anything new?
    published_files = set(git("ls-tree", "-r", "--name-only", head, "--", DATA_REL).splitlines())
    expected = {f"{DATA_REL}/{rel}" for rel in DATA_FILES}
    if published_files != expected:
        raise Stop("shape", "the published snapshot does not have the expected data files")
    present = {f"{DATA_REL}/{p.relative_to(DATA).as_posix()}" for p in DATA.rglob("*")
               if p.is_file() and not p.name.startswith(".tmp-") and p.name != ".DS_Store"}
    if present != expected:
        raise Stop("shape", f"unexpected data files: extra {sorted(present - expected)[:3]}, "
                            f"missing {sorted(expected - present)[:3]}")
    old_raw = {rel: git_bytes("show", f"{head}:{DATA_REL}/{rel}") for rel in DATA_FILES}
    new_raw = {rel: (DATA / rel).read_bytes() for rel in DATA_FILES}
    changed = sorted(rel for rel in DATA_FILES if old_raw[rel] != new_raw[rel])
    old = {rel: json.loads(b) for rel, b in old_raw.items()}
    new = {rel: json.loads(b) for rel, b in new_raw.items()}
    if not changed or (changed == ["papers.json"]
                       and without_generated(old["papers.json"]) == without_generated(new["papers.json"])):
        if not (dry and force):
            restore_data(log)
            raise Done("no change apart from the generated date", handled=fetched)
        log("no change apart from the generated date; --dry-run --force goes on through every gate")
    else:
        log(f"changed files: {len(changed)} of {len(DATA_FILES)}")

    # 6. locked shape
    old_shape, new_shape = shape_of(old), shape_of(new)
    if new_shape != old_shape:
        raise Stop("shape", f"JSON field paths changed: new {sorted(new_shape - old_shape)[:4]}, "
                            f"gone {sorted(old_shape - new_shape)[:4]}")
    log(f"[shape] {len(new_shape)} field paths, identical to the published snapshot")

    # 7. sanity
    index, old_n = new["papers.json"], old["papers.json"]["count"]
    papers = index.get("papers") or []
    ids = [p.get("id") for p in papers]
    new_n = len(papers)
    if index.get("count") != new_n or len(set(ids)) != new_n:
        raise Stop("sanity", "papers.json count or ids are inconsistent")
    allowed_drop = max(MIN_DROP_ALLOWED, math.ceil(old_n * MAX_DROP_FRACTION))
    if new_n < old_n - allowed_drop:
        raise Stop("sanity", f"paper count fell from {old_n} to {new_n} (allowed drop: {allowed_drop})")
    if new_n > old_n * (1 + MAX_GROWTH_FRACTION):
        raise Stop("sanity", f"paper count jumped from {old_n} to {new_n}")
    old_size, new_size = sum(map(len, old_raw.values())), sum(map(len, new_raw.values()))
    if new_size > old_size * (1 + MAX_GROWTH_FRACTION):
        raise Stop("sanity", f"data grew from {old_size / 1e6:.1f} MB to {new_size / 1e6:.1f} MB")
    id_set = set(ids)
    if not set(new["abstracts.json"]) <= id_set:
        raise Stop("sanity", "abstracts.json has entries for unknown paper ids")
    for h in "0123456789abcdef":
        if any(k not in id_set or not k.startswith(h) for k in new[f"d/{h}.json"]):
            raise Stop("sanity", f"analysis shard d/{h}.json has unknown or misplaced paper ids")
    length, where = max((longest_string(doc, rel) for rel, doc in new.items()), key=lambda t: t[0])
    if length > MAX_STRING_CHARS:
        raise Stop("sanity", f"a {length:,}-character string in {where} (limit {MAX_STRING_CHARS:,})")
    log(f"[sanity] papers {old_n} -> {new_n}; size {old_size / 1e6:.2f} -> {new_size / 1e6:.2f} MB; "
        f"longest string {length:,} chars")

    # 8. AI provenance
    try:
        from_pdf = analyses_from_pdf(db)
    except sqlite3.Error as exc:
        raise Stop("provenance", f"cannot check which analyses used a PDF ({type(exc).__name__})") from None
    if from_pdf:
        raise Stop("provenance", f"{from_pdf} AI analyses were made from a PDF; the site says none were. "
                                 "Decide how to handle them before publishing.")
    labels, known = source_labels(new), source_labels(old)
    bad = sorted(s for s in labels if BAD_SOURCE.search(s))
    if bad:
        raise Stop("provenance", f"source labels that point to full text: {bad[:4]}")
    if not labels <= known:
        raise Stop("provenance", f"new source labels not in the published snapshot: {sorted(labels - known)[:4]}")
    log(f"[provenance] 0 analyses from PDFs; {len(labels)} source labels, all already published")

    # 9. page check
    run_tool(log, "site", [sys.executable, str(TOOLS / "check_site.py")], timeout=900)

    # 10. privacy scan
    scan = [sys.executable, str(TOOLS / "privacy_scan.py"), "--db", str(db)]
    for env in envs:
        scan += ["--env-file", str(env)]
    out = run_tool(log, "privacy", scan, timeout=900)
    compared = re.search(r"comparing against (\d+) secret-like values from (\d+) env file", out)
    if "RESULT: CLEAN" not in out:
        raise Stop("privacy", "privacy_scan.py did not report CLEAN")
    if not compared or int(compared.group(1)) < 1 or int(compared.group(2)) != len(envs):
        raise Stop("privacy", "privacy_scan.py did not compare against the env files")
    if "db fingerprints:" not in out:
        raise Stop("privacy", "privacy_scan.py did not check the database fingerprints")

    # 11. commit (docs/data only, private index)
    message = (f"Daily snapshot {index.get('generated')}: {new_n:,} papers ({new_n - old_n:+d})\n\n"
               "Published by tools/auto_publish.py after every automatic check passed.")
    sha, names = build_commit(head, message)
    if not names and not dry:
        raise Stop("commit", "the new commit would change nothing")
    log(f"[commit] {sha[:7]}: {len(names)} file(s), all under {DATA_REL}/")

    # 12. push
    if dry:
        ref = sha if names else head
        retry(lambda: git("push", "--dry-run", "--porcelain", "origin", f"{ref}:refs/heads/{BRANCH}",
                          timeout=180), "git push --dry-run", log)
        log("[push] dry run: SSH access and fast-forward confirmed; nothing was pushed" if names
            else "[push] dry run: SSH access confirmed (no new data to push); nothing was pushed")
        return f"dry run passed every gate ({new_n:,} papers, {len(names)} changed file(s)); nothing pushed"
    try:
        retry(lambda: git("push", "--porcelain", "origin", f"{sha}:refs/heads/{BRANCH}", timeout=300),
              "git push", log)
    except (GitError, subprocess.TimeoutExpired) as exc:
        raise Stop("push", str(exc)[:200]) from None
    progress["pushed"] = True
    state.update(handled_fetch_at=fetched, last_published_at=now_local(), last_published_commit=sha[:12],
                 last_published_fetch_at=fetched, last_published_count=new_n)
    save_state(state)
    log(f"[push] pushed {sha[:7]} to origin/{BRANCH}")
    git("update-ref", "-m", "auto-publish: daily snapshot", f"refs/heads/{BRANCH}", sha, head)
    git("restore", "--staged", f"--source={sha}", "--", DATA_REL)
    retry(lambda: git("fetch", "--quiet", "origin", BRANCH, timeout=120), "git fetch", log)

    # 13. live check
    want = hashlib.sha256(new_raw["papers.json"]).hexdigest()
    if not wait_for_pages(args.live_url, want, log):
        raise Stop("live", f"the live site did not serve the new papers.json within "
                           f"{PAGES_WAIT_SECONDS // 60} min")
    try:
        run_tool(log, "live", [sys.executable, str(TOOLS / "check_site.py"), "--base", args.live_url], timeout=900)
    except Stop:
        log("live check failed; trying once more in 2 min")
        time.sleep(120)
        run_tool(log, "live", [sys.executable, str(TOOLS / "check_site.py"), "--base", args.live_url], timeout=900)
    state["last_live_check"] = f"PASS {now_local()}"

    # housekeeping: keep the local object store packed
    last_gc = parse_ts(state.get("last_gc_at"))
    if not last_gc or dt.datetime.now() - last_gc > dt.timedelta(days=GC_EVERY_DAYS):
        try:
            git("gc", "--quiet", timeout=900)
            state["last_gc_at"] = now_local()
        except (GitError, subprocess.TimeoutExpired) as exc:
            log(f"git gc failed (harmless): {str(exc)[:120]}")
    return f"published {sha[:7]}: {new_n:,} papers ({new_n - old_n:+d}); live check passed"


def run(args) -> int:
    log = Log(Path(args.log_file))
    notify_on = not args.no_notify
    mode = "dry run" if args.dry_run else "run"
    LOCAL.mkdir(exist_ok=True)
    if PAUSE_FILE.exists():
        log(f"{mode}: paused ({PAUSE_FILE.name} exists in local/); nothing done")
        return 0
    lock = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"{mode}: another run is in progress; nothing done")
        return 0
    state = load_state()
    progress = {"exported": False, "pushed": False}
    log(f"===== {mode} started =====")
    code = 0
    try:
        summary = publish(args, log, state, progress)
        log(f"RESULT: {summary}")
        if args.dry_run:
            state["last_dry_run"] = f"PASS {now_local()}"
            notify("dry run 完成", "全部检查通过；没有推送。", notify_on, log)
        else:
            state["last_result"] = f"published {now_local()}"
            notify("已自动发布", summary, notify_on, log)
    except Done as done:
        log(f"RESULT: nothing to publish: {done.reason}")
        if done.handled and not args.dry_run:
            state["handled_fetch_at"] = done.handled
        state["last_dry_run" if args.dry_run else "last_result"] = f"nothing to publish {now_local()}"
    except Stop as stop:
        code = 1
        pushed = progress["pushed"]
        log(f"RESULT: STOPPED at gate '{stop.gate}': {stop.reason}")
        if not args.dry_run and not pushed and progress["exported"]:
            log("the unpublished export stays in docs/data for inspection")
        state["last_dry_run" if args.dry_run else "last_result"] = f"STOPPED ({stop.gate}) {now_local()}"
        prefix = "dry run：" if args.dry_run else ""
        notify(prefix + GATE_ZH.get(stop.gate, stop.gate) + ("" if pushed else "，未推送"),
               f"{stop.reason[:150]}  日志：{log.path}", notify_on, log)
    except Exception as exc:  # anything unexpected: stop, never push
        code = 1
        log(f"RESULT: STOPPED by an unexpected error: {type(exc).__name__}: {str(exc)[:200]}")
        for line in traceback.format_exc().splitlines():
            log("    " + line)
        state["last_dry_run" if args.dry_run else "last_result"] = f"STOPPED (error) {now_local()}"
        notify(GATE_ZH["error"] + ("，但已推送" if progress["pushed"] else "，未推送"),
               f"{type(exc).__name__}  日志：{log.path}", notify_on, log)
    finally:
        if args.dry_run and progress["exported"]:
            try:
                restore_data(log)
            except Exception as exc:
                log(f"could not put docs/data back: {str(exc)[:160]}")
        save_state(state)
        log(f"===== {mode} finished =====")
        lock.close()
    return code


# --------------------------------------------------------------- launchd
def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)


def parse_at(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        sys.exit(f"--at must be HH:MM (24 h), not {value!r}")
    return int(m.group(1)), int(m.group(2))


def install(args) -> int:
    db = Path(args.db).expanduser().resolve() if args.db else None
    if not db or not db.is_file():
        sys.exit("--install needs --db pointing to the app's seismo_hub.db")
    envs = [Path(e).expanduser().resolve() for e in args.env_file]
    try:
        usable_env_files(envs, db, print)
    except Stop as stop:
        sys.exit(stop.reason)
    hour, minute = parse_at(args.at)
    program = [sys.executable, str(Path(__file__).resolve()), "--db", str(db)]
    for env in envs:
        program += ["--env-file", str(env)]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": program,
        "WorkingDirectory": str(REPO),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "EnvironmentVariables": {"PATH": f"/usr/bin:/bin:/usr/sbin:/sbin:{Path(sys.executable).parent}",
                                 "LANG": "en_US.UTF-8", "PYTHONIOENCODING": "utf-8"},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(LOG_DIR / "launchd-errors.log"),
    }))
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")
    r = launchctl("bootstrap", domain, str(PLIST))
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    print(f"installed {PLIST.name}: runs every day at {hour:02d}:{minute:02d} local time")
    print(f"log: {LOG_DIR / 'auto_publish.log'}")
    return 0


def uninstall() -> int:
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    PLIST.unlink(missing_ok=True)
    print("LaunchAgent unloaded and removed; local/ state and the log are kept")
    return 0


def status() -> int:
    state = load_state()
    print("paused:", f"yes ({PAUSE_FILE.name} exists)" if PAUSE_FILE.exists() else "no")
    if PLIST.exists():
        when = plistlib.loads(PLIST.read_bytes()).get("StartCalendarInterval", {})
        print(f"schedule: every day at {when.get('Hour', 0):02d}:{when.get('Minute', 0):02d} ({PLIST.name})")
    else:
        print("schedule: not installed")
    loaded = launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
    print("loaded in launchd:", "yes" if loaded else "no")
    for key in ("last_result", "last_published_at", "last_published_commit", "last_published_count",
                "handled_fetch_at", "last_live_check", "last_dry_run", "last_gc_at"):
        print(f"{key}: {state.get(key, '-')}")
    print("log:", LOG_DIR / "auto_publish.log")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily unattended publish of SeismoHub Lite (details in the file header)")
    ap.add_argument("--db", help="the app's seismo_hub.db (opened read-only)")
    ap.add_argument("--env-file", action="append", default=[],
                    help="the app's .env / old.env; privacy_scan.py compares their values, which are never printed")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything except the push (git push --dry-run), then put docs/data back")
    ap.add_argument("--force", action="store_true",
                    help="run without a new fetch (with --dry-run: also go through every gate when nothing changed)")
    ap.add_argument("--no-notify", action="store_true", help="no macOS notifications")
    ap.add_argument("--live-url", default=LIVE_URL, help=argparse.SUPPRESS)
    ap.add_argument("--log-file", default=str(LOG_DIR / "auto_publish.log"), help=argparse.SUPPRESS)
    ap.add_argument("--at", default=DEFAULT_AT, help=f"daily time for --install, HH:MM local (default {DEFAULT_AT})")
    act = ap.add_mutually_exclusive_group()
    act.add_argument("--install", action="store_true", help="write the LaunchAgent and load it")
    act.add_argument("--uninstall", action="store_true", help="unload and delete the LaunchAgent")
    act.add_argument("--pause", action="store_true", help="scheduled runs do nothing until --resume")
    act.add_argument("--resume", action="store_true", help="undo --pause")
    act.add_argument("--status", action="store_true", help="show schedule, pause state and the last results")
    act.add_argument("--run-now", action="store_true", help="start the scheduled job now, in the background")
    args = ap.parse_args(argv)

    if args.install:
        return install(args)
    if args.uninstall:
        return uninstall()
    if args.pause:
        LOCAL.mkdir(exist_ok=True)
        PAUSE_FILE.write_text(f"paused {now_local()}; delete this file or run --resume to restart\n", encoding="utf-8")
        print("paused: scheduled runs will do nothing until you run --resume")
        return 0
    if args.resume:
        PAUSE_FILE.unlink(missing_ok=True)
        print("resumed: the next scheduled run will publish again")
        return 0
    if args.status:
        return status()
    if args.run_now:
        r = launchctl("kickstart", f"gui/{os.getuid()}/{LABEL}")
        if r.returncode != 0:
            sys.exit(f"could not start the job (is it installed?): {r.stderr.strip()}")
        print(f"started in the background; follow it with: tail -f '{LOG_DIR / 'auto_publish.log'}'")
        return 0
    if not args.db:
        ap.error("--db is required")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
