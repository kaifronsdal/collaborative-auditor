"""P1.8(a) — scanner library + named groups.

``STORE_DIR/scanners/`` (or ``Settings.scanner_dir``) holds user ``*.py``
files with ``@scanner``-decorated factories, loaded via scout's
``scanners_from_file`` — the same pattern sonde's ``project_scanners()``
uses for its ``graders/*.py`` dir. Alongside sits an optional
``groups.yaml`` (``{name: [scanner, …]}``) so ``wb.scan(logs,
"deception-suite")`` fans out to a curated set.

:func:`resolve` is the one entry point ``wb.scan`` calls: it takes anything
the agent might reasonably pass — a group name, a registry name, a raw
``Scanner`` instance, a list, a ``{name: Scanner}`` dict — and normalises to
``dict[str, Scanner]`` (name → instantiated scanner) so ``ScanJob`` /
``ScanHandle`` see one shape.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from inspect_ai._util.entrypoints import ensure_entry_points
from inspect_ai._util.registry import (
    is_registry_object,
    registry_find,
    registry_info,
    registry_unqualified_name,
)
from inspect_ai.model import ChatMessage
from inspect_scout import Result, Scanner, Transcript
from inspect_scout._scanner.scanner import (
    config_for_scanner,
    scanner_create,
    scanners_from_file,
)

from workbench import config

logger = logging.getLogger(__name__)

#: ``load_library()`` cache — keyed on the sorted (path, mtime) of every
#: ``*.py`` under ``scanner_dir()``, so editing/adding a scanner file
#: invalidates without a server restart.
_lib_cache: tuple[tuple[tuple[str, float], ...], dict[str, Scanner]] | None = None


def scanner_dir() -> Path:
    """The user-scanner root: ``Settings.scanner_dir`` if set, else
    ``STORE_DIR/scanners``. Read at call time so ``PATCH /settings`` /
    ``--store-dir`` take effect without re-import."""
    override = config.settings.scanner_dir
    if override:
        return Path(override).expanduser()
    return config.STORE_DIR / "scanners"


def _builtin_scanners() -> dict[str, Scanner]:
    """Every zero-arg-constructible ``@scanner`` already in the registry.

    Covers scout's built-ins (``grep_scanner``/``llm_scanner``) plus anything
    an installed extension (``inspect_petri``, …) registered via the
    ``inspect_ai`` entry-point group. Factories that *require* arguments
    (e.g. ``grep_scanner`` needs a pattern) are skipped — the agent constructs
    those explicitly; the library is for ready-to-run scanners.
    """
    ensure_entry_points()
    out: dict[str, Scanner] = {}
    for factory in registry_find(lambda i: i.type == "scanner"):
        # ``scanner_create`` needs the package-qualified registry name
        # (``inspect_petri/audit_judge``); we key the library on the
        # unqualified form so ``wb.scan(logs, "audit_judge")`` works.
        try:
            out[registry_unqualified_name(factory)] = scanner_create(
                registry_info(factory).name, {}
            )
        except (TypeError, ValueError):
            # Factory has required params — resolvable by name via
            # ``scanner_create`` in-cell, but not a library entry.
            continue
    return out


def load_library() -> dict[str, Scanner]:
    """Merge user ``*.py`` scanners with the built-in registry.

    Each ``*.py`` under :func:`scanner_dir` is loaded via scout's
    ``scanners_from_file(path, {})`` (which ``load_module``s, walks
    ``@scanner`` decorators, and instantiates each factory with no args).
    User names shadow built-ins. Cached on file mtimes.
    """
    global _lib_cache  # noqa: PLW0603
    root = scanner_dir()
    files = sorted(root.glob("*.py")) if root.is_dir() else []
    key = tuple((str(p), p.stat().st_mtime) for p in files)
    if _lib_cache is not None and _lib_cache[0] == key:
        return dict(_lib_cache[1])

    lib = _builtin_scanners()
    for path in files:
        try:
            for s in scanners_from_file(str(path), {}):
                lib[registry_unqualified_name(s)] = s
        except Exception:
            # A syntax error in one user scanner file shouldn't take the
            # whole library (and thus ``wb.scan`` by name) down.
            logger.exception("failed to load scanners from %s", path)
    _lib_cache = (key, dict(lib))
    return lib


def load_groups() -> dict[str, list[str]]:
    """``scanner_dir()/groups.yaml`` → ``{group: [scanner_name, …]}``;
    ``{}`` if the file is absent."""
    path = scanner_dir() / "groups.yaml"
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path}: expected a mapping of group → [scanner, …]")
    return {str(k): [str(n) for n in v] for k, v in raw.items()}


ScannerSpec = str | Scanner | Sequence[Any] | Mapping[str, Any]


def resolve(spec: ScannerSpec) -> dict[str, Scanner]:
    """Normalise anything ``wb.scan(scanner=…)`` accepts to ``{name: Scanner}``.

    Precedence for a string ``spec``:
      1. group name (``groups.yaml``) — recurses per member
      2. registry name via ``scanner_create`` (built-in / entry-point,
         qualified or unqualified, no-arg factories)
      3. user-library name (``scanner_dir()/*.py``)

    Lists/tuples and dicts recurse per element (dict keys override the
    resolved name). A raw ``Scanner`` instance is kept as-is under its
    registry name (or ``repr`` if unregistered).
    """
    lib = load_library()
    groups = load_groups()
    return _resolve(spec, lib, groups, seen=set())


def _resolve(
    spec: ScannerSpec,
    lib: dict[str, Scanner],
    groups: dict[str, list[str]],
    seen: set[str],
) -> dict[str, Scanner]:
    if isinstance(spec, str):
        if spec in groups:
            if spec in seen:
                raise ValueError(f"scanner group cycle at {spec!r}")
            seen = seen | {spec}
            out: dict[str, Scanner] = {}
            for member in groups[spec]:
                out.update(_resolve(member, lib, groups, seen))
            return out
        try:
            s = scanner_create(spec, {})
        except (TypeError, ValueError):
            s = lib.get(spec)
        if s is None:
            raise ValueError(_miss(spec, lib, groups)) from None
        return {registry_unqualified_name(s): s}

    if isinstance(spec, Mapping):
        out = {}
        for name, v in spec.items():
            r = _resolve(v, lib, groups, seen)
            # Dict key names the entry (matches pre-P1.8 ``wb.scan`` semantics
            # for ``{name: Scanner}``); if the value expands to >1 scanner
            # (a group), keep the members' own names.
            if len(r) == 1:
                out[name] = next(iter(r.values()))
            else:
                out.update(r)
        return out

    if isinstance(spec, Sequence):
        out = {}
        for item in spec:
            out.update(_resolve(item, lib, groups, seen))
        return out

    # Raw ``Scanner`` instance (or anything callable the agent built inline).
    name = registry_unqualified_name(spec) if is_registry_object(spec) else repr(spec)
    return {name: spec}


async def scan_messages(
    messages: Sequence[ChatMessage],
    scanners: dict[str, Scanner],
    *,
    transcript_id: str,
    model: str | None = None,
) -> dict[str, Result | Exception]:
    """Run each scanner directly on an in-memory scout ``Transcript``.

    P1.8(b): the M0 branch's target conversation is live in
    ``branch.channel.state.messages`` — there is no ``.eval`` on disk to
    point ``ScanJob`` at, so this bypasses ``scan_async`` and calls each
    scanner on a synthetic ``Transcript(messages=…)`` (same construction
    scout's own ``as_scorer`` uses). Per-scanner message filtering honours
    the scanner's declared ``config.content.messages``; events/timelines are
    empty (a live M0 branch has no per-sample event log to hand over).

    A scanner that raises (wrong input type, model error, …) is captured
    per-entry so one failure doesn't sink the batch; the caller renders it
    as ``errors: 1`` in the ``ScanPayload``.
    """
    out: dict[str, Result | Exception] = {}
    for name, s in scanners.items():
        cfg = config_for_scanner(s)
        roles = cfg.content.messages
        if roles == "all" or roles is None:
            filtered = list(messages)
        else:
            filtered = [m for m in messages if m.role in roles]
        t = Transcript(
            transcript_id=transcript_id,
            model=model,
            messages=filtered,
            message_count=len(filtered),
        )
        try:
            r = await s(t)
        except Exception as exc:  # noqa: BLE001
            out[name] = exc
            continue
        # ``list[Result]`` collapses to the first entry for the card; the
        # full list is what a per-turn (P1.8c) badge would iterate.
        if isinstance(r, list):
            out[name] = r[0] if r else RuntimeError(f"{name}: empty result")
        else:
            out[name] = r
    return out


def _miss(spec: str, lib: dict[str, Scanner], groups: dict[str, list[str]]) -> str:
    names = sorted(set(lib) | set(groups))
    hint = difflib.get_close_matches(spec, names, n=1)
    hint_s = f" (did you mean {hint[0]!r}?)" if hint else ""
    g = ", ".join(sorted(groups)) or "none"
    s = ", ".join(sorted(lib)) or "none"
    return (
        f"unknown scanner or group {spec!r}{hint_s}. "
        f"Available groups: {g}. Available scanners: {s}."
    )
