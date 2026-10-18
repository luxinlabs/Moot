"""Moot web UI: a human posts a task, picks candidate agents (baseline tools, Claude, GPT, GLM) and watches
Moot clarify, audition, hire, verify and recover live. The UI calls the same orchestrator the MCP server uses.

    python3 -m moot.web            # http://127.0.0.1:8787
"""
import argparse
import json
import re
import threading
import time
import traceback
from collections import defaultdict
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from . import DATA, RUNTIME, llm, waitlist
from .contracts import MootError
from .intake import parse_task
from .log import CURRENT_JOB, SINKS, publish
from .orchestrator import Orchestrator
from .studio import Studio, load_skills, skills_version
from .studio_service import StudioService
from .outreach import Outreach

STATIC = Path(__file__).resolve().parent / "static"
app = Flask(__name__, static_folder=None)
app.register_blueprint(waitlist.bp)  # /join also works locally; phones use the separate --waitlist server
EVENTS = defaultdict(list)
EV_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()  # one job operation at a time (auditions inside it still run in parallel)
JOB_AGENTS = {}
ORCH = None
STUDIO = None
DECKS = None
OUTREACH = None


def _sink(ev):
    if ev.get("job_id"):
        with EV_LOCK:
            evs = _events(ev["job_id"])
            evs.append(ev)
            if ev["kind"] != "log":  # persist the replayable timeline (sandbox traces, decisions, ...) for reloads
                ORCH.store.insert("ui_events", job_id=ev["job_id"], seq=len(evs), doc=ev)


def _events(job_id):
    """In-memory event log for a job, rehydrated from SQLite after a server restart."""
    if job_id not in EVENTS:
        EVENTS[job_id] = [r["doc"] for r in ORCH.store.rows("SELECT doc FROM ui_events WHERE job_id=? ORDER BY seq", (job_id,))]
    return EVENTS[job_id]


def _err(e, status=400):
    if isinstance(e, MootError):
        return jsonify(e.to_dict()), status
    return jsonify({"error": {"code": "ERROR", "message": str(e)}}), status


def _background(job_id, fn):
    def run():
        CURRENT_JOB.set(job_id)
        publish("busy", job_id=job_id)
        try:
            with RUN_LOCK:
                fn()
        except MootError as e:
            publish("error", job_id=job_id, message=f"{e.code}: {e.message}")
        except Exception as e:
            traceback.print_exc()
            publish("error", job_id=job_id, message=str(e))
        finally:
            publish("idle", job_id=job_id)
    threading.Thread(target=run, daemon=True).start()


def _job_view(job_id):
    doc = ORCH._job(job_id)
    view = {k: doc.get(k) for k in ("job_id", "status", "objective", "questions", "contract", "decision", "costs",
                                    "execution_estimate_usd", "evaluation_run_id", "required_fields", "simulate_outage",
                                    "task_family", "benchmark_preview", "designer_note")}
    view["sources"] = [{k: a[k] for k in ("uri", "artifact_id", "kind", "pages")} for a in doc["sources"]]
    view["result"] = ORCH.get_job(job_id)
    view["selected_agents"] = JOB_AGENTS.get(job_id)
    view["intake"] = doc.get("intake")
    return view


# ------------------------------------------------------------------ static + catalog
@app.get("/")
def landing_page():
    return send_from_directory(STATIC, "landing.html")


@app.get("/studio")
def studio_page():
    return send_from_directory(STATIC, "studio.html")


@app.get("/api/waitlist/info")
def waitlist_info():
    return jsonify({"join_url": waitlist.STATE["join_url"], "count": waitlist.count()})


@app.get("/api/waitlist.csv")
def waitlist_csv():
    if request.remote_addr not in ("127.0.0.1", "::1"):  # names and emails never leave this machine via the web
        return _err(ValueError("the waitlist export is available only on this machine"), 403)
    return Response(waitlist.export_csv(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=moot-waitlist.csv"})


@app.get("/how")
def how_page():
    return send_from_directory(STATIC, "how.html")


@app.get("/lab")
def index():
    return send_from_directory(STATIC, "index.html")


# ------------------------------------------------------------------ Deck Studio
@app.get("/api/studio/agents")
def studio_agents():
    return jsonify(STUDIO.agents())


@app.get("/api/studio/skills")
def studio_skills():
    return jsonify({"version": skills_version(), "skills": load_skills()})


@app.get("/api/studio/communication")
def studio_communication():
    try:
        return jsonify(DECKS.communication(request.args.get("job_id")))
    except MootError as e:
        return _err(e, 404)


@app.get("/api/studio/jobs")
def studio_jobs():
    rows = ORCH.store.rows("SELECT job_id, status, doc, updated_at FROM jobs ORDER BY updated_at DESC LIMIT 30")
    return jsonify([{"job_id": r["job_id"], "status": r["status"], "request": r["doc"].get("request", "")}
                    for r in rows if r["doc"].get("task_family") == "pitch_deck_studio"][:15])


@app.post("/api/studio/jobs")
def studio_create():
    b = request.get_json(force=True)
    if not (b.get("request") or "").strip():
        return _err(ValueError("describe the deck you want"))
    session_id = request.headers.get("X-Moot-Session-Id")
    try:
        job = DECKS.create(b["request"].strip(), (b.get("url") or "").strip() or None, b.get("sources") or [], b.get("candidates"), session_id=session_id)
    except llm.LLMError as e:
        return _err(ValueError(f"main agent unavailable: {e}"))
    except MootError as e:
        return _err(e)
    return jsonify(job)


@app.get("/api/studio/jobs/<job_id>")
def studio_get(job_id):
    try:
        return jsonify(STUDIO.get(job_id))
    except MootError as e:
        return _err(e, 404)


@app.post("/api/studio/jobs/<job_id>/answers")
def studio_answers(job_id):
    try:
        job = DECKS.answer_and_start(job_id, request.get_json(force=True) or {})
    except llm.LLMError as e:
        return _err(ValueError(f"main agent unavailable: {e}"))
    except MootError as e:
        return _err(e)
    return jsonify(job)


@app.post("/api/studio/jobs/<job_id>/rerun")
def studio_rerun(job_id):
    """Re-run a job's brief and answers with the current candidates (e.g. a job from an older line-up)."""
    try:
        new_id, answers = STUDIO.rerun(job_id)
        return jsonify(DECKS.answer_and_start(new_id, answers))
    except llm.LLMError as e:
        return _err(ValueError(f"main agent unavailable: {e}"))
    except MootError as e:
        return _err(e)


@app.get("/api/studio/jobs/<job_id>/deck.pptx")
def studio_pptx(job_id):
    job = STUDIO.get(job_id)
    if not (job.get("final") or {}).get("pptx"):
        return _err(ValueError("no final deck yet"), 404)
    name = re.sub(r"[^A-Za-z0-9]+", "_", (job.get("product") or {}).get("name", "deck")).strip("_") or "deck"
    return send_file(RUNTIME / "decks" / job["final"]["pptx"], as_attachment=True, download_name=f"{name}_pitch_deck.pptx")


@app.get("/api/studio/jobs/<job_id>/skills/<worker_id>/<name>")
def studio_skill_md(job_id, worker_id, name):
    """A candidate's own skill for a job: SKILL.md (refined, v2) or SKILL.v1.md (before practice)."""
    if name not in ("SKILL.md", "SKILL.v1.md") or not re.fullmatch(r"[a-z0-9_]+", worker_id) or not re.fullmatch(r"studio_[a-f0-9]+", job_id):
        return _err(ValueError("unknown skill file"), 404)
    path = RUNTIME / "skills" / job_id / worker_id / name
    if not path.exists():
        return _err(ValueError("this candidate has no skill file for this job"), 404)
    return send_file(path, mimetype="text/markdown", as_attachment=False, download_name=f"{worker_id}_{name}")


# ------------------------------------------------------------------ outreach (human approval required to send)
def _human(action):
    """Approve/send must come from the Studio page itself: it sets this header, which cross-site forms cannot."""
    if request.headers.get("X-Moot-Human-Action") != action:
        raise MootError("HUMAN_ACTION_REQUIRED", "this action must be confirmed by you in the Studio page")
    if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
        raise MootError("HUMAN_ACTION_REQUIRED", "cross-site requests cannot approve or send email")
    return f"ui-user ({request.headers.get('X-Moot-Session-Id') or 'no session'})"


@app.get("/api/outreach/config")
def outreach_config():
    return jsonify(OUTREACH.config())


@app.get("/api/studio/jobs/<job_id>/outreach")
def outreach_list(job_id):
    return jsonify(OUTREACH.list(job_id))


@app.post("/api/studio/jobs/<job_id>/outreach/drafts")
def outreach_draft(job_id):
    b = request.get_json(force=True) or {}
    try:
        return jsonify(OUTREACH.draft(job_id, b.get("audience"), b.get("recipients", ""), b.get("sender_name", ""),
                                      b.get("sender_local", "founder"), b.get("signature", ""), bool(b.get("attach_deck", True)),
                                      (b.get("note") or "")[:500], b.get("contact_ids") or [], b.get("template")))
    except MootError as e:
        return _err(e)


@app.get("/api/contacts")
def contacts_list():
    return jsonify(OUTREACH.contacts(request.args.get("q", ""), request.args.get("type", "")))


@app.post("/api/contacts")
def contacts_add():
    b = request.get_json(force=True) or {}
    return jsonify(OUTREACH.add_contacts(b.get("csv", ""), b.get("contact"), b.get("default_type", "investor")))


@app.delete("/api/contacts/<contact_id>")
def contacts_delete(contact_id):
    return jsonify(OUTREACH.delete_contact(contact_id))


@app.post("/api/contacts/explore")
def contacts_explore():
    b = request.get_json(force=True) or {}
    if not (b.get("prompt") or "").strip():
        return _err(ValueError("describe who you're looking for"))
    try:
        return jsonify(OUTREACH.explore(b["prompt"].strip()[:500], b.get("type", "")))
    except llm.LLMError as e:
        return _err(ValueError(f"exploration unavailable: {e}"))


@app.post("/api/outreach/approve-batch")
def outreach_approve_batch():
    try:
        return jsonify(OUTREACH.approve_many((request.get_json(force=True) or {}).get("ids") or [], _human("approve")))
    except MootError as e:
        return _err(e, 403 if e.code == "HUMAN_ACTION_REQUIRED" else 400)


@app.patch("/api/outreach/<draft_id>")
def outreach_edit(draft_id):
    b = request.get_json(force=True) or {}
    try:
        return jsonify(OUTREACH.edit(draft_id, b.get("subject", ""), b.get("body", "")))
    except MootError as e:
        return _err(e)


@app.post("/api/outreach/<draft_id>/approve")
def outreach_approve(draft_id):
    try:
        return jsonify(OUTREACH.approve(draft_id, _human("approve")))
    except MootError as e:
        return _err(e, 403 if e.code == "HUMAN_ACTION_REQUIRED" else 400)


@app.post("/api/outreach/<draft_id>/reject")
def outreach_reject(draft_id):
    try:
        return jsonify(OUTREACH.reject(draft_id))
    except MootError as e:
        return _err(e)


@app.post("/api/outreach/send")
def outreach_send():
    try:
        approver = _human("send")
        return jsonify(OUTREACH.send((request.get_json(force=True) or {}).get("confirm") or {}, approver))
    except MootError as e:
        return _err(e, 403 if e.code == "HUMAN_ACTION_REQUIRED" else 400)


@app.get("/api/outreach/<draft_id>.eml")
def outreach_eml(draft_id):
    import io
    try:
        data = OUTREACH.eml(draft_id)
    except MootError as e:
        return _err(e, 404)
    return send_file(io.BytesIO(data), mimetype="message/rfc822", as_attachment=True, download_name=f"{draft_id}.eml")


@app.get("/api/agents")
def agents():
    out = []
    for w in ORCH.workers.values():
        ok, why = w.available()
        reg = w.registration()
        # Add time estimate for a typical single-page document
        sample_art = {"pages": 1, "artifact_id": "sample"}
        est_time = w.estimate_time(sample_art) if hasattr(w, "estimate_time") else None
        # Add cost estimate for deck agents (trial vs final)
        est_cost = w.estimate_cost(sample_art) if hasattr(w, "estimate_cost") else None
        trial_cost = est_cost.get("trial_usd") if isinstance(est_cost, dict) else None
        final_cost = est_cost.get("final_usd") if isinstance(est_cost, dict) else None
        agent_data = {
            "worker_id": w.worker_id,
            "display": w.display,
            "kind": "llm" if hasattr(w, "provider") else "baseline",
            "families": list(w.families),
            "provider": getattr(w, "provider", None),
            "model": w.model,
            "tools": list(w.tools),
            "supported_inputs": list(w.supported_inputs),
            "pricing": reg["pricing"],
            "fingerprint": w.fingerprint(),
            "available": ok,
            "unavailable_reason": why,
            "estimate_time_seconds": round(est_time, 1) if est_time else None,
            "estimate_cost_usd": est_cost if isinstance(est_cost, (int, float)) else None,
            "estimate_cost_trial_usd": trial_cost,
            "estimate_cost_final_usd": final_cost,
            "description": " ".join((type(w).__doc__ or "").split())
        }
        out.append(agent_data)
    return jsonify(out)


@app.get("/api/settings")
def get_settings():
    return jsonify(llm.public_settings())


@app.post("/api/settings")
def set_settings():
    body = request.get_json(force=True)
    for pid, cfg in body.items():
        if pid in llm.PROVIDERS:
            llm.configure(pid, api_key=cfg.get("api_key"), model=cfg.get("model"), base_url=cfg.get("base_url"),
                          price=cfg.get("price_per_mtok"))
    return jsonify(llm.public_settings())


@app.get("/api/benchmarks")
def benchmarks():
    """Each task family's test suite (no answers) plus a leaderboard built from capability memory."""
    out = {}
    for name, fam in ORCH.families.items():
        tests = [{k: f.get(k) for k in ("id", "stage", "modality", "description", "checks")} for f in fam.evaluator.fixtures]
        board = []
        rows = ORCH.store.rows("SELECT * FROM capability_evidence WHERE kind='audition_summary' AND task_taxonomy LIKE ? "
                               "ORDER BY created_at DESC", (name.split("_")[0] + "%",))
        latest = {}
        for r in rows:
            latest.setdefault((r["task_taxonomy"], r["worker_id"]), r)
        by_tax = {}
        for (tax, wid), r in latest.items():
            if wid in ORCH.workers:
                by_tax.setdefault(tax, []).append(r)
        for tax, rs in by_tax.items():
            summaries = [r["doc"] for r in rs]
            plan = {"probe_stages": sorted({st for s in summaries for st in s["stages_covered"]}), "judge": True}
            dec = fam.evaluator.decide(summaries, {"constraints": {"priority": "quality"}}, plan)
            meta = {r["worker_id"]: r for r in rs}
            for rank in dec["ranking"]:
                r = meta[rank["worker_id"]]
                board.append({"taxonomy": tax, "worker_id": rank["worker_id"], "score": rank["score"],
                              "qualified": rank["qualified"], "failed_gates": rank["failed_gates"],
                              "checks": f"{r['doc']['fields_pass']}/{r['doc']['fields_total']}", "n_tests": r["doc"]["n_fixtures"],
                              "fixture_results": r["doc"].get("fixture_results", []),
                              "current_version": r["version_fingerprint"] == ORCH.workers[r["worker_id"]].fingerprint(),
                              "age_minutes": round((time.time() - r["created_at"]) / 60), "audition_cost_usd": r["doc"]["audition_cost_usd"]})
        out[name] = {"label": fam.label, "evaluator_version": fam.evaluator.VERSION, "tests": tests, "leaderboard": board}
    return jsonify(out)


@app.get("/api/library")
def library():
    items = []
    for p in sorted((DATA / "sources").rglob("*")):
        if p.is_file() and p.suffix in (".txt", ".html", ".htm", ".md"):
            rel = p.relative_to(DATA).as_posix()
            kind = "pdf" if rel.endswith(".pdf.txt") else "web" if p.suffix in (".html", ".htm") else "doc"
            items.append({"uri": f"file://{rel}", "name": p.name, "kind": kind,
                          "group": "uploads" if "/uploads/" in rel else "brief" if "/briefs/" in rel else "sample"})
    for p in sorted((DATA / "web_mirror").rglob("*.html")):
        rel = p.relative_to(DATA / "web_mirror").as_posix()
        items.append({"uri": "https://" + rel[:-5], "name": "https://" + rel[:-5], "kind": "web", "group": "web"})
    return jsonify(items)


@app.post("/api/upload")
def upload():
    body = request.get_json(force=True)
    text, name = body.get("text") or "", (body.get("name") or "document").strip()
    if not text.strip():
        return _err(ValueError("document is empty"))
    if len(text) > 300_000:
        return _err(ValueError("document is larger than 300k characters"))
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:60] or "document"
    is_html = bool(re.search(r"<(html|body|table|p)\b", text, re.I))
    if not is_html and "=== Page" not in text:
        text = "=== Page 1 ===\n" + text
    path = DATA / "sources" / "uploads" / (f"{slug}.html" if is_html else f"{slug}.pdf.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    rel = path.relative_to(DATA).as_posix()
    return jsonify({"uri": f"file://{rel}", "name": path.name, "kind": "web" if is_html else "pdf", "group": "uploads"})


# ------------------------------------------------------------------ jobs
@app.post("/api/jobs")
def create_job():
    b = request.get_json(force=True)
    task = (b.get("task") or "").strip()
    if not task:
        return _err(ValueError("describe the task first"))
    spec = parse_task(task)
    if not spec["in_scope"]:
        return _err(MootError("OUT_OF_SCOPE", "This Moot MVP hires agents for two kinds of work: financial field "
                                              "extraction from reports/sites, and pitch decks from a startup brief."))
    c = spec["constraints"]
    if b.get("budget") not in (None, ""):
        c["max_total_cost_usd"] = float(b["budget"])
    if b.get("priority"):
        c["priority"] = b["priority"]
    if b.get("recurring") is not None:
        spec["workflow"].update(recurring=bool(b["recurring"]), follow_up_updates_expected=bool(b["recurring"]))
    domains = sorted({re.sub(r"^https?://([^/]+).*", r"\1", s) for s in b.get("sources", []) if s.startswith("http")})
    try:
        out = ORCH.create_job(objective=spec["objective"], sources=[{"uri": s} for s in b.get("sources", [])],
                              required_fields=spec["required_fields"], output_schema_id=spec["output_schema_id"],
                              constraints=c, workflow=spec["workflow"],
                              authorization={"approved_domains": domains, "approved_by": "ui-user"},
                              simulate_outage=bool(b.get("simulate_outage")), task_family=spec["task_family"],
                              ask_benchmark_questions=True)
    except MootError as e:
        return _err(e)
    job = ORCH._job(out["job_id"])
    job["intake"] = {k: spec[k] for k in ("intake_by", "note", "output_schema_id", "task_family")}
    ORCH.store.save_job(job)
    JOB_AGENTS[out["job_id"]] = b.get("agents") or None
    return jsonify(_job_view(out["job_id"]))


@app.get("/api/jobs")
def list_jobs():
    rows = ORCH.store.rows("SELECT job_id, status, doc, updated_at FROM jobs ORDER BY updated_at DESC LIMIT 15")
    return jsonify([{"job_id": r["job_id"], "status": r["status"], "objective": r["doc"]["objective"],
                     "updated_at": r["updated_at"]} for r in rows])


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    try:
        return jsonify(_job_view(job_id))
    except MootError as e:
        return _err(e, 404)


@app.post("/api/jobs/<job_id>/answers")
def answers(job_id):
    try:
        ORCH.answer_questions(job_id, request.get_json(force=True))
        return jsonify(_job_view(job_id))
    except (MootError, ValueError) as e:
        return _err(e)


@app.get("/api/jobs/<job_id>/candidates")
def candidates(job_id):
    try:
        return jsonify(ORCH.find_candidates(job_id))
    except MootError as e:
        return _err(e)


@app.post("/api/jobs/<job_id>/evaluate")
def evaluate(job_id):
    def go():
        rec = ORCH.run_audition(job_id, candidate_ids=JOB_AGENTS.get(job_id))
        if not rec.get("recommendation"):
            return
        if not rec.get("approval_required"):
            ORCH.hire_and_execute(job_id)  # within the workspace auto-approve limit
            return
        job = ORCH._job(job_id)
        budget, spent, est = job["contract"]["constraints"]["max_total_cost_usd"], job["costs"]["total_usd"], rec["execution_estimate_usd"]
        if spent + est <= budget:
            # the person set this budget in the contract; that is the authorization for spend within it
            publish("auto_approved", job_id=job_id, estimate_usd=est, remaining_usd=round(budget - spent, 4),
                    recommendation=rec["recommendation"])
            ORCH.hire_and_execute(job_id, authorization={"approved_by": "ui-user (contract budget)",
                                                         "spend_cap_usd": round(budget - spent, 4)})
        else:
            publish("approval_required", job_id=job_id, estimate_usd=est, recommendation=rec["recommendation"],
                    remaining_usd=round(budget - spent, 4))
    _background(job_id, go)
    return jsonify({"accepted": True})


@app.post("/api/jobs/<job_id>/approve")
def approve(job_id):
    b = request.get_json(force=True) or {}
    auth = {"approved_by": "ui-user", "spend_cap_usd": float(b.get("spend_cap_usd", 0))}
    _background(job_id, lambda: ORCH.hire_and_execute(job_id, worker_id=b.get("worker_id") or "auto", authorization=auth))
    return jsonify({"accepted": True})


@app.get("/api/runs/<run_id>")
def run(run_id):
    try:
        return jsonify(ORCH.get_evaluation(run_id))
    except MootError as e:
        return _err(e, 404)


@app.get("/api/workers/<worker_id>/passport")
def passport(worker_id):
    try:
        return jsonify(ORCH.get_worker_passport(worker_id))
    except MootError as e:
        return _err(e, 404)


@app.get("/api/jobs/<job_id>/eventcount")
def eventcount(job_id):
    with EV_LOCK:
        return jsonify({"count": len(_events(job_id))})


@app.get("/api/jobs/<job_id>/events")
def events(job_id):
    start = int(request.args.get("from", 0))

    def stream():
        i, last = start, time.time()
        while True:
            with EV_LOCK:
                batch = _events(job_id)[i:]
            for ev in batch:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
            i += len(batch)
            if time.time() - last > 15:
                yield ": keepalive\n\n"
                last = time.time()
            time.sleep(0.2)
    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def initialize(db=None):
    """Initialize one shared workspace for the web UI and Manufact MCP server."""
    global ORCH, STUDIO, DECKS, OUTREACH
    if DECKS is not None:
        return DECKS
    llm.load_dotenv()
    RUNTIME.mkdir(exist_ok=True)
    ORCH = Orchestrator(db or RUNTIME / "web.db", tenant="ui-workspace", include_llm=True)
    STUDIO = Studio(ORCH)
    STUDIO.recover_interrupted()
    DECKS = StudioService(STUDIO)
    OUTREACH = Outreach(STUDIO)  # drafts via the agent; approve/send exist only as human web routes below, not as MCP tools
    ORCH.store.db.execute("CREATE TABLE IF NOT EXISTS ui_events(job_id TEXT, seq INTEGER, doc TEXT, created_at REAL, "
                          "PRIMARY KEY(job_id, seq))")
    if _sink not in SINKS:
        SINKS.append(_sink)
    return DECKS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--waitlist", action="store_true", help="Serve the waitlist sign-up form to phones on this network")
    ap.add_argument("--waitlist-port", type=int, default=8788)
    ap.add_argument("--public-url", help="Public URL for the QR code, e.g. a tunnel to the waitlist port")
    a = ap.parse_args()
    initialize()
    if a.waitlist:
        waitlist.start_server(a.waitlist_port, a.public_url)
    print(f"Moot UI on http://{a.host}:{a.port}  (Deck Studio at /studio)")
    app.run(host=a.host, port=a.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
