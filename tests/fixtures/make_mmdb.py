"""Build tiny country-only test databases."""

from pathlib import Path

from mmdb_writer import MMDBWriter
from netaddr import IPSet

COUNTRIES = {"203.0.113.0/24": "RU", "2001:db8::/32": "NL"}


def build_mmdb_bytes(tmp_dir: Path) -> bytes:
    """Return the bytes of a fresh test database."""
    path = tmp_dir / "fixture.mmdb"
    writer = MMDBWriter(
        ip_version=6, database_type="Country", languages=["en"], ipv4_compatible=True
    )
    for network, code in COUNTRIES.items():
        writer.insert_network(IPSet([network]), {"country": {"iso_code": code}})
    writer.to_db_file(str(path))
    data = path.read_bytes()
    path.unlink()
    return data
