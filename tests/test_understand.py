"""Unit tests: understanding pipeline via real local omlx (marked slow)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from glimpsely.config import Config
from glimpsely.llm import OmlxClient
from glimpsely.understand import understand_message

ROOT = Path(__file__).resolve().parents[1]


def omlx_available() -> bool:
    try:
        cfg = Config.load(ROOT)
        OmlxClient(cfg).chat("回复OK两个字母即可", max_tokens=10)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not omlx_available(),
                                reason="omlx server not reachable")


def test_courier_screenshot():
    cfg = Config.load(ROOT)
    client = OmlxClient(cfg)
    rec = understand_message(client, None, ROOT / "assets/courier_shot.png")
    assert not rec.degraded
    assert rec.kind in ("courier", "other")
    joined = " ".join(rec.entities.values()) + rec.title
    assert "SF" in joined or "顺丰" in joined


def test_coupon_screenshot():
    cfg = Config.load(ROOT)
    client = OmlxClient(cfg)
    rec = understand_message(client, None, ROOT / "assets/coupon_shot.png")
    assert not rec.degraded
    assert rec.kind == "coupon"
    assert rec.deadline is not None and "2026" in rec.deadline


def test_text_event():
    cfg = Config.load(ROOT)
    client = OmlxClient(cfg)
    rec = understand_message(
        client, "今晚8点参加线上产品评审会，腾讯会议 428-7721", None)
    assert not rec.degraded
    assert rec.kind == "event"
    assert rec.deadline is not None


def test_garbage_degrades():
    cfg = Config.load(ROOT)
    cfg.omlx_base_url = "http://127.0.0.1:1"  # 不可达端口
    client = OmlxClient(cfg)
    rec = understand_message(client, "随便说点什么", None)
    assert rec.degraded and rec.kind == "note"


def _route_once(text: str):
    import asyncio

    from glimpsely.skills import route
    cfg = Config.load(ROOT)
    client = OmlxClient(cfg)
    return asyncio.run(route(client, text, None, ""))


def test_route_semantic_query():
    d = _route_once("帮我看看最近都记了些什么？")
    assert d.skill == "query"


def test_route_semantic_record():
    d = _route_once("记一下：明天下午3点去看牙医")
    assert d.skill == "record"
    assert d.record is not None and not d.record.degraded
    assert d.record.kind == "event"
    assert d.record.deadline is not None


def test_route_semantic_chat():
    d = _route_once("苹果和橙子哪个维生素C含量更高？")
    assert d.skill == "chat"
    assert d.reply_hint != ""


def test_receipt_ocr_full_text():
    from glimpsely.ocr import full_ocr
    cfg = Config.load(ROOT)
    client = OmlxClient(cfg)
    ocr = full_ocr(client, ROOT / "assets/receipt_shot.png")
    assert ocr is not None
    assert "20260915-8823" in ocr or "8823" in ocr
    assert "宫保鸡丁" in ocr
    assert "62" in ocr


def test_paddle_ocr_deterministic():
    from glimpsely.ocr import ocr_image_text
    t1 = ocr_image_text(ROOT / "assets/receipt_shot.png")
    t2 = ocr_image_text(ROOT / "assets/receipt_shot.png")
    assert t1 == t2  # 确定性：同图两次结果一致
    assert t1 and "订单" in t1
