"use strict";

/**
 * Real-browser regression for the mobile Aurora scroll owner.
 *
 * On phones the Aurora `.shell` is the only vertical scrolling surface. The
 * document and the surrounding Home Assistant view must have no scroll range,
 * while live updates preserve the shell offset, expanded state and stable DOM.
 */

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

let chromium;
let firefox;
let webkit;
try {
  ({ chromium, firefox, webkit } = require("playwright"));
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
const VIEWPORT = Object.freeze({ width: 390, height: 844 });

function check(condition, message) {
  if (!condition) throw new Error(message);
}

const HARNESS_HTML = String.raw`<!doctype html>
<html lang="pl">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
    <title>Aurora mobile scroll regression</title>
    <style>
      :root {
        color-scheme: dark;
        --header-height: 56px;
        --primary-background-color: #07121f;
        --card-background-color: #101b27;
        --ha-card-background: #101b27;
        --primary-text-color: #f3f7fa;
        --secondary-text-color: #8fa4b5;
        --divider-color: rgba(136,181,210,.18);
      }
      * { box-sizing: border-box; }
      html, body { height: 100%; margin: 0; min-height: 0; overflow: hidden; width: 100%; }
      body { background: #080d13; color: #f3f7fa; font-family: Inter, system-ui, sans-serif; }
      #ha-scroll { display: flex; flex-direction: column; height: 100dvh; min-height: 0; overflow: hidden; width: 100%; }
      .native-header {
        align-items: center;
        background: #111820;
        display: flex;
        flex: 0 0 56px;
        height: 56px;
        padding: 0 14px;
        z-index: 40;
      }
      #stage { flex: 1 1 auto; height: calc(100dvh - 56px); min-height: 0; overflow: hidden; width: 100%; }
    </style>
    <script>
      window.customCards = [];

      window.loadCardHelpers = async function () {
        return {
          createCardElement(config) {
            const element = document.createElement(String(config.type || "").replace(/^custom:/, ""));
            element.setConfig(config);
            return element;
          },
        };
      };
    </script>
    <script type="module" src="/hoymiles-rce-chart-card.js"></script>
  </head>
  <body>
    <div id="ha-scroll"><header class="native-header">Home Assistant</header><main id="stage"></main></div>
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
  const browserType = { chromium, firefox, webkit }[requested];
  check(browserType, `Unsupported HOYMILES_UI_BROWSER: ${requested}`);
  const options = { headless: true };
  if (requested !== "chromium") return browserType.launch(options);
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

async function verifyRouteScroll(page, route) {
  const before = await page.evaluate(async (currentPath) => {
    const stage = document.getElementById("stage");
    stage.replaceChildren();
    const shell = document.createElement("hoymiles-aurora-app-shell-card");
    stage.append(shell);
    const periodCard = (title, entity, color) => ({
      key: "test48h",
      label_pl: "Energia 48 h",
      label_en: "Energy 48 h",
      card: {
        type: "custom:hoymiles-aurora-history-card",
        title,
        hours_to_show: 48,
        layout: "overview",
        entities: [{ entity, name: title, color }],
      },
    });
    const makeDisclosureCard = (title, entity, color) => ({
      type: "custom:hoymiles-aurora-disclosure-card",
      title,
      description: "Rozwinięta sekcja musi pozostać otwarta podczas odświeżania.",
      accent: "cyan",
      card: {
        type: "custom:hoymiles-aurora-history-card",
        title: `${title} — historia`,
        hours_to_show: 24,
        layout: "overview",
        entities: [{ entity, name: title, color }],
      },
    });
    const pageConfigs = {
      start: { type: "custom:hoymiles-aurora-overview-card", language: "pl" },
      "ustawienia-ems": {
        type: "custom:hoymiles-aurora-variant-a-settings-page-card",
        language: "pl",
      },
      pv: {
        type: "custom:hoymiles-aurora-compact-page-card",
        page: "pv",
        language: "pl",
        period_cards: [periodCard(
          "Produkcja PV 48 h",
          "sensor.hoymiles_hit_overview_pv_total_power",
          "#37d991",
        )],
        detail_cards: [makeDisclosureCard(
          "Energia stringów i przepływy",
          "sensor.hoymiles_hit_pv_total_power_direct",
          "#58d6ff",
        )],
      },
      bateria: {
        type: "custom:hoymiles-aurora-compact-page-card",
        page: "battery",
        language: "pl",
        history_visual_mode: "battery_soc",
        history_entities: [
          {
            entity: "sensor.hoymiles_hit_overview_battery_power",
            name: "Moc magazynu",
            color: "#42a5ff",
          },
          {
            entity: "sensor.hoymiles_hit_overview_battery_soc",
            name: "SOC",
            color: "#58d6ff",
            value_mode: "raw",
            unit: "%",
          },
        ],
        technical_bms_entities: [
          { entity: "sensor.test_bms_cell_min", name: "Cela minimum" },
          { entity: "sensor.test_bms_cell_max", name: "Cela maksimum" },
          { entity: "sensor.test_bms_cycles", name: "Cykle" },
          { entity: "sensor.test_bms_temperature", name: "Temperatura BMS" },
        ],
        period_cards: [periodCard(
          "Magazyn 48 h",
          "sensor.hoymiles_hit_overview_battery_power",
          "#42a5ff",
        )],
      },
      "load-eps": {
        type: "custom:hoymiles-aurora-compact-page-card",
        page: "energy",
        language: "pl",
        history_visual_mode: "energy_mix",
        history_entities: [
          {
            entity: "sensor.hoymiles_hit_overview_pv_total_power",
            name: "PV",
            color: "#37d991",
          },
          {
            entity: "sensor.hoymiles_actual_load_power",
            name: "Dom",
            color: "#ff617d",
          },
          {
            entity: "sensor.hoymiles_hit_overview_battery_power",
            name: "Magazyn",
            color: "#42a5ff",
          },
        ],
        groups: [
          {
            title_pl: "Zużycie domu",
            subtitle_pl: "Pełny podział energii",
            accent: "load",
            rows: [
              { entity: "sensor.hoymiles_actual_load_energy_today", name: "Dzisiaj" },
              { entity: "sensor.test_load_week", name: "7 dni" },
              { entity: "sensor.test_load_month", name: "30 dni" },
              { entity: "sensor.test_load_year", name: "12 miesięcy" },
            ],
          },
          {
            title_pl: "Sieć",
            subtitle_pl: "Import i eksport",
            accent: "grid",
            rows: [
              { entity: "sensor.hoymiles_hit_grid_energy_buy_today", name: "Import dzisiaj" },
              { entity: "sensor.hoymiles_hit_grid_energy_sell_today", name: "Eksport dzisiaj" },
              { entity: "sensor.test_grid_month", name: "Bilans 30 dni" },
            ],
          },
        ],
        period_cards: [periodCard(
          "Energia instalacji 48 h",
          "sensor.hoymiles_actual_load_power",
          "#ff617d",
        )],
        detail_cards: [makeDisclosureCard(
          "Okresy i porównania",
          "sensor.hoymiles_hit_overview_grid_total_active_power",
          "#ffbf47",
        )],
      },
      "plan-automatyki": {
        type: "custom:hoymiles-aurora-variant-a-ems-page-card",
        language: "pl",
      },
    };
    const pageConfig = pageConfigs[currentPath];
    if (!pageConfig) throw new Error(`Unsupported real route: ${currentPath}`);
    shell.setConfig({
      current_path: currentPath,
      language: "pl",
      card: pageConfig,
    });

    const testNow = Date.now();
    const historyValues = new Map();
    const powerEntities = [
      "sensor.hoymiles_hit_overview_pv_total_power",
      "sensor.hoymiles_actual_load_power",
      "sensor.hoymiles_hit_overview_grid_total_active_power",
      "sensor.hoymiles_hit_overview_battery_power",
      "sensor.hoymiles_hit_pv_total_power_direct",
      "sensor.hoymiles_hit_pv1_power_direct",
      "sensor.hoymiles_hit_pv2_power_direct",
      "sensor.hoymiles_hit_pv3_power_direct",
      "sensor.hoymiles_hit_pv4_power_direct",
      "sensor.hoymiles_hit_overview_generator_active_power",
    ];
    const makeHass = (tick) => {
      const updated = new Date(testNow + tick).toISOString();
      const states = {};
      const put = (entityId, value, unit = "", attributes = {}) => {
        states[entityId] = {
          entity_id: entityId,
          state: String(value),
          last_changed: updated,
          last_updated: updated,
          attributes: {
            ...(unit ? { unit_of_measurement: unit } : {}),
            ...attributes,
          },
        };
      };
      put("binary_sensor.hoymiles_ems_execution_ready", "on");
      put("binary_sensor.hoymiles_ems_control_conflict", "off");
      put("sensor.hoymiles_hit_ems_supervisor", "active");
      put("sensor.hoymiles_hit_pv_link_status", "online");
      put("select.hoymiles_hit_gen_port_mode", "pv");
      powerEntities.forEach((entityId, index) => {
        put(entityId, 420 + index * 35 + tick, "W");
      });
      put("sensor.hoymiles_hit_overview_battery_soc", 38, "%");
      put("sensor.hoymiles_hit_battery_capacity", 26, "kWh");
      put("sensor.hoymiles_hit_ems_self_use_soc_readback", 25, "%");
      put("sensor.hoymiles_solcast_forecast_today", 39.9, "kWh");
      put("sensor.hoymiles_solcast_forecast_remaining_today", 22, "kWh");
      put("sensor.hoymiles_solcast_forecast_tomorrow", 39.8, "kWh");
      put("sensor.hoymiles_load_average_4_days", 25.4, "kWh");
      put("sensor.hoymiles_hit_pv_total_energy_today", 4.2, "kWh");
      put("sensor.hoymiles_actual_load_energy_today", 3.4, "kWh");
      put("sensor.hoymiles_hit_grid_energy_buy_today", 3.2, "kWh");
      put("sensor.hoymiles_hit_grid_energy_sell_today", 0, "kWh");
      put("sensor.test_load_week", 172.3, "kWh");
      put("sensor.test_load_month", 738.4, "kWh");
      put("sensor.test_load_year", 8842.1, "kWh");
      put("sensor.test_grid_month", 103.7, "kWh");
      put("sensor.test_bms_cell_min", 3.28, "V");
      put("sensor.test_bms_cell_max", 3.31, "V");
      put("sensor.test_bms_cycles", 214);
      put("sensor.test_bms_temperature", 27.4, "°C");
      for (let index = 1; index <= 4; index += 1) {
        put(`sensor.hoymiles_hit_pv${index}_voltage`, 318 + index, "V");
        put(`sensor.hoymiles_hit_pv${index}_current`, 0.6 + index / 10, "A");
        put(`sensor.hoymiles_hit_pv${index}_today_energy`, 1 + index / 10, "kWh");
      }
      return {
        language: "pl",
        locale: { language: "pl" },
        states,
        callApi: async (_method, requestPath) => {
          const url = new URL(requestPath, window.location.origin);
          const entityIds = decodeURIComponent(url.searchParams.get("filter_entity_id") || "")
            .split(",")
            .filter(Boolean);
          return entityIds.map((entityId, seriesIndex) => {
            if (!historyValues.has(entityId)) {
              historyValues.set(entityId, Array.from({ length: 25 }, (_, pointIndex) => ({
                entity_id: entityId,
                state: String(200 + seriesIndex * 45 + pointIndex * 12),
                last_changed: new Date(testNow - (24 - pointIndex) * 60 * 60 * 1000).toISOString(),
                last_updated: new Date(testNow - (24 - pointIndex) * 60 * 60 * 1000).toISOString(),
                attributes: { unit_of_measurement: "W" },
              })));
            }
            return historyValues.get(entityId);
          });
        },
        callService: async () => undefined,
      };
    };

    shell.hass = makeHass(0);
    for (let attempt = 0; attempt < 100 && !shell._child; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    if (!shell._child) throw new Error(`Child card did not mount for ${currentPath}`);

    const pageCard = shell._child;
    let disclosure = null;
    let disclosureRoot = null;
    let disclosureCard = null;
    let disclosureSvg = null;
    let groupDetails = null;
    let bmsDetails = null;
    if (pageCard.tagName === "HOYMILES-AURORA-COMPACT-PAGE-CARD") {
      const periodButton = pageCard.shadowRoot?.querySelector?.('[data-period="test48h"]');
      if (currentPath !== "bateria") {
        if (!periodButton) throw new Error(`${currentPath}: configured period button is missing`);
        periodButton.click();
        for (let attempt = 0; attempt < 200; attempt += 1) {
          if (
            pageCard._periodKey === "test48h"
            && Number(pageCard._historyChild?._config?.hours_to_show) === 48
          ) break;
          await new Promise((resolve) => setTimeout(resolve, 10));
        }
        if (Number(pageCard._historyChild?._config?.hours_to_show) !== 48) {
          throw new Error(`${currentPath}: selected period history did not mount`);
        }
      }

      if (currentPath === "load-eps") {
        const groupToggle = pageCard.shadowRoot?.querySelector?.('[data-action="toggle-group"]');
        if (!groupToggle) throw new Error("load-eps: expandable energy group is missing");
        groupToggle.click();
        groupDetails = groupToggle.closest("[data-group]")?.querySelector(".group-details") || null;
        if (!groupDetails || groupDetails.hidden) throw new Error("load-eps: energy group did not expand");
      }

      if (currentPath === "bateria") {
        bmsDetails = pageCard.shadowRoot?.querySelector?.("details.bms-technical") || null;
        if (!bmsDetails) throw new Error("bateria: BMS technical disclosure is missing");
        bmsDetails.open = true;
      } else {
        for (let attempt = 0; attempt < 200; attempt += 1) {
          disclosure = pageCard.shadowRoot?.querySelector?.("hoymiles-aurora-disclosure-card") || null;
          if (disclosure?._card && disclosure.shadowRoot?.querySelector?.(".disclosure-button")) break;
          await new Promise((resolve) => setTimeout(resolve, 10));
        }
        if (!disclosure?._card) throw new Error(`${currentPath}: Aurora disclosure did not mount`);
        disclosure.shadowRoot.querySelector(".disclosure-button").click();
        disclosureRoot = disclosure.shadowRoot.querySelector("ha-card");
        disclosureCard = disclosure._card;
        for (let attempt = 0; attempt < 200; attempt += 1) {
          disclosureSvg = disclosureCard.shadowRoot?.querySelector?.("svg") || null;
          if (!disclosureCard._loading && disclosureSvg) break;
          await new Promise((resolve) => setTimeout(resolve, 10));
        }
        if (!disclosure._expanded || !disclosureSvg) {
          throw new Error(`${currentPath}: expanded detail history did not become ready`);
        }
      }
    }

    const expectsHistory = !["plan-automatyki", "ustawienia-ems"].includes(currentPath);
    let historyCard = null;
    if (expectsHistory) {
      for (let attempt = 0; attempt < 200; attempt += 1) {
        historyCard = pageCard.shadowRoot?.querySelector?.("[data-history-host] > hoymiles-aurora-history-card")
          || pageCard.shadowRoot?.querySelector?.("hoymiles-aurora-history-card")
          || null;
        if (historyCard && !historyCard._loading && historyCard.shadowRoot?.querySelector?.("svg")) break;
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
    }
    const historySvg = historyCard?.shadowRoot?.querySelector?.("svg");
    if (expectsHistory && (!historyCard || !historySvg)) {
      throw new Error(`Real ${currentPath} history did not mount`);
    }

    const outer = document.getElementById("ha-scroll");
    const surface = shell.shadowRoot.querySelector(".shell");
    const pageRoot = pageCard.shadowRoot?.querySelector?.(".root") || null;
    surface.scrollTop = surface.scrollHeight - surface.clientHeight;
    await new Promise((resolve) => {
      let frames = 6;
      const settle = () => {
        frames -= 1;
        if (frames > 0) requestAnimationFrame(settle);
        else resolve();
      };
      requestAnimationFrame(settle);
    });
    window.__routeRefs = {
      shell,
      surface,
      root: shell.shadowRoot.querySelector(".content"),
      pageCard,
      pageRoot,
      historyCard,
      historySvg,
      disclosure,
      disclosureRoot,
      disclosureCard,
      disclosureSvg,
      groupDetails,
      bmsDetails,
      periodKey: pageCard._periodKey,
      configSignature: pageCard._configSignature,
      makeHass,
    };
    return {
      outerScrollTop: outer.scrollTop,
      outerScrollRange: outer.scrollHeight - outer.clientHeight,
      surfaceScrollTop: surface.scrollTop,
      surfaceScrollRange: surface.scrollHeight - surface.clientHeight,
      documentScrollTop: document.scrollingElement.scrollTop,
      documentScrollRange: document.scrollingElement.scrollHeight - document.scrollingElement.clientHeight,
      shellOverflowY: getComputedStyle(surface).overflowY,
      shellPosition: getComputedStyle(surface).position,
      bodyOverflowY: getComputedStyle(document.body).overflowY,
      periodKey: pageCard._periodKey,
      disclosureExpanded: disclosure?._expanded ?? null,
      groupExpanded: groupDetails ? !groupDetails.hidden : null,
      bmsExpanded: bmsDetails?.open ?? null,
    };
  }, route);

  check(before.documentScrollRange <= 1, `${route}: document has ${before.documentScrollRange}px scroll range`);
  check(before.outerScrollRange <= 1, `${route}: outer HA view has ${before.outerScrollRange}px scroll range`);
  check(before.documentScrollTop === 0, `${route}: document acquired scroll offset ${before.documentScrollTop}px`);
  check(before.outerScrollTop === 0, `${route}: outer HA view acquired scroll offset ${before.outerScrollTop}px`);
  check(before.surfaceScrollRange > 300, `${route}: Aurora shell has only ${before.surfaceScrollRange}px scroll range`);
  check(before.surfaceScrollTop > 100, `${route}: Aurora shell was not scrolled`);
  check(
    Math.abs(before.surfaceScrollTop - before.surfaceScrollRange) <= 1,
    `${route}: Aurora shell did not reach the exact bottom`,
  );
  check(before.shellOverflowY === "auto", `${route}: shell overflow is ${before.shellOverflowY}`);
  check(before.shellPosition === "relative", `${route}: shell position is ${before.shellPosition}`);
  check(before.bodyOverflowY === "hidden", `${route}: harness no longer isolates the shell scroll owner`);

  const after = await page.evaluate(async () => {
    const outer = document.getElementById("ha-scroll");
    const {
      shell,
      surface,
      root,
      pageCard,
      pageRoot,
      historyCard,
      historySvg,
      disclosure,
      disclosureRoot,
      disclosureCard,
      disclosureSvg,
      groupDetails,
      bmsDetails,
      periodKey,
      configSignature,
      makeHass,
    } = window.__routeRefs;
    const surfaceBefore = surface.scrollTop;
    const clone = (value) => JSON.parse(JSON.stringify(value));
    for (let tick = 1; tick <= 50; tick += 1) {
      shell.setConfig(clone(shell._config));
      if (pageCard.tagName === "HOYMILES-AURORA-COMPACT-PAGE-CARD") {
        pageCard.setConfig(clone(pageCard._config));
      }
      if (disclosure) disclosure.setConfig(clone(disclosure._config));
      shell.hass = makeHass(tick);
    }
    await new Promise((resolve) => {
      let frames = 6;
      const settle = () => {
        frames -= 1;
        if (frames > 0) requestAnimationFrame(settle);
        else resolve();
      };
      requestAnimationFrame(settle);
    });
    const currentHistory = pageCard.shadowRoot?.querySelector?.("[data-history-host] > hoymiles-aurora-history-card")
      || pageCard.shadowRoot?.querySelector?.("hoymiles-aurora-history-card")
      || null;
    return {
      surfaceBefore,
      documentAfter: document.scrollingElement.scrollTop,
      documentRangeAfter: document.scrollingElement.scrollHeight - document.scrollingElement.clientHeight,
      outerAfter: outer.scrollTop,
      outerRangeAfter: outer.scrollHeight - outer.clientHeight,
      surfaceScrollTop: surface.scrollTop,
      surfaceRangeAfter: surface.scrollHeight - surface.clientHeight,
      sameSurface: surface === shell.shadowRoot.querySelector(".shell"),
      sameContent: root === shell.shadowRoot.querySelector(".content"),
      samePage: pageCard === shell._child,
      samePageRoot: !pageRoot || pageRoot === pageCard.shadowRoot.querySelector(".root"),
      sameHistory: !historyCard || historyCard === currentHistory,
      sameHistorySvg: !historySvg || historySvg === historyCard.shadowRoot.querySelector("svg"),
      sameDisclosure: !disclosure
        || disclosure === pageCard.shadowRoot.querySelector("hoymiles-aurora-disclosure-card"),
      sameDisclosureRoot: !disclosureRoot
        || disclosureRoot === disclosure.shadowRoot.querySelector("ha-card"),
      sameDisclosureCard: !disclosureCard || disclosureCard === disclosure._card,
      sameDisclosureSvg: !disclosureSvg
        || disclosureSvg === disclosureCard.shadowRoot.querySelector("svg"),
      disclosureExpanded: disclosure?._expanded ?? null,
      sameGroupDetails: !groupDetails
        || groupDetails === pageCard.shadowRoot.querySelector(".group-details"),
      groupExpanded: groupDetails ? !groupDetails.hidden : null,
      sameBmsDetails: !bmsDetails
        || bmsDetails === pageCard.shadowRoot.querySelector("details.bms-technical"),
      bmsExpanded: bmsDetails?.open ?? null,
      periodBefore: periodKey,
      periodAfter: pageCard._periodKey,
      signatureChanged: configSignature !== pageCard._configSignature,
      scrollRestoreToken: shell._scrollRestoreToken,
      scrollIntentRevision: shell._scrollIntentRevision,
      scrollGestureActive: shell._scrollGestureActive,
      scrollGestureUntil: shell._scrollGestureUntil,
      mediaMobile: window.matchMedia("(max-width: 620px)").matches,
    };
  });

  check(
    Math.abs(after.surfaceScrollTop - after.surfaceBefore) <= 1,
    `${route}: 50 state+last_updated updates shifted the shell scroll by ${Math.abs(after.surfaceScrollTop - after.surfaceBefore)}px (${after.surfaceBefore} -> ${after.surfaceScrollTop}; range ${before.surfaceScrollRange} -> ${after.surfaceRangeAfter}; restore ${after.scrollRestoreToken}; intent ${after.scrollIntentRevision}; active ${after.scrollGestureActive}; until ${after.scrollGestureUntil}; mobile ${after.mediaMobile})`,
  );
  check(
    Math.abs(after.surfaceScrollTop - after.surfaceRangeAfter) <= 1,
    `${route}: identical setConfig replays no longer leave the shell at the exact bottom`,
  );
  check(after.documentRangeAfter <= 1, `${route}: updates created document scroll range`);
  check(after.outerRangeAfter <= 1, `${route}: updates created outer HA scroll range`);
  check(after.documentAfter === 0, `${route}: updates moved the document`);
  check(after.outerAfter === 0, `${route}: updates moved the outer HA view`);
  check(after.sameSurface && after.sameContent, `${route}: app shell DOM was replaced during live updates`);
  check(after.samePage, `${route}: real page card was replaced during live updates`);
  check(after.samePageRoot, `${route}: real page root was replaced by an identical setConfig replay`);
  check(
    after.sameHistory,
    `${route}: real history card was replaced during live updates (signature changed: ${after.signatureChanged}, page root stable: ${after.samePageRoot}, period: ${after.periodBefore} -> ${after.periodAfter})`,
  );
  check(after.sameHistorySvg, `${route}: real history SVG was replaced during live updates`);
  check(
    after.sameDisclosure && after.sameDisclosureRoot && after.sameDisclosureCard && after.sameDisclosureSvg,
    `${route}: expanded Aurora disclosure or its history DOM was replaced`,
  );
  if (before.disclosureExpanded !== null) {
    check(after.disclosureExpanded, `${route}: identical setConfig replay collapsed the Aurora disclosure`);
  }
  check(after.sameGroupDetails, `${route}: expanded energy group DOM was replaced`);
  if (before.groupExpanded !== null) {
    check(after.groupExpanded, `${route}: identical setConfig replay collapsed the energy group`);
  }
  check(after.sameBmsDetails, `${route}: expanded BMS details DOM was replaced`);
  if (before.bmsExpanded !== null) {
    check(after.bmsExpanded, `${route}: identical setConfig replay collapsed the BMS details`);
  }
  check(
    after.periodAfter === after.periodBefore,
    `${route}: period changed from ${after.periodBefore} to ${after.periodAfter}`,
  );

  const targetHref = route === "start"
    ? "/hoymiles-falownik/pv"
    : "/hoymiles-falownik/start";
  const ancestorReset = await page.evaluate((href) => {
    const shell = window.__routeRefs.shell;
    const stage = document.getElementById("stage");
    const link = [...shell.shadowRoot.querySelectorAll(".mobile-nav a")]
      .find((candidate) => candidate.getAttribute("href") === href);
    const spacer = document.createElement("div");
    spacer.style.height = "1400px";
    stage.style.overflowY = "auto";
    stage.append(spacer);
    stage.scrollTop = 180;
    const before = stage.scrollTop;
    shell._resetMobileScrollForNavigation(link);
    const after = stage.scrollTop;
    spacer.remove();
    stage.style.overflowY = "hidden";
    return { before, after };
  }, targetHref);
  check(ancestorReset.before > 0, `${route}: ancestor reset fixture did not scroll`);
  check(ancestorReset.after === 0, `${route}: destination navigation left the HA ancestor at ${ancestorReset.after}px`);
  await page.evaluate((href) => {
    const shell = window.__routeRefs.shell;
    const link = [...shell.shadowRoot.querySelectorAll(".mobile-nav a")]
      .find((candidate) => candidate.getAttribute("href") === href);
    if (!link) throw new Error(`Mobile navigation link is missing: ${href}`);
    window.__routeNavTap = { count: 0, href: "" };
    link.addEventListener("click", (event) => {
      event.preventDefault();
      window.__routeNavTap.count += 1;
      window.__routeNavTap.href = event.currentTarget.getAttribute("href");
    }, { once: true });
  }, targetHref);
  await page.locator(`hoymiles-aurora-app-shell-card .mobile-nav a[href="${targetHref}"]`).tap();
  const navigation = await page.evaluate(() => ({
    ...window.__routeNavTap,
    shellScrollTop: window.__routeRefs.surface.scrollTop,
    outerScrollTop: document.getElementById("ha-scroll").scrollTop,
    documentScrollTop: document.scrollingElement.scrollTop,
  }));
  check(navigation.count === 1, `${route}: mobile navigation did not receive a real tap`);
  check(navigation.href === targetHref, `${route}: mobile navigation href is ${navigation.href}`);
  check(
    navigation.shellScrollTop <= 1,
    `${route}: destination navigation did not reset the mobile shell to the top (${navigation.shellScrollTop}px)`,
  );
  check(navigation.outerScrollTop === 0, `${route}: tap moved outer HA view`);
  check(navigation.documentScrollTop === 0, `${route}: tap moved document`);
  const userGesture = await page.evaluate(async () => {
    const { shell, surface, makeHass } = window.__routeRefs;
    surface.scrollTop = Math.max(120, Math.floor((surface.scrollHeight - surface.clientHeight) * 0.72));
    const scheduledFrom = surface.scrollTop;
    shell.hass = makeHass(51);
    surface.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    surface.dispatchEvent(new PointerEvent("pointermove", { bubbles: true }));
    surface.scrollTop = Math.max(40, scheduledFrom - 117);
    const intended = surface.scrollTop;
    surface.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    await new Promise((resolve) => {
      let frames = 6;
      const settle = () => {
        frames -= 1;
        if (frames > 0) requestAnimationFrame(settle);
        else resolve();
      };
      requestAnimationFrame(settle);
    });
    return { scheduledFrom, intended, final: surface.scrollTop };
  });
  check(
    Math.abs(userGesture.final - userGesture.intended) <= 1,
    `${route}: refresh scroll preservation overrode a user gesture (${userGesture.scheduledFrom} -> ${userGesture.intended} -> ${userGesture.final})`,
  );
  const momentum = await page.evaluate(async () => {
    const { shell, surface, makeHass } = window.__routeRefs;
    const waitFrames = (count = 2) => new Promise((resolve) => {
      let frames = count;
      const settle = () => {
        frames -= 1;
        if (frames > 0) requestAnimationFrame(settle);
        else resolve();
      };
      requestAnimationFrame(settle);
    });
    await new Promise((resolve) => setTimeout(resolve, 280));
    const max = Math.max(0, surface.scrollHeight - surface.clientHeight);
    surface.scrollTop = Math.max(80, Math.floor(max * 0.28));
    await waitFrames(3);
    surface.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    surface.dispatchEvent(new PointerEvent("pointermove", { bubbles: true }));
    surface.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    const violations = [];
    let requested = surface.scrollTop;
    let boundedRequested = requested;
    for (let tick = 0; tick < 50; tick += 1) {
      shell.setConfig(JSON.parse(JSON.stringify(shell._config)));
      shell.hass = makeHass(1000 + tick);
      requested = Math.min(
        Math.max(0, surface.scrollHeight - surface.clientHeight - 20),
        surface.scrollTop + 7,
      );
      surface.scrollTop = requested;
      surface.dispatchEvent(new Event("scroll"));
      await waitFrames(2);
      const actual = surface.scrollTop;
      const currentMax = Math.max(0, surface.scrollHeight - surface.clientHeight);
      boundedRequested = Math.min(requested, currentMax);
      if (actual < boundedRequested - 1) {
        violations.push({ tick, requested, boundedRequested, actual, currentMax });
      }
      await new Promise((resolve) => setTimeout(resolve, 18));
    }
    await new Promise((resolve) => setTimeout(resolve, 280));
    await waitFrames(4);
    const momentumFinal = surface.scrollTop;

    surface.scrollTop = surface.scrollHeight - surface.clientHeight;
    await waitFrames(3);
    const snapshot = shell._captureMobileScroll();
    const oldBottom = surface.scrollTop;
    const intentBeforeRestore = shell._scrollIntentRevision;
    const spacer = document.createElement("div");
    spacer.style.height = "240px";
    spacer.setAttribute("data-scroll-restore-spacer", "");
    shell.shadowRoot.querySelector(".content").append(spacer);
    const expandedBottom = surface.scrollHeight - surface.clientHeight;
    shell._preserveMobileScroll(
      snapshot.surface,
      snapshot.top,
      snapshot.max,
      snapshot.intentRevision,
    );
    await waitFrames(7);
    await new Promise((resolve) => setTimeout(resolve, 60));
    const restoredBottom = surface.scrollTop;
    const intentAfterRestore = shell._scrollIntentRevision;
    spacer.remove();

    surface.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    surface.dispatchEvent(new PointerEvent("pointermove", { bubbles: true }));
    surface.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 80));
    surface.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 280));
    const heldSessionActive = shell._scrollUserSessionActive && shell._scrollGestureActive;
    surface.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 280));
    const overlapReleased = !shell._scrollUserSessionActive
      && !shell._scrollGestureActive
      && shell._scrollGestureUntil === 0;
    const overlapCaptureAvailable = shell._captureMobileScroll() !== null;
    return {
      violations,
      requested,
      boundedRequested,
      momentumFinal,
      oldBottom,
      expandedBottom,
      restoredBottom,
      intentBeforeRestore,
      intentAfterRestore,
      userSessionActive: shell._scrollUserSessionActive,
      heldSessionActive,
      overlapReleased,
      overlapCaptureAvailable,
      outerScrollTop: document.getElementById("ha-scroll").scrollTop,
      documentScrollTop: document.scrollingElement.scrollTop,
    };
  });
  check(
    momentum.violations.length === 0
      && momentum.momentumFinal >= momentum.boundedRequested - 1,
    `${route}: >800 ms momentum scrolling fought a stale refresh restore: ${JSON.stringify(momentum)}`,
  );
  check(
    momentum.expandedBottom >= momentum.oldBottom + 239
      && Math.abs(momentum.restoredBottom - momentum.expandedBottom) <= 1
      && momentum.intentAfterRestore === momentum.intentBeforeRestore,
    `${route}: programmatic bottom restore was mistaken for user scroll: ${JSON.stringify(momentum)}`,
  );
  check(
    momentum.heldSessionActive === true
      && momentum.overlapReleased === true
      && momentum.overlapCaptureAvailable === true
      && momentum.userSessionActive === false
      && momentum.outerScrollTop === 0
      && momentum.documentScrollTop === 0,
    `${route}: overlapping touch left a stale user-scroll session or moved an outer surface: ${JSON.stringify(momentum)}`,
  );
  return { ...after, navigation, userGesture, momentum };
}

async function verifyHistoryDom(page) {
  const initial = await page.evaluate(async () => {
    const stage = document.getElementById("stage");
    stage.replaceChildren();
    const historyStage = document.createElement("section");
    historyStage.id = "history-stage";
    const card = document.createElement("hoymiles-aurora-history-card");
    historyStage.append(card);
    stage.append(historyStage);

    const entityId = "sensor.mobile_history_power";
    const now = Date.now();
    const history = [Array.from({ length: 25 }, (_, index) => ({
      entity_id: entityId,
      state: (0.4 + index * 0.08).toFixed(2),
      last_changed: new Date(now - (24 - index) * 60 * 60 * 1000).toISOString(),
      last_updated: new Date(now - (24 - index) * 60 * 60 * 1000).toISOString(),
      attributes: { unit_of_measurement: "kW" },
    }))];
    const makeHass = (tick) => ({
      language: "pl",
      locale: { language: "pl" },
      states: {
        [entityId]: {
          state: (1 + tick / 10).toFixed(2),
          last_updated: new Date(now + tick * 1000).toISOString(),
          attributes: { unit_of_measurement: "kW" },
        },
      },
      callApi: async () => history,
    });

    card.setConfig({
      title: "Moc — ostatnie 24 godziny",
      hours_to_show: 24,
      layout: "overview",
      entities: [{ entity: entityId, name: "PV", color: "#37d991" }],
    });
    card.hass = makeHass(0);
    for (let attempt = 0; attempt < 150; attempt += 1) {
      if (!card._loading && card.shadowRoot.querySelector("svg")) break;
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    const svg = card.shadowRoot.querySelector("svg");
    const root = card.shadowRoot.querySelector("ha-card");
    const legend = card.shadowRoot.querySelector(`[data-history-live-value="${entityId}"]`);
    if (!svg || !root || !legend) throw new Error("History chart did not finish its initial render");

    window.__historyRefs = { card, svg, root, legend, makeHass };
    const outer = document.getElementById("ha-scroll");
    return {
      outerScrollRange: outer.scrollHeight - outer.clientHeight,
      documentScrollRange: document.scrollingElement.scrollHeight - document.scrollingElement.clientHeight,
      legend: legend.textContent,
    };
  });

  check(initial.outerScrollRange <= 1, "history: outer HA view acquired a scroll range");
  check(initial.documentScrollRange <= 1, "history: document acquired a scroll range");

  const after = await page.evaluate(async () => {
    const outer = document.getElementById("ha-scroll");
    const { card, svg, root, legend, makeHass } = window.__historyRefs;
    const outerBefore = outer.scrollTop;
    for (let tick = 1; tick <= 50; tick += 1) card.hass = makeHass(tick);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return {
      outerBefore,
      outerAfter: outer.scrollTop,
      outerRangeAfter: outer.scrollHeight - outer.clientHeight,
      documentAfter: document.scrollingElement.scrollTop,
      documentRangeAfter: document.scrollingElement.scrollHeight - document.scrollingElement.clientHeight,
      sameRoot: root === card.shadowRoot.querySelector("ha-card"),
      sameSvg: svg === card.shadowRoot.querySelector("svg"),
      sameLegend: legend === card.shadowRoot.querySelector("[data-history-live-value]"),
      initialLegend: window.__historyRefs.legend.textContent,
      finalLegend: card.shadowRoot.querySelector("[data-history-live-value]").textContent,
    };
  });

  check(
    Math.abs(after.outerAfter - after.outerBefore) <= 1,
    `history: 50 updates shifted the outer HA view by ${Math.abs(after.outerAfter - after.outerBefore)}px`,
  );
  check(after.outerRangeAfter <= 1, "history: updates created outer HA scroll range");
  check(after.documentRangeAfter <= 1, "history: updates created document scroll range");
  check(after.documentAfter === 0, "history: updates moved the document");
  check(after.sameRoot && after.sameSvg && after.sameLegend, "history: live updates replaced the card, SVG or legend DOM");
  check(after.finalLegend === "6,00 kW", `history: final legend value is ${after.finalLegend}`);
  check(after.finalLegend !== initial.legend, "history: legend did not update its latest value");
  return after;
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
    try {
      await page.waitForFunction(() => customElements.get("hoymiles-aurora-app-shell-card")
        && customElements.get("hoymiles-aurora-history-card")
        && customElements.get("hoymiles-aurora-variant-a-ems-page-card"));
    } catch (error) {
      throw new Error(`Aurora custom elements did not register: ${browserErrors.join(" | ") || error.message}`);
    }

    const overview = await verifyRouteScroll(page, "start");
    const settings = await verifyRouteScroll(page, "ustawienia-ems");
    const pv = await verifyRouteScroll(page, "pv");
    const battery = await verifyRouteScroll(page, "bateria");
    const energy = await verifyRouteScroll(page, "load-eps");
    const ems = await verifyRouteScroll(page, "plan-automatyki");
    const history = await verifyHistoryDom(page);
    check(browserErrors.length === 0, `Browser errors: ${browserErrors.join(" | ")}`);

    console.log("PASS Aurora mobile scroll Playwright regression");
    console.log(`- viewport: ${VIEWPORT.width}x${VIEWPORT.height}`);
    console.log(`- browser: ${String(process.env.HOYMILES_UI_BROWSER || "chromium").toLowerCase()}`);
    console.log(`- Przegląd: real page, 50 updates, shell scroll delta ${Math.abs(overview.surfaceScrollTop - overview.surfaceBefore)}px, tap ${overview.navigation.href}`);
    console.log(`- Ustawienia: real route, 50 identical setConfig+hass replays, shell scroll delta ${Math.abs(settings.surfaceScrollTop - settings.surfaceBefore)}px, tap ${settings.navigation.href}`);
    console.log(`- PV: real route, 50 identical setConfig+hass replays, shell scroll delta ${Math.abs(pv.surfaceScrollTop - pv.surfaceBefore)}px, tap ${pv.navigation.href}`);
    console.log(`- Magazyn: real route, 50 identical setConfig+hass replays, shell scroll delta ${Math.abs(battery.surfaceScrollTop - battery.surfaceBefore)}px, tap ${battery.navigation.href}`);
    console.log(`- Energia: real route, 50 identical setConfig+hass replays, shell scroll delta ${Math.abs(energy.surfaceScrollTop - energy.surfaceBefore)}px, tap ${energy.navigation.href}`);
    console.log(`- EMS: real route, 50 identical setConfig+hass replays, shell scroll delta ${Math.abs(ems.surfaceScrollTop - ems.surfaceBefore)}px, tap ${ems.navigation.href}`);
    console.log(`- history: same SVG ${history.sameSvg}, latest legend ${history.finalLegend}`);
    await context.close();
  } finally {
    if (browser) await browser.close();
    await closeServer(server);
  }
}

main().catch((error) => {
  console.error(`FAIL Aurora mobile scroll Playwright regression: ${error.stack || error.message}`);
  process.exitCode = 1;
});
