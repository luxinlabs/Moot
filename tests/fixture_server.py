"""Subprocess entrypoint for tests only: real mcp-use server, fake model backend."""
import os
import tempfile

os.environ["MCP_USE_ANONYMIZED_TELEMETRY"] = "false"
from fixture_studio import FixtureStudio
from moot.manufact_server import build_server
from moot.studio_service import StudioService

with tempfile.TemporaryDirectory() as folder:
    service = StudioService(FixtureStudio(folder), folder)
    try:
        build_server(service).run(transport="stdio")
    finally:
        service.close()
