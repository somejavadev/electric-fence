"""Base class for collectors."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from homeassistant.core import CALLBACK_TYPE, HomeAssistant

from ..contracts import check_contracts
from ..failopen import FailOpen
from ..hooks import HookHandle
from ..repairs import async_clear_disabled_collector, async_report_disabled_collector

if TYPE_CHECKING:
    from ..runtime import Runtime


class BaseCollector(ABC):
    """A collector installs hooks and records events, failing open."""

    name: ClassVar[str]

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the collector."""
        self.hass = hass
        self.runtime = runtime
        self.guard = FailOpen(self.name)
        self.disabled_reason: list[str] = []
        self._handles: list[HookHandle] = []
        self._unsubs: list[CALLBACK_TYPE] = []

    @abstractmethod
    def _install(self) -> None:
        """Install hooks and subscriptions."""

    @abstractmethod
    def _uninstall(self) -> None:
        """Collector-specific teardown."""

    def install(self) -> bool:
        """Install if all contracts hold; otherwise raise a repair issue."""
        reasons = check_contracts(self.hass, self.name)
        if reasons:
            self.disabled_reason = reasons
            async_report_disabled_collector(self.hass, self.name, reasons)
            return False
        self.disabled_reason = []
        self._install()
        async_clear_disabled_collector(self.hass, self.name)
        return True

    def uninstall(self) -> None:
        """Remove hooks and subscriptions. Idempotent."""
        self._uninstall()
        handles, self._handles = self._handles, []
        unsubs, self._unsubs = self._unsubs, []
        for handle in handles:
            handle.restore()
        for unsub in unsubs:
            unsub()
