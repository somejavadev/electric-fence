"""The shipped Wazuh rules stay in step with what Electric Fence forwards."""

from pathlib import Path
import re
import xml.etree.ElementTree as ET

from custom_components.electric_fence.forwarding.format import EVENT_TYPES
from custom_components.electric_fence.models import ChangeAction, ChangeKind

RULES = Path(__file__).parent.parent / "docs" / "wazuh" / "electric_fence_rules.xml"


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
    assert fields["kind"] <= {str(kind) for kind in ChangeKind}
    assert fields["actor_kind"] <= {"user", "outside", "system"}
    assert "action" not in fields, "match actions on the JSON text instead"
    assert fields.keys() <= {"event_type", "kind", "actor_kind"}


def test_wazuh_rules_references_and_precedence() -> None:
    """Parents exist, matched actions are real, specific rules come first."""
    root = ET.fromstring(f"<root>{RULES.read_text(encoding='utf-8')}</root>")  # noqa: S314
    rules = root.findall(".//rule")
    ids = {rule.attrib["id"] for rule in rules}
    for tag in ("if_sid", "if_matched_sid"):
        for ref in root.iter(tag):
            assert (ref.text or "").strip() in ids
    actions: dict[str, str] = {}
    for rule in rules:
        for match in rule.findall("match"):
            found = re.fullmatch(r'"action":"(\w+)"', match.text or "")
            assert found, match.text
            assert found[1] in {str(action) for action in ChangeAction}
            actions[rule.attrib["id"]] = found[1]
    children = [r.attrib["id"] for r in rules if r.findtext("if_sid") == "100500"]
    generic = [
        r.attrib["id"]
        for r in rules
        if (r.find("*[@name='kind']") is not None and r.find("match") is None)
        or r.find("*[@name='actor_kind']") is not None
    ]
    assert generic
    last_specific = max(children.index(rule_id) for rule_id in actions)
    assert last_specific < min(children.index(rule_id) for rule_id in generic)
