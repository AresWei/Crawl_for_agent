# Model API / 模型 API

[Agent integration](AGENT_INTEGRATION.md) · [Usage](USAGE.md)

Users can connect an **OpenAI-compatible Chat Completions API** or **Anthropic Messages API** to the application's research and saved-material answer tasks. The default remains the existing Codex account. API access is a separate user choice with the selected provider's own billing.

用户可以为程序的搜索研究和已存资料回答接入 **OpenAI 兼容 Chat Completions API** 或 **Anthropic Messages API**。默认仍使用已有 Codex 账号；API 是用户单独选择的方式，按所选服务的规则计费。

## Configure once / 配置一次

1. Open the local workbench and expand **模型 API 配置**.
2. Enter a profile name, protocol, API base URL, model identifier, and API key. Use the base URL documented by your provider, including any gateway path. Standard examples are `https://api.openai.com/v1` and `https://api.anthropic.com/v1`.
3. Save the configuration. Saving and listing profiles make no model requests.
4. Choose a research or saved-material answer task, select **模型 API**, and select the saved profile before submitting.

1. 打开本机工作台，展开 **模型 API 配置**。
2. 填写配置别名、协议、接口基础地址、模型标识和密钥。基础地址以提供方文档为准，保留需要的网关路径；标准示例见上方。
3. 保存配置。保存和查看配置不会请求模型。
4. 选择研究回答或已存资料回答任务，将回答方式切换到 **模型 API**，选择已保存配置，再提交任务。

Selecting API sends the question and selected source text to the configured provider for generation and claim review. A task can make several model requests for drafting, review, and eligible repair. Request and token metadata are recorded where available; they are not a billing receipt. Review the destination and task budget before submitting.

选择 API 后，问题和选定的来源正文会发送到配置的提供方，用于生成与逐条审核。一次任务可能包含初稿、审核及有限修正等多次模型请求。程序保存可取得的请求和 token 信息，这些记录不等于账单；提交前确认接口目标与任务预算。

## Job parameters / 任务参数

Use a named profile in task parameters; never put an API key in a job file or chat message.

任务参数只填写配置别名，不把 API 密钥放入任务文件或聊天消息。

```json
{
  "query": "Python TaskGroup cancellation official documentation",
  "question": "Why should cancellation normally propagate after cleanup?",
  "answer_backend": "api",
  "api_profile": "my-model",
  "evidence_selection": "context",
  "source_context_chars": 64000,
  "limit": 5,
  "fetch_top": 3,
  "max_seconds": 300
}
```

Submit with `research.answer`. For `answer.model`, provide `query`, `question`, `answer_backend`, `api_profile`, and the desired retrieval limits instead. These two answer routes support the API backend; comment classification and optional local media workers keep their existing settings.

以上参数用于 `research.answer`。`answer.model` 使用 `query`、`question`、`answer_backend`、`api_profile` 与所需检索上限即可。这两个回答入口支持 API；评论分类和可选本地媒体处理继续使用原有配置。

API research uses at most 64,000 source characters, leaving room for instructions and review. Model context limits still vary by provider. Truncation, invalid JSON, and failed quotation checks remain incomplete outcomes with saved evidence. No backend switch or provider retry occurs automatically.

API 研究的来源正文最多 64,000 字符，为指令和审核预留空间；模型实际上下文上限仍由提供方决定。输出截断、JSON 格式或引用校验失败时保留原文和未完成状态，不自动切换后端或重试提供方。

## Local configuration interface / 本机配置接口

All requests use the existing local `X-Research-Token` authentication described in [Agent integration](AGENT_INTEGRATION.md). Keep the service on loopback. External model endpoints require HTTPS; HTTP is allowed only for loopback services.

所有请求沿用[接入指南](AGENT_INTEGRATION.md)中的本机 `X-Research-Token` 鉴权，服务保持本机访问。外部模型接口使用 HTTPS；仅回环服务允许 HTTP。

| Request | Purpose / 用途 |
|---|---|
| `GET /api/model-profiles` | List safe profile metadata / 查看不含密钥的配置摘要 |
| `PUT /api/model-profiles/{name}` | Save `provider`, `base_url`, `model`, `api_key` in a private request body / 通过私密请求体保存配置 |
| `DELETE /api/model-profiles/{name}` | Delete the selected local profile / 删除选定的本机配置 |

`provider` is `openai` or `anthropic`. Configuration responses never return the key. Windows stores profiles under `state/model-api` using the current Windows account's DPAPI protection; other systems use owner-only file permissions. Keep this private state out of source control and backups intended for publication. Replacing a profile changes its cache identity; active tasks use their already loaded configuration snapshot.

`provider` 为 `openai` 或 `anthropic`，配置响应不返回密钥。Windows 在 `state/model-api` 使用当前 Windows 账号的 DPAPI 保护；其他系统使用仅文件所有者可读写的权限。私密配置不进入源码仓库或公开备份。更新配置会改变回答缓存身份；运行中的任务继续使用已加载的配置快照。

Protocol and failure handling are checked with local simulated endpoints. Using a real provider still requires the user's own endpoint, model, and credentials; provider acceptance and billing are not inferred from local checks.

协议和失败处理使用本机模拟接口验证。真实提供方仍需用户自己的接口、模型和凭据；本地验证不代表真实服务已验收或已发生计费。
