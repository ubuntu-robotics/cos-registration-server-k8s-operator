"""Library for the devices_auth_keys relation.

This library contains the Requires and Provides classes for handling the
rob-cos devices_keys interface. The devices keys allow a device to publish
data to the rob-cos fileserver.

Import `DevicesAuthKeysRequires` in your charm by adding the following to `src/charm.py`:
```
from charms.devices_keys_k8s.v0.device_keys import DevicesAuthKeysRequires
```

Define in your charm's `__init__` method:
```
# Make sure you set auth_devices_keys_relation in StoredState. Assuming you refer to this
# as `self._stored`:
self.auth_devices_keys_consumer = AuthDevicesKeysConsumer(self)
        self.framework.observe(
            self.auth_devices_keys_consumer.on.auth_devices_keys_changed,  # pyright: ignore
            self._on_auth_devices_keys_changed,
        )
```

And then wherever you need to reference the relation data it will be available
as a string in the stored data of the devices_keys_consumer:
```
auth_devices_keys_list = self.devices_keys_consumer._stored.auth_devices_keys
```
You will also need to add the endpoint definition to `metadata.yaml` as follows:
```
requires:
  auth-devices-keys:
    interface: auth_devices_keys
```

On the provider side import the `DevicesAuthKeysProvides` in your charm by adding the following to `src/charm.py`:
```
from charms.auth_devices_keys_k8s.v0.auth_devices_keys import AuthDevicesKeysProvider
```

Define in your charm's `__init__` method:
```
self.device_pub_keys_provider = AuthDevicesKeysProvider(
    charm=self, devices_keys_file=self._devices_keys_file
)
```
"""

import logging
from typing import Dict, Optional

from ops.framework import (
    EventBase,
    EventSource,
    Object,
    ObjectEvents,
    StoredDict,
    StoredList,
    StoredState,
)

from ops.charm import (
    CharmBase,
    HookEvent,
    RelationBrokenEvent,
    RelationChangedEvent,
    RelationCreatedEvent,
    RelationRole,
)

from ops.model import ModelError, Relation
from typing import Any, Dict, Optional
import json

## TODO: once this library is registered with charmlib the ID will be unique.
# The unique Charmhub library identifier, never change it
LIBID = "1"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1

logger = logging.getLogger(__name__)

DEFAULT_RELATION_NAME = "auth_devices_keys"

class RelationNotFoundError(Exception):
    """Raised if there is no relation with the given name."""

    def __init__(self, relation_name: str):
        self.relation_name = relation_name
        self.message = "No relation named '{}' found".format(relation_name)

        super().__init__(self.message)


class RelationInterfaceMismatchError(Exception):
    """Raised if the relation with the given name has a different interface."""

    def __init__(
        self,
        relation_name: str,
        expected_relation_interface: str,
        actual_relation_interface: str,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_interface
        self.actual_relation_interface = actual_relation_interface
        self.message = (
            f"The '{relation_name}' relation has '{actual_relation_interface}' as "
            "interface rather than the expected '{actual_relation_interface}'"
        )

        super().__init__(self.message)


class RelationRoleMismatchError(Exception):
    """Raised if the relation with the given name has a different direction."""

    def __init__(
        self,
        relation_name: str,
        expected_relation_role: RelationRole,
        actual_relation_role: RelationRole,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_role
        self.actual_relation_role = actual_relation_role
        self.message = f"The '{relation_name}' relation has role '{repr(actual_relation_role)}' rather than the expected '{repr(actual_relation_role)}'"

        super().__init__(self.message)

def _type_convert_stored(obj):
    """Convert Stored* to their appropriate types, recursively."""
    if isinstance(obj, StoredList):
        return list(map(_type_convert_stored, obj))
    if isinstance(obj, StoredDict):
        rdict = {}  # type: Dict[Any, Any]
        for k in obj.keys():
            rdict[k] = _type_convert_stored(obj[k])
        return rdict
    return obj


class AuthDevicesKeysChanged(EventBase):
    """Event emitted when device keys change """


class AuthDevicesKeysRelationCharmEvents(ObjectEvents):
    """A class to carry custom charm events so requires can react to relation changes."""
    auth_devices_keys_changed = EventSource(AuthDevicesKeysChanged)


class AuthDevicesKeysConsumer(Object):

    on =AuthDevicesKeysRelationCharmEvents()
    _stored = StoredState()

    def __init__(self, charm, relation_name: str = DEFAULT_RELATION_NAME):
        """A class implementing the auth_devices_keys requires relation."""

        super().__init__(charm, relation_name)

        self._charm = charm
        self._relation_name = relation_name

        self._stored.set_default(auth_devices_keys=[])  # type: ignore
        self.framework.observe(
            self._charm.on[relation_name].relation_changed,
            self._on_relation_changed,
        )
        self.framework.observe(
            self._charm.on[relation_name].relation_broken,
            self._on_relation_broken,
        )

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        """Handle relation changes in related providers.

        If there are changes in relations between devices public keys consumers
        and providers, this event handler (if the unit is the leader) will
        get data for an incoming devices-pub-keys relation through a
        :class:`DevicesKeysChanged` event, and make the relation data
        available in the app's datastore object.
        """
        if self._charm.unit.is_leader():
            try:
                databag = event.relation.data[event.relation.app].get("auth_devices_keys", "")
            except ModelError as e:
                logger.debug(
                    f"Error {e} attempting to read remote app data; "
                    f"probably we are in a relation_departed hook"
                )
                return False

            if not databag:
                return False

        coerced_data = _type_convert_stored(self._stored.auth_devices_keys) if self._stored.auth_devices_keys else []
        if coerced_data != databag:
            self._stored.auth_devices_keys = databag
            self.on.auth_devices_keys_changed.emit()


    def _on_relation_broken(self, event: RelationBrokenEvent) -> None:
        """Update job config when providers depart.

        When a devices public keys provider departs, the configuration
        for that provider is removed from the list of dashboards
        """
        if not self._charm.unit.is_leader():
            return

        pass

    @property
    def relation_data(self) -> Optional[Dict[str, str]]:
        """Retrieve the relation data.

        Returns:
            Dict: dict containing the relation data.
        """
        relation = self.model.get_relation(self._relation_name)
        if not relation:
            return None

        return relation.data[relation.app]


class AuthDevicesKeysProvider(Object):

    _stored = StoredState()
    on = AuthDevicesKeysRelationCharmEvents()  # pyright: ignore

    def __init__(   
        self,
        charm: CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
    ) -> None:

        """A class implementing the auth_devices_keys provides relation."""
        super().__init__(charm, relation_name)

        self._charm = charm
        self._relation_name = relation_name

        self._stored.set_default(auth_devices_keys=[])  # type: ignore

        self.framework.observe(self._charm.on.leader_elected, self._on_handle_relation)
        self.framework.observe(self._charm.on.upgrade_charm, self._on_handle_relation)

        self.framework.observe(
            self._charm.on[self._relation_name].relation_created,
            self._on_handle_relation,
        )
        self.framework.observe(
            self._charm.on[self._relation_name].relation_changed,
            self._on_relation_changed,
        )

    def _on_handle_relation(self, event: RelationCreatedEvent) -> None:
        """Watch for a relation being created and automatically send authorized devices keys.

        Args:
            event: The :class:`RelationJoinedEvent` sent when a
                `devices_keys` relationship is joined
        """
        if not self._charm.unit.is_leader():
            return

        auth_devices_keys_dict = self._charm._get_auth_devices_keys_from_db()
        self.update_all_auth_devices_keys_from_db(auth_devices_keys_dict)

    def update_all_auth_devices_keys_from_db(
        self, auth_devices_keys, _: Optional[HookEvent] = None
    ) -> None:
        """Scans the available public keys and updates relations with changes."""
        # Update of storage must be done irrespective of leadership, so
        # that the stored state is there when this unit becomes leader.

        if auth_devices_keys:
            stored_auth_devices_keys: Any = self._stored.auth_devices_keys  # pyright: ignore

            stored_auth_devices_keys.clear()

            self._stored.auth_devices_keys = auth_devices_keys

            if self._charm.unit.is_leader():
                for ssh_keys_relation in self._charm.model.relations[self._relation_name]:
                    self._update_auth_devices_keys_on_relation(ssh_keys_relation)

    def _update_auth_devices_keys_on_relation(self, relation: Relation) -> None:
        """Update the available devices public keys in the relation data bucket."""

        logger.debug(self._stored.auth_devices_keys)
        stored_data = _type_convert_stored(self._stored.auth_devices_keys)

        logger.debug(type(stored_data))
        relation.data[self._charm.app]["auth_devices_keys"] = json.dump(stored_data)

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        """Handle relation changes in related providers.

        If there are changes in relations between devices public keys consumers
        and providers, this event handler (if the unit is the leader) will
        get data for an incoming auth_devices_keys relation through a
        :class:`DevicesKeysChanged` event, and make the relation data
        available in the app's datastore object.
        """
        changes = False
        if self._charm.unit.is_leader():
            auth_devices_keys_dict = self._charm._get_auth_devices_keys_from_db()
            changes = self.update_all_auth_devices_keys_from_db(auth_devices_keys_dict, event.relation)

        if changes:
            self.on.auth_devices_keys_changed.emit() 
