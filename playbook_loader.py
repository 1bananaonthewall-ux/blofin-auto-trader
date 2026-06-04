"""Load curated playbooks (Moon Dev / X threads) — no paid APIs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_PLAYBOOK = ROOT / "playbooks" / "moon_dev_intel.json"


@dataclass(frozen=True)
class PlaybookDoctrine:
    id: str
    title: str
    summary: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Playbook:
    version: int
    references: list[str]
    doctrines: dict[str, PlaybookDoctrine]


def load_playbook(path: Path | None = None) -> Playbook:
    p = path or DEFAULT_PLAYBOOK
    if not p.is_file():
        log.warning("playbook missing: %s", p)
        return Playbook(version=0, references=[], doctrines={})
    raw = json.loads(p.read_text(encoding="utf-8"))
    doctrines: dict[str, PlaybookDoctrine] = {}
    for item in raw.get("doctrines", []):
        did = str(item.get("id", ""))
        if not did:
            continue
        params = {k: v for k, v in item.items() if k not in ("id", "title", "summary")}
        doctrines[did] = PlaybookDoctrine(
            id=did,
            title=str(item.get("title", did)),
            summary=str(item.get("summary", "")),
            params=params,
        )
    return Playbook(
        version=int(raw.get("version", 1)),
        references=list(raw.get("references", [])),
        doctrines=doctrines,
    )
