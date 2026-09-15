"""iLink Bot entry: long-polling, handlers, scheduler."""

import asyncio
from datetime import datetime
from pathlib import Path

from wechat_bot import Bot as WeChatBot
from wechat_bot import Filter

from .config import Config
from .llm import OmlxClient
from .memory import MemoryStore
from .push import Pusher
from .skills import Decision, answer_query, route
from .triggers import TriggerEngine

ACK_TEXT = "已记下 ✓"


def build_pipeline(cfg: Config):
    store = MemoryStore(cfg.db_path)
    llm = OmlxClient(cfg)
    pusher = Pusher(cfg, store, bot=None)
    engine = TriggerEngine(cfg, store, pusher, llm)
    return store, llm, pusher, engine


async def handle_update(store: MemoryStore, llm: OmlxClient, pusher: Pusher,
                        engine: TriggerEngine, user_id: str,
                        text: str | None, image_path: Path | None,
                        context_token: str | None,
                        ack: str = ACK_TEXT) -> str:
    """Skill-routed processing used by both real bot and demo."""
    if user_id and context_token:
        store.push_state_upsert(user_id, context_token)

    decision: Decision = await route(
        llm, text, image_path, store.history_text(user_id))

    if decision.skill == "record" and decision.record is not None:
        rec = decision.record
        store.save_event(rec)
        store.update_profile_from_record(rec)
        extra = "\n⏰ 我会在临近时提醒你" if rec.deadline else ""
        note = rec.memory_note[:40]
        return f"{ack}  {note}{extra}" if note else ack

    if decision.skill == "query":
        reply = await answer_query(llm, text or "[图片]", store, user_id)
        return reply

    if decision.skill == "digest":
        ok = await engine.fire_daily_digest(user_id)
        return "日报已发送 ✓" if ok else "日报发送失败：会话可能已过期，请随便发条消息恢复"

    if decision.skill == "clear":
        store.conn.execute("DELETE FROM events")
        store.conn.execute("DELETE FROM dedupe")
        store.conn.execute("DELETE FROM trigger_log")
        store.conn.execute("DELETE FROM chat_log WHERE user_id=?", (user_id,))
        store.conn.commit()
        return "记忆已清空 🧹"

    # chat
    reply = decision.reply_hint or "嗯嗯，我在呢～ 随手转发点什么给我记着吧。"
    return reply[:300]


class Glimpsely:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.store, self.llm, self.pusher, self.engine = build_pipeline(cfg)

    async def _run_scheduler(self) -> None:
        while True:
            await asyncio.sleep(60)
            state_owner = None
            for row in self.store.conn.execute(
                    "SELECT user_id FROM push_state").fetchall():
                state_owner = row["user_id"]
            if not state_owner:
                continue
            st = self.store.push_state_get(state_owner) or {}
            if not st.get("alive"):
                continue
            now = datetime.now()
            if now.hour == self.cfg.digest_hour and now.minute < 2:
                if not self.store.fired(0, "daily_digest"):
                    await self.engine.fire_daily_digest(state_owner)
            await self.engine.fire_deadlines(state_owner)

    async def start(self) -> None:
        bot = WeChatBot()
        if not bot.list_accounts():
            print("未发现已登录账号，进入扫码登录（二维码将打印在终端，480s 内有效）…")
            await bot.login()
            print("登录成功 ✓")
        store, llm, engine = self.store, self.llm, self.engine
        media_dir = str(self.cfg.media_dir)

        @bot.on_message(Filter.text() | Filter.image())
        async def _(ctx):
            import logging
            user_id = ctx.message.from_user_id
            tok = ctx.context_token
            text = None
            image_path = None
            if ctx.message.item_list:
                for it in ctx.message.item_list:
                    if it.type == 1 and it.text_item:
                        text = (text or "") + (it.text_item.text or "")
                    elif it.type == 2 and it.image_item:
                        try:
                            path = await ctx.download_media(media_dir)
                            image_path = Path(path) if path else None
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "download_media failed for msg %s", ctx.message.message_id)
            if text and text.strip() in ("/digest", "日报"):
                ok = await engine.fire_daily_digest(user_id)
                reply = "日报已发送 ✓" if ok else "日报发送失败：会话可能已过期，请随便发条消息恢复"
            elif text is None and image_path is None:
                return  # 空更新/未支持类型，静默跳过，不打扰用户
            else:
                store.add_chat_turn(user_id, "user", text or "[图片]")
                reply = await handle_update(store, llm, self.pusher, engine,
                                            user_id, text or None, image_path, tok)
                store.add_chat_turn(user_id, "assistant", reply[:200])
            try:
                await ctx.reply(reply)
            except Exception:
                logging.getLogger(__name__).exception("reply failed")

        self.pusher.bot = bot
        sched = asyncio.create_task(self._run_scheduler())
        try:
            await bot.run_async()
        finally:
            sched.cancel()


async def amain() -> None:
    cfg = Config.load()
    app = Glimpsely(cfg)
    await app.start()


if __name__ == "__main__":
    asyncio.run(amain())
