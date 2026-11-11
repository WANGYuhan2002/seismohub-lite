# SeismoHub Lite

New research in seismology and wave physics on one searchable page, with
abstracts, links to the original articles and short AI-written notes. It is
updated about once a day.

**Open the site:** https://wangyuhan2002.github.io/seismohub-lite/

SeismoHub Lite is a free, read-only snapshot of SeismoHub, a personal
literature tracker for seismology and wave physics. It is shared here so that
anyone can skim what has come out recently and decide what to read.

中文简介见[文末](#中文简介)。

## What is on the page

For every paper:

- the title, authors, journal and publication date
- the abstract, and a link to the article through its DOI (or its own page)
- AI notes: a one-sentence takeaway, a short summary, structured notes
  (methods, data, results, limitations), topic labels and keywords

The collection follows the curator's research interests, so it is not a
complete index of the field. Papers that the topic classifier marks as
unrelated to seismology are left out.

## How to use it

- **See what is new:** pick *Last 7 days* or *Last 30 days*. The header shows
  when the data was last updated.
- **Search** titles, authors, keywords and abstracts. Results come in order of
  best match, so papers with your terms in the title come first.
- **Filter** by topic, journal, keyword, institution, method, data, target and
  paper type.
- **Sort** by newest, oldest, journal or title. Journals are ordered by
  approximate impact factor, and the numbers themselves are not shown.
- **Read a paper:** the title opens the article, and the *AI* button opens the
  abstract and the AI notes. Use ‹ › or the arrow keys to move between papers.
- **Cite or save:** copy a BibTeX entry, or export the papers you are looking
  at as BibTeX or RIS for Zotero, EndNote or Mendeley.
- **Share** a search, a filter or a single paper: each one has its own link.
- **Browse list:** on wide screens, a list on the right shows every paper in
  the current view by first author, journal and date. Click a row to jump to
  that paper.

The page works on phones, follows your system's light or dark mode, and
switches between English and 中文.

## About the AI notes

Everything marked “AI” was generated offline with Google Gemini from each
paper's abstract, public metadata and web search results. No full texts were
used. The notes can be wrong or incomplete, the authors and publishers have
not endorsed them, and they do not replace reading the paper. No AI runs on
this website, and nothing you type is sent anywhere.

## Where the data comes from

Titles, authors, journals, dates, DOIs and abstracts come from public
bibliographic metadata: Crossref, OpenAlex, arXiv, the ETH Research Collection
and publisher RSS feeds.

Each update is exported from the curator's database and goes online only after
automatic checks pass: a full test of the page in a headless browser and a
privacy scan. If a check fails, that update is skipped.

## Copyright and takedown

Titles and abstracts belong to their authors and are often © the publisher.
They are shown here only for non-commercial scholarly reference, each with a
link to the original article, and this project claims no rights in them.

If you hold rights in an abstract and want it removed, please
[open an issue](https://github.com/WANGYuhan2002/seismohub-lite/issues). It will
be taken down promptly.

## Privacy

The site is plain static files hosted on GitHub Pages. It has no accounts,
cookies or analytics, and loads no third-party scripts or fonts. Search and
filtering run in your browser.

Only two settings are kept, in your browser's local storage: your language and
whether the Browse list is shown. As the host, GitHub may keep standard server
logs.

## Feedback

Spotted a mistake, a broken link or a paper that does not belong?
[Open an issue](https://github.com/WANGYuhan2002/seismohub-lite/issues).

## How it is built

| Path | Content |
| --- | --- |
| `docs/index.html` | The whole page: HTML, CSS and plain JavaScript in one file, no frameworks |
| `docs/data/` | The paper list, abstracts and AI notes, as JSON |
| `tools/` | The read-only exporter, the page test and the privacy scan |

GitHub Pages serves the `docs/` folder. Notes on maintaining the site are in
[MAINTAINING.md](MAINTAINING.md).

## 中文简介

SeismoHub Lite 把地震学和波动物理方向的新文章集中在一个可以搜索的网页上，附有摘要、原文链接和
AI 写的简短笔记，大约每天更新一次。

**网址：** https://wangyuhan2002.github.io/seismohub-lite/

- **内容**：每篇文章都有标题、作者、期刊、发表日期、摘要和 DOI 链接，另有 AI 笔记，包括：
  - 一句话要点
  - 简短总结
  - 方法、数据、结果和局限
  - 主题和关键词

  收录范围跟随维护者的研究兴趣，不是全领域索引。被判定与地震学无关的文章不收录。
- **用法**：
  - 用「最近 7 天」或「最近 30 天」看新文章。
  - 搜索结果按匹配程度排序，标题命中的排在最前。
  - 可以按主题、期刊、关键词、机构、方法、数据、研究对象和文章类型筛选。
  - 点标题打开原文，点「AI」看摘要和 AI 笔记。
  - 可以复制 BibTeX，也可以把当前列表导出成 BibTeX 或 RIS。
  - 每个搜索、筛选结果和每篇文章都有自己的链接，方便分享。
  - 页面支持手机，跟随系统的深浅色，可以切换中英文。
- **AI 内容**：
  - 由 Google Gemini 离线生成，依据摘要、公开元数据和网络检索结果，没有使用全文。
  - 可能有错，未经作者或出版社认可，不能代替阅读原文。
  - 网站本身不运行任何 AI。
- **数据来源**：Crossref、OpenAlex、arXiv、ETH Research Collection 和出版社 RSS。每次更新都要先通过页面测试和隐私扫描才会上线，没通过就跳过这次更新。
- **版权**：标题和摘要的版权属于作者或出版社，这里只作非商业的学术参考，并附原文链接。权利人如需删除，请[提交 issue](https://github.com/WANGYuhan2002/seismohub-lite/issues)，会尽快处理。
- **隐私**：
  - 纯静态网页，没有账号、cookie 和统计，也不加载第三方脚本或字体。
  - 浏览器里只记住两项设置：语言，以及是否显示 Browse 列表。
  - GitHub 作为托管方，可能保留常规的服务器日志。
