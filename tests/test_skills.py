"""Unit tests: skill router with a fake LLM client (no network)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from glimpsely.llm import extract_decision  # noqa: E402
from glimpsely.skills import route  # noqa: E402


class FakeClient:
    """Scripted OmlxClient stand-in: chat() pops canned outputs."""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)

    def chat(self, prompt, image_path=None, max_tokens=None,
             temperature=None, timeout=None):
        return self.outputs.pop(0)


def run(coro):
    return asyncio.run(coro)


def test_extract_decision():
    assert extract_decision('{"skill": "chat", "reply": "hi"}')["skill"] == "chat"
    assert extract_decision('前置说明 {"skill":"query"} 后置')["skill"] == "query"
    assert extract_decision('{"kind": "note"}') is None
    assert extract_decision("纯文本") is None


def test_route_chat():
    client = FakeClient(['{"skill":"chat","reply":"今天也想记录点什么吗"}'])
    d = run(route(client, "你觉得周末去哪玩好？", None, ""))
    assert d.skill == "chat"
    assert d.reply_hint != ""


def test_route_record_nested():
    client = FakeClient([
        '{"skill":"record","record":{"kind":"coupon","title":"瑞幸9.9券",'
        '"entities":{"有效期":"2026-09-20"},"deadline":"2026-09-20",'
        '"importance":3,"user_intent":"记券","memory_note":"瑞幸券9-20到期"}}'])
    d = run(route(client, "[图片]", "/tmp/x.png", ""))
    assert d.skill == "record"
    assert d.record is not None and d.record.kind == "coupon"
    assert d.record.deadline == "2026-09-20"


def test_route_record_flattened():
    client = FakeClient([
        '{"skill":"record","kind":"courier","title":"顺丰SF1",'
        '"entities":{"单号":"SF1"},"deadline":null,"importance":4,'
        '"user_intent":"记快递","memory_note":"顺丰一件"}'])
    d = run(route(client, "记一下这个快递", None, ""))
    assert d.skill == "record"
    assert d.record.kind == "courier"


def test_route_record_without_record_degrades():
    client = FakeClient(['{"skill":"record"}'])
    d = run(route(client, "[图片]", "/tmp/y.png", ""))
    assert d.skill == "record" and d.record is not None and d.record.degraded


def test_route_garbage_text_falls_to_chat():
    client = FakeClient(["我想想啊……{不完整"])
    d = run(route(client, "在吗", None, ""))
    assert d.skill == "chat"


def test_route_garbage_image_falls_to_record():
    client = FakeClient(["我觉得这张图可能是个聊天记录截图，但也说不好"])
    d = run(route(client, None, "/tmp/z.png", ""))
    assert d.skill == "record" and d.record.degraded


def test_route_exception_falls_to_chat():
    class Boom:
        def chat(self, *args, **kwargs):
            raise RuntimeError("boom")
    d = run(route(Boom(), "hello", None, ""))
    assert d.skill == "chat"
