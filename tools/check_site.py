#!/usr/bin/env python3
"""End-to-end check of the SeismoHub Lite page in headless Chrome.

By default it serves docs/ on a throw-away 127.0.0.1 server (random free port),
drives the page with Playwright, and shuts the server down when done, so
nothing is left running. With --base it tests a live URL instead, for example
right after a publish.

Checks:
  * the page loads with no console or page errors;
  * every request stays on the site's own host (no third-party calls);
  * abstracts load in the background: they show in cards, and a word that
    appears only in an abstract is still found by search;
  * search, facet filters, sorting and infinite scroll work;
  * the detail dialog works, with prev/next and arrow keys;
  * BibTeX/RIS export and "copy BibTeX" work;
  * the language switch, Chinese labels, deep links and back-to-top work;
  * the mobile filter drawer works, and there is no horizontal overflow at
    phone width (390 and 360 px, including the longest author and journal
    names);
  * the page degrades gracefully if abstracts.json cannot be loaded;
  * the card style the owner asked for on 2026-11-09 holds: titles larger
    than 19 px (17 px on phones), journal names in a light weight (<= 300)
    that stays readable in light and dark mode, a tag icon before every
    keyword row, and one person icon per author on the cards and in the
    dialog, without making any line taller;
  * the owner's 2026-11-10 choices hold: no relevance score or relevance
    sort, no follow-up section; one date filter (last 7 / 30 days counted
    back from the snapshot date, all, or custom years / months; old year= and
    month= links still work); the journal filter ordered by impact factor
    with preprints and open platforms last; fixed topic colours; and on wide
    screens a Browse directory in its own column whose rows jump to their
    paper, follow the scrolling, and can be hidden (remembered);
  * the owner's 2026-11-11 choices hold: the list can be sorted by journal
    impact factor (each journal kept together, in the journal filter's order,
    newest first inside), and a search sorts by best match (title hits
    first, then keywords or authors, then the abstract) unless another order
    was picked; the Browse directory shows each first author's full name,
    and its three column headings sort it A–Z, Z–A, or back to the list's
    order, without moving the cards; each card has the abstract & AI
    analysis button (sparkles + italic "AI") and its DOI at the top right,
    and the card title opens the paper's DOI (else its link) in a new tab.

Usage:
    python3 tools/check_site.py [--shots DIR] [--base https://…/seismohub-lite]

Needs Playwright (``pip install playwright``) and Google Chrome or Playwright's
own Chromium.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import functools
import http.server
import json
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

DOCS = Path(__file__).resolve().parent.parent / "docs"
CJK = re.compile(r"[一-鿿]")

# Card style (owner's request, 2026-11-09), measured on every rendered card.
# For each card: [paper id, title px, journal weight, keyword chips, keyword
# row ok, person icons, authors shown, svgs in the author line, author lines
# keep their height, author text as read aloud/copied].
CARD_LOOK_JS = """(() => {
  const cs = e => getComputedStyle(e);
  const isSvg = e => !!e && e.tagName.toLowerCase() === 'svg';
  return [...document.querySelectorAll('article.card')].map(c => {
    const op = c.querySelector('[data-open]');
    const jn = c.querySelector('.meta .jn');
    const row = c.querySelector('.kwrow');
    let rowOk = null;
    if (row) {
      const ic = row.firstElementChild; const chips = ic && ic.nextElementSibling; const first = row.querySelector('.chip.k');
      rowOk = row.querySelectorAll('svg').length === 1 && isSvg(ic) && !!chips && chips.classList.contains('chips') && !!first;
      if (rowOk) {
        const a = ic.getBoundingClientRect(); const b = first.getBoundingClientRect();
        rowOk = a.width > 0 && a.height > 0 && a.right <= b.left && a.top < b.bottom && a.bottom > b.top
          && Math.abs(row.getBoundingClientRect().height - chips.getBoundingClientRect().height) < 0.5;
      }
    }
    const box = c.querySelector('.authors');
    const aus = box ? [...box.querySelectorAll('.au')] : [];
    const lh = box ? parseFloat(cs(box).lineHeight) : 0;
    const flat = aus.every(a => { const r = a.getBoundingClientRect().height / lh; return Math.abs(r - Math.round(r)) < 0.05; });
    return [op ? op.getAttribute('data-open') : '', parseFloat(cs(c.querySelector('.title')).fontSize),
      jn ? parseInt(cs(jn).fontWeight, 10) : 999, c.querySelectorAll('.chip.k').length, rowOk,
      aus.filter(a => isSvg(a.firstElementChild) && a.querySelectorAll('svg').length === 1 && a.firstElementChild.getBoundingClientRect().width > 0).length,
      aus.length, box ? box.querySelectorAll('svg').length : 0, flat, box ? box.innerText : ''];
  });
})()"""

# Contrast ratio (WCAG) of the first card's journal name on the card, and its weight.
JOURNAL_CONTRAST_JS = """(() => {
  const rgb = s => (s.match(/[\\d.]+/g) || []).slice(0, 3).map(Number);
  const lum = c => { const v = c.map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
  const jn = document.querySelector('article.card .meta .jn');
  if (!jn) return [0, 999];
  const a = lum(rgb(getComputedStyle(jn).color)); const b = lum(rgb(getComputedStyle(jn.closest('article.card')).backgroundColor));
  return [Math.round((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) * 100) / 100, parseInt(getComputedStyle(jn).fontWeight, 10)];
})()"""

# The dialog's author line and keyword row: [person icons, authors shown, svgs
# in the author line, "show all" button present, keyword row ok, kicker journal weight].
DIALOG_LOOK_JS = """(() => {
  const b = document.querySelector('#detail .d-authors');
  const aus = b ? [...b.querySelectorAll('.au')] : [];
  const jn = document.querySelector('#dKicker .jn');
  const row = document.querySelector('#detail .kwrow');
  return [aus.filter(a => a.firstElementChild && a.firstElementChild.tagName.toLowerCase() === 'svg').length, aus.length,
    b ? b.querySelectorAll('svg').length : 0, !!(b && b.querySelector('button.linkish')),
    row ? (row.firstElementChild.tagName.toLowerCase() === 'svg' && row.querySelectorAll('svg').length === 1 && !!row.querySelector(':scope > .chips .chip.k')) : null,
    jn ? parseInt(getComputedStyle(jn).fontWeight, 10) : 999];
})()"""

def page_tables():
    """The journal impact factors, the preprint pattern and the fixed topic
    colours, read from the page itself so the checks follow its tables."""
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    jif = {norm_journal(name): float(v) for name, _ab, v in re.findall(r"'([^'\n]+)': \['([^'\n]*)', ([\d.]+)\]", html)}
    misc = re.compile(re.search(r"const MISC_VENUE = /(.+?)/i;", html).group(1), re.I)
    hues = {name: (float(h), float(k)) for name, h, k in re.findall(r"'([^'\n]+)': \[(\d+), ([\d.]+)\]", html)}
    return jif, misc, hues


def norm_journal(s: str) -> str:
    """Python twin of the page's normJ()."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[\s\-_.,:;/\\()\[\]{}'\"!?–—]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# The journal filter in page order: [value, count] per row, and the headings.
JOURNAL_ROWS_JS = """(() => {
  const box = document.querySelector('.facet[data-key="journal"] .fvals');
  return [[...box.querySelectorAll('.fval')].map(l => [l.querySelector('input').getAttribute('data-v'), l.querySelector('.n').textContent]),
          [...box.querySelectorAll('.fsub')].map(s => s.textContent)];
})()"""

# The directory: [display, rows, layout columns, highlighted row].
DIR_STATE_JS = """(() => {
  const cur = document.querySelector('#dirList .drow.cur');
  return [getComputedStyle(document.querySelector('#dir')).display, document.querySelectorAll('#dirList .drow').length,
    getComputedStyle(document.querySelector('.layout')).gridTemplateColumns.split(' ').length, cur ? cur.dataset.i : null];
})()"""

# Each card's top right and title link: [paper id, AI button in the head, its label is an
# italic "AI" after an icon, its accessible name, the buttons at the top right above the
# title, link button href, title href, title target, title rel, an old bottom actions row].
CARD_HEAD_JS = """(() => [...document.querySelectorAll('article.card')].map(c => {
  const ai = c.querySelector('.card-head .cacts button[data-open]');
  const i = ai && ai.querySelector('i');
  const cr = c.getBoundingClientRect(); const box = c.querySelector('.card-head .cacts'); const t = c.querySelector('.title');
  const r = box ? box.getBoundingClientRect() : null;
  const a = c.querySelector('.title a'); const ln = c.querySelector('.cacts a.btn');
  return [ai ? ai.getAttribute('data-open') : '', !!ai,
    !!(i && i.textContent === 'AI' && getComputedStyle(i).fontStyle === 'italic' && ai.firstElementChild.tagName.toLowerCase() === 'svg'),
    ai ? (ai.getAttribute('aria-label') || '') : '',
    !!(r && cr.right - r.right < 24 && r.bottom <= t.getBoundingClientRect().top),
    ln ? ln.getAttribute('href') : '', a ? a.getAttribute('href') : '', a ? a.getAttribute('target') : '', a ? (a.getAttribute('rel') || '') : '',
    !!c.querySelector('.actions')];
}))()"""

# Paper ids of the rendered cards, in order, and the sort menu's values.
CARD_IDS_JS = "[...document.querySelectorAll('article.card button[data-open]')].map(b => b.getAttribute('data-open'))"
SORT_OPTS_JS = "[...document.querySelectorAll('#sort option')].map(o => o.value)"

# The directory as shown: [list index, author, journal, date] per row.
DIR_ROWS_JS = "[...document.querySelectorAll('#dirList .drow')].map(r => [Number(r.dataset.i), ...[...r.children].map(s => s.textContent)])"

# Is the directory sorted by column col (1 author, 2 journal, 3 date) in direction dir,
# missing values last and ties in the list's order? Uses the page's collation.
# Returns [rows, rows out of order].
DIR_SORTED_JS = """([col, dir]) => {
  const rows = [...document.querySelectorAll('#dirList .drow')].map(r => [Number(r.dataset.i), r.children[col - 1].textContent]);
  const coll = new Intl.Collator('en', { sensitivity: 'base', numeric: true });
  const key = s => (s === '—' ? '' : col === 3 && s.length === 4 ? s + '-00-00' : s);
  const cmp = col === 3 ? (a, b) => (a < b ? -1 : a > b ? 1 : 0) : coll.compare;
  let bad = 0;
  for (let n = 1; n < rows.length; n++) {
    const a = key(rows[n - 1][1]); const b = key(rows[n][1]); const tie = rows[n - 1][0] < rows[n][0];
    let ok;
    if (!a || !b) ok = !a ? (!b && tie) : true;
    else { const c = cmp(a, b) * dir; ok = c < 0 || (c === 0 && tie); }
    if (!ok) bad++;
  }
  return [rows.length, bad];
}"""

# The directory's column headings: [key, aria-pressed, label cut off?] per heading.
DIR_HEADS_JS = """[...document.querySelectorAll('#dirCols .dsort')].map(b => [b.dataset.k, b.getAttribute('aria-pressed'),
  b.firstElementChild.scrollWidth > b.firstElementChild.clientWidth + 1])"""

# Horizontal overflow: the page, every rendered card, and the open dialog.
OVERFLOW_JS = """(() => {
  const over = e => (e ? e.scrollWidth - e.clientWidth : 0);
  const cards = [...document.querySelectorAll('article.card')];
  return [document.documentElement.scrollWidth - innerWidth, cards.length, cards.filter(c => over(c) > 0).length,
    over(document.querySelector('#detail[open] .dlg-bar')), over(document.querySelector('#detail[open] #dBody'))];
})()"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    requested: list = []

    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):
        QuietHandler.requested.append(self.path)
        super().do_GET()


def start_server():
    handler = functools.partial(QuietHandler, directory=str(DOCS))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def abstract_only_word() -> str:
    """A word that appears in a few abstracts but in no list field, so finding
    it proves that search covers the background-loaded abstracts."""
    index = json.loads((DOCS / "data" / "papers.json").read_text(encoding="utf-8"))["papers"]
    abstracts = json.loads((DOCS / "data" / "abstracts.json").read_text(encoding="utf-8"))
    listed = " ".join(" ".join([p["t"], " ".join(p["a"]), p["j"], " ".join(p["kw"]), p["doi"]]) for p in index).lower()
    seen = collections.Counter()
    for text in abstracts.values():
        seen.update(set(re.findall(r"\b[a-z]{11,}\b", text.lower())))
    for word, n in sorted(seen.items(), key=lambda kv: (kv[1], kv[0])):
        if 1 <= n <= 3 and word not in listed:
            return word
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None, help="folder for screenshots (optional)")
    ap.add_argument("--base", default=None, help="test this live URL instead of a local server")
    args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    if args.base:
        srv, base = None, args.base.rstrip("/")
    else:
        srv, base = start_server()
    host = urlparse(base).netloc
    failures: list[str] = []
    notes: list[str] = []
    external: list[str] = []
    errors: list[str] = []

    def check(cond: bool, msg: str):
        (notes if cond else failures).append(("ok   " if cond else "FAIL ") + msg)

    def wire(page):
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("request", lambda r: external.append(r.url) if urlparse(r.url).netloc not in (host, "") and not r.url.startswith(("data:", "blob:")) else None)

    def count_text(page):
        return page.locator("#count").inner_text()

    def wait_js(page, expr: str, timeout: float = 30.0):
        # The page's CSP forbids eval, which page.wait_for_function() needs;
        # page.evaluate() goes through the devtools protocol, so poll with it.
        end = time.time() + timeout
        while time.time() < end:
            if page.evaluate(expr):
                return
            page.wait_for_timeout(100)
        raise TimeoutError(f"timed out waiting for: {expr}")

    def wait_abstracts(page):
        wait_js(page, "!document.querySelector('[data-abs]') && !document.querySelector('#count .hint')")

    stray = re.compile(r"(^|\n)\s*(null|undefined|NaN|\[object Object\])\s*(\n|$)")
    index = json.loads((DOCS / "data" / "papers.json").read_text(encoding="utf-8"))
    total = index["count"]
    by_id = {p["id"]: p for p in index["papers"]}
    rare = abstract_only_word()
    squash = lambda s: " ".join(str(s).split())  # noqa: E731
    jif_table, misc_re, topic_hues = page_tables()
    # what the date filter should count: full dates, in days before the snapshot date
    generated = str(index.get("generated") or "")
    full_dates = [dt.date.fromisoformat(p["d"]) for p in index["papers"] if len(p["d"]) == 10]
    ref_day = dt.date.fromisoformat(generated) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", generated) else max(full_dates)
    n7 = sum(1 for d in full_dates if (ref_day - d).days < 7)
    n30 = sum(1 for d in full_dates if (ref_day - d).days < 30)
    months = collections.Counter(p["d"][:7] for p in index["papers"] if len(p["d"]) >= 7)
    journal_counts = collections.Counter(p["j"] or "Others" for p in index["papers"])
    abstracts = json.loads((DOCS / "data" / "abstracts.json").read_text(encoding="utf-8"))

    def dkey(p):
        d = p["d"] or ""
        return d + "-00-00" if len(d) == 4 else d

    def before_by_date(a, b) -> bool:
        """The page's tie order: newer date first, then the smaller id."""
        return dkey(a) > dkey(b) or (dkey(a) == dkey(b) and a["id"] < b["id"])

    by_newest = sorted(sorted(index["papers"], key=lambda p: p["id"]), key=dkey, reverse=True)  # stable: ids ascend within a date

    def fold(s) -> str:
        """Python twin of the page's fold(): no accents, lower case."""
        s = unicodedata.normalize("NFD", str(s or ""))
        return "".join(c for c in s if not unicodedata.category(c).startswith("M")).lower()

    def fold_hay(p) -> str:
        """Python twin of the page's search text: title, authors, abstract, journal, keywords, DOI."""
        ab = abstracts.get(p["id"])
        return fold("\n".join([p["t"] or "Untitled", " ".join(p["a"]), ab if isinstance(ab, str) else "",
                               p["j"] or "Others", " ".join(p["kw"]), p["doi"] or ""]))

    def whole_word(text: str, t: str) -> bool:
        i = text.find(t)
        while i >= 0:
            before = text[i - 1] if i > 0 else ""
            after = text[i + len(t)] if i + len(t) < len(text) else ""
            if not (before and unicodedata.category(before)[0] in "LN") and not (after and unicodedata.category(after)[0] in "LN"):
                return True
            i = text.find(t, i + 1)
        return False

    def match_score(p, toks) -> int:
        """Python twin of the page's best-match score."""
        title = fold(p["t"] or "Untitled")
        names = fold(" ".join(p["a"]) + "\n" + " ".join(p["kw"]))
        venue = fold((p["j"] or "Others") + "\n" + (p["doi"] or ""))
        s = 0
        for t in toks:
            if t in title:
                s += 10 if whole_word(title, t) else 8
            elif t in names:
                s += 4
            elif t in venue:
                s += 2
            else:
                s += 1
        return s

    def match_word() -> str:
        """A search word some titles have and other matching papers have only in their
        keywords, authors or abstract, so best match has something to reorder."""
        for w in ("tomography", "inversion", "earthquake", "seismic", "noise", "fault"):
            hits = [p for p in index["papers"] if w in fold_hay(p)]
            in_title = sum(1 for p in hits if w in fold(p["t"]))
            if in_title >= 3 and len(hits) - in_title >= 3:
                return w
        return ""

    def full_name(p) -> str:
        """The first author as the directory shows it: "Family, Given" turned round."""
        if not p["a"]:
            return "—"
        s = " ".join(str(p["a"][0]).split())
        m = re.fullmatch(r"([^,]+),\s*([^,]+)", s)
        return f"{m.group(2)} {m.group(1)}" if m else s

    def journal_key(name: str):
        """(group, impact factor) as the page orders the journal filter."""
        if misc_re.search(name):
            return (2, 0.0)
        if name == "Others":
            return (1, -1.0)
        v = jif_table.get(norm_journal(name), 0.0)
        return (0, v) if v > 0 else (1, 0.0)

    def check_journal_order(page, where: str, expect_all: bool):
        rows, heads = page.evaluate(JOURNAL_ROWS_JS)
        keys = [journal_key(v) for v, _ in rows]
        counts = [int(n.replace(",", "")) for _, n in rows]
        pairs = list(zip(keys, counts))
        ordered = all(k1[0] < k2[0] or (k1[0] == k2[0] and (k1[1] > k2[1] or (k1[1] == k2[1] and c1 >= c2)))
                      for (k1, c1), (k2, c2) in zip(pairs, pairs[1:]))
        groups = len({k[0] for k in keys if k[0] != 0})
        complete = len(rows) == len(journal_counts) if expect_all else bool(rows)
        check(complete and ordered and len(heads) == groups,
              f"{where}: journal filter is ordered by impact factor, preprints and open platforms last "
              f"({len(rows)} journals, {len(heads)} headings)")

    def check_if_order(ids, where: str, expect=None):
        """The impact-factor sort: each journal in one block, blocks in the journal
        filter's order (counted over all papers), newest first inside a block."""
        rank = lambda j: (journal_key(j)[0], -journal_key(j)[1], -journal_counts[j])  # noqa: E731
        bad, done = None, set()
        for x, y in zip(ids, ids[1:]):
            a, b = by_id[x], by_id[y]
            ja, jb = a["j"] or "Others", b["j"] or "Others"
            if ja == jb:
                if not before_by_date(a, b):
                    bad = f"dates out of order in {ja}"
                    break
                continue
            done.add(ja)
            if jb in done:
                bad = f"{jb} split in two"
                break
            if rank(ja) > rank(jb):
                bad = f"{ja} before {jb}"
                break
        same = expect is None or sorted(ids) == sorted(expect)
        check(bool(ids) and bad is None and same,
              f"{where}: impact-factor sort keeps each journal together, highest first, newest first inside "
              f"({len(ids)} papers, {len({by_id[i]['j'] for i in ids})} journals{'; ' + bad if bad else ''}{'' if same else '; wrong papers'})")

    def pick_sort(page, value: str) -> bool:
        """Choose a sort order; False, without Playwright's 30 s wait, when the menu lacks it."""
        if value not in page.evaluate(SORT_OPTS_JS):
            return False
        page.select_option("#sort", value)
        page.wait_for_timeout(300)
        return True

    def paper_href(p) -> str:
        """Where a card's title should go: the DOI (as the page's doiUrl builds it), else the paper's own link."""
        if p["doi"]:
            return "https://doi.org/" + "/".join(quote(part, safe="!*'()") for part in str(p["doi"]).split("/"))
        u = p["url"] or ""
        return u if re.fullmatch(r"https?://\S+", u, re.I) else ""

    def check_heads(page, where: str):
        """The owner's 2026-11-11 card points: AI button and DOI at the top right, title to the DOI."""
        rows = [r for r in page.evaluate(CARD_HEAD_JS) if r[0] in by_id]
        bad = []
        for r in rows:
            p = by_id[r[0]]
            want = paper_href(p)
            ok = r[1] and r[2] and bool(r[3]) and r[4] and not r[9]
            if want:
                ok = ok and r[5] == want and r[6] == want and r[7] == "_blank" and "noopener" in r[8]
            else:
                ok = ok and r[6] == "#p=" + p["id"]
            if not ok:
                bad.append(r[0])
        check(bool(rows) and not bad, f"{where}: AI button (sparkles + italic AI) and DOI at each card's top right, "
                                      f"title opens the DOI in a new tab ({len(rows)} cards, {len(bad)} wrong)")

    def check_cards(page, where: str, min_title: float):
        """The owner's four style points, on every card rendered so far."""
        rows = page.evaluate(CARD_LOOK_JS)
        if not rows:
            check(False, f"{where}: cards to measure")
            return
        known = [r for r in rows if r[0] in by_id]
        title_px = min(r[1] for r in rows)
        check(title_px > min_title, f"{where}: card titles are larger than {min_title:g}px ({title_px:g}px)")
        weight = max(r[2] for r in rows)
        check(weight <= 300, f"{where}: journal names use a light weight ({weight})")
        with_kw = [r for r in rows if r[3]]
        check(bool(with_kw) and all(r[4] is True for r in with_kw) and all(r[4] is None for r in rows if not r[3]),
              f"{where}: a tag icon sits before every keyword row ({sum(r[4] is True for r in with_kw)}/{len(with_kw)} cards)")
        wrong = [r[0] for r in known if not (r[5] == r[6] == r[7] == min(len(by_id[r[0]]["a"]), 8))]
        check(bool(known) and not wrong, f"{where}: one person icon per author on every card ({len(known)} cards, {len(wrong)} wrong)")
        check(all(r[8] for r in rows), f"{where}: the person icons make no author line taller")
        said = [r[0] for r in known if by_id[r[0]]["a"] and not squash(r[9]).startswith(squash(", ".join(by_id[r[0]]["a"][:8])))]
        check(not said, f"{where}: author lists still read and copy as 'A, B, C' ({len(said)} wrong)")
        check_heads(page, where)

    def check_dialog_authors(page, pid: str, where: str):
        dl = page.evaluate(DIALOG_LOOK_JS)
        p = by_id.get(pid)
        if p is not None:
            want = min(len(p["a"]), 40)
            check(dl[0] == dl[1] == dl[2] == want, f"{where}: one person icon per author ({dl[0]} icons, {want} authors shown)")
            if p.get("kw"):
                check(dl[4] is True, f"{where}: a tag icon sits before the keyword row")
        check(dl[5] <= 300, f"{where}: the journal name in the header uses a light weight ({dl[5]})")
        return dl

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = pw.chromium.launch(headless=True)

            # ---------------------------------------------------------- desktop
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US",
                                      accept_downloads=True, permissions=["clipboard-read", "clipboard-write"])
            page = ctx.new_page()
            wire(page)
            t0 = time.time()
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_selector("article.card", timeout=20000)
            notes.append(f"info first cards rendered after {time.time() - t0:.2f}s")
            wait_abstracts(page)
            notes.append(f"info abstracts ready after {time.time() - t0:.2f}s")
            check(f"{total:,} papers" in count_text(page), f"count shows {total:,} papers ({count_text(page)!r})")
            check(page.locator("article.card").count() == 40, "first batch renders 40 cards")
            check(page.locator('.facet[data-key="topic"] .fval').count() > 5, "topic facet lists values")
            check_journal_order(page, "desktop", True)
            check(not stray.search(page.locator("body").inner_text()), "list page shows no stray null/undefined")
            page.locator("article.card details.absd summary").first.click()
            abs_text = page.locator("article.card details.absd .abs").first.inner_text()
            check(len(abs_text) > 40, f"background-loaded abstract shows in the card ({len(abs_text)} chars)")
            check_cards(page, "desktop", 19)
            ratio, weight = page.evaluate(JOURNAL_CONTRAST_JS)
            check(ratio >= 4.5, f"light journal names stay readable (contrast {ratio}:1, weight {weight})")
            if shots:
                page.screenshot(path=str(shots / "desktop-list.png"))

            # search reaches the background-loaded abstracts
            if rare:
                page.fill("#q", rare)
                page.wait_for_timeout(600)
                ct = count_text(page)
                check(" of " in ct and page.locator("article.card").count() >= 1, f"abstract-only word {rare!r} is found ({ct!r})")
                check(page.locator("article.card details.absd[open] mark").count() >= 1, "matching abstract opens with the word highlighted")
            else:
                notes.append("info no abstract-only word found; skipped that check")

            # search
            page.fill("#q", "distributed acoustic sensing")
            page.wait_for_timeout(600)
            ct = count_text(page)
            check(" of " in ct, f"search narrows results ({ct!r})")
            check(page.locator("article.card mark").count() > 0, "search terms highlighted")
            check("q=distributed" in page.url, "search stored in URL")

            # facet checkbox
            first_journal = page.locator('.facet[data-key="journal"] .fval input').first
            jv = first_journal.get_attribute("data-v")
            first_journal.check()
            page.wait_for_timeout(300)
            check(page.locator(".achip").count() >= 2, f"active chips shown (journal {jv!r})")
            check("journal=" in page.url, "journal filter stored in URL")
            cards_j = page.locator("article.card .meta span").first.inner_text()
            check(cards_j.strip().lower() == (jv or "").lower(), f"filtered card journal matches ({cards_j!r})")

            # export the filtered list
            n_list = int(re.search(r"^([\d,]+)", count_text(page)).group(1).replace(",", ""))
            with page.expect_download() as dl:
                page.locator("#exBib").click()
            bib = Path(dl.value.path()).read_text(encoding="utf-8")
            n_bib = len(re.findall(r"^@(article|misc)\{", bib, re.M))
            check(n_bib == n_list and ("doi = {" in bib or "url = {" in bib), f"BibTeX export has one entry per paper ({n_bib}/{n_list})")
            with page.expect_download() as dl:
                page.locator("#exRis").click()
            risf = Path(dl.value.path()).read_text(encoding="utf-8")
            check(risf.count("TY  - ") == n_list == risf.count("ER  - "), f"RIS export has one record per paper ({risf.count('TY  - ')}/{n_list})")

            # clear all
            page.locator("#active .linkish").click()
            page.wait_for_timeout(300)
            check(f"{total:,} papers" == count_text(page).strip(), "clear all restores the full list")

            # no relevance score anywhere, and no relevance sort (an old sort=rel link falls back to newest)
            sorts = page.evaluate("[...document.querySelectorAll('#sort option')].map(o => o.value)")
            check(page.locator(".rel").count() == 0 and "rel" not in sorts, f"no relevance score or relevance sort ({sorts})")

            # sort orders (owner, 2026-11-11): newest, oldest, journal impact factor, title;
            # best match only while searching, and a new search starts on it
            opts = page.evaluate(SORT_OPTS_JS)
            check(opts == ["new", "old", "if", "title"], f"sort menu without a search: newest, oldest, impact factor, title ({opts})")
            if pick_sort(page, "if"):
                check("sort=if" in page.url, "impact-factor sort stored in URL")
                check_if_order(page.evaluate(CARD_IDS_JS), "all papers, first batch")
                page.goto(base + "/#win=30&sort=if")
                page.wait_for_timeout(500)
                for _ in range(80):
                    if page.locator("#more").is_hidden():
                        break
                    page.locator("#more").click()
                    page.wait_for_timeout(60)
                win30 = [p["id"] for p in index["papers"] if len(p["d"]) == 10 and (ref_day - dt.date.fromisoformat(p["d"])).days < 30]
                check_if_order(page.evaluate(CARD_IDS_JS), "last 30 days, every card", win30)
            page.goto(base + "/")
            page.wait_for_selector("article.card", timeout=20000)
            wait_abstracts(page)
            mw = match_word()
            if mw:
                page.fill("#q", mw)
                page.wait_for_timeout(600)
                wait_abstracts(page)
                opts = page.evaluate(SORT_OPTS_JS)
                check(page.locator("#sort").input_value() == "match" and opts == ["match", "new", "old", "if", "title"] and "sort=" not in page.url,
                      f"a new search switches to best match, offered first ({opts})")
                hits = [p for p in index["papers"] if mw in fold_hay(p)]
                n_title = sum(1 for p in hits if mw in fold(p["t"]))
                ids = page.evaluate(CARD_IDS_JS)
                sc = [match_score(by_id[i], [mw]) for i in ids]
                ordered = all(s1 > s2 or (s1 == s2 and before_by_date(by_id[a], by_id[b]))
                              for a, b, s1, s2 in zip(ids, ids[1:], sc, sc[1:]))
                lead = sum(1 for i in ids[:n_title] if mw in fold(by_id[i]["t"]))
                ct = count_text(page)
                check(ordered and lead == min(n_title, len(ids)) and ct.startswith(f"{len(hits):,} of "),
                      f"best match for {mw!r}: the {n_title} title hits come first, then keywords or authors, then abstracts ({ct!r})")
                pick_sort(page, "new")
                ids = page.evaluate(CARD_IDS_JS)
                check("sort=new" in page.url and all(before_by_date(by_id[a], by_id[b]) for a, b in zip(ids, ids[1:])),
                      "newest can still be picked while searching")
                page.fill("#q", "")
                page.wait_for_timeout(500)
                check(page.locator("#sort").input_value() == "new" and "match" not in page.evaluate(SORT_OPTS_JS),
                      "clearing the search takes best match away")
                kept = None
                if pick_sort(page, "if"):
                    page.fill("#q", mw)
                    page.wait_for_timeout(600)
                    kept = page.locator("#sort").input_value()
                page.fill("#q", "")
                page.wait_for_timeout(500)
                check(kept == "if" and page.locator("#sort").input_value() == "if", f"an order picked before a search stays ({kept})")
                got = []
                for frag in (urlencode({"q": mw}), urlencode({"q": mw, "sort": "new"}), "sort=match"):
                    page.goto(base + "/#" + frag)
                    page.wait_for_timeout(400)
                    got.append(page.locator("#sort").input_value())
                check(got == ["match", "new", "new"], f"links: q= opens on best match, sort=new keeps newest, best match needs a search ({got})")
            else:
                notes.append("info no word for the best-match check; skipped it")
            page.goto(base + "/")
            page.wait_for_selector("article.card", timeout=20000)
            check(count_text(page).strip() == f"{total:,} papers" and page.locator("#sort").input_value() == "new", "back to the full list, newest first")
            # papers without a DOI: the title and the top-right button go to the paper's own link
            no_doi = collections.Counter(p["j"] or "Others" for p in index["papers"] if not p["doi"])
            if no_doi:
                page.goto(base + "/#" + urlencode({"journal": no_doi.most_common(1)[0][0]}, quote_via=quote))
                page.wait_for_timeout(500)
                check_heads(page, f"{no_doi.most_common(1)[0][0]} (no DOI on {no_doi.most_common(1)[0][1]} papers)")
                page.goto(base + "/")
                page.wait_for_selector("article.card", timeout=20000)

            # date filter: last 7 / 30 days counted back from the snapshot date, all, or custom months
            wins = page.evaluate("[...document.querySelectorAll('.facet[data-key=\"date\"] .fwin .fval .n')].map(n => n.textContent)")
            check(wins == [f"{n7:,}", f"{n30:,}", f"{total:,}"], f"date filter offers last 7 days, last 30 days and all ({wins})")
            check(page.locator('.facet[data-key="year"], .facet[data-key="month"]').count() == 0, "year and month are merged into the date filter")
            page.locator('.facet[data-key="date"] input[name=win][value="7"]').check()
            page.wait_for_timeout(300)
            ct = count_text(page)
            check(ct.startswith(f"{n7:,} of ") and "win=7" in page.url and page.locator("#active .achip").count() == 1,
                  f"'last 7 days' keeps the papers of the last week ({ct!r})")
            newest = max(months)
            page.locator('.facet[data-key="date"] .dcustom summary').click()
            page.locator(f'.facet[data-key="date"] .dcustom input[data-v="{newest}"]').check()
            page.wait_for_timeout(300)
            ct = count_text(page)
            check(ct.startswith(f"{months[newest]:,} of ") and f"date={newest}" in page.url and "win=" not in page.url
                  and not page.locator('.facet[data-key="date"] input[name=win][value="7"]').is_checked(),
                  f"a custom month replaces the window ({ct!r})")
            page.locator('.facet[data-key="date"] input[name=win][value=""]').check()
            page.wait_for_timeout(300)
            check(count_text(page).strip() == f"{total:,} papers", "'all dates' restores the full list")
            # links from before the merge (month=, year=) and from before relevance was dropped (sort=rel)
            page.goto(base + f"/#month={newest}&sort=rel")
            page.wait_for_timeout(500)
            ct = count_text(page)
            check(ct.startswith(f"{months[newest]:,} of ") and page.locator("#sort").input_value() == "new",
                  f"old month= and sort=rel links still open ({ct!r})")
            page.goto(base + "/")
            page.wait_for_selector("article.card", timeout=20000)

            # fixed topic colours: every topic chip has its own topic's hue, not its card's
            chips = page.evaluate("""[...document.querySelectorAll('.chip.t:not(.gray)')].map(c => [c.getAttribute('data-value'),
              getComputedStyle(c).getPropertyValue('--h').trim(), getComputedStyle(c).getPropertyValue('--k').trim()])""")
            wrong = [c for c in chips if c[0] in topic_hues and (float(c[1] or -1), float(c[2] or -1)) != topic_hues[c[0]]]
            check(bool(chips) and not wrong, f"topic colours are fixed per topic ({len(chips)} chips, {len(wrong)} wrong)")

            # Browse directory (wide screens): its own column, one row per paper
            page.wait_for_selector("#dirList .drow", timeout=20000)
            d = page.evaluate(DIR_STATE_JS)
            check(d[0] == "flex" and d[1] == total and d[2] == 3, f"Browse directory has its own column with one row per paper ({d})")
            page.locator('#dirList .drow[data-i="120"]').click()
            page.wait_for_timeout(400)
            jump = page.evaluate("""(() => { const c = document.querySelector('article.card[data-i="120"]');
              return c ? Math.round(c.getBoundingClientRect().top - parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--top-h'))) : null; })()""")
            d = page.evaluate(DIR_STATE_JS)
            check(jump is not None and 0 <= jump < 40 and d[3] == "120", f"a directory row jumps to its paper, just below the header ({jump}px, row {d[3]})")
            over = page.evaluate("""(() => { const t = document.querySelector('#toTop'); const a = t.getBoundingClientRect(); const b = document.querySelector('#dir').getBoundingClientRect();
              return [t.classList.contains('show'), a.right > b.left && a.left < b.right && a.bottom > b.top && a.top < b.bottom]; })()""")
            check(over == [True, False], f"the back-to-top button stays off the directory ({over})")
            page.evaluate("document.querySelector('article.card[data-i=\"5\"]').scrollIntoView({block: 'start'})")
            page.wait_for_timeout(500)
            d = page.evaluate(DIR_STATE_JS)
            check(d[3] == "5", f"the highlighted row follows the scrolling (row {d[3]})")
            page.locator("#dirBtn").click()
            page.wait_for_timeout(200)
            d = page.evaluate(DIR_STATE_JS)
            check(d[0] == "none" and d[2] == 2 and page.evaluate("localStorage.getItem('seismohub-lite:dir')") == "off",
                  f"the directory can be hidden, and the choice is remembered ({d})")
            page.reload()
            page.wait_for_selector("article.card", timeout=20000)
            check(page.evaluate(DIR_STATE_JS)[0] == "none", "it stays hidden after a reload")
            page.locator("#dirBtn").click()
            page.wait_for_timeout(300)
            d = page.evaluate(DIR_STATE_JS)
            check(d[0] == "flex" and d[1] == total, f"the Browse button shows it again ({d})")
            # first authors' full names (owner, 2026-11-11), rows in the list's order
            rows = page.evaluate(DIR_ROWS_JS)
            want = [full_name(p) for p in by_newest]
            wrong = sum(1 for r, w in zip(rows, want) if r[1] != w)
            check(len(rows) == len(want) and not wrong and all(r[0] == n for n, r in enumerate(rows)),
                  f"directory rows follow the list and show each first author's full name ({len(rows)} rows, {wrong} wrong)")
            heads = page.evaluate(DIR_HEADS_JS)
            check([x[0] for x in heads] == ["a", "j", "d"] and not any(x[2] for x in heads), f"three column headings, none cut off ({heads})")
            # each heading sorts the directory A–Z, then Z–A, then back to the list's order; the cards stay put
            first_cards = page.evaluate(CARD_IDS_JS)[:5]
            for col, k in (((1, "a"), (2, "j"), (3, "d")) if page.locator("#dirCols .dsort").count() == 3 else ()):
                head = page.locator(f'#dirCols .dsort[data-k="{k}"]')
                head.click()
                page.wait_for_timeout(250)
                up = page.evaluate(DIR_SORTED_JS, [col, 1])
                pressed = [x[1] for x in page.evaluate(DIR_HEADS_JS)]
                head.click()
                page.wait_for_timeout(250)
                down = page.evaluate(DIR_SORTED_JS, [col, -1])
                head.click()
                page.wait_for_timeout(250)
                back = page.evaluate(DIR_ROWS_JS)
                check(up == [total, 0] and down == [total, 0] and pressed == [str(x == k).lower() for x in ("a", "j", "d")]
                      and all(r[0] == n for n, r in enumerate(back)),
                      f"directory heading {k!r}: A–Z, then Z–A, then the list's order again (out of order: {up[1]}, {down[1]})")
            if page.locator("#dirCols .dsort").count() == 3:
                check(page.evaluate(CARD_IDS_JS)[:5] == first_cards, "sorting the directory leaves the cards where they are")
                page.locator('#dirCols .dsort[data-k="a"]').click()
                page.wait_for_timeout(250)
                row = page.locator("#dirList .drow").nth(3)
                ri, rtitle = row.get_attribute("data-i"), (row.get_attribute("title") or "").split("\n")[0]
                row.click()
                page.wait_for_timeout(400)
                landed = page.evaluate(f"""(() => {{ const c = document.querySelector('article.card[data-i="{ri}"]');
                  return c ? [Math.round(c.getBoundingClientRect().top - parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--top-h'))),
                              c.querySelector('.title').textContent] : null; }})()""")
                check(landed is not None and 0 <= landed[0] < 40 and squash(landed[1]) == squash(rtitle),
                      f"a row of the sorted directory jumps to its own paper (row {ri}, {landed and landed[0]}px)")
                page.locator('#dirCols .dsort[data-k="a"]').click()
                page.locator('#dirCols .dsort[data-k="a"]').click()
                page.wait_for_timeout(250)
            page.set_viewport_size({"width": 1440, "height": 900})
            page.wait_for_timeout(300)
            cols = page.evaluate("getComputedStyle(document.querySelector('.layout')).gridTemplateColumns")
            check(cols.split()[-1] == "340px", f"at 1440px the directory is wider, for full names ({cols})")
            page.set_viewport_size({"width": 1024, "height": 800})
            page.wait_for_timeout(200)
            narrow = page.evaluate("[getComputedStyle(document.querySelector('#dir')).display, getComputedStyle(document.querySelector('#dirBtn')).display]")
            check(narrow == ["none", "none"], f"below 1200px the directory and its button are hidden ({narrow})")
            page.set_viewport_size({"width": 1280, "height": 900})
            page.goto(base + "/")
            page.wait_for_selector("article.card", timeout=20000)
            page.mouse.move(640, 600)  # over the cards, so the wheel scrolls the page and not a sidebar

            # infinite scroll + back to top
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(800)
            check(page.locator("article.card").count() > 40, f"scrolling loads more ({page.locator('article.card').count()} cards)")
            check(page.locator("#toTop.show").count() == 1, "back-to-top button appears after scrolling")
            page.locator("#toTop").click()
            try:
                wait_js(page, "window.scrollY < 50", 5)  # smooth scroll from ~15k px takes ~1.5 s
                at_top = True
            except TimeoutError:
                at_top = False
            check(at_top, "back-to-top returns to the top")

            # detail dialog
            page.locator("article.card button[data-open]").first.click()
            page.wait_for_selector("#detail[open] .sec.ai", timeout=15000)
            check("p=" in page.url, "dialog id stored in URL")
            n_ai = page.locator("#detail .sec.ai").count()
            check(n_ai >= 4, f"dialog shows AI sections ({n_ai})")
            check(page.locator("#detail .sec").first.locator("h3").inner_text().strip().lower() == "abstract", "abstract section first")
            check(page.locator("#detail .notice.cr").count() == 1, "abstract copyright note shown")
            check(page.locator("#detail .rel").count() == 0 and page.locator("#detail", has_text="follow-up ideas").count() == 0,
                  "the dialog has no relevance score and no follow-up section")
            check(not stray.search(page.locator("#detail").inner_text()), "dialog shows no stray null/undefined")
            # Safari/WebKit collapsed the dialog to a 105 px strip while its body had
            # "flex:1" (basis 0%). Chrome never showed it, so check the cause too.
            dlg_h, vh, basis = page.evaluate(
                "[document.querySelector('#detail').getBoundingClientRect().height, innerHeight,"
                " getComputedStyle(document.querySelector('#dBody')).flexBasis]")
            check(dlg_h >= 0.8 * vh, f"dialog fills the window height ({dlg_h:.0f} of {vh}px)")
            check(basis == "auto", f"dialog body flex-basis is auto, not 0% ({basis})")
            m = re.search(r"[#&]p=([0-9a-f]{16})", page.url)
            check_dialog_authors(page, m.group(1) if m else "", "dialog")

            # prev / next
            pos1 = page.locator("#dPos").inner_text().strip()
            title1 = page.locator("#dTitle").inner_text()
            check(pos1 == f"1 / {total:,}" and page.locator("#dPrev").is_disabled(), f"dialog shows its position ({pos1!r}), prev disabled at the start")
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(300)
            pos2 = page.locator("#dPos").inner_text().strip()
            check(pos2 == f"2 / {total:,}" and page.locator("#dTitle").inner_text() != title1, f"ArrowRight opens the next paper ({pos2!r})")
            page.locator("#dPrev").click()
            page.wait_for_timeout(300)
            check(page.locator("#dTitle").inner_text() == title1, "prev button goes back")

            # copy BibTeX
            page.locator("#detail .d-actions button", has_text="BibTeX").click()
            page.wait_for_timeout(300)
            clip = page.evaluate("navigator.clipboard.readText()")
            check(clip.startswith("@") and "title = {{" in clip, "copy BibTeX puts an entry on the clipboard")

            # language switch while the dialog is open
            page.evaluate("document.getElementById('langBtn').click()")
            page.wait_for_timeout(500)
            check(page.locator("#detail", has_text="一句话摘要").count() == 1, "language switch re-renders dialog in Chinese")
            check(bool(CJK.search(page.locator("#detail .chip.t").first.inner_text())), "topic chips show Chinese labels")
            check("lang=zh" in page.url, "language stored in URL")
            if shots:
                page.screenshot(path=str(shots / "desktop-detail-zh.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            check(page.locator("#detail[open]").count() == 0, "Escape closes the dialog")
            check("p=" not in page.url, "closing removes the paper from the URL")
            page.evaluate("document.getElementById('langBtn').click()")
            page.wait_for_timeout(300)

            # deep link
            pid = page.locator("article.card button[data-open]").nth(3).get_attribute("data-open")
            page.goto(base + "/#p=" + pid)
            page.wait_for_selector("#detail[open] .sec.ai", timeout=15000)
            check(page.locator("#detail[open]").count() == 1, "deep link opens the paper dialog")
            if shots:
                page.screenshot(path=str(shots / "desktop-detail.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

            # the paper with the most authors: 40 with icons, then "show all" adds the rest
            many = max(index["papers"], key=lambda p: len(p["a"]))
            if len(many["a"]) > 40:
                page.goto(base + "/#p=" + many["id"])
                page.wait_for_selector("#detail[open] .d-authors", timeout=15000)
                dl = check_dialog_authors(page, many["id"], "dialog with many authors")
                check(dl[3], "dialog offers 'show all' when there are more than 40 authors")
                if dl[3]:
                    page.locator("#detail .d-authors button.linkish").click()
                    page.wait_for_timeout(300)
                    dl = page.evaluate(DIALOG_LOOK_JS)
                    n = len(many["a"])
                    check(dl[0] == dl[1] == dl[2] == n and not dl[3], f"'show all' lists all {n} authors, one icon each ({dl[0]})")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)

            # chip inside the dialog applies a filter and closes it
            page.locator("article.card button[data-open]").first.click()
            page.wait_for_selector("#detail[open] .sec.ai", timeout=15000)
            page.locator("#detail .chip.t").first.click()
            page.wait_for_timeout(400)
            check(page.locator("#detail[open]").count() == 0 and "topic=" in page.url, "topic chip in dialog filters the list")

            # about dialog
            page.locator("#aboutBtn").click()
            check(page.locator("#about[open]", has_text="Copyright and takedown").count() == 1, "about dialog shows copyright notice")
            page.keyboard.press("Escape")

            # abstracts.json unavailable: the list still works and says so
            p2 = ctx.new_page()
            p2.route("**/data/abstracts.json", lambda route: route.abort())
            p2.goto(base + "/")
            p2.wait_for_selector("article.card", timeout=20000)
            wait_js(p2, "!document.querySelector('[data-abs]')", 15)
            p2.locator("article.card details.absd summary").first.click()
            msg = p2.locator("article.card details.absd .abs").first.inner_text().strip()
            check(msg == "Could not load the abstract.", f"missing abstracts degrade gracefully ({msg!r})")
            p2.close()
            ctx.close()

            # ---------------------------------------------------------- mobile
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                      is_mobile=True, has_touch=True, locale="zh-CN")
            page = ctx.new_page()
            wire(page)
            page.goto(base + "/")
            page.wait_for_selector("article.card", timeout=20000)
            check(page.locator("#langBtn").inner_text().strip() == "EN", "zh-CN browser starts in Chinese")
            check(bool(CJK.search(page.locator("article.card .chip.t").first.inner_text())), "cards show Chinese topic labels")
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            check(overflow <= 0, f"no horizontal overflow at 390px (overflow {overflow}px)")
            check_cards(page, "phone 390px", 17)
            check(page.evaluate(DIR_STATE_JS)[0] == "none", "phones get no Browse directory")
            check_journal_order(page, "phone", True)
            btn = page.locator("#filtersBtn").inner_text()
            check("null" not in btn and "undefined" not in btn, f"filter button label is clean ({btn.strip()!r})")
            if shots:
                page.screenshot(path=str(shots / "mobile-list.png"))
            page.locator("#filtersBtn").tap()
            page.wait_for_timeout(350)
            check(page.locator("#filters.open").count() == 1, "filter drawer opens")
            page.locator('.facet[data-key="topic"] .fval').nth(1).tap()
            page.wait_for_timeout(300)
            if shots:
                page.screenshot(path=str(shots / "mobile-filters.png"))
            page.locator("#showResults").tap()
            page.wait_for_timeout(350)
            check(page.locator("#filters.open").count() == 0 and "topic=" in page.url, "drawer applies filter and closes")
            page.locator("article.card button[data-open]").first.tap()
            page.wait_for_selector("#detail[open] .sec.ai", timeout=15000)
            box = page.locator("#detail").bounding_box()
            check(box and box["width"] >= 389, f"dialog is full width on phones ({box and box['width']})")
            bar_overflow = page.evaluate("(() => { const b = document.querySelector('#detail .dlg-bar'); return b.scrollWidth - b.clientWidth; })()")
            check(bar_overflow <= 0, f"dialog toolbar fits on a phone (overflow {bar_overflow}px)")
            if shots:
                page.screenshot(path=str(shots / "mobile-detail.png"))
            ctx.close()

            # ---------------------------------------------------------- small phone (360 px)
            # The list, the longest author name and the longest journal name,
            # each with its dialog open: nothing may stick out sideways.
            ctx = browser.new_context(viewport={"width": 360, "height": 780}, device_scale_factor=2,
                                      is_mobile=True, has_touch=True, locale="zh-CN")
            page = ctx.new_page()
            wire(page)
            page.goto(base + "/")
            page.wait_for_selector("article.card", timeout=20000)
            wait_abstracts(page)
            check_cards(page, "phone 360px", 17)
            longest_author = max((a for p in index["papers"] for a in p["a"]), key=len, default="")
            longest_journal = max((p["j"] for p in index["papers"]), key=len, default="")
            cases = [("list", "")]
            if longest_author:
                cases.append((f"longest author ({len(longest_author)} chars)", "#" + urlencode({"q": f'"{longest_author}"'}, quote_via=quote)))
            if longest_journal:
                cases.append((f"longest journal ({len(longest_journal)} chars)", "#" + urlencode({"journal": longest_journal}, quote_via=quote)))
            for label, frag in cases:
                if frag:
                    page.goto(base + "/" + frag)
                    page.wait_for_timeout(500)
                ov = page.evaluate(OVERFLOW_JS)
                check(ov[1] >= 1 and ov[0] <= 0 and ov[2] == 0,
                      f"360px, {label}: nothing overflows sideways (page {ov[0]}px, {ov[2]} of {ov[1]} cards)")
                page.locator("article.card button[data-open]").first.tap()
                page.wait_for_selector("#detail[open] .sec.ai, #detail[open] .aiwrap .muted", timeout=15000)
                ov = page.evaluate(OVERFLOW_JS)
                check(ov[3] <= 0 and ov[4] <= 0, f"360px, {label}: the dialog does not overflow (toolbar {ov[3]}px, body {ov[4]}px)")
                page.locator("#dClose").tap()
                page.wait_for_timeout(300)
            # the sort menu (widest while searching) leaves the search box room, in both languages
            page.goto(base + "/#" + urlencode({"q": "seismic"}))
            page.wait_for_timeout(400)
            widths = []
            for _ in range(2):
                widths.append(page.evaluate("Math.round(document.querySelector('.search').getBoundingClientRect().width)"))
                page.locator("#langBtn").tap()
                page.wait_for_timeout(300)
            check(min(widths) >= 100, f"360px: the search box keeps its room next to the sort menu while searching ({widths} px, zh then en)")
            ctx.close()

            # ---------------------------------------------------------- dark mode
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark", locale="en-US")
            page = ctx.new_page()
            wire(page)
            page.goto(base + "/")
            page.wait_for_selector("article.card", timeout=20000)
            bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
            check(bg != "rgb(244, 246, 249)", f"dark mode applies ({bg})")
            ratio, weight = page.evaluate(JOURNAL_CONTRAST_JS)
            check(ratio >= 4.5 and weight <= 300, f"dark mode: light journal names stay readable (contrast {ratio}:1, weight {weight})")
            if shots:
                page.screenshot(path=str(shots / "desktop-dark.png"))
            ctx.close()
            browser.close()
    finally:
        if srv:
            srv.shutdown()

    check(not external, f"no third-party requests ({len(external)} found)")
    check(not errors, f"no console/page errors ({len(errors)} found)")
    for line in notes + failures:
        print(line)
    for e in errors[:10]:
        print("   ", e)
    for u in external[:10]:
        print("    external:", u)
    if srv:
        served = sorted(set(p.split("?")[0].split("#")[0] for p in QuietHandler.requested))
        print("files requested:", ", ".join(served))
    print("RESULT:", "PASS" if not failures else f"{len(failures)} FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
