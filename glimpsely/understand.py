"""Understanding pipeline: raw message -> validated Record."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMError, OmlxClient, extract_record

VALID_KINDS = {"courier", "bill", "coupon", "event", "address",
               "person", "chat_digest", "note", "other"}


@dataclass
class Record:
    kind: str = "note"
    title: str = ""
    entities: dict = field(default_factory=dict)
    deadline: str | None = None
    importance: int = 3
    user_intent: str = ""
    memory_note: str = ""
    raw_text: str | None = None
    media_path: Path | None = None
    degraded: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "kind": self.kind, "title": self.title,
            "entities": self.entities, "deadline": self.deadline,
            "importance": self.importance, "user_intent": self.user_intent,
            "memory_note": self.memory_note,
        }, ensure_ascii=False)


def _coerce(data: dict, raw_text: str | None, media_path: Path | None) -> Record:
    kind = data.get("kind", "note")
    if kind not in VALID_KINDS:
        kind = "other"
    try:
        importance = int(data.get("importance", 3))
    except (TypeError, ValueError):
        importance = 3
    importance = max(1, min(5, importance))
    deadline = data.get("deadline")
    if deadline in ("", "null", None):
        deadline = None
    entities = data.get("entities")
    if not isinstance(entities, dict):
        entities = {}
    entities = {str(k): str(v) for k, v in entities.items()}
    return Record(
        kind=kind,
        title=str(data.get("title") or "")[:60],
        entities=entities,
        deadline=deadline,
        importance=importance,
        user_intent=str(data.get("user_intent") or "")[:40],
        memory_note=str(data.get("memory_note") or "")[:60],
        raw_text=raw_text,
        media_path=media_path,
    )


def _fallback(raw_text: str | None, media_path: Path | None) -> Record:
    title = (raw_text or "").strip()[:40]
    if not title and media_path:
        title = f"[图片] {media_path.name}"
    return Record(kind="note", title=title or "[空消息]",
                  memory_note=title or "[图片消息，待补理解]",
                  raw_text=raw_text, media_path=media_path, degraded=True)


def understand_message(client: OmlxClient,
                       text: str | None,
                       image_path: Path | None) -> Record:
    if text is None and image_path is None:
        raise ValueError("text and image_path cannot both be None")
    try:
        raw = client.understand(text, image_path)
        data = extract_record(raw)
        if data is None:
            # bounded repair: one retry turning prior output into pure JSON
            raw2 = client.repair(raw, text, image_path)
            data = extract_record(raw2)
        if data is None:
            raise LLMError("no JSON in output after repair")
        return _coerce(data, text, image_path)
    except (LLMError, Exception):
        return _fallback(text, image_path)
