"""Build a single FFmpeg filter graph with intentional frame-timing policies."""

from dataclasses import dataclass
from pathlib import Path

from ..domain import Draft, Join, Quality, Transition
from .formats import PixelLayout, layout_for
from .graphics import Overlay
from .probe import MediaInfo


@dataclass(frozen=True)
class Segment:
    path: Path
    info: MediaInfo
    start: float = 0
    end: float | None = None

    @property
    def duration(self) -> float:
        return (self.end if self.end is not None else self.info.duration) - self.start


@dataclass(frozen=True)
class Graph:
    filters: str
    video: str
    audio: str | None
    copy_audio: bool
    duration: float
    normalize_fps: bool
    width: int
    height: int
    pixel_format: str


def build_graph(draft: Draft, segments: list[Segment], overlays: list[Overlay]) -> Graph:
    main_index = 1 if draft.intro_id else 0
    main = segments[main_index]
    lossless = draft.settings.quality == Quality.LOSSLESS
    layout = layout_for(main.info) if lossless else PixelLayout("yuv444p", "yuv420p", "yuv444")
    gx = layout.horizontal_grid if lossless else 2
    gy = layout.vertical_grid if lossless else 2
    width = main.info.width + (-main.info.width % gx)
    height = main.info.height + (-main.info.height % gy)
    joins: list[Join] = []
    if draft.intro_id:
        joins.append(draft.settings.intro_join)
    if draft.outro_id:
        joins.append(draft.settings.outro_join)
    normalize = any(join.kind != Transition.CUT for join in joins)
    fps = main.info.fps
    filters: list[str] = []
    total = main.duration if len(segments) == 1 else segments[0].duration
    copy_audio = (
        len(segments) == 1
        and not draft.mute
        and draft.trim_start == 0
        and draft.trim_end is None
        and main.duration == main.info.duration
        and main.info.audio_codec is not None
        and abs(main.info.audio_start - main.info.video_start) < 0.001
    )
    filtered_audio = not draft.mute and not copy_audio and any(s.info.audio_codec for s in segments)
    for i, segment in enumerate(segments):
        trim = f"trim=start={segment.start:.9f}:duration={segment.duration:.9f},"
        chain = f"[{i}:{segment.info.video_index}]{trim}setpts=PTS-STARTPTS"
        if i != main_index:
            chain += (
                f",scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos"
                f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
        elif width != main.info.width or height != main.info.height:
            chain += f",pad={width}:{height}:0:0:color=black"
        chain += ",setsar=1"
        if normalize:
            chain += f",fps={fps},settb=AVTB"
        else:
            chain += ",settb=AVTB"
        chain += f",format={layout.working}[v{i}]"
        filters.append(chain)
        if filtered_audio:
            if segment.info.audio_codec:
                # Align the audio start against the corresponding video timeline before trim.
                delta = segment.info.audio_start - segment.info.video_start
                audio = (
                    f"[{i}:{segment.info.audio_index}]asetpts=PTS-STARTPTS+{delta:.9f}/TB,"
                    "aresample=48000:async=1:first_pts=0,"
                    "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"apad,atrim=start={segment.start:.9f}:duration={segment.duration:.9f},"
                    f"asetpts=PTS-STARTPTS[a{i}]"
                )
            else:
                audio = (
                    "anullsrc=r=48000:cl=stereo,"
                    f"atrim=duration={segment.duration:.9f},asetpts=PTS-STARTPTS[a{i}]"
                )
            filters.append(audio)
    video, audio = "v0", "a0" if filtered_audio else None
    for i, join in enumerate(joins, start=1):
        new_video, new_audio = f"joinv{i}", f"joina{i}"
        if join.kind == Transition.CUT:
            normalize_after = f",fps={fps},settb=AVTB" if normalize else ""
            filters.append(f"[{video}][v{i}]concat=n=2:v=1:a=0{normalize_after}[{new_video}]")
            if audio:
                filters.append(f"[{audio}][a{i}]concat=n=2:v=0:a=1[{new_audio}]")
            total += segments[i].duration
        else:
            # The main clip may have two overlapping transitions: each uses < half a segment.
            duration = min(join.seconds, segments[i - 1].duration / 2, segments[i].duration / 2)
            if duration < 1 / float(fps):
                raise ValueError(
                    "کلیپ برای ترنزیشن بیش از حد کوتاه است؛ اتصال ساده را انتخاب کنید."
                )
            offset = total - duration
            filters.append(
                f"[{video}][v{i}]xfade=transition={join.kind.value}:"
                f"duration={duration:.9f}:offset={offset:.9f}[{new_video}]"
            )
            if audio:
                filters.append(
                    f"[{audio}][a{i}]acrossfade=d={duration:.9f}:c1=tri:c2=tri[{new_audio}]"
                )
            total += segments[i].duration - duration
        video = new_video
        if audio:
            audio = new_audio
    for i, overlay in enumerate(overlays):
        input_index = len(segments) + i
        output = f"overlay{i}"
        if overlay.dynamic:
            # Average luma of the rectangle under the text; invert black/white each frame.
            filters.extend(
                [
                    f"[{video}]split=2[canvas{i}][sample{i}]",
                    f"[sample{i}]crop={overlay.width}:{overlay.height}:"
                    f"{overlay.x}:{overlay.y}:exact=1,"
                    "format=gray,scale=1:1:flags=area,"
                    "lut=y='if(gt(val,128),0,255)',"
                    f"scale={overlay.width}:{overlay.height}:flags=neighbor,format=rgb24[color{i}]",
                    f"[{input_index}:v]format=rgba,alphaextract[mask{i}]",
                    f"[color{i}][mask{i}]alphamerge=shortest=0:repeatlast=1[stamp{i}]",
                    f"[canvas{i}][stamp{i}]overlay=x={overlay.x}:y={overlay.y}:"
                    f"eof_action=repeat:repeatlast=1:format={layout.overlay}[{output}]",
                ]
            )
        else:
            filters.append(
                f"[{video}][{input_index}:v]overlay=x={overlay.x}:y={overlay.y}:"
                f"eof_action=repeat:repeatlast=1:format={layout.overlay}[{output}]"
            )
        video = output
    return Graph(
        ";\n".join(filters),
        video,
        audio,
        copy_audio,
        total,
        normalize,
        width,
        height,
        layout.encoded,
    )
