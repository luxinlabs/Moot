"""Evaluation planner + runner + evidence-based hiring decision."""
import json
import threading

from . import DATA
from .contracts import MONEY_FIELDS, validate_schema
from .log import emit, publish, usd
from .memory import wilson
from .providers import ProviderSuite

COST_FLOOR_USD, LATENCY_FLOOR_MS = 0.001, 300


class AuditionBudget:
    """One audition budget shared by all candidates auditioning in parallel."""

    def __init__(self, cap):
        self.cap, self.spent, self._lock = cap, 0.0, threading.Lock()

    def exhausted(self):
        with self._lock:
            return self.spent >= self.cap

    def charge(self, usd):
        with self._lock:
            self.spent += usd


def skipped(fx):
    return {"fixture_id": fx["id"], "stage": fx["stage"], "status": "skipped", "pass": False, "checks": [],
            "field_checks": [], "provenance": [], "summary": "skipped: audition budget exhausted (counts as a failure)"}
WEIGHTS = {
    "quality": {"correctness": .35, "evidence": .20, "multi_step": .20, "recovery": .15, "cost_latency": .10},
    "cost_latency": {"correctness": .30, "evidence": .15, "multi_step": .10, "recovery": .10, "cost_latency": .35},
}


class Evaluator:
    VERSION = "eval-harness-1.0"

    def __init__(self, artifacts, verifier, transport):
        self.artifacts, self.verifier, self.transport = artifacts, verifier, transport
        self.fixtures = json.loads((DATA / "fixtures" / "manifest.json").read_text())

    def plan(self, contract):
        modality = contract["modalities"][0] if len(contract["modalities"]) == 1 else "mixed"
        stages = ["field_extraction", "abstention", "failure_handling"]
        if contract["workflow"].get("follow_up_updates_expected"):
            stages.insert(2, "multi_step")
        fx = [f for f in self.fixtures if f["modality"] == modality and f["stage"] in stages]
        return {"modality": modality, "stages": ["capability_gate", *stages, "efficiency"],
                "probe_stages": stages, "fixtures": [f["id"] for f in fx]}

    def _fixture(self, fid):
        return next(f for f in self.fixtures if f["id"] == fid)

    def _reference_prior(self, fx):
        art = self.artifacts.register(f"file://{fx['source']}")
        return {"source_ref": art["artifact_id"],
                "fields": {k: {"value": v, "citation": None, "reason": None} for k, v in fx["truth"].items()}}

    def audition(self, run_id, worker, plan, contract, budget):
        fields = contract["required_fields"]
        report = {"worker_id": worker.worker_id, "version_fingerprint": worker.fingerprint(), "fixtures": [],
                  "audition_cost_usd": 0.0, "pages": 0, "latency_ms": 0, "failure_modes": []}
        emit("EVAL", f"audition {worker.worker_id} ({worker.fingerprint()})")
        for fid in plan["fixtures"]:
            fx = self._fixture(fid)
            if budget.exhausted():
                emit("EVAL", f"{worker.worker_id}: audition budget exhausted; skipping {fid}", 1)
                report["fixtures"].append(skipped(fx))
                publish("audition", worker_id=worker.worker_id, fixture_id=fid, stage=fx["stage"], passed=False,
                        summary=report["fixtures"][-1]["summary"], status="skipped")
                continue
            art = self.artifacts.register(f"file://{fx['source']}")
            task = {"kind": "extract", "artifact_ref": art["artifact_id"], "modality": fx["modality"],
                    "required_fields": fields, "guidance": contract["constraints"].get("client_guidance", [])}
            if fx["stage"] == "multi_step":
                task["prior_record"] = self._reference_prior(self._fixture(fx["prior_from"]))
            sandbox = ProviderSuite(self.artifacts, faults=[("*", "*")] if fx.get("inject_fault") else [],
                                    trace_ctx={"worker_id": worker.worker_id, "phase": "audition", "run_id": run_id, "fixture_id": fid})
            sandbox.trace(f"test: {fx.get('description', fid)}", kind="begin")
            out = self.transport.offer_task(worker.worker_id, run_id, {"task": task, "audition": True,
                                            "deadline_seconds": 30}, f"{run_id}:{worker.worker_id}:{fid}",
                                            sandbox, quiet=True)
            report["audition_cost_usd"] += out["cost_usd"]
            budget.charge(out["cost_usd"])
            report["latency_ms"] += out["latency_ms"]
            if out["status"] == "completed":
                report["pages"] += art["pages"]
            report["fixtures"].append(self._check(fx, art, out, fields))
            r = report["fixtures"][-1]
            emit("EVAL", f"{worker.worker_id} {fid:<26} {r['stage']:<17} {'PASS' if r['pass'] else 'FAIL'}  {r['summary']}", 1)
            publish("audition", worker_id=worker.worker_id, fixture_id=fid, stage=r["stage"], passed=r["pass"],
                    summary=r["summary"], cost_usd=round(out["cost_usd"], 5), latency_ms=out["latency_ms"],
                    output=r.get("output"), field_checks=r.get("field_checks"), status=out["status"])
        return self._summarize(report, plan, contract)

    def _check(self, fx, art, out, fields):
        stage, rec = fx["stage"], out["record"]
        res = {"fixture_id": fx["id"], "stage": stage, "status": out["status"], "cost_usd": out["cost_usd"],
               "latency_ms": out["latency_ms"], "field_checks": [], "provenance": []}
        if stage == "failure_handling":
            explicit = out["status"] == "failed" and bool(out.get("error"))
            res.update(**{"pass": explicit, "summary": "explicit failure reported" if explicit else
                          "silent failure: reported completion with no data"})
            return res
        if out["status"] != "completed":
            res.update(**{"pass": False, "summary": f"did not complete: {out.get('error')}"})
            return res
        res["output"] = rec
        res["schema_errors"] = validate_schema(rec, "financial_comparison_v1" if "operating_income" in fields
                                               else "headline_revenue_v1", fields)
        res["field_checks"] = self.verifier.truth_checks(rec, fx["truth"], fields)
        if stage == "multi_step":
            for f, prev in fx["expect_revised_from"].items():
                got = rec["fields"].get(f, {}).get("revised_from")
                res["field_checks"].append({"field": f"{f}.revised_from", "expected": prev, "got": got,
                                            "pass": got is not None and abs(got - prev) < 1, "kind": "delta"})
        else:
            for f in fields:
                e = rec["fields"].get(f) or {}
                if e.get("value") is not None:
                    score, detail = self.verifier.provenance(f, e)
                    res["provenance"].append({"field": f, "score": score, "detail": detail})
        bad = [c for c in res["field_checks"] if not c["pass"]]
        res["pass"] = not bad and not res["schema_errors"]
        res["summary"] = "all fields match ground truth" if res["pass"] else "; ".join(
            f"{c['field']}: {c['kind']} (got {c['got']}, want {c['expected']})" for c in bad)
        return res

    def _summarize(self, rep, plan, contract):
        fx = rep["fixtures"]
        by = lambda s: [r for r in fx if r["stage"] == s]
        fc = [c for r in by("field_extraction") + by("abstention") for c in r["field_checks"]]
        prov = [p["score"] for r in fx for p in r["provenance"]]
        crit = [c for r in by("field_extraction") for c in r["field_checks"] if c["field"] in MONEY_FIELDS]
        invented = [c for r in by("abstention") for c in r["field_checks"] if c["kind"] == "invented_value"]
        schema_ok = all(not r.get("schema_errors") for r in fx)
        comps = {
            "correctness": sum(c["pass"] for c in fc) / len(fc) if fc else 0.0,
            "evidence": sum(prov) / len(prov) if prov else 0.0,
            "multi_step": (sum(r["pass"] for r in by("multi_step")) / len(by("multi_step"))) if by("multi_step") else None,
            "recovery": (sum(r["pass"] for r in by("failure_handling")) / len(by("failure_handling")))
            if by("failure_handling") else None,
        }
        gates = {
            "authorized_tools": {"pass": all(t in contract["authorization"]["allowed_tools"]
                                             for t in self.transport.endpoints[rep["worker_id"]].tools),
                                 "detail": "tools within tenant policy"},
            "supported_modality": {"pass": True, "detail": f"supports {plan['modality']}"},
            "schema": {"pass": schema_ok, "detail": "all outputs schema-valid" if schema_ok else "schema errors"},
            "critical_field_correctness": {"pass": bool(crit) and all(c["pass"] for c in crit),
                                           "detail": f"{sum(c['pass'] for c in crit)}/{len(crit)} critical numeric fields correct"},
            "benchmark_complete": {"pass": all(r["status"] != "skipped" for r in fx) and len(fx) == len(plan["fixtures"]),
                                   "detail": "ran every planned test"},
            "abstention_policy": {"pass": not invented,
                                  "detail": "no invented values" if not invented else f"{len(invented)} invented value(s)"},
        }
        modes = []
        for r in fx:
            for c in r["field_checks"]:
                if not c["pass"]:
                    modes.append(f"{r['stage']}:{c['field']}:{c['kind']}")
            if r["stage"] == "failure_handling" and not r["pass"]:
                modes.append("failure_handling:silent_failure")
        passed, total = sum(c["pass"] for c in fc), len(fc)
        return {
            "worker_id": rep["worker_id"], "version_fingerprint": rep["version_fingerprint"],
            "task_taxonomy": contract["task_taxonomy"], "evaluator_version": self.VERSION,
            "verifier_version": self.verifier.VERSION, "stages_covered": sorted({r["stage"] for r in fx if r["status"] != "skipped"}),
            "fixture_ids": [r["fixture_id"] for r in fx], "n_fixtures": len(fx), "fixtures": fx,
            "fixture_results": [{"id": r["fixture_id"], "pass": r["pass"]} for r in fx],
            "fields_pass": passed, "fields_total": total, "ci95": wilson(passed, total),
            "provenance_coverage": round(sum(1 for p in prov if p > 0) / len(prov), 3) if prov else 0.0,
            "components": comps, "gates": gates, "failure_modes": sorted(set(modes)),
            "audition_cost_usd": round(rep["audition_cost_usd"], 4),
            "cost_per_page": rep["audition_cost_usd"] / max(rep["pages"], 1),
            "latency_ms_per_page": rep["latency_ms"] / max(rep["pages"], 1), "reused": False,
        }

    def weights_for(self, contract, plan):
        w = dict(WEIGHTS[contract["constraints"].get("priority", "quality")])
        if "multi_step" not in plan["probe_stages"]:
            w.pop("multi_step")
        return w

    def decide(self, summaries, contract, plan):
        weights = self.weights_for(contract, plan)
        z = sum(weights.values())
        weights = {k: round(v / z, 3) for k, v in weights.items()}
        qualified = [s for s in summaries if all(g["pass"] for g in s["gates"].values())]
        pool = qualified or summaries
        # floors keep a free local tool from zeroing everyone else's cost score
        cost = lambda s: max(s["cost_per_page"], COST_FLOOR_USD)
        lat = lambda s: max(s["latency_ms_per_page"], LATENCY_FLOOR_MS)
        min_cost, min_lat = min(cost(s) for s in pool), min(lat(s) for s in pool)
        ranking = []
        for s in summaries:
            c = dict(s["components"])
            c["cost_latency"] = round(min(1.0, 0.5 * min_cost / cost(s) + 0.5 * min_lat / lat(s)), 3)
            c = {k: (1.0 if c.get(k) is None else round(c[k], 3)) for k in weights}
            score = round(sum(weights[k] * c[k] for k in weights), 3)
            ok = s in qualified
            ranking.append({"worker_id": s["worker_id"], "qualified": ok, "score": score, "components": c,
                            "failed_gates": [g for g, v in s["gates"].items() if not v["pass"]],
                            "ci95_correctness": s["ci95"], "n_fixtures": s["n_fixtures"],
                            "evidence_source": "memory (reused)" if s.get("reused") else "fresh audition",
                            "cost_per_page": round(s["cost_per_page"], 4)})
        ranking.sort(key=lambda r: (r["qualified"], r["score"]), reverse=True)
        top = ranking[0] if ranking and ranking[0]["qualified"] else None
        rationale = "No candidate passed all hard gates." if not top else _rationale(ranking, weights)
        return {"weights": weights, "ranking": ranking, "recommended": top and top["worker_id"],
                "rationale": rationale,
                "caveat": "Small fixture set: scores are indicative, not statistically precise (see ci95)."}


def _rationale(ranking, weights):
    top = ranking[0]
    others = [r for r in ranking[1:] if r["qualified"]]
    lines = [f"{top['worker_id']} passes all hard gates with score {top['score']}."]
    if others:
        ru = others[0]
        diffs = sorted(((weights[k] * (top["components"][k] - ru["components"][k]), k) for k in weights), reverse=True)
        pros = [f"{k} {d:+.3f}" for d, k in diffs if d > 0.001]
        cons = [f"{k} {d:+.3f}" for d, k in diffs if d < -0.001]
        lines.append(f"vs runner-up {ru['worker_id']} ({ru['score']}): ahead on {', '.join(pros) or 'none'}"
                     + (f"; behind on {', '.join(cons)}" if cons else "") + ".")
    for r in ranking:
        if not r["qualified"]:
            lines.append(f"{r['worker_id']} disqualified by hard gates: {', '.join(r['failed_gates'])} "
                         f"(low cost cannot compensate).")
    return " ".join(lines)
