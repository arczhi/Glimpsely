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
        now = datetime.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        items = self.store.events_between(day_start.isoformat(timespec="seconds"),
                                          now.isoformat(timespec="seconds"))
        profile = self.store.profile_all()
        recommend = compose_recommend(profile, items, self.llm)
        text = compose_digest_text(items, recommend, self.llm)
        result = await self.pusher.send(user_id, text)
        if result.ok:
            self.store.mark_fired(0, "daily_digest")
        return result.ok
