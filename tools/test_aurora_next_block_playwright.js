"use strict";

const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright");

const root = path.resolve(process.env.HOYMILES_UI_TEST_ROOT || process.cwd());
const cardPath = path.join(root, "home_assistant", "www", "hoymiles-rce-chart-card.js");
const outputDir = path.resolve(
  process.env.HOYMILES_NEXT_BLOCK_OUTPUT || path.join(os.tmpdir(), "hoymiles-next-block-01"),
);
fs.mkdirSync(outputDir, { recursive: true });

function check(condition, message) {
  if (!condition) throw new Error(message);
}

const html = String.raw`<!doctype html>
<html lang="pl">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
      :root { color-scheme:dark; --primary-background-color:#07121f; --card-background-color:#101b27; --primary-text-color:#f3f7fa; --secondary-text-color:#8fa4b5; }
      html,body { background:#080d13; margin:0; min-height:100%; width:100%; }
      #surface { height:900px; overflow:auto; width:100%; }
      #after { background:#162331; color:#d9f6ff; height:48px; padding:12px; }
    </style>
    <script>
      window.customCards = [];
      window.__clockBase = Date.parse("2026-09-15T18:00:00+02:00");
      window.__clockStarted = performance.now();
      Date.now = function () { return window.__clockBase + performance.now() - window.__clockStarted; };
      window.loadCardHelpers = async function () {
        return { createCardElement: function (config) {
          var element = document.createElement(String(config.type || "").replace(/^custom:/, ""));
          if (element.setConfig) element.setConfig(config);
          return element;
        }};
      };
    </script>
    <script type="module" src="/card.js"></script>
  </head>
  <body><div id="surface"><hoymiles-aurora-overview-card id="card"></hoymiles-aurora-overview-card><div id="after">NEXT ELEMENT</div></div></body>
</html>`;

const server = http.createServer((request, response) => {
  if (request.url === "/card.js") {
    response.writeHead(200, { "content-type": "text/javascript; charset=utf-8", "cache-control": "no-store" });
    response.end(fs.readFileSync(cardPath));
    return;
  }
  response.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
  response.end(html);
});

function listen() {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

function closeServer() {
  return new Promise((resolve) => server.close(resolve));
}

async function setup(page, language) {
  await page.waitForFunction(() => customElements.get("hoymiles-aurora-overview-card"));
  await page.evaluate((lang) => {
    const iso = (hour) => `2026-09-15T${String(hour).padStart(2, "0")}:00:00+02:00`;
    window.__slot = function (start, end, energy, soc, noise) {
      return {
        starts_at: iso(start), ends_at: iso(end), selected_policy: "rce", selected_action: "rce_export",
        planned: { grid_kwh_import_positive: -energy },
        soc_equation: { battery_to_grid_kwh: energy, grid_to_battery_kwh: 0, soc_end_percent: soc },
        input_revision: noise, price: noise,
      };
    };
    window.__plan = function (noise) {
      return [
        __slot(17, 18, 0.5, 38, noise), __slot(18, 19, 0.7, 35, noise + 1),
        __slot(19, 20, 0.8, 32, noise + 2), __slot(22, 23, 2.5, 27, noise + 3),
      ];
    };
    window.__hass = function (mode, noise, customSlots) {
      const stamp = new Date(Date.now() + noise).toISOString();
      const make = (value, attributes = {}) => ({ state:String(value), attributes, last_changed:stamp, last_updated:stamp });
      const pending = mode === "pending";
      const timeline = make(pending ? "pending" : "current", pending ? {
        canonical_status:"pending", canonical_blocker_code:"source_recalculation_pending",
        result_current:false, recalculation_pending:true, built_at:"2026-09-15T17:59:00+02:00",
        slots: customSlots || __plan(noise), revision: noise,
      } : {
        result_current:true, recalculation_pending:false, built_at:"2026-09-15T17:59:00+02:00",
        slots: customSlots === undefined ? __plan(noise) : customSlots, revision: noise,
      });
      const states = {
        timeline, supervisor:make("idle", { selected_action:"none", master_stop:{status:"not_requested"} }),
        mode:make("Active", {options:["Off","Active"]}), paused:make("off"), conflict:make("off"), ready:make("on"),
        allow_rce:make("on"), allow_tariff:make("on"), allow_rcm:make("on"), physical:make("self_use"),
        pv:make("4.2", {unit_of_measurement:"kWh"}), forecast:make("8.5", {unit_of_measurement:"kWh"}),
        load:make("6.1", {unit_of_measurement:"kWh"}), average:make("18.5", {unit_of_measurement:"kWh"}),
        soc:make("42", {unit_of_measurement:"%"}), capacity:make("26", {unit_of_measurement:"kWh"}),
        reserve:make("25", {unit_of_measurement:"%"}), grid_import:make("1.5", {unit_of_measurement:"kWh"}),
        grid_export:make("2.2", {unit_of_measurement:"kWh"}), pv_power:make("1200", {unit_of_measurement:"W"}),
        load_power:make("800", {unit_of_measurement:"W"}), grid_power:make("-300", {unit_of_measurement:"W"}),
        battery_power:make("-100", {unit_of_measurement:"W"}),
      };
      return { states, language:lang, locale:{language:lang}, config:{time_zone:"Europe/Warsaw"}, callService:async()=>{ throw new Error("writes forbidden"); } };
    };
    const card = document.getElementById("card");
    card.setConfig({
      language:lang, canonical_timeline_entity:"timeline", supervisor_entity:"supervisor", supervisor_mode_entity:"mode",
      supervisor_paused_entity:"paused", control_conflict_entity:"conflict", readiness_entity:"ready",
      supervisor_allow_rce_entity:"allow_rce", supervisor_allow_tariff_entity:"allow_tariff", supervisor_allow_rcm_entity:"allow_rcm",
      physical_mode_entity:"physical", pv_today_entity:"pv", forecast_today_entity:"forecast", load_today_entity:"load",
      average_load_entity:"average", battery_soc_entity:"soc", battery_capacity_entity:"capacity",
      battery_self_use_floor_entity:"reserve", grid_import_today_entity:"grid_import", grid_export_today_entity:"grid_export",
      pv_entity:"pv_power", load_entity:"load_power", grid_entity:"grid_power", battery_entity:"battery_power",
      health_entities:[], alarm_entities:[], history_entities:[{entity:"pv_power",name:"PV"}],
    });
    card.hass = __hass("current", 0);
  }, language);
  await page.waitForTimeout(80);
}

async function replay(page) {
  return page.evaluate(async () => {
    const card = document.getElementById("card");
    const next = card.shadowRoot.querySelector(".brief-action.next");
    const brief = card.shadowRoot.querySelector(".ems-brief");
    const after = document.getElementById("after");
    const read = () => ({
      kicker:next.querySelector('[data-value="next-range"]').textContent,
      primary:next.querySelector('[data-value="next-primary"]').textContent,
      secondary:next.querySelector('[data-value="next-secondary"]').textContent,
      status:next.querySelector('[data-value="next-status"]').textContent,
      brief:brief.getBoundingClientRect().toJSON(), after:after.getBoundingClientRect().toJSON(),
    });
    const baseline = read();
    let mutations = 0;
    let textMutations = 0;
    let blockEmptyFlips = 0;
    let maxMovement = 0;
    const observer = new MutationObserver((records) => {
      mutations += records.length;
      textMutations += records.filter((record) => record.type === "characterData" || record.type === "childList").length;
    });
    observer.observe(next, { subtree:true, childList:true, characterData:true, attributes:true });
    for (let index = 1; index <= 100; index += 1) {
      for (const mode of ["pending", "current"]) {
        card.hass = __hass(mode, index);
        const sample = read();
        if (sample.primary !== baseline.primary || sample.kicker !== baseline.kicker || sample.secondary !== baseline.secondary) blockEmptyFlips += 1;
        maxMovement = Math.max(
          maxMovement,
          Math.abs(sample.brief.x - baseline.brief.x), Math.abs(sample.brief.y - baseline.brief.y),
          Math.abs(sample.brief.width - baseline.brief.width), Math.abs(sample.brief.height - baseline.brief.height),
          Math.abs(sample.after.x - baseline.after.x), Math.abs(sample.after.y - baseline.after.y),
        );
      }
    }
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    observer.disconnect();
    const final = read();
    return { baseline, final, mutations, textMutations, blockEmptyFlips, maxMovement };
  });
}

async function timerAndLifecycle(page) {
  return page.evaluate(async () => {
    const card = document.getElementById("card");
    const primary = () => card.shadowRoot.querySelector('[data-value="next-primary"]').textContent;
    const start = Date.now() + 120;
    const end = start + 300;
    const boundarySlot = [{
      starts_at:new Date(start).toISOString(), ends_at:new Date(end).toISOString(),
      selected_policy:"rce", selected_action:"rce_export", planned:{grid_kwh_import_positive:-0.2},
      soc_equation:{battery_to_grid_kwh:0.2,grid_to_battery_kwh:0,soc_end_percent:40},
    }];
    card.hass = __hass("current", 500, boundarySlot);
    const before = primary();
    const timerBefore = card._nextBoundaryTimer !== null;
    await new Promise((resolve) => setTimeout(resolve, 230));
    const after = primary();
    const parent = card.parentNode;
    parent.removeChild(card);
    const disconnected = card._nextBoundaryTimer === null && card._nextVisibilityListening === false;
    parent.insertBefore(card, document.getElementById("after"));
    card.hass = __hass("current", 501);
    const reconnected = card._nextVisibilityListening === true && card._nextBoundaryTimer !== null;
    let visibilityPatches = 0;
    const originalPatch = card._patch.bind(card);
    card._patch = function () { visibilityPatches += 1; return originalPatch(); };
    document.dispatchEvent(new Event("visibilitychange"));
    card._patch = originalPatch;
    return { before, after, timerBefore, disconnected, reconnected, visibilityPatches };
  });
}

(async () => {
  const port = await listen();
  const browserExecutable = process.env.HOYMILES_BROWSER_EXECUTABLE
    || (fs.existsSync("C:/Program Files/Google/Chrome/Application/chrome.exe")
      ? "C:/Program Files/Google/Chrome/Application/chrome.exe"
      : undefined);
  const browser = await chromium.launch({ headless:true, executablePath:browserExecutable });
  const results = [];
  let videoPath = null;
  try {
    for (const language of ["pl", "en"]) {
      for (const viewport of [{width:320,height:844},{width:390,height:844},{width:1280,height:900}]) {
        const recordVideo = language === "pl" && viewport.width === 390
          ? { dir:outputDir, size:viewport }
          : undefined;
        const context = await browser.newContext({ viewport, recordVideo });
        const page = await context.newPage();
        await page.goto(`http://127.0.0.1:${port}/`, { waitUntil:"networkidle" });
        await setup(page, language);
        const replayResult = await replay(page);
        check(replayResult.blockEmptyFlips === 0, `${language}/${viewport.width}: block text flickered`);
        check(replayResult.maxMovement <= 1, `${language}/${viewport.width}: layout moved ${replayResult.maxMovement}px`);
        check(replayResult.baseline.primary === (language === "pl" ? "Sprzedaż dynamiczna" : "Dynamic sales"), `${language}/${viewport.width}: action text`);
        check(replayResult.baseline.kicker.includes("22:00–23:00"), `${language}/${viewport.width}: whole range`);
        check(replayResult.baseline.secondary.includes(language === "pl" ? "Plan 2,5 kWh · cel 27% SOC" : "Plan 2.5 kWh · target 27% SOC"), `${language}/${viewport.width}: aggregate details`);
        const screenshot = path.join(outputDir, `next-block-${language}-${viewport.width}.png`);
        await page.screenshot({ path:screenshot, fullPage:true });
        let lifecycle = null;
        if (language === "pl" && viewport.width === 390) {
          lifecycle = await timerAndLifecycle(page);
          check(lifecycle.before === "Sprzedaż dynamiczna", "timer fixture starts as future block");
          check(lifecycle.after === "Brak kolejnych działań w planie", "time boundary updates without hass event");
          check(lifecycle.timerBefore && lifecycle.disconnected && lifecycle.reconnected, "timer cleanup/reconnect contract");
          check(lifecycle.visibilityPatches === 1, "visibility recovery performs one patch");
        }
        results.push({ language, viewport, screenshot, ...replayResult, lifecycle });
        const video = page.video();
        await context.close();
        if (video) videoPath = await video.path();
      }
    }
  } finally {
    await browser.close();
    await closeServer();
  }
  const artifact = {
    schema:"NEXT-BLOCK-01-playwright-v1", generated_at:new Date().toISOString(),
    source:path.relative(root, cardPath).replaceAll("\\", "/"), iterations:100,
    results, video:videoPath,
  };
  const jsonPath = path.join(outputDir, "next-block-playwright.json");
  fs.writeFileSync(jsonPath, JSON.stringify(artifact, null, 2) + "\n");
  console.log(`NEXT-BLOCK-01 Playwright: ${results.length} viewport/language cases PASS`);
  console.log(`Evidence: ${jsonPath}`);
  if (videoPath) console.log(`Video: ${videoPath}`);
})().catch(async (error) => {
  console.error(error);
  try { await closeServer(); } catch {}
  process.exitCode = 1;
});
