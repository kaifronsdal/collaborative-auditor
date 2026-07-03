"""Compact one-line summaries for the variable-inspector tooltip (M1-FEATURES §7).

``short_repr(v)`` returns the full ``"TypeName · detail"`` string that lands
in ``cell_done.ns[name]`` and surfaces as a native ``title=`` tooltip on
``.cc-var`` tokens in the collapsed code gist. The point is *recognition* —
enough shape/size/identity to disambiguate which value a name refers to
without expanding the cell — not a full repr.

Dispatch is by ``type.__name__`` / ``__module__`` string match, never by
``import``: the orchestrator's ``user_ns`` may hold pandas/numpy/plotly/
inspect_ai/petri objects, but this module must not force any of those to
import at kernel boot. Every branch is guarded — a broken ``__repr__`` /
``__len__`` / missing attribute falls back to the bare type name; a summary
must never raise into ``_settle``.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import numbers
import os
from collections import abc
from functools import partial
from pathlib import PurePath
from typing import Any

_CAP = 80

#: Workbench live handles — ``{done}/{total} state · task``.
_HANDLE_TYPES = {"AttachedRun", "ScanHandle"}

#: inspect_ai ``ChatMessage*`` — role + truncated text.
_CHAT_TYPES = {
    "ChatMessageUser",
    "ChatMessageAssistant",
    "ChatMessageSystem",
    "ChatMessageTool",
}


def short_repr(v: Any) -> str:  # noqa: PLR0911, PLR0912, C901
    """``"TypeName · detail"`` capped at 80 chars; never raises."""
    tn = type(v).__name__
    try:
        mod = type(v).__module__
        # -- primitives ------------------------------------------------------
        if v is None or isinstance(v, bool):
            return repr(v)
        if isinstance(v, str):
            return _cap(f"str · len {len(v)} · {v!r}")
        if isinstance(v, bytes):
            return f"bytes · len {len(v)}"
        if isinstance(v, numbers.Integral):  # int, np.int*, bool caught above
            return f"{tn} · {int(v)}"
        if isinstance(v, numbers.Real):  # float, np.float*
            return f"{tn} · {float(v):g}"
        if isinstance(v, numbers.Complex):
            return f"{tn} · {v!r}"
        # -- workbench handles ----------------------------------------------
        if tn in _HANDLE_TYPES:
            state = "done" if v.finished else "running"
            if getattr(v, "error", None):
                state = "error"
            tag = getattr(v, "task_name", None) or getattr(v, "description", "") or ""
            total = f"/{v.total}" if v.total else ""
            return _cap(f"{tn} · {v.n_done}{total} {state} · {tag}".rstrip(" ·"))
        # -- pandas ----------------------------------------------------------
        if tn == "DataFrame" and hasattr(v, "shape"):
            r, c = v.shape
            cols = _preview_seq(list(v.columns), 3)
            return _cap(f"DataFrame · {r}×{c} · [{cols}]")
        if tn == "Series" and hasattr(v, "dtype"):
            extra = f" · mean {v.mean():g}" if v.dtype.kind in "fiu" and len(v) else ""
            return _cap(f"Series[{v.dtype}] · len {len(v)}{extra}")
        if tn in {"Index", "RangeIndex", "MultiIndex", "DatetimeIndex"}:
            return _cap(f"{tn}[{getattr(v, 'dtype', '')}] · len {len(v)}")
        if "GroupBy" in tn and hasattr(v, "keys"):
            return _cap(f"{tn} · {len(v)} groups · by {v.keys}")
        # -- numpy -----------------------------------------------------------
        if tn == "ndarray" and hasattr(v, "shape"):
            return f"ndarray · {v.dtype} · {v.shape}"
        # -- plotly ----------------------------------------------------------
        if tn == "Figure" and mod.startswith("plotly"):
            traces = [getattr(t, "type", "?") for t in v.data]
            kinds = "+".join(dict.fromkeys(traces)) or "empty"
            n = len(traces)
            return _cap(f"Figure · {kinds} · {n} trace{'s' if n != 1 else ''}")
        # -- inspect_ai ------------------------------------------------------
        if tn == "EvalLog":
            n = len(v.samples) if v.samples is not None else "?"
            return _cap(f"EvalLog · {n} samples · {v.eval.task}")
        if tn == "EvalSample":
            return _cap(f"EvalSample · id {v.id} · {len(v.messages)} msgs")
        if tn == "EvalSampleSummary":
            return _cap(f"EvalSampleSummary · id {v.id}")
        if tn == "Task" and mod.startswith("inspect_ai"):
            name = getattr(v, "name", None) or "unnamed"
            ds = getattr(v, "dataset", None)
            n = f" · {len(ds)} samples" if ds is not None else ""
            return _cap(f"Task · {name}{n}")
        if tn == "Model" and mod.startswith("inspect_ai"):
            return _cap(f"Model · {v.name}")
        if tn in _CHAT_TYPES:
            return _cap(f"{tn} · {_flatten_content(v.content)!r}")
        # -- petri -----------------------------------------------------------
        if tn == "History" and hasattr(v, "root"):
            n, d = _tree_shape(v.root)
            return f"History · {n} node{'s' if n != 1 else ''} · depth {d}"
        if tn == "Trajectory" and hasattr(v, "tape"):
            return f"Trajectory · {len(v.tape.log)} steps"
        if tn == "AuditTape":
            return f"AuditTape · {len(v.trajectories)} trajectories"
        # -- awaitables ------------------------------------------------------
        if isinstance(v, asyncio.Task):
            state = (
                "cancelled" if v.cancelled()
                else "done" if v.done()
                else "pending"
            )
            name = v.get_name()
            return _cap(f"Task · {state} · {name}")
        if isinstance(v, asyncio.Future):
            return f"Future · {'done' if v.done() else 'pending'}"
        if inspect.iscoroutine(v):
            return _cap(f"coroutine · {v.__qualname__}")
        # -- callables -------------------------------------------------------
        if isinstance(v, partial):
            fn = getattr(v.func, "__name__", repr(v.func))
            bound = len(v.args) + len(v.keywords)
            return _cap(f"partial · {fn}({bound} bound)")
        if inspect.isroutine(v) or inspect.isclass(v):
            kind = "class" if inspect.isclass(v) else "function"
            name = getattr(v, "__qualname__", getattr(v, "__name__", tn))
            return _cap(f"{kind} · {name}{_sig(v)}")
        # -- paths / IO ------------------------------------------------------
        if isinstance(v, PurePath):
            return _cap(f"{tn} · {_short_path(v)}")
        if isinstance(v, io.IOBase):
            name = getattr(v, "name", "?")
            state = "closed" if getattr(v, "closed", False) else "open"
            return _cap(f"{tn} · {state} · {name}")
        # -- containers (after everything typed above) ----------------------
        if isinstance(v, abc.Mapping):
            keys = _preview_seq(list(v)[:4], 3)
            return _cap(
                f"{tn}[{_peek(v)}, {_peek_val(v)}] · {len(v)} keys: [{keys}]"
                if v
                else f"{tn} · empty"
            )
        if isinstance(v, (list, tuple, set, frozenset)):
            return (
                f"{tn}[{_peek(v)}] · len {len(v)}" if v else f"{tn} · empty"
            )
        # -- pydantic (would otherwise dump every field) --------------------
        if hasattr(v, "model_fields") and hasattr(v, "__dict__"):
            fields = _preview_seq(list(type(v).model_fields), 3)
            return _cap(f"{tn} · {{{fields}}}")
        # -- last resort: repr, capped --------------------------------------
        return _cap(f"{tn} · {repr(v)}")
    except Exception:  # noqa: BLE001
        return tn


# -- helpers ------------------------------------------------------------------


def _cap(s: str, n: int = _CAP) -> str:
    s = " ".join(s.split())  # collapse newlines/whitespace — tooltip is one line
    return s if len(s) <= n else s[: n - 1] + "…"


def _sig(fn: Any) -> str:
    """``(a, b=1)`` — param names + defaults, no annotations (they bloat under
    ``from __future__ import annotations`` to quoted strings)."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (ValueError, TypeError):
        return "(…)"
    parts = [
        p.name + ("" if p.default is inspect.Parameter.empty else f"={p.default!r}")
        for p in params
    ]
    return f"({', '.join(parts)})"


def _preview_seq(xs: list[Any], k: int) -> str:
    """``a, b, c, …`` (first ``k`` items, ellipsis if more)."""
    shown = ", ".join(str(x) for x in xs[:k])
    return shown + (", …" if len(xs) > k else "")


def _peek(xs: Any) -> str:
    """Element-type tag for a container, depth 1: ``str`` / ``mixed`` / ``?``."""
    try:
        it = iter(xs)
        first = next(it)
    except (StopIteration, TypeError):
        return "?"
    tn = type(first).__name__
    # Sample up to 4 more; if any differs → mixed. Cheap and non-exhaustive by
    # design — a 10k-row list shouldn't be scanned for a tooltip.
    for _, x in zip(range(4), it, strict=False):
        if type(x).__name__ != tn:
            return "mixed"
    return tn


def _peek_val(m: abc.Mapping[Any, Any]) -> str:
    try:
        return _peek(list(m.values())[:5])
    except Exception:  # noqa: BLE001
        return "?"


def _short_path(p: PurePath) -> str:
    """Compress a long path to ``head/…/tail`` under the cap."""
    s = str(p)
    if len(s) <= 60:
        return s
    parts = p.parts
    if len(parts) > 2:
        return str(PurePath(parts[0], "…", *parts[-2:]))
    return s[:59] + "…"


def _tree_shape(root: Any) -> tuple[int, int]:
    """(node count, max depth) via BFS on ``.children``. Capped at 1000."""
    n, depth, frontier = 0, 0, [root]
    while frontier and n < 1000:
        depth += 1
        n += len(frontier)
        frontier = [c for node in frontier for c in getattr(node, "children", ())]
    return n, depth


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            getattr(c, "text", "") for c in content if getattr(c, "type", "") == "text"
        )
    return str(content)


def ns_size_estimate(ns: dict[str, str]) -> int:
    """Rough JSON byte count for the ``cell_done.ns`` payload — used by the
    smoke test to bound wire size (20 turns × 30 vars should stay well under
    the ~1 MB WS frame default)."""
    return sum(len(k) + len(v) + 8 for k, v in ns.items()) + 2


# Re-export the handle-type set so ``kernel.py`` doesn't duplicate it.
__all__ = ["short_repr", "ns_size_estimate", "_HANDLE_TYPES"]


if __name__ == "__main__":
    # Tiny inline sanity — the real coverage is in ``_smoke_m1_kernel.py``.
    for v in [42, "hello", [1, 2, 3], {"a": 1}, None, os.getcwd, PurePath("/tmp/x")]:
        print(f"{v!r:>30}  →  {short_repr(v)}")
