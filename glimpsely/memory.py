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
  ocr_text TEXT,
  forgotten INTEGER DEFAULT 0,
  forgotten_at TEXT,
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


def fingerprint(text: str | None, media_path: Path | None,
                semantic: str = "") -> str:
    h = hashlib.sha256()
    if text:
        h.update(b"T" + text.strip().encode("utf-8"))
    if media_path and media_path.exists():
        h.update(b"M" + str(media_path.stat().st_size).encode())
        h.update(media_path.read_bytes()[:65536])
    if semantic:
        h.update(b"S" + semantic.strip().encode("utf-8"))
    return h.hexdigest()


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._init_vec()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "ocr_text" not in cols:
            self.conn.execute("ALTER TABLE events ADD COLUMN ocr_text TEXT")
        if "forgotten" not in cols:
            self.conn.execute(
                "ALTER TABLE events ADD COLUMN forgotten INTEGER DEFAULT 0")
        if "forgotten_at" not in cols:
            self.conn.execute("ALTER TABLE events ADD COLUMN forgotten_at TEXT")

    def _init_vec(self) -> None:
        """Attach sqlite-vec KNN engine; degrade gracefully if unavailable."""
        self.has_vec = False
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            from .embeddings import detect_dim
            dim = detect_dim()
            stored = self.profile_get("_vec_dim")
            exists = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='event_vecs'").fetchone() is not None
            if exists and stored and int(stored) != dim:
                # 嵌入模型切换 → 维度变化 → 重建（启动后 backfill 重新索引）
                self.conn.execute("DROP TABLE event_vecs")
                exists = False
            if exists and not stored:
                # 旧库无元数据（遗留512表）：当前维度不同则重建
                if dim != 512:
                    self.conn.execute("DROP TABLE event_vecs")
                else:
                    self.profile_set("_vec_dim", str(dim))
            self.conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS event_vecs USING vec0("
                f"id integer primary key, embedding float[{dim}])")
            self.profile_set("_vec_dim", str(dim))
            self.has_vec = True
        except Exception:  # noqa: BLE001 — 无 vec 引擎时退化为按时间检索
            self.has_vec = False

    def close(self) -> None:
        self.conn.close()

    # ---- events ----

    def save_event(self, rec: Record) -> tuple[int, bool]:
        """Returns (event_id, is_new). Duplicates return existing id."""
        semantic = " ".join(filter(None, [
            rec.title, rec.memory_note, rec.ocr_text or "",
            json.dumps(rec.entities, ensure_ascii=False) if rec.entities else ""]))
        fp = fingerprint(rec.raw_text, rec.media_path, semantic)
        row = self.conn.execute(
            "SELECT event_id FROM dedupe WHERE hash=?", (fp,)).fetchone()
        if row:
            return int(row["event_id"]), False
        cur = self.conn.execute(
            "INSERT INTO events (ts, kind, title, entities_json, deadline,"
            " importance, user_intent, memory_note, raw_text, media_path, ocr_text, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), rec.kind, rec.title, json.dumps(rec.entities, ensure_ascii=False),
             rec.deadline, rec.importance, rec.user_intent, rec.memory_note,
             rec.raw_text, str(rec.media_path) if rec.media_path else None,
             rec.ocr_text,
             "demo" if rec.media_path and "assets" in str(rec.media_path) else "wechat"))
        eid = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO dedupe (hash, event_id, created_at) VALUES (?,?,?)",
            (fp, eid, _now()))
        self.conn.commit()
        self._index_embedding(eid, rec)
        return eid, True

    def _index_embedding(self, event_id: int, rec) -> None:
        from .embeddings import embed_one
        text = " ".join(filter(None, [
            rec.title, rec.memory_note, rec.ocr_text or "",
            json.dumps(rec.entities, ensure_ascii=False)]))
        try:
            blob = embed_one(text)
            self.conn.execute(
                "INSERT INTO event_vecs (id, embedding) VALUES (?,?)",
                (event_id, blob))
        except Exception:  # noqa: BLE001 — 向量索引失败不影响入库
            pass

    def backfill_embeddings(self) -> int:
        """为存量事件补建向量索引（幂等，启动时调用）。"""
        if not self.has_vec:
            return 0
        from .embeddings import embed_one
        missing = self.conn.execute(
            "SELECT e.id, e.title, e.memory_note, e.ocr_text, e.entities_json"
            " FROM events e LEFT JOIN event_vecs v ON v.id=e.id"
            " WHERE v.id IS NULL").fetchall()
        n = 0
        for row in missing:
            text = " ".join(filter(None, [
                row["title"], row["memory_note"] or "", row["ocr_text"] or "",
                row["entities_json"] or ""]))
            try:
                self.conn.execute(
                    "INSERT INTO event_vecs (id, embedding) VALUES (?,?)",
                    (int(row["id"]), embed_one(text)))
                n += 1
            except Exception:  # noqa: BLE001
                pass
        self.conn.commit()
        return n

    def event(self, event_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def events_between(self, start_iso: str, end_iso: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE ts>=? AND ts<? ORDER BY ts",
            (start_iso, end_iso)).fetchall()
        return [dict(r) for r in rows]

    def pending_deadlines(self, horizon_hours: float = 26.0,
                          trigger_type: str = "deadline") -> list[dict]:
        """Active events with a deadline in (now, now+horizon] not yet fired for this stage."""
        now = datetime.now()
        until = (now + timedelta(hours=horizon_hours)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT * FROM events WHERE deadline IS NOT NULL AND deadline>? "
            "AND deadline<=? AND forgotten=0 ORDER BY deadline",
            (now.isoformat(timespec="seconds"), until)
        ).fetchall()
        out = []
        for r in rows:
            if self._fired(int(r["id"]), trigger_type):
                continue
            out.append(dict(r))
        return out

    def upcoming_events_for_digest(self) -> list[dict]:
        now = datetime.now()
        until = (now + timedelta(hours=26)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT * FROM events WHERE deadline IS NOT NULL AND deadline>? AND deadline<=?"
            " AND forgotten=0 ORDER BY deadline",
            (now.isoformat(timespec="seconds"), until)).fetchall()
        return [dict(r) for r in rows]

    # ---- forgetting ----

    def forget_old(self, ttl_days: int = 3) -> int:
        """Soft-forget events older than ttl_days; returns newly forgotten count."""
        cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "UPDATE events SET forgotten=1, forgotten_at=? "
            "WHERE forgotten=0 AND ts < ?", (_now(), cutoff))
        self.conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def reactivate(self, event_id: int) -> dict | None:
        """唤醒遗忘记录：deadline 按原始剩余时长重新锚定到当前。"""
        row = self.event(event_id)
        if row is None:
            return None
        new_deadline = row.get("deadline")
        if new_deadline:
            try:
                dl = datetime.fromisoformat(new_deadline)
                ts = datetime.fromisoformat(row["ts"])
                delta = dl - ts
                if delta.total_seconds() > 0:
                    new_deadline = (datetime.now() + delta).isoformat(timespec="seconds")
            except (ValueError, TypeError):
                pass
        self.conn.execute(
            "UPDATE events SET forgotten=0, forgotten_at=NULL, deadline=? WHERE id=?",
            (new_deadline, event_id))
        self.conn.commit()
        return self.event(event_id)

    def active_events_recent(self, ttl_days: int = 3) -> list[dict]:
        cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT * FROM events WHERE forgotten=0 AND ts>=? ORDER BY ts",
            (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 情境信号层（贴心建议的事实来源，纯确定性计算） ----

    def situation_signals(self, ttl_days: int = 3) -> dict:
        """从记忆库提取可推断的情境信号，供建议引擎挑选。

        全部基于已记录的事实（entities/deadline/kind），不做语义猜测。
        """
        now = datetime.now()
        rows = self.active_events_recent(ttl_days)
        expiring: list[dict] = []
        couriers: list[dict] = []
        coupons: list[dict] = []
        brands: dict[str, int] = {}
        for r in rows:
            if r.get("deadline"):
                try:
                    dl = datetime.fromisoformat(r["deadline"])
                    days = (dl - now).total_seconds() / 86400
                    if 0 < days <= 3:
                        expiring.append({
                            "title": r["title"], "kind": r["kind"],
                            "deadline": r["deadline"], "days_left": round(days, 1),
                            "entities": r.get("entities_json"),
                        })
                except (ValueError, TypeError):
                    pass
            if r["kind"] == "courier":
                ts = datetime.fromisoformat(r["ts"])
                if (now - ts).total_seconds() <= 48 * 3600:
                    couriers.append({"title": r["title"], "ts": r["ts"],
                                     "entities": r.get("entities_json")})
            if r["kind"] == "coupon":
                coupons.append(r)
            try:
                ents = json.loads(r.get("entities_json") or "{}")
                for key in ("品牌", "门店", "店名", "平台"):
                    v = ents.get(key)
                    if v:
                        brands[str(v)] = brands.get(str(v), 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

        # 习惯连击：某 kind 连续出现的天数（按自然日）
        day_kinds: dict[str, set[str]] = {}
        for r in rows:
            day = r["ts"][:10]
            day_kinds.setdefault(r["kind"], set()).add(day)
        streaks = []
        for kind, days in day_kinds.items():
            streak, d = 0, now.date()
            while d.isoformat() in days:
                streak += 1
                d -= timedelta(days=1)
            if streak >= 3:
                streaks.append({"kind": kind, "days": streak})

        return {
            "expiring": sorted(expiring, key=lambda x: x["days_left"]),
            "couriers": couriers,
            "coupon_count": len(coupons),
            "streaks": sorted(streaks, key=lambda x: -x["days"]),
            "top_brands": sorted(brands.items(), key=lambda kv: -kv[1])[:3],
            "hour": now.hour,
        }

    def semantic_search(self, query: str, k: int = 8) -> list[dict]:
        """向量检索（含遗忘记录）；vec 不可用时退化为最近记录。"""
        from .embeddings import embed_one
        if self.has_vec:
            try:
                blob = embed_one(query)
                hits = self.conn.execute(
                    "SELECT id, distance FROM event_vecs "
                    "WHERE embedding MATCH ? AND k=? ORDER BY distance",
                    (blob, k)).fetchall()
                out = []
                for h in hits:
                    row = self.event(int(h["id"]))
                    if row is not None:
                        d = dict(row)
                        d["distance"] = h["distance"]
                        out.append(d)
                return out
            except Exception:  # noqa: BLE001
                pass
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (k,)).fetchall()
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

    def history_text(self, user_id: str, limit: int = 8) -> str:
        turns = self.recent_turns(user_id, limit)
        if not turns:
            return ""
        return "最近对话（供参考）：\n" + "\n".join(
            f"{'用户' if role == 'user' else '助理'}：{c[:80]}"
            for role, c in turns)

    def briefing(self, max_events: int = 8) -> str:
        """记忆简报：画像事实 + 最近 active 事件一行摘要（注入每次 LLM 调用）。"""
        facts = {k: v for k, v in self.profile_all().items()
                 if not k.startswith(("count_", "_"))}
        lines = []
        if facts:
            lines.append("用户画像：" + "；".join(f"{k}={v}" for k, v in facts.items()))
        rows = self.conn.execute(
            "SELECT kind, title, ts FROM events WHERE forgotten=0"
            " ORDER BY ts DESC LIMIT ?", (max_events,)).fetchall()
        if rows:
            lines.append("已记录的记忆（最近优先，可直接引用）：")
            for r in reversed(rows):
                lines.append(f"- [{r['kind']}] {r['title']} ({r['ts'][:16]})")
        return "\n".join(lines)

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

    def fired_today(self, trigger_type: str) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trigger_log WHERE trigger_type=?"
            " AND fired_at LIKE ?", (trigger_type, today + "%")).fetchone()
        return int(row["n"])

    def fired(self, event_id: int, trigger_type: str) -> bool:
        return self._fired(event_id, trigger_type)
