"""Unit tests: reactivation via semantic retrieval (real sqlite-vec + fake LLM)."""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from glimpsely.memory import MemoryStore  # noqa: E402
from glimpsely.skills import answer_query  # noqa: E402


def test_answer_query_reactivates_forgotten(tmp_path):
    store = MemoryStore(tmp_path / "q.db")
    ts = (datetime.now() - timedelta(days=6)).isoformat(timespec="seconds")
    frozen_at = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    store.conn.execute(
        "INSERT INTO events (ts, kind, title, memory_note, ocr_text,"
        " forgotten, forgotten_at) VALUES (?,?,?,?,?,1,?)",
        (ts, "note", "甜品店蛋糕照片", "一家甜品店的提拉米苏蛋糕",
         "草莓蛋糕 ¥45", frozen_at))
    eid = store.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    store.conn.execute(
        "INSERT INTO event_vecs (id, embedding) VALUES (?,?)",
        (eid, __import__("glimpsely.embeddings", fromlist=["embed_one"]).embed_one(
            "甜品店蛋糕 提拉米苏 草莓蛋糕")))
    store.conn.commit()

    class Fake:
        def chat(self, prompt, image_path=None, max_tokens=None,
                 temperature=None, timeout=None):
            assert "草莓蛋糕" in prompt  # 原文进 prompt
            return "你说的是草莓蛋糕 ¥45 那家甜品店。"

    reply = asyncio.run(answer_query(Fake(), "之前那个甜品店多少钱", store, "u@im.wechat"))
    assert "草莓蛋糕" in reply
    assert "唤醒" in reply
    n = store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE forgotten=1").fetchone()[0]
    assert n == 0
