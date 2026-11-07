# Source and publication scope / 来源与发布范围

This source preview was prepared on 2026-11-07 from the locally integrated Research Crawler project. The public repository name is **Crawl_for_agent**; the Python package, CLI command and Skill keep the existing **research-crawler** identity.

## Source pins

| Component | Frozen revision | Records |
|---|---|---|
| Crawl4AI | `838869bdda93f25e6d50b3f50ae9577d9b19a586` (v0.9.5) | [Freeze record](../source-freeze-crawl4ai.json), [license](../licenses/crawl4ai/LICENSE) |
| MediaCrawler | `54a3d92412f4b20c06b45f1d542b78dd92049d6d` | [Freeze record](../source-freeze-mediacrawler.json), [license](../licenses/mediacrawler/LICENSE) |

The freeze records describe upstream acquisition, including historical archive locations. Archives and complete upstream checkouts are not bundled. Incorporated code and its source manifests are retained under `src/`.

## Included

- Integrated Python application and required frontend/JavaScript/GraphQL assets.
- Selected runtime entry scripts, scoped Chrome extension, and a portable Skill template.
- English-first documentation, Chinese translation, capability boundaries and three generated concept illustrations.
- Upstream licenses, attribution and source provenance.

## Deliberately excluded

Accounts, cookies, API tokens, proxy credentials, browser profiles, local databases, private collected posts/comments, task histories, raw acceptance artifacts, private audit notes, runtime executables, caches, model weights, downloaded upstream archives, and temporary diagnostics.

This publication does not rerun platform tasks or transfer the maintainer's authenticated accounts to readers. Validation statements summarize bounded prior work on a configured Windows installation. They are not a guarantee that a fresh clone has every optional worker or model installed. The private test collection and account-bearing acceptance records are not bundled.

## Packaging-only difference

`intelligence/codex_model.py` resolves an explicitly supplied executable first, then `RESEARCH_CODEX_EXE`, then the system PATH, instead of a personal machine path. No feature expansion was performed for publication.

## 中文

本仓库为 2026-11-07 整理的公开源码预览。仓库名为 Crawl_for_agent，内部包、CLI 与 Skill 名保留 research-crawler。完整上游归档只存在于开发工作区；公开仓库保留实际集成源码、来源清单、许可和署名。

账号、Cookie、密钥、代理凭据、浏览器资料、采集数据、任务历史、私人审计、运行时、模型权重、缓存与临时诊断均未发布。已有实测来自配置完成的 Windows 安装，不代表首次克隆已具备所有可选依赖。测试原始资料没有随仓库分发。发布时仅将专属 Codex 路径改为可配置/PATH 发现，不扩展功能。

