"""Prompt versioning registry (task Section 22).

File-based (no DB exists anywhere in the Phase 1 codebase to reuse or
conform to - confirmed by direct inspection). Each agent's prompt versions
live in one JSON file under config.PROMPT_STORE_DIR; the store is seeded on
first use from the SME-verbatim instruction .md files in
prompts/instructions/ (see PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md
Section 12/18/19: engineering controls storage/versioning mechanics, SMEs
only ever see/edit `instruction_text`).

Supports: draft, save (new immutable version), activate (exactly one active
version per agent), restore (reactivate an older version), compare (diff
two versions' instruction_text), and the version list itself doubles as the
audit history.
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional

from p2_config import PROMPT_STORE_DIR

AGENT_IDS = ["context_locking", "pico_consolidation", "set_assembly", "validation"]

_INSTRUCTIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instructions")
_DEFAULT_FILES = {
    "context_locking": "agent1_context_locking.md",
    "pico_consolidation": "agent2_pico_consolidation.md",
    "set_assembly": "agent3_set_assembly.md",
    "validation": "agent4_validation.md",
}

_STATUS_DRAFT = "draft"
_STATUS_ACTIVE = "active"
_STATUS_ARCHIVED = "archived"


@dataclass
class PromptDefinition:
    prompt_id: str
    agent_id: str
    instruction_text: str
    version: int
    status: str
    created_by: str
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PromptRegistry:
    def __init__(self, store_dir: str = PROMPT_STORE_DIR):
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)
        self._ensure_seeded()

    # -- storage ---------------------------------------------------------

    def _agent_file(self, agent_id: str) -> str:
        if agent_id not in AGENT_IDS:
            raise ValueError(f"Unknown agent_id {agent_id!r} - must be one of {AGENT_IDS}")
        return os.path.join(self.store_dir, f"{agent_id}.json")

    def _ensure_seeded(self) -> None:
        for agent_id, filename in _DEFAULT_FILES.items():
            path = self._agent_file(agent_id)
            if os.path.exists(path):
                continue
            with open(os.path.join(_INSTRUCTIONS_DIR, filename), encoding="utf-8") as f:
                text = f.read()
            now = _now()
            seed = PromptDefinition(
                prompt_id=f"{agent_id}-v1", agent_id=agent_id, instruction_text=text,
                version=1, status=_STATUS_ACTIVE, created_by="system", created_at=now, updated_at=now,
            )
            self._write_all(agent_id, [seed])

    def _write_all(self, agent_id: str, versions: List[PromptDefinition]) -> None:
        with open(self._agent_file(agent_id), "w", encoding="utf-8") as f:
            json.dump([asdict(v) for v in versions], f, indent=2, ensure_ascii=False)

    def _read_all(self, agent_id: str) -> List[PromptDefinition]:
        with open(self._agent_file(agent_id), encoding="utf-8") as f:
            raw = json.load(f)
        return [PromptDefinition(**r) for r in raw]

    # -- public API --------------------------------------------------------

    def list_versions(self, agent_id: str) -> List[PromptDefinition]:
        """Doubles as the audit history."""
        return sorted(self._read_all(agent_id), key=lambda d: d.version)

    def get_active(self, agent_id: str) -> PromptDefinition:
        active = [v for v in self._read_all(agent_id) if v.status == _STATUS_ACTIVE]
        if not active:
            raise ValueError(f"No active prompt version for agent {agent_id!r}")
        return max(active, key=lambda d: d.version)

    def get_version(self, agent_id: str, version: int) -> PromptDefinition:
        for v in self._read_all(agent_id):
            if v.version == version:
                return v
        raise ValueError(f"No version {version} for agent {agent_id!r}")

    def save_draft(self, agent_id: str, instruction_text: str, created_by: str = "unknown") -> PromptDefinition:
        versions = self._read_all(agent_id)
        next_version = max((v.version for v in versions), default=0) + 1
        now = _now()
        new_version = PromptDefinition(
            prompt_id=f"{agent_id}-v{next_version}", agent_id=agent_id, instruction_text=instruction_text,
            version=next_version, status=_STATUS_DRAFT, created_by=created_by, created_at=now, updated_at=now,
        )
        versions.append(new_version)
        self._write_all(agent_id, versions)
        return new_version

    def activate(self, agent_id: str, version: int) -> PromptDefinition:
        versions = self._read_all(agent_id)
        target: Optional[PromptDefinition] = None
        for v in versions:
            if v.version == version:
                v.status = _STATUS_ACTIVE
                v.updated_at = _now()
                target = v
            elif v.status == _STATUS_ACTIVE:
                v.status = _STATUS_ARCHIVED
                v.updated_at = _now()
        if target is None:
            raise ValueError(f"No version {version} for agent {agent_id!r}")
        self._write_all(agent_id, versions)
        return target

    def restore(self, agent_id: str, version: int) -> PromptDefinition:
        """Restoring a previous version IS activating it - no new version
        is created, matching the task's 'restore previous version' requirement
        as a pointer flip, not a content copy."""
        return self.activate(agent_id, version)

    def compare(self, agent_id: str, version_a: int, version_b: int) -> str:
        a = self.get_version(agent_id, version_a)
        b = self.get_version(agent_id, version_b)
        diff = difflib.unified_diff(
            a.instruction_text.splitlines(), b.instruction_text.splitlines(),
            fromfile=f"{agent_id} v{version_a}", tofile=f"{agent_id} v{version_b}", lineterm="",
        )
        return "\n".join(diff)


_default_registry: Optional[PromptRegistry] = None


def get_registry() -> PromptRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = PromptRegistry()
    return _default_registry
