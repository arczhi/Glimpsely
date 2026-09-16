"""Demo: full pipeline without WeChat — feeds sample messages, prints pushes."""

import asyncio
import json
from pathlib import Path

from .bot import build_pipeline, handle_update
from .config import Config

SAMPLES = [
    ("text", "今晚8点记得参加线上产品评审会，腾讯会议 428-7721"),
    ("image", "assets/courier_shot.png"),
    ("image", "assets/coupon_shot.png"),
    ("text", "帮我看下这个截图（重复上一次发的）"),
]


def demo_config(root: Path) -> Config:
    """Isolated config: demo NEVER touches production store."""
    cfg = Config.load(root)
    cfg.db_path = root / "data/demo.db"
    cfg.media_dir = root / "data/demo_media"
    return cfg


def run_demo(root: Path, repeat_last: bool = False) -> None:
    cfg = demo_config(root)
    cfg.ensure_dirs()
    store, llm, pusher, engine = build_pipeline(cfg)
    user_id = "demo_user@im.wechat"
    tok = "demo_context_token_0001"

    print("=" * 62)
    print("Glimpsely demo — 模拟微信转发流程（LLM 为本地 %s）" % cfg.omlx_model)
    print("=" * 62)

    last = None
    for kind, payload in SAMPLES:
        if kind == "text":
            text, image = payload, None
            print(f"\n📩 [文字转发] {text}")
        else:
            p = root / payload
            if not p.exists():
                print(f"  (跳过缺失样例图 {payload})")
                continue
            text, image = None, p
            print(f"\n📩 [图片转发] {p.name}")
        last = (text, image)
        reply = asyncio.run(handle_update(store, llm, pusher, engine,
                                          user_id, text, [image] if image else None, tok))
        print(f"🤖 bot回复: {reply}")

    if repeat_last and last:
        text, image = last
        print(f"\n📩 [再次转发] {'同一条文字' if text else '同一张图片'}")
        reply = asyncio.run(handle_update(store, llm, pusher, engine,
                                          user_id, text, [image] if image else None, tok))
        print(f"🤖 bot回复: {reply}")

    print("\n" + "-" * 62)
    print("🧠 记忆库内容：")
    for row in store.conn.execute("SELECT kind,title,deadline FROM events"):
        dl = f"  deadline={row['deadline']}" if row["deadline"] else ""
        print(f"  [{row['kind']}] {row['title']}{dl}")
    print("\n👤 画像：", json.dumps(store.profile_all(), ensure_ascii=False))

    # force-expire a fake deadline to show proactive push
    print("\n" + "-" * 62)
    print("⏰ 触发器演示（人为把一条事件 deadline 设为 30 分钟后）：")
    from datetime import datetime, timedelta
    soon = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
    for row in store.conn.execute("SELECT id FROM events WHERE deadline IS NOT NULL LIMIT 1"):
        store.conn.execute("UPDATE events SET deadline=? WHERE id=?",
                           (soon, row["id"]))
        store.conn.commit()
        asyncio.run(engine.fire_deadlines(user_id))

    print("\n🌙 日报演示（v3：一句概括 + 情境信号驱动的建议）：")
    from glimpsely.triggers import compose_daily_report
    events = store.active_events_recent(3)
    signals = store.situation_signals(3)
    text = compose_daily_report(events, signals, llm, 3)
    print("  " + text.replace("\n", "\n  "))

    store.close()
    print("\n✅ demo 完成（推送以终端打印模拟，真机见 README runbook）")
