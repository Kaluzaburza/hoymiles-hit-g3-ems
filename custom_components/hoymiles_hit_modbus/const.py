"""Constants for EMS for Hoymiles HIT-(5–20)L-G3."""

from __future__ import annotations

from homeassistant.const import Platform


DOMAIN = "hoymiles_hit_modbus"
NAME = "EMS for Hoymiles HIT-(5–20)L-G3"
VERSION = "1.5.7"

# Version of the managed Home Assistant EMS package schema. It changes only
# when the package YAML changes, independently from dashboard-only releases.
EMS_PACKAGE_VERSION = "1.5.7-supervisor-1b3"

# Existing helper created by the managed Home Assistant EMS package. Keep the
# setup-status sensor and Repairs check on this single shared sentinel so they
# cannot drift to a helper name that the package never creates.
EMS_PACKAGE_SENTINEL = "input_boolean.hoymiles_rce_discharge_enabled"
EMS_PACKAGE_VERSION_ENTITY = "sensor.hoymiles_ems_package_version"

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
ATTR_OVERWRITE = "overwrite"
