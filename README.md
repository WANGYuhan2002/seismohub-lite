# SeismoHub Lite

A static, read-only snapshot of SeismoHub, a personal literature tracker for
seismology and wave physics.

**Live site:** https://wangyuhan2002.github.io/seismohub-lite/

Each paper shows its title, authors, journal, date, abstract and DOI or link,
plus the AI analyses that were generated offline: takeaway, summary,
structured notes, topic labels, keywords and facets.

What you can do on the page:

- Search, sort, and filter by date, topic, journal, keyword, institution,
  method, data type, target and paper type.
  - The date filter offers the last 7 or 30 days, counted back from the
    snapshot date, or any months you pick.
  - The journal list puts the highest impact factor first. Preprint
    servers and open platforms come last.
  - Sort by newest, oldest, journal impact factor or title. The impact
    factor sort keeps each journal's papers together, in the journal list's
    order, newest first. The impact factors are approximate JCR values; they
    set the order and are not shown.
  - A search sorts by best match: papers with the search terms in the title
    come first, then those with them in the keywords or authors, then those
    with them only in the abstract. Newest is still one click away.
- Click a paper's title to open it through its DOI (or its own link) in a
  new tab. The AI button at the top right of each paper opens its abstract
  and AI analysis.
- On wide screens (1200 px and up), use the Browse directory on the right.
  It lists the current papers by first author (full name), journal and date.
  Click a row to jump to that paper. Click a column heading to sort the
  directory A–Z, then Z–A, then back to the list's order; the papers in the
  middle keep their order. The Browse button hides or shows it.
- Step through papers with ‹ / › or the arrow keys.
- Copy a BibTeX entry, or export the current list as BibTeX or RIS.
- The page opens in English. The 中文 button switches to Chinese, and the
  browser remembers that choice.

The page works on phones.

What the site does **not** have:

- No server, database, API keys or AI calls at runtime. Visitors can only read
  and follow links, so nothing they do can use up a paid quota.
- No PDFs, no AI chat, and no personal reading data (read or liked state,
  notes, projects).
- No cookies, analytics or third-party scripts, fonts or trackers.

## Layout

| Path | Content |
| --- | --- |
| `docs/index.html` | The whole site: HTML, CSS and vanilla JS in one file |
| `docs/data/papers.json` | Paper list: metadata, AI topics, keywords and takeaways. Loaded first. |
| `docs/data/abstracts.json` | Abstracts, loaded in the background after the list renders |
| `docs/data/d/<0-f>.json` | Per-paper AI analysis, split into 16 shards and loaded when a paper is opened |
| `tools/export_lite.py` | Read-only exporter from the SeismoHub database; whitelisted fields only |
| `tools/check_site.py` | End-to-end check in headless Chrome. `--base <url>` tests the live site. |
| `tools/privacy_scan.py` | Privacy scan to run before every publish |
| `tools/update.py` | Runs export, check and scan in one go, then shows the diff. Never pushes. |
| `tools/auto_publish.py` | Daily automatic publish, run by a LaunchAgent. Pushes only if every check passes. |
| `tools/abstract_blocklist.txt` | Abstracts to withhold, for takedown requests |
| `tools/privacy_allowlist.txt` | Strings the scan may let through after human review, each with a reason |

GitHub Pages serves the `docs/` folder of the `main` branch.

## Updating the snapshot

```bash
python3 tools/update.py --db <path-to>/seismo_hub.db --env-file <path-to>/.env
git add -A
git diff --cached --stat
git commit -m "Update snapshot"
git push
python3 tools/check_site.py --base https://wangyuhan2002.github.io/seismohub-lite
```

`update.py` runs `export_lite.py`, `check_site.py` and `privacy_scan.py` in
that order. It stops at the first failure. Once Pages has rebuilt, about a
minute after the push, the last command checks the live site.

The exporter opens the database read-only (`mode=ro` and `query_only`). It
reads only whitelisted columns. It never exports the following:

- PDFs or PDF paths
- reading state, notes or projects
- the AI chat
- verbatim figure-caption excerpts
- the AI's group-specific follow-up suggestions
- papers the app's topic classifier labels "Off-topic / not seismology"

To export those suggestions anyway, pass `--include-group-followups`. The
privacy scan blocks them by default.

## Automatic daily publish

Since 2026-11-09 the snapshot also updates itself once a day. A LaunchAgent on
the owner's Mac runs `tools/auto_publish.py` every day at 21:00 local time.

- If the Mac is asleep at that time, the job runs when it wakes.
- If the Mac is switched off, that day is skipped and the next run catches up.

A run publishes only if the app has finished a new successful fetch and every
one of these checks passes:

- the export (read-only)
- the data files and JSON field paths match the live snapshot exactly
- paper count, total size and string length stay within limits
- no AI analysis was made from a PDF, and every source label has been
  published before
- `check_site.py` passes, and `privacy_scan.py` reports CLEAN
- the commit changes `docs/data/` only
- after the push, `check_site.py --base` passes on the live site

If any check fails, nothing is pushed. A macOS notification appears and the
reason goes to `$HOME/Library/Logs/SeismoHubLite/auto_publish.log`. The job
needs no API key.

```bash
python3 tools/auto_publish.py --status     # schedule, pause state, last results
python3 tools/auto_publish.py --pause      # later runs do nothing until --resume
python3 tools/auto_publish.py --resume
python3 tools/auto_publish.py --run-now    # start the daily job now
python3 tools/auto_publish.py --uninstall  # remove the LaunchAgent
```

`--pause` only creates the file `local/AUTO_PUBLISH_PAUSED`, so deleting that
file resumes too.

To install it again or change the time, run:

```bash
python3 tools/auto_publish.py --install --db <path-to>/seismo_hub.db --env-file <path-to>/.env --env-file <path-to>/old.env --at 21:00
```

Manual publishes work as before: run `update.py`, review the diff, then push.

## Copyright and takedown

Titles and abstracts belong to their authors and are often © the publisher.
They come from public bibliographic metadata: Crossref, OpenAlex, arXiv, the
ETH Research Collection and publisher feeds. They are reproduced here only
for non-commercial scholarly reference, each with a link to the original
article, and this project claims no rights in them.

If you hold rights in an abstract and want it removed, please
[open an issue](https://github.com/WANGYuhan2002/seismohub-lite/issues). It will
be taken down promptly: the abstract goes into `tools/abstract_blocklist.txt`
and the site is re-exported.

## AI-generated content

The following were generated offline with Google Gemini:

- takeaways, summaries and structured analyses
- key contributions, methods, data notes and limitations
- figure notes
- topic labels, keywords, facets and relevance scores

Gemini worked from each paper's abstract, public metadata and web search
results. No full-text PDFs were used. This content may be inaccurate or
incomplete, and the authors or publishers have not endorsed it. Always check
the original article.

## Privacy

The site is plain static files. Search and filtering run entirely in the
browser. Only two settings are saved, in the browser's local storage: the
visitor's language and whether the Browse directory is shown. As the host,
GitHub may keep standard server logs.

## 维护说明（中文）

更新步骤：

1. 运行 `tools/update.py`。它会依次做三件事，任何一步失败就停下：
   - 导出：只读打开 `seismo_hub.db`，只取白名单字段；被标为“离题 / 非地震学”的文章不导出。
   - 页面检查。
   - 隐私扫描：结果必须是 `RESULT: CLEAN`。
2. 查看改动。
3. 提交，然后推送。
4. 推送大约一分钟后，用 `check_site.py --base <网址>` 检查线上页面。

推送就等于公开发布。手动推送前，先确认扫描结果和改动内容。如果收到删除摘要的请求，在
`tools/abstract_blocklist.txt` 里加上对应的 DOI 或期刊，再重新导出。

每日自动发布：

- 每天 21:00（本机时间），LaunchAgent 运行 `tools/auto_publish.py`。
  - Mac 在睡眠：唤醒后补跑。
  - Mac 关机：当天跳过，下一次运行会把内容补上。
- 推送需要两个条件：App 有一次新的成功抓取，并且上面列出的检查全部通过。
  - 自动发布用这些检查代替人工看改动。
  - 任何一项没通过就不推送：系统会弹出通知，原因写进
    `$HOME/Library/Logs/SeismoHubLite/auto_publish.log`。
- 暂停：运行 `python3 tools/auto_publish.py --pause`，或者在 `local/` 里新建一个名为
  `AUTO_PUBLISH_PAUSED` 的文件。
- 恢复：运行 `--resume`，或者删掉这个文件。
- 查看状态用 `--status`，立即跑一次用 `--run-now`，彻底停用用 `--uninstall`。
