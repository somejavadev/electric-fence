"""Blocking backends: where bans are actually enforced."""

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime
import ipaddress
from ipaddress import IPv4Address, IPv6Address
import logging
from pathlib import Path
import shutil
import tempfile
from typing import Literal

from homeassistant.components.http.ban import (
    KEY_BAN_MANAGER,
    KEY_FAILED_LOGIN_ATTEMPTS,
    IpBanManager,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import yaml as yaml_util

from .network import IpAddress, normalise_ip

_LOGGER = logging.getLogger(__name__)

HEALTH_IP_BAN_DISABLED = "ip_ban_disabled"
# What Core's own append creates under the usual umask. Reading the umask
# means setting it, process-wide, which other threads would race.
_NEW_FILE_MODE = 0o644


class BlockingBackend(ABC):
    """Enforces blocks; Core's ban manager today, others later."""

    @abstractmethod
    async def async_add(self, ip: IPv4Address | IPv6Address) -> None:
        """Block an address."""

    @abstractmethod
    async def async_remove(self, ip: IPv4Address | IPv6Address) -> bool:
        """Unblock an address; True when it is no longer blocked."""

    @abstractmethod
    def list_ips(self) -> dict[IPv4Address | IPv6Address, datetime]:
        """Return the blocked addresses with the time they were blocked."""

    @abstractmethod
    def supports(self, rule_type: Literal["ip"]) -> bool:
        """Return whether this backend can enforce the rule type."""

    @abstractmethod
    def health(self) -> str | None:
        """Return None when working, otherwise the reason it is not."""


def _rewrite_bans_file(
    path: str, remove: IpAddress, keep_from_memory: dict[IpAddress, datetime]
) -> None:
    """Drop `remove` from the bans file, keeping every other entry.

    Entries Core holds in memory but not in the file are written back. A
    malformed file is left untouched.
    """
    try:
        current = yaml_util.load_yaml_dict(path)
    except FileNotFoundError:
        current = {}
    except HomeAssistantError:
        _LOGGER.warning(
            "The IP bans file is malformed; the unban applies only until restart"
        )
        return

    kept = {key: value for key, value in current.items() if normalise_ip(key) != remove}
    present = set()
    for key in kept:
        try:
            present.add(ipaddress.ip_address(key))
        except ValueError:
            continue
    for ip, banned_at in keep_from_memory.items():
        if normalise_ip(ip) != remove and ip not in present:
            kept[str(ip)] = {"banned_at": banned_at.isoformat()}

    # An empty mapping must stay an empty file: Core appends to it, and "{}"
    # followed by more entries is not valid YAML.
    content = yaml_util.dump(kept) if kept else ""
    target = Path(path)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf8", dir=target.parent, prefix=f"{target.name}.", delete=False
    ) as out:
        tmp = Path(out.name)
        try:
            out.write(content)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        # The temporary file is 0600; keep the original's mode.
        if target.exists():
            shutil.copymode(target, tmp)
        else:
            tmp.chmod(_NEW_FILE_MODE)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class CoreBanBackend(BlockingBackend):
    """Core's `ip_bans.yaml` ban manager."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the backend."""
        self._hass = hass
        self._lock = asyncio.Lock()

    def _manager(self) -> IpBanManager | None:
        http = getattr(self._hass, "http", None)
        return http.app.get(KEY_BAN_MANAGER) if http is not None else None

    async def async_add(self, ip: IPv4Address | IPv6Address) -> None:
        """Ban through Core."""
        if (manager := self._manager()) is None:
            return
        if (addr := normalise_ip(ip)) is None:
            return
        # Core appends to the file; during a rewrite the append would land on
        # the replaced file and be lost on restart.
        async with self._lock:
            await manager.async_add_ban(addr)
            if isinstance(addr, IPv4Address):
                # Core's middleware matches the remote address exactly, and a
                # dual-stack socket reports IPv4 clients in mapped form.
                await manager.async_add_ban(IPv6Address(f"::ffff:{addr}"))

    async def async_remove(self, ip: IPv4Address | IPv6Address) -> bool:
        """Unban in memory, in Core's counter and in the file."""
        if (manager := self._manager()) is None:
            return False
        if (addr := normalise_ip(ip)) is None:
            return False
        async with self._lock:
            lookup = manager.ip_bans_lookup
            for key in [k for k in lookup if normalise_ip(k) == addr]:
                lookup.pop(key, None)
            attempts = self._hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS]
            for key in [k for k in attempts if normalise_ip(k) == addr]:
                attempts.pop(key, None)
            keep = {ban.ip_address: ban.banned_at for ban in list(lookup.values())}
            try:
                await self._hass.async_add_executor_job(
                    _rewrite_bans_file, manager.path, addr, keep
                )
            except OSError as err:
                _LOGGER.warning(
                    "Could not update the IP bans file: %s", type(err).__name__
                )
        return True

    def list_ips(self) -> dict[IPv4Address | IPv6Address, datetime]:
        """Return Core's current bans."""
        if (manager := self._manager()) is None:
            return {}
        return {
            norm: ban.banned_at
            for ban in list(manager.ip_bans_lookup.values())
            if (norm := normalise_ip(ban.ip_address)) is not None
        }

    def supports(self, rule_type: Literal["ip"]) -> bool:
        """Core bans addresses only."""
        return rule_type == "ip"

    def health(self) -> str | None:
        """Report whether Core's ban manager is enabled."""
        return HEALTH_IP_BAN_DISABLED if self._manager() is None else None
