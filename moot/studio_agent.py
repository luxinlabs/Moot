"""External MCP client demo: create -> answer -> poll -> review -> retrieve PPTX.

Default mode is a scripted buyer policy. --agent uses an LLM-driven mcp-use MCPAgent.
Both modes talk exclusively through MCP, never import Studio or its database.
"""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

os.environ.setdefault("MCP_USE_ANONYMIZED_TELEMETRY", "false")
from mcp_use import MCPClient


def unpack(result):
    if result.isError:
        raise RuntimeError("; ".join(c.text for c in result.content if hasattr(c, "text")))
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(next(c.text for c in result.content if hasattr(c, "text")))


async def scripted_demo(client, request, url, output, answers=None, candidates=None, timeout=900):
    await client.create_all_sessions()
    session = client.get_session("moot")
    async def call(name, **arguments):
        result = unpack(await session.call_tool(name=name, arguments=arguments))
        print(json.dumps({"tool": name, "status": result.get("status"), "job_id": result.get("job_id")}), flush=True)
        return result
    catalog = await call("moot_studio_catalog")
    if not any(a["available"] for a in catalog["agents"]):
        raise RuntimeError("No candidate model is available; configure provider keys on the server")
    job = await call("moot_studio_create", request=request, url=url, candidates=candidates)
    chosen = {q["id"]: q["default"] for q in job["questions"]}
    chosen.update(answers or {})
    print(json.dumps({"authorized_answers": chosen}), flush=True)
    await call("moot_studio_answer", job_id=job["job_id"], answers=chosen)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await call("moot_studio_get", job_id=job["job_id"])
        if status["status"] == "FAILED":
            raise RuntimeError(f"Deck failed: {status.get('history', [])[-1:]}")
        if status["status"] == "COMPLETED":
            break
        await asyncio.sleep(2)
    else:
        raise TimeoutError(f"Still running: {job['job_id']}. Resume polling this job; do not recreate it.")
    review = await call("moot_studio_review", job_id=job["job_id"])
    artifact = await call("moot_studio_artifact", job_id=job["job_id"], include_base64=True)
    data = base64.b64decode(artifact.pop("content_base64"), validate=True)
    if hashlib.sha256(data).hexdigest() != artifact["sha256"] or len(data) != artifact["size_bytes"]:
        raise RuntimeError("Artifact checksum/length mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as deck:
        if "ppt/presentation.xml" not in deck.namelist():
            raise RuntimeError("Artifact is not a PowerPoint file")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    evidence = {"job_id": job["job_id"], "catalog": catalog, "review": review, "artifact": artifact}
    output.with_suffix(".evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"Verified deck saved to {output}", flush=True)
    return evidence


async def main_async(args):
    if args.endpoint:
        config = {"url": args.endpoint}
        if os.getenv("MOOT_MCP_TOKEN"):
            config["headers"] = {"Authorization": "Bearer " + os.environ["MOOT_MCP_TOKEN"]}
    else:
        config = {"command": sys.executable, "args": ["-m", "moot.manufact_server", "--transport", "stdio"]}
    client = MCPClient.from_dict({"mcpServers": {"moot": config}})
    try:
        if args.agent:
            from langchain_openai import ChatOpenAI
            from mcp_use import MCPAgent
            agent = MCPAgent(llm=ChatOpenAI(model=args.model), client=client, max_steps=60)
            result = await agent.run(
                f"Use Moot to create this deck: {args.request}. Product URL: {args.url}. "
                f"The user authorizes the displayed default answers, except budget={args.budget}. "
                "Inspect questions, answer, then poll without creating duplicate jobs. Review the final scores. "
                "Retrieve the final artifact manifest and report its resource URI and SHA-256. "
                "If work is still running at your step limit, report the job ID for resumption.")
            print(result)
        else:
            await scripted_demo(client, args.request, args.url, args.output, {"budget": args.budget})
    finally:
        await client.close_all_sessions()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", help="Existing HTTP MCP endpoint; otherwise launch a stdio subprocess")
    parser.add_argument("--request", default="Build a pitch deck for Moot for hackathon judges")
    parser.add_argument("--url", default="https://moot.example/")
    parser.add_argument("--output", default="runtime/mcp-demo.pptx")
    parser.add_argument("--budget", type=float, default=3.0)
    parser.add_argument("--use-defaults", action="store_true", required=True,
                        help="Explicitly authorize displayed default answers and the requested spend target")
    parser.add_argument("--agent", action="store_true", help="Use a real LLM-driven MCPAgent instead of scripted policy")
    parser.add_argument("--model", default=os.getenv("MOOT_OPENAI_MODEL", "gpt-5"))
    args = parser.parse_args()
    from .llm import load_dotenv
    load_dotenv()
    awaitable = main_async(args)
    asyncio.run(awaitable)


if __name__ == "__main__":
    main()
