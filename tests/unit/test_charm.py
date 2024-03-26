import unittest
from unittest.mock import patch

import ops
import ops.testing
from charm import (
    CosRegistrationServerCharm,
    GrafanaDashboardProvider,
    AuthDevicesKeysProvider,
    md5_update_from_file,
    md5_dir,
    md5_dict,
)

import yaml
import os
import hashlib
import tempfile
from pathlib import Path

ops.testing.SIMULATE_CAN_CONNECT = True

EXTERNAL_HOST = "1.2.3.4"

k8s_resource_multipatch = patch.multiple(
    "charm.KubernetesComputeResourcesPatch",
    _namespace="test-namespace",
    _patch=lambda *a, **kw: True,
    is_ready=lambda *a, **kw: True,
)


class TestCharm(unittest.TestCase):

    def setUp(self):
        self.harness = ops.testing.Harness(CosRegistrationServerCharm)
        self.addCleanup(self.harness.cleanup)

        self.name = "cos-registration-server"

        self.harness.set_model_name("testmodel")
        self.harness.container_pebble_ready(self.name)
        self.harness.handle_exec(self.name, ["/usr/bin/install.bash"], result=0)
        self.harness.handle_exec(self.name, ["/usr/bin/configure.bash"], result=0)

        self.harness.add_storage("database", attach=True)[0]

        self.harness.set_leader(True)
        self.harness.begin()

    def test_create_super_user_action(self):
        self.harness.set_can_connect(self.name, True)
        self.harness.handle_exec(
            self.name, ["/usr/bin/create_super_user.bash", "--noinput"], result=0
        )
        action_output = self.harness.run_action("get-admin-password")
        self.assertEqual(len(action_output.results), 3)
        second_action_output = self.harness.run_action("get-admin-password")
        self.assertEqual(
            action_output.results["password"], second_action_output.results["password"]
        )

    @patch.multiple("charm.TraefikRouteRequirer", external_host=EXTERNAL_HOST)
    def test_cos_registration_server_pebble_ready(self):
        # Expected plan after Pebble ready with default config
        command = " ".join(["/usr/bin/launcher.bash"])

        expected_plan = {
            "services": {
                self.name: {
                    "override": "replace",
                    "summary": "cos-registration-server-k8s service",
                    "command": command,
                    "startup": "enabled",
                    "environment": {
                        "ALLOWED_HOST_DJANGO": EXTERNAL_HOST,
                        "SCRIPT_NAME": f"/{self.harness._backend.model_name}-{self.name}",
                        "GRAFANA_DASHBOARD_PATH": "/server_data/grafana_dashboards",
                    },
                }
            },
        }
        # Simulate the container coming up and emission of pebble-ready event
        self.harness.container_pebble_ready(self.name)
        # Get the plan now we've run PebbleReady
        updated_plan = self.harness.get_container_pebble_plan(self.name).to_dict()
        # Check we've got the plan we expected
        self.assertEqual(expected_plan, updated_plan)
        # Check the service was started
        service = self.harness.model.unit.get_container(self.name).get_service(self.name)
        self.assertTrue(service.is_running())
        # Ensure we set an ActiveStatus with no message
        self.assertEqual(self.harness.model.unit.status, ops.ActiveStatus())

    @patch.multiple("charm.TraefikRouteRequirer", external_host="1.2.3.4")
    @patch(
        "socket.getfqdn", new=lambda *args: "cos-registration-server-0.testmodel.svc.cluster.local"
    )
    def test_ingress_relation_sets_options_and_rel_data(self):
        self.harness.set_leader(True)
        self.harness.container_pebble_ready(self.name)
        rel_id = self.harness.add_relation("ingress", "traefik")
        self.harness.add_relation_unit(rel_id, "traefik/0")

        expected_rel_data = {
            "http": {
                "routers": {
                    "juju-testmodel-cos-registration-server-router": {
                        "entryPoints": ["web"],
                        "rule": "PathPrefix(`/testmodel-cos-registration-server`)",
                        "service": "juju-testmodel-cos-registration-server-service",
                    },
                    "juju-testmodel-cos-registration-server-router-tls": {
                        "entryPoints": ["websecure"],
                        "rule": "PathPrefix(`/testmodel-cos-registration-server`)",
                        "service": "juju-testmodel-cos-registration-server-service",
                        "tls": {"domains": [{"main": "1.2.3.4", "sans": ["*.1.2.3.4"]}]},
                    },
                },
                "services": {
                    "juju-testmodel-cos-registration-server-service": {
                        "loadBalancer": {
                            "servers": [
                                {
                                    "url": "http://cos-registration-server-0.testmodel.svc.cluster.local:8000"
                                }
                            ]
                        }
                    },
                },
            }
        }
        rel_data = self.harness.get_relation_data(rel_id, self.harness.charm.app.name)

        # The insanity of YAML here. It works for the lib, but a single load just strips off
        # the extra quoting and leaves regular YAML. Double parse it for the tests
        self.maxDiff = None
        self.assertEqual(yaml.safe_load(rel_data["config"]), expected_rel_data)

        self.assertEqual(
            self.harness.charm.external_url, "http://1.2.3.4/testmodel-cos-registration-server"
        )

    @patch.object(GrafanaDashboardProvider, "_reinitialize_dashboard_data")
    @patch.object(AuthDevicesKeysProvider, "update_all_auth_devices_keys_from_db")
    def test_update_status(
        self, patch__reinitialize_dashboard_data, patch_update_all_auth_devices_keys_from_db
    ):
        self.harness.set_can_connect(self.name, True)
        json_file_path = os.path.join(self.harness.charm._grafana_dashboards_path, "robot-1.json")
        os.mkdir(self.harness.charm._grafana_dashboards_path)
        with open(json_file_path, "w") as f:
            f.write('{"dashboard": True }')

        self.harness.charm.on.update_status.emit()

        self.assertEqual(patch__reinitialize_dashboard_data.call_count, 1)
        self.assertEqual(patch_update_all_auth_devices_keys_from_db.call_count, 1)

    @patch("requests.get")
    def test_get_pub_keys_from_db_success(self, mock_get):
        mock_get.return_value.json.return_value = {"0": "ssh-rsa pubkey1", "1": "ssh-rsa pubkey2"}
        result = self.harness.charm._get_auth_devices_keys_from_db()
        self.assertEqual(result, {"0": "ssh-rsa pubkey1", "1": "ssh-rsa pubkey2"})
        mock_get.assert_called_once_with(
            f"{self.harness.charm.internal_url}/api/v1/devices/?fields=uid,public_ssh_key"
        )

    @patch("requests.get")
    def test_update_auth_devices_keys_changed(self, mock_get):
        mock_get.return_value.json.return_value = {"0": "ssh-rsa pubkey1"}
        self.harness.charm._stored.auth_devices_keys_hash = ""
        self.harness.charm._update_auth_devices_keys()
        mock_get.assert_called_with(
            f"{self.harness.charm.internal_url}/api/v1/devices/?fields=uid,public_ssh_key"
        )
        self.assertNotEqual(self.harness.charm._stored.auth_devices_keys_hash, "")

        previous_hash = self.harness.charm._stored.auth_devices_keys_hash
        mock_get.return_value.json.return_value = {"0": "ssh-rsa pubkey1", "1": "ssh-rsa pubkey2"}
        self.harness.charm._update_auth_devices_keys()
        mock_get.assert_called_with(
            f"{self.harness.charm.internal_url}/api/v1/devices/?fields=uid,public_ssh_key"
        )
        self.assertNotEqual(self.harness.charm._stored.auth_devices_keys_hash, previous_hash)

    @patch("requests.get")
    def test_update_auth_devices_keys_not_changed(self, mock_get):
        mock_get.return_value.json.return_value = {"0": "ssh-rsa pubkey1"}
        self.harness.charm._stored.auth_devices_keys_hash = ""
        self.harness.charm._update_auth_devices_keys()
        mock_get.assert_called_with(
            f"{self.harness.charm.internal_url}/api/v1/devices/?fields=uid,public_ssh_key"
        )
        self.assertNotEqual(self.harness.charm._stored.auth_devices_keys_hash, "")

        previous_hash = self.harness.charm._stored.auth_devices_keys_hash
        self.harness.charm._update_auth_devices_keys()
        mock_get.assert_called_with(
            f"{self.harness.charm.internal_url}/api/v1/devices/?fields=uid,public_ssh_key"
        )
        self.assertEqual(self.harness.charm._stored.auth_devices_keys_hash, previous_hash)


class TestMD5(unittest.TestCase):

    def create_file(self, name, content):
        with open(self.directory_path / Path(name), "w") as f:
            f.write(content)

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory_path = Path(self.temporary_directory.name)

    def test_md5_update_file(self):
        self.create_file("robot-1.json", '{"dashboard": True}')
        hash = hashlib.md5()
        result = md5_update_from_file(self.directory_path / Path("robot-1.json"), hash)
        self.assertNotEqual(result, str())

    def test_md5_dir(self):
        self.create_file("robot-1.json", '{"dashboard": True}')
        self.create_file("robot-2.json", '{"dashboard": False}')
        result = md5_dir(self.directory_path)
        self.assertNotEqual(result, str())

    def test_md5_dict(self):
        test_dict = {"key1": "value1", "key2": "value2"}

        result = md5_dict(test_dict)
        self.assertNotEqual(result, str())
