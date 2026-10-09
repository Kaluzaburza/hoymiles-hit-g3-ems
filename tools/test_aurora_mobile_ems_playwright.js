"use strict";

/**
 * Real-browser mobile regression for the Aurora shell and Variant A EMS page.
 *
 * The harness serves the canonical frontend module from this checkout and
 * supplies read-only synthetic baseline and canonical timelines. It never
 * connects to Home Assistant and its callService stub rejects every attempted
 * write.
 */

const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

let chromium;
let webkit;
try {
  ({ chromium, webkit } = require("playwright"));
} catch (error) {
  console.error(
    "Playwright is required. Install it or expose the bundled module through NODE_PATH.",
  );
  throw error;
}

const ROOT = path.resolve(process.env.HOYMILES_UI_TEST_ROOT || process.cwd());
const CARD_PATH = path.join(
  ROOT,
  "home_assistant",
  "www",
  "hoymiles-rce-chart-card.js",
);
const SCREENSHOT_PATH = path.resolve(
  process.env.HOYMILES_MOBILE_QA_SCREENSHOT
    || path.join(os.tmpdir(), "hoymiles-aurora-mobile-ems.png"),
);
const VIEWPORT = Object.freeze({ width: 390, height: 844 });

function check(condition, message) {
  if (!condition) throw new Error(message);
}

const HARNESS_HTML = String.raw`<!doctype html>
<html lang="pl">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
    <title>Aurora mobile EMS contract</title>
    <style>
      :root {
        color-scheme: dark;
        --primary-background-color: #07121f;
        --card-background-color: #101b27;
        --ha-card-background: #101b27;
        --primary-text-color: #f3f7fa;
        --secondary-text-color: #8fa4b5;
        --divider-color: rgba(136,181,210,.18);
      }
      html, body { background:#080d13; height:100%; margin:0; min-height:100%; overflow:hidden; width:100%; }
      body { display:flex; flex-direction:column; font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
      .fake-ha-header { align-items:center; background:#111820; border-bottom:1px solid rgba(255,255,255,.14); display:flex; flex:0 0 56px; height:56px; padding:0 12px; position:relative; z-index:4; }
      .fake-ha-header button { background:transparent; border:0; color:#f3f7fa; font:inherit; height:44px; min-width:44px; }
      #ha-view { flex:1 1 auto; min-height:0; min-width:0; overflow-y:auto; }
    </style>
    <script>
      window.customCards = [];
      window.__auroraServiceCalls = [];
      window.loadCardHelpers = async function () {
        return {
          createCardElement: function (config) {
            var tag = String(config.type || "").replace(/^custom:/, "");
            var element = document.createElement(tag);
            if (typeof element.setConfig !== "function") {
              throw new Error("Unknown nested card: " + config.type);
            }
            element.setConfig(config);
            return element;
          }
        };
      };

      window.__baselinePoints = function () {
        var today = new Date();
        var base = Date.UTC(
          today.getUTCFullYear(),
          today.getUTCMonth(),
          today.getUTCDate(),
          0, 0, 0, 0
        );
        var points = [];
        var soc = 42;
        for (var index = 0; index < 96; index += 1) {
          var hour = (index / 2) % 24;
          var pv = Math.max(0, Math.sin((hour - 6) / 12 * Math.PI)) * 4.8;
          var load = 0.55 + ((hour >= 17 && hour < 22) ? 0.85 : 0.15);
          var battery = pv > load + 0.35
            ? Math.min(1.8, pv - load)
            : ((hour >= 18 || hour < 6) ? -0.45 : 0);
          var nextSoc = Math.max(25, Math.min(92, soc + battery * 0.5 / 26 * 100));
          var start = new Date(base + index * 30 * 60 * 1000).toISOString();
          var end = new Date(base + (index + 1) * 30 * 60 * 1000).toISOString();
          points.push({
            start: start,
            end: end,
            pv_kw: Number(pv.toFixed(4)),
            load_kw: Number(load.toFixed(4)),
            battery_kw: Number(battery.toFixed(4)),
            grid_import_kw: Number(Math.max(0, load - pv + Math.max(0, battery)).toFixed(4)),
            grid_export_kw: Number(Math.max(0, pv - load - Math.max(0, battery)).toFixed(4)),
            soc_start_percent: Number(soc.toFixed(4)),
            soc_end_percent: Number(nextSoc.toFixed(4)),
            reserve_soc_percent: 25,
            quality: "synthetic",
            blocker_code: null,
            source: { fixture: "playwright" },
            provenance: { fixture: "synthetic_baseline" }
          });
          soc = nextSoc;
        }
        return points;
      };

      window.__makeHass = function (refresh) {
        var stamp = new Date(Date.now() + Number(refresh || 0)).toISOString();
        var state = function (entityId, value, attributes) {
          return {
            entity_id: entityId,
            state: String(value),
            attributes: attributes || {},
            last_changed: stamp,
            last_updated: stamp,
            context: { id: "synthetic-" + String(refresh || 0) }
          };
        };
        var points = window.__baselinePoints();
        var states = {};
        var put = function (item) { states[item.entity_id] = item; };
        put(state("sensor.hoymiles_ems_baseline_energy_timeline", "current", {
          schema_version: "2.0",
          timeline_kind: "baseline_self_use",
          output_only: true,
          authority: false,
          battery_sign_convention: "positive_charge",
          current: true,
          generated_at: stamp,
          quality: "synthetic",
          interval_minutes: 30,
          point_count: points.length,
          source: { fixture: "playwright" },
          provenance: { fixture: "synthetic_baseline" },
          points: points
        }));
        put(state("sensor.hoymiles_hit_ems_supervisor", "idle", {
          selected_action: "none",
          execution_health: {
            schema_version: 1,
            status: "healthy",
            control_status: "healthy",
            reason: "idle",
            action: "none",
            phase: "idle",
            connectivity: "connected",
            last_valid_read_at: stamp,
            last_valid_read_age_seconds: 0,
            owner: "none",
            observed_owner: "none",
            owner_conflict: false,
            master_stop_status: "not_requested",
            master_stop_confirmation: "not_requested",
            rollback_status: "confirmed",
            physical_verification: "current",
            degradations: {}
          }
        }));
        put(state("input_select.hoymiles_ems_supervisor_mode", "Off", {
          options: ["Off", "Active"]
        }));
        put(state("input_select.hoymiles_ems_supervisor_profile", "Balanced", {
          options: ["Balanced", "Maximum Profit", "High Reserve — Winter"]
        }));
        put(state("input_boolean.hoymiles_ems_supervisor_allow_rce", "off"));
        put(state("input_boolean.hoymiles_ems_supervisor_allow_tariff", "off"));
        put(state("input_boolean.hoymiles_ems_supervisor_allow_rcm", "off"));
        put(state("input_button.hoymiles_ems_supervisor_master_stop", stamp));
        put(state("input_boolean.hoymiles_tariff_charge_active", "off"));
        put(state("input_text.hoymiles_tariff_active_action", "none"));
        put(state("input_boolean.hoymiles_rcm_active", "off"));
        put(state("input_boolean.hoymiles_rcm_export_control_active", "off"));
        put(state("input_boolean.hoymiles_rcm_pre_discharge_active", "off"));
        put(state("sensor.hoymiles_ems_hardware_mode", "self_use"));
        put(state("binary_sensor.hoymiles_ems_control_conflict", "off"));
        put(state("binary_sensor.hoymiles_ems_execution_ready", "off"));
        put(state("sensor.hoymiles_hit_overview_battery_soc", "42", {
          unit_of_measurement: "%"
        }));
        put(state("sensor.hoymiles_hit_battery_capacity", "26", {
          unit_of_measurement: "kWh"
        }));
        put(state("sensor.hoymiles_hit_overview_battery_power", "0.25", {
          unit_of_measurement: "kW"
        }));
        put(state("sensor.hoymiles_hit_grid_voltage_l1", "233.5", {
          unit_of_measurement: "V"
        }));
        put(state("input_boolean.hoymiles_battery_balancing_enabled", "on"));
        put(state("input_boolean.hoymiles_battery_balancing_active", "off"));
        put(state("sensor.hoymiles_battery_balancing_status", "waiting"));
        put(state("sensor.hoymiles_battery_balancing_next_run", new Date(Date.now() + 18 * 86400000).toISOString()));
        put(state("input_number.hoymiles_battery_balancing_hold_hours", "2", {
          unit_of_measurement: "h"
        }));
        return {
          states: states,
          language: "pl",
          locale: { language: "pl", number_format: "comma_decimal" },
          config: { time_zone: "Europe/Warsaw" },
          callService: async function () {
            window.__auroraServiceCalls.push(Array.from(arguments));
            throw new Error("Read-only mobile QA attempted a Home Assistant service call");
          }
        };
      };

      window.__canonicalPlan = function (builtAt, expectedSource) {
        expectedSource = expectedSource || "provider_p50";
        var baseline = window.__baselinePoints();
        var capacityKwh = 26;
        var soc = 60;
        var actionByPolicy = {
          rce: "rce_export",
          tariff: "tariff_battery_charge",
          rcm: "rcm_absorb_pv"
        };
        var targetByAction = {
          none: "none",
          rce_export: "ems_block_4300_4306",
          tariff_battery_charge: "ems_block_4300_4306",
          rcm_absorb_pv: "battery_charge_limit_306"
        };
        var physicalByAction = {
          none: "none",
          rce_export: "grid_export_and_battery_discharge",
          tariff_battery_charge: "grid_import_and_battery_charge",
          rcm_absorb_pv: "pv_surplus_and_battery_charge"
        };
        var slots = baseline.map(function (point, index) {
          var selectedPolicy = index >= 8 && index < 12
            ? "rce"
            : index >= 28 && index < 32
              ? "tariff"
              : index >= 44 && index < 48
                ? "rcm"
                : "none";
          var selectedAction = selectedPolicy === "none"
            ? "none"
            : actionByPolicy[selectedPolicy];
          var batteryKwh = selectedPolicy === "rce"
            ? -0.52
            : selectedPolicy === "none"
              ? 0
              : 0.52;
          var pvKwh = selectedPolicy === "rcm" ? 0.72 : 0.2;
          var loadKwh = 0.2;
          var gridKwh = selectedPolicy === "rce"
            ? -0.52
            : selectedPolicy === "tariff"
              ? 0.52
              : 0;
          var flows = {
            pv_to_battery_kwh: selectedPolicy === "rcm" ? 0.52 : 0,
            grid_to_battery_kwh: selectedPolicy === "tariff" ? 0.52 : 0,
            battery_to_load_kwh: 0,
            battery_to_grid_kwh: selectedPolicy === "rce" ? 0.52 : 0,
            losses_kwh: 0
          };
          var socEnd = soc + 100 * batteryKwh / capacityKwh;
          var candidates = ["rce", "tariff", "rcm"].map(function (policy) {
            var selected = policy === selectedPolicy;
            return {
              policy_id: policy,
              requested_action: actionByPolicy[policy],
              eligible: selected,
              start_eligibility: selected ? "eligible" : "blocked",
              input_revision: 1000 + index,
              candidate_revision: 2000 + index,
              target_soc_percent: selected && policy !== "rce" ? socEnd : null,
              rejected_reasons: selected ? [] : [selectedPolicy === "none" ? "no_action" : "lower_priority"]
            };
          });
          var rejectedReasons = candidates
            .filter(function (candidate) { return candidate.rejected_reasons.length > 0; })
            .map(function (candidate) {
              return {
                policy_id: candidate.policy_id,
                reasons: candidate.rejected_reasons
              };
            });
          var commandValues = selectedAction === "none"
            ? {}
            : selectedPolicy === "rcm"
              ? { battery_charge_limit_percent: 100 }
              : { ems_mode_code: selectedPolicy === "rce" ? 3 : 4 };
          var slot = {
            slot_id: "mobile-slot-" + String(index).padStart(3, "0"),
            starts_at: point.start,
            ends_at: point.end,
            policy_candidates: candidates,
            selected_policy: selectedPolicy,
            selected_action: selectedAction,
            rejected_reasons: rejectedReasons,
            start_eligibility: selectedPolicy === "none" ? "not_applicable" : "eligible",
            owner: selectedPolicy,
            planned: {
              pv_kwh: pvKwh,
              load_kwh: loadKwh,
              battery_kwh: batteryKwh,
              grid_kwh_import_positive: gridKwh,
              potential_pv_kwh: pvKwh + 0.08,
              usable_pv_kwh: pvKwh,
              pv_curtailed_kwh: 0.08,
              expected_grid_export_kwh: selectedPolicy === "rce" ? 0.52 : 0,
              authorization_grid_export_kwh: selectedPolicy === "rce" ? 0.42 : 0
            },
            soc_equation: {
              pv_to_battery_kwh: flows.pv_to_battery_kwh,
              grid_to_battery_kwh: flows.grid_to_battery_kwh,
              battery_to_load_kwh: flows.battery_to_load_kwh,
              battery_to_grid_kwh: flows.battery_to_grid_kwh,
              losses_kwh: flows.losses_kwh,
              soc_start_percent: soc,
              soc_end_percent: socEnd,
              expected_soc_start_percent: soc + 1,
              expected_soc_end_percent: socEnd + 1,
              expected_source: expectedSource,
              authorization_soc_start_percent: soc,
              authorization_soc_end_percent: socEnd,
              energy_balance_residual_kwh: 0,
              continuity_residual_percent: 0,
              system_balance_residual_kwh: 0
            },
            protected_reserve: {
              percent: 25,
              margin_end_percent: socEnd - 25,
              respected: true
            },
            command_expectation: {
              target: targetByAction[selectedAction],
              source_generation: selectedAction === "none" ? null : 3000 + index,
              values: commandValues
            },
            readback_expectation: {
              target: targetByAction[selectedAction],
              newer_than_source_generation: selectedAction !== "none",
              values: commandValues,
              physical_expectation: physicalByAction[selectedAction]
            }
          };
          soc = socEnd;
          return slot;
        });
        return {
          state: "current",
          attributes: {
            schema_version: 1,
            output_only: true,
            built_at: builtAt,
            arbitration_revision: "a".repeat(64),
            usable_capacity_kwh: capacityKwh,
            initial_soc_percent: 60,
            final_soc_percent: soc,
            slots: slots,
            audit: {
              max_continuity_residual_percent: 0,
              max_energy_balance_residual_kwh: 0,
              max_system_balance_residual_kwh: 0,
              reserve_violation_count: 0
            },
            ledger_revision: "b".repeat(64)
          }
        };
      };

      window.__makeCanonicalHass = function (refresh, expectedSource) {
        var hass = window.__makeHass(refresh);
        var stamp = new Date(Date.now() + Number(refresh || 0)).toISOString();
        var entityId = "sensor.hoymiles_hit_ems_supervisor_canonical_plan";
        var canonical = window.__canonicalPlan(stamp, expectedSource);
        hass.states[entityId] = {
          entity_id: entityId,
          state: canonical.state,
          attributes: canonical.attributes,
          last_changed: stamp,
          last_updated: stamp,
          context: { id: "synthetic-canonical-" + String(refresh || 0) }
        };
        hass.states["input_select.hoymiles_ems_supervisor_mode"].state = "Active";
        hass.states["input_boolean.hoymiles_ems_supervisor_allow_rce"].state = "on";
        hass.states["input_boolean.hoymiles_ems_supervisor_allow_tariff"].state = "on";
        hass.states["input_boolean.hoymiles_ems_supervisor_allow_rcm"].state = "on";
        hass.states["binary_sensor.hoymiles_ems_execution_ready"].state = "on";
        ["tariff", "rcm"].forEach(function (policy) {
          var actionCode = policy === "tariff" ? "battery_charge" : "absorb_pv";
          var points = canonical.attributes.slots
            .filter(function (slot) { return slot.selected_policy === policy; })
            .map(function (slot) {
              return {
                start: slot.starts_at,
                end: slot.ends_at,
                action_code: actionCode,
                selected: true,
                policy: policy === "tariff" ? {
                  planned_import_kwh: slot.planned.grid_kwh_import_positive,
                  stored_energy_kwh: slot.soc_equation.grid_to_battery_kwh,
                  direct_load_kwh: 0
                } : {}
              };
            });
          var timelineId = "sensor.hoymiles_hit_" + policy + "_automation_plan_timeline";
          hass.states[timelineId] = {
            entity_id: timelineId,
            state: "current",
            attributes: {
              schema_version: 2,
              policy_id: policy,
              result_current: true,
              recalculation_pending: false,
              quality: "complete",
              blocker_code: null,
              point_count: points.length,
              points: points
            },
            last_changed: stamp,
            last_updated: stamp,
            context: { id: "synthetic-" + policy + "-" + String(refresh || 0) }
          };
        });
        return hass;
      };

      window.__makePendingCanonicalHass = function (refresh, pendingPolicy) {
        var hass = window.__makeCanonicalHass(refresh);
        var canonical = hass.states["sensor.hoymiles_hit_ems_supervisor_canonical_plan"];
        canonical.state = "pending";
        canonical.attributes.canonical_status = "pending";
        canonical.attributes.canonical_blocker_code = "source_recalculation_pending";
        canonical.attributes.result_current = false;
        canonical.attributes.recalculation_pending = true;
        var timeline = hass.states[
          "sensor.hoymiles_hit_" + pendingPolicy + "_automation_plan_timeline"
        ];
        timeline.state = "pending";
        timeline.attributes.result_current = false;
        timeline.attributes.recalculation_pending = true;
        timeline.attributes.quality = "pending";
        timeline.attributes.blocker_code = "recalculation_pending";
        timeline.attributes.pending_input_revision = 1001;
        return hass;
      };
    </script>
    <script type="module" src="/hoymiles-rce-chart-card.js"></script>
    <script type="module">
      await customElements.whenDefined("hoymiles-aurora-app-shell-card");
      await customElements.whenDefined("hoymiles-aurora-variant-a-ems-page-card");
      var shell = document.createElement("hoymiles-aurora-app-shell-card");
      shell.setConfig({
        current_path: "plan-automatyki",
        language: "pl",
        card: {
          type: "custom:hoymiles-aurora-variant-a-ems-page-card",
          language: "pl"
        }
      });
      shell.hass = window.__makeHass(0);
      document.getElementById("ha-view").append(shell);
      window.__auroraQaShell = shell;
      window.__auroraQaReady = true;
    </script>
  </head>
  <body>
    <header class="fake-ha-header" data-ha-native-header>
      <button id="ha-menu-button" type="button" aria-label="Otwórz menu Home Assistant">☰</button>
      <strong>Home Assistant</strong>
    </header>
    <main id="ha-view"></main>
  </body>
</html>`;

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return server.address().port;
}

async function closeServer(server) {
  await new Promise((resolve) => server.close(resolve));
}

async function launchBrowser() {
  const requested = String(process.env.HOYMILES_UI_BROWSER || "chromium").toLowerCase();
  const browserType = { chromium, webkit }[requested];
  check(browserType, `Unsupported HOYMILES_UI_BROWSER: ${requested}`);
  const options = { headless: true };
  if (requested === "webkit") return webkit.launch(options);
  if (process.env.HOYMILES_CHROME_EXECUTABLE) {
    return chromium.launch({
      ...options,
      executablePath: process.env.HOYMILES_CHROME_EXECUTABLE,
    });
  }
  try {
    return await chromium.launch({ ...options, channel: "chrome" });
  } catch (chromeError) {
    try {
      return await chromium.launch(options);
    } catch (chromiumError) {
      chromiumError.message += `\nInstalled-Chrome launch also failed: ${chromeError.message}`;
      throw chromiumError;
    }
  }
}

function heightDelta(before, after) {
  return Math.max(
    ...Object.keys(before).map((key) => Math.abs(Number(after[key]) - Number(before[key]))),
  );
}

async function main() {
  check(fs.existsSync(CARD_PATH), `Canonical frontend module is missing: ${CARD_PATH}`);
  const cardSource = fs.readFileSync(CARD_PATH);
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    if (url.pathname === "/hoymiles-rce-chart-card.js") {
      response.writeHead(200, {
        "content-type": "text/javascript; charset=utf-8",
        "cache-control": "no-store",
      });
      response.end(cardSource);
      return;
    }
    if (url.pathname === "/favicon.ico") {
      response.writeHead(204);
      response.end();
      return;
    }
    response.writeHead(200, {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
    });
    response.end(HARNESS_HTML);
  });

  let browser;
  const browserErrors = [];
  try {
    const port = await listen(server);
    browser = await launchBrowser();
    const context = await browser.newContext({
      viewport: VIEWPORT,
      screen: VIEWPORT,
      deviceScaleFactor: 1,
      colorScheme: "dark",
      reducedMotion: "reduce",
      isMobile: true,
      hasTouch: true,
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => browserErrors.push(`pageerror: ${error.message}`));
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
    });
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => {
      if (!window.__auroraQaReady) return false;
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const ems = shell?.shadowRoot?.querySelector(
        "hoymiles-aurora-variant-a-ems-page-card",
      );
      return Boolean(
        ems?.shadowRoot?.querySelector("[data-soc-chart]")
        && ems.shadowRoot.querySelectorAll("[data-chart-slot]").length === 96,
      );
    });

    const shellSelector = "hoymiles-aurora-app-shell-card";
    const emsSelector = `${shellSelector} hoymiles-aurora-variant-a-ems-page-card`;
    const nativeChromeGeometry = await page.evaluate(() => {
      const header = document.querySelector("[data-ha-native-header]");
      const button = document.getElementById("ha-menu-button");
      const host = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = host.shadowRoot.querySelector(".shell");
      const headerRect = header.getBoundingClientRect();
      const buttonRect = button.getBoundingClientRect();
      const hostRect = host.getBoundingClientRect();
      const surfaceRect = surface.getBoundingClientRect();
      const hit = document.elementFromPoint(
        buttonRect.left + buttonRect.width / 2,
        buttonRect.top + buttonRect.height / 2,
      );
      return {
        headerBottom: headerRect.bottom,
        hostTop: hostRect.top,
        hostHeight: hostRect.height,
        surfaceTop: surfaceRect.top,
        surfaceBottom: surfaceRect.bottom,
        surfaceHeight: surfaceRect.height,
        position: getComputedStyle(surface).position,
        overflowY: getComputedStyle(surface).overflowY,
        zIndex: getComputedStyle(surface).zIndex,
        outerOverflowY: getComputedStyle(document.getElementById("ha-view")).overflowY,
        hitId: hit?.id || "",
      };
    });
    check(
      nativeChromeGeometry.position === "relative"
      && nativeChromeGeometry.zIndex === "auto"
      && nativeChromeGeometry.overflowY === "auto"
      && nativeChromeGeometry.outerOverflowY === "auto",
      `Aurora shell is not the bounded mobile scroll owner inside Home Assistant: ${JSON.stringify(nativeChromeGeometry)}`,
    );
    check(
      nativeChromeGeometry.hostTop >= nativeChromeGeometry.headerBottom - 1
      && nativeChromeGeometry.surfaceTop >= nativeChromeGeometry.headerBottom - 1
      && nativeChromeGeometry.surfaceBottom > nativeChromeGeometry.surfaceTop
      && nativeChromeGeometry.surfaceBottom <= VIEWPORT.height + 1,
      `Aurora shell covers native Home Assistant chrome: ${JSON.stringify(nativeChromeGeometry)}`,
    );
    check(
      nativeChromeGeometry.hostHeight > 0
      && Math.abs(nativeChromeGeometry.hostHeight - (VIEWPORT.height - nativeChromeGeometry.headerBottom)) <= 1
      && Math.abs(nativeChromeGeometry.surfaceHeight - nativeChromeGeometry.hostHeight) <= 1,
      `Aurora shell has invalid embedded height: ${JSON.stringify(nativeChromeGeometry)}`,
    );
    check(
      nativeChromeGeometry.hitId === "ha-menu-button",
      `Native Home Assistant menu is not the top click target: ${JSON.stringify(nativeChromeGeometry)}`,
    );
    await page.evaluate(() => {
      window.__nativeHaMenuClicks = 0;
      document.getElementById("ha-menu-button").addEventListener("click", () => {
        window.__nativeHaMenuClicks += 1;
      });
    });
    await page.locator("#ha-menu-button").click();
    check(
      await page.evaluate(() => window.__nativeHaMenuClicks) === 1,
      "Native Home Assistant menu button did not receive a real click",
    );
    const mobileNav = page.locator(`${shellSelector} .mobile-nav`);
    check(await mobileNav.isVisible(), "Mobile bottom navigation is not visible at 390 px");
    const navMetrics = await mobileNav.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      const links = [...node.querySelectorAll("a")];
      return {
        display: getComputedStyle(node).display,
        top: rect.top,
        bottom: rect.bottom,
        height: rect.height,
        labels: links.map((link) => link.textContent.trim()),
        widths: links.map((link) => link.getBoundingClientRect().width),
        heights: links.map((link) => link.getBoundingClientRect().height),
        rows: links.map((link) => ({top:link.getBoundingClientRect().top,bottom:link.getBoundingClientRect().bottom})),
        icons: links.map((link) => link.querySelector("ha-icon")?.getAttribute("icon") || ""),
        iconBoxes: links.map((link) => {
          const rect = link.querySelector(".mobile-icon")?.getBoundingClientRect();
          return rect ? { width: rect.width, height: rect.height } : null;
        }),
        active: links.filter((link) => link.classList.contains("active")).map((link) => link.textContent.trim()),
      };
    });
    check(navMetrics.display === "grid", `Mobile navigation display is ${navMetrics.display}`);
    check(navMetrics.rows.every(row => Math.abs(row.top-navMetrics.rows[0].top)<1 && row.bottom<=navMetrics.bottom), "All seven tabs must fit in one mobile navigation row");
    check(navMetrics.labels.join("|") === "24h|EMS|UST|PV|BAT|kWh|Zyski", `Unexpected mobile labels: ${navMetrics.labels.join("|")}`);
    check(navMetrics.active.length === 1 && navMetrics.active[0] === "EMS", `Unexpected active mobile destination: ${navMetrics.active.join(", ")}`);
    check(navMetrics.widths.every((width) => width >= 40), "At least one mobile navigation target is too narrow to click");
    check(navMetrics.heights.every((height) => height >= 48), "At least one mobile navigation target is too short to tap");
    check(
      navMetrics.icons.join("|") === "mdi:view-dashboard-outline|mdi:chart-timeline-variant|mdi:tune-variant|mdi:solar-power-variant|mdi:battery-high|mdi:lightning-bolt|mdi:cash-multiple",
      `Unexpected mobile icons: ${navMetrics.icons.join("|")}`,
    );
    check(
      navMetrics.iconBoxes.every((box) => box && box.width >= 41 && box.height >= 41),
      `Mobile icon tiles are too small: ${JSON.stringify(navMetrics.iconBoxes)}`,
    );
    check(navMetrics.bottom <= VIEWPORT.height + 1 && navMetrics.bottom >= VIEWPORT.height - 1, "Mobile navigation is not fixed to the viewport bottom");
    const topNavScrollbar = await page.locator(`${shellSelector} .nav`).evaluate((node) => ({
      msOverflowStyle: getComputedStyle(node).msOverflowStyle,
      overflowX: getComputedStyle(node).overflowX,
      scrollbarWidth: getComputedStyle(node).scrollbarWidth,
    }));
    check(
      topNavScrollbar.overflowX === "auto"
      && topNavScrollbar.scrollbarWidth === "none",
      `Top navigation must stay swipeable without a visible native scrollbar: ${JSON.stringify(topNavScrollbar)}`,
    );

    await page.evaluate(() => {
      const link = document
        .querySelector("hoymiles-aurora-app-shell-card")
        .shadowRoot.querySelectorAll(".mobile-nav a")[1];
      window.__auroraMobileClickCount = 0;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        window.__auroraMobileClickCount += 1;
      }, { once: true });
    });
    await mobileNav.locator("a").nth(1).click();
    check(
      await page.evaluate(() => window.__auroraMobileClickCount) === 1,
      "The EMS item in mobile navigation did not receive a real click",
    );

    const moreButton = page.locator(`${shellSelector} [data-more]`);
    check(await moreButton.isVisible(), "The three-dot overflow button is not visible");
    check(
      await moreButton.getAttribute("aria-label") === "Więcej opcji nawigacji",
      "The overflow button lost its accessible Polish name",
    );
    check(
      await moreButton.getAttribute("aria-expanded") === "false",
      "The overflow menu must start collapsed",
    );
    await moreButton.click();
    const overflowMenu = page.locator(`${shellSelector} [data-overflow-menu]`);
    check(await overflowMenu.isVisible(), "The three-dot button did not open its menu");
    const overflowLinks = await overflowMenu.locator("a").evaluateAll((links) =>
      links.map((link) => ({ href: link.getAttribute("href"), text: link.childNodes[0]?.textContent?.trim() || link.textContent.trim() })),
    );
    check(
      overflowLinks.length === 2
      && overflowLinks[0].href === "/hoymiles-falownik/diagnostyka"
      && overflowLinks[1].href === "/config",
      `Unexpected overflow destinations: ${JSON.stringify(overflowLinks)}`,
    );
    await page.keyboard.press("Escape");
    check(!(await overflowMenu.isVisible()), "Escape did not close the overflow menu");
    check(
      await moreButton.getAttribute("aria-expanded") === "false",
      "Escape did not restore aria-expanded=false",
    );
    await moreButton.click();
    check(await overflowMenu.isVisible(), "The overflow menu did not reopen for outside-tap QA");
    await page.locator(`${shellSelector} .content`).click({ position: { x: 3, y: 3 }, force: true });
    check(!(await overflowMenu.isVisible()), "A tap in Aurora content did not close the overflow menu");
    check(
      await moreButton.getAttribute("aria-expanded") === "false",
      "Outside tap did not restore aria-expanded=false",
    );

    const overflowBefore = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const root = ems.shadowRoot.querySelector(".root");
      return {
        viewport: document.documentElement.clientWidth,
        document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        shellClient: surface.clientWidth,
        shellScroll: surface.scrollWidth,
        emsClient: root.clientWidth,
        emsScroll: root.scrollWidth,
      };
    });
    check(
      overflowBefore.document <= overflowBefore.viewport + 1
      && overflowBefore.body <= overflowBefore.viewport + 1
      && overflowBefore.shellScroll <= overflowBefore.shellClient + 1
      && overflowBefore.emsScroll <= overflowBefore.emsClient + 1,
      `Whole-page horizontal overflow detected: ${JSON.stringify(overflowBefore)}`,
    );

    const mobileChartGeometry = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const viewport = ems.shadowRoot.querySelector("[data-chart-viewport]");
      const svg = viewport.querySelector("[data-soc-chart]");
      const today = ems.shadowRoot.querySelector('[data-chart-day="today"]');
      const tomorrow = ems.shadowRoot.querySelector('[data-chart-day="tomorrow"]');
      const viewportRect = viewport.getBoundingClientRect();
      const svgRect = svg.getBoundingClientRect();
      const buttonMetric = (button) => {
        const rect = button.getBoundingClientRect();
        return {
          height: rect.height,
          width: rect.width,
          display: getComputedStyle(button).display,
          pressed: button.getAttribute("aria-pressed"),
        };
      };
      return {
        shellOverflowY: getComputedStyle(surface).overflowY,
        viewportOverflowX: getComputedStyle(viewport).overflowX,
        viewportClientWidth: viewport.clientWidth,
        viewportScrollWidth: viewport.scrollWidth,
        viewportRectWidth: viewportRect.width,
        svgWidth: svgRect.width,
        svgHeight: svgRect.height,
        viewBoxWidth: svg.viewBox.baseVal.width,
        today: buttonMetric(today),
        tomorrow: buttonMetric(tomorrow),
      };
    });
    check(
      mobileChartGeometry.shellOverflowY === "auto"
      && mobileChartGeometry.viewportOverflowX === "auto"
      && mobileChartGeometry.viewportScrollWidth > mobileChartGeometry.viewportClientWidth
      && mobileChartGeometry.viewportRectWidth <= overflowBefore.shellClient + 1,
      `Mobile EMS chart is not isolated in a horizontal viewport: ${JSON.stringify(mobileChartGeometry)}`,
    );
    check(
      mobileChartGeometry.svgWidth >= 1100
      && mobileChartGeometry.svgWidth <= 1140
      && mobileChartGeometry.viewBoxWidth === 1120
      && mobileChartGeometry.svgHeight >= 290,
      `Mobile EMS chart lost its readable approximately 1120 px canvas: ${JSON.stringify(mobileChartGeometry)}`,
    );
    check(
      mobileChartGeometry.today.height >= 44
      && mobileChartGeometry.tomorrow.height >= 44
      && mobileChartGeometry.today.width >= 44
      && mobileChartGeometry.tomorrow.width >= 44
      && mobileChartGeometry.today.display !== "none"
      && mobileChartGeometry.tomorrow.display !== "none"
      && mobileChartGeometry.today.pressed === "true"
      && mobileChartGeometry.tomorrow.pressed === "false",
      `Dziś/Jutro mobile chart controls are not accessible 44 px targets: ${JSON.stringify(mobileChartGeometry)}`,
    );

    const tomorrowJump = await page.evaluate(async () => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const viewport = ems.shadowRoot.querySelector("[data-chart-viewport]");
      const button = ems.shadowRoot.querySelector('[data-chart-day="tomorrow"]');
      button.scrollIntoView({ block: "center", inline: "nearest" });
      surface.scrollTop = Math.max(0, surface.scrollTop - 18);
      const beforeTop = surface.scrollTop;
      const beforeLeft = viewport.scrollLeft;
      button.click();
      await new Promise((resolve) => {
        let frames = 6;
        const settle = () => {
          frames -= 1;
          if (frames > 0) requestAnimationFrame(settle);
          else resolve();
        };
        requestAnimationFrame(settle);
      });
      return {
        beforeTop,
        afterTop: surface.scrollTop,
        beforeLeft,
        afterLeft: viewport.scrollLeft,
        todayPressed: ems.shadowRoot.querySelector('[data-chart-day="today"]').getAttribute("aria-pressed"),
        tomorrowPressed: button.getAttribute("aria-pressed"),
        serviceCalls: window.__auroraServiceCalls.length,
      };
    });
    check(tomorrowJump.beforeTop > 0, "Tomorrow jump was not tested from a vertically scrolled mobile shell");
    check(
      tomorrowJump.afterLeft > tomorrowJump.beforeLeft + 100
      && Math.abs(tomorrowJump.afterTop - tomorrowJump.beforeTop) <= 1
      && tomorrowJump.todayPressed === "false"
      && tomorrowJump.tomorrowPressed === "true"
      && tomorrowJump.serviceCalls === 0,
      `Jutro did not move only the horizontal chart viewport: ${JSON.stringify(tomorrowJump)}`,
    );

    await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const chart = ems.shadowRoot.querySelector("[data-chart]");
      const chartViewport = ems.shadowRoot.querySelector("[data-chart-viewport]");
      chart.scrollIntoView({ block: "center", inline: "nearest" });
      chartViewport.scrollLeft = Math.min(420, chartViewport.scrollWidth - chartViewport.clientWidth);
      const scrollOwner = shell.shadowRoot.querySelector(".shell");
      scrollOwner.scrollTop = Math.max(0, scrollOwner.scrollTop - 18);
    });
    await page.waitForTimeout(50);

    await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const scrollOwner = surface;
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const chart = ems.shadowRoot.querySelector("[data-chart]");
      const chartViewport = ems.shadowRoot.querySelector("[data-chart-viewport]");
      window.__auroraChartIdentity = chart.querySelector("[data-soc-chart]");
      window.__auroraChartMutations = 0;
      window.__auroraChartObserver = new MutationObserver((records) => {
        window.__auroraChartMutations += records.length;
      });
      window.__auroraChartObserver.observe(chart, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
      });
      window.__auroraShellMutations = 0;
      window.__auroraShellMutationDetails = [];
      window.__auroraShellObserver = new MutationObserver((records) => {
        window.__auroraShellMutations += records.length;
        window.__auroraShellMutationDetails.push(...records.map((record) => ({
          attribute: record.attributeName,
          target: record.target?.getAttribute?.("data-chart-plan-status") !== null
            ? "chart-plan-status"
            : record.target?.className || record.target?.nodeName,
        })));
      });
      window.__auroraShellObserver.observe(surface, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
      });
      window.__auroraEmsMutations = 0;
      window.__auroraEmsMutationDetails = [];
      window.__auroraEmsObserver = new MutationObserver((records) => {
        window.__auroraEmsMutations += records.length;
        window.__auroraEmsMutationDetails.push(...records.map((record) => ({
          attribute: record.attributeName,
          target: record.target?.hasAttribute?.("data-chart-plan-status")
            ? "chart-plan-status"
            : record.target?.className || record.target?.nodeName,
        })));
      });
      window.__auroraEmsObserver.observe(ems.shadowRoot.querySelector(".root"), {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
      });
      window.__auroraScrollTopBeforeRefresh = scrollOwner.scrollTop;
      window.__auroraChartScrollLeftBeforeRefresh = chartViewport.scrollLeft;
      for (let refresh = 1; refresh <= 50; refresh += 1) {
        shell.hass = window.__makeHass(refresh);
      }
    });
    await page.waitForTimeout(50);
    const refreshResult = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const scrollOwner = surface;
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const chart = ems.shadowRoot.querySelector("[data-chart]");
      const chartViewport = ems.shadowRoot.querySelector("[data-chart-viewport]");
      const header = document.querySelector("[data-ha-native-header]");
      const hostRect = shell.getBoundingClientRect();
      const surfaceRect = surface.getBoundingClientRect();
      window.__auroraChartObserver.disconnect();
      window.__auroraShellObserver.disconnect();
      window.__auroraEmsObserver.disconnect();
      return {
        before: window.__auroraScrollTopBeforeRefresh,
        after: scrollOwner.scrollTop,
        delta: Math.abs(scrollOwner.scrollTop - window.__auroraScrollTopBeforeRefresh),
        chartScrollLeftBefore: window.__auroraChartScrollLeftBeforeRefresh,
        chartScrollLeftAfter: chartViewport.scrollLeft,
        chartScrollLeftDelta: Math.abs(chartViewport.scrollLeft - window.__auroraChartScrollLeftBeforeRefresh),
        headerBottom: header.getBoundingClientRect().bottom,
        hostTop: hostRect.top,
        hostHeight: hostRect.height,
        surfaceTop: surfaceRect.top,
        surfaceHeight: surfaceRect.height,
        outerScrollTop: document.getElementById("ha-view").scrollTop,
        sameSvg: chart.querySelector("[data-soc-chart]") === window.__auroraChartIdentity,
        mutations: window.__auroraChartMutations,
        shellMutations: window.__auroraShellMutations,
        shellMutationDetails: window.__auroraShellMutationDetails.slice(0, 12),
        emsMutations: window.__auroraEmsMutations,
        emsMutationDetails: window.__auroraEmsMutationDetails.slice(0, 12),
        slotCount: chart.querySelectorAll("[data-chart-slot]").length,
      };
    });
    check(refreshResult.before > 0, "Refresh stability was not tested from a scrolled position");
    check(refreshResult.delta <= 1, `50 hass refreshes shifted scrollTop by ${refreshResult.delta}px`);
    check(
      Math.abs(refreshResult.headerBottom - nativeChromeGeometry.headerBottom) <= 1
      && Math.abs(refreshResult.hostTop - nativeChromeGeometry.hostTop) <= 1
      && Math.abs(refreshResult.hostHeight - nativeChromeGeometry.hostHeight) <= 1
      && Math.abs(refreshResult.surfaceTop - nativeChromeGeometry.surfaceTop) <= 1
      && Math.abs(refreshResult.surfaceHeight - nativeChromeGeometry.surfaceHeight) <= 1
      && refreshResult.outerScrollTop === 0,
      `50 hass refreshes changed native-header or bounded-shell geometry: ${JSON.stringify(refreshResult)}`,
    );
    check(
      refreshResult.chartScrollLeftBefore > 0
      && refreshResult.chartScrollLeftDelta <= 1,
      `50 hass refreshes shifted chart scrollLeft: ${JSON.stringify(refreshResult)}`,
    );
    check(refreshResult.sameSvg, "50 unchanged hass refreshes recreated the SOC chart SVG");
    check(refreshResult.mutations === 0, `Unchanged SOC chart received ${refreshResult.mutations} DOM mutations`);
    check(refreshResult.shellMutations === 0, `Unchanged Aurora shell received ${refreshResult.shellMutations} DOM mutations: ${JSON.stringify(refreshResult.shellMutationDetails)}`);
    check(refreshResult.emsMutations === 0, `Unchanged EMS page received ${refreshResult.emsMutations} DOM mutations: ${JSON.stringify(refreshResult.emsMutationDetails)}`);
    check(refreshResult.slotCount === 96, `Chart lost slots after refresh: ${refreshResult.slotCount}`);

    const heightsBefore = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const root = ems.shadowRoot.querySelector(".root");
      return {
        document: document.documentElement.scrollHeight,
        body: document.body.scrollHeight,
        shell: surface.scrollHeight,
        ems: root.scrollHeight,
      };
    });
    const inspectorBefore = await page.evaluate(() => {
      const ems = document
        .querySelector("hoymiles-aurora-app-shell-card")
        .shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const svg = ems.shadowRoot.querySelector("[data-soc-chart]");
      const inspector = ems.shadowRoot.querySelector("[data-chart-inspector]");
      const svgRect = svg.getBoundingClientRect();
      const inspectorRect = inspector.getBoundingClientRect();
      const style = getComputedStyle(inspector);
      return {
        svg: { left: svgRect.left, right: svgRect.right, top: svgRect.top, bottom: svgRect.bottom },
        inspector: { left: inspectorRect.left, right: inspectorRect.right, top: inspectorRect.top, bottom: inspectorRect.bottom, width: inspectorRect.width, height: inspectorRect.height },
        ariaHidden: inspector.getAttribute("aria-hidden"),
        position: style.position,
        pointerEvents: style.pointerEvents,
        touchAction: style.touchAction,
        overflowY: style.overflowY,
        clientHeight: inspector.clientHeight,
        scrollHeight: inspector.scrollHeight,
      };
    });
    check(inspectorBefore.ariaHidden === "true", "Slot inspector must start in its stable hint state");
    check(
      inspectorBefore.position === "static"
      && inspectorBefore.pointerEvents === "auto"
      && inspectorBefore.touchAction === "pan-y"
      && ["visible", "hidden", "clip"].includes(inspectorBefore.overflowY)
      && inspectorBefore.inspector.height >= 63
      && inspectorBefore.scrollHeight <= inspectorBefore.clientHeight + 1,
      `Slot inspector is not an interactive stable panel: ${JSON.stringify(inspectorBefore)}`,
    );
    check(
      inspectorBefore.inspector.top >= inspectorBefore.svg.bottom - 1,
      `Mobile slot inspector overlaps the SOC chart: ${JSON.stringify(inspectorBefore)}`,
    );
    const planLegend = page.locator(`${emsSelector} [data-plan-legend]`);
    check(await planLegend.isVisible(), "The RCE/Tariff/Voltage/Balance plan legend is not visible on mobile");
    const planLegendPolicies = await planLegend.locator("[data-plan-policy]").evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-plan-policy")),
    );
    check(
      planLegendPolicies.join("|") === "rcm|tariff|rce|balance",
      `Unexpected plan legend policies: ${planLegendPolicies.join("|")}`,
    );
    const firstFlowSlotIndex = 65;
    const slot = page.locator(`${emsSelector} [data-chart-slot]`).nth(firstFlowSlotIndex);
    const legacyPlansTrack = page.locator(`${emsSelector} [data-plans-track]`);
    const gridFlowBars = page.locator(`${emsSelector} .grid-flow-bar`);
    check(await slot.isVisible(), "Synthetic SOC slot is not visible/clickable");
    check(await legacyPlansTrack.count() === 0, "The duplicated PLANY track is still rendered");
    check(await gridFlowBars.count() > 0, "Grid import/export bars are not rendered");
    const slotBox = await slot.boundingBox();
    check(slotBox, "The SOC slot has no rendered geometry");
    const flowTapForSlot = async (box) => {
      const bars = await gridFlowBars.evaluateAll((nodes, target) => nodes
        .map((node) => {
          const rect = node.getBoundingClientRect();
          return {
            direction: node.dataset.gridFlow,
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
            width: rect.width,
            height: rect.height,
          };
        })
        .filter((bar) => bar.width > 0 && bar.height > 0
          && bar.left >= target.x - 0.5
          && bar.right <= target.x + target.width + 0.5
          && bar.top >= target.y - 0.5
          && bar.bottom <= target.y + target.height + 0.5)
        .sort((left, right) => right.height - left.height), box);
      check(
        bars.length > 0,
        `The slot hit area does not cover a visible grid-flow bar: slot=${JSON.stringify(box)}`,
      );
      const bar = bars[0];
      const position = {
        x: bar.left + bar.width / 2 - box.x,
        y: bar.top + bar.height / 2 - box.y,
      };
      check(
        position.x > 0 && position.x < box.width
        && position.y > 0 && position.y < box.height,
        `The grid-flow bar centre is outside its slot hit area: slot=${JSON.stringify(box)} bar=${JSON.stringify(bar)}`,
      );
      return { bar, position };
    };
    const firstFlowTap = await flowTapForSlot(slotBox);
    await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const ems = shell.shadowRoot.querySelector(
        "hoymiles-aurora-variant-a-ems-page-card",
      );
      const root = ems.shadowRoot;
      window.__auroraSelectionChartIdentity = root.querySelector("[data-chart]");
      window.__auroraSelectionSvgIdentity = root.querySelector("[data-soc-chart]");
      window.__auroraSelectionLineIdentity = root.querySelector("[data-chart-selected-line]");
      window.__auroraSelectionDotIdentity = root.querySelector("[data-chart-selected-dot]");
      window.__auroraSelectionInspectorIdentity = root.querySelector("[data-chart-inspector]");
      window.__auroraSelectionRenderSignature = ems._chartRenderSignature;
      window.__auroraCaptureSelectionState = (targetEms, slotIndex) => {
        const targetRoot = targetEms.shadowRoot;
        const chart = targetRoot.querySelector("[data-chart]");
        const svg = targetRoot.querySelector("[data-soc-chart]");
        const line = targetRoot.querySelector("[data-chart-selected-line]");
        const dot = targetRoot.querySelector("[data-chart-selected-dot]");
        const inspector = targetRoot.querySelector("[data-chart-inspector]");
        const target = targetRoot.querySelector(`[data-chart-slot="${slotIndex}"]`);
        const selected = [...targetRoot.querySelectorAll("[data-chart-slot].selected")];
        const slotData = targetEms._chartSlots?.[slotIndex] || null;
        const lineX1 = Number(line?.getAttribute("x1"));
        const lineX2 = Number(line?.getAttribute("x2"));
        const dotX = Number(dot?.getAttribute("cx"));
        const dotY = Number(dot?.getAttribute("cy"));
        const targetX = Number(target?.dataset?.selectionX);
        const targetY = Number(target?.dataset?.selectionY);
        const inspectorText = inspector?.textContent.replace(/\s+/g, " ").trim() || "";
        const expectedWindow = slotData
          ? `${targetEms._formatTime(slotData.start)}–${targetEms._formatTime(slotData.end)}`
          : "";
        const expectedSoc = slotData
          ? `${targetEms._number(slotData.socStart, 1)}% → ${targetEms._number(slotData.soc, 1)}%`
          : "";
        return {
          selectedCount: selected.length,
          selectedIndex: Number(selected[0]?.dataset?.chartSlot),
          selectedStart: targetEms._selectedChartStart,
          expectedStart: slotData?.start ?? null,
          lineVisible: line?.dataset?.visible,
          dotVisible: dot?.dataset?.visible,
          lineDisplay: line ? getComputedStyle(line).display : "missing",
          dotDisplay: dot ? getComputedStyle(dot).display : "missing",
          lineX1,
          lineX2,
          dotX,
          dotY,
          targetX,
          targetY,
          finiteGeometry: [lineX1, lineX2, dotX, dotY, targetX, targetY]
            .every(Number.isFinite),
          inspectorAriaHidden: inspector?.getAttribute("aria-hidden"),
          inspectorVisible: inspector?.dataset?.visible,
          inspectorText,
          expectedWindow,
          expectedSoc,
          panelMatches: Boolean(expectedWindow && expectedSoc)
            && inspectorText.includes(expectedWindow)
            && inspectorText.includes(expectedSoc),
          sameChart: chart === window.__auroraSelectionChartIdentity,
          sameSvg: svg === window.__auroraSelectionSvgIdentity,
          sameLine: line === window.__auroraSelectionLineIdentity,
          sameDot: dot === window.__auroraSelectionDotIdentity,
          sameInspector: inspector === window.__auroraSelectionInspectorIdentity,
          sameRenderSignature:
            targetEms._chartRenderSignature === window.__auroraSelectionRenderSignature,
        };
      };
      window.__auroraSynchronousSelections = [];
      const originalSelectChartSlot = ems._selectChartSlot;
      ems._selectChartSlot = function (slotIndex, focus = false) {
        const result = originalSelectChartSlot.call(this, slotIndex, focus);
        window.__auroraSynchronousSelections.push(
          window.__auroraCaptureSelectionState(this, slotIndex),
        );
        return result;
      };
    });
    const readImmediateSelection = async (expectedIndex) => page.evaluate((slotIndex) => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const ems = shell.shadowRoot.querySelector(
        "hoymiles-aurora-variant-a-ems-page-card",
      );
      const state = window.__auroraCaptureSelectionState(ems, slotIndex);
      const synchronous = [...window.__auroraSynchronousSelections]
        .reverse()
        .find((snapshot) => snapshot.selectedIndex === slotIndex) || null;
      return { ...state, synchronous };
    }, expectedIndex);
    await slot.tap({ position: firstFlowTap.position });
    const firstImmediateSelection = await readImmediateSelection(firstFlowSlotIndex);
    const { synchronous: firstSynchronousSelection, ...firstPostTapSelection } =
      firstImmediateSelection;
    check(
      firstSynchronousSelection
      && JSON.stringify(firstSynchronousSelection) === JSON.stringify(firstPostTapSelection),
      `First marker latch was not complete in the _selectChartSlot call stack: ${JSON.stringify(firstImmediateSelection)}`,
    );
    check(
      firstImmediateSelection.selectedCount === 1
      && firstImmediateSelection.selectedIndex === firstFlowSlotIndex
      && firstImmediateSelection.selectedStart === firstImmediateSelection.expectedStart
      && firstImmediateSelection.lineVisible === "true"
      && firstImmediateSelection.dotVisible === "true"
      && firstImmediateSelection.lineDisplay !== "none"
      && firstImmediateSelection.dotDisplay !== "none"
      && firstImmediateSelection.finiteGeometry
      && Math.abs(firstImmediateSelection.lineX1 - firstImmediateSelection.targetX) < 0.01
      && Math.abs(firstImmediateSelection.lineX2 - firstImmediateSelection.targetX) < 0.01
      && Math.abs(firstImmediateSelection.dotX - firstImmediateSelection.targetX) < 0.01
      && Math.abs(firstImmediateSelection.dotY - firstImmediateSelection.targetY) < 0.01
      && firstImmediateSelection.inspectorAriaHidden === "false"
      && firstImmediateSelection.inspectorVisible === "true"
      && firstImmediateSelection.panelMatches
      && firstImmediateSelection.sameChart
      && firstImmediateSelection.sameSvg
      && firstImmediateSelection.sameLine
      && firstImmediateSelection.sameDot
      && firstImmediateSelection.sameInspector
      && firstImmediateSelection.sameRenderSignature,
      `First tap did not synchronously latch marker and panel without rerender: ${JSON.stringify(firstImmediateSelection)}`,
    );
    const nextSlot = page.locator(`${emsSelector} [data-chart-slot]`).nth(13);
    const nextSlotBox = await nextSlot.boundingBox();
    check(nextSlotBox, "The second SOC slot has no rendered geometry");
    const secondFlowTap = {
      position: {
        x: nextSlotBox.width / 2,
        y: firstFlowTap.bar.top + firstFlowTap.bar.height / 2 - nextSlotBox.y,
      },
    };
    check(
      secondFlowTap.position.y > 0 && secondFlowTap.position.y < nextSlotBox.height,
      `The adjacent slot does not reach the verified grid-flow region: slot=${JSON.stringify(nextSlotBox)} bar=${JSON.stringify(firstFlowTap.bar)}`,
    );
    await nextSlot.tap({ position: secondFlowTap.position });
    const secondImmediateSelection = await readImmediateSelection(13);
    const { synchronous: secondSynchronousSelection, ...secondPostTapSelection } =
      secondImmediateSelection;
    check(
      secondSynchronousSelection
      && JSON.stringify(secondSynchronousSelection) === JSON.stringify(secondPostTapSelection),
      `Second marker latch was not complete in the _selectChartSlot call stack: ${JSON.stringify(secondImmediateSelection)}`,
    );
    check(
      secondImmediateSelection.selectedCount === 1
      && secondImmediateSelection.selectedIndex === 13
      && secondImmediateSelection.selectedStart === secondImmediateSelection.expectedStart
      && secondImmediateSelection.lineVisible === "true"
      && secondImmediateSelection.dotVisible === "true"
      && secondImmediateSelection.lineDisplay !== "none"
      && secondImmediateSelection.dotDisplay !== "none"
      && secondImmediateSelection.finiteGeometry
      && Math.abs(secondImmediateSelection.lineX1 - secondImmediateSelection.targetX) < 0.01
      && Math.abs(secondImmediateSelection.lineX2 - secondImmediateSelection.targetX) < 0.01
      && Math.abs(secondImmediateSelection.dotX - secondImmediateSelection.targetX) < 0.01
      && Math.abs(secondImmediateSelection.dotY - secondImmediateSelection.targetY) < 0.01
      && secondImmediateSelection.inspectorAriaHidden === "false"
      && secondImmediateSelection.inspectorVisible === "true"
      && secondImmediateSelection.panelMatches
      && secondImmediateSelection.sameChart
      && secondImmediateSelection.sameSvg
      && secondImmediateSelection.sameLine
      && secondImmediateSelection.sameDot
      && secondImmediateSelection.sameInspector
      && secondImmediateSelection.sameRenderSignature
      && Math.abs(secondImmediateSelection.targetX - firstImmediateSelection.targetX) > 0.01
      && secondImmediateSelection.expectedWindow !== firstImmediateSelection.expectedWindow
      && secondImmediateSelection.inspectorText !== firstImmediateSelection.inspectorText,
      `Second tap did not synchronously move the latched marker and panel without rerender: first=${JSON.stringify(firstImmediateSelection)} second=${JSON.stringify(secondImmediateSelection)}`,
    );
    const inspector = page.locator(`${emsSelector} [data-chart-inspector]`);
    check(await inspector.isVisible(), "Clicking a SOC slot did not reveal the inspector");
    check(
      await inspector.getAttribute("aria-hidden") === "false",
      "The visible slot inspector remains hidden from accessibility APIs",
    );
    const inspectorText = (await inspector.innerText()).replace(/\s+/g, " ").trim();
    check(inspectorText.includes("SOC"), `Slot inspector has no SOC transition: ${inspectorText}`);
    check(
      inspectorText.includes("Prognoza bazowa"),
      `Slot inspector did not retain baseline provenance: ${inspectorText}`,
    );
    const inspectorAfterClick = await inspector.evaluate((node) => {
      const svg = node.parentElement.querySelector("[data-soc-chart]");
      const svgRect = svg.getBoundingClientRect();
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return {
        svgBottom: svgRect.bottom,
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height,
        overflowY: style.overflowY,
        clientHeight: node.clientHeight,
        scrollHeight: node.scrollHeight,
      };
    });
    check(
      inspectorAfterClick.top >= inspectorAfterClick.svgBottom - 1,
      `Selected-slot panel overlaps the SOC chart: ${JSON.stringify(inspectorAfterClick)}`,
    );
    check(
      inspectorAfterClick.height >= 63
      && ["visible", "hidden", "clip"].includes(inspectorAfterClick.overflowY)
      && inspectorAfterClick.scrollHeight <= inspectorAfterClick.clientHeight + 1,
      `Selected-slot panel has an internal scrollbar or is too short: ${JSON.stringify(inspectorAfterClick)}`,
    );
    check(
      Math.abs(inspectorAfterClick.left - inspectorBefore.inspector.left) <= 1
      && Math.abs(inspectorAfterClick.top - inspectorBefore.inspector.top) <= 1
      && Math.abs(inspectorAfterClick.width - inspectorBefore.inspector.width) <= 1
      && inspectorAfterClick.height >= inspectorBefore.inspector.height,
      `Selecting a SOC slot moved or narrowed the mobile panel: before=${JSON.stringify(inspectorBefore.inspector)} after=${JSON.stringify(inspectorAfterClick)}`,
    );
    const heightsSelected = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const root = ems.shadowRoot.querySelector(".root");
      return {
        document: document.documentElement.scrollHeight,
        body: document.body.scrollHeight,
        shell: surface.scrollHeight,
        ems: root.scrollHeight,
      };
    });
    await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const scrollOwner = surface;
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const inspector = ems.shadowRoot.querySelector("[data-chart-inspector]");
      window.__auroraShellSurfaceIdentity = surface;
      window.__auroraEmsIdentity = ems;
      window.__auroraShellScrollTop = scrollOwner.scrollTop;
      window.__auroraInspectorIdentity = inspector;
      window.__auroraInspectorHtml = inspector.innerHTML;
      window.__auroraInspectorSelectedStart = ems._selectedChartStart;
      window.__auroraInspectorScrollTop = inspector.scrollTop;
      window.__auroraInspectorClientHeight = inspector.clientHeight;
      window.__auroraInspectorScrollHeight = inspector.scrollHeight;
      window.__auroraInspectorMutations = 0;
      window.__auroraInspectorObserver = new MutationObserver((records) => {
        window.__auroraInspectorMutations += records.length;
      });
      window.__auroraInspectorObserver.observe(inspector, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
      });
      for (let refresh = 51; refresh <= 100; refresh += 1) {
        shell.hass = window.__makeHass(refresh);
      }
      for (let configRefresh = 0; configRefresh < 50; configRefresh += 1) {
        shell.setConfig({ ...shell._config });
      }
    });
    await page.waitForTimeout(50);
    const inspectorRefreshResult = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const scrollOwner = surface;
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const inspector = ems.shadowRoot.querySelector("[data-chart-inspector]");
      window.__auroraInspectorObserver.disconnect();
      return {
        sameNode: inspector === window.__auroraInspectorIdentity,
        sameSurface: surface === window.__auroraShellSurfaceIdentity,
        sameEms: ems === window.__auroraEmsIdentity,
        sameHtml: inspector.innerHTML === window.__auroraInspectorHtml,
        sameSelection: ems._selectedChartStart === window.__auroraInspectorSelectedStart,
        beforeShellScrollTop: window.__auroraShellScrollTop,
        afterShellScrollTop: scrollOwner.scrollTop,
        beforeScrollTop: window.__auroraInspectorScrollTop,
        afterScrollTop: inspector.scrollTop,
        beforeClientHeight: window.__auroraInspectorClientHeight,
        afterClientHeight: inspector.clientHeight,
        beforeScrollHeight: window.__auroraInspectorScrollHeight,
        afterScrollHeight: inspector.scrollHeight,
        mutations: window.__auroraInspectorMutations,
      };
    });
    check(inspectorRefreshResult.sameSurface, "50 identical setConfig calls replaced the shell surface");
    check(inspectorRefreshResult.sameEms, "50 identical setConfig calls replaced the EMS card");
    check(inspectorRefreshResult.sameNode, "50 hass refreshes replaced the selected-slot inspector");
    check(inspectorRefreshResult.sameHtml, "50 hass refreshes rewrote unchanged inspector content");
    check(inspectorRefreshResult.sameSelection, "50 hass refreshes lost the selected SOC slot");
    check(
      Math.abs(inspectorRefreshResult.afterScrollTop - inspectorRefreshResult.beforeScrollTop) <= 1,
      `50 hass refreshes shifted inspector scrollTop: ${JSON.stringify(inspectorRefreshResult)}`,
    );
    check(
      inspectorRefreshResult.beforeScrollHeight <= inspectorRefreshResult.beforeClientHeight + 1
      && inspectorRefreshResult.afterScrollHeight <= inspectorRefreshResult.afterClientHeight + 1,
      `50 hass refreshes introduced inspector overflow: ${JSON.stringify(inspectorRefreshResult)}`,
    );
    check(
      Math.abs(inspectorRefreshResult.afterShellScrollTop - inspectorRefreshResult.beforeShellScrollTop) <= 1,
      `50 identical setConfig calls shifted shell scrollTop: ${JSON.stringify(inspectorRefreshResult)}`,
    );
    check(
      inspectorRefreshResult.mutations === 0,
      `Unchanged selected-slot inspector received ${inspectorRefreshResult.mutations} mutations`,
    );
    const heightsAfter = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
      const root = ems.shadowRoot.querySelector(".root");
      return {
        document: document.documentElement.scrollHeight,
        body: document.body.scrollHeight,
        shell: surface.scrollHeight,
        ems: root.scrollHeight,
      };
    });
    check(
      heightDelta(heightsSelected, heightsAfter) <= 1,
      `Unchanged selected inspector changed document/layout height: before=${JSON.stringify(heightsSelected)} after=${JSON.stringify(heightsAfter)}`,
    );

    const overflowAfter = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const surface = shell.shadowRoot.querySelector(".shell");
      return {
        viewport: document.documentElement.clientWidth,
        document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        shellClient: surface.clientWidth,
        shellScroll: surface.scrollWidth,
      };
    });
    check(
      overflowAfter.document <= overflowAfter.viewport + 1
      && overflowAfter.body <= overflowAfter.viewport + 1
      && overflowAfter.shellScroll <= overflowAfter.shellClient + 1,
      `Inspector introduced whole-page horizontal overflow: ${JSON.stringify(overflowAfter)}`,
    );
    check(
      await page.evaluate(() => window.__auroraServiceCalls.length) === 0,
      "Chart inspection attempted a Home Assistant service write",
    );

    const tariffNoChargeContract = await page.evaluate(() => {
      const card = document.createElement("hoymiles-aurora-variant-a-ems-page-card");
      card.setConfig({ language: "pl" });
      const stamp = new Date().toISOString();
      const entity = (state, attributes) => ({
        state,
        attributes,
        last_changed: stamp,
        last_updated: stamp,
        last_reported: stamp,
      });
      const planEntity = "sensor.hoymiles_hit_tariff_charge_plan";
      const timelineEntity = "sensor.hoymiles_hit_tariff_automation_plan_timeline";
      const conflictEntity = "binary_sensor.hoymiles_ems_control_conflict";
      const verifiedStates = {
        [conflictEntity]: entity("off", {}),
        [planEntity]: entity("ready", {
          status_code: "no_charge_needed",
          result_current: true,
          recalculation_pending: false,
          control_inputs_fresh: true,
          input_revision: 73,
          planned_slots: [],
          planned_grid_import_kwh: 0,
          planned_stored_energy_kwh: 0,
          planned_direct_load_kwh: 0,
        }),
        [timelineEntity]: entity("current", {
          schema_version: 2,
          policy_id: "tariff",
          result_current: true,
          recalculation_pending: false,
          input_revision: 73,
          plan_entity_id: planEntity,
          plan_revision_scope: "runtime",
          quality: "complete",
          generated_at: stamp,
          plan_revision: 9,
          point_count: 2,
          points: [0, 1].map(() => ({
            selected: false,
            action_code: "idle",
            policy: {
              planned_import_kwh: 0,
              stored_energy_kwh: 0,
              direct_load_kwh: 0,
              planned_charge_kw: 0,
            },
          })),
        }),
      };
      const verify = (mutate) => {
        const states = structuredClone(verifiedStates);
        if (mutate) mutate(states);
        card._hass = { states };
        return card._verifiedTariffNoChargePlan();
      };
      return {
        bindings: {
          plan: card._config.tariff_plan_entity,
          timeline: card._config.tariff_timeline_entity,
          conflict: card._config.control_conflict_entity,
        },
        confirmed: verify(),
        unavailable: verify((states) => {
          states[planEntity].state = "unavailable";
        }),
        pending: verify((states) => {
          states[timelineEntity].state = "pending";
          states[timelineEntity].attributes.result_current = false;
          states[timelineEntity].attributes.recalculation_pending = true;
        }),
        revisionMismatch: verify((states) => {
          states[timelineEntity].attributes.input_revision = 72;
        }),
        stringZero: verify((states) => {
          states[planEntity].attributes.planned_grid_import_kwh = "0";
        }),
      };
    });
    check(
      tariffNoChargeContract.confirmed === true
      && tariffNoChargeContract.unavailable === false
      && tariffNoChargeContract.pending === false
      && tariffNoChargeContract.revisionMismatch === false
      && tariffNoChargeContract.stringZero === false,
      `Tariff no-charge gate confuses a confirmed idle result with unavailable, pending or unverifiable data: ${JSON.stringify(tariffNoChargeContract)}`,
    );

    await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      shell.hass = window.__makeCanonicalHass(200);
    });
    await page.waitForFunction(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const ems = shell?.shadowRoot?.querySelector(
        "hoymiles-aurora-variant-a-ems-page-card",
      );
      const chart = ems?.shadowRoot?.querySelector("[data-chart]");
      const socPolicies = new Set(
        [...(chart?.querySelectorAll(".soc-action-segment") || [])].map(
          (node) => node.dataset.socPolicy,
        ),
      );
      const glassPolicies = new Set(
        [...(chart?.querySelectorAll(".soc-action-glass") || [])].map(
          (node) => node.dataset.socPolicy,
        ),
      );
      const gridFlows = new Set(
        [...(chart?.querySelectorAll(".grid-flow-bar") || [])].map(
          (node) => node.dataset.gridFlow,
        ),
      );
      return chart?.dataset.source === "current"
        && chart.querySelectorAll(".plan-band,[data-plans-track]").length === 0
        && ["rce", "tariff", "rcm"].every((policy) => socPolicies.has(policy))
        && ["rce", "tariff", "rcm"].every((policy) => glassPolicies.has(policy))
        && ["import", "export"].every((direction) => gridFlows.has(direction));
    });
    const canonicalGeometry = await page.evaluate(() => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const ems = shell.shadowRoot.querySelector(
        "hoymiles-aurora-variant-a-ems-page-card",
      );
      const chart = ems.shadowRoot.querySelector("[data-chart]");
      const svg = chart.querySelector("[data-soc-chart]");
      const expectedSoc = chart.querySelector(".soc-line");
      const authorizationSoc = chart.querySelector(".soc-authorization-line");
      const midnightBoundary = chart.querySelector("[data-midnight-boundary]");
      const midnightLine = midnightBoundary?.querySelector(".day-boundary-line");
      const midnightLabel = midnightBoundary?.querySelector(".day-boundary-label");
      const midnightTime = midnightBoundary?.querySelector(".day-boundary-time");
      const safeLegend = ems.shadowRoot.querySelector("[data-safe-soc-legend]");
      const inspector = chart.querySelector("[data-chart-inspector]");
      const svgRect = svg.getBoundingClientRect();
      const flowBars = [...chart.querySelectorAll(".grid-flow-bar")].map((node) => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return {
          direction: node.dataset.gridFlow,
          width: rect.width,
          height: rect.height,
          fill: style.fill.replace(/\s+/g, ""),
        };
      });
      const glasses = [...chart.querySelectorAll(".soc-action-glass")].map((node) => {
        const rect = node.getBoundingClientRect();
        return {
          policy: node.dataset.socPolicy,
          width: rect.width,
          height: rect.height,
          fill: getComputedStyle(node).fill,
        };
      });
      const segments = [...chart.querySelectorAll(".soc-action-segment")].map(
        (node) => {
          const rect = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          return {
            policy: node.dataset.socPolicy,
            action: node.dataset.socAction,
            width: rect.width,
            height: rect.height,
            strokeWidth: Number.parseFloat(style.strokeWidth) || 0,
            stroke: style.stroke.replace(/\s+/g, ""),
            vectorEffect: style.vectorEffect,
          };
        },
      );
      return {
        source: chart.dataset.source,
        svgHeight: svgRect.height,
        viewBoxHeight: svg.viewBox.baseVal.height,
        legacyPlanTrack: Boolean(chart.querySelector("[data-plans-track]")),
        legacyPlanBandCount: chart.querySelectorAll(".plan-band").length,
        flowBars,
        glasses,
        segments,
        expectedSocStroke: getComputedStyle(expectedSoc).stroke.replace(/\s+/g, ""),
        authorizationSocStroke: getComputedStyle(authorizationSoc).stroke.replace(/\s+/g, ""),
        authorizationSocDash: getComputedStyle(authorizationSoc).strokeDasharray,
        midnightBoundary: {
          exists: Boolean(midnightBoundary && midnightLine && midnightLabel && midnightTime),
          label: midnightLabel?.textContent,
          time: midnightTime?.textContent,
          x1: Number(midnightLine?.getAttribute("x1")),
          x2: Number(midnightLine?.getAttribute("x2")),
          y1: Number(midnightLine?.getAttribute("y1")),
          y2: Number(midnightLine?.getAttribute("y2")),
          stroke: midnightLine ? getComputedStyle(midnightLine).stroke.replace(/\s+/g, "") : "",
          strokeWidth: midnightLine ? Number.parseFloat(getComputedStyle(midnightLine).strokeWidth) : 0,
          filter: midnightLine ? getComputedStyle(midnightLine).filter : "",
          pointerEvents: midnightBoundary ? getComputedStyle(midnightBoundary).pointerEvents : "",
        },
        safeLegendVisible: safeLegend?.dataset.visible,
        safeLegendText: safeLegend?.textContent.replace(/\s+/g, " ").trim(),
        inspectorText: inspector?.textContent.replace(/\s+/g, " ").trim(),
        inspectorClientHeight: inspector?.clientHeight,
        inspectorScrollHeight: inspector?.scrollHeight,
      };
    });
    const glassPolicies = [...new Set(
      canonicalGeometry.glasses.map((glass) => glass.policy),
    )].sort().join("|");
    const flowDirections = [...new Set(
      canonicalGeometry.flowBars.map((bar) => bar.direction),
    )].sort().join("|");
    const segmentPolicies = [...new Set(
      canonicalGeometry.segments.map((segment) => segment.policy),
    )].sort().join("|");
    const expectedColors = {
      rce: "rgb(255,191,71)",
      tariff: "rgb(74,167,255)",
      rcm: "rgb(169,135,255)",
    };
    check(
      canonicalGeometry.source === "current",
      `Manual canonical plan was not selected: ${JSON.stringify(canonicalGeometry)}`,
    );
    check(
      !canonicalGeometry.legacyPlanTrack
      && canonicalGeometry.legacyPlanBandCount === 0,
      `The duplicated PLANY track returned: ${JSON.stringify(canonicalGeometry)}`,
    );
    check(
      flowDirections === "export|import"
      && canonicalGeometry.flowBars.some((bar) => bar.direction === "import" && bar.height > 0)
      && canonicalGeometry.flowBars.some((bar) => bar.direction === "export" && bar.height > 0)
      && canonicalGeometry.flowBars.every((bar) => bar.width > 0),
      `Grid import/export bars are missing or have invalid geometry: ${JSON.stringify(canonicalGeometry.flowBars)}`,
    );
    check(
      canonicalGeometry.glasses.length === canonicalGeometry.segments.length
      && glassPolicies === "rce|rcm|tariff"
      && canonicalGeometry.glasses.every((glass) =>
        glass.width > 0 && glass.height > 0 && glass.fill !== "none"
      ),
      `Canonical SOC action glass is missing or has invalid geometry: ${JSON.stringify(canonicalGeometry.glasses)}`,
    );
    check(
      canonicalGeometry.segments.length === 12
      && segmentPolicies === "rce|rcm|tariff"
      && canonicalGeometry.segments.every((segment) =>
        segment.action !== "none"
        && segment.width > 0
        && segment.strokeWidth >= 5
        && segment.vectorEffect === "non-scaling-stroke"
        && segment.stroke === expectedColors[segment.policy]
      ),
      `Canonical SOC action segments are missing, thin or use the wrong palette: ${JSON.stringify(canonicalGeometry.segments)}`,
    );
    check(
      canonicalGeometry.expectedSocStroke === "rgb(88,214,255)"
      && canonicalGeometry.authorizationSocStroke === "rgb(170,184,197)"
      && canonicalGeometry.authorizationSocDash !== "none"
      && canonicalGeometry.safeLegendVisible === "true"
      && canonicalGeometry.safeLegendText.includes("Bezpieczny plan automatyki"),
      `Expected and safe SOC trajectories or their legend are not visually distinct: ${JSON.stringify(canonicalGeometry)}`,
    );
    check(
      canonicalGeometry.midnightBoundary.exists
      && canonicalGeometry.midnightBoundary.label === "Dziś | Jutro"
      && canonicalGeometry.midnightBoundary.time === "00:00"
      && canonicalGeometry.midnightBoundary.x1 === canonicalGeometry.midnightBoundary.x2
      && canonicalGeometry.midnightBoundary.x1 > 44
      && canonicalGeometry.midnightBoundary.x1 < 1080
      && canonicalGeometry.midnightBoundary.y1 === 24
      && canonicalGeometry.midnightBoundary.y2 === canonicalGeometry.viewBoxHeight - 30
      && canonicalGeometry.midnightBoundary.stroke === "rgb(255,49,91)"
      && canonicalGeometry.midnightBoundary.strokeWidth >= 2
      && canonicalGeometry.midnightBoundary.filter !== "none"
      && canonicalGeometry.midnightBoundary.pointerEvents === "none",
      `Local-midnight separator is missing, misplaced, interactive or not red-neon: ${JSON.stringify(canonicalGeometry.midnightBoundary)}`,
    );
    check(
      canonicalGeometry.inspectorText.includes("SOC · Przewidywany")
      && canonicalGeometry.inspectorText.includes("Bezpieczny plan automatyki")
      && canonicalGeometry.inspectorText.includes("Źródło prognozy SOC")
      && canonicalGeometry.inspectorText.includes("P50 dostawcy · prognoza centralna")
      && canonicalGeometry.inspectorText.includes("PV potencjalne")
      && canonicalGeometry.inspectorText.includes("PV dostępne")
      && canonicalGeometry.inspectorText.includes("PV ograniczone")
      && canonicalGeometry.inspectorText.includes("Eksport oczekiwany")
      && canonicalGeometry.inspectorText.includes("Eksport bezpieczny")
      && canonicalGeometry.inspectorScrollHeight <= canonicalGeometry.inspectorClientHeight + 1,
      `Canonical persistent inspector omits the expected/safe physical contract or scrolls internally: ${JSON.stringify(canonicalGeometry)}`,
    );
    const expectedSourceTransition = await page.evaluate(async () => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const ems = shell.shadowRoot.querySelector(
        "hoymiles-aurora-variant-a-ems-page-card",
      );
      const scrollOwner = shell.shadowRoot.querySelector(".shell");
      const chartViewport = ems.shadowRoot.querySelector("[data-chart-viewport]");
      const selectedStart = ems._selectedChartStart;
      const beforeScrollTop = scrollOwner.scrollTop;
      chartViewport.scrollLeft = Math.min(510, chartViewport.scrollWidth - chartViewport.clientWidth);
      const beforeScrollLeft = chartViewport.scrollLeft;
      shell.hass = window.__makeCanonicalHass(201, "tariff_conservative_fallback");
      await new Promise((resolve) => {
        let frames = 6;
        const settle = () => {
          frames -= 1;
          if (frames > 0) requestAnimationFrame(settle);
          else resolve();
        };
        requestAnimationFrame(settle);
      });
      const deferredChart = ems.shadowRoot.querySelector("[data-chart]");
      const deferredViewport = deferredChart.querySelector("[data-chart-viewport]");
      const deferredInspector = deferredChart.querySelector("[data-chart-inspector]");
      const deferred = {
        source: deferredChart.dataset.source,
        selectedPreserved: ems._selectedChartStart === selectedStart,
        scrollDelta: scrollOwner.scrollTop - beforeScrollTop,
        chartScrollLeft: deferredViewport.scrollLeft,
        inspectorText: deferredInspector?.textContent.replace(/\s+/g, " ").trim(),
        inspectorAriaHidden: deferredInspector?.getAttribute("aria-hidden"),
        serviceCalls: window.__auroraServiceCalls.length,
      };
      shell.hass = window.__makeCanonicalHass(202, "tariff_conservative_fallback");
      await new Promise((resolve) => {
        let frames = 6;
        const settle = () => {
          frames -= 1;
          if (frames > 0) requestAnimationFrame(settle);
          else resolve();
        };
        requestAnimationFrame(settle);
      });
      const chart = ems.shadowRoot.querySelector("[data-chart]");
      const refreshedViewport = chart.querySelector("[data-chart-viewport]");
      const inspector = chart.querySelector("[data-chart-inspector]");
      return {
        deferred,
        source: chart.dataset.source,
        selectedPreserved: ems._selectedChartStart === selectedStart,
        scrollDelta: scrollOwner.scrollTop - beforeScrollTop,
        chartScrollLeftBefore: beforeScrollLeft,
        chartScrollLeftAfter: refreshedViewport.scrollLeft,
        chartScrollLeftDelta: refreshedViewport.scrollLeft - beforeScrollLeft,
        inspectorText: inspector?.textContent.replace(/\s+/g, " ").trim(),
        inspectorAriaHidden: inspector?.getAttribute("aria-hidden"),
        inspectorClientHeight: inspector?.clientHeight,
        inspectorScrollHeight: inspector?.scrollHeight,
        serviceCalls: window.__auroraServiceCalls.length,
      };
    });
    check(
      expectedSourceTransition.deferred.source === "current"
      && expectedSourceTransition.deferred.selectedPreserved
      && Math.abs(expectedSourceTransition.deferred.scrollDelta) <= 1
      && Math.abs(expectedSourceTransition.deferred.chartScrollLeft
        - expectedSourceTransition.chartScrollLeftBefore) <= 1
      && expectedSourceTransition.deferred.inspectorAriaHidden === "false"
      && expectedSourceTransition.deferred.inspectorText.includes("Taryfa · prognoza ostrożna")
      && !expectedSourceTransition.deferred.inspectorText.includes("P50 dostawcy · prognoza centralna")
      && expectedSourceTransition.deferred.serviceCalls === 0
      && expectedSourceTransition.source === "current"
      && expectedSourceTransition.selectedPreserved
      && Math.abs(expectedSourceTransition.scrollDelta) <= 1
      && expectedSourceTransition.chartScrollLeftBefore > 0
      && Math.abs(expectedSourceTransition.chartScrollLeftDelta) <= 1
      && expectedSourceTransition.inspectorAriaHidden === "false"
      && expectedSourceTransition.inspectorText.includes("Taryfa · prognoza ostrożna")
      && !expectedSourceTransition.inspectorText.includes("P50 dostawcy · prognoza centralna")
      && expectedSourceTransition.inspectorScrollHeight
        <= expectedSourceTransition.inspectorClientHeight + 1
      && expectedSourceTransition.serviceCalls === 0,
      `Expected-source cadence or refresh lost selection, shifted the mobile page, opened overflow or wrote to HA: ${JSON.stringify(expectedSourceTransition)}`,
    );
    const retainedTransition = await page.evaluate(async () => {
      const shell = document.querySelector("hoymiles-aurora-app-shell-card");
      const ems = shell.shadowRoot.querySelector(
        "hoymiles-aurora-variant-a-ems-page-card",
      );
      const root = ems.shadowRoot;
      const scrollOwner = shell.shadowRoot.querySelector(".shell");
      const selectors = [
        "[data-next-primary]",
        "[data-next-secondary]",
        '[data-total="tariff"]',
        '[data-total="battery"]',
        '[data-total="rce"]',
        '[data-day="rcm"] .day-plan-time',
        '[data-day="rcm"] small',
        '[data-day="rcm"] .day-plan-energy',
        '[data-day="tariff"] .day-plan-time',
        '[data-day="tariff"] small',
        '[data-day="tariff"] .day-plan-energy',
        '[data-day="rce"] .day-plan-time',
        '[data-day="rce"] small',
        '[data-day="rce"] .day-plan-energy',
      ];
      const nodes = selectors.map((selector) => root.querySelector(selector));
      const snapshot = () => nodes.map((node) => node?.textContent || "");
      const geometry = () => {
        const chartCard = root.querySelector(".chart-card").getBoundingClientRect();
        const chartStage = root.querySelector(".chart-stage").getBoundingClientRect();
        const status = root.querySelector("[data-chart-plan-status]").getBoundingClientRect();
        const subtitle = root.querySelector(".chart-subtitle-slot").getBoundingClientRect();
        return {
          cardHeight: chartCard.height,
          stageTop: chartStage.top,
          statusHeight: status.height,
          subtitleHeight: subtitle.height,
        };
      };
      const current = snapshot();
      const observer = new MutationObserver((records) => {
        window.__auroraRetainedTextMutations += records.length;
      });
      window.__auroraRetainedTextMutations = 0;
      nodes.forEach((node) => observer.observe(node, {
        childList: true,
        characterData: true,
        subtree: true,
      }));
      scrollOwner.scrollTop = Math.max(0, scrollOwner.scrollHeight - scrollOwner.clientHeight - 40);
      const beforeScrollTop = scrollOwner.scrollTop;
      const currentGeometry = geometry();
      const currentViewport = root.querySelector("[data-chart-viewport]");
      currentViewport.scrollLeft = Math.min(610, currentViewport.scrollWidth - currentViewport.clientWidth);
      const beforeScrollLeft = currentViewport.scrollLeft;
      const settle = () => new Promise((resolve) => {
        let frames = 6;
        const next = () => {
          frames -= 1;
          if (frames > 0) requestAnimationFrame(next);
          else resolve();
        };
        requestAnimationFrame(next);
      });

      ems._chartLastRenderAt = Date.now() - 3 * 60_000;
      shell.hass = window.__makePendingCanonicalHass(203, "tariff");
      await settle();
      const pending = snapshot();
      const pendingReadiness = root.querySelector("[data-readiness]");
      const pendingChart = root.querySelector("[data-chart]");
      const pendingState = pendingReadiness?.dataset.state;
      const pendingSource = pendingChart?.dataset.source;
      const pendingOverlay = Boolean(pendingChart?.querySelector(".retained-overlay"));
      const pendingScrollLeft = pendingChart?.querySelector("[data-chart-viewport]")?.scrollLeft;
      const pendingGeometry = geometry();
      const pendingSameNodes = selectors.every(
        (selector, index) => root.querySelector(selector) === nodes[index],
      );

      shell.hass = window.__makeCanonicalHass(204);
      await settle();
      const restored = snapshot();
      const restoredReadiness = root.querySelector("[data-readiness]");
      const restoredChart = root.querySelector("[data-chart]");
      const restoredScrollLeft = restoredChart?.querySelector("[data-chart-viewport]")?.scrollLeft;
      const restoredGeometry = geometry();
      const restoredSameNodes = selectors.every(
        (selector, index) => root.querySelector(selector) === nodes[index],
      );
      observer.disconnect();
      return {
        current,
        currentGeometry,
        pending,
        pendingGeometry,
        restored,
        restoredGeometry,
        pendingState,
        pendingSource,
        pendingOverlay,
        pendingSameNodes,
        restoredState: restoredReadiness?.dataset.state,
        restoredSource: restoredChart?.dataset.source,
        restoredSameNodes,
        mutations: window.__auroraRetainedTextMutations,
        scrollDelta: scrollOwner.scrollTop - beforeScrollTop,
        beforeScrollLeft,
        pendingScrollLeft,
        restoredScrollLeft,
      };
    });
    check(
      retainedTransition.current[0]
      && retainedTransition.current[0] !== "—"
      && retainedTransition.current.slice(2).every((value) => value && value !== "—")
      && JSON.stringify(retainedTransition.pending) === JSON.stringify(retainedTransition.current)
      && JSON.stringify(retainedTransition.restored) === JSON.stringify(retainedTransition.current)
      && retainedTransition.pendingState === "updating"
      && retainedTransition.pendingSource === "retained"
      && !retainedTransition.pendingOverlay
      && retainedTransition.pendingSameNodes
      && retainedTransition.restoredState === "ready"
      && retainedTransition.restoredSource === "current"
      && retainedTransition.restoredSameNodes
      && retainedTransition.mutations === 0
      && Math.abs(retainedTransition.scrollDelta) <= 1
      && retainedTransition.beforeScrollLeft > 0
      && Math.abs(retainedTransition.pendingScrollLeft - retainedTransition.beforeScrollLeft) <= 1
      && Math.abs(retainedTransition.restoredScrollLeft - retainedTransition.beforeScrollLeft) <= 1
      && Math.abs(retainedTransition.pendingGeometry.stageTop - retainedTransition.currentGeometry.stageTop) <= 1
      && Math.abs(retainedTransition.restoredGeometry.stageTop - retainedTransition.currentGeometry.stageTop) <= 1
      && Math.abs(retainedTransition.pendingGeometry.cardHeight - retainedTransition.currentGeometry.cardHeight) <= 1
      && Math.abs(retainedTransition.restoredGeometry.cardHeight - retainedTransition.currentGeometry.cardHeight) <= 1
      && retainedTransition.currentGeometry.statusHeight > 0
      && retainedTransition.pendingGeometry.statusHeight === retainedTransition.currentGeometry.statusHeight
      && retainedTransition.restoredGeometry.statusHeight === retainedTransition.currentGeometry.statusHeight
      && retainedTransition.pendingGeometry.subtitleHeight === retainedTransition.currentGeometry.subtitleHeight
      && retainedTransition.restoredGeometry.subtitleHeight === retainedTransition.currentGeometry.subtitleHeight,
      `Retained current-pending-current display flickered on mobile: ${JSON.stringify(retainedTransition)}`,
    );

    const responsiveGeometry = {};
    for (const width of [390, 768, 1374, 1440]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      responsiveGeometry[width] = await page.evaluate(async (refreshBase) => {
        const shell = document.querySelector("hoymiles-aurora-app-shell-card");
        const ems = shell.shadowRoot.querySelector("hoymiles-aurora-variant-a-ems-page-card");
        const root = ems.shadowRoot;
        const settle = () => new Promise((resolve) => {
          let frames = 6;
          const next = () => {
            frames -= 1;
            if (frames > 0) requestAnimationFrame(next);
            else resolve();
          };
          requestAnimationFrame(next);
        });
        const measure = () => {
          const card = root.querySelector(".chart-card").getBoundingClientRect();
          const stage = root.querySelector(".chart-stage").getBoundingClientRect();
          const status = root.querySelector("[data-chart-plan-status]").getBoundingClientRect();
          const subtitle = root.querySelector(".chart-subtitle-slot").getBoundingClientRect();
          return {
            cardHeight: card.height,
            stageTop: stage.top,
            statusHeight: status.height,
            subtitleHeight: subtitle.height,
            statusVisibility: getComputedStyle(root.querySelector("[data-chart-plan-status]")).visibility,
            subtitleText: root.querySelector("[data-plan-subtitle]").textContent,
            chartNodes: root.querySelectorAll("[data-chart-slot]").length,
          };
        };
        shell.hass = window.__makeCanonicalHass(refreshBase);
        await settle();
        const current = measure();
        shell.hass = window.__makePendingCanonicalHass(refreshBase + 1, "tariff");
        await settle();
        const pending = measure();
        shell.hass = window.__makeCanonicalHass(refreshBase + 2);
        await settle();
        const restored = measure();
        return { current, pending, restored };
      }, 3000 + width);
      const samples = responsiveGeometry[width];
      check(
        samples.current.chartNodes === 96
        && samples.pending.chartNodes === 96
        && samples.restored.chartNodes === 96
        && samples.current.statusVisibility === "hidden"
        && samples.pending.statusVisibility === "visible"
        && samples.restored.statusVisibility === "hidden"
        && samples.current.subtitleText !== samples.pending.subtitleText
        && samples.current.subtitleText === samples.restored.subtitleText
        && samples.current.statusHeight > 0
        && samples.current.statusHeight === samples.pending.statusHeight
        && samples.current.statusHeight === samples.restored.statusHeight
        && samples.current.subtitleHeight === samples.pending.subtitleHeight
        && samples.current.subtitleHeight === samples.restored.subtitleHeight
        && Math.abs(samples.pending.stageTop - samples.current.stageTop) <= 1
        && Math.abs(samples.restored.stageTop - samples.current.stageTop) <= 1
        && Math.abs(samples.pending.cardHeight - samples.current.cardHeight) <= 1
        && Math.abs(samples.restored.cardHeight - samples.current.cardHeight) <= 1,
        `Chart geometry shifted at ${width}px: ${JSON.stringify(samples)}`,
      );
    }
    await page.setViewportSize(VIEWPORT);
    check(
      await page.evaluate(() => window.__auroraServiceCalls.length) === 0,
      "Rendering the manual canonical plan attempted a Home Assistant service write",
    );

    fs.mkdirSync(path.dirname(SCREENSHOT_PATH), { recursive: true });
    await page.screenshot({ path: SCREENSHOT_PATH, fullPage: false });
    check(browserErrors.length === 0, `Browser errors: ${browserErrors.join(" | ")}`);
    console.log("PASS Aurora mobile EMS Playwright contract");
    console.log(`- viewport: ${VIEWPORT.width}x${VIEWPORT.height}`);
    console.log(`- browser: ${String(process.env.HOYMILES_UI_BROWSER || "chromium").toLowerCase()}`);
    console.log(`- mobile nav: ${navMetrics.labels.join(", ")} (active: ${navMetrics.active.join(", ")})`);
    console.log(`- refreshes: 50; scrollTop delta: ${refreshResult.delta}px; same SVG: ${refreshResult.sameSvg}`);
    console.log(`- immediate marker latch: slot ${firstImmediateSelection.selectedIndex} -> ${secondImmediateSelection.selectedIndex}; same SVG: ${secondImmediateSelection.sameSvg}`);
    console.log(`- slot inspector height delta: ${heightDelta(heightsBefore, heightsAfter)}px`);
    console.log(`- canonical grid flows: ${flowDirections}; action glass policies: ${glassPolicies}`);
    console.log(`- canonical SOC action stroke: ${Math.min(...canonicalGeometry.segments.map((segment) => segment.strokeWidth)).toFixed(2)}px; policies: ${segmentPolicies}`);
    console.log(`- chart geometry current→pending→current: ${Object.entries(responsiveGeometry).map(([width, sample]) => `${width}px ${sample.current.stageTop.toFixed(2)}→${sample.pending.stageTop.toFixed(2)}→${sample.restored.stageTop.toFixed(2)}`).join("; ")}`);
    console.log(`- screenshot: ${SCREENSHOT_PATH}`);
    await context.close();
  } finally {
    if (browser) await browser.close();
    await closeServer(server);
  }
}

main().catch((error) => {
  console.error(`FAIL Aurora mobile EMS Playwright contract: ${error.stack || error.message}`);
  process.exitCode = 1;
});
