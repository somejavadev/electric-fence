"""Config entry collector (H4): who added, changed or removed an integration.

Core dispatches `SIGNAL_CONFIG_ENTRY_CHANGED` for every entry change, and
also for every state change (setup, unload). The collector keeps a snapshot
of each entry's saved fields, in memory, and records only real changes:

- A dispatch whose `entry.state` differs from the snapshot is a state change
  (`_async_set_state`, which never changes saved fields). Nothing is
  recorded and no credit is used (R26).
- Otherwise the saved fields are compared. No change: nothing is recorded,
  and the scope's credit stays unused (R25).

Core starts an entry's update listeners as eager tasks after it set the new
fields but before it dispatches them (`_async_save_and_notify`). A listener
that reloads can run the whole reload, including the integration's own
setup-time update, before the admin's dispatch. So a state dispatch that
already sees changed fields means that change's own dispatch is pending:
the change is held back (`_pending`) and recorded at that dispatch, which
then shows no diff of its own, in the admin's context. Changes dispatched
while it is pending are the listener's follow-ups, and System. A hold-back
lasts until the loop runs its next callback at most; one no dispatch
claimed is then recorded as System (R28).

Data and options are kept as a hash of each value's JSON dump, so the diff
names the changed keys. Data values never leave the entry. Option values
are also kept, as a copy in memory, so a change can show its before and
after (redacted, and only when `record_values` is on), like the title,
disabled-by and preference fields; subentries and data are named only.
"""

from collections.abc import Mapping
import copy
from dataclasses import dataclass, field
import hashlib
import json
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration
from homeassistant.util import dt as dt_util

from .. import attribution
from ..models import ChangeAction, ChangeKind, SettingsChange
from ..privacy import sanitise_text
from ..redact import diff_details
from .base import BaseCollector

if TYPE_CHECKING:
    from ..runtime import Runtime

# The saved fields a change is reported by.
FIELDS = (
    "title",
    "data",
    "options",
    "disabled_by",
    "pref_disable_new_entities",
    "pref_disable_polling",
    "subentries",
)

# Saved fields shown with their values, beside the option keys.
_VALUE_FIELDS = (
    "title",
    "disabled_by",
    "pref_disable_new_entities",
    "pref_disable_polling",
)
# Saved fields named only: data by its keys, subentries as a whole.
_NAMED_FIELDS = ("data", "subentries")

# Actions that name the admin's own change; only for a credited change.
_SCOPED = (ChangeAction.RECONFIGURED, ChangeAction.OPTIONS_CHANGED)


def _hash(value: Any) -> str:
    try:
        dump = json.dumps(value, sort_keys=True, default=repr)
    except TypeError:
        # Keys of mixed types can't be sorted: order them by their text.
        dump = repr(_sorted_by_text(value))
    return hashlib.sha256(dump.encode()).hexdigest()


def _sorted_by_text(value: Any) -> Any:
    """Return `value` with every mapping as items sorted by `str(key)`."""
    if isinstance(value, Mapping):
        return sorted(
            ((str(k), repr(k), _sorted_by_text(v)) for k, v in value.items()),
            key=lambda item: item[:2],
        )
    if isinstance(value, (list, tuple)):
        return [_sorted_by_text(v) for v in value]
    return value


def _copy_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the options that later changes in place can't reach."""
    try:
        return copy.deepcopy(dict(options))
    except Exception:  # noqa: BLE001
        # Not deep-copyable (no JSON value is): the top level at least.
        return dict(options)


def _hashes(values: Mapping[str, Any]) -> dict[str, str]:
    return {key: _hash(value) for key, value in values.items()}


def _changed_keys(old: Mapping[str, str], new: Mapping[str, str]) -> list[str]:
    return sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))


@dataclass(slots=True, frozen=True)
class EntrySnapshot:
    """The saved fields of a config entry, plus its state (R26)."""

    state: ConfigEntryState
    title: str
    # Key to hash of the value's JSON dump: the keys, and which values
    # changed, without keeping any value.
    data: Mapping[str, str]
    options: Mapping[str, str]
    disabled_by: ConfigEntryDisabler | None
    pref_disable_new_entities: bool
    pref_disable_polling: bool
    # Subentry ID to hash of its content.
    subentries: Mapping[str, str]
    # Hash of the saved fields that aren't reported (unique ID, versions,
    # discovery keys): their change is a real dispatch too.
    untracked: str
    # A copy of the option values, for a change's before and after. Not
    # compared: `options` already names what changed.
    option_values: Mapping[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )

    @classmethod
    def of(cls, entry: ConfigEntry) -> EntrySnapshot:
        """Take a snapshot of an entry."""
        return cls(
            state=entry.state,
            title=entry.title,
            data=_hashes(entry.data),
            options=_hashes(entry.options),
            disabled_by=entry.disabled_by,
            pref_disable_new_entities=entry.pref_disable_new_entities,
            pref_disable_polling=entry.pref_disable_polling,
            subentries={
                subentry_id: _hash(subentry.as_dict())
                for subentry_id, subentry in entry.subentries.items()
            },
            untracked=_hash(
                [
                    entry.unique_id,
                    entry.version,
                    entry.minor_version,
                    dict(entry.discovery_keys),
                ]
            ),
            option_values=_copy_options(entry.options),
        )


def _changed_fields(old: EntrySnapshot, new: EntrySnapshot) -> list[str]:
    """Return the names of the reported saved fields that differ."""
    return sorted(name for name in FIELDS if getattr(old, name) != getattr(new, name))


def _saved_changed(old: EntrySnapshot, new: EntrySnapshot) -> bool:
    return old.untracked != new.untracked or bool(_changed_fields(old, new))


def _updated_action(old: EntrySnapshot, new: EntrySnapshot) -> ChangeAction:
    """Return the action of a real UPDATED, from the diff and the scope."""
    if old.disabled_by is None and new.disabled_by is not None:
        return ChangeAction.DISABLED
    if old.disabled_by is not None and new.disabled_by is None:
        return ChangeAction.ENABLED
    # The scope names the kind of action only; crediting is separate.
    scope = attribution.SCOPE.get()
    if scope is not None:
        if (
            scope.action == attribution.ACTION_CONFIG_FLOW
            and scope.details.get("source") == SOURCE_RECONFIGURE
        ):
            return ChangeAction.RECONFIGURED
        if scope.action == attribution.ACTION_OPTIONS_FLOW:
            return ChangeAction.OPTIONS_CHANGED
    return ChangeAction.UPDATED


class ConfigEntryCollector(BaseCollector):
    """Record config entry changes with strict attribution."""

    name = "config_entries"

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the collector."""
        super().__init__(hass, runtime)
        self._snapshots: dict[str, EntrySnapshot] = {}
        # Entry ID to (before, after) of a change whose dispatch is pending.
        self._pending: dict[str, tuple[EntrySnapshot, EntrySnapshot]] = {}

    def _install(self) -> None:
        self._snapshots = {
            entry.entry_id: EntrySnapshot.of(entry)
            for entry in self.hass.config_entries.async_entries()
        }
        self._unsubs.append(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._on_change
            )
        )

    def _uninstall(self) -> None:
        self._snapshots.clear()
        self._pending.clear()

    @callback
    def _on_change(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        """Handle a config entry dispatch (inside Core's call)."""
        self.guard.call(self._handle, change, entry)

    def _handle(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        entry_id = entry.entry_id
        if change is ConfigEntryChange.REMOVED:
            self._snapshots.pop(entry_id, None)
            self._pending.pop(entry_id, None)
            self._record(entry, ChangeAction.REMOVED, None)
            return
        new = EntrySnapshot.of(entry)
        old = self._snapshots.get(entry_id)
        self._snapshots[entry_id] = new
        if change is ConfigEntryChange.ADDED:
            self._record(entry, ChangeAction.ADDED, None)
            return
        if old is None:
            # Not seen before: nothing to compare with.
            return
        if old.state is not new.state:
            self._handle_state_change(entry_id, old, new)
            return
        if _saved_changed(old, new):
            if entry_id in self._pending:
                # A follow-up of a change still to be dispatched: System.
                self._record_update(entry, old, new, credit=False)
            else:
                self._record_update(entry, old, new, credit=True)
            return
        # No diff of its own: the dispatch of a held-back change, if any.
        if (pending := self._pending.pop(entry_id, None)) is not None:
            self._record_update(entry, *pending, credit=True)

    def _handle_state_change(
        self, entry_id: str, old: EntrySnapshot, new: EntrySnapshot
    ) -> None:
        """Hold back fields a state dispatch sees changed (see module doc)."""
        if not _saved_changed(old, new):
            return
        before = self._pending.get(entry_id, (old, new))[0]
        pending = self._pending[entry_id] = (before, new)
        # The held-back dispatch comes before the loop runs anything else;
        # an unclaimed hold-back is recorded then, as System, so it neither
        # gets lost nor makes a later change System.
        self.hass.loop.call_soon(self.guard.call, self._expire, entry_id, pending)

    def _expire(
        self, entry_id: str, pending: tuple[EntrySnapshot, EntrySnapshot]
    ) -> None:
        """Record a hold-back no dispatch claimed, as System (R28)."""
        if self._pending.get(entry_id) is not pending:
            return
        del self._pending[entry_id]
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is not None:
            self._record_update(entry, *pending, credit=False)

    def _record_update(
        self,
        entry: ConfigEntry,
        old: EntrySnapshot,
        new: EntrySnapshot,
        *,
        credit: bool,
    ) -> None:
        changed = _changed_fields(old, new)
        if not changed:
            # Only unreported fields (unique ID, versions) changed.
            return
        details = self._value_diff(old, new)
        details["changed"] = sorted(
            {
                *details["changed"],
                # By hash too: values `==` can miss (1 and 1.0) still count.
                *_changed_keys(old.options, new.options),
                *(name for name in _NAMED_FIELDS if name in changed),
            }
        )
        details["data_keys_changed"] = _changed_keys(old.data, new.data)
        self._record(entry, _updated_action(old, new), details, credit=credit)

    def _value_diff(self, old: EntrySnapshot, new: EntrySnapshot) -> dict[str, Any]:
        """Return the changed option and field names, and values when on."""

        def fields(snapshot: EntrySnapshot) -> dict[str, Any]:
            return {
                **{name: getattr(snapshot, name) for name in _VALUE_FIELDS},
                **snapshot.option_values,
            }

        return diff_details(
            fields(old),
            fields(new),
            include_values=self.runtime.settings.record_values,
        )

    def _record(
        self,
        entry: ConfigEntry,
        action: ChangeAction,
        diff: dict[str, Any] | None,
        *,
        credit: bool = True,
    ) -> None:
        actor = (
            attribution.credited_actor(
                ChangeKind.CONFIG_ENTRY, action, entry.entry_id, domain=entry.domain
            )
            if credit
            else None
        )
        if actor is None and action in _SCOPED:
            # Not the admin's own change, though made during it.
            action = ChangeAction.UPDATED
        details: dict[str, Any] = {
            "domain": entry.domain,
            "changed": [],
            "data_keys_changed": [],
            **(diff or {}),
        }
        self.runtime.worker.submit(
            SettingsChange(
                ts=dt_util.utcnow(),
                kind=ChangeKind.CONFIG_ENTRY,
                action=action,
                target_id=entry.entry_id,
                target_name=sanitise_text(
                    f"{self._integration_title(entry.domain)}: {entry.title}"
                ),
                details=details,
                actor_user_id=actor.user_id if actor else None,
                actor_name=actor.user_name if actor else None,
                actor_ip=actor.ip if actor else None,
            )
        )

    def _integration_title(self, domain: str) -> str:
        """Return the loaded integration's name, else the domain."""
        try:
            return async_get_loaded_integration(self.hass, domain).name
        except IntegrationNotLoaded:
            return domain
