"""The integration's runtime requirement and the test pin stay equal."""

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def test_maxminddb_pin_matches_manifest() -> None:
    """Dependabot can't update manifest.json, so a drift must fail here."""
    manifest = json.loads(
        (ROOT / "custom_components/electric_fence/manifest.json").read_text()
    )
    (runtime,) = [r for r in manifest["requirements"] if r.startswith("maxminddb")]
    pins = re.findall(
        r"(?m)^maxminddb==\S+", (ROOT / "requirements_test.txt").read_text()
    )
    assert pins == [runtime]
