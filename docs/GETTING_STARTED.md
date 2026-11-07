# Preparing a Windows checkout / Windows 环境准备

[English README](../README.md) · [中文说明](../README.zh-CN.md)

This is a **source preview**, not a bundled installer. The existing configured Windows workspace has bounded live evidence; the commands below describe source setup and were not rerun as a clean-room installation for this publication. Model weights, runtimes, account sessions and private validation data are omitted.

## Prerequisites

- Windows with an existing Python 3.12+ installation and PowerShell.
- Git for cloning; Node.js on PATH for the Node-backed signing paths.
- An already installed, signed-in Codex CLI if you want account-backed answers. No API key is required by that answer route. Available quota and network access still matter.
- Your own platform account only when a task needs login.

## Core environment

Choose a checkout on the drive where you want its controllable runtime, cache and state stored. Example:

```powershell
git clone https://github.com/AresWei/Crawl_for_agent.git F:\Crawl_for_agent
Set-Location F:\Crawl_for_agent
. .\scripts\runtime-env.ps1
py -3.12 -m venv .\runtime\venv
& .\runtime\venv\Scripts\python.exe -m pip install -e .
& .\runtime\venv\Scripts\python.exe -m playwright install chromium
.\scripts\research.ps1 status
.\scripts\start-research.ps1
```

The workbench binds to [127.0.0.1:8946](http://127.0.0.1:8946/). It is a local interface; do not expose it as a public web service. These commands install the core declared dependencies, not all optional media/model workers. A `dependency_missing` or unavailable optional model remains a setup gap, not a successful capability.

If Codex is not on PATH, set the actual executable path for the current PowerShell process before starting the workbench:

```powershell
$env:RESEARCH_CODEX_EXE = 'C:\path\to\codex.exe'
```

This selects an existing installation; it does not install Codex, copy login credentials, or change account settings. API-key-only authentication is not a substitute for the configured account-backed route.

## First use

1. Use [the examples](USAGE.md) for a bounded webpage collection.
2. For agent access, use the [Skill template](../skill/research-crawler/SKILL.md) and explicitly give your agent the actual checkout path. Skill installation/discovery depends on the host's supported configuration; cloning alone does not install it.
3. Use the workbench's normal-login flow for platform accounts. Only use the [Chrome bridge](../bridge-extension/README.md) when you deliberately want to import a selected site's existing browser session.
4. Read [capability boundaries](CAPABILITIES.md) before optional media, local models or advanced platform work.

The optional local signing component starts with `scripts/start-signing-service.ps1` and listens on loopback port 8950. It does not make every platform independent of a browser. Model worker scripts are included as source; their binaries, manifests and weights need separate setup. This release does not auto-download all models or claim complete Linux/Docker portability.

## 中文

这是源码预览，不是免配置安装包。上述命令说明如何在 Windows 准备核心环境，本次发布没有重复执行全新安装测试。Python、Node.js、Codex CLI 等前提需自行具备；模型权重、私有账号和运行资料不随仓库提供。

示例使用 F 盘。先创建项目虚拟环境、安装声明的核心依赖和 Chromium，再启动本地工作台。可选媒体/模型能力需要对应独立依赖和资源，不应把缺失状态当成功。Codex 回答使用已有登录账号和额度，不需要另购 API，但并非离线或无限免费。

复制 Skill 模板后，要向你的 Codex 明确实际仓库位置；仅克隆仓库不等于自动安装技能。正常平台登录不需要桥接扩展；只有主动导入已有 Chrome 会话才使用扩展。不要共享 Cookie、验证码或本地服务令牌。

