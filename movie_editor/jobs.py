"""One active operation, live progress, cancellation and faithful file delivery."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from telethon.errors import MessageNotModifiedError
from telethon.tl.types import DocumentAttributeVideo

from .config import Config
from .domain import Delivery, Draft, GifRange, Quality
from .media.gallery import FilterGallery
from .media.gif import GifExporter
from .media.renderer import Renderer
from .storage import Store
from .telegram.views import button

logger = logging.getLogger(__name__)


class Activity:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.label = ""

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, label: str, operation: Callable[[], Awaitable[None]]) -> None:
        if self.busy:
            raise ValueError("یک عملیات در حال اجرا است.")
        self.label = label
        self.task = asyncio.create_task(operation(), name=label)
        self.task.add_done_callback(self._consume)

    @staticmethod
    def _consume(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            error = task.exception()
            logger.error(
                "Background operation failed", exc_info=(type(error), error, error.__traceback__)
            )

    async def cancel(self) -> None:
        if self.busy:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


class JobService:
    def __init__(
        self,
        client,
        config: Config,
        store: Store,
        renderer: Renderer,
        gif_exporter: GifExporter | None = None,
        gallery: FilterGallery | None = None,
    ):
        self.client, self.config, self.store, self.renderer = client, config, store, renderer
        self.gif_exporter = gif_exporter or GifExporter(config)
        self.gallery = gallery or FilterGallery(config, store)

    async def run(
        self,
        draft: Draft,
        preview: bool,
        refresh: Callable[[], Awaitable[None]],
        *,
        gif: GifRange | None = None,
        comparison: bool = False,
    ) -> None:
        # Snapshot prevents a later settings change from mutating a running render.
        draft = Draft.from_dict(draft.to_dict())
        if comparison:
            mode, operation = "compare", "مقایسهٔ فیلترها"
        elif gif is not None:
            mode, operation = "gif", "تبدیل به GIF"
        else:
            mode, operation = ("preview" if preview else "render"), "رندر"
        folder = self.config.data_dir / "jobs" / draft.id / mode
        status = await self.client.send_message(
            self.config.admin_id,
            f"در حال آماده‌سازی {operation}…",
        )
        last_update = 0.0

        async def update(message: str, running: bool = True) -> None:
            try:
                await status.edit(
                    message, buttons=[[button("توقف عملیات", "g:stop")]] if running else None
                )
            except MessageNotModifiedError:
                pass

        async def render_progress(percent: float) -> None:
            nonlocal last_update
            if time.monotonic() - last_update >= 5:
                last_update = time.monotonic()
                await update(f"در حال {operation}: {percent:.0f}٪\nبرای توقف: /cancel")

        async def upload_progress(current: int, total: int) -> None:
            nonlocal last_update
            if time.monotonic() - last_update >= 5:
                last_update = time.monotonic()
                await update(f"در حال ارسال خروجی: {current / total * 100:.0f}٪")

        try:
            if comparison:
                async with asyncio.timeout(self.config.max_render_seconds):
                    result = await self.gallery.render(draft, folder, render_progress)
            elif gif is not None:
                result = await self.gif_exporter.convert(
                    Path(draft.source), gif, folder, render_progress
                )
            else:
                result = await self.renderer.render(draft, folder, render_progress, preview=preview)
            await update("رندر تمام شد؛ در حال ارسال…")
            thumb = await self.renderer.thumbnail(result.path, folder / "thumb.jpg")
            as_file = (
                comparison
                or gif is not None
                or result.path.suffix.lower() == ".mkv"
                or not preview
                and (
                    draft.settings.delivery == Delivery.FILE
                    or draft.settings.quality == Quality.LOSSLESS
                )
            )
            attributes = (
                []
                if as_file
                else [
                    DocumentAttributeVideo(
                        duration=result.info.duration,
                        w=result.info.width,
                        h=result.info.height,
                        supports_streaming=True,
                    )
                ]
            )
            caption = (
                "پیش‌نمایش کوتاه: حداکثر ۸ ثانیه از ویدیوی اصلی و ۳ ثانیه از ابتدا/انتها. "
                + result.notice
                if preview
                else "خروجی آماده است. " + result.notice
            )
            await self.client.send_file(
                self.config.admin_id,
                str(result.path),
                force_document=as_file,
                supports_streaming=not as_file,
                attributes=attributes,
                thumb=str(thumb),
                caption=caption,
                parse_mode=None,
                progress_callback=upload_progress,
                mime_type="image/gif" if gif is not None else None,
            )
            if not preview and not comparison:
                self.store.record(draft.original_name, "done", result.notice)
            await update(
                "GIF ارسال شد؛ پروژه برای ویرایش و تبدیل دوباره حفظ شده است."
                if gif is not None
                else "پیش‌نمایش ارسال شد."
                if preview
                else "خروجی ارسال شد؛ امکان تغییر و رندر دوباره دارید.",
                running=False,
            )
        except asyncio.CancelledError:
            await update("عملیات متوقف شد.", running=False)
            raise
        except TimeoutError:
            self.store.record(draft.original_name, "failed", "render timeout")
            await update("مهلت رندر تمام شد؛ برش کوتاه‌تر انتخاب کنید.", running=False)
        except ValueError as exc:
            self.store.record(draft.original_name, "failed", str(exc))
            await update(str(exc)[:1800], running=False)
        except Exception:
            logger.exception("Render or Telegram delivery failed for %s", draft.id)
            self.store.record(draft.original_name, "failed", "render or delivery error")
            await update(
                "رندر یا ارسال با خطا روبه‌رو شد. پروژه حفظ شده؛ دوباره تلاش کنید. "
                "جزئیات در لاگ سرور است.",
                running=False,
            )
        finally:
            # Keep tiny filter/log files for diagnostics; large outputs can be regenerated.
            for name in (
                "edited.mp4",
                "edited.mkv",
                "edited.gif",
                "text.png",
                "logo.png",
                "thumb.jpg",
                "look.cube",
                "comparison.png",
            ):
                (folder / name).unlink(missing_ok=True)
            for path in folder.glob("sample-*.png"):
                path.unlink(missing_ok=True)
        # The completion menu must be usable after Activity marks this task done.
        await refresh()
