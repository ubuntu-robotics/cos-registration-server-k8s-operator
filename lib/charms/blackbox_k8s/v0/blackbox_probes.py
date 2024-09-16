# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""## Overview

## Provider Library Usage

The Blackbox Exporter charm interacts with its datasources using this charm
library. The goal of this library is to be as simple to use as possible.
"""

import logging
import json
import yaml
from typing import Dict, List, Optional, Union

from ops import Object
from ops.charm import CharmBase
from ops.framework import BoundEvent, EventBase
from ops.charm import CharmBase, RelationRole
from cosl import JujuTopology
from ops.model import Relation
from ops.framework import (
    BoundEvent,
    EventBase,
    EventSource,
    Object,
    ObjectEvents,
)

# The unique Charmhub library identifier, never change it
LIBID = "1"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1

logger = logging.getLogger(__name__)

DEFAULT_RELATION_NAME = "blackbox-probes"

class BlackboxProbesProvider(Object):
    """A provider object for Blackbox Exporter probes."""

    def __init__(
        self,
        charm: CharmBase,
        probes: List,
        modules: Optional[Dict] = None,
        refresh_event: Optional[Union[BoundEvent, List[BoundEvent]]] = None,
        relation_name: str = DEFAULT_RELATION_NAME,
    ):
        super().__init__(charm, relation_name)
        """Construct a Blackbox Exporter client.

        To integrate with Blackbox Exporter, a charm should instantiate a
        `BlackboxProbesProvider` as follows:

            self.blackbox_probes = BlackboxProbesProvider(
                self,
                probes = [
                    ""
                    - job_name: 'blackbox_http_2xx'
                      metrics_path: /probe
                      params:
                        module: [http_2xx]
                      static_configs:
                        - targets: 
                            - http://target-a
                            labels: 
                                name: "device"
                    "",
                    ""
                    - job_name: 'blackbox_http_2xx_longer_timeout'
                      metrics_path: /probe
                      params:
                        module: [http_2xx_longer_timeout]
                      static_configs:
                        - targets: ['http://target-b' 'http://target-c']
                    ""
                ],
                modules={
                    "http_2xx_longer_timeout": {
                        "prober": "http"
                        "timeout": "30s"  # default is 5s
                    }
                },
                refresh_event=[
                    self.on.update_status,
                    self.ingress.on.ready_for_unit,
                ],
                relation_name="blackbox-probes",
            )

        Args:
            charm: a `CharmBase` object which manages the
                `BlackboxProbesProvider` object. Generally, this is `self` in
                the instantiating class.
            probes: the probes to configure in Blackbox Exporter passed as
                known args to the constructor. The "key" should correspond to
                the chosen module, while the "value" is a list of endpoints to
                probe with that module.
            modules: an optional definition of modules for Blackbox Exporter to
                use. For details on how to write pass a module, see the
                [docs upstream](https://github.com/prometheus/blackbox_exporter/blob/master/CONFIGURATION.md).
                Further examples are provided [upstream](https://github.com/prometheus/blackbox_exporter/blob/master/example.yml).
            refresh_event: additional `CharmEvents` event (or a list of them)
                on which the probes and modules should be updated.
            relation_name: name of the relation providing the Blackbox Probes
                service. It's recommended to not change it, to ensure a
                consistent experience acrosss all charms that use the library.
        """
        self.topology = JujuTopology.from_charm(charm)
        self._charm = charm
        self._relation_name = relation_name

        events = self._charm.on[self._relation_name]
        self._probes = [] if probes is None else probes
        self._modules = {} if modules is None else modules

        if not refresh_event:
            if len(self._charm.meta.containers) == 1:
                container = list(self._charm.meta.containers.values())[0]
                refresh_event = [self._charm.on[container.name.replace("-", "_")].pebble_ready]
            else:
                refresh_event = []
        elif not isinstance(refresh_event, list):
            refresh_event = [refresh_event]

        topology = JujuTopology.from_dict(self._scrape_metadata)
        module_name_prefix = "juju_{}_".format(topology.identifier)
        self._prefix_probes(module_name_prefix)
        self._prefix_modules(module_name_prefix)

        self.framework.observe(events.relation_joined, self.set_probes_spec)
        for ev in refresh_event:
            self.framework.observe(ev, self.set_probes_spec)

    def set_probes_spec(self, _=None):
        """Ensure probes target information is made available to Blackbox Exporter.

        When a probes provider charm is related to a blackbox exporter charm, the
        probes provider sets specification and metadata related to it
        """
        if not self._charm.unit.is_leader():
            return
        
        for relation in self._charm.model.relations[self._relation_name]:
            relation.data[self._charm.app]["scrape_metadata"] = json.dumps(self._scrape_metadata)
            relation.data[self._charm.app]["scrape_probes"] = json.dumps(self._probes)
            relation.data[self._charm.app]["scrape_modules"] = json.dumps(self._modules)

    def _prefix_probes(self, prefix: str):
        """Prefix the job_names and the probes_modules with the charm metadata."""
        for job in self._probes:
            job_name = job["job_name"]
            job["job_name"] = prefix + "_" + job_name if job_name else prefix
            job_params = job["params"].get('params', {}).get('module', [])
            for param in job_params:
                modules = param["module"]
                for module in modules:
                    if module in self._modules:
                        prefixed_module_value = f"{prefix}_{module}"
                        job['params']['module'] = prefixed_module_value

    def _prefix_modules(self, prefix: str) -> None:
        """Prefix the modules with the charm metadata."""
        self._modules = {f"{prefix}{key}": value for key, value in self._modules.items()}

    @property
    def _scrape_metadata(self) -> dict:
        """Generate scrape metadata.

        Returns:
            Scrape configuration metadata for this metrics provider charm.
        """
        return self.topology.as_dict()

class TargetsChangedEvent(EventBase):
    """Event emitted when Blackbox Exporter scrape targets change."""

    def __init__(self, handle, relation_id):
        super().__init__(handle)
        self.relation_id = relation_id

    def snapshot(self):
        """Save scrape target relation information."""
        return {"relation_id": self.relation_id}

    def restore(self, snapshot):
        """Restore scrape target relation information."""
        self.relation_id = snapshot["relation_id"]

class MonitoringEvents(ObjectEvents):
    """Event descriptor for events raised by `BlackboxExporterRequirer`."""

    targets_changed = EventSource(TargetsChangedEvent)

class BlackboxProbesRequirer(Object):
    """A requirer object for Blackbox Exporter probes."""

    on = MonitoringEvents()  # pyright: ignore

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """"A requirer object for Blackbox Exporter probes.

        Args:
            charm: a `CharmBase` instance that manages this
                instance of the Blackbox Exporter service.
            relation_name: an optional string name of the relation between `charm`
                and the Blackbox Exporter charmed service. The default is "blackbox-probes".

        Raises:
            RelationNotFoundError: If there is no relation in the charm's metadata.yaml
                with the same name as provided via `relation_name` argument.
            RelationInterfaceMismatchError: The relation with the same name as provided
                via `relation_name` argument does not have the `prometheus_scrape` relation
                interface.
            RelationRoleMismatchError: If the relation with the same name as provided
                via `relation_name` argument does not have the `RelationRole.requires`
                role.
        """

        super().__init__(charm, relation_name)
        self._charm = charm
        self._relation_name = relation_name
        events = self._charm.on[relation_name]
        self.framework.observe(events.relation_changed, self._on_probes_provider_relation_changed)
        self.framework.observe(
            events.relation_departed, self._on_probes_provider_relation_departed
        )

    def _on_probes_provider_relation_changed(self, event):
        """Handle changes with related probes providers.

        Anytime there are changes in relations between Blackbox Exporter
        and probes provider charms the Blackbox Exporter charm is informed,
        through a `TargetsChangedEvent` event. The Blackbox Exporter charm can
        then choose to update its scrape configuration.

        Args:
            event: a `CharmEvent` in response to which the Blackbox Exporter
                charm must update its scrape configuration.
        """
        rel_id = event.relation.id

        self.on.targets_changed.emit(relation_id=rel_id)

    def _on_probes_provider_relation_departed(self, event):
        """Update job config when a probes provider departs.

        When a probes provider departs the Blackbox Exporter charm is informed
        through a `TargetsChangedEvent` event so that it can update its
        scrape configuration to ensure that the departed probes provider
        is removed from the list of scrape jobs.

        Args:
            event: a `CharmEvent` that indicates a probes provider
               unit has departed.
        """
        rel_id = event.relation.id
        self.on.targets_changed.emit(relation_id=rel_id)

    def probes(self) -> list:
        """Fetch the dict of probes to scrape.

        Returns:
            A dict consisting of all the static probes configurations
            for each related `BlackboxExporterProvider'.
        """
        scrape_probes = []

        for relation in self._charm.model.relations[self._relation_name]:
            static_probes_jobs = json.loads(relation.data[relation.app].get("scrape_probes", "[]"))
            scrape_probes.extend(static_probes_jobs)

        return scrape_probes

    def modules(self) -> dict:
        """Fetch the dict of blackbox modules to configure.

        Returns:
            A dict consisting of all the modueles configurations
            for each related `BlackboxExporterProvider`.
        """
        blackbox_scrape_modules = {}

        for relation in self._charm.model.relations[self._relation_name]:
            scrape_modules = json.loads(relation.data[relation.app].get("scrape_modules", "{}"))
            blackbox_scrape_modules.update(scrape_modules)

        return blackbox_scrape_modules
