"""Worker registry: deterministic baseline agents plus LLM-backed agents (Claude / GPT / GLM) per task family."""
from .decks import LLM_DECK_AGENTS, TEMPLATE_DECK
from .llm_workers import LLM_WORKERS
from .workers import ALL_WORKERS as BASELINE


def build_workers(include_llm):
    return tuple(BASELINE) + (TEMPLATE_DECK,) + ((tuple(LLM_WORKERS) + tuple(LLM_DECK_AGENTS)) if include_llm else ())
