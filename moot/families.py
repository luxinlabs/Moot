"""Task families: what differs between kinds of work (contract, tests, verification, units of work, recovery)."""
from .contracts import DEFAULT_CONSTRAINTS, SCHEMAS, MootError, acceptance_criteria
from .decks import REQUIRED_SECTIONS, DeckEvaluator, DeckVerifier, canon
from .evaluator import Evaluator
from .verifier import Verifier


class FinancialFamily:
    name, label = "financial_extraction", "Financial extraction"
    clarify = ("max_total_cost_usd", "missing_field_policy")
    defaults = {}

    def __init__(self, artifacts, transport):
        self.verifier = Verifier(artifacts)
        self.evaluator = Evaluator(artifacts, self.verifier, transport)

    def missing(self, c):
        return [k for k in self.clarify if c.get(k) is None]

    def validate(self, fields, schema_id):
        if schema_id not in ("financial_comparison_v1", "headline_revenue_v1"):
            raise MootError("BAD_SCHEMA", f"unknown output_schema_id {schema_id}")
        unknown = set(fields) - set(SCHEMAS[schema_id])
        if unknown:
            raise MootError("BAD_FIELDS", f"fields not in schema: {sorted(unknown)}")
        return fields, schema_id

    def acceptance(self, c, job):
        return acceptance_criteria(c, job["output_schema_id"])

    def taxonomy(self, modalities):
        return f"financial_extraction.{'+'.join(modalities)}"

    def units(self, job):
        return job["sources"]

    def task(self, job, art, checkpoint):
        return {"kind": "extract", "artifact_ref": art["artifact_id"], "modality": art["kind"],
                "required_fields": job["contract"]["required_fields"],
                "guidance": job["contract"]["constraints"].get("client_guidance", [])}

    def outage(self, art):
        return [("*", art["path"])], []

    def verify(self, contract, art, record, job, trace):
        return self.verifier.verify(contract, art, record)

    def accept_checkpoint(self, cp, art, artifacts):
        return None, "sources already verified are kept; this source restarts with the replacement"

    def view(self, d):
        return {"fields": {f: {k: e.get(k) for k in ("value", "reason", "citation")} for f, e in d["record"]["fields"].items()}}


class DeckFamily:
    name, label = "pitch_deck", "Pitch deck"
    clarify = ("max_total_cost_usd", "audience")
    defaults = {"max_slides": 12, "deadline_seconds": 300, "minimum_acceptable_quality": "objective_checks_pass"}

    def __init__(self, artifacts, transport):
        self.verifier = DeckVerifier(artifacts)
        self.evaluator = DeckEvaluator(artifacts, self.verifier, transport)

    def missing(self, c):
        return [k for k in self.clarify if c.get(k) is None]

    def validate(self, fields, schema_id):
        return list(fields or REQUIRED_SECTIONS), "pitch_deck_v1"

    def acceptance(self, c, job):
        return [
            f"Deck validates against pitch_deck_v1 with 5-{c['max_slides']} slides",
            f"Covers the required sections: {', '.join(job['required_fields'])}",
            "Every number in the deck appears in the brief (no invented metrics)",
            "At least 80% of cited source quotes appear verbatim in the brief",
            "Instructions embedded in the brief are ignored",
            "Narrative quality scored by a cross-vendor LLM judge panel (qualitative, reported separately)",
            f"Total spend including auditions <= ${c['max_total_cost_usd']:.2f}",
        ]

    def taxonomy(self, modalities):
        return "pitch_deck.brief"

    def units(self, job):
        return job["sources"][:1]  # the brief; any further sources are context

    def task(self, job, art, checkpoint):
        ct = job["contract"]
        return {"kind": "deck", "artifact_ref": art["artifact_id"], "modality": art["kind"],
                "required_fields": ct["required_fields"], "context_refs": [a["artifact_id"] for a in job["sources"][1:]],
                "max_slides": ct["constraints"]["max_slides"], "audience": ct["constraints"].get("audience"),
                "guidance": ct["constraints"].get("client_guidance", []), "checkpoint": checkpoint}

    def outage(self, art):
        return [], ["slides"]  # fail mid-task, after the plan step has been checkpointed

    def verify(self, contract, art, record, job, trace):
        return self.verifier.verify(contract, art, record, [a["path"] for a in job["sources"][1:]], judge=True, trace=trace)

    def accept_checkpoint(self, cp, art, artifacts):
        """Moot validates a failed agent's plan before handing it to a replacement."""
        plan = (cp or {}).get("plan")
        if not plan:
            return None, "no completed steps to hand over"
        src = canon(artifacts.read_text(art["artifact_id"]))
        quotes = [f["quote"] for f in plan.get("facts", []) if f.get("quote")]
        ok = [q for q in quotes if canon(q) in src]
        if not quotes or len(ok) / len(quotes) < 0.8:
            return None, f"plan rejected: only {len(ok)}/{len(quotes)} fact quotes found in the brief"
        return {"plan": plan}, f"plan validated: {len(ok)}/{len(quotes)} fact quotes found in the brief"

    def view(self, d):
        return {"deck": d["record"]["deck"], "quality": d.get("quality")}


def build_families(artifacts, transport):
    return {f.name: f for f in (FinancialFamily(artifacts, transport), DeckFamily(artifacts, transport))}
