# Manufact integration

Moot's real Deck Studio is exposed using **mcp-use 1.7.1**, Manufact's Python SDK.
The legacy nine-tool `moot.mcp_server` remains available for the original orchestration demo.
The new `moot.manufact_server` exposes seven Studio tools plus a binary PPTX resource.

## Install and run

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env  # only if you do not already have .env
# Configure a provider key in .env.
.venv/bin/python -m moot.manufact_server --transport streamable-http --with-web --inspect
```

- Landing page: http://127.0.0.1:8787 · Studio: http://127.0.0.1:8787/studio
- MCP endpoint: http://127.0.0.1:8000/mcp
- Manufact SDK Inspector: http://127.0.0.1:8000/inspector

Use this combined entrypoint instead of starting `moot.web` separately. Both interfaces share
one service, database, job queue, events and artifacts. Run one process/replica per workspace;
the in-process execution queue is not a distributed scheduler. Interrupted work is not automatically
resumed after a process restart. Completed jobs and files remain available on persistent storage.

For an MCP desktop client, set its working directory to this `moot/` directory and use:

```json
{
  "mcpServers": {
    "moot": {
      "command": "/absolute/path/to/moot/.venv/bin/python",
      "args": ["-m", "moot.manufact_server", "--transport", "stdio"],
      "cwd": "/absolute/path/to/moot"
    }
  }
}
```

Clients vary in support for `cwd`; use their documented working-directory setting if needed.
Connect to the existing HTTP endpoint when sharing a running web workspace.

## Agent workflow

| Tool | Purpose |
|---|---|
| `moot_studio_catalog` | Available agents and loaded skill versions/provenance |
| `moot_studio_create` | Read the product and return clarification questions; paid intake |
| `moot_studio_answer` | Compile benchmark and enqueue the paid trial/final build |
| `moot_studio_get` | Poll persisted state and inspect deck content |
| `moot_studio_review` | Read ranking, gate results, feedback and costs |
| `moot_studio_communication` | Inspect Primitive identities and message delivery evidence (sends nothing) |
| `moot_studio_artifact` | Retrieve SHA-256, size and resource URI; optional base64 bytes |

Read `moot://decks/{job_id}/deck.pptx` through MCP resources to download the binary file.
Tool-only clients can request `include_base64=true`. No server filesystem path is needed.
Repeated identical answer submissions are idempotent in this single-process workspace.
The budget is currently an estimate target, not a provider-enforced hard spend cap.

### Reproducible external buyer demo

With the server running, this scripted buyer uses **mcp-use MCPClient** and calls only MCP:

```bash
.venv/bin/python -m moot.studio_agent \
  --endpoint http://127.0.0.1:8000/mcp --use-defaults --budget 3
```

It creates a job, answers with the authorized defaults, polls, reviews scores, downloads the
PPTX, verifies its checksum and ZIP structure, and writes `runtime/mcp-demo.pptx` plus an evidence
JSON file. Omit `--endpoint` to start a separate stdio server instead (do not run that against
the same database as another live server). This mode is a scripted policy, not an LLM agent.

### LLM-driven external agent

```bash
.venv/bin/python -m pip install -r requirements-agent.txt
.venv/bin/python -m moot.studio_agent \
  --endpoint http://127.0.0.1:8000/mcp --use-defaults --budget 3 --agent
```

This uses **mcp-use MCPAgent** with a tool-calling OpenAI model. It independently chooses tool
calls and reports the final artifact URI and checksum. It has separate model costs and a finite
step limit; if that limit is reached while work is ongoing, resume with the reported job ID.
Use the scripted mode for deterministic polling and saving the file locally.

## Pinned external skills

Every candidate now receives seven instruction sets: five local ones, plus selected sections
from **Pitch Deck Advisor** and **deckgen**. Their unmodified upstream skill files and MIT licenses
are vendored under `data/skills/vendor/`. `manifest.json` records source repositories, full commit
IDs, exact selected headings and SHA-256 hashes. Loading fails if the files or licenses change
without updating the manifest. Provenance participates in the capability fingerprint.

The adapter gives the user's benchmark and Moot's JSON/renderer contract precedence over
upstream defaults. Upstream illustrative metrics are never product evidence. These integrations
are instruction excerpts: they do not run upstream scripts or install PptxGenJS. PPTX output
continues to use Moot's editable python-pptx renderer. See [attribution](THIRD_PARTY.md).

## Manufact Cloud deployment

A root-level Dockerfile packages the SDK server and its shared Studio backend. Configure a
single replica, port **8000**, and `/mcp` as the MCP endpoint. Set `MOOT_MCP_TOKEN` and provider
keys through Manufact's environment settings. The server refuses public binding without the
token. Clients send `Authorization: Bearer <MOOT_MCP_TOKEN>`; `studio_agent` reads it from the
environment. This is one trusted workspace, not per-user OAuth or tenant isolation.

Put your Manufact API key in `.env` as `MANUFACT_API_KEY` (Moot maps it to `MCP_USE_API_KEY`, the name the
SDK and CLI read), then deploy from the repository root:

```bash
./deploy_manufact.sh
```

The script checks the key with `mcp-use whoami`, generates `MOOT_MCP_TOKEN` in `.env` if it is missing, and runs
`mcp-use deploy --no-github --name moot --dockerfile Dockerfile`, which uploads local source to a Manufact-managed
private repository. The upload excludes `.env*` files and everything in the root `.gitignore` (including
`runtime/`). Provider keys and the token reach Manufact through a temporary `--env-file` that is deleted afterwards.
The platform supplies the port through `PORT`, which the server reads.

Do not commit secrets. Persist
`/app/runtime` for jobs/artifacts. Learned lessons currently live in
`/app/data/skills/pitch-deck/lessons.md`; without persistent storage, a redeploy resets these
and container-local artifacts. The Docker image exposes only authenticated MCP, not the local
web UI/settings routes. Actual cloud provisioning is separate from this source integration.

References: [Python SDK](https://github.com/mcp-use/mcp-use/tree/main/libraries/python),
[Manufact server deployment](https://docs.manufact.com/dashboard/servers),
[CLI deploy options](https://docs.manufact.com/cli/deploy).

## Checks without paid model calls

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use a clearly labeled fixture backend for the MCP round trip, while exercising the real
mcp-use client/server, shared service, binary artifact checks and python-pptx renderer.
They do not establish the quality of a live model-generated deck or a successful cloud deploy.
