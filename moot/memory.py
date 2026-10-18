"""Versioned capability memory. Evidence, not a global score: records are scoped by worker version
fingerprint and task taxonomy, carry freshness and sample size, and reference artifacts instead of
raw document text."""
import math
import time

from .store import new_id

MAX_AGE_DAYS = 30
MIN_FIXTURES = 3


def wilson(passed, total, z=1.96):
    if total == 0:
        return (0.0, 1.0)
    p = passed / total
    d = 1 + z * z / total
    c = p + z * z / (2 * total)
    m = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (round((c - m) / d, 3), round((c + m) / d, 3))


class CapabilityMemory:
    def __init__(self, store):
        self.store = store

    def record(self, worker, taxonomy, kind, ref_id, doc, scope="tenant_private"):
        eid = new_id("ev")
        self.store.insert("capability_evidence", evidence_id=eid, worker_id=worker.worker_id,
                          version_fingerprint=worker.fingerprint(), task_taxonomy=taxonomy, kind=kind,
                          ref_id=ref_id, scope=scope, doc=doc)
        return eid

    def latest_summary(self, worker, taxonomy):
        r = self.store.rows("SELECT * FROM capability_evidence WHERE worker_id=? AND task_taxonomy=? AND "
                            "kind='audition_summary' ORDER BY created_at DESC LIMIT 1",
                            (worker.worker_id, taxonomy))
        return r[0] if r else None

    def sufficiency(self, worker, taxonomy, stages, evaluator_version):
        row = self.latest_summary(worker, taxonomy)
        if not row:
            return False, "no prior evidence for this task taxonomy", None
        doc = row["doc"]
        age_days = (time.time() - row["created_at"]) / 86400
        if row["version_fingerprint"] != worker.fingerprint():
            return False, "worker version changed since last evaluation (evidence invalidated)", None
        if doc["evaluator_version"] != evaluator_version:
            return False, "evaluation harness changed", None
        if age_days > MAX_AGE_DAYS:
            return False, f"evidence stale ({age_days:.0f}d old)", None
        if not set(stages) <= set(doc["stages_covered"]):
            return False, f"missing stages {sorted(set(stages) - set(doc['stages_covered']))}", None
        if doc["n_fixtures"] < MIN_FIXTURES:
            return False, "sample too small", None
        real = self.real_job_stats(worker, taxonomy)
        return True, (f"fresh ({age_days * 24 * 60:.0f} min old), {doc['n_fixtures']} fixtures, same fingerprint; "
                      f"real jobs: {real['verified']} verified / {real['failed']} failed"), {**doc, "real_jobs": real}

    def real_job_stats(self, worker, taxonomy):
        rows = self.store.rows("SELECT doc FROM capability_evidence WHERE worker_id=? AND task_taxonomy=? AND "
                               "kind='real_job' AND version_fingerprint=?",
                               (worker.worker_id, taxonomy, worker.fingerprint()))
        outcomes = [r["doc"]["outcome"] for r in rows]
        modes = sorted({r["doc"].get("failure_mode") for r in rows if r["doc"].get("failure_mode")})
        return {"verified": sum(o in ("verified", "partially_verified") for o in outcomes),
                "failed": sum(o in ("failed", "rejected") for o in outcomes), "failure_modes": modes}

    def passport(self, worker):
        rows = self.store.rows("SELECT * FROM capability_evidence WHERE worker_id=? ORDER BY created_at",
                               (worker.worker_id,))
        by_tax = {}
        for r in rows:
            t = by_tax.setdefault(r["task_taxonomy"], {"audition_summary": None, "real_jobs": [],
                                                       "fingerprints_seen": set()})
            t["fingerprints_seen"].add(r["version_fingerprint"])
            if r["kind"] == "audition_summary":
                d = r["doc"]
                t["audition_summary"] = {
                    "evidence_id": r["evidence_id"], "version_fingerprint": r["version_fingerprint"],
                    "fixtures": d["n_fixtures"], "field_accuracy": f"{d['fields_pass']}/{d['fields_total']}",
                    "ci95": d["ci95"], "gates_passed": all(g["pass"] for g in d["gates"].values()),
                    "failure_modes": d["failure_modes"], "scope": r["scope"],
                    "age_minutes": round((time.time() - r["created_at"]) / 60, 1)}
            else:
                t["real_jobs"].append({"evidence_id": r["evidence_id"], "job_id": r["doc"]["job_id"],
                                       "source": r["doc"]["source_uri"], "outcome": r["doc"]["outcome"],
                                       "failure_mode": r["doc"].get("failure_mode"), "scope": r["scope"]})
        for t in by_tax.values():
            t["fingerprints_seen"] = sorted(t["fingerprints_seen"])
        return {"registration": worker.registration(), "evidence_by_taxonomy": by_tax,
                "limitations": "Evidence is task- and version-scoped; not a global rank. Costs are estimates."}
