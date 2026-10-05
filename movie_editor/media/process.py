"""Cancellable subprocess execution without a shell."""

import asyncio
import json
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .resources import resident_bytes

Progress = Callable[[float], Awaitable[None]]
Stage = Callable[[str], Awaitable[None]]


async def stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.kill()
            await process.wait()


def command(args: list[str]) -> list[str]:
    program = shutil.which(args[0])
    return [str(Path(program).resolve()) if program else args[0], *args[1:]]


async def capture(args: list[str], timeout: float = 60, *, cwd: Path | None = None) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *command(args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
        if process.returncode:
            raise ValueError(
                "فایل رسانه قابل خواندن نیست: " + stderr.decode(errors="replace")[-1200:]
            )
        return stdout
    finally:
        await stop(process)


async def encode(
    args: list[str],
    log_path: Path,
    duration: float,
    timeout: float,
    progress: Progress | None,
    *,
    cwd: Path | None = None,
    memory_mb: int | None = None,
    watchdog: Callable[[], None] | None = None,
) -> None:
    # A disk log prevents an undrained stderr pipe from deadlocking a long render.
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *command(args),
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
            cwd=cwd,
        )
        started, peak = time.monotonic(), 0
        finished = asyncio.Event()

        async def drain() -> None:
            try:
                assert process.stdout is not None
                async for line in process.stdout:
                    key, _, value = line.decode(errors="replace").strip().partition("=")
                    if key == "out_time_us" and progress and value.lstrip("-").isdigit():
                        await progress(min(99, max(0, int(value) / 1e6 / duration * 100)))
                await process.wait()
            finally:
                finished.set()

        async def watch() -> None:
            nonlocal peak
            while not finished.is_set():
                peak = max(peak, resident_bytes(process.pid) or 0)
                if memory_mb is not None and peak > memory_mb * 1024**2:
                    raise ValueError(
                        f"مصرف RAM رندر از {memory_mb} MiB گذشت؛ "
                        "برای حفظ ربات، فقط FFmpeg متوقف شد. کیفیت پایین آورده نشد. "
                        "جزئیات در ffmpeg.metrics.json است."
                    )
                if watchdog:
                    watchdog()
                try:
                    await asyncio.wait_for(finished.wait(), timeout=0.5)
                except TimeoutError:
                    pass

        tasks = [asyncio.create_task(drain()), asyncio.create_task(watch())]
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks)
            if process.returncode:
                raise ValueError(failure_message(log_path, process.returncode))
        finally:
            for task in tasks:
                task.cancel()
            await stop(process)
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                log_path.with_suffix(".metrics.json").write_text(
                    json.dumps(
                        {
                            "returncode": process.returncode,
                            "peak_rss_bytes": peak,
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "memory_limit_mb": memory_mb,
                        }
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass  # Diagnostics must not mask the original disk/process failure.


def failure_message(log_path: Path, returncode: int) -> str:
    if returncode >= 2**31:
        returncode -= 2**32  # Windows exposes a negative FFmpeg exit code as an unsigned DWORD.
    with log_path.open("rb") as source:
        source.seek(max(0, log_path.stat().st_size - 8192))
        tail = source.read().decode(errors="replace").strip()
    if returncode in {-9, 137}:
        reason = "FFmpeg با SIGKILL متوقف شد؛ احتمال کمبود RAM یا توقف توسط مدیر سرویس وجود دارد."
    elif "Cannot allocate memory" in tail or "Error allocating" in tail:
        reason = "FFmpeg نتوانست حافظه تخصیص دهد؛ RAM در دسترس کافی نبود."
    elif "No space left" in tail or "Disk quota exceeded" in tail:
        reason = "فضای دیسک یا سهمیهٔ ذخیره‌سازی برای خروجی کافی نبود."
    else:
        reason = "FFmpeg نتوانست خروجی بسازد."
    return f"{reason} کد خروج: {returncode}. جزئیات در ffmpeg.log است.\n{tail[-800:]}"
