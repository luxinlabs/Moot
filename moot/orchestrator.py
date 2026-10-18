"""Hiring orchestrator: job state machine, candidate matching, auditions, hiring, execution, recovery."""
import contextvars
import time
from concurrent.futures import ThreadPoolExecutor

from .contracts import (DEFAULT_CONSTRAINTS, DEFAULT_WORKFLOW, SCHEMAS, MootError, acceptance_criteria,
                        missing_constraints, questions_for)
from . import benchmark_questions
from .evaluator import AuditionBudget
from .families import build_families
from .log import emit, money, publish, usd
from .memory import CapabilityMemory
from .providers import PRICING, ArtifactStore, ProviderSuite
from .store import Store, new_id
from .transport import LocalTransport
from .registry import build_workers

TRANSITIONS = {
    "DRAFT": {"CLARIFICATION_REQUIRED", "READY"},
    "CLARIFICATION_REQUIRED": {"READY"},
    "READY": {"EVALUATING"},
    "EVALUATING": {"RECOMMENDED", "NO_MATCH"},
    "RECOMMENDED": {"AWAITING_APPROVAL", "ASSIGNED"},
    "AWAITING_APPROVAL": {"ASSIGNED", "CANCELLED"},
    "ASSIGNED": {"RUNNING"},
    "RUNNING": {"VERIFYING", "RECOVERING"},
    # VERIFYING -> RUNNING: next source of a multi-source job, after checkpointing the verified one.
    "VERIFYING": {"COMPLETED", "RECOVERING", "PARTIAL", "RUNNING"},
    "RECOVERING": {"ASSIGNED", "FAILED", "PARTIAL"},
}

TENANT_POLICIES = {
    "acme-research": {
        "allowed_tools": ["reducto", "firecrawl", "tavily", "reader", "template", "anthropic", "openai", "glm"],
        "approved_paths": ["sources/"],
        "approved_domains": ["ir.fernway.example"],
        "auto_approve_usd": 0.015,  # execution spend above this needs explicit buyer authorization
        "max_attempts_per_source": 3,
        "max_audition_share": 0.5,  # auditions may use at most half the job budget
    },
    # Web UI workspace: the person at the keyboard approves each URL they add, so any domain may be listed per job.
    "ui-workspace": {
        "allowed_tools": ["reducto", "firecrawl", "tavily", "reader", "template", "anthropic", "openai", "glm"],
        "approved_paths": ["sources/"],
        "approved_domains": ["*"],
        "auto_approve_usd": 0.02,
        "max_attempts_per_source": 3,
        "max_audition_share": 0.5,
    },
}


class Orchestrator:
    def __init__(self, db_path, tenant="acme-research", faults=(), include_llm=False):
        self.store = Store(db_path)
        self.tenant = tenant
        self.policy = TENANT_POLICIES[tenant]
        all_workers = build_workers(include_llm)
        self.workers = {w.worker_id: w for w in all_workers}
        self.artifacts = ArtifactStore(self.store)
        self.transport = LocalTransport(self.store, all_workers)
        self.families = build_families(self.artifacts, self.transport)
        self.evaluator = self.families["financial_extraction"].evaluator
        self.verifier = self.families["financial_extraction"].verifier
        self.memory = CapabilityMemory(self.store)
        self.faults = list(faults)
        emit("MOOT", f"tenant={tenant}  transport={self.transport.label}")
        emit("MOOT", f"workers: {', '.join(f'{w.worker_id}@{w.fingerprint()}' for w in all_workers)}")
        for f in self.faults:
            emit("FAULT", f"fault injection ENABLED for real jobs: tool={f[0]} source~'{f[1]}' (reproducible demo scenario)")

    # ------------------------------------------------------------ helpers
    def _job(self, job_id):
        job = self.store.load_job(job_id)
        if not job or job["tenant"] != self.tenant:
            raise MootError("NOT_FOUND", f"job {job_id} not found for this tenant")
        return job

    def _to(self, job, new, why=""):
        old = job["status"]
        if old != new and new not in TRANSITIONS.get(old, set()):
            raise MootError("INVALID_TRANSITION", f"{old} -> {new} not allowed")
        job["status"] = new
        job["history"].append({"at": time.time(), "from": old, "to": new, "why": why})
        self.store.save_job(job)
        emit("STATE", f"{job['job_id']}: {old} -> {new}" + (f"  ({why})" if why else ""))
        publish("state", job_id=job["job_id"], from_state=old, to_state=new, why=why)

    def _authorize_source(self, uri, job_auth):
        from urllib.parse import urlparse
        p = urlparse(uri)
        if p.scheme == "file":
            rel = (p.netloc + p.path).lstrip("/")
            if not any(rel.startswith(a) for a in self.policy["approved_paths"]):
                raise MootError("UNAUTHORIZED_SOURCE", f"{uri} is outside approved paths")
        else:
            tenant_ok = "*" in self.policy["approved_domains"] or p.netloc in self.policy["approved_domains"]
            job_domains = job_auth.get("approved_domains")
            if not tenant_ok or (job_domains is not None and p.netloc not in job_domains):
                raise MootError("UNAUTHORIZED_SOURCE", f"domain {p.netloc} not approved for this job")

    def _public(self, job, **extra):
        keys = ("job_id", "status", "objective", "questions", "contract", "evaluation_run_id", "costs", "benchmark_preview")
        return {**{k: job.get(k) for k in keys}, **extra}

    def _est_cost(self, worker, art):
        return worker.estimate_cost(art)

    def _est_time(self, worker, art):
        return worker.estimate_time(art)

    def _fam(self, job):
        return self.families[job.get("task_family", "financial_extraction")]

    # ------------------------------------------------------------ create / clarify
    def create_job(self, objective, sources, required_fields, output_schema_id="financial_comparison_v1",
                   constraints=None, workflow=None, authorization=None, simulate_outage=False,
                   task_family="financial_extraction", ask_benchmark_questions=False):
        if task_family not in self.families:
            raise MootError("BAD_FAMILY", f"unknown task family {task_family}; known: {sorted(self.families)}")
        fam = self.families[task_family]
        required_fields, output_schema_id = fam.validate(required_fields, output_schema_id)
        authorization = authorization or {}
        arts = []
        for s in sources:
            self._authorize_source(s["uri"], authorization)
            try:
                arts.append(self.artifacts.register(s["uri"]))
            except (OSError, ValueError) as e:
                raise MootError("SOURCE_UNAVAILABLE", f"could not load {s['uri']}: {e}")
        if not arts:
            raise MootError("NO_SOURCES", "at least one source is required")
        job = {"job_id": new_id("job"), "tenant": self.tenant, "status": "DRAFT", "history": [],
               "objective": objective, "sources": arts, "required_fields": required_fields,
               "output_schema_id": output_schema_id, "constraints": dict(constraints or {}),
               "workflow": {**DEFAULT_WORKFLOW, **(workflow or {})}, "authorization": authorization,
               "questions": [], "contract": None, "deliverables": {}, "attempts": [],
               "costs": {"evaluation_usd": 0.0, "execution_usd": 0.0, "total_usd": 0.0},
               "simulate_outage": bool(simulate_outage), "task_family": task_family, "checkpoints": {}}
        self.store.save_job(job)
        emit("MOOT", f"create_job {job['job_id']}: {objective!r}")
        for a in arts:
            emit("MOOT", f"source {a['uri']} -> {a['artifact_id']} ({a['kind']}, {a['pages']} page(s))", 1)
        if ask_benchmark_questions:
            stated = set(job["constraints"])
            qs = benchmark_questions.standard_questions(fam, job, stated)
            extra, note = benchmark_questions.designer_questions(fam, job, self.artifacts, objective)
            emit("MOOT", f"benchmark designer: {note}")
            job["questions"], job["benchmark_questions"] = qs + extra, True
            job["benchmark_preview"] = benchmark_questions.preview(fam, job)
            job["designer_note"] = note
            self._to(job, "CLARIFICATION_REQUIRED", f"{len(job['questions'])} benchmark-driven question(s) prepared")
            return self._public(job)
        miss = fam.missing(job["constraints"])
        if miss:
            job["questions"] = questions_for(miss)
            self._to(job, "CLARIFICATION_REQUIRED", f"missing critical constraints: {', '.join(miss)}")
        else:
            self._compile(job)
        return self._public(job)

    def answer_questions(self, job_id, answers):
        job = self._job(job_id)
        if job["status"] != "CLARIFICATION_REQUIRED":
            raise MootError("BAD_STATE", f"job is {job['status']}")
        if job.get("benchmark_questions"):
            try:
                missing = benchmark_questions.apply_answers(job, answers)
            except ValueError as e:
                raise MootError("BAD_ANSWER", str(e))
            job["benchmark_preview"] = benchmark_questions.preview(self._fam(job), job)
            if missing:
                self.store.save_job(job)
                return self._public(job, outstanding=missing)
            self._compile(job)
            return self._public(job, outstanding=[])
        for q in job["questions"]:
            if q["id"] in answers:
                v = answers[q["id"]]
                if q["type"] == "enum" and v not in q["options"]:
                    raise MootError("BAD_ANSWER", f"{q['id']} must be one of {q['options']}")
                if q["type"] == "number":
                    v = float(v)
                job["constraints"][q["id"]] = v
        outstanding = self._fam(job).missing(job["constraints"])
        if outstanding:
            job["questions"] = questions_for(outstanding)
            self.store.save_job(job)
            return self._public(job, outstanding=outstanding)
        job["questions"] = []
        self._compile(job)
        return self._public(job, outstanding=[])

    def _compile(self, job):
        fam = self._fam(job)
        c = {**DEFAULT_CONSTRAINTS, **fam.defaults, **job["constraints"]}
        modalities = sorted({a["kind"] for a in job["sources"]})
        job["contract"] = {
            "contract_id": new_id("contract"), "version": 1, "job_id": job["job_id"],
            "objective": job["objective"], "modalities": modalities,
            "task_family": fam.name, "task_taxonomy": fam.taxonomy(modalities),
            "source_refs": [a["artifact_id"] for a in job["sources"]],
            "required_fields": job["required_fields"], "output_schema_id": job["output_schema_id"],
            "constraints": c, "workflow": job["workflow"],
            "authorization": {**job["authorization"], "allowed_tools": self.policy["allowed_tools"]},
            "acceptance_criteria": fam.acceptance(c, job),
        }
        self._to(job, "READY", f"contract {job['contract']['contract_id']} compiled "
                               f"(taxonomy {job['contract']['task_taxonomy']})")

    # ------------------------------------------------------------ matching
    def find_candidates(self, job_id, quiet=False):
        job = self._job(job_id)
        if not job["contract"]:
            raise MootError("BAD_STATE", "contract not compiled; answer clarification questions first")
        ct = job["contract"]
        fam = self._fam(job)
        plan = fam.evaluator.plan(ct)
        eligible, excluded = [], []
        for w in self.workers.values():
            if fam.name not in w.families:
                continue  # not a candidate for this kind of work at all
            unsupported = [m for m in ct["modalities"] if m not in w.supported_inputs]
            unauthorized = [t for t in w.tools if t not in self.policy["allowed_tools"]]
            up, down_why = w.available()
            if unsupported or unauthorized or not up:
                why = (f"unsupported modality {unsupported}" if unsupported else
                       f"unauthorized tools {unauthorized}" if unauthorized else f"unavailable: {down_why}")
                excluded.append({"worker_id": w.worker_id, "reason": why})
                continue
            ok, why, _ = self.memory.sufficiency(w, ct["task_taxonomy"], plan["probe_stages"], fam.evaluator.VERSION)
            eligible.append({"worker_id": w.worker_id, "version_fingerprint": w.fingerprint(),
                             "registration": w.registration(),
                             "memory": {"sufficient": ok, "detail": why}})
        if quiet:
            return {"job_id": job_id, "eligible": eligible, "excluded": excluded, "evaluation_plan": plan}
        emit("MEM", f"find_candidates {job_id} (taxonomy {ct['task_taxonomy']})")
        for e in eligible:
            emit("MEM", f"eligible {e['worker_id']:<17} evidence: {'SUFFICIENT' if e['memory']['sufficient'] else 'needs audition'}"
                        f" - {e['memory']['detail']}", 1)
        for x in excluded:
            emit("MEM", f"excluded {x['worker_id']:<17} {x['reason']} (fairness: not ranked on unsupported inputs)", 1)
        return {"job_id": job_id, "eligible": eligible, "excluded": excluded, "evaluation_plan": plan}

    # ------------------------------------------------------------ evaluation + decision
    def run_audition(self, job_id, candidate_ids=None, max_audition_cost_usd=None, force=False):
        job = self._job(job_id)
        if job["status"] != "READY":
            raise MootError("BAD_STATE", f"job is {job['status']}, expected READY")
        ct = job["contract"]
        fam = self._fam(job)
        cands = self.find_candidates(job_id, quiet=True)
        plan = cands["evaluation_plan"]
        run_id = new_id("run")
        self._to(job, "EVALUATING", f"evaluation run {run_id}")
        cap = min(max_audition_cost_usd or 1e9, ct["constraints"]["max_total_cost_usd"] * self.policy["max_audition_share"])
        emit("EVAL", f"plan: stages={plan['stages']}  fixtures={len(plan['fixtures'])}  audition cap={usd(cap)}")
        summaries, spent, to_run = [], 0.0, []
        for e in cands["eligible"]:
            w = self.workers[e["worker_id"]]
            if candidate_ids and w.worker_id not in candidate_ids:
                continue
            reuse_ok, why, doc = self.memory.sufficiency(w, ct["task_taxonomy"], plan["probe_stages"], fam.evaluator.VERSION)
            if reuse_ok and not force:
                emit("MEM", f"reusing evidence for {w.worker_id}: {why} -> no re-test")
                publish("reused", job_id=job_id, worker_id=w.worker_id, detail=why)
                summaries.append({**doc, "reused": True})
                continue
            to_run.append(w)
        if not to_run and not summaries:
            self._to(job, "NO_MATCH", "no selected agent is eligible for this job")
            return self._public(job, run_id=run_id, recommendation=None, decision=None)
        budget = AuditionBudget(cap)  # shared by all candidates; a test skipped for budget counts as a failure
        with ThreadPoolExecutor(max_workers=max(len(to_run), 1)) as pool:
            futures = [(w, pool.submit(contextvars.copy_context().run, fam.evaluator.audition, run_id, w, plan, ct, budget))
                       for w in to_run]
            results = [(w, f.result()) for w, f in futures]
        for w, s in results:
            spent += s["audition_cost_usd"]
            s["evidence_id"] = self.memory.record(w, ct["task_taxonomy"], "audition_summary", run_id,
                                                  {k: v for k, v in s.items() if k != "fixtures"})
            gates = ", ".join(f"{g}={'ok' if v['pass'] else 'FAIL'}" for g, v in s["gates"].items())
            emit("EVAL", f"{w.worker_id}: checks {s['fields_pass']}/{s['fields_total']} (ci95 {s['ci95']}), "
                         f"audition cost {usd(s['audition_cost_usd'])}\n  gates: {gates}", 1)
            emit("MEM", f"recorded audition evidence {s['evidence_id']} for {w.worker_id}@{w.fingerprint()}", 1)
            summaries.append(s)
        decision = fam.evaluator.decide(summaries, ct, plan)
        job["costs"]["evaluation_usd"] = round(job["costs"]["evaluation_usd"] + spent, 4)
        job["costs"]["total_usd"] = round(job["costs"]["evaluation_usd"] + job["costs"]["execution_usd"], 4)
        job["evaluation_run_id"], job["decision"] = run_id, decision
        self.store.insert("evaluation_runs", run_id=run_id, job_id=job_id,
                          doc={"plan": plan, "summaries": summaries, "decision": decision, "spent_usd": spent})
        emit("MOOT", f"decision: {decision['rationale']}")
        publish("decision", job_id=job_id, run_id=run_id, decision=decision, spent_usd=round(spent, 5))
        if not decision["recommended"]:
            self._to(job, "NO_MATCH", "no candidate passed hard constraints")
            return self._public(job, run_id=run_id, recommendation=None, decision=decision)
        winner = self.workers[decision["recommended"]]
        est = sum(self._est_cost(winner, a) for a in fam.units(job))
        job["execution_estimate_usd"] = round(est, 4)
        self._to(job, "RECOMMENDED", f"{winner.worker_id} recommended")
        if est > self.policy["auto_approve_usd"]:
            self._to(job, "AWAITING_APPROVAL", f"estimated execution {usd(est)} > auto-approve "
                                               f"{usd(self.policy['auto_approve_usd'])}")
        return self._public(job, run_id=run_id, recommendation=decision["recommended"], decision=decision,
                            execution_estimate_usd=round(est, 4), approval_required=job["status"] == "AWAITING_APPROVAL")

    def evaluate_and_recommend(self, job_id, **kw):
        return self.run_audition(job_id, **kw)

    def get_evaluation(self, run_id):
        r = self.store.rows("SELECT * FROM evaluation_runs WHERE run_id=?", (run_id,))
        if not r:
            raise MootError("NOT_FOUND", f"run {run_id} not found")
        doc = r[0]["doc"]
        return {"run_id": run_id, "job_id": r[0]["job_id"], "plan": doc["plan"], "decision": doc["decision"],
                "spent_usd": round(doc["spent_usd"], 4),
                "candidates": [{k: s.get(k) for k in ("worker_id", "version_fingerprint", "reused", "n_fixtures",
                                                       "fields_pass", "fields_total", "ci95", "gates", "components",
                                                       "failure_modes", "audition_cost_usd")}
                               | {"fixtures": [{k: f.get(k) for k in ("fixture_id", "stage", "pass", "summary", "quality", "judge_panel",
                                                                      "output", "checks", "field_checks", "status")}
                                               for f in s.get("fixtures", [])]}
                               for s in doc["summaries"]]}

    # ------------------------------------------------------------ hire, execute, verify, recover
    def hire_and_execute(self, job_id, worker_id="auto", authorization=None):
        job = self._job(job_id)
        ct = job["contract"]
        if job["status"] == "AWAITING_APPROVAL":
            auth = authorization or {}
            if not auth.get("approved_by") or float(auth.get("spend_cap_usd", 0)) < job["execution_estimate_usd"]:
                raise MootError("APPROVAL_REQUIRED", "authorization with approved_by and spend_cap_usd >= estimate required",
                                execution_estimate_usd=job["execution_estimate_usd"])
            job["approval"] = {**auth, "at": time.time()}
            emit("MOOT", f"authorization accepted from {auth['approved_by']} (spend cap {usd(float(auth['spend_cap_usd']))})")
        elif job["status"] != "RECOMMENDED":
            raise MootError("BAD_STATE", f"job is {job['status']}")
        ranking = [r["worker_id"] for r in job["decision"]["ranking"] if r["qualified"]]
        if worker_id != "auto":
            if worker_id not in ranking:
                raise MootError("NOT_QUALIFIED", f"{worker_id} did not pass hard gates for this job")
            ranking.remove(worker_id)
            ranking.insert(0, worker_id)
        fam = self._fam(job)
        audition_shas = {self.artifacts.register(f"file://{f['source']}")["sha256"] for f in fam.evaluator.fixtures}
        for a in job["sources"]:
            if a["sha256"] in audition_shas:
                emit("WARN", f"{a['uri']} was used in auditions; not an unseen job")
        current = ranking[0]
        units = fam.units(job)
        outage = (current, units[0]["path"]) if job.get("simulate_outage") else None
        if outage:
            emit("FAULT", f"simulated provider outage for {current} on {units[0]['uri']} (requested by user)")
        self._to(job, "ASSIGNED", f"assigned to {current}")
        self.transport.send("assignment", "moot-orchestrator", current, job_id,
                            {"artifact_refs": [a["artifact_id"] for a in job["sources"]], "contract_ref": ct["contract_id"]})
        self._to(job, "RUNNING")
        pending = [a for a in units if a["artifact_id"] not in job["deliverables"]]
        tried = {a["artifact_id"]: set() for a in pending}
        n_attempts = {a["artifact_id"]: 0 for a in pending}
        limit = min(self.policy["max_attempts_per_source"], 1 + len(ranking))
        budget = ct["constraints"]["max_total_cost_usd"]
        while pending:
            art = pending[0]
            aid = art["artifact_id"]
            worker = self.workers[current]
            est = self._est_cost(worker, art)
            if job["costs"]["total_usd"] + est > budget:
                self._to(job, "RECOVERING", f"budget exhausted before {art['uri']}")
                return self._finish(job, "RECOVERING", "spend cap reached")
            n_attempts[aid] += 1
            tried[aid].add(current)
            attempt_id = new_id("att")
            emit("MOOT", f"attempt {attempt_id}: {current} <- {art['uri']}  (est {usd(est)})")
            task = fam.task(job, art, job.get("checkpoints", {}).get(aid))
            faults, fail_steps = list(self.faults), []
            if outage and outage[0] == current and art["path"] == outage[1]:
                f, s = fam.outage(art)
                faults, fail_steps = faults + f, s
            sandbox = ProviderSuite(self.artifacts, faults=faults, fail_steps=fail_steps,
                                    trace_ctx={"worker_id": current, "phase": "job", "attempt_id": attempt_id, "source": art["uri"]})
            sandbox.trace(f"hired: {art['uri']}" + (" (resuming from checkpoint)" if task.get("checkpoint") else ""), kind="begin")
            publish("attempt", job_id=job_id, attempt_id=attempt_id, worker_id=current, source=art["uri"], phase="started")
            out = self.transport.offer_task(current, job_id, {"task": task, "contract_ref": ct["contract_id"],
                                            "deadline_seconds": ct["constraints"]["deadline_seconds"]},
                                            f"{job_id}:{aid}:{current}:{n_attempts[aid]}", sandbox)
            for c in out["tool_calls"]:
                emit("MOOT", f"tool {c['tool']}: {c['units']} {c['unit']}(s) ok={c['ok']} est {usd(c['est_cost_usd'])} "
                             f"modeled {c['modeled_latency_ms']}ms", 1)
            job["costs"]["execution_usd"] = round(job["costs"]["execution_usd"] + out["cost_usd"], 4)
            job["costs"]["total_usd"] = round(job["costs"]["evaluation_usd"] + job["costs"]["execution_usd"], 4)
            attempt = {"attempt_id": attempt_id, "worker_id": current, "source_ref": aid, "status": out["status"],
                       "error": out.get("error"), "cost_usd": out["cost_usd"], "latency_ms": out["latency_ms"]}
            job["attempts"].append(attempt)
            publish("attempt", job_id=job_id, phase="finished", tool_calls=out["tool_calls"], **attempt)
            self.store.insert("execution_attempts", attempt_id=attempt_id, job_id=job_id, worker_id=current,
                              source_ref=aid, status=out["status"], doc={**attempt, "record": out["record"]})
            if out["status"] != "completed":
                cp, why_cp = fam.accept_checkpoint(out.get("checkpoint"), art, self.artifacts)
                if cp is not None:
                    job.setdefault("checkpoints", {})[aid] = {**cp, "_by": current}
                emit("RECOVER", f"checkpoint from {current}: {why_cp}")
                publish("checkpoint", job_id=job_id, worker_id=current, accepted=cp is not None, detail=why_cp,
                        steps=sorted((cp or {}).keys()))
                self.memory.record(worker, ct["task_taxonomy"], "real_job", job_id,
                                   {"job_id": job_id, "source_uri": art["uri"], "outcome": "failed",
                                    "failure_mode": "provider_timeout" if "timeout" in (out["error"] or "") else "worker_error",
                                    "error": out["error"], "cost_usd": out["cost_usd"]})
                self._to(job, "RECOVERING", f"{current} failed: {out['error']}")
                nxt = self._replacement(job, ranking, tried[aid], n_attempts[aid], limit, art)
                if not nxt:
                    return self._finish(job, "RECOVERING", "no viable recovery")
                current = nxt
                continue
            self._to(job, "VERIFYING", f"deliverable for {art['uri']}")
            v = fam.verify(ct, art, out["record"], job,
                           lambda text, kind="judge": publish("trace", job_id=job_id, worker_id="moot-verifier", phase="job",
                                                              tkind=kind, text=text, source=art["uri"]))
            if v.get("cost_usd"):
                job["costs"]["execution_usd"] = round(job["costs"]["execution_usd"] + v["cost_usd"], 4)
                job["costs"]["total_usd"] = round(job["costs"]["evaluation_usd"] + job["costs"]["execution_usd"], 4)
            vid = new_id("ver")
            self.store.insert("verification_results", verification_id=vid, job_id=job_id, attempt_id=attempt_id,
                              tier=v["tier"], doc=v)
            for chk in v["checks"]:
                emit("VERIFY", f"{'PASS' if chk['pass'] else 'FAIL'}  {chk['check']:<34} {chk['detail']}", 1)
            for wmsg in v["warnings"]:
                emit("WARN", wmsg, 1)
            emit("VERIFY", f"tier={v['tier'].upper()}  ({vid})")
            publish("verification", job_id=job_id, verification_id=vid, worker_id=current, source=art["uri"],
                    tier=v["tier"], checks=v["checks"], warnings=v["warnings"], quality=v.get("quality"))
            self.memory.record(worker, ct["task_taxonomy"], "real_job", job_id,
                               {"job_id": job_id, "source_uri": art["uri"], "outcome": v["tier"],
                                "failure_mode": "; ".join(v["failures"]) or None, "verification_id": vid,
                                "cost_usd": out["cost_usd"], "latency_ms": out["latency_ms"]})
            emit("MEM", f"recorded real-job outcome for {current}@{worker.fingerprint()}: {v['tier']}", 1)
            if v["tier"] == "rejected":
                self._to(job, "RECOVERING", "verifier rejected deliverable: " + "; ".join(v["failures"]))
                nxt = self._replacement(job, ranking, tried[aid], n_attempts[aid], limit, art)
                if not nxt:
                    return self._finish(job, "RECOVERING", "no viable recovery")
                current = nxt
                continue
            job["deliverables"][aid] = {"source_uri": art["uri"], "worker_id": current, "attempt_id": attempt_id,
                                        "verification_id": vid, "tier": v["tier"], "record": out["record"],
                                        "warnings": v["warnings"], "quality": v.get("quality")}
            emit("MOOT", f"checkpoint: {len(job['deliverables'])}/{len(units)} deliverables accepted")
            pending.pop(0)
            self.store.save_job(job)
            if pending:
                if current != ranking[0]:
                    emit("MOOT", f"returning to the hired agent {ranking[0]} for the next source (the failure was source-specific)")
                    current = ranking[0]
                self._to(job, "RUNNING", "next source (previous deliverable checkpointed)")
        return self._finish(job, "VERIFYING", "all deliverables verified")

    def _replacement(self, job, ranking, tried, n, limit, art):
        if n >= limit:
            emit("RECOVER", f"attempt limit {limit} reached for {art['uri']}")
            return None
        if not job["workflow"].get("allow_reassignment", True):
            emit("RECOVER", "reassignment not allowed by contract")
            return None
        for w in ranking:
            if w not in tried:
                done = [d for d in job["deliverables"]]
                self.transport.send("handoff_requested", "moot-orchestrator", w, job["job_id"],
                                    {"checkpoint_ref": f"checkpoint:{job['job_id']}:{len(done)}",
                                     "remaining_refs": [art["artifact_id"]],
                                     "reason": "previous worker failed; only remaining sources are shared"})
                self.transport.send("handoff_accepted", w, "moot-orchestrator", job["job_id"], {})
                emit("RECOVER", f"replacement {w} (next qualified in ranking); {len(done)} verified deliverable(s) preserved")
                publish("handoff", job_id=job["job_id"], to_worker=w, source=art["uri"], preserved=len(done))
                self._to(job, "ASSIGNED", f"handoff to {w}")
                self._to(job, "RUNNING")
                return w
        emit("RECOVER", "no other qualified worker available")
        return None

    def _finish(self, job, from_state, why):
        n_ok, n = len(job["deliverables"]), len(self._fam(job).units(job))
        if from_state == "VERIFYING" and n_ok == n:
            self._to(job, "COMPLETED", why)
        elif n_ok:
            self._to(job, "PARTIAL", f"{why}; {n_ok}/{n} sources delivered")
        else:
            self._to(job, "FAILED", why)
        return self.get_job(job["job_id"])

    # ------------------------------------------------------------ read APIs
    def get_job(self, job_id):
        job = self._job(job_id)
        fam = self._fam(job)
        deliverables = [{"source_uri": d["source_uri"], "worker_id": d["worker_id"], "verification": d["tier"],
                         "verification_id": d["verification_id"], "warnings": d["warnings"], **fam.view(d)}
                        for d in job["deliverables"].values()]
        missing = [a["uri"] for a in fam.units(job) if a["artifact_id"] not in job["deliverables"]]
        used = {a["worker_id"] for a in job["attempts"]}
        limitations = []
        if any(not hasattr(self.workers[w], "provider") for w in used):
            limitations.append("Baseline agents use offline local tool stand-ins; their costs are price-model estimates and latencies are modeled.")
        if any(hasattr(self.workers[w], "provider") for w in used):
            limitations.append("LLM agents made real model calls; costs are estimated from token counts x configured prices.")
        limitations.append("Agent-to-agent transport is LocalTransport, not Primitive.")
        if any(d["verification"] == "partially_verified" for d in deliverables):
            limitations.append("Partially verified: objective checks against the sources passed, but there is no reviewed answer key for this input.")
        if fam.name == "pitch_deck":
            limitations.append("Narrative quality is an LLM-judge opinion (qualitative), not a verified property.")
        return {"job_id": job_id, "status": job["status"], "task_family": fam.name,
                "contract_id": (job["contract"] or {}).get("contract_id"),
                "deliverables": deliverables, "undelivered_sources": missing, "attempts": job["attempts"],
                "costs": job["costs"], "history": [f"{h['from']}->{h['to']}" for h in job["history"]],
                "limitations": limitations}

    def get_worker_passport(self, worker_id):
        if worker_id not in self.workers:
            raise MootError("NOT_FOUND", f"worker {worker_id} not registered")
        return self.memory.passport(self.workers[worker_id])
