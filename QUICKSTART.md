# Moot quick start

Moot is the hiring and trust layer for agents. You chat with Moot about a job. It auditions candidate agents (Claude, GPT,
GLM and deterministic baselines) **side by side in sandboxes** on a hidden-answer benchmark, hires the best qualified one,
verifies the deliverable and recovers from a checkpoint if the agent fails mid-task.

It handles two task families:

| Family | Example | Why it's interesting |
|---|---|---|
| **Pitch deck** (multi-step) | "Make a seed pitch deck from this brief" | brief → plan → slides → self-check; objective gates and a cross-vendor judge; resumable handoff |
| **Financial extraction** | "Compare FY2025 operating margins…" | numbers verified against a reviewed answer key, with page-level citations |

## 1. Set up (once)

```bash
pip install -r requirements.txt
cp .env.example .env        # then put your keys in .env (not .env.example)
```

```bash
ANTHROPIC_API_KEY=sk-ant-...   # Claude agents (claude-opus-5-5)
OPENAI_API_KEY=sk-proj-...     # GPT agents (gpt-5)
ZAI_API_KEY=                   # GLM agents (glm-5.2), optional
```

## 2. Start

```bash
python3 -m moot.web            # → http://127.0.0.1:8787   (landing page; Deck Studio at /studio, the earlier workspace at /lab)
```

If the port is already in use, run `pkill -f moot.web` and start again.

**Waitlist QR code.** Add `--waitlist` to serve the sign-up form to phones on the same Wi-Fi (port 8788, which serves
only the form; the Studio stays on loopback). The landing page then shows a QR code for it, and **⛶ Present QR** shows it
full screen. If the venue Wi-Fi blocks device-to-device traffic, tunnel port 8788 (for example `ngrok http 8788`) and pass
`--public-url https://<tunnel-host>`. Sign-ups go to `runtime/waitlist.db`; download them from
http://127.0.0.1:8787/api/waitlist.csv on this machine.

## 3. Deck Studio: the demo

**The idea.** Moot's *main agent* hires a pitch-deck agent the way a person would: by running a paid work trial on
the real job and scoring every candidate against what the human actually wants.

| Step | Who | What you see |
|---|---|---|
| 1. Ask | Human | Type the deck you want and a **product link**, then send |
| 2. Questions | Main agent | Reads the product page and asks 4 product-specific questions (story focus, how to handle missing traction, call to action, visual direction) plus audience, slide limit and budget. Defaults are preselected |
| 3. Benchmark | Main agent | Your answers become a **weighted rubric** (each criterion quotes the answer it came from) plus Moot's **hard gates** (numbers traceable to sources, valid citations, required sections, slide limit, readable contrast) |
| 4. Candidate skills | Candidates | Claude Opus 5.5, GPT-5 and Qwen on Groq each **write their own skill** (a SKILL.md playbook: principles, workflow, design system, slide recipes, a quality checklist mapped to your benchmark, pitfalls) from your requirements, their design direction and the shared skill memory. Each **practises** it on one slide, which Moot checks objectively, then **refines** it to v2. See them under **Candidate skills** (download SKILL.md) |
| 5. Work trial | Candidates | Each builds a **3-slide demo deck** of its approach with the shared skills plus its own refined skill, and runs Moot's hard gates before submitting (one repair attempt). Each lane shows its conversation with the main agent, its sandbox log and its trial deck |
| 6. Scoring | Moot + judges | Gates are checked by Moot. A cross-vendor judge panel (Claude + GPT) scores each deck 1–5 per criterion; the scorecard fills in under the benchmark |
| 7. Hire | Main agent | Hires the top qualified score and sends it the judges' feedback plus what the judges liked in the other decks |
| 8. Final deck | Winner | Expands its 3-slide demo into the **full deck at your slide count** (default 8) using the judges' feedback, checks the hard gates before submitting, and is gated and scored again as the final deliverable. If it fails a gate, the job is handed to the runner-up. Rendered as a styled **.pptx** (download in the chat or under Final deck); the main agent then adds general lessons to the shared skill memory |

**Run it:** click **Try it: Moot's own deck** (it uses the sample page `https://moot.example/`, served offline), then
**Send to main agent**, then **Build the benchmark →**. A full run takes about 3 minutes and costs about $0.80.
For your own product, paste its real URL. Pages that render their content with JavaScript may give the agents little text,
so check the product summary the main agent shows before answering.

**Shared skill memory** lives in `data/skills/pitch-deck/` (narrative arc, slide writing, visual style system, integrity
rules, and `lessons.md`, which grows after each job). Every candidate loads it, and it is part of each agent's version
fingerprint, so editing a skill invalidates old trial evidence.

## 4. The Lab (`/lab`)

The earlier workspace stays available: financial extraction and pitch-deck auditions on fixed held-out
benchmarks, sandbox lanes, capability-memory leaderboard, and recovery from a checkpoint on simulated outages.

## 5. Your own inputs

- **Deck:** paste or upload a brief under **＋ Sources → Paste or upload**. Use headings `## Problem`, `## Solution`, `## Market`,
  `## Traction`, `## Business model`, `## Team`, `## Ask`, plus `Company:` and `One-liner:` lines.
- **Extraction:** a URL (fetched live and snapshotted) or pasted report text. Mark pages with `=== Page N ===`.
- Your own inputs have no hidden answer key, so results are *partially verified*: every objective check against the
  sources passed, but no reviewed truth exists.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Lane shows *needs key* / *key rejected* | Fix the key in **Keys & models** or `.env`, then restart |
| An agent re-auditions unexpectedly | Its model ID or code changed, so its version fingerprint changed and its old evidence no longer counts |
| Clean slate | Stop the server and delete `runtime/web.db`. This clears jobs, traces and capability memory |
| GLM fails | Check the model ID / base URL in **Keys & models**. In mainland China, use `https://open.bigmodel.cn/api/paas/v4/` |

Costs are **estimates** (token counts × per-token prices). Baseline agents use offline tool stand-ins.
Judge scores are qualitative opinions and are reported separately from the objective checks.

## Other entry points

```bash
python3 run_demo.py                                       # scripted Buyer Agent drives Moot over MCP (no API cost)
claude mcp add moot -- python3 -m moot.mcp_server --llm   # use Moot's tools from Claude Code
```

## Manufact MCP workflow

See [MANUFACT.md](MANUFACT.md) for the shared web/MCP server, Inspector, external agent demo, pinned external skills and Docker deployment.
