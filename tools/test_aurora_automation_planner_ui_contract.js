const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ALLOW_GENERATED_DRIFT =
  process.env.HOYMILES_UI_ALLOW_GENERATED_DRIFT === "1";

const root = path.resolve(process.env.HOYMILES_UI_TEST_ROOT || process.cwd());
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), "utf8");
const cardPath = "home_assistant/www/hoymiles-rce-chart-card.js";
const dashboardPath = "dashboard_hoymiles.yaml";
const strategyPath = "home_assistant/www/hoymiles-dashboard-strategy.js";
const cardSource = read(cardPath);
const dashboardSource = read(dashboardPath);
const strategySource = read(strategyPath);
const assetsSource = read("custom_components/hoymiles_hit_modbus/assets.py");
const assetGeneratorSource = read("tools/build_hacs_assets.py");
const dashboardLf = dashboardSource.replaceAll("\r", "");

const EXPECTED_BINDINGS = Object.freeze({
  baseline_timeline_entity:
    "sensor.hoymiles_ems_baseline_energy_timeline",
  canonical_timeline_entity:
    "sensor.hoymiles_hit_ems_supervisor_canonical_plan",
  rce_timeline_entity: "sensor.hoymiles_hit_rce_automation_plan_timeline",
  tariff_timeline_entity: "sensor.hoymiles_hit_tariff_automation_plan_timeline",
  rcm_timeline_entity: "sensor.hoymiles_hit_rcm_automation_plan_timeline",
  rce_plan_entity: "sensor.hoymiles_hit_rce_optimized_plan",
  tariff_plan_entity: "sensor.hoymiles_hit_tariff_charge_plan",
  rcm_plan_entity: "sensor.hoymiles_hit_rcm_voltage_plan",
  supervisor_entity: "sensor.hoymiles_hit_ems_supervisor",
  physical_mode_entity: "sensor.hoymiles_ems_hardware_mode",
  control_conflict_entity: "binary_sensor.hoymiles_ems_control_conflict",
  rce_enabled_entity: "input_boolean.hoymiles_rce_discharge_enabled",
  rce_active_entity: "input_boolean.hoymiles_rce_discharge_active",
  rce_expert_entity: "input_boolean.hoymiles_rce_advanced_view",
  tariff_enabled_entity: "input_boolean.hoymiles_tariff_charge_enabled",
  tariff_active_entity: "input_boolean.hoymiles_tariff_charge_active",
  tariff_active_action_entity: "input_text.hoymiles_tariff_active_action",
  tariff_expert_entity: "input_boolean.hoymiles_tariff_advanced_view",
  rcm_enabled_entity: "input_boolean.hoymiles_rcm_enabled",
  rcm_active_entity: "input_boolean.hoymiles_rcm_active",
  rcm_export_active_entity: "input_boolean.hoymiles_rcm_export_control_active",
  rcm_pre_discharge_active_entity:
    "input_boolean.hoymiles_rcm_pre_discharge_active",
  rcm_expert_entity: "input_boolean.hoymiles_rcm_advanced_view",
});

const ledger = {
  textWrites: 0,
  attributeWrites: 0,
  classWrites: 0,
  childListWrites: 0,
  focusCalls: 0,
  scrollCalls: 0,
  reset() {
    this.textWrites = 0;
    this.attributeWrites = 0;
    this.classWrites = 0;
    this.childListWrites = 0;
    this.focusCalls = 0;
    this.scrollCalls = 0;
  },
  snapshot() {
    return {
      textWrites: this.textWrites,
      attributeWrites: this.attributeWrites,
      classWrites: this.classWrites,
      childListWrites: this.childListWrites,
      focusCalls: this.focusCalls,
      scrollCalls: this.scrollCalls,
    };
  },
};

class FakeStyle {
  constructor() {
    this.values = new Map();
  }
  setProperty(name, value) {
    const normalized = String(value);
    if (this.values.get(name) !== normalized) {
      this.values.set(name, normalized);
      ledger.attributeWrites += 1;
    }
  }
  removeProperty(name) {
    if (this.values.delete(name)) ledger.attributeWrites += 1;
  }
}

class FakeClassList {
  constructor(owner) {
    this.owner = owner;
  }
  _tokens() {
    return new Set(String(this.owner.className || "").split(/\s+/).filter(Boolean));
  }
  _write(tokens) {
    this.owner.className = [...tokens].join(" ");
  }
  add(...names) {
    const tokens = this._tokens();
    for (const name of names) tokens.add(name);
    this._write(tokens);
  }
  remove(...names) {
    const tokens = this._tokens();
    for (const name of names) tokens.delete(name);
    this._write(tokens);
  }
  toggle(name, force) {
    const tokens = this._tokens();
    const enabled = force === undefined ? !tokens.has(name) : Boolean(force);
    if (enabled) tokens.add(name);
    else tokens.delete(name);
    this._write(tokens);
    return enabled;
  }
  contains(name) {
    return this._tokens().has(name);
  }
}

let fakeDocument;
class FakeElement {
  constructor(tagName = "div") {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = new FakeStyle();
    this._className = "";
    this._textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.value = "";
    this.type = "";
    this.scrollLeft = 0;
    this.scrollTop = 0;
    this.dataset = new Proxy({}, {
      set: (target, key, value) => {
        const normalized = String(value);
        if (target[key] !== normalized) {
          target[key] = normalized;
          ledger.attributeWrites += 1;
        }
        return true;
      },
      deleteProperty: (target, key) => {
        if (Object.prototype.hasOwnProperty.call(target, key)) {
          delete target[key];
          ledger.attributeWrites += 1;
        }
        return true;
      },
    });
    this.classList = new FakeClassList(this);
  }
  set className(value) {
    const normalized = String(value || "");
    if (this._className !== normalized) {
      this._className = normalized;
      ledger.classWrites += 1;
    }
  }
  get className() {
    return this._className;
  }
  set textContent(value) {
    const normalized = String(value ?? "");
    if (this._textContent !== normalized || this.children.length > 0) {
      this._textContent = normalized;
      if (this.children.length > 0) {
        for (const child of this.children) child.parentNode = null;
        this.children = [];
        ledger.childListWrites += 1;
      }
      ledger.textWrites += 1;
    }
  }
  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent || "").join("");
  }
  append(...children) {
    for (const child of children) {
      if (child === null || child === undefined) continue;
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = this;
      this.children.push(child);
      ledger.childListWrites += 1;
    }
  }
  prepend(...children) {
    for (const child of [...children].reverse()) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = this;
      this.children.unshift(child);
      ledger.childListWrites += 1;
    }
  }
  replaceChildren(...children) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    ledger.childListWrites += 1;
    this.append(...children);
  }
  insertBefore(child, reference) {
    if (child.parentNode) child.parentNode.removeChild(child);
    const index = reference ? this.children.indexOf(reference) : -1;
    child.parentNode = this;
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
    ledger.childListWrites += 1;
    return child;
  }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) {
      this.children.splice(index, 1);
      child.parentNode = null;
      ledger.childListWrites += 1;
    }
    return child;
  }
  remove() {
    this.parentNode?.removeChild(this);
  }
  setAttribute(name, value) {
    const normalized = String(value);
    if (this.attributes.get(name) !== normalized) {
      this.attributes.set(name, normalized);
      ledger.attributeWrites += 1;
    }
  }
  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }
  hasAttribute(name) {
    return this.attributes.has(name);
  }
  removeAttribute(name) {
    if (this.attributes.delete(name)) ledger.attributeWrites += 1;
  }
  addEventListener(type, listener) {
    const values = this.listeners.get(type) || [];
    values.push(listener);
    this.listeners.set(type, values);
  }
  removeEventListener(type, listener) {
    const values = this.listeners.get(type) || [];
    this.listeners.set(type, values.filter((value) => value !== listener));
  }
  dispatch(type, init = {}) {
    const event = {
      type,
      target: this,
      currentTarget: this,
      key: init.key,
      clientX: init.clientX ?? 0,
      clientY: init.clientY ?? 0,
      pointerId: init.pointerId ?? 1,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {},
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
    return !event.defaultPrevented;
  }
  click() {
    if (this.disabled) return false;
    return this.dispatch("click");
  }
  focus() {
    ledger.focusCalls += 1;
    fakeDocument.activeElement = this;
  }
  scrollIntoView() {
    ledger.scrollCalls += 1;
  }
  scroll() {
    ledger.scrollCalls += 1;
  }
  scrollTo() {
    ledger.scrollCalls += 1;
  }
  scrollBy() {
    ledger.scrollCalls += 1;
  }
  matches(selector) {
    if (selector.startsWith(".")) return this.classList.contains(selector.slice(1));
    if (selector.startsWith("#")) return (this.id || this.getAttribute("id")) === selector.slice(1);
    const dataMatch = selector.match(/^\[data-([a-z0-9-]+)(?:=["']?([^\]"']+)["']?)?\]$/i);
    if (dataMatch) {
      const key = dataMatch[1].replace(/-([a-z])/g, (_all, char) => char.toUpperCase());
      return dataMatch[2] === undefined
        ? Object.prototype.hasOwnProperty.call(this.dataset, key)
        : this.dataset[key] === dataMatch[2];
    }
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) {
    return walk(this).filter((node) => node !== this && node.matches?.(selector));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  getBoundingClientRect() {
    return { x: 0, y: 0, top: 0, left: 0, right: 44, bottom: 44, width: 44, height: 44 };
  }
}

class FakeSvgElement extends FakeElement {
  set className(_value) {
    throw new TypeError("SVGElement.className is read-only in the browser contract");
  }
  get className() {
    return { baseVal: this._className };
  }
}

class TestHTMLElement extends FakeElement {
  constructor() {
    super("hoymiles-test-host");
    this.isConnected = true;
  }
  attachShadow() {
    if (!this.shadowRoot) this.shadowRoot = new FakeElement("shadow-root");
    return this.shadowRoot;
  }
  dispatchEvent() {
    return true;
  }
}

const registry = new Map();
let rafId = 0;
let rafQueue = new Map();
let rafCallbackCount = 0;
const requestAnimationFrame = (callback) => {
  const id = ++rafId;
  rafQueue.set(id, callback);
  return id;
};
const cancelAnimationFrame = (id) => rafQueue.delete(id);
const flushAnimationFrames = () => {
  const current = [...rafQueue.entries()];
  rafQueue = new Map();
  for (const [, callback] of current) {
    rafCallbackCount += 1;
    callback(1_000 + rafCallbackCount * 16);
  }
  return current.length;
};
let timerId = 0;
const timerQueue = new Map();
const setTimeout = (callback, delay = 0) => {
  const id = ++timerId;
  timerQueue.set(id, { callback, delay: Number(delay) || 0 });
  return id;
};
const clearTimeout = (id) => timerQueue.delete(id);

fakeDocument = {
  documentElement: { lang: "pl", clientWidth: 390, scrollWidth: 390 },
  activeElement: null,
  createElement: (tagName) => new FakeElement(tagName),
  createElementNS: (_namespace, tagName) => new FakeSvgElement(tagName),
  createTextNode: (value) => {
    const node = new FakeElement("#text");
    node._textContent = String(value);
    return node;
  },
};
const fakeWindow = {
  customCards: [],
  scrollY: 0,
  innerWidth: 390,
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
};
let fetchCount = 0;
let webSocketCount = 0;
const context = {
  console,
  Date,
  Event: class {},
  CustomEvent: class {},
  HTMLElement: TestHTMLElement,
  Intl,
  URL,
  AbortController,
  document: fakeDocument,
  window: fakeWindow,
  customElements: {
    define(name, constructor) {
      if (!registry.has(name)) registry.set(name, constructor);
    },
    get(name) {
      return registry.get(name);
    },
    async whenDefined() {},
  },
  requestAnimationFrame,
  cancelAnimationFrame,
  setTimeout,
  clearTimeout,
  ResizeObserver: class {
    constructor(callback) { this.callback = callback; }
    observe() {}
    disconnect() {}
  },
  MutationObserver: class {
    observe() {}
    disconnect() {}
    takeRecords() { return []; }
  },
  fetch: async () => {
    fetchCount += 1;
    return { ok: true, json: async () => ({ views: [] }) };
  },
  WebSocket: class {
    constructor() {
      webSocketCount += 1;
      throw new Error("AP-3B must not create a WebSocket");
    }
  },
};
context.globalThis = context;
vm.runInNewContext(
  `${cardSource.replaceAll(
    "import.meta.url",
    JSON.stringify("https://homeassistant.example/local/hoymiles-rce-chart-card.js?v=1.5.8.106"),
  )}\nglobalThis.__hoymilesDecorateDashboard = hoymilesDecorateDashboard;`,
  context,
  { filename: cardPath },
);

const missing = [];
if (!registry.get("hoymiles-automation-planner-card")) {
  missing.push("custom element absent");
}
if (!/\n  -(?:\n    | )title: EMS\n    path: plan-automatyki\n/.test(dashboardLf)) {
  missing.push("dedicated dashboard view absent");
}
if (!cardSource.includes("class HoymilesAutomationPlannerCard")) {
  missing.push("static shell absent");
}
if (!cardSource.includes("_semanticFingerprint")) {
  missing.push("semantic fingerprint absent");
}
if (!cardSource.includes("_reconcileLane")) {
  missing.push("100/100/1 DOM-preservation contract absent");
}
if (!cardSource.includes("touch-action: pan-x pan-y")) {
  missing.push("mobile scroll preservation absent");
}
if (missing.length) {
  console.error("AURORA AP-3B UI CONTRACT: RED");
  for (const reason of missing) console.error(`- ${reason}`);
  process.exitCode = 1;
  return;
}

let checks = 0;
const check = (condition, message) => {
  checks += 1;
  if (!condition) throw new Error(message);
};
const equal = (actual, expected, message) => {
  check(JSON.stringify(actual) === JSON.stringify(expected), `${message}: ${JSON.stringify(actual)}`);
};
const clone = (value) => JSON.parse(JSON.stringify(value));
function walk(rootNode) {
  const result = [];
  const visit = (node) => {
    if (!node || typeof node !== "object") return;
    result.push(node);
    for (const child of node.children || []) visit(child);
  };
  visit(rootNode);
  return result;
}
const byClass = (rootNode, className) =>
  walk(rootNode).filter((node) => node.classList?.contains(className));

const policyShape = (policyId, seed = 0) => {
  if (policyId === "rce") {
    return {
      sell_price_pln_kwh: 0.82 + seed,
      planned_export_kwh: 1.2 + seed,
      planned_battery_withdrawal_kwh: 1.3 + seed,
      target_discharge_kw: 2.4,
      command_discharge_power_percent: 40,
      target_tolerance_kw: 0.1,
      expected_revenue_pln: 0.98 + seed,
    };
  }
  if (policyId === "tariff") {
    return {
      buy_price_pln_kwh: 0.31 + seed,
      tariff_zone: "cheap",
      need_class: "required_energy",
      planned_import_kwh: 1.5 + seed,
      stored_energy_kwh: 1 + seed,
      direct_load_kwh: 0.5,
      planned_charge_kw: 2.0,
      expected_cost_pln: 0.47 + seed,
      expected_saving_pln: null,
    };
  }
  return {
    voltage_risk_code: "high",
    planned_export_limit_percent: 35,
    planned_export_limit_kw: null,
    recommended_charge_power_kw: null,
    planned_pre_discharge_kw: 1.1,
    planned_pre_discharge_kwh: 0.55 + seed,
    planned_pre_discharge_stored_kwh: 0.6 + seed,
    action_start_offset_seconds: 0,
    action_end_offset_seconds: 900,
    headroom_shortfall_kwh: 0.8 + seed,
    control_mode: "grid_discharge_preparation",
  };
};

const actionFor = (policyId) => ({
  rce: "export",
  tariff: "battery_charge",
  rcm: "grid_discharge_preparation",
})[policyId];

function point(policyId, start, minutes, selected = true, seed = 0) {
  const end = new Date(new Date(start).getTime() + minutes * 60_000).toISOString();
  const batteryKw = policyId === "rce" ? -2.4 : policyId === "tariff" ? 2.0 : -1.1;
  const gridKw = policyId === "rce" ? -1.6 : policyId === "tariff" ? 2.7 : -1.2;
  const result = {
    start,
    end,
    kind: "forecast_plan",
    pv_kw: 0.8,
    load_kw: 0.7,
    battery_kw: batteryKw,
    grid_kw: gridKw,
    grid_import_kw: Math.max(gridKw, 0),
    grid_export_kw: Math.max(-gridKw, 0),
    soc_percent: 74 - seed,
    baseline_soc_percent: 78,
    protected_soc_floor_percent: 25,
    action_code: actionFor(policyId),
    active: false,
    selected,
    quality: "complete",
    policy: policyShape(policyId, seed),
  };
  if (policyId === "tariff") result.target_soc_percent = 82;
  if (policyId === "rcm") {
    result.target_soc_percent = 70;
    result.required_headroom_kwh = 2.5;
  }
  return result;
}

function timeline(policyId, inputRevision, planRevision, start, count = 2) {
  const minutes = policyId === "rcm" ? 15 : 30;
  const points = [];
  let cursor = start;
  for (let index = 0; index < count; index += 1) {
    const value = point(policyId, cursor, minutes, index === 0, index / 100);
    points.push(value);
    cursor = value.end;
  }
  const planEntity = EXPECTED_BINDINGS[`${policyId}_plan_entity`];
  return {
    state: "current",
    attributes: {
      schema_version: 2,
      policy_id: policyId,
      config_entry_id: "entry-ap3b",
      generated_at: "2026-08-30T13:55:00.000Z",
      horizon_start: points[0].start,
      horizon_end: points.at(-1).end,
      actual_until: null,
      forecast_from: points[0].start,
      timezone: "Europe/Warsaw",
      slot_minutes: minutes,
      point_count: points.length,
      input_revision: inputRevision,
      plan_revision: planRevision,
      plan_revision_scope: "runtime",
      result_current: true,
      recalculation_pending: false,
      quality: "complete",
      blocker_code: null,
      active_scope: "publication_snapshot",
      active_observed_at: "2026-08-30T13:55:00.000Z",
      plan_entity_id: planEntity,
      current_actual: {
        observed_at: "2026-08-30T13:55:00.000Z",
        pv_kw: 2.1,
        load_kw: 0.7,
        battery_kw: -0.5,
        grid_kw: -0.9,
        soc_percent: 75,
        quality: "complete",
        source_ages_seconds: { pv: 1, load: 1, battery: 1, grid: 1, soc: 1 },
      },
      sources: [{ role: "optimizer", entity_id: planEntity }],
      points,
    },
    last_updated: "2026-08-30T13:55:00.000Z",
  };
}

function candidate(policyId, inputRevision, planRevision) {
  return {
    policy_id: policyId,
    input_revision: inputRevision,
    candidate_revision: planRevision,
    temporal_status: "current",
    allowed_by_user: true,
    enabled: true,
    available: true,
    result_current: true,
    recalculation_pending: false,
    start_eligible: true,
    continuation_eligible: false,
    active_latched: false,
    local_hard_stop: false,
    requested_action: actionFor(policyId),
    actuator_scope: "ems_mode",
    priority_class: "economic",
    need_class: "planned",
    reason_code: "candidate_ready",
    blocked_reason: null,
    rejection_reason: null,
    valid_from: "2026-08-30T14:00:00.000Z",
    valid_until: "2026-08-30T15:00:00.000Z",
    desired_actuator_fingerprint: `fp-${policyId}`,
    requested_mode: policyId === "tariff" ? "grid_charge" : "grid_discharge",
    requested_power_kw: 2,
    requested_energy_kwh: 1,
    target_soc_percent: policyId === "rce" ? null : 80,
    protected_soc_floor_percent: 25,
    economic_value_status: "available",
    economic_contract_id: `contract-${policyId}`,
    economic_basis_fingerprint: `basis-${policyId}`,
    expected_marginal_net_benefit_pln: 1,
    urgency: "normal",
    severity: "normal",
  };
}

function canonicalTimeline(
  start = "2026-08-30T14:00:00.000Z",
  count = 10,
  includeRcmAction = false,
) {
  const capacityKwh = 10;
  const policyActions = {
    rce: "rce_export",
    rcm: "rcm_absorb_pv",
    tariff: "tariff_battery_charge",
  };
  const candidatesFor = (selectedPolicy, selectedAction, index) =>
    ["rce", "rcm", "tariff"].map((policyId) => {
      const selected = policyId === selectedPolicy;
      return {
        policy_id: policyId,
        requested_action: selected ? selectedAction : policyActions[policyId],
        eligible: selected,
        start_eligibility: selected ? "eligible" : "blocked",
        input_revision: 100 + index,
        candidate_revision: 200 + index,
        target_soc_percent:
          selected && policyId !== "rce" ? 80 : null,
        rejected_reasons: selected ? [] : ["lower_priority"],
      };
    });
  const noActionCandidates = (index) =>
    ["rce", "rcm", "tariff"].map((policyId) => ({
      policy_id: policyId,
      requested_action: policyActions[policyId],
      eligible: false,
      start_eligibility: "blocked",
      input_revision: 100 + index,
      candidate_revision: 200 + index,
      target_soc_percent: null,
      rejected_reasons: ["no_action"],
    }));
  const slots = [];
  let cursor = start;
  let soc = 75;
  for (let index = 0; index < count; index += 1) {
    const end = new Date(Date.parse(cursor) + 15 * 60_000).toISOString();
    const selectedPolicy = index === 0
      ? "rce"
      : index === 4
        ? "tariff"
        : includeRcmAction && index === 8
          ? "rcm"
          : "none";
    const selectedAction =
      selectedPolicy === "rce"
        ? "rce_export"
        : selectedPolicy === "tariff"
          ? "tariff_battery_charge"
          : selectedPolicy === "rcm"
            ? "rcm_absorb_pv"
          : "none";
    const batteryKwh =
      index === 0
        ? -0.25
        : index === 4
          ? 0.25
          : index < 8 || (includeRcmAction && index === 8)
            ? 0.025
            : 0;
    const gridKwh =
      index === 0
        ? -0.275
        : index === 4
          ? 0.225
          : index < 8 || (includeRcmAction && index === 8)
            ? 0
            : -0.025;
    const flows = index === 0
      ? {
          pv_to_battery_kwh: 0,
          grid_to_battery_kwh: 0,
          battery_to_load_kwh: 0,
          battery_to_grid_kwh: 0.25,
          losses_kwh: 0,
        }
      : index === 4
        ? {
            pv_to_battery_kwh: 0.025,
            grid_to_battery_kwh: 0.225,
            battery_to_load_kwh: 0,
            battery_to_grid_kwh: 0,
            losses_kwh: 0,
          }
        : index < 8 || (includeRcmAction && index === 8)
          ? {
            pv_to_battery_kwh: 0.025,
            grid_to_battery_kwh: 0,
            battery_to_load_kwh: 0,
            battery_to_grid_kwh: 0,
            losses_kwh: 0,
          }
          : {
              pv_to_battery_kwh: 0,
              grid_to_battery_kwh: 0,
              battery_to_load_kwh: 0,
              battery_to_grid_kwh: 0,
              losses_kwh: 0,
            };
    const socEnd = soc + (100 * batteryKwh) / capacityKwh;
    const candidates = selectedPolicy === "none"
      ? noActionCandidates(index)
      : candidatesFor(selectedPolicy, selectedAction, index);
    const rejected = candidates
      .filter((item) => item.rejected_reasons.length)
      .map((item) => ({
        policy_id: item.policy_id,
        reasons: item.rejected_reasons,
      }));
    const target = selectedAction === "none"
      ? "none"
      : selectedAction === "rcm_absorb_pv"
        ? "battery_charge_limit_306"
        : "ems_block_4300_4306";
    const values = selectedAction === "none"
      ? {}
      : selectedAction === "rcm_absorb_pv"
        ? { battery_charge_limit_percent: 60 }
        : { ems_mode_code: index === 0 ? 3 : 2 };
    slots.push({
      slot_id: `slot-${index}`,
      starts_at: cursor,
      ends_at: end,
      policy_candidates: candidates,
      selected_policy: selectedPolicy,
      selected_action: selectedAction,
      rejected_reasons: rejected,
      start_eligibility: selectedPolicy === "none" ? "not_applicable" : "eligible",
      owner: "none",
      planned: {
        pv_kwh: 0.2,
        load_kwh: 0.175,
        battery_kwh: batteryKwh,
        grid_kwh_import_positive: gridKwh,
      },
      soc_equation: {
        ...flows,
        soc_start_percent: soc,
        soc_end_percent: socEnd,
        energy_balance_residual_kwh: 0,
        continuity_residual_percent: 0,
        system_balance_residual_kwh: 0,
      },
      protected_reserve: {
        percent: 25,
        margin_end_percent: socEnd - 25,
        respected: true,
      },
      command_expectation: {
        target,
        source_generation: selectedAction === "none" ? null : 300 + index,
        values,
      },
      readback_expectation: {
        target,
        newer_than_source_generation: selectedAction !== "none",
        values,
        physical_expectation:
          selectedAction === "rce_export"
            ? "grid_export_and_battery_discharge"
            : selectedAction === "tariff_battery_charge"
              ? "grid_import_and_battery_charge"
              : selectedAction === "rcm_absorb_pv"
                ? "pv_surplus_and_battery_charge"
              : "none",
      },
    });
    soc = socEnd;
    cursor = end;
  }
  return {
    state: "current",
    attributes: {
      schema_version: 1,
      output_only: true,
      built_at: start,
      arbitration_revision: "a".repeat(64),
      usable_capacity_kwh: capacityKwh,
      initial_soc_percent: 75,
      final_soc_percent: soc,
      slots,
      audit: {
        max_continuity_residual_percent: 0,
        max_energy_balance_residual_kwh: 0,
        max_system_balance_residual_kwh: 0,
        reserve_violation_count: 0,
      },
      ledger_revision: "b".repeat(64),
    },
  };
}

function baselineTimeline(canonical = canonicalTimeline()) {
  const points = canonical.attributes.slots.map((slot) => {
    const durationHours =
      (Date.parse(slot.ends_at) - Date.parse(slot.starts_at)) / 3_600_000;
    const gridKwh = slot.planned.grid_kwh_import_positive;
    return {
      start: slot.starts_at,
      end: slot.ends_at,
      pv_kw: slot.planned.pv_kwh / durationHours,
      load_kw: slot.planned.load_kwh / durationHours,
      battery_kw: slot.planned.battery_kwh / durationHours,
      grid_import_kw: Math.max(gridKwh / durationHours, 0),
      grid_export_kw: Math.max(-gridKwh / durationHours, 0),
      soc_start_percent: slot.soc_equation.soc_start_percent,
      soc_end_percent: slot.soc_equation.soc_end_percent,
      reserve_soc_percent: slot.protected_reserve.percent,
      quality: "current",
      blocker_code: null,
      source: { pv: "fixture", load: "fixture", soc: "fixture" },
      provenance: { contract: "baseline_v1", producer: "fixture" },
    };
  });
  return {
    state: "current",
    attributes: {
      schema_version: "2.0",
      timeline_kind: "baseline_self_use",
      output_only: true,
      authority: false,
      battery_sign_convention: "positive_charge",
      current: true,
      generated_at: canonical.attributes.built_at,
      quality: "current",
      blocker_code: null,
      interval_minutes: 15,
      point_count: points.length,
      current_actual_soc_percent: canonical.attributes.initial_soc_percent,
      source: { producer: "fixture" },
      provenance: { contract: "baseline_v1" },
      points,
    },
  };
}

function partialTailBaselineTimeline() {
  const baseline = baselineTimeline();
  const template = baseline.attributes.points[0];
  const startMs = Date.parse(template.start);
  const intervalMinutes = 30;
  const pointCount = 96;
  const completePointCount = 88;
  const points = Array.from({ length: pointCount }, (_, index) => {
    const complete = index < completePointCount;
    const socStart = 75 - index * 0.05;
    return {
      ...clone(template),
      start: new Date(startMs + index * intervalMinutes * 60_000).toISOString(),
      end: new Date(startMs + (index + 1) * intervalMinutes * 60_000).toISOString(),
      pv_kw: complete ? 0.8 : null,
      load_kw: 0.7,
      battery_kw: complete ? 0 : null,
      grid_import_kw: complete ? 0 : null,
      grid_export_kw: complete ? 0 : null,
      soc_start_percent: complete ? socStart : null,
      soc_end_percent: complete ? socStart - 0.05 : null,
      reserve_soc_percent: complete ? 25 : null,
      quality: complete ? "current" : "partial",
      blocker_code: complete ? null : "pv_unavailable",
      source: {
        pv: complete ? "fixture" : null,
        load: "fixture",
        soc: complete ? "fixture" : null,
      },
    };
  });
  return {
    state: "partial",
    attributes: {
      ...baseline.attributes,
      quality: "partial",
      blocker_code: "series_partial",
      interval_minutes: intervalMinutes,
      point_count: pointCount,
      points,
    },
  };
}

function executionHealth(overrides = {}) {
  return {
    schema_version: 1,
    status: "healthy",
    control_status: "healthy",
    reason: "healthy",
    action: "none",
    phase: "idle",
    control_error: null,
    connectivity: "connected",
    last_valid_read_at: "2026-09-12T20:00:00+00:00",
    last_valid_read_age_seconds: 10,
    owner: "none",
    observed_owner: "none",
    owner_conflict: false,
    master_stop_status: "not_requested",
    master_stop_confirmation: "not_requested",
    rollback_status: "not_required",
    physical_verification: "confirmed",
    degradations: {},
    ...overrides,
  };
}

function hassFixture(language = "pl") {
  const rce = timeline("rce", 11, 5, "2026-08-30T14:00:00.000Z");
  const tariff = timeline("tariff", 12, 6, "2026-08-30T15:00:00.000Z");
  const rcm = timeline("rcm", 13, 7, "2026-08-30T14:15:00.000Z");
  let callServiceCount = 0;
  let callWSCount = 0;
  const states = {
    [EXPECTED_BINDINGS.baseline_timeline_entity]: baselineTimeline(),
    [EXPECTED_BINDINGS.canonical_timeline_entity]: canonicalTimeline(),
    [EXPECTED_BINDINGS.rce_timeline_entity]: rce,
    [EXPECTED_BINDINGS.tariff_timeline_entity]: tariff,
    [EXPECTED_BINDINGS.rcm_timeline_entity]: rcm,
    [EXPECTED_BINDINGS.rce_plan_entity]: {
      state: "ready",
      attributes: { status_code: "planned", result_current: true, recalculation_pending: false, input_revision: 11 },
    },
    [EXPECTED_BINDINGS.tariff_plan_entity]: {
      state: "ready",
      attributes: {
        status_code: "planned",
        result_current: true,
        recalculation_pending: false,
        input_revision: 12,
        current_slot_planned: false,
        current_action: "none",
        current_run_start_eligible: false,
        current_run_suppression_reason: "not_support_only",
      },
    },
    [EXPECTED_BINDINGS.rcm_plan_entity]: {
      state: "ready",
      attributes: { status_code: "planned", result_current: true, recalculation_pending: false, input_revision: 13, action: "grid_discharge_preparation", prediction_ready: true, prediction_block_reason: null },
    },
    [EXPECTED_BINDINGS.supervisor_entity]: {
      state: "selected",
      attributes: {
        schema_version: 1,
        supervisor_mode: "Active",
        profile: "Balanced",
        execution_phase: "idle",
        selected_policy: "rce",
        selected_candidate_revision: 5,
        selection_kind: "economic",
        selection_reason: "candidate_ready",
        execution_blocked_reason: null,
        arbitration_revision: 9,
        supervisor_execution_authorized: false,
        legacy_execution_unchanged: true,
        rejected_reasons: [],
        profile_effects_applied: [],
        profile_effects_not_applied: [],
        execution_health: executionHealth(),
        candidate_summaries: [candidate("rcm", 13, 7), candidate("rce", 11, 5), candidate("tariff", 12, 6)],
      },
    },
    [EXPECTED_BINDINGS.physical_mode_entity]: { state: "self_use", attributes: {} },
    [EXPECTED_BINDINGS.control_conflict_entity]: { state: "off", attributes: {} },
    [EXPECTED_BINDINGS.rce_enabled_entity]: { state: "on", attributes: {} },
    [EXPECTED_BINDINGS.rce_active_entity]: { state: "off", attributes: {} },
    [EXPECTED_BINDINGS.rce_expert_entity]: { state: "on", attributes: {} },
    [EXPECTED_BINDINGS.tariff_enabled_entity]: { state: "on", attributes: {} },
    [EXPECTED_BINDINGS.tariff_active_entity]: { state: "off", attributes: {} },
    [EXPECTED_BINDINGS.tariff_active_action_entity]: {
      state: "none",
      attributes: {},
    },
    [EXPECTED_BINDINGS.tariff_expert_entity]: { state: "off", attributes: {} },
    [EXPECTED_BINDINGS.rcm_enabled_entity]: { state: "on", attributes: {} },
    [EXPECTED_BINDINGS.rcm_active_entity]: { state: "off", attributes: {} },
    [EXPECTED_BINDINGS.rcm_export_active_entity]: { state: "off", attributes: {} },
    [EXPECTED_BINDINGS.rcm_pre_discharge_active_entity]: { state: "off", attributes: {} },
    [EXPECTED_BINDINGS.rcm_expert_entity]: { state: "on", attributes: {} },
    "input_select.hoymiles_ems_supervisor_mode": {
      state: "Active",
      attributes: { options: ["Off", "Active"] },
    },
    "input_boolean.hoymiles_ems_paused": { state: "off", attributes: {} },
    "input_boolean.hoymiles_ems_policy_preference_rce": { state: "on", attributes: {} },
    "input_boolean.hoymiles_ems_policy_preference_tariff": { state: "on", attributes: {} },
    "input_boolean.hoymiles_ems_policy_preference_rcm": { state: "on", attributes: {} },
    "input_boolean.hoymiles_ems_policy_preference_balancing": { state: "on", attributes: {} },
    "input_boolean.hoymiles_ems_supervisor_allow_rce": { state: "on", attributes: {} },
    "input_boolean.hoymiles_ems_supervisor_allow_tariff": { state: "on", attributes: {} },
    "input_boolean.hoymiles_ems_supervisor_allow_rcm": { state: "on", attributes: {} },
    "input_boolean.hoymiles_battery_balancing_enabled": { state: "on", attributes: {} },
    "input_boolean.hoymiles_battery_balancing_active": { state: "off", attributes: {} },
    "binary_sensor.hoymiles_ems_execution_ready": { state: "on", attributes: {} },
  };
  return {
    language,
    locale: { language },
    states,
    get callServiceCount() { return callServiceCount; },
    get callWSCount() { return callWSCount; },
    callService() { callServiceCount += 1; throw new Error("service forbidden"); },
    callWS() { callWSCount += 1; throw new Error("WS forbidden"); },
  };
}

function mount(hass = hassFixture()) {
  const Card = registry.get("hoymiles-automation-planner-card");
  const card = new Card();
  card.setConfig({ ...EXPECTED_BINDINGS });
  card.connectedCallback?.();
  card.hass = hass;
  flushAnimationFrames();
  return { card, hass };
}

function testExecutionHealthUiContract() {
  const AppShell = registry.get("hoymiles-aurora-app-shell-card");
  check(typeof AppShell === "function", "Aurora shell class exists for health audit");
  const shell = new AppShell();
  shell._config = {
    supervisor_entity: EXPECTED_BINDINGS.supervisor_entity,
    control_conflict_entity: EXPECTED_BINDINGS.control_conflict_entity,
    execution_readiness_entity: "binary_sensor.hoymiles_ems_execution_ready",
  };
  const setHealth = (health, language = "pl") => {
    const hass = hassFixture(language);
    if (health === null) delete hass.states[EXPECTED_BINDINGS.supervisor_entity].attributes.execution_health;
    else hass.states[EXPECTED_BINDINGS.supervisor_entity].attributes.execution_health = health;
    shell._hass = hass;
    return hass;
  };

  setHealth(executionHealth({
    status: "unhealthy",
    control_status: "unhealthy",
    reason: "execution_adapter_error",
    phase: "executing",
    control_error: "active_reconcile_failed",
    action: "restart_ems",
  }));
  check(!shell._emsReady(), "executing plus active reconcile failure is not ready");
  check(shell._supervisorUnhealthy(), "active reconcile failure is unhealthy");
  check(shell._systemHealthStatus() === "unhealthy", "active reconcile failure drives system health");

  setHealth(executionHealth({ status: "unhealthy", control_status: "unhealthy", reason: "execution_adapter_error", control_error: "write_timeout", action: "restart_ems" }));
  check(!shell._emsReady() && shell._systemHealthStatus() === "unhealthy", "other execution adapter error is fail-closed");

  setHealth(null);
  check(!shell._emsReady(), "missing health contract is not ready");
  check(shell._supervisorUnhealthy(), "missing health contract is unknown-unhealthy");
  check(shell._systemHealthStatus() === "unknown", "missing health contract stays unknown");

  setHealth(executionHealth({ status: "unknown", control_status: "unknown", reason: "physical_readback_stale", action: "wait_for_fresh_read", connectivity: "stale", last_valid_read_age_seconds: 181 }));
  check(shell._systemHealthStatus() === "unknown" && !shell._emsReady(), "stale physical readback is unknown and blocked");

  setHealth(executionHealth({ status: "degraded", reason: "accounting_degraded", action: "inspect_degradations", degradations: { accounting: "ledger_unavailable" } }));
  check(shell._emsReady(), "accounting degradation does not block healthy control");
  check(!shell._supervisorUnhealthy(), "accounting degradation is not physical failure");
  check(shell._systemHealthStatus() === "degraded", "accounting degradation remains visible");

  setHealth(executionHealth({ status: "degraded", reason: "notifications_degraded", action: "inspect_degradations", degradations: { notifications: "phone_unavailable" } }));
  check(shell._emsReady() && shell._systemHealthStatus() === "degraded", "phone unavailable is a separate visible degradation");

  setHealth(executionHealth({ phase: "pending", physical_verification: "pending" }));
  check(shell._systemHealthStatus() === "healthy", "ordinary pending state is not an inverter fault");

  setHealth(executionHealth({ status: "unhealthy", control_status: "unhealthy", reason: "owner_conflict", action: "resolve_owner_conflict", owner_conflict: true, observed_owner: "manual" }));
  check(!shell._emsReady() && shell._systemHealthStatus() === "unhealthy", "owner conflict blocks readiness");

  setHealth(executionHealth({ status: "recovering", control_status: "recovering", reason: "recovery_in_progress", action: "wait_for_recovery", phase: "restoring", rollback_status: "pending" }));
  check(!shell._emsReady() && shell._systemHealthStatus() === "recovering", "recovery is visible and cannot resume");

  setHealth(executionHealth({ status: "recovering", control_status: "recovering", reason: "master_stop_unconfirmed", action: "wait_for_master_stop", master_stop_status: "requested", master_stop_confirmation: "unconfirmed" }));
  check(shell._systemHealthStatus() === "recovering", "requested STOP is recovering until confirmed");
  setHealth(executionHealth({ master_stop_status: "completed", master_stop_confirmation: "confirmed" }));
  check(shell._systemHealthStatus() === "healthy", "completed STOP is healthy with fresh evidence");
  setHealth(executionHealth({ status: "unhealthy", control_status: "unhealthy", reason: "master_stop_blocked", action: "verify_master_stop", master_stop_status: "blocked", master_stop_confirmation: "blocked" }));
  check(shell._systemHealthStatus() === "unhealthy", "blocked STOP is unhealthy");

  setHealth(executionHealth({ last_valid_read_age_seconds: 1 }));
  check(shell._emsReady() && shell._systemHealthStatus() === "healthy", "new trustworthy data restores healthy state");

  const polish = shell._healthReasonText(shell._healthContract());
  check(polish === "Brak aktywnego problemu sterowania", "Polish health reason copy differs");
  setHealth(executionHealth({ reason: "execution_adapter_error", control_error: "write_timeout", action: "restart_ems" }), "en");
  check(shell._healthReasonText(shell._healthContract()).includes("Execution adapter error"), "English health reason copy differs");
  check(shell._healthActionText("restart_ems").includes("controlled EMS restart"), "English safe action copy differs");
}

testExecutionHealthUiContract();

function tariffPresentationFixture({
  language = "pl",
  currentSoc = 100,
  currentPvKw = 0,
  currentLoadKw = 2,
  currentBatteryKw = -1,
  currentGridKw = 0,
  startSoc = 50,
  endSoc = 50,
  targetSoc = 50,
  actionCode = "grid_support",
  batteryKw = 0,
  gridImportKw = 1,
  plannedImportKwh = 0.5,
  plannedChargeKw = 0,
  tariffEnabled = true,
  tariffActive = false,
  currentPoint = tariffActive,
  startEligible = false,
  suppressionReason = "intent_not_stable",
  candidateStartEligible = startEligible,
  activeAction = tariffActive ? actionCode : "none",
  physicalMode = "self_use",
  pointActive = false,
  candidateActive = false,
  controlConflict = false,
} = {}) {
  const hass = hassFixture(language);
  const attributes =
    hass.states[EXPECTED_BINDINGS.tariff_timeline_entity].attributes;
  Object.assign(attributes.current_actual, {
    pv_kw: currentPvKw,
    load_kw: currentLoadKw,
    battery_kw: currentBatteryKw,
    grid_kw: currentGridKw,
    soc_percent: currentSoc,
    quality: "complete",
  });
  const [lead, selected] = attributes.points;
  Object.assign(lead, {
    action_code: "idle",
    battery_kw: 0,
    grid_kw: 0,
    grid_import_kw: 0,
    grid_export_kw: 0,
    soc_percent: startSoc,
    target_soc_percent: null,
    selected: false,
    policy: {
      ...lead.policy,
      planned_import_kwh: 0,
      planned_charge_kw: 0,
    },
  });
  Object.assign(selected, {
    action_code: actionCode,
    battery_kw: batteryKw,
    grid_kw: gridImportKw,
    grid_import_kw: gridImportKw,
    grid_export_kw: 0,
    soc_percent: endSoc,
    target_soc_percent: targetSoc,
    active: pointActive,
    selected: true,
    policy: {
      ...selected.policy,
      planned_import_kwh: plannedImportKwh,
      planned_charge_kw: plannedChargeKw,
    },
  });
  if (currentPoint) {
    attributes.current_actual.observed_at = new Date(
      Date.parse(selected.start) + 5 * 60_000,
    ).toISOString();
  }
  Object.assign(
    hass.states[EXPECTED_BINDINGS.tariff_plan_entity].attributes,
    {
      current_slot_planned: currentPoint,
      current_action: currentPoint ? actionCode : "none",
      current_run_start_eligible: currentPoint ? startEligible : false,
      current_run_suppression_reason: currentPoint
        ? suppressionReason
        : "not_support_only",
    },
  );
  hass.states[EXPECTED_BINDINGS.tariff_enabled_entity].state =
    tariffEnabled ? "on" : "off";
  hass.states[EXPECTED_BINDINGS.tariff_active_entity].state =
    tariffActive ? "on" : "off";
  hass.states[EXPECTED_BINDINGS.tariff_active_action_entity].state =
    activeAction;
  hass.states[EXPECTED_BINDINGS.physical_mode_entity].state = physicalMode;
  hass.states[EXPECTED_BINDINGS.control_conflict_entity].state =
    controlConflict ? "on" : "off";
  const supervisor = hass.states[EXPECTED_BINDINGS.supervisor_entity].attributes;
  const tariffCandidate = supervisor.candidate_summaries.find(
    (item) => item.policy_id === "tariff",
  );
  tariffCandidate.active_latched = candidateActive;
  tariffCandidate.start_eligible = candidateStartEligible;
  if (candidateActive) {
    supervisor.selected_policy = "tariff";
    supervisor.selected_candidate_revision = tariffCandidate.candidate_revision;
  }
  return hass;
}

function selectTariffInterval(card) {
  const point = card._lastModel.timelines.tariff.points.find(
    (item) => item.selected,
  );
  const baselinePoint = card._lastModel.baselineTimeline.points.find(
    (item) => item.start === point.start,
  );
  card._selectPoint(baselinePoint.key);

  // The canonical C4 inspector is deliberately baseline-first.  Exercise the
  // inherited tariff semantic formatter in a detached probe so C2/C3 keeps
  // validating its exact classification copy, while the active card remains
  // on the matching baseline interval and exposes the canonical action only.
  const LegacyCard = Object.getPrototypeOf(card.constructor.prototype).constructor;
  const legacyCard = new LegacyCard();
  legacyCard.setConfig({ ...EXPECTED_BINDINGS });
  legacyCard.connectedCallback?.();
  legacyCard.hass = card._latestHass;
  flushAnimationFrames();
  legacyCard._selectPoint(point.key);
  const result = {
    point,
    event: legacyCard._refs.selectedFields.event.value.textContent,
    battery: legacyCard._refs.selectedFields.battery.value.textContent.replaceAll("\u00a0", ""),
    reason: legacyCard._refs.selectedFields.reason.value.textContent,
    planOnly: legacyCard._refs.selectedFields.planOnly.value.textContent,
    blocker: legacyCard._refs.selectedFields.blocker.value.textContent,
    status: card._lastModel.policies.tariff.status,
    assessment: card._lastModel.policies.tariff.executionAssessment,
  };
  legacyCard.disconnectedCallback?.();
  return result;
}

const LocalNavigation = registry.get("hoymiles-local-nav-card");
check(Boolean(LocalNavigation), "I2 local-navigation custom element is registered");
const localNavigation = new LocalNavigation();
let localNavigationWrites = 0;
let localNavigationHtml = "";
Object.defineProperty(localNavigation.shadowRoot, "innerHTML", {
  configurable: true,
  get: () => localNavigationHtml,
  set: (value) => {
    localNavigationWrites += 1;
    localNavigationHtml = String(value);
  },
});
localNavigation.setConfig({
  group: "settings",
  current_path: "ladowanie-taryfowe",
  language: "pl",
});
const settingsNavPaths = [
  "ustawienia-ems", "automatyka-ems", "ladowanie-taryfowe",
  "rcem-253v", "ustawienia-balansowania", "diagnostyka",
];
check(
  settingsNavPaths.every((route, index) => {
    const position = localNavigationHtml.indexOf(`/hoymiles-falownik/${route}`);
    const priorPosition = index === 0
      ? -1
      : localNavigationHtml.indexOf(`/hoymiles-falownik/${settingsNavPaths[index - 1]}`);
    return position > priorPosition;
  }),
  "settings navigation follows the exact Variant A order",
);
check(
  (localNavigationHtml.match(/<a\b[^>]*\baria-current="page"/g) || []).length === 1
    && localNavigationHtml.includes('href="/hoymiles-falownik/ladowanie-taryfowe" aria-current="page"')
    && localNavigationHtml.includes('aria-label="Nawigacja lokalna"')
    && localNavigationHtml.includes('data-accent="neutral"')
    && localNavigationHtml.includes('data-accent="grid"')
    && localNavigationHtml.includes('data-accent="battery"')
    && localNavigationHtml.includes('data-accent="warning"')
    && localNavigationHtml.includes('data-accent="pv"')
    && localNavigationHtml.includes("min-height: 56px")
    && localNavigationHtml.includes("grid-template-columns: repeat(auto-fit, minmax(158px, 1fr))")
    && localNavigationHtml.includes("<ha-card><nav"),
  "settings navigation exposes one framed current page and all Variant A accents",
);
localNavigation.shadowRoot.scrollLeft = 73;
localNavigation.hass = { language: "pl", states: { "sensor.telemetry": { state: "1" } } };
localNavigation.hass = { language: "pl", states: { "sensor.telemetry": { state: "2" } } };
check(
  localNavigationWrites === 1 && localNavigation.shadowRoot.scrollLeft === 73,
  "identical-context telemetry updates do not rebuild local navigation or reset horizontal scroll",
);
check(
  ["Ogólne", "Sprzedaż dynamiczna", "Ładowanie taryfowe", "Ochrona napięciowa", "Balansowanie magazynu", "Serwis"]
    .every((label) => localNavigationHtml.includes(`<span>${label}</span>`))
    && (localNavigationHtml.match(/class="local-link related"/g) || []).length === 1
    && localNavigationHtml.includes("<small>Powiązane</small>"),
  "settings navigation uses compact EMS copy and exposes balancing as a first-class setting",
);
for (const retiredGroup of ["automation", "energy", "pv"]) {
  let rejected = false;
  try {
    localNavigation.setConfig({ group: retiredGroup, current_path: "start", language: "pl" });
  } catch (_error) {
    rejected = true;
  }
  check(rejected, `retired ${retiredGroup} navigation group is unavailable`);
}
const localNavigationSource = cardSource.slice(
  cardSource.indexOf("class HoymilesLocalNavCard"),
  cardSource.indexOf("class HoymilesRceChartCard"),
);
check(
  localNavigationSource.includes('viewTag === "HUI-SIDEBAR-VIEW"')
    && localNavigationSource.includes('viewTag === "HUI-MASONRY-VIEW"')
    && localNavigationSource.includes('placement: config?.placement === "sidebar" ? "sidebar" : "top"')
    && localNavigationSource.includes('if (this._config?.placement === "sidebar") return;')
    && localNavigationSource.includes('data-hoymiles-local-nav-anchor')
    && localNavigationSource.includes('hoymiles-aurora-view-frame')
    && localNavigationSource.includes('--ha-card-background: var(--hoymiles-local-card-surface)')
    && localNavigationSource.includes('nativeContainer.style.setProperty("--hoymiles-local-frame-accent", frameAccent)')
    && localNavigationSource.includes('hostCard.dataset.hoymilesLocalNavPlaced === "true"')
    && localNavigationSource.includes('hostCard.dataset.hoymilesLocalNavPlaced = "true"')
    && localNavigationSource.includes('#columns.hoymiles-aurora-view-frame > .column:has(> .hoymiles-local-nav-anchor)')
    && localNavigationSource.includes('.hoymiles-aurora-view-frame:has(.hoymiles-local-nav-anchor)')
    && localNavigationSource.includes('@media (max-width: 1050px)')
    && localNavigationSource.includes('grid-template-columns: ${sidebar ? "1fr"')
    && localNavigationSource.includes('class="back-link"')
    && localNavigationSource.includes('class="menu-note"')
    && localNavigationSource.includes('overflow-x:auto')
    && localNavigationSource.includes('touch-action:pan-x pan-y')
    && localNavigationSource.includes('flex:0 0 180px')
    && localNavigationSource.includes('flex:0 0 150px')
    && localNavigationSource.includes('top: -112px;'),
  "local navigation supports both the bounded top row and the Variant A native sidebar without overlap",
);
for (const forbidden of ["addEventListener", "callApi", "callService", "fetch(", "setTimeout", "setInterval"]) {
  check(!localNavigationSource.includes(forbidden), `local navigation excludes ${forbidden}`);
}
check(
  cardSource.includes("function hoymilesCardTreeIncludesType")
    && cardSource.includes("const HOYMILES_LOCAL_NAV_CONTEXT_PATHS = new Set([")
    && !cardSource.slice(
      cardSource.indexOf("function hoymilesDecorateDashboard"),
      cardSource.indexOf("class HoymilesLocalNavCard"),
    ).includes('type: "custom:hoymiles-local-nav-card"'),
  "dashboard decoration retains manual-card compatibility without injecting the obsolete coloured strip",
);
const manualYamlDashboard = {
  views: [{
    title: "Ustawienia EMS",
    path: "ustawienia-ems",
    type: "panel",
    cards: [{
      type: "vertical-stack",
      cards: [
        { type: "custom:hoymiles-local-nav-card", group: "settings", current_path: "ustawienia-ems" },
        { type: "custom:hoymiles-ems-shared-inputs-card", mode: "full" },
      ],
    }],
  }],
};
const decoratedManualDashboard = context.__hoymilesDecorateDashboard(
  manualYamlDashboard,
  "pl",
);
const countCardType = (value, type) => {
  if (Array.isArray(value)) {
    return value.reduce((total, item) => total + countCardType(item, type), 0);
  }
  if (!value || typeof value !== "object") return 0;
  return (value.type === type ? 1 : 0)
    + countCardType(value.cards, type)
    + countCardType(value.card, type);
};
check(
  countCardType(
    decoratedManualDashboard.views?.[0]?.cards,
    "custom:hoymiles-local-nav-card",
  ) === 1,
  "manual YAML settings navigation remains exactly once after strategy decoration",
);
const canonicalNavigation = new LocalNavigation();
let canonicalNavigationHtml = "";
Object.defineProperty(canonicalNavigation.shadowRoot, "innerHTML", {
  configurable: true,
  get: () => canonicalNavigationHtml,
  set: (value) => { canonicalNavigationHtml = String(value); },
});
canonicalNavigation.setConfig({
  group: "settings",
  current_path: "ustawienia-ems",
});
canonicalNavigation.hass = { locale: { language: "pl-PL" }, states: {} };
check(
  canonicalNavigationHtml.includes('aria-label="Nawigacja lokalna"')
    && canonicalNavigationHtml.includes("<span>Ogólne</span>")
    && !canonicalNavigationHtml.includes("<span>General</span>"),
  "canonical YAML navigation without a language override follows the Home Assistant locale",
);

const BalancingPlan = registry.get("hoymiles-battery-balancing-plan-card");
check(Boolean(BalancingPlan), "compact battery-balancing plan custom element is registered");
const balancingPlanSource = cardSource.slice(
  cardSource.indexOf("class HoymilesBatteryBalancingPlanCard"),
  cardSource.indexOf('if (!customElements.get("hoymiles-battery-balancing-plan-card"))'),
);
check(
  cardSource.includes("@container (max-width: 950px)")
    && !cardSource.includes("@container (max-width: 850px) { .row"),
  "balancing summary changes to its compact grid before desktop columns can clip",
);
check(
  balancingPlanSource.includes('<article class="row" aria-labelledby="hoymiles-balancing-plan-title">')
    && balancingPlanSource.includes('role="status" aria-live="polite"')
    && ["callService(", "callApi(", "fetch(", "addEventListener("].every(
      (token) => !balancingPlanSource.includes(token),
    ),
  "balancing summary is accessible and remains a strictly read-only evidence card",
);
const balancingPlan = new BalancingPlan();
let balancingPlanWrites = 0;
let balancingPlanHtml = "";
Object.defineProperty(balancingPlan.shadowRoot, "innerHTML", {
  configurable: true,
  get: () => balancingPlanHtml,
  set: (value) => {
    balancingPlanWrites += 1;
    balancingPlanHtml = String(value);
  },
});
balancingPlan.setConfig({});
balancingPlan.hass = {
  language: "pl",
  states: {
    "input_boolean.hoymiles_battery_balancing_enabled": { state: "on", attributes: {} },
    "input_boolean.hoymiles_battery_balancing_active": { state: "off", attributes: {} },
    "sensor.hoymiles_battery_balancing_status": { state: "Cykl gotowy", attributes: {} },
    "sensor.hoymiles_battery_balancing_next_run": { state: "22.09.2026 — po wschodzie słońca", attributes: {} },
    "input_number.hoymiles_battery_balancing_hold_hours": { state: "3.5", attributes: {} },
  },
};
check(
  balancingPlanHtml.includes("Balansowanie magazynu")
    && balancingPlanHtml.includes("Okresowy cykl poza osią planu dobowego")
    && balancingPlanHtml.includes("Włączone")
    && balancingPlanHtml.includes("Cykl gotowy")
    && balancingPlanHtml.includes("22.09.2026 — po wschodzie słońca")
    && balancingPlanHtml.includes("100% SOC")
    && balancingPlanHtml.includes("utrzymanie 3,5 h")
    && balancingPlanHtml.includes("Brak planu kWh")
    && balancingPlanHtml.includes("Zapotrzebowanie wynika z SOC dopiero w czasie cyklu.")
    && balancingPlanHtml.includes('href="/hoymiles-falownik/bateria"'),
  "balancing summary exposes only truthful status, next run and cycle target outside the canonical day timeline",
);
const balancingWritesAfterRelevantState = balancingPlanWrites;
balancingPlan.hass = {
  language: "pl",
  states: {
    ...balancingPlan._hass.states,
    "sensor.unrelated_balancing_probe": { state: "1", attributes: {} },
  },
};
check(
  balancingPlanWrites === balancingWritesAfterRelevantState,
  "unrelated telemetry does not rebuild the balancing summary",
);
balancingPlan.hass = {
  language: "pl",
  states: {
    ...balancingPlan._hass.states,
    "input_boolean.hoymiles_battery_balancing_enabled": { state: "unavailable", attributes: {} },
    "input_boolean.hoymiles_battery_balancing_active": { state: "on", attributes: {} },
  },
};
check(
  balancingPlanHtml.includes("Brak danych")
    && balancingPlanHtml.includes('class="state" data-tone="idle"')
    && !balancingPlanHtml.includes('class="state" data-tone="active"'),
  "partial balancing availability stays visually neutral and cannot imply confirmed activity",
);

const RceChartCard = registry.get("hoymiles-rce-chart-card");
check(Boolean(RceChartCard), "RCE chart remains registered");
const rceChart = new RceChartCard();
rceChart._rceSamples = [
  { index: 0, key: "2026-09-02|00:00", period: "00:00", price: 0.5, sellPrice: 0.5, pairAverage: 0.55, selected: false, plannedPowerKw: null, plannedEnergyKwh: null, expectedRevenuePln: null, socBeforePercent: null, socAfterPercent: null, stateLabel: "Autokonsumpcja" },
  { index: 1, key: "2026-09-02|00:15", period: "00:15", price: 0.6, sellPrice: 0.6, pairAverage: 0.55, selected: true, plannedPowerKw: 2.4, plannedEnergyKwh: 1.2, expectedRevenuePln: 0.98, socBeforePercent: null, socAfterPercent: 74, stateLabel: "Zaplanowane rozładowanie" },
];
check(
  rceChart._selectRceSample(1)?.key === "2026-09-02|00:15"
    && rceChart._selectedRceKey === "2026-09-02|00:15",
  "RCE click/keyboard selection pins an exact real quarter-hour sample",
);
rceChart._rcePlot = { width: 100, height: 100, left: 10, right: 10, top: 10, plotHeight: 80, slotWidth: 40 };
check(
  rceChart._rceIndexAtClientPoint(
    { clientX: 33, clientY: 22 },
    { getBoundingClientRect: () => ({ left: 0, top: 0, width: 44, height: 44 }) },
  ) === 1,
  "RCE chart tap resolves the nearest real sample from chart coordinates",
);
const rceChartSource = cardSource.slice(
  cardSource.indexOf("class HoymilesRceChartCard"),
  cardSource.indexOf('customElements.get("hoymiles-rce-chart-card")'),
);
check(
  rceChartSource.includes('data-rce-index="${index}" role="button"')
    && rceChartSource.includes('data-rce-panel aria-live="polite"')
    && rceChartSource.includes('event.key === "Enter" || event.key === " "')
    && rceChartSource.includes("_rceIndexAtClientPoint(event, chart)")
    && rceChartSource.includes("planState?.attributes?.result_current === true")
    && rceChartSource.includes("planState?.attributes?.recalculation_pending === false"),
  "RCE bars expose focusable click/tap/Enter/Space targets and a persistent detail panel",
);
check(
  rceChart._rcePanelHtml(rceChart._rceSamples[1]).includes("Cena kupna/sprzedaży")
    && rceChart._rcePanelHtml(rceChart._rceSamples[1]).includes("2,40 kW")
    && rceChart._rcePanelHtml(rceChart._rceSamples[1]).includes("1,20 kWh")
    && rceChart._rcePanelHtml(rceChart._rceSamples[1]).includes("0,98 PLN")
    && rceChart._rcePanelHtml(rceChart._rceSamples[1]).includes("Niedostępne → 74,0 %")
    && rceChart._rcePanelHtml(rceChart._rceSamples[1]).includes("Zaplanowane rozładowanie"),
  "RCE pinned panel reports direct per-slot price, selection, power, energy, SOC availability and result",
);
check(
  rceChart._rceLocalSlotKey("2026-09-02T10:00:00Z") === "2026-09-02|12:00",
  "RCE timeline samples match planned slots in the canonical Europe/Warsaw wall-clock",
);
const staleRceChart = new RceChartCard();
staleRceChart.setConfig({ entity: "sensor.rce_prices", plan_entity: "sensor.rce_plan" });
staleRceChart.hass = {
  language: "pl",
  states: {
    "sensor.rce_prices": {
      state: "ok",
      last_updated: "2026-09-02T10:00:00Z",
      attributes: { value: [
        { period: "12:00 - 12:15", business_date: "2026-09-02", rce_pln: 500 },
        { period: "12:15 - 12:30", business_date: "2026-09-02", rce_pln: 600 },
      ] },
    },
    "sensor.rce_plan": {
      state: "ready",
      last_updated: "2026-09-02T10:00:00Z",
      attributes: {
        result_current: false,
        recalculation_pending: true,
        planned_slots: [{ date: "2026-09-02", start: "12:00", price: 0.55, energy: 1.2, revenue: 0.66 }],
      },
    },
  },
};
check(
  staleRceChart._rceSamples.length === 2
    && staleRceChart._rceSamples.every((sample) => !sample.planned && !sample.selected
      && sample.plannedEnergyKwh === null && sample.expectedRevenuePln === null),
  "stale RCE plan cannot mark slots or expose stale energy/revenue as a current selection",
);

const HistoryCard = registry.get("hoymiles-aurora-history-card");
check(Boolean(HistoryCard), "legacy Aurora history element remains compatibility-registered");
const historyCard = new HistoryCard();
const historyConfig = {
  hours_to_show: 24,
  entities: [
    "sensor.hoymiles_hit_overview_pv_total_power",
    "sensor.hoymiles_actual_load_power",
  ],
};
let historyApiCalls = 0;
const neverSettlingHistoryHass = {
  language: "pl",
  states: {},
  callApi: () => {
    historyApiCalls += 1;
    return new Promise(() => {});
  },
};
historyCard.setConfig(historyConfig);
historyCard.hass = neverSettlingHistoryHass;
historyCard.connectedCallback();
historyCard.hass = { ...neverSettlingHistoryHass, states: { "sensor.telemetry": { state: "2" } } };
check(historyApiCalls === 1, "compatibility history card performs at most one initial callApi for a stable configuration");
historyCard._loading = false;
historyCard._lastFetch = Date.now();
historyCard.hass = neverSettlingHistoryHass;
historyCard.setConfig({ ...historyConfig });
check(
  historyApiCalls === 1 && historyCard._historyRequested === true,
  "stable history configuration does not refetch inside the five-minute recorder cooldown",
);
historyCard.setConfig({ ...historyConfig, hours_to_show: 48 });
check(historyApiCalls === 2, "only a material history configuration change permits one new initial request");
const historyComponentSource = cardSource.slice(
  cardSource.indexOf("class HoymilesAuroraHistoryCard"),
  cardSource.indexOf('if (!customElements.get("hoymiles-aurora-history-card"))'),
);
check(
  historyComponentSource.includes("const cooldown = this._error ? 60_000 : 300_000;")
    && historyComponentSource.includes("const checkpoint = this._error ? this._lastAttempt : this._lastFetch;"),
  "recorder history retries failures after one minute and refreshes healthy data every five minutes",
);

const rawHistoryCard = new HistoryCard();
rawHistoryCard.setConfig({
  language: "pl",
  unit: "V",
  digits: 1,
  entities: [{ entity: "sensor.grid_voltage", name: "Napięcie" }],
});
rawHistoryCard._hass = {
  language: "pl",
  states: {
    "sensor.grid_voltage": { state: "253.46", attributes: { unit_of_measurement: "V" } },
  },
};
check(
  rawHistoryCard._config.entities[0].value_mode === "raw"
    && rawHistoryCard._currentValue("sensor.grid_voltage") === 253.46
    && rawHistoryCard._formatValue(253.46, rawHistoryCard._config.entities[0]) === "253,5 V",
  "Aurora history supports explicit raw units and bounded digits while power remains the default mode",
);
const voltageHistoryCard = new HistoryCard();
voltageHistoryCard.setConfig({
  language: "pl",
  value_mode: "raw",
  unit: "V",
  digits: 1,
  inspection_mode: "rcm_voltage",
  inspection_threshold_v: 253,
  entities: [
    { entity: "sensor.l1", name: "L1", inspection_role: "phase_l1" },
    { entity: "sensor.l2", name: "L2", inspection_role: "phase_l2" },
    { entity: "sensor.l3", name: "L3", inspection_role: "phase_l3" },
    { entity: "sensor.avg", name: "Średnia 10 min", inspection_role: "average_10m" },
  ],
});
const voltageSelection = {
  time: 10_000,
  entries: voltageHistoryCard._config.entities.map((series, index) => ({
    series,
    point: { time: 10_000, value: [250.1, 253.4, 251.2, 250.7][index] },
  })),
};
const voltagePanel = voltageHistoryCard._historyPanelHtml(voltageSelection);
check(
  voltagePanel.includes("L1")
    && voltagePanel.includes("250,1 V")
    && voltagePanel.includes("L2")
    && voltagePanel.includes("253,4 V")
    && voltagePanel.includes("L3")
    && voltagePanel.includes("251,2 V")
    && voltagePanel.includes("Średnia 10 min")
    && voltagePanel.includes("250,7 V"),
  "RCEm pinned panel reports all four actual recorded voltage samples",
);
check(
  voltagePanel.includes("Najwyższa faza")
    && voltagePanel.includes("253,4 V · L2")
    && voltagePanel.includes("253,0 V")
    && voltagePanel.includes("Ryzyko ograniczenia PV — próg przekroczony")
    && voltagePanel.includes('data-risk="high"'),
  "RCEm voltage inspection derives the highest recorded phase and 253 V risk state",
);
const safeVoltageSelection = {
  ...voltageSelection,
  entries: voltageSelection.entries.map((entry, index) => ({
    ...entry,
    point: { ...entry.point, value: [250.1, 252.0, 251.2, 250.7][index] },
  })),
};
check(
  voltageHistoryCard._historyPanelHtml(safeVoltageSelection).includes("Brak ryzyka — napięcie poniżej progu")
    && voltageHistoryCard._historyPanelHtml(null).includes("Brak danych do oceny ryzyka"),
  "RCEm voltage inspection distinguishes below-threshold and unavailable real-sample states",
);
const selectionHistoryCard = new HistoryCard();
selectionHistoryCard.setConfig({ entities: ["sensor.a", "sensor.b"] });
selectionHistoryCard._renderedHistorySeries = [
  { entity: "sensor.a", value_mode: "power", digits: 2, color: "#2de083", points: [{ time: 1_000, value: 1 }, { time: 3_000, value: 3 }] },
  { entity: "sensor.b", value_mode: "power", digits: 2, color: "#3ea6ff", points: [{ time: 2_000, value: 2 }, { time: 5_000, value: 5 }] },
];
const nearestHistory = selectionHistoryCard._nearestHistorySelection(2_800);
check(
  nearestHistory?.time === 3_000
    && nearestHistory.entries[0].point.time === 3_000
    && nearestHistory.entries[1].point.time === 2_000,
  "Aurora history pins nearest recorded samples without interpolation",
);
selectionHistoryCard._render = () => {};
selectionHistoryCard._selectHistoryAtTime(2_800);
selectionHistoryCard.hass = { language: "pl", states: {} };
check(
  selectionHistoryCard._selectedHistoryTime === 3_000,
  "Aurora history selection remains stable across live hass renders",
);
const hoverHistoryCard = new HistoryCard();
hoverHistoryCard.setConfig({ language: "pl", entities: ["sensor.a", "sensor.b"] });
hoverHistoryCard._renderedHistorySeries = [
  { entity: "sensor.a", name: "A", value_mode: "power", digits: 2, color: "#2de083", points: [{ time: 1_000, value: 1 }, { time: 3_000, value: 3 }] },
  { entity: "sensor.b", name: "B", value_mode: "power", digits: 2, color: "#3ea6ff", points: [{ time: 2_000, value: 2 }, { time: 5_000, value: 5 }] },
];
hoverHistoryCard._selectedHistoryTime = 1_000;
hoverHistoryCard._historyAxis = {
  start: 1_000,
  end: 5_000,
  x: (time) => time / 10,
  y: (value) => 100 - value,
  top: 10,
  plotHeight: 80,
};
const hoverTarget = new FakeElement("rect");
const hoverPanel = new FakeElement("div");
const hoverMarker = new FakeElement("g");
hoverHistoryCard.shadowRoot.querySelector = (selector) => ({
  "[data-history-hit]": hoverTarget,
  "[data-history-selection]": hoverPanel,
  "[data-history-marker]": hoverMarker,
}[selector] || null);
hoverHistoryCard._bindHistoryInteractions();
hoverTarget.dispatch("pointermove", { clientX: 40, pointerType: "mouse" });
check(
  hoverHistoryCard._selectedHistoryTime === 1_000
    && hoverHistoryCard._hoverHistoryTime === 5_000
    && hoverPanel.innerHTML.includes("Podgląd próbki")
    && hoverMarker.hidden === false,
  "fine-pointer hover previews the nearest real sample without changing the persistent pin",
);
hoverTarget.dispatch("pointerleave");
check(
  hoverHistoryCard._selectedHistoryTime === 1_000
    && hoverHistoryCard._hoverHistoryTime === null
    && hoverPanel.innerHTML.includes("Przypięta próbka"),
  "pointerleave restores the persistent pinned sample without a render or layout reset",
);
const originalMatchMedia = fakeWindow.matchMedia;
fakeWindow.matchMedia = (query) => ({
  matches: query === "(pointer: coarse)",
  addEventListener() {},
  removeEventListener() {},
});
const coarseHistoryCard = new HistoryCard();
coarseHistoryCard.setConfig({ entities: ["sensor.a"] });
coarseHistoryCard._renderedHistorySeries = hoverHistoryCard._renderedHistorySeries.slice(0, 1);
coarseHistoryCard._historyAxis = hoverHistoryCard._historyAxis;
const coarseTarget = new FakeElement("rect");
coarseHistoryCard.shadowRoot.querySelector = (selector) => selector === "[data-history-hit]" ? coarseTarget : null;
coarseHistoryCard._bindHistoryInteractions();
fakeWindow.matchMedia = originalMatchMedia;
check(
  !coarseTarget.listeners.has("pointermove")
    && !coarseTarget.listeners.has("pointerleave")
    && coarseTarget.listeners.has("click")
    && coarseTarget.listeners.has("keydown"),
  "coarse pointers keep tap/keyboard pinning but receive no semantic hover handlers",
);
const historySource = cardSource.slice(
  cardSource.indexOf("class HoymilesAuroraHistoryCard"),
  cardSource.indexOf("class HoymilesAuroraFinanceCard"),
);
check(
  historySource.includes("data-history-hit")
    && historySource.includes("history-marker")
    && historySource.includes("_nearestHistorySelection")
    && historySource.includes('event.key === "Enter" || event.key === " "')
    && !historySource.includes("setInterval("),
  "Aurora history adds accessible pinned inspection without polling or a timer",
);
check(
  cardSource.includes(".ap-c4-slot-target, .ap-c4-lane-slot-target { min-width: 44px; }")
    && cardSource.includes(".details { align-items: center;")
    && cardSource.includes(".chip { align-items: center;")
    && cardSource.includes("min-height: 44px;"),
  "planner slots, status actions and diagnostics retain 44-pixel touch targets",
);
const dashboardHistoryCards = dashboardLf.match(
  /^\s+- type: custom:hoymiles-aurora-history-card$/gm,
) || [];
const dashboardCompactHistoryPages = dashboardLf.match(
  /^\s+- type: custom:hoymiles-aurora-compact-page-card$/gm,
) || [];
check(
  dashboardHistoryCards.length === 6
    && dashboardCompactHistoryPages.length === 3
    && dashboardLf.includes("type: custom:hoymiles-aurora-overview-card")
    && !dashboardLf.includes("type: history-graph"),
  "six standalone histories, three compact-page histories and the overview use Aurora recorder history",
);
for (const contract of [
  ["Napięcie L1/L2/L3 i średnia 10-minutowa — ostatnie 24 godziny", "hours_to_show: 24", "value_mode: raw", "unit: V", "digits: 1"],
  ["Zużycie domu — moc ostatnie 24 godziny [W]", "hours_to_show: 24", "value_mode: raw", "unit: W", "digits: 0"],
  ["Temperatura ogniw — ostatnie 7 dni", "hours_to_show: 168", "value_mode: raw", 'unit: "°C"', "digits: 1"],
  ["Napięcie sieci — ostatnie 7 dni", "hours_to_show: 168", "value_mode: raw", "unit: V", "digits: 1"],
  ["Moc złącza generatora — ostatnie 24 godziny [W]", "hours_to_show: 24", "value_mode: raw", "unit: W", "digits: 0"],
  ["Temperatury falownika — ostatnie 7 dni", "hours_to_show: 168", "value_mode: raw", 'unit: "°C"', "digits: 1"],
]) {
  const titlePosition = dashboardLf.indexOf(`title: ${contract[0]}`);
  const nextCardPosition = dashboardLf.indexOf("\n      - type:", titlePosition + 1);
  const block = dashboardLf.slice(
    Math.max(0, dashboardLf.lastIndexOf("\n      - type:", titlePosition)),
    nextCardPosition < 0 ? dashboardLf.length : nextCardPosition,
  );
  check(
    titlePosition >= 0 && contract.slice(1).every((token) => block.includes(token)),
    `Aurora history preserves exact raw/power semantics for ${contract[0]}`,
  );
}
const compactPageBlock = (page) => {
  const marker = `      - type: custom:hoymiles-aurora-compact-page-card\n        page: ${page}\n`;
  const start = dashboardLf.indexOf(marker);
  const nextView = start < 0 ? -1 : dashboardLf.indexOf("\n  - title:", start + marker.length);
  return start < 0 ? "" : dashboardLf.slice(start, nextView < 0 ? dashboardLf.length : nextView);
};
const compactHistoryContracts = {
  pv: [
    "history_hours: 24",
    "history_value_mode: power",
    "history_digits: 2",
    "sensor.hoymiles_hit_pv1_power_direct",
    "sensor.hoymiles_hit_pv2_power_direct",
    "sensor.hoymiles_hit_pv3_power_direct",
    "sensor.hoymiles_hit_pv4_power_direct",
    "sensor.hoymiles_hit_overview_generator_active_power",
  ],
  battery: [
    "history_hours: 24",
    "history_value_mode: power",
    "history_digits: 2",
    "sensor.hoymiles_hit_overview_battery_power",
    "sensor.hoymiles_hit_overview_battery_soc",
    "history_visual_mode: battery_soc",
    "secondary_history_entities: []",
  ],
  energy: [
    "history_hours: 24",
    "history_value_mode: power",
    "history_digits: 2",
    "sensor.hoymiles_hit_overview_pv_total_power",
    "sensor.hoymiles_actual_load_power",
    "sensor.hoymiles_hit_overview_battery_power",
    "history_visual_mode: energy_mix",
  ],
};
for (const [page, tokens] of Object.entries(compactHistoryContracts)) {
  const block = compactPageBlock(page);
  check(
    block.length > 0 && tokens.every((token) => block.includes(token)),
    `Aurora compact ${page} page preserves its exact recorder-history configuration`,
  );
}
const compactPageSource = cardSource.slice(
  cardSource.indexOf("class HoymilesAuroraCompactPageCard"),
  cardSource.indexOf('if (!customElements.get("hoymiles-aurora-compact-page-card"))'),
);
check(
  compactPageSource.includes('document.createElement("hoymiles-aurora-history-card")')
    && compactPageSource.includes("this._config.history_hours")
    && compactPageSource.includes("this._config.history_value_mode")
    && compactPageSource.includes("this._config.history_unit")
    && compactPageSource.includes("this._config.history_digits")
    && compactPageSource.includes("this._config.secondary_history_hours")
    && compactPageSource.includes("this._config.secondary_history_value_mode")
    && compactPageSource.includes("this._config.secondary_history_unit")
    && compactPageSource.includes("this._config.secondary_history_digits")
    && compactPageSource.includes('visual_mode: this._config.history_visual_mode || (this._config.page === "pv" ? "pv_sources" : null)')
    && compactPageSource.includes(': this._config.history_entities')
    && compactPageSource.includes('dash: series.dash === true || String(series.name || "").trim().toUpperCase() === "GEN"')
    && compactPageSource.includes("entities: this._config.secondary_history_entities"),
  "compact pages mount pv_sources, battery_soc and energy_mix histories and render GEN distinctly",
);
const overviewComponentSource = cardSource.slice(
  cardSource.indexOf("class HoymilesAuroraOverviewCard"),
  cardSource.indexOf('if (!customElements.get("hoymiles-aurora-overview-card"))'),
);
check(
  overviewComponentSource.includes('document.createElement("hoymiles-aurora-history-card")')
    && overviewComponentSource.includes("hours_to_show: 24")
    && overviewComponentSource.includes('value_mode: "power"')
    && overviewComponentSource.includes("digits: 2")
    && overviewComponentSource.includes('layout: "overview"'),
  "composed overview retains one interactive 24-hour signed-power history chart",
);
check(
  (dashboardLf.match(/type: statistics-graph/g) || []).length === 13
    && (dashboardLf.match(/stat_types:\s*\n\s+- change/g) || []).length === 13
    && dashboardLf.includes("type: custom:hoymiles-aurora-profit-card")
    && !/stat_types:\s*\n\s+- (?:mean|max|min)/.test(dashboardLf),
  "native energy statistics remain; earnings use their own complete net-economics archive",
);
const rcmHistoryPosition = dashboardLf.indexOf(
  "title: Napięcie L1/L2/L3 i średnia 10-minutowa — ostatnie 24 godziny",
);
const rcmHistoryEnd = dashboardLf.indexOf("\n      - type:", rcmHistoryPosition + 1);
const rcmHistoryBlock = dashboardLf.slice(rcmHistoryPosition, rcmHistoryEnd);
check(
  rcmHistoryBlock.includes("inspection_mode: rcm_voltage")
    && rcmHistoryBlock.includes("inspection_threshold_v: 253")
    && ["phase_l1", "phase_l2", "phase_l3", "average_10m"].every((role) => rcmHistoryBlock.includes(`inspection_role: ${role}`)),
  "RCEm history dashboard contract binds actual phases, 10-minute average and the explicit 253 V threshold",
);
check(
  (dashboardLf.match(/^\s+timeline_entity: sensor\.hoymiles_hit_rce_automation_plan_timeline$/gm) || []).length === 1,
  "the single compact RCE price chart binds its policy timeline for direct slot inspection",
);
const rcmSettingsPosition = dashboardLf.indexOf(
  "title: Ochrona napięciowa — ustawienia",
);
const rcmSettingsEnd = dashboardLf.indexOf(
  "\n      - type: custom:hoymiles-aurora-disclosure-card",
  rcmSettingsPosition,
);
const rcmSettingsBlock = dashboardLf.slice(rcmSettingsPosition, rcmSettingsEnd);
check(
  rcmSettingsPosition >= 0
    && rcmSettingsEnd > rcmSettingsPosition
    && rcmSettingsBlock.includes("readiness_title: Gotowość konfiguracji")
    && !rcmSettingsBlock.includes("readiness_entity:")
    && (rcmSettingsBlock.match(/requirement: required/g) || []).length === 2,
  "RCEm settings readiness reports required-field completion without treating a text plan state as a green boolean",
);
check(
  dashboardLf.includes("title: Plan sprzedaży w skrócie")
    && dashboardLf.includes("plan.attributes.requested_export_power_kw")
    && dashboardLf.includes("plan.attributes.planned_revenue_pln")
    && dashboardLf.includes("plan.attributes.battery_wear_cost_pln")
    && dashboardLf.includes("plan.attributes.control_reserve_energy_kwh")
    && dashboardLf.includes("slots | length")
    && dashboardLf.includes("plan.attributes.result_current is sameas true")
    && dashboardLf.includes("plan.attributes.recalculation_pending is sameas false")
    && dashboardLf.includes("next.date ~ ' · ' ~ next.start ~ '–' ~ next.end"),
  "simple RCE summary is freshness-gated and exposes percent/kW, nearest slot, wear and reserve",
);
check(
  dashboardLf.includes("title: Plan, gotowość i wykonanie")
    && !dashboardLf.includes("supervisor.attributes.command_snapshot")
    && !dashboardLf.includes("supervisor.attributes.readback_result")
    && dashboardLf.includes("supervisor.attributes.expected_readback")
    && dashboardLf.includes("expected.base_ems_generation")
    && dashboardLf.includes("supervisor.attributes.physical_verification")
    && dashboardLf.includes("physical.evidence")
    && !dashboardLf.includes("sensor.hoymiles_tariff_grid_charge_power")
    && dashboardLf.includes("next.grid_import_kwh")
    && dashboardLf.includes("next.stored_energy_kwh")
    && dashboardLf.includes("next.direct_load_kwh")
    && dashboardLf.includes("plan.attributes.model_input_battery_soc_percent")
    && dashboardLf.includes("Pomiar zgodności (legacy, bez authority)")
    && dashboardLf.includes("Brak dostawcy fizycznego rozdziału mocy")
    && dashboardLf.includes("plan.attributes.planned_cost_pln")
    && dashboardLf.includes("plan.attributes.estimated_savings_pln")
    && dashboardLf.includes("Spodziewana oszczędność")
    && dashboardLf.includes("physical_labels.get(physical_status")
    && dashboardLf.includes("'confirmed': 'Potwierdzone fizycznie'")
    && dashboardLf.includes("Wynik zweryfikowany sieć → magazyn")
    && dashboardLf.includes("Wynik zweryfikowany sieć → dom")
    && !dashboardLf.includes("Wynik zweryfikowany grid → magazyn"),
  "simple tariff summary freshness-gates authority and separates plan, FC03, physical, estimated and legacy evidence",
);
check(
  dashboardLf.includes("title: Decyzja i najbliższy plan")
    && dashboardLf.includes("plan.attributes.voltage_l1_v")
    && dashboardLf.includes("plan.attributes.voltage_l2_v")
    && dashboardLf.includes("plan.attributes.voltage_l3_v")
    && dashboardLf.includes("maximum_phase")
    && dashboardLf.includes("risk_labels")
    && dashboardLf.includes("action_labels")
    && dashboardLf.includes("horizon_labels.get(horizon, 'brak danych')")
    && dashboardLf.includes("'today': 'dzisiaj', 'tomorrow': 'jutro', 'none': 'brak'")
    && dashboardLf.includes("Ostatni plan — bez prawa wykonania.** RCEm"),
  "simple RCEm summary freshness-gates readiness and uses natural voltage, risk and action labels",
);
check(
  dashboardLf.includes("title: Stan bezpiecznego zatrzymania")
    && dashboardLf.includes("supervisor.attributes.master_stop")
    && dashboardLf.includes("'requested': 'Wysyłanie polecenia bezpiecznego zatrzymania'")
    && dashboardLf.includes("'completed': 'Sukces — bezpieczny stan potwierdzony'")
    && dashboardLf.includes("'failed': 'Błąd — bezpieczne zatrzymanie nie powiodło się'")
    && dashboardLf.includes("stop.requested_at")
    && dashboardLf.includes("stop.completed_at")
    && dashboardLf.includes("stop.off_grid_preserved")
    && dashboardLf.includes("supervisor.attributes.rollback_status")
    && dashboardLf.includes("skorelowanego payloadu FC03")
    && dashboardLf.includes("sensor.hoymiles_hit_ems_mode_readback_code")
    && !dashboardLf.includes("sensor.hoymiles_hit_ems_control_readback_generation"),
  "safe-stop status comes from the persisted MASTER STOP transaction without adding a non-baseline entity",
);
check(
  dashboardLf.includes("**Prognoza PV dla okna ryzyka:**")
    && dashboardLf.includes("plan.attributes.target_soc_before_risk_percent")
    && dashboardLf.includes("plan.attributes.recommended_charge_limit_percent")
    && dashboardLf.includes("plan.attributes.recommended_charge_power_kw")
    && dashboardLf.includes("plan.attributes.recommended_export_limit_percent"),
  "simple RCEm summary exposes direct forecast, SOC target, charge and export limits",
);
const nonBmsBatteryTerms = dashboardLf.split("\n").filter((line) => (
  /\bbateria\b|\bbaterii\b/i.test(line)
  && !line.includes("(BMS)")
  && !line.includes("path: bateria")
));
check(
  nonBmsBatteryTerms.length === 0
    && dashboardLf.replace(/\s+/g, " ").includes("falownik najpierw zasila bieżące odbiorniki domu"),
  "canonical Polish uses magazyn energii outside explicit BMS labels and keeps the corrected home-load sentence",
);
check(
  cardSource.includes('grid_to_load_today: "Szacunkowo do domu"')
    && cardSource.includes('grid_to_battery_today: "Szacunkowo do magazynu"')
    && cardSource.includes('grid_to_load_today: "Estimated to home"')
    && cardSource.includes('grid_to_battery_today: "Estimated to battery"'),
  "providerless overview grid allocations are explicitly labelled as estimates in PL and EN",
);
check(
  dashboardLf.includes("title: Produkcja dzienna · zakres do 90 dni")
    && dashboardLf.includes("days_to_show: 90")
    && !dashboardLf.includes("Produkcja dzienna — ostatnie 90 dni")
    && assetGeneratorSource.includes('"Daily production · range up to 90 days"'),
  "PV daily chart describes a maximum 90-day range without claiming full recorder coverage",
);
const batteryDetailStart = dashboardLf.indexOf("title: Magazyn energii — stan, moc i limity BMS");
const batteryDetailEnd = dashboardLf.indexOf("\n      - type:", batteryDetailStart + 1);
const batteryDetailBlock = dashboardLf.slice(batteryDetailStart, batteryDetailEnd);
const batteryPageBlock = compactPageBlock("battery");
check(
  batteryPageBlock.includes("shared_inputs_entity: sensor.hoymiles_hit_ems_shared_inputs")
    && compactPageSource.includes("_sharedBmsPower(key)")
    && compactPageSource.includes('typeof value === "number"')
    && compactPageSource.includes("sample?.fresh === true")
    && compactPageSource.includes("Number.isFinite(value)")
    && !compactPageSource.includes("const value = Number(sample?.value)")
    && compactPageSource.includes('this._sharedBmsPower("maximum_charge_power_kw")')
    && compactPageSource.includes('this._sharedBmsPower("maximum_discharge_power_kw")')
    && compactPageSource.includes('charge === null ? "—"')
    && compactPageSource.includes('discharge === null ? "—"')
    && dashboardLf.includes("title: Dostępna moc magazynu według BMS")
    && dashboardLf.includes("bms.maximum_charge_power_kw")
    && dashboardLf.includes("bms.maximum_discharge_power_kw")
    && dashboardLf.includes("if charge.fresh")
    && dashboardLf.includes("if discharge.fresh")
    && batteryDetailBlock.includes("sensor.hoymiles_hit_maximum_charge_current")
    && batteryDetailBlock.includes("sensor.hoymiles_hit_maximum_discharge_current"),
  "battery composite fails closed on backend kW capabilities while BMS currents remain in details",
);
const meterSummaryStart = dashboardLf.indexOf("title: Licznik sieci — podsumowanie");
const meterSummaryEnd = dashboardLf.indexOf("title: Szczegóły licznika sieci", meterSummaryStart);
const meterSummaryBlock = dashboardLf.slice(meterSummaryStart, meterSummaryEnd);
check(
  meterSummaryBlock.includes("sensor.hoymiles_hit_grid_energy_buy_total")
    && meterSummaryBlock.includes("sensor.hoymiles_hit_grid_energy_sell_total")
    && meterSummaryBlock.includes("sensor.hoymiles_hit_external_pv_energy_total"),
  "both simple meter summaries expose existing total-energy entities",
);
check(
  assetGeneratorSource.includes("I2 final closeout adds compact, evidence-only summaries")
    && assetGeneratorSource.includes('"Plan sprzedaży w skrócie": "Energy-sales plan at a glance"')
    && assetGeneratorSource.includes('"Plan, gotowość i wykonanie": "Plan, readiness, and execution"')
    && assetGeneratorSource.includes('"Dostępna moc magazynu według BMS": "Available battery-storage power from BMS"'),
  "new canonical PL summaries have exact English generator replacements without a broad Battery rewrite",
);

const Card = registry.get("hoymiles-automation-planner-card");
equal(Card.getStubConfig(), EXPECTED_BINDINGS, "exact stub bindings");
check(() => Card, "custom element registered");
const VariantAEms = registry.get("hoymiles-aurora-variant-a-ems-page-card");
check(Boolean(VariantAEms), "Variant A EMS page custom element is registered");

// The installed dashboard uses Variant A, so the live LOAD provenance must be
// checked on that actual card as well as the legacy standalone planner card.
for (const language of ["pl", "en"]) {
  const card = new VariantAEms();
  card._config = VariantAEms.getStubConfig();
  card._hass = hassFixture(language);
  const rce = card._config.rce_plan_entity;
  const tariff = card._config.tariff_plan_entity;
  card._hass.states[rce] = { state: "ready", attributes: {
    recorder_load_recent_4d_kwh: { "2026-09-25": 15.4 },
    recorder_load_average_4d_kwh: 15.4,
    selected_average_daily_load_kwh: 17,
    load_model_source: "provisional_history_or_fallback",
    load_model_quality: "fallback",
    history_complete: false,
    result_current: true,
    recalculation_pending: false,
  } };
  card._hass.states[tariff] = { state: "ready", attributes: {
    model_input_average_daily_load_kwh: 17,
    load_profile_source: "configured_daily_fallback",
    result_current: true,
    recalculation_pending: false,
  } };
  const shown = card._loadSourcePresentation();
  card._root = new FakeElement("ha-card");
  for (const key of ["history", "rce", "tariff"]) {
    for (const field of ["value", "note"]) {
      const node = new FakeElement("span");
      node.dataset[`load${key[0].toUpperCase()}${key.slice(1)}${field[0].toUpperCase()}${field.slice(1)}`] = "";
      card._root.append(node);
    }
  }
  card._patchLoadSources();
  check(card._root.querySelector('[data-load-history-value]')?.textContent === shown.history.value
    && card._root.querySelector('[data-load-rce-value]')?.textContent === shown.rce.value
    && card._root.querySelector('[data-load-tariff-value]')?.textContent === shown.tariff.value,
    `${language} Variant A actual DOM shows three distinct LOAD sources`);
  check(shown.history.value.includes("15,4") || shown.history.value.includes("15.4"),
    `${language} Variant A displays measured LOAD separately`);
  check(shown.history.note.includes(language === "pl" ? "1 zakwalifikowana doba" : "1 qualified day"),
    `${language} Variant A displays actual one-day coverage`);
  check(shown.rce.value.includes("17") && shown.tariff.value.includes("17")
    && shown.rce.note.includes(language === "pl" ? "historia niepełna" : "history incomplete")
    && shown.tariff.note.includes(language === "pl" ? "zastępcza" : "fallback"),
    `${language} Variant A separates model inputs from measured LOAD`);
  card._hass.states[rce].attributes.result_current = false;
  const stale = card._loadSourcePresentation();
  check(shown.history.value !== "—" && stale.history.value === "—"
    && stale.rce.value === "—" && stale.history.note.includes(language === "pl" ? "nieaktualny" : "not current"),
    `${language} stale RCE cannot display retained history or model input as current`);
  card._hass.states[rce].attributes.result_current = true;
  card._hass.states[rce].state = "unavailable";
  card._hass.states[tariff].state = "unknown";
  const unavailable = card._loadSourcePresentation();
  check(unavailable.history.value === "—"
    && unavailable.rce.value === "—" && unavailable.tariff.value === "—"
    && unavailable.rce.note.includes(language === "pl" ? "nieaktualny" : "not current")
    && unavailable.tariff.note.includes(language === "pl" ? "nieaktualny" : "not current"),
    `${language} unavailable plan entities cannot display cached model inputs`);
}

for (const language of ["pl", "en"]) {
  const Energy = registry.get("hoymiles-aurora-energy-card");
  const card = new Energy();
  card._config = {
    average_load_entity: "sensor.hoymiles_load_average_4_days",
    average_load_model_entity: "sensor.hoymiles_hit_rce_optimized_plan",
  };
  card._hass = hassFixture(language);
  card._hass.states[card._config.average_load_entity] = {
    state: "17", attributes: { unit_of_measurement: "kWh" },
  };
  card._hass.states[card._config.average_load_model_entity] = { state: "ready", attributes: {
    recorder_load_recent_4d_kwh: { "2026-09-25": 15.4 },
    recorder_load_average_4d_kwh: 15.4,
    selected_average_daily_load_kwh: 17,
    result_current: true,
    recalculation_pending: false,
  } };
  const history = card._averageLoadPresentation();
  check(history.label.includes(language === "pl" ? "1 z 4 dób" : "1 of 4 days")
    && history.value.includes(language === "pl" ? "15,4" : "15.4"),
    `${language} active Aurora overview uses the qualified RCE snapshot despite template fallback`);
  card._hass.states[card._config.average_load_model_entity].attributes.recorder_load_recent_4d_kwh = {};
  const fallback = card._averageLoadPresentation();
  check(fallback.label.includes(language === "pl" ? "użyte przez EMS" : "used by EMS")
    && fallback.value.includes("17") && !fallback.label.includes("4 days"),
    `${language} unqualified history does not masquerade as model input`);
  card._hass.states[card._config.average_load_model_entity].state = "unavailable";
  check(card._averageLoadPresentation().value === "—",
    `${language} unavailable RCE snapshot hides retained history and fallback`);

  const Overview = registry.get("hoymiles-aurora-overview-card");
  const overview = new Overview();
  overview._config = card._config;
  overview._hass = card._hass;
  check(overview._averageLoadKwh() === null,
    `${language} PowerFlow hides template fallback when RCE snapshot is unavailable`);
  card._hass.states[card._config.average_load_model_entity].state = "ready";
  check(overview._averageLoadKwh() === 17,
    `${language} PowerFlow uses the current RCE model input rather than the template helper`);
}

async function testOperationOutcomes() {
const operationFixture = (language = "pl") => {
  const hass = hassFixture(language);
  for (const entityId of [
    "input_boolean.hoymiles_ems_policy_preference_rce",
    EXPECTED_BINDINGS.rce_enabled_entity,
    "input_boolean.hoymiles_ems_supervisor_allow_rce",
  ]) hass.states[entityId] = { state: "off", attributes: {} };
  hass.states[EXPECTED_BINDINGS.supervisor_entity].attributes.ems_paused = false;
  const card = new VariantAEms();
  card._config = VariantAEms.getStubConfig();
  card._hass = hass;
  card._mounted = true;
  const status = new FakeElement("p");
  status.dataset.error = "";
  status.setAttribute("data-error", "");
  status.setAttribute("data-outcome", "empty");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  card.shadowRoot.append(status);
  card._patch = () => {
    status.setAttribute("data-outcome", card._operationOutcome?.status || "empty");
    status.textContent = card._operationMessage(card._copy());
  };
  card._patch();
  return { card, hass };
};

const rceOperation = (card) => card._service(
  "rce",
  "hoymiles_hit_modbus",
  "set_policy_enabled",
  { policy: "rce", enabled: true },
);

{
  const { card, hass } = operationFixture("pl");
  const status = card.shadowRoot.querySelector("[data-error]");
  let calls = 0;
  const focusedControl = new FakeElement("button");
  fakeDocument.activeElement = focusedControl;
  ledger.reset();
  hass.callService = async () => {
    calls += 1;
    hass.states["input_boolean.hoymiles_ems_policy_preference_rce"] = { state: "on", attributes: {} };
    hass.states[EXPECTED_BINDINGS.rce_enabled_entity] = { state: "on", attributes: {} };
    hass.states["input_boolean.hoymiles_ems_supervisor_allow_rce"] = { state: "on", attributes: {} };
  };
  card._operationDelay = async () => {};
  const request = rceOperation(card);
  check(
    status.textContent.includes("Żądanie wysłane")
      && card._pending.has("rce"),
    "actual Variant A service method exposes a sent state while preserving the pending gate",
  );
  await request;
  check(
    calls === 1
      && status.textContent.includes("Operacja potwierdzona")
      && status.getAttribute("data-outcome") === "confirmed",
    "PL DOM reports success only from the reread target state",
  );
  check(
    fakeDocument.activeElement === focusedControl && ledger.focusCalls === 0,
    "operation settlement preserves the user's existing focus",
  );
}

{
  const { card, hass } = operationFixture("pl");
  const status = card.shadowRoot.querySelector("[data-error]");
  hass.callService = async () => {
    hass.states["input_boolean.hoymiles_ems_policy_preference_rce"] = { state: "on", attributes: {} };
    hass.states[EXPECTED_BINDINGS.rce_enabled_entity] = { state: "on", attributes: {} };
    hass.states["input_boolean.hoymiles_ems_paused"] = { state: "on", attributes: {} };
    hass.states["input_select.hoymiles_ems_supervisor_mode"] = { state: "Off", attributes: { options: ["Off", "Active"] } };
    hass.states[EXPECTED_BINDINGS.supervisor_entity].attributes.ems_paused = true;
    throw new Error("allowed write failed");
  };
  card._operationDelay = async () => {};
  await rceOperation(card);
  check(
    status.textContent.includes("Operacja wykonana częściowo")
      && status.textContent.includes("EMS jest wstrzymany")
      && !status.textContent.includes("nie został zmieniony"),
    "PL DOM reports partial policy writes and appends pause only from the current three-source state",
  );
}

{
  const { card, hass } = operationFixture("en");
  const status = card.shadowRoot.querySelector("[data-error]");
  hass.callService = async () => { throw new Error("request failed before state evidence"); };
  card._operationDelay = async () => {};
  await rceOperation(card);
  check(
    status.textContent.includes("not fully confirmed")
      && status.textContent.includes("Check the current EMS state")
      && !status.textContent.includes("was not changed"),
    "EN DOM reports an unknown result instead of promising an unchanged installation",
  );
}

{
  const { card, hass } = operationFixture("en");
  const status = card.shadowRoot.querySelector("[data-error]");
  hass.callService = async () => {
    hass.states["input_boolean.hoymiles_ems_paused"] = { state: "on", attributes: {} };
    hass.states["input_select.hoymiles_ems_supervisor_mode"] = { state: "Off", attributes: { options: ["Off", "Active"] } };
    throw new Error("supervisor pause evidence delayed");
  };
  card._operationDelay = async () => {};
  await rceOperation(card);
  check(
    status.textContent.includes("completed partially")
      && !status.textContent.includes("EMS is paused"),
    "pause suffix is withheld when the current Supervisor state does not confirm the helper and mode pair",
  );
}

{
  const { card, hass } = operationFixture("en");
  const status = card.shadowRoot.querySelector("[data-error]");
  let settleAttempts = 0;
  hass.callService = async () => {};
  card._operationDelay = async (milliseconds) => {
    if (milliseconds === 200 && ++settleAttempts === 1) {
      hass.states["input_boolean.hoymiles_ems_policy_preference_rce"] = { state: "on", attributes: {} };
      hass.states[EXPECTED_BINDINGS.rce_enabled_entity] = { state: "on", attributes: {} };
      hass.states["input_boolean.hoymiles_ems_supervisor_allow_rce"] = { state: "on", attributes: {} };
    }
  };
  await rceOperation(card);
  check(
    settleAttempts === 1
      && status.textContent.includes("Operation confirmed"),
    "actual service method keeps waiting through a delayed entity update and then confirms from reread state",
  );
}

{
  const { card, hass } = operationFixture("pl");
  const status = card.shadowRoot.querySelector("[data-error]");
  hass.callService = () => {
    hass.states["input_boolean.hoymiles_ems_policy_preference_rce"] = { state: "on", attributes: {} };
    hass.states[EXPECTED_BINDINGS.rce_enabled_entity] = { state: "on", attributes: {} };
    hass.states["input_boolean.hoymiles_ems_supervisor_allow_rce"] = { state: "on", attributes: {} };
    return new Promise(() => {});
  };
  card._operationDelay = async () => {};
  await rceOperation(card);
  check(
    status.textContent.includes("Operacja potwierdzona"),
    "client response timeout after successful backend work is resolved by current entity evidence",
  );
}

{
  const { card, hass } = operationFixture("pl");
  let release;
  let calls = 0;
  hass.callService = () => {
    calls += 1;
    return new Promise((resolve) => { release = resolve; });
  };
  let releaseTimeout;
  card._operationDelay = () => new Promise((resolve) => { releaseTimeout = resolve; });
  const first = rceOperation(card);
  const second = rceOperation(card);
  check(calls === 0 && card._pending.has("rce"), "double click is rejected before a second service dispatch");
  await Promise.resolve();
  check(calls === 1, "pending gate permits exactly one writing service call");
  hass.states["input_boolean.hoymiles_ems_policy_preference_rce"] = { state: "on", attributes: {} };
  hass.states[EXPECTED_BINDINGS.rce_enabled_entity] = { state: "on", attributes: {} };
  hass.states["input_boolean.hoymiles_ems_supervisor_allow_rce"] = { state: "on", attributes: {} };
  release();
  await first;
  await second;
  if (releaseTimeout) releaseTimeout();
  check(calls === 1 && !card._pending.has("rce"), "double click never retries the writing operation");
}

for (const width of [390, 1440]) {
  fakeWindow.innerWidth = width;
  fakeDocument.documentElement.clientWidth = width;
  fakeDocument.documentElement.scrollWidth = width;
  const { card } = operationFixture("pl");
  const status = card.shadowRoot.querySelector("[data-error]");
  check(
    Boolean(status)
      && status.getAttribute("role") === "status"
      && status.getAttribute("aria-live") === "polite"
      && cardSource.includes("min-height:42px")
      && cardSource.includes('.error[data-outcome="empty"] { visibility:hidden; }')
      && fakeDocument.documentElement.scrollWidth === width,
    `operation result has a stable live-status slot without page overflow at ${width}px`,
  );
}
}

const variantAZeroExportHass = () => {
  const hass = hassFixture();
  const freshAt = new Date().toISOString();
  const planState = hass.states[EXPECTED_BINDINGS.rce_plan_entity];
  planState.last_updated = freshAt;
  Object.assign(planState.attributes, {
    status_code: "home_energy_shortage",
    result_current: true,
    recalculation_pending: false,
    input_revision: 11,
    gcf_execution_data_fresh: true,
    gcf_enabled: true,
    gcf_export_limit_percent: 0,
    planned_export_kwh: 0,
    planned_slots: [],
  });
  const timelineState = hass.states[EXPECTED_BINDINGS.rce_timeline_entity];
  timelineState.last_updated = freshAt;
  Object.assign(timelineState.attributes, {
    schema_version: 2,
    policy_id: "rce",
    input_revision: 11,
    result_current: true,
    recalculation_pending: false,
    generated_at: freshAt,
    plan_entity_id: EXPECTED_BINDINGS.rce_plan_entity,
    plan_revision_scope: "runtime",
  });
  timelineState.state = "current";
  timelineState.attributes.points = timelineState.attributes.points.map((item) => ({
    ...item,
    selected: false,
    action_code: "idle",
    policy: { ...item.policy, planned_export_kwh: 0 },
  }));
  timelineState.attributes.point_count = timelineState.attributes.points.length;
  hass.states["input_boolean.hoymiles_ems_supervisor_allow_rce"] = {
    state: "on",
    attributes: {},
  };
  hass.states["binary_sensor.hoymiles_ems_execution_ready"] = {
    state: "off",
    attributes: {},
  };
  return hass;
};

const variantAZeroExport = new VariantAEms();
variantAZeroExport._config = VariantAEms.getStubConfig();
variantAZeroExport._hass = variantAZeroExportHass();
check(
  variantAZeroExport._verifiedRceZeroExportPlan() === true,
  "Variant A derives verified zero export from the current coherent GCF cohort even when status_code explains a home-energy shortage",
);
const rejectedZeroExportCases = [
  ["source pending", (hass) => { hass.states[EXPECTED_BINDINGS.rce_plan_entity].attributes.recalculation_pending = true; }],
  ["timeline pending", (hass) => { hass.states[EXPECTED_BINDINGS.rce_timeline_entity].state = "pending"; }],
  ["revision mismatch", (hass) => { hass.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.input_revision = 12; }],
  ["stale GCF", (hass) => { hass.states[EXPECTED_BINDINGS.rce_plan_entity].attributes.gcf_execution_data_fresh = false; }],
  ["non-numeric GCF limit", (hass) => { hass.states[EXPECTED_BINDINGS.rce_plan_entity].attributes.gcf_export_limit_percent = "0"; }],
  ["stale plan timestamp", (hass) => {
    hass.states[EXPECTED_BINDINGS.rce_plan_entity].last_updated = new Date(Date.now() - 300_001).toISOString();
  }],
  ["future timeline timestamp", (hass) => {
    hass.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.generated_at = new Date(Date.now() + 10_000).toISOString();
  }],
  ["selected export point", (hass) => {
    const point = hass.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.points[0];
    point.selected = true;
    point.action_code = "export";
    point.policy.planned_export_kwh = 1;
  }],
  ["control conflict", (hass) => { hass.states[EXPECTED_BINDINGS.control_conflict_entity].state = "on"; }],
];
for (const [label, mutate] of rejectedZeroExportCases) {
  const hass = variantAZeroExportHass();
  mutate(hass);
  const candidate = new VariantAEms();
  candidate._config = VariantAEms.getStubConfig();
  candidate._hass = hass;
  check(
    candidate._verifiedRceZeroExportPlan() === false,
    `Variant A zero-export helper fails closed for ${label}`,
  );
}

const variantATariffNoChargeHass = () => {
  const hass = hassFixture();
  const freshAt = new Date().toISOString();
  const planState = hass.states[EXPECTED_BINDINGS.tariff_plan_entity];
  planState.last_updated = freshAt;
  Object.assign(planState.attributes, {
    status_code: "no_charge_needed",
    result_current: true,
    recalculation_pending: false,
    control_inputs_fresh: true,
    input_revision: 12,
    planned_slots: [],
    planned_grid_import_kwh: 0,
    planned_stored_energy_kwh: 0,
    planned_direct_load_kwh: 0,
  });
  const timelineState = hass.states[EXPECTED_BINDINGS.tariff_timeline_entity];
  timelineState.last_updated = freshAt;
  Object.assign(timelineState.attributes, {
    schema_version: 2,
    policy_id: "tariff",
    input_revision: 12,
    result_current: true,
    recalculation_pending: false,
    generated_at: freshAt,
    plan_entity_id: EXPECTED_BINDINGS.tariff_plan_entity,
    plan_revision_scope: "runtime",
    quality: "complete",
  });
  timelineState.state = "current";
  timelineState.attributes.points = timelineState.attributes.points.map((item) => ({
    ...item,
    selected: false,
    action_code: "idle",
    policy: {
      ...item.policy,
      planned_import_kwh: 0,
      stored_energy_kwh: 0,
      direct_load_kwh: 0,
      planned_charge_kw: 0,
    },
  }));
  timelineState.attributes.point_count = timelineState.attributes.points.length;
  hass.states[EXPECTED_BINDINGS.control_conflict_entity].state = "off";
  hass.states["input_boolean.hoymiles_ems_supervisor_allow_tariff"] = {
    state: "on",
    attributes: {},
  };
  return hass;
};

const variantATariffNoCharge = new VariantAEms();
variantATariffNoCharge._config = VariantAEms.getStubConfig();
variantATariffNoCharge._hass = variantATariffNoChargeHass();
check(
  variantATariffNoCharge._verifiedTariffNoChargePlan() === true,
  "Variant A accepts only a current, coherent tariff result that explicitly proves no top-up is needed",
);
const rejectedTariffNoChargeCases = [
  ["source pending", (hass) => { hass.states[EXPECTED_BINDINGS.tariff_plan_entity].attributes.recalculation_pending = true; }],
  ["timeline pending", (hass) => { hass.states[EXPECTED_BINDINGS.tariff_timeline_entity].state = "pending"; }],
  ["revision mismatch", (hass) => { hass.states[EXPECTED_BINDINGS.tariff_timeline_entity].attributes.input_revision = 13; }],
  ["stale plan timestamp", (hass) => {
    hass.states[EXPECTED_BINDINGS.tariff_plan_entity].last_updated = new Date(Date.now() - 300_001).toISOString();
  }],
  ["future timeline timestamp", (hass) => {
    hass.states[EXPECTED_BINDINGS.tariff_timeline_entity].attributes.generated_at = new Date(Date.now() + 10_000).toISOString();
  }],
  ["string zero", (hass) => { hass.states[EXPECTED_BINDINGS.tariff_plan_entity].attributes.planned_grid_import_kwh = "0"; }],
  ["selected tariff point", (hass) => {
    const point = hass.states[EXPECTED_BINDINGS.tariff_timeline_entity].attributes.points[0];
    point.selected = true;
    point.action_code = "battery_charge";
    point.policy.planned_import_kwh = 1;
  }],
  ["control conflict", (hass) => { hass.states[EXPECTED_BINDINGS.control_conflict_entity].state = "on"; }],
];
for (const [label, mutate] of rejectedTariffNoChargeCases) {
  const hass = variantATariffNoChargeHass();
  mutate(hass);
  const candidate = new VariantAEms();
  candidate._config = VariantAEms.getStubConfig();
  candidate._hass = hass;
  check(
    candidate._verifiedTariffNoChargePlan() === false,
    `Variant A tariff no-charge helper fails closed for ${label}`,
  );
}

const tariffNoChargeText = {};
const tariffNoChargeDay = {};
const tariffNoChargeNode = { dataset: {}, hidden: false, disabled: false, querySelector() { return this; } };
const tariffNoChargeChart = { dataset: {}, innerHTML: "" };
variantATariffNoCharge._mounted = true;
const tariffNoChargeCanonicalSlots = variantATariffNoCharge._baselineSlots();
let tariffNoChargeCanonicalMode = "current";
variantATariffNoCharge._canonicalSlots = (mode = "current") => {
  if (mode === "current") return tariffNoChargeCanonicalMode === "current" ? tariffNoChargeCanonicalSlots : null;
  if (mode === "retained") return tariffNoChargeCanonicalMode === "retained" ? tariffNoChargeCanonicalSlots : null;
  return null;
};
variantATariffNoCharge._baselineSlots = () => [];
variantATariffNoCharge._patchProfile = () => {};
variantATariffNoCharge._updateChartSelection = () => {};
variantATariffNoCharge._setAttribute = () => {};
variantATariffNoCharge._setDisabled = () => {};
variantATariffNoCharge._setNodeText = () => {};
variantATariffNoCharge._setText = (selector, value) => { tariffNoChargeText[selector] = String(value); };
variantATariffNoCharge._patchDayCard = (key, time, state, energy, status) => {
  tariffNoChargeDay[key] = { time, state, energy, status };
};
variantATariffNoCharge._root = {
  querySelector(selector) { return selector === "[data-chart]" ? tariffNoChargeChart : tariffNoChargeNode; },
  querySelectorAll() { return []; },
};
variantATariffNoCharge._patch();
check(
  tariffNoChargeText['[data-policy-meta="tariff"]'] === "Brak potrzeby doładowania"
    && tariffNoChargeText['[data-total="tariff"]'] === "0,0 kWh"
    && tariffNoChargeDay.tariff?.state === "Brak potrzeby doładowania — PV i bateria wystarczą"
    && tariffNoChargeDay.tariff?.energy === "0,0 kWh poboru"
    && tariffNoChargeDay.tariff?.status === "no-charge"
    && !tariffNoChargeDay.tariff.state.includes("Brak planu"),
  "Variant A EMS shows a verified no-charge tariff result as a completed plan, never as no plan",
);
const retainedTariffNoChargeHass = variantATariffNoChargeHass();
const retainedTariffPlan = retainedTariffNoChargeHass.states[EXPECTED_BINDINGS.tariff_plan_entity];
Object.assign(retainedTariffPlan.attributes, {
  result_current: false,
  recalculation_pending: true,
});
variantATariffNoCharge._hass = retainedTariffNoChargeHass;
variantATariffNoCharge._patch();
check(
  tariffNoChargeText['[data-policy-meta="tariff"]'] === "Brak potrzeby doładowania"
    && tariffNoChargeDay.tariff?.state === "Brak potrzeby doładowania — PV i bateria wystarczą"
    && tariffNoChargeDay.tariff?.status === "no-charge",
  "Variant A keeps the verified no-charge label when the tariff plan publishes pending before canonical state",
);
const retainedTariffTimeline = retainedTariffNoChargeHass.states[EXPECTED_BINDINGS.tariff_timeline_entity];
retainedTariffTimeline.state = "pending";
Object.assign(retainedTariffTimeline.attributes, {
  quality: "pending",
  blocker_code: "recalculation_pending",
  result_current: false,
  recalculation_pending: true,
  pending_input_revision: 13,
});
variantATariffNoCharge._patch();
check(
  tariffNoChargeText['[data-policy-meta="tariff"]'] === "Brak potrzeby doładowania"
    && tariffNoChargeDay.tariff?.state === "Brak potrzeby doładowania — PV i bateria wystarczą"
    && tariffNoChargeDay.tariff?.status === "no-charge",
  "Variant A keeps the verified no-charge label through the policy-timeline pending transition",
);
const retainedTariffCanonical = retainedTariffNoChargeHass.states[EXPECTED_BINDINGS.canonical_timeline_entity];
retainedTariffCanonical.state = "pending";
Object.assign(retainedTariffCanonical.attributes, {
  canonical_status: "pending",
  canonical_blocker_code: "source_recalculation_pending",
  result_current: false,
  recalculation_pending: true,
});
tariffNoChargeCanonicalMode = "retained";
variantATariffNoCharge._patch();
check(
  tariffNoChargeText['[data-policy-meta="tariff"]'] === "Brak potrzeby doładowania"
    && tariffNoChargeDay.tariff?.state === "Brak potrzeby doładowania — PV i bateria wystarczą"
    && tariffNoChargeDay.tariff?.energy === "0,0 kWh poboru"
    && tariffNoChargeDay.tariff?.status === "no-charge",
  "Variant A retains a previously verified no-charge result descriptively while its replacement is pending",
);
const replacedTariffNoChargeHass = variantATariffNoChargeHass();
const uneconomicTariffStatus =
  "Ładowanie pominięte — różnica cen nie pokrywa strat, zużycia baterii i wymaganego marginesu";
replacedTariffNoChargeHass.states[
  EXPECTED_BINDINGS.tariff_plan_entity
].state = uneconomicTariffStatus;
replacedTariffNoChargeHass.states[
  EXPECTED_BINDINGS.tariff_plan_entity
].attributes.status_code = "not_economically_beneficial";
tariffNoChargeCanonicalMode = "current";
variantATariffNoCharge._hass = replacedTariffNoChargeHass;
variantATariffNoCharge._patch();
check(
  variantATariffNoCharge._retainedTariffNoCharge === false
    && tariffNoChargeDay.tariff?.state === uneconomicTariffStatus
    && tariffNoChargeDay.tariff?.energy === "0,0 kWh poboru"
    && tariffNoChargeDay.tariff?.status === "source-status",
  "Variant A replaces the retained no-charge label with the current detailed tariff-plan status",
);
variantATariffNoCharge._retainedTariffNoCharge = true;
variantATariffNoCharge._retainedTariffNoChargeAt = Date.now();
variantATariffNoCharge._mount = () => {};
variantATariffNoCharge._patch = () => {};
variantATariffNoCharge.setConfig({
  ...VariantAEms.getStubConfig(),
  tariff_plan_entity: "sensor.rebound_tariff_plan",
});
check(
  variantATariffNoCharge._retainedTariffNoCharge === false
    && variantATariffNoCharge._retainedTariffNoChargeAt === 0,
  "Variant A clears descriptive tariff retention when its source binding changes",
);

const zeroExportText = {};
const zeroExportDay = {};
const zeroExportNode = { dataset: {}, hidden: false, disabled: false, querySelector() { return this; } };
const zeroExportChart = { dataset: {}, innerHTML: "" };
variantAZeroExport._mounted = true;
variantAZeroExport._canonicalSlots = () => [];
variantAZeroExport._baselineSlots = () => [];
variantAZeroExport._patchProfile = () => {};
variantAZeroExport._updateChartSelection = () => {};
variantAZeroExport._setAttribute = () => {};
variantAZeroExport._setDisabled = () => {};
variantAZeroExport._setNodeText = () => {};
variantAZeroExport._setText = (selector, value) => { zeroExportText[selector] = String(value); };
variantAZeroExport._patchDayCard = (key, time, state, energy) => {
  zeroExportDay[key] = { time, state, energy };
};
variantAZeroExport._root = {
  querySelector(selector) { return selector === "[data-chart]" ? zeroExportChart : zeroExportNode; },
  querySelectorAll() { return []; },
};
variantAZeroExport._patch();
check(
  zeroExportText['[data-policy-meta="rce"]'] === "Sprzedaż zablokowana — 0 export aktywny"
    && zeroExportDay.rce?.state === "Sprzedaż zablokowana — 0 export aktywny"
    && zeroExportDay.rce?.energy === "0,0 kWh sprzedaży"
    && !zeroExportDay.rce.state.includes("Brak planu"),
  "Variant A EMS reports a ready zero-export plan as an explicit blocked sale with exact zero energy",
);
const variantARetainedHass = hassFixture();
const variantARetainedStartMs = Math.ceil(Date.now() / (60 * 60_000)) * (60 * 60_000);
const variantARetainedStart = new Date(variantARetainedStartMs).toISOString();
const variantARetainedCanonical = canonicalTimeline(variantARetainedStart, 12, true);
variantARetainedCanonical.attributes.built_at = new Date().toISOString();
variantARetainedHass.states[EXPECTED_BINDINGS.canonical_timeline_entity] = variantARetainedCanonical;
variantARetainedHass.states[EXPECTED_BINDINGS.baseline_timeline_entity] = baselineTimeline(variantARetainedCanonical);
variantARetainedHass.states[EXPECTED_BINDINGS.rce_timeline_entity] = timeline(
  "rce", 11, 5, variantARetainedStart,
);
variantARetainedHass.states[EXPECTED_BINDINGS.tariff_timeline_entity] = timeline(
  "tariff", 12, 6, new Date(variantARetainedStartMs + 60 * 60_000).toISOString(),
);
variantARetainedHass.states[EXPECTED_BINDINGS.rcm_timeline_entity] = timeline(
  "rcm", 13, 7, new Date(variantARetainedStartMs + 2 * 60 * 60_000).toISOString(),
);
variantARetainedHass.states[
  EXPECTED_BINDINGS.rcm_timeline_entity
].attributes.points[0].action_code = "absorb_pv";
variantARetainedHass.states["input_select.hoymiles_ems_supervisor_mode"] = {
  state: "Active",
  attributes: { options: ["Off", "Active"] },
};
variantARetainedHass.states["binary_sensor.hoymiles_ems_execution_ready"] = {
  state: "on",
  attributes: {},
};
variantARetainedHass.states["input_boolean.hoymiles_ems_supervisor_allow_rce"] = {
  state: "on",
  attributes: {},
};
variantARetainedHass.states["input_boolean.hoymiles_ems_supervisor_allow_tariff"] = {
  state: "on",
  attributes: {},
};
variantARetainedHass.states["input_boolean.hoymiles_ems_supervisor_allow_rcm"] = {
  state: "on",
  attributes: {},
};
const variantARetained = new VariantAEms();
variantARetained._config = VariantAEms.getStubConfig();
variantARetained._hass = variantARetainedHass;
const variantARetainedReadiness = { textContent: "", dataState: "" };
const variantARetainedChart = { dataset: {}, innerHTML: "" };
const variantARetainedGeneric = { hidden: true, querySelector() { return {}; } };
const variantARetainedText = {};
const variantARetainedDay = {};
variantARetained._mounted = true;
variantARetained._baselineSlots = () => [];
variantARetained._patchProfile = () => {};
variantARetained._updateChartSelection = () => {};
variantARetained._setDisabled = () => {};
variantARetained._setNodeText = (node, value) => {
  if (node === variantARetainedReadiness) node.textContent = String(value);
};
variantARetained._setAttribute = (node, name, value) => {
  if (node === variantARetainedReadiness && name === "data-state") {
    node.dataState = String(value);
  }
};
variantARetained._setText = (selector, value) => {
  variantARetainedText[selector] = String(value);
};
variantARetained._patchDayCard = (key, time, state, energy) => {
  variantARetainedDay[key] = { time, state, energy };
};
variantARetained._root = {
  querySelector(selector) {
    if (selector === "[data-readiness]") return variantARetainedReadiness;
    if (selector === "[data-chart]") return variantARetainedChart;
    return variantARetainedGeneric;
  },
  querySelectorAll() { return []; },
};
const variantARetainedSnapshot = () => ({
  top: {
    nowPrimary: variantARetainedText["[data-now-primary]"],
    nowSecondary: variantARetainedText["[data-now-secondary]"],
    tariffMeta: variantARetainedText['[data-policy-meta="tariff"]'],
  },
  nextPrimary: variantARetainedText["[data-next-primary]"],
  nextSecondary: variantARetainedText["[data-next-secondary]"],
  tariffTotal: variantARetainedText['[data-total="tariff"]'],
  batteryTotal: variantARetainedText['[data-total="battery"]'],
  rceTotal: variantARetainedText['[data-total="rce"]'],
  day: clone(variantARetainedDay),
});

variantARetained._patch();
const variantACurrentSnapshot = variantARetainedSnapshot();
const variantACurrentChartMarkup = variantARetainedChart.innerHTML;
const variantACurrentSvgReplacements = variantARetained._metrics.svgReplacementCount;
check(
  variantARetainedReadiness.textContent === "Gotowe do sterowania"
    && variantARetainedReadiness.dataState === "ready"
    && variantACurrentSnapshot.nextPrimary.includes("Sprzedaż dynamiczna")
    && variantACurrentSnapshot.nextSecondary !== "—"
    && variantACurrentSnapshot.tariffTotal !== "—"
    && variantACurrentSnapshot.batteryTotal !== "—"
    && variantACurrentSnapshot.rceTotal !== "—"
    && variantACurrentSnapshot.day.rce?.energy !== "—"
    && variantACurrentSnapshot.day.tariff?.energy !== "—"
    && variantACurrentSnapshot.day.rcm?.energy !== "—"
    && variantACurrentSnapshot.day.rce?.state.includes("ready")
    && variantACurrentSnapshot.day.tariff?.state.includes("ready")
    && variantACurrentSnapshot.day.rcm?.state.includes("ready"),
  "Variant A current fixture exposes a concrete next action, totals and every source-plan status in the day cards",
);
const variantACurrentTariffSlot = variantARetained._canonicalSlots().find(
  (slot) => slot.policy === "tariff",
);
for (const action of [
  "tariff_battery_charge",
  "tariff_grid_support",
  "tariff_grid_support_and_charge",
]) {
  const summary = variantARetained._policySummary("tariff", [{
    ...variantACurrentTariffSlot,
    action,
    gridKwh: 0.625,
    equation: {
      ...variantACurrentTariffSlot.equation,
      grid_to_battery_kwh: 0.225,
    },
  }]);
  check(
    summary.valid === true
      && Math.abs(summary.tariffImport - 0.625) < 1e-9
      && Math.abs(summary.storedEnergy - 0.225) < 1e-9
      && Math.abs(summary.directLoad - 0.4) < 1e-9,
    `Variant A derives exact display-only tariff import, battery and direct-load totals from canonical ${action}`,
  );
}
for (const [label, overrides] of [
  ["unsupported action", { action: "tariff_unknown" }],
  ["negative import", { gridKwh: -0.1 }],
  ["non-finite import", { gridKwh: Number.NaN }],
  ["negative battery flow", { gridToBattery: -0.1 }],
  ["non-finite battery flow", { gridToBattery: Number.POSITIVE_INFINITY }],
  ["battery flow above import", { gridKwh: 0.1, gridToBattery: 0.2 }],
]) {
  const slot = {
    ...variantACurrentTariffSlot,
    ...overrides,
    equation: {
      ...variantACurrentTariffSlot.equation,
      ...(Object.prototype.hasOwnProperty.call(overrides, "gridToBattery")
        ? { grid_to_battery_kwh: overrides.gridToBattery }
        : {}),
    },
  };
  check(
    variantARetained._policySummary("tariff", [slot]).valid === false,
    `Variant A tariff canonical display summary fails closed for ${label}`,
  );
}

const variantARetainedState = variantARetainedHass.states[EXPECTED_BINDINGS.canonical_timeline_entity];
variantARetainedState.state = "pending";
Object.assign(variantARetainedState.attributes, {
  canonical_status: "pending",
  canonical_blocker_code: "source_recalculation_pending",
  result_current: false,
  recalculation_pending: true,
});
const variantARetainedTariff = variantARetainedHass.states[EXPECTED_BINDINGS.tariff_timeline_entity];
variantARetainedTariff.state = "pending";
Object.assign(variantARetainedTariff.attributes, {
  result_current: false,
  recalculation_pending: true,
  quality: "pending",
  blocker_code: "recalculation_pending",
  pending_input_revision: 13,
});
const variantARetainedSlots = variantARetained._canonicalSlots("retained");
variantARetained._patch();
const variantAPendingSnapshot = variantARetainedSnapshot();
const variantARetainedEvidence = {
  currentClosed: variantARetained._canonicalSlots() === null,
  retainedLength: variantARetainedSlots?.length === variantARetainedState.attributes.slots.length,
  hatch: variantARetained._chartSvg(variantARetainedSlots, true).includes('id="variantARetainedHatch"'),
  noAuthority: variantARetained._chartSvg(variantARetainedSlots, true).includes("brak prawa do nowego wykonania"),
  summaryClosed: variantARetained._policySummary("rce", null).valid === false,
};
check(
  Object.values(variantARetainedEvidence).every(Boolean),
  `Variant A displays an explicitly retained pending geometry with hatch while current decisions remain fail closed: ${JSON.stringify(variantARetainedEvidence)}`,
);
check(
  variantARetainedReadiness.textContent === "Plan gotowy · aktualizacja"
    && variantARetainedReadiness.dataState === "updating"
    && variantARetained._policyTimeline("tariff") === null
    && variantARetained._policyTimeline("tariff", true)?.length > 0
    && variantARetainedChart.innerHTML === variantACurrentChartMarkup
    && variantARetained._metrics.svgReplacementCount === variantACurrentSvgReplacements
    && variantARetainedChart.dataset.source === "retained"
    && JSON.stringify(variantAPendingSnapshot) === JSON.stringify(variantACurrentSnapshot),
  "Variant A updates retained status without replacing SVG and keeps next action, totals and day cards without restoring execution authority",
);
const variantARetainedBuiltAt = variantARetainedState.attributes.built_at;
variantARetained._patch();
check(
  variantARetainedState.attributes.built_at === variantARetainedBuiltAt
    && variantARetainedChart.innerHTML === variantACurrentChartMarkup
    && variantARetained._metrics.svgReplacementCount === variantACurrentSvgReplacements,
  "repeated pending neither extends retained-plan age nor replaces unchanged chart geometry",
);
variantARetainedHass.states[EXPECTED_BINDINGS.tariff_timeline_entity] = timeline(
  "tariff", 14, 9, new Date(variantARetainedStartMs + 3 * 60 * 60_000).toISOString(),
);
for (const blocker of ["rce_timeline_stale", "tariff_timeline_unavailable"]) {
  variantARetainedState.state = "partial";
  Object.assign(variantARetainedState.attributes, {
    canonical_status: "partial", canonical_blocker_code: blocker,
  });
  variantARetained._patch();
  check(
    variantARetained._canonicalSlots() === null
      && variantARetained._canonicalSlots("retained")?.length === variantARetainedSlots.length
      && variantARetainedChart.innerHTML === variantACurrentChartMarkup
      && variantARetained._metrics.svgReplacementCount === variantACurrentSvgReplacements
      && variantARetainedChart.dataset.source === "retained"
      && variantARetainedReadiness.dataState === "unavailable"
      && variantARetainedReadiness.textContent === "Ostatni plan · dane nieaktualne"
      && variantARetainedState.attributes.built_at === variantARetainedBuiltAt,
    `Variant A retains complete display geometry without authority or age extension during ${blocker}`,
  );
}
variantARetainedState.state = "pending";
Object.assign(variantARetainedState.attributes, {
  canonical_status: "pending", canonical_blocker_code: "source_recalculation_pending",
});
variantARetained._patch();
const variantAMismatchedTariffSnapshot = variantARetainedSnapshot();
check(
  variantARetainedReadiness.textContent === "Plan gotowy · aktualizacja"
    && variantARetainedReadiness.dataState === "updating"
    && variantARetained._policyTimeline("tariff")?.length > 0
    && JSON.stringify(variantAMismatchedTariffSnapshot) === JSON.stringify(variantACurrentSnapshot)
    && !JSON.stringify(variantAMismatchedTariffSnapshot).includes("Brak planu")
    && variantARetainedHass.callServiceCount === 0
    && variantARetainedHass.callWSCount === 0,
  "Variant A keeps top state, next action, totals and day cards from retained canonical slots while a newer tariff timeline is mismatched",
);
variantARetainedHass.states["binary_sensor.hoymiles_ems_execution_ready"].state = "off";
variantARetained._patch();
check(
  variantARetainedReadiness.textContent === "EMS zablokowany"
    && variantARetainedReadiness.dataState === "unavailable",
  "Variant A does not hide lost physical execution readiness behind retained-plan updating",
);
variantARetainedHass.states["binary_sensor.hoymiles_ems_execution_ready"].state = "on";
variantARetained._patch();
check(
  variantARetainedReadiness.textContent === "Plan gotowy · aktualizacja"
    && variantARetainedReadiness.dataState === "updating",
  "Variant A restores the retained-plan updating state after physical readiness returns",
);

const variantANextStart = new Date(variantARetainedStartMs + 15 * 60_000).toISOString();
variantARetainedHass.states[EXPECTED_BINDINGS.canonical_timeline_entity] = canonicalTimeline(
  variantANextStart, 12, true,
);
variantARetainedHass.states[EXPECTED_BINDINGS.tariff_timeline_entity] = timeline(
  "tariff", 15, 10, new Date(Date.parse(variantANextStart) + 60 * 60_000).toISOString(),
);
variantARetainedHass.states[EXPECTED_BINDINGS.rcm_timeline_entity] = timeline(
  "rcm", 16, 11, new Date(Date.parse(variantANextStart) + 2 * 60 * 60_000).toISOString(),
);
variantARetainedHass.states[
  EXPECTED_BINDINGS.rcm_timeline_entity
].attributes.points[0].action_code = "absorb_pv";
variantARetained._patch();
const variantANextCurrentSnapshot = variantARetainedSnapshot();
check(
  variantARetainedReadiness.textContent === "Gotowe do sterowania"
    && variantARetainedReadiness.dataState === "ready"
    && variantANextCurrentSnapshot.nextPrimary !== variantACurrentSnapshot.nextPrimary
    && variantANextCurrentSnapshot.tariffTotal === variantACurrentSnapshot.tariffTotal
    && variantANextCurrentSnapshot.batteryTotal === variantACurrentSnapshot.batteryTotal
    && !JSON.stringify(variantANextCurrentSnapshot).includes("Brak planu")
    && variantARetainedHass.callServiceCount === 0
    && variantARetainedHass.callWSCount === 0,
  "Variant A transitions from retained canonical display to a new matching canonical plan without a readiness or summary blank",
);
check(
  variantARetained._metrics.svgReplacementCount === variantACurrentSvgReplacements + 1,
  "a new accepted canonical plan replaces chart geometry exactly once",
);
const variantAExpired = new VariantAEms();
variantAExpired._config = VariantAEms.getStubConfig();
variantAExpired._hass = clone(variantARetainedHass);
const expiredCanonical = variantAExpired._hass.states[EXPECTED_BINDINGS.canonical_timeline_entity];
expiredCanonical.state = "pending";
Object.assign(expiredCanonical.attributes, {
  canonical_status: "pending",
  canonical_blocker_code: "source_recalculation_pending",
  result_current: false,
  recalculation_pending: true,
  built_at: new Date(Date.now() - 15 * 60_000 - 1).toISOString(),
});
check(
  variantAExpired._canonicalSlots("retained") === null,
  "Variant A expires a retained plan after the bounded display-only window",
);
expiredCanonical.state = "partial";
Object.assign(expiredCanonical.attributes, {
  canonical_status: "partial", canonical_blocker_code: "rce_timeline_stale",
});
check(variantAExpired._canonicalSlots("retained") === null,
  "partial source staleness cannot extend the retained display window");
expiredCanonical.attributes.built_at = new Date().toISOString();
expiredCanonical.attributes.canonical_blocker_code = "invalid_trace";
check(variantAExpired._canonicalSlots("retained") === null,
  "a structural blocker cannot use the source-freshness display fallback");

flushAnimationFrames();
const variantADependency = new VariantAEms();
variantADependency._config = VariantAEms.getStubConfig();
variantADependency._mount = () => {};
variantADependency._patch = () => { variantADependency._metrics.planAggregationCount += 1; };
const dependencyHass = hassFixture();
variantADependency.hass = dependencyHass;
const variantAInitialDependencyFrames = flushAnimationFrames();
check(
  variantAInitialDependencyFrames === 1,
  `Variant A initial dependency snapshot uses one frame: ${variantAInitialDependencyFrames}`,
);
const dependencyAggregations = variantADependency._metrics.planAggregationCount;
const dependencySvgReplacements = variantADependency._metrics.svgReplacementCount;
for (let index = 0; index < 100; index += 1) {
  variantADependency.hass = {
    ...dependencyHass,
    states: {
      ...dependencyHass.states,
      "sensor.unrelated_task_2": { state: String(index), attributes: {} },
    },
  };
}
check(
  rafQueue.size === 0
    && variantADependency._metrics.planAggregationCount === dependencyAggregations
    && variantADependency._metrics.svgReplacementCount === dependencySvgReplacements,
  "100 unrelated HA updates cause zero future-plan aggregations and zero SVG replacements",
);
const dependencyContentUpdate = clone(dependencyHass);
dependencyContentUpdate.states[EXPECTED_BINDINGS.canonical_timeline_entity].attributes.ledger_revision = "e".repeat(64);
variantADependency.hass = dependencyContentUpdate;
check(
  rafQueue.size === 1
    && flushAnimationFrames() === 1
    && variantADependency._metrics.planAggregationCount === dependencyAggregations + 1,
  "one relevant plan-content update coalesces into one aggregation frame",
);
const variantAFallbackHass = hassFixture();
variantAFallbackHass.states[EXPECTED_BINDINGS.canonical_timeline_entity].state = "pending";
variantAFallbackHass.states[EXPECTED_BINDINGS.canonical_timeline_entity].attributes.slots = [];
variantAFallbackHass.states[EXPECTED_BINDINGS.baseline_timeline_entity] =
  partialTailBaselineTimeline();
variantAFallbackHass.states["input_select.hoymiles_ems_supervisor_mode"] = {
  state: "Active",
  attributes: { options: ["Off", "Active"] },
};
variantAFallbackHass.states["binary_sensor.hoymiles_ems_execution_ready"] = {
  state: "on",
  attributes: {},
};
const variantABaseline = variantAFallbackHass.states[EXPECTED_BINDINGS.baseline_timeline_entity];
Object.assign(variantABaseline.attributes.points[1], {
  battery_kw: null,
  grid_import_kw: null,
  grid_export_kw: null,
  reserve_soc_percent: null,
  quality: "partial",
  blocker_code: "dependent_series_partial",
});
variantABaseline.attributes.points[0].start = new Date(
  Date.parse(variantABaseline.attributes.points[0].start) + 7.5 * 60_000,
).toISOString();
const variantAFallback = new VariantAEms();
variantAFallback._config = VariantAEms.getStubConfig();
variantAFallback._hass = variantAFallbackHass;
const variantAFallbackReadiness = { textContent: "", dataState: "" };
const variantAFallbackChart = { dataset: {}, innerHTML: "" };
const variantAFallbackGeneric = { hidden: true, querySelector() { return {}; } };
const variantAFallbackText = {};
const variantAFallbackDay = {};
variantAFallback._mounted = true;
variantAFallback._patchProfile = () => {};
variantAFallback._updateChartSelection = () => {};
variantAFallback._setDisabled = () => {};
variantAFallback._setNodeText = (node, value) => {
  if (node === variantAFallbackReadiness) node.textContent = String(value);
};
variantAFallback._setAttribute = (node, name, value) => {
  if (node === variantAFallbackReadiness && name === "data-state") {
    node.dataState = String(value);
  }
};
variantAFallback._setText = (selector, value) => {
  variantAFallbackText[selector] = String(value);
};
variantAFallback._patchDayCard = (key, time, state, energy) => {
  variantAFallbackDay[key] = { time, state, energy };
};
variantAFallback._root = {
  querySelector(selector) {
    if (selector === "[data-readiness]") return variantAFallbackReadiness;
    if (selector === "[data-chart]") return variantAFallbackChart;
    return variantAFallbackGeneric;
  },
  querySelectorAll() { return []; },
};
const variantABaselineSlots = variantAFallback._baselineSlots();
const variantABaselineModel = variantAFallback._hourlyModel(variantABaselineSlots);
variantAFallback._patch();
check(
  variantAFallback._canonicalSlots() === null
    && variantAFallback._policySummary("tariff", []).valid === false
    && variantABaselineSlots.length === 88
    && variantABaselineSlots.at(-1)?.end === Date.parse(variantABaseline.attributes.points[87].end)
    && variantABaselineSlots[1]?.gridKwh === null
    && variantABaselineSlots[1]?.expectedGridExportKwh === null
    && variantABaselineSlots.every((slot) => slot.policy === "none" && slot.action === "none")
    && variantABaselineModel?.buckets?.length === 44
    && variantABaselineModel.start % 3_600_000 === 0
    && variantABaselineModel.end % 3_600_000 === 0
    && variantAFallback._actionBands(variantABaselineSlots, variantABaselineModel).length === 0
    && variantAFallback._formatTime(variantABaselineModel.buckets[0].start) !== "—"
    && variantAFallback._chartSvg(variantABaselineSlots).includes('class="energy-chart"')
    && !variantAFallback._chartSvg(variantABaselineSlots).includes(variantAFallback._copy().chartUnavailable)
    && variantAFallbackText['[data-total="tariff"]'] === "—"
    && variantAFallbackText['[data-total="battery"]'] === "—"
    && variantAFallbackDay.tariff?.energy === "—"
    && variantAFallbackReadiness.textContent === "Plan w przygotowaniu"
    && variantAFallbackReadiness.dataState === "updating",
  "Variant A startup pending renders the complete prefix of a 96-point partial baseline without coercing nullable flows or inventing actions and totals",
);

const gappedVariantABaselineHass = hassFixture();
gappedVariantABaselineHass.states[EXPECTED_BINDINGS.baseline_timeline_entity] =
  clone(variantABaseline);
gappedVariantABaselineHass.states[
  EXPECTED_BINDINGS.baseline_timeline_entity
].attributes.points[20].load_kw = null;
const gappedVariantABaseline = new VariantAEms();
gappedVariantABaseline._config = VariantAEms.getStubConfig();
gappedVariantABaseline._hass = gappedVariantABaselineHass;
const gappedVariantABaselineSlots = gappedVariantABaseline._baselineSlots();
check(
  gappedVariantABaselineSlots.length === 20
    && gappedVariantABaselineSlots.at(-1)?.end
      === Date.parse(variantABaseline.attributes.points[19].end)
    && !gappedVariantABaselineSlots.some(
      (slot) => slot.start >= Date.parse(variantABaseline.attributes.points[21].start),
    )
    && gappedVariantABaseline._hourlyModel(gappedVariantABaselineSlots) !== null,
  "Variant A stops baseline chart geometry at the first nullable-series gap and never rejoins later complete points",
);

const malformedTailVariantABaselineHass = hassFixture();
malformedTailVariantABaselineHass.states[EXPECTED_BINDINGS.baseline_timeline_entity] =
  clone(variantABaseline);
malformedTailVariantABaselineHass.states[
  EXPECTED_BINDINGS.baseline_timeline_entity
].attributes.points.at(-1).pv_kw = "unknown";
const malformedTailVariantABaseline = new VariantAEms();
malformedTailVariantABaseline._config = VariantAEms.getStubConfig();
malformedTailVariantABaseline._hass = malformedTailVariantABaselineHass;
check(
  malformedTailVariantABaseline._baselineSlots().length === 0,
  "Variant A validates the full baseline payload and rejects a malformed non-null value after the renderable prefix",
);
variantAFallbackHass.states["binary_sensor.hoymiles_ems_execution_ready"].state = "off";
variantAFallback._patch();
check(
  variantAFallbackReadiness.textContent === "EMS zablokowany"
    && variantAFallbackReadiness.dataState === "unavailable",
  "Variant A keeps the not-ready warning when physical execution readiness is off",
);
variantAFallbackHass.states["binary_sensor.hoymiles_ems_execution_ready"].state = "on";
variantAFallbackHass.states[EXPECTED_BINDINGS.control_conflict_entity].state = "on";
variantAFallback._patch();
check(
  variantAFallbackReadiness.textContent === "Konflikt sterowania"
    && variantAFallbackReadiness.dataState === "error",
  "Variant A keeps control conflict above the startup plan state",
);
const rejectedVariantABaselineHass = hassFixture();
rejectedVariantABaselineHass.states[EXPECTED_BINDINGS.canonical_timeline_entity].state = "pending";
rejectedVariantABaselineHass.states[EXPECTED_BINDINGS.canonical_timeline_entity].attributes.slots = [];
rejectedVariantABaselineHass.states[EXPECTED_BINDINGS.baseline_timeline_entity].attributes.authority = true;
const rejectedVariantABaseline = new VariantAEms();
rejectedVariantABaseline._config = VariantAEms.getStubConfig();
rejectedVariantABaseline._hass = rejectedVariantABaselineHass;
check(
  rejectedVariantABaseline._baselineSlots().length === 0,
  "Variant A rejects a baseline that claims execution authority",
);

const variantAInspectorHass = hassFixture();
variantAInspectorHass.states[EXPECTED_BINDINGS.canonical_timeline_entity] =
  canonicalTimeline("2026-08-30T14:00:00.000Z", 12, true);
variantAInspectorHass.states[
  EXPECTED_BINDINGS.canonical_timeline_entity
].attributes.slots.forEach((slot, index) => {
  Object.assign(slot.soc_equation, {
    expected_soc_start_percent: slot.soc_equation.soc_start_percent + 1,
    expected_soc_end_percent: slot.soc_equation.soc_end_percent + 1,
    authorization_soc_start_percent: slot.soc_equation.soc_start_percent,
    authorization_soc_end_percent: slot.soc_equation.soc_end_percent,
    expected_source: [
      "provider_p50",
      "provider_p50_partial_bounds",
      "rce_calibrated_fallback",
      "tariff_conservative_fallback",
      "authorization_fallback",
      "unsupported_legacy_value",
    ][index] ?? null,
  });
  Object.assign(slot.planned, {
    potential_pv_kwh: 0.24,
    expected_load_kwh: 0.13,
    usable_pv_kwh: 0.2,
    pv_curtailed_kwh: 0.04,
    expected_grid_export_kwh: index === 0 ? 0.3 : 0,
    authorization_grid_export_kwh: index === 0 ? 0.25 : 0,
  });
});
const variantAInspector = new VariantAEms();
variantAInspector._config = VariantAEms.getStubConfig();
variantAInspector._hass = variantAInspectorHass;
check(
  variantAInspector._formatEnergy(-Number.EPSILON, 2) === "0,00 kWh",
  "Variant A normalizes floating-point negative zero in energy values",
);
const variantAInspectorSlots = variantAInspector._canonicalSlots();
const variantAInspectorModel = variantAInspector._hourlyModel(variantAInspectorSlots);
const variantAVisibleSlots = variantAInspectorSlots.filter(
  (slot) => slot.end > variantAInspectorModel.start && slot.start < variantAInspectorModel.end,
);
const variantAInspectorSvg = variantAInspector._chartSvg(variantAVisibleSlots);
const variantASocPointList = variantAInspectorSvg.match(
  /<polyline class="soc-line" points="([^"]+)"/,
)?.[1]?.trim()?.split(/\s+/) || [];
const variantASocActionPolicies = [
  ...variantAInspectorSvg.matchAll(/class="soc-action-segment"[^>]*data-soc-policy="([^"]+)"/g),
].map((match) => match[1]);
const variantAAuthorizationPath = variantAInspectorSvg.match(
  /<path class="soc-authorization-line" d="([^"]+)"/,
)?.[1] || "";
check(
  variantAVisibleSlots.length > variantAInspectorModel.buckets.length
    && variantAVisibleSlots.every(
      (slot) => Number.isFinite(slot.socStart) && Number.isFinite(slot.soc),
    )
    && variantASocPointList.length === variantAVisibleSlots.length + 1
    && (variantAInspectorSvg.match(/data-chart-slot="\d+"/g) || []).length
      === variantAVisibleSlots.length,
  "Variant A SOC line keeps every exact canonical slot boundary and one selectable target per interval",
);
check(
  variantAVisibleSlots.every(
    (slot) => slot.hasAuthorizationSoc
      && slot.soc === slot.equation.soc_end_percent + 1
      && slot.authorizationSoc === slot.equation.soc_end_percent
      && slot.potentialPvKwh === 0.24
      && Math.abs(slot.pvKw * slot.durationHours - 0.24) < 1e-9
      && Math.abs(slot.loadKw * slot.durationHours - 0.13) < 1e-9
      && slot.usablePvKwh === 0.2
      && slot.pvCurtailedKwh === 0.04,
  )
    && (variantAAuthorizationPath.match(/\bM\s/g) || []).length === variantAVisibleSlots.length
    && variantAInspectorSvg.includes("SOC · Przewidywany")
    && variantAInspectorSvg.includes("SOC · Bezpieczny plan automatyki")
    && variantAInspectorSvg.includes("Źródło prognozy SOC: P50 dostawcy · prognoza centralna")
    && variantAInspectorSvg.includes("Źródło prognozy SOC: P50 dostawcy · niepełne limity magazynu")
    && variantAInspectorSvg.includes("Źródło prognozy SOC: RCE · prognoza skorygowana")
    && variantAInspectorSvg.includes("Źródło prognozy SOC: Taryfa · prognoza ostrożna")
    && variantAInspectorSvg.includes("PV potencjalne")
    && variantAInspectorSvg.includes("PV dostępne")
    && variantAInspectorSvg.includes("PV ograniczone")
    && variantAInspectorSvg.includes("Eksport oczekiwany")
    && variantAInspectorSvg.includes("Eksport bezpieczny")
    && cardSource.includes('class="soc-expected-key" style="color:#58d6ff"')
    && cardSource.includes("data-safe-soc-legend data-visible=\"false\"")
    && cardSource.includes("this._chartSlots.some((slot) => slot.hasAuthorizationSoc)"),
  "Variant A renders predicted SOC in cyan, a separate safe automation trajectory, honest per-slot forecast provenance, a conditional legend and the optional PV/export contract in slot tooltips",
);
check(
  variantAVisibleSlots.slice(0, 6).map((slot) => slot.expectedSource).join("|")
    === "provider_p50|provider_p50_partial_bounds|rce_calibrated_fallback|tariff_conservative_fallback|authorization_fallback|unknown"
    && variantAInspector._expectedSocSourceLabel("rce_authorization_fallback")
      === "RCE · prognoza skorygowana"
    && variantAInspector._expectedSocSourceLabel("tariff_authorization_fallback")
      === "Taryfa · prognoza ostrożna",
  "Variant A normalizes current and compatibility expected_source values without presenting an unknown value as P50",
);
check(
  !variantAInspectorSvg.includes("data-plans-track")
    && !variantAInspectorSvg.includes('class="plan-band"')
    && (variantAInspectorSvg.match(/class="grid-flow-bar"/g) || []).length > 0
    && (variantAInspectorSvg.match(/class="soc-action-glass"/g) || []).length > 0,
  "Variant A replaces the duplicated plan rails with grid-flow bars and action-aware SOC glass",
);
check(
  variantASocActionPolicies.includes("rce")
    && variantASocActionPolicies.includes("tariff")
    && variantASocActionPolicies.includes("rcm")
    && !variantAInspectorSvg.includes('data-soc-policy="none"'),
  "Variant A colors only SOC segments changed by canonical RCE, tariff or RCEm actions",
);

const tariffGlowSlots = (action, delta = 0, selected = true) =>
  variantAVisibleSlots.map((slot, index) => ({
    ...slot,
    policy: index === 0 && selected ? "tariff" : "none",
    action: index === 0 && selected ? action : "none",
    socStart: 50,
    soc: index === 0 ? 50 + delta : 50,
    gridKwh: index === 0 ? 0.35 : 0,
    gridImportKwh: index === 0 && selected ? 0.35 : 0,
  }));
const flatGridSupportSvg = variantAInspector._chartSvg(
  tariffGlowSlots("tariff_grid_support"),
);
const retainedFlatGridSupportSvg = variantAInspector._chartSvg(
  tariffGlowSlots("tariff_grid_support"),
  true,
);
const flatGridSupportAndChargeSvg = variantAInspector._chartSvg(
  tariffGlowSlots("tariff_grid_support_and_charge"),
);
const risingGridSupportSvg = variantAInspector._chartSvg(
  tariffGlowSlots("tariff_grid_support", 0.3),
);
const unselectedFlatTariffSvg = variantAInspector._chartSvg(
  tariffGlowSlots("tariff_grid_support", 0, false),
);
const actionEffects = (svg, action) =>
  (svg.match(new RegExp(`class="soc-action-glass"[^>]*data-soc-action="${action}"`, "g")) || []).length === 1
  && (svg.match(new RegExp(`class="soc-action-segment"[^>]*data-soc-action="${action}"`, "g")) || []).length === 1;
check(
  actionEffects(flatGridSupportSvg, "tariff_grid_support")
    && actionEffects(retainedFlatGridSupportSvg, "tariff_grid_support")
    && actionEffects(flatGridSupportAndChargeSvg, "tariff_grid_support_and_charge")
    && actionEffects(risingGridSupportSvg, "tariff_grid_support")
    && flatGridSupportSvg.includes("Zasilanie domu z sieci")
    && retainedFlatGridSupportSvg.includes("Zasilanie domu z sieci")
    && retainedFlatGridSupportSvg.includes('class="retained-overlay"')
    && !unselectedFlatTariffSvg.includes('class="soc-action-glass"')
    && !unselectedFlatTariffSvg.includes('class="soc-action-segment"'),
  "Variant A highlights selected flat-SOC grid support in current and retained plans without coloring none or baseline slots",
);

const variantANoTariffSlots = variantAInspectorSlots.slice(0, 4).map(
  (slot, index) => index === 1
    ? { ...slot, gridKwh: 0.36, gridImportKwh: 0, soc: slot.socStart - 0.3 }
    : slot,
);
const variantANoTariffSvg = variantAInspector._chartSvg(variantANoTariffSlots);
check(
  variantAInspectorSlots
    .filter((slot) => slot.policy !== "tariff")
    .every((slot) => slot.gridImportKwh === 0)
    && variantAInspectorSlots
      .filter((slot) => slot.policy === "tariff")
      .every((slot) => slot.gridImportKwh === Math.max(slot.gridKwh, 0))
    && !variantANoTariffSvg.includes('data-grid-flow="import"'),
  "Variant A shows blue grid-import bars only for selected tariff actions, never for a falling self-use SOC forecast",
);

const variantABalancingHass = hassFixture();
const variantABalancingStart = new Date(
  Math.floor((Date.now() - 30 * 60_000) / (15 * 60_000)) * (15 * 60_000),
).toISOString();
variantABalancingHass.states[EXPECTED_BINDINGS.canonical_timeline_entity] =
  canonicalTimeline(variantABalancingStart, 8, false);
variantABalancingHass.states["input_boolean.hoymiles_battery_balancing_active"] = {
  state: "on",
  attributes: {},
};
const variantABalancing = new VariantAEms();
variantABalancing._config = VariantAEms.getStubConfig();
variantABalancing._hass = variantABalancingHass;
const variantABalancingSlots = variantABalancing._canonicalSlots();
const variantABalancingCurrent = variantABalancingSlots.find(
  (slot) => slot.start <= Date.now() && Date.now() < slot.end,
);
const variantABalancingSvg = variantABalancing._chartSvg(variantABalancingSlots);
check(
  Boolean(variantABalancingCurrent)
    && variantABalancingSvg.includes('id="variantABalanceGlass"')
    && variantABalancingSvg.includes('data-soc-policy="balance"')
    && variantABalancingSvg.includes('data-soc-action="battery_balancing"')
    && variantABalancingSvg.includes('class="soc-action-glass"')
    && variantABalancing._chartInspectorHtml(variantABalancingCurrent)
      .includes("Balansowanie magazynu")
    && cardSource.includes('data-plan-policy="balance" style="color:#37d991"'),
  "Variant A marks the physically active balancing interval on SOC in green with glass, legend and inspector copy",
);

const variantALegacyHass = hassFixture();
variantALegacyHass.states[EXPECTED_BINDINGS.canonical_timeline_entity] =
  canonicalTimeline("2026-08-30T14:00:00.000Z", 12, true);
const variantALegacy = new VariantAEms();
variantALegacy._config = VariantAEms.getStubConfig();
variantALegacy._hass = variantALegacyHass;
const variantALegacySlots = variantALegacy._canonicalSlots();
const variantALegacySvg = variantALegacy._chartSvg(variantALegacySlots);
check(
  variantALegacySlots.every((slot, index) => {
    const source = variantALegacyHass.states[
      EXPECTED_BINDINGS.canonical_timeline_entity
    ].attributes.slots[index];
    return slot.socStart === source.soc_equation.soc_start_percent
      && slot.soc === source.soc_equation.soc_end_percent
      && slot.potentialPvKwh === source.planned.pv_kwh
      && slot.usablePvKwh === source.planned.pv_kwh
      && slot.hasPvBreakdown === false
      && slot.hasAuthorizationSoc === false
      && slot.expectedSource === "unknown";
  })
    && !variantALegacySvg.includes('class="soc-authorization-line"')
    && variantALegacySvg.includes('<polyline class="soc-line"')
    && variantALegacySvg.includes("Źródło prognozy SOC: Źródło nieokreślone"),
  "Variant A preserves the legacy canonical soc/pv_kwh fallback without inventing a safe trajectory or P50 provenance",
);

const variantAInspectorRoot = new FakeElement("div");
const variantAInspectorPanel = new FakeElement("aside");
variantAInspectorPanel.dataset.chartInspector = "";
variantAInspectorRoot.append(variantAInspectorPanel);
const variantAInspectorTargets = variantAVisibleSlots.map((_slot, index) => {
  const target = new FakeElement("button");
  target.dataset.chartSlot = String(index);
  target.closest = (selector) =>
    selector === "[data-chart-slot]" || selector === "[data-action]"
      ? target
      : null;
  target.dataset.action = "rce";
  variantAInspectorRoot.append(target);
  return target;
});
variantAInspector._root = variantAInspectorRoot;
variantAInspector._chartSlots = variantAVisibleSlots;
variantAInspector._chartSource = "current";
variantAInspectorHass.states[
  variantAInspector._config.supervisor_allow_rce_entity
] = { state: "on", attributes: {} };

variantAInspector._handleClick({ target: variantAInspectorTargets[0] });
check(
  variantAInspector._selectedChartStart === variantAVisibleSlots[0].start
    && variantAInspectorHass.callServiceCount === 0
    && variantAInspectorPanel.dataset.visible === "true"
    && variantAInspectorPanel.getAttribute("aria-hidden") === "false"
    && String(variantAInspectorPanel.innerHTML).includes("Aktualny plan EMS")
    && String(variantAInspectorPanel.innerHTML).includes("Sprzedaż dynamiczna")
    && String(variantAInspectorPanel.innerHTML).includes("SOC · Przewidywany")
    && String(variantAInspectorPanel.innerHTML).includes("SOC · Bezpieczny plan automatyki")
    && String(variantAInspectorPanel.innerHTML).includes("Źródło prognozy SOC")
    && String(variantAInspectorPanel.innerHTML).includes("P50 dostawcy · prognoza centralna")
    && String(variantAInspectorPanel.innerHTML).includes("PV potencjalne")
    && String(variantAInspectorPanel.innerHTML).includes("PV dostępne")
    && String(variantAInspectorPanel.innerHTML).includes("PV ograniczone")
    && String(variantAInspectorPanel.innerHTML).includes("Eksport oczekiwany")
    && String(variantAInspectorPanel.innerHTML).includes("Eksport bezpieczny")
    && String(variantAInspectorPanel.innerHTML).includes("Przepływy przewidywane")
    && String(variantAInspectorPanel.innerHTML).includes("Przepływy bezpiecznego planu")
    && String(variantAInspectorPanel.innerHTML).includes("Zużycie"),
  "Variant A click/tap inspection distinguishes expected and safe SOC, PV availability and export without a service call",
);

const providerInspectorSignature = variantAInspector._chartInspectorSignature;
variantAVisibleSlots[0].expectedSource = "tariff_conservative_fallback";
variantAInspector._updateChartSelection();
check(
  variantAInspector._selectedChartStart === variantAVisibleSlots[0].start
    && variantAInspector._chartInspectorSignature !== providerInspectorSignature
    && String(variantAInspectorPanel.innerHTML).includes("Taryfa · prognoza ostrożna")
    && !String(variantAInspectorPanel.innerHTML).includes("P50 dostawcy · prognoza centralna")
    && variantAInspectorHass.callServiceCount === 0,
  "Variant A refreshes the persistent inspector when only expected_source changes without losing selection or writing to Home Assistant",
);
variantAVisibleSlots[0].expectedSource = "provider_p50";
variantAInspector._updateChartSelection();

const variantAEnglish = new VariantAEms();
variantAEnglish._config = { ...VariantAEms.getStubConfig(), language: "en" };
variantAEnglish._hass = variantAInspectorHass;
check(
  variantAEnglish._copy().expectedSoc === "SOC · Predicted"
    && variantAEnglish._copy().authorizationSoc === "SOC · Safe automation plan"
    && variantAEnglish._copy().chartExpectedFlows === "Predicted flows"
    && variantAEnglish._copy().chartAuthorizationFlows === "Safe-plan flows"
    && variantAEnglish._expectedSocSourceLabel("provider_p50")
      === "Provider P50 · central forecast"
    && variantAEnglish._expectedSocSourceLabel("provider_p50_partial_bounds")
      === "Provider P50 · partial battery limits"
    && variantAEnglish._expectedSocSourceLabel("authorization_fallback")
      === "Conservative automation forecast"
    && variantAEnglish._expectedSocSourceLabel(undefined) === "Source not specified",
  "Variant A exposes equally honest predicted/safe SOC and provenance copy in English",
);

const variantAKey = (key, target) => {
  let prevented = false;
  variantAInspector._handleChartKeyDown({
    key,
    target,
    preventDefault() { prevented = true; },
  });
  return prevented;
};
check(
  variantAKey("Enter", variantAInspectorTargets[0])
    && variantAKey(" ", variantAInspectorTargets[0])
    && variantAKey("ArrowRight", variantAInspectorTargets[0])
    && variantAInspector._selectedChartStart === variantAVisibleSlots[1].start
    && fakeDocument.activeElement === variantAInspectorTargets[1]
    && variantAKey("ArrowLeft", variantAInspectorTargets[1])
    && variantAInspector._selectedChartStart === variantAVisibleSlots[0].start
    && fakeDocument.activeElement === variantAInspectorTargets[0]
    && variantAInspectorHass.callServiceCount === 0,
  "Variant A SOC slots support Enter, Space and arrow navigation without mutating Home Assistant",
);

const retainedSelectedStart = variantAInspector._selectedChartStart;
const retainedSlots = variantAVisibleSlots.map((slot, index) => ({
  ...slot,
  soc: index === 0 ? slot.soc - 1 : slot.soc,
}));
variantAInspector._chartSlots = retainedSlots;
variantAInspector._chartSource = "retained";
variantAInspector._updateChartSelection();
const retainedInspectorHtml = String(variantAInspectorPanel.innerHTML);
const baselineSelected = {
  ...retainedSlots[0],
  action: "none",
  expectedSource: "unknown",
  equation: {},
};
variantAInspector._chartSlots = [baselineSelected, ...retainedSlots.slice(1)];
variantAInspector._chartSource = "baseline";
variantAInspector._updateChartSelection();
const baselineInspectorHtml = String(variantAInspectorPanel.innerHTML);
check(
  variantAInspector._selectedChartStart === retainedSelectedStart
    && retainedInspectorHtml.includes("Ostatni plan · trwa przeliczanie")
    && retainedInspectorHtml.includes(
      `${variantAInspector._number(retainedSlots[0].socStart, 1)}% → ${variantAInspector._number(retainedSlots[0].soc, 1)}%`,
    )
    && baselineInspectorHtml.includes("Prognoza bazowa · bez dodatkowych działań EMS")
    && baselineInspectorHtml.includes("Autokonsumpcja")
    && variantAInspectorHass.callServiceCount === 0,
  "Variant A preserves the selected interval while refreshed retained and baseline sources update the inspector truthfully",
);

const selectedBeforeInvalidInspection = variantAInspector._selectedChartStart;
variantAInspector._selectChartSlot(999);
check(
  variantAInspector._selectedChartStart === selectedBeforeInvalidInspection
    && variantAInspectorHass.callServiceCount === 0,
  "Variant A ignores an invalid inspection target without changing selection or calling a service",
);
let wrongBindingRejected = false;
try {
  const wrong = new Card();
  wrong.setConfig({ ...EXPECTED_BINDINGS, rce_plan_entity: "sensor.wrong" });
} catch (_error) {
  wrongBindingRejected = true;
}
check(wrongBindingRejected, "wrong exact binding is rejected");

for (const [key, entityId] of Object.entries(EXPECTED_BINDINGS)) {
  const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const escapedEntityId = entityId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  check(
    new RegExp(`${escapedKey}:\\s*"${escapedEntityId}"`).test(cardSource),
    `registered planner exact binding ${key}`,
  );
}
const canonicalViews = [...dashboardSource.matchAll(/^  - title: (.+)$/gm)];
check(canonicalViews.length === 21, "canonical dashboard has exactly 21 stable views");
check(
  canonicalViews[0]?.[1] === "Przegląd"
    && canonicalViews[1]?.[1] === "EMS"
    && canonicalViews[2]?.[1] === "Automatyka EMS",
  "canonical Overview, EMS plan and EMS automation order",
);
const canonicalPlannerView = dashboardSource.slice(
  canonicalViews[1].index,
  canonicalViews[2].index,
);
const canonicalViewRecords = canonicalViews.map((match, index) => {
  const source = dashboardSource.slice(
    match.index,
    canonicalViews[index + 1]?.index ?? dashboardSource.length,
  );
  return {
    title: match[1],
    path: source.match(/^    path: (.+)$/m)?.[1] || "",
    subview: /^    subview: true$/m.test(source),
  };
});
equal(
  canonicalViewRecords.map((view) => view.path),
  [
    "start", "plan-automatyki", "ems-supervisor", "ustawienia-ems",
    "ustawienia-balansowania", "automatyka-ems", "ladowanie-taryfowe", "rcem-253v",
    "produkcja-pv", "pv", "bateria", "load-eps", "zyski", "siec", "przeplywy",
    "falownik", "generator", "liczniki", "sterowanie", "stany-alarmy",
    "diagnostyka",
  ],
  "all twenty-one established dashboard route paths remain stable and ordered",
);
equal(
  canonicalViewRecords.filter((view) => !view.subview).map((view) => view.path),
  ["start", "plan-automatyki", "ustawienia-ems", "pv", "bateria", "load-eps", "zyski"],
  "Variant A exposes seven main routes with Earnings after Energy",
);
equal(
  canonicalViewRecords.filter((view) => view.subview).map((view) => view.path),
  [
    "ems-supervisor", "ustawienia-balansowania", "automatyka-ems", "ladowanie-taryfowe", "rcem-253v",
    "produkcja-pv", "siec", "przeplywy", "falownik",
    "generator", "liczniki", "sterowanie", "stany-alarmy", "diagnostyka",
  ],
  "Variant A keeps fourteen other routes as deep-linkable subviews",
);
check(
  (dashboardLf.match(/type: custom:hoymiles-aurora-variant-a-settings-page-card/g) || []).length === 1
    && (dashboardLf.match(/type: custom:hoymiles-ems-shared-inputs-card\n\s+mode: full/g) || []).length === 0
    && (dashboardLf.match(/type: custom:hoymiles-ems-shared-inputs-card\n\s+mode: summary/g) || []).length === 0,
  "one Variant A row-based settings card remains and repeated policy summaries are removed",
);
check(
  canonicalPlannerView.includes("    path: plan-automatyki")
    && canonicalPlannerView.includes("    type: panel")
    && (canonicalPlannerView.match(/custom:hoymiles-aurora-variant-a-ems-page-card/g) || []).length === 1
    && !canonicalPlannerView.includes("vertical-stack")
    && !canonicalPlannerView.includes("custom:hoymiles-automation-planner-card"),
  "Variant A planner view is one fluid controls-summary-chart-day-plan composition",
);
check(
  assetsSource.includes("FRONTEND_ASSET_REVISION = 123")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 38")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 39")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 40")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 41")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 42")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 43")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 44")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 45")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 46")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 47")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 48")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 49")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 60")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 61")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 62")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 63")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 64")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 65")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 66")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 67")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 68")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 69")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 70")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 71")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 72")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 73")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 74")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 75")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 76")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 77")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 78")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 79")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 80")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 81")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 82")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 87")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 88"),
  "frontend asset revision is exactly 123",
);
check(
  strategySource.includes("const frontendRevision = 123")
    && strategySource.includes("history=48h-executed")
    && strategySource.includes("canonicalModuleUrl.search = canonicalQuery")
    && !strategySource.includes("1.5.8.38")
    && !strategySource.includes("1.5.8.39")
    && !strategySource.includes("1.5.8.40")
    && !strategySource.includes("1.5.8.41")
    && !strategySource.includes("1.5.8.42")
    && !strategySource.includes("1.5.8.43")
    && !strategySource.includes("1.5.8.44")
    && !strategySource.includes("1.5.8.45")
    && !strategySource.includes("1.5.8.46")
    && !strategySource.includes("1.5.8.47")
    && !strategySource.includes("1.5.8.48")
    && !strategySource.includes("1.5.8.49")
    && !strategySource.includes("1.5.8.60")
    && !strategySource.includes("1.5.8.61")
    && !strategySource.includes("1.5.8.62")
    && !strategySource.includes("1.5.8.63")
    && !strategySource.includes("1.5.8.64")
    && !strategySource.includes("1.5.8.65")
    && !strategySource.includes("1.5.8.66")
    && !strategySource.includes("1.5.8.67")
    && !strategySource.includes("1.5.8.68")
    && !strategySource.includes("1.5.8.69")
    && !strategySource.includes("1.5.8.70")
    && !strategySource.includes("1.5.8.71")
    && !strategySource.includes("1.5.8.72")
    && !strategySource.includes("1.5.8.73")
    && !strategySource.includes("1.5.8.74")
    && !strategySource.includes("1.5.8.75")
    && !strategySource.includes("1.5.8.76")
    && !strategySource.includes("1.5.8.77")
    && !strategySource.includes("1.5.8.78")
    && !strategySource.includes("1.5.8.79")
    && !strategySource.includes("1.5.8.82")
    && !strategySource.includes("1.5.8.87")
    && !strategySource.includes("1.5.8.88"),
  "bootstrap cache reference is exactly revision 123",
);
check(
  ALLOW_GENERATED_DRIFT
    || (read("custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js") === cardSource
      && read("custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js") === strategySource),
  "generated frontend JS mirrors match canonical sources",
);
if (!ALLOW_GENERATED_DRIFT) {
  for (const [language, expectedTitle] of [["pl", "EMS"], ["en", "EMS"]]) {
    const generated = JSON.parse(read(`custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_${language}.json`));
    const plannerView = generated.views?.[1];
    check(generated.views?.length === 21, `${language} generated dashboard has 21 views`);
    check(
      plannerView?.title === expectedTitle
        && plannerView?.path === "plan-automatyki"
        && plannerView?.icon === undefined
        && plannerView?.type === "panel"
        && plannerView?.cards?.length === 1
        && plannerView.cards[0]?.type === "custom:hoymiles-aurora-variant-a-ems-page-card"
        && generated.views[2]?.path === "ems-supervisor",
      `${language} exact Plan and Supervisor order`,
    );
  }
}

const { card, hass } = mount();
const loadSourceFixture = hassFixture("pl");
Object.assign(loadSourceFixture.states[EXPECTED_BINDINGS.rce_plan_entity].attributes, {
  selected_average_daily_load_kwh: 17,
  load_model_source: "configured_daily_fallback",
  load_model_quality: "fallback",
  history_complete: false,
  recorder_load_accepted_energy_days: 1,
  recorder_load_average_4d_kwh: 15.4,
  recorder_load_recent_4d_kwh: { "2026-09-24": 15.4 },
});
Object.assign(loadSourceFixture.states[EXPECTED_BINDINGS.tariff_plan_entity].attributes, {
  model_input_average_daily_load_kwh: 17,
  load_profile_source: "configured_daily_fallback",
});
const loadSourceView = card._dailyLoadModelInputs(loadSourceFixture.states, "pl");
check(
  loadSourceView.history?.value.includes("15,4")
    && loadSourceView.history?.note.includes("1 zakwalifikowana doba"),
  "one accepted history day must be shown as a one-day diagnostic, not a four-day model mean",
);
check(
  loadSourceView.rce.value.includes("17")
    && loadSourceView.rce.note.includes("Wartość zastępcza")
    && loadSourceView.tariff.value.includes("17")
    && loadSourceView.tariff.note.includes("Wartość zastępcza"),
  "RCE and tariff must show their actual separate fallback model inputs",
);
for (const [count, grammar] of [
  [0, "0 zakwalifikowanych dób"],
  [1, "1 zakwalifikowana doba"],
  [3, "3 zakwalifikowane doby"],
  [4, "4 zakwalifikowane doby"],
]) {
  const dates = Object.fromEntries(
    Array.from({ length: count }, (_, index) => [`2026-09-${String(20 + index).padStart(2, "0")}`, 15.4]),
  );
  const attributes = loadSourceFixture.states[EXPECTED_BINDINGS.rce_plan_entity].attributes;
  attributes.recorder_load_recent_4d_kwh = dates;
  attributes.recorder_load_average_4d_kwh = count ? 15.4 : null;
  const view = card._dailyLoadModelInputs(loadSourceFixture.states, "pl");
  check(view.history.note.includes(grammar), `history grammar for ${count} qualified days`);
  check(
    count ? view.history.value.includes("15,4") : !view.history.value.includes("0 kWh"),
    `history value must not turn ${count} qualified days into invented energy`,
  );
}
const uncertainRce = loadSourceFixture.states[EXPECTED_BINDINGS.rce_plan_entity].attributes;
uncertainRce.recorder_load_recent_4d_kwh = {
  "2026-09-20": 15.4, "2026-09-21": 15.4, "2026-09-22": 15.4,
};
uncertainRce.recorder_load_average_4d_kwh = 15.4;
uncertainRce.load_model_source = "provisional_history_or_fallback";
uncertainRce.load_model_quality = "fallback";
check(
  card._dailyLoadModelInputs(loadSourceFixture.states, "pl").rce.note.includes("historia niepełna"),
  "three accepted days alone do not prove the RCE history is active",
);
uncertainRce.load_model_source = "history_28d_gap_weighted";
uncertainRce.load_model_quality = "complete";
uncertainRce.history_complete = true;
const separateSources = card._dailyLoadModelInputs(loadSourceFixture.states, "pl");
check(
  separateSources.rce.note.includes("Średnia z historii")
    && separateSources.tariff.note.includes("Wartość zastępcza"),
  "RCE and tariff must retain independent published source labels",
);
const englishSources = card._dailyLoadModelInputs(loadSourceFixture.states, "en");
check(
  englishSources.history.note.includes("3 qualified days")
    && englishSources.tariff.note.includes("Configured fallback"),
  "English diagnostic and tariff source labels must remain independent",
);
uncertainRce.load_model_source = "stale_history_or_fallback";
uncertainRce.history_complete = false;
check(
  card._dailyLoadModelInputs(loadSourceFixture.states, "pl").rce.note.includes("historia niepełna"),
  "stale history is not labeled as active RCE model input",
);
delete uncertainRce.load_model_source;
check(
  card._dailyLoadModelInputs(loadSourceFixture.states, "pl").rce.note === "Brak danych",
  "missing RCE source metadata is not guessed from the history day count",
);
const tariffAttributes = loadSourceFixture.states[EXPECTED_BINDINGS.tariff_plan_entity].attributes;
tariffAttributes.load_profile_source = "rce_recorder_broker";
tariffAttributes.load_profile_broker_fresh = false;
check(
  card._dailyLoadModelInputs(loadSourceFixture.states, "pl").tariff.note === "Brak danych",
  "stale tariff broker metadata is not presented as a fresh shared RCE input",
);
const refs = card._refs;
check(Boolean(refs), "stable reference registry exists");
check(
  Object.keys(refs.modelInputValues).join(",") === "history,rce,tariff,rcm"
    && refs.modelInputGrid.children.length === 4,
  "desktop and mobile share four explicit LOAD source tiles in the mounted DOM",
);
check(card.shadowRoot.children.length === 2, "one style and one ha-card");
check(refs.card.tagName === "HA-CARD", "ha-card shell");
check(refs.shell.tagName === "ARTICLE" && refs.shell.classList.contains("ap-shell"), "article shell");
check(refs.hero.tagName === "HEADER", "hero shell");
check(
  refs.hero.querySelector("#ap-title")?.textContent === "Plan automatyki EMS" &&
    refs.dayPlanEyebrow.textContent === "Plan dnia" &&
    refs.dayPlanTitle.textContent === "Co zrobi EMS" &&
    !refs.hero.textContent.includes("Co zrobi EMS"),
  "compact heading keeps the chart title while the restored day plan owns Co zrobi EMS",
);
check(
  refs.shell.children.indexOf(refs.hero) < refs.shell.children.indexOf(refs.batterySummary)
    && refs.shell.children.indexOf(refs.batterySummary) < refs.shell.children.indexOf(refs.timelineSection)
    && refs.shell.children.indexOf(refs.timelineSection) < refs.shell.children.indexOf(refs.policySection)
    && refs.shell.children.indexOf(refs.policySection) < refs.shell.children.indexOf(refs.safety)
    && refs.batterySummary.children.length === 3
    && refs.timelineSection.children.indexOf(refs.timelineHeading)
      < refs.timelineSection.children.indexOf(refs.viewport)
    && refs.timelineSection.children.indexOf(refs.viewport)
      < refs.timelineSection.children.indexOf(refs.selectedDetail)
    && refs.timelineControls.parentNode === refs.timelineHeading
    && refs.timelineControls.querySelector(".ap-c4-compare")
    && refs.timelineControls.querySelector(".ap-c4-refresh")
    && refs.modelInputs.parentNode === null
    && refs.planMeaning.parentNode === null,
  "compact DOM keeps header, three tiles, chart, day plan and safety in the intended reading order",
);
check(
  Object.keys(refs.policies).length === 3
    && refs.policySection.parentNode === refs.shell
    && Object.values(refs.policies).every((policy) => walk(refs.shell).includes(policy)),
  "three stable policy cards are restored as the visible canonical day plan",
);
check(
  refs.dayPlanTotals.tariffImport.label.textContent === "Pobór taryfowy"
    && refs.dayPlanTotals.tariffImport.value.dataset.rawKwh === "0.225000"
    && refs.dayPlanTotals.gridToBattery.label.textContent === "Do magazynu"
    && refs.dayPlanTotals.gridToBattery.value.dataset.rawKwh === "0.225000"
    && refs.dayPlanTotals.rceExport.label.textContent === "Sprzedaż dynamiczna"
    && refs.dayPlanTotals.rceExport.value.dataset.rawKwh === "0.275000"
    && refs.dayPlanTotals.tariffImport.value.dataset.sourceField
      === "canonical.slots.planned.grid_kwh_import_positive"
    && refs.dayPlanTotals.gridToBattery.value.dataset.sourceField
      === "canonical.slots.soc_equation.grid_to_battery_kwh"
    && refs.dayPlanTotals.rceExport.value.dataset.sourceField
      === "canonical.slots.planned.grid_kwh_import_positive",
  "day-plan totals use only exact selected canonical import, storage and export energy",
);
check(
  refs.policies.rce.dataset.sourcePolicy === "canonical"
    && refs.policies.tariff.dataset.sourcePolicy === "canonical"
    && refs.policyFacts.rce.effect.value.textContent.includes("0,28")
    && refs.policyFacts.tariff.effect.value.textContent.includes("0,23")
    && !refs.policyFacts.rce.effect.value.textContent.includes("1,2")
    && !refs.policyFacts.tariff.effect.value.textContent.includes("1,5"),
  "visible day cards cannot substitute independent candidate-plan energy for the canonical ledger",
);
check(Object.keys(refs.lanes).length === 3, "three stable lanes");
check(Object.keys(refs.details).length === 3, "three stable expert details");
check(
  refs.socChart.getAttribute("class") === "ap-soc-chart"
    && Object.values(refs.socSeries).every(
      (series) => series.getAttribute("class") === "ap-soc-series",
    )
    && refs.socBaseline.getAttribute("class") === "ap-soc-series ap-soc-baseline"
    && refs.socFloor.getAttribute("class") === "ap-soc-series ap-soc-floor",
  "SVG classes use the browser-safe attribute contract",
);
check(
  refs.socChart.tagName === "SVG"
    && refs.socExpected.tagName === "PATH"
    && refs.socPlanTarget.tagName === "PATH"
    && Object.values(refs.socSeries).every((series) => series.tagName === "PATH")
    && Object.values(refs.socTargets).every((series) => series.tagName === "PATH"),
  "stable SVG uses dedicated expected, target and expert-comparison paths",
);
check(
  refs.socExpected.getAttribute("class") === "ap-soc-expected"
    && refs.socExpected.dataset.sourceField
      === "canonical.slots.soc_equation.soc_start_percent+soc_end_percent",
  "optional EMS trajectory is sourced only from the canonical post-arbitration ledger",
);
check(
  refs.socBaseline.dataset.sourceField
    === "baseline.points[].soc_start_percent+soc_end_percent",
  "always-on green baseline is sourced only from the shared Self-Use energy timeline",
);
const mainPlanPoints = card._lastModel.baselineTimeline.points;
check(
  refs.planPowerBars.tagName === "G"
    && refs.socChart.children.indexOf(refs.planPowerBars)
      < refs.socChart.children.indexOf(refs.socExpected)
    && card._powerBarNodes.size === mainPlanPoints.length,
  "PV and LOAD plan bars are layered below the final green SOC line one-for-one",
);
check(
  mainPlanPoints.every((point) => {
    const record = card._powerBarNodes.get(point.key);
    return record?.pv.tagName === "RECT"
      && record?.load.tagName === "RECT"
      && record.pv.dataset.sourcePolicy === "baseline"
      && record.load.dataset.sourcePolicy === "baseline"
      && record.pv.dataset.sourceField
        === "baseline.points[].pv_kw"
      && record.load.dataset.sourceField
        === "baseline.points[].load_kw"
      && record.pv.dataset.rawValue === String(point.pvKw)
      && record.load.dataset.rawValue === String(point.loadKw)
      && Number(record.pv.getAttribute("height")) > 0
      && Number(record.load.getAttribute("height")) > 0;
  }),
  "plan bars retain baseline PV/LOAD values for every always-on green-series point",
);
check(
  Object.keys(refs.powerLegendItems).join("|") === "pv|load"
    && refs.powerLegendItems.pv.label.textContent === "Prognoza PV"
    && refs.powerLegendItems.load.label.textContent === "Prognoza zużycia domu",
  "PV and LOAD bars have explicit homeowner-facing meanings",
);
check(
    refs.rawPoints.open === false
    && refs.rawPoints.parentNode === refs.shell
    && refs.shell.children.at(-1) === refs.rawPoints
    && card._rawPointRows.size === mainPlanPoints.length,
  `raw final-plan point table remains complete but is collapsed and last in the simple hierarchy: ${JSON.stringify({
    open: refs.rawPoints.open,
    parent: refs.rawPoints.parentNode?.className,
    last: refs.shell.children.at(-1)?.className,
    rows: card._rawPointRows.size,
    expectedRows: mainPlanPoints.length,
  })}`,
);
check(
  mainPlanPoints.every((point) => {
    const record = card._rawPointRows.get(point.key);
    return record?.row.dataset.sourcePolicy === "baseline"
      && record.row.dataset.selectedPolicy === "none"
      && record.row.dataset.selectedAction === "none"
      && record.cells.start.textContent === point.start
      && record.cells.end.textContent === point.end
      && record.cells.policy.textContent === "Prognoza bazowa — autokonsumpcja."
      && record.cells.pv.textContent === String(point.pvKw)
      && record.cells.load.textContent === String(point.loadKw)
      && record.cells.battery.textContent === String(point.batteryKw)
      && record.cells.gridImport.textContent === String(point.gridImportKw)
      && record.cells.gridExport.textContent === String(point.gridExportKw)
      && record.cells.soc.textContent === String(point.socPercent);
  }),
  "raw table exports exact baseline time, power balance and SOC without inventing authority",
);
check(
  refs.socChart.children.indexOf(refs.socPlanTarget)
    > refs.socChart.children.indexOf(refs.socExpected),
  "blue plan target is layered above the wider green trajectory so coincident values remain distinguishable",
);
check(
  refs.currentSocPoint.dataset.sourceField === "baseline.current_actual_soc_percent",
  "current SOC marker freezes the always-on baseline physical-SOC source field",
);
check(
  byClass(refs.socYAxis, "ap-soc-y-label").map((node) => node.textContent).join("|")
    === "100%|75%|50%|25%|0%",
  "SOC axis visibly spans 0 through 100 percent",
);
check(!refs.nowMarker.hidden && refs.nowMarker.textContent.includes("Teraz"), "current-time marker is visible");
const visibleHourTicks = byClass(refs.axis, "ap-axis-tick").map((node) => node.textContent);
check(
  visibleHourTicks.length >= 2
    && visibleHourTicks.every((label) => /^(?:[01]\d|2[0-3]):00$/.test(label))
    && visibleHourTicks.every((label) => Number(label.slice(0, 2)) % 2 === 0),
  "visible time axis uses aligned two-hour HH:mm labels",
);
check(
  byClass(refs.axis, "ap-axis-day").some((node) => node.textContent === "Dziś"),
  "time axis names the current local day",
);
check(
  !refs.currentSoc.hidden
    && !refs.currentSocPoint.hidden
    && refs.currentSocValue.textContent.includes("75"),
  "exact backend current SOC is visible as label and point",
);
check(Boolean(refs.socFloor.getAttribute("d")), "protected SOC floor path rendered");
check(Boolean(refs.socExpected.getAttribute("d")), "expected battery trajectory is rendered");
check(
  !refs.socBaseline.hidden && Boolean(refs.socBaseline.getAttribute("d")),
  "always-on Self-Use baseline remains visible in simple mode",
);
check(
  Object.values(refs.socSeries).every((series) => Boolean(series.getAttribute("d"))),
  "every exact alternative policy trajectory remains available for local expert comparison",
);
check(
  Boolean(refs.socTargets.rce.getAttribute("d"))
    && Boolean(refs.socTargets.tariff.getAttribute("d"))
    && Boolean(refs.socTargets.rcm.getAttribute("d")),
  "each policy target overlay renders from the exact policy-local backend field",
);
check(
  refs.socTargets.rce.dataset.sourceField
    === "policy.points[].protected_soc_floor_percent"
    && refs.socTargets.tariff.dataset.sourceField
      === "policy.points[].target_soc_percent"
    && refs.socTargets.rcm.dataset.sourceField
      === "policy.points[].target_soc_percent",
  "RCE floor and tariff/RCEm targets remain distinct policy-local overlays",
);
check(
  Object.keys(refs.lineLegendItems).join("|") === "expected|target|reserve",
  "simple mode exposes no more than the three human-first battery line meanings",
);
check(
  refs.lineLegendItems.expected.label.textContent === "Plan EMS"
    && refs.lineLegendItems.target.label.textContent === "Zamiar planu"
    && refs.lineLegendItems.reserve.label.textContent === "Rezerwa awaryjna",
  "plain-language three-line legend is visible",
);
check(
  !refs.lineLegendItems.expected.item.hidden
    && !refs.lineLegendItems.target.item.hidden
    && !refs.lineLegendItems.reserve.item.hidden,
  "canonical simple mode shows expected level, action intent and reserve",
);
const visibleTextOutsideExpert = (node) => {
  if (node === refs.selectedExpert) return "";
  return (node._textContent || "")
    + node.children.map((child) => visibleTextOutsideExpert(child)).join("");
};
const simpleRenderedCopy = visibleTextOutsideExpert(refs.shell);
check(
  !simpleRenderedCopy.includes("SOC bazowe")
    && !simpleRenderedCopy.includes("baseline_soc_percent")
    && !simpleRenderedCopy.includes("protected_soc_floor_percent")
    && !simpleRenderedCopy.includes("target_soc_percent")
    && !refs.selectedExpert.open
    && refs.selectedExpert.textContent.includes("protected_soc_floor_percent")
    && refs.selectedExpert.textContent.includes("target_soc_percent"),
  "simple rendered copy hides raw SOC fields while the collapsed expert inspector preserves provenance",
);
check(
  refs.timelineSection.textContent.includes("Poziom magazynu — dziś i jutro")
    && refs.timelineSection.textContent.includes("100% oznacza pełny magazyn"),
  "Polish homeowner-first chart title and SOC explanation are visible",
);
check(
  refs.summaryValues.battery.label.textContent === "Prognoza PV"
    && refs.summaryValues.battery.value.textContent.includes("kWh")
    && refs.summaryValues.lowest.label.textContent === "Zużycie domu"
    && refs.summaryValues.lowest.value.textContent.includes("kWh")
    && refs.summaryValues.reserve.label.textContent === "Rezerwa awaryjna"
    && refs.summaryValues.reserve.value.textContent.includes("25")
    && !walk(refs.shell).includes(refs.summaryValues.next.row),
  "compact summary exposes exactly PV energy, home-load energy and emergency reserve",
);
const pendingCanonicalHass = hassFixture();
pendingCanonicalHass.states[EXPECTED_BINDINGS.canonical_timeline_entity].state =
  "pending";
const { card: pendingCanonicalCard } = mount(pendingCanonicalHass);
check(
  pendingCanonicalCard._lastModel.canonicalTimeline.valid === true
    && pendingCanonicalCard._lastModel.canonicalTimeline.points.length > 0
    && Boolean(pendingCanonicalCard._refs.socExpected.getAttribute("d"))
    && Boolean(pendingCanonicalCard._refs.socBaseline.getAttribute("d"))
    && Boolean(pendingCanonicalCard._refs.socFloor.getAttribute("d"))
    && Boolean(pendingCanonicalCard._refs.socPlanTarget.getAttribute("d"))
    && pendingCanonicalCard._powerBarNodes.size
      === pendingCanonicalCard._lastModel.baselineTimeline.points.length
    && pendingCanonicalCard._rawPointRows.size
      === pendingCanonicalCard._lastModel.baselineTimeline.points.length
    && pendingCanonicalCard._refs.badge.dataset.status !== "data_unavailable",
  "pending canonical retains its last complete EMS overlay while baseline, reserve and power balance stay visible",
);
check(
  Object.values(pendingCanonicalCard._refs.socSeries).every(
    (series) => Boolean(series.getAttribute("d")),
  ),
  "independent policy trajectories remain available only for local comparison",
);
check(
  pendingCanonicalCard._refs.dayPlanSection.dataset.state === "retained"
    && pendingCanonicalCard._refs.dayPlanSection.dataset.stale === "true"
    && pendingCanonicalCard._refs.dayPlanState.textContent
      === "Ostatni plan · trwa przeliczanie"
    && pendingCanonicalCard._refs.dayPlanTotals.rceExport.value.dataset.rawKwh
      === "0.275000",
  "a pending canonical calculation keeps exact retained totals but labels them as stale",
);
const policyLocalHass = hassFixture();
policyLocalHass.states[EXPECTED_BINDINGS.canonical_timeline_entity].state =
  "unavailable";
policyLocalHass.states[EXPECTED_BINDINGS.rcm_timeline_entity].state =
  "unavailable";
const policyLocalSupervisor =
  policyLocalHass.states[EXPECTED_BINDINGS.supervisor_entity];
policyLocalSupervisor.attributes.selected_policy = "tariff";
policyLocalSupervisor.attributes.selected_candidate_revision = 6;
policyLocalSupervisor.attributes.selection_reason = "required_energy_restore";
policyLocalSupervisor.attributes.owner = "none";
policyLocalHass.states[
  EXPECTED_BINDINGS.tariff_timeline_entity
].attributes.current_actual.soc_percent = 24;
policyLocalHass.states[
  EXPECTED_BINDINGS.baseline_timeline_entity
].attributes.current_actual_soc_percent = 24;
policyLocalSupervisor.attributes.rejected_reasons = [
  { policy_id: "rce", reason: "confirmed_zero_export" },
  { policy_id: "rcm", reason: "unavailable" },
];
for (const summary of policyLocalSupervisor.attributes.candidate_summaries) {
  if (summary.policy_id === "rce") {
    summary.start_eligible = false;
    summary.rejection_reason = "confirmed_zero_export";
  } else if (summary.policy_id === "rcm") {
    summary.available = false;
    summary.result_current = false;
    summary.start_eligible = false;
    summary.blocked_reason = "current_soc_outside_operating_bounds";
    summary.rejection_reason = "unavailable";
  }
}
const { card: policyLocalCard } = mount(policyLocalHass);
check(
  policyLocalCard._lastModel.relevantPolicyId === "tariff",
  "policy-local hero follows the Supervisor-selected tariff",
);
check(
  policyLocalCard._lastModel.currentActual.socPercent === 24
    && policyLocalCard._lastModel.currentActualSourceField
      === "tariff.current_actual.soc_percent"
    && policyLocalCard._lastModel.timelines.tariff.points[0]
      .protectedSocFloorPercent === 25,
  "policy-local fixture is exactly SOC 24 with protected floor 25",
);
check(
  policyLocalCard._lastModel.supervisor.candidateContextValidity.tariff
    === true
    && policyLocalCard._lastModel.supervisor.candidateContextValidity.rcm
      === false
    && policyLocalCard._lastModel.supervisor.candidateContextValid === false,
  "only the selected tariff context grants policy authority",
);
check(
  !["data_unavailable", "blocked"].includes(
    policyLocalCard._refs.badge.dataset.status,
  ),
  "unavailable peer policies do not globally block tariff authority",
);
check(
  policyLocalCard._lastModel.hero.action
    === "Ładowanie magazynu z sieci"
    && policyLocalCard._refs.heroFacts.action.value.textContent
      === "Ładowanie magazynu z sieci",
  "canonical unavailability does not hide the selected tariff action",
);
check(
  policyLocalCard._lastModel.summary.battery === "24\u00a0%"
    && policyLocalCard._refs.currentSocValue.textContent === "24\u00a0%"
    && policyLocalCard._refs.currentSocPoint.dataset.sourceField
      === "baseline.current_actual_soc_percent",
  "current SOC remains anchored to the shared physical baseline while tariff is selected",
);
check(
  policyLocalCard._lastModel.policies.tariff.status === "planned"
    && policyLocalCard._lastModel.policies.rce.status === "blocked"
    && policyLocalCard._lastModel.policies.rcm.status === "data_unavailable",
  "tariff remains executable while RCE and RCEm stay individually rejected",
);

const activeIdleHass = hassFixture();
activeIdleHass.states[EXPECTED_BINDINGS.canonical_timeline_entity].state =
  "unavailable";
activeIdleHass.states[EXPECTED_BINDINGS.rcm_timeline_entity].state =
  "unavailable";
const activeIdleSupervisor =
  activeIdleHass.states[EXPECTED_BINDINGS.supervisor_entity];
activeIdleSupervisor.state = "active_idle";
activeIdleSupervisor.attributes.selected_policy = null;
activeIdleSupervisor.attributes.selected_candidate_revision = null;
activeIdleSupervisor.attributes.owner = "none";
const { card: activeIdleCard } = mount(activeIdleHass);
check(
  activeIdleCard._lastModel.hero.supervisor
    === "EMS jest włączony. Stan wykonania: Brak działania. Właściciel: Brak."
    && activeIdleCard._refs.badge.dataset.status !== "data_unavailable",
  "Active idle with owner none is shown as enabled and is not helper desynchronization",
);
const malformedCanonicalHass = hassFixture();
malformedCanonicalHass.states[
  EXPECTED_BINDINGS.canonical_timeline_entity
].attributes.slots[0].planned.pv_kwh += 1;
const { card: malformedCanonicalCard } = mount(malformedCanonicalHass);
check(
  malformedCanonicalCard._lastModel.canonicalTimeline.valid === false
    && malformedCanonicalCard._lastModel.canonicalTimeline.error
      === "canonical_energy_balance_mismatch"
    && malformedCanonicalCard._refs.socExpected.getAttribute("d") === ""
    && Boolean(malformedCanonicalCard._refs.socBaseline.getAttribute("d"))
    && malformedCanonicalCard._powerBarNodes.size
      === malformedCanonicalCard._lastModel.baselineTimeline.points.length
    && malformedCanonicalCard._rawPointRows.size
      === malformedCanonicalCard._lastModel.baselineTimeline.points.length,
  "frontend rejects an energy-incoherent canonical overlay without erasing the independent baseline",
);
check(
  malformedCanonicalCard._refs.dayPlanSection.dataset.state === "unavailable"
    && malformedCanonicalCard._refs.dayPlanState.textContent === "Brak aktualnego planu"
    && malformedCanonicalCard._refs.dayPlanTotals.rceExport.value.textContent
      === "Brak danych"
    && malformedCanonicalCard._refs.dayPlanTotals.rceExport.value.dataset.rawKwh
      === undefined
    && malformedCanonicalCard._refs.dayPlanTotals.tariffImport.value.dataset.rawKwh
      === undefined
    && malformedCanonicalCard._refs.dayPlanTotals.gridToBattery.value.dataset.rawKwh
      === undefined,
  "invalid canonical data fails closed and never turns missing day-plan energy into zero",
);
const { card: englishCard } = mount(hassFixture("en"));
check(
  englishCard._refs.timelineSection.textContent.includes("Battery level — today and tomorrow")
    && englishCard._refs.timelineSection.textContent.includes("100% means a full battery"),
  "English homeowner-first title and SOC explanation are visible",
);
check(
  englishCard._refs.lineLegendItems.expected.label.textContent === "EMS plan"
    && englishCard._refs.lineLegendItems.target.label.textContent === "Plan intent"
    && englishCard._refs.lineLegendItems.reserve.label.textContent === "Emergency reserve",
  "English simple legend uses natural nontechnical labels",
);
check(
  englishCard._refs.powerLegendItems.pv.label.textContent === "PV forecast"
    && englishCard._refs.powerLegendItems.load.label.textContent === "Home-load forecast"
    && englishCard._refs.rawPoints.textContent.includes("Raw baseline forecast points"),
  "English PV/LOAD legend and raw-table title remain explicit",
);
check(
  englishCard._refs.summaryValues.next.label.textContent === "Next action",
  "English battery summary uses the plain-language next-action label",
);
const blockerHass = hassFixture();
blockerHass.states[
  EXPECTED_BINDINGS.rce_timeline_entity
].attributes.blocker_code = "insufficient_cheap_window";
const { card: blockerCard } = mount(blockerHass);
blockerCard._slotNodes.get(
  blockerCard._lastModel.baselineTimeline.points[0].key,
).dispatch("click");
check(
  blockerCard._refs.selectedFields.blocker.value.textContent
    === "Plan jest niepełny, dlatego karta nie przedstawia go jako pewnego działania."
    && !blockerCard._refs.selectedFields.blocker.value.textContent.includes("insufficient_cheap_window"),
  `simple selected detail translates the blocker code into homeowner language (actual: ${blockerCard._refs.selectedFields.blocker.value.textContent})`,
);
const englishBlockerHass = hassFixture("en");
englishBlockerHass.states[
  EXPECTED_BINDINGS.rce_timeline_entity
].attributes.blocker_code = "insufficient_cheap_window";
const { card: englishBlockerCard } = mount(englishBlockerHass);
englishBlockerCard._slotNodes.get(
  englishBlockerCard._lastModel.baselineTimeline.points[0].key,
).dispatch("click");
check(
  englishBlockerCard._refs.selectedFields.blocker.value.textContent
    === "The plan is incomplete, so the card does not present it as a confirmed action."
    && !englishBlockerCard._refs.selectedFields.blocker.value.textContent.includes("insufficient_cheap_window"),
  `English simple detail translates the blocker code without exposing the backend code (actual: ${englishBlockerCard._refs.selectedFields.blocker.value.textContent})`,
);
const tariffHass = hassFixture();
tariffHass.states[EXPECTED_BINDINGS.supervisor_entity].attributes.selected_policy = "tariff";
tariffHass.states[EXPECTED_BINDINGS.supervisor_entity].attributes.selected_candidate_revision = 6;
const { card: tariffCard } = mount(tariffHass);
check(
  tariffCard._lastModel.relevantPolicyId === "tariff"
    && !tariffCard._refs.socPlanTarget.hidden
    && !tariffCard._refs.lineLegendItems.target.item.hidden,
  "canonical intent remains visible independently of the highlighted policy",
);
check(
  tariffCard._refs.socExpected.getAttribute("d")
    === refs.socExpected.getAttribute("d")
    && tariffCard._refs.socExpected.getAttribute("d")
      !== tariffCard._refs.socSeries.tariff.getAttribute("d")
    && tariffCard._refs.heroFacts.action.value.textContent
      === "Sprzedaż energii z magazynu",
  "green trajectory and main action remain canonical when Supervisor selects tariff",
);
check(
  Object.values(refs.legendItems).every(
    (item) => item.item.dataset.relevant === "false",
  )
    && Object.values(refs.socSeries).every(
      (series) => series.getAttribute("data-relevant") === "false",
    ),
  "independent policies remain comparison-only and never become the main source",
);
check(refs.safety.getAttribute("role") === "status", "safety role status");
check(
  refs.policyFacts.rce.active.label.textContent === "Trwa działanie",
  "compact policy card keeps the physical-status label distinct from its meaning",
);
check(refs.shell.getAttribute("lang") === "pl", "Polish language on article");
check(refs.badge.dataset.status === "planned", "complete enabled plan is Planned");
check(
  card._pointNodes.size === 0
    && card._slotNodes.size === mainPlanPoints.length
    && card._laneSlotNodes.size
      === mainPlanPoints.length * Object.keys(refs.lanes).length,
  "stable baseline slot targets replace redundant policy-point DOM",
);
check(
  [...card._slotNodes.keys()].every((key) => /^baseline\|.+Z\|.+Z$/.test(key))
    && [...card._laneSlotNodes.keys()].every(
      (key) => /^(?:rce|tariff|rcm)\|baseline\|.+Z\|.+Z$/.test(key),
    ),
  "baseline and policy-lane hit targets retain deterministic slot keys",
);
check(card._bandNodes.size === 2, "canonical selected actions form two chronological bands");
check(
  [...card._bandNodes.values()].every(
    (record) => record.pointKeys.length === 1 && record.element.classList.contains("ap-band"),
  ),
  "bands preserve exact canonical action identities",
);
check(
  [...card._bandNodes.values()].every(
    (record) => walk(record.element).filter((node) => node.tagName === "HA-ICON").length === 1,
  ),
  "each contiguous action band renders one semantic icon instead of one per slot",
);
const actionBand = (policyId, actionCode, pointIndex) => {
  const baselinePoint = mainPlanPoints[pointIndex];
  const actionPoint = {
    key: `action-bar|${policyId}|${baselinePoint.start}`,
    start: baselinePoint.start,
    end: baselinePoint.end,
    actionCode,
    policy: policyShape(policyId),
    quality: "complete",
  };
  return {
    key: `action-bar-band|${policyId}|${baselinePoint.start}`,
    policyId,
    actionCode,
    presentationCode: actionCode,
    start: actionPoint.start,
    end: actionPoint.end,
    points: [actionPoint],
    pointKeys: [actionPoint.key],
    quality: "complete",
  };
};
const rceActionBand = actionBand("rce", "export", 0);
const tariffActionBand = actionBand("tariff", "battery_charge", 4);
const rceActionRecord = card._newBandRecord(rceActionBand);
const tariffActionRecord = card._newBandRecord(tariffActionBand);
card._patchBand(
  rceActionRecord,
  rceActionBand,
  "pl",
  card._lastModel.axis,
  card._actionEnergyMaximum("rce", rceActionBand.points),
);
card._patchBand(
  tariffActionRecord,
  tariffActionBand,
  "pl",
  card._lastModel.axis,
  card._actionEnergyMaximum("tariff", tariffActionBand.points),
);
const actionBars = [
  ...rceActionRecord.energyBarNodes.values(),
  ...tariffActionRecord.energyBarNodes.values(),
];
const actionBarFor = (series) =>
  actionBars.find((bar) => bar.dataset.series === series);
const rceExportBar = actionBarFor("rce-export");
const tariffStorageBar = actionBarFor("tariff-storage");
const tariffHomeBar = actionBarFor("tariff-home");
check(
  rceExportBar?.dataset.sourceField === "planned_export_kwh"
    && rceExportBar?.dataset.rawKwh === "1.200000"
    && rceExportBar?.style.values.get("--ap-action-energy-height") !== "0.0000%",
  "RCE action bar uses only the exact planned-export field and a quantitative height",
);
check(
  tariffStorageBar?.dataset.sourceField === "stored_energy_kwh"
    && tariffStorageBar?.dataset.rawKwh === "1.000000"
    && tariffHomeBar?.dataset.sourceField === "direct_load_kwh"
    && tariffHomeBar?.dataset.rawKwh === "0.500000",
  "tariff action bars keep exact non-overlapping battery and home allocations",
);
check(
  !actionBars.some((bar) => bar.dataset.sourceField === "planned_import_kwh"),
  "tariff aggregate import remains an exact inspector total and is never double-drawn",
);
equal(
  card._policyActionEnergyComponents(
    "rcm",
    "grid_discharge_preparation",
    { planned_pre_discharge_kwh: 0.75, required_headroom_kwh: 2 },
  ),
  [{ field: "planned_pre_discharge_kwh", series: "rcm-pre-discharge", kwh: 0.75 }],
  "RCEm pre-discharge bar uses its exact planned energy",
);
equal(
  card._policyActionEnergyComponents(
    "rcm",
    "limit_export",
    { required_headroom_kwh: 2, planned_export_limit_percent: 30 },
  ),
  [],
  "RCEm export limits and required headroom never fabricate an energy bar",
);
check(
  [...card._slotNodes.values()].every(
    (record) => walk(record).every((node) => node.tagName !== "HA-ICON"),
  )
    && [...card._laneSlotNodes.values()].every(
      (record) => walk(record).every((node) => node.tagName !== "HA-ICON"),
    ),
  "keyed baseline and lane hit targets do not repeat visual icons",
);
check(
  Object.values(refs.compareButtons).every(
    (button) => button.getAttribute("aria-pressed") === "false",
  ) && !refs.shell.getAttribute("data-compare"),
  "alternative policy trajectories are hidden by default",
);
refs.compareButtons.tariff.dispatch("click");
check(
  refs.shell.getAttribute("data-compare") === "tariff"
    && refs.compareButtons.tariff.getAttribute("aria-pressed") === "true"
    && hass.callServiceCount === 0
    && hass.callWSCount === 0,
  `local expert comparison reveals one exact scenario without HA authority (compare=${refs.shell.getAttribute("data-compare")}, pressed=${refs.compareButtons.tariff.getAttribute("aria-pressed")}, service=${hass.callServiceCount}, ws=${hass.callWSCount})`,
);
refs.compareButtons.tariff.dispatch("click");
check(
  !refs.shell.getAttribute("data-compare")
    && refs.compareButtons.tariff.getAttribute("aria-pressed") === "false",
  "local comparison closes without rebuilding or changing backend state",
);
check(
  Object.keys(refs.selectedFields).join("|")
    === "range|pvPower|loadPower|batteryPower|soc|importPower|exportPower|actionEnergy|action|balance|blocker"
    && Object.values(refs.selectedFields).every(
      (field) => !field.label.textContent.includes("_") && field.label.textContent !== "",
    ),
  "selected interval exposes exact action energy among its labelled homeowner fields before collapsed expert provenance",
);

const gapPath = card._socStepPath(
  [
    { start: "2026-08-30T14:00:00.000Z", end: "2026-08-30T14:30:00.000Z", socPercent: 72 },
    { start: "2026-08-30T14:30:00.000Z", end: "2026-08-30T15:00:00.000Z", socPercent: null },
    { start: "2026-08-30T15:00:00.000Z", end: "2026-08-30T15:30:00.000Z", socPercent: 68 },
  ],
  "socPercent",
  "2026-08-30T14:00:00.000Z",
  "2026-08-30T15:30:00.000Z",
);
check((gapPath.match(/\bM\b/g) || []).length === 2, "missing SOC creates a true SVG path gap");
check(!gapPath.includes("polyline") && gapPath.includes(" L "), "SOC uses discrete step segments, not linear interpolation");
const longHass = hassFixture();
longHass.states[EXPECTED_BINDINGS.canonical_timeline_entity] = canonicalTimeline(
  "2026-08-30T14:00:00.000Z",
  128,
);
longHass.states[EXPECTED_BINDINGS.baseline_timeline_entity] = baselineTimeline(
  longHass.states[EXPECTED_BINDINGS.canonical_timeline_entity],
);
longHass.states[EXPECTED_BINDINGS.rce_timeline_entity] = timeline(
  "rce", 11, 5, "2026-08-30T14:00:00.000Z", 96,
);
longHass.states[EXPECTED_BINDINGS.tariff_timeline_entity] = timeline(
  "tariff", 12, 6, "2026-08-30T14:00:00.000Z", 96,
);
longHass.states[EXPECTED_BINDINGS.rcm_timeline_entity] = timeline(
  "rcm", 13, 7, "2026-08-30T14:00:00.000Z", 192,
);
const longModel = card._buildModel(longHass);
check(
  longModel.axis.end === "2026-08-31T22:00:00.000Z",
  "48-hour backend payload is visually bounded to the end of tomorrow in Europe/Warsaw",
);
check(
  longModel.axis.ticks.length > 8
    && longModel.axis.ticks.some((tick) => tick.label === "00:00" && tick.midnight)
    && longModel.axis.ticks.some((tick) => tick.dayLabel === "Jutro"),
  "long horizon exposes every two-hour tick plus a labelled midnight boundary",
);
const idleFallbackHass = hassFixture();
for (const key of ["rce_timeline_entity", "tariff_timeline_entity", "rcm_timeline_entity"]) {
  for (const item of idleFallbackHass.states[EXPECTED_BINDINGS[key]].attributes.points) {
    item.action_code = "idle";
    if (key === "tariff_timeline_entity") {
      Object.assign(item, {
        battery_kw: 0,
        grid_kw: 0,
        grid_import_kw: 0,
        grid_export_kw: 0,
        soc_percent:
          idleFallbackHass.states[EXPECTED_BINDINGS.tariff_timeline_entity]
            .attributes.current_actual.soc_percent,
        target_soc_percent: null,
        policy: {
          ...item.policy,
          planned_import_kwh: 0,
          planned_charge_kw: 0,
        },
      });
    }
  }
}
idleFallbackHass.states[EXPECTED_BINDINGS.supervisor_entity].state = "unavailable";
check(
  card._buildModel(idleFallbackHass).relevantPolicyId === "rce",
  "idle policy timelines remain comparison context while canonical stays main",
);
const { card: idleCard } = mount(idleFallbackHass);
check(
  [...idleCard._bandNodes.values()].length === 2
    && [...idleCard._bandNodes.values()].every(
      (record) => record.element.dataset.sourcePolicy === "canonical",
    ),
  "policy-local idle previews cannot replace the two canonical selected-action bands",
);
check(
  [...idleCard._bandNodes.values()].some(
    (record) => record.element.dataset.policy === "rce"
      && record.label.textContent.includes("Sprzedaż"),
  )
    && [...idleCard._bandNodes.values()].some(
      (record) => record.element.dataset.policy === "tariff"
        && record.label.textContent.includes("Ładowanie"),
    ),
  "canonical RCE and tariff action bands retain homeowner language",
);

const stable = {
  shadowRoot: card.shadowRoot,
  card: refs.card,
  shell: refs.shell,
  hero: refs.hero,
  batterySummary: refs.batterySummary,
  viewport: refs.viewport,
  rail: refs.rail,
  rawPoints: refs.rawPoints,
  selectedDetail: refs.selectedDetail,
  safety: refs.safety,
  policies: { ...refs.policies },
  lanes: { ...refs.lanes },
  details: { ...refs.details },
  slots: new Map(card._slotNodes),
  laneSlots: new Map(card._laneSlotNodes),
  bands: new Map(card._bandNodes),
  socChart: refs.socChart,
  planPowerBars: refs.planPowerBars,
  powerBars: new Map(card._powerBarNodes),
  rawRows: new Map(card._rawPointRows),
  socExpected: refs.socExpected,
  socPlanTarget: refs.socPlanTarget,
  currentSocPoint: refs.currentSocPoint,
  socSeries: { ...refs.socSeries },
  socTargets: { ...refs.socTargets },
};
const firstPoint = card._slotNodes.get(mainPlanPoints[0].key);
refs.details.rce.open = true;
firstPoint.dispatch("click");
firstPoint.focus();
check(
  refs.selectedFields.action.value.textContent.includes("Sprzedaż")
    && refs.selectedFields.balance.value.textContent.includes("Sprzedaż"),
  "selected baseline interval explains the canonical EMS action without claiming authority for the baseline",
);
check(
  refs.selectedFields.range.value.textContent.includes("–")
    && refs.selectedFields.soc.value.textContent.includes("→")
    && refs.selectedFields.pvPower.value.textContent.includes("kW")
    && refs.selectedFields.blocker.value.textContent === "Brak danych",
  "selected interval leads with time, exact baseline SOC/PV and honest blocker availability",
);
fakeWindow.scrollY = 950;
refs.viewport.scrollLeft = 240;
const selectedKey = card._selectedKey;

function assertStable(label) {
  check(card.shadowRoot === stable.shadowRoot, `${label}: shadow root stable`);
  check(refs.card === stable.card && refs.shell === stable.shell, `${label}: top shell stable`);
  check(refs.hero === stable.hero && refs.viewport === stable.viewport && refs.rail === stable.rail, `${label}: hero/timeline stable`);
  check(refs.rawPoints === stable.rawPoints && refs.planPowerBars === stable.planPowerBars, `${label}: raw table and power-bar layer stable`);
  check(refs.batterySummary === stable.batterySummary, `${label}: battery summary stable`);
  check(refs.selectedDetail === stable.selectedDetail && refs.safety === stable.safety, `${label}: detail/safety stable`);
  check(refs.socChart === stable.socChart, `${label}: SVG root stable`);
  check(
    refs.socExpected === stable.socExpected
      && refs.socPlanTarget === stable.socPlanTarget
      && refs.currentSocPoint === stable.currentSocPoint,
    `${label}: human-first expected/target/current nodes stable`,
  );
  for (const policyId of ["rce", "tariff", "rcm"]) {
    check(refs.policies[policyId] === stable.policies[policyId], `${label}: ${policyId} policy stable`);
    check(refs.lanes[policyId] === stable.lanes[policyId], `${label}: ${policyId} lane stable`);
    check(refs.details[policyId] === stable.details[policyId], `${label}: ${policyId} details stable`);
    check(refs.socSeries[policyId] === stable.socSeries[policyId], `${label}: ${policyId} SOC path stable`);
    check(refs.socTargets[policyId] === stable.socTargets[policyId], `${label}: ${policyId} target path stable`);
  }
  for (const [key, node] of stable.slots) {
    check(card._slotNodes.get(key) === node, `${label}: keyed baseline slot stable ${key}`);
  }
  for (const [key, node] of stable.laneSlots) {
    check(card._laneSlotNodes.get(key) === node, `${label}: keyed policy-lane slot stable ${key}`);
  }
  for (const [key, node] of stable.bands) {
    check(card._bandNodes.get(key) === node, `${label}: keyed action band stable ${key}`);
  }
  for (const [key, record] of stable.powerBars) {
    check(
      card._powerBarNodes.get(key)?.pv === record.pv
        && card._powerBarNodes.get(key)?.load === record.load,
      `${label}: keyed PV/LOAD bars stable ${key}`,
    );
  }
  for (const [key, record] of stable.rawRows) {
    check(card._rawPointRows.get(key)?.row === record.row, `${label}: keyed raw row stable ${key}`);
  }
  check(fakeWindow.scrollY === 950, `${label}: page scroll stable`);
  check(refs.viewport.scrollLeft === 240, `${label}: timeline scroll stable`);
  check(refs.details.rce.open, `${label}: details open stable`);
  check(card._selectedKey === selectedKey, `${label}: selected key stable`);
  check(fakeDocument.activeElement === firstPoint, `${label}: focus node stable`);
}

ledger.reset();
const rafBeforeBurst = rafCallbackCount;
for (let index = 0; index < 100; index += 1) {
  const next = clone(hass);
  next.states[`sensor.unrelated_${index}`] = { state: String(index), attributes: {} };
  card.hass = next;
}
check(rafQueue.size === 1, "100 unrelated burst updates coalesce to one rAF");
flushAnimationFrames();
check(rafCallbackCount === rafBeforeBurst + 1, "one callback for unrelated burst");
equal(ledger.snapshot(), { textWrites: 0, attributeWrites: 0, classWrites: 0, childListWrites: 0, focusCalls: 0, scrollCalls: 0 }, "unrelated burst is a DOM no-op");
assertStable("unrelated burst");

ledger.reset();
for (let index = 0; index < 100; index += 1) {
  const next = clone(hass);
  next.states[`sensor.flush_unrelated_${index}`] = { state: String(index), attributes: {} };
  card.hass = next;
  check(flushAnimationFrames() === 1, `unrelated flush update ${index} has one frame`);
}
equal(ledger.snapshot(), { textWrites: 0, attributeWrites: 0, classWrites: 0, childListWrites: 0, focusCalls: 0, scrollCalls: 0 }, "100 unrelated flushed updates are DOM no-ops");
assertStable("unrelated flushed");

const ageOnly = (source, index) => {
  const next = clone(source);
  for (const key of ["rce_timeline_entity", "tariff_timeline_entity", "rcm_timeline_entity"]) {
    const entity = next.states[EXPECTED_BINDINGS[key]];
    entity.last_updated = `2026-08-30T16:${String(index % 60).padStart(2, "0")}:00.000Z`;
    entity.attributes.generated_at = `2026-08-30T16:${String(index % 60).padStart(2, "0")}:00.000Z`;
    entity.attributes.active_observed_at = entity.attributes.generated_at;
    entity.attributes.current_actual.observed_at = entity.attributes.generated_at;
    entity.attributes.current_actual.source_ages_seconds = { pv: index, load: index + 1, battery: index + 2, grid: index + 3, soc: index + 4 };
  }
  return next;
};

ledger.reset();
for (let index = 0; index < 100; index += 1) card.hass = ageOnly(hass, index);
check(rafQueue.size === 1, "100 age-only burst updates coalesce to one rAF");
flushAnimationFrames();
equal(ledger.snapshot(), { textWrites: 0, attributeWrites: 0, classWrites: 0, childListWrites: 0, focusCalls: 0, scrollCalls: 0 }, "age-only burst is a DOM no-op");
assertStable("age-only burst");

ledger.reset();
for (let index = 0; index < 100; index += 1) {
  card.hass = ageOnly(hass, index);
  check(flushAnimationFrames() === 1, `age-only flush update ${index} has one frame`);
}
equal(ledger.snapshot(), { textWrites: 0, attributeWrites: 0, classWrites: 0, childListWrites: 0, focusCalls: 0, scrollCalls: 0 }, "100 age-only flushed updates are DOM no-ops");
assertStable("age-only flushed");

const { card: actualCard, hass: actualHass } = mount();
const actualExpectedNode = actualCard._refs.socExpected;
const actualExpectedPath = actualExpectedNode.getAttribute("d");
const actualPlanPaths = Object.fromEntries(
  Object.entries(actualCard._refs.socSeries).map(([key, node]) => [key, node.getAttribute("d")]),
);
const actualPointNode = actualCard._refs.currentSocPoint;
const actualPointBefore = actualPointNode.getAttribute("cy");
const actualPathPatchesBefore = actualCard._metrics.planPathPatchCount;
const actualPvTileBefore = actualCard._refs.summaryValues.battery.value.textContent;
const actualUpdate = clone(actualHass);
actualUpdate.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.current_actual.soc_percent = 73;
ledger.reset();
actualCard.hass = actualUpdate;
check(flushAnimationFrames() === 1, "one policy-current SOC change uses one visual frame");
check(
  actualCard._refs.currentSocValue.textContent.includes("75")
    && actualCard._refs.summaryValues.battery.value.textContent === actualPvTileBefore
    && actualPointNode.getAttribute("cy") === actualPointBefore,
  "independent policy current SOC cannot replace the shared physical baseline marker",
);
check(
  actualCard._refs.socExpected === actualExpectedNode
    && actualExpectedNode.getAttribute("d") === actualExpectedPath
    && Object.entries(actualCard._refs.socSeries).every(
      ([key, node]) => node.getAttribute("d") === actualPlanPaths[key],
    ),
  "policy-current SOC change neither replaces nor mutates baseline, canonical, or comparison trajectories",
);
check(
  actualCard._metrics.planPathPatchCount === actualPathPatchesBefore
    && ledger.childListWrites === 0,
  "policy-current SOC change creates no semantic-plan repaint, SVG rebuild or path mutation",
);

const canonicalSocUpdate = clone(actualHass);
const canonicalAttributes =
  canonicalSocUpdate.states[EXPECTED_BINDINGS.canonical_timeline_entity].attributes;
canonicalAttributes.initial_soc_percent -= 2;
canonicalAttributes.final_soc_percent -= 2;
canonicalAttributes.ledger_revision = "c".repeat(64);
for (const slot of canonicalAttributes.slots) {
  slot.soc_equation.soc_start_percent -= 2;
  slot.soc_equation.soc_end_percent -= 2;
  slot.protected_reserve.margin_end_percent -= 2;
}
ledger.reset();
actualCard.hass = canonicalSocUpdate;
check(flushAnimationFrames() === 1, "one canonical SOC update uses one visual frame");
check(
  actualCard._refs.currentSocValue.textContent.includes("75")
    && actualCard._refs.summaryValues.battery.value.textContent === actualPvTileBefore
    && actualPointNode.getAttribute("cy") === actualPointBefore
    && actualExpectedNode.getAttribute("d") !== actualExpectedPath
    && actualCard._pendingPlanModel === null
    && actualCard._metrics.planPathPatchCount === actualPathPatchesBefore + 1
    && ![...timerQueue.values()].some((timer) => timer.delay >= 175_000),
  "canonical content changes are visible in one frame without replacing physical baseline SOC",
);
check(
  Object.entries(actualCard._refs.socSeries).every(
    ([key, node]) => node.getAttribute("d") === actualPlanPaths[key],
  ),
  "canonical-only content update leaves all policy comparison trajectories unchanged",
);

ledger.reset();
const genuine = clone(hass);
const rcePoint = genuine.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.points[0];
rcePoint.policy.planned_export_kwh = 1.75;
rcePoint.policy.expected_revenue_pln = 1.44;
genuine.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.plan_revision = 6;
genuine.states[EXPECTED_BINDINGS.supervisor_entity].attributes.candidate_summaries.find((item) => item.policy_id === "rce").candidate_revision = 6;
genuine.states[EXPECTED_BINDINGS.supervisor_entity].attributes.selected_candidate_revision = 6;
const genuinePlanPatchesBefore = card._metrics.planPathPatchCount;
card.hass = genuine;
check(flushAnimationFrames() === 1, "genuine update uses one visual frame");
check(
  card._pendingPlanModel === null
    && card._metrics.planPathPatchCount === genuinePlanPatchesBefore + 1
    && card._renderedPlanModel?.timelines?.rce?.planRevision === 6,
  "genuine semantic update is rendered in the next visual frame",
);
check(ledger.childListWrites === 0, "same-key genuine update does not mutate child lists");
check(card._slotNodes.get(selectedKey) === firstPoint, "same-key baseline slot remains in place");
check(refs.lanes.tariff === stable.lanes.tariff && refs.lanes.rcm === stable.lanes.rcm, "unaffected lanes remain identical");
check(refs.policies.tariff === stable.policies.tariff && refs.policies.rcm === stable.policies.rcm, "unaffected policies remain identical");
check(fakeWindow.scrollY === 950 && refs.viewport.scrollLeft === 240, "genuine update preserves both scroll positions");
check(refs.details.rce.open && fakeDocument.activeElement === firstPoint, "genuine update preserves details and focus");
check(ledger.scrollCalls === 0 && ledger.focusCalls === 0, "background update makes no scroll/focus call");
check(hass.callServiceCount === 0 && hass.callWSCount === 0, "no HA service or WS calls");
// The production control releases its duplicate-click guard in a Promise
// microtask. This deliberately synchronous DOM harness mirrors that settled
// state before a later, independent refresh scenario.
refs.refresh.disabled = false;
refs.refresh.removeAttribute("aria-busy");

const componentStart = cardSource.indexOf("const HOYMILES_AUTOMATION_PLANNER_BINDINGS");
const componentEnd = cardSource.indexOf("const HOYMILES_SUPERVISOR_BINDINGS", componentStart);
const componentSource = cardSource.slice(componentStart, componentEnd);
const canonicalExtensionStart = cardSource.indexOf("const HOYMILES_C4_LIVE_INTERVAL_MS");
const canonicalExtensionEnd = cardSource.indexOf(
  "class HoymilesEmsSupervisorCanonicalCard",
  canonicalExtensionStart,
);
const canonicalExtensionSource = cardSource.slice(
  canonicalExtensionStart,
  canonicalExtensionEnd,
);
const activePlannerSource = componentSource + canonicalExtensionSource;
for (const forbidden of [
  "callService", "callWS", "fetch(", "new WebSocket", "fireEvent(",
  "select_option", "turn_on", "turn_off", "toggle", "modbus",
  "owner_acquire", "grant_execution", "handover", "setInterval",
]) {
  check(!activePlannerSource.includes(forbidden), `planner source excludes ${forbidden}`);
}
check(
  canonicalExtensionSource.includes("const HOYMILES_C4_LIVE_INTERVAL_MS = 5_000;")
    && canonicalExtensionSource.includes("const HOYMILES_C4_USER_IDLE_MS = 1_500;")
    && !canonicalExtensionSource.includes("HOYMILES_C4_PLAN_INTERVAL_MS")
    && !canonicalExtensionSource.includes("180_000")
    && canonicalExtensionSource.includes("this._pendingPlanModel = model")
    && canonicalExtensionSource.includes("this._pendingLiveModel = model"),
  "canonical visual cadence keeps five-second live patches and defers plan content only during a short active interaction",
);
const c4FlushSource = canonicalExtensionSource.slice(
  canonicalExtensionSource.indexOf("  _flush()"),
  canonicalExtensionSource.indexOf("  _patchExecution(model)", canonicalExtensionSource.indexOf("  _flush()")),
);
const c4ApplyPlanSource = canonicalExtensionSource.slice(
  canonicalExtensionSource.indexOf("  _applyPlanPatch(model"),
  canonicalExtensionSource.indexOf("  _reconcileCanonicalLane", canonicalExtensionSource.indexOf("  _applyPlanPatch(model")),
);
check(
  !c4FlushSource.includes("_patchCanonicalAvailabilityGate(model)")
    && !c4FlushSource.includes("_patchBaselineInspectorSnapshot(model")
    && !c4FlushSource.includes("_patchSelectedDetail("),
  "status snapshots cannot bypass the atomic plan-content path through availability, raw rows or inspector detail",
);
check(
  c4ApplyPlanSource.includes("this._inspectorModel = model")
    && c4ApplyPlanSource.includes("this._patchCanonicalAvailabilityGate(model)")
    && c4ApplyPlanSource.includes("this._patchSelectedDetail(model)"),
  "geometry, availability, raw data and inspector detail are committed as one rendered-plan snapshot",
);
check((componentSource.match(/shadowRoot\.replaceChildren\(/g) || []).length === 1, "exactly one initial shadowRoot.replaceChildren");
for (const forbidden of ["shadowRoot.innerHTML", "outerHTML", "insertAdjacentHTML", ".scrollTo(", ".scrollBy("]) {
  check(!componentSource.includes(forbidden), `planner source excludes ${forbidden}`);
}

// AP-3B-R3-C2 fixture 1: sufficient SOC remains natural Self-Use and the
// frontend never invents Grid Support for a zero-import idle trajectory.
const sufficientBatteryFixture = mount(tariffPresentationFixture({
  currentSoc: 60,
  startSoc: 60,
  endSoc: 55,
  targetSoc: null,
  actionCode: "idle",
  batteryKw: -0.5,
  gridImportKw: 0,
  plannedImportKwh: 0,
  plannedChargeKw: 0,
}));
const sufficientBatteryDetail = selectTariffInterval(sufficientBatteryFixture.card);
check(
  sufficientBatteryDetail.event === "Brak działania taryfy"
    && sufficientBatteryDetail.battery.includes("60% → 55%")
    && sufficientBatteryFixture.card._refs.heroFacts.physical.value.textContent
      === "Teraz: autokonsumpcja — dom zasilany z magazynu."
    && !sufficientBatteryFixture.card.shadowRoot.textContent.includes("Plan taryfowy: tani prąd dla domu"),
  "C2 fixture 1 keeps sufficient-SOC zero-import operation in natural Self-Use",
);

// Fixture 2: a valid future shortage-preservation plan remains explicit plan
// intent and does not claim that grid supply is already physical.
const laterShortageFixture = mount(tariffPresentationFixture({
  currentSoc: 60,
  startSoc: 60,
  endSoc: 60,
  targetSoc: 60,
}));
const laterShortageDetail = selectTariffInterval(laterShortageFixture.card);
check(
  laterShortageDetail.event === "Plan taryfowy: tani prąd dla domu"
    && laterShortageDetail.reason === "Magazyn ma zostać zachowany na droższe godziny."
    && laterShortageDetail.planOnly === "Plan nie jest obecnie wykonywany."
    && laterShortageDetail.blocker === "Brak danych"
    && !laterShortageDetail.battery.includes("Dom jest teraz zasilany tanią energią z sieci."),
  "C2 fixture 2 preserves the economically justified plan without presenting it as execution",
);

// Fixture 3: exact live B case — preview plan, automation off and physical
// Self-Use with the home supplied from the battery.
const disabledLiveFixture = mount(tariffPresentationFixture({
  currentSoc: 66,
  currentPvKw: 0,
  currentLoadKw: 2.013,
  currentBatteryKw: -2.505,
  currentGridKw: -0,
  startSoc: 64.56807420034568,
  endSoc: 64.56807420034568,
  targetSoc: 64.56807420034568,
  gridImportKw: 0.4829614035087719,
  plannedImportKwh: 0.24148070175438596,
  tariffEnabled: false,
  currentPoint: true,
  startEligible: false,
  suppressionReason: "intent_not_stable",
}));
const disabledLiveDetail = selectTariffInterval(disabledLiveFixture.card);
check(
  disabledLiveDetail.event === "Plan taryfowy: tani prąd dla domu"
    && disabledLiveDetail.reason === "Magazyn ma zostać zachowany na droższe godziny."
    && disabledLiveDetail.planOnly === "Plan nie jest obecnie wykonywany."
    && disabledLiveDetail.blocker === "Plan nie zostanie teraz uruchomiony. Powód: intencja planu musi pozostać niezmienna przez 120 sekund."
    && disabledLiveFixture.card._refs.heroFacts.physical.value.textContent
      === "Teraz: autokonsumpcja — dom zasilany z magazynu."
    && disabledLiveFixture.card._lastModel.policies.tariff.active === false,
  "C2 fixture 3 separates the exact disabled live plan from physical battery Self-Use",
);

// Fixture 4: no planned or modeled import forbids the home-grid label.
const zeroImportFixture = mount(tariffPresentationFixture({
  currentSoc: 60,
  startSoc: 60,
  endSoc: 60,
  targetSoc: 60,
  gridImportKw: 0,
  plannedImportKwh: 0,
}));
const zeroImportDetail = selectTariffInterval(zeroImportFixture.card);
check(
  zeroImportDetail.event === "Plan wymaga sprawdzenia"
    && !zeroImportFixture.card._refs.selectedDetail.textContent.includes("tani prąd dla domu")
    && !zeroImportFixture.card._refs.selectedDetail.textContent.includes("Tania strefa dla domu"),
  "C2 fixture 4 forbids a home-grid claim when both import fields are zero",
);

// Fixture 5: helper, latch, point, physical mode and measured flow all agree.
const activeSupportFixture = mount(tariffPresentationFixture({
  currentSoc: 60,
  currentPvKw: 0,
  currentLoadKw: 1.2,
  currentBatteryKw: 0,
  currentGridKw: 1.2,
  startSoc: 60,
  endSoc: 60,
  targetSoc: 60,
  tariffActive: true,
  physicalMode: "grid_charge",
  pointActive: true,
  candidateActive: true,
}));
const activeSupportDetail = selectTariffInterval(activeSupportFixture.card);
check(
  activeSupportDetail.event === "Tania strefa dla domu"
    && activeSupportDetail.planOnly === "Bieżące działanie potwierdzone fizycznie"
    && activeSupportDetail.battery.includes("Dom jest teraz zasilany tanią energią z sieci.")
    && activeSupportFixture.card._refs.heroFacts.physical.value.textContent
      === "Teraz: dom jest zasilany z sieci w taniej strefie."
    && activeSupportFixture.card._refs.badge.dataset.status === "physically_active",
  "C2 fixture 5 requires complete physical evidence before confirming cheap home supply",
);

// Fixture 6: a non-zero raw SOC change remains visible even if both ends
// round to the same one-decimal display value.
const hiddenSocChangeFixture = mount(tariffPresentationFixture({
  currentSoc: 60.01,
  startSoc: 60.01,
  endSoc: 60.04,
  targetSoc: 60,
}));
const hiddenSocChangeDetail = selectTariffInterval(hiddenSocChangeFixture.card);
check(
  hiddenSocChangeDetail.event === "Plan wymaga sprawdzenia"
    && hiddenSocChangeDetail.battery.includes("Zmiana mniejsza niż 1 p.p.")
    && hiddenSocChangeDetail.reason === "Plan wymaga sprawdzenia"
    && !hiddenSocChangeFixture.card._refs.selectedDetail.textContent.includes("Magazyn ma zostać zachowany"),
  "C2 fixture 6 never infers battery preservation from rounded-equal SOC",
);

// Fixture 7: active metadata contradicted by Self-Use telemetry fails closed.
const conflictingExecutionFixture = mount(tariffPresentationFixture({
  currentSoc: 60,
  currentPvKw: 0,
  currentLoadKw: 1.2,
  currentBatteryKw: -1.2,
  currentGridKw: 0,
  startSoc: 60,
  endSoc: 60,
  targetSoc: 60,
  tariffActive: true,
  physicalMode: "self_use",
  pointActive: true,
  candidateActive: true,
}));
const conflictingExecutionDetail = selectTariffInterval(conflictingExecutionFixture.card);
check(
  conflictingExecutionDetail.event === "Plan taryfowy: tani prąd dla domu"
    && conflictingExecutionDetail.planOnly === "Plan nie jest obecnie wykonywany."
    && conflictingExecutionFixture.card._refs.badge.dataset.status === "data_unavailable"
    && conflictingExecutionFixture.card._refs.safetyText.textContent.startsWith("Wykonanie niepotwierdzone / dane sprzeczne")
    && conflictingExecutionFixture.card._lastModel.policies.tariff.active === false,
  "C2 fixture 7 fails closed when plan metadata contradicts physical flow",
);

// AP-3B-R3-C3 A: automation enabled, but the exact current Grid Support run
// is start-suppressed. The physical Self-Use state remains an independent fact.
const suppressedStartFixture = mount(tariffPresentationFixture({
  currentSoc: 62,
  currentPvKw: 0,
  currentLoadKw: 0.759,
  currentBatteryKw: -0.914,
  currentGridKw: 0,
  startSoc: 62,
  endSoc: 62,
  targetSoc: 62,
  currentPoint: true,
  startEligible: false,
  suppressionReason: "intent_not_stable",
  candidateStartEligible: false,
  tariffEnabled: true,
}));
const suppressedStartDetail = selectTariffInterval(suppressedStartFixture.card);
check(
  suppressedStartDetail.event === "Plan taryfowy: tani prąd dla domu"
    && suppressedStartDetail.planOnly === "Plan nie zostanie teraz uruchomiony."
    && suppressedStartDetail.blocker === "Plan nie zostanie teraz uruchomiony. Powód: intencja planu musi pozostać niezmienna przez 120 sekund."
    && suppressedStartDetail.status === "suppressed"
    && suppressedStartDetail.assessment.enabled === true
    && suppressedStartDetail.assessment.startEligibility === "suppressed"
    && suppressedStartDetail.assessment.schedulerActive === false
    && suppressedStartDetail.assessment.modeAcknowledged === null
    && suppressedStartDetail.assessment.physicalConfirmed === false
    && suppressedStartFixture.card._refs.heroFacts.physical.value.textContent
      === "Teraz: autokonsumpcja — dom zasilany z magazynu."
    && !suppressedStartFixture.card._refs.safetyText.textContent.includes("sprzęt"),
  "R3-C3 classification A presents localized start suppression without a hardware-failure claim",
);

// R3-C3 B presentation: both revision-matched plan and Supervisor candidate
// allow a start, but the scheduler helper is still inactive.
const readyStartFixture = mount(tariffPresentationFixture({
  currentSoc: 62,
  currentPvKw: 0,
  currentLoadKw: 0.8,
  currentBatteryKw: -0.9,
  currentGridKw: 0,
  startSoc: 62,
  endSoc: 62,
  targetSoc: 62,
  currentPoint: true,
  startEligible: true,
  suppressionReason: "eligible",
  candidateStartEligible: true,
  tariffEnabled: true,
}));
const readyStartDetail = selectTariffInterval(readyStartFixture.card);
check(
  readyStartDetail.event === "Plan taryfowy: tani prąd dla domu"
    && readyStartDetail.planOnly === "Plan gotowy do uruchomienia."
    && readyStartDetail.status === "ready_to_start"
    && readyStartDetail.assessment.state === "ready_inactive"
    && readyStartDetail.assessment.startEligible === true
    && readyStartDetail.assessment.schedulerActive === false
    && readyStartDetail.assessment.physicalConfirmed === false
    && !readyStartDetail.battery.includes("Dom jest teraz zasilany tanią energią z sieci."),
  "R3-C3 classification B presents an eligible inactive run as ready, never physically active",
);

// R3-C3 C: lifecycle metadata says Grid Support is active, but register 4300
// still reads Self-Use.
check(
  conflictingExecutionDetail.assessment.state === "mode_ack_mismatch"
    && conflictingExecutionDetail.assessment.modeAcknowledged === false
    && conflictingExecutionDetail.assessment.physicalConfirmed === false
    && conflictingExecutionFixture.card._refs.safetyText.textContent.startsWith("Wykonanie niepotwierdzone / dane sprzeczne"),
  "R3-C3 classification C fails closed when Grid Charge mode is not acknowledged",
);

// R3-C3 D: Grid Charge is acknowledged, but the measured home flow remains
// contradictory. The formal 60-second capability diagnosis belongs to field sampling.
const wrongFlowFixture = mount(tariffPresentationFixture({
  currentSoc: 60,
  currentPvKw: 0,
  currentLoadKw: 1.2,
  currentBatteryKw: -1,
  currentGridKw: 0.05,
  startSoc: 60,
  endSoc: 60,
  targetSoc: 60,
  tariffActive: true,
  physicalMode: "grid_charge",
  pointActive: true,
  candidateActive: true,
}));
const wrongFlowDetail = selectTariffInterval(wrongFlowFixture.card);
check(
  wrongFlowDetail.assessment.state === "physical_flow_mismatch"
    && wrongFlowDetail.assessment.modeAcknowledged === true
    && wrongFlowDetail.assessment.physicalConfirmed === false
    && wrongFlowFixture.card._refs.badge.dataset.status === "data_unavailable"
    && wrongFlowFixture.card._refs.safetyText.textContent.startsWith("Wykonanie niepotwierdzone / dane sprzeczne")
    && wrongFlowDetail.event === "Plan taryfowy: tani prąd dla domu",
  "R3-C3 classification D fails closed when acknowledged mode contradicts physical flow",
);

// R3-C3 E is the physically coherent C2 fixture 5.
check(
  activeSupportDetail.assessment.state === "confirmed"
    && activeSupportDetail.assessment.schedulerActive === true
    && activeSupportDetail.assessment.activeAction === "grid_support"
    && activeSupportDetail.assessment.modeAcknowledged === true
    && activeSupportDetail.assessment.physicalConfirmed === true,
  "R3-C3 classification E exposes separate scheduler, mode-ack and physical-confirmation facts",
);

const suppressedEnglishFixture = mount(tariffPresentationFixture({
  language: "en",
  currentPoint: true,
  startEligible: false,
  suppressionReason: "intent_not_stable",
  candidateStartEligible: false,
}));
const suppressedEnglishDetail = selectTariffInterval(suppressedEnglishFixture.card);
check(
  suppressedEnglishDetail.planOnly === "The plan will not start now."
    && suppressedEnglishDetail.blocker === "The plan will not start now. Reason: the plan intent must remain unchanged for 120 seconds.",
  "R3-C3 suppression reason is localized in English without exposing a raw reason code",
);

const unknownSuppressionFixture = mount(tariffPresentationFixture({
  currentPoint: true,
  startEligible: false,
  suppressionReason: "future_backend_reason",
  candidateStartEligible: false,
}));
const unknownSuppressionDetail = selectTariffInterval(unknownSuppressionFixture.card);
check(
  unknownSuppressionDetail.blocker === "Plan nie zostanie teraz uruchomiony. Powód: warunki uruchomienia nie zostały potwierdzone."
    && !unknownSuppressionFixture.card.shadowRoot.textContent.includes("future_backend_reason"),
  "R3-C3 unknown suppression code uses a localized fail-closed fallback",
);

const wrongActionFixture = mount(tariffPresentationFixture({
  currentSoc: 60,
  currentPvKw: 0,
  currentLoadKw: 1.2,
  currentBatteryKw: 0,
  currentGridKw: 1.2,
  startSoc: 60,
  endSoc: 60,
  targetSoc: 60,
  tariffActive: true,
  activeAction: "battery_charge",
  physicalMode: "grid_charge",
  pointActive: true,
  candidateActive: true,
}));
const wrongActionDetail = selectTariffInterval(wrongActionFixture.card);
check(
  wrongActionDetail.assessment.state === "active_context_mismatch"
    && wrongActionDetail.assessment.physicalConfirmed === false
    && wrongActionFixture.card._refs.badge.dataset.status === "data_unavailable",
  "R3-C3 active-action mismatch cannot be presented as confirmed Grid Support",
);

// Existing charge regression: positive planned charging and an exact SOC increase are real charge.
const realChargeFixture = mount(tariffPresentationFixture({
  startSoc: 40,
  endSoc: 50,
  targetSoc: 50,
  actionCode: "battery_charge",
  batteryKw: 2,
  gridImportKw: 2.7,
  plannedImportKwh: 1.5,
  plannedChargeKw: 2,
}));
const realChargeDetail = selectTariffInterval(realChargeFixture.card);
check(
  realChargeDetail.event === "Ładowanie magazynu z sieci"
    && realChargeDetail.battery.includes("Ładowanie magazynu: 40% → 50%"),
  "charge regression presents 40-to-50 battery charging",
);

// Existing target regression: no requested charge when the start SOC already meets the target.
const targetSatisfiedFixture = mount(tariffPresentationFixture({
  startSoc: 60,
  endSoc: 60,
  targetSoc: 50,
  actionCode: "battery_charge",
  batteryKw: 0,
  gridImportKw: 0,
  plannedImportKwh: 0,
  plannedChargeKw: 0,
}));
const targetSatisfiedDetail = selectTariffInterval(targetSatisfiedFixture.card);
check(
  targetSatisfiedDetail.event === "Ładowanie niepotrzebne — cel już osiągnięty"
    && targetSatisfiedDetail.reason === "Przewidywany poziom magazynu jest już wystarczający.",
  "target regression explains an already-satisfied charging target",
);

// Existing nullability regression: nullable backend fields stay unavailable and never become fake zero.
const missingValueFixture = mount(tariffPresentationFixture({
  endSoc: null,
  batteryKw: null,
  plannedChargeKw: null,
}));
const missingValueDetail = selectTariffInterval(missingValueFixture.card);
check(
  missingValueDetail.event === "Plan wymaga sprawdzenia"
    && missingValueDetail.battery.includes("Przewidywany poziom po przedziale: Brak danych")
    && !missingValueDetail.battery.includes("Przewidywany poziom po przedziale: 0%"),
  "nullability regression exposes missing data without fabricating zero",
);

// Fixture 6: missing Supervisor confirmation does not invalidate available plans.
const noSupervisorHass = hassFixture();
noSupervisorHass.states[EXPECTED_BINDINGS.supervisor_entity] = {
  state: "unavailable",
  attributes: {},
};
const noSupervisorFixture = mount(noSupervisorHass);
check(
  noSupervisorFixture.card._refs.badge.textContent
      === "Plan dostępny — wykonanie niepotwierdzone"
    && noSupervisorFixture.card._refs.badge.dataset.status === "plan_unconfirmed"
    && noSupervisorFixture.card._refs.heroFacts.supervisor.value.textContent
      === "Brak danych EMS"
    && noSupervisorFixture.card._refs.safetyText.textContent
      === "Plan jest dostępny, ale EMS nie potwierdza jego wykonania."
    && !noSupervisorFixture.card._refs.badge.textContent.includes("Dane niedostępne"),
  "fixture 6 keeps an available plan visible when only Supervisor confirmation is missing",
);

// Fixture 7: three native slots keep their identities inside one visible semantic band.
const continuousTariffHass = hassFixture();
continuousTariffHass.states[EXPECTED_BINDINGS.tariff_timeline_entity] = timeline(
  "tariff", 12, 6, "2026-08-30T15:00:00.000Z", 3,
);
const continuousAttributes =
  continuousTariffHass.states[EXPECTED_BINDINGS.tariff_timeline_entity].attributes;
continuousAttributes.current_actual.soc_percent = 50;
for (const item of continuousAttributes.points) {
  Object.assign(item, {
    action_code: "hold",
    battery_kw: 0,
    grid_kw: 1,
    grid_import_kw: 1,
    grid_export_kw: 0,
    soc_percent: 50,
    target_soc_percent: 50,
    policy: {
      ...item.policy,
      planned_import_kwh: 0.5,
      planned_charge_kw: 0,
    },
  });
}
const continuousTariffFixture = mount(continuousTariffHass);
const visibleTariffBands = [...continuousTariffFixture.card._bandNodes.values()]
  .filter((record) => record.element.dataset.policy === "tariff");
check(
  visibleTariffBands.length === 1
    && visibleTariffBands[0].pointKeys.length === 1
    && visibleTariffBands[0].element.dataset.sourcePolicy === "canonical"
    && walk(visibleTariffBands[0].element)
      .filter((node) => node.tagName === "HA-ICON").length === 1
    && continuousTariffFixture.card._lastModel.timelines.tariff.points.length === 3
    && new Set(
      continuousTariffFixture.card._lastModel.timelines.tariff.points.map(
        (point) => point.key,
      ),
    ).size === 3
    && Boolean(continuousTariffFixture.card._refs.socSeries.tariff.getAttribute("d"))
    && continuousTariffFixture.card._pointNodes.size === 0,
  "fixture 7 preserves three exact tariff comparison points while rendering only the canonical action band",
);

// Fixture 8: tariff interactions remain local and observational.
const observationalFixture = mount(tariffPresentationFixture());
selectTariffInterval(observationalFixture.card);
observationalFixture.card._refs.compareButtons.tariff.dispatch("click");
observationalFixture.card._refs.compareButtons.tariff.dispatch("click");
check(
  observationalFixture.hass.callServiceCount === 0
    && observationalFixture.hass.callWSCount === 0
    && fetchCount === 0
    && webSocketCount === 0
    && !componentSource.includes("fetch(")
    && !componentSource.includes("new WebSocket"),
  "fixture 8 keeps click and comparison interactions fully observational",
);

const zeroImportChargeFixture = mount(tariffPresentationFixture({
  startSoc: 40,
  endSoc: 50,
  targetSoc: 50,
  actionCode: "battery_charge",
  batteryKw: 2,
  gridImportKw: 0,
  plannedImportKwh: 0,
  plannedChargeKw: 2,
}));
check(
  selectTariffInterval(zeroImportChargeFixture.card).event
    === "Plan wymaga sprawdzenia",
  "zero-import charging contradiction fails closed to verification",
);

const unsupportedTariffFixture = mount(tariffPresentationFixture({
  actionCode: "export",
  batteryKw: 0,
  gridImportKw: 0,
  plannedImportKwh: 0,
  plannedChargeKw: 0,
}));
check(
  selectTariffInterval(unsupportedTariffFixture.card).event
    === "Plan wymaga sprawdzenia",
  "unsupported tariff action fails closed to verification",
);

const largeUnexplainedRiseFixture = mount(tariffPresentationFixture({
  startSoc: 40,
  endSoc: 70,
  targetSoc: 50,
}));
check(
  selectTariffInterval(largeUnexplainedRiseFixture.card).event
    === "Plan wymaga sprawdzenia",
  "large SOC rise without battery charging evidence fails closed",
);

const degradedNoSupervisorHass = hassFixture();
degradedNoSupervisorHass.states[EXPECTED_BINDINGS.supervisor_entity] = {
  state: "unavailable",
  attributes: {},
};
for (const key of ["rce_timeline_entity", "tariff_timeline_entity", "rcm_timeline_entity"]) {
  degradedNoSupervisorHass.states[EXPECTED_BINDINGS[key]].attributes.quality = "degraded";
}
const degradedNoSupervisorFixture = mount(degradedNoSupervisorHass);
check(
  degradedNoSupervisorFixture.card._refs.badge.dataset.status
    === "data_unavailable",
  "degraded plan data cannot be upgraded by a missing-Supervisor fallback",
);

const disabledNoSupervisorHass = hassFixture();
disabledNoSupervisorHass.states[EXPECTED_BINDINGS.supervisor_entity] = {
  state: "unavailable",
  attributes: {},
};
for (const key of ["rce_enabled_entity", "tariff_enabled_entity", "rcm_enabled_entity"]) {
  disabledNoSupervisorHass.states[EXPECTED_BINDINGS[key]].state = "off";
}
const disabledNoSupervisorFixture = mount(disabledNoSupervisorHass);
check(
  disabledNoSupervisorFixture.card._refs.badge.dataset.status
    === "policy_disabled",
  "disabled automations remain explicitly disabled when Supervisor data is missing",
);

const blockedSupervisorHass = hassFixture();
blockedSupervisorHass.states[EXPECTED_BINDINGS.supervisor_entity].state = "blocked";
const blockedSupervisorFixture = mount(blockedSupervisorHass);
check(
  blockedSupervisorFixture.card._refs.badge.dataset.status === "blocked"
    && blockedSupervisorFixture.card._refs.safetyText.textContent
      !== "Brak działania — automatyka teraz niczego nie zmienia.",
  "blocked Supervisor state is always presented fail closed",
);

check(componentSource.includes("overflow-x: auto"), "timeline owns horizontal overflow");
check(componentSource.includes("overflow-anchor: none"), "dynamic content disables overflow anchoring");
check(componentSource.includes("touch-action: pan-x pan-y"), "touch permits page and timeline gestures");
check(componentSource.includes("min-height: 44px") && componentSource.includes("min-width: 44px"), "phone targets are at least 44 by 44 CSS px");
check(
  componentSource.includes("var(--hoymiles-aurora-surface)")
    && componentSource.includes("var(--hoymiles-aurora-border)")
    && componentSource.includes("var(--hoymiles-aurora-shadow)"),
  "planner reuses native Aurora surface, border and shadow tokens",
);
check(
  componentSource.includes('timelineTitle: "Poziom magazynu — dziś i jutro"')
    && componentSource.includes('timelineTitle: "Battery level — today and tomorrow"'),
  "R3 freezes the exact homeowner-first PL/EN chart titles",
);
check(
  componentSource.includes("SOC pokazuje, ile energii zostało w magazynie. 100% oznacza pełny magazyn.")
    && componentSource.includes("SOC shows how much energy remains in the battery. 100% means a full battery."),
  "R3 freezes the exact plain-language PL/EN SOC explanation",
);
check(
  canonicalExtensionSource.includes(
    '"canonical.slots.soc_equation.soc_start_percent+soc_end_percent"',
  )
    && canonicalExtensionSource.includes(
      '"baseline.points[].soc_start_percent+soc_end_percent"',
    )
    && canonicalExtensionSource.includes(
      '"baseline.current_actual_soc_percent"',
    ),
  "baseline physical SOC and optional canonical trajectory sources are explicit and cannot be discovered heuristically",
);
check(
  componentSource.includes("--ap-expected: var(--hoymiles-aurora-good)")
    && componentSource.includes(".ap-soc-expected")
    && componentSource.includes("stroke-width: 3.8")
    && componentSource.includes("drop-shadow"),
  "main battery trajectory uses the dominant Aurora green semantic style",
);
check(
  componentSource.includes("hoymilesPlannerTwoHourTicks")
    && componentSource.includes("parts.hour % 2 !== 0")
    && componentSource.includes('tick.label === "00:00"') === false,
  "runtime derives real Europe/Warsaw two-hour ticks instead of fixed start/end labels",
);
check(
  componentSource.includes('.ap-time-grid-line[data-midnight="true"]')
    && componentSource.includes("dayLabel = copy.today")
    && componentSource.includes("copy.tomorrow"),
  "runtime and CSS preserve a distinct midnight plus Today/Tomorrow labels",
);
check(
  componentSource.includes('.ap-soc-baseline { display: none; }')
    && canonicalExtensionSource.includes('.ap-soc-baseline { display: inline !important; }')
    && componentSource.includes('.ap-soc-series:not(.ap-soc-floor),\n  .ap-soc-target { display: none; }')
    && componentSource.includes('data-compare'),
  "canonical promotion makes the independent baseline visible while policy alternatives remain local comparisons",
);
check(
  componentSource.includes('.ap-soc-floor { display: block;')
    && componentSource.includes('stroke-width: 2.6; stroke-dasharray: 12 7; opacity: 1;')
    && componentSource.includes('svg.append(baseline, expectedSoc, floor, planTarget)'),
  "minimum reserve stays above the expected trajectory while expert policy series stay hidden",
);
check(
  componentSource.includes('stroke-width: 2.4; stroke-dasharray: 10 7;')
    && componentSource.includes('var(--ap-target) 46%')
    && componentSource.includes('.ap-line-legend-item[data-line="target"] .ap-line-sample { border-top-style: dashed;'),
  "plan target stays legible when it overlaps the dominant expected trajectory",
);
check(
  componentSource.includes("--ap-panel-surface")
    && componentSource.includes("backdrop-filter: blur(10px)")
    && componentSource.includes("radial-gradient")
    && componentSource.includes("ap-policy::before"),
  "planner uses restrained Aurora glass, glow and policy accents instead of flat grey panels",
);
check(
  componentSource.includes("--ap-panel-surface: color-mix(")
    && !/--ap-panel-surface:\s*#[0-9a-f]+/i.test(componentSource),
  "planner surface remains theme-derived rather than a standalone flat-grey fill",
);
check(
  componentSource.includes('.ap-band[data-whole-horizon="true"]')
    && componentSource.includes("var(--card-background-color, var(--ha-card-background)) 92%, var(--ap-muted) 8%"),
  "whole-horizon no-action state remains muted instead of a saturated policy row",
);
check(
  assetsSource.includes("FRONTEND_ASSET_REVISION = 123")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 38")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 39")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 40")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 41")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 42")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 43")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 44")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 45")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 46")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 47")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 48")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 49")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 60")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 61")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 62")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 63")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 64")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 65")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 66")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 67")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 68")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 69")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 70")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 71")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 72")
    && !assetsSource.includes("FRONTEND_ASSET_REVISION = 73"),
  "execution-health fix-forward uses frontend revision 123",
);
check(
  canonicalExtensionSource.includes('range: "Zakres czasu"')
    && canonicalExtensionSource.includes('batteryPower: "Moc magazynu"')
    && canonicalExtensionSource.includes('balance: "Co się wydarzy"')
    && canonicalExtensionSource.includes('blocker: "Co może zablokować wykonanie?"'),
  "selected baseline interval leads with Kowalski-friendly labels and a translated blocker",
);
check(
  !/--ap-(?:rce|tariff|rcm|good|warn|error):\s*#[0-9a-f]+/i.test(componentSource),
  "planner has no standalone generic policy color system",
);
check(
  componentSource.includes("--ap-mobile-rail-min-width")
    && componentSource.includes("min-width: max(100%, var(--ap-mobile-rail-min-width"),
  "phone layout keeps chronology in the internal timeline viewport",
);
check(
  componentSource.includes("hoymilesPlannerFormatBandRange")
    && componentSource.includes('dataset.compact = String(')
    && componentSource.includes('.ap-band[data-compact="true"]'),
  "cross-day and compact action bands retain readable time/icon semantics",
);
check(
  componentSource.includes('dataset.compact = String(bandDurationMs <= 60 * 60_000)')
    && componentSource.includes('dataset.micro = String(bandDurationMs <= 30 * 60_000)'),
  "one-hour and half-hour action bands receive explicit narrow-layout density",
);
check(
  componentSource.includes('.ap-band[data-compact="true"] { overflow: hidden; }')
    && componentSource.includes('.ap-band[data-compact="true"] .ap-band-copy { display: none; }'),
  "narrow action bands clip decoration and keep verbose copy in the selected detail",
);
check(
  componentSource.includes('--mdc-icon-size: 16px;')
    && componentSource.includes('--mdc-icon-size: 14px;')
    && componentSource.includes('--mdc-icon-size: 12px;'),
  "timeline icons scale down with band density instead of escaping their tiles",
);
check(
  componentSource.includes('.ap-band { position: absolute;')
    && componentSource.includes('container-type: inline-size;')
    && componentSource.includes('@container (max-width: 13px)')
    && componentSource.includes('.ap-band-visual ha-icon { display: none; }'),
  "an action icon is hidden only when its rendered band is physically too narrow",
);
check(
  componentSource.includes("_socStepPath")
    && componentSource.includes("previous = null")
    && canonicalExtensionSource.includes("_normalizeBaselineTimeline(entity)")
    && canonicalExtensionSource.includes('value.timeline_kind !== "baseline_self_use"'),
  "frontend neither interpolates gaps nor invents a baseline outside the strict backend contract",
);
check(
  canonicalExtensionSource.includes("this._patchPlanPowerBars(model, baselinePoints)")
    && canonicalExtensionSource.includes("this._patchBaselineRawPointTable(model, baselinePoints)")
    && canonicalExtensionSource.includes('"baseline.points[].pv_kw"')
    && canonicalExtensionSource.includes('"baseline.points[].load_kw"')
    && canonicalExtensionSource.includes('record.row.dataset.sourcePolicy = "baseline"'),
  "PV/LOAD bars and raw table share only the exact backend baseline point array",
);
check(
  componentSource.includes('.ap-raw-table-viewport { max-height: 340px; overflow: auto;')
    && componentSource.includes('.ap-raw-table { width: 100%; min-width: 980px;'),
  "the complete raw point table remains bounded and independently scrollable on phone and desktop",
);
for (const width of [320, 360, 390, 768, 1024, 1366, 1920]) {
  fakeWindow.innerWidth = width;
  fakeDocument.documentElement.clientWidth = width;
  fakeDocument.documentElement.scrollWidth = width;
  check(fakeDocument.documentElement.scrollWidth <= fakeDocument.documentElement.clientWidth, `viewport ${width} has no page overflow`);
}

const gesturePoint = [...card._slotNodes.values()][1];
const beforeGestureSelection = card._selectedKey;
gesturePoint.dispatch("pointerdown", { clientX: 10, clientY: 10 });
gesturePoint.dispatch("pointermove", { clientX: 11, clientY: 24 });
gesturePoint.dispatch("pointerup", { clientX: 11, clientY: 24 });
check(card._selectedKey === beforeGestureSelection, "vertical gesture over 8px does not select a point");

const tariffPathNode = refs.socSeries.tariff;
const rcmPathNode = refs.socSeries.rcm;
const rcePathNode = refs.socSeries.rce;
const expectedPathNode = refs.socExpected;
const tariffPathBefore = tariffPathNode.getAttribute("d");
const rcmPathBefore = rcmPathNode.getAttribute("d");
const rcePathBefore = rcePathNode.getAttribute("d");
const expectedPathBefore = expectedPathNode.getAttribute("d");
const loadSummaryBefore = refs.summaryValues.lowest.value.textContent;
const tariffBandBefore = [...card._bandNodes.values()].find(
  (record) => record.element.dataset.policy === "tariff",
);
const rcmBandBefore = [...card._bandNodes.values()].find(
  (record) => record.element.dataset.policy === "rcm",
);
const pathPatchesBefore = card._metrics.planPathPatchCount;
const updatedPointKey = card._lastModel.baselineTimeline.points[0].key;
const updatedPowerBars = card._powerBarNodes.get(updatedPointKey);
const updatedRawRow = card._rawPointRows.get(updatedPointKey);
const visualUpdate = clone(genuine);
visualUpdate.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.points[0].soc_percent = 69;
visualUpdate.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.points[0].action_code = "idle";
visualUpdate.states[EXPECTED_BINDINGS.rce_timeline_entity].attributes.plan_revision = 7;
visualUpdate.states[EXPECTED_BINDINGS.supervisor_entity].attributes.candidate_summaries
  .find((item) => item.policy_id === "rce").candidate_revision = 7;
visualUpdate.states[EXPECTED_BINDINGS.supervisor_entity].attributes.selected_candidate_revision = 7;
const visualCanonical =
  visualUpdate.states[EXPECTED_BINDINGS.canonical_timeline_entity].attributes;
const visualSlot = visualCanonical.slots[0];
Object.assign(visualSlot.planned, {
  pv_kwh: 0.8125,
  load_kwh: 0.275,
  battery_kwh: -0.3,
  grid_kwh_import_positive: -0.8375,
});
visualSlot.soc_equation.battery_to_grid_kwh = 0.3;
visualSlot.soc_equation.soc_end_percent = 72;
visualSlot.protected_reserve.margin_end_percent = 47;
for (const slot of visualCanonical.slots.slice(1)) {
  slot.soc_equation.soc_start_percent -= 0.5;
  slot.soc_equation.soc_end_percent -= 0.5;
  slot.protected_reserve.margin_end_percent -= 0.5;
}
visualCanonical.final_soc_percent -= 0.5;
visualCanonical.ledger_revision = "d".repeat(64);
ledger.reset();
card.hass = visualUpdate;
check(flushAnimationFrames() === 1, "one genuine SOC/action update uses one visual frame");
check(
  card._metrics.planPathPatchCount === pathPatchesBefore
    && card._pendingPlanModel?.canonicalTimeline?.ledgerRevision === "d".repeat(64)
    && rcePathNode.getAttribute("d") === rcePathBefore
    && expectedPathNode.getAttribute("d") === expectedPathBefore
    && tariffPathNode.getAttribute("d") === tariffPathBefore
    && rcmPathNode.getAttribute("d") === rcmPathBefore,
  "latest RCE/canonical semantic update stays pending without repainting paths inside 180 seconds",
);
refs.refresh.dispatch("click");
check(
  card._metrics.planPathPatchCount === pathPatchesBefore + 1
    && card._pendingPlanModel === null
    && rcePathNode.getAttribute("d") !== rcePathBefore
    && expectedPathNode.getAttribute("d") !== expectedPathBefore
    && tariffPathNode.getAttribute("d") === tariffPathBefore
    && rcmPathNode.getAttribute("d") === rcmPathBefore,
  "manual refresh applies separate RCE comparison and canonical EMS-overlay paths",
);
check(
  card._bandNodes.size === 2
    && [...card._bandNodes.values()].every(
      (record) => record.element.dataset.sourcePolicy === "canonical"
        && walk(record.element).filter((node) => node.tagName === "HA-ICON").length === 1,
    )
    && card._pointNodes.size === 0,
  "semantic refresh retains two canonical action bands without redundant point icons",
);
check(
  refs.socSeries.rce === rcePathNode
    && refs.socSeries.tariff === tariffPathNode
    && refs.socSeries.rcm === rcmPathNode
    && refs.socExpected === expectedPathNode,
  "genuine update preserves every stable SVG path node",
);
check(
  card._powerBarNodes.get(updatedPointKey)?.pv === updatedPowerBars.pv
    && card._powerBarNodes.get(updatedPointKey)?.load === updatedPowerBars.load
    && updatedPowerBars.pv.dataset.rawValue === "0.8"
    && updatedPowerBars.load.dataset.rawValue === "0.7"
    && card._rawPointRows.get(updatedPointKey)?.row === updatedRawRow.row
    && updatedRawRow.cells.pv.textContent === "0.8"
    && updatedRawRow.cells.load.textContent === "0.7"
    && updatedRawRow.cells.soc.textContent === "72.5",
  "canonical update cannot mutate baseline PV, LOAD or SOC rows and preserves their keyed DOM",
);
check(
  refs.summaryValues.lowest.label.textContent === "Zużycie domu"
    && refs.summaryValues.lowest.value.textContent === loadSummaryBefore
    && refs.summaryValues.lowest.value.textContent.includes("kWh"),
  "canonical trajectory update leaves the baseline-owned home-consumption summary unchanged",
);
check(
  fakeWindow.scrollY === 950
    && refs.viewport.scrollLeft === 240
    && ledger.scrollCalls === 0
    && ledger.focusCalls === 0,
  "genuine band/path patch causes no page jump or background focus",
);

testOperationOutcomes().then(() => {
  console.log(`Aurora automation planner UI contract: PASS (${checks} checks)`);
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
