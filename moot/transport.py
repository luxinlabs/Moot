"""Agent-to-agent coordination port.

`PrimitiveMessagingPort` is the abstraction from the original orchestration demo.
`LocalTransport` below remains in-process. Deck Studio's real Primitive request/reply
transport lives in primitive_transport.py and is enabled with MOOT_COMMUNICATION=primitive.
"""
from abc import ABC, abstractmethod

from .log import emit
from .store import new_id


class PrimitiveMessagingPort(ABC):
    label = "abstract"

    @abstractmethod
    def offer_task(self, recipient, correlation_id, payload, idempotency_key, sandbox, quiet=False):
        """Send task_offer, collect accept/reject, run, return (status, result, events)."""

    @abstractmethod
    def send(self, event_type, sender, recipient, correlation_id, payload, idempotency_key=None, quiet=False):
        """Send one logical event (handoff_requested, ...)."""


class LocalTransport(PrimitiveMessagingPort):
    label = "LocalTransport (in-process; Primitive adapter NOT connected)"

    def __init__(self, store, workers):
        self.store = store
        self.endpoints = {w.worker_id: w for w in workers}
        self._completed = {}  # idempotency_key -> result (at-least-once delivery, dedup on our side)

    def send(self, event_type, sender, recipient, correlation_id, payload, idempotency_key=None, quiet=False):
        msg = {"event_type": event_type, "message_id": new_id("msg"), "correlation_id": correlation_id,
               "sender": sender, "recipient": recipient, "payload": payload}
        self.store.insert("coordination_events", message_id=msg["message_id"], correlation_id=correlation_id,
                          event_type=event_type, sender=sender, recipient=recipient,
                          idempotency_key=idempotency_key, payload=payload)
        if not quiet:
            brief = {k: v for k, v in payload.items() if k in ("artifact_refs", "reason", "error", "checkpoint_ref",
                                                                "remaining_refs", "status", "cost_usd")}
            emit("BUS", f"{event_type:<19} {sender} -> {recipient}  {msg['message_id']}  {brief if brief else ''}")
        return msg

    def offer_task(self, recipient, correlation_id, payload, idempotency_key, sandbox, quiet=False):
        if idempotency_key in self._completed:
            emit("BUS", f"duplicate offer suppressed (idempotency_key={idempotency_key})")
            return self._completed[idempotency_key]
        worker = self.endpoints[recipient]
        payload = {**payload, "idempotency_key": idempotency_key}
        self.send("task_offer", "moot-orchestrator", recipient, correlation_id, payload, idempotency_key, quiet)
        ok, reason = worker.accepts(payload["task"])
        if not ok:
            self.send("offer_rejected", recipient, "moot-orchestrator", correlation_id, {"reason": reason}, quiet=quiet)
            out = {"status": "rejected", "error": reason, "record": None, "cost_usd": 0.0, "latency_ms": 0,
                   "tool_calls": []}
            return out
        self.send("offer_accepted", recipient, "moot-orchestrator", correlation_id, {}, quiet=quiet)
        self.send("execution_started", recipient, "moot-orchestrator", correlation_id, {}, quiet=quiet)
        out = worker.execute(payload["task"], sandbox)
        ev = "execution_completed" if out["status"] == "completed" else "execution_failed"
        self.send(ev, recipient, "moot-orchestrator", correlation_id,
                  {"status": out["status"], "error": out.get("error"), "cost_usd": round(out["cost_usd"], 4)},
                  quiet=quiet)
        self._completed[idempotency_key] = out
        return out
