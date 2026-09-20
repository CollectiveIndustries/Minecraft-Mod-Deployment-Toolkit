#!/usr/bin/env python3
# src/minecraft/generate_drawers.py

r"""Learn and generate Storage Drawers wooden front textures.

Two models are available:

1. Template model (default, recommended).

   The drawer face is treated as a fixed 16x16 template of
   (palette_role, scale, bias) derived from the six vanilla woods.
   Rendering a new wood is a lookup and a per-channel affine apply:

       front[(x, y)][c] = round(scale[c] * palette[role][c] + bias[c])

   The per-channel scale and bias are fit from all six vanilla woods
   simultaneously, so they are wood-independent by construction and
   generalize to new woods. Interior pixels fit scale=(1,1,1) and
   bias=(0,0,0); structural pixels (drawer split lines, bevel seams)
   get their own per-channel affine that captures detail the
   scalar-shade model cannot represent.

   The outer 1px ring is a geometry inset and is not rendered in-game.
   Those 60 pixels are per-wood in the source assets and are not
   scored. Only the 196 visible pixels per front are evaluated.

2. Legacy model (--legacy-model).

   The original per-pixel arbitrary-donor affine search. Retained for
   comparison and as a fallback.

Usage:

    pdm run python src/minecraft/generate_drawers.py validate \
        --storage-drawers-jar sync/downloads/StorageDrawers-forge-1.20.1-12.14.3.jar

    pdm run python src/minecraft/generate_drawers.py generate \
        --storage-drawers-jar sync/downloads/StorageDrawers-forge-1.20.1-12.14.3.jar \
        --bop-jar sync/downloads/BiomesOPlenty-forge-1.20.1-19.0.0.96.jar

    pdm run python src/minecraft/generate_drawers.py analyze \
        --storage-drawers-jar sync/downloads/StorageDrawers-forge-1.20.1-12.14.3.jar

    pdm run python src/minecraft/generate_drawers.py self-test
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
DEFAULT_STORAGE_DRAWERS_GLOB = "StorageDrawers-forge-1.20.1-12.14.3.jar"
DEFAULT_BOP_GLOB = "BiomesOPlenty-forge-1.20.1-19.0.0.96.jar"
DEFAULT_OUTPUT_SUBDIR = "sync/kubejs/assets/storagedrawersextra/textures/block/biomesoplenty"
DEFAULT_MAPLE_RESOURCE = "assets/biomesoplenty/textures/block/maple_planks.png"


# ---------------------------------------------------------------------------
# Shared data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PixelModel:
    """Learned source coordinate and scalar color transform for one output pixel."""

    source_x: int
    source_y: int
    scale: float
    bias_r: float
    bias_g: float
    bias_b: float
    rmse: float


@dataclass(frozen=True)
class FrontModel:
    """Learned model for one of the three drawer front sizes (legacy)."""

    width: int
    height: int
    pixels: tuple[PixelModel, ...]
    alpha: tuple[int, ...]


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

    def find(self, predicate) -> list[str]:
        """Return archive paths satisfying a predicate."""
        return [name for name in self.names if predicate(name)]

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


def rgb(pixel: tuple[int, int, int, int]) -> tuple[float, float, float]:
    """Return an RGBA pixel as floating-point RGB."""
    return float(pixel[0]), float(pixel[1]), float(pixel[2])


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

def discover_jar(root: Path, exact_name: str) -> Path:
    """Locate a jar by exact filename under a repository root."""
    direct = root / "sync" / "downloads" / exact_name
    if direct.is_file():
        return direct

    matches = sorted(root.rglob(exact_name))
    if matches:
        return matches[0]

    return Path()


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
            f"Could not uniquely locate Storage Drawers {wood} front_{shape} texture.\n"
            f"Expected: {expected}\n"
            f"Basename candidates: {exact[:20]}"
        )
    return resources


def locate_maple_planks(archive: AssetArchive) -> str:
    """Locate the BOP Maple plank texture."""
    expected = DEFAULT_MAPLE_RESOURCE
    if expected in archive.names:
        return expected

    matches = find_resource_by_fragments(
        archive.names,
        ("textures/block", "maple_planks"),
    )
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"Could not uniquely locate Biomes O' Plenty Maple planks in {archive.path}.\nCandidates: {matches[:20]}")


# ---------------------------------------------------------------------------
# Legacy fitting
# ---------------------------------------------------------------------------

def fit_scalar_affine(
    source_samples: list[tuple[float, float, float]],
    target_samples: list[tuple[float, float, float]],
) -> tuple[float, tuple[float, float, float], float]:
    """Fit target ~= scale * source + per-channel bias."""
    if len(source_samples) != len(target_samples):
        raise ValueError("Source and target sample counts differ")
    if not source_samples:
        raise ValueError("Cannot fit an empty sample set")

    mean_source = [0.0, 0.0, 0.0]
    mean_target = [0.0, 0.0, 0.0]
    for source, target in zip(source_samples, target_samples, strict=True):
        for channel in range(3):
            mean_source[channel] += source[channel]
            mean_target[channel] += target[channel]
    count = float(len(source_samples))
    for channel in range(3):
        mean_source[channel] /= count
        mean_target[channel] /= count

    numerator = 0.0
    denominator = 0.0
    for source, target in zip(source_samples, target_samples, strict=True):
        for channel in range(3):
            source_center = source[channel] - mean_source[channel]
            target_center = target[channel] - mean_target[channel]
            numerator += source_center * target_center
            denominator += source_center * source_center

    if denominator > 1e-9:
        scale = numerator / denominator
    else:
        scale = 1.0

    bias = (
        mean_target[0] - scale * mean_source[0],
        mean_target[1] - scale * mean_source[1],
        mean_target[2] - scale * mean_source[2],
    )

    squared_error = 0.0
    sample_count = 0
    for source, target in zip(source_samples, target_samples, strict=True):
        for channel in range(3):
            predicted = scale * source[channel] + bias[channel]
            difference = predicted - target[channel]
            squared_error += difference * difference
            sample_count += 1
    rmse = math.sqrt(squared_error / float(sample_count))
    return scale, bias, rmse


def fit_zero_intercept_scalar(
    source_samples: list[tuple[float, float, float]],
    target_samples: list[tuple[float, float, float]],
) -> tuple[float, float]:
    """Fit target ~= scale * source with one scalar scale and return RMSE."""
    numerator = 0.0
    denominator = 0.0
    for source, target in zip(source_samples, target_samples, strict=True):
        for channel in range(3):
            numerator += source[channel] * target[channel]
            denominator += source[channel] * source[channel]
    scale = 1.0
    if denominator > 1e-9:
        scale = numerator / denominator

    squared_error = 0.0
    sample_count = 0
    for source, target in zip(source_samples, target_samples, strict=True):
        for channel in range(3):
            difference = scale * source[channel] - target[channel]
            squared_error += difference * difference
            sample_count += 1
    rmse = math.sqrt(squared_error / float(sample_count))
    return scale, rmse


def donor_distance(source_index: int, target_index: int) -> int:
    """Return Manhattan distance between two 16x16 pixel coordinates."""
    target_x = target_index % 16
    target_y = target_index // 16
    source_x = source_index % 16
    source_y = source_index // 16
    return abs(target_x - source_x) + abs(target_y - source_y)


def train_front(
    source_images: dict[str, Image.Image],
    target_images: dict[str, Image.Image],
    allow_relocated_donor: bool = True,
    same_coordinate_epsilon: float = 0.08,
) -> FrontModel:
    """Legacy per-pixel donor-search trainer."""
    source_pixels = {wood: image_pixels(image) for wood, image in source_images.items()}
    target_pixels = {wood: image_pixels(image) for wood, image in target_images.items()}

    all_target = target_pixels[VANILLA_WOODS[0]]
    width, height = TEXTURE_SIZE
    alpha = tuple(pixel[3] for pixel in all_target)
    learned: list[PixelModel] = []

    for target_index in range(width * height):
        target_samples = [target_pixels[wood][target_index] for wood in VANILLA_WOODS]
        candidate_indices = range(width * height)
        if not allow_relocated_donor:
            candidate_indices = (target_index,)

        candidates: list[tuple[int, float, float, float, float, float]] = []
        for source_index in candidate_indices:
            source_samples = [source_pixels[wood][source_index] for wood in VANILLA_WOODS]
            source_rgb = [rgb(pixel) for pixel in source_samples]
            target_rgb = [rgb(pixel) for pixel in target_samples]
            scale, bias, rmse = fit_scalar_affine(source_rgb, target_rgb)
            candidates.append((source_index, scale, bias[0], bias[1], bias[2], rmse))

        minimum_rmse = min(candidate[5] for candidate in candidates)
        same_coordinate_candidate = None
        for candidate in candidates:
            if candidate[0] == target_index:
                same_coordinate_candidate = candidate
                break

        selected = candidates[0]
        if same_coordinate_candidate is not None and same_coordinate_candidate[5] <= minimum_rmse + same_coordinate_epsilon:
            selected = same_coordinate_candidate
        else:
            selected = min(
                candidates,
                key=lambda candidate: (candidate[5], donor_distance(candidate[0], target_index), candidate[0]),
            )

        source_index, scale, bias_r, bias_g, bias_b, rmse = selected
        best = PixelModel(
            source_x=source_index % width,
            source_y=source_index // width,
            scale=scale,
            bias_r=bias_r,
            bias_g=bias_g,
            bias_b=bias_b,
            rmse=rmse,
        )

        if best is None:
            raise RuntimeError(f"No donor candidate found for pixel {target_index}")
        learned.append(best)

    return FrontModel(width=width, height=height, pixels=tuple(learned), alpha=alpha)


def apply_model(model: FrontModel, source_image: Image.Image) -> Image.Image:
    """Legacy renderer for the donor-search model."""
    source = image_pixels(source_image)
    if source_image.size != TEXTURE_SIZE:
        raise ValueError(f"Expected source texture to be 16x16, got {source_image.size}")

    output = Image.new("RGBA", TEXTURE_SIZE)
    out_pixels: list[tuple[int, int, int, int]] = []
    for target_index, pixel_model in enumerate(model.pixels):
        source_index = pixel_model.source_y * model.width + pixel_model.source_x
        src = source[source_index]
        channels = (
            pixel_model.scale * float(src[0]) + pixel_model.bias_r,
            pixel_model.scale * float(src[1]) + pixel_model.bias_g,
            pixel_model.scale * float(src[2]) + pixel_model.bias_b,
        )
        rgb_values = tuple(max(0, min(255, round(value))) for value in channels)
        out_pixels.append((rgb_values[0], rgb_values[1], rgb_values[2], model.alpha[target_index]))
    output.putdata(out_pixels)
    return output


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


def model_metrics(model: FrontModel) -> dict[str, float | int]:
    """Summarize the training fit of a learned front model."""
    rmses = [pixel.rmse for pixel in model.pixels]
    return {
        "pixels": len(rmses),
        "rmse_le_0_1": sum(rmse <= 0.1 for rmse in rmses),
        "rmse_le_0_5": sum(rmse <= 0.5 for rmse in rmses),
        "rmse_le_1_0": sum(rmse <= 1.0 for rmse in rmses),
        "rmse_le_2_0": sum(rmse <= 2.0 for rmse in rmses),
        "rmse_le_3_0": sum(rmse <= 3.0 for rmse in rmses),
        "rmse_le_5_0": sum(rmse <= 5.0 for rmse in rmses),
        "training_mean_pixel_rmse": statistics.mean(rmses),
        "training_max_pixel_rmse": max(rmses),
    }


def leave_one_out_validation(
    source_images: dict[str, Image.Image],
    target_images: dict[str, Image.Image],
    allow_relocated_donor: bool,
) -> dict[str, ValidationMetrics]:
    """Legacy leave-one-out validation."""
    results: dict[str, ValidationMetrics] = {}
    original_woods = tuple(VANILLA_WOODS)

    for holdout in original_woods:
        training_woods = tuple(wood for wood in original_woods if wood != holdout)
        train_sources = {wood: source_images[wood] for wood in training_woods}
        train_targets = {wood: target_images[wood] for wood in training_woods}

        model = train_front_subset(
            train_sources,
            train_targets,
            allow_relocated_donor=allow_relocated_donor,
        )
        predicted = apply_model(model, source_images[holdout])
        results[holdout] = validation_metrics(target_images[holdout], predicted)

    return results


def train_front_subset(
    source_images: dict[str, Image.Image],
    target_images: dict[str, Image.Image],
    allow_relocated_donor: bool,
) -> FrontModel:
    """Legacy trainer on an arbitrary subset of vanilla woods."""
    woods = tuple(source_images.keys())
    if not woods:
        raise ValueError("Cannot train on zero woods")

    source_pixels = {wood: image_pixels(image) for wood, image in source_images.items()}
    target_pixels = {wood: image_pixels(image) for wood, image in target_images.items()}

    width, height = TEXTURE_SIZE
    alpha_source = target_pixels[woods[0]]
    alpha = tuple(pixel[3] for pixel in alpha_source)
    learned: list[PixelModel] = []

    for target_index in range(width * height):
        target_rgb = [rgb(target_pixels[wood][target_index]) for wood in woods]
        candidate_indices = range(width * height)
        if not allow_relocated_donor:
            candidate_indices = (target_index,)

        candidates: list[tuple[int, float, float, float, float, float]] = []
        for source_index in candidate_indices:
            source_rgb = [rgb(source_pixels[wood][source_index]) for wood in woods]
            scale, bias, rmse = fit_scalar_affine(source_rgb, target_rgb)
            candidates.append((source_index, scale, bias[0], bias[1], bias[2], rmse))

        minimum_rmse = min(candidate[5] for candidate in candidates)
        same_coordinate_candidate = None
        for candidate in candidates:
            if candidate[0] == target_index:
                same_coordinate_candidate = candidate
                break

        selected = candidates[0]
        if same_coordinate_candidate is not None and same_coordinate_candidate[5] <= minimum_rmse + 0.08:
            selected = same_coordinate_candidate
        else:
            selected = min(
                candidates,
                key=lambda candidate: (candidate[5], donor_distance(candidate[0], target_index), candidate[0]),
            )

        source_index, scale, bias_r, bias_g, bias_b, rmse = selected
        best = PixelModel(
            source_x=source_index % width,
            source_y=source_index // width,
            scale=scale,
            bias_r=bias_r,
            bias_g=bias_g,
            bias_b=bias_b,
            rmse=rmse,
        )

        if best is None:
            raise RuntimeError(f"No donor candidate found for pixel {target_index}")
        learned.append(best)

    return FrontModel(width=width, height=height, pixels=tuple(learned), alpha=alpha)


# ---------------------------------------------------------------------------
# Template fitting (default)
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
    out: dict[str, dict[str, tuple[int, int, float, float]]] = {
        band: {} for band in ("interior", "inner_ring", "handle", "border")
    }

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
                    pred = tuple(
                        max(0, min(255, round(scale[c] * src[c] + bias[c])))
                        for c in range(3)
                    )
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

def emit_training_summary(shape: int, model: FrontModel) -> None:
    """Print a compact summary of learned pixel transforms (legacy)."""
    summary = model_metrics(model)
    print(f"FRONT_{shape}")
    print(f"  training fit: {summary['rmse_le_0_1']}/256 <= 0.1 RMSE")
    print(f"  training fit: {summary['rmse_le_0_5']}/256 <= 0.5 RMSE")
    print(f"  training fit: {summary['rmse_le_3_0']}/256 <= 3.0 RMSE")
    print(f"  training mean pixel RMSE: {summary['training_mean_pixel_rmse']:.3f}")
    print(f"  training max pixel RMSE: {summary['training_max_pixel_rmse']:.3f}")

    scales = [pixel.scale for pixel in model.pixels]
    print(
        "  scale clusters: "
        + ", ".join(
            f"{rounded:.3f}={sum(abs(scale - rounded) < 0.006 for scale in scales)}" for rounded in (0.215, 0.267, 0.833, 0.837, 0.865, 0.875, 0.990, 1.000)
        )
    )

    same_coordinate_count = 0
    for index, pixel in enumerate(model.pixels):
        if pixel.source_x == index % 16 and pixel.source_y == index // 16:
            same_coordinate_count += 1
    print(f"  same-coordinate donor pixels: {same_coordinate_count}/256")


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
# Commands
# ---------------------------------------------------------------------------

def _resolve_client_jar(args: argparse.Namespace) -> Path:
    """Return the Minecraft client jar path from args or the default cache."""
    if args.client_jar:
        return Path(args.client_jar).expanduser()
    return Path.home() / ".gradle" / "caches" / "forge_gradle" / "minecraft_repo" / "versions" / "1.20.1" / "client.jar"


def _resolve_storage_jar(args: argparse.Namespace, repo_root: Path) -> Path:
    """Return the Storage Drawers jar path from args or discovery."""
    if args.storage_drawers_jar:
        return Path(args.storage_drawers_jar).expanduser()
    return discover_jar(repo_root, DEFAULT_STORAGE_DRAWERS_GLOB)


def _resolve_bop_jar(args: argparse.Namespace, repo_root: Path) -> Path:
    """Return the Biomes O' Plenty jar path from args or discovery."""
    if args.bop_jar:
        return Path(args.bop_jar).expanduser()
    return discover_jar(repo_root, DEFAULT_BOP_GLOB)


def cmd_validate(args: argparse.Namespace) -> int:
    """Run leave-one-out validation for fronts 1, 2, and 4."""
    repo_root = Path(args.repo_root).expanduser().resolve()
    client_jar = _resolve_client_jar(args)
    storage_jar = _resolve_storage_jar(args, repo_root)

    if not client_jar.is_file():
        raise FileNotFoundError(f"Minecraft 1.20.1 client jar not found: {client_jar}")
    if not storage_jar.is_file():
        raise FileNotFoundError(f"Storage Drawers 1.20.1-12.14.3 jar not found. Expected under {repo_root}/sync/downloads/ or supply --storage-drawers-jar.")

    if args.legacy_model:
        mode = "legacy"
    else:
        mode = "template"

    overall: dict[str, object] = {
        "minecraft_client": str(client_jar),
        "storage_drawers": str(storage_jar),
        "mode": mode,
        "fronts": {},
    }

    print("=== Storage Drawers 1.20.1 texture validation ===")
    print(f"Minecraft client: {client_jar}")
    print(f"Storage Drawers:  {storage_jar}")
    print(f"Model:            {mode}")
    print()

    for shape in FRONT_SHAPES:
        sources, targets, source_resources, target_resources = load_vanilla_dataset(
            client_jar, storage_jar, shape
        )

        print(f"===== FRONT_{shape} =====")
        print("source:")
        for wood in VANILLA_WOODS:
            print(f"  {wood:8s} {source_resources[wood]}")
        print("target:")
        for wood in VANILLA_WOODS:
            print(f"  {wood:8s} {target_resources[wood]}")

        if args.legacy_model:
            results = leave_one_out_validation(
                sources,
                targets,
                allow_relocated_donor=not args.same_coordinate_only,
            )
        else:
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

        if not args.legacy_model:
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


def cmd_analyze(args: argparse.Namespace) -> int:
    """Train on all six vanilla woods and print learned model statistics."""
    repo_root = Path(args.repo_root).expanduser().resolve()
    client_jar = _resolve_client_jar(args)
    storage_jar = _resolve_storage_jar(args, repo_root)

    if args.legacy_model:
        model_name = "legacy"
    else:
        model_name = "template"

    print(f"=== Learned Storage Drawers front construction ({model_name}) ===")
    for shape in FRONT_SHAPES:
        sources, targets, _, _ = load_vanilla_dataset(client_jar, storage_jar, shape)

        if args.legacy_model:
            model = train_front(sources, targets, allow_relocated_donor=not args.same_coordinate_only)
            emit_training_summary(shape, model)
        else:
            tmodel = fit_template_from_images(sources, targets)
            emit_template_summary(shape, tmodel)

            print("  role map:")
            for y in range(16):
                print("    " + " ".join(f"{tmodel.role_map[y][x]:2d}" for x in range(16)))
            print("  scale map (mean of R,G,B):")
            for y in range(16):
                row = []
                for x in range(16):
                    s = tmodel.scale_map[y][x]
                    row.append(f"{(s[0] + s[1] + s[2]) / 3.0:.2f}")
                print("    " + " ".join(row))
            print("  bias map (R,G,B):")
            for y in range(16):
                cells = []
                for x in range(16):
                    b = tmodel.bias_map[y][x]
                    cells.append(f"{b[0]:+.1f},{b[1]:+.1f},{b[2]:+.1f}")
                print("    " + " ".join(cells))
            print()
        print()
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    """Generate Maple drawer front textures after validation."""
    repo_root = Path(args.repo_root).expanduser().resolve()
    client_jar = _resolve_client_jar(args)
    storage_jar = _resolve_storage_jar(args, repo_root)
    bop_jar = _resolve_bop_jar(args, repo_root)

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser()
    else:
        output_dir = repo_root / DEFAULT_OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if not client_jar.is_file():
        raise FileNotFoundError(f"Minecraft 1.20.1 client jar not found: {client_jar}")
    if not storage_jar.is_file():
        raise FileNotFoundError(f"Storage Drawers jar not found: {storage_jar}")
    if not bop_jar.is_file():
        raise FileNotFoundError(f"Biomes O' Plenty 1.20.1-19.0.0.96 jar not found. Expected under {repo_root}/sync/downloads/ or supply --bop-jar.")

    with AssetArchive(bop_jar) as bop_archive:
        maple_resource = locate_maple_planks(bop_archive)
        maple_image = bop_archive.read_image(maple_resource)

    maple_palette = extract_wood_palette(maple_image)

    if args.legacy_model:
        mode = "legacy"
    else:
        mode = "template"

    print(f"=== Storage Drawers Maple texture generation ({mode}) ===")
    print(f"BOP source: {bop_jar}!{maple_resource}")
    print(f"Output:     {output_dir}")
    print("Maple palette:")
    for i, c in enumerate(maple_palette):
        print(f"  P{i:02d} = {c[0]:02X}{c[1]:02X}{c[2]:02X}")
    print()

    for shape in FRONT_SHAPES:
        sources, targets, _, _ = load_vanilla_dataset(client_jar, storage_jar, shape)

        if args.legacy_model:
            model = train_front(sources, targets, allow_relocated_donor=not args.same_coordinate_only)
            generated = apply_model(model, maple_image)
        else:
            tmodel = fit_template_from_images(sources, targets)
            generated = render_template(tmodel, maple_palette)

        output_path = output_dir / f"drawers_maple_front_{shape}.png"
        generated.save(output_path)
        print(f"front_{shape}: {output_path}")

    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    """Exercise the fitting and reconstruction math without Minecraft assets."""
    print("=== generator self-test ===")

    source: list[tuple[float, float, float]] = []
    target: list[tuple[float, float, float]] = []
    for base in range(20, 140, 20):
        source.append((float(base), float(base + 5), float(base + 10)))
        target.append(
            (
                0.875 * base + 4.0,
                0.875 * (base + 5) + 3.0,
                0.875 * (base + 10) + 2.0,
            )
        )

    scale, _bias, rmse = fit_scalar_affine(source, target)
    print(f"legacy fit: scale={scale:.6f} rmse={rmse:.9f}")
    if abs(scale - 0.875) > 1e-6:
        raise AssertionError("scalar scale regression failed")
    if rmse > 1e-6:
        raise AssertionError("affine regression should reconstruct the synthetic samples exactly")

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
    if VISIBLE_PIXELS_PER_FRONT != 256 - border_count:
        raise AssertionError("VISIBLE_PIXELS_PER_FRONT is out of sync with border band count")

    print("PASS")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Learn and generate Storage Drawers 1.20.1 wooden front textures.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Minecraft repo root; defaults to the grandparent of this file (the repo root).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    self_test = subparsers.add_parser("self-test", help="Run math self-tests without Minecraft assets.")
    self_test.set_defaults(func=cmd_self_test)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--client-jar", type=Path, help="Path to the Minecraft 1.20.1 client.jar.")
    common.add_argument("--storage-drawers-jar", type=Path, help="Path to Storage Drawers 1.20.1-12.14.3 jar.")
    common.add_argument(
        "--legacy-model",
        action="store_true",
        help="Use the original donor-search model instead of the template model.",
    )
    common.add_argument(
        "--same-coordinate-only",
        action="store_true",
        help="Legacy-only: disable relocated donor search and force target(x,y) to use source(x,y).",
    )

    validate = subparsers.add_parser("validate", parents=[common], help="Leave-one-out validation against all six vanilla woods.")
    validate.add_argument("--report", type=Path, help="Write JSON validation results to this path.")
    validate.set_defaults(func=cmd_validate)

    analyze = subparsers.add_parser("analyze", parents=[common], help="Train from all six woods and print model statistics.")
    analyze.set_defaults(func=cmd_analyze)

    generate = subparsers.add_parser("generate", parents=[common], help="Generate BOP Maple front_1/front_2/front_4 textures.")
    generate.add_argument("--bop-jar", type=Path, help="Path to Biomes O' Plenty 1.20.1-19.0.0.96 jar.")
    generate.add_argument("--output-dir", type=Path, help="Directory for generated front PNGs.")
    generate.set_defaults(func=cmd_generate)
    return parser


def main() -> int:
    """Run the command-line tool."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())