#!/usr/bin/env python3

"""A kubernetes charm for registering devices."""

import logging
import string
import secrets
import hashlib
import requests
import json

from os import path
from pathlib import Path

from ops.charm import (
    ActionEvent,
    CharmBase,
    HookEvent,
    RelationJoinedEvent,
)

from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, WaitingStatus
from ops.pebble import Layer, ExecError, ChangeError


from charms.traefik_route_k8s.v0.traefik_route import TraefikRouteRequirer
from charms.catalogue_k8s.v0.catalogue import CatalogueConsumer, CatalogueItem
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.auth_devices_keys_k8s.v0.auth_devices_keys import AuthDevicesKeysProvider
import socket

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

VALID_LOG_LEVELS = ["info", "debug", "warning", "error", "critical"]

COS_REGISTRATION_SERVER_API_URL_BASE = "/api/v1/"


def md5_update_from_file(filename, hash):
    """Generate the md5 of a file."""
    assert Path(filename).is_file()
    with open(str(filename), "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash.update(chunk)
    return hash


def md5_dir(directory):
    """Generate the md5 of a directory."""
    hash = hashlib.md5()
    assert Path(directory).is_dir()
    for file_path in sorted(Path(directory).iterdir(), key=lambda p: str(p).lower()):
        hash.update(file_path.name.encode())
        if file_path.is_file():
            hash = md5_update_from_file(file_path, hash)
    return hash.hexdigest()


def md5_dict(dict):
    """Generate the hash of a dictionary."""
    json_str = json.dumps(dict, sort_keys=True)
    hash_object = hashlib.md5(json_str.encode())
    hash_value = hash_object.hexdigest()
    return hash_value


class CosRegistrationServerCharm(CharmBase):
    """Charm to run a COS registration server on Kubernetes."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.name = "cos-registration-server"

        if len(self.model.storages["database"]) == 0:
            # Storage isn't available yet. Since storage becomes available early enough, no need
            # to observe storage-attached and complicate things; simply abort until it is ready.
            return
        self._server_data_mount_point = self.model.storages["database"][0].location
        self._grafana_dashboards_path = path.join(
            self._server_data_mount_point, "grafana_dashboards"
        )

        self.container = self.unit.get_container(self.name)
        self._stored.set_default(
            admin_password="", dashboard_dict_hash="", auth_devices_keys_hash=""
        )
        self.ingress = TraefikRouteRequirer(self, self.model.get_relation("ingress"), "ingress")  # type: ignore
        self.framework.observe(self.on["ingress"].relation_joined, self._configure_ingress)
        self.framework.observe(self.ingress.on.ready, self._on_ingress_ready)
        self.framework.observe(self.on.leader_elected, self._configure_ingress)
        self.framework.observe(self.on.config_changed, self._configure_ingress)
        self.framework.observe(self.on.update_status, self._on_update_status)

        self.framework.observe(
            self.on.cos_registration_server_pebble_ready, self._update_layer_and_restart
        )

        self.framework.observe(
            self.on.get_admin_password_action,  # pyright: ignore
            self._on_get_admin_password,
        )

        self.catalog = CatalogueConsumer(
            charm=self,
            refresh_event=[
                self.on.cos_registration_server_pebble_ready,
                self.ingress.on.ready,
                self.on["ingress"].relation_broken,
                self.on.config_changed,
            ],
            item=CatalogueItem(
                name="COS registration server",
                icon="graph-line-variant",
                url=self.external_url + "/devices/",
                description=("COS registration server to register devices."),
            ),
        )

        self.grafana_dashboard_provider = GrafanaDashboardProvider(
            charm=self, dashboards_path=self._grafana_dashboards_path
        )

        self.auth_devices_keys_provider = AuthDevicesKeysProvider(
            charm=self, relation_name="auth-devices-keys"
        )

    def _on_ingress_ready(self, _) -> None:
        """Once Traefik tells us our external URL, make sure we reconfigure the charm."""
        self._update_layer_and_restart(None)

    def _generate_password(self) -> str:
        """Generates a random 12 character password."""
        chars = string.ascii_letters + string.digits
        return "".join(secrets.choice(chars) for _ in range(12))

    def _generate_admin_password(self) -> None:
        """Generate the admin password if it's not already in stored state, and store it there."""
        generated_password = self._generate_password()
        try:
            self.container.exec(
                ["/usr/bin/create_super_user.bash", "--noinput"],
                environment={
                    "DJANGO_SUPERUSER_PASSWORD": generated_password,
                    "DJANGO_SUPERUSER_EMAIL": "admin@example.com",
                    "DJANGO_SUPERUSER_USERNAME": "admin",
                },
            ).wait()
            self._stored.admin_password = generated_password
        except (ChangeError, ExecError) as e:
            logger.error(f"Failed to create the super user: {e}")

    def _get_admin_password(self) -> str:
        """Returns the password for the admin user.

        Assuming we can_connect, otherwise cannot produce output. Caller should guard.
        """
        if self._stored.admin_password:  # type: ignore[truthy-function]
            logger.debug("Admin was already created, returning the stored password")
        else:
            logger.debug(
                "COS registration server admin password is not in stored state, so generating a new one."
            )
            self._generate_admin_password()
        return self._stored.admin_password  # type: ignore

    def _on_get_admin_password(self, event: ActionEvent) -> None:
        """Returns the django url and password for the admin user as an action response."""
        if not self.container.can_connect():
            event.fail("The container is not ready yet. Please try again in a few minutes")
            return

        event.set_results(
            {
                "url": self.external_url + "/admin/",
                "user": "admin",
                "password": self._get_admin_password(),
            }
        )

    def _configure_ingress(self, event: HookEvent) -> None:
        """Set up ingress if a relation is joined, config changed, or a new leader election."""
        if not self.unit.is_leader():
            return

        # If it's a RelationJoinedEvent, set it in the ingress object
        if isinstance(event, RelationJoinedEvent):
            self.ingress._relation = event.relation

        # No matter what, check readiness -- this blindly checks whether `ingress._relation` is not
        # None, so it overlaps a little with the above, but works as expected on leader elections
        # and config-change
        if self.ingress.is_ready():
            self._update_layer_and_restart(None)
            self.ingress.submit_to_traefik(self._ingress_config)

    def _on_update_status(self, _) -> None:
        """Event processing hook that is common to all events to ensure idempotency."""
        if not self.container.can_connect():
            self.unit.status = MaintenanceStatus("Waiting for pod startup to complete")
            return
        self._update_grafana_dashboards()
        self._update_auth_devices_keys()

    def _get_grafana_dashboards_from_db(self):
        database_url = (
            self.external_url
            + COS_REGISTRATION_SERVER_API_URL_BASE
            + "applications/grafana/dashboards"
        )
        try:
            response = requests.get(database_url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch Grafana dashboards from '{database_url}': {e}")
            return None

    def _update_grafana_dashboards(self) -> None:
        if grafana_dashboards := self._get_grafana_dashboards_from_db():
            md5 = md5_dict(grafana_dashboards)
            if md5 != self._stored.dashboard_dict_hash:
                logger.info("Grafana dashboards dict hash changed, updating dashboards!")
                self._stored.dashboard_dict_hash = md5
                self.grafana_dashboard_provider.remove_non_builtin_dashboards()
                for dashboard in grafana_dashboards:
                    # assign dashboard uid in the grafana dashboard format
                    dashboard["dashboard"]["uid"] = dashboard["uid"]
                    self.grafana_dashboard_provider.add_dashboard(
                        json.dumps(dashboard["dashboard"]), inject_dropdowns=False
                    )

    def _update_auth_devices_keys(self) -> None:
        if auth_devices_keys_dict := self._get_auth_devices_keys_from_db():
            md5_keys_dict_hash = md5_dict(auth_devices_keys_dict)
            if md5_keys_dict_hash != self._stored.auth_devices_keys_hash:
                logger.info("Authorized device keys hash has changed, updating them!")
                self._stored.auth_devices_keys_hash = md5_keys_dict_hash
                self.auth_devices_keys_provider.update_all_auth_devices_keys_from_db(
                    auth_devices_keys_dict
                )

    def _update_layer_and_restart(self, event) -> None:
        """Define and start a workload using the Pebble API."""
        self.unit.status = MaintenanceStatus("Assembling pod spec")
        if self.container.can_connect():
            try:
                if not self.container.exists("/server_data/secret_key"):
                    self.container.exec(["/usr/bin/install.bash"]).wait()
                environment = {"GRAFANA_DASHBOARD_PATH": "/server_data/grafana_dashboards"}
                self.container.exec(["/usr/bin/configure.bash"], environment=environment).wait()
            except ExecError as e:
                logger.error(f"Failed to setup the server: {e}")

            new_layer = self._pebble_layer.to_dict()

            # Get the current pebble layer config
            services = self.container.get_plan().to_dict().get("services", {})
            if services != new_layer["services"]:  # pyright: ignore
                self.container.add_layer(self.name, self._pebble_layer, combine=True)

                logger.info("Added updated layer 'COS registration server' to Pebble plan")

                self.container.restart(self.name)
                logger.info(f"Restarted '{self.name}' service")
            self.unit.status = ActiveStatus()
        else:
            self.unit.status = WaitingStatus("Waiting for Pebble in workload container")

    def _get_auth_devices_keys_from_db(self):
        database_url = (
            self.internal_url
            + COS_REGISTRATION_SERVER_API_URL_BASE
            + "devices/?fields=uid,public_ssh_key"
        )
        try:
            response = requests.get(database_url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch auth devices keys from '{database_url}': {e}")
            return None

    @property
    def _scheme(self) -> str:
        return "http"

    @property
    def internal_url(self) -> str:
        """Return workload's internal URL. Used for ingress."""
        return f"{self._scheme}://{socket.getfqdn()}:{8000}"

    @property
    def external_url(self) -> str:
        """Return the external hostname configured, if any."""
        if self.ingress.external_host:
            path_prefix = f"{self.model.name}-{self.model.app.name}"
            return f"{self._scheme}://{self.ingress.external_host}/{path_prefix}"
        return self.internal_url

    @property
    def _ingress_config(self) -> dict:
        """Build a raw ingress configuration for Traefik."""
        # The path prefix is the same as in ingress per app
        external_path = f"{self.model.name}-{self.model.app.name}"

        routers = {
            "juju-{}-{}-router".format(self.model.name, self.model.app.name): {
                "entryPoints": ["web"],
                "rule": f"PathPrefix(`/{external_path}`)",
                "service": "juju-{}-{}-service".format(self.model.name, self.app.name),
            },
            "juju-{}-{}-router-tls".format(self.model.name, self.model.app.name): {
                "entryPoints": ["websecure"],
                "rule": f"PathPrefix(`/{external_path}`)",
                "service": "juju-{}-{}-service".format(self.model.name, self.app.name),
                "tls": {
                    "domains": [
                        {
                            "main": self.ingress.external_host,
                            "sans": [f"*.{self.ingress.external_host}"],
                        },
                    ],
                },
            },
        }

        services = {
            "juju-{}-{}-service".format(self.model.name, self.model.app.name): {
                "loadBalancer": {"servers": [{"url": self.internal_url}]}
            }
        }

        return {"http": {"routers": routers, "services": services}}

    @property
    def _pebble_layer(self):
        """Return a dictionary representing a Pebble layer."""
        command = " ".join(["/usr/bin/launcher.bash"])

        pebble_layer = Layer(
            {
                "summary": "cos registration server k8s layer",
                "description": "cos registration server k8s layer",
                "services": {
                    self.name: {
                        "override": "replace",
                        "summary": "cos-registration-server-k8s service",
                        "command": command,
                        "startup": "enabled",
                        "environment": {
                            "ALLOWED_HOST_DJANGO": self.ingress.external_host,
                            "SCRIPT_NAME": f"/{self.model.name}-{self.model.app.name}",
                            "GRAFANA_DASHBOARD_PATH": "/server_data/grafana_dashboards",
                        },
                    }
                },
            }
        )
        return pebble_layer


if __name__ == "__main__":  # pragma: nocover
    main(CosRegistrationServerCharm)  # type: ignore
