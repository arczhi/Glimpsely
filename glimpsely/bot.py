"""iLink Bot entry: long-polling, handlers, scheduler."""

import asyncio
from datetime import datetime
from pathlib import Path

from wechat_bot import Bot as WeChatBot
from wechat_bot import Filter

from .asr import available as asr_available
from .asr import transcribe as asr_transcribe
from .asr import warmup as asr_warmup
from .config import Config
from .llm import OmlxClient
from .memory import MemoryStore
from .ocr import downscale_for_llm, full_ocr, warmup
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
                        text: str | None, image_paths, context_token: str | None,
                        ack: str = ACK_TEXT) -> str:
    """Skill-routed processing used by both real bot and demo.

    image_paths: 单条消息可能含多张图（列表），逐张记录；文本消息则路由语义技能。
    """
    from .understand import _fallback

    if user_id and context_token:
        store.push_state_upsert(user_id, context_token)

    paths = [Path(p) for p in image_paths] if image_paths else []

    # 图片消息：OCR 先行 → 原文喂给 LLM 理解（实体抽取有 ground truth）
    if paths:
        lines: list[str] = []
        for img in paths:
            ocr_text = await asyncio.to_thread(full_ocr, llm, img)
            ocr_rich = bool(ocr_text and len(ocr_text) >= 80)
            first_img = None if ocr_rich else downscale_for_llm(img)
            decision = await route(llm, text, first_img, store.history_text(user_id),
                                   ocr_text=ocr_text)
            # 逃生门：模型判断 OCR 零散文字不代表图片主题 → 带图重跑
            if ocr_rich and decision.needs_vision:
                decision = await route(llm, text, downscale_for_llm(img),
                                       store.history_text(user_id), ocr_text=ocr_text)
            rec = decision.record or _fallback(text, img)
            rec.media_path = img
            rec.ocr_text = ocr_text
            store.save_event(rec)
            store.update_profile_from_record(rec)
            note = (rec.memory_note or rec.title)[:40]
            line = f"· {note}" if note else ack
            if rec.degraded and rec.ocr_text:
                line = f"· [图片] {rec.ocr_text[:40]}…"
            lines.append(line)
        if len(lines) == 1:
            extra = "\n⏰ 我会在临近时提醒你" if rec.deadline else ""
            return f"{ack}  {lines[0][2:]}{extra}" if lines[0].startswith("· ") else f"{ack}  {lines[0]}{extra}"
        return f"已记下 {len(lines)} 条 ✓\n" + "\n".join(lines)

    # 纯文本消息：语义路由
    decision: Decision = await route(
        llm, text, None, store.history_text(user_id))

    if decision.skill == "record" and decision.record is not None:
        rec = decision.record
        store.save_event(rec)
        store.update_profile_from_record(rec)
        extra = "\n⏰ 我会在临近时提醒你" if rec.deadline else ""
        note = (rec.memory_note or rec.title)[:40]
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
            # 自然遗忘：软标记超 TTL 的记录（幂等，每小时跑一次）
            self.store.forget_old(self.cfg.memory_ttl_days)
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
        import logging
        bot = WeChatBot()
        if not bot.list_accounts():
            print("未发现已登录账号，进入扫码登录（二维码将打印在终端，480s 内有效）…")
            await bot.login()
            print("登录成功 ✓")
        n = self.store.backfill_embeddings()
        if n:
            print(f"向量索引回填：{n} 条存量记录")
        store, llm, engine = self.store, self.llm, self.engine
        media_dir = str(self.cfg.media_dir)
        sem = asyncio.Semaphore(3)  # 并发处理上限：连续多条消息同时理解
        tasks_set: set[asyncio.Task] = set()

        async def _download_image(ctx, item) -> Path | None:
            """逐张下载（ctx.download_media 只会取第一张）。"""
            from wechat_bot import DEFAULT_CDN_BASE_URL
            from wechat_bot.context import _resolve_media_from_item
            from wechat_bot.media.download import download_media_to_file
            media, aeskey_hex, fname = _resolve_media_from_item(item)
            if media is None:
                return None
            path = await download_media_to_file(
                media,
                getattr(ctx, "_cdn_base_url", None) or DEFAULT_CDN_BASE_URL,
                media_dir,
                filename=fname,
                aeskey_hex_override=aeskey_hex,
            )
            return Path(path) if path else None

        async def _process(ctx):
            # 立刻给"正在输入"反馈（感知延迟优化，失败不影响主流程）
            try:
                await ctx.send_typing()
            except Exception:
                logging.getLogger(__name__).debug("typing hint failed", exc_info=True)
            user_id = ctx.message.from_user_id
            tok = ctx.context_token
            text = None
            image_paths: list[Path] = []
            voice_paths: list[Path] = []
            if ctx.message.item_list:
                for it in ctx.message.item_list:
                    if it.type == 1 and it.text_item:
                        text = (text or "") + (it.text_item.text or "")
                    elif it.type == 2 and it.image_item:
                        try:
                            p = await _download_image(ctx, it)
                            if p:
                                image_paths.append(p)
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "download failed for msg %s item", ctx.message.message_id)
                    elif it.type == 3 and it.voice_item:
                        try:
                            p = await _download_image(ctx, it)
                            if p:
                                voice_paths.append(p)
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "voice download failed for msg %s", ctx.message.message_id)
            # 语音 → 本地 ASR → 文字（并入消息文本）
            for vp in voice_paths:
                if asr_available(self.cfg):
                    asr_text = await asyncio.to_thread(asr_transcribe, self.cfg, vp)
                    if asr_text:
                        text = (text or "") + (asr_text or "")
                        logging.getLogger(__name__).info("asr ok: %s", asr_text[:50])
                else:
                    logging.getLogger(__name__).warning("voice received but asr model missing")
            if text and text.strip() in ("/digest", "日报"):
                ok = await engine.fire_daily_digest(user_id)
                reply = "日报已发送 ✓" if ok else "日报发送失败：会话可能已过期，请随便发条消息恢复"
            elif text is None and not image_paths and not voice_paths:
                return  # 空更新/未支持类型，静默跳过，不打扰用户
            elif text is None and voice_paths and not image_paths:
                reply = "这条语音没听清，再说一遍？"
            else:
                store.add_chat_turn(user_id, "user",
                                    text or f"[图片×{len(image_paths)}]" or "[语音]")
                reply = await handle_update(store, llm, self.pusher, engine,
                                            user_id, text or None, image_paths, tok)
                store.add_chat_turn(user_id, "assistant", reply[:200])
            try:
                await ctx.reply(reply)
            except Exception:
                logging.getLogger(__name__).exception("reply failed")

        @bot.on_message(Filter.text() | Filter.image() | Filter.voice())
        async def _(ctx):
            async def _guarded():
                async with sem:
                    try:
                        await _process(ctx)
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "process failed for msg %s", ctx.message.message_id)
            t = asyncio.create_task(_guarded())
            tasks_set.add(t)
            t.add_done_callback(tasks_set.discard)
        self.pusher.bot = bot
        sched = asyncio.create_task(self._run_scheduler())
        warm = asyncio.create_task(asyncio.to_thread(warmup))
        warm2 = asyncio.create_task(asyncio.to_thread(asr_warmup, self.cfg))
        try:
            await bot.run_async()
        finally:
            sched.cancel()
            warm.cancel()
            warm2.cancel()


async def amain() -> None:
    cfg = Config.load()
    app = Glimpsely(cfg)
    await app.start()


if __name__ == "__main__":
    asyncio.run(amain())
