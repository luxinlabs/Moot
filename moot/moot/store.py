"""SQLite persistence (stands in for Postgres in the MVP). Durable local state is the source of truth."""
import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, tenant TEXT, status TEXT, doc TEXT,
    created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, uri TEXT, kind TEXT, path TEXT,
    sha256 TEXT, pages INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS coordination_events(message_id TEXT PRIMARY KEY, correlation_id TEXT,
    event_type TEXT, sender TEXT, recipient TEXT, idempotency_key TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS evaluation_runs(run_id TEXT PRIMARY KEY, job_id TEXT, doc TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS execution_attempts(attempt_id TEXT PRIMARY KEY, job_id TEXT, worker_id TEXT,
    source_ref TEXT, status TEXT, doc TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS verification_results(verification_id TEXT PRIMARY KEY, job_id TEXT,
    attempt_id TEXT, tier TEXT, doc TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS capability_evidence(evidence_id TEXT PRIMARY KEY, worker_id TEXT,
    version_fingerprint TEXT, task_taxonomy TEXT, kind TEXT, ref_id TEXT, scope TEXT, doc TEXT,
    created_at REAL);
"""


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def insert(self, table, **cols):
        cols.setdefault("created_at", time.time())
        enc = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in cols.items()}
        keys = ", ".join(enc)
        marks = ", ".join("?" for _ in enc)
        with self.lock:
            self.db.execute(f"INSERT OR REPLACE INTO {table} ({keys}) VALUES ({marks})", list(enc.values()))
            self.db.commit()

    def rows(self, sql, args=()):
        out = []
        with self.lock:
            fetched = self.db.execute(sql, args).fetchall()
        for r in fetched:
            d = dict(r)
            if "doc" in d and d["doc"]:
                d["doc"] = json.loads(d["doc"])
            if "payload" in d and d["payload"]:
                d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def save_job(self, job):
        now = time.time()
        with self.lock:
            self._save_job(job, now)

    def _save_job(self, job, now):
        self.db.execute(
            "INSERT INTO jobs(job_id, tenant, status, doc, created_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, doc=excluded.doc, updated_at=excluded.updated_at",
            (job["job_id"], job["tenant"], job["status"], json.dumps(job), now, now))
        self.db.commit()

    def load_job(self, job_id):
        r = self.rows("SELECT doc FROM jobs WHERE job_id=?", (job_id,))
        return r[0]["doc"] if r else None
