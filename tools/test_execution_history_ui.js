"use strict";
// Focused, offline history-panel tests. No browser or Home Assistant connection.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(path.join(__dirname, "../home_assistant/www/hoymiles-rce-chart-card.js"), "utf8");
const start = source.indexOf("class HoymilesExecutionHistoryPanel {");
const end = source.indexOf("class HoymilesAuroraVariantAEmsPageCard", start);
const context = vm.createContext({
  Intl, Date, Number, Math, Set, Object,
  hoymilesEscape: (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
  HOYMILES_SUPERVISOR_LIFECYCLE_REASON_COPY: {},
  HOYMILES_SUPERVISOR_REASON_COPY: {required_energy_restore: {pl: "Uzupełnienie energii", en: "Restore energy"}},
  hoymilesPlannerFinite: (value) => typeof value === "number" && Number.isFinite(value),
  hoymilesPlannerWarsawMidnightAfter: (start, day) => new Date(start + day * 86400000).toISOString(),
});
vm.runInContext(source.slice(start, end) + "\nthis.Panel = HoymilesExecutionHistoryPanel;", context);
const chartStart = source.indexOf("  _chartSvg(slots, retained = false, recorded = null)", end);
const chartEnd = source.indexOf("\n  _policyPlanState(", chartStart);
vm.runInContext(`this.Chart = class {${source.slice(chartStart, chartEnd)}}`, context);
const payload = () => ({
  start: 1789030000, end: 1789202800, sources: {soc: "sensor.actual_soc"}, missing: [],
  series: Object.fromEntries(["soc", "pv", "load", "battery", "grid"].map((role) => [role,
    Array.from({length: 576}, (_, index) => ({time: 1789030000 + index * 300, value: index === 4 ? null : role === "soc" ? 50 : 0, positive_kwh: .1, negative_kwh: .2}))])),
  executions: [{start: 1789202500, end: 1789202700, policy: "tariff", action: "tariff_battery_charge"}],
  summary: {tariff_charge: {minimum_kwh: 1, maximum_kwh: 2, covered_seconds: 60, execution_seconds: 120}},
  events: [{start: 1789202500, end: 1789202700, policy: "tariff", action: "<script>", confirmed: true,
    decision_reasons: ["required_energy_restore"], readback: "confirmed", physical: "confirmed", transaction_id: "<private>"}],
});
function fixture(callApi) {
  const elements = new Map(["[data-history-chart-selected-line]", "[data-history-chart-selected-dot]", "[data-history-chart-inspector]"].map((selector) =>
    [selector, {innerHTML: "", attributes: {}, setAttribute(key, value) { this.attributes[key] = value; }}]));
  const listeners = {};
  const root = {innerHTML: "", addEventListener(type, fn) { listeners[type] = fn; }, querySelector(selector) { return elements.get(selector); }, querySelectorAll() { return []; }};
  const host = {isConnected: true, _language: () => "pl", _actionLabel: (action) => action,
    _config: {supervisor_entity: "sensor.renamed_supervisor"}, _hass: {callApi, callService() { throw new Error("History attempted a write"); }},
    _chartSvg: context.Chart.prototype._chartSvg,
    _copy: () => ({expectedSoc: "Przewidywany SOC", chartSelfUse: "Brak komendy", gridImport: "Import"}),
    _number: (n) => n === null ? "—" : n, _formatTime: (t) => new Date(t).toISOString(),
    _formatEnergy: (n) => `${n} kWh`,
    _hourlyModel() { throw Error("History attempted to build a plan"); },
    _chartAction() { throw Error("History attempted to use live state"); },
  };
  return {panel: new context.Panel(host, root), root, host, elements, listeners};
}

(async () => {
  const calls = [];
  const one = fixture(async (...args) => { calls.push(args); return payload(); });
  assert.equal(calls.length, 0, "No history read until requested");
  await one.panel.load();
  assert.deepEqual(calls, [["GET", "hoymiles_hit_modbus/execution-history?entity_id=sensor.renamed_supervisor"]]);
  assert.match(one.root.innerHTML, /viewBox="0 0 1120 480"/);
  assert.match(one.root.innerHTML, /data-history-chart-slot/);
  assert.equal((one.root.innerHTML.match(/class="soc-line"/g) || []).length, 2, "SOC does not bridge a missing bin");
  assert.equal((one.root.innerHTML.match(/data-history-summary=/g) || []).length, 4);
  assert.match(one.root.innerHTML, /1–2 kWh/);
  assert.match(one.root.innerHTML, /niepełne dane/);
  assert.equal(one.root.innerHTML.match(/Przewidywany SOC|data-chart-slot|NaN|undefined|eh-event/), null, "Recorded markup must not contain plan copy or invalid values");
  one.panel.select(575);
  assert.match(one.elements.get("[data-history-chart-inspector]").innerHTML, /Wykonanie potwierdzone/);
  assert.match(one.elements.get("[data-history-chart-inspector]").innerHTML, /Uzupełnienie energii/);
  assert.doesNotMatch(one.elements.get("[data-history-chart-inspector]").innerHTML, /<script>|<private>/);
  one.panel.select(-3); assert.equal(one.panel.index, 0);
  one.panel.select(999); assert.equal(one.panel.index, 575);
  let prevented = false;
  one.listeners.keydown({target: {closest: () => true}, key: "ArrowLeft", preventDefault() { prevented = true; }});
  assert.equal(one.panel.index, 574); assert(prevented);
  let resolve;
  let count = 0;
  const delayed = fixture(() => { count++; return new Promise((done) => { resolve = done; }); });
  const pending = delayed.panel.load();
  await delayed.panel.load(); assert.equal(count, 1, "No overlapping requests");
  delayed.panel.disconnect(); resolve(payload()); await pending;
  assert.equal(delayed.panel.data, null, "Ignore detached/stale response");
  const unavailable = fixture(async () => { throw new Error("unavailable"); });
  await unavailable.panel.load(); assert.equal(unavailable.panel.error, true);
  assert.match(unavailable.root.innerHTML, /data-history-retry/);
  for (const [body, pl, en] of [
    ["history_timeout", /nie zdążył/, /timed out/],
    ["history_busy", /poprzedni odczyt/, /previous history request/],
    ["<private error>", /jest niedostępna/, /is unavailable/],
  ]) {
    const failed = fixture(async () => { throw {body}; });
    await failed.panel.load(); assert.match(failed.root.innerHTML, pl);
    failed.host._language = () => "en"; failed.panel.render();
    assert.match(failed.root.innerHTML, en);
    assert.doesNotMatch(failed.root.innerHTML, /<private error>|0 kWh|Brak zapisanych pomiarów/);
    assert.equal(failed.panel.data, null);
  }
  const invalid = fixture(async () => ({...payload(), end: 1}));
  await invalid.panel.load(); assert.equal(invalid.panel.error, true);
  const empty = fixture(async () => ({...payload(), series: {}, events: []}));
  await empty.panel.load(); assert.match(empty.root.innerHTML, /Brak zapisanych pomiarów/);
  one.host._language = () => "en"; one.panel.render();
  assert.match(one.root.innerHTML, /Recorded SOC/);
  assert.match(source, /data-history-toggle/);
  const assets = fs.readFileSync(path.join(__dirname, "../custom_components/hoymiles_hit_modbus/assets.py"), "utf8");
  const bootstrap = fs.readFileSync(path.join(__dirname, "../home_assistant/www/hoymiles-dashboard-strategy.js"), "utf8");
  assert.equal((assets.match(/&history=48h-executed/g) || []).length, 2, "Both the card and extra-module bootstrap must invalidate the previous resource");
  const revision = assets.match(/FRONTEND_ASSET_REVISION = (\d+)/)?.[1];
  assert.ok(Number(revision) >= 121, "History cache invalidation requires revision 121 or newer");
  assert.equal(bootstrap.match(/const frontendRevision = (\d+);/)?.[1], revision,
    "Bootstrap revision must match the published frontend cache revision");
  assert.equal(
    source.match(/static hoymilesFrontendRevision = (\d+);/)?.[1],
    revision,
    "Canonical dashboard strategy revision must match the published frontend cache revision",
  );
  assert.ok(bootstrap.includes(`/local/hoymiles-rce-chart-card.js`), "ES-module bootstrap must retain a deterministic no-script fallback");
  assert.ok(bootstrap.includes("canonicalModuleUrl.search = canonicalQuery"), "All bootstrap paths must apply the canonical version and history query");
  assert.ok(bootstrap.includes("history=48h-executed"), "Bootstrap must preserve the executed-history cache namespace");
  const inspectableBootstrap = bootstrap.replace(
    "let canonicalModulePromise;",
    "window.__canonicalModuleUrl = canonicalModuleUrl.href; let canonicalModulePromise;",
  );
  const runBootstrap = (candidate, {currentScript = null, scripts = [], registry = new Map()} = {}) => {
    const window = {location: {origin: "https://homeassistant.example"}};
    const sandbox = {
      URL,
      HTMLElement: class {},
      document: {currentScript, scripts},
      window,
      customElements: {
        define(name, constructor) {
          assert.equal(registry.has(name), false, `duplicate definition of ${name}`);
          registry.set(name, constructor);
        },
        get(name) { return registry.get(name); },
      },
    };
    vm.runInNewContext(candidate, sandbox);
    return {window, registry};
  };
  const selected = runBootstrap(inspectableBootstrap, {
    currentScript: {src: "https://unrelated.example/cards/arbitrary.js?v=999"},
    scripts: [
      {src: "https://old.example/local/hoymiles-dashboard-strategy.js?v=1.5.8.78"},
      {src: `https://new.example/local/hoymiles-dashboard-strategy.js?v=1.5.8.${revision}`},
    ],
  });
  assert.equal(
    selected.window.__canonicalModuleUrl,
    `https://new.example/local/hoymiles-rce-chart-card.js?v=1.5.8.${revision}&history=48h-executed`,
    "Unrelated currentScript and DOM order must not select an old module",
  );
  const noScript = runBootstrap(inspectableBootstrap);
  assert.equal(
    noScript.window.__canonicalModuleUrl,
    `https://homeassistant.example/local/hoymiles-rce-chart-card.js?v=1.5.8.${revision}&history=48h-executed`,
    "ES-module currentScript=null without matching DOM must use the canonical fallback",
  );
  const staleBootstrap = bootstrap.replace(
    `const frontendRevision = ${revision};`,
    "const frontendRevision = 78;",
  );
  assert.notEqual(staleBootstrap, bootstrap, "Old-bootstrap fixture must actually change the revision");
  for (const [first, second, label] of [
    [staleBootstrap, bootstrap, "old then new"],
    [bootstrap, staleBootstrap, "new then old"],
  ]) {
    const registry = new Map();
    const initial = runBootstrap(first, {registry}).registry.get(
      "ll-strategy-dashboard-hoymiles-hit-xxl-g3",
    );
    runBootstrap(second, {registry});
    const final = registry.get("ll-strategy-dashboard-hoymiles-hit-xxl-g3");
    assert.equal(final, initial, `${label}: immutable custom element must not be redefined`);
    assert.equal(final.hoymilesFrontendRevision, Number(revision), `${label}: strategy must not downgrade`);
  }
  assert.match(source, /\? "Aktualnie" : "Current"/);
  console.log("Execution history UI: PASS (shared plan renderer, 4 energy summaries, lazy GET, evidence, gaps, keyboard, escaping, stale response, error/empty, PL/EN)");
})().catch((error) => { console.error(error); process.exitCode = 1; });
