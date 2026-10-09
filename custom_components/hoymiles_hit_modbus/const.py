"""Constants for EMS for Hoymiles HIT-(5–20)L-G3."""

from __future__ import annotations

from homeassistant.const import Platform


DOMAIN = "hoymiles_hit_modbus"
NAME = "EMS for Hoymiles HIT-(5–20)L-G3"
VERSION = "1.5.8rc2"

# Version of the managed Home Assistant EMS package schema. It changes only
# when the package YAML changes, independently from dashboard-only releases.
EMS_PACKAGE_VERSION = "1.5.8rc2"

# Existing helper created by the managed Home Assistant EMS package. Keep the
# setup-status sensor and Repairs check on this single shared sentinel so they
# cannot drift to a helper name that the package never creates.
EMS_PACKAGE_SENTINEL = "input_boolean.hoymiles_rce_discharge_enabled"
EMS_PACKAGE_VERSION_ENTITY = "sensor.hoymiles_ems_package_version"

# A0 output-only timeline intentionally omits the integration-name prefix so
# the DEV planner has one exact, policy-neutral public identity.
EMS_BASELINE_TIMELINE_ENTITY_ID = "sensor.hoymiles_ems_baseline_energy_timeline"
EMS_BASELINE_TIMELINE_TRANSLATION_KEY = "ems_baseline_energy_timeline"

CONF_SOURCE_DEVICE_ID = "source_device_id"
# Keep the user-selected source id as a stable identity anchor. Home Assistant
# 2026.8 can split a formerly composite device into per-config-entry devices;
# this optional value records the currently verified ESPHome successor without
# discarding the old composite id needed to resolve a later replacement.
CONF_RESOLVED_SOURCE_DEVICE_ID = "resolved_source_device_id"
CONF_COPY_ASSETS = "copy_assets"

PLATFORMS: tuple[Platform, ...] = (
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
)

SUPPORTED_SOURCE_DOMAINS = {"button", "sensor", "number", "select"}

SERVICE_INSTALL_ASSETS = "install_assets"
SERVICE_MASTER_STOP = "master_stop"
SERVICE_SET_EMS_PAUSED = "set_ems_paused"
SERVICE_SET_POLICY_ENABLED = "set_policy_enabled"
SERVICE_RESUME_AFTER_MASTER_STOP = "resume_after_master_stop"
ATTR_OVERWRITE = "overwrite"
ATTR_PAUSED = "paused"
ATTR_POLICY = "policy"
ATTR_ENABLED = "enabled"
