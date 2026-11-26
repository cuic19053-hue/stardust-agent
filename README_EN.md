# Student Application Materials Workbench

<img align="right" src="./assets/logo.png" width="72" height="72" alt="Stardust Agent">

**Stardust Agent** is an open source AI agent workbench for student research proposals, applications, business plans, social practice reports and personal statements. Students organize calls for applications and research notes in a project, ask the agent to find evidence, edit sections and export a Word draft. The Python and FastAPI backend supports local Ollama models and APIs compatible with OpenAI Chat Completions.

[Open the workbench](https://agent.stardust.website) · [Explore the Qwen fine-tuning demo](https://agent.stardust.website/student-intake) · [Download a sample Word proposal](./examples/demos/demo_innovation_research.docx) · [Agent design notes (Chinese)](./docs/design/AGENT_REFACTOR.md) · [简体中文](./README.md)

[Download the desktop client](https://github.com/cuic19053-hue/stardust-agent/releases/latest) · [Desktop guide](./desktop/README.md)

[![GitHub stars](https://img.shields.io/github/stars/cuic19053-hue/stardust-agent?style=social)](https://github.com/cuic19053-hue/stardust-agent/stargazers) [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE) [![Python 3.12+](https://img.shields.io/badge/Python-3.12+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/) [![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/) [![MCP Protocol](https://img.shields.io/badge/MCP-Protocol-blue.svg)](https://modelcontextprotocol.io/) [![Online Platform](https://img.shields.io/badge/Online-agent.stardust.website-7952CC.svg)](https://agent.stardust.website)

The 35-scenario catalog describes domain guidance. Drafting, templates, charts and export support vary, and not every scenario has completed end-to-end delivery validation. Repository changes do not establish that the live site has been updated.

User confirmation records information the student has supplied or accepted; it is not independent verification. Numerical, source and completeness checks cover selected cases. Models can still omit, misread or invent claims, so applicants must check the facts, citations and current school requirements.

## Turn a student's account into a structured record

Describe your grades, experience or plans in the project chat, then choose **事实矩阵 → 学生口述档案 → 提取本项目口述** to review what the workbench found. Live extraction defaults to DeepSeek V4 Flash and uses workbench credits. The extractor supports 41 fields and shows your exact words and the chat turn for each suggestion. You can confirm individual self-reports, estimates, plans and explicit negative statements. The saved record preserves that status. Unknowns, refusals and unresolved conflicts stay in review.

For example, if you say “我的专业是文物保护” (“My major is cultural heritage conservation”), the workbench checks both the field and the value against that sentence. When it can trace two conflicting budgets for one project, it shows both quotes and asks which amount applies. If part of a response needs manual review, you can still confirm the other entries that passed the checks.

**Run the fine-tuned 0.5B model on your device.** Open the browser extractor and choose “加载端侧模型” to download roughly 320 MB on first use. It requires WebGPU with half-precision support. Your transcript, inference and quote checks stay on the device, and the tool uses no workbench credits. The reviewer can recover an explicitly stated major or planned deliverable from the speaker’s own words. It omits household income for National Scholarship applications. Check each suggestion before copying its status and source. [Open on-device extraction](https://agent.stardust.website/student-intake/local) · [Model files and limitations](./server/static/intake-0p5b-v1/MODEL.md)

The free public demo replays Qwen2.5 7B's actual answers to five synthetic conversations before and after fine-tuning. No sign-in is required. In the “两份预算” (two budgets) example, the tuned model extracted only 8,400 yuan. The source check recovered the 4,800 yuan figure from another turn, kept both quotes and flagged the budget for clarification. [Explore the replay and results (Chinese)](https://agent.stardust.website/student-intake)

On a separate set of 20 held-out synthetic conversations, LoRA raised field F1 from 13.6% to 55.07% with the same MLX base model and source checks. Before tuning, 2 of 20 conversations passed all checks. After tuning, 3 passed. The historical Ollama configuration passed 8 of 20 with the same checks. The original 24 cases served only as development regression tests. [Training and evaluation notes (Chinese)](./server/evals/student_intake_pilot/README.md)

## Follow a tool call

> Check my confirmed materials. What was the sample size, and what did we measure?

The evaluation project contains one confirmed observation log. In a local run, Qwen2.5 7B selected `search_project_materials`. The server searched the current project and returned the matching passage to the model. A translated excerpt from the reply follows.

> According to the confirmed observation log, the team observed 12 tomato seedlings and recorded soil moisture and lesion area.

The evaluation checks the tool trace as well as the answer. It fails if the model introduces a claim of 92% accuracy, which does not appear in the log. [Fixture and expected behavior](./server/evals/cases.json) · [Evaluation runner](./server/evals/run.py)

| Engineering question | How the workbench handles it | Source |
| --- | --- | --- |
| How does a model choose tools and finish a turn? | Streaming and synchronous replies share a loop with up to six iterations and a budget of eight tool calls per turn. The server checks the grant again at invocation and binds tools to the current project. | [Runtime](./server/proposal/chat_runtime.py) · [Tool router](./server/proposal/chat_tools.py) |
| Where does evidence come from? | User confirmation makes project documents eligible for indexing. The RAG pipeline combines keyword matches with compatible vectors when available and returns the source file and passage location. Official notices use school, year and applicability filters. | [Project retrieval](./server/proposal/retrieval.py) · [Knowledge coverage](./docs/rag-status.md) |
| How does a conversation become a document? | Students preview candidate sections in the chat, select and edit the text, then save it. Word export reads the saved sections so students can check the draft against the current school template. | [Chat drafts](./server/proposal/chat_drafts.py) · [Word exporter](./server/proposal/exporter.py) |
| How do long jobs handle interruptions? | When enabled, the material worker queues jobs such as section generation and export. Submissions use idempotency keys and writes check the project revision. A paid job with an expired lease is held for review. Ordinary chat remains request-bound; reconnecting and cross-device deduplication are not fully integrated. | [Job queue](./server/proposal/material_jobs.py) · [Worker](./server/material_worker.py) |

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). The commands below use Python 3.12 and the locked runtime and test dependencies.

```bash
git clone https://github.com/cuic19053-hue/stardust-agent.git
cd stardust-agent
uv python install
uv sync --locked --extra full --extra review --group test
uv run --no-sync uvicorn server.app:app --host 127.0.0.1 --port 8765
```

Open the local workbench to organize your materials. For local AI, start Ollama, run `ollama pull qwen2.5:7b` and `ollama pull bge-m3`, then select Ollama in the workbench model settings. Upload a source document and confirm its text before asking the agent to retrieve evidence from it. Local mode uses SQLite and stores data in `.agent-data/`. Cloud mode uses Supabase to scope projects to each account. Hosted model calls use trial allowances and workbench credits. When you bring your own API key, your provider's pricing applies. Local Ollama does not consume workbench credits and requires your own runtime. [Local workbench](http://127.0.0.1:8765) · [Model and storage configuration](./server/README.md)

In the current repository, a basic national-scholarship starter can avoid model calls, while explicit length, style or experience-focused requests use the selected model. Live availability depends on the deployed version.

Run the evaluation from the repository root to reproduce the example above. It creates temporary projects and reports tool calls, answer excerpts and results for each case.

```bash
uv run --no-sync python server/evals/run.py
```

## Domain guides and integrations

The workbench loads filtered guidance for the selected scenario and can progressively read linked reference files. For example, `subskills/innovation-research/SKILL.md` describes the structure of an undergraduate innovation proposal. These guides are not official rules or student facts. Sample Word files use example data and illustrate layouts; they are not approved real student submissions. Chart generation requires a supported builder, valid image or graph data and rendering dependencies. Missing inputs or failed rendering can leave a placeholder. [Domain catalog](./subskills/README.md) · [Challenge Cup sample](./examples/demos/demo_challenge_cup.docx) · [Scholarship sample](./examples/demos/demo_national_scholarship.docx) · [All Word examples](./examples/demos/)

The repository's public corpus includes **42 formal university notices and one college-level innovation activity report**. Retrieval must match the school, year and applicable audience, and users must check the original source. **Zero real example submissions have completed authorization, de-identification and approval for inclusion.** See [RAG status (Chinese)](./docs/rag-status.md).

Codex, Cursor and other MCP clients can use the local project tools over stdio. Run `uv run --no-sync python server/mcp_server.py --transport stdio` to share the workbench's project store. Developers can also load individual Skills in a compatible IDE. [Project MCP server](./server/mcp_server.py) · [Skills entry point](./SKILL.md)

## Credits and license

The project grew out of the team's work preparing Challenge Cup proposals. Special thanks to [@richyhu](https://github.com/richyhu), who wrote most of the project. Contributions covering school templates, changes to application requirements and reproducible issues are welcome. [Demo video](https://www.douyin.com/video/7692700167341985051) · [Contributing](./CONTRIBUTING.md) · [MIT license](./LICENSE)
