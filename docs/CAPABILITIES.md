# Capabilities and validation / 能力与验证

[English overview](../README.md) · [中文概览](../README.zh-CN.md)

Snapshot scope: **2026-11-07**. This is a summary of observed behavior in an existing configured Windows environment, not a fresh-install certificate or a promise of current external endpoint availability.

本页记录截至 **2026-11-07** 的既有 Windows 配置环境证据，不是全新安装通过证明，也不保证外部接口始终可用。

## How to read the status / 如何理解状态

- **Implemented / 已实现**: code is integrated into the shared application; live access may still be unverified.
- **Local checks / 本地验证**: fixtures or local services exercised a defined behavior, not a platform account.
- **Live sample / 真实样本**: an actual endpoint or runtime completed a bounded scenario; untested combinations remain unknown.
- **Partial / 部分完成**: usable records may exist, but coverage, budgets, or failed sources prevent a full-completion claim.

## Core workflow / 核心流程

| Capability / 能力 | Evidence and boundary / 证据与边界 |
|---|---|
| HTTP/browser collection · HTTP/浏览器采集 | Real page capture and bounded local scenarios; preserves source content and failures. Does not guarantee arbitrary sites or every browser/configuration combination. 已有真实网页与有限本地场景；保留原文和失败，不保证任意网站/组合。 |
| Online search · 联网搜索 | Search-to-collection has run on real sources. Providers can reject, challenge, time out, or rank poorly. 搜索及采集实际执行过；提供方可拒绝、挑战、超时或排序不佳。 |
| Source-grounded answers · 有来源回答 | Existing-account Codex generation and support review passed bounded questions and a repaired resume case. Some tasks remain partial due to source gaps. 账号路线通过有限问题与修后恢复样本，来源缺口仍保留 partial。 |
| Resume and budgets · 恢复与预算 | Source/version/range/backend/contracts govern answer reuse. Targeted checks cover keeping reviewed conclusions on budget interruption. 回答复用绑定来源版本、范围、后端及协议；定点验证覆盖预算中断保留结论。 |
| Text/semantic retrieval · 文字/语义检索 | Local search and optional CPU multilingual embeddings retain source ranges. Token/chunk limits are reported; similarity is not truth. 保留原文范围；编码/分块上限可见，相似度不是真实性评分。 |
| Long screenshots · 长截图 | Bounded complete captures and a capped worktable task retained image/source evidence and released browser resources. Older standalone diagnostic termination remains unexplained. 有限完整图与工作台上限样本通过；旧独立诊断退出原因未解。 |
| Structured extraction · 结构化提取 | CSS/XPath and optional model-assisted extraction are integrated. Original material stays separate from derived output; schema validity does not establish semantic accuracy. 规则/模型提取已接入，原文与派生结果分开；结构有效不等于理解正确。 |
| Exports · 导出 | File exports and local MySQL/PostgreSQL/MongoDB destinations have bounded evidence. Arbitrary versions, remote production deployments and scale are not certified. 文件与本机数据库有有限证据，不代表任意版本、远程生产或规模能力。 |

## Platform collection / 平台采集

| Platform / 平台 | Current scope / 当前范围 |
|---|---|
| Xiaohongshu / 小红书 | Authenticated samples: search/details, two root-comment pages, creator posts, images, a 12-card home-feed response with service signing, and real extension-import reuse for one note. Not exhaustive comments, all media variants, or session-expiry recovery. 已有授权实采；不代表评论穷尽、所有媒体或失效重登闭环。 |
| Bilibili / B站 | Bounded anonymous collection/discovery evidence, including trending topics and a two-page, 12-video home feed. Anonymous recommendations are not authenticated personalization. 有限匿名实证；不冒充登录个性化推荐。 |
| Tieba / 贴吧 | Public hot-topic snapshot and bounded collection evidence. Hot-topic records are topics, not posts or comments; the snapshot has no verified pagination contract. 热议是话题，不是帖子/评论；无已验证榜单分页。 |
| Douyin, Kuaishou, Weibo, Zhihu / 抖音、快手、微博、知乎 | Source adapters exist; full selected-account search/detail/creator/comment workflows are not accepted across all four. Entry-point availability and authorization remain prerequisites. 已有源码适配，不等于四平台全部登录流程均验收；知乎问题全部回答不宣称完成。 |
| Additional community discovery / 其他社区发现 | StackExchange question/answer discovery is integrated with quotas and backoff. Reddit access encountered HTTP403; full morechildren expansion is not implemented/accepted. StackExchange 已接入；Reddit 拒绝及未实现的回复补支保持明确。 |

## Identity and signing / 身份与签名

| Component / 组件 | What is established / 已建立的事实 |
|---|---|
| Saved accounts · 已保存账号 | Explicit account selection and protected local state; XHS reuse demonstrated. Some probes remain indeterminate. 显式选号与本地保护，XHS 实际复用通过；部分探针仍不确定。 |
| CookieBridge | Real XHS toolbar → persistent permission page → import → one-note reuse completed; four temporary host permissions were removed. Only selected-site root, non-partitioned cookies are covered, not all browser storage or platforms. XHS 真实链通过且撤回临时权限；不覆盖全部存储/平台。 |
| Account/proxy pool · 账号代理池 | Fixed pairing, leases, identity snapshots, bounded allocation and resume checks have local integration evidence. Real public-platform traffic through a supplied proxy pool is unvalidated. 固定配对/租约/快照有本地证据；真实代理联合平台未验。 |
| Signing service · 签名服务 | XHS/Douyin/Zhihu algorithms exercised over local HTTP; XHS live home feed and imported-session detail accepted service signing. Douyin/Zhihu live acceptance is not implied. 三算法本机往返通过，XHS 有真实请求；不能外推另两平台。 |
| Kuaishou signing · 快手签名 | Browser dependency retained; not advertised as browser-free service signing. 保留浏览器依赖，不宣称已实现脱离浏览器签名。 |

## Optional models and media / 可选模型与媒体

Local OCR, transcription, visual descriptions and image generation/editing have selected runtime samples. Comment analysis with Codex passed a fixed, small sample review; that is not a general sentiment or sarcasm benchmark. Image editing passed a particular two-step composition/color/style task, not arbitrary instruction fidelity. Model weights are omitted from this source snapshot.

本地 OCR、转写、视觉及图片生成/编辑有有限运行样本；Codex 评论分析通过固定小样本审阅，不是情绪或反讽通用基准。图片编辑通过特定两步构图/颜色/风格任务，不保证任意指令效果。公开源码不含模型权重。

Local 4B/8B text-model experiments did not consistently meet the grounding-quality threshold. Current defaults use the existing Codex account, require network access, and consume account allowance. No API-key fallback is implied.

本地 4B/8B 文本模型未稳定达到依据充分的回答门槛。当前默认使用已有 Codex 账号，需要联网并消耗额度，不隐含 API Key 回退。

## Non-goals and remaining gaps / 非承诺与剩余项

No complete Crawl4AI equivalence, full MediaCrawlerPro replacement, private Pro protocol compatibility, unrestricted commercial license, universal platform access, distributed production scaling, or clean-install portability claim is made. Source mapping, unit checks, live samples, and production-scale acceptance are distinct evidence levels.

不宣称 Crawl4AI 全功能等价、完整 Pro 替代、私有协议兼容、无限制商用、平台普遍可访问、分布式生产扩展或全新安装跨平台通过。源码映射、单元验证、真实样本、生产规模验收是不同层级。
