"""Primitive-backed request/reply transport for Studio agents.

REST contract: https://www.primitive.dev/openapi.json (checked 2026-10-17).
Agents run in this process, but execute only after their inbox receives the task.
Payloads travel as JSON attachments; results must arrive back through Primitive.
"""
import base64
import contextvars
import hashlib
import hmac
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parseaddr
from urllib.parse import quote

import httpx

from .log import publish
from .store import new_id

MAX_ATTACHMENT = 2_000_000
MAIN = "moot-main-agent"
WORKERS = ("claude", "gpt", "groq")  # must match studio.CANDIDATE_SPECS ids, or Primitive mode has no address for them


class PrimitiveError(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class PrimitiveAPI:
    """Small authenticated REST adapter; HTTP client injection keeps tests offline."""
    def __init__(self, key, client=None):
        self.http = client or httpx.Client(base_url="https://api.primitive.dev/v1", timeout=40,
                                          headers={"Authorization": f"Bearer {key}"})

    def request(self, method, path, **kwargs):
        for attempt in range(4):
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise PrimitiveError("Primitive connection failed after retries") from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    try:
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    if delay > 30:
                        raise PrimitiveError(f"Primitive rate limited; retry after {delay:g} seconds")
                    time.sleep(max(0, delay))
                    continue
            if not response.is_success:
                # Do not expose response bodies that could include keys or source content.
                raise PrimitiveError(f"Primitive HTTP {response.status_code} for {method} {path}")
            return response
        raise PrimitiveError("Primitive retry limit reached")

    def json(self, method, path, **kwargs):
        result = self.request(method, path, **kwargs).json()
        if result.get("success") is False:
            raise PrimitiveError("Primitive rejected the request")
        return result

    def send(self, payload, key, reply_id=None):
        path = f"/emails/{quote(reply_id, safe='')}/reply" if reply_id else "/send-mail"
        result = self.json("POST", path, json=payload, headers={"Idempotency-Key": key})["data"]
        if result.get("status") in {"bounced", "gate_denied", "agent_failed", "failed", "canceled"} or result.get("rejected"):
            raise PrimitiveError("Primitive did not accept this message for delivery")
        return result

    def inbox(self, recipient, request_id, date_from):
        return self.json("GET", "/emails", params={"recipient": recipient, "search": request_id,
                         "date_from": date_from, "limit": 100})["data"]

    def email(self, email_id):
        return self.json("GET", f"/emails/{quote(email_id, safe='')}")["data"]

    def attachment(self, email_id, part):
        response = self.request("GET", f"/emails/{quote(email_id, safe='')}/attachments/{int(part)}")
        data = response.content
        if len(data) > MAX_ATTACHMENT:
            raise PrimitiveError("Attachment exceeds Moot's 2 MB limit")
        digest = response.headers.get("X-Content-SHA256")
        if digest and hashlib.sha256(data).hexdigest() != digest:
            raise PrimitiveError("Primitive attachment checksum mismatch")
        return data

    def close(self):
        self.http.close()


class PrimitiveTransport:
    label = "Primitive email (asynchronous request/reply)"

    def __init__(self, store, identities, api, signing_key, timeout=600, poll_interval=2):
        self.store, self.identities, self.api = store, identities, api
        self.signing_key = signing_key.encode()
        self.timeout, self.poll_interval = timeout, poll_interval
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="primitive-inbox")
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS primitive_messages(
            message_id TEXT PRIMARY KEY, job_id TEXT, sender TEXT, recipient TEXT, kind TEXT,
            status TEXT, provider_id TEXT, doc TEXT, created_at REAL)""")
        self.store.db.commit()

    @classmethod
    def from_env(cls, store):
        if os.getenv("MOOT_COMMUNICATION", "local") != "primitive":
            return None
        key = os.getenv("PRIMITIVE_API_KEY")
        domain = os.getenv("PRIMITIVE_DOMAIN", "").strip().lower()
        identities = json.loads(os.getenv("PRIMITIVE_IDENTITIES_JSON", "{}"))
        if domain:
            for worker in (MAIN, *WORKERS):
                identities.setdefault(worker, f"{worker.replace('_', '-')}@{domain}")
        if not key or not identities.get(MAIN):
            raise PrimitiveError("Primitive mode needs PRIMITIVE_API_KEY and PRIMITIVE_DOMAIN or PRIMITIVE_IDENTITIES_JSON")
        if len(set(identities.values())) != len(identities):
            raise PrimitiveError("Each Primitive agent needs a distinct email identity")
        for name, address in identities.items():
            if name not in (MAIN, *WORKERS) or not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", address):
                raise PrimitiveError("Invalid Primitive agent identity mapping")
        return cls(store, identities, PrimitiveAPI(key), os.getenv("PRIMITIVE_SIGNING_SECRET", key),
                   timeout=float(os.getenv("PRIMITIVE_REPLY_TIMEOUT", "600")))

    def status(self, job_id=None):
        rows = self.store.rows("SELECT * FROM primitive_messages" + (" WHERE job_id=?" if job_id else "") +
                               " ORDER BY created_at DESC LIMIT 60", (job_id,) if job_id else ())
        return {"mode": "primitive", "transport": self.label, "identities": self.identities,
                "messages": rows, "configured": True}

    def _record(self, envelope, status, provider_id=None):
        self.store.insert("primitive_messages", message_id=envelope["message_id"], job_id=envelope["job_id"],
                          sender=envelope["sender"], recipient=envelope["recipient"], kind=envelope["kind"],
                          status=status, provider_id=provider_id,
                          doc={"request_id": envelope["request_id"], "attachment": envelope["attachment"],
                               "text": envelope["text"], "from_address": self.identities[envelope["sender"]],
                               "to_address": self.identities[envelope["recipient"]]})
        worker = envelope["recipient"] if envelope["sender"] == MAIN else envelope["sender"]
        publish("communication", job_id=envelope["job_id"], worker_id=worker, transport="primitive",
                status=status, message_id=envelope["message_id"], provider_id=provider_id,
                sender=envelope["sender"], recipient=envelope["recipient"], kind=envelope["kind"],
                text=envelope["text"], attachment=envelope["attachment"])

    def _envelope(self, job_id, sender, recipient, kind, request_id, payload, text):
        if sender not in self.identities or recipient not in self.identities:
            raise PrimitiveError(f"No configured Primitive identity for {sender} or {recipient}")
        raw = canonical(payload)
        if len(raw) > MAX_ATTACHMENT:
            raise PrimitiveError("Coordination payload exceeds Moot's 2 MB attachment limit")
        envelope = {"protocol": "moot.primitive.v1", "message_id": new_id("mail"), "job_id": job_id,
                    "request_id": request_id, "sender": sender, "recipient": recipient, "kind": kind,
                    "at": time.time(), "text": text[:2000],
                    "attachment": {"filename": "payload.json", "sha256": hashlib.sha256(raw).hexdigest(),
                                   "size_bytes": len(raw), "content_type": "application/json"}}
        envelope["signature"] = hmac.new(self.signing_key, canonical(envelope), hashlib.sha256).hexdigest()
        attachment = {"filename": "payload.json", "content_type": "application/json",
                      "content_base64": base64.b64encode(raw).decode()}
        return envelope, attachment

    def _send(self, envelope, attachment, reply_id=None):
        body = {"from": self.identities[envelope["sender"]], "body_text": canonical(envelope).decode(),
                "attachments": [attachment]}
        if not reply_id:
            body.update(to=self.identities[envelope["recipient"]], subject=f"Moot {envelope['request_id']}: {envelope['kind']}")
        self._record(envelope, "sending")
        try:
            sent = self.api.send(body, envelope["message_id"], reply_id)
        except Exception:
            self._record(envelope, "send_failed")
            raise
        self._record(envelope, "accepted", sent["id"])
        return sent

    def _read(self, email, expected):
        parsed = email.get("parsed") or {}
        body = email.get("body_text") or parsed.get("body_text") or ""
        if len(body) > 16000:
            raise PrimitiveError("Unexpected oversized coordination envelope")
        envelope = json.loads(body)
        signature = envelope.pop("signature", "")
        digest = hmac.new(self.signing_key, canonical(envelope), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature.encode(), digest.encode()):
            raise PrimitiveError("Untrusted coordination signature")
        if any(envelope.get(k) != expected[k] for k in ("protocol", "message_id", "job_id", "request_id", "sender", "recipient", "kind")):
            raise PrimitiveError("Message does not match the pending request")
        if parseaddr(email.get("from_email", ""))[1].lower() != self.identities[envelope["sender"]].lower():
            raise PrimitiveError("Unexpected sender")
        if email.get("recipient", "").lower() != self.identities[envelope["recipient"]].lower():
            raise PrimitiveError("Unexpected recipient")
        # Replies must not be redirected away from the authenticated agent.
        if any(a.get("address", a.get("email", "")).lower() != self.identities[envelope["sender"]].lower()
               for a in parsed.get("reply_to") or []):
            raise PrimitiveError("Unexpected Reply-To address")
        meta = envelope["attachment"]
        if meta != expected["attachment"] or meta["size_bytes"] > MAX_ATTACHMENT:
            raise PrimitiveError("Attachment manifest mismatch")
        matches = [a for a in parsed.get("attachments", []) if a.get("filename") == meta["filename"]]
        if len(matches) != 1:
            raise PrimitiveError("Expected exactly one payload.json attachment")
        if matches[0].get("size_bytes") != meta["size_bytes"]:
            raise PrimitiveError("Attachment metadata size mismatch")
        raw = self.api.attachment(email["id"], matches[0]["part_index"])
        if len(raw) != meta["size_bytes"] or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            raise PrimitiveError("Attachment content failed integrity verification")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise PrimitiveError("Coordination payload must be a JSON object")
        envelope["signature"] = signature
        self._record(envelope, "received", email["id"])
        return value

    def _wait(self, expected, deadline, canceled):
        since = datetime.fromtimestamp(expected["at"] - 60, timezone.utc).isoformat()
        ignored = set()
        while time.monotonic() < deadline and not self.stop.is_set() and not canceled.is_set():
            for summary in self.api.inbox(self.identities[expected["recipient"]], expected["request_id"], since):
                if summary["id"] in ignored:
                    continue
                email = self.api.email(summary["id"])
                try:
                    value = self._read(email, expected)
                except PrimitiveError as exc:
                    if "HTTP" in str(exc) or "connection failed" in str(exc):
                        raise
                    ignored.add(summary["id"])
                    continue
                except (ValueError, KeyError, TypeError):
                    ignored.add(summary["id"])
                    continue
                return value, email
            canceled.wait(self.poll_interval)
        self._record(expected, "timed_out")
        raise PrimitiveError("Timed out waiting for a verified Primitive reply; no local fallback was used")

    def exchange(self, job_id, sender, recipient, kind, payload, handler, text):
        """Deliver a request, execute handler on inbox receipt, deliver its threaded response."""
        request_id = new_id("exchange")
        request, attachment = self._envelope(job_id, sender, recipient, kind, request_id, payload, text)
        deadline = time.monotonic() + self.timeout
        canceled = threading.Event()
        reply_ready = threading.Event()
        reply_holder = {}
        self._send(request, attachment)

        def receive_and_handle():
            incoming, email = self._wait(request, deadline, canceled)
            result = handler(incoming)
            reply_holder["result"] = result
            if canceled.is_set() or self.stop.is_set():
                raise PrimitiveError("Exchange canceled before reply")
            reply, attached = self._envelope(job_id, recipient, sender, kind + "_reply", request_id, result,
                                             str(result.get("answer") or f"{kind} completed; result attached"))
            reply_holder["envelope"] = reply
            self._send(reply, attached, reply_id=email["id"])
            reply_ready.set()

        worker = self.pool.submit(contextvars.copy_context().run, receive_and_handle)
        try:
            while not reply_ready.wait(.1):
                if worker.done():
                    worker.result()
                if self.stop.is_set() or time.monotonic() >= deadline:
                    raise PrimitiveError("Primitive exchange timed out")
            result, _ = self._wait(reply_holder["envelope"], deadline, canceled)
            worker.result()
            return result
        except PrimitiveError as exc:
            exc.cost_usd = reply_holder.get("result", {}).get("cost_usd", 0)
            self._record(request, "exchange_failed")
            raise
        finally:
            canceled.set()

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.api.close()
