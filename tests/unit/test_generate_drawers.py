# tests/unit/test_generate_drawers.py

"""Tests for src/minecraft/generate_drawers.py.

Coverage areas:
  * image helpers: ``image_pixels``, ``luminance``, ``extract_wood_palette``
  * archive I/O: ``AssetArchive`` context management, ``read_image``
    success / missing-member / wrong-size paths
  * resource discovery: ``find_resource_by_fragments``,
    ``locate_vanilla_source_resources``, ``locate_vanilla_front_resources``,
    ``locate_wood_planks`` (exact, fallback, error)
  * fitting math: ``fit_channel_affine`` (normal + zero-variance),
    ``fit_template_pixel``, ``fit_template_from_images``,
    ``leave_one_out_template_validation``
  * rendering: ``render_template`` size and pixel correctness,
    ``template_metrics`` keys, ``validation_metrics``
  * band classification: ``band_of`` (all four bands),
    ``mean_scale_of``, ``band_exact_summary``
  * JSON generators: every blockstate / model / item function
  * KubeJS: ``kubejs_startup_script`` content and class paths
  * writers: ``write_json``, ``write_text_file``
  * path helpers: ``resolve_client_jar``, ``resolve_storage_drawers_jar``
    (file + directory forms), ``resolve_input_jar``, ``resolve_output_root``,
    ``parse_wood_type``
  * commands: ``cmd_self_test``, ``cmd_validate``, ``cmd_generate``,
    and ``main`` dispatcher + exit codes

Fixtures
--------

Each of the six vanilla woods gets a distinct palette: the base 7-colour
list plus a small, even-valued jitter that depends on *both* the wood
index and the palette role index.

Non-separability matters.  A pure per-wood offset (``base + k(w)``) is a
rank-1 variation: for any two roles ``r`` and ``r'`` the difference
``x_{r'}(w) - x_r(w)`` is constant across woods, so the fit's objective
is exactly zero for *every* candidate role.  The ``error < best_error``
tie-break then collapses the role map to the first-encountered role (0),
which is exactly what ``test_role_map_reproduces_pattern`` and
``test_scale_and_bias_recovered`` catch.

A weaker but still-broken variant is a jitter of the form
``(a*w + b*r) mod m``: it is not separable *as a function of (w, r)*,
but the difference between two role slices reduces to a fixed modular
offset, so several wrong roles can still tie or nearly tie on SSE.  The
``idx * i`` term below breaks that: for every pair of distinct roles the
per-wood difference sequence is genuinely non-affine, so only the true
role fits with SSE = 0.

Even jitter values keep ``0.5 * src + 20`` integral, so the synthetic
target is exact and ``(scale, bias)`` is recovered without rounding
noise.  The jitter magnitude (0..24) is smaller than the base palette's
luminance spacing (30), so ``extract_wood_palette`` still returns the
per-role colours in the same order and every ``_palette_for``-based
assertion holds unchanged.

A second, subtler dependency: ``_target_image``'s default ``scale=0.5``
places every fitted scale exactly at ``HANDLE_SCALE_THRESHOLD``.  Since
``band_of`` uses a strict ``<``, ``mean_scale == 0.5`` falls through to
the geometric branches - which is what the ``band_exact_summary`` tests
rely on.  Tightening that comparison to ``<=`` or changing the default
scale will bucket every pixel as ``handle`` and break those two tests.
"""

from __future__ import annotations

import dataclasses
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from minecraft import generate_drawers
from minecraft.generate_drawers import (
    FRONT_SHAPES,
    VANILLA_WOODS,
    VISIBLE_PIXELS_PER_FRONT,
    AssetArchive,
    TemplateModel,
    ValidationMetrics,
    band_exact_summary,
    band_of,
    build_parser,
    cmd_generate,
    cmd_self_test,
    cmd_validate,
    drawer_blockstate,
    extract_wood_palette,
    find_resource_by_fragments,
    fit_channel_affine,
    fit_template_from_images,
    fit_template_pixel,
    full_drawer_model,
    half_drawer_model,
    image_pixels,
    item_model,
    kubejs_startup_script,
    leave_one_out_template_validation,
    load_vanilla_dataset,
    locate_vanilla_front_resources,
    locate_vanilla_source_resources,
    locate_wood_planks,
    luminance,
    mean_scale_of,
    parse_wood_type,
    render_template,
    resolve_client_jar,
    resolve_input_jar,
    resolve_output_root,
    resolve_storage_drawers_jar,
    template_metrics,
    trim_blockstate,
    trim_model,
    validation_metrics,
    write_json,
    write_text_file,
)

_BASE_PALETTE: list[tuple[int, int, int, int]] = [
    (10, 20, 30, 255),
    (40, 50, 60, 255),
    (70, 80, 90, 255),
    (100, 110, 120, 255),
    (130, 140, 150, 255),
    (160, 170, 180, 255),
    (190, 200, 210, 255),
]
_PATTERN_LEN = len(_BASE_PALETTE)


def _palette_for(wood: str) -> list[tuple[int, int, int, int]]:
    """Return wood's palette: base palette plus per-(wood, role) jitter.

    Non-separability is required.  A pure per-wood offset is a rank-1
    variation: for any two roles r, r', ``x_r(w) - x_{r'}(w)`` is constant
    across woods, so ``y(w)`` is exactly affine in ``x_r(w)`` for *every*
    candidate role.  All seven roles then tie on SSE at 0, and
    ``fit_template_pixel``'s strict ``<`` tie-break collapses the role
    map to role 0.

    A weaker trap is a jitter of the form ``(a*w + b*r) mod m``: it is
    not separable *as a function of (w, r)*, but the difference between
    two role slices is a fixed modular offset, so several wrong roles can
    still tie or nearly tie.  The ``w * r`` cross term below breaks that
    for every role pair: the per-wood difference sequence is non-affine,
    so only the true role fits with SSE = 0.

    Even jitter values keep ``0.5 * src + 20`` integral, so the synthetic
    target is exact and ``(scale, bias)`` is recovered without rounding
    noise.  The jitter range [0, 24] stays strictly below the base
    palette's per-role luminance spacing (30), so
    ``extract_wood_palette`` still returns the per-role colours in the
    same order and every ``_palette_for``-based assertion holds
    unchanged.
    """
    idx = VANILLA_WOODS.index(wood)
    result: list[tuple[int, int, int, int]] = []
    for i, (r, g, b, a) in enumerate(_BASE_PALETTE):
        jitter = 2 * ((7 * idx + 3 * i + 7 * idx * i) % 13)
        result.append((r + jitter, g + jitter, b + jitter, a))
    return result


def _source_image(wood: str) -> Image.Image:
    """16x16 image cycling wood's palette by (x + y) % 7."""
    palette = _palette_for(wood)
    img = Image.new("RGBA", (16, 16))
    for y in range(16):
        for x in range(16):
            img.putpixel((x, y), palette[(x + y) % _PATTERN_LEN])
    return img


def _target_image(wood: str, scale: float = 0.5, bias: float = 20.0) -> Image.Image:
    """16x16 image: per-channel ``round(scale * src + bias)`` of wood's source."""
    src = _source_image(wood)
    img = Image.new("RGBA", (16, 16))
    for y in range(16):
        for x in range(16):
            p = src.getpixel((x, y))
            img.putpixel(
                (x, y),
                (max(0, min(255, round(scale * p[0] + bias))), max(0, min(255, round(scale * p[1] + bias))), max(0, min(255, round(scale * p[2] + bias))), 255),
            )
    return img


def _png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _zip_write(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _build_client_jar(path: Path) -> Path:
    members = {f"assets/minecraft/textures/block/{wood}_planks.png": _png_bytes(_source_image(wood)) for wood in VANILLA_WOODS}
    return _zip_write(path, members)


def _build_drawers_jar(path: Path, *, scale: float = 0.5, bias: float = 20.0) -> Path:
    members: dict[str, bytes] = {}
    for shape in FRONT_SHAPES:
        for wood in VANILLA_WOODS:
            members[f"assets/storagedrawers/textures/block/drawers_{wood}_front_{shape}.png"] = _png_bytes(_target_image(wood, scale, bias))
    return _zip_write(path, members)


def _build_input_jar(path: Path, namespace: str, wood: str) -> Path:
    members = {f"assets/{namespace}/textures/block/{wood}_planks.png": _png_bytes(_source_image("oak"))}
    return _zip_write(path, members)


def _vanilla_pair() -> tuple[dict[str, Image.Image], dict[str, Image.Image]]:
    sources = {w: _source_image(w) for w in VANILLA_WOODS}
    targets = {w: _target_image(w) for w in VANILLA_WOODS}
    return (sources, targets)


class TestImagePixels:
    """Tests for the image_pixels function, verifying pixel extraction order and completeness."""

    def test_returns_row_major_rgba(self) -> None:
        """Tests that image_pixels returns pixels in row-major RGBA order."""
        img = Image.new("RGBA", (2, 2))
        img.putpixel((0, 0), (1, 2, 3, 255))
        img.putpixel((1, 0), (4, 5, 6, 255))
        img.putpixel((0, 1), (7, 8, 9, 255))
        img.putpixel((1, 1), (10, 11, 12, 255))
        assert image_pixels(img) == [(1, 2, 3, 255), (4, 5, 6, 255), (7, 8, 9, 255), (10, 11, 12, 255)]

    def test_full_16x16_has_256_entries(self) -> None:
        """Tests that a full 16x16 image has 256 pixel entries."""
        assert len(image_pixels(_source_image("oak"))) == 256


class TestLuminance:
    """Tests for the luminance function, verifying color weighting and alpha channel handling."""

    def test_black_is_zero(self) -> None:
        """Tests that black has a luminance of zero."""
        assert luminance((0, 0, 0, 255)) == 0.0

    def test_white_is_255(self) -> None:
        """Tests that white has a luminance of 255."""
        assert luminance((255, 255, 255, 255)) == pytest.approx(255.0)

    def test_green_dominates(self) -> None:
        """Tests that green has higher luminance than red, and red higher than blue."""
        g = luminance((0, 255, 0, 255))
        r = luminance((255, 0, 0, 255))
        b = luminance((0, 0, 255, 255))
        assert g > r > b

    def test_alpha_ignored(self) -> None:
        """Tests that the alpha channel does not contribute to luminance."""
        assert luminance((10, 20, 30, 255)) == luminance((10, 20, 30, 0))


class TestExtractWoodPalette:
    """Tests for the extract_wood_palette function."""

    def test_sorted_darkest_first(self) -> None:
        """Tests that the palette is sorted from darkest to lightest."""
        palette = extract_wood_palette(_source_image("oak"))
        for i in range(len(palette) - 1):
            assert luminance(palette[i]) <= luminance(palette[i + 1])

    def test_exactly_seven_colours(self) -> None:
        """Tests that the extracted palette contains exactly seven colors."""
        assert len(extract_wood_palette(_source_image("oak"))) == 7

    def test_matches_source_palette(self) -> None:
        """Tests that the extracted palette matches the source palette."""
        palette = extract_wood_palette(_source_image("oak"))
        assert set(palette) == set(_palette_for("oak"))

    def test_solid_image_returns_one(self) -> None:
        """Tests that a solid-color image returns a single-color palette."""
        img = Image.new("RGBA", (16, 16), (42, 84, 126, 255))
        assert extract_wood_palette(img) == [(42, 84, 126, 255)]

    def test_expected_cap(self) -> None:
        """Tests that the extracted wood palette has the expected number of colors."""
        img = Image.new("RGBA", (16, 16))
        for i in range(256):
            img.putpixel((i % 16, i // 16), (i, i, i, 255))
        assert len(extract_wood_palette(img)) == 7


class TestAssetArchive:
    """Tests for the AssetArchive class covering image reading, validation, and context management."""

    def test_read_image_success(self, tmp_path: Path) -> None:
        """Tests successful reading of an image from the archive."""
        jar = _zip_write(tmp_path / "a.jar", {"assets/x/t.png": _png_bytes(_source_image("oak"))})
        with AssetArchive(jar) as archive:
            img = archive.read_image("assets/x/t.png")
            assert img.size == (16, 16)
            assert img.mode == "RGBA"

    def test_read_image_missing_member(self, tmp_path: Path) -> None:
        """Tests that reading a missing archive member raises FileNotFoundError."""
        jar = _zip_write(tmp_path / "a.jar", {})
        with AssetArchive(jar) as archive, pytest.raises(FileNotFoundError):
            archive.read_image("assets/x/missing.png")

    def test_read_image_wrong_size(self, tmp_path: Path) -> None:
        """Tests that reading an image with an unexpected size raises an error."""
        small = Image.new("RGBA", (8, 8), (0, 0, 0, 255))
        jar = _zip_write(tmp_path / "a.jar", {"assets/x/small.png": _png_bytes(small)})
        with AssetArchive(jar) as archive, pytest.raises(ValueError):
            archive.read_image("assets/x/small.png")

    def test_context_manager_closes(self, tmp_path: Path) -> None:
        """Tests that the archive rejects reads once the context manager has exited."""
        jar = _zip_write(tmp_path / "a.jar", {})
        archive = AssetArchive(jar)
        archive.__enter__()
        archive.__exit__(None, None, None)
        with pytest.raises(ValueError):
            archive.read_image("anything.png")


class TestFindResourceByFragments:
    """Tests for find_resource_by_fragments across matching, sorting, case-insensitivity, suffixes, and non-matches."""

    def test_single_match(self) -> None:
        """Tests that a single matching resource is returned correctly."""
        names = ["assets/a/textures/block/oak_planks.png"]
        assert find_resource_by_fragments(names, ("assets/a/textures/block", "oak_planks")) == names

    def test_multiple_matches_sorted(self) -> None:
        """Tests that multiple matching resources are returned in sorted order."""
        names = ["assets/a/z_oak_planks.png", "assets/a/a_oak_planks.png"]
        result = find_resource_by_fragments(names, ("assets/a/", "oak_planks"))
        assert result == sorted(names)

    def test_case_insensitive(self) -> None:
        """Tests that fragment matching is case-insensitive."""
        names = ["Assets/A/Textures/Block/Oak_Planks.PNG"]
        assert find_resource_by_fragments(names, ("assets/a/", "oak_planks")) == names

    def test_non_png_rejected(self) -> None:
        """Tests that resources without the default PNG suffix are rejected."""
        assert find_resource_by_fragments(["assets/a/x.png.bak"], ("assets/a/",)) == []

    def test_custom_suffix(self) -> None:
        """Tests that resources are filtered using the specified custom suffix."""
        names = ["assets/a/x.json"]
        assert find_resource_by_fragments(names, ("assets/a/",), suffix=".json") == names

    def test_no_match(self) -> None:
        """Tests that find_resource_by_fragments returns an empty list when no fragments match."""
        assert find_resource_by_fragments(["assets/a/x.png"], ("assets/b/",)) == []


class TestLocateVanillaSourceResources:
    """Tests for locating vanilla source resources in an asset archive."""

    def test_exact_paths(self, tmp_path: Path) -> None:
        """Tests exact path matching for resource location."""
        jar = _build_client_jar(tmp_path / "c.jar")
        with AssetArchive(jar) as archive:
            resources = locate_vanilla_source_resources(archive)
        for wood in VANILLA_WOODS:
            assert resources[wood] == f"assets/minecraft/textures/block/{wood}_planks.png"

    def test_fallback_fragment_search(self, tmp_path: Path) -> None:
        """Tests that fallback fragment search uniquely locates nested resources."""
        members = {f"assets/minecraft/textures/block/sub/{w}_planks.png": _png_bytes(_source_image(w)) for w in VANILLA_WOODS}
        jar = _zip_write(tmp_path / "c.jar", members)
        with AssetArchive(jar) as archive:
            resources = locate_vanilla_source_resources(archive)
        assert resources["oak"].endswith("sub/oak_planks.png")
        assert resources["dark_oak"].endswith("sub/dark_oak_planks.png")

    def test_missing_wood_raises(self, tmp_path: Path) -> None:
        """Tests that missing wood resources raise FileNotFoundError."""
        members = {f"assets/minecraft/textures/block/{w}_planks.png": b"x" for w in VANILLA_WOODS if w != "oak"}
        jar = _zip_write(tmp_path / "c.jar", members)
        with AssetArchive(jar) as archive, pytest.raises(FileNotFoundError):
            locate_vanilla_source_resources(archive)


class TestLocateVanillaFrontResources:
    """Tests for locating vanilla drawers front textures within an asset archive."""

    def test_exact_paths(self, tmp_path: Path) -> None:
        """Tests exact path matching for resource location."""
        jar = _build_drawers_jar(tmp_path / "d.jar")
        with AssetArchive(jar) as archive:
            resources = locate_vanilla_front_resources(archive, 1)
        for wood in VANILLA_WOODS:
            assert resources[wood] == f"assets/storagedrawers/textures/block/drawers_{wood}_front_1.png"

    def test_short_wood_does_not_match_longer_name(self, tmp_path: Path) -> None:
        """Tests that a short wood name does not match a longer wood name."""
        members = {f"assets/storagedrawers/textures/block/drawers_{w}_front_1.png": _png_bytes(_source_image(w)) for w in VANILLA_WOODS if w != "oak"}
        jar = _zip_write(tmp_path / "d.jar", members)
        with AssetArchive(jar) as archive, pytest.raises(FileNotFoundError):
            locate_vanilla_front_resources(archive, 1)

    def test_missing_shape_raises(self, tmp_path: Path) -> None:
        """Tests that missing shape resources raise FileNotFoundError."""
        members = {f"assets/storagedrawers/textures/block/drawers_{w}_front_1.png": _png_bytes(_source_image(w)) for w in VANILLA_WOODS}
        jar = _zip_write(tmp_path / "d.jar", members)
        with AssetArchive(jar) as archive, pytest.raises(FileNotFoundError):
            locate_vanilla_front_resources(archive, 4)


class TestLocateWoodPlanks:
    """Tests for locating wood plank textures within an asset archive."""

    def test_exact_path(self, tmp_path: Path) -> None:
        """Tests that the canonical plank path is returned verbatim."""
        jar = _build_input_jar(tmp_path / "m.jar", "biomesoplenty", "maple")
        with AssetArchive(jar) as archive:
            assert locate_wood_planks(archive, "biomesoplenty", "maple") == "assets/biomesoplenty/textures/block/maple_planks.png"

    def test_case_insensitive_fallback(self, tmp_path: Path) -> None:
        """Tests that a case-mismatched plank path is still located."""
        members = {"assets/biomesoplenty/textures/block/Maple_Planks.png": _png_bytes(_source_image("oak"))}
        jar = _zip_write(tmp_path / "m.jar", members)
        with AssetArchive(jar) as archive:
            assert locate_wood_planks(archive, "biomesoplenty", "maple") == "assets/biomesoplenty/textures/block/Maple_Planks.png"

    def test_missing_raises(self, tmp_path: Path) -> None:
        """Tests that a missing plank texture raises FileNotFoundError."""
        jar = _zip_write(tmp_path / "m.jar", {})
        with AssetArchive(jar) as archive, pytest.raises(FileNotFoundError):
            locate_wood_planks(archive, "biomesoplenty", "maple")

    def test_wrong_namespace_raises(self, tmp_path: Path) -> None:
        """Tests that a plank texture under a different namespace is not found."""
        members = {"assets/other/textures/block/maple_planks.png": _png_bytes(_source_image("oak"))}
        jar = _zip_write(tmp_path / "m.jar", members)
        with AssetArchive(jar) as archive, pytest.raises(FileNotFoundError):
            locate_wood_planks(archive, "biomesoplenty", "maple")


class TestFitChannelAffine:
    """Tests for fit_channel_affine covering exact fits, zero variance, and degenerate inputs."""

    def test_exact_linear(self) -> None:
        """Tests that a perfectly linear relation recovers slope and intercept."""
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [3.0, 5.0, 7.0, 9.0]
        slope, intercept = fit_channel_affine(xs, ys)
        assert slope == pytest.approx(2.0)
        assert intercept == pytest.approx(1.0)

    def test_zero_variance_returns_mean(self) -> None:
        """Tests that zero variance in source values yields a zero slope and the mean target as intercept."""
        xs = [5.0, 5.0, 5.0]
        ys = [10.0, 20.0, 30.0]
        slope, intercept = fit_channel_affine(xs, ys)
        assert slope == 0.0
        assert intercept == pytest.approx(20.0)

    def test_single_point_degenerate(self) -> None:
        """Tests that fitting a single data point returns a zero slope and the target value as intercept."""
        slope, intercept = fit_channel_affine([2.0], [4.0])
        assert slope == 0.0
        assert intercept == pytest.approx(4.0)


class TestFitTemplatePixel:
    """Tests for per-pixel template fitting of palette role, scale, and bias."""

    def test_recovers_known_role_scale_bias(self) -> None:
        """Tests that the fitting function recovers known role, scale, and bias values."""
        palette_a: list[tuple[int, int, int, int]] = [(10, 20, 30, 255), (40, 50, 60, 255), (80, 100, 120, 255)]
        palette_b: list[tuple[int, int, int, int]] = [(100, 110, 120, 255), (140, 150, 160, 255), (200, 210, 220, 255)]
        role_true = 1
        scale = 0.5
        bias = 7.0
        fronts = []
        for palette in (palette_a, palette_b):
            src = palette[role_true]
            fronts.append((round(scale * src[0] + bias), round(scale * src[1] + bias), round(scale * src[2] + bias)))
        role, fitted_scale, fitted_bias, err = fit_template_pixel(fronts, [palette_a, palette_b])
        assert role == role_true
        assert err == pytest.approx(0.0, abs=1.0)
        for c in range(3):
            assert fitted_scale[c] == pytest.approx(scale, abs=0.05)
            assert fitted_bias[c] == pytest.approx(bias, abs=2.0)

    def test_single_palette_does_not_crash(self) -> None:
        """Tests that fitting with a single-color palette does not crash and returns role 0."""
        palette: list[tuple[int, int, int, int]] = [(10, 20, 30, 255)]
        role, _scale, _bias, _err = fit_template_pixel([(5, 10, 15)], [palette])
        assert role == 0


class TestFitTemplateFromImages:
    """Tests for fitting a template from source and target images."""

    def test_expected_shape(self) -> None:
        """Tests that the fitted model has the expected dimensions."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        assert model.width == 16
        assert model.height == 16
        assert len(model.role_map) == 16
        assert len(model.scale_map[0]) == 16
        assert len(model.bias_map[0]) == 16
        assert len(model.rmse_map[0]) == 16

    def test_role_map_reproduces_pattern(self) -> None:
        """Tests that the fitted model's role map reproduces the expected pattern."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        for y in range(16):
            for x in range(16):
                assert model.role_map[y][x] == (x + y) % _PATTERN_LEN

    def test_scale_and_bias_recovered(self) -> None:
        """Tests that the fitted template recovers the expected per-channel scale and bias within tolerance."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        for y in range(16):
            for x in range(16):
                scale = model.scale_map[y][x]
                bias = model.bias_map[y][x]
                for c in range(3):
                    assert scale[c] == pytest.approx(0.5, abs=0.05)
                    assert bias[c] == pytest.approx(20.0, abs=2.0)


class TestRenderTemplate:
    """Tests for rendering a fitted template against a target palette."""

    def test_produces_16x16_rgba(self) -> None:
        """Tests that rendering the fitted template produces a 16x16 RGBA image."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        out = render_template(model, extract_wood_palette(sources["oak"]))
        assert out.size == (16, 16)
        assert out.mode == "RGBA"

    def test_matches_target(self) -> None:
        """Tests that rendering the fitted template exactly reproduces the target image's flattened data."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        out = render_template(model, extract_wood_palette(sources["oak"]))
        assert list(out.get_flattened_data()) == list(targets["oak"].get_flattened_data())

    def test_output_fully_opaque(self) -> None:
        """Tests that rendered template output is fully opaque."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        out = render_template(model, extract_wood_palette(sources["oak"]))
        for pixel in out.get_flattened_data():
            assert pixel[3] == 255


class TestTemplateMetrics:
    """Tests for template metric computation."""

    def test_keys(self) -> None:
        """Tests that template metrics contain all expected keys."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        m = template_metrics(model)
        for key in (
            "pixels",
            "rmse_le_0_1",
            "rmse_le_0_5",
            "rmse_le_1_0",
            "rmse_le_2_0",
            "rmse_le_3_0",
            "rmse_le_5_0",
            "training_mean_pixel_rmse",
            "training_max_pixel_rmse",
            "max_abs_bias",
            "max_abs_scale_delta",
        ):
            assert key in m
        assert m["pixels"] == 256

    def test_perfect_fit_has_zero_rmse(self) -> None:
        """Tests that fitting a template to identical images yields zero RMSE."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        m = template_metrics(model)
        assert m["rmse_le_0_5"] == 256
        assert m["training_max_pixel_rmse"] == pytest.approx(0.0, abs=0.5)


class TestValidationMetrics:
    """Tests for pixel-exact and error metrics between two textures."""

    def test_identical_images(self) -> None:
        """Tests validation metrics when the two input images are identical."""
        img = _source_image("oak")
        v = validation_metrics(img, img)
        assert v.exact_pixels == 256
        assert v.total_pixels == 256
        assert v.exact_ratio == 1.0
        assert v.mae == 0.0
        assert v.rmse == 0.0
        assert v.max_error == 0.0

    def test_known_offset(self) -> None:
        """Tests validation metrics for two images with a known constant pixel offset."""
        a = Image.new("RGBA", (16, 16), (100, 100, 100, 255))
        b = Image.new("RGBA", (16, 16), (110, 110, 110, 255))
        v = validation_metrics(a, b)
        assert v.exact_pixels == 0
        assert v.mae == pytest.approx(10.0)
        assert v.rmse == pytest.approx(10.0)
        assert v.max_error == pytest.approx(10.0)

    def test_partial_exact(self) -> None:
        """Tests that a single differing pixel is counted as 255 exact pixels.

        Creates a 16x16 RGBA image, copies it, modifies one pixel, and verifies
        that validation_metrics reports 255 exact matching pixels.
        """
        a = Image.new("RGBA", (16, 16), (100, 100, 100, 255))
        b = a.copy()
        b.putpixel((0, 0), (200, 200, 200, 255))
        v = validation_metrics(a, b)
        assert v.exact_pixels == 255

    def test_size_mismatch_raises(self) -> None:
        """Tests that validation_metrics raises ValueError for mismatched image sizes.

        Creates a 16x16 RGBA image and an 8x8 RGBA image, then asserts that
        calling validation_metrics with them raises ValueError.
        """
        a = Image.new("RGBA", (16, 16))
        b = Image.new("RGBA", (8, 8))
        with pytest.raises(ValueError):
            validation_metrics(a, b)


class TestLeaveOneOutValidation:
    """Tests for leave-one-out template validation, ensuring all woods are present and reconstruction is perfect."""

    def test_all_six_woods_present(self) -> None:
        """Tests that leave_one_out_template_validation returns all six vanilla woods.

        Builds a vanilla source/target pair and verifies that the result keys
        match the expected set of VANILLA_WOODS.
        """
        sources, targets = _vanilla_pair()
        results = leave_one_out_template_validation(sources, targets)
        assert set(results.keys()) == set(VANILLA_WOODS)

    def test_perfect_reconstruction(self) -> None:
        """Tests that leave_one_out_template_validation perfectly reconstructs each wood.

        Builds a vanilla source/target pair and asserts that every wood's metrics
        report exactly 256 exact pixels.
        """
        sources, targets = _vanilla_pair()
        results = leave_one_out_template_validation(sources, targets)
        for wood, metrics in results.items():
            assert metrics.exact_pixels == 256, f"{wood} produced {metrics.exact_pixels} exact pixels"


class TestBandOf:
    """Tests for the band_of function, covering border, inner ring, handle, and interior classification."""

    def test_border_is_outer_ring(self) -> None:
        """Tests that band_of identifies the outer ring as "border" at threshold 1.0.

        Checks all pixels along the four edges of a 16x16 grid to ensure
        they are classified as "border".
        """
        for x in (0, 15):
            for y in range(16):
                assert band_of(x, y, 1.0) == "border"
        for y in (0, 15):
            for x in range(16):
                assert band_of(x, y, 1.0) == "border"

    def test_border_is_exactly_60_pixels(self) -> None:
        """Tests that the outer 1px ring is exactly 60 pixels."""
        count = sum(1 for y in range(16) for x in range(16) if band_of(x, y, 1.0) == "border")
        assert count == 60

    def test_visible_pixels_matches_constant(self) -> None:
        """Tests that the number of non-border pixels matches VISIBLE_PIXELS_PER_FRONT."""
        count = sum(1 for y in range(16) for x in range(16) if band_of(x, y, 1.0) != "border")
        assert count == VISIBLE_PIXELS_PER_FRONT

    def test_handle_when_scale_low(self) -> None:
        """Tests that a mean scale below the threshold classifies as handle."""
        assert band_of(5, 5, 0.0) == "handle"

    def test_inner_ring_at_row_or_col_1_or_14(self) -> None:
        """Tests that rows/cols 1 and 14 classify as inner_ring."""
        assert band_of(1, 5, 1.0) == "inner_ring"
        assert band_of(14, 5, 1.0) == "inner_ring"
        assert band_of(5, 1, 1.0) == "inner_ring"
        assert band_of(5, 14, 1.0) == "inner_ring"

    def test_interior_default(self) -> None:
        """Tests that non-border, non-ring pixels classify as interior."""
        assert band_of(5, 5, 1.0) == "interior"


class TestMeanScaleOf:
    """Tests for the mean scale of a fitted template across image channels."""

    def test_average_of_three_channels(self) -> None:
        """Tests that mean_scale_of averages the three per-channel scales."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        for y in range(16):
            for x in range(16):
                assert mean_scale_of(model, x, y) == pytest.approx(0.5, abs=0.05)


class TestBandExactSummary:
    """Tests for band exact-summary reporting."""

    def test_all_bands_present(self) -> None:
        """Tests that every band key appears in the summary."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        bands = band_exact_summary(sources, targets, model)
        assert set(bands.keys()) == {"interior", "inner_ring", "handle", "border"}

    def test_border_hits_all_256_in_perfect_fit(self) -> None:
        """Tests that a perfect fit reproduces every border pixel."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        bands = band_exact_summary(sources, targets, model)
        for wood, (exact, total, _mae, _rmse) in bands["border"].items():
            assert exact == total, f"wood {wood} border mismatch: {exact}/{total}"

    def test_interior_hits_all_256_in_perfect_fit(self) -> None:
        """Tests that a perfect fit reproduces every interior pixel."""
        sources, targets = _vanilla_pair()
        model = fit_template_from_images(sources, targets)
        bands = band_exact_summary(sources, targets, model)
        for _wood, (exact, total, _mae, _rmse) in bands["interior"].items():
            assert exact == total


class TestBlockstateJson:
    """Tests for blockstate JSON generation."""

    def test_drawer_has_four_facings(self) -> None:
        """Tests that a drawers blockstate carries the four cardinal facings."""
        data = drawer_blockstate("storagedrawersextra:block/x")
        assert set(data["variants"].keys()) == {"facing=north", "facing=east", "facing=south", "facing=west"}
        assert data["variants"]["facing=north"]["y"] == 0
        assert data["variants"]["facing=east"]["y"] == 90

    def test_trim_single_variant(self) -> None:
        """Tests that the trim blockstate emits a single empty-key variant."""
        data = trim_blockstate("storagedrawersextra:block/y")
        assert data == {"variants": {"": {"model": "storagedrawersextra:block/y"}}}


class TestBlockModels:
    """Tests for block model generation functions."""

    def test_full_drawer_model(self) -> None:
        """Tests that the full drawer model generates the expected parent and texture references for a given wood type and drawer count."""
        data = full_drawer_model("biomesoplenty", "maple", 2)
        assert data["parent"] == "storagedrawers:block/full_drawers_orientable"
        assert data["textures"]["front"] == "storagedrawersextra:block/biomesoplenty/drawers_maple_front_2"
        assert data["textures"]["side"] == "biomesoplenty:block/maple_planks"
        assert data["textures"]["top"] == "biomesoplenty:block/maple_planks"

    def test_half_drawer_model(self) -> None:
        """Tests that half_drawer_model generates the expected half drawer model data."""
        data = half_drawer_model("biomesoplenty", "maple", 4)
        assert data["parent"] == "storagedrawers:block/half_drawers_orientable"
        assert data["textures"]["back"] == "biomesoplenty:block/maple_planks"
        assert data["textures"]["front"] == "storagedrawersextra:block/biomesoplenty/drawers_maple_front_4"

    def test_trim_model(self) -> None:
        """Tests that trim_model generates the expected plank model data."""
        data = trim_model("biomesoplenty", "maple")
        assert data["parent"] == "minecraft:block/cube_all"
        assert data["textures"] == {"all": "biomesoplenty:block/maple_planks"}

    def test_item_model(self) -> None:
        """Tests that item_model returns the expected parent model mapping."""
        assert item_model("storagedrawersextra:block/x") == {"parent": "storagedrawersextra:block/x"}


class TestKubejsStartupScript:
    """Tests for the KubeJS startup script generation."""

    def test_contains_material_prefix(self) -> None:
        """Tests that the generated script contains the material prefix for the mod and block."""
        script = kubejs_startup_script("maple", "biomesoplenty_maple")
        assert "'storagedrawersextra'" in script
        assert "'biomesoplenty_maple'" in script

    def test_display_name_title_cased(self) -> None:
        """Tests that the display name is title-cased in the generated script."""
        script = kubejs_startup_script("maple", "biomesoplenty_maple")
        assert "[SD Maple]" in script

    def test_multi_word_title_cased(self) -> None:
        """Tests that multi-word block names are title-cased in the generated script."""
        script = kubejs_startup_script("twilight_oak", "twilightforest_twilight_oak")
        assert "[SD Twilight Oak]" in script

    def test_class_paths_use_minecraft_segment(self) -> None:
        """Tests that the startup script uses fully qualified class paths with the Minecraft segment."""
        script = kubejs_startup_script("maple", "ns_maple")
        assert "com.jaquadro.minecraft.storagedrawers.core.ModBlockVariants" in script
        assert "com.jaquadro.minecraft.storagedrawersextra.core.ModBlocks" in script
        assert "com.jaquadro.minecraft.storagedrawersextra.core.ModItems" in script

    def test_variant_data_class_referenced(self) -> None:
        """Tests that the startup script references the VariantData inner class."""
        script = kubejs_startup_script("maple", "ns_maple")
        assert "ModBlockVariants$VariantData" in script

    def test_registers_both_block_and_item(self) -> None:
        """Tests that the startup script registers both a block variant and its item."""
        script = kubejs_startup_script("maple", "ns_maple")
        assert "registerVariant" in script
        assert "registerVariantItem" in script


class TestWriters:
    """Tests for JSON and text file writer utilities."""

    def test_write_json_round_trip(self, tmp_path: Path) -> None:
        """Tests that write_json writes JSON that can be read back to the original data."""
        path = tmp_path / "deep" / "x.json"
        write_json(path, {"a": 1, "b": [1, 2]})
        assert json.loads(path.read_text()) == {"a": 1, "b": [1, 2]}

    def test_write_json_trailing_newline(self, tmp_path: Path) -> None:
        """Tests that write_json terminates the file with a newline."""
        path = tmp_path / "x.json"
        write_json(path, {})
        assert path.read_text().endswith("\n")

    def test_write_json_custom_indent(self, tmp_path: Path) -> None:
        """Tests that write_json honors a custom indentation level."""
        path = tmp_path / "x.json"
        write_json(path, {"k": "v"}, indent=2)
        text = path.read_text()
        assert '  "k"' in text

    def test_write_text_file(self, tmp_path: Path) -> None:
        """Tests that write_text_file writes the given text to a file."""
        path = tmp_path / "deep" / "x.js"
        write_text_file(path, "hello\n")
        assert path.read_text() == "hello\n"


class TestResolveClientJar:
    """Tests for resolve_client_jar covering default, relative, and absolute path handling."""

    def test_default_under_gradle_cache(self, tmp_path: Path) -> None:
        """Tests that the default client jar resolves under the Gradle cache."""
        default = resolve_client_jar(None, tmp_path)
        assert "1.20.1" in str(default)
        assert default.name == "client.jar"

    def test_relative_resolved_against_repo_root(self, tmp_path: Path) -> None:
        """Tests that a relative client jar path is resolved against the repository root."""
        assert resolve_client_jar(Path("local.jar"), tmp_path) == tmp_path / "local.jar"

    def test_absolute_kept(self, tmp_path: Path) -> None:
        """Tests that an absolute client jar path is returned unchanged."""
        abs_path = Path("/tmp/abs-client.jar")
        assert resolve_client_jar(abs_path, tmp_path) == abs_path


class TestResolveStorageDrawersJar:
    """Tests for resolving StorageDrawers jar file paths from explicit files, directories, or default sync downloads."""

    def test_explicit_file(self, tmp_path: Path) -> None:
        """Tests that an explicit file path is returned unchanged."""
        jar = _zip_write(tmp_path / "StorageDrawers-1.20.1.jar", {})
        assert resolve_storage_drawers_jar(jar, tmp_path) == jar

    def test_directory_picks_first_match(self, tmp_path: Path) -> None:
        """Tests that a directory path selects the first matching StorageDrawers jar."""
        _zip_write(tmp_path / "StorageDrawers-b.jar", {})
        first = _zip_write(tmp_path / "StorageDrawers-a.jar", {})
        assert resolve_storage_drawers_jar(tmp_path, tmp_path) == first

    def test_directory_no_match_raises(self, tmp_path: Path) -> None:
        """Tests that a directory with no matching jar raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            resolve_storage_drawers_jar(tmp_path, tmp_path)

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        """Tests that a missing path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            resolve_storage_drawers_jar(tmp_path / "nope", tmp_path)

    def test_default_uses_sync_downloads(self, tmp_path: Path) -> None:
        """Tests that the default lookup uses the sync/downloads directory."""
        _zip_write(tmp_path / "sync/downloads/StorageDrawers-1.jar", {})
        result = resolve_storage_drawers_jar(None, tmp_path)
        assert result.name == "StorageDrawers-1.jar"


class TestResolveInputJar:
    """Tests for resolve_input_jar input validation and path resolution."""

    def test_none_raises(self, tmp_path: Path) -> None:
        """Tests that passing None raises ValueError."""
        with pytest.raises(ValueError):
            resolve_input_jar(None, tmp_path)

    def test_missing_raises(self, tmp_path: Path) -> None:
        """Tests that a missing jar file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            resolve_input_jar(Path("nope.jar"), tmp_path)

    def test_existing_returned(self, tmp_path: Path) -> None:
        """Tests that an existing input jar path is returned as-is."""
        jar = tmp_path / "m.jar"
        jar.write_bytes(b"")
        assert resolve_input_jar(jar, tmp_path) == jar


class TestResolveOutputRoot:
    """Tests for resolve_output_root default, relative, and absolute path handling."""

    def test_default(self, tmp_path: Path) -> None:
        """Tests that a missing output root falls back to the default path."""
        assert resolve_output_root(None, tmp_path) == tmp_path / "sync/kubejs"

    def test_relative(self, tmp_path: Path) -> None:
        """Tests that a relative output root is resolved against the base path."""
        assert resolve_output_root(Path("out"), tmp_path) == tmp_path / "out"

    def test_absolute(self, tmp_path: Path) -> None:
        """Tests that an absolute output root path is returned unchanged."""
        abs_path = Path("/tmp/abs-output")
        assert resolve_output_root(abs_path, tmp_path) == abs_path


class TestParseWoodType:
    """Tests for parse_wood_type parsing, whitespace handling, and malformed input."""

    def test_valid(self) -> None:
        """Tests that a valid namespaced wood type is parsed correctly."""
        assert parse_wood_type("biomesoplenty:maple") == ("biomesoplenty", "maple")

    def test_whitespace_stripped(self) -> None:
        """Tests that parse_wood_type strips surrounding whitespace from input."""
        assert parse_wood_type(" biomesoplenty : maple ") == ("biomesoplenty", "maple")

    def test_none_raises(self) -> None:
        """Tests that parse_wood_type raises ValueError when given None."""
        with pytest.raises(ValueError):
            parse_wood_type(None)

    @pytest.mark.parametrize("spec", ["no_colon", ":wood", "ns:", "", "a:b:c", "a:b:c:d"])
    def test_malformed_raises(self, spec: str) -> None:
        """Tests that parsing a malformed wood type raises ValueError."""
        with pytest.raises(ValueError):
            parse_wood_type(spec)


class TestLoadVanillaDataset:
    """Tests for load_vanilla_dataset completeness and shape-specific target resources."""

    def test_returns_all_six(self, tmp_path: Path) -> None:
        """Tests that the vanilla dataset returns entries for all six wood types."""
        client = _build_client_jar(tmp_path / "c.jar")
        drawers = _build_drawers_jar(tmp_path / "d.jar")
        sources, targets, source_resources, target_resources = load_vanilla_dataset(client, drawers, 2)
        assert set(sources.keys()) == set(VANILLA_WOODS)
        assert set(targets.keys()) == set(VANILLA_WOODS)
        assert all(k in source_resources for k in VANILLA_WOODS)
        assert all(k in target_resources for k in VANILLA_WOODS)

    def test_shape_specific_targets(self, tmp_path: Path) -> None:
        """Tests that each wood type has the expected shape-specific target resource."""
        client = _build_client_jar(tmp_path / "c.jar")
        drawers = _build_drawers_jar(tmp_path / "d.jar")
        _sources, _targets, _src_res, tgt_res = load_vanilla_dataset(client, drawers, 4)
        for wood in VANILLA_WOODS:
            assert tgt_res[wood].endswith(f"drawers_{wood}_front_4.png")


class TestCmdSelfTest:
    """Tests for cmd_self_test execution and success output."""

    def test_runs_and_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Tests that the self-test command runs and prints PASS."""
        args = build_parser().parse_args(["self-test"])
        rc = cmd_self_test(args)
        assert rc == 0
        assert "PASS" in capsys.readouterr().out


class TestCmdValidate:
    """Tests for the validate subcommand."""

    def test_runs_and_prints_each_front(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Tests that the validate command prints each expected front."""
        client = _build_client_jar(tmp_path / "c.jar")
        drawers = _build_drawers_jar(tmp_path / "d.jar")
        args = build_parser().parse_args(["--client-jar", str(client), "--storage-drawers", str(drawers), "validate"])
        rc = cmd_validate(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "FRONT_1" in out
        assert "FRONT_2" in out
        assert "FRONT_4" in out

    def test_writes_json_report_when_requested(self, tmp_path: Path) -> None:
        """Tests that validate writes a JSON report when the report option is given."""
        client = _build_client_jar(tmp_path / "c.jar")
        drawers = _build_drawers_jar(tmp_path / "d.jar")
        report_path = tmp_path / "report.json"
        args = build_parser().parse_args(["--client-jar", str(client), "--storage-drawers", str(drawers), "validate", "--report", str(report_path)])
        cmd_validate(args)
        payload = json.loads(report_path.read_text())
        assert "fronts" in payload
        for shape in FRONT_SHAPES:
            assert f"front_{shape}" in payload["fronts"]


class TestCmdGenerate:
    """Tests for the generate subcommand."""

    def test_generates_all_files(self, tmp_path: Path) -> None:
        """Tests that running generate with all inputs creates every expected output file."""
        client = _build_client_jar(tmp_path / "c.jar")
        drawers = _build_drawers_jar(tmp_path / "d.jar")
        modjar = _build_input_jar(tmp_path / "m.jar", "biomesoplenty", "maple")
        output_root = tmp_path / "kubejs"
        args = build_parser().parse_args(
            [
                "--client-jar",
                str(client),
                "--storage-drawers",
                str(drawers),
                "--input-jar",
                str(modjar),
                "--wood-type",
                "biomesoplenty:maple",
                "--output-root",
                str(output_root),
            ]
        )
        rc = cmd_generate(args)
        assert rc == 0
        prefix = "biomesoplenty_maple"
        for shape in FRONT_SHAPES:
            assert (output_root / "assets/storagedrawersextra/textures/block/biomesoplenty" / f"drawers_maple_front_{shape}.png").is_file()
        for shape in FRONT_SHAPES:
            for kind in ("full", "half"):
                assert (output_root / "assets/storagedrawersextra/blockstates" / f"{prefix}_{kind}_drawers_{shape}.json").is_file()
        assert (output_root / "assets/storagedrawersextra/blockstates" / f"{prefix}_trim.json").is_file()
        for shape in FRONT_SHAPES:
            for kind in ("full", "half"):
                assert (output_root / "assets/storagedrawersextra/models/block" / f"{prefix}_{kind}_drawers_{shape}.json").is_file()
        assert (output_root / "assets/storagedrawersextra/models/block" / f"{prefix}_trim.json").is_file()
        for shape in FRONT_SHAPES:
            for kind in ("full", "half"):
                assert (output_root / "assets/storagedrawersextra/models/item" / f"{prefix}_{kind}_drawers_{shape}.json").is_file()
        assert (output_root / "assets/storagedrawersextra/models/item" / f"{prefix}_trim.json").is_file()
        script_path = output_root / "startup_scripts/060_integration/000_storage_drawers" / f"{prefix}.js"
        assert script_path.is_file()
        assert "biomesoplenty_maple" in script_path.read_text()

    def test_missing_input_jar_raises(self, tmp_path: Path) -> None:
        """Tests that omitting the input jar raises a ValueError during generation."""
        client = _build_client_jar(tmp_path / "c.jar")
        drawers = _build_drawers_jar(tmp_path / "d.jar")
        args = build_parser().parse_args(
            ["--client-jar", str(client), "--storage-drawers", str(drawers), "--wood-type", "biomesoplenty:maple", "--output-root", str(tmp_path / "kubejs")]
        )
        with pytest.raises(ValueError):
            cmd_generate(args)


class TestParser:
    """Tests for the command-line argument parser."""

    def test_parser_defaults(self) -> None:
        """Tests that default parser arguments are None when no arguments are given."""
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None
        assert args.input_jar is None
        assert args.wood_type is None

    def test_self_test_subcommand(self) -> None:
        """Tests that the 'self-test' subcommand is parsed correctly with its handler."""
        parser = build_parser()
        args = parser.parse_args(["self-test"])
        assert args.command == "self-test"
        assert args.func is cmd_self_test

    def test_validate_subcommand(self) -> None:
        """Tests that the 'validate' subcommand is parsed correctly with its handler."""
        parser = build_parser()
        args = parser.parse_args(["validate"])
        assert args.command == "validate"
        assert args.func is cmd_validate


class TestMain:
    """Tests for the main entry point of the drawer generation CLI."""

    def test_no_args_missing_input_raises_exit_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a bare invocation with no input jar exits 2 with ERROR on stderr."""
        monkeypatch.setattr("sys.argv", ["generate_drawers.py", "--repo-root", str(tmp_path)])
        rc = generate_drawers.main()
        assert rc == 2
        assert "ERROR" in capsys.readouterr().err

    def test_self_test_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the self-test subcommand exits zero."""
        monkeypatch.setattr("sys.argv", ["generate_drawers.py", "self-test"])
        assert generate_drawers.main() == 0

    def test_validate_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the validate subcommand exits zero on synthetic assets."""
        client = _build_client_jar(tmp_path / "c.jar")
        drawers = _build_drawers_jar(tmp_path / "d.jar")
        monkeypatch.setattr("sys.argv", ["generate_drawers.py", "--client-jar", str(client), "--storage-drawers", str(drawers), "validate"])
        assert generate_drawers.main() == 0

    def test_generate_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the generate command exits zero on synthetic assets."""
        client = _build_client_jar(tmp_path / "c.jar")
        drawers = _build_drawers_jar(tmp_path / "d.jar")
        modjar = _build_input_jar(tmp_path / "m.jar", "biomesoplenty", "maple")
        monkeypatch.setattr(
            "sys.argv",
            [
                "generate_drawers.py",
                "--client-jar",
                str(client),
                "--storage-drawers",
                str(drawers),
                "--input-jar",
                str(modjar),
                "--wood-type",
                "biomesoplenty:maple",
                "--output-root",
                str(tmp_path / "kubejs"),
            ],
        )
        assert generate_drawers.main() == 0


class TestDataclasses:
    """Tests for the dataclass surface of TemplateModel and ValidationMetrics."""

    def test_template_model_is_frozen(self) -> None:
        """Tests that TemplateModel is immutable and raises on attribute assignment."""
        model = TemplateModel(width=1, height=1, role_map=((0,),), scale_map=(((1.0, 1.0, 1.0),),), bias_map=(((0.0, 0.0, 0.0),),), rmse_map=((0.0,),))
        with pytest.raises(dataclasses.FrozenInstanceError):
            model.width = 2

    def test_validation_metrics_fields(self) -> None:
        """Tests that ValidationMetrics stores its provided field values."""
        v = ValidationMetrics(exact_pixels=1, total_pixels=2, exact_ratio=0.5, mae=1.0, rmse=2.0, max_error=3.0, p95_error=2.5)
        assert v.exact_pixels == 1
        assert v.exact_ratio == 0.5
        assert v.max_error == 3.0
