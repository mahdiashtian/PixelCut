"""Admin-only conversation state and input routing, delegating media work to services."""

import asyncio
import logging
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from telethon import events

from ..assets import AssetService
from ..config import Config
from ..domain import (
    Color,
    ColorGrade,
    Delivery,
    Draft,
    GifRange,
    Look,
    Position,
    Quality,
    Settings,
    Transition,
    number,
)
from ..jobs import Activity, JobService
from ..media.probe import inspect
from ..storage import Store
from . import views
from .uploads import SourceVideo, UploadService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pending:
    kind: str
    scope: str
    field: str = ""


class Controller:
    def __init__(
        self, client, config: Config, store: Store, assets: AssetService, jobs: JobService
    ):
        self.client, self.config, self.store, self.assets, self.jobs = (
            client,
            config,
            store,
            assets,
            jobs,
        )
        self.uploads = UploadService(client, config, assets)
        self.activity = Activity()
        self.pending: Pending | None = None
        self.draft = store.draft()
        if self.draft and not Path(self.draft.source).is_file():
            logger.warning("Discarding draft whose source is missing")
            self.draft = None
            self.store.save_draft(None)

    def allowed(self, event) -> bool:
        return bool(
            event.sender_id == self.config.admin_id
            and event.is_private
            and event.chat_id == self.config.admin_id
        )

    def register(self) -> None:
        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.on_callback, events.CallbackQuery())

    async def say(self, text: str, buttons=None) -> None:
        await self.client.send_message(self.config.admin_id, text, buttons=buttons, parse_mode=None)

    async def show(self) -> None:
        if self.draft:
            await self.say(views.draft_text(self.draft), views.draft_buttons(self.draft))
        else:
            await self.say("سلام. ویدیوی اصلی را بفرستید تا ویرایش شروع شود.", views.home())

    def scope(self) -> str:
        return f"d:{self.draft.id}" if self.draft else "s"

    def settings(self, scope: str) -> Settings:
        if scope == "s":
            return self.store.defaults()
        if not self.draft or scope != f"d:{self.draft.id}":
            raise ValueError("این دکمه متعلق به پروژهٔ قبلی است؛ از منوی فعلی استفاده کنید.")
        return self.draft.settings

    def save_settings(self, scope: str, settings: Settings) -> None:
        settings.validate()
        if scope == "s":
            self.store.save_defaults(settings)
        else:
            self.store.save_draft(self.draft)

    async def defaults_menu(self) -> None:
        b = views.button
        await self.say(
            "پیش‌فرض‌ها\n" + views.settings_text(self.store.defaults()),
            [
                [b("ظاهر و فونت متن", "s:style:text"), b("ظاهر لوگو", "s:style:logo")],
                [b("ترنزیشن ابتدا", "s:join:intro"), b("ترنزیشن انتها", "s:join:outro")],
                [b("کیفیت", "s:quality"), b("نوع ارسال", "s:delivery")],
                [b("فیلتر و اصلاح رنگ", "s:filters")],
                [b("بازنشانی پیش‌فرض‌ها", "s:reset"), b("بازگشت", "g:home")],
            ],
        )

    async def back(self, scope: str) -> None:
        if scope == "s":
            await self.defaults_menu()
        else:
            await self.show()

    async def ask(self, pending: Pending, text: str, buttons=None) -> None:
        self.pending = pending
        rows = list(buttons or [])
        rows.append([views.button("انصراف و بازگشت", "g:cancel_input")])
        await self.say(text, rows)

    async def on_message(self, event) -> None:
        if not self.allowed(event):
            return
        try:
            text = (event.raw_text or "").strip()
            command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
            if command == "/cancel":
                await self.cancel()
                return
            if command == "/status":
                await self.say(
                    "در حال " + self.activity.label if self.activity.busy else "عملیات فعال ندارید."
                )
                return
            if self.activity.busy:
                await self.say("عملیات در حال اجرا است. برای توقف /cancel را بزنید.")
                return
            if command:
                self.pending = None
                if command == "/help":
                    await self.say(views.HELP)
                elif command == "/settings":
                    await self.defaults_menu()
                elif command == "/fonts":
                    await self.library("g", "font", 0)
                elif command == "/library":
                    await self.asset_menu()
                else:
                    await self.show()
            elif self.pending:
                pending = self.pending
                if pending.kind in {"font", "logo", "intro", "outro", "lut"}:
                    if not event.media:
                        raise ValueError("لطفاً فایل درخواست‌شده را ارسال کنید.")
                    self.pending = None
                    self.activity.start("دریافت فایل", lambda: self.upload(event, pending))
                else:
                    await self.accept_text(pending, text)
            elif event.media:
                if self.draft:
                    await self.say("یک پروژهٔ باز دارید. ابتدا از دکمهٔ «ویرایش جدید» استفاده کنید.")
                    return
                self.activity.start(
                    "دریافت ویدیو", lambda: self.upload(event, Pending("source", "g"))
                )
            else:
                await self.show()
        except ValueError as exc:
            await self.say(str(exc))
        except Exception:
            logger.exception("Message handler failed")
            await self.say("خطای داخلی رخ داد؛ جزئیات در لاگ سرور ثبت شد.")

    async def cancel(self) -> None:
        if self.activity.busy:
            await self.activity.cancel()
            await self.say("عملیات متوقف شد. اگر پروژه‌ای باز بود، برای تلاش دوباره حفظ شده است.")
        elif self.pending:
            self.pending = None
            await self.say("ورودی لغو شد.")
        else:
            await self.say("عملیات فعالی ندارید. پروژه حفظ شده است.")
        await self.show()

    def discard(self) -> None:
        if self.draft:
            folder = (self.config.data_dir / "jobs" / self.draft.id).resolve()
            if not folder.is_relative_to((self.config.data_dir / "jobs").resolve()):
                raise ValueError("مسیر پروژه معتبر نیست.")
            self.store.save_draft(None)
            self.draft = None
            if folder.exists():
                shutil.rmtree(folder)

    async def accept_text(self, pending: Pending, text: str) -> None:
        if pending.kind == "savedtext":
            if len(text) > 200 or "\n" in text or "\r" in text or not text:
                raise ValueError("متن باید یک خط و حداکثر ۲۰۰ کاراکتر باشد؛ برای حذف «-» بفرستید.")
            self.store.set("saved_text", "" if text == "-" else text)
            self.pending = None
            await self.say("متن ذخیره شد. برای هر پروژه دکمهٔ «متن ذخیره‌شده» را بزنید.")
            return
        settings = self.settings(pending.scope)
        if pending.kind == "text":
            if len(text) > 200 or "\n" in text or "\r" in text or not text:
                raise ValueError("واترمارک باید یک خط و حداکثر ۲۰۰ کاراکتر باشد.")
            self.draft.text = "" if text == "-" else text
        elif pending.kind == "trim":
            if text == "-":
                self.draft.trim_start, self.draft.trim_end = 0, None
            else:
                pieces = text.replace("،", " ").split()
                if len(pieces) != 2:
                    raise ValueError("دو عدد ثانیه بفرستید؛ مثال: 5 35. برای حذف برش: -")
                start, end = (
                    number(pieces[0], 0, 86400, "شروع"),
                    number(pieces[1], 0, 86400, "پایان"),
                )
                info = await inspect(Path(self.draft.source), self.config)
                settings = self.settings(pending.scope)
                if not start < end <= info.duration:
                    raise ValueError("شروع و پایان باید داخل مدت ویدیو و به ترتیب باشند.")
                self.draft.trim_start, self.draft.trim_end = start, end
        elif pending.kind == "gif":
            await self.start_gif(pending.scope, GifRange.from_text(text))
            return
        elif pending.kind == "grade":
            if pending.field not in views.GRADE_FIELDS:
                raise ValueError("تنظیم رنگ معتبر نیست.")
            candidate = replace(settings.color_grade, **{pending.field: text})
            candidate.validate()
            settings.color_grade = candidate
            self.save_settings(pending.scope, settings)
            self.pending = None
            await self.color_menu(pending.scope)
            return
        elif pending.kind == "value":
            target, field = pending.field.split(".")
            if target in {"intro", "outro"}:
                value = number(text, 0.1, 3, "زمان ترنزیشن")
                getattr(settings, f"{target}_join").seconds = value
            else:
                limits = {"width_percent": (1, 95), "opacity": (1, 100), "margin_percent": (0, 20)}
                value = number(text, *limits[field], "مقدار")
                getattr(settings, f"{target}_style").__setattr__(field, value)
        self.save_settings(pending.scope, settings)
        self.pending = None
        await self.back(pending.scope)

    async def start_gif(self, scope: str, selection: GifRange) -> None:
        self.settings(scope)
        info = await inspect(Path(self.draft.source), self.config)
        selection.resolve(info.duration)
        self.settings(scope)
        snapshot = Draft.from_dict(self.draft.to_dict())
        self.pending = None
        self.activity.start(
            "تبدیل به GIF", lambda: self.jobs.run(snapshot, False, self.show, gif=selection)
        )

    async def upload(self, event, pending: Pending) -> None:
        try:
            if pending.scope not in {"g", "s"}:
                self.settings(pending.scope)
            result = await self.uploads.receive(event, pending.kind)
            if isinstance(result, SourceVideo):
                self.draft = Draft(result.id, str(result.path), result.name, self.store.defaults())
                self.store.save_draft(self.draft)
            elif pending.scope != "g":
                self.select_asset(pending.scope, pending.kind, result.id, pending.field)
            await self.back(pending.scope)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError) as exc:
            self.pending = pending if pending.kind != "source" else None
            await self.say(str(exc)[:1500])
        except TimeoutError:
            self.pending = pending if pending.kind != "source" else None
            await self.say("مهلت دریافت تمام شد؛ فایل را دوباره ارسال کنید.")
        except Exception:
            logger.exception("Upload failed")
            self.pending = pending if pending.kind != "source" else None
            await self.say(
                "دریافت فایل با خطا روبه‌رو شد؛ دوباره تلاش کنید. جزئیات در لاگ سرور است."
            )

    def select_asset(self, scope: str, kind: str, asset_id: str | None, target: str = "") -> None:
        self.store.asset(asset_id, kind)
        settings = self.settings(scope)
        if kind == "font":
            settings.text_style.font_id = asset_id
        elif kind == "lut":
            look = Look(target) if target else Look.CUSTOM
            if not look.needs_lut:
                raise ValueError("پروفایل LUT معتبر نیست.")
            settings.color_grade.look = look if asset_id else Look.NONE
            settings.color_grade.lut_id = asset_id
            if asset_id and look.needs_lut:
                self.store.bind_look(look, asset_id)
        elif scope != "s":
            setattr(self.draft, f"{kind}_id", asset_id)
        else:
            raise ValueError("محتوای کلیپ و لوگو به‌عنوان پیش‌فرض ذخیره نمی‌شود.")
        self.save_settings(scope, settings)

    async def asset_menu(self) -> None:
        items = [
            views.button(label, f"g:library:{kind}:0") for kind, label in views.KIND_NAMES.items()
        ]
        await self.say(
            "کتابخانهٔ فایل‌های قابل استفادهٔ مجدد",
            [items[i : i + 3] for i in range(0, len(items), 3)]
            + [[views.button("بازگشت", "g:home")]],
        )

    async def library(self, scope: str, kind: str, page: int, target: str = "") -> None:
        if kind not in views.KIND_NAMES or (scope == "s" and kind not in {"font", "lut"}):
            raise ValueError("این کتابخانه برای این تنظیم قابل انتخاب نیست.")
        if target and (kind != "lut" or not Look(target).needs_lut):
            raise ValueError("پروفایل LUT معتبر نیست.")
        rows = []
        suffix = f":{target}" if target else ""
        for asset in self.store.assets(kind, page * 8):
            select = (
                f"{scope}:select:{kind}:{asset.id}{suffix}"
                if scope != "g"
                else f"g:info:{kind}:{asset.id}"
            )
            rows.append(
                [
                    views.button(asset.name, select),
                    views.button("حذف", f"{scope}:delete:{kind}:{asset.id}"),
                ]
            )
        rows.append([views.button("آپلود فایل جدید", f"{scope}:upload:{kind}{suffix}")])
        if scope != "g":
            rows.append([views.button("برداشتن انتخاب", f"{scope}:select:{kind}:none{suffix}")])
        navigation = []
        if page:
            navigation.append(views.button("قبلی", f"{scope}:library:{kind}:{page - 1}{suffix}"))
        if self.store.assets(kind, (page + 1) * 8):
            navigation.append(views.button("بعدی", f"{scope}:library:{kind}:{page + 1}{suffix}"))
        if navigation:
            rows.append(navigation)
        rows.append([views.button("بازگشت", f"{scope}:back")])
        self.pending = Pending(kind, scope, target)
        label = f" برای {target.upper()}" if target else ""
        await self.say(
            f"یک {views.KIND_NAMES[kind]}{label} بفرستید یا از فایل‌های ذخیره‌شده انتخاب کنید.", rows
        )

    def used_assets(self) -> set[str]:
        defaults = self.store.defaults()
        used = {defaults.text_style.font_id, defaults.color_grade.lut_id}
        if self.draft:
            used.update(
                [
                    self.draft.logo_id,
                    self.draft.intro_id,
                    self.draft.outro_id,
                    self.draft.settings.text_style.font_id,
                    self.draft.settings.color_grade.lut_id,
                ]
            )
        return {asset for asset in used if asset}

    async def color_menu(self, scope: str) -> None:
        grade = self.settings(scope).color_grade
        await self.say(
            views.grade_text(grade),
            views.grade_buttons(scope, grade, set(self.store.get("look_luts", {}))),
        )

    async def on_callback(self, event) -> None:
        if not self.allowed(event):
            return
        try:
            if event.data == b"g:stop":
                await event.answer()
                if self.activity.busy:
                    await self.cancel()
                else:
                    await self.say("عملیات فعالی ندارید.")
                return
            if self.activity.busy:
                await event.answer("عملیات در حال اجراست؛ برای توقف /cancel را بزنید.", alert=True)
                return
            await event.answer()
            parts = event.data.decode().split(":")
            if parts[0] == "d":
                scope, args = ":".join(parts[:2]), parts[2:]
                self.settings(scope)
            else:
                scope, args = parts[0], parts[1:]
            if scope not in {"g", "s"} and not scope.startswith("d:"):
                raise ValueError("دکمه معتبر نیست.")
            self.pending = None
            await self.action(scope, args)
        except (ValueError, UnicodeError, IndexError, KeyError) as exc:
            await self.say(str(exc) if isinstance(exc, ValueError) else "دکمه معتبر نیست.")
        except Exception:
            logger.exception("Callback handler failed")
            await self.say("خطای داخلی رخ داد؛ جزئیات در لاگ سرور ثبت شد.")

    async def action(self, scope: str, args: list[str]) -> None:
        action = args[0]
        b = views.button
        if action in {"home", "back"}:
            await self.show() if scope != "s" else await self.defaults_menu()
        elif action == "cancel_input":
            self.pending = None
            await self.show()
        elif action == "advanced":
            await self.say(
                views.settings_text(self.draft.settings), views.advanced_buttons(self.draft)
            )
        elif action == "filters":
            await self.color_menu(scope)
        elif action == "references":
            self.settings(scope)
            rows = [
                [
                    b(
                        f"انتخاب / تغییر مرجع {look.value.upper()}",
                        f"{scope}:library:lut:0:{look.value}",
                    )
                ]
                for look in (Look.DNT1, Look.DNT2, Look.DNT3, Look.DNT4, Look.DNT5)
            ]
            rows.extend(
                [
                    [b("LUT شخصی", f"{scope}:library:lut:0:custom")],
                    [b("بازگشت", f"{scope}:filters")],
                ]
            )
            await self.say("فایل .cube مرجع همان پروفایل را انتخاب یا آپلود کنید.", rows)
        elif action == "compare":
            snapshot = Draft.from_dict(self.draft.to_dict())
            self.activity.start(
                "مقایسهٔ فیلترها",
                lambda: self.jobs.run(snapshot, False, self.show, comparison=True),
            )
        elif action == "adjust":
            await self.say(
                views.grade_text(self.settings(scope).color_grade), views.adjustment_buttons(scope)
            )
        elif action == "look":
            look = Look(args[1])
            settings = self.settings(scope)
            if look.needs_lut:
                asset_id = self.store.look_lut(look)
                if asset_id is None or look == Look.CUSTOM:
                    await self.library(scope, "lut", 0, look.value)
                    return
                self.store.asset(asset_id, "lut")
                settings.color_grade.lut_id = asset_id
            else:
                settings.color_grade.lut_id = None
            settings.color_grade.look = look
            self.save_settings(scope, settings)
            await self.color_menu(scope)
        elif action == "grade_reset":
            settings = self.settings(scope)
            settings.color_grade = ColorGrade()
            self.save_settings(scope, settings)
            await self.color_menu(scope)
        elif action == "grade_value":
            name = args[1]
            if name not in views.GRADE_FIELDS:
                raise ValueError("تنظیم رنگ معتبر نیست.")
            label, choices = views.GRADE_FIELDS[name]
            buttons = [b(str(value), f"{scope}:grade_set:{name}:{value}") for value in choices]
            await self.ask(
                Pending("grade", scope, name),
                f"{label}: یک مقدار انتخاب کنید یا عدد بفرستید.",
                [buttons],
            )
        elif action == "grade_set":
            await self.accept_text(Pending("grade", scope, args[1]), args[2])
        elif action == "help":
            await self.say(views.HELP)
        elif action == "assets":
            await self.asset_menu()
        elif action == "history":
            rows = self.store.history()
            await self.say(
                "\n".join(f"{r['created']} UTC | {r['name']} | {r['status']}" for r in rows)
                or "تاریخچه خالی است."
            )
        elif action == "savedtext":
            await self.ask(
                Pending("savedtext", "g"), "متن قابل استفادهٔ مجدد را بفرستید. برای حذف: -"
            )
        elif action == "defaults":
            if scope == "g":
                await self.defaults_menu()
            else:
                self.draft.settings = self.store.defaults()
                self.store.save_draft(self.draft)
                await self.show()
        elif action == "save_defaults":
            self.store.save_defaults(self.settings(scope))
            await self.say("تنظیمات ذخیره شد؛ متن و فایل‌های پروژه وارد پیش‌فرض نشدند.")
        elif action == "reset":
            self.store.save_defaults(Settings())
            await self.defaults_menu()
        elif action == "text":
            await self.ask(
                Pending("text", scope),
                "متن واترمارک را بفرستید یا متن ذخیره‌شده را انتخاب کنید.",
                [
                    [b("متن ذخیره‌شده", f"{scope}:use_text")],
                    [b("ظاهر و فونت", f"{scope}:style:text"), b("حذف متن", f"{scope}:clear_text")],
                ],
            )
        elif action == "clear_text":
            self.draft.text = ""
            self.store.save_draft(self.draft)
            await self.show()
        elif action == "use_text":
            self.draft.text = self.store.get("saved_text", "")
            self.store.save_draft(self.draft)
            await self.show()
        elif action == "trim":
            await self.ask(
                Pending("trim", scope), "شروع و پایان را به ثانیه بفرستید؛ مثال: 5 35. حذف برش: -"
            )
        elif action == "gif":
            info = await inspect(Path(self.draft.source), self.config)
            self.settings(scope)
            await self.say(views.gif_text(info.duration), views.gif_buttons(self.draft))
        elif action == "gif_all":
            await self.start_gif(scope, GifRange())
        elif action == "gif_range":
            await self.ask(
                Pending("gif", scope),
                "شروع و پایان GIF را به ثانیه بفرستید؛ مثال: 12 20 یا ۱۲ تا ۲۰.\n"
                "اعداد اعشاری هم مجازند؛ برای کل ویدیو «کل» بفرستید.",
            )
        elif action == "mute":
            self.draft.mute = not self.draft.mute
            self.store.save_draft(self.draft)
            await self.show()
        elif action == "discard":
            self.discard()
            await self.show()
        elif action in {"render", "preview"}:
            snapshot = Draft.from_dict(self.draft.to_dict())
            self.activity.start(
                "رندر", lambda: self.jobs.run(snapshot, action == "preview", self.show)
            )
        elif action == "library":
            await self.library(
                scope, args[1], max(0, int(args[2])), args[3] if len(args) > 3 else ""
            )
        elif action == "upload":
            kind = args[1]
            if kind not in views.KIND_NAMES:
                raise ValueError("نوع فایل معتبر نیست.")
            await self.ask(
                Pending(kind, scope, args[2] if len(args) > 2 else ""),
                f"فایل {views.KIND_NAMES[kind]} را بفرستید. "
                "فونت: TTF/OTF؛ لوگو: PNG/WebP/JPG؛ LUT: .cube؛ کلیپ: MP4 و مشابه.",
            )
        elif action == "select":
            self.select_asset(
                scope,
                args[1],
                None if args[2] == "none" else args[2],
                args[3] if len(args) > 3 else "",
            )
            await self.back(scope)
        elif action == "info":
            asset = self.store.asset(args[2], args[1])
            await self.say(
                f"{asset.name} در کتابخانه موجود است. در ویرایش، از بخش مربوط آن را انتخاب کنید."
            )
        elif action == "delete":
            asset = self.store.asset(args[2], args[1])
            await self.say(
                f"«{asset.name}» برای همیشه از کتابخانه حذف شود؟",
                [
                    [
                        b("حذف فایل", f"{scope}:remove:{args[1]}:{args[2]}"),
                        b("انصراف", f"{scope}:back"),
                    ]
                ],
            )
        elif action == "remove":
            self.assets.delete(args[2], args[1], self.used_assets())
            await self.library(scope, args[1], 0)
        elif action == "style":
            target = args[1]
            rows = [
                [
                    b("اندازه (درصد عرض)", f"{scope}:value:{target}:width_percent"),
                    b("شفافیت", f"{scope}:value:{target}:opacity"),
                ],
                [
                    b("موقعیت", f"{scope}:positions:{target}"),
                    b("حاشیه", f"{scope}:value:{target}:margin_percent"),
                ],
            ]
            if target == "text":
                rows.append([b("رنگ", f"{scope}:colors"), b("فونت", f"{scope}:library:font:0")])
            rows.append([b("بازگشت", f"{scope}:back")])
            await self.say("تنظیمات " + ("متن" if target == "text" else "لوگو"), rows)
        elif action == "value":
            presets = {
                "width_percent": (5, 10, 15, 20, 30, 50),
                "opacity": (25, 50, 75, 100),
                "margin_percent": (0, 1, 2, 5, 10),
                "seconds": (0.3, 0.5, 0.8, 1, 1.5),
            }
            buttons = [
                b(str(value), f"{scope}:preset:{args[1]}:{args[2]}:{value}")
                for value in presets[args[2]]
            ]
            await self.ask(
                Pending("value", scope, f"{args[1]}.{args[2]}"),
                "یک مقدار انتخاب کنید یا عدد دلخواه را بفرستید.",
                [buttons[i : i + 3] for i in range(0, len(buttons), 3)],
            )
        elif action == "preset":
            await self.accept_text(Pending("value", scope, f"{args[1]}.{args[2]}"), args[3])
        elif action == "positions":
            buttons = [
                b(label, f"{scope}:position:{args[1]}:{key.value}")
                for key, label in views.POSITION_NAMES.items()
            ]
            await self.say(
                "محل قرارگیری",
                [buttons[i : i + 3] for i in range(0, 9, 3)] + [[b("بازگشت", f"{scope}:back")]],
            )
        elif action == "colors":
            await self.say(
                "رنگ متن",
                [
                    [
                        b(label, f"{scope}:color:{key.value}")
                        for key, label in views.COLOR_NAMES.items()
                    ]
                ]
                + [[b("بازگشت", f"{scope}:back")]],
            )
        elif action == "join":
            target = args[1]
            buttons = [
                b(label, f"{scope}:transition:{target}:{key.value}")
                for key, label in views.TRANSITION_NAMES.items()
            ]
            await self.say(
                "یک افکت انتخاب کنید. اتصال ساده FPS را حفظ می‌کند. "
                "افکت متحرک ممکن است FPS کلیپ‌های متفاوت را تبدیل کند.",
                [[item] for item in buttons]
                + [
                    [b("مدت ترنزیشن", f"{scope}:value:{target}:seconds")],
                    [b("بازگشت", f"{scope}:back")],
                ],
            )
        elif action == "quality":
            await self.say(
                "خروجی بدون افت کیفیت پیش‌فرض است و حجم بیشتری دارد. "
                "گزینه‌های MP4 کوچک‌ترند و فشرده‌سازی با افت دارند.",
                [
                    [b(label, f"{scope}:set_quality:{key.value}")]
                    for key, label in views.QUALITY_NAMES.items()
                ]
                + [[b("بازگشت", f"{scope}:back")]],
            )
        elif action == "delivery":
            await self.say(
                "نوع ارسال خروجی",
                [
                    [
                        b("فایل اصلی", f"{scope}:set_delivery:file"),
                        b("ویدیوی قابل پخش", f"{scope}:set_delivery:video"),
                    ]
                ],
            )
        elif action in {"position", "color", "transition", "set_quality", "set_delivery"}:
            settings = self.settings(scope)
            if action == "position":
                getattr(settings, f"{args[1]}_style").position = Position(args[2])
            elif action == "color":
                settings.text_style.color = Color(args[1])
            elif action == "transition":
                getattr(settings, f"{args[1]}_join").kind = Transition(args[2])
            elif action == "set_quality":
                settings.quality = Quality(args[1])
            else:
                settings.delivery = Delivery(args[1])
            self.save_settings(scope, settings)
            await self.back(scope)
