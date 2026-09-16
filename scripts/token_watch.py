"""Token stability long test: heartbeat push every N minutes, log -14 events.

Usage: .venv/bin/python scripts/token_watch.py --hours 48
Requires: real WeChat login once (`main.py run`) so push_state exists.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glimpsely.config import Config  # noqa: E402
from glimpsely.memory import MemoryStore  # noqa: E402
from glimpsely.push import Pusher  # noqa: E402

LOG = ROOT / "data/token_watch.jsonl"


def log(record: dict) -> None:
    record["ts"] = datetime.now().isoformat(timespec="seconds")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(record)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--interval-min", type=float, default=30.0)
    args = ap.parse_args()

    cfg = Config.load(ROOT)
    store = MemoryStore(cfg.db_path)

    rows = store.conn.execute(
        "SELECT user_id FROM push_state WHERE alive=1 "
        "ORDER BY last_seen_at DESC LIMIT 1").fetchall()
    if not rows:
        print("push_state 为空：请先真机扫码登录并互发一条消息（见 README runbook）")
        return 1
    user_id = rows[0]["user_id"]
    # 锚定真实登录 owner：防止测试/demo 污染行导致心跳发错人
    try:
        owner = json.loads((Path.home() / ".wechat_bot/current_user.json")
                           .read_text()).get("user_id")
        if owner and owner in [r["user_id"] for r in store.conn.execute(
                "SELECT user_id FROM push_state")]:
            user_id = owner
    except (OSError, json.JSONDecodeError):
        pass
    if "@" not in user_id or user_id.startswith(("u@", "demo_user")):
        print(f"疑似污染的 push_state（{user_id}），请真机互发一条消息后再挂长测")
        return 1

    log({"event": "watch_start", "user_id": user_id, "hours": args.hours})
    end = time.time() + args.hours * 3600
    first_dead = None
    beat = 0
    while time.time() < end:
        beat += 1
        # 每次 beat 全新 Bot/loop：避免 httpx 连接池绑定已关闭的 event loop
        from wechat_bot import Bot  # noqa: E402
        pusher = Pusher(cfg, store, bot=Bot())
        r = asyncio.run(pusher.send(user_id, f"💓 心跳 #{beat}（token 长测）"))
        rec = {"event": "heartbeat", "beat": beat, "ok": r.ok, "reason": r.reason}
        if not r.ok and "-14" in r.reason and first_dead is None:
            first_dead = datetime.now().isoformat(timespec="seconds")
            rec["first_dead_at"] = first_dead
            log({"event": "session_expired", "at": first_dead})
        log(rec)
        time.sleep(args.interval_min * 60)

    log({"event": "watch_end", "first_dead_at": first_dead,
         "total_beats": beat})
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
