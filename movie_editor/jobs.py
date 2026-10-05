"""One active operation, live progress, cancellation and faithful file delivery."""

import asyncio
import json
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
from .media.probe import inspect
from .media.renderer import Renderer
from .media.result import RenderResult
from .storage import Store
from .telegram.delivery import FileDelivery
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
        self.delivery = FileDelivery(client, config)

    def pending(self, draft: Draft) -> dict | None:
        folder = self.config.data_dir / "jobs" / draft.id / "render"
        try:
            data = json.loads((folder / "pending.json").read_text(encoding="utf-8"))
            if data["path"] not in {"edited.mkv", "edited.mp4"}:
                return None
            if data["draft"]["id"] != draft.id or not (folder / data["path"]).is_file():
                return None
            if not isinstance(data["notice"], str):
                return None
            Draft.from_dict(data["draft"]).validate()
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def run(
        self,
        draft: Draft,
        preview: bool,
        refresh: Callable[[], Awaitable[None]],
        *,
        gif: GifRange | None = None,
        comparison: bool = False,
        retry: bool = False,
    ) -> None:
        if retry and (preview or comparison or gif is not None):
            raise ValueError("ارسال دوباره فقط برای خروجی ویدیوی نهایی است.")
        pending = self.pending(draft) if retry else None
        if retry:
            if pending is None:
                raise ValueError("خروجی آماده‌ای برای ارسال دوباره وجود ندارد؛ خروجی جدید بسازید.")
            draft = Draft.from_dict(pending["draft"])
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

        async def update(message: str, running: bool = True, buttons=None) -> None:
            try:
                await status.edit(
                    message,
                    buttons=buttons or ([[button("توقف عملیات", "g:stop")]] if running else None),
                )
            except MessageNotModifiedError:
                pass
            except Exception:
                logger.warning("Could not update progress message", exc_info=True)

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

        retain_output = pending is not None
        completed_render = False
        final_video = not preview and not comparison and gif is None
        result = None
        try:
            if retry:
                result = RenderResult(
                    folder / pending["path"],
                    await inspect(folder / pending["path"], self.config),
                    pending["notice"],
                )
            elif comparison:
                async with asyncio.timeout(self.config.max_render_seconds):
                    result = await self.gallery.render(draft, folder, render_progress)
            elif gif is not None:
                result = await self.gif_exporter.convert(
                    Path(draft.source), gif, folder, render_progress
                )
            else:
                (folder / "pending.json").unlink(missing_ok=True)
                (folder / "export.manifest.json").unlink(missing_ok=True)
                async with asyncio.timeout(self.config.max_render_seconds):
                    result = await self.renderer.render(
                        draft, folder, render_progress, preview=preview, stage=update
                    )
            completed_render = True
            if final_video:
                for name in ("edited.mp4", "edited.mkv"):
                    if name != result.path.name:
                        (folder / name).unlink(missing_ok=True)
                (folder / "pending.json").write_text(
                    json.dumps(
                        {
                            "path": result.path.name,
                            "draft": draft.to_dict(),
                            "notice": result.notice,
                        }
                    ),
                    encoding="utf-8",
                )
                retain_output = True
            await update(
                f"خروجی آماده است ({result.path.stat().st_size / 1024**2:.1f} MiB)؛ در حال ارسال…"
            )
            thumb = None
            try:
                thumb = str(await self.renderer.thumbnail(result.path, folder / "thumb.jpg"))
            except Exception:
                logger.warning("Thumbnail failed; sending the original file", exc_info=True)
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
            multipart = await self.delivery.send(
                result.path,
                force_document=as_file,
                supports_streaming=not as_file,
                attributes=attributes,
                thumb=thumb,
                caption=caption,
                progress=upload_progress,
                mime_type="image/gif" if gif is not None else None,
            )
            retain_output = False
            if not preview and not comparison:
                self.store.record(draft.original_name, "done", result.notice)
            await update(
                "GIF ارسال شد؛ پروژه برای ویرایش و تبدیل دوباره حفظ شده است."
                if gif is not None
                else "پیش‌نمایش ارسال شد."
                if preview
                else "همهٔ قسمت‌ها و راهنمای اتصال ارسال شدند؛ "
                "بعد از اتصال، فایل کامل بدون تغییر تصویر یا صدا بازیابی می‌شود."
                if multipart
                else "خروجی ارسال شد؛ امکان تغییر و رندر دوباره دارید.",
                running=False,
            )
        except asyncio.CancelledError:
            await update(
                "ارسال متوقف شد؛ خروجی برای ارسال دوباره حفظ شده است."
                if retain_output
                else "عملیات متوقف شد.",
                running=False,
            )
            raise
        except TimeoutError:
            self.store.record(draft.original_name, "failed", "render timeout")
            await update(
                "مهلت ارسال تمام شد؛ خروجی برای ارسال دوباره حفظ شده است."
                if retain_output
                else "مهلت رندر تمام شد؛ برش کوتاه‌تر انتخاب کنید.",
                running=False,
            )
        except ValueError as exc:
            self.store.record(draft.original_name, "failed", str(exc))
            await update(
                str(exc)[:1600]
                + ("\nخروجی حفظ شد؛ دکمهٔ «ارسال دوبارهٔ خروجی» را بزنید." if retain_output else ""),
                running=False,
            )
        except Exception as exc:
            logger.exception("Render or Telegram delivery failed for %s", draft.id)
            self.store.record(draft.original_name, "failed", "render or delivery error")
            message = (
                f"ارسال خروجی ({result.path.stat().st_size / 1024**2:.1f} MiB) با خطا "
                f"روبه‌رو شد: {type(exc).__name__}. "
                "خروجی روی سرور حفظ شد؛ «ارسال دوبارهٔ خروجی» را بزنید. "
                "جزئیات در لاگ سرور است."
                if completed_render and retain_output
                else f"رندر یا ارسال با خطا روبه‌رو شد: {type(exc).__name__}. "
                "پروژه حفظ شده؛ جزئیات در لاگ سرور است."
            )
            await update(
                message,
                running=False,
                buttons=(
                    [[button("ارسال دوبارهٔ خروجی", f"d:{draft.id}:resend")]]
                    if retain_output
                    else None
                ),
            )
        finally:
            # Keep a validated final video after interrupted/failed upload for a cheap retry.
            for name in (
                "edited.mp4",
                "edited.mkv",
                "edited.gif",
                "text.png",
                "logo.png",
                "thumb.jpg",
                "look.cube",
                "comparison.png",
                "pending.json",
                "export.manifest.json",
            ):
                if retain_output and name in {
                    "edited.mp4",
                    "edited.mkv",
                    "pending.json",
                    "export.manifest.json",
                }:
                    continue
                (folder / name).unlink(missing_ok=True)
            for path in folder.glob("sample-*.png"):
                path.unlink(missing_ok=True)
        # The completion menu must be usable after Activity marks this task done.
        await refresh()
