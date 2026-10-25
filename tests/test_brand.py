"""Tests for the brand images and the sidebar glyph source."""

from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "custom_components" / "electric_fence" / "brand"
GLYPH = ROOT / "brand-src" / "sidebar-glyph.svg"
SVG_NS = "{http://www.w3.org/2000/svg}"

ICON_SIZES = {
    "icon.png": (256, 256),
    "icon@2x.png": (512, 512),
    "dark_icon.png": (256, 256),
    "dark_icon@2x.png": (512, 512),
}
LOGO_HEIGHTS = {
    "logo.png": 128,
    "logo@2x.png": 256,
    "dark_logo.png": 128,
    "dark_logo@2x.png": 256,
}
HOUSE = (0x18, 0xBC, 0xF2)
FENCE_LIGHT = (0x37, 0x47, 0x4F)
FENCE_DARK = (0xB0, 0xBE, 0xC5)


def _has_colour(image: Image.Image, colour: tuple[int, int, int]) -> bool:
    """Return whether an opaque pixel is within 8 per channel of the colour."""
    return any(
        a == 255
        and all(abs(c - t) <= 8 for c, t in zip((r, g, b), colour, strict=True))
        for r, g, b, a in image.get_flattened_data()
    )


def test_brand_files_are_exactly_the_served_set() -> None:
    """Every image Home Assistant serves is shipped, and nothing else."""
    assert {p.name for p in BRAND.iterdir()} == set(ICON_SIZES) | set(LOGO_HEIGHTS)


@pytest.mark.parametrize(("name", "size"), ICON_SIZES.items())
def test_icon_format(name: str, size: tuple[int, int]) -> None:
    """Icons are square RGBA images with a transparent background."""
    with Image.open(BRAND / name) as image:
        assert image.mode == "RGBA"
        assert image.size == size
        assert image.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize(("name", "height"), LOGO_HEIGHTS.items())
def test_logo_format(name: str, height: int) -> None:
    """Logos are landscape RGBA images with a transparent background."""
    with Image.open(BRAND / name) as image:
        assert image.mode == "RGBA"
        assert image.height == height
        assert image.width > 2 * height
        assert image.getpixel((image.width - 1, 0))[3] == 0


@pytest.mark.parametrize(
    ("light", "dark"),
    [("icon.png", "dark_icon.png"), ("logo.png", "dark_logo.png")],
)
def test_dark_variant_uses_light_fence(light: str, dark: str) -> None:
    """The dark variants draw the fence light grey; the house stays blue."""
    with (
        Image.open(BRAND / light) as light_image,
        Image.open(BRAND / dark) as dark_image,
    ):
        light_rgba = light_image.convert("RGBA")
        dark_rgba = dark_image.convert("RGBA")
        assert _has_colour(light_rgba, FENCE_LIGHT)
        assert not _has_colour(light_rgba, FENCE_DARK)
        assert _has_colour(dark_rgba, FENCE_DARK)
        assert not _has_colour(dark_rgba, FENCE_LIGHT)
        assert _has_colour(light_rgba, HOUSE)
        assert _has_colour(dark_rgba, HOUSE)


def test_sidebar_glyph_is_one_path() -> None:
    """The glyph is a single filled path on a 24 grid, as icon sets need."""
    root = ET.parse(GLYPH).getroot()  # noqa: S314 - a file in this repository
    assert root.get("viewBox") == "0 0 24 24"
    elements = [el for el in root.iter() if el is not root]
    assert [el.tag for el in elements] == [f"{SVG_NS}path"]
    path = elements[0]
    assert path.get("d")
    assert not {k for k in path.attrib if k.startswith("stroke")}
