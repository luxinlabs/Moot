"""Moot MCP server: newline-delimited JSON-RPC 2.0 over stdio (MCP stdio transport), stdlib only.

Register with an MCP client, e.g. Claude Code:
    claude mcp add moot -- python3 -m moot.mcp_server
stdout carries only protocol messages; the human-readable timeline goes to stderr.
"""
import argparse
import json
import sys
import traceback

from . import RUNTIME
from .contracts import MootError
from .log import emit
from .orchestrator import Orchestrator

PROTOCOL_VERSION = "2025-06-18"

_S = {"type": "string"}
TOOLS = [
    ("moot_create_job", "Submit a machine-readable job (objective, sources, required fields, constraints). "
     "Returns job_id plus either clarification questions or a compiled contract.",
     {"objective": _S, "sources": {"type": "array", "items": {"type": "object", "properties": {"uri": _S}, "required": ["uri"]}},
      "required_fields": {"type": "array", "items": _S}, "output_schema_id": _S,
      "constraints": {"type": "object"}, "workflow": {"type": "object"}, "authorization": {"type": "object"}},
     ["objective", "sources", "required_fields"]),
    ("moot_answer_questions", "Answer clarification questions for a job; returns the compiled contract or what is still missing.",
     {"job_id": _S, "answers": {"type": "object"}}, ["job_id", "answers"]),
    ("moot_find_candidates", "List eligible workers (modality/permission filtered) with capability-memory evidence status.",
     {"job_id": _S}, ["job_id"]),
    ("moot_run_audition", "Run targeted auditions for candidates lacking fresh evidence, then rank with hard gates + soft score.",
     {"job_id": _S, "candidate_ids": {"type": "array", "items": _S}, "max_audition_cost_usd": {"type": "number"},
      "force": {"type": "boolean"}}, ["job_id"]),
    ("moot_evaluate_and_recommend", "find_candidates + run_audition in one call (stages stay distinct internally).",
     {"job_id": _S}, ["job_id"]),
    ("moot_get_evaluation", "Field-level checks, gates, component scores, cost and rationale for an evaluation run.",
     {"run_id": _S}, ["run_id"]),
    ("moot_hire_and_execute", "Hire the recommended (or a named qualified) worker, execute the unseen job, verify, recover.",
     {"job_id": _S, "worker_id": _S, "authorization": {"type": "object"}}, ["job_id"]),
    ("moot_get_job", "Job state, verified deliverables with citations, attempts, costs, limitations.",
     {"job_id": _S}, ["job_id"]),
    ("moot_get_worker_passport", "Scoped, versioned capability memory and known limitations for a worker.",
     {"worker_id": _S}, ["worker_id"]),
]


def tool_list():
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": p, "required": r}} for n, d, p, r in TOOLS]


def dispatch(orch, name, args):
    handlers = {
        "moot_create_job": orch.create_job, "moot_answer_questions": orch.answer_questions,
        "moot_find_candidates": orch.find_candidates, "moot_run_audition": orch.run_audition,
        "moot_evaluate_and_recommend": orch.evaluate_and_recommend, "moot_get_evaluation": orch.get_evaluation,
        "moot_hire_and_execute": orch.hire_and_execute, "moot_get_job": orch.get_job,
        "moot_get_worker_passport": orch.get_worker_passport,
    }
    if name not in handlers:
        raise MootError("UNKNOWN_TOOL", name)
    return handlers[name](**args)


def handle(orch, msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification (e.g. notifications/initialized)
    try:
        if method == "initialize":
            result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "moot", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_list()}
        elif method == "tools/call":
            p = msg["params"]
            emit("MCP", f"tools/call {p['name']}")
            try:
                out = dispatch(orch, p["name"], p.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(out, default=str)}],
                          "structuredContent": out, "isError": False}
            except MootError as e:
                emit("WARN", f"{p['name']} -> {e.code}: {e.message}")
                result = {"content": [{"type": "text", "text": json.dumps(e.to_dict())}],
                          "structuredContent": e.to_dict(), "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # keep the server alive; report as JSON-RPC error
        traceback.print_exc(file=sys.stderr)
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(RUNTIME / "moot.db"))
    ap.add_argument("--tenant", default="acme-research")
    ap.add_argument("--llm", action="store_true", help="also register LLM agents (Claude / GPT / GLM)")
    ap.add_argument("--fault", action="append", default=[],
                    help="inject a reproducible provider fault for real jobs: TOOL:SOURCE_SUBSTRING")
    a = ap.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    orch = Orchestrator(a.db, a.tenant, faults=[tuple(f.split(":", 1)) for f in a.fault], include_llm=a.llm)
    for line in sys.stdin:
        if not line.strip():
            continue
        resp = handle(orch, json.loads(line))
        if resp is not None:
            sys.stdout.write(json.dumps(resp, default=str) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
