"""Console timeline output. Server-side logs go to stderr so stdout stays a clean MCP channel."""
import contextvars
import os
import sys
import time

# Web UI hooks: sinks receive every timeline line and structured event, tagged with the current job.
CURRENT_JOB = contextvars.ContextVar("moot_job", default=None)
SINKS = []


def publish(_event, **data):
    ev = {"kind": _event, "job_id": data.pop("job_id", None) or CURRENT_JOB.get(), "at": time.time(), **data}
    for s in SINKS:
        s(ev)

_CODES = {
    "BUYER": "1;36", "HUMAN": "1;33", "MCP": "36", "MOOT": "1;35", "STATE": "95", "MEM": "34",
    "EVAL": "33", "VERIFY": "32", "BUS": "90", "RECOVER": "1;31", "FAULT": "31", "WARN": "31",
}


def _use_color(stream):
    if os.environ.get("NO_COLOR"):
        return False
    return bool(os.environ.get("FORCE_COLOR")) or stream.isatty()


def paint(text, code, stream=None):
    stream = stream or sys.stderr
    return f"\033[{code}m{text}\033[0m" if _use_color(stream) else text


def emit(tag, msg, indent=0, stream=None):
    stream = stream or sys.stderr
    label = paint(f"[{tag}]".ljust(9), _CODES.get(tag, "0"), stream)
    pad = "  " * indent
    if SINKS:
        publish("log", tag=tag, text=pad + str(msg))
    for i, line in enumerate(str(msg).splitlines() or [""]):
        stream.write(f"{label if i == 0 else ' ' * 9} {pad}{line}\n")
    stream.flush()


def banner(title, stream=None):
    stream = stream or sys.stdout
    bar = "=" * 78
    stream.write("\n" + paint(bar, "1", stream) + "\n" + paint(f"  {title}", "1", stream) + "\n"
                 + paint(bar, "1", stream) + "\n")
    stream.flush()


def table(headers, rows):
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(str(h)), *(len(r[i]) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    out = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    out += [fmt.format(*r) for r in rows]
    return "\n".join(out)


def usd(x):
    return f"${x:.4f}"


def money(v):
    if v is None:
        return "null"
    return f"{'-' if v < 0 else ''}${abs(v) / 1e6:,.1f}M"
