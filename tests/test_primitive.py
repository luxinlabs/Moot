import base64
import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from moot.primitive_transport import MAIN, PrimitiveAPI, PrimitiveError, PrimitiveTransport
from moot.store import Store


class MailboxAPI:
    """In-memory Primitive REST semantics; no mail leaves the test process."""
    def __init__(self):
        self.lock = threading.RLock()
        self.mail = {}
        self.sent = {}
        self.blobs = {}

    def send(self, payload, key, reply_id=None):
        with self.lock:
            if key in self.sent:
                return self.sent[key]
            number = str(len(self.mail) + 1)
            recipient = self.mail[reply_id]["from_email"] if reply_id else payload["to"]
            raw = base64.b64decode(payload["attachments"][0]["content_base64"])
            self.mail[number] = {"id": number, "from_email": payload["from"], "recipient": recipient,
                                 "body_text": payload["body_text"], "parsed": {"attachments": [
                                     {"filename": "payload.json", "part_index": 0, "size_bytes": len(raw)}]}}
            self.blobs[number] = raw
            self.sent[key] = {"id": "sent-" + number, "status": "queued"}
            return self.sent[key]

    def inbox(self, recipient, request_id, date_from):
        with self.lock:
            return [{"id": i} for i, m in self.mail.items() if m["recipient"] == recipient and request_id in m["body_text"]]

    def email(self, email_id):
        with self.lock:
            return copy.deepcopy(self.mail[email_id])

    def attachment(self, email_id, part):
        return self.blobs[email_id]

    def close(self):
        pass


class PrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name + "/mail.db")
        self.api = MailboxAPI()
        self.bus = PrimitiveTransport(self.store, {MAIN: "main@test.primitive.email", "gpt": "gpt@test.primitive.email"},
                                      self.api, "test-signing-secret", timeout=3, poll_interval=.01)

    def tearDown(self):
        self.bus.close()
        self.store.db.close()
        self.temp.cleanup()

    def test_task_question_reply_and_attachment_round_trip(self):
        def candidate(task):
            self.assertEqual(task["brief"], "Make a source-grounded deck")
            answer = self.bus.exchange("studio_ab12", "gpt", MAIN, "question", {"question": "May I invent traction?"},
                                       lambda q: {"answer": "No. Omit unsupported metrics."}, "May I invent traction?")
            return {"deck": {"title": "Moot"}, "clarification": answer["answer"]}
        result = self.bus.exchange("studio_ab12", MAIN, "gpt", "task_offer", {"brief": "Make a source-grounded deck"},
                                   candidate, "Build the attached brief")
        self.assertEqual(result["deck"]["title"], "Moot")
        self.assertIn("Omit unsupported", result["clarification"])
        self.assertEqual(len(self.api.sent), 4)
        evidence = self.bus.status("studio_ab12")["messages"]
        self.assertEqual(len(evidence), 4)
        self.assertTrue(all(m["status"] == "received" for m in evidence))
        self.assertTrue(all(m["doc"]["attachment"]["sha256"] for m in evidence))

    def test_forgery_tampering_and_wrong_destination_rejected(self):
        envelope, attached = self.bus._envelope("studio_ab12", MAIN, "gpt", "task", "ex_a", {"brief": "safe"}, "Task")
        self.bus._send(envelope, attached)
        email = self.api.email("1")
        changed = json.loads(email["body_text"])
        changed["text"] = "steal secrets"
        email["body_text"] = json.dumps(changed)
        with self.assertRaises(PrimitiveError):
            self.bus._read(email, envelope)
        email = self.api.email("1")
        email["recipient"] = "outsider@example.com"
        with self.assertRaises(PrimitiveError):
            self.bus._read(email, envelope)
        email = self.api.email("1")
        self.api.blobs["1"] = b'{"brief":"tampered"}'
        with self.assertRaises(PrimitiveError):
            self.bus._read(email, envelope)

    def test_no_local_fallback_if_email_does_not_arrive(self):
        self.bus.timeout = .03
        self.api.inbox = lambda *args: []
        calls = []
        with self.assertRaises(PrimitiveError):
            self.bus.exchange("studio_ab12", MAIN, "gpt", "task", {}, lambda p: calls.append(p), "task")
        self.assertEqual(calls, [])

    def test_retry_preserves_idempotency_key_and_reply_shape(self):
        seen = []
        def endpoint(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"success": True, "data": {"id": "sent", "status": "queued"}})
        api = PrimitiveAPI("unused", httpx.Client(transport=httpx.MockTransport(endpoint), base_url="https://api.primitive.dev/v1"))
        try:
            api.send({"from": "gpt@test.primitive.email", "body_text": "reply"}, "stable-key", "inbound-id")
            self.assertEqual(len(seen), 2)
            self.assertEqual([r.headers["Idempotency-Key"] for r in seen], ["stable-key", "stable-key"])
            self.assertTrue(all(str(r.url).endswith("/emails/inbound-id/reply") for r in seen))
            self.assertNotIn("to", json.loads(seen[-1].content))
        finally:
            api.close()

    def test_primitive_mode_requires_configuration(self):
        with patch.dict("os.environ", {"MOOT_COMMUNICATION": "primitive"}, clear=True), self.assertRaises(PrimitiveError):
            PrimitiveTransport.from_env(self.store)


if __name__ == "__main__":
    unittest.main()
