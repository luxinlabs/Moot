"""Shared Studio operations for HTTP and MCP clients (one process per workspace)."""
import hashlib
import math
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import RUNTIME
from .contracts import MootError
from .log import CURRENT_JOB, publish
from .studio import load_skills, skills_version

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class StudioService:
    def __init__(self, studio, deck_dir=None):
        self.studio = studio
        self.deck_dir = Path(deck_dir or RUNTIME / "decks")
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio")

    def catalog(self):
        skills = load_skills()
        return {"agents": self.studio.agents(), "skills_version": skills_version(skills),
                "communication": self.communication(),
                "skills": [{k: s[k] for k in ("id", "title", "summary", "provenance") if k in s} for s in skills]}

    def communication(self, job_id=None):
        if job_id:
            self.get(job_id)
        transport = getattr(self.studio, "communication", None)
        if transport:
            return transport.status(job_id)
        return {"mode": "local", "configured": False, "identities": {}, "messages": [],
                "transport": "Local in-process coordination; Primitive is not enabled"}

    def create(self, request, url=None, sources=None, candidates=None, session_id=None):
        if not request or not request.strip():
            raise MootError("INVALID_REQUEST", "Describe the deck you want")
        if candidates is not None:
            known = {a["id"] for a in self.studio.agents()}
            if not candidates or set(candidates) - known:
                raise MootError("INVALID_CANDIDATES", "Choose at least one catalog candidate")
        return self.studio.create(request.strip(), url, sources or [], candidates, session_id=session_id)

    def answer_and_start(self, job_id, answers):
        # Serialize answer submission: duplicate requests must never charge twice.
        with self.lock:
            job = self.studio.get(job_id)
            if job["status"] != "QUESTIONS":
                types = {q["id"]: q["type"] for q in job.get("questions", [])}
                def same(k, value):
                    prior = (job.get("answers") or {}).get(k)
                    try:
                        return float(prior) == float(value) if types.get(k) == "number" else prior == value
                    except (TypeError, ValueError):
                        return False
                if job.get("answers") and all(same(k, v) for k, v in answers.items()):
                    return job
                raise MootError("BAD_STATE", "Answers already submitted; create a new job to change the brief")
            for q in job["questions"]:
                value = answers.get(q["id"], q["default"])
                if q["type"] == "number":
                    try:
                        valid = math.isfinite(float(value)) and float(value) > 0
                    except (TypeError, ValueError):
                        valid = False
                    if not valid:
                        raise MootError("INVALID_ANSWER", f"{q['id']} must be a positive finite number")
                elif value not in q["options"]:
                    raise MootError("INVALID_ANSWER", f"Choose a listed option for {q['id']}")
            unknown = set(answers) - {q["id"] for q in job["questions"]}
            if unknown:
                raise MootError("INVALID_ANSWER", f"Unknown question IDs: {sorted(unknown)}")
            result = self.studio.answer(job_id, answers)
            self.executor.submit(self._run, job_id)
            return result

    def _run(self, job_id):
        token = CURRENT_JOB.set(job_id)
        publish("busy", job_id=job_id)
        try:
            self.studio.run(job_id)
        except Exception as exc:
            traceback.print_exc()  # full detail in the server log
            detail = f"{type(exc).__name__}: {str(exc)[:160]}"
            job = self.studio._load(job_id)
            self.studio._state(job, "FAILED", f"Build failed: {detail}")
            publish("error", job_id=job_id, message=f"Build failed: {detail}")
        finally:
            publish("idle", job_id=job_id)
            CURRENT_JOB.reset(token)

    def get(self, job_id):
        return self.studio.get(job_id)

    def review(self, job_id):
        job = self.get(job_id)
        return {"job_id": job_id, "status": job["status"], "benchmark": job.get("benchmark"),
                "ranking": job.get("ranking"), "winner": job.get("winner"),
                "candidates": {w: {"score": c.get("score"), "status": c.get("status")}
                               for w, c in (job.get("candidates") or {}).items()},
                "final_score": (job.get("final") or {}).get("score"), "costs": job.get("costs"),
                "skills_version": job.get("skills_version"), "skill_provenance": job.get("skill_provenance", []),
                "limitations": ["Costs are estimates; the budget is not a provider-enforced billing cap.",
                                "Judges review structured slide content, not rendered slide images."]}

    def artifact(self, job_id):
        job = self.get(job_id)
        name = (job.get("final") or {}).get("pptx")
        if job["status"] != "COMPLETED" or not name:
            raise MootError("NOT_READY", "Poll moot_studio_get until COMPLETED before retrieving the deck")
        if not re.fullmatch(r"studio_[a-f0-9]+", job_id) or name != job_id + ".pptx":
            raise MootError("INVALID_ARTIFACT", "Unexpected deck filename")
        path = (self.deck_dir / name).resolve()
        if path.parent != self.deck_dir.resolve() or not path.is_file():
            raise MootError("NOT_FOUND", "Deck artifact is missing")
        data = path.read_bytes()
        return {"job_id": job_id, "filename": name, "mime_type": PPTX_MIME,
                "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "resource_uri": f"moot://decks/{job_id}/deck.pptx"}, data

    def close(self):
        transport = getattr(self.studio, "communication", None)
        if transport:
            transport.stop.set()
        self.executor.shutdown(wait=True)
        if transport:
            transport.close()
