"""Generate a reproducible preview of PixelCut's local color looks, without Telegram."""

import asyncio
from pathlib import Path

from movie_editor.config import Config
from movie_editor.domain import Draft
from movie_editor.media.gallery import FilterGallery
from movie_editor.media.process import capture
from movie_editor.storage import Store


async def main():
    root = Path(__file__).resolve().parents[1]
    config = Config.from_env(require_telegram=False)
    config.prepare()
    folder = config.data_dir / "filter-example"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "source.mkv"
    await capture(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x360:r=30",
            "-t",
            "1",
            "-c:v",
            "ffv1",
            "-threads",
            str(config.threads),
            str(source),
        ]
    )
    store = Store(folder / "demo.sqlite3")
    try:
        result = await FilterGallery(config, store).render(
            Draft("demo", str(source), "source.mkv"), folder / "gallery"
        )
        destination = root / "examples" / "local-filters.png"
        destination.write_bytes(result.path.read_bytes())
        print(destination)
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
