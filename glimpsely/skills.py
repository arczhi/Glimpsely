"""Semantic skill router: LLM-driven intents replace literal commands."""

import asyncio
from dataclasses import dataclass

from .llm import OmlxClient, extract_decision
from .understand import Record, _coerce, _fallback

SKILLS = ("record", "query", "digest", "clear", "chat")

ROUTER_PROMPT = (
    "你是 Glimpsely，用户的私人记忆助理。用户会随手转发消息/截图，也可能和你正常对话。\n"
    "根据消息语义选择一个 skill，输出只允许一个 JSON 对象：不要解释、不要英文、"
    "不要思考过程，第一个字符必须是 { 。\n"
    "skill 定义：\n"
    "- record：用户想让我记住某条信息（转发截图、快递/账单/优惠券/日程/地址/聊天记录/任何要保存的内容）\n"
    "- query：用户想查询或回顾已记的内容（如：最近记了什么、我的快递单号是多少、那天开的会）\n"
    "- digest：用户想要今日汇总/日报（如：给我发日报、总结一下今天记的）\n"
    "- clear：用户明确要求清空所有记忆（如：忘掉全部记录）\n"
    "- chat：普通聊天或知识提问，与记忆无关\n"
    '输出格式：\n'
    '{"skill":"record|query|digest|clear|chat",\n'
    ' "reply":"仅chat时：一句自然的对话回复(<=80字)",\n'
    ' "record":仅record时：{"kind":"courier|bill|coupon|event|address|person|chat_digest|note|other",'
    '"title":"<=30字摘要","entities":{"键":"值"},"deadline":"ISO8601或null(相对时间以当前时间折算)",'
    '"importance":1到5,"user_intent":"<=25字","memory_note":"<=40字"}}\n'
)

GENERIC_FALLBACK_REPLY = "（本地模型这会儿没响应，稍后再试试）"


@dataclass
class Decision:
    skill: str = "chat"
    record: Record | None = None
    reply_hint: str = ""


def _clock() -> str:
    from datetime import datetime
    weekday = "一二三四五六日"[datetime.now().weekday()]
    return f"当前时间：{datetime.now().isoformat(timespec='minutes')}，星期{weekday}。"


async def route(client: OmlxClient, text: str | None,
                image_path, history_text: str = "") -> Decision:
    """One LLM call decides skill + optional record + chat reply hint."""
    from pathlib import Path
    image_path = Path(image_path) if image_path else None
    parts = [history_text] if history_text else []
    if text:
        parts.append(f"用户消息：{text}")
    prompt = _clock() + ROUTER_PROMPT + ("\n".join(parts) + "\n" if parts else "")
    try:
        raw = await asyncio.to_thread(
            client.chat, prompt, image_path, 800, None, None)
        data = extract_decision(raw)
        if data and data.get("skill") in SKILLS:
            skill = data["skill"]
            rec_data = data.get("record")
            rec = None
            if isinstance(rec_data, dict) and rec_data:
                rec = _coerce(rec_data, text, image_path)
            elif "kind" in data:  # 模型把 record 字段平铺在顶层
                rec = _coerce(data, text, image_path)
            if skill == "record" and rec is None:
                rec = _fallback(text, image_path)
            return Decision(skill, rec, str(data.get("reply") or "")[:300])
    except Exception:  # noqa: BLE001 — 网络错/超时/解析错统一降级
        pass
    if image_path is not None:
        return Decision("record", _fallback(text, image_path))
    return Decision("chat", None, GENERIC_FALLBACK_REPLY)


def _format_events(rows: list[dict], limit: int = 12,
                   with_ocr: bool = False) -> str:
    lines = []
    for r in rows[-limit:]:
        dl = f" 截止:{r['deadline']}" if r.get("deadline") else ""
        line = f"- [{r['kind']}] {r['title']} ({r['ts'][:16]}){dl}"
        ocr = (r.get("ocr_text") or "").strip()
        if with_ocr and ocr:
            line += f"\n  原文: {ocr[:400]}"
        lines.append(line)
    return "\n".join(lines) if lines else "（记忆库为空）"


async def answer_query(client: OmlxClient, question: str,
                       store, user_id: str) -> str:
    """向量检索（含遗忘记录）→ 自动唤醒 → LLM 基于原文回答。"""
    matches = store.semantic_search(question, k=8)
    upcoming = store.upcoming_events_for_digest()
    awoken = 0
    for m in matches:
        if m.get("forgotten"):
            store.reactivate(int(m["id"]))
            awoken += 1
    block_lines = []
    for m in matches[:8]:
        dl = f" 截止:{m['deadline'][:16]}" if m.get("deadline") else ""
        state = "（这条已从遗忘中唤醒）" if m.get("forgotten") else ""
        block_lines.append(
            f"- [{m['kind']}] {m['title']} ({m['ts'][:16]}){dl}{state}")
        ocr = (m.get("ocr_text") or "").strip()
        if ocr:
            block_lines.append(f"  原文: {ocr[:400]}")
    memory_block = "\n".join(block_lines) or "（没找到相关记忆）"
    upcoming_block = _format_events(upcoming)
    prompt = (
        f"{_clock()}\n"
        f"从用户记忆库里检索到的相关记录（含截图原文）：\n{memory_block}\n"
        f"（即将到期）\n{upcoming_block}\n\n"
        f"用户问题：{question}\n"
        "基于检索到的记录回答（可引用原文细节，如订单号、金额、菜名），"
        "<=120字，直接回答不要解释；没检索到就说记不起来。"
    )
    try:
        out = await asyncio.to_thread(client.chat, prompt, None, 350, 0.3, None)
        reply = (out or "").strip()
    except Exception:  # noqa: BLE001
        reply = memory_block[:200]
    if not reply:
        reply = "记库里没找到相关内容。"
    if awoken:
        reply += f"\n（已从遗忘中唤醒 {awoken} 条相关记忆）"
    return reply[:400]
