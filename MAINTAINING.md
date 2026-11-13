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

## What the exporter leaves out

The exporter opens the database read-only (`mode=ro` and `query_only`) and reads
only whitelisted columns. It never exports:

- PDFs or PDF paths
- reading state, notes or projects
- the AI chat
- verbatim figure-caption excerpts
- the AI's group-specific follow-up suggestions
- papers that the app's topic classifier labels "Off-topic / not seismology"
- anything from an AI analysis made from a PDF (see below)

To export the follow-up suggestions anyway, pass `--include-group-followups`.
The privacy scan blocks them by default.

### Papers analysed from a PDF

The desktop app redoes a paper's analysis from its PDF once the PDF is
attached, and that analysis also sets the paper's topics. The site promises
that no full text was used, so the exporter publishes nothing from such an
analysis:

- If the paper is already on the site, it keeps the AI note, takeaway, topics,
  keywords, facets and relevance shown in the last published snapshot. Those
  were made from the abstract. The exporter reads them with git from
  `--previous` (`HEAD` by default).
- If the paper has never been on the site, it appears with its metadata and
  abstract only: topic "Others", no keywords, facets or note.

Pass `--previous none` only for a first export into an empty repository.

## Automatic daily publish

The snapshot updates itself once a day. A LaunchAgent on the curator's Mac runs
`tools/auto_publish.py` every day at 21:00 local time.

- If the Mac is asleep at that time, the job runs when it wakes.
- If the Mac is switched off, that day is skipped and the next run catches up.

A run publishes only if the app has finished a new successful fetch and all of
these checks pass:

- the export (read-only)
- the data files and JSON field paths match the live snapshot exactly
- paper count, total size and string length stay within limits
- nothing from an AI analysis made from a PDF is in the export: each such
  paper shows exactly what the live snapshot showed for it, or no AI content
  if it is new; and every source label has been published before
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
publish.

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

如果收到删除摘要的请求，把对应的 DOI 或期刊加进 `tools/abstract_blocklist.txt`，再重新导出。

### 读过 PDF 的论文

桌面版挂上 PDF 后会读 PDF 重新分析，这次分析也会改写论文的主题。网站承诺没有使用全文，所以这类分析的任何内容都不导出：

- 论文已经在网站上：继续显示上一次发布时的 AI 笔记、一句话要点、主题、关键词、分面和相关度。这些都是根据摘要生成的，导出脚本用 git 从 `--previous`（默认 `HEAD`）读取。
- 论文从没上过网站：只显示元数据和摘要，主题记为 “Others”，没有关键词、分面和 AI 笔记。

只有往空仓库做第一次导出时才用 `--previous none`。每日自动发布还会单独核对一遍：这类论文显示的内容必须和线上快照完全一样，新论文必须不带任何 AI 内容，否则不推送。

### 每日自动发布

- 每天 21:00（本机时间），LaunchAgent 会运行 `tools/auto_publish.py`。
  - Mac 在睡眠：唤醒后补跑。
  - Mac 关机：当天跳过，下一次运行会把内容补上。
- 推送需要同时满足两个条件：App 有一次新的成功抓取，并且上面列出的检查全部通过。这些检查代替了人工查看改动。
- 任何一项检查没通过就不推送。系统会弹出通知，原因写进 `$HOME/Library/Logs/SeismoHubLite/auto_publish.log`。

常用操作：

- 暂停：运行 `python3 tools/auto_publish.py --pause`，或者在 `local/` 里新建一个名为 `AUTO_PUBLISH_PAUSED` 的文件。
- 恢复：运行 `--resume`，或者删掉这个文件。
- 查看状态：`--status`。
- 立即跑一次：`--run-now`。
- 彻底停用：`--uninstall`。
