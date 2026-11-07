# Agent integration / 智能体接入

[Usage](USAGE.md) · [Architecture](ARCHITECTURE.md) · [Capabilities](CAPABILITIES.md)

Crawl_for_agent gives an agent a persistent collection and research workspace. Codex, Claude Code, and other tool-capable hosts can invoke its Skill instructions, CLI, or local HTTP API, then use their own configured model to reason over the returned material.

Crawl_for_agent 为智能体提供持久化的采集与研究工作空间。Codex、Claude Code及其他支持工具调用的宿主，可以通过Skill指引、命令行或本机HTTP API调用，再用宿主已配置的模型分析返回资料。

![Agent and research workspace](assets/agent-harness.png)

[Artwork notes / 插画说明](ARTWORK.md)

## Choose the connection / 选择接入方式

| Connection / 方式 | Setup / 配置 | Best fit / 适用场景 |
|---|---|---|
| Skill | Copy [the template](../skill/research-crawler/SKILL.md) into the host's supported skill location and provide the clone root / 复制模板到宿主支持的技能目录，并指定项目位置 | Conversational collection and research / 对话式采集研究 |
| CLI | Allow the host to invoke the configured project entrypoint / 允许宿主调用已配置的项目入口 | Explicit jobs, local scripts and inspection / 明确任务、本机脚本与结果检查 |
| Local HTTP API | Start the local service and configure a private authenticated client / 启动本机服务，配置私密鉴权客户端 | Custom tools and local integrations / 自定义工具与本地集成 |

Use the clone root supplied by the user or `RESEARCH_CRAWLER_HOME`. An installed Skill copy reads documentation from that root. Host-specific installation and tool permissions follow the host's configuration. Current conversational acceptance is from Codex; the other hosts listed above are integration routes through the same interfaces.

使用用户指定目录或 `RESEARCH_CRAWLER_HOME`。单独安装的Skill从该目录读取文档，安装位置与工具权限遵循宿主配置。现有对话式验收来自Codex；其他宿主通过相同接口接入。

## Host-driven research / 由宿主完成研究

1. The host interprets the question and selects a bounded collection or retrieval task.
2. Crawl_for_agent saves original material, source identity, coverage and errors.
3. The host reads the saved evidence and answers with source references.
4. Follow-up questions reuse the library or resume the relevant task.

1. 宿主理解问题，选择有界采集或检索任务。
2. Crawl_for_agent保存原文、来源身份、覆盖范围与错误。
3. 宿主读取证据并附来源回答。
4. 后续问题复用资料库或继续原任务。

Inference belongs to the host in this route: it uses whatever API connection or subscription-backed model access the host supports and the user has configured. Billing, quota, model choice and login remain with the host. Collecting a page does not require starting the application's independent answer backend.

这条路线由宿主执行推理，使用宿主支持且用户已配置的API连接或订阅模型访问方式；计费、额度、模型选择及登录归宿主管理。采集网页本身不需要启动程序的独立回答后端。

For example, an agent can run `web.search`, `web.collect` or `search.local`, inspect saved records, and answer in the current conversation. This avoids a second model call for the same answer. Examples are in [Usage](USAGE.md).

例如，智能体可以执行 `web.search`、`web.collect` 或 `search.local`，读取记录后直接在当前对话回答，避免同一回答再次调用独立模型。示例见[使用方式](USAGE.md)。

## Local HTTP interface / 本机HTTP接口

The default address is `http://127.0.0.1:8946`. API requests require the `X-Research-Token` header. The service creates/reads its local token at `<configured-root>/state/api-token`; an integration should load it privately and keep it out of model messages, logs, command history and repository files.

默认地址为 `http://127.0.0.1:8946`。API请求使用 `X-Research-Token` 请求头；服务在 `<配置目录>/state/api-token` 创建／读取本地令牌。接入程序应私密加载，不把值放入模型消息、日志、命令历史或仓库。

| Request / 请求 | Purpose / 用途 |
|---|---|
| `GET /api/status` | Registered handlers and runtime availability / 已注册能力及运行状态 |
| `POST /api/jobs` | Submit `{"kind":"web.collect","params":{...}}`; HTTP202 returns the job / 提交任务，202返回新任务 |
| `GET /api/jobs/{job_id}` | Status, counts, records and resume limits / 状态、计数、记录及可提高上限 |
| `POST /api/jobs/{job_id}/resume` | Resume with existing settings or an explicit supported record increase / 按原配置或明确提高条数继续 |
| `POST /api/jobs/{job_id}/pause` | Request a running task to pause / 请求暂停运行任务 |
| `POST /api/jobs/{job_id}/cancel` | Request cancellation / 请求取消 |

The API checks loopback host and request origin as well as the token. Keep integrations local. Jobs run asynchronously; inspect persisted status instead of treating HTTP202 as completion. Poll at a reasonable interval and honor concurrency limits.

接口同时检查本机主机、请求来源和令牌，集成保持本机访问。任务异步执行，HTTP202表示已提交，应继续查看持久状态；合理设置查询间隔并遵守并发上限。

## Application answer configuration / 程序内回答配置

Use this route when the application itself should persist generation, quotation review and eligible repair, independently of the host conversation.

需要程序独立保存生成、引文审核及有限修正流程时，使用这条路线。

| Setting / 配置 | Current behavior / 当前行为 |
|---|---|
| `research.answer` | Search, fetch, select context, generate and review / 搜索、采集、组织上下文、生成与审核 |
| `answer.model` | Model answer over stored material / 基于已存资料进行模型回答 |
| `answer_backend:"codex"` | Existing Codex CLI with ChatGPT login; network and account allowance / 已有Codex CLI及ChatGPT登录，使用网络和账户额度 |
| `answer_backend:"local"` | Configured local runtime and model assets; experimental answer quality / 已配置本机运行环境和模型，回答质量仍属实验 |
| `evidence_selection:"context"` | Bounded complete-source context with recorded ranges for Codex research / Codex研究使用有界来源正文并记录范围 |

New answer jobs default to `codex`; historical parameters remain unchanged on resume. The standalone selector accepts `codex` and `local`. Claude Code or another host can reason through its own model after collection; that is distinct from a built-in `claude` backend or generic API-provider parameter.

新建回答任务默认 `codex`，历史任务恢复不改原参数。独立后端选择项为 `codex` 和 `local`。Claude Code等宿主可在采集后调用自身模型；这与程序内置 `claude` 后端或通用API提供方参数是两回事。

The Codex route requires an installed CLI with its supported ChatGPT-account login. If an executable location is needed, configure `RESEARCH_CODEX_EXE` before starting the service. This backend does not use an API key. Quota or network errors preserve sources and an incomplete outcome, with no automatic paid-API fallback. Local setup is optional; see [Capabilities](CAPABILITIES.md) for current quality.

Codex路线需要已安装CLI及其支持的ChatGPT账号登录；需明确程序位置时，在启动服务前设置 `RESEARCH_CODEX_EXE`。该后端不使用API Key。额度或网络错误保留来源及未完成状态，不自动转用付费API。本地环境为可选项，当前质量见[能力矩阵](CAPABILITIES.md)。

## Operating contract / 调用约定

Use user-selected sources and accounts, bounded requests, and the shared task lifecycle. Read original text for factual claims. Preserve `partial` and `auth_required` outcomes, and keep session secrets out of model input. Platform login and extension permissions remain user actions.

使用用户选定来源和账号、明确预算及共同任务生命周期；事实结论基于原文，保留 `partial` 和 `auth_required` 状态。会话秘密不进入模型输入；平台登录与扩展权限由用户操作。
