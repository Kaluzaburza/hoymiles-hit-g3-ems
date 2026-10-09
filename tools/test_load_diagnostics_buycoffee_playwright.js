"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");

const ROOT = path.resolve(process.env.HOYMILES_UI_TEST_ROOT || process.cwd());
const CARD = path.join(ROOT, "home_assistant", "www", "hoymiles-rce-chart-card.js");
const EVIDENCE = process.env.HOYMILES_UI_EVIDENCE_DIR || "";

function check(value, message) {
  if (!value) throw new Error(message);
}

const HTML = `<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{box-sizing:border-box}html,body{margin:0;background:#080d13;color:#fff;font-family:Arial,sans-serif}#stage{display:grid;gap:16px;padding:12px}hoymiles-aurora-variant-a-settings-page-card,hoymiles-aurora-variant-a-policy-settings-card,hoymiles-ems-shared-inputs-card{display:block;min-width:0}</style>
<script>window.customCards=[];window.loadCardHelpers=async()=>({createCardElement(config){const el=document.createElement(String(config.type||"").replace(/^custom:/,""));el.setConfig(config);return el;}});</script>
<script type="module" src="/card.js"></script></head><body><main id="stage"></main></body></html>`;

function state(entityId, value, attributes = {}) {
  const stamp = new Date().toISOString();
  return { entity_id: entityId, state: String(value), attributes, last_changed: stamp, last_updated: stamp };
}

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return server.address().port;
}

async function close(server) {
  await new Promise((resolve) => server.close(resolve));
}

async function verify(page, language, screenshotName) {
  await page.waitForFunction(() => customElements.get("hoymiles-aurora-variant-a-settings-page-card")
    && customElements.get("hoymiles-aurora-variant-a-policy-settings-card")
    && customElements.get("hoymiles-ems-shared-inputs-card"));
  const result = await page.evaluate((lang) => {
    const stage = document.getElementById("stage");
    stage.replaceChildren();
    let serviceCalls = 0;
    const sharedEntity = "sensor.hoymiles_hit_ems_shared_inputs";
    const planEntity = "sensor.hoymiles_hit_rce_optimized_plan";
    const states = {
      [sharedEntity]: state(sharedEntity, "ready", {
        schema_version: 1,
        config_entry_id: "entry-a",
        system: {}, forecast: {}, bms: {}, power: {}, migration: {},
        load: {
          ready: false,
          source: "configured_daily_fallback",
          daily_history_days: 0,
          average_daily_home_load_kwh: 17,
          weekday_profile_30m_kwh: [],
          weekend_profile_30m_kwh: [],
          weekday_profile_days: 0,
          weekend_profile_days: 0,
          fallback_currently_used: true,
        },
      }),
      [planEntity]: state(planEntity, "fallback", {
        recorder_load_history_days: 0,
        recorder_load_qualification_date: "2026-09-22",
        recorder_load_qualification_reason: "partial_phase_counter",
        recorder_load_phase_quality: {
          "2026-09-22": { l1: "missing_start_edge", l2: "missing_start_edge", l3: "missing_start_edge" },
        },
        recorder_night_history_days: 0,
        recorder_night_qualification_date: null,
        recorder_night_qualification_reason: "unknown",
        recorder_night_quality: {},
        load_history_read_status: "extended_complete",
        load_profile_generated_at: null,
        load_model_source: "configured_daily_fallback",
      }),
    };
    function state(entityId, value, attributes = {}) {
      const stamp = new Date().toISOString();
      return { entity_id: entityId, state: String(value), attributes, last_changed: stamp, last_updated: stamp };
    }
    const hass = {
      language: lang,
      locale: { language: lang },
      states,
      callService: async () => { serviceCalls += 1; },
      callApi: async () => ({}),
    };

    const general = document.createElement("hoymiles-aurora-variant-a-settings-page-card");
    general.setConfig({ language: lang });
    general.hass = hass;
    stage.append(general);

    const policy = document.createElement("hoymiles-aurora-variant-a-policy-settings-card");
    policy.setConfig({ language: lang, section: "tariff", detail_cards: [] });
    policy.hass = hass;
    stage.append(policy);

    const shared = document.createElement("hoymiles-ems-shared-inputs-card");
    shared.setConfig({
      language: lang,
      mode: "full",
      shared_inputs_entity: sharedEntity,
      inverter_power_helper_entity: "input_select.test_inverter",
      forecast_today_helper_entity: "input_text.test_today",
      forecast_tomorrow_helper_entity: "input_text.test_tomorrow",
      forecast_day_3_helper_entity: "input_text.test_day3",
      fallback_load_helper_entity: "input_number.test_fallback",
    });
    shared.hass = hass;
    stage.append(shared);

    const inspectSupport = (card, expectedText) => {
      const links = card.shadowRoot.querySelectorAll("a.support-coffee");
      const link = links[0];
      const diagnostics = card.shadowRoot.querySelector("[data-settings-diagnostics]");
      if (!link || !diagnostics) throw new Error("support controls missing");
      const sameNode = link;
      for (let tick = 0; tick < 25; tick += 1) card.hass = { ...hass, states: { ...states } };
      const stable = card.shadowRoot.querySelector("a.support-coffee") === sameNode;
      link.addEventListener("click", (event) => event.preventDefault(), { once: true });
      link.focus();
      link.click();
      return {
        count: links.length,
        href: link.getAttribute("href"),
        target: link.getAttribute("target"),
        rel: link.getAttribute("rel"),
        text: link.textContent.trim(),
        expectedText,
        beforeDiagnostics: link.nextElementSibling === diagnostics,
        focused: card.shadowRoot.activeElement === link,
        stable,
      };
    };

    const generalSupport = inspectSupport(
      general,
      lang === "pl" ? "☕ Postaw kawę autorowi" : "☕ Support the author",
    );
    const policySupport = inspectSupport(
      policy,
      lang === "pl" ? "☕ Postaw kawę autorowi" : "☕ Support the author",
    );
    const dayFact = shared.shadowRoot.querySelector('[data-fact="historyDays"]');
    const nightFact = shared.shadowRoot.querySelector('[data-fact="nightHistory"]');
    const diagnosed = {
      dayValue: dayFact?.querySelector("strong")?.textContent || "",
      dayNote: dayFact?.querySelector("small")?.textContent || "",
      nightValue: nightFact?.querySelector("strong")?.textContent || "",
      nightNote: nightFact?.querySelector("small")?.textContent || "",
    };
    delete states[planEntity];
    shared.hass = { ...hass, states: { ...states } };
    const unknown = {
      dayNote: dayFact?.querySelector("small")?.textContent || "",
      nightNote: nightFact?.querySelector("small")?.textContent || "",
    };
    states[planEntity] = state(planEntity, "history", {
      recorder_load_history_days: 1,
      recorder_load_qualification_date: "2026-09-23",
      recorder_load_qualification_reason: "complete",
      recorder_night_history_days: 1,
      recorder_night_qualification_date: "2026-09-23",
      recorder_night_qualification_reason: "complete",
      load_history_read_status: "extended_complete",
    });
    states[sharedEntity].attributes.load.daily_history_days = 1;
    shared.hass = { ...hass, states: { ...states } };
    const qualified = {
      dayValue: dayFact?.querySelector("strong")?.textContent || "",
      dayNote: dayFact?.querySelector("small")?.textContent || "",
      nightValue: nightFact?.querySelector("strong")?.textContent || "",
      nightNote: nightFact?.querySelector("small")?.textContent || "",
    };
    states[planEntity] = state(planEntity, "fallback", {
      recorder_load_history_days: 0,
      recorder_load_qualification_date: "2026-09-22",
      recorder_load_qualification_reason: "partial_phase_counter",
      recorder_load_phase_quality: {
        "2026-09-22": { l1: "missing_start_edge", l2: "missing_start_edge", l3: "missing_start_edge" },
      },
      recorder_night_history_days: 0,
      recorder_night_qualification_date: null,
      recorder_night_qualification_reason: "unknown",
      recorder_night_quality: {},
      load_history_read_status: "extended_complete",
      load_profile_generated_at: null,
      load_model_source: "configured_daily_fallback",
    });
    states[sharedEntity].attributes.load.daily_history_days = 0;
    shared.hass = { ...hass, states: { ...states } };
    return {
      generalSupport,
      policySupport,
      diagnosed,
      unknown,
      qualified,
      serviceCalls,
      overflow: Math.max(
        general.shadowRoot.querySelector(".root").scrollWidth - general.shadowRoot.querySelector(".root").clientWidth,
        policy.shadowRoot.querySelector(".root").scrollWidth - policy.shadowRoot.querySelector(".root").clientWidth,
        shared.shadowRoot.querySelector("ha-card").scrollWidth - shared.shadowRoot.querySelector("ha-card").clientWidth,
      ),
    };
  }, language);

  for (const support of [result.generalSupport, result.policySupport]) {
    check(support.count === 1, "BuyCoffee link duplicated");
    check(support.href === "https://buycoffee.to/kaluzaaa", `unexpected href ${support.href}`);
    check(support.target === "_blank", "BuyCoffee target is not _blank");
    check(support.rel === "noopener noreferrer", `unexpected rel ${support.rel}`);
    check(support.text === support.expectedText, `unexpected copy ${support.text}`);
    check(support.beforeDiagnostics, "BuyCoffee is not immediately before diagnostics");
    check(support.focused, "BuyCoffee cannot receive keyboard focus");
    check(support.stable, "hass updates replaced the BuyCoffee link");
  }
  check(result.serviceCalls === 0, "BuyCoffee click called a Home Assistant service");
  check(result.overflow <= 1, `settings overflow by ${result.overflow}px`);
  if (language === "pl") {
    check(result.diagnosed.dayValue.startsWith("0 "), `unexpected day count ${result.diagnosed.dayValue}`);
    check(result.diagnosed.dayNote.includes("brak początku doby") && result.diagnosed.dayNote.includes("22.09"), `missing day reason: ${result.diagnosed.dayNote}`);
    check(result.diagnosed.nightNote.includes("zbieranie bieżącego okna"), `open night confused with failure: ${result.diagnosed.nightNote}`);
    check(result.unknown.dayNote.includes("stan kwalifikacji nieznany"), `missing unknown day state: ${result.unknown.dayNote}`);
    check(result.unknown.nightNote.includes("stan kwalifikacji nieznany"), `missing unknown night state: ${result.unknown.nightNote}`);
    check(result.qualified.dayValue.startsWith("1 ") && result.qualified.dayNote === "", "qualified day did not increment exactly once");
    check(result.qualified.nightValue.startsWith("1 ") && result.qualified.nightNote === "", "qualified night did not increment exactly once");
  }
  if (EVIDENCE) {
    fs.mkdirSync(EVIDENCE, { recursive: true });
    await page.screenshot({ path: path.join(EVIDENCE, screenshotName), fullPage: true });
  }
}

async function main() {
  const source = fs.readFileSync(CARD);
  const server = http.createServer((request, response) => {
    if (new URL(request.url, "http://127.0.0.1").pathname === "/card.js") {
      response.writeHead(200, { "content-type": "text/javascript; charset=utf-8", "cache-control": "no-store" });
      response.end(source);
      return;
    }
    response.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    response.end(HTML);
  });
  let browser;
  try {
    const port = await listen(server);
    browser = await chromium.launch({ headless: true, channel: "chrome" });
    for (const [viewport, language, screenshot] of [
      [{ width: 390, height: 844 }, "pl", "load-buycoffee-mobile-pl.png"],
      [{ width: 1280, height: 900 }, "en", "load-buycoffee-desktop-en.png"],
    ]) {
      const context = await browser.newContext({ viewport, colorScheme: "dark", reducedMotion: "reduce" });
      const page = await context.newPage();
      await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "networkidle" });
      await verify(page, language, screenshot);
      await context.close();
    }
    console.log("LOAD diagnostics + BuyCoffee Playwright: PASS (PL/EN, mobile/desktop, focus, order, no service calls, stable DOM)");
  } finally {
    if (browser) await browser.close();
    await close(server);
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
