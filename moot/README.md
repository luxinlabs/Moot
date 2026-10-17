# Moot — hackathon MVP

Implements `../Moot_System_Design_and_Workflow.md` end to end. A Buyer Agent drives Moot **only through MCP
tools/call** and goes from job creation through clarification, candidate matching, auditions, hiring decision,
execution on unseen documents, independent verification, recovery after a failure and capability memory, then
a follow-up job that reuses that memory.

```bash
python3 run_demo.py              # full demo (fresh memory, injected provider fault on Job 1)
python3 run_demo.py --no-fault   # same, without the fault
python3 run_demo.py --keep-memory  # keep capability memory from previous runs (Job 1 then skips auditions)
```

Python 3.10+, stdlib only.

## Web UI

```bash
pip install flask anthropic openai   # already present on this machine
python3 -m moot.web                  # http://127.0.0.1:8787
```

You type a task in plain language and pick sources: the sample documents, a URL (fetched live and snapshotted), or a pasted or
uploaded text document. Then you choose which candidate agents compete and set the constraints. Moot then:

1. reads the task with an intake agent (the first working LLM, or a keyword parser) and asks only for what's missing;
2. auditions the selected agents **in parallel** on held-out fixtures, and the pass/fail matrix fills in live;
3. ranks them with hard gates and a weighted score, and asks you to approve if the spend estimate is above the auto-approve limit;
4. executes, verifies each deliverable, and recovers through handoff when **Simulate an outage** is ticked;
5. shows the verified table with source quotes, and records the outcome in capability memory (`runtime/web.db`).

**Agents.** There are three deterministic baselines (Document Analyst, Site Miner, Web Scout) and three LLM agents:

- **Claude Analyst:** `claude-opus-5-5` through the Anthropic SDK, with structured JSON output and `fallbacks: "default"` refusal fallback.
- **GPT Analyst:** `gpt-5` through the OpenAI SDK.
- **GLM Analyst:** `glm-5.2` through Z.ai's OpenAI-compatible endpoint.

All three LLM agents get the same document reader and prompt, so an audition compares the models. Each agent's fingerprint
includes its model ID, so switching models invalidates its old evidence. Keys come from the environment, `moot/.env` (see
`.env.example`) or the **Keys & models** dialog. Agents without a key, or whose key the provider rejected, show as
unavailable and are never auditioned. Model IDs and per-token prices can be edited in the dialog. The default prices for GPT and GLM are estimates.

The UI drives the same orchestrator that the MCP server exposes. `python3 -m moot.mcp_server --llm` gives MCP clients the LLM agents too.

## What the demo shows

| Job | Contract | What happens |
|---|---|---|
| 1 | FY2025 revenue + operating income from 2 annual-report PDFs, recurring | Moot asks 2 questions (budget escalated to the human, missing-field policy answered by the agent). All 3 workers are eligible for PDF and audition on 5 ground-truth fixtures. `web_scout` fails hard gates (unit scale, invented value). `document_analyst` beats `site_miner` on multi-step restatement handling. After approval, Halcyon is verified. Reducto then times out on Brightwater (labeled injected fault), the worker fails explicitly, Moot hands off **only the remaining source** to `site_miner` and keeps the verified checkpoint, and the replacement's output is verified. The verifier also flags a prompt-injection line in the source. |
| 2 | Add a 3rd company, same taxonomy | Evidence for each worker is fresh and matches its version fingerprint, so there are **no auditions ($0 eval spend)**. The job is hired and verified. |
| 3 | Cheap/fast headline revenue from a website | `document_analyst` is excluded (no web support, so it isn't ranked unfairly). Cost-weighted scoring makes **`web_scout` win** this time, even though it has a known silent-failure mode. |

Then the worker passports (versioned, scoped evidence and known failure modes) and the definition-of-done checklist are printed.

No winner is hard-coded. Each worker has its own extraction logic, and the rankings come from ground-truth checks.

## Layout

```
moot/mcp_server.py   MCP stdio server (JSON-RPC 2.0, 9 tools); timeline -> stderr
moot/buyer_agent.py  External Buyer Agent (scripted MCP client, no LLM)
moot/orchestrator.py Job state machine, matching, auditions, hiring, execution, recovery
moot/evaluator.py    Evaluation planner/runner, hard gates, soft score, rationale
moot/verifier.py     Independent checks: schema, provenance quotes, business rules, held-out truth, injection scan
moot/memory.py       Versioned capability evidence (fingerprint + taxonomy + freshness + sample size)
moot/transport.py    PrimitiveMessagingPort + LocalTransport (idempotency keys, logical events)
moot/workers.py      Document Analyst (Reducto), Site Miner (Firecrawl), Web Scout (Tavily)
moot/providers.py    Artifact store + offline provider tools + fault injection
data/fixtures/       Audition fixtures + manifest with ground truth
data/sources/        Unseen real-job documents;  data/truth/ held-out truth (verifier only)
runtime/moot.db      SQLite state (jobs, events, evaluations, attempts, verifications, evidence)
```

## Use it from a real MCP client

```bash
claude mcp add moot -- python3 -m moot.mcp_server   # run from this directory
```

Tools: `moot_create_job`, `moot_answer_questions`, `moot_find_candidates`, `moot_run_audition`,
`moot_evaluate_and_recommend`, `moot_get_evaluation`, `moot_hire_and_execute`, `moot_get_job`,
`moot_get_worker_passport`. These use underscores instead of `moot.create_job` for broad client compatibility.

## Honest labels (what is *not* real yet)

- **Providers are offline stand-ins.** Reducto, Firecrawl and Tavily are local tools over fixture files. They keep each provider's
  output shape (page-aware tables, flattened markdown, plain snippets). Costs come from a price model and latencies are modeled.
  To go live, swap real HTTP clients in behind the `ProviderSuite` methods.
- **Primitive is not connected.** `LocalTransport` implements the `PrimitiveMessagingPort` events in-process.
- **The fault is injected** with `--fault reducto:brightwater`, and it is announced at startup and in the error text.
- **Documents are synthetic** (`.pdf.txt` with page markers; web pages come from `data/web_mirror/`).
- SQLite stands in for Postgres. Execution is synchronous inside `hire_and_execute`, and `get_job` supports polling.
- There is one addition to the design's state machine: `VERIFYING -> RUNNING`, for the next source of a multi-source job after a checkpoint.
