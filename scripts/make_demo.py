"""Build a small local transition preview without using Telegram credentials."""

import asyncio
import tempfile
from pathlib import Path

from movie_editor.config import Config
from movie_editor.domain import Asset, Color, Draft, Position, Quality, Transition
from movie_editor.media.process import capture
from movie_editor.media.renderer import Renderer
from movie_editor.storage import Store


async def main() -> None:
    config = Config.from_env(require_telegram=False)
    config.prepare()
    output_dir = Path(__file__).resolve().parents[1] / "examples"
    output_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="movie_editor_demo_") as temporary:
        work = Path(temporary)
        for name, source in (
            ("intro", "color=c=0x183a64:s=640x360:r=30"),
            ("main", "testsrc2=s=640x360:r=30"),
            ("outro", "color=c=0x432954:s=640x360:r=30"),
        ):
            await capture(
                [
                    config.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    source,
                    "-t",
                    "1.4",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-threads",
                    "2",
                    str(work / f"{name}.mp4"),
                ]
            )
        store = Store(work / "library.sqlite3")
        try:
            for name in ("intro", "outro"):
                store.add_asset(Asset(name, name, name, str(work / f"{name}.mp4")))
            renderer = Renderer(config, store)
            parts = []
            labels = {
                Transition.SHADOW: "Shadow / Fade through black",
                Transition.FADE: "Soft fade",
                Transition.DISSOLVE: "Dissolve",
                Transition.SLIDE: "Smooth slide left",
                Transition.CIRCLE: "Circle open",
                Transition.PIXEL: "Pixelize",
            }
            for transition, label in labels.items():
                draft = Draft(
                    transition.value,
                    str(work / "main.mp4"),
                    "demo.mp4",
                    text=label,
                    intro_id="intro",
                    outro_id="outro",
                )
                draft.settings.quality = Quality.FAST
                draft.settings.text_style.width_percent = 65
                draft.settings.text_style.position = Position.TOP_CENTER
                draft.settings.text_style.color = Color.WHITE
                draft.settings.intro_join.kind = draft.settings.outro_join.kind = transition
                draft.settings.intro_join.seconds = draft.settings.outro_join.seconds = 0.4
                result = await renderer.render(draft, work / transition.value)
                parts.append(result.path)
            listing = work / "concat.txt"
            listing.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")
            await capture(
                [
                    config.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(listing),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(output_dir / "transition-demo.mp4"),
                ]
            )
            print(output_dir / "transition-demo.mp4")
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
