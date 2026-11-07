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

Connect an agent through the [Skill template](skill/research-crawler/SKILL.md), CLI commands, or the local HTTP API. The agent coordinates the research; Crawl_for_agent collects, stores, and returns the evidence. **Codex has live samples in the configured Windows environment. Claude Code and other agent harnesses are integration options through these interfaces; their end-to-end setup has not been validated.**

![Agent harness integration](docs/assets/agent-harness.png)

Users choose how to run research answers. In an agent conversation, model access follows the user's configuration in that tool. The application's `codex` backend uses Codex CLI with a ChatGPT-account login and its available allowance. Its `api` backend uses a separately configured API key and endpoint, with usage governed by that provider; it does not use a ChatGPT or Claude subscription login. There is no built-in Claude subscription backend.

New `research.answer` and `answer.model` tasks default to Codex. Users must explicitly select a saved profile for API calls; there is no automatic switch. The OpenAI-compatible and Anthropic API implementations have passed local simulated checks; real-provider calls have not been validated. See [Agent integration](docs/AGENT_INTEGRATION.md) and the [Model API guide](docs/MODEL_API.md).

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
