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
