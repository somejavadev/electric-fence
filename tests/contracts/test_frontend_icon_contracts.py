"""Contracts: the frontend internals the sidebar icon module relies on.

These tests characterise the Home Assistant frontend, not Electric Fence. If
one fails, re-check `frontend/src/electric-fence-icons.ts`: the sidebar icon
may stay blank when the module loads after the sidebar has drawn.
"""

from pathlib import Path
import re

import hass_frontend
import pytest

BUNDLE = Path(hass_frontend.__file__).parent / "frontend_latest"

# Patterns over the minified bundle, anchored so that look-alike names
# (`customIconsets`, `this._legacyCanvasSize`) do not match.
MARKERS = {
    "custom icon registry on window": re.compile(r'"customIcons"in\s*\w+'),
    "ha-icon resolves custom icon sets": re.compile(r"\b_setCustomPath\("),
    "ha-icon caches a missing icon set": re.compile(r"this\._legacy=!0\b"),
    "ha-icon clears that cache": re.compile(r"this\._legacy=!1\b"),
    "ha-icon looks the icon up again when `icon` changes": re.compile(
        r'willUpdate\((\w+)\)\{super\.willUpdate\(\1\),\1\.has\("icon"\)&&'
        r"\([^{}]{0,200}?this\._loadIcon\(\)"
    ),
}

# The custom element registration `[(0,x.EM)("ha-icon")],I)`, not a mere use
# such as `document.createElement("ha-icon")`.
HA_ICON_DEFINED = re.compile(r'\("ha-icon"\)\],[A-Za-z_$]+\)')

# Look-alike identifiers that exist elsewhere in the frontend.
DECOYS = (
    'this._legacyCanvasSize=!0;this._legacyOnDecode=!1;"customIconsets"in c;'
    'willUpdate(e){super.willUpdate(e),e.has("icon")&&(this._loadIcon())}'
    "_setCustomPath(e,t)"
)


def missing_markers(chunk: str) -> list[str]:
    """Return the names of the markers the chunk does not contain."""
    return [name for name, marker in MARKERS.items() if not marker.search(chunk)]


@pytest.fixture(scope="module")
def ha_icon_chunks() -> dict[str, str]:
    """Return every frontend chunk that bundles a copy of ha-icon."""
    chunks = {
        path.name: text
        for path in BUNDLE.glob("*.js")
        if HA_ICON_DEFINED.search(
            text := path.read_text(encoding="utf-8", errors="replace")
        )
    }
    assert chunks, "ha-icon is no longer defined in the frontend bundle"
    return chunks


def test_frontend_has_icon_internals(ha_icon_chunks: dict[str, str]) -> None:
    """Every copy of ha-icon still has what the icon module's refresh needs."""
    missing = {
        name: gaps
        for name, chunk in ha_icon_chunks.items()
        if (gaps := missing_markers(chunk))
    }
    assert missing == {}, (
        "the sidebar glyph refresh in frontend/src/electric-fence-icons.ts "
        f"needs re-checking: {missing}"
    )


def test_look_alike_names_do_not_satisfy_the_contract() -> None:
    """Unrelated identifiers that share a prefix are not taken as the real ones."""
    assert missing_markers(DECOYS) == [
        "custom icon registry on window",
        "ha-icon caches a missing icon set",
        "ha-icon clears that cache",
    ]
