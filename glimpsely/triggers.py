"""Trigger engine: deadline / courier / daily digest / context rules."""

from datetime import datetime, timedelta

from .config import Config
from .llm import OmlxClient
from .memory import MemoryStore
from .push import Pusher

FALLBACK_DEADLINE_TMPL = "⏰ 提醒：{title}（{deadline}）"
FALLBACK_DIGEST_TMPL = "🌙 今天的记忆：\n{items}\n{recommend}"


def _pick_user_time_hint(event: dict) -> str:
    dl = event.get("deadline") or ""
    try:
        dt = datetime.fromisoformat(dl)
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return dl[:16]


def compose_deadline_text(event: dict, client: OmlxClient | None) -> str:
    base = FALLBACK_DEADLINE_TMPL.format(
        title=event.get("title") or event.get("kind", ""),
        deadline=_pick_user_time_hint(event))
    if client is None:
        return base
    entities = event.get("entities_json") or "{}"
    try:
        ents = ", ".join(f"{k}={v}" for k, v in
                         (eval(entities) if isinstance(entities, str) else entities).items())  # noqa: S307
    except Exception:  # noqa: BLE001
        ents = ""
    prompt = (
        f"把下面这条提醒写成一句<=50字、有温度的中文推送，不要解释：\n"
        f"事件：{event.get('title')}\n时间：{_pick_user_time_hint(event)}\n"
        f"关键信息：{ents}"
    )
    try:
        out = (client.chat(prompt, max_tokens=120, temperature=0.5) or "").strip()
        return out[:120] if out else base
    except Exception:  # noqa: BLE001
        return base


def compose_digest_text(items: list[dict], recommend: str,
                        client: OmlxClient | None) -> str:
    lines = [f"· {it.get('title') or it.get('kind')}" for it in items]
    if not lines:
        lines = ["今天很清净，没有新记录 🙂"]
    body = "\n".join(lines[:8])
    return f"🌙 今日记忆\n{body}\n\n{recommend}"


def compose_recommend(profile: dict, recent: list[dict],
                      client: OmlxClient | None) -> str:
    kinds = [it.get("kind") for it in recent]
    facts = {k: v for k, v in profile.items() if not k.startswith("count_")}
    if client is None:
        if "coupon" in kinds and kinds.count("coupon") >= 2:
            return "💡 你最近收藏了几张券，记得在过期前用掉哦。"
        return "💡 今天辛苦了，早点休息。"
    prompt = (
        "你是贴心生活助理。根据用户画像和最近记录，写一条<=40字的主动推荐或关心，"
        "不要解释不要列表：\n"
        f"画像：{facts or '暂无'}\n"
        f"最近记录类型：{kinds}\n"
        f"最近标题：{[it.get('title') for it in recent][:5]}"
    )
    try:
        out = (client.chat(prompt, max_tokens=100, temperature=0.7) or "").strip()
        return out[:80] if out else "💡 今天辛苦了，早点休息。"
    except Exception:  # noqa: BLE001
        return "💡 今天辛苦了，早点休息。"


def compose_daily_report(events: list[dict], profile: dict,
                         client: OmlxClient | None, ttl_days: int = 3) -> str:
    """日报 v2：基于最近全部内容（含 OCR 原文）生成贴心建议/灵感启发。"""
    if not events:
        return "🌙 记忆这几天很清净～有新鲜事随手转发给我，我帮你记着。"
    lines = []
    for ev in events[-10:]:
        dl = f" 截止:{ev['deadline'][:16]}" if ev.get("deadline") else ""
        lines.append(f"- [{ev['kind']}] {ev['title']}{dl}")
        ocr = (ev.get("ocr_text") or "").strip()
        if ocr:
            lines.append(f"  原文摘录: {ocr[:250]}")
    memory_block = "\n".join(lines)
    facts = {k: v for k, v in profile.items() if not k.startswith("count_")}
    if client is None:
        items = "\n".join(f"· {ev.get('title') or ev.get('kind')}" for ev in events[-8:])
        return f"🌙 最近记忆\n{items}\n\n💡 收藏的券记得过期前用掉，有快递还没取就早点去拿。"
    prompt = (
        f"你是用户的朋友兼生活助理，写一份<=160字的中文晚间简报，"
        "基于以下最近几天的记忆（含截图原文）。要求：\n"
        "1. 第一句一句带过这段时间记了什么，不要流水账罗列\n"
        "2. 给出2-3条贴心建议或灵感启发，必须引用具体细节"
        "（比如快过期的券、没取的快递、重复出现的习惯），像朋友随口聊\n"
        "3. 不要列表符号，不要标题，直接输出正文\n\n"
        f"最近记忆（近{ttl_days}天，含OCR原文）：\n{memory_block}\n\n"
        f"用户画像：{facts or '暂无'}"
    )
    try:
        out = (client.chat(prompt, max_tokens=400, temperature=0.6) or "").strip()
        return out[:400] if out else "🌙 今天辛苦了。"
    except Exception:  # noqa: BLE001
        items = "\n".join(f"· {ev.get('title')}" for ev in events[-8:])
        return f"🌙 最近记忆\n{items}\n\n💡 记得处理即将到期的事项。"


class TriggerEngine:
    def __init__(self, cfg: Config, store: MemoryStore,
                 pusher: Pusher, llm: OmlxClient | None):
        self.cfg = cfg
        self.store = store
        self.pusher = pusher
        self.llm = llm

    async def fire_deadlines(self, user_id: str) -> int:
        pending = self.store.pending_deadlines(horizon_hours=26.0)
        fired = 0
        for ev in pending:
            dl = ev.get("deadline") or ""
            soon = False
            try:
                dt = datetime.fromisoformat(dl)
                soon = dt - datetime.now() <= timedelta(hours=1)
            except (ValueError, TypeError):
                pass
            if not soon:
                continue  # 只在<1h时触发，避免提前一天打扰
            text = compose_deadline_text(ev, self.llm)
            result = await self.pusher.send(user_id, text)
            if result.ok:
                self.store.mark_fired(int(ev["id"]), "deadline")
                fired += 1
        return fired

    async def fire_daily_digest(self, user_id: str) -> bool:
        events = self.store.active_events_recent(self.cfg.memory_ttl_days)
        profile = self.store.profile_all()
        text = compose_daily_report(
            events, profile, self.llm, self.cfg.memory_ttl_days)
        result = await self.pusher.send(user_id, text)
        if result.ok:
            self.store.mark_fired(0, "daily_digest")
        return result.ok
