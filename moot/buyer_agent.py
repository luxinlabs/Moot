"""Buyer Research Agent: an external MCP client that uses Moot as a service.

Deterministic scripted policy (no LLM): it decides from its mission config and its human principal's
standing policy. Every interaction with Moot goes through MCP tools/call over stdio.
"""
import json
import os
import subprocess
import sys

from . import ROOT
from .log import banner, emit, money, paint, table, usd

OUT = sys.stdout


def say(msg):
    emit("BUYER", msg, stream=OUT)


class McpClient:
    def __init__(self, cmd):
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None, text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self._id = 0

    def request(self, method, params=None):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        resp = json.loads(self.proc.stdout.readline())
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp["result"]

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call(self, name, **args):
        say(paint(f"-> tools/call {name}", "36", OUT) + f" {json.dumps(args)[:160]}")
        r = self.request("tools/call", {"name": name, "arguments": args})
        out = r["structuredContent"]
        if r.get("isError"):
            say(paint(f"<- error {out['error']['code']}: {out['error']['message']}", "31", OUT))
        return out

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


# The human principal's standing policy, consulted only for things the agent may not decide alone.
HUMAN_POLICY = {"budget_usd_per_comparison": 0.50, "max_auto_spend_usd": 0.30}
AGENT_ID = "buyer-agent:research-01"


def answer(questions):
    answers = {}
    for q in questions:
        if q["id"] == "missing_field_policy":
            answers[q["id"]] = "null_with_reason"
            say(f"Q[{q['id']}] -> 'null_with_reason' (agent's own rule: never fabricate figures)")
        elif q["id"] == "max_total_cost_usd":
            answers[q["id"]] = HUMAN_POLICY["budget_usd_per_comparison"]
            emit("HUMAN", f"Q[{q['id']}] escalated to human principal -> budget ${HUMAN_POLICY['budget_usd_per_comparison']:.2f}",
                 stream=OUT)
    return answers


def authorize(rec):
    est = rec["execution_estimate_usd"]
    if est > HUMAN_POLICY["max_auto_spend_usd"]:
        raise SystemExit("estimate exceeds the agent's delegated spend authority; would escalate to human")
    say(f"approval required for est. {usd(est)}; within delegated authority {usd(HUMAN_POLICY['max_auto_spend_usd'])} -> authorizing")
    return {"approved_by": AGENT_ID, "spend_cap_usd": HUMAN_POLICY["max_auto_spend_usd"],
            "delegated_from": "human-principal standing policy"}


def print_ranking(ev):
    rows = []
    for r in ev["decision"]["ranking"]:
        c = r["components"]
        rows.append([r["worker_id"], "yes" if r["qualified"] else "NO", r["score"],
                     *(c.get(k, "-") for k in ("correctness", "evidence", "multi_step", "recovery", "cost_latency")),
                     f"{r['ci95_correctness'][0]:.2f}-{r['ci95_correctness'][1]:.2f}", r["evidence_source"],
                     ",".join(r["failed_gates"]) or "-"])
    OUT.write(table(["worker", "qualified", "score", "correct", "evidence", "multistep", "recovery", "cost/lat",
                     "ci95", "evidence src", "failed gates"], rows) + "\n")
    OUT.write(f"weights: {ev['decision']['weights']}\n{ev['decision']['caveat']}\n")


def print_fixtures(ev):
    for c in ev["candidates"]:
        if c.get("reused"):
            continue
        OUT.write(f"  {c['worker_id']}: " + ", ".join(f"{f['fixture_id']}={'PASS' if f['pass'] else 'FAIL'}"
                                                         for f in c["fixtures"]) + "\n")


def comparison(rows_by_company):
    rows = []
    for d in rows_by_company:
        f = d["fields"]
        rev, op = f["revenue"]["value"], f.get("operating_income", {}).get("value")
        margin = f"{100 * op / rev:.1f}%" if rev and op is not None else "n/a"
        cite = f["revenue"]["citation"] or {}
        where = f"p.{cite['page']}" if cite.get("page") else cite.get("locator", "")
        rows.append([f["company"]["value"], f["fiscal_year"]["value"], money(rev), money(op), margin,
                     d["verification"], d["worker_id"], where])
    return table(["company", "FY", "revenue", "op. income", "op. margin", "verification", "worker", "rev. cite"], rows)


def run(fault=True):
    cmd = [sys.executable, "-m", "moot.mcp_server"]
    if fault:
        cmd += ["--fault", "reducto:brightwater"]
    client = McpClient(cmd)
    init = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "buyer-research-agent", "version": "1.0"}})
    client.notify("notifications/initialized")
    tools = client.request("tools/list")["tools"]
    say(f"connected to MCP server '{init['serverInfo']['name']}' (protocol {init['protocolVersion']}); "
        f"{len(tools)} tools: {', '.join(t['name'] for t in tools)}")

    # ---------------------------------------------------------------- Job 1
    banner("JOB 1  Buyer mission: compare FY2025 operating margins (annual-report PDFs)")
    emit("HUMAN", "Objective to agent: 'Compare FY2025 operating margins of Halcyon Freight and Brightwater Labs.'", stream=OUT)
    say("I lack reliable PDF table extraction -> delegating via Moot")
    j = client.call("moot_create_job",
                    objective="Extract FY2025 revenue and operating income from annual reports for a margin comparison",
                    sources=[{"uri": "file://sources/halcyon_freight_fy2025.pdf.txt"},
                             {"uri": "file://sources/brightwater_labs_fy2025.pdf.txt"}],
                    required_fields=["company", "fiscal_year", "revenue", "operating_income"],
                    output_schema_id="financial_comparison_v1",
                    constraints={"deadline_seconds": 120, "source_citations_required": True},
                    workflow={"recurring": True, "follow_up_updates_expected": True, "allow_reassignment": True})
    job_id = j["job_id"]
    say(f"<- {job_id} status={j['status']}; {len(j['questions'])} question(s)")
    for q in j["questions"]:
        say(f"   ? {q['question']}  [{q['why']}]")
    j = client.call("moot_answer_questions", job_id=job_id, answers=answer(j["questions"]))
    say(f"<- status={j['status']} contract={j['contract']['contract_id']}")
    for c in j["contract"]["acceptance_criteria"]:
        OUT.write(f"     - {c}\n")

    banner("JOB 1  Candidate matching + targeted auditions")
    cands = client.call("moot_find_candidates", job_id=job_id)
    say(f"<- eligible: {[e['worker_id'] for e in cands['eligible']]}; excluded: "
        f"{[(x['worker_id'], x['reason']) for x in cands['excluded']]}")
    rec = client.call("moot_run_audition", job_id=job_id)
    ev = client.call("moot_get_evaluation", run_id=rec["run_id"])
    say(f"<- evaluation {rec['run_id']}: audition spend {usd(ev['spent_usd'])}")
    print_fixtures(ev)
    print_ranking(ev)
    say(f"recommendation: {rec['recommendation']}  ({rec['decision']['rationale']})")

    banner("JOB 1  Hire, execute unseen documents, verify, recover")
    auth = authorize(rec) if rec["approval_required"] else None
    res = client.call("moot_hire_and_execute", job_id=job_id, worker_id="auto", authorization=auth)
    say(f"<- {job_id} status={res['status']}  path: {' '.join(res['history'])}")
    for a in res["attempts"]:
        OUT.write(f"     attempt {a['attempt_id']} {a['worker_id']:<17} {a['status']:<10} {a.get('error') or ''}\n")
    say(f"costs: evaluation {usd(res['costs']['evaluation_usd'])} + execution {usd(res['costs']['execution_usd'])} "
        f"= {usd(res['costs']['total_usd'])} (budget $0.50)")
    banner("JOB 1  Buyer agent continues its mission with the verified result")
    OUT.write(comparison(res["deliverables"]) + "\n")
    for lim in res["limitations"]:
        OUT.write(f"     limitation: {lim}\n")

    # ---------------------------------------------------------------- Job 2 (follow-up, memory reuse)
    banner("JOB 2  Follow-up: add a third company (same task taxonomy -> memory reuse)")
    j2 = client.call("moot_create_job",
                     objective="Add Cobalt Mills FY2025 to the operating margin comparison",
                     sources=[{"uri": "file://sources/cobalt_mills_fy2025.pdf.txt"}],
                     required_fields=["company", "fiscal_year", "revenue", "operating_income"],
                     output_schema_id="financial_comparison_v1",
                     constraints={"max_total_cost_usd": 0.50, "missing_field_policy": "null_with_reason"},
                     workflow={"recurring": True, "follow_up_updates_expected": True})
    say(f"<- {j2['job_id']} status={j2['status']} (all constraints supplied -> no questions)")
    rec2 = client.call("moot_evaluate_and_recommend", job_id=j2["job_id"])
    ev2 = client.call("moot_get_evaluation", run_id=rec2["run_id"])
    say(f"<- audition spend this time: {usd(ev2['spent_usd'])} (evidence reused from capability memory)")
    print_ranking(ev2)
    auth2 = authorize(rec2) if rec2["approval_required"] else None
    res2 = client.call("moot_hire_and_execute", job_id=j2["job_id"], authorization=auth2)
    say(f"<- status={res2['status']}  total cost {usd(res2['costs']['total_usd'])}")
    OUT.write(comparison(res["deliverables"] + res2["deliverables"]) + "\n")

    # ---------------------------------------------------------------- Job 3 (different contract)
    banner("JOB 3  Different contract, same pool: cheap + fast headline revenue from a website")
    j3 = client.call("moot_create_job",
                     objective="Get Fernway Systems' FY2025 headline revenue from its investor-relations site",
                     sources=[{"uri": "https://ir.fernway.example/fy2025"}],
                     required_fields=["company", "fiscal_year", "revenue"], output_schema_id="headline_revenue_v1",
                     constraints={"max_total_cost_usd": 0.05, "missing_field_policy": "null_with_reason",
                                  "priority": "cost_latency", "deadline_seconds": 30},
                     workflow={"recurring": False, "follow_up_updates_expected": False},
                     authorization={"approved_domains": ["ir.fernway.example"]})
    say(f"<- {j3['job_id']} status={j3['status']} taxonomy={j3['contract']['task_taxonomy']}")
    c3 = client.call("moot_find_candidates", job_id=j3["job_id"])
    say(f"<- eligible: {[e['worker_id'] for e in c3['eligible']]}; excluded: {[x['worker_id'] for x in c3['excluded']]}")
    rec3 = client.call("moot_run_audition", job_id=j3["job_id"])
    ev3 = client.call("moot_get_evaluation", run_id=rec3["run_id"])
    print_fixtures(ev3)
    print_ranking(ev3)
    say(f"recommendation: {rec3['recommendation']} (auto-approved: {not rec3['approval_required']})")
    res3 = client.call("moot_hire_and_execute", job_id=j3["job_id"])
    d = res3["deliverables"][0] if res3["deliverables"] else None
    if d:
        say(f"<- status={res3['status']}: {d['fields']['company']['value']} FY{d['fields']['fiscal_year']['value']} "
            f"revenue {money(d['fields']['revenue']['value'])} [{d['verification']}] by {d['worker_id']}, "
            f"quote: {d['fields']['revenue']['citation']['quote']!r}")

    # ---------------------------------------------------------------- Passports
    banner("Capability memory: worker passports after three jobs")
    passports = {}
    for wid in ("document_analyst", "site_miner", "web_scout"):
        p = passports[wid] = client.call("moot_get_worker_passport", worker_id=wid)
        OUT.write(f"{wid} @ {p['registration']['version_fingerprint']}\n")
        for tax, t in p["evidence_by_taxonomy"].items():
            a = t["audition_summary"] or {}
            real = ", ".join(f"{r['outcome']}{'(' + r['failure_mode'] + ')' if r['failure_mode'] else ''}"
                             for r in t["real_jobs"]) or "none"
            OUT.write(f"   {tax:<28} audition {a.get('field_accuracy')} ci95={a.get('ci95')} "
                      f"gates={'pass' if a.get('gates_passed') else 'FAIL'} | real jobs: {real}\n")
            if a.get("failure_modes"):
                OUT.write(f"   {'':<28} known failure modes: {', '.join(a['failure_modes'])}\n")

    banner("Definition-of-done checklist (this run)")
    checks = [
        ("External agent invoked Moot through MCP (stdio JSON-RPC)", True),
        ("Machine-readable job contract generated after clarification", j["status"] == "READY" or bool(j.get("contract"))),
        ("Two+ workers executed comparable eligible auditions", sum(not c.get("reused") for c in ev["candidates"]) >= 2),
        ("Scores backed by visible ground-truth checks", any(f["fixture_id"] for c in ev["candidates"] for f in c["fixtures"])),
        ("Qualified worker hired; unseen extraction completed", res["status"] in ("COMPLETED", "PARTIAL")),
        ("Verifier emitted structured pass/fail with source references", all(dd["verification_id"] for dd in res["deliverables"])),
        ("Failure detected and work reassigned with checkpoint", len({a["worker_id"] for a in res["attempts"]}) > 1),
        ("Verified outcome written to versioned capability memory",
         any(r["outcome"] == "verified" for p in passports.values() for t in p["evidence_by_taxonomy"].values()
             for r in t["real_jobs"])),
        ("Next relevant job reused prior evidence", ev2["spent_usd"] == 0),
        ("Primitive transport: NOT connected - labeled LocalTransport adapter", True),
    ]
    for label, ok in checks:
        OUT.write(f"  [{'x' if ok else ' '}] {label}\n")
    client.close()
