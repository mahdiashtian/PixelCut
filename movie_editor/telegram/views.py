"""Inline keyboards and display text; business rules stay in services/models."""

from telethon import Button

from ..domain import (
    GRADE_LIMITS,
    Color,
    ColorGrade,
    Delivery,
    Draft,
    Look,
    Position,
    Quality,
    Settings,
    Transition,
)

POSITION_NAMES = {
    Position.TOP_LEFT: "بالا چپ",
    Position.TOP_CENTER: "بالا وسط",
    Position.TOP_RIGHT: "بالا راست",
    Position.CENTER_LEFT: "وسط چپ",
    Position.CENTER: "مرکز",
    Position.CENTER_RIGHT: "وسط راست",
    Position.BOTTOM_LEFT: "پایین چپ",
    Position.BOTTOM_CENTER: "پایین وسط",
    Position.BOTTOM_RIGHT: "پایین راست",
}
COLOR_NAMES = {Color.WHITE: "سفید", Color.BLACK: "سیاه", Color.DYNAMIC: "داینامیک"}
QUALITY_NAMES = {
    Quality.HIGH: "حجم کمتر (تصویر فشرده)",
    Quality.FAST: "رندر سریع (تصویر فشرده)",
    Quality.LOSSLESS: "بدون افت کیفیت (حجم بزرگ‌تر)",
}
TRANSITION_NAMES = {
    Transition.CUT: "اتصال ساده",
    Transition.SHADOW: "سایه / عبور از سیاه",
    Transition.FADE: "محو نرم",
    Transition.DISSOLVE: "حل شدن",
    Transition.SLIDE: "حرکت نرم به چپ",
    Transition.CIRCLE: "باز شدن دایره",
    Transition.PIXEL: "پیکسلی",
}
KIND_NAMES = {
    "font": "فونت",
    "logo": "لوگو",
    "intro": "کلیپ ابتدا",
    "outro": "کلیپ انتها",
    "lut": "LUT رنگی",
}
LOOK_NAMES = {
    Look.NONE: "بدون فیلتر",
    Look.BW: "B&W",
    Look.WARM: "گرم",
    Look.COOL: "سرد",
    Look.SEPIA: "سپیا",
    Look.VINTAGE: "وینتیج",
    Look.CINEMA: "سینمایی",
    Look.FADE: "مات",
    Look.VIVID: "رنگ زنده",
    Look.NOIR: "نوآر",
    Look.DNT1: "DNT1",
    Look.DNT2: "DNT2",
    Look.DNT3: "DNT3",
    Look.DNT4: "DNT4",
    Look.DNT5: "DNT5",
    Look.CUSTOM: "LUT شخصی",
}
GRADE_FIELDS = {
    name: (GRADE_LIMITS[name][2], choices)
    for name, choices in {
        "strength": (0, 25, 50, 75, 100),
        "brightness": (-50, -25, 0, 25, 50),
        "contrast": (75, 100, 125, 150),
        "saturation": (0, 50, 100, 150, 200),
        "gamma": (0.8, 1, 1.2, 1.5),
        "temperature": (-50, -25, 0, 25, 50),
        "vignette": (0, 25, 50, 75, 100),
    }.items()
}


def button(label: str, data: str):
    return Button.inline(label, data.encode())


def home():
    return [
        [button("فونت‌ها", "g:library:font:0"), button("کتابخانه", "g:assets")],
        [button("پیش‌فرض‌ها", "g:defaults"), button("تاریخچه", "g:history")],
        [button("متن واترمارک ذخیره‌شده", "g:savedtext"), button("راهنما", "g:help")],
    ]


def draft_buttons(draft: Draft, pending_output: bool = False):
    prefix = f"d:{draft.id}:"

    def b(label, action):
        return button(label, prefix + action)

    rows = [
        [b("واترمارک متنی", "text"), b("لوگو", "library:logo:0")],
        [b("کلیپ ابتدا", "library:intro:0"), b("کلیپ انتها", "library:outro:0")],
        [b("ظاهر و فونت", "style:text"), b("فیلتر رنگی", "filters"), b("پیش‌فرض", "defaults")],
        [b("پیش‌نمایش", "preview"), b("ساخت خروجی", "render"), b("تبدیل به GIF", "gif")],
        [b("خروجی فشرده (حجم کمتر)", "compact")],
        [b("گزینه‌های بیشتر", "advanced"), b("ویدیوی جدید", "discard")],
    ]
    if pending_output:
        rows.insert(0, [b("ارسال دوبارهٔ خروجی", "resend")])
    return rows


def advanced_buttons(draft: Draft):
    prefix = f"d:{draft.id}:"

    def b(label, action):
        return button(label, prefix + action)

    return [
        [b("ظاهر لوگو", "style:logo"), b("متن ذخیره‌شده", "use_text")],
        [b("ترنزیشن ابتدا", "join:intro"), b("ترنزیشن انتها", "join:outro")],
        [b("برش زمانی", "trim"), b("روشن کردن صدا" if draft.mute else "حذف صدا", "mute")],
        [b("نوع خروجی", "quality"), b("نوع ارسال", "delivery")],
        [b("ذخیره ظاهر به‌عنوان پیش‌فرض", "save_defaults")],
        [button("کتابخانه", "g:assets"), button("ذخیره متن تکراری", "g:savedtext")],
        [button("پیش‌فرض‌ها", "g:defaults"), button("تاریخچه", "g:history")],
        [b("بازگشت به ویرایش", "back")],
    ]


def gif_buttons(draft: Draft):
    prefix = f"d:{draft.id}:"
    return [
        [button("کل ویدیو", prefix + "gif_all"), button("انتخاب بازهٔ زمانی", prefix + "gif_range")],
        [button("بازگشت", prefix + "back")],
    ]


def gif_text(duration: float) -> str:
    return (
        f"تبدیل به GIF از ویدیوی اصلی ({duration:g} ثانیه)\n"
        "کل ویدیو یا بازه‌ای از آن را انتخاب کنید. ابعاد اصلی و فریم‌ها حفظ می‌شوند.\n"
        "GIF بی‌صدا و محدود به ۲۵۶ رنگ در هر فریم است؛ کیفیت رنگ عین ویدیو نمی‌ماند.\n"
        "برای ویدیوی طولانی، حجم و زمان تبدیل می‌تواند زیاد باشد."
    )


def settings_text(settings: Settings) -> str:
    text, logo = settings.text_style, settings.logo_style
    return (
        f"متن: عرض {text.width_percent:g}٪، {POSITION_NAMES[text.position]}، "
        f"{COLOR_NAMES[text.color]}، شفافیت {text.opacity:g}٪، حاشیه {text.margin_percent:g}٪\n"
        f"لوگو: عرض {logo.width_percent:g}٪، {POSITION_NAMES[logo.position]}، "
        f"شفافیت {logo.opacity:g}٪، حاشیه {logo.margin_percent:g}٪\n"
        f"ترنزیشن ابتدا: {TRANSITION_NAMES[settings.intro_join.kind]} "
        f"({settings.intro_join.seconds:g}s)\n"
        f"ترنزیشن انتها: {TRANSITION_NAMES[settings.outro_join.kind]} "
        f"({settings.outro_join.seconds:g}s)\n"
        f"فیلتر: {LOOK_NAMES[settings.color_grade.look]} ({settings.color_grade.strength:g}٪)\n"
        f"خروجی: {QUALITY_NAMES[settings.quality]}، "
        f"{'فایل اصلی' if settings.delivery == Delivery.FILE else 'ویدیوی قابل پخش'}"
    )


def draft_text(draft: Draft) -> str:
    trim = (
        f"{draft.trim_start:g} تا {draft.trim_end:g}" if draft.trim_end is not None else "کل ویدیو"
    )
    return (
        f"🎬 {draft.original_name}\n"
        f"واترمارک: {draft.text or 'انتخاب نشده'}\n"
        f"لوگو: {'دارد' if draft.logo_id else 'ندارد'} | "
        f"ابتدا: {'دارد' if draft.intro_id else 'ندارد'} | "
        f"انتها: {'دارد' if draft.outro_id else 'ندارد'}\n"
        f"برش: {trim} | صدا: {'خاموش' if draft.mute else 'روشن'}\n\n"
        + f"فیلتر: {LOOK_NAMES[draft.settings.color_grade.look]} "
        + f"({draft.settings.color_grade.strength:g}٪)\n"
        + f"کیفیت خروجی: {QUALITY_NAMES[draft.settings.quality]}\n"
        + "گزینه‌ها را انتخاب کنید و «ساخت خروجی» را بزنید."
    )


HELP = (
    "ویدیوی اصلی را به‌صورت فایل بفرستید؛ بعد گزینه‌ها با دکمه‌های شیشه‌ای نمایش داده می‌شوند.\n"
    "برای متن، لوگو و کلیپ ابتدا/انتها انتخاب مستقل دارید. "
    "فونت TTF/OTF و لوگوی PNG شفاف قابل آپلود است.\n"
    "داینامیک: متن روی زمینه روشن سیاه و روی زمینه تیره سفید می‌شود.\n"
    "«فیلتر رنگی»: B&W و لوک‌های محلی، شدت، اصلاح رنگ و LUT سه‌بعدی .cube.\n"
    "DNT1 تا DNT5 فقط پس از نصب LUT مرجع فعال می‌شوند؛ افکت حدسی جای آن‌ها نیست.\n"
    "پیش‌فرض فقط ظاهر، فونت، رنگ، ترنزیشن، کیفیت و نوع ارسال را ذخیره می‌کند؛ "
    "محتوای پروژه را ذخیره نمی‌کند.\n"
    "خروجی پیش‌فرض بدون افت کیفیت، به‌صورت فایل MKV است. افزایش حجم، افزایش کیفیت منبع نیست.\n"
    "خروجی بزرگ در قسمت‌های بایتی ارسال می‌شود؛ بعد از اتصال، فایل کامل بدون تغییر بازیابی می‌شود.\n"
    "«خروجی فشرده» یک انتخاب جداگانه برای حجم کمتر و تصویر با اتلاف است.\n"
    "در خطای ارسال، خروجی نهایی حفظ می‌شود؛ «ارسال دوبارهٔ خروجی» رندر را تکرار نمی‌کند.\n"
    "«حجم کمتر» و «رندر سریع» تصویر را فشرده می‌کنند؛ صدا با افت دوباره فشرده نمی‌شود.\n"
    "برش و اتصال با صدای پردازش‌شده به‌صورت فایل MKV ارسال می‌شود.\n"
    "«تبدیل به GIF»: کل ویدیوی اصلی یا بازه‌ای مثل ۱۲ تا ۲۰؛ فایل GIF با ابعاد اصلی.\n"
    "GIF بی‌صدا و دارای حداکثر ۲۵۶ رنگ در هر فریم است و کیفیت عین ویدیو را تضمین نمی‌کند.\n"
    "ویرایش پیکسل‌ها نیاز به encode دارد. حفظ بیت‌به‌بیت فایل ورودی ممکن نیست.\n"
    "HDR و ۱۰/۱۲ بیت فعلاً پذیرفته نمی‌شود تا رنگ و عمق بیت ناخواسته تغییر نکند.\n"
    "/start منو | /settings پیش‌فرض‌ها | /fonts فونت‌ها | /library کتابخانه\n"
    "/status وضعیت | /cancel توقف عملیات یا انصراف از ورودی، همراه حفظ پروژه.\n"
    "فایل خروجی به‌صورت Document پیش‌فرض است. "
    "برای حفظ فایل اصلی آن را دوباره با فشرده‌سازی ارسال نکنید."
)


def grade_text(grade: ColorGrade) -> str:
    return (
        f"فیلتر: {LOOK_NAMES[grade.look]} | شدت: {grade.strength:g}٪\n"
        f"روشنایی: {grade.brightness:g} | کنتراست: {grade.contrast:g} | "
        f"اشباع: {grade.saturation:g}\n"
        f"گاما: {grade.gamma:g} | دمای رنگ: {grade.temperature:g} | "
        f"تیرگی لبه‌ها: {grade.vignette:g}\n"
        "DNTها به LUT مرجع نیاز دارند. سایر لوک‌ها پروفایل محلی هستند."
    )


def grade_buttons(scope: str, grade: ColorGrade, installed: set[str]):
    looks = []
    for look, name in LOOK_NAMES.items():
        label = ("✓ " if grade.look == look else "") + name
        if look.needs_lut and look != Look.CUSTOM and look.value not in installed:
            label += " • LUT لازم"
        looks.append(button(label, f"{scope}:look:{look.value}"))
    rows = [looks[i : i + 3] for i in range(0, len(looks), 3)]
    rows.extend(
        [
            [
                button("شدت فیلتر", f"{scope}:grade_value:strength"),
                button("اصلاح رنگ", f"{scope}:adjust"),
            ],
            [
                button("بازنشانی رنگ", f"{scope}:grade_reset"),
                button("مراجع DNT / LUT", f"{scope}:references"),
            ],
        ]
    )
    if scope.startswith("d:"):
        rows.append(
            [
                button("پیش‌نمایش ویدیو", f"{scope}:preview"),
                button("مقایسهٔ فیلترها", f"{scope}:compare"),
            ]
        )
    rows.append([button("بازگشت", f"{scope}:back")])
    return rows


def adjustment_buttons(scope: str):
    items = [
        button(label, f"{scope}:grade_value:{name}") for name, (label, _) in GRADE_FIELDS.items()
    ]
    return [items[i : i + 2] for i in range(0, len(items), 2)] + [
        [button("بازگشت به فیلترها", f"{scope}:filters")]
    ]
