"""The shipped Wazuh rules stay in step with what Electric Fence forwards."""

from pathlib import Path
import xml.etree.ElementTree as ET

from custom_components.electric_fence.models import ChangeAction, ChangeKind

RULES = Path(__file__).parent.parent / "docs" / "wazuh" / "electric_fence_rules.xml"
EVENT_TYPES = {
    "login_failed",
    "login_succeeded",
    "new_country_login",
    "settings_changed",
}


def test_wazuh_rules_xml() -> None:
    """The rules parse, use unique IDs in range, and name real events."""
    root = ET.fromstring(  # noqa: S314 - our own file
        f"<root>{RULES.read_text(encoding='utf-8')}</root>"
    )
    rules = root.findall(".//rule")
    ids = [int(rule.attrib["id"]) for rule in rules]
    assert ids
    assert len(ids) == len(set(ids))
    assert all(100500 <= rule_id <= 100520 for rule_id in ids)
    assert {group.attrib["name"] for group in root.findall("group")} == {
        "electric_fence,"
    }
    for rule in rules:
        assert rule.find("description") is not None
        assert 0 <= int(rule.attrib["level"]) <= 15
    fields: dict[str, set[str]] = {}
    for field in root.iter("field"):
        fields.setdefault(field.attrib["name"], set()).add(
            (field.text or "").strip("^$")
        )
    assert fields["event_type"] <= EVENT_TYPES
    assert fields["action"] <= {str(action) for action in ChangeAction}
    assert fields["kind"] <= {str(kind) for kind in ChangeKind}
    assert fields["actor_kind"] <= {"user", "outside", "system"}
