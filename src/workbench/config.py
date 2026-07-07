"""Single source of truth for the workbench store root (P0.3) and global
user settings (P1.7).

``STORE_DIR`` is the persistence root (default ``~/.workbench``, overridable
via the ``WORKBENCH_STORE`` env var). Sessions — both the M0 branch/index
JSON written by `workbench.persist` and the M1 orchestrator's ``session_dir``
(bash cwd, eval log-dirs, ``write_file`` artifacts) — live under
``sessions_dir() == STORE_DIR / "sessions"``.

Before this module, ``server.py`` honoured ``--store-dir`` but
``Orchestrator.__init__`` hardcoded ``~/.workbench/sessions/{span_id}``, so a
user pointing the flag at durable storage still lost half the session's
files. Both now read from here; ``server.main()`` mutates ``STORE_DIR`` in
place from ``--store-dir`` before any session is created.

``settings`` is the module-level `Settings` singleton, loaded from
``STORE_DIR / "settings.json"`` at import and mutated in place by
``PATCH /settings``. Consumers read ``config.settings.<field>`` (never bind
the dataclass locally — the server rebinds the module attr on PATCH).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

logger = logging.getLogger(__name__)

#: Persistence root. Mutated by ``server.main()`` from ``--store-dir``.
STORE_DIR: Path = Path(os.environ.get("WORKBENCH_STORE", "~/.workbench")).expanduser()


def sessions_dir() -> Path:
    """``{STORE_DIR}/sessions`` — one subdir per session id."""
    return STORE_DIR / "sessions"


# -- P1.7: global settings ----------------------------------------------------


@dataclass
class Settings:
    """User-editable global knobs (PRODUCT-GAPS.md §Settings inventory).

    Persisted as ``{STORE_DIR}/settings.json``; edited via the sidebar
    settings modal → ``PATCH /settings``. Fields with a "consumer:" note are
    *defined* here so the panel can round-trip them, but not yet read by
    their consumer (wiring those touches P0.4/P0.5-scoped files and lands
    separately).
    """

    # -- default models per role (P1.1/P1.2 seed values) --
    default_orchestrator: str = "anthropic/claude-opus-4-8"
    default_auditor: str = "anthropic/claude-sonnet-4-6"
    default_target: str = "anthropic/claude-haiku-4-5"
    default_judge: str = "anthropic/claude-sonnet-4-6"

    # -- seed-review gate (consumer: m1/prompt.py build_system_prompt) --
    seed_review_cost_threshold: float = 5.0
    seed_review_count_threshold: int = 20
    auto_approve_under_threshold: bool = False

    # -- tool caps (consumers not yet wired — see P0.5 scope) --
    #: consumer: m1/bash_tool.py `bash(timeout=…)` default
    bash_timeout: int = 300
    #: consumer: m1/orchestrator.py MODEL_TEXT_CAP (tool-result truncation)
    model_text_cap: int = 4000
    #: consumer: m1/orchestrator.py `read_file(limit=…)` default
    read_file_limit: int = 2000
    #: consumer: m1/attach.py AttachedRun._grace (seconds to wait for a
    #: settled `.eval` after the subprocess exits)
    attach_grace: float = 15.0

    #: Escape hatch for keys added by a newer server that this build's
    #: dataclass doesn't know about — round-tripped verbatim so a PATCH
    #: from an older UI doesn't silently drop them.
    extra: dict = field(default_factory=dict)


#: ``{STORE_DIR}/settings.json``. Bound at import from ``WORKBENCH_STORE``;
#: ``server.main()``'s ``--store-dir`` mutation happens after ``load_settings``
#: has already run, so settings follow the env var, not the flag (acceptable
#: for a global — the flag is per-invocation, settings are per-install).
SETTINGS_PATH: Path = STORE_DIR / "settings.json"

_FIELD_NAMES = {f.name for f in fields(Settings)}


def load_settings() -> Settings:
    """Read ``settings.json`` and merge over dataclass defaults.

    Unknown keys land in ``Settings.extra``; a missing/corrupt file yields
    pure defaults (fail-soft here is deliberate — a bad settings file
    shouldn't brick server startup).
    """
    if not SETTINGS_PATH.exists():
        return Settings()
    try:
        raw = json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        logger.exception("settings.json unreadable — using defaults")
        return Settings()
    known = {k: v for k, v in raw.items() if k in _FIELD_NAMES and k != "extra"}
    extra = {k: v for k, v in raw.items() if k not in _FIELD_NAMES}
    return Settings(**known, extra=extra)


def save_settings(s: Settings) -> None:
    """Persist ``s`` to ``settings.json`` (flat — ``extra`` is inlined)."""
    d = asdict(s)
    d.update(d.pop("extra"))
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(d, indent=2))


def patch_settings(body: dict) -> Settings:
    """Merge ``body`` into the module-level ``settings`` and persist.

    Known keys go through ``replace`` (so a wrong-typed value raises at the
    dataclass boundary); unknown keys accumulate in ``extra``.
    """
    global settings  # noqa: PLW0603 — module singleton, same pattern as STORE_DIR
    known = {k: v for k, v in body.items() if k in _FIELD_NAMES and k != "extra"}
    extra = {**settings.extra, **{k: v for k, v in body.items() if k not in _FIELD_NAMES}}
    settings = replace(settings, **known, extra=extra)
    save_settings(settings)
    return settings


#: Module-level singleton. Read as ``config.settings.<field>``; rebound by
#: ``patch_settings`` on ``PATCH /settings``.
settings: Settings = load_settings()
