"""RGB color operations; named DNT profiles require a supplied reference LUT."""

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..domain import ColorGrade, Look
from ..storage import Store


@dataclass(frozen=True)
class Recipe:
    saturation: float = 1
    contrast: float = 1
    brightness: float = 0
    gamma: float = 1
    warmth: float = 0
    fade: float = 0
    sepia: bool = False


RECIPES = {
    Look.NONE: Recipe(),
    Look.BW: Recipe(saturation=0),
    Look.WARM: Recipe(warmth=0.1, saturation=1.08),
    Look.COOL: Recipe(warmth=-0.1, saturation=0.9),
    Look.SEPIA: Recipe(sepia=True),
    Look.VINTAGE: Recipe(saturation=0.75, warmth=0.06, fade=0.1, contrast=0.9),
    Look.CINEMA: Recipe(saturation=0.82, warmth=-0.045, contrast=1.18, gamma=1.05),
    Look.FADE: Recipe(fade=0.15, contrast=0.82, saturation=0.8),
    Look.VIVID: Recipe(saturation=1.4, contrast=1.1),
    Look.NOIR: Recipe(saturation=0, contrast=1.35, brightness=-0.025),
}


def stage_lut(grade: ColorGrade, store: Store, folder: Path) -> None:
    if grade.active and grade.look.needs_lut and grade.strength > 0:
        asset = store.asset(grade.lut_id, "lut")
        shutil.copyfile(asset.path, folder / "look.cube")


def _tone(brightness: float, contrast: float, gamma: float) -> str:
    expression = (
        f"clip(255*((pow(val/255,{1 / gamma:.9f})-0.5)*{contrast:.9f}+0.5+{brightness:.9f}),0,255)"
    )
    return "lutrgb=" + ":".join(f"{c}='{expression}'" for c in "rgb")


def _saturation(value: float) -> str:
    weights = (0.2126, 0.7152, 0.0722)
    return "colorchannelmixer=" + ":".join(
        f"{row}{col}={((value if i == j else 0) + (1 - value) * weights[j]):.9f}"
        for i, row in enumerate("rgb")
        for j, col in enumerate("rgb")
    )


def _warmth(value: float) -> str:
    return "colorbalance=" + ":".join(
        f"{channel}{region}={shift:.9f}"
        for channel, shift in (("r", value), ("b", -value))
        for region in "smh"
    )


def _corrections(grade: ColorGrade) -> list[str]:
    filters = []
    if (grade.brightness, grade.contrast, grade.gamma) != (0, 100, 1):
        filters.append(_tone(grade.brightness / 400, grade.contrast / 100, grade.gamma))
    if grade.saturation != 100:
        filters.append(_saturation(grade.saturation / 100))
    if grade.temperature:
        filters.append(_warmth(grade.temperature / 100 * 0.12))
    if grade.vignette:
        filters.append(f"vignette=angle={grade.vignette / 100 * 0.65:.9f}")
    return filters


def grade_graph(grade: ColorGrade, source: str, output: str, prefix: str = "grade") -> str:
    """Build safe filter strings exclusively from validated enums/numbers."""
    grade.validate()
    if not grade.active:
        return f"[{source}]null[{output}]"
    alpha = grade.strength / 100
    filters = []
    current = source
    if grade.look.needs_lut and alpha > 0:
        if alpha == 1:
            filters.append(
                f"[{current}]format=gbrp,lut3d=file=look.cube:interp=tetrahedral[{prefix}lut]"
            )
        else:
            filters.extend(
                [
                    f"[{current}]format=gbrp,split[{prefix}base][{prefix}input]",
                    f"[{prefix}input]lut3d=file=look.cube:interp=tetrahedral[{prefix}effect]",
                    f"[{prefix}base][{prefix}effect]blend="
                    f"all_expr='A*{1 - alpha:.9f}+B*{alpha:.9f}'"
                    f"[{prefix}lut]",
                ]
            )
        current = f"{prefix}lut"
        operations = []
    else:
        recipe = RECIPES.get(grade.look, Recipe())
        operations = []
        saturation = 1 + (recipe.saturation - 1) * alpha
        contrast = 1 + (recipe.contrast - 1) * alpha
        gamma = 1 + (recipe.gamma - 1) * alpha
        brightness = recipe.brightness * alpha
        if (brightness, contrast, gamma) != (0, 1, 1):
            operations.append(_tone(brightness, contrast, gamma))
        if saturation != 1:
            operations.append(_saturation(saturation))
        if recipe.warmth:
            operations.append(_warmth(recipe.warmth * alpha))
        if recipe.fade:
            lift = recipe.fade * alpha
            operations.append(f"curves=all='0/{lift:.9f} 1/{1 - lift * 0.3:.9f}'")
        if recipe.sepia:
            matrix = ((0.393, 0.769, 0.189), (0.349, 0.686, 0.168), (0.272, 0.534, 0.131))
            operations.append(
                "colorchannelmixer="
                + ":".join(
                    f"{r}{c}={((1 - alpha if i == j else 0) + alpha * matrix[i][j]):.9f}"
                    for i, r in enumerate("rgb")
                    for j, c in enumerate("rgb")
                )
            )
    operations.extend(_corrections(grade))
    filters.append(f"[{current}]format=gbrp,{','.join(operations + ['format=bgr0'])}[{output}]")
    return ";\n".join(filters)
