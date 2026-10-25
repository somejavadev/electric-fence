"""Contracts: the frontend internals the sidebar icon module relies on.

These tests characterise the Home Assistant frontend, not Electric Fence. If
one fails, re-check `frontend/src/electric-fence-icons.ts`: the sidebar icon
may stay blank when the module loads after the sidebar has drawn.
"""

from pathlib import Path

import hass_frontend
import pytest

BUNDLE = Path(hass_frontend.__file__).parent / "frontend_latest"


@pytest.fixture(scope="module")
def bundle_text() -> str:
    """Return the text of every modern frontend chunk."""
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in BUNDLE.glob("*.js")
    )


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("customIcons", id="custom icon registry on window"),
        pytest.param('"ha-icon"', id="ha-icon element"),
        pytest.param("_setCustomPath", id="ha-icon resolves custom icon sets"),
        pytest.param("this._legacy", id="ha-icon caches a missing icon set"),
    ],
)
def test_frontend_has_icon_internals(bundle_text: str, marker: str) -> None:
    """The frontend still has what the icon module's refresh depends on."""
    assert marker in bundle_text, (
        f"{marker} is gone from the frontend; the sidebar glyph refresh in "
        "frontend/src/electric-fence-icons.ts needs re-checking"
    )
