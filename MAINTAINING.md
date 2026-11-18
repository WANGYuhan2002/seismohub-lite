# Maintaining SeismoHub Lite

These are notes for the site's curator. Readers of the site do not need any of
this. [README.md](README.md) explains what the site is.

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
| `tools/fulltext_blocklist.txt` | Papers that get no AI content made from their full text, for takedown requests |
| `tools/privacy_allowlist.txt` | Strings the scan may let through after human review, each with a reason |

GitHub Pages serves the `docs/` folder of the `main` branch.

## Updating the snapshot by hand

```bash
python3 tools/update.py --db <path-to>/seismo_hub.db --env-file <path-to>/.env
git add -A
git diff --cached --stat
git commit -m "Update snapshot"
git push
python3 tools/check_site.py --base https://wangyuhan2002.github.io/seismohub-lite
```

`update.py` runs `export_lite.py`, `check_site.py` and `privacy_scan.py`, in
that order, and stops at the first failure. Pages rebuilds about a minute after
the push; then the last command checks the live site.

The exporter reads the app's harvest reports (`harvest_*.jsonl` in the
`_reports` folder of the app's PDF folder, next to `seismo_hub.db`) to see
which papers have a verified open copy. If that folder is missing it stops;
`--open-reports DIR` names another folder, and `--open-reports none` exports
nothing made from a full text.

## What the exporter leaves out

The exporter opens the database read-only (`mode=ro` and `query_only`) and reads
only whitelisted columns. It never exports:

- PDFs, full texts or PDF paths
- reading state, notes or projects
- the AI chat
- figure analyses and figure-caption summaries, from any analysis
- the AI's group-specific follow-up suggestions
- papers that the app's topic classifier labels "Off-topic / not seismology"
- AI analyses made from a PDF that is not a verified open copy (see below)

To export the follow-up suggestions anyway, pass `--include-group-followups`.
The privacy scan blocks them by default.

### Papers analysed from a PDF

The desktop app redoes a paper's analysis from its PDF once the PDF is
attached, and that analysis also sets the paper's topics. Since 2026-11-18 the
site publishes such an analysis when the PDF is a verified open copy (owner's
decision). The exporter decides per paper:

- **Verified open copy:** a harvest report records status `success` with
  licence tier `OPEN_CC_BY_OR_PREPRINT` for the paper, and no report records
  any other licence tier for it. A later failed run does not count against
  it, because the app never replaces a PDF a paper already has. The paper
  then shows the analysis made from its full text: takeaway, summary,
  structured analysis, key contributions, methods, data, limitations, topic
  matches, evidence, topics, keywords and facets. Figure analyses and the
  page and paragraph locators of the structured analysis are left out, and
  PDF file names in the text are masked. The note's evidence says "Based on:
  open-access full text", and the page tells readers that this paper's AI
  content was made from its open-access full text.
- **Not published** when there is no verified open copy, when the paper is in
  `tools/fulltext_blocklist.txt`, or when the model's output could not be
  parsed (no takeaway or summary). The paper keeps the AI note, takeaway,
  topics, keywords, facets and relevance shown in the last published snapshot
  if those were made from the abstract. The exporter reads them with git from
  `--previous` (`HEAD` by default). Otherwise the paper appears with its
  metadata and abstract only: topic "Others", no keywords, facets or note.
- If the new analysis labels the paper "Off-topic / not seismology", the paper
  leaves the site, like every off-topic paper (decided on 2026-11-16).

Pass `--previous none` only for a first export into an empty repository.

## Automatic daily publish

The snapshot updates itself once a day. A LaunchAgent on the curator's Mac runs
`tools/auto_publish.py` every day at 21:00 local time.

- If the Mac is asleep at that time, the job runs when it wakes.
- If the Mac is switched off, that day is skipped and the next run catches up.

The app fetches only while it is running (every 6 hours), so while it is
closed there is no new fetch and the site does not change.

A run publishes only if the app has finished a new successful fetch and all of
these checks pass:

- the export (read-only)
- the data files and JSON field paths match the live snapshot exactly
- paper count, total size and string length stay within limits. Published
  papers that the app now labels off-topic do not count as a drop, but at most
  10 % of the site may leave that way in one run.
- AI content made from a PDF appears only for papers that the harvest
  reports show with a verified open copy and that are not on the full-text
  blocklist. It never has figure analyses or page locators, and it always
  carries the "open-access full text" label, which no other paper carries.
  Every other paper analysed from a PDF shows exactly the abstract-based AI
  content the live snapshot showed for it, or no AI content. Every source
  label has been published before, apart from that one label.
- `check_site.py` passes, and `privacy_scan.py` reports CLEAN
- the commit changes only `docs/data/`
- after the push, `check_site.py --base` passes on the live site

If any check fails, nothing is pushed. A macOS notification appears, and the
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
file also resumes the job.

To install the job again or change its time, run:

```bash
python3 tools/auto_publish.py --install --db <path-to>/seismo_hub.db --env-file <path-to>/.env --env-file <path-to>/old.env --at 21:00
```

Manual publishes work as before: run `update.py`, review the diff, then push.

## Takedown requests

Add the DOI or the journal to `tools/abstract_blocklist.txt`, re-export, and
publish. For an AI note made from a full text, add the DOI, the paper id or
the journal to `tools/fulltext_blocklist.txt` instead.

## 维护说明（中文）

### 手动更新

1. 运行 `tools/update.py`。它依次做下面三件事，任何一步失败就停下：
   - 导出：只读打开 `seismo_hub.db`，只取白名单字段。被标为“离题 / 非地震学”的文章不导出。
   - 页面检查。
   - 隐私扫描：结果必须是 `RESULT: CLEAN`。
2. 查看改动。
3. 提交，然后推送。
4. 推送大约一分钟后，用 `check_site.py --base <网址>` 检查线上页面。

推送就是公开发布，手动推送前要先确认扫描结果和改动内容。

如果收到删除摘要的请求，把对应的 DOI 或期刊加进 `tools/abstract_blocklist.txt`，再重新导出。如果是针对依据全文生成的 AI 笔记，就把 DOI、论文 id 或期刊加进 `tools/fulltext_blocklist.txt`。

### 读过 PDF 的论文

桌面版挂上 PDF 后会读 PDF 重新分析，这次分析也会改写论文的主题。从 2026-11-18 起（维护者决定），PDF 是经过核实的开放获取版本时，网站发布这次分析。导出脚本逐篇判断：

- **经过核实的开放版本**：抓取报告（App 的 PDF 文件夹里 `_reports` 下的 `harvest_*.jsonl`）里有这篇的 `success` 记录，许可等级为 `OPEN_CC_BY_OR_PREPRINT`，并且没有任何一条记录是别的许可等级。之后某次抓取失败不影响判断，因为 App 从不替换已经有的 PDF。这时网站显示依据全文的分析：一句话要点、总结、结构化分析、主要贡献、方法、数据、局限性、主题匹配、分析依据、主题、关键词和分面。图表解析和结构化分析里的页码、段落定位不发布，文中的 PDF 文件名被隐去。分析依据写作 “open-access full text”，页面上会告诉读者这篇的 AI 内容依据的是开放获取全文。
- **不发布**：没有经过核实的开放版本、论文在 `tools/fulltext_blocklist.txt` 里，或者模型输出无法解析（没有要点也没有总结）。这时如果上一次发布的 AI 笔记、一句话要点、主题、关键词、分面和相关度是根据摘要生成的，就继续显示这些内容，导出脚本用 git 从 `--previous`（默认 `HEAD`）读取；否则只显示元数据和摘要，主题记为 “Others”，没有关键词、分面和 AI 笔记。
- 新的分析把论文标为“离题 / 非地震学”：论文从网站上撤下，和其他离题论文一样（2026-11-16 决定）。

只有往空仓库做第一次导出时才用 `--previous none`。找不到抓取报告时导出会停下；`--open-reports none` 表示不发布任何依据全文的内容。每日自动发布还会单独核对一遍：依据全文的内容只能出现在经过核实的开放版本上，不能带图表解析或页码定位，并且必须标为 “open-access full text”；其他读过 PDF 的论文，显示的内容必须和线上快照里根据摘要生成的内容完全一样，或者不带任何 AI 内容，否则不推送。

### 每日自动发布

- 每天 21:00（本机时间），LaunchAgent 会运行 `tools/auto_publish.py`。
  - Mac 在睡眠：唤醒后补跑。
  - Mac 关机：当天跳过，下一次运行会把内容补上。
- 推送需要同时满足两个条件：App 有一次新的成功抓取，并且上面列出的检查全部通过。这些检查代替了人工查看改动。
- 论文数一次最多减少 1%（至少允许 5 篇）。已发布、但 App 现在标为离题的论文不算在内；不过一次最多撤下网站上 10% 的论文，超过就停下。
- 抓取只在 SeismoHub 主程序开着时进行。主程序关着，就没有新的抓取，网站也就不会更新。
- 任何一项检查没通过就不推送。系统会弹出通知，原因写进 `$HOME/Library/Logs/SeismoHubLite/auto_publish.log`。

常用操作：

- 暂停：运行 `python3 tools/auto_publish.py --pause`，或者在 `local/` 里新建一个名为 `AUTO_PUBLISH_PAUSED` 的文件。
- 恢复：运行 `--resume`，或者删掉这个文件。
- 查看状态：`--status`。
- 立即跑一次：`--run-now`。
- 彻底停用：`--uninstall`。
