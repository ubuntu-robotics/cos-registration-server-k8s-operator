#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging
from pathlib import Path

import pytest
import yaml
from charmed_kubeflow_chisme.testing import (
    APP_GRAFANA_DASHBOARD,
    APP_LOGGING,
    GRAFANA_AGENT_APP,
    GRAFANA_AGENT_GRAFANA_DASHBOARD,
    GRAFANA_AGENT_LOGGING_PROVIDER,
    assert_grafana_dashboards,
    assert_logging,
)
from charmed_kubeflow_chisme.testing import get_alert_rules as get_alert_rule_from_files
from charmed_kubeflow_chisme.testing import get_grafana_dashboards
from charmed_kubeflow_chisme.testing.cos_integration import PROVIDES, REQUIRES
from charmed_kubeflow_chisme.testing.cos_integration import (
    _get_alert_rules as get_alert_rules_from_str,
)
from charmed_kubeflow_chisme.testing.cos_integration import (
    _get_app_relation_data,
    _get_unit_relation_data,
)
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
RESOURCE_NAME = "cos-registration-server-image"
RESOURCE_PATH = METADATA["resources"][RESOURCE_NAME]["upstream-source"]
APP_NAME = METADATA["name"]

APP_TRACING = "tracing"

APP_GRAFANA_DASHBOARD_DEVICES = "grafana-dashboard-devices"

GRAFANA_AGENT_LOGGING_CONSUMER = "logging"
GRAFANA_AGENT_TRACING_PROVIDER = "tracing-provider"

APP_LOKI_ALERT_RULE_FILES_DEVICES = "logging-alerts-devices"
APP_PROMETHEUS_ALERT_RULE_FILES_DEVICES = "send-remote-write-alerts-devices"

LOKI_ALERT_RULE_FILES_DIRECTORY_DEVICES = Path("./src/loki_alert_rules/devices")
PROMETHEUS_ALERT_RULE_FILES_DIRECTORY_DEVICES = Path("./src/prometheus_alert_rules/devices")

PROMETHEUS_SEND_REMOTE_WRITE = "send-remote-write"
PROMETHEUS_RECEIVE_REMOTE_WRITE = "receive-remote-write"
PROMETHEUS_APP = "prometheus-k8s"


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    # Build and deploy charm from local source folder
    charm = await ops_test.build_charm(".")
    resources = {RESOURCE_NAME: RESOURCE_PATH}

    await asyncio.gather(
        # Deploy the charm
        ops_test.model.deploy(charm, resources=resources, application_name=APP_NAME),

        # Deploy prometheus-k8s
        # We must deploy prometheus since grafana-agent-k8s doesn't receive remote-write
        ops_test.model.deploy(PROMETHEUS_APP, channel="latest/stable", trust=True),

        # Deploy grafana-agent
        ops_test.model.deploy(GRAFANA_AGENT_APP, channel="latest/stable")
    )

    # and wait for active/idle status
    await asyncio.gather(
        ops_test.model.wait_for_idle(
            apps=[APP_NAME, PROMETHEUS_APP], status="active", raise_on_blocked=True, timeout=600
        ),
        ops_test.model.wait_for_idle(apps=[GRAFANA_AGENT_APP], status="blocked", timeout=600)
    )


@pytest.mark.abort_on_fail
async def test_integrate(ops_test: OpsTest):
    await asyncio.gather(
        ops_test.model.integrate(
            f"{APP_NAME}:{APP_GRAFANA_DASHBOARD}",
            f"{GRAFANA_AGENT_APP}:{GRAFANA_AGENT_GRAFANA_DASHBOARD}",
        ),

        ops_test.model.integrate(
            f"{APP_NAME}:{APP_LOGGING}",
            f"{GRAFANA_AGENT_APP}:{GRAFANA_AGENT_LOGGING_PROVIDER}",
        ),

        ops_test.model.integrate(
            f"{APP_NAME}:{APP_GRAFANA_DASHBOARD_DEVICES}",
            f"{GRAFANA_AGENT_APP}:{GRAFANA_AGENT_GRAFANA_DASHBOARD}",
        ),

        ops_test.model.integrate(
            f"{APP_NAME}:{APP_LOKI_ALERT_RULE_FILES_DEVICES}",
            f"{GRAFANA_AGENT_APP}:{GRAFANA_AGENT_LOGGING_PROVIDER}",
        ),

        ops_test.model.integrate(
            f"{APP_NAME}:{APP_PROMETHEUS_ALERT_RULE_FILES_DEVICES}",
            f"{PROMETHEUS_APP}:{PROMETHEUS_RECEIVE_REMOTE_WRITE}",
        ),

        ops_test.model.integrate(
            f"{APP_NAME}:{APP_TRACING}",
            f"{GRAFANA_AGENT_APP}:{GRAFANA_AGENT_TRACING_PROVIDER}",
        )
    )

    await asyncio.gather(
        ops_test.model.wait_for_idle(
            apps=[APP_NAME, PROMETHEUS_APP], status="active", raise_on_blocked=True, timeout=180
        ),
        ops_test.model.wait_for_idle(apps=[GRAFANA_AGENT_APP], status="blocked", timeout=180)
    )


async def test_status(ops_test):
    """Assert on the unit status."""
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


async def test_logging(ops_test: OpsTest):
    """Test logging is defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    await assert_logging(app)


async def test_grafana_dashboards(ops_test: OpsTest):
    """Test Grafana dashboards are defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    dashboards = get_grafana_dashboards()
    logger.info("found dashboards: %s", dashboards)
    await assert_grafana_dashboards(app, dashboards)


async def test_grafana_dashboards_devices(ops_test: OpsTest, mocker):
    """Test Grafana dashboards are defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    # @todo get dashboard 'from db'
    dashboards = set()
    logger.info("found dashboards: %s", dashboards)
    mocker.patch(
        "charmed_kubeflow_chisme.testing.cos_integration.APP_GRAFANA_DASHBOARD",
        "grafana-dashboard-devices",
    )
    await assert_grafana_dashboards(app, dashboards)


async def test_loki_alert_rules_devices(ops_test: OpsTest):
    """Test Loki alert rules for devices are defined in relation data bag."""
    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", timeout=120)
    app = ops_test.model.applications[APP_NAME]
    # Get the set of rules that should have been pre-loaded
    alert_rules = get_alert_rule_from_files(LOKI_ALERT_RULE_FILES_DIRECTORY_DEVICES)
    logger.info("found alert rules: %s", alert_rules)
    # Get the dict of rules that has been received
    relation_data = await _get_app_relation_data(
        app, APP_LOKI_ALERT_RULE_FILES_DEVICES, side=REQUIRES
    )
    assert (
        "alert_rules" in relation_data
    ), f"{APP_LOKI_ALERT_RULE_FILES_DEVICES} relation is missing 'alert_rules'"  # fmt: skip
    # Convert the Dict to a set of rules
    relation_alert_rules = {
        get_alert_rules_from_str(alert_rules)
        for alert_rules in yaml.safe_load(relation_data["alert_rules"])
    }
    assert set(relation_alert_rules) == alert_rules


async def test_prometheus_alert_rules_devices(ops_test: OpsTest):
    """Test Loki alert rules for devices are defined in relation data bag."""
    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", timeout=120)
    app = ops_test.model.applications[APP_NAME]
    alert_rules = get_alert_rule_from_files(PROMETHEUS_ALERT_RULE_FILES_DIRECTORY_DEVICES)
    logger.info("found alert rules: %s", alert_rules)
    relation_data = await _get_app_relation_data(
        app, APP_PROMETHEUS_ALERT_RULE_FILES_DEVICES, side=PROVIDES
    )
    # When no rules, prometheus doesn't send the key "alert_rules"
    assert relation_data == {}


async def test_tracing(ops_test: OpsTest):
    """Test logging is defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]

    unit_relation_data = await _get_unit_relation_data(app, "tracing", side=PROVIDES)

    assert unit_relation_data
