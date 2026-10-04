"""Composition root: configuration, repositories, services, then Telethon."""

import argparse
import asyncio
import logging
import os

from telethon import TelegramClient

from .assets import AssetService
from .config import Config
from .health import check
from .jobs import JobService
from .media.gallery import FilterGallery
from .media.gif import GifExporter
from .media.renderer import Renderer
from .storage import Store
from .telegram.controller import Controller


async def run(config: Config) -> None:
    config.prepare()
    store = Store(config.data_dir / "editor.sqlite3")
    client = TelegramClient(str(config.data_dir / "bot"), config.api_id, config.api_hash)
    client.parse_mode = None
    renderer = Renderer(config, store)
    controller = Controller(
        client,
        config,
        store,
        AssetService(config, store),
        JobService(
            client, config, store, renderer, GifExporter(config), FilterGallery(config, store)
        ),
    )
    controller.register()
    try:
        await client.start(bot_token=config.bot_token)
        me = await client.get_me()
        if not me.bot:
            raise ValueError(
                "این session متعلق به حساب کاربری است؛ "
                "برای ربات از پوشهٔ DATA_DIR جدید استفاده کنید."
            )
        logging.getLogger(__name__).info(
            "Bot ready; only configured admin in private chat is accepted"
        )
        await client.run_until_disconnected()
    finally:
        await controller.activity.cancel()
        await client.disconnect()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Telethon movie editor")
    parser.add_argument(
        "--check", action="store_true", help="Check local media tools without Telegram login"
    )
    args = parser.parse_args()
    try:
        config = Config.from_env(require_telegram=not args.check)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(check(config) if args.check else run(config))
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        pass
