#!/usr/bin/env python3
# src/minecraft/generate_drawers.py

r"""Generate Storage Drawers wooden front textures and metadata.

The drawer face is a fixed 16x16 template of (palette_role, scale, bias)
derived from the six vanilla woods. Rendering a new wood is a lookup
and a per-channel affine apply:

    front[(x, y)][c] = round(scale[c] * palette[role][c] + bias[c])

The per-channel scale and bias are fit from all six vanilla woods
simultaneously, so they are wood-independent by construction and
generalize to new woods. Interior pixels fit scale=(1,1,1) and
bias=(0,0,0); structural pixels (drawer split lines, bevel seams) get
their own per-channel affine that captures detail the scalar-shade
model cannot represent.

The outer 1px ring is a geometry inset and is not rendered in-game.
Those 60 pixels are per-wood in the source assets and are not scored.
Only the 196 visible pixels per front are evaluated.

Primary usage (generate a new wood):

    pdm run python src/minecraft/generate_drawers.py \
        --input-jar sync/downloads/BiomesOPlenty-forge-1.20.1-19.0.0.96.jar \
        --wood-type biomesoplenty:maple \
        --output-root sync/kubejs

Diagnostics:

    pdm run python src/minecraft/generate_drawers.py self-test
    pdm run python src/minecraft/generate_drawers.py validate

Flags:

    --input-jar       Mod jar providing the wood's plank texture (required).
    --wood-type       namespace:wood_name (e.g. biomesoplenty:maple, required).
    --output-root     KubeJS root. Default: sync/kubejs
    --storage-drawers Storage Drawers jar file, or a directory containing
                      StorageDrawers-*.jar. Default: sync/downloads
    --client-jar      Minecraft 1.20.1 client jar. Default: gradle cache.
    --repo-root       Repository root. Default: grandparent of this file.

Generated files per wood (25 total):

    assets/storagedrawersextra/textures/block/<namespace>/drawers_<wood>_front_{1,2,4}.png   (3)
    assets/storagedrawersextra/blockstates/<prefix>_{full,half}_drawers_{1,2,4}.json
        + <prefix>_trim.json                                                                 (7)
    assets/storagedrawersextra/models/block/<prefix>_{full,half}_drawers_{1,2,4}.json
        + <prefix>_trim.json                                                                 (7)
    assets/storagedrawersextra/models/item/<prefix>_{full,half}_drawers_{1,2,4}.json
        + <prefix>_trim.json                                                                 (7)
    startup_scripts/060_integration/000_storage_drawers/<prefix>.js                          (1)

where <prefix> = <namespace>_<wood>.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import statistics
import sys
import zipfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - environment dependent
    raise SystemExit("Pillow is required. Install it in your existing Python environment, for example with: python -m pip install Pillow") from exc


VANILLA_WOODS = (
    "oak",
    "spruce",
    "birch",
    "jungle",
    "acacia",
    "dark_oak",
)
FRONT_SHAPES = (1, 2, 4)
TEXTURE_SIZE = (16, 16)
HANDLE_SCALE_THRESHOLD = 0.5
VISIBLE_PIXELS_PER_FRONT = 196

DEFAULT_STORAGE_DRAWERS_DIR = "sync/downloads"
DEFAULT_STORAGE_DRAWERS_PATTERN = "StorageDrawers-*.jar"
DEFAULT_OUTPUT_ROOT = "sync/kubejs"

# Asset subpaths under --output-root.
ASSET_NAMESPACE = "storagedrawersextra"
TEXTURES_SUBPATH = ("assets", ASSET_NAMESPACE, "textures", "block")
BLOCKSTATES_SUBPATH = ("assets", ASSET_NAMESPACE, "blockstates")
MODELS_BLOCK_SUBPATH = ("assets", ASSET_NAMESPACE, "models", "block")
MODELS_ITEM_SUBPATH = ("assets", ASSET_NAMESPACE, "models", "item")
STARTUP_SCRIPTS_SUBPATH = (
    "startup_scripts",
    "060_integration",
    "000_storage_drawers",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateModel:
    """Template model for one drawer front size.

    Model: front[c] = round(scale[c] * palette[role][c] + bias[c])

    role_map is 16x16 ints in 0..6.
    scale_map is 16x16 3-tuples (scale_r, scale_g, scale_b).
    bias_map is 16x16 3-tuples (bias_r, bias_g, bias_b).
    rmse_map is 16x16 floats.
    """

    width: int
    height: int
    role_map: tuple[tuple[int, ...], ...]
    scale_map: tuple[tuple[tuple[float, float, float], ...], ...]
    bias_map: tuple[tuple[tuple[float, float, float], ...], ...]
    rmse_map: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class ValidationMetrics:
    """Validation measurements for one predicted texture."""

    exact_pixels: int
    total_pixels: int
    exact_ratio: float
    mae: float
    rmse: float
    max_error: float
    p95_error: float


# ---------------------------------------------------------------------------
# Asset archive
# ---------------------------------------------------------------------------


class AssetArchive:
    """Read PNG resources from a Minecraft/mod JAR without extracting it."""

    def __init__(self, path: Path):
        self.path = path
        self.archive = zipfile.ZipFile(path)
        self.names = tuple(self.archive.namelist())

    def close(self) -> None:
        """Close the underlying ZIP archive."""
        self.archive.close()

    def __enter__(self) -> AssetArchive:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def read_image(self, resource_path: str) -> Image.Image:
        """Read a PNG resource and return it as RGBA."""
        try:
            data = self.archive.read(resource_path)
        except KeyError as exc:
            raise FileNotFoundError(f"Resource '{resource_path}' was not found in {self.path}") from exc
        image = Image.open(io.BytesIO(data)).convert("RGBA")
        if image.size != TEXTURE_SIZE:
            raise ValueError(f"Expected 16x16 texture, got {image.size}: {self.path}!{resource_path}")
        return image


# ---------------------------------------------------------------------------
# Basic pixel helpers
# ---------------------------------------------------------------------------


def image_pixels(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Return RGBA pixels in row-major order."""
    return list(image.get_flattened_data())


def luminance(pixel: tuple[int, int, int, int]) -> float:
    """Perceptual luminance of an RGBA pixel."""
    return 0.2126 * pixel[0] + 0.7152 * pixel[1] + 0.0722 * pixel[2]


def extract_wood_palette(image: Image.Image, expected: int = 7) -> list[tuple[int, int, int, int]]:
    """Return the wood's dominant palette sorted by luminance (darkest first)."""
    counts = Counter(image.get_flattened_data())
    top = [color for color, _ in counts.most_common(expected)]
    top.sort(key=lambda c: (luminance(c), c[0], c[1], c[2]))
    return top


# ---------------------------------------------------------------------------
# Resource discovery
# ---------------------------------------------------------------------------


def find_resource_by_fragments(
    names: Iterable[str],
    required_fragments: Iterable[str],
    suffix: str = ".png",
) -> list[str]:
    """Find archive resources containing every requested fragment."""
    fragments = tuple(fragment.lower() for fragment in required_fragments)
    results: list[str] = []
    for name in names:
        lower = name.lower()
        if not lower.endswith(suffix):
            continue
        if all(fragment in lower for fragment in fragments):
            results.append(name)
    return sorted(results)


def locate_vanilla_source_resources(archive: AssetArchive) -> dict[str, str]:
    """Locate the six vanilla plank textures in the Minecraft client jar."""
    resources: dict[str, str] = {}
    for wood in VANILLA_WOODS:
        expected = f"assets/minecraft/textures/block/{wood}_planks.png"
        if expected in archive.names:
            resources[wood] = expected
            continue

        matches = find_resource_by_fragments(
            archive.names,
            ("assets/minecraft/textures/block", f"{wood}_planks"),
        )
        if len(matches) == 1:
            resources[wood] = matches[0]
            continue

        raise FileNotFoundError(f"Could not uniquely locate vanilla {wood} plank texture in {archive.path}")
    return resources


def locate_vanilla_front_resources(archive: AssetArchive, shape: int) -> dict[str, str]:
    """Locate vanilla Storage Drawers front textures for one drawer shape.

    Uses exact-path matching so that a short wood name such as ``oak``
    cannot accidentally match a longer wood name such as ``dark_oak``.
    """
    resources: dict[str, str] = {}
    for wood in VANILLA_WOODS:
        expected = f"assets/storagedrawers/textures/block/drawers_{wood}_front_{shape}.png"
        if expected in archive.names:
            resources[wood] = expected
            continue

        exact_name = f"drawers_{wood}_front_{shape}.png"
        exact = [name for name in archive.names if name.lower().endswith(f"/{exact_name}")]
        if len(exact) == 1:
            resources[wood] = exact[0]
            continue

        raise FileNotFoundError(
            f"Could not uniquely locate Storage Drawers {wood} front_{shape} texture.\nExpected: {expected}\nBasename candidates: {exact[:20]}"
        )
    return resources


def locate_wood_planks(archive: AssetArchive, namespace: str, wood: str) -> str:
    """Locate ``assets/<namespace>/textures/block/<wood>_planks.png``.

    Case-insensitive. Raises FileNotFoundError if the resource is
    missing, or if the found path does not have the expected shape.
    """
    expected = f"assets/{namespace}/textures/block/{wood}_planks.png"
    if expected in archive.names:
        return expected

    target = f"textures/block/{wood}_planks.png".lower()
    candidates = [name for name in archive.names if name.lower().endswith(target) and name.lower().startswith(f"assets/{namespace.lower()}/")]
    if not candidates:
        raise FileNotFoundError(f"Could not find '{expected}' in {archive.path}")

    candidates.sort(key=len)
    return candidates[0]


# ---------------------------------------------------------------------------
# Template fitting
# ---------------------------------------------------------------------------


def fit_channel_affine(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Fit y ~= slope * x + intercept by ordinary least squares.

    Returns (slope, intercept). When the x values have zero variance,
    returns (0.0, mean(y)) so the model degenerates to a constant.
    """
    n = len(xs)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys, strict=True))

    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) > 1e-9:
        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n
    else:
        slope = 0.0
        intercept = sum_y / n

    return slope, intercept


def fit_template_pixel(
    front_rgbs: list[tuple[int, int, int]],
    palettes: list[list[tuple[int, int, int, int]]],
) -> tuple[int, tuple[float, float, float], tuple[float, float, float], float]:
    """Fit (palette_role, per-channel scale, per-channel bias) for one pixel.

    Model: front[c] = scale[c] * palette[role][c] + bias[c]

    For each role in the palette, fit per-channel affine by ordinary
    least squares across all training woods simultaneously, then pick
    the role with the lowest total squared error.

    Returns (role, (scale_r, scale_g, scale_b), (bias_r, bias_g, bias_b),
    total_squared_error).
    """
    max_roles = min(len(palette) for palette in palettes)

    best_role = 0
    best_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    best_bias: tuple[float, float, float] = (0.0, 0.0, 0.0)
    best_error: float | None = None

    for role in range(max_roles):
        scales: list[float] = [1.0, 1.0, 1.0]
        biases: list[float] = [0.0, 0.0, 0.0]

        for c in range(3):
            xs = [palette[role][c] for palette in palettes]
            ys = [target[c] for target in front_rgbs]
            slope, intercept = fit_channel_affine(xs, ys)
            scales[c] = slope
            biases[c] = intercept

        error = 0.0
        for target, palette in zip(front_rgbs, palettes, strict=True):
            src = palette[role]
            for c in range(3):
                pred = scales[c] * src[c] + biases[c]
                diff = pred - target[c]
                error += diff * diff

        if best_error is None or error < best_error:
            best_error = error
            best_role = role
            best_scale = (scales[0], scales[1], scales[2])
            best_bias = (biases[0], biases[1], biases[2])

    if best_error is None:
        best_error = 0.0

    return best_role, best_scale, best_bias, best_error


def fit_template_from_images(
    source_images: dict[str, Image.Image],
    target_images: dict[str, Image.Image],
) -> TemplateModel:
    """Fit the 16x16 (role, scale, bias) template from paired training data."""
    woods = tuple(target_images.keys())
    palettes = [extract_wood_palette(source_images[w]) for w in woods]

    width, height = TEXTURE_SIZE
    role_map = [[0] * width for _ in range(height)]
    scale_map = [[(1.0, 1.0, 1.0) for _ in range(width)] for _ in range(height)]
    bias_map = [[(0.0, 0.0, 0.0) for _ in range(width)] for _ in range(height)]
    rmse_map = [[0.0] * width for _ in range(height)]

    for y in range(height):
        for x in range(width):
            front_rgbs = [target_images[w].getpixel((x, y))[:3] for w in woods]
            role, scale, bias, sq_err = fit_template_pixel(front_rgbs, palettes)
            role_map[y][x] = role
            scale_map[y][x] = scale
            bias_map[y][x] = bias
            rmse_map[y][x] = (sq_err / (len(woods) * 3)) ** 0.5

    return TemplateModel(
        width=width,
        height=height,
        role_map=tuple(tuple(row) for row in role_map),
        scale_map=tuple(tuple(row) for row in scale_map),
        bias_map=tuple(tuple(row) for row in bias_map),
        rmse_map=tuple(tuple(row) for row in rmse_map),
    )


def render_template(model: TemplateModel, palette: list[tuple[int, int, int, int]]) -> Image.Image:
    """Render a 16x16 front from a template and a target wood's palette."""
    output = Image.new("RGBA", TEXTURE_SIZE)
    out_pixels: list[tuple[int, int, int, int]] = []
    for y in range(model.height):
        for x in range(model.width):
            role = model.role_map[y][x]
            scale = model.scale_map[y][x]
            bias = model.bias_map[y][x]
            src = palette[role]
            r = max(0, min(255, round(scale[0] * src[0] + bias[0])))
            g = max(0, min(255, round(scale[1] * src[1] + bias[1])))
            b = max(0, min(255, round(scale[2] * src[2] + bias[2])))
            out_pixels.append((r, g, b, 255))
    output.putdata(out_pixels)
    return output


def template_metrics(model: TemplateModel) -> dict[str, float | int]:
    """Summarize the fit quality of a learned template."""
    rmses = [model.rmse_map[y][x] for y in range(model.height) for x in range(model.width)]
    max_abs_bias = 0.0
    max_abs_scale_delta = 0.0
    for y in range(model.height):
        for x in range(model.width):
            scale = model.scale_map[y][x]
            bias = model.bias_map[y][x]
            for c in range(3):
                bias_value = abs(bias[c])
                if bias_value > max_abs_bias:
                    max_abs_bias = bias_value
                scale_delta = abs(scale[c] - 1.0)
                if scale_delta > max_abs_scale_delta:
                    max_abs_scale_delta = scale_delta
    return {
        "pixels": len(rmses),
        "rmse_le_0_1": sum(r <= 0.1 for r in rmses),
        "rmse_le_0_5": sum(r <= 0.5 for r in rmses),
        "rmse_le_1_0": sum(r <= 1.0 for r in rmses),
        "rmse_le_2_0": sum(r <= 2.0 for r in rmses),
        "rmse_le_3_0": sum(r <= 3.0 for r in rmses),
        "rmse_le_5_0": sum(r <= 5.0 for r in rmses),
        "training_mean_pixel_rmse": statistics.mean(rmses),
        "training_max_pixel_rmse": max(rmses),
        "max_abs_bias": max_abs_bias,
        "max_abs_scale_delta": max_abs_scale_delta,
    }


def validation_metrics(actual: Image.Image, predicted: Image.Image) -> ValidationMetrics:
    """Compare two 16x16 textures using RGB absolute and squared error."""
    actual_pixels = image_pixels(actual)
    predicted_pixels = image_pixels(predicted)
    if len(actual_pixels) != len(predicted_pixels):
        raise ValueError("Image pixel counts differ")

    errors: list[float] = []
    exact = 0
    for actual_pixel, predicted_pixel in zip(actual_pixels, predicted_pixels, strict=True):
        if actual_pixel[:3] == predicted_pixel[:3]:
            exact += 1
        channel_squared = 0.0
        for channel in range(3):
            difference = float(actual_pixel[channel]) - float(predicted_pixel[channel])
            channel_squared += difference * difference
        errors.append(math.sqrt(channel_squared / 3.0))

    mae = statistics.mean(errors)
    rmse = math.sqrt(statistics.mean(error * error for error in errors))
    ordered = sorted(errors)
    p95_index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return ValidationMetrics(
        exact_pixels=exact,
        total_pixels=len(errors),
        exact_ratio=exact / len(errors),
        mae=mae,
        rmse=rmse,
        max_error=max(errors),
        p95_error=ordered[p95_index],
    )


def leave_one_out_template_validation(
    source_images: dict[str, Image.Image],
    target_images: dict[str, Image.Image],
) -> dict[str, ValidationMetrics]:
    """Leave-one-out validation for the template model."""
    results: dict[str, ValidationMetrics] = {}
    original_woods = tuple(VANILLA_WOODS)

    for holdout in original_woods:
        training_woods = tuple(wood for wood in original_woods if wood != holdout)
        train_sources = {wood: source_images[wood] for wood in training_woods}
        train_targets = {wood: target_images[wood] for wood in training_woods}

        model = fit_template_from_images(train_sources, train_targets)
        palette = extract_wood_palette(source_images[holdout])
        predicted = render_template(model, palette)
        results[holdout] = validation_metrics(target_images[holdout], predicted)

    return results


# ---------------------------------------------------------------------------
# Band classification (reporting)
# ---------------------------------------------------------------------------


def band_of(x: int, y: int, mean_scale: float) -> str:
    """Classify a pixel by its coordinate and mean fitted scale value.

    border     = outer 1px ring, not rendered in-game (geometry inset)
    handle     = deep recess pixels, identified by mean scale < threshold
    inner_ring = rows/cols 1 or 14, excluding handle
    interior   = everything else

    Using the fitted scale rather than hardcoded coordinates makes this
    classification correct for all three drawer front shapes, which have
    different handle positions.
    """
    if x == 0 or x == 15 or y == 0 or y == 15:
        return "border"
    if mean_scale < HANDLE_SCALE_THRESHOLD:
        return "handle"
    if x == 1 or x == 14 or y == 1 or y == 14:
        return "inner_ring"
    return "interior"


def mean_scale_of(model: TemplateModel, x: int, y: int) -> float:
    """Return the mean of the three per-channel scales at (x, y)."""
    scale = model.scale_map[y][x]
    return (scale[0] + scale[1] + scale[2]) / 3.0


def band_exact_summary(
    source_images: dict[str, Image.Image],
    target_images: dict[str, Image.Image],
    model: TemplateModel,
) -> dict[str, dict[str, tuple[int, int, float, float]]]:
    """Return {band: {wood: (exact, total, mae, rmse)}}."""
    woods = tuple(target_images.keys())
    palettes = {w: extract_wood_palette(source_images[w]) for w in woods}
    out: dict[str, dict[str, tuple[int, int, float, float]]] = {band: {} for band in ("interior", "inner_ring", "handle", "border")}

    for band, band_dict in out.items():
        for w in woods:
            exact = 0
            total = 0
            sum_abs = 0
            sum_sq = 0
            for y in range(16):
                for x in range(16):
                    mean_scale = mean_scale_of(model, x, y)
                    if band_of(x, y, mean_scale) != band:
                        continue
                    role = model.role_map[y][x]
                    scale = model.scale_map[y][x]
                    bias = model.bias_map[y][x]
                    src = palettes[w][role][:3]
                    pred = tuple(max(0, min(255, round(scale[c] * src[c] + bias[c]))) for c in range(3))
                    tgt = target_images[w].getpixel((x, y))[:3]
                    if pred == tgt:
                        exact += 1
                    for c in range(3):
                        d = pred[c] - tgt[c]
                        sum_abs += abs(d)
                        sum_sq += d * d
                    total += 1
            if total:
                mae = sum_abs / (total * 3)
                rmse = (sum_sq / (total * 3)) ** 0.5
            else:
                mae = 0.0
                rmse = 0.0
            band_dict[w] = (exact, total, mae, rmse)

    return out


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def load_vanilla_dataset(
    client_jar: Path,
    storage_drawers_jar: Path,
    shape: int,
) -> tuple[dict[str, Image.Image], dict[str, Image.Image], dict[str, str], dict[str, str]]:
    """Load one complete six-wood training dataset."""
    with AssetArchive(client_jar) as client, AssetArchive(storage_drawers_jar) as drawers:
        source_resources = locate_vanilla_source_resources(client)
        target_resources = locate_vanilla_front_resources(drawers, shape)
        source_images = {wood: client.read_image(resource) for wood, resource in source_resources.items()}
        target_images = {wood: drawers.read_image(resource) for wood, resource in target_resources.items()}
    return source_images, target_images, source_resources, target_resources


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def emit_template_summary(shape: int, model: TemplateModel) -> None:
    """Print a compact summary of a learned template."""
    summary = template_metrics(model)
    print(f"FRONT_{shape} (template, per-channel affine)")
    print(f"  training fit: {summary['rmse_le_0_1']}/256 <= 0.1 RMSE")
    print(f"  training fit: {summary['rmse_le_0_5']}/256 <= 0.5 RMSE")
    print(f"  training fit: {summary['rmse_le_3_0']}/256 <= 3.0 RMSE")
    print(f"  training mean pixel RMSE: {summary['training_mean_pixel_rmse']:.3f}")
    print(f"  training max pixel RMSE: {summary['training_max_pixel_rmse']:.3f}")
    print(f"  max |bias|: {summary['max_abs_bias']:.3f}")
    print(f"  max |scale - 1|: {summary['max_abs_scale_delta']:.3f}")

    mean_scales = [mean_scale_of(model, x, y) for y in range(16) for x in range(16)]
    rounded = Counter(round(s, 2) for s in mean_scales)
    print("  mean-scale distribution:")
    for value, count in sorted(rounded.items(), key=lambda item: (-item[1], item[0])):
        print(f"    {value:.2f}: {count:3d} pixels")


def emit_band_table(bands: dict[str, dict[str, tuple[int, int, float, float]]]) -> None:
    """Print per-band exact/MAE/RMSE for each wood."""
    print()
    print("  per-band exact pixels (opaque pixels only):")
    print(f"    {'band':<11s} {'wood':<10s} {'exact':>10s} {'mae':>7s} {'rmse':>7s}")
    for band in ("interior", "inner_ring", "handle", "border"):
        for wood, (exact, total, mae, rmse) in bands[band].items():
            ratio = f"{exact}/{total}"
            print(f"    {band:<11s} {wood:<10s} {ratio:>10s} {mae:>7.3f} {rmse:>7.3f}")
        print()


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------


def write_json(path: Path, data, indent: int = 4) -> None:
    """Write a dict to path as JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent) + "\n"
    path.write_text(text, encoding="utf-8")


def write_text_file(path: Path, text: str) -> None:
    """Write text to path with parent directories created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# JSON generation
# ---------------------------------------------------------------------------


def drawer_blockstate(model_id: str) -> dict:
    """Return blockstate JSON for a drawers block (4 cardinal facings)."""
    return {
        "variants": {
            "facing=north": {"model": model_id, "y": 0},
            "facing=east": {"model": model_id, "y": 90},
            "facing=south": {"model": model_id, "y": 180},
            "facing=west": {"model": model_id, "y": 270},
        }
    }


def trim_blockstate(model_id: str) -> dict:
    """Return blockstate JSON for the trim block (single variant)."""
    return {"variants": {"": {"model": model_id}}}


def full_drawer_model(namespace: str, wood: str, shape: int) -> dict:
    """Return block model JSON for a full-drawer variant."""
    return {
        "parent": "storagedrawers:block/full_drawers_orientable",
        "textures": {
            "front": f"storagedrawersextra:block/{namespace}/drawers_{wood}_front_{shape}",
            "side": f"{namespace}:block/{wood}_planks",
            "top": f"{namespace}:block/{wood}_planks",
            "trim": f"{namespace}:block/{wood}_planks",
        },
    }


def half_drawer_model(namespace: str, wood: str, shape: int) -> dict:
    """Return block model JSON for a half-drawer variant."""
    return {
        "parent": "storagedrawers:block/half_drawers_orientable",
        "textures": {
            "front": f"storagedrawersextra:block/{namespace}/drawers_{wood}_front_{shape}",
            "back": f"{namespace}:block/{wood}_planks",
            "side": f"{namespace}:block/{wood}_planks",
            "top": f"{namespace}:block/{wood}_planks",
            "trim": f"{namespace}:block/{wood}_planks",
        },
    }


def trim_model(namespace: str, wood: str) -> dict:
    """Return block model JSON for the trim block."""
    return {
        "parent": "minecraft:block/cube_all",
        "textures": {"all": f"{namespace}:block/{wood}_planks"},
    }


def item_model(block_model_id: str) -> dict:
    """Return item model JSON that inherits from a block model."""
    return {"parent": block_model_id}


# ---------------------------------------------------------------------------
# KubeJS startup script generation
# ---------------------------------------------------------------------------


def kubejs_startup_script(wood: str, prefix: str) -> str:
    """Return the KubeJS startup script that registers this wood variant.

    Registers the wood's drawer variant with StorageDrawersExtras through
    the ModBlockVariants API. The material ResourceLocation is
    ``storagedrawersextra:<prefix>``, where ``<prefix>`` matches the
    blockstate / model file prefix that the rest of this tool generates.

    Class paths must match the actual package structure:

        com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants
        com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants$VariantData
        com.jaquadro.minecraft.storagedrawersextra.core.ModBlocks
        com.jaquadro.minecraft.storagedrawersextra.core.ModItems
    """
    wood_display = " ".join(part.capitalize() for part in wood.split("_"))

    return f"""// kubejs/startup_scripts/060_integration/000_storage_drawers/{prefix}.js

const ResourceLocation = Java.loadClass('net.minecraft.resources.ResourceLocation');
const VariantData = Java.loadClass(
    'com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants$VariantData'
);
const ModBlockVariants = Java.loadClass(
    'com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants'
);
const ExtraBlocks = Java.loadClass(
    'com.jaquadro.minecraft.storagedrawersextra.core.ModBlocks'
);
const ExtraItems = Java.loadClass(
    'com.jaquadro.minecraft.storagedrawersextra.core.ModItems'
);

console.info('[SD {wood_display}] loading');

const material = new ResourceLocation(
    'storagedrawersextra',
    '{prefix}'
);

const data = new VariantData(material);

ModBlockVariants.registerVariant(
    ExtraBlocks.BLOCK_REGISTER,
    data
);

ModBlockVariants.registerVariantItem(
    ExtraItems.ITEM_REGISTER,
    data
);

console.info('[SD {wood_display}] registered blocks and items');
"""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_client_jar(arg: Path | None, repo_root: Path) -> Path:
    """Return the Minecraft client jar path."""
    if arg is None:
        return Path.home() / ".gradle" / "caches" / "forge_gradle" / "minecraft_repo" / "versions" / "1.20.1" / "client.jar"
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = repo_root / p
    return p


def resolve_storage_drawers_jar(arg: Path | None, repo_root: Path) -> Path:
    """Return the Storage Drawers jar path.

    Accepts either a jar file, or a directory containing a
    StorageDrawers-*.jar. Relative paths resolve against repo_root.
    """
    if arg is None:
        candidate = repo_root / DEFAULT_STORAGE_DRAWERS_DIR
    else:
        candidate = Path(arg).expanduser()
        if not candidate.is_absolute():
            candidate = repo_root / candidate

    if candidate.is_file():
        return candidate

    if candidate.is_dir():
        matches = sorted(candidate.glob(DEFAULT_STORAGE_DRAWERS_PATTERN))
        if not matches:
            raise FileNotFoundError(f"No {DEFAULT_STORAGE_DRAWERS_PATTERN} found in {candidate}")
        return matches[0]

    raise FileNotFoundError(f"Storage Drawers jar or directory not found: {candidate}")


def resolve_input_jar(arg: Path | None, repo_root: Path) -> Path:
    """Return the input mod jar path."""
    if arg is None:
        raise ValueError("--input-jar is required to generate a wood")
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = repo_root / p
    if not p.is_file():
        raise FileNotFoundError(f"Input jar not found: {p}")
    return p


def resolve_output_root(arg: Path | None, repo_root: Path) -> Path:
    """Return the KubeJS output root path."""
    if arg is None:
        return repo_root / DEFAULT_OUTPUT_ROOT
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = repo_root / p
    return p


def parse_wood_type(spec: str | None) -> tuple[str, str]:
    """Parse ``namespace:wood`` into (namespace, wood)."""
    if spec is None:
        raise ValueError("--wood-type is required to generate a wood")
    parts = spec.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"--wood-type must be 'namespace:wood' (e.g. biomesoplenty:maple); got {spec!r}")
    return parts[0].strip(), parts[1].strip()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    """Generate all assets and metadata for a new wood.

    Reads the wood's plank texture from --input-jar, fits the template
    model on the six vanilla woods, generates front_1/front_2/front_4
    PNGs, writes the JSON metadata, then writes the KubeJS startup
    script that registers the variant with StorageDrawersExtras. All
    output lands under --output-root.
    """
    repo_root = Path(args.repo_root).expanduser().resolve()
    client_jar = resolve_client_jar(args.client_jar, repo_root)
    storage_jar = resolve_storage_drawers_jar(args.storage_drawers, repo_root)
    input_jar = resolve_input_jar(args.input_jar, repo_root)
    output_root = resolve_output_root(args.output_root, repo_root)
    namespace, wood = parse_wood_type(args.wood_type)

    if not client_jar.is_file():
        raise FileNotFoundError(f"Minecraft 1.20.1 client jar not found: {client_jar}")
    if not storage_jar.is_file():
        raise FileNotFoundError(f"Storage Drawers jar not found: {storage_jar}")
    if not input_jar.is_file():
        raise FileNotFoundError(f"Input jar not found: {input_jar}")

    with AssetArchive(input_jar) as mod:
        plank_resource = locate_wood_planks(mod, namespace, wood)
        plank_image = mod.read_image(plank_resource)

    palette = extract_wood_palette(plank_image)

    prefix = f"{namespace}_{wood}"

    print("=== Storage Drawers wood generation ===")
    print(f"Input jar:     {input_jar}")
    print(f"Namespace:     {namespace}")
    print(f"Wood:          {wood}")
    print(f"Plank texture: {plank_resource}")
    print(f"Storage jar:   {storage_jar}")
    print(f"Client jar:    {client_jar}")
    print(f"Output root:   {output_root}")
    print("Palette:")
    for i, c in enumerate(palette):
        print(f"  P{i:02d} = {c[0]:02X}{c[1]:02X}{c[2]:02X}")
    print()

    # --- Textures -----------------------------------------------------
    texture_dir = output_root.joinpath(*TEXTURES_SUBPATH) / namespace
    texture_dir.mkdir(parents=True, exist_ok=True)

    print("Generating textures:")
    for shape in FRONT_SHAPES:
        sources, targets, _, _ = load_vanilla_dataset(client_jar, storage_jar, shape)
        tmodel = fit_template_from_images(sources, targets)
        generated = render_template(tmodel, palette)
        out_path = texture_dir / f"drawers_{wood}_front_{shape}.png"
        generated.save(out_path)
        print(f"  {out_path}")
    print()

    # --- Blockstates --------------------------------------------------
    blockstate_dir = output_root.joinpath(*BLOCKSTATES_SUBPATH)
    blockstate_dir.mkdir(parents=True, exist_ok=True)

    print("Generating blockstates:")
    for shape in FRONT_SHAPES:
        for kind in ("full", "half"):
            name = f"{prefix}_{kind}_drawers_{shape}"
            path = blockstate_dir / f"{name}.json"
            write_json(path, drawer_blockstate(f"storagedrawersextra:block/{name}"))
            print(f"  {path}")

    name = f"{prefix}_trim"
    path = blockstate_dir / f"{name}.json"
    write_json(path, trim_blockstate(f"storagedrawersextra:block/{name}"))
    print(f"  {path}")
    print()

    # --- Block models -------------------------------------------------
    model_dir = output_root.joinpath(*MODELS_BLOCK_SUBPATH)
    model_dir.mkdir(parents=True, exist_ok=True)

    print("Generating block models:")
    for shape in FRONT_SHAPES:
        name = f"{prefix}_full_drawers_{shape}"
        path = model_dir / f"{name}.json"
        write_json(path, full_drawer_model(namespace, wood, shape))
        print(f"  {path}")

        name = f"{prefix}_half_drawers_{shape}"
        path = model_dir / f"{name}.json"
        write_json(path, half_drawer_model(namespace, wood, shape))
        print(f"  {path}")

    name = f"{prefix}_trim"
    path = model_dir / f"{name}.json"
    write_json(path, trim_model(namespace, wood))
    print(f"  {path}")
    print()

    # --- Item models --------------------------------------------------
    item_dir = output_root.joinpath(*MODELS_ITEM_SUBPATH)
    item_dir.mkdir(parents=True, exist_ok=True)

    print("Generating item models:")
    item_names = []
    for shape in FRONT_SHAPES:
        for kind in ("full", "half"):
            item_names.append(f"{prefix}_{kind}_drawers_{shape}")
    item_names.append(f"{prefix}_trim")

    for name in item_names:
        path = item_dir / f"{name}.json"
        write_json(path, item_model(f"storagedrawersextra:block/{name}"), indent=2)
        print(f"  {path}")
    print()

    # --- KubeJS startup script ----------------------------------------
    script_dir = output_root.joinpath(*STARTUP_SCRIPTS_SUBPATH)
    script_dir.mkdir(parents=True, exist_ok=True)

    print("Generating KubeJS startup script:")
    script_path = script_dir / f"{prefix}.js"
    write_text_file(script_path, kubejs_startup_script(wood, prefix))
    print(f"  {script_path}")
    print()

    print("Done.")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Run leave-one-out validation for fronts 1, 2, and 4."""
    repo_root = Path(args.repo_root).expanduser().resolve()
    client_jar = resolve_client_jar(args.client_jar, repo_root)
    storage_jar = resolve_storage_drawers_jar(args.storage_drawers, repo_root)

    if not client_jar.is_file():
        raise FileNotFoundError(f"Minecraft 1.20.1 client jar not found: {client_jar}")
    if not storage_jar.is_file():
        raise FileNotFoundError(f"Storage Drawers jar not found: {storage_jar}")

    overall: dict[str, object] = {
        "minecraft_client": str(client_jar),
        "storage_drawers": str(storage_jar),
        "mode": "template",
        "fronts": {},
    }

    print("=== Storage Drawers 1.20.1 texture validation ===")
    print(f"Minecraft client: {client_jar}")
    print(f"Storage Drawers:  {storage_jar}")
    print("Model:            template")
    print()

    for shape in FRONT_SHAPES:
        sources, targets, source_resources, target_resources = load_vanilla_dataset(client_jar, storage_jar, shape)

        print(f"===== FRONT_{shape} =====")
        print("source:")
        for wood in VANILLA_WOODS:
            print(f"  {wood:8s} {source_resources[wood]}")
        print("target:")
        for wood in VANILLA_WOODS:
            print(f"  {wood:8s} {target_resources[wood]}")

        results = leave_one_out_template_validation(sources, targets)

        overall_front: dict[str, object] = {}
        for wood, metrics in results.items():
            overall_front[wood] = asdict(metrics)
            print(
                f"  {wood:8s} exact={metrics.exact_pixels:3d}/256 "
                f"({metrics.exact_ratio * 100:5.1f}%) "
                f"MAE={metrics.mae:6.3f} RMSE={metrics.rmse:6.3f} "
                f"P95={metrics.p95_error:6.3f} max={metrics.max_error:6.3f}"
            )
        overall["fronts"][f"front_{shape}"] = overall_front

        full_model = fit_template_from_images(sources, targets)
        bands = band_exact_summary(sources, targets, full_model)
        emit_band_table(bands)
        print(f"  visible (non-border) pixels per front: {VISIBLE_PIXELS_PER_FRONT}/256")
        print()
        print()

    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(overall, indent=2) + "\n", encoding="utf-8")
        print(f"JSON report: {report_path}")

    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    """Exercise the fitting and reconstruction math without Minecraft assets."""
    print("=== generator self-test ===")

    palette_a: list[tuple[int, int, int, int]] = [
        (10, 20, 30, 255),
        (40, 50, 60, 255),
        (80, 100, 120, 255),
    ]
    palette_b: list[tuple[int, int, int, int]] = [
        (100, 110, 120, 255),
        (140, 150, 160, 255),
        (200, 210, 220, 255),
    ]
    palettes = [palette_a, palette_b]

    role_true = 1
    scale_true_r = 0.5
    scale_true_g = 0.6
    scale_true_b = 0.7
    bias_true_r = 5.0
    bias_true_g = -3.0
    bias_true_b = 2.0

    front_rgbs: list[tuple[int, int, int]] = []
    for palette in palettes:
        src = palette[role_true]
        front_rgbs.append(
            (
                round(scale_true_r * src[0] + bias_true_r),
                round(scale_true_g * src[1] + bias_true_g),
                round(scale_true_b * src[2] + bias_true_b),
            )
        )

    role, scale_fit, bias_fit, err = fit_template_pixel(front_rgbs, palettes)
    print(
        f"template fit: role={role} "
        f"scale=({scale_fit[0]:.4f},{scale_fit[1]:.4f},{scale_fit[2]:.4f}) "
        f"bias=({bias_fit[0]:+.4f},{bias_fit[1]:+.4f},{bias_fit[2]:+.4f}) "
        f"err={err:.9f}"
    )
    if role != role_true:
        raise AssertionError(f"template fit chose role {role}, expected {role_true}")
    if err > 1.0:
        raise AssertionError(f"template fit residual too large: {err}")

    border_count = 0
    for y in range(16):
        for x in range(16):
            if band_of(x, y, 0.0) == "border":
                border_count += 1
    if border_count != 60:
        raise AssertionError(f"border band should have 60 pixels, found {border_count}")
    if 256 - border_count != VISIBLE_PIXELS_PER_FRONT:
        raise AssertionError("VISIBLE_PIXELS_PER_FRONT is out of sync with border band count")

    script = kubejs_startup_script("maple", "biomesoplenty_maple")
    if "biomesoplenty_maple" not in script:
        raise AssertionError("startup script missing material prefix")
    if "[SD Maple]" not in script:
        raise AssertionError("startup script missing display name")

    required_class_paths = (
        "com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants$VariantData",
        "com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants",
        "com.jaquadro.minecraft.storagedrawersextra.core.ModBlocks",
        "com.jaquadro.minecraft.storagedrawersextra.core.ModItems",
    )
    for path in required_class_paths:
        if path not in script:
            raise AssertionError(f"startup script missing class path: {path}")

    if "com.jaquadro.storagedrawers." in script and "com.jaquadro.minecraft.storagedrawers." not in script:
        raise AssertionError("startup script missing '.minecraft.' package segment")

    script_multi = kubejs_startup_script("twilight_oak", "twilightforest_twilight_oak")
    if "[SD Twilight Oak]" not in script_multi:
        raise AssertionError("multi-word wood display name did not title-case correctly")

    print("PASS")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Generate Storage Drawers wooden front textures and metadata.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root. Default: grandparent of this file.",
    )
    parser.add_argument(
        "--client-jar",
        type=Path,
        default=None,
        help="Minecraft 1.20.1 client jar. Default: gradle cache.",
    )
    parser.add_argument(
        "--storage-drawers",
        type=Path,
        default=None,
        help=(f"Storage Drawers jar, or a directory containing {DEFAULT_STORAGE_DRAWERS_PATTERN}. Default: {DEFAULT_STORAGE_DRAWERS_DIR}"),
    )
    parser.add_argument(
        "--input-jar",
        type=Path,
        default=None,
        help="Mod jar providing the wood's plank texture (required to generate).",
    )
    parser.add_argument(
        "--wood-type",
        type=str,
        default=None,
        help="Wood identifier as 'namespace:wood' (e.g. biomesoplenty:maple).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help=f"KubeJS root. Default: {DEFAULT_OUTPUT_ROOT}",
    )

    subparsers = parser.add_subparsers(dest="command")

    validate = subparsers.add_parser(
        "validate",
        help="Leave-one-out validation against all six vanilla woods.",
    )
    validate.add_argument("--report", type=Path, help="Write JSON validation results to this path.")
    validate.set_defaults(func=cmd_validate)

    self_test = subparsers.add_parser(
        "self-test",
        help="Run math self-tests without Minecraft assets.",
    )
    self_test.set_defaults(func=cmd_self_test)

    return parser


def main() -> int:
    """Run the command-line tool."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        func = cmd_generate
    else:
        func = args.func

    try:
        return func(args)
    except (FileNotFoundError, ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
