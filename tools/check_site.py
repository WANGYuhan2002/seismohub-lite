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
    phone width;
  * the page degrades gracefully if abstracts.json cannot be loaded.

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
from urllib.parse import urlparse

DOCS = Path(__file__).resolve().parent.parent / "docs"
CJK = re.compile(r"[一-鿿]")


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
    total = json.loads((DOCS / "data" / "papers.json").read_text(encoding="utf-8"))["count"]
    rare = abstract_only_word()

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

            # ---------------------------------------------------------- dark mode
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark", locale="en-US")
            page = ctx.new_page()
            wire(page)
            page.goto(base + "/")
            page.wait_for_selector("article.card", timeout=20000)
            bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
            check(bg != "rgb(244, 246, 249)", f"dark mode applies ({bg})")
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
