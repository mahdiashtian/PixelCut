"""Validate bounded, normalized 3D CUBE data before FFmpeg reads it."""

from math import isfinite
from pathlib import Path


def validate_cube(path: Path) -> int:
    size = None
    samples = 0
    domain = {"DOMAIN_MIN": (0.0, 0.0, 0.0), "DOMAIN_MAX": (1.0, 1.0, 1.0)}
    seen = set()
    try:
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                parts = line.partition("#")[0].split()
                if not parts:
                    continue
                key = parts[0]
                if key == "TITLE":
                    continue
                if key in {"LUT_3D_SIZE", "DOMAIN_MIN", "DOMAIN_MAX"}:
                    if key in seen or samples:
                        raise ValueError("هدر تکراری یا نامرتب LUT")
                    seen.add(key)
                    if key == "LUT_3D_SIZE":
                        if len(parts) != 2:
                            raise ValueError("اندازهٔ LUT")
                        size = int(parts[1])
                        if not 2 <= size <= 65:
                            raise ValueError("اندازهٔ LUT باید بین ۲ و ۶۵ باشد")
                    else:
                        if len(parts) != 4:
                            raise ValueError("دامنهٔ LUT")
                        values = tuple(float(v) for v in parts[1:])
                        if not all(isfinite(v) and 0 <= v <= 1 for v in values):
                            raise ValueError("دامنهٔ LUT باید نرمال‌شده باشد")
                        domain[key] = values
                    continue
                if size is None or len(parts) != 3:
                    raise ValueError("فقط LUT سه‌بعدی .cube پشتیبانی می‌شود")
                values = [float(v) for v in parts]
                if not all(isfinite(v) and 0 <= v <= 1 for v in values):
                    raise ValueError("دادهٔ LUT باید بین صفر و یک باشد")
                samples += 1
                if samples > size**3:
                    raise ValueError("تعداد نمونه‌های LUT")
        if size is None or samples != size**3:
            raise ValueError("تعداد نمونه‌های LUT با اندازه مطابقت ندارد")
        if not all(a < b for a, b in zip(domain["DOMAIN_MIN"], domain["DOMAIN_MAX"], strict=True)):
            raise ValueError("دامنهٔ LUT معتبر نیست")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"فایل LUT معتبر نیست: {exc}") from exc
    return size
