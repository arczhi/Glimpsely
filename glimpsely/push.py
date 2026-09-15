"""Push layer: context_token management + proactive send with rate limits."""

from datetime import datetime

from .config import Config
from .memory import MemoryStore


class PushResult:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason


class Pusher:
    def __init__(self, cfg: Config, store: MemoryStore, bot=None):
        self.cfg = cfg
        self.store = store
        self.bot = bot  # wechat_bot.Bot or None (demo mode prints)

    async def send(self, user_id: str, text: str) -> PushResult:
        st = self.store.push_state_get(user_id)
        if st is None:
            return PushResult(False, "no push_state yet (user never messaged)")
        if not st.get("alive"):
            return PushResult(False, "session degraded (-14), waiting for user")

        hour_count, day_count = self.store.push_counts(user_id)
        if hour_count >= self.cfg.push_limit_per_hour:
            return PushResult(False, "hourly push limit reached")
        if day_count >= self.cfg.push_daily_cap:
            return PushResult(False, "daily push cap reached")

        if self.bot is None:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[推送→{user_id[:18]}…] {ts}  {text}\n")
            self.store.push_record(user_id, True)
            return PushResult(True, "demo-printed")

        try:
            await self.bot.send_text(to=user_id, text=text)
            self.store.push_record(user_id, True)
            return PushResult(True, "sent")
        except Exception as e:  # noqa: BLE001 - SDK raises typed errors
            msg = str(e)
            if "-14" in msg or "session" in msg.lower():
                self.store.push_mark_dead(user_id)
                return PushResult(False, "session expired (-14), marked dead")
            return PushResult(False, f"send failed: {msg}")
