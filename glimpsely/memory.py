"""SQLite memory store: events / profile / dedupe / push_state / trigger_log."""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .understand import Record

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT,
  entities_json TEXT,
  deadline TEXT,
  importance INTEGER DEFAULT 3,
  user_intent TEXT,
  memory_note TEXT,
  raw_text TEXT,
  media_path TEXT,
  source TEXT DEFAULT 'wechat'
);
CREATE INDEX IF NOT EXISTS idx_events_deadline ON events(deadline)
  WHERE deadline IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS profile (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS dedupe (
  hash TEXT PRIMARY KEY,
  event_id INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS push_state (
  user_id TEXT PRIMARY KEY,
  context_token TEXT,
  alive INTEGER DEFAULT 1,
  push_count_hour INTEGER DEFAULT 0,
  push_hour TEXT,
  push_count_day INTEGER DEFAULT 0,
  push_day TEXT,
  last_push_at TEXT,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_user_ts ON chat_log(user_id, ts);

CREATE TABLE IF NOT EXISTS trigger_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER,
  trigger_type TEXT,
  fired_at TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fingerprint(text: str | None, media_path: Path | None) -> str:
    h = hashlib.sha256()
    if text:
        h.update(b"T" + text.strip().encode("utf-8"))
    if media_path and media_path.exists():
        h.update(b"M" + str(media_path.stat().st_size).encode())
        h.update(media_path.read_bytes()[:65536])
    return h.hexdigest()


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- events ----

    def save_event(self, rec: Record) -> tuple[int, bool]:
        """Returns (event_id, is_new). Duplicates return existing id."""
        fp = fingerprint(rec.raw_text, rec.media_path)
        row = self.conn.execute(
            "SELECT event_id FROM dedupe WHERE hash=?", (fp,)).fetchone()
        if row:
            return int(row["event_id"]), False
        cur = self.conn.execute(
            "INSERT INTO events (ts, kind, title, entities_json, deadline,"
            " importance, user_intent, memory_note, raw_text, media_path, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), rec.kind, rec.title, json.dumps(rec.entities, ensure_ascii=False),
             rec.deadline, rec.importance, rec.user_intent, rec.memory_note,
             rec.raw_text, str(rec.media_path) if rec.media_path else None,
             "demo" if rec.media_path and "assets" in str(rec.media_path) else "wechat"))
        eid = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO dedupe (hash, event_id, created_at) VALUES (?,?,?)",
            (fp, eid, _now()))
        self.conn.commit()
        return eid, True

    def event(self, event_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def events_between(self, start_iso: str, end_iso: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE ts>=? AND ts<? ORDER BY ts",
            (start_iso, end_iso)).fetchall()
        return [dict(r) for r in rows]

    def pending_deadlines(self, horizon_hours: float = 26.0) -> list[dict]:
        """Events with a deadline in (now, now+horizon] and not yet fired as deadline trigger."""
        now = datetime.now()
        until = (now + timedelta(hours=horizon_hours)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT * FROM events WHERE deadline IS NOT NULL AND deadline>? "
            "AND deadline<=? ORDER BY deadline", (now.isoformat(timespec="seconds"), until)
        ).fetchall()
        out = []
        for r in rows:
            if self._fired(int(r["id"]), "deadline"):
                continue
            out.append(dict(r))
        return out

    def upcoming_events_for_digest(self) -> list[dict]:
        now = datetime.now()
        until = (now + timedelta(hours=26)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT * FROM events WHERE deadline IS NOT NULL AND deadline>? AND deadline<=?"
            " ORDER BY deadline", (now.isoformat(timespec="seconds"), until)).fetchall()
        return [dict(r) for r in rows]

    # ---- profile ----

    def profile_get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM profile WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def profile_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO profile (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (key, value, _now()))
        self.conn.commit()

    def profile_all(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM profile").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def update_profile_from_record(self, rec: Record) -> None:
        """Incremental画像: record counters + kind-specific keys."""
        k = f"count_{rec.kind}"
        try:
            n = int(self.profile_get(k) or "0") + 1
        except ValueError:
            n = 1
        self.profile_set(k, str(n))
        self.profile_set("last_active", _now())

    # ---- push_state ----

    def push_state_upsert(self, user_id: str, context_token: str) -> None:
        self.conn.execute(
            "INSERT INTO push_state (user_id, context_token, alive, last_seen_at)"
            " VALUES (?,?,1,?)"
            " ON CONFLICT(user_id) DO UPDATE SET context_token=excluded.context_token,"
            " alive=1, last_seen_at=excluded.last_seen_at",
            (user_id, context_token, _now()))
        self.conn.commit()

    def push_state_get(self, user_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM push_state WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def push_mark_dead(self, user_id: str) -> None:
        self.conn.execute(
            "UPDATE push_state SET alive=0 WHERE user_id=?", (user_id,))
        self.conn.commit()

    def push_record(self, user_id: str, ok: bool) -> bool:
        """Record a push attempt; returns True if allowed under limits."""
        st = self.push_state_get(user_id) or {}
        now = datetime.now()
        hour_key = now.strftime("%Y-%m-%d-%H")
        day_key = now.strftime("%Y-%m-%d")
        if st.get("push_hour") != hour_key:
            st["push_count_hour"], st["push_hour"] = 0, hour_key
        if st.get("push_day") != day_key:
            st["push_count_day"], st["push_day"] = 0, day_key
        # limits read from config by caller; here we just track
        self.conn.execute(
            "INSERT INTO push_state (user_id, context_token, alive, push_count_hour,"
            " push_hour, push_count_day, push_day, last_push_at)"
            " VALUES (?,?,COALESCE((SELECT alive FROM push_state WHERE user_id=?),1),?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET push_count_hour=excluded.push_count_hour,"
            " push_hour=excluded.push_hour, push_count_day=excluded.push_count_day,"
            " push_day=excluded.push_day, last_push_at=excluded.last_push_at",
            (user_id, st.get("context_token", ""), user_id,
             int(st.get("push_count_hour", 0)) + 1, hour_key,
             int(st.get("push_count_day", 0)) + 1, day_key, _now()))
        self.conn.commit()
        return bool(ok)

    def push_counts(self, user_id: str) -> tuple[int, int]:
        st = self.push_state_get(user_id) or {}
        now = datetime.now()
        hour_key = now.strftime("%Y-%m-%d-%H")
        day_key = now.strftime("%Y-%m-%d")
        ch = int(st.get("push_count_hour", 0)) if st.get("push_hour") == hour_key else 0
        cd = int(st.get("push_count_day", 0)) if st.get("push_day") == day_key else 0
        return ch, cd

    # ---- chat log ----

    def add_chat_turn(self, user_id: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO chat_log (user_id, role, content, ts) VALUES (?,?,?,?)",
            (user_id, role, content, _now()))
        self.conn.commit()

    def recent_turns(self, user_id: str, limit: int = 6) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT role, content FROM chat_log WHERE user_id=?"
            " ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [(r["role"], r["content"] or "") for r in reversed(rows)]

    def history_text(self, user_id: str, limit: int = 6) -> str:
        turns = self.recent_turns(user_id, limit)
        if not turns:
            return ""
        return "最近对话（供参考）：\n" + "\n".join(
            f"{'用户' if role == 'user' else '助理'}：{c[:80]}"
            for role, c in turns)

    # ---- trigger log ----

    def mark_fired(self, event_id: int, trigger_type: str) -> None:
        self.conn.execute(
            "INSERT INTO trigger_log (event_id, trigger_type, fired_at) VALUES (?,?,?)",
            (event_id, trigger_type, _now()))
        self.conn.commit()

    def _fired(self, event_id: int, trigger_type: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM trigger_log WHERE event_id=? AND trigger_type=?",
            (event_id, trigger_type)).fetchone()
        return row is not None

    def fired(self, event_id: int, trigger_type: str) -> bool:
        return self._fired(event_id, trigger_type)
