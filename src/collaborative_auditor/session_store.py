"""Persistence layer for collaborative auditor sessions.

Sessions are stored as individual JSON files in a configurable directory.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from collaborative_auditor.models import Session

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path.home() / ".collaborative-auditor" / "sessions"


class SessionSummary(BaseModel):
    """Lightweight summary of a session for sidebar listing."""

    id: str
    initial_prompt: str
    auditor_model: str
    target_model: str
    created_at: str
    updated_at: str
    branch_count: int
    message_count: int


_VALID_SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class SessionStore:
    """File-based session persistence.

    Each session is stored as `{data_dir}/{session_id}.json`.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, session_id: str) -> Path:
        if not _VALID_SESSION_ID.match(session_id):
            raise ValueError(f"Invalid session ID format: {session_id!r}")
        return self.data_dir / f"{session_id}.json"

    def save(self, session: Session) -> None:
        """Persist a session to disk, updating its updated_at timestamp."""
        path = self._path_for(session.id)
        old_updated_at = session.updated_at
        session.updated_at = datetime.now(timezone.utc)
        data = session.model_dump(mode="json")
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            session.updated_at = old_updated_at
            raise

    def load(self, session_id: str) -> Session:
        """Load a session from disk.

        Raises FileNotFoundError if the session file doesn't exist.
        """
        path = self._path_for(session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session.model_validate(data)

    def exists(self, session_id: str) -> bool:
        return self._path_for(session_id).is_file()

    def delete(self, session_id: str) -> None:
        """Delete a session file. No-op if it doesn't exist."""
        path = self._path_for(session_id)
        path.unlink(missing_ok=True)

    def list_summaries(self) -> list[SessionSummary]:
        """List all saved sessions as lightweight summaries.

        Sorted by updated_at descending (most recent first).
        Corrupt files are included with placeholder data so the user can see and delete them.
        """
        summaries: list[SessionSummary] = []
        for path in self.data_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                branches = data.get("branches", [])
                first_branch = branches[0] if branches else None
                msg_count = len(first_branch.get("auditor_messages", [])) if first_branch else 0

                summaries.append(SessionSummary(
                    id=data["id"],
                    initial_prompt=data["initial_prompt"],
                    auditor_model=data["auditor_model"],
                    target_model=data["target_model"],
                    created_at=data.get("created_at", ""),
                    updated_at=data.get("updated_at", ""),
                    branch_count=len(branches),
                    message_count=msg_count,
                ))
            except Exception:
                logger.warning(f"Failed to read session file {path.name}", exc_info=True)
                session_id = path.stem
                summaries.append(SessionSummary(
                    id=session_id,
                    initial_prompt="[corrupt session file]",
                    auditor_model="unknown",
                    target_model="unknown",
                    created_at="",
                    updated_at="",
                    branch_count=0,
                    message_count=0,
                ))

        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries
