"""Unit tests: skill router with a fake LLM client (no network)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from glimpsely.llm import build_content, extract_decision  # noqa: E402
from glimpsely.skills import route  # noqa: E402


class FakeClient:
    """Scripted OmlxClient stand-in: chat()/ocr() pop canned outputs."""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)

    def chat(self, prompt, image_path=None, max_tokens=None,
             temperature=None, timeout=None):
        return self.outputs.pop(0)

    def ocr(self, image_path, max_tokens=None, temperature=None):
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


def test_build_content_multi_image(tmp_path):
    a = tmp_path / "a.jpg"
    a.write_bytes(b"\xff\xd8fake-a")
    b = tmp_path / "b.jpg"
    b.write_bytes(b"\xff\xd8fake-b")
    content = build_content("问？", [a, b])
    imgs = [c for c in content if c["type"] == "image_url"]
    assert len(imgs) == 2
    assert content[-1] == {"type": "text", "text": "问？"}
    single = build_content("问？", a)
    assert len([c for c in single if c["type"] == "image_url"]) == 1
    text_only = build_content("问？", None)
    assert text_only == [{"type": "text", "text": "问？"}]


def test_handle_update_multi_image(tmp_path):
    import asyncio

    from glimpsely.bot import handle_update
    from glimpsely.config import Config
    from glimpsely.memory import MemoryStore
    from glimpsely.push import Pusher
    from glimpsely.triggers import TriggerEngine

    root = Path(__file__).resolve().parents[1]
    cfg = Config.load(root)
    store = MemoryStore(tmp_path / "m.db")
    pusher = Pusher(cfg, store, bot=None)
    engine = TriggerEngine(cfg, store, pusher, None)

    a = tmp_path / "a.jpg"
    a.write_bytes(b"\xff\xd8fake-a")
    b = tmp_path / "b.jpg"
    b.write_bytes(b"\xff\xd8fake-b")
    client = FakeClient([
        # 图1: route -> ocr
        '{"skill":"record","record":{"kind":"courier","title":"顺丰SF1",'
        '"entities":{},"deadline":null,"importance":3,"user_intent":"记快递",'
        '"memory_note":"顺丰一件"}}',
        "运单号SF1 取件码3002",
        # 图2: route -> ocr
        '{"skill":"record","record":{"kind":"note","title":"甜品照片",'
        '"entities":{},"deadline":null,"importance":2,"user_intent":"收藏",'
        '"memory_note":"一家甜品店"}}',
        "蛋糕 提拉米苏",
    ])
    reply = asyncio.run(handle_update(
        store, client, pusher, engine, "u@im.wechat",
        None, [a, b], "tok"))
    assert "已记下 2 条" in reply
    assert "顺丰一件" in reply and "甜品店" in reply
    rows = store.events_between("1970-01-01", "2999-12-31")
    assert len(rows) == 2
    ocrs = {r["ocr_text"] for r in rows}
    assert ocrs == {"运单号SF1 取件码3002", "蛋糕 提拉米苏"}


def test_daily_report_v2_composes_advice():
    from glimpsely.triggers import compose_daily_report
    events = [
        {"kind": "coupon", "title": "瑞幸咖啡满30减9.9券", "deadline": "2026-09-20",
         "ocr_text": "瑞幸咖啡 满30减9.9券 有效期至 2026-09-20"},
        {"kind": "note", "title": "跑步记录：2.67公里", "deadline": None,
         "ocr_text": "跑步 2.67公里 配速8:12"},
    ]
    out = compose_daily_report(events, {}, None, ttl_days=3)
    assert "最近记忆" in out
    assert "瑞幸" in out


def test_daily_report_v2_with_llm():
    from glimpsely.triggers import compose_daily_report

    class Fake:
        def chat(self, prompt, image_path=None, max_tokens=None,
                 temperature=None, timeout=None):
            assert "瑞幸" in prompt and "2.67" in prompt  # OCR 原文进 prompt
            return "这几天你记了咖啡券和夜跑，券周五到期记得用，跑步配速稳！"

    out = compose_daily_report(
        [{"kind": "coupon", "title": "瑞幸券", "deadline": None,
          "ocr_text": "瑞幸 有效期2026-09-20"},
         {"kind": "note", "title": "跑步2.67公里", "deadline": None, "ocr_text": "配速8:12"}],
        {}, Fake(), ttl_days=3)
    assert "咖啡券" in out and len(out) < 400
