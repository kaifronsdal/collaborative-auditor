"""Single source of truth for the workbench store root (P0.3).

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
"""

from __future__ import annotations

import os
from pathlib import Path

#: Persistence root. Mutated by ``server.main()`` from ``--store-dir``.
STORE_DIR: Path = Path(os.environ.get("WORKBENCH_STORE", "~/.workbench")).expanduser()


def sessions_dir() -> Path:
    """``{STORE_DIR}/sessions`` — one subdir per session id."""
    return STORE_DIR / "sessions"
