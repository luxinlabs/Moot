#!/usr/bin/env python3
"""End-to-end Moot demo: a Buyer Agent drives Moot over MCP from job creation to verified delivery."""
import argparse

from moot import RUNTIME
from moot.buyer_agent import run

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep-memory", action="store_true", help="keep capability memory from previous runs")
    ap.add_argument("--no-fault", action="store_true", help="disable the injected provider fault on Job 1")
    a = ap.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    if not a.keep_memory:  # reset only the CLI demo's own database; the web app's data (web.db, decks) is untouched
        (RUNTIME / "moot.db").unlink(missing_ok=True)
    run(fault=not a.no_fault)
