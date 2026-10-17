"""Model providers for LLM-backed agents: Claude (Anthropic SDK), OpenAI and GLM (OpenAI-compatible API).

Keys come from the environment, `moot/.env`, or the web UI (held in process memory only, never written to disk).
Prices are per 1M tokens and are estimates you can override; costs shown in Moot are estimates from them.
"""
import json
import os
import re
import time

from . import ROOT

PROVIDERS = {
    "anthropic": {"label": "Claude", "vendor": "Anthropic", "env": "ANTHROPIC_API_KEY",
                  "model": os.environ.get("MOOT_CLAUDE_MODEL", "claude-opus-5-5"), "price": [4.0, 20.0]},
    "openai": {"label": "GPT", "vendor": "OpenAI", "env": "OPENAI_API_KEY",
               "model": os.environ.get("MOOT_OPENAI_MODEL", "gpt-5"), "price": [1.25, 10.0]},
    "glm": {"label": "GLM", "vendor": "Zhipu / Z.ai", "env": "ZAI_API_KEY",
            "model": os.environ.get("MOOT_GLM_MODEL", "glm-5.2"), "price": [0.6, 2.2],
            "base_url": os.environ.get("MOOT_GLM_BASE_URL", "https://api.z.ai/api/paas/v4/")},
}
# Per-model price estimates (USD per 1M input / output tokens) for models used besides each provider's default.
MODEL_PRICES = {"claude-opus-5-5": [4.0, 20.0], "claude-sonnet-5-5": [2.0, 10.0], "claude-haiku-5-5": [0.1, 0.5]}
_KEYS = {}
REJECTED = {}  # provider -> message, set when the provider rejects the key; cleared when a key is entered


class LLMError(Exception):
    pass


def load_dotenv():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            value = m.group(2).strip("'\"")
            if value:  # moot/.env wins over the shell, so a stale exported key doesn't shadow it
                os.environ[m.group(1)] = value
    for pid, var in (("anthropic", "MOOT_CLAUDE_MODEL"), ("openai", "MOOT_OPENAI_MODEL"), ("glm", "MOOT_GLM_MODEL")):
        if os.environ.get(var):
            PROVIDERS[pid]["model"] = os.environ[var]
    if os.environ.get("MOOT_GLM_BASE_URL"):
        PROVIDERS["glm"]["base_url"] = os.environ["MOOT_GLM_BASE_URL"]


def key_for(provider):
    p = PROVIDERS[provider]
    return _KEYS.get(provider) or os.environ.get(p["env"]) or (os.environ.get("ZHIPUAI_API_KEY") if provider == "glm" else None)


def configure(provider, api_key=None, model=None, base_url=None, price=None):
    if api_key is not None:
        REJECTED.pop(provider, None)
        if api_key:
            _KEYS[provider] = api_key.strip()
        else:
            _KEYS.pop(provider, None)
    if model:
        PROVIDERS[provider]["model"] = model.strip()
    if base_url and provider == "glm":
        PROVIDERS[provider]["base_url"] = base_url.strip()
    if price:
        PROVIDERS[provider]["price"] = [float(price[0]), float(price[1])]


def public_settings():
    out = {}
    for pid, p in PROVIDERS.items():
        k = key_for(pid)
        out[pid] = {"label": p["label"], "vendor": p["vendor"], "model": p["model"], "price_per_mtok": p["price"],
                    "base_url": p.get("base_url"), "key_set": bool(k),
                    "key_hint": (k[:4] + "…" + k[-4:]) if k and len(k) > 12 else None,
                    "key_source": "ui" if pid in _KEYS else ("env" if k else None),
                    "key_rejected": REJECTED.get(pid)}
    return out


def first_available():
    return next((pid for pid in PROVIDERS if key_for(pid) and pid not in REJECTED), None)


def _parse_json(text):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            raise LLMError("model did not return JSON")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"model returned malformed JSON: {e}")


def complete_json(provider, system, user, schema, model=None):
    """One structured-output call. Returns (data, usage) where usage has tokens, cost and latency."""
    key = key_for(provider)
    if not key:
        raise LLMError(f"no API key configured for {PROVIDERS[provider]['vendor']}")
    p = dict(PROVIDERS[provider])
    if model and model != p["model"]:
        p["price"] = MODEL_PRICES.get(model, p["price"])
        p["model"] = model
    t0 = time.time()
    if provider == "anthropic":
        data, tin, tout = _anthropic(key, p["model"], system, user, schema)
    else:
        data, tin, tout = _openai_compatible(provider, key, p, system, user, schema)
    cost = tin / 1e6 * p["price"][0] + tout / 1e6 * p["price"][1]
    return data, {"provider": provider, "model": p["model"], "input_tokens": tin, "output_tokens": tout,
                  "est_cost_usd": cost, "latency_ms": int((time.time() - t0) * 1000)}


def _anthropic(key, model, system, user, schema):
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=120.0)
    try:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},  # re-run a declined request on Anthropic's recommended fallback
        )
    except anthropic.AuthenticationError:
        REJECTED["anthropic"] = "Anthropic rejected the API key"
        raise LLMError(REJECTED["anthropic"])
    except anthropic.NotFoundError:
        raise LLMError(f"Anthropic model '{model}' not found")
    except anthropic.RateLimitError:
        raise LLMError("Anthropic rate limit")
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise LLMError("could not reach the Anthropic API")
    if resp.stop_reason == "refusal":
        raise LLMError("Claude declined the request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("Claude output truncated (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return _parse_json(text), resp.usage.input_tokens, resp.usage.output_tokens


def _openai_compatible(provider, key, p, system, user, schema):
    import openai
    client = openai.OpenAI(api_key=key, base_url=p.get("base_url"), timeout=120.0)
    messages = [{"role": "system", "content": system + "\n\nRespond with one JSON object matching this JSON Schema:\n"
                 + json.dumps(schema)}, {"role": "user", "content": user}]
    kwargs = {"model": p["model"], "messages": messages, "response_format": {"type": "json_object"}}
    try:
        resp = client.chat.completions.create(**kwargs)
    except openai.AuthenticationError:
        REJECTED[provider] = f"{p['vendor']} rejected the API key"
        raise LLMError(REJECTED[provider])
    except openai.NotFoundError:
        raise LLMError(f"{p['vendor']} model '{p['model']}' not found")
    except openai.RateLimitError:
        raise LLMError(f"{p['vendor']} rate limit / quota")
    except openai.BadRequestError as e:
        raise LLMError(f"{p['vendor']} bad request: {e.message}")
    except openai.APIStatusError as e:
        raise LLMError(f"{p['vendor']} API error {e.status_code}")
    except openai.APIConnectionError:
        raise LLMError(f"could not reach the {p['vendor']} API")
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise LLMError(f"{p['vendor']} output truncated")
    usage = resp.usage
    return _parse_json(choice.message.content), getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0)
