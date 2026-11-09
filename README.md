# SeismoHub Lite

A static, read-only snapshot of SeismoHub, a personal literature tracker for
seismology and wave physics.

**Live site:** https://wangyuhan2002.github.io/seismohub-lite/

Each paper shows its title, authors, journal, date, abstract and DOI or link,
plus the AI analyses that were generated offline: takeaway, summary,
structured notes, topic labels, keywords and facets.

What you can do on the page:

- Search, sort, and filter by topic, journal, year, month, keyword,
  institution, method, data type, target and paper type.
- Step through papers with ‹ / › or the arrow keys.
- Copy a BibTeX entry, or export the current list as BibTeX or RIS.
- Switch between English and Chinese.

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

To export those suggestions anyway, pass `--include-group-followups`. The
privacy scan blocks them by default.

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
browser. Only the visitor's language choice is saved, in the browser's local
storage. As the host, GitHub may keep standard server logs.

## 维护说明（中文）

更新步骤：

1. 运行 `tools/update.py`。它会依次做三件事，任何一步失败就停下：
   - 导出：只读打开 `seismo_hub.db`，只取白名单字段。
   - 页面检查。
   - 隐私扫描：结果必须是 `RESULT: CLEAN`。
2. 查看改动。
3. 提交，然后推送。
4. 推送大约一分钟后，用 `check_site.py --base <网址>` 检查线上页面。

推送就等于公开发布。每次推送前，先确认扫描结果和改动内容。如果收到删除摘要的请求，在
`tools/abstract_blocklist.txt` 里加上对应的 DOI 或期刊，再重新导出。
