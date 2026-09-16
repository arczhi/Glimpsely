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
        self.prompts: list[str] = []
        self.images: list = []

    def chat(self, prompt, image_path=None, max_tokens=None,
             temperature=None, timeout=None):
        self.prompts.append(prompt)
        self.images.append(image_path)
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


def test_handle_update_multi_image(tmp_path, monkeypatch):
    import asyncio

    import glimpsely.bot as bot_mod
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
    # patch 在使用处（bot 模块的全局绑定）
    monkeypatch.setattr(bot_mod, "full_ocr", lambda client, p: f"OCR:{Path(p).name}")
    monkeypatch.setattr(bot_mod, "downscale_for_llm", lambda p: Path(p))
    client = FakeClient([
        # 图1 route -> 图2 route
        '{"skill":"record","record":{"kind":"courier","title":"顺丰SF1",'
        '"entities":{},"deadline":null,"importance":3,"user_intent":"记快递",'
        '"memory_note":"顺丰一件"}}',
        '{"skill":"record","record":{"kind":"note","title":"甜品照片",'
        '"entities":{},"deadline":null,"importance":2,"user_intent":"收藏",'
        '"memory_note":"一家甜品店"}}',
    ])
    reply = asyncio.run(handle_update(
        store, client, pusher, engine, "u@im.wechat",
        None, [a, b], "tok"))
    assert "已记下 2 条" in reply
    assert "顺丰一件" in reply and "甜品店" in reply
    assert "OCR:a.jpg" in client.prompts[0]  # 图1 原文进了第一次 route
    assert "OCR:b.jpg" in client.prompts[1]  # 图2 原文进了第二次 route
    rows = store.events_between("1970-01-01", "2999-12-31")
    assert len(rows) == 2
    assert {r["ocr_text"] for r in rows} == {"OCR:a.jpg", "OCR:b.jpg"}


def test_daily_report_v2_composes_advice():
    from glimpsely.triggers import compose_daily_report
    events = [
        {"kind": "coupon", "title": "瑞幸咖啡满30减9.9券", "deadline": "2026-09-20",
         "ocr_text": "瑞幸咖啡 满30减9.9券 有效期至 2026-09-20"},
        {"kind": "note", "title": "跑步记录：2.67公里", "deadline": None,
         "ocr_text": "跑步 2.67公里 配速8:12"},
    ]
    out = compose_daily_report(events, {}, None, ttl_days=3)
    assert "近3天记了 2 件" in out  # v3：一句概括
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


def test_downscale_for_llm(tmp_path):
    from PIL import Image

    from glimpsely.ocr import downscale_for_llm
    big = tmp_path / "big.jpg"
    Image.new("RGB", (2400, 1200), "white").save(big)
    out = downscale_for_llm(big)
    assert out != big
    with Image.open(out) as im:
        assert max(im.size) <= 1280
    # 小图原样返回
    small = tmp_path / "small.jpg"
    Image.new("RGB", (800, 600), "white").save(small)
    assert downscale_for_llm(small) == small


def test_rich_ocr_bypasses_vision(tmp_path, monkeypatch):
    import asyncio

    import glimpsely.bot as bot_mod
    from glimpsely.bot import handle_update
    from glimpsely.config import Config
    from glimpsely.memory import MemoryStore
    from glimpsely.push import Pusher
    from glimpsely.triggers import TriggerEngine

    cfg = Config.load(Path(__file__).resolve().parents[1])
    store = MemoryStore(tmp_path / "r.db")
    pusher = Pusher(cfg, store, bot=None)
    engine = TriggerEngine(cfg, store, pusher, None)
    img = tmp_path / "doc.jpg"
    img.write_bytes(b"\xff\xd8fake")

    rich = "长文本截图内容 " * 20  # >= 80 字
    monkeypatch.setattr(bot_mod, "full_ocr", lambda c, p: rich)
    client = FakeClient([
        '{"skill":"record","record":{"kind":"chat_digest","title":"聊天记录",'
        '"entities":{},"deadline":null}}',
    ])
    reply = asyncio.run(handle_update(
        store, client, pusher, engine, "u@im.wechat", None, [img], "tok"))
    assert "已记下" in reply
    assert "截图OCR原文" in client.prompts[0] and "长文本截图内容" in client.prompts[0]


def test_needs_vision_escape_hatch(tmp_path, monkeypatch):
    import asyncio

    import glimpsely.bot as bot_mod
    from glimpsely.bot import handle_update
    from glimpsely.config import Config
    from glimpsely.memory import MemoryStore
    from glimpsely.push import Pusher
    from glimpsely.triggers import TriggerEngine

    cfg = Config.load(Path(__file__).resolve().parents[1])
    store = MemoryStore(tmp_path / "v.db")
    pusher = Pusher(cfg, store, bot=None)
    engine = TriggerEngine(cfg, store, pusher, None)
    img = tmp_path / "food.jpg"
    img.write_bytes(b"\xff\xd8fake")

    # OCR 零散文字（>=80字）但模型判断不代表主题 → needs_vision → 带图重跑
    monkeypatch.setattr(bot_mod, "full_ocr", lambda c, p: "宫保鸡丁 麻婆豆腐 水煮鱼 优惠 " * 5)
    monkeypatch.setattr(bot_mod, "downscale_for_llm", lambda p: Path(p))
    client = FakeClient([
        '{"skill":"record","needs_vision":true,"record":{"kind":"note","title":"零散文字",'
        '"entities":{},"deadline":null}}',
        '{"skill":"record","needs_vision":false,"record":{"kind":"note",'
        '"title":"三道菜家常餐","entities":{},"deadline":null}}',
    ])
    reply = asyncio.run(handle_update(
        store, client, pusher, engine, "u@im.wechat", None, [img], "tok"))
    assert "三道菜" in reply
    assert len(client.prompts) == 2          # 两次调用
    assert client.images[0] is None          # 第一次纯文本（快路径）
    assert client.images[1] == img           # 第二次带图


def test_two_phase_deadline_trigger(tmp_path):
    import asyncio
    from datetime import datetime, timedelta

    from glimpsely.bot import build_pipeline
    from glimpsely.config import Config
    from glimpsely.understand import Record

    cfg = Config.load(Path(__file__).resolve().parents[1])
    cfg.db_path = tmp_path / "t.db"
    cfg.media_dir = tmp_path
    store, llm, pusher, engine = build_pipeline(cfg)
    store.push_state_upsert("u@im.wechat", "tok")
    # 25h 后截止 → 阶段1首推（记录 deadline），阶段2不动
    dl1 = (datetime.now() + timedelta(hours=25)).isoformat(timespec="seconds")
    eid1, _ = store.save_event(Record(kind="coupon", title="首推券", deadline=dl1))
    fired = asyncio.run(engine.fire_deadlines("u@im.wechat"))
    assert fired == 1 and store.fired(eid1, "deadline") and not store.fired(eid1, "deadline_soon")
    # 30 分钟后截止 → 阶段2催办
    dl2 = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
    eid2, _ = store.save_event(Record(kind="event", title="临期会", deadline=dl2))
    fired = asyncio.run(engine.fire_deadlines("u@im.wechat"))
    assert fired == 1 and store.fired(eid2, "deadline_soon")
    # 幂等：再跑不再推
    assert asyncio.run(engine.fire_deadlines("u@im.wechat")) == 0


def test_quiet_hours_block_nonurgent(tmp_path, monkeypatch):
    import asyncio

    from glimpsely.bot import build_pipeline
    from glimpsely.config import Config
    from glimpsely.triggers import _in_quiet_hour
    cfg = Config.load(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(cfg, "push_quiet_hour", 0)
    monkeypatch.setattr(cfg, "push_wake_hour", 23)  # 全天静默
    assert _in_quiet_hour(cfg)
    store, llm, pusher, engine = build_pipeline(cfg)
    cfg.db_path = tmp_path / "t301.db"
    cfg.media_dir = tmp_path
    store.push_state_upsert("u@im.wechat", "tok")
    assert asyncio.run(engine.fire_context_advice("u@im.wechat")) is False


def test_immediate_advice_daily_cap(tmp_path, monkeypatch):
    import asyncio

    from glimpsely.bot import build_pipeline
    from glimpsely.config import Config
    from glimpsely.understand import Record
    cfg = Config.load(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(cfg, "advice_daily_cap", 1)
    monkeypatch.setattr(cfg, "push_quiet_hour", 23)  # 关闭静默干扰
    cfg.db_path = tmp_path / "t314.db"
    cfg.media_dir = tmp_path
    store, llm, pusher, engine = build_pipeline(cfg)
    store.push_state_upsert("u@im.wechat", "tok")
    # 种子：囤积信号（近3天3张券）
    for i in range(3):
        store.save_event(Record(kind="coupon", title=f"优惠券{i}"))
    # 预算 1：第一次推成功，第二次被预算拦下
    ok1 = asyncio.run(engine.fire_immediate_advice("u@im.wechat"))
    ok2 = asyncio.run(engine.fire_immediate_advice("u@im.wechat"))
    assert ok1 is True and ok2 is False
    assert store.fired_today("context") == 1
