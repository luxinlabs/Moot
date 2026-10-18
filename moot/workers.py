"""Candidate worker agents. Provider APIs are tools used BY workers; each worker is independently
configured (inputs, tools, permissions, pricing, extraction strategy) and has a version fingerprint."""
import hashlib
import inspect
import json
import re

from .contracts import MONEY_FIELDS
from .providers import ProviderError, PRICING

AMOUNT_RE = re.compile(r"(?<![\w.])(\$\s?)?(\(?-?\d[\d,]*(?:\.\d+)?\)?)(?![\w%])")
SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
LABELS = {
    "revenue": {"revenue", "revenues", "total revenue", "net revenue"},
    "operating_income": {"operating income", "operating income (loss)", "income from operations",
                         "operating profit"},
}


def parse_amount(tok):
    t = tok.strip().replace("$", "").strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def scale_of(text):
    m = re.search(r"\b(thousand|million|billion)s?\b", text.lower())
    return SCALES[m.group(1)] if m else None


def norm(s):
    return " ".join(s.strip().lower().split())


def citation(ref, quote, locator, page=None):
    return {"source_ref": ref, "page": page, "locator": locator, "quote": quote.strip()}


def meta_fields(lines_with_loc, ref):
    """Company / fiscal year from header lines. lines_with_loc: [(line, page, locator)]."""
    out = {}
    for line, page, loc in lines_with_loc:
        m = re.match(r"\s*Company:\s*(.+?)\s*$", line)
        if m and "company" not in out:
            out["company"] = {"value": m.group(1), "citation": citation(ref, line, loc, page), "reason": None}
        m = re.match(r"\s*Fiscal year:\s*(\d{4})\s*$", line)
        if m and "fiscal_year" not in out:
            out["fiscal_year"] = {"value": int(m.group(1)), "citation": citation(ref, line, loc, page),
                                  "reason": None}
    return out


def missing(field, why):
    return {"value": None, "citation": None, "reason": why}


class Worker:
    worker_id = ""
    display = ""
    supported_inputs = ()
    tools = ()
    permissions = ("read_uploaded_documents",)
    model = "deterministic-parser"
    prompt = ""
    max_retries = 1
    families = ("financial_extraction",)

    def registration(self):
        unit, price, _ = PRICING[self.tools[0]]
        return {"worker_id": self.worker_id, "display": self.display,
                "version_fingerprint": self.fingerprint(), "supported_inputs": list(self.supported_inputs),
                "supported_outputs": ["structured_json", "source_quotes"], "tool_dependencies": list(self.tools),
                "pricing": {"unit": unit, "estimate_usd": price}, "permissions": list(self.permissions),
                "endpoint": f"local://{self.worker_id}"}

    def fingerprint(self):
        cfg = {"model": self.model, "prompt": self.prompt, "tools": self.tools, "perm": self.permissions,
               "inputs": self.supported_inputs, "code": inspect.getsource(type(self))}
        return "fp_" + hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]

    def available(self):
        return True, None

    def estimate_cost(self, art):
        unit, price, _ = PRICING[self.tools[0]]
        return price * (art["pages"] if unit == "page" else 1)

    def estimate_time(self, art):
        """Estimated execution time in seconds. Override in subclasses."""
        unit, _, latency = PRICING[self.tools[0]]
        return latency * (art["pages"] if unit == "page" else 1)

    def accepts(self, task):
        if task["modality"] not in self.supported_inputs:
            return False, f"unsupported input modality '{task['modality']}'"
        return True, None

    def execute(self, task, sandbox):
        try:
            record = self.run(task, sandbox)
            status, error = "completed", None
            sandbox.trace(summarize_record(record), kind="result")
        except ProviderError as e:
            record, status, error = None, "failed", str(e)
            sandbox.trace(f"explicit failure reported: {e}", kind="error")
        return {"status": status, "record": record, "error": error, "cost_usd": sandbox.meter.cost_usd,
                "latency_ms": sandbox.meter.latency_ms, "tool_calls": sandbox.meter.calls,
                "checkpoint": sandbox.checkpoints}

    def _with_retry(self, fn, sandbox):
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except ProviderError:
                if attempt == self.max_retries:
                    raise
                sandbox.trace(f"retry {attempt + 1}/{self.max_retries} after provider error", kind="warn")

    def run(self, task, sandbox):
        raise NotImplementedError


def summarize_record(record):
    if not record:
        return "no output"
    if "deck" in record:
        d = record["deck"]
        return f"deck: {len(d.get('slides', []))} slides — " + " · ".join(s.get("title", "?") for s in d.get("slides", [])[:8])
    parts = []
    for f, e in (record.get("fields") or {}).items():
        v = e.get("value")
        if isinstance(v, float) or (isinstance(v, int) and f != "fiscal_year"):
            v = f"{'-' if v < 0 else ''}${abs(v) / 1e6:,.1f}M"
        parts.append(f"{f}={v if v is not None else 'null'}")
    return "result: " + " · ".join(parts)


class DocumentAnalyst(Worker):
    """Reducto-backed. Page-aware tables; resolves unit scale from table context; picks the fiscal-year
    or restated column; exact line-item labels only (abstains otherwise); merges updates into prior."""
    worker_id = "document_analyst"
    display = "Document Analyst (Reducto)"
    supported_inputs = ("pdf",)
    tools = ("reducto",)
    prompt = "Extract only labelled line items from parsed statements; never infer a missing value."

    def run(self, task, sandbox):
        ref = task["artifact_ref"]
        parsed = self._with_retry(lambda: sandbox.reducto_parse(ref), sandbox)
        lines = [(l, p["page"], f"page {p['page']}") for p in parsed["pages"] for l in p["lines"]]
        fields = meta_fields(lines, ref)
        year = fields.get("fiscal_year", {}).get("value")
        for f in [f for f in task["required_fields"] if f in MONEY_FIELDS]:
            fields[f] = self._find(parsed["tables"], f, year, ref) or missing(
                f, f"no line item labelled {sorted(LABELS[f])} in {len(parsed['tables'])} parsed table(s)")
        record = {"source_ref": ref, "fields": fields}
        if task.get("prior_record"):
            record = self._merge(task["prior_record"], record)
        return record

    @staticmethod
    def _column(header, year):
        hs = [h.lower().replace(" ", "") for h in header]
        for i, h in enumerate(hs[1:], 1):
            if "restated" in h:
                return i
        for i, h in enumerate(hs[1:], 1):
            if year and str(year) in h:
                return i
        return 1 if len(header) == 2 else None

    def _find(self, tables, field, year, ref):
        for ti, t in enumerate(tables):
            col = self._column(t["header"], year)
            if col is None:
                continue
            for row in t["rows"]:
                if norm(row["cells"][0]) in LABELS[field] and col < len(row["cells"]):
                    v = parse_amount(row["cells"][col])
                    if v is None:
                        continue
                    scale = scale_of(t["unit_line"] or "") or 1.0
                    return {"value": v * scale,
                            "citation": citation(ref, row["raw"], f"page {t['page']} table {ti + 1} "
                                                 f"col '{t['header'][col]}' x{scale:g}", t["page"]),
                            "reason": None}
        return None

    @staticmethod
    def _merge(prior, new):
        out = {"source_ref": new["source_ref"], "fields": {}, "changes": []}
        for f, p in prior["fields"].items():
            n = new["fields"].get(f)
            if n and n.get("value") is not None and n["value"] != p.get("value"):
                out["fields"][f] = {**n, "revised_from": p.get("value")}
                out["changes"].append({"field": f, "from": p.get("value"), "to": n["value"]})
            elif n and n.get("value") is not None:
                out["fields"][f] = n
            else:
                out["fields"][f] = {**p, "preserved_from_prior": True}
        return out


class SiteMiner(Worker):
    """Firecrawl-backed. Parses markdown tables (fiscal-year column, unit lines) and '$X million'
    prose. PDFs arrive flattened, so citations are document-level. No restatement awareness."""
    worker_id = "site_miner"
    display = "Site Miner (Firecrawl)"
    supported_inputs = ("web", "pdf")
    tools = ("firecrawl",)
    prompt = "Convert pages to markdown, read labelled table rows or explicit dollar figures."
    PROSE = {
        "revenue": re.compile(r"\b(?:total |net )?revenues?\b[^$\n]{0,40}?\$\s?(\d[\d,]*(?:\.\d+)?)\s*"
                              r"(thousand|million|billion)?", re.I),
        "operating_income": re.compile(r"\boperating income\b[^$\n]{0,40}?\$\s?(\d[\d,]*(?:\.\d+)?)\s*"
                                       r"(thousand|million|billion)?", re.I),
    }

    def run(self, task, sandbox):
        ref = task["artifact_ref"]
        md = self._with_retry(lambda: sandbox.firecrawl_scrape(ref), sandbox)["markdown"]
        lines = md.splitlines()
        fields = meta_fields([(l, None, f"markdown line {i + 1}") for i, l in enumerate(lines)], ref)
        year = fields.get("fiscal_year", {}).get("value")
        wanted = [f for f in task["required_fields"] if f in MONEY_FIELDS]
        scale, header, in_table = None, None, False
        for i, line in enumerate(lines):
            s = line.strip()
            loc = f"markdown line {i + 1}"
            if s.startswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if not in_table:
                    header, in_table = cells, True
                    continue
                for f in wanted:
                    if f not in fields and norm(cells[0]) in LABELS[f]:
                        col = next((j for j, h in enumerate(header) if j and year and str(year) in h), None)
                        if col is None:
                            col = next((j for j in range(1, len(cells)) if parse_amount(cells[j]) is not None), None)
                        v = parse_amount(cells[col]) if col is not None else None
                        if v is not None:
                            fields[f] = {"value": v * (scale or 1.0), "citation": citation(ref, line, loc),
                                         "reason": None}
                continue
            in_table = False
            if "(in " in s.lower():
                scale = scale_of(s) or scale
            for f in wanted:
                m = self.PROSE[f].search(s)
                if f not in fields and m:
                    fields[f] = {"value": parse_amount(m.group(1)) * SCALES.get((m.group(2) or "").lower(), 1.0),
                                 "citation": citation(ref, line, loc), "reason": None}
        for f in wanted:
            fields.setdefault(f, missing(f, "no labelled row or explicit dollar figure found"))
        record = {"source_ref": ref, "fields": fields}
        if task.get("prior_record"):
            prior = task["prior_record"]["fields"]
            merged = {f: (record["fields"][f] if record["fields"].get(f, {}).get("value") is not None
                          else {**p, "preserved_from_prior": True}) for f, p in prior.items()}
            record = {"source_ref": ref, "fields": merged}
        return record


class WebScout(Worker):
    """Tavily-backed. Keyword snippet extraction: first line mentioning the field, prefers '$' amounts,
    skips percentages and years, applies a scale word only if it appears on the same line. Cheap and fast.
    Swallows provider errors and reports an empty result."""
    worker_id = "web_scout"
    display = "Web Scout (Tavily)"
    supported_inputs = ("web", "pdf")
    tools = ("tavily",)
    permissions = ("read_public_web",)
    max_retries = 0
    prompt = "Find the sentence that mentions the metric and read the number."
    KEYWORDS = {"revenue": "revenue", "operating_income": "operating"}

    def execute(self, task, sandbox):
        out = super().execute(task, sandbox)
        if out["status"] == "failed":  # swallow and report "done" with nothing found
            sandbox.trace("error swallowed — reporting 'completed' with empty fields", kind="warn")
            fields = {f: missing(f, "not found") for f in task["required_fields"]}
            out.update(status="completed", error=None, record={"source_ref": task["artifact_ref"], "fields": fields})
        return out

    def run(self, task, sandbox):
        ref = task["artifact_ref"]
        lines = sandbox.tavily_extract(ref)["content"].splitlines()
        fields = meta_fields([(l, None, f"snippet {i + 1}") for i, l in enumerate(lines)], ref)
        for f in [f for f in task["required_fields"] if f in MONEY_FIELDS]:
            for i, line in enumerate(lines):
                if self.KEYWORDS[f] not in line.lower():
                    continue
                v = self._pick(line)
                if v is not None:
                    fields[f] = {"value": v * (scale_of(line) or 1.0),
                                 "citation": citation(ref, line, f"snippet {i + 1}"), "reason": None}
                    break
            fields.setdefault(f, missing(f, "no snippet with a number mentions this metric"))
        return {"source_ref": ref, "fields": fields}

    @staticmethod
    def _pick(line):
        matches = list(AMOUNT_RE.finditer(line))
        for m in matches:
            if m.group(1):
                return parse_amount(m.group(2))
        for m in matches:
            raw = m.group(2)
            v = parse_amount(raw)
            if v is not None and not (re.fullmatch(r"\d{4}", raw) and 1900 <= v <= 2100):
                return v
        return None


ALL_WORKERS = (DocumentAnalyst(), SiteMiner(), WebScout())
