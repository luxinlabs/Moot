"""Manufact mcp-use server for the real Deck Studio workflow."""
import argparse
import asyncio
import base64
import hmac
import os
import threading
from urllib.parse import urlparse

# mcp-use telemetry is opt-in for this workspace.
os.environ.setdefault("MCP_USE_ANONYMIZED_TELEMETRY", "false")

from mcp_use import MCPServer
from mcp_use.server.auth import AccessToken, BearerAuthProvider
from mcp.types import ToolAnnotations

from . import RUNTIME, llm
from .studio_service import PPTX_MIME


class WorkspaceAuth(BearerAuthProvider):
    def __init__(self, secret):
        self.secret = secret

    async def verify_token(self, token):
        if hmac.compare_digest(token.encode(), self.secret.encode()):
            return AccessToken(token=token, claims={"sub": "moot-workspace"})
        return None


def build_server(service, host="127.0.0.1", port=8000, token=None):
    server = MCPServer(
        name="Moot Deck Studio", version="0.2.0", host=host, port=port,
        auth=WorkspaceAuth(token) if token else None,
        instructions="Create a deck job, inspect its questions, submit answers to start the paid work trial, "
                     "poll status, review scores, then retrieve the editable PPTX. Do not invent answers. "
                     "Use defaults only when authorized by your user. Costs are estimates. "
                     "This server serves one trusted workspace.",
    )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def moot_studio_catalog() -> dict:
        """List available candidate agents, shared skills, commit provenance and skill version. No model calls."""
        return await asyncio.to_thread(service.catalog)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    async def moot_studio_create(request: str, url: str, candidates: list[str] | None = None) -> dict:
        """Read a product URL and create a deck job with questions. Calls a paid model for intake.

        Returns job_id and questions; no trial starts until moot_studio_answer is called.
        Use https://moot.example/ for the bundled Moot product fixture.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Provide a product HTTP(S) URL without credentials")
        return await asyncio.to_thread(service.create, request, url, None, candidates)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    async def moot_studio_answer(job_id: str, answers: dict) -> dict:
        """Submit question-ID answers, compile the benchmark and start the paid trial/final build.

        Omitted answers use displayed defaults. The budget answer authorizes spending, but is an
        estimate target, not a provider-enforced cap. Returns promptly after benchmark compilation.
        Poll moot_studio_get; repeating the same answers does not start another trial.
        """
        return await asyncio.to_thread(service.answer_and_start, job_id, answers)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def moot_studio_get(job_id: str) -> dict:
        """Get persisted questions, benchmark, candidates, decks, history and costs. Poll every 2–5 seconds
        until COMPLETED or FAILED. Reading never starts work or incurs model charges.
        """
        return await asyncio.to_thread(service.get, job_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def moot_studio_review(job_id: str) -> dict:
        """Read candidate ranking, hard gates, judge feedback, winner and cost evidence without re-judging."""
        return await asyncio.to_thread(service.review, job_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def moot_studio_communication(job_id: str | None = None) -> dict:
        """Inspect main/candidate Primitive identities and persisted message/attachment delivery evidence.
        Returns local mode explicitly if Primitive is not enabled. Does not send any mail.
        """
        return await asyncio.to_thread(service.communication, job_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def moot_studio_artifact(job_id: str, include_base64: bool = False) -> dict:
        """Retrieve a completed PPTX manifest with SHA-256 and MCP resource URI.

        Clients supporting resources should read that URI for binary PPTX bytes. Tool-only clients
        may set include_base64=true and decode content_base64. Never return a server-local file path.
        """
        manifest, data = await asyncio.to_thread(service.artifact, job_id)
        if include_base64:
            manifest["content_base64"] = base64.b64encode(data).decode("ascii")
        return manifest

    @server.resource("moot://decks/{job_id}/deck.pptx", mime_type=PPTX_MIME)
    async def deck_resource(job_id: str) -> bytes:
        """Editable PowerPoint binary for a completed Studio job."""
        _, data = await asyncio.to_thread(service.artifact, job_id)
        return data

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--db", default=str(RUNTIME / "web.db"))
    parser.add_argument("--with-web", action="store_true", help="Serve the shared Studio UI on loopback")
    parser.add_argument("--web-port", type=int, default=8787)
    parser.add_argument("--waitlist", action="store_true", help="With --with-web: serve the waitlist form to this network")
    parser.add_argument("--waitlist-port", type=int, default=8788)
    parser.add_argument("--public-url", help="Public URL for the waitlist QR code")
    parser.add_argument("--inspect", action="store_true", help="Enable mcp-use Inspector for local development")
    args = parser.parse_args()
    llm.load_dotenv()
    token = os.getenv("MOOT_MCP_TOKEN")
    if args.transport != "stdio" and args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        parser.error("Set MOOT_MCP_TOKEN before exposing this paid, single-workspace server on a network")
    if args.inspect and args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Inspector is available only on a loopback interface")
    from . import web
    service = web.initialize(args.db)
    http_server = None
    if args.with_web:
        from werkzeug.serving import make_server
        http_server = make_server("127.0.0.1", args.web_port, web.app, threaded=True)
        threading.Thread(target=http_server.serve_forever, daemon=True).start()
        if args.waitlist:
            web.waitlist.start_server(args.waitlist_port, args.public_url)
    try:
        build_server(service, args.host, args.port, token).run(transport=args.transport, debug=args.inspect)
    finally:
        if http_server:
            http_server.shutdown()
        service.close()


if __name__ == "__main__":
    main()
