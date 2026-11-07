# Usage / 使用方式

[English overview](../README.md) · [中文概览](../README.zh-CN.md) · [Capabilities](CAPABILITIES.md)

These examples assume the required environment has already been configured. They are task patterns, not fresh-install instructions or new live-test results. Optional modules should be checked with `status` before use.

以下示例以环境已配置为前提，是任务写法，不是全新安装教程或新增实测结果。可选模块先通过 `status` 确认可用。

## Ask an agent / 在智能体中提出任务

> Find the official documentation for this behavior, save the relevant pages, and answer using quotations from the saved sources. Mark any inaccessible page or uncertain interpretation.

> 查找这项行为的官方文档，保存相关网页，再用原文引用回答。打不开的来源和不确定的解释分别说明。

> Search only the material already collected for this topic. Do not browse again. Show the source passages behind the conclusion.

> 只查这个主题已采集的资料，不重新联网。给出结论对应的原文片段。

The skill can use the host conversation's available search and reasoning tools. Standalone worktable tasks use the configured application backends; these are different execution paths and should not be billed or described as identical.

技能可使用当前对话已有搜索与推理能力；独立工作台使用已配置的程序后端。两条执行路径不同，不能把宿主能力描述为独立内置 API。

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
  "answer_backend": "codex",
  "evidence_selection": "context",
  "limit": 5,
  "fetch_top": 3,
  "max_seconds": 300
}
```

The Codex account must already be usable. Search results are fetched before answering; a provider failure or missing condition can legitimately produce a partial answer or insufficient-evidence result. Default source-context budgets are finite, so inspect the recorded read coverage.

须已有可用 Codex 账号。先采集来源再回答；搜索失败或关键条件缺失时，合理结果可能是部分回答或证据不足。来源上下文预算有限，应查看实际读取范围。

## Choose a session deliberately / 明确选择会话

**Normal login** opens the selected platform so you can log in and save that session. It does not require the extension. **Extension import** reads the supported cookie subset from a deliberately selected site tab after pairing and a permission gesture. They are alternative account-preparation routes, not two mandatory steps.

**登录并保存会话**：打开所选平台，由用户正常登录并保存，不需要扩展。**通过 Chrome 扩展导入**：配对后，由用户授权读取明确选定站点页的受支持 Cookie 子集。这是两种账号准备方式，不是必须连续操作的两步。

For extension import, select the intended destination account, generate a local pairing code, open the extension from the actual platform tab, and follow its persistent authorization page. Verify the destination before import: it replaces that selected account's saved state. Never put session cookies or pairing tokens in task descriptions, source files or issue reports.

扩展导入前选择目标账号并生成本机配对码，从真实平台页点击扩展，再在持久授权页操作。先核对目标：导入会替换该账号保存的状态。不要把 Cookie、配对令牌写进任务说明、源码或问题反馈。

## Start with a bounded platform request / 从有限平台任务开始

For a source you are authorized to access, use one known note and no comments:

```json
{
  "platform": "xhs",
  "account": "your-saved-account",
  "mode": "detail",
  "ids": ["YOUR_AUTHORIZED_NOTE_URL"],
  "max_items": 1,
  "max_comments": 0,
  "max_pages": 1,
  "max_seconds": 60,
  "signing_backend": "local"
}
```

Run it with `platform.collect`. Replace both placeholders with your own selected account and source. `signing_backend: "service"` is optional and requires the separately configured local signing component. An existing account, successful signature calculation, or HTTP200 alone does not certify login validity or successful content extraction.

使用 `platform.collect` 执行，替换账号与来源占位内容。可选 `service` 签名需先配置本机签名组件。账号存在、签名算出或 HTTP200 本身都不等于登录有效或正文采集成功。

## Inspect, resume, export / 查看、恢复与导出

- Read `partial`, `auth_required`, and errors as meaningful outcomes. Keep the records already obtained.
- Resume a compatible task to reuse saved source versions and eligible reviewed answers. Raising a record limit does not remove authentication or network requirements.
- Export only the selected task's records. Optional remote database export writes to the destination you explicitly configure.
- Treat model labels, semantic ranks and image descriptions as derived analysis. Keep original text and media available for review.

认真处理部分完成、需要登录和错误状态，保留已取得记录。恢复可复用兼容的来源版本与已审核回答；提高数量上限不会消除账号或网络条件。导出仅针对所选任务，数据库写入必须选择已配置目标。模型标签、相似度和图片描述是派生分析，应保留原始材料供回查。

No example here establishes full upstream or Pro equivalence, complete platform coverage, or an unrestricted commercial-use license. Review the [README licensing section](../README.md#relationship-to-upstream-and-licensing) and component notices before redistribution.
