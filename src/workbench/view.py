"""Shared literals for the audit-workbench backend.

STREAMING.md §C. The wire `state` snapshot is built inline by `Session.view()`
as a plain dict (pool + events + span_role + queued) — there is no backend
message derivation, so no per-role `ChatMessage[]` view type. This module keeps
only the `Role`/`Status` literals shared by `session.py` and `run.py`.
"""

from __future__ import annotations

from typing import Literal

Role = Literal["auditor", "target"]
Status = Literal["idle", "running", "paused", "ended"]
