"""Intake: turn a free-text task into a structured job spec.

Uses the first configured LLM if one is available, otherwise a transparent keyword parser. Anything the text does
not settle (budget, missing-field policy) is left unset so Moot asks for it instead of guessing.
"""
import re

from . import llm

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["task_family", "audience", "in_scope", "objective", "wants_operating_income", "max_total_cost_usd", "missing_field_policy",
                 "priority", "follow_up_updates_expected"],
    "properties": {
        "task_family": {"type": "string", "enum": ["financial_extraction", "pitch_deck"]},
        "audience": {"anyOf": [{"type": "string", "enum": ["seed investors", "series A investors", "customers", "hackathon judges"]},
                               {"type": "null"}]},
        "in_scope": {"type": "boolean"},
        "objective": {"type": "string"},
        "wants_operating_income": {"type": "boolean"},
        "max_total_cost_usd": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "missing_field_policy": {"anyOf": [{"type": "string", "enum": ["null_with_reason", "fail_job"]}, {"type": "null"}]},
        "priority": {"type": "string", "enum": ["quality", "cost_latency"]},
        "follow_up_updates_expected": {"type": "boolean"},
    },
}
SYSTEM = """You convert a user's task into a job spec for Moot, a service that hires extraction agents.
Moot currently supports two task families:
- financial_extraction: extracting company name, fiscal year, revenue and optionally operating income from financial
  documents or investor-relations pages.
- pitch_deck: writing a startup pitch deck from a brief (multi-step: plan, slides, self-check).
Set in_scope=false for anything else. audience applies to pitch decks only; null unless the user said who it is for.
Only fill max_total_cost_usd / missing_field_policy if the user actually stated them; otherwise null (Moot will ask).
priority is cost_latency only if the user emphasises cheap/fast/quick; otherwise quality.
wants_operating_income is true if the task needs operating income, operating profit or margins."""


def heuristic(text):
    t = text.lower()
    family = "pitch_deck" if re.search(r"pitch|deck|slides|presentation", t) else "financial_extraction"
    audience = next((a for pat, a in ((r"series a", "series A investors"), (r"seed|investor|vc", "seed investors"),
                                      (r"customer|buyer", "customers"), (r"judge|hackathon", "hackathon judges"))
                     if re.search(pat, t)), None)
    budget = re.search(r"\$\s?(\d+(?:\.\d+)?)", text)
    policy = None
    if re.search(r"never (guess|make up|invent)|don'?t (guess|make up|invent)|no guess|leave (it )?(blank|null)", t):
        policy = "null_with_reason"
    elif re.search(r"fail (the job )?if (any|a) (field|value) is missing", t):
        policy = "fail_job"
    return {
        "task_family": family, "audience": audience if family == "pitch_deck" else None,
        "in_scope": family == "pitch_deck" or bool(re.search(r"revenue|sales|operating|margin|income|profit|financ|annual report|earnings|10-k", t)),
        "objective": text.strip()[:300],
        "wants_operating_income": bool(re.search(r"operating|margin|profit|ebit", t)),
        "max_total_cost_usd": float(budget.group(1)) if budget else None,
        "missing_field_policy": policy,
        "priority": "cost_latency" if re.search(r"\b(cheap|cheapest|fast|quick|quickly|asap|low[- ]cost)\b", t) else "quality",
        "follow_up_updates_expected": bool(re.search(r"quarter|recurring|every (month|year|week)|ongoing|update|follow[- ]up|track", t)),
    }


def parse_task(text):
    provider = llm.first_available()
    note = None
    spec = None
    if provider:
        try:
            spec, usage = llm.complete_json(provider, SYSTEM, f"Task:\n{text}", SCHEMA)
            by = f"{llm.PROVIDERS[provider]['label']} ({usage['model']})"
        except llm.LLMError as e:
            note = f"LLM intake failed ({e}); used keyword parser"
    if spec is None:
        spec, by = heuristic(text), "keyword parser (no LLM configured)" if not note else "keyword parser"
    else:
        # explicit phrases the keyword parser is sure about ("never guess", "$2", "for seed investors") fill LLM gaps
        h = heuristic(text)
        for k in ("max_total_cost_usd", "missing_field_policy", "audience"):
            if spec.get(k) is None and h.get(k) is not None:
                spec[k] = h[k]
    family = spec.get("task_family") or "financial_extraction"
    fields = ["company", "fiscal_year", "revenue"] + (["operating_income"] if spec["wants_operating_income"] else [])
    constraints = {"priority": spec["priority"]}
    if family == "pitch_deck":
        fields = []
        if spec.get("audience"):
            constraints["audience"] = spec["audience"]
    m = re.search(r"(\d+)\s*slides", text, re.I)
    if family == "pitch_deck" and m:
        constraints["max_slides"] = max(5, min(20, int(m.group(1))))
    if spec.get("max_total_cost_usd") is not None:
        constraints["max_total_cost_usd"] = float(spec["max_total_cost_usd"])
    if spec.get("missing_field_policy") and family == "financial_extraction":
        constraints["missing_field_policy"] = spec["missing_field_policy"]
    return {
        "task_family": family, "in_scope": spec["in_scope"], "objective": spec["objective"] or text.strip()[:300],
        "required_fields": fields,
        "output_schema_id": "pitch_deck_v1" if family == "pitch_deck" else
                            "financial_comparison_v1" if spec["wants_operating_income"] else "headline_revenue_v1",
        "constraints": constraints,
        "workflow": {"recurring": spec["follow_up_updates_expected"],
                     "follow_up_updates_expected": spec["follow_up_updates_expected"], "allow_reassignment": True},
        "intake_by": by, "note": note,
    }
