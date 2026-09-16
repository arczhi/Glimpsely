"""Unit tests: memory store + understand coercion (no LLM calls)."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from glimpsely.llm import extract_json, extract_record
from glimpsely.memory import MemoryStore
from glimpsely.understand import Record, _coerce, _fallback


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    yield s
    s.close()


def test_save_and_dedupe(store, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG-fake")
    rec = Record(kind="courier", title="顺丰", entities={"单号": "SF1"},
                 memory_note="顺丰快递", media_path=img)
    eid, is_new = store.save_event(rec)
    assert is_new
    eid2, is_new2 = store.save_event(rec)
    assert not is_new2 and eid2 == eid


def test_fingerprint_text_differs(store):
    r1 = Record(kind="note", title="a", raw_text="hello")
    r2 = Record(kind="note", title="b", raw_text="hello2")
    e1, n1 = store.save_event(r1)
    e2, n2 = store.save_event(r2)
    assert n1 and n2 and e1 != e2


def test_pending_deadlines_horizon(store):
    soon = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
    far = (datetime.now() + timedelta(days=5)).isoformat(timespec="seconds")
    past = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    store.save_event(Record(kind="event", title="soon", deadline=soon))
    store.save_event(Record(kind="event", title="far", deadline=far))
    store.save_event(Record(kind="event", title="past", deadline=past))
    pending = store.pending_deadlines(horizon_hours=26.0)
    titles = {p["title"] for p in pending}
    assert titles == {"soon"}


def test_mark_fired_dedupes_triggers(store):
    soon = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
    eid, _ = store.save_event(Record(kind="event", title="m", deadline=soon))
    store.mark_fired(eid, "deadline")
    assert store.pending_deadlines(horizon_hours=26.0) == []


def test_profile_counters(store):
    store.save_event(Record(kind="coupon", title="c1"))
    store.save_event(Record(kind="coupon", title="c2"))
    rec = Record(kind="coupon", title="c3")
    store.update_profile_from_record(rec)
    assert store.profile_get("count_coupon") == "1"  # save_event不更新画像
    assert store.profile_get("last_active") is not None


def test_push_state_lifecycle(store):
    store.push_state_upsert("u1@im.wechat", "tok1")
    st = store.push_state_get("u1@im.wechat")
    assert st["alive"] == 1 and st["context_token"] == "tok1"
    store.push_mark_dead("u1@im.wechat")
    assert store.push_state_get("u1@im.wechat")["alive"] == 0
    store.push_state_upsert("u1@im.wechat", "tok2")
    assert store.push_state_get("u1@im.wechat")["alive"] == 1


def test_push_counts_hourly(store):
    store.push_state_upsert("u2@im.wechat", "t")
    for _ in range(3):
        store.push_record("u2@im.wechat", True)
    h, d = store.push_counts("u2@im.wechat")
    assert h == 3 and d == 3


def test_chat_log_roundtrip(store):
    store.add_chat_turn("u1@im.wechat", "user", "看看最近记了啥")
    store.add_chat_turn("u1@im.wechat", "assistant", "最近记了3条")
    store.add_chat_turn("u1@im.wechat", "user", "日报呢")
    turns = store.recent_turns("u1@im.wechat", limit=2)
    assert turns == [("assistant", "最近记了3条"), ("user", "日报呢")]
    text = store.history_text("u1@im.wechat", limit=6)
    assert "用户：" in text and "助理：" in text
    assert store.history_text("nobody@im.wechat") == ""


def test_demo_config_isolation():
    # pin: demo 数据永远不写进生产库
    from glimpsely.demo import demo_config
    root = Path(__file__).resolve().parents[1]
    cfg = demo_config(root)
    assert cfg.db_path == root / "data/demo.db"
    assert cfg.db_path != root / "data/glimpsely.db"
    assert cfg.media_dir == root / "data/demo_media"


def test_ocr_text_roundtrip(store):
    rec = Record(kind="note", title="小票", media_path=None,
                 ocr_text="订单号: 20260915-8823 宫保鸡丁 x1")
    eid, is_new = store.save_event(rec)
    assert is_new
    row = store.event(eid)
    assert row["ocr_text"].startswith("订单号")


def test_ocr_column_migration(tmp_path):
    # pin: 旧库（无 ocr_text 列）打开时自动补列
    import sqlite3
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript("""
    CREATE TABLE events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, title TEXT,
      entities_json TEXT, deadline TEXT, importance INTEGER, user_intent TEXT,
      memory_note TEXT, raw_text TEXT, media_path TEXT, source TEXT
    );
    """)
    conn.execute("INSERT INTO events (ts, kind, title) VALUES ('t1','note','旧记录')")
    conn.commit()
    conn.close()
    s = MemoryStore(legacy)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(events)")}
    assert "ocr_text" in cols and "forgotten" in cols
    rows = s.conn.execute("SELECT * FROM events").fetchall()
    assert len(rows) == 1  # 旧数据完好


def test_forget_old_soft_marks(store):
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    fresh = datetime.now().isoformat(timespec="seconds")
    store.conn.execute(
        "INSERT INTO events (ts, kind, title) VALUES (?, 'note', '旧记录')", (old,))
    store.conn.execute(
        "INSERT INTO events (ts, kind, title) VALUES (?, 'note', '新记录')", (fresh,))
    conn_row = store.conn
    conn_row.commit()
    n = store.forget_old(ttl_days=3)
    assert n == 1
    forgotten = conn_row.execute(
        "SELECT title FROM events WHERE forgotten=1").fetchall()
    assert [r["title"] for r in forgotten] == ["旧记录"]
    # 幂等
    assert store.forget_old(ttl_days=3) == 0


def test_reactivate_reanchors_deadline(store):
    from datetime import datetime, timedelta
    ts = (datetime.now() - timedelta(days=6)).isoformat(timespec="seconds")
    deadline = (datetime.fromisoformat(ts) + timedelta(days=5)).isoformat(timespec="seconds")
    eid, _ = store.save_event(Record(kind="coupon", title="旧券", deadline=deadline))
    store.conn.execute("UPDATE events SET ts=? WHERE id=?", (ts, eid))
    store.conn.commit()
    store.forget_old(ttl_days=3)
    assert store.event(eid)["forgotten"] == 1

    before = datetime.now()
    row = store.reactivate(eid)
    after = datetime.now()
    assert row["forgotten"] == 0 and row["forgotten_at"] is None
    new_dl = datetime.fromisoformat(row["deadline"])
    expected = before + timedelta(days=5)
    assert new_dl >= expected - timedelta(seconds=5)
    assert new_dl <= after + timedelta(days=5) + timedelta(seconds=5)


def test_semantic_search_finds_relevant(tmp_path):
    from glimpsely.memory import MemoryStore as MS
    s = MS(tmp_path / "vec.db")
    s.save_event(Record(kind="note", title="瑞幸咖啡优惠券",
                        memory_note="瑞幸咖啡满30减9.9券", ocr_text="有效期至2026-09-20"))
    s.save_event(Record(kind="note", title="甜品店蛋糕",
                        memory_note="一家甜品店的提拉米苏蛋糕", ocr_text="草莓蛋糕"))
    s.save_event(Record(kind="courier", title="顺丰快递",
                        memory_note="顺丰快递取件码3002", ocr_text="丰巢"))
    hits = s.semantic_search("之前记的蛋糕甜品是哪家", k=2)
    assert len(hits) >= 1
    assert "蛋糕" in hits[0]["title"] or "甜品" in hits[0]["title"] or \
        "蛋糕" in (hits[0]["memory_note"] or "")


def test_coerce_validates():
    rec = _coerce({"kind": "HACK", "title": "x" * 100, "importance": 99,
                   "deadline": "", "entities": "not-a-dict",
                   "user_intent": "y" * 100, "memory_note": "z"},
                  "raw", None)
    assert rec.kind == "other"
    assert rec.importance == 5
    assert rec.deadline is None
    assert rec.entities == {}
    assert len(rec.title) <= 60


def test_fallback_degraded():
    rec = _fallback(None, None)
    assert rec.degraded and rec.kind == "note"


def test_extract_json_variants():
    assert extract_json('{"a":1}') == {"a": 1}
    assert extract_json('前言 {"a":1} 后言') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json("no json here") is None


def test_extract_json_skips_embedded_fragments():
    # pin: LLM夹带解释文字时，entities片段不是记录本体，必须取含kind的对象
    raw = (
        "检查各项要求：\n"
        '*   entities: {"快递公司": "顺丰速运", "单号": "SF13688889999"}\n'
        "*修正*：再仔细看图片，第四行是取件码\n"
        '{"kind": "courier", "title": "顺丰快递", "entities": {"单号": "SF13688889999"},'
        ' "deadline": null, "importance": 3, "user_intent": "记快递", "memory_note": "顺丰一件"}'
    )
    data = extract_record(raw)
    assert data is not None and data.get("kind") == "courier"


def test_extract_json_prefers_record_over_inner_dicts():
    raw = ('结论 {"快递公司": "顺丰"} 最终答案\n'
           '{"kind": "bill", "title": "水电费", "entities": {}, "deadline": null,'
           ' "importance": 2, "user_intent": "记账", "memory_note": "水电费"}')
    assert extract_record(raw)["kind"] == "bill"


def test_briefing_includes_profile_and_events(store):
    from glimpsely.understand import Record as R
    store.save_event(R(kind="courier", title="顺丰快递一件", memory_note="顺丰"))
    store.profile_set("常去健身房", "星河店")
    b = store.briefing(max_events=5)
    assert "顺丰快递一件" in b and "[courier]" in b
    assert "常去健身房=星河店" in b


def test_briefing_empty(store):
    assert store.briefing() == ""


def test_config_paths_anchor_to_project_root(tmp_path, monkeypatch):
    # pin: 从任何 cwd 启动，DB 都锚定项目根（防"重启丢记忆"类 bug）
    from glimpsely.config import Config
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("MEDIA_DIR", raising=False)
    cfg = Config.load()
    assert cfg.db_path.name == "glimpsely.db"
    assert "data" in str(cfg.db_path)
    assert cfg.db_path.is_absolute()
