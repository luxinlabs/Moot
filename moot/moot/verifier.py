"""Independent outcome verifier. Reads raw sources itself (never through a worker's tools).

Hierarchy: held-out ground truth > source-based provenance checks > structural checks.
Tiers: verified (truth + provenance pass), partially_verified (provenance pass, no truth), rejected.
"""
import html
import json
import re

from . import DATA
from .contracts import MONEY_FIELDS, validate_schema
from .providers import split_pages

NUM_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
INJECTION_RE = re.compile(r"ignore (all )?previous instructions|disregard .{0,20}instructions|"
                          r"note to (ai|automated systems)", re.I)
TOLERANCE = 0.005


def canon(s):
    return " ".join(re.sub(r"[^a-z0-9.$()\-]+", " ", s.lower()).split())


def _html_text(raw):
    metas = re.findall(r'<meta\s+name="([^"]+)"\s+content="([^"]*)"', raw)
    body = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(f"{n.replace('_', ' ')} {c}" for n, c in metas) + " " + html.unescape(body)


def _num(tok):
    t = tok.replace(",", "")
    neg = t.startswith("(") and t.endswith(")")
    try:
        v = float(t.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def close(a, b, tol=TOLERANCE):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol * max(abs(b), 1.0)


class Verifier:
    VERSION = "verifier-1.0"

    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.heldout = json.loads((DATA / "truth" / "heldout_truth.json").read_text())

    # -- provenance: does the cited quote exist and contain the value?
    def provenance(self, field, entry):
        cit = entry.get("citation") or {}
        if not cit.get("quote"):
            return 0.0, "no citation"
        art = self.artifacts.get(cit["source_ref"])
        raw = self.artifacts.read_text(cit["source_ref"])
        q = canon(cit["quote"])
        if art["kind"] == "pdf":
            pages = {p["page"]: "\n".join(p["lines"]) for p in split_pages(raw)}
            if q not in canon("\n".join(pages.values())):
                return 0.0, "quote not found in source"
            if cit.get("page") is not None and q not in canon(pages.get(cit["page"], "")):
                return 0.0, f"quote not on cited page {cit['page']}"
            locality = 1.0 if cit.get("page") is not None else 0.75
        else:
            if q not in canon(_html_text(raw)):
                return 0.0, "quote not found in source"
            locality = None
        v = entry["value"]
        if field in MONEY_FIELDS:
            nums = [_num(t) for t in NUM_RE.findall(cit["quote"])]
            if not any(n is not None and close(n * s, v, 1e-6) for n in nums for s in (1, 1e3, 1e6, 1e9)):
                return 0.0, "quote does not contain the reported value"
        elif field == "fiscal_year" and str(v) not in cit["quote"]:
            return 0.0, "quote does not contain the year"
        elif field == "company" and canon(str(v)) not in q:
            return 0.0, "quote does not contain the company name"
        if locality is None:
            return 1.0, "located in fetched web page"
        return locality, "page-level" if locality == 1.0 else "document-level (no page locator)"

    # -- ground truth comparison (fixtures or held-out real-job truth)
    @staticmethod
    def truth_checks(record, truth, fields):
        out = []
        got_fields = (record or {}).get("fields") or {}
        for f in fields:
            exp = truth.get(f)
            entry = got_fields.get(f) or {}
            got = entry.get("value")
            if exp is None:
                ok = got is None and bool(entry.get("reason"))
                kind = "abstained_correctly" if ok else "invented_value"
            elif isinstance(exp, str):
                ok = isinstance(got, str) and canon(got) == canon(exp)
                kind = "match" if ok else ("missed" if got is None else "wrong")
            else:
                ok = isinstance(got, (int, float)) and close(float(got), float(exp))
                kind = "match" if ok else ("missed" if got is None else "wrong")
            out.append({"field": f, "expected": exp, "got": got, "pass": ok, "kind": kind})
        return out

    def scan_untrusted(self, aid):
        raw = self.artifacts.read_text(aid)
        return [l.strip() for l in raw.splitlines() if INJECTION_RE.search(l)]

    # -- full deliverable verification against the job contract
    def verify(self, contract, artifact, record):
        fields = contract["required_fields"]
        checks, failures, warnings = [], [], []

        errs = validate_schema(record, contract["output_schema_id"], fields)
        checks.append({"check": "schema", "pass": not errs, "detail": errs or "ok"})
        failures += [f"schema: {e}" for e in errs]

        fmap = (record or {}).get("fields") or {}
        policy = contract["constraints"]["missing_field_policy"]
        for f in fields:
            e = fmap.get(f) or {}
            if e.get("value") is None:
                ok = policy == "null_with_reason" and bool(e.get("reason"))
                checks.append({"check": f"abstention:{f}", "pass": ok, "detail": e.get("reason")})
                if not ok:
                    failures.append(f"{f}: null not allowed under policy '{policy}'")
                continue
            if contract["constraints"]["source_citations_required"]:
                score, detail = self.provenance(f, e)
                checks.append({"check": f"provenance:{f}", "pass": score > 0, "detail": detail})
                if score == 0:
                    failures.append(f"{f}: {detail}")

        rev, op = (fmap.get("revenue") or {}).get("value"), (fmap.get("operating_income") or {}).get("value")
        if rev is not None and op is not None:
            ok = op <= rev
            checks.append({"check": "rule:operating_income<=revenue", "pass": ok, "detail": "ok" if ok else "violated"})
            if not ok:
                failures.append("business rule violated: operating_income > revenue")

        truth = self.heldout.get(artifact["path"])
        if truth:
            tc = self.truth_checks(record, truth, fields)
            for t in tc:
                checks.append({"check": f"ground_truth:{t['field']}", "pass": t["pass"], "detail": t["kind"]})
                if not t["pass"]:
                    failures.append(f"{t['field']}: ground truth mismatch ({t['kind']})")

        hits = self.scan_untrusted(artifact["artifact_id"])
        if hits:
            warnings.append(f"untrusted instruction-like text in source treated as data: {hits[0]!r}")

        if failures:
            tier = "rejected"
        elif truth:
            tier = "verified"
        else:
            tier = "partially_verified"
        return {"tier": tier, "verifier_version": self.VERSION, "ground_truth_available": bool(truth),
                "checks": checks, "failures": failures, "warnings": warnings}
