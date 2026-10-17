# Moot

Moot auditions AI agents, scores their work against your requirements, and hires the best qualified candidate to deliver the result.

## First MVP

- Deck Studio with product intake, clarification questions, weighted benchmarks, candidate work trials, judging, and editable PowerPoint export.
- Shared pitch-deck instructions and learned lessons across candidates.
- Monet-inspired interface with collapsible details, resizable panels, and clickable workflow stages.
- MCP stdio tools for the underlying job orchestration workflow, plus a financial-extraction demo.

## Run locally

```bash
cd moot
python3 -m pip install flask anthropic openai python-pptx
cp .env.example .env
# Configure provider keys in .env.
python3 -m moot.web
```

Open http://127.0.0.1:8787.

See [Quick start](moot/QUICKSTART.md), [architecture and demos](moot/README.md), and [system design](Moot_System_Design_and_Workflow.md).

## Current integration boundaries

The Studio uses local shared skills and a custom python-pptx renderer. The external skills research document is a roadmap, not a list of installed integrations. Manufact is not integrated, and the Studio HTTP workflow is not yet exposed through the existing MCP tools.

Provider keys, runtime databases, generated artifacts, and uploaded sources are excluded from version control.
