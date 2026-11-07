# Preparing a Windows checkout / Windows 环境准备

[English README](../README.md) · [中文说明](../README.zh-CN.md)

Prepare the core Python environment and browser first, then add optional workers as needed. Windows is the current validated execution environment. The commands below describe a new checkout; published validation comes from an existing configured installation. Runtime assets, model weights and account sessions are prepared locally.

## Prerequisites

- Windows with an existing Python 3.12+ installation and PowerShell.
- Git for cloning; Node.js on PATH for the Node-backed signing paths.
- A configured agent host or optional application answer backend for model-assisted work; see [Agent integration](AGENT_INTEGRATION.md).
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

The workbench binds to [127.0.0.1:8946](http://127.0.0.1:8946/). Keep it local. These commands install the core dependencies; database drivers, additional browser providers and media/model workers are optional components. Check `status` for availability and use `doctor` for read-only setup diagnostics.

For a copied Skill or external local tool, configure the checkout location in its process environment:

```powershell
$env:RESEARCH_CRAWLER_HOME = (Get-Location).Path
```

Host model connections and the optional application answer backend are configured in [Agent integration](AGENT_INTEGRATION.md).

## Next setup choices

1. Use [the examples](USAGE.md) for a bounded webpage collection.
2. For agent access, use the [Skill template](../skill/research-crawler/SKILL.md) and explicitly give your agent the actual checkout path. Skill installation/discovery depends on the host's supported configuration; cloning alone does not install it.
3. Use the workbench's normal-login flow for platform accounts. Only use the [Chrome bridge](../bridge-extension/README.md) when you deliberately want to import a selected site's existing browser session.
4. Read [capability boundaries](CAPABILITIES.md) before optional media, local models or advanced platform work.

The optional signing component starts with `scripts/start-signing-service.ps1` on loopback port8950; Kuaishou retains its browser signer. Model workers need their own binaries, manifests and weights. Declared `databases` and `browser` extras install additional database drivers and Patchright respectively. Linux execution awaits a suitable validation environment.

## 中文

先准备Python环境与采集所需浏览器，再按需增加可选worker。当前执行证据来自既有Windows配置；以上命令说明新目录的准备方式。Python和按需使用的Node.js须预先具备，模型、运行资源与账号状态在本机准备。

示例使用F盘，可换成自己的目录。创建项目虚拟环境、安装核心依赖和Chromium后启动本机工作台；可选媒体／模型需对应依赖与资源。用 `status` 查看可用能力，用 `doctor` 作只读检查。宿主模型及独立回答后端配置见[智能体接入](AGENT_INTEGRATION.md)。

复制 Skill 模板后，要向你的 Codex 明确实际仓库位置；仅克隆仓库不等于自动安装技能。正常平台登录不需要桥接扩展；只有主动导入已有 Chrome 会话才使用扩展。不要共享 Cookie、验证码或本地服务令牌。

