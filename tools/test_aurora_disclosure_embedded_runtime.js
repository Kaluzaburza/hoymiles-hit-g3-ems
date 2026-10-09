"use strict";

/**
 * Real-browser contract for Aurora cards embedded in expandable sections.
 *
 * The harness loads the canonical frontend module from this checkout and
 * supplies small, deliberately "legacy-looking" Home Assistant card doubles.
 * It never connects to Home Assistant and rejects every attempted service call.
 */

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

let chromium;
try {
  ({ chromium } = require("playwright"));
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

function check(condition, message) {
  if (!condition) throw new Error(message);
}

const HARNESS_HTML = String.raw`<!doctype html>
<html lang="pl">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Aurora embedded disclosure runtime contract</title>
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
      html, body { background:#080d13; margin:0; min-height:100%; }
      body { font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
      #fixtures { display:grid; gap:16px; padding:16px; }
    </style>
    <script>
      window.customCards = [];
      window.__qaServiceCalls = [];

      class QaGenericEntityRow extends HTMLElement {
        constructor() {
          super();
          this.attachShadow({ mode: "open" });
          this.shadowRoot.innerHTML =
            '<div class="row"><state-badge></state-badge><div class="info">legacy label</div><slot></slot></div>';
        }
      }
      customElements.define("hui-generic-entity-row", QaGenericEntityRow);

      class QaEntityRow extends HTMLElement {
        constructor() {
          super();
          this.attachShadow({ mode: "open" });
          this.shadowRoot.innerHTML =
            '<hui-generic-entity-row><div class="flex"><span class="state">value</span></div></hui-generic-entity-row>';
        }
      }
      customElements.define("qa-entity-row", QaEntityRow);

      class QaEntitiesCard extends HTMLElement {
        constructor() {
          super();
          this.attachShadow({ mode: "open" });
          this._hass = null;
          this._config = null;
        }
        setConfig(config) {
          this._config = JSON.parse(JSON.stringify(config));
          const rows = (config.entities || []).map(
            (_entry, index) => '<div data-native-row="' + index + '"><qa-entity-row></qa-entity-row></div>'
          ).join("");
          this.shadowRoot.innerHTML =
            '<style>' +
              ':host{display:block}' +
              'ha-card{display:block;background:rgb(92,18,18);border:3px solid rgb(255,0,0);' +
              'border-radius:24px;box-shadow:rgb(255,0,0) 0 0 18px;padding:5px}' +
              '#states>div{display:flex;min-height:12px}' +
            '</style>' +
            '<ha-card data-legacy-surface><div class="card-header">Legacy entities</div>' +
            '<div id="states">' + rows + '</div></ha-card>';
        }
        set hass(hass) { this._hass = hass; }
        get hass() { return this._hass; }
        get updateComplete() { return Promise.resolve(); }
        getCardSize() { return (this._config?.entities || []).length || 1; }
      }
      customElements.define("hui-entities-card", QaEntitiesCard);

      class QaNativeCard extends HTMLElement {
        constructor() {
          super();
          this.attachShadow({ mode: "open" });
          this._hass = null;
        }
        setConfig(config) {
          this._config = JSON.parse(JSON.stringify(config));
          this.shadowRoot.innerHTML =
            '<style>' +
              ':host{display:block}' +
              'ha-card{display:block;background:rgb(92,18,18);border:3px solid rgb(255,0,0);' +
              'border-radius:24px;box-shadow:rgb(255,0,0) 0 0 18px;padding:10px}' +
              'button{min-height:12px}' +
            '</style>' +
            '<ha-card data-legacy-surface><div class="card-header">Legacy ' +
            String(config.type || "card") +
            '</div><div class="card-content"><button type="button">Native control</button></div></ha-card>';
        }
        set hass(hass) { this._hass = hass; }
        get hass() { return this._hass; }
        get updateComplete() { return Promise.resolve(); }
        getCardSize() { return 1; }
      }
      customElements.define("qa-native-card", QaNativeCard);

      class QaCollectionCard extends HTMLElement {
        constructor() {
          super();
          this.attachShadow({ mode: "open" });
          this._children = [];
        }
        setConfig(config) {
          this._config = JSON.parse(JSON.stringify(config));
          const configs = config.type === "conditional"
            ? (config.card ? [config.card] : [])
            : (Array.isArray(config.cards) ? config.cards : []);
          this._children = configs.map((childConfig) => window.__createQaCard(childConfig));
          this.shadowRoot.replaceChildren(...this._children);
        }
        set hass(hass) {
          this._hass = hass;
          for (const child of this._children) child.hass = hass;
        }
        get updateComplete() { return Promise.resolve(); }
      }
      customElements.define("qa-collection-card", QaCollectionCard);

      window.__createQaCard = function (config) {
        const type = String(config?.type || "");
        let element;
        if (["vertical-stack", "horizontal-stack", "grid", "conditional"].includes(type)) {
          element = document.createElement("qa-collection-card");
        } else if (type.startsWith("custom:")) {
          element = document.createElement(type.slice(7));
        } else {
          element = document.createElement("qa-native-card");
        }
        if (typeof element.setConfig !== "function") {
          throw new Error("Unknown QA nested card: " + type);
        }
        element.setConfig(config);
        return element;
      };

      window.loadCardHelpers = async function () {
        return { createCardElement: window.__createQaCard };
      };

      window.__qaState = function (entityId, state, attributes) {
        const stamp = new Date().toISOString();
        return {
          entity_id: entityId,
          state: String(state),
          attributes: attributes || {},
          last_changed: stamp,
          last_updated: stamp,
        };
      };

      window.__qaHass = function (safeOff) {
        const states = {};
        const put = function (state) { states[state.entity_id] = state; };
        put(window.__qaState(
          "sensor.hoymiles_hit_ems_supervisor",
          safeOff ? "off" : "running",
          safeOff ? {
            execution_phase: "idle",
            owner: "none",
            observed_owner: "none",
            transaction_owner: "none",
            transaction_id: null,
            owner_conflict: false,
            supervisor_execution_authorized: false,
            master_stop_adapter_latched: false,
            manual_proxy_readback_pending: false,
          } : {
            execution_phase: "executing",
            owner: "tariff",
            observed_owner: "tariff",
            transaction_owner: "tariff",
            transaction_id: "qa-locked",
            owner_conflict: false,
            supervisor_execution_authorized: true,
            master_stop_adapter_latched: false,
            manual_proxy_readback_pending: false,
          }
        ));
        put(window.__qaState(
          "input_select.hoymiles_ems_supervisor_mode",
          safeOff ? "Off" : "Active",
          { options: ["Off", "Active"] }
        ));
        put(window.__qaState("binary_sensor.hoymiles_ems_control_conflict", "off"));
        put(window.__qaState("binary_sensor.hoymiles_ems_execution_ready", safeOff ? "off" : "on"));
        put(window.__qaState("binary_sensor.hoymiles_ems_export_allowed", "on"));
        put(window.__qaState("binary_sensor.hoymiles_rce_control_data_ready", "off"));
        put(window.__qaState(
          "sensor.hoymiles_hit_rce_optimized_plan",
          "Za mało energii na potrzeby domu — sprzedaż zablokowana",
          {
            status_code: "home_energy_shortage",
            result_current: true,
            recalculation_pending: false,
            input_revision: 41,
            gcf_execution_data_fresh: true,
            gcf_enabled: true,
            gcf_export_limit_percent: 0,
            planned_export_kwh: 0,
            planned_slots: [],
          }
        ));
        put(window.__qaState(
          "sensor.hoymiles_hit_rce_automation_plan_timeline",
          "current",
          {
            schema_version: 2,
            policy_id: "rce",
            generated_at: new Date().toISOString(),
            input_revision: 41,
            plan_revision: 7,
            plan_revision_scope: "runtime",
            result_current: true,
            recalculation_pending: false,
            plan_entity_id: "sensor.hoymiles_hit_rce_optimized_plan",
            point_count: 1,
            points: [{
              selected: false,
              action_code: "idle",
              policy: { planned_export_kwh: 0 },
            }],
          }
        ));
        put(window.__qaState("select.qa_manual_mode", "Self-Use", { options: ["Self-Use", "Off-Grid"] }));
        for (let index = 1; index <= 5; index += 1) {
          put(window.__qaState("number.qa_manual_" + index, String(index), {
            min: 0,
            max: 100,
            step: 1,
            unit_of_measurement: "%",
          }));
        }
        return {
          states: states,
          language: "pl",
          locale: { language: "pl", number_format: "comma_decimal" },
          config: { time_zone: "Europe/Warsaw" },
          callService: async function (domain, service, data) {
            window.__qaServiceCalls.push({ domain: domain, service: service, data: data });
            throw new Error("Read-only embedded-disclosure QA attempted a service call");
          },
        };
      };
    </script>
    <script type="module" src="/hoymiles-rce-chart-card.js"></script>
  </head>
  <body><main id="fixtures"></main></body>
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

async function launchChrome() {
  const options = { headless: true };
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
    browser = await launchChrome();
    const context = await browser.newContext({
      viewport: { width: 1280, height: 900 },
      colorScheme: "dark",
      reducedMotion: "reduce",
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => browserErrors.push(`pageerror: ${error.message}`));
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
    });
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => [
      "hoymiles-aurora-disclosure-card",
      "hoymiles-aurora-compact-page-card",
      "hoymiles-aurora-variant-a-policy-settings-card",
      "hoymiles-aurora-variant-a-service-page-card",
      "hoymiles-zebra-entities-card",
    ].every((tag) => Boolean(customElements.get(tag))));

    const result = await page.evaluate(async () => {
      const fixture = document.querySelector("#fixtures");
      const lockedHass = window.__qaHass(false);
      const readyHass = window.__qaHass(true);
      const cases = {};

      const pause = (milliseconds = 0) => new Promise(
        (resolve) => setTimeout(resolve, milliseconds)
      );
      async function eventually(predicate, label) {
        const deadline = performance.now() + 3000;
        while (performance.now() < deadline) {
          const value = predicate();
          if (value) return value;
          await pause(10);
        }
        throw new Error("Timed out waiting for " + label);
      }
      function mount(tag, config, hass, caseName) {
        const element = document.createElement(tag);
        element.dataset.qaCase = caseName;
        element.setConfig(config);
        element.hass = hass;
        fixture.append(element);
        return element;
      }
      function policyNested(section) {
        if (section === "rce") {
          return { type: "markdown", content: "RCE today and tomorrow" };
        }
        if (section === "tariff") {
          return {
            type: "vertical-stack",
            cards: [
              { type: "statistics-graph", entities: ["sensor.qa_tariff_price"] },
              {
                type: "custom:hoymiles-zebra-entities-card",
                entities: ["sensor.qa_tariff_window"],
              },
            ],
          };
        }
        return {
          type: "conditional",
          conditions: [],
          card: { type: "button", entity: "sensor.qa_voltage" },
        };
      }

      for (const section of ["rce", "tariff", "voltage"]) {
        const host = mount(
          "hoymiles-aurora-variant-a-policy-settings-card",
          {
            section: section,
            language: "pl",
            detail_cards: [{
              type: "custom:hoymiles-aurora-disclosure-card",
              title: "QA " + section,
              accent: section === "tariff" ? "battery" : "grid",
              card: policyNested(section),
            }],
          },
          lockedHass,
          section,
        );
        cases[section] = { host: host };
      }

      const rceReadiness = await eventually(() => {
        const pill = cases.rce.host.shadowRoot?.querySelector("[data-readiness]");
        const blocker = cases.rce.host.shadowRoot?.querySelector("[data-rce-zero-export]");
        return pill?.textContent === "Plan gotowy" && blocker && !blocker.hidden
          ? {
              pill: pill.textContent,
              tone: pill.dataset.ready,
              blocker: blocker.textContent.trim(),
              blockerHidden: blocker.hidden,
            }
          : null;
      }, "verified RCE zero-export readiness");
      const rceRejected = [];
      const rcePlan = lockedHass.states["sensor.hoymiles_hit_rce_optimized_plan"];
      const rceTimeline = lockedHass.states["sensor.hoymiles_hit_rce_automation_plan_timeline"];
      const controlConflict = lockedHass.states["binary_sensor.hoymiles_ems_control_conflict"];
      const freshPlanUpdated = rcePlan.last_updated;
      const freshTimelineGenerated = rceTimeline.attributes.generated_at;
      const rejectionCases = [
        ["revision", () => { rceTimeline.attributes.input_revision = 42; }, () => { rceTimeline.attributes.input_revision = 41; }],
        ["freshness", () => { rcePlan.attributes.gcf_execution_data_fresh = false; }, () => { rcePlan.attributes.gcf_execution_data_fresh = true; }],
        ["stale plan", () => { rcePlan.last_updated = new Date(Date.now() - 300_001).toISOString(); }, () => { rcePlan.last_updated = freshPlanUpdated; }],
        ["future timeline", () => { rceTimeline.attributes.generated_at = new Date(Date.now() + 5_001).toISOString(); }, () => { rceTimeline.attributes.generated_at = freshTimelineGenerated; }],
        ["conflict", () => { controlConflict.state = "on"; }, () => { controlConflict.state = "off"; }],
      ];
      for (const [name, reject, restore] of rejectionCases) {
        reject();
        cases.rce.host.hass = lockedHass;
        await pause();
        const pill = cases.rce.host.shadowRoot.querySelector("[data-readiness]");
        const blocker = cases.rce.host.shadowRoot.querySelector("[data-rce-zero-export]");
        rceRejected.push({
          name: name,
          pill: pill.textContent,
          tone: pill.dataset.ready,
          blockerHidden: blocker.hidden,
        });
        restore();
        cases.rce.host.hass = lockedHass;
        await pause();
      }

      const pendingHass = window.__qaHass(false);
      const pendingPlan = pendingHass.states["sensor.hoymiles_hit_rce_optimized_plan"];
      const pendingTimeline = pendingHass.states["sensor.hoymiles_hit_rce_automation_plan_timeline"];
      const pendingExecutionReady = pendingHass.states["binary_sensor.hoymiles_ems_execution_ready"];
      const pendingExportAllowed = pendingHass.states["binary_sensor.hoymiles_ems_export_allowed"];
      const pendingRceReady = pendingHass.states["binary_sensor.hoymiles_rce_control_data_ready"];
      const pendingConflict = pendingHass.states["binary_sensor.hoymiles_ems_control_conflict"];
      Object.assign(pendingPlan.attributes, {
        result_current: false,
        recalculation_pending: true,
      });
      pendingTimeline.state = "pending";
      Object.assign(pendingTimeline.attributes, {
        result_current: false,
        recalculation_pending: true,
        quality: "pending",
        blocker_code: "recalculation_pending",
        pending_input_revision: 42,
      });
      const pendingHost = mount(
        "hoymiles-aurora-variant-a-policy-settings-card",
        { section: "rce", language: "pl", detail_cards: [] },
        pendingHass,
        "rce-pending",
      );
      await pause();
      const pendingSnapshot = () => {
        const pill = pendingHost.shadowRoot.querySelector("[data-readiness]");
        const blocker = pendingHost.shadowRoot.querySelector("[data-rce-zero-export]");
        return {
          pill: pill.textContent,
          tone: pill.dataset.ready,
          blockerHidden: blocker.hidden,
          policyReady: pendingRceReady.state,
        };
      };
      const rcePendingLifecycle = { updating: pendingSnapshot() };
      const pendingRejected = [];
      const capturePendingRejection = async (name, reject, restore) => {
        reject();
        pendingHost.hass = pendingHass;
        await pause();
        pendingRejected.push({ name: name, ...pendingSnapshot() });
        restore();
        pendingHost.hass = pendingHass;
        await pause();
      };
      const pendingGeneratedAt = pendingTimeline.attributes.generated_at;
      await capturePendingRejection(
        "execution lost",
        () => { pendingExecutionReady.state = "off"; },
        () => { pendingExecutionReady.state = "on"; },
      );
      await capturePendingRejection(
        "export blocked",
        () => { pendingExportAllowed.state = "off"; },
        () => { pendingExportAllowed.state = "on"; },
      );
      await capturePendingRejection(
        "invalid blocker",
        () => { pendingTimeline.attributes.blocker_code = "missing_data"; },
        () => { pendingTimeline.attributes.blocker_code = "recalculation_pending"; },
      );
      await capturePendingRejection(
        "conflict",
        () => { pendingConflict.state = "on"; },
        () => { pendingConflict.state = "off"; },
      );
      await capturePendingRejection(
        "retained revision mismatch",
        () => { pendingTimeline.attributes.input_revision = 40; },
        () => { pendingTimeline.attributes.input_revision = 41; },
      );
      await capturePendingRejection(
        "pending revision not newer",
        () => { pendingTimeline.attributes.pending_input_revision = 41; },
        () => { pendingTimeline.attributes.pending_input_revision = 42; },
      );
      await capturePendingRejection(
        "future timeline",
        () => { pendingTimeline.attributes.generated_at = new Date(Date.now() + 5_001).toISOString(); },
        () => { pendingTimeline.attributes.generated_at = pendingGeneratedAt; },
      );
      await capturePendingRejection(
        "point count mismatch",
        () => { pendingTimeline.attributes.point_count = 2; },
        () => { pendingTimeline.attributes.point_count = 1; },
      );

      cases.pv = {
        host: mount(
          "hoymiles-aurora-compact-page-card",
          {
            page: "pv",
            language: "pl",
            history_entities: [],
            strings: [],
            groups: [],
            detail_cards: [{
              type: "custom:hoymiles-aurora-disclosure-card",
              title: "QA PV",
              accent: "pv",
              card: { type: "statistics-graph", entities: ["sensor.qa_pv"] },
            }],
          },
          lockedHass,
          "pv",
        ),
      };

      cases.energy = {
        host: mount(
          "hoymiles-aurora-compact-page-card",
          {
            page: "energy",
            language: "pl",
            history_entities: [],
            groups: [],
            detail_cards: [{
              type: "custom:hoymiles-aurora-disclosure-card",
              title: "QA energy",
              accent: "grid",
              card: {
                type: "horizontal-stack",
                cards: [
                  { type: "glance", entities: ["sensor.qa_energy"] },
                  { type: "statistic", entity: "sensor.qa_energy" },
                ],
              },
            }],
          },
          lockedHass,
          "energy",
        ),
      };

      const manualEntities = [
        {
          entity: "select.qa_manual_mode",
          name: "Tryb falownika",
          description: "Wybierz fizyczny tryb po bezpiecznym przekazaniu sterowania.",
        },
        ...[1, 2, 3, 4, 5].map((index) => ({
          entity: "number.qa_manual_" + index,
          name: "Parametr " + index,
          description: "Krótki opis parametru " + index + ".",
        })),
      ];
      const manualConfig = {
        type: "custom:hoymiles-zebra-entities-card",
        title: "Ręczne sterowanie EMS",
        language: "pl",
        accent: "ems",
        aurora_compact_manual: true,
        safe_off_only: true,
        safe_off_banner: true,
        safe_off_off_grid_option: true,
        supervisor_entity: "sensor.hoymiles_hit_ems_supervisor",
        supervisor_mode_entity: "input_select.hoymiles_ems_supervisor_mode",
        control_conflict_entity: "binary_sensor.hoymiles_ems_control_conflict",
        entities: manualEntities,
      };
      cases.service = {
        host: mount(
          "hoymiles-aurora-variant-a-service-page-card",
          {
            language: "pl",
            quick_links: [
              {
                name: "Plan EMS",
                icon: "mdi:shield-check",
                navigation_path: "/hoymiles-falownik/plan-automatyki",
              },
              {
                name: "RCE",
                icon: "mdi:transmission-tower-export",
                navigation_path: "/hoymiles-falownik/automatyka-ems",
              },
              {
                name: "Sterowanie",
                icon: "mdi:tune-vertical",
                navigation_path: "/hoymiles-falownik/sterowanie",
              },
            ],
            detail_cards: [
              {
                type: "custom:hoymiles-aurora-disclosure-card",
                title: "QA service details",
                accent: "neutral",
                card: { type: "markdown", content: "Service details" },
              },
              manualConfig,
            ],
          },
          lockedHass,
          "service",
        ),
      };

      const embeddedEntityIds = [
        "sensor.qa_embedded_1",
        "sensor.qa_embedded_2",
        "sensor.qa_embedded_3",
        "sensor.qa_embedded_4",
      ];
      const embeddedZebra = mount(
        "hoymiles-zebra-entities-card",
        {
          title: "QA embedded Zebra",
          aurora_embedded: true,
          entities: embeddedEntityIds.map((entity, index) => ({
            entity: entity,
            name: "Pomiar " + (index + 1),
          })),
        },
        lockedHass,
        "embedded-zebra",
      );

      for (const section of ["rce", "tariff", "voltage"]) {
        const details = await eventually(
          () => cases[section].host.shadowRoot?.querySelector("details.advanced"),
          section + " native advanced disclosure",
        );
        details.open = true;
        details.dispatchEvent(new Event("toggle"));
      }

      for (const [name, item] of Object.entries(cases)) {
        item.disclosure = await eventually(
          () => item.host.shadowRoot?.querySelector("hoymiles-aurora-disclosure-card"),
          name + " Aurora disclosure",
        );
        await eventually(
          () => item.disclosure.shadowRoot?.querySelector(".disclosure-button"),
          name + " disclosure button",
        );
        item.disclosure.shadowRoot.querySelector(".disclosure-button").click();
      }

      const deepElements = (start) => {
        const output = [];
        const walk = (node) => {
          if (node instanceof Element) {
            output.push(node);
            if (node.shadowRoot) walk(node.shadowRoot);
          }
          for (const child of node.children || []) walk(child);
        };
        walk(start);
        return output;
      };

      const quickLinks = await eventually(
        () => cases.service.host.shadowRoot?.querySelector(".quick-links"),
        "service Aurora quick-links grid",
      );
      const quickAnchors = [...quickLinks.querySelectorAll("a")];
      const nativeQuickLinkButtons = deepElements(cases.service.host).filter(
        (node) => node._config?.type === "button",
      );
      const quickLinkResult = {
        display: getComputedStyle(quickLinks).display,
        count: quickAnchors.length,
        paths: quickAnchors.map((link) => link.getAttribute("href")),
        nativeButtonCards: nativeQuickLinkButtons.length,
        minHeights: quickAnchors.map((link) => parseFloat(getComputedStyle(link).minHeight)),
        fitsWidth: quickLinks.scrollWidth <= quickLinks.clientWidth + 1,
      };

      await eventually(
        () => embeddedZebra._card?.shadowRoot?.querySelectorAll("#states > div").length === 4,
        "four non-manual embedded Zebra rows",
      );
      await pause();
      const embeddedNative = embeddedZebra._card;
      const embeddedStates = embeddedNative.shadowRoot.querySelector("#states");
      const embeddedRows = [...embeddedStates.querySelectorAll(":scope > div")];
      const embeddedBadge = embeddedRows[0]
        ?.querySelector("qa-entity-row")
        ?.shadowRoot?.querySelector("hui-generic-entity-row")
        ?.shadowRoot?.querySelector("state-badge");
      const embeddedDesktop = {
        wrapperEmbedded: embeddedZebra.hasAttribute("data-hoymiles-aurora-embedded"),
        nativeEmbedded: embeddedNative.hasAttribute("data-hoymiles-aurora-embedded"),
        stateDisplay: getComputedStyle(embeddedStates).display,
        firstPairSameRow: Math.abs(embeddedRows[0].getBoundingClientRect().top - embeddedRows[1].getBoundingClientRect().top) < 1,
        firstPairSeparateColumns: Math.abs(embeddedRows[0].getBoundingClientRect().left - embeddedRows[1].getBoundingClientRect().left) > 1,
        badgeDisplay: embeddedBadge ? getComputedStyle(embeddedBadge).display : "missing",
        entityIds: (embeddedNative._config?.entities || []).map((entry) => (
          typeof entry === "string" ? entry : entry?.entity
        )),
      };

      const sectionResults = [];
      for (const [name, item] of Object.entries(cases)) {
        const disclosure = item.disclosure;
        await eventually(
          () => deepElements(disclosure).some((node) => node.matches?.("ha-card[data-legacy-surface]")),
          name + " embedded native surface",
        );
        const button = disclosure.shadowRoot.querySelector(".disclosure-button");
        const body = disclosure.shadowRoot.querySelector(".disclosure-body");
        const disclosureCard = disclosure.shadowRoot.querySelector("ha-card");
        const disclosureStyle = getComputedStyle(disclosureCard);
        const childBefore = disclosure._card;
        const surfaces = deepElements(disclosure).filter(
          (node) => node.matches?.("ha-card[data-legacy-surface]")
        );
        const surfaceResults = surfaces.map((surface) => {
          const style = getComputedStyle(surface);
          const host = surface.getRootNode()?.host;
          return {
            background: style.backgroundColor,
            borderTopWidth: style.borderTopWidth,
            boxShadow: style.boxShadow,
            marker: Boolean(
              host?.shadowRoot?.querySelector("style[data-hoymiles-aurora-embedded-style]")
              || (
                host?.hasAttribute?.("data-hoymiles-aurora-embedded")
                && host.shadowRoot?.querySelector("style[data-hoymiles-zebra-rows]")
              )
            ),
          };
        });
        disclosure.setConfig(JSON.parse(JSON.stringify(disclosure._config)));
        await pause();
        sectionResults.push({
          name: name,
          expanded: disclosure._expanded,
          ariaExpanded: button.getAttribute("aria-expanded"),
          bodyVisible: !body.hidden,
          sameChild: disclosure._card === childBefore,
          disclosureMinHeight: parseFloat(getComputedStyle(button).minHeight),
          disclosureBackground: disclosureStyle.backgroundImage,
          disclosureBorder: disclosureStyle.borderColor,
          surfaces: surfaceResults,
        });
      }

      const manual = await eventually(
        () => cases.service.host.shadowRoot?.querySelector("hoymiles-zebra-entities-card"),
        "service compact manual card",
      );
      await eventually(
        () => manual._card?.shadowRoot?.querySelectorAll("#states > div").length === 6,
        "six manual-control rows",
      );
      await pause();
      const nativeManualCard = manual._card;
      const nativeRoot = nativeManualCard.shadowRoot;
      const rowsLocked = [...nativeRoot.querySelectorAll("#states > div")];
      const locked = {
        compactAttribute: manual.hasAttribute("data-hoymiles-compact-manual"),
        embeddedAttribute: manual.hasAttribute("data-hoymiles-aurora-embedded"),
        nativeCompactAttribute: nativeManualCard.hasAttribute("data-hoymiles-compact-manual"),
        nativeEmbeddedAttribute: nativeManualCard.hasAttribute("data-hoymiles-aurora-embedded"),
        rowCount: rowsLocked.length,
        allInert: rowsLocked.every((row) => row.inert === true),
        allAriaDisabled: rowsLocked.every((row) => row.getAttribute("aria-disabled") === "true"),
        footerReady: nativeRoot.querySelector("[data-hoymiles-safe-off-footer]")?.dataset.ready,
        labelCount: nativeRoot.querySelectorAll("[data-hoymiles-compact-label]").length,
        firstRowDisplay: getComputedStyle(rowsLocked[0]).display,
        firstRowMinHeight: parseFloat(getComputedStyle(rowsLocked[0]).minHeight),
      };

      manual.hass = readyHass;
      await pause();
      await pause();
      const rowsReady = [...nativeRoot.querySelectorAll("#states > div")];
      const ready = {
        sameNativeCard: manual._card === nativeManualCard,
        allInteractive: rowsReady.every((row) => row.inert === false),
        allAriaEnabled: rowsReady.every((row) => row.getAttribute("aria-disabled") === "false"),
        footerReady: nativeRoot.querySelector("[data-hoymiles-safe-off-footer]")?.dataset.ready,
        firstRowDisplay: getComputedStyle(rowsReady[0]).display,
        firstRowMinHeight: parseFloat(getComputedStyle(rowsReady[0]).minHeight),
      };

      return {
        sectionResults: sectionResults,
        manual: { locked: locked, ready: ready },
        quickLinks: quickLinkResult,
        embeddedDesktop: embeddedDesktop,
        rceReadiness: rceReadiness,
        rceRejected: rceRejected,
        rcePendingLifecycle: rcePendingLifecycle,
        rcePendingRejected: pendingRejected,
        serviceCalls: window.__qaServiceCalls.length,
      };
    });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(50);
    const mobile = await page.evaluate(() => {
      const embedded = document.querySelector('[data-qa-case="embedded-zebra"]');
      const native = embedded?._card;
      const states = native?.shadowRoot?.querySelector("#states");
      const rows = [...(states?.querySelectorAll(":scope > div") || [])];
      const manual = document.querySelector('[data-qa-case="service"]')
        ?.shadowRoot?.querySelector("hoymiles-zebra-entities-card");
      const manualRow = manual?._card?.shadowRoot?.querySelector("#states > div");
      const quickLinks = document.querySelector('[data-qa-case="service"]')
        ?.shadowRoot?.querySelector(".quick-links");
      return {
        stateDisplay: states ? getComputedStyle(states).display : "missing",
        firstPairStacked: rows.length >= 2
          && rows[1].getBoundingClientRect().top > rows[0].getBoundingClientRect().bottom - 1,
        rowsFitWidth: rows.every((row) => row.scrollWidth <= row.clientWidth + 1),
        manualDisplay: manualRow ? getComputedStyle(manualRow).display : "missing",
        manualStillInteractive: manualRow?.getAttribute("aria-disabled") === "false",
        quickLinksFitWidth: quickLinks
          ? quickLinks.scrollWidth <= quickLinks.clientWidth + 1
          : false,
      };
    });

    check(browserErrors.length === 0, `Browser emitted errors:\n${browserErrors.join("\n")}`);
    check(
      result.rceReadiness.pill === "Plan gotowy"
        && result.rceReadiness.tone === "plan"
        && result.rceReadiness.blocker === "Sprzedaż zablokowana — 0 export aktywny"
        && result.rceReadiness.blockerHidden === false,
      `RCE zero-export presentation is incomplete: ${JSON.stringify(result.rceReadiness)}`,
    );
    check(
      result.rceRejected.every(
        (item) => item.pill === "Brak gotowości"
          && item.tone === "false"
          && item.blockerHidden === true,
      ),
      `RCE zero-export presentation did not fail closed: ${JSON.stringify(result.rceRejected)}`,
    );
    check(
      result.rcePendingLifecycle.updating.pill === "Plan gotowy · aktualizacja"
        && result.rcePendingLifecycle.updating.tone === "updating"
        && result.rcePendingLifecycle.updating.blockerHidden === true
        && result.rcePendingLifecycle.updating.policyReady === "off",
      `RCE retained-plan recalculation is not presented as updating: ${JSON.stringify(result.rcePendingLifecycle)}`,
    );
    check(
      result.rcePendingRejected.every(
        (item) => item.pill === "Brak gotowości"
          && item.tone === "false"
          && item.blockerHidden === true
          && item.policyReady === "off",
      ),
      `RCE retained-plan recalculation did not fail closed outside the strict pending lifecycle: ${JSON.stringify(result.rcePendingRejected)}`,
    );
    check(result.sectionResults.length === 6, `Expected six expanded sections, got ${result.sectionResults.length}`);
    for (const section of result.sectionResults) {
      check(section.expanded, `${section.name}: identical setConfig collapsed the disclosure`);
      check(section.ariaExpanded === "true", `${section.name}: aria-expanded was not preserved`);
      check(section.bodyVisible, `${section.name}: expanded body was hidden after identical setConfig`);
      check(section.sameChild, `${section.name}: identical setConfig recreated the nested card`);
      check(
        Number.isFinite(section.disclosureMinHeight) && section.disclosureMinHeight >= 44,
        `${section.name}: disclosure target is ${section.disclosureMinHeight}px instead of at least 44px`,
      );
      check(section.surfaces.length > 0, `${section.name}: no embedded native surface was exercised`);
      for (const [index, surface] of section.surfaces.entries()) {
        check(
          surface.background === "rgba(0, 0, 0, 0)",
          `${section.name}[${index}]: legacy background survived (${surface.background})`,
        );
        check(
          surface.borderTopWidth === "0px",
          `${section.name}[${index}]: legacy border survived (${surface.borderTopWidth})`,
        );
        check(
          surface.boxShadow === "none",
          `${section.name}[${index}]: legacy shadow survived (${surface.boxShadow})`,
        );
        check(surface.marker, `${section.name}[${index}]: embedded presentation marker is missing`);
      }
    }
    check(
      new Set(result.sectionResults.map((section) => section.disclosureBackground)).size === 1,
      "Shared disclosures do not use one uniform Aurora-blue surface",
    );
    check(
      result.sectionResults.every(
        (section) => section.disclosureBackground.includes("88, 214, 255")
          && section.disclosureBorder === "rgba(88, 214, 255, 0.22)"
      ),
      `Shared disclosure shell lost its blue Aurora background or border: ${JSON.stringify(
        result.sectionResults.map((section) => ({
          name: section.name,
          background: section.disclosureBackground,
          border: section.disclosureBorder,
        }))
      )}`,
    );

    const { locked, ready } = result.manual;
    check(locked.compactAttribute, "Service manual card lost aurora_compact_manual on its wrapper");
    check(locked.embeddedAttribute, "Service decorator did not retain the aurora_embedded wrapper marker");
    check(locked.nativeCompactAttribute, "Native manual card lost compact-manual mode");
    check(locked.nativeEmbeddedAttribute, "Native manual card lost embedded mode");
    check(locked.rowCount === 6, `Compact manual card rendered ${locked.rowCount} rows instead of six`);
    check(locked.labelCount === 6, `Compact manual card rendered ${locked.labelCount} labels instead of six`);
    check(locked.allInert, "Exact safe-off gate did not lock every manual row");
    check(locked.allAriaDisabled, "Locked manual rows are not exposed as aria-disabled=true");
    check(locked.footerReady === "false", `Locked safe-off footer reports ${locked.footerReady}`);
    check(locked.firstRowDisplay === "grid", `Compact manual row display is ${locked.firstRowDisplay}`);
    check(locked.firstRowMinHeight >= 58, `Compact manual row shrank to ${locked.firstRowMinHeight}px`);
    check(ready.sameNativeCard, "A hass-only safe-off transition recreated the native manual card");
    check(ready.allInteractive, "Exact safe-off handoff did not unlock every manual row");
    check(ready.allAriaEnabled, "Unlocked manual rows are not exposed as aria-disabled=false");
    check(ready.footerReady === "true", `Ready safe-off footer reports ${ready.footerReady}`);
    check(ready.firstRowDisplay === "grid", "Embedded mode overrode compact-manual grid layout");
    check(ready.firstRowMinHeight >= 58, `Ready compact manual row shrank to ${ready.firstRowMinHeight}px`);

    check(result.quickLinks.display === "grid", `Service quick links use ${result.quickLinks.display} instead of a grid`);
    check(result.quickLinks.count === 3, `Service rendered ${result.quickLinks.count} quick links instead of three`);
    check(
      JSON.stringify(result.quickLinks.paths) === JSON.stringify([
        "/hoymiles-falownik/plan-automatyki",
        "/hoymiles-falownik/automatyka-ems",
        "/hoymiles-falownik/sterowanie",
      ]),
      `Service quick links changed navigation_path: ${JSON.stringify(result.quickLinks.paths)}`,
    );
    check(
      result.quickLinks.nativeButtonCards === 0,
      `Service quick links mounted ${result.quickLinks.nativeButtonCards} native button-card(s)`,
    );
    check(
      result.quickLinks.minHeights.every((height) => Number.isFinite(height) && height >= 44),
      `Service quick-link targets are below 44px: ${result.quickLinks.minHeights.join(", ")}`,
    );
    check(result.quickLinks.fitsWidth, "Service quick-links grid overflows on desktop");

    check(result.embeddedDesktop.wrapperEmbedded, "Non-manual Zebra lost its wrapper embedded marker");
    check(result.embeddedDesktop.nativeEmbedded, "Non-manual Zebra lost its native embedded marker");
    check(result.embeddedDesktop.stateDisplay === "grid", `Embedded Zebra #states uses ${result.embeddedDesktop.stateDisplay} instead of grid`);
    check(result.embeddedDesktop.firstPairSameRow, "Embedded Zebra does not render two columns on desktop");
    check(result.embeddedDesktop.firstPairSeparateColumns, "Embedded Zebra desktop rows overlap instead of using separate columns");
    check(result.embeddedDesktop.badgeDisplay === "none", `Embedded Zebra state-badge display is ${result.embeddedDesktop.badgeDisplay}`);
    check(
      JSON.stringify(result.embeddedDesktop.entityIds) === JSON.stringify([
        "sensor.qa_embedded_1",
        "sensor.qa_embedded_2",
        "sensor.qa_embedded_3",
        "sensor.qa_embedded_4",
      ]),
      `Embedded Zebra changed entity_id values: ${JSON.stringify(result.embeddedDesktop.entityIds)}`,
    );
    check(mobile.stateDisplay === "grid", `Mobile embedded Zebra #states uses ${mobile.stateDisplay}`);
    check(mobile.firstPairStacked, "Embedded Zebra did not collapse to one column on mobile");
    check(mobile.rowsFitWidth, "Embedded Zebra rows overflow on mobile");
    check(mobile.manualDisplay === "grid", "Mobile breakpoint overrode compact-manual row layout");
    check(mobile.manualStillInteractive, "Viewport change altered the ready safe-off state");
    check(mobile.quickLinksFitWidth, "Service quick-links grid overflows on mobile");
    check(result.serviceCalls === 0, `Runtime QA attempted ${result.serviceCalls} Home Assistant writes`);

    console.log("Aurora embedded disclosure runtime: PASS");
    console.log("  expanded surfaces: RCE/tariff/voltage/PV/energy/service");
    console.log("  identical setConfig: expansion and nested-card identity preserved");
    console.log("  legacy ha-card surface: transparent, borderless and shadowless");
    console.log("  service quick links: Aurora grid, preserved paths, no native button cards");
    console.log("  embedded Zebra: 2 columns desktop, 1 mobile, state badges hidden");
    console.log("  RCE zero export: current plan ready, sale block separate, incoherent states fail closed");
    console.log("  compact service manual: six rows and exact safe-off handoff preserved");
  } finally {
    if (browser) await browser.close();
    await closeServer(server);
  }
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exitCode = 1;
});
