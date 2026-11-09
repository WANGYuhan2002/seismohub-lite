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
    dialog, without making any line taller.

Usage:
    python3 tools/check_site.py [--shots DIR] [--base https://…/seismohub-lite]

Needs Playwright (``pip install playwright``) and Google Chrome or Playwright's
own Chromium.
"""
from __future__ import annotations

import argparse
import collections
import functools
import http.server
import json
import re
import sys
import threading
import time
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
            check(page.locator('.facet[data-key="journal"] .fval').count() == 25, "journal facet shows top 25")
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

            # sort by relevance
            page.select_option("#sort", "rel")
            page.wait_for_timeout(300)
            top_rel = page.locator("article.card .rel b").first.inner_text()
            check(top_rel.strip() == "9", f"relevance sort puts a 9 first (got {top_rel})")
            page.select_option("#sort", "new")
            page.wait_for_timeout(300)

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
