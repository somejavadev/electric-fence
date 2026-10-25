"""Addresses that are never banned or slowed down."""

from collections.abc import Mapping
import ipaddress
from ipaddress import IPv4Address, IPv6Address
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import get_supervisor_ip, is_hassio

from ..geo.base import classify_ip

_LOGGER = logging.getLogger(__name__)

type IpAddress = IPv4Address | IPv6Address

# Core keeps the running HTTP config in its HTTP config store
# (`homeassistant.components.http.config.DATA_STORE`, key "http"). It is read
# by duck typing, so a missing or changed store only makes the value unknown.
_HTTP_STORE_KEY = "http"


def http_config(hass: HomeAssistant) -> Mapping[str, Any] | None:
    """Return the running HTTP config, or None when it can't be told."""
    store = hass.data.get(_HTTP_STORE_KEY)
    active = getattr(store, "active_config_type", None)
    if active is None:
        return None
    active = str(active)
    if active in ("default", "default_legacy_port"):
        # The built-in default config has no proxy settings.
        return {}
    if active not in ("stable", "pending"):
        return None
    conf = getattr(store, active, None)
    return conf if isinstance(conf, Mapping) else None


def http_config_value(hass: HomeAssistant, key: str) -> Any | None:
    """Return one value of the running HTTP config, None when unknown or unset."""
    conf = http_config(hass)
    return conf.get(key) if conf is not None else None


def normalise_ip(ip: str | IpAddress | None) -> IpAddress | None:
    """Parse an address, dropping a `%zone` and unmapping IPv4-mapped IPv6."""
    if ip is None:
        return None
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0]) if isinstance(ip, str) else ip
    except ValueError:
        return None
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _trusted_networks(
    hass: HomeAssistant,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    value = http_config_value(hass, "trusted_proxies")
    if not isinstance(value, (list, tuple, set)):
        return []
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in value:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError, TypeError:
            continue
    return networks


def _is_supervisor(hass: HomeAssistant, addr: IpAddress) -> bool:
    try:
        return is_hassio(hass) and normalise_ip(get_supervisor_ip()) == addr
    except Exception:
        _LOGGER.debug("Supervisor address lookup failed", exc_info=True)
        return False


def is_exempt(hass: HomeAssistant, ip: str | IpAddress | None) -> bool:
    """Return whether an address is never banned or slowed down.

    An address that can't be parsed is exempt: it can't be banned anyway.
    """
    addr = normalise_ip(ip)
    if addr is None:
        return True
    if classify_ip(str(addr))[1] is not None:
        return True
    if _is_supervisor(hass, addr):
        return True
    return any(
        addr in net for net in _trusted_networks(hass) if net.version == addr.version
    )
