"""No-model test fixture; never registered by the production server."""
import copy
import threading
from pathlib import Path

from moot.contracts import MootError
from moot.pptx_render import render
from moot.store import Store, new_id


class FixtureStudio:
    def __init__(self, root):
        self.root = Path(root)
        self.store = Store(self.root / "fixture.db")
        self.run_count = 0
        self.release = threading.Event()
        self.release.set()
        self.fail = False

    def agents(self):
        return [{"id": "fixture", "available": True, "label": "TEST FIXTURE — no model"}]

    def create(self, request, url, sources, candidates, session_id=None):
        job = {"job_id": new_id("studio"), "tenant": "fixture", "status": "QUESTIONS",
               "request": request, "url": url, "questions": [
                   {"id": "audience", "type": "enum", "options": ["hackathon judges", "seed investors"], "default": "hackathon judges"},
                   {"id": "budget", "type": "number", "default": 3.0}],
               "answers": None, "history": [], "costs": {"total_usd": 0}, "candidates": {}}
        self.store.save_job(job)
        return job

    def _load(self, job_id):
        job = self.store.load_job(job_id)
        if not job:
            raise MootError("NOT_FOUND", "No such fixture job")
        return job

    def get(self, job_id):
        return self._load(job_id)

    def _state(self, job, state, why=""):
        job["history"].append({"from": job["status"], "to": state, "why": why})
        job["status"] = state
        self.store.save_job(job)

    def answer(self, job_id, answers):
        job = self.get(job_id)
        job["answers"] = {q["id"]: answers.get(q["id"], q["default"]) for q in job["questions"]}
        job["benchmark"] = {"summary": "TEST FIXTURE, not model judging"}
        self._state(job, "BENCHMARK_READY")
        return copy.deepcopy(job)

    def run(self, job_id):
        self.run_count += 1
        if not self.release.wait(10):
            raise TimeoutError("fixture blocked")
        if self.fail:
            raise RuntimeError("fixture failure")
        job = self.get(job_id)
        self._state(job, "TRIAL")
        deck = {"title": "Moot protocol test fixture", "slides": [
            {"title": "TEST FIXTURE", "layout": "title", "subtitle": "No live model was called.",
             "notes": "Fixture validates MCP transport and editable PPTX delivery."},
            {"title": "An editable workflow", "layout": "bullets", "bullets": ["Create, review, retrieve"],
             "visual": "flow", "visual_items": ["Create", "Review", "Retrieve"]}]}
        name = job_id + ".pptx"
        render(deck, self.root / name, "Moot fixture")
        score = {"total": 100, "qualified": True, "note": "Synthetic fixture score", "gates": []}
        job.update(winner="fixture", ranking=[{"worker_id": "fixture", "total": 100, "qualified": True}],
                   final={"pptx": name, "deck": deck, "score": score},
                   candidates={"fixture": {"status": "completed", "score": score}})
        self._state(job, "COMPLETED")
