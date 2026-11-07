# Usage / 使用方式

[English overview](../README.md) · [中文概览](../README.zh-CN.md) · [Capabilities](CAPABILITIES.md) · [Agent integration](AGENT_INTEGRATION.md)

Start from a configured environment and use `status` to check available capabilities. Choose small page and record limits for the first task.

准备好环境后，用 `status` 查看可用能力。首次任务建议设置较小的页面与记录上限。

## Ask an agent / 在智能体中提出任务

> Find the official documentation for this behavior, save the relevant pages, and answer using quotations from the saved sources. Mark any inaccessible page or uncertain interpretation.

> 查找这项行为的官方文档，保存相关网页，再用原文引用回答。打不开的来源和不确定的解释分别说明。

> Search only the material already collected for this topic. Do not browse again. Show the source passages behind the conclusion.

> 只查这个主题已采集的资料，不重新联网。给出结论对应的原文片段。

Users can choose their agent tool's supported subscription or API connection, or configure a model API directly in the local workbench, to answer from saved sources. See [Agent integration](AGENT_INTEGRATION.md) and [Model API setup](MODEL_API.md).

用户可以选择智能体工具支持的账号订阅或 API，也可以直接在本机工作台配置模型 API，根据保存的来源回答。具体见[智能体接入](AGENT_INTEGRATION.md)与[API 配置](MODEL_API.md)。

## Collect first / 先采集原文

In the configured project directory:

```powershell
.\scripts\research.ps1 collect https://example.com --max-pages 1
```

For structured task parameters, save JSON to a local file and pass that path:

```json
{
  "urls": ["https://example.com"],
  "max_pages": 1,
  "max_seconds": 60
}
```

```powershell
.\scripts\research.ps1 run web.collect --params-file .\request.json
```

`example.com` is a syntax example, not a research source. Read the resulting task status, coverage and errors rather than treating a saved file as proof of complete collection.

`example.com` 仅演示语法，不是研究来源。查看任务状态、覆盖范围和错误，不能仅凭文件存在判断采集完整。

## Search and answer / 搜索后回答

Save this task description to a parameters file and run `research.answer`:

```json
{
  "query": "Python TaskGroup cancellation official documentation",
  "question": "Why should a coroutine normally propagate cancellation after cleanup?",
  "limit": 5,
  "fetch_top": 3,
  "max_seconds": 300
}
```

This example uses the default Codex account backend. To use a saved API profile, explicitly add `"answer_backend":"api"` and `"api_profile":"your-profile"`; saving a profile alone does not change the default. Search results are fetched before answering; missing sources or conditions produce partial coverage or an insufficient-evidence result. Inspect the recorded read ranges with the answer.

此示例使用默认的 Codex 账号后端。使用已保存的 API 配置时，须明确添加 `"answer_backend":"api"` 和 `"api_profile":"配置别名"`；仅保存配置不会改变默认方式。搜索后采集原文再回答；来源或关键条件缺失时保留部分覆盖或证据不足。查看回答时一并核对实际读取范围。

## Choose a session deliberately / 明确选择会话

**Normal login** opens the selected platform so you can log in and save that session. It does not require the extension. **Extension import** reads the supported cookie subset from a deliberately selected site tab after pairing and a permission gesture. They are alternative account-preparation routes, not two mandatory steps.

**登录并保存会话**：打开所选平台，由用户正常登录并保存，不需要扩展。**通过 Chrome 扩展导入**：配对后，由用户授权读取明确选定站点页的受支持 Cookie 子集。这是两种账号准备方式，不是必须连续操作的两步。

For extension import, select the intended destination account, generate a local pairing code, open the extension from the actual platform tab, and follow its persistent authorization page. Verify the destination before import: it replaces that selected account's saved state. Never put session cookies or pairing tokens in task descriptions, source files or issue reports.

扩展导入前选择目标账号并生成本机配对码，从真实平台页点击扩展，再在持久授权页操作。先核对目标：导入会替换该账号保存的状态。不要把 Cookie、配对令牌写进任务说明、源码或问题反馈。

## Start with a bounded platform request / 从有限平台任务开始

Ask your agent for a small Xiaohongshu search and comment sample:

> Use my selected Xiaohongshu account to search for museum visits. Collect at most2 posts, at most3 root comments per post and one page. Keep the post links, original text and coverage status.

> 使用我选定的小红书账号搜索“博物馆”，最多2篇帖子，每篇最多3条根评论、最多1页。保存帖子链接、原文及覆盖状态。

Use this object with `platform.collect`:

```json
{"platform":"xhs","account":"your-saved-account","mode":"search","keywords":["博物馆"],"max_posts":2,"max_comments":3,"max_replies":0,"max_pages":1,"max_seconds":120}
```

For one known post with root comments:

> Read this Xiaohongshu post with my selected account: [full link]. Save at most20 root comments across at most2 pages; keep any remaining comment coverage explicit.

> 用我选定的账号读取这篇小红书帖子：[完整链接]。最多20条根评论、最多2页，未读部分明确标出。

```json
{
  "platform": "xhs",
  "account": "your-saved-account",
  "mode": "detail",
  "ids": ["YOUR_AUTHORIZED_NOTE_URL"],
  "max_posts": 1,
  "max_comments": 20,
  "max_replies": 0,
  "max_pages": 2,
  "max_seconds": 120,
  "signing_backend": "local"
}
```

Run it with `platform.collect` and replace the account and source placeholders. Keep the full post URL, including access parameters. `signing_backend:"service"` selects the separately configured local signing component; the default is local signing. A login challenge pauses access for normal user verification.

使用 `platform.collect` 执行，替换账号与来源占位内容，保留完整帖链及访问参数。`service` 选择已配置的本机签名组件，默认直接本地签名；遇登录验证时由本人正常完成。

## Inspect, resume, export / 查看、恢复与导出

- Read `partial`, `auth_required`, and errors as meaningful outcomes. Keep the records already obtained.
- Resume a compatible task to reuse saved source versions and eligible reviewed answers. Raising a record limit does not remove authentication or network requirements.
- Export only the selected task's records. Optional remote database export writes to the destination you explicitly configure.
- Treat model labels, semantic ranks and image descriptions as derived analysis. Keep original text and media available for review.

认真处理部分完成、需要登录和错误状态，保留已取得记录。恢复可复用兼容的来源版本与已审核回答；提高数量上限不会消除账号或网络条件。导出仅针对所选任务，数据库写入必须选择已配置目标。模型标签、相似度和图片描述是派生分析，应保留原始材料供回查。

See [Capabilities](CAPABILITIES.md) for current platform and media validation, and [License](../LICENSE.md) for component use and redistribution terms.

当前平台与媒体验证范围见[能力矩阵](CAPABILITIES.md)，组件使用和再分发条件见[许可](../LICENSE.md)。
