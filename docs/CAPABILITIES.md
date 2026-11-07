# Capabilities / 能力矩阵

[Overview](../README.md) · [中文概览](../README.zh-CN.md) · [Usage](USAGE.md)

Current evidence: **2026-11-07, configured Windows environment**. “Local checks” means a fixture or local service exercised the behavior; “live sample” means actual external content or model execution. Setup instructions are in [Getting started](GETTING_STARTED.md).

当前证据来自 **2026-11-07 已配置的 Windows 环境**。本地验证指构造样本或本机服务，真实样本指实际外部内容或模型执行；环境准备见[开始使用](GETTING_STARTED.md)。

## Collection and research / 采集与研究

| Capability / 能力 | What it does / 功能 | Conditions and validation / 条件与验证 |
|---|---|---|
| Web collection / 网页采集 | HTTP and browser pages, waiting/scrolling, selected sessions, screenshots, PDF and local inputs / 网页、动态浏览、会话、截图、PDF及本机输入 | Real page captures and bounded browser scenarios; access depends on the selected site / 已有网页及有限浏览器样本，访问受站点条件影响 |
| Search and retrieval / 搜索检索 | Public search, saved-text search, optional multilingual CPU embeddings / 公开搜索、已存文字及可选多语言CPU向量检索 | Actual search-to-collection and retrieval samples; provider errors and read limits remain in results / 已有实际样本，保留提供方错误与读取范围 |
| Grounded answers / 来源回答 | Source versions, selected quotations, claim review and eligible repair / 来源版本、引文、逐条审核与有限修正 | Host-driven answers or configured application backends; bounded questions and resume cases reviewed / 可由智能体工具回答或程序后端执行，已有有限问题及恢复审阅 |
| Model API / 模型 API | Named OpenAI-compatible and Anthropic profiles for research and saved-material answers / 命名配置接入两类协议，用于研究及已存资料回答 | Explicit user selection; local simulated transport/configuration/task checks, real provider credentials still required / 用户明确选用，模拟接口、配置与任务验证；真实提供方需用户凭据，见[指南](MODEL_API.md) |
| Checkpoints and budgets / 断点与预算 | Atomic result/progress writes; pause, resume, cancellation and explicit coverage / 原子保存、暂停、续跑、取消与覆盖状态 | Focused lifecycle checks; compatible reviewed answers and source snapshots can be reused / 已有生命周期定点验证，可复用兼容答案与来源快照 |
| Structured extraction / 结构化提取 | CSS/XPath/regex, Markdown, schemas, complex tables and optional model extraction / 规则、Markdown、结构、复杂表及可选模型提取 | Original content and derived output kept separately; strict DOM recovery available for explicit table structures / 原文与派生结果分存，明确表格结构可用严格DOM恢复 |
| Discovery and traversal / 网址发现与遍历 | Sitemaps, bounded deep traversal, adaptive selection and cache controls / 站点地图、有界深度遍历、自适应选择和缓存 | Local/source behavior checked; archive providers and Google-specific extraction retain public-access or model-quality gaps / 本地及源行为已验证，档案提供方及Google专用路径仍有公网或模型质量待验项 |
| Long screenshots / 长截图 | Scroll-based stitching with segment/pixel limits and partial prefixes / 按滚动位置拼接，支持分段、像素上限与部分前缀 | Complete samples and a capped workbench task passed; older standalone diagnostic termination remains unexplained / 完整图及工作台上限样本通过，旧独立诊断退出原因未明 |
| Exports / 导出 | JSON/JSONL/CSV/Excel/SQLite and configured MySQL/PostgreSQL/MongoDB destinations / 文件及已配置数据库导出 | File outputs and local database samples; destination credentials stay in named private configuration / 文件与本机数据库已有样本，凭据存于命名私密配置 |

## Platforms / 平台

| Platform / 平台 | Available paths / 已接路径 | Current validation / 当前验证范围 |
|---|---|---|
| Xiaohongshu / 小红书 | Search, details, creator posts, comments/replies, homepage cards and media candidates / 搜索、详情、作者内容、评论回复、首页卡及媒体候选 | Selected-account search/detail, two root-comment pages, creator posts, two image downloads, one 12-card homepage response and imported-session detail; full child replies, homepage next page, video/live-photo and expiry recovery await live samples / 所列路径已有授权样本，完整子回复、首页下一页、视频动态照片及自然失效重登待实采 |
| Bilibili / B站 | Posts/comments, popular videos, trending topics and homepage recommendations / 帖子评论、热门视频、热搜话题和首页推荐 | Bounded anonymous samples, including two homepage pages and12 videos / 有限匿名样本，含两页12条首页视频 |
| Tieba / 贴吧 | Search/details/creator adapters and a public hot-topic snapshot / 搜索详情作者适配及公开热议快照 | Bounded collection and topic snapshot; topic records carry source rank and raw metrics / 有限采集和话题快照，保留原排名与原指标 |
| Douyin, Kuaishou, Weibo, Zhihu / 抖音、快手、微博、知乎 | Search/detail/creator and platform-specific comment adapters / 搜索详情作者及各平台评论适配 | Selected-account workflows need further live validation; new homepage/trending routes and Zhihu question-to-all-answers remain to be added / 登录流程待进一步实采，新增首页热搜及知乎问题全回答待接入 |
| StackExchange | Questions/search/details, answers/comments and bounded pagination / 问题搜索详情、回答评论及有界分页 | Public question/answer samples; quotas and backoff respected / 已有公开问答样本，遵守配额退避 |
| Reddit | Listing/detail/QA-sort adapters / 列表详情及问答排序适配 | Public request returned403; morechildren branch expansion remains to be implemented / 公开请求403，morechildren补支待实现 |

Homepage cards may omit full text, dates, and comment counts. Hot-topic records remain topics. Recommendation streams without a terminal marker retain partial coverage.

首页卡可能缺正文、日期和评论数；热议记录保持话题类型。没有明确末页标记的推荐流保留部分覆盖状态。

## Sessions and local services / 会话与本机服务

| Component / 组件 | Function and evidence / 功能与证据 | Operating condition / 使用条件 |
|---|---|---|
| Saved accounts / 会话保存 | Explicit identity, protected state and actual XHS reuse / 显式身份、本地保护及XHS实际复用 | User login; account probes can remain indeterminate / 本人登录，部分校验仍可能不确定 |
| CookieBridge | Selected site/store import with one-use pairing; actual XHS import/reuse and temporary-permission removal / 指定站点容器、一次性配对，XHS真实导入复用及临时权限撤回 | User permission; current scope is supported root, non-partitioned cookies / 本人授权，当前覆盖受支持的根路径非分区Cookie |
| Account/proxy pool / 账号代理池 | Fixed pairs, leases, task snapshots and resume; local integration checks / 固定配对、租约、快照、恢复与本地验证 | User-supplied accounts/proxies; public-platform pool validation and Excel/MySQL account import remain open / 用户提供账号代理，公网联合验证及Excel/MySQL账号导入待补 |
| Signing service / 签名服务 | Local HTTP XHS/Douyin/Zhihu algorithms; XHS homepage and imported-session detail accepted signatures / 三算法本机往返，XHS首页及导入会话详情实际接受 | Optional authenticated loopback service; Kuaishou retains its browser signer / 可选鉴权回环服务，快手保留浏览器签名 |
| Workbench, CLI, Skill and API / 工作台、命令行、技能与接口 | Shared task and evidence lifecycle / 共享任务和证据生命周期 | New pool/homepage UI has code/interface review; its latest visual/interaction acceptance is pending / 新池及首页控件已有代码接口审查，最新视觉交互待验 |

## Media and analysis / 媒体与分析

| Capability / 能力 | Available behavior / 功能 | Current quality or coverage / 当前质量与覆盖 |
|---|---|---|
| Media download / 媒体保存 | Bounded downloads, hashes, backups, conditional ranges and format assembly / 限额下载、哈希、备用地址、条件续传和格式合并 | Actual XHS images; protected video, live-photo, CDN expiry and account quality variants need further live samples / XHS图片已实采，受保护视频、动态照片、过期及账号档位待验证 |
| OCR and transcription / OCR与转写 | Local image text and audio/video speech processing / 本机图文及音视频处理 | Natural samples executed; observed OCR character duplication and one Mandarin character substitution remain documented quality limits / 已运行自然样本，保留OCR重复字符及中文单字替换误差 |
| Visual understanding / 视觉理解 | Image descriptions and video frame/time references / 图片描述及视频帧时间引用 | Reviewed natural photo and sampled frames; video coverage is sampled / 自然照片与采样帧已审阅，视频覆盖为抽帧 |
| Comment analysis / 评论分析 | Versioned emotion/intent/stance/aspect labels, counts and reports / 情绪意图立场方面标签、版本计数报告 | Fixed small account-model sample reviewed; context-sensitive interpretation still benefits from human review / 固定小样本已审阅，依赖上下文的判断仍需人工核对 |
| Image creation/editing / 图片生成编辑 | Local generation, reference/instruction editing, versions and3 original templates / 本机生成、参考指令编辑、版本与3个原创模板 | A two-step edit passed composition/color/style checks; generation color adherence still has a gap; evaluation uses pixel statistics and human review / 两步编辑样本通过，原生成颜色遵循仍有缺口，评价为像素统计及人工审阅 |

Optional local workers need their own dependencies and model assets. Local4B/8B answer experiments remain uneven; configure the answer route in [Agent integration](AGENT_INTEGRATION.md). Windows has the current execution evidence; Linux execution awaits a suitable validation environment.

可选本机worker需独立依赖与模型资源；本地4B/8B回答质量仍不稳定，推理配置见[智能体接入](AGENT_INTEGRATION.md)。当前执行证据在Windows，Linux执行待可用环境验证。组件使用条件见[许可](../LICENSE.md)。
