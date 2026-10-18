"""Artifact store and provider tools.

Offline mode: the Reducto / Firecrawl / Tavily tools below are LOCAL stand-ins that read fixture
files. They keep each provider's characteristic output shape (page-aware tables vs. flattened
markdown vs. plain text snippets) so worker differences come from real parsing behaviour, but costs
are estimated from a price model and latencies are modeled. Swap in real HTTP clients behind the
same method signatures for live provider calls.
"""
import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from . import DATA

PRICING = {  # tool -> (unit, est USD per unit, modeled ms per unit)
    "reducto": ("page", 0.010, 1200),
    "firecrawl": ("page", 0.004, 500),
    "tavily": ("call", 0.001, 350),
    "reader": ("page", 0.0, 0),
    "template": ("call", 0.0, 40),  # local deck template engine  # local document reader used by LLM agents (model tokens are metered separately)
}
MAX_DOC_CHARS = 300_000
TIMEOUT_MS = 5000


class ProviderError(Exception):
    pass


# ---------------------------------------------------------------- artifacts

def split_pages(text):
    pages, cur = [], None
    for line in text.splitlines():
        m = re.match(r"\s*=== Page (\d+) ===\s*$", line)
        if m:
            cur = {"page": int(m.group(1)), "lines": []}
            pages.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
    return pages


class ArtifactStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def resolve(uri):
        p = urlparse(uri)
        if p.scheme == "file":
            rel = (p.netloc + p.path).lstrip("/")
        elif p.scheme in ("http", "https"):
            # Offline web mirror: https://host/path -> data/web_mirror/host/path.html
            rel = f"web_mirror/{p.netloc}{p.path.rstrip('/') or '/index'}.html"
        else:
            raise ValueError(f"unsupported uri scheme: {uri}")
        path = (DATA / rel).resolve()
        if DATA.resolve() not in path.parents:
            raise ValueError(f"path escapes data root: {uri}")
        return rel, path

    def register(self, uri):
        rel, path = self.resolve(uri)
        if not path.exists() and uri.startswith(("http://", "https://")):
            self._fetch(uri, path)
        if not path.exists():
            raise FileNotFoundError(uri)
        text = path.read_text()
        sha = hashlib.sha256(text.encode()).hexdigest()
        kind = "pdf" if rel.endswith(".pdf.txt") else "web" if rel.endswith((".html", ".htm")) else "doc"
        pages = len(split_pages(text)) if kind == "pdf" else 1
        aid = f"art_{sha[:10]}"
        self.store.insert("artifacts", artifact_id=aid, uri=uri, kind=kind, path=rel, sha256=sha, pages=pages)
        return {"artifact_id": aid, "uri": uri, "kind": kind, "path": rel, "sha256": sha, "pages": pages}

    @staticmethod
    def _fetch(uri, path):
        """Live fetch of an approved URL into the web mirror (snapshot = the artifact that gets verified)."""
        import urllib.request
        req = urllib.request.Request(uri, headers={"User-Agent": "MootBot/0.1 (+hackathon demo)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("page larger than 2 MB")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.decode("utf-8", errors="replace"))

    def get(self, aid):
        r = self.store.rows("SELECT * FROM artifacts WHERE artifact_id=?", (aid,))
        if not r:
            raise KeyError(aid)
        return r[0]

    def read_text(self, aid):
        return (DATA / self.get(aid)["path"]).read_text()


# ---------------------------------------------------------------- html flattening

class _Flattener(HTMLParser):
    BLOCK = {"p", "h1", "h2", "h3", "li", "caption", "title", "dt", "dd", "div"}
    HIDDEN = {"style", "script", "noscript", "template"}  # not page text; styled pages must not leak CSS/JS into agents

    def __init__(self, markdown):
        super().__init__()
        self.markdown, self.lines, self.buf, self.row, self.cell = markdown, [], [], None, None
        self.hidden = 0

    def _flush(self):
        t = " ".join("".join(self.buf).split())
        if t:
            self.lines.append(t)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.HIDDEN:
            self.hidden += 1
        elif tag == "meta"and a.get("name") in ("company", "fiscal_year"):
            label = "Company" if a["name"] == "company" else "Fiscal year"
            self.lines.append(f"{label}: {a.get('content', '')}")
        elif tag == "tr":
            self._flush()
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.lines.append(("| " + " | ".join(self.row) + " |") if self.markdown else " ".join(self.row))
            self.row = None
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self.hidden:
            return
        (self.cell if self.cell is not None else self.buf).append(data)


def flatten_html(text, markdown):
    f = _Flattener(markdown)
    f.feed(text)
    f._flush()
    return f.lines


# ---------------------------------------------------------------- provider tools

class Meter:
    def __init__(self):
        self.calls, self.cost_usd, self.latency_ms = [], 0.0, 0

    def charge(self, tool, units, ok):
        unit, price, ms = PRICING[tool]
        cost = price * units if ok else 0.0
        lat = ms * units if ok else TIMEOUT_MS
        self.cost_usd += cost
        self.latency_ms += lat
        self.calls.append({"tool": tool, "units": units, "unit": unit, "ok": ok,
                           "est_cost_usd": round(cost, 4), "modeled_latency_ms": lat})

    def charge_llm(self, usage):
        self.cost_usd += usage["est_cost_usd"]
        self.latency_ms += usage["latency_ms"]
        self.calls.append({"tool": f"{usage['provider']}:{usage['model']}", "units": usage["input_tokens"] + usage["output_tokens"],
                           "unit": "token", "ok": True, "est_cost_usd": round(usage["est_cost_usd"], 4),
                           "modeled_latency_ms": usage["latency_ms"], "measured": True})


class ProviderSuite:
    """The sandbox a worker runs in. Moot controls it: which tools exist, fault injection for probes,
    metering, checkpoints of completed steps, and a live trace of everything the worker does."""

    def __init__(self, artifacts, faults=(), fail_steps=(), trace_ctx=None):
        self.artifacts = artifacts
        self.faults = list(faults)  # (tool or "*", substring of artifact path or "*")
        self.fail_steps = set(fail_steps)  # step names that time out (multi-step fault injection)
        self.trace_ctx = trace_ctx or {}
        self.meter = Meter()
        self.checkpoints = {}

    def trace(self, text, kind="step", **data):
        if self.trace_ctx:
            from .log import publish
            publish("trace", tkind=kind, text=text, **self.trace_ctx, **data)

    def step(self, name, detail=""):
        """Begin a named step; the sandbox may inject a timeout here."""
        self.trace(f"step: {name}" + (f" — {detail}" if detail else ""), kind="stepstart", step=name)
        if name in self.fail_steps:
            self.meter.charge("reader", 0, ok=False)
            self.trace(f"model provider timed out during '{name}' [injected fault]", kind="error")
            raise ProviderError(f"model provider timeout during step '{name}' [injected fault]")

    def checkpoint(self, name, value):
        self.checkpoints[name] = value
        self.trace(f"checkpoint saved: {name}", kind="checkpoint")

    def model(self, provider, step, system, user, schema, model=None, effort=None):
        from . import llm
        name = model or llm.PROVIDERS[provider]["model"]
        self.trace(f"→ {name} ({step})", kind="model")
        try:
            data, usage = llm.complete_json(provider, system, user, schema, model=model, effort=effort)
        except llm.LLMError as e:
            self.trace(f"model error: {e}", kind="error")
            raise ProviderError(f"{provider}: {e}")
        self.charge_llm(usage)
        self.trace(f"← {usage['input_tokens']:,} in / {usage['output_tokens']:,} out tok · {usage['latency_ms'] / 1000:.1f}s · "
                   f"${usage['est_cost_usd']:.4f}", kind="model")
        return data

    def charge_llm(self, usage):
        self.meter.charge_llm(usage)

    def _fault(self, tool, art):
        for t, match in self.faults:
            if t in ("*", tool) and (match == "*" or match in art["path"]):
                self.meter.charge(tool, 0, ok=False)
                self.trace(f"{tool}: upstream timeout after {TIMEOUT_MS}ms [injected fault]", kind="error")
                raise ProviderError(f"{tool}: upstream timeout after {TIMEOUT_MS}ms [injected fault]")

    def reducto_parse(self, aid):
        """Layout-aware document parse -> pages with detected tables (unit line + header + rows)."""
        art = self.artifacts.get(aid)
        if art["kind"] != "pdf":
            raise ProviderError("reducto: unsupported input type")
        self._fault("reducto", art)
        pages = split_pages(self.artifacts.read_text(aid))
        tables = []
        for pg in pages:
            context, block = None, []
            for line in pg["lines"] + [""]:
                if line.strip().startswith("|"):
                    block.append(line)
                    continue
                if block:
                    cells = [_cells(b) for b in block]
                    tables.append({"page": pg["page"], "unit_line": context, "header": cells[0],
                                   "rows": [{"cells": c, "raw": b} for c, b in zip(cells[1:], block[1:])]})
                    block = []
                if "(in " in line.lower():
                    context = line.strip()
        self.meter.charge("reducto", len(pages), ok=True)
        self.trace(f"reducto.parse → {len(pages)} pages, {len(tables)} tables · ${PRICING['reducto'][1] * len(pages):.3f}", kind="tool")
        return {"pages": pages, "tables": tables}

    def read_document(self, aid):
        """Plain reader for LLM agents: page-marked text for documents, flattened markdown for web pages."""
        art = self.artifacts.get(aid)
        self._fault("reader", art)
        text = self.artifacts.read_text(aid)
        if art["kind"] == "web":
            text = "\n".join(flatten_html(text, markdown=True))
        if len(text) > MAX_DOC_CHARS:
            raise ProviderError(f"reader: document has {len(text)} chars, above the {MAX_DOC_CHARS} single-pass limit")
        self.meter.charge("reader", art["pages"], ok=True)
        self.trace(f"reader.read → {len(text):,} chars ({art['kind']}, {art['pages']} page(s))", kind="tool")
        return {"text": text}

    def firecrawl_scrape(self, aid):
        """Scrape to markdown. PDFs are flattened (page boundaries are lost)."""
        art = self.artifacts.get(aid)
        self._fault("firecrawl", art)
        text = self.artifacts.read_text(aid)
        if art["kind"] == "pdf":
            pages = split_pages(text)
            lines = [l for p in pages for l in p["lines"]]
        else:
            lines = flatten_html(text, markdown=True)
        self.meter.charge("firecrawl", art["pages"], ok=True)
        self.trace(f"firecrawl.scrape → {len(lines)} markdown lines (page boundaries lost) · ${PRICING['firecrawl'][1] * art['pages']:.3f}", kind="tool")
        return {"markdown": "\n".join(lines)}

    def tavily_extract(self, aid):
        """Search/extract API -> plain text content for a URL/document."""
        art = self.artifacts.get(aid)
        self._fault("tavily", art)
        text = self.artifacts.read_text(aid)
        if art["kind"] == "pdf":
            lines = [l for p in split_pages(text) for l in p["lines"]]
        else:
            lines = flatten_html(text, markdown=False)
        self.meter.charge("tavily", 1, ok=True)
        self.trace(f"tavily.extract → {len(lines)} text snippets · ${PRICING['tavily'][1]:.3f}", kind="tool")
        return {"content": "\n".join(html.unescape(l) for l in lines)}


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]
