import unittest
from unittest.mock import patch

import ops
import ops.testing
from charm import CosRegistrationServerCharm
import yaml

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
        self.harness.begin_with_initial_hooks()

    def test_create_super_user_action(self):
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
