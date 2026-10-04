"""Cancellable subprocess execution without a shell."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

Progress = Callable[[float], Awaitable[None]]


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


async def capture(args: list[str], timeout: float = 60) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
    args: list[str], log_path: Path, duration: float, timeout: float, progress: Progress | None
) -> None:
    # A disk log prevents an undrained stderr pipe from deadlocking a long render.
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=log
        )
        try:
            async with asyncio.timeout(timeout):
                assert process.stdout is not None
                async for line in process.stdout:
                    key, _, value = line.decode(errors="replace").strip().partition("=")
                    if key == "out_time_us" and progress and value.lstrip("-").isdigit():
                        await progress(min(99, max(0, int(value) / 1e6 / duration * 100)))
                await process.wait()
            if process.returncode:
                raise ValueError("FFmpeg نتوانست خروجی بسازد؛ جزئیات در ffmpeg.log ثبت شد.")
        finally:
            await stop(process)
