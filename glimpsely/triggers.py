"""Trigger engine: deadline / courier / daily digest / context rules."""

from datetime import datetime, timedelta

from .config import Config
from .llm import OmlxClient
from .memory import MemoryStore
from .push import Pusher

FALLBACK_DEADLINE_TMPL = "⏰ 提醒：{title}（{deadline}）"
FALLBACK_DIGEST_TMPL = "🌙 今天的记忆：\n{items}\n{recommend}"


def _in_quiet_hour(cfg) -> bool:
    """静默时段（默认 22:00-08:00）：非紧急建议不推。相同值=无静默。"""
    if cfg.push_quiet_hour == cfg.push_wake_hour:
        return False
    h = datetime.now().hour
    return h >= cfg.push_quiet_hour or h < cfg.push_wake_hour


def _advice_budget_left(cfg, store) -> bool:
    """当日情境建议总预算（含 12 点定时与即时推送）。"""
    return store.fired_today("context") < cfg.advice_daily_cap


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


def pick_advice_signals(signals: dict) -> list[dict]:
    """按优先级挑选 1-3 条建议信号，每条带具体锚点（禁止无事实的建议）。"""
    out: list[dict] = []
    for e in signals.get("expiring", [])[:1]:
        ents = ""
        try:
            import json as _json
            d = _json.loads(e.get("entities") or "{}")
            ents = ", ".join(f"{k}={v}" for k, v in list(d.items())[:2])
        except Exception:  # noqa: BLE001
            pass
        out.append({"type": "deadline",
                    "anchor": f"{e['title']}（还剩{e['days_left']:.0f}天, {ents}）"})
    for c in signals.get("couriers", [])[:1]:
        out.append({"type": "courier",
                    "anchor": f"{c['title']}（{c['ts'][:16]}记的）"})
    for s in signals.get("streaks", [])[:1]:
        kind_cn = {"note": "随手记", "event": "日程", "coupon": "券",
                   "courier": "快递", "bill": "账单"}.get(s["kind"], s["kind"])
        out.append({"type": "streak",
                    "anchor": f"{kind_cn}连着{s['days']}天了"})
    if signals.get("coupon_count", 0) >= 3:
        out.append({"type": "coupon_pile",
                    "anchor": f"近3天囤了{signals['coupon_count']}张券"})
    brands = signals.get("top_brands") or []
    if brands and brands[0][1] >= 2:
        out.append({"type": "brand",
                    "anchor": f"你最近常记「{brands[0][0]}」相关的内容"})
    return out[:3]


def compose_advice(signals: dict, client: OmlxClient | None,
                   max_items: int = 3) -> str:
    """情境信号 → 贴心建议（锚点必引，杜绝泛泛而谈）。"""
    picked = pick_advice_signals(signals)
    if not picked:
        return "这几天没有需要惦记的事～随手转发点什么给我记着吧。"
    anchors = "；".join(p["anchor"] for p in picked)
    if client is None:
        tmpl = {
            "deadline": "⏳ {a}——快到期了，安排上别拖",
            "courier": "📦 {a}——记得去取，别过保管期",
            "streak": "🔥 {a}，继续保持",
            "coupon_pile": "🎟 {a}，周末清一清哪些还没用",
        }
        lines = []
        for p in picked:
            lines.append(tmpl.get(p["type"], "· {a}").format(a=p["anchor"]))
        return "\n".join(lines)
    prompt = (
        "你是用户的老朋友，基于以下【已核实的记忆信号】写建议，铁律：\n"
        "1. 每条建议必须引用对应信号里的具体细节（券名/剩余天数/取件信息/连续天数），"
        "禁止写「注意休息」「保持健康」这类无锚点的话\n"
        "2. 2-3条，每条<=40字，像随口聊，不罗列不编号\n"
        "3. 直接输出建议正文，不要标题不要解释\n\n"
        f"记忆信号：{anchors}\n"
        f"当前时段：{signals.get('hour', 20)}点"
    )
    try:
        out = (client.chat(prompt, max_tokens=250, temperature=0.7) or "").strip()
        return out[:300] if out else "💡 今天也辛苦了。"
    except Exception:  # noqa: BLE001
        return "💡 今天也辛苦了。"


def compose_daily_report(events: list[dict], signals: dict,
                         client: OmlxClient | None, ttl_days: int = 3) -> str:
    """日报 v3：一句概括 + 情境信号驱动的贴心建议（不再流水账）。"""
    if not events:
        return "🌙 记忆这几天很清净～有新鲜事随手转发给我，我帮你记着。"
    advice = compose_advice(signals, client)
    if client is None:
        titles = "、".join((ev.get("title") or ev.get("kind") or "")[:14]
                           for ev in events[-3:])
        return f"🌙 近{ttl_days}天记了 {len(events)} 件：{titles}…\n{advice}"
    titles_block = "；".join(
        f"{ev.get('title') or ev.get('kind')}" for ev in events[-6:])
    prompt = (
        "你是用户的老朋友。用一句话（<=25字）概括用户最近记下的这些内容，"
        "自然口语，不要列表：\n"
        f"{titles_block}"
    )
    try:
        opening = (client.chat(prompt, max_tokens=80, temperature=0.5) or "").strip()
        opening = opening[:40] if opening else "🌙 近几天记了些东西。"
    except Exception:  # noqa: BLE001
        opening = "🌙 近几天记了些东西。"
    return f"{opening}\n\n{advice}"


class TriggerEngine:
    def __init__(self, cfg: Config, store: MemoryStore,
                 pusher: Pusher, llm: OmlxClient | None):
        self.cfg = cfg
        self.store = store
        self.pusher = pusher
        self.llm = llm

    async def fire_deadlines(self, user_id: str) -> int:
        """两阶段截止提醒：~24h 首推（尊重静默）+ <1h 催办（全天候）。"""
        fired = 0
        now = datetime.now()
        # 阶段1：提前首推（>1h 且 <=26h）
        for ev in self.store.pending_deadlines(26.0, "deadline"):
            try:
                dt = datetime.fromisoformat(ev["deadline"])
            except (ValueError, TypeError):
                continue
            if (dt - now) <= timedelta(hours=1):
                continue
            if _in_quiet_hour(self.cfg):
                continue
            text = compose_deadline_text(ev, self.llm)
            result = await self.pusher.send(user_id, text)
            if result.ok:
                self.store.mark_fired(int(ev["id"]), "deadline")
                fired += 1
        # 阶段2：<1h 催办（紧急，静默时段也推）
        for ev in self.store.pending_deadlines(1.05, "deadline_soon"):
            text = compose_deadline_text(ev, self.llm)
            result = await self.pusher.send(user_id, text)
            if result.ok:
                self.store.mark_fired(int(ev["id"]), "deadline_soon")
                fired += 1
        return fired

    async def fire_context_advice(self, user_id: str) -> bool:
        """定时的情境建议（默认 12 点后）：无锚点信号则不打扰。"""
        if _in_quiet_hour(self.cfg):
            return False
        if not _advice_budget_left(self.cfg, self.store):
            return False
        signals = self.store.situation_signals(self.cfg.memory_ttl_days)
        if not pick_advice_signals(signals):
            return False  # 没有值得推的信号 → 保持安静
        text = compose_advice(signals, self.llm)
        result = await self.pusher.send(user_id, text)
        if result.ok:
            self.store.mark_fired(0, "context")
        return result.ok

    async def fire_immediate_advice(self, user_id: str) -> bool:
        """入库即推：新记录让情境信号成立的当下就提醒（事件驱动时机）。"""
        if _in_quiet_hour(self.cfg):
            return False
        if not _advice_budget_left(self.cfg, self.store):
            return False
        signals = self.store.situation_signals(self.cfg.memory_ttl_days)
        picked = pick_advice_signals(signals)
        if not picked:
            return False
        text = compose_advice(signals, self.llm)
        result = await self.pusher.send(user_id, text)
        if result.ok:
            self.store.mark_fired(0, "context")
        return result.ok
