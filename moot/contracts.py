"""Job contract, output schemas, clarification questions and record validation."""

SCHEMAS = {
    "financial_comparison_v1": {"company": "string", "fiscal_year": "integer",
                                "revenue": "money", "operating_income": "money"},
    "headline_revenue_v1": {"company": "string", "fiscal_year": "integer", "revenue": "money"},
}
MONEY_FIELDS = ("revenue", "operating_income")

# Constraints Moot will not guess: it asks instead.
CLARIFICATIONS = {
    "max_total_cost_usd": {
        "question": "What is the maximum total spend in USD for this job, including auditions?",
        "type": "number",
        "why": "Bounds audition and execution spend; no provider calls are made without it."},
    "missing_field_policy": {
        "question": "If a required field is absent from a source, return null with a reason, or fail the job?",
        "type": "enum", "options": ["null_with_reason", "fail_job"],
        "why": "Decides whether abstention is an acceptable deliverable."},
    "audience": {
        "question": "Who is this deck for?",
        "type": "enum", "options": ["seed investors", "series A investors", "customers", "hackathon judges"],
        "why": "Agents tailor tone and emphasis to the audience; it is part of the contract."},
}

DEFAULT_CONSTRAINTS = {
    "deadline_seconds": 120,
    "source_citations_required": True,
    "minimum_acceptable_quality": "all_required_fields_supported_or_abstained",
    "priority": "quality",  # or "cost_latency"
}
DEFAULT_WORKFLOW = {"recurring": False, "follow_up_updates_expected": False, "allow_reassignment": True}


class MootError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message, **self.details}}


def missing_constraints(constraints):
    return [k for k in CLARIFICATIONS if constraints.get(k) is None]


def questions_for(missing):
    return [{"id": k, **CLARIFICATIONS[k]} for k in missing]


def acceptance_criteria(c, schema_id):
    return [
        f"Output validates against {schema_id}; every required field is present",
        "Every non-null field cites a quote that exists in the source and contains the value",
        f"Missing fields follow policy '{c['missing_field_policy']}' (null requires an explicit reason)",
        "Business rule: operating_income <= revenue when both are present",
        "Where held-out ground truth exists, numeric fields match within 0.5%",
        f"Total spend including auditions <= ${c['max_total_cost_usd']:.2f}",
    ]


def validate_schema(record, schema_id, required_fields):
    """Structural checks only. JSON validity is not factual accuracy."""
    errors = []
    schema = SCHEMAS[schema_id]
    fields = (record or {}).get("fields") or {}
    for f in required_fields:
        if f not in fields:
            errors.append(f"missing field '{f}'")
            continue
        entry = fields[f]
        v = entry.get("value")
        if v is None:
            if not entry.get("reason"):
                errors.append(f"'{f}' is null without a reason")
            continue
        kind = schema[f]
        if kind == "string" and not isinstance(v, str):
            errors.append(f"'{f}' should be a string")
        if kind == "integer" and not isinstance(v, int):
            errors.append(f"'{f}' should be an integer")
        if kind == "money" and not isinstance(v, (int, float)):
            errors.append(f"'{f}' should be a number (USD)")
    return errors
