# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helper functions for writing tests."""

import json
import logging
from typing import Any, Dict

from juju.unit import Unit
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


async def get_traefik_proxied_endpoints(
    ops_test: OpsTest, traefik_app: str = "traefik"
) -> Dict[str, Any]:
    assert ops_test.model is not None
    traefik_leader: Unit = ops_test.model.applications[traefik_app].units[0]  # type: ignore
    action = await traefik_leader.run_action("show-proxied-endpoints")
    action_result = await action.wait()
    return json.loads(action_result.results["proxied-endpoints"])
