"""CLI: run (real WeChat) / demo / test-llm / scan."""

import argparse
import asyncio
import sys
from pathlib import Path

from .config import Config
from .llm import OmlxClient


def cmd_test_llm(cfg: Config) -> int:
    client = OmlxClient(cfg)
    img = Path("assets/courier_shot.png")
    if not img.exists():
        print(f"missing {img}")
        return 1
    print("调用本地模型识别样例快递截图…")
    raw = client.understand(None, img)
    print("模型原始输出：\n", raw)
    from .llm import extract_json
    data = extract_json(raw)
    if data is None:
        print("\n❌ 未解析出 JSON")
        return 1
    print("\n✅ 解析结果：")
    print({k: data.get(k) for k in ("kind", "title", "entities", "deadline")})
    return 0


def cmd_login() -> int:
    from wechat_bot import Bot
    bot = Bot()

    async def _login():
        result = await bot.login()
        print(f"登录成功 ✓ account={result.account_id} user={result.user_id}")
        await bot.stop()

    print("即将显示登录二维码（用微信扫码并在手机上确认，480s 内有效）…")
    asyncio.run(_login())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="glimpsely")
    ap.add_argument("command", choices=["run", "login", "demo", "test-llm", "scan"])
    ap.add_argument("--repeat", action="store_true",
                    help="demo: 重复最后一条消息验证去重")
    args = ap.parse_args()
    cfg = Config.load()
    cfg.ensure_dirs()

    if args.command == "login":
        return cmd_login()
    if args.command == "run":
        import logging
        log_file = Path("data/run.log")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
        )
        from .bot import Glimpsely
        app = Glimpsely(cfg)
        print("Glimpsely 启动中…（已检测到登录凭据；如需换号: main.py login）")
        asyncio.run(app.start())
        return 0
    if args.command == "demo":
        from .demo import run_demo
        run_demo(Path.cwd(), repeat_last=args.repeat)
        return 0
    if args.command == "test-llm":
        return cmd_test_llm(cfg)
    if args.command == "scan":
        import wechat_bot
        print("wechat-ilink-bot version:", getattr(wechat_bot, "__version__", "0.1.0"))
        print("module path:", wechat_bot.__file__)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
