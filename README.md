# Crawl_for_agent

**From the open web to answers you can trace.**

[English](README.md) · [简体中文](README.zh-CN.md) · [Get started](docs/GETTING_STARTED.md) · [Architecture](docs/ARCHITECTURE.md)

![Crawl_for_agent](docs/assets/hero.png)

Crawl_for_agent is an independently engineered research application for agents. Its first working version was built over **two days and one night**, starting with a new project and a unified architecture for collecting sources, managing sessions, retaining evidence, and producing answers.

One task system connects web pages, platform content, search, and analysis. Run it through an agent, the local worktable, or the command line; collected material stays available for the next question.

## Core capabilities

| Capability | What you can do |
|---|---|
| Web collection | Capture HTML and Markdown through HTTP or a browser, extract structured fields, explore sites within a budget, and save screenshots |
| Platform research | Work with adapters for Xiaohongshu, Douyin, Kuaishou, Bilibili, Weibo, Tieba, and Zhihu, with platform-specific search, detail, creator, and comment operations |
| Search and answers | Discover sources online, search collected material, optionally retrieve across languages, and answer with quotations linked to saved evidence |
| Sessions and signing | Select saved accounts, log in normally or import a selected site's session, pair accounts with supplied proxies, and choose supported local or service signing |
| Media and analysis | Download media, use optional OCR, transcription and vision, and analyze collected comments |
| Research output | Keep versioned records, resume tasks, build reports, and export to files or configured databases |

![Platform research](docs/assets/platform-research.png)

## A connected research workflow

**Discover → Collect → Read → Answer → Reuse**

Search identifies candidate sources. Collection saves the actual material and its provenance. Answers select quotations from those records and review the support for each claim. The resulting research library can be searched, exported, or used in a follow-up task.

![Evidence workflow](docs/assets/evidence-flow.png)

Tasks share budgets, checkpoints, and resource management. If a source is unavailable or a task reaches its limit, the application retains the material and reviewed conclusions already obtained, with their coverage status.

## Built for Agent Harnesses

Use Crawl_for_agent with **Codex, Claude Code, and other agent harnesses** through a [Skill](skill/research-crawler/SKILL.md), CLI commands, or the local API. The agent coordinates the research; Crawl_for_agent collects, stores, and returns the evidence.

![Agent harness integration](docs/assets/agent-harness.png)

Users choose **a model API or their account's subscription allowance** for research answers. Use the account or API configuration supported by Codex, Claude Code, or another agent tool, or configure an OpenAI-compatible endpoint or Anthropic API directly in this project's workbench. The application can then generate and review answers against collected sources. Subscription allowances and API billing follow the selected service's separate rules.

New application answer tasks use the Codex account by default. API calls require the user's explicit selection of a saved configuration; there is no automatic switch. See [Agent integration](docs/AGENT_INTEGRATION.md) and the [Model API guide](docs/MODEL_API.md).

## Ask naturally

> Find the official documentation for this behavior, save the relevant pages, and explain it with quotations from the sources.

> Collect the selected creator's recent posts and comments. Summarize recurring questions and link each finding to the original material.

> Use only the research I have already collected. Compare the competing explanations and show where the evidence is incomplete.

See [Usage](docs/USAGE.md) for task parameters and examples.

## Environment setup

Prepare the Windows environment, Python dependencies, Node.js, and the browser components needed for your tasks using [Getting started](docs/GETTING_STARTED.md). Configure an agent host or standalone answer backend for model-assisted work; add local models only for the optional features you choose.

From the configured project directory:

```powershell
.\scripts\research.ps1 status
.\scripts\start-research.ps1
```

Open the worktable at `http://127.0.0.1:8946`.

## Capability status

The [capability matrix](docs/CAPABILITIES.md) separates implemented features, local checks, and live platform samples. It covers the authenticated Xiaohongshu workflow, collection and answer validation, and the remaining platform-specific work.

For the shared task system and evidence flow, read [Architecture](docs/ARCHITECTURE.md).

## License

[License](LICENSE.md)
