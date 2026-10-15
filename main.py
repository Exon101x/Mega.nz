"""Entry point:  python main.py"""
import asyncio
import logging

import config
from app import App


async def main() -> None:
    cfg = config.load()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    app = App(cfg)
    await app.start()
    me = await app.bot.get_me()
    logging.getLogger("main").info("Bot @%s is running (%d worker%s)", me.username, cfg.max_concurrent,
                                   "" if cfg.max_concurrent == 1 else "s")
    await app.bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
