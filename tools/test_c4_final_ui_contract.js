const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(process.env.HOYMILES_UI_TEST_ROOT || process.cwd());
const cardPath = "home_assistant/www/hoymiles-rce-chart-card.js";
const dashboardPath = "dashboard_hoymiles.yaml";
const cardSource = fs.readFileSync(path.join(root, cardPath), "utf8");
const dashboardSource = fs.readFileSync(path.join(root, dashboardPath), "utf8").replaceAll("\r", "");

let checks = 0;
const check = (condition, message) => {
  checks += 1;
  if (!condition) throw new Error(message);
};
const equal = (actual, expected, message) => {
  check(
    JSON.stringify(actual) === JSON.stringify(expected),
    `${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
  );
};

const ledger = {
  childListWrites: 0,
  focusCalls: 0,
  scrollCalls: 0,
  reset() {
    this.childListWrites = 0;
    this.focusCalls = 0;
    this.scrollCalls = 0;
  },
};

class FakeStyle {
  constructor() { this.values = new Map(); }
  setProperty(name, value) { this.values.set(name, String(value)); }
  removeProperty(name) { this.values.delete(name); }
  getPropertyValue(name) { return this.values.get(name) || ""; }
}

class FakeClassList {
  constructor(owner) { this.owner = owner; }
  _set() { return new Set(String(this.owner.className || "").split(/\s+/).filter(Boolean)); }
  _write(values) { this.owner.className = [...values].join(" "); }
  add(...names) { const values = this._set(); names.forEach((name) => values.add(name)); this._write(values); }
  remove(...names) { const values = this._set(); names.forEach((name) => values.delete(name)); this._write(values); }
  toggle(name, force) {
    const values = this._set();
    const enabled = force === undefined ? !values.has(name) : Boolean(force);
    if (enabled) values.add(name); else values.delete(name);
    this._write(values);
    return enabled;
  }
  contains(name) { return this._set().has(name); }
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
    this.className = "";
    this._textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.value = "";
    this.type = "";
    this.selected = false;
    this.scrollLeft = 0;
    this.scrollTop = 0;
    this.dataset = {};
    this.classList = new FakeClassList(this);
  }
  get parentElement() { return this.parentNode; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get options() { return this.children.filter((child) => child.tagName === "OPTION"); }
  set textContent(value) {
    this._textContent = String(value ?? "");
    if (this.children.length) {
      this.children.forEach((child) => { child.parentNode = null; });
      this.children = [];
      ledger.childListWrites += 1;
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
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    ledger.childListWrites += 1;
    this.append(...children);
  }
  insertBefore(child, reference) {
    if (child.parentNode) child.parentNode.removeChild(child);
    const index = reference ? this.children.indexOf(reference) : -1;
    child.parentNode = this;
    if (index < 0) this.children.push(child); else this.children.splice(index, 0, child);
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
  remove() { this.parentNode?.removeChild(this); }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_all, character) => character.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) {
    this.attributes.delete(name);
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_all, character) => character.toUpperCase());
      delete this.dataset[key];
    }
  }
  toggleAttribute(name, force) {
    const enabled = force === undefined ? !this.hasAttribute(name) : Boolean(force);
    if (enabled) this.setAttribute(name, ""); else this.removeAttribute(name);
    return enabled;
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter((item) => item !== listener));
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
  click() { if (!this.disabled) return this.dispatch("click"); return false; }
  focus() { ledger.focusCalls += 1; fakeDocument.activeElement = this; }
  scrollIntoView() { ledger.scrollCalls += 1; }
  scroll() { ledger.scrollCalls += 1; }
  scrollTo() { ledger.scrollCalls += 1; }
  scrollBy() { ledger.scrollCalls += 1; }
  matches(selector) {
    if (selector.startsWith(".")) return this.classList.contains(selector.slice(1));
    if (selector.startsWith("#")) return (this.id || this.getAttribute("id")) === selector.slice(1);
    const dataMatch = selector.match(/^\[data-([a-z0-9-]+)(?:=["']?([^\]"']+)["']?)?\]$/i);
    if (dataMatch) {
      const key = dataMatch[1].replace(/-([a-z])/g, (_all, character) => character.toUpperCase());
      return dataMatch[2] === undefined
        ? Object.prototype.hasOwnProperty.call(this.dataset, key)
        : this.dataset[key] === dataMatch[2];
    }
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) {
    return walk(this).filter((node) => node !== this && node.matches?.(selector));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getBoundingClientRect() {
    return { x: 0, y: 0, top: 0, left: 0, right: 390, bottom: 44, width: 390, height: 44 };
  }
}

class FakeSvgElement extends FakeElement {}
class TestHTMLElement extends FakeElement {
  constructor() { super("hoymiles-test-host"); this.isConnected = true; }
  attachShadow() {
    if (!this.shadowRoot) this.shadowRoot = new FakeElement("shadow-root");
    return this.shadowRoot;
  }
  dispatchEvent() { return true; }
}

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

let nowMs = Date.parse("2026-09-01T08:00:00.000Z");
class FakeDate extends Date {
  constructor(...args) { super(...(args.length ? args : [nowMs])); }
  static now() { return nowMs; }
}
let timerId = 0;
const timers = new Map();
const fakeSetTimeout = (callback, delay = 0) => {
  const id = ++timerId;
  timers.set(id, { callback, due: nowMs + Math.max(0, Number(delay) || 0) });
  return id;
};
const fakeClearTimeout = (id) => timers.delete(id);
const tick = (milliseconds) => {
  const target = nowMs + milliseconds;
  while (true) {
    const next = [...timers.entries()]
      .filter(([, timer]) => timer.due <= target)
      .sort((left, right) => left[1].due - right[1].due || left[0] - right[0])[0];
    if (!next) break;
    timers.delete(next[0]);
    nowMs = next[1].due;
    next[1].callback();
  }
  nowMs = target;
};

let rafId = 0;
let rafQueue = new Map();
const requestAnimationFrame = (callback) => {
  const id = ++rafId;
  rafQueue.set(id, callback);
  return id;
};
const cancelAnimationFrame = (id) => rafQueue.delete(id);
const flushAnimationFrames = () => {
  const current = [...rafQueue.entries()];
  rafQueue = new Map();
  current.forEach(([, callback]) => callback(nowMs));
  return current.length;
};

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
  customStrategies: [],
  scrollY: 87,
  innerWidth: 390,
  innerHeight: 844,
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
};
const registry = new Map();
const context = {
  console,
  Date: FakeDate,
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
      if (registry.has(name)) throw new Error(`duplicate custom element: ${name}`);
      registry.set(name, constructor);
    },
    get(name) { return registry.get(name); },
    async whenDefined() {},
  },
  requestAnimationFrame,
  cancelAnimationFrame,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
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
  fetch: async () => ({ ok: true, json: async () => ({ views: [] }) }),
  WebSocket: class { constructor() { throw new Error("canonical must not open WebSocket"); } },
};
context.globalThis = context;

vm.runInNewContext(
  cardSource.replaceAll(
    "import.meta.url",
    JSON.stringify("https://homeassistant.example/local/hoymiles-rce-chart-card.js?v=i1-test"),
  ),
  context,
  { filename: cardPath },
);

const CANONICAL_ELEMENTS = [
  "hoymiles-automation-planner-card",
  "hoymiles-aurora-disclosure-card",
  "hoymiles-aurora-ems-card",
  "hoymiles-aurora-overview-card",
  "hoymiles-aurora-variant-a-ems-page-card",
  "hoymiles-aurora-variant-a-settings-page-card",
  "hoymiles-ems-shared-inputs-card",
  "hoymiles-ems-supervisor-card",
];
for (const elementName of CANONICAL_ELEMENTS) {
  check(
    registry.has(elementName),
    `canonical resource registers ${elementName}`,
  );
  check(
    fakeWindow.customCards.filter((card) => card.type === elementName).length === 1,
    `canonical resource advertises ${elementName} exactly once`,
  );
}
check(
  ![...registry.keys()].some((name) => name.includes("c4-dev"))
    && !fakeWindow.customCards.some((card) => card.type.includes("c4-dev")),
  "canonical resource exposes no DEV custom element or card",
);

const disclosureSource = cardSource.slice(
  cardSource.indexOf("class HoymilesAuroraDisclosureCard"),
  cardSource.indexOf("class HoymilesAuroraStatusCard"),
);
check(disclosureSource.length > 1000, "Aurora disclosure has one canonical implementation");
for (const forbiddenCapability of ["fetch(", "WebSocket", "setTimeout", "setInterval", "callService"]) {
  check(
    !disclosureSource.includes(forbiddenCapability),
    `Aurora disclosure never uses ${forbiddenCapability}`,
  );
}
check(
  disclosureSource.includes('document.createElement("button")') &&
    disclosureSource.includes('button.type = "button"') &&
    disclosureSource.includes('button.setAttribute("aria-controls"') &&
    disclosureSource.includes('setAttribute("aria-expanded"') &&
    disclosureSource.includes("min-height: 54px"),
  "Aurora disclosure uses a native 54 px button with an explicit ARIA relationship and expanded state",
);
check(
  disclosureSource.includes('this._expanded = false') &&
    disclosureSource.includes('this._expanded = config.open === true'),
  "Aurora disclosure is collapsed by default and supports an explicit initial open state",
);
check(
  disclosureSource.includes("if (this._card) this._card.hass = hass") &&
    !disclosureSource.slice(
      disclosureSource.indexOf("set hass(hass)"),
      disclosureSource.indexOf("connectedCallback()"),
    ).includes("createCardElement"),
  "HA state updates patch the existing disclosure child without rebuilding it",
);

const forbiddenSimpleTerm = /\b(?:shadow|active|inactive|off)\b/i;
for (const language of ["pl", "en"]) {
  for (const supervisorState of [
    "off",
    "active_idle",
    "selected",
    "starting",
    "waiting_readback",
    "executing",
    "stopping",
    "restoring",
    "blocked",
    "fault",
    "unavailable",
  ]) {
    const text = vm.runInContext(
      `hoymilesC4PlannerSupervisorText(${JSON.stringify(language)}, ${JSON.stringify(supervisorState)}, "RCE")`,
      context,
    );
    check(
      !forbiddenSimpleTerm.test(text),
      `${language} planner hero maps ${supervisorState} to natural wording`,
    );
  }
}
for (const copyName of [
  "HOYMILES_C4_PLANNER_COPY",
  "HOYMILES_SUPERVISOR_C4_COPY",
  "HOYMILES_AURORA_EMS_C4_COPY",
  "HOYMILES_EMS_SHARED_INPUTS_C4_COPY",
]) {
  const copy = JSON.parse(vm.runInContext(`JSON.stringify(${copyName})`, context));
  const collectStrings = (value) => {
    if (typeof value === "string") return [value];
    if (!value || typeof value !== "object") return [];
    return Object.values(value).flatMap(collectStrings);
  };
  const renderedCopy = collectStrings(copy).join("\n");
  check(
    !forbiddenSimpleTerm.test(renderedCopy),
    `${copyName} contains no raw simple-UI Shadow/Active/Inactive/Off wording`,
  );
}

check(
  dashboardSource.includes("title: Hoymiles Energy Storage\n"),
  "canonical dashboard keeps the production title",
);
for (const elementName of [
  "hoymiles-aurora-disclosure-card",
  "hoymiles-aurora-overview-card",
  "hoymiles-aurora-variant-a-ems-page-card",
  "hoymiles-aurora-variant-a-settings-page-card",
  "hoymiles-ems-supervisor-card",
]) {
  check(dashboardSource.includes(`custom:${elementName}`), `canonical dashboard uses ${elementName}`);
}
check(
  (dashboardSource.match(/custom:hoymiles-aurora-variant-a-ems-page-card/g) || []).length === 1
    && (dashboardSource.match(/custom:hoymiles-automation-planner-card/g) || []).length === 0
    && !dashboardSource.includes("c4-dev"),
  "canonical dashboard binds one Variant A EMS composition and no legacy planner or DEV element",
);
check(
  !dashboardSource.includes("Dobowe zużycie użyte przez algorytmy") &&
    !dashboardSource.includes("Daily home use used by the algorithms"),
  "accidental daily-consumption card is absent from canonical dashboard",
);
check(
  !/\b(?:Active|Inactive|Off|Shadow)\b|Tryb obserwacyjny|Tylko obserwacja|Plan tylko podglądowy/.test(dashboardSource),
  "canonical dashboard copy contains no raw execution-mode or observation wording",
);
check(
  dashboardSource.includes("Wyłączona w EMS") &&
    !dashboardSource.includes("Nadzorca") &&
    !dashboardSource.includes("- entity: sensor.hoymiles_rcem_status\n            name: Stan automatyki"),
  "RCEm simple settings use concise EMS wording without the legacy Polish Supervisor name",
);
check(
  (dashboardSource.match(/- title: Ustawienia EMS\n/g) || []).length === 1 &&
    dashboardSource.includes("path: ustawienia-ems"),
  "canonical dashboard has exactly one isolated EMS settings view",
);
check(
  (dashboardSource.match(/type: custom:hoymiles-aurora-variant-a-settings-page-card/g) || []).length === 1 &&
    (dashboardSource.match(/type: custom:hoymiles-ems-shared-inputs-card\n\s+mode: full/g) || []).length === 0 &&
    (dashboardSource.match(/type: custom:hoymiles-ems-shared-inputs-card\n\s+mode: summary/g) || []).length === 0,
  "canonical dashboard uses one row-based settings composition and removes repeated shared-input summaries",
);
for (const legacyEditableEntity of [
  "input_select.hoymiles_rce_inverter_rated_power",
  "input_text.hoymiles_solcast_forecast_today_entity",
  "input_text.hoymiles_solcast_forecast_tomorrow_entity",
  "input_text.hoymiles_solcast_forecast_day_3_entity",
  "input_number.hoymiles_rce_fallback_daily_load",
]) {
  check(
    !dashboardSource.includes(`- entity: ${legacyEditableEntity}`),
    `canonical policy views do not repeat editable legacy field ${legacyEditableEntity}`,
  );
}
for (const sharedHelperEntity of [
  "input_select.hoymiles_ems_inverter_rated_power_each",
  "input_text.hoymiles_ems_pv_forecast_today_entity",
  "input_text.hoymiles_ems_pv_forecast_tomorrow_entity",
  "input_text.hoymiles_ems_pv_forecast_day_3_entity",
  "input_number.hoymiles_ems_fallback_daily_home_load",
]) {
  check(
    (dashboardSource.match(new RegExp(sharedHelperEntity.replaceAll(".", "\\."), "g")) || []).length === 1,
    `shared helper ${sharedHelperEntity} is configured only in the full settings view`,
  );
}
check(
  cardSource.includes('data-canonical-current="false"') &&
    cardSource.includes("waitingForSafePlan") &&
    cardSource.includes('data-baseline-available="false"') &&
    cardSource.includes("baseline.current_actual_soc_percent"),
  "C4 planner gates canonical overlays separately from backend baseline data",
);
check(
  cardSource.includes("model.baselineTimeline.generatedAt"),
  "physical SOC point uses the backend baseline capture timestamp instead of a frontend timestamp",
);
check(
  !cardSource.includes(
    '.ap-shell[data-canonical-current="false"] .ap-plan-power-bars',
  ) &&
    cardSource.includes(
      '.ap-shell[data-baseline-available="false"] .ap-plan-power-bars',
    ),
  "canonical availability never hides backend baseline PV/LOAD bars",
);
check(
  !cardSource.includes(
    '.ap-shell[data-canonical-current="false"] .ap-c4-slot-layer',
  ) &&
    !cardSource.includes(
      '.ap-shell[data-canonical-current="false"] .ap-c4-selection-marker',
    ) &&
    cardSource.includes('.ap-plan-power-bar { pointer-events: auto; }'),
  "baseline slot targets and bar controls remain interactive without canonical",
);
check(
  !cardSource.includes(
    '.ap-shell[data-canonical-current="false"] .ap-soc-series[data-policy]',
  ) &&
    !cardSource.includes(
      '.ap-shell[data-canonical-current="false"] .ap-soc-target',
    ) &&
    !cardSource.includes(
      '.ap-shell[data-canonical-current="false"] .ap-action-heading',
    ) &&
    !cardSource.includes(
      '.ap-shell[data-canonical-current="false"] .ap-lane',
    ),
  "canonical unavailable keeps independent RCE/tariff/RCEm comparison layers policy-local",
);
for (const canonicalLegend of ["expected", "target"]) {
  check(
    cardSource.includes(
      `.ap-shell[data-canonical-current="false"] .ap-line-legend-item[data-line="${canonicalLegend}"]`,
    ),
    `canonical unavailable immediately gates stale ${canonicalLegend} legend content`,
  );
}
check(
  !cardSource.includes(
    '.ap-shell[data-canonical-current="false"] .ap-line-legend-item[data-line="reserve"]',
  ) &&
    cardSource.includes(
      '.ap-shell[data-baseline-reserve-available="false"] .ap-line-legend-item[data-line="reserve"]',
    ),
  "emergency-reserve legend follows the shared baseline readback, not canonical availability",
);
check(
  cardSource.includes(
    '.ap-shell[data-baseline-soc-available="false"] .ap-soc-baseline',
  ) &&
    cardSource.includes(
      '.ap-shell[data-baseline-pv-available="false"] .ap-plan-power-bar[data-series="pv"]',
    ) &&
    cardSource.includes(
      '.ap-shell[data-baseline-load-available="false"] .ap-plan-power-bar[data-series="load"]',
    ) &&
    cardSource.includes(
      '.ap-shell[data-baseline-current-soc-available="false"] .ap-current-soc',
    ) &&
    cardSource.includes(
      '.ap-shell[data-baseline-current-soc-available="false"] .ap-soc-current-point',
    ),
  "deferred plan/live geometry is immediately fail-closed independently for baseline SOC, physical SOC, PV, and LOAD",
);
for (const baselineOnlySurface of [
  ".ap-raw-points",
  ".ap-c4-slot-layer",
  ".ap-c4-lane-layer",
  ".ap-c4-selection-marker",
  ".ap-power-scale",
  ".ap-selected-detail",
]) {
  check(
    cardSource.includes(
      `.ap-shell[data-baseline-available="false"] ${baselineOnlySurface}`,
    ),
    `baseline unavailable hides stale ${baselineOnlySurface} without gating canonical geometry`,
  );
}
check(
  cardSource.includes("_reconcileCanonicalLane(policyId, model)") &&
    !cardSource.includes("_balanceText(point, language)"),
  "action lanes use canonical slots when available and inspector does not synthesize flow narratives",
);
check(
  dashboardSource.includes(
    "baseline_timeline_entity: sensor.hoymiles_ems_baseline_energy_timeline",
  ) &&
    cardSource.includes(
      '"sensor.hoymiles_ems_baseline_energy_timeline"',
    ),
  "canonical planner binds the exact backend baseline timeline entity",
);
check(
  cardSource.includes(
    '"Prognoza bazowa — autokonsumpcja.\\nPlan działań EMS jest chwilowo niedostępny."',
  ),
  "canonical-unavailable notice uses the exact corrected Polish copy",
);
check(
  ![
    "custom_components/hoymiles_hit_modbus/",
    "home_assistant/hoymiles_ems_scheduler.yaml",
    "packages/settings.yaml",
    "packages/parallel_network.yaml",
  ].some((marker) => cardSource.includes(marker)),
  "canonical JavaScript contains no backend, scheduler, or firmware source path",
);
check(cardSource.includes("ha-card { container-type: inline-size;"), "Aurora EMS card responds to its own rendered width");
check(cardSource.includes("@container (max-width: 470px)"), "narrow Aurora EMS columns switch to the mobile reading order");
check(cardSource.includes("overflow-wrap: anywhere; font-size: clamp(18px"), "long NOW/NEXT labels cannot overflow into the adjacent panel");

const Planner = registry.get("hoymiles-automation-planner-card");
const shellCard = new Planner();
shellCard.setConfig({});
const shellNode = shellCard.shadowRoot.children.find((node) => node.tagName === "HA-CARD");
check(shellNode, "planner mounts one persistent ha-card");
check(byClass(shellCard.shadowRoot, "ap-model-inputs").length === 0, "model-input section is absent at runtime");
check(shellCard._metrics.fullRenderCount === 1, "first mount is counted exactly once");
const c4Controls = shellCard._refs.timelineHeading.querySelector(".ap-c4-controls");
check(
  shellCard._refs.compare.parentNode === null &&
    c4Controls?.parentNode === shellCard._refs.timelineHeading &&
    c4Controls.querySelector(".ap-c4-compare")?.querySelector(".ap-compare-controls") === shellCard._refs.compareButtons.rce.parentNode &&
    c4Controls.querySelector(".ap-c4-refresh"),
  "C4 puts direct RCE, tariff and RCEm comparison controls with refresh in the timeline header, not in a disclosure",
);
check(
  shellCard._refs.powerLegendItems.pv.label.textContent === "Prognoza PV" &&
    shellCard._refs.rawPoints.querySelector(".ap-raw-points-help").textContent.includes("bez interpolacji i syntezy"),
  "planner labels backend baseline series and its no-synthesis inspector explicitly",
);
for (const metric of ["fullRenderCount", "livePointPatchCount", "executionPatchCount", "planPathPatchCount"]) {
  check(Number.isInteger(shellCard._metrics[metric]), `${metric} instrumentation is present`);
}
equal(
  [
    shellCard._refs.shell.getAttribute("data-c4-full-render-count"),
    shellCard._refs.shell.getAttribute("data-c4-live-point-patch-count"),
    shellCard._refs.shell.getAttribute("data-c4-execution-patch-count"),
    shellCard._refs.shell.getAttribute("data-c4-plan-path-patch-count"),
  ],
  ["1", "0", "0", "0"],
  "live DOM exposes the four C4 render counters without rebuilding the card",
);
ledger.reset();
shellCard._ensureShell();
shellCard._ensureShell();
check(
  shellCard.shadowRoot.children.find((node) => node.tagName === "HA-CARD") === shellNode,
  "repeated updates preserve ha-card identity",
);
check(shellCard._metrics.fullRenderCount === 1, "no full render occurs after mount");
check(ledger.focusCalls === 0 && ledger.scrollCalls === 0, "mount maintenance never focuses or scrolls");

const SharedInputs = registry.get("hoymiles-ems-shared-inputs-card");
const fullSharedConfig = {
  mode: "full",
  shared_inputs_entity: "sensor.hoymiles_hit_ems_shared_inputs",
  inverter_power_helper_entity: "input_select.hoymiles_ems_inverter_rated_power_each",
  forecast_today_helper_entity: "input_text.hoymiles_ems_pv_forecast_today_entity",
  forecast_tomorrow_helper_entity: "input_text.hoymiles_ems_pv_forecast_tomorrow_entity",
  forecast_day_3_helper_entity: "input_text.hoymiles_ems_pv_forecast_day_3_entity",
  fallback_load_helper_entity: "input_number.hoymiles_ems_fallback_daily_home_load",
};
let missingFullBindingRejected = false;
try {
  new SharedInputs().setConfig({
    mode: "full",
    shared_inputs_entity: "sensor.hoymiles_hit_ems_shared_inputs",
  });
} catch (_error) {
  missingFullBindingRejected = true;
}
check(missingFullBindingRejected, "full shared-input card rejects implicit helper discovery");

const sharedStates = {
  "sensor.hoymiles_hit_ems_shared_inputs": {
    state: "ready",
    attributes: {
      schema_version: 1,
      config_entry_id: "entry-a0",
      inverter_model: "HIT-10L-G3",
      inverter_model_source: "source_device",
      inverter_rated_power_each_kw: 10,
      inverter_rated_power_source: "configured_shared_helper",
      inverter_count: 1,
      inverter_count_source: "physical_fc03",
      system_rated_power_kw: 10,
      battery_capacity_kwh: 26,
      battery_capacity_source: "physical_capacity_register",
      bms_ready: true,
      forecast_today_entity: "sensor.fixture_forecast_today",
      forecast_tomorrow_entity: "sensor.fixture_forecast_tomorrow",
      forecast_day_3_entity: null,
      forecast_remaining_today_entity: "sensor.fixture_forecast_remaining",
      forecast_today_ready: true,
      forecast_tomorrow_ready: true,
      forecast_day_3_ready: false,
      forecast_remaining_today_ready: true,
      forecast_last_updated: "2026-09-01T07:59:30Z",
      forecast_quality: "complete",
      forecast_provenance: "mixed_sources",
      load_model_ready: true,
      load_model_source: "recorder_phase_energy_counters",
      load_history_days: 9,
      average_daily_home_load_kwh: 12.4,
      weekday_profile_ready: true,
      weekend_profile_ready: true,
      fallback_currently_used: false,
      physical_power_ready: true,
      pv_power_entity: "sensor.entry_a0_pv_power",
      home_load_power_entity: "sensor.entry_a0_home_load_power",
      system_load_power_entity: "sensor.entry_a0_system_load_power",
      battery_power_entity: "sensor.entry_a0_battery_power",
      grid_power_entity: "sensor.entry_a0_grid_power",
      grid_to_battery_power_entity: "sensor.hoymiles_hit_grid_to_battery_power",
      grid_to_battery_ready: false,
      grid_to_battery_quality: "providerless",
      migration_version: 1,
      migration_result: "not_required",
      legacy_aliases: ["input_text.hoymiles_solcast_forecast_today_entity"],
    },
  },
  "sensor.fixture_forecast_today": { state: "17.2", attributes: {} },
  "sensor.fixture_forecast_tomorrow": { state: "19.8", attributes: {} },
  "sensor.fixture_forecast_remaining": { state: "8.4", attributes: {} },
  "input_select.hoymiles_ems_inverter_rated_power_each": {
    state: "Auto",
    attributes: { options: ["Auto", "5 kW", "8 kW", "10 kW", "12 kW", "15 kW", "20 kW"] },
  },
  "input_text.hoymiles_ems_pv_forecast_today_entity": { state: "", attributes: {} },
  "input_text.hoymiles_ems_pv_forecast_tomorrow_entity": { state: "", attributes: {} },
  "input_text.hoymiles_ems_pv_forecast_day_3_entity": { state: "", attributes: {} },
  "input_number.hoymiles_ems_fallback_daily_home_load": { state: "12", attributes: {} },
};
const fullSharedCard = new SharedInputs();
fullSharedCard.setConfig(fullSharedConfig);
const fullSharedShell = fullSharedCard._refs.shell;
ledger.reset();
fullSharedCard.hass = { language: "pl", states: sharedStates, async callService() {} };
check(fullSharedCard._refs.shell === fullSharedShell, "shared settings update preserves its mounted shell");
check(ledger.childListWrites <= 8, "shared settings patches bounded text/options without rebuilding sections");
check(fullSharedShell.dataset.entryId === "entry-a0", "shared settings exposes the exact broker entry identity");
check(
  fullSharedCard._refs.readiness.gridBattery.text.textContent.includes("Niezweryfikowane") &&
    fullSharedCard._refs.readiness.gridBattery.row.dataset.ready === "informational",
  "providerless Grid-to-battery signal is explicit diagnostic information and never presented as verified accounting authority",
);
check(
  fullSharedCard.shadowRoot.textContent.includes("Model zużycia domu") &&
    fullSharedCard.shadowRoot.textContent.includes("Gotowość wspólnych danych") &&
    fullSharedCard.shadowRoot.textContent.includes("Wspólne ustawienie EMS") &&
    fullSharedCard.shadowRoot.textContent.includes("Połączone źródła prognozy") &&
    fullSharedCard.shadowRoot.textContent.includes("Pełne dane"),
  "shared settings translates singular helper, mixed forecast provenance, quality and natural Polish headings",
);
check(
  !fullSharedCard._refs.fallbackWrapper.hidden &&
    fullSharedCard._refs.fallbackHelp.textContent.includes("Nie jest obecnie używana"),
  "fallback setting remains discoverable and clearly says that healthy load history is active",
);
fullSharedCard._refs.expert.open = true;
fullSharedCard._patchFallbackVisibility();
check(!fullSharedCard._refs.fallbackWrapper.hidden, "opening expert settings does not duplicate or hide the fallback editor");
check(
  fullSharedCard._refs.controls.inverterPower.input.options.length === 7 &&
    fullSharedCard._refs.controls.inverterPower.input.options[0].value === "Auto",
  "manual per-inverter override presents Auto and the six supported ratings",
);
fullSharedCard.hass = { language: "en", states: sharedStates, async callService() {} };
check(
  fullSharedCard.shadowRoot.textContent.includes("Home-load model") &&
    fullSharedCard.shadowRoot.textContent.includes("Shared data readiness") &&
    fullSharedCard.shadowRoot.textContent.includes("Shared EMS setting") &&
    fullSharedCard.shadowRoot.textContent.includes("Combined forecast sources") &&
    fullSharedCard.shadowRoot.textContent.includes("Complete data"),
  "shared settings translates the same provenance and quality contract in English",
);

const summarySharedCard = new SharedInputs();
summarySharedCard.setConfig({
  mode: "summary",
  shared_inputs_entity: "sensor.hoymiles_hit_ems_shared_inputs",
  settings_path: "ustawienia-ems",
});
summarySharedCard.hass = { language: "pl", states: sharedStates, async callService() {} };
check(
  summarySharedCard.shadowRoot.textContent.includes("9 dni") &&
    summarySharedCard.shadowRoot.textContent.includes("10 kW"),
  "compact policy summary reports shared load history and system power",
);
check(
  !summarySharedCard.shadowRoot.textContent.includes("input_text.") &&
    Object.keys(summarySharedCard._config).every((key) => !key.endsWith("helper_entity")),
  "compact summary neither renders nor binds helper entity IDs",
);

const nestedSample = (
  value,
  entityId = null,
  fresh = true,
  provenance = "entry_local_registry",
) => ({
  value,
  entity_id: entityId,
  source_entity_ids: entityId ? [entityId] : [],
  selector_entity_id: null,
  reported_at: fresh ? "2026-09-01T07:59:30Z" : null,
  age_seconds: fresh ? 30 : null,
  fresh,
  reason: fresh ? "current" : "provider_missing",
  quality: fresh ? "current" : "unavailable",
  provenance,
});
const nestedBrokerStates = {
  ...sharedStates,
  "sensor.hoymiles_hit_ems_shared_inputs": {
    state: "partial",
    attributes: {
      schema_version: 1,
      config_entry_id: "entry-nested-a0",
      system: {
        inverter_rated_power_each_kw: nestedSample(10, null, true, "device_model"),
        inverter_count: nestedSample(1, "sensor.entry_nested_count"),
        system_rated_power_kw: nestedSample(10, null, true, "derived_inverter_power_x_count"),
        battery_capacity_kwh: nestedSample(26, "sensor.entry_nested_capacity"),
      },
      forecast: {
        today: nestedSample(17.2, "sensor.fixture_forecast_today", true, "new_helper"),
        tomorrow: nestedSample(19.8, "sensor.fixture_forecast_tomorrow", true, "new_helper"),
        day3: nestedSample(null, null, false, "unavailable"),
      },
      load: {
        fallback_daily_home_load_kwh: nestedSample(12, "input_number.hoymiles_ems_fallback_daily_home_load"),
        average_daily_home_load_kwh: 12.4,
        daily_history_days: 9,
        weekday_profile_30m_kwh: Array(48).fill(0.25),
        weekend_profile_30m_kwh: Array(48).fill(0.24),
        weekday_profile_days: 5,
        weekend_profile_days: 4,
        ready: true,
        source: "rce_recorder_broker",
      },
      bms: {
        voltage_v: nestedSample(51.2, "sensor.entry_nested_voltage"),
        maximum_charge_current_a: nestedSample(100, "sensor.entry_nested_charge_current"),
        maximum_discharge_current_a: nestedSample(100, "sensor.entry_nested_discharge_current"),
        maximum_charge_power_kw: nestedSample(5.12, null, true, "derived_bms_limit"),
        maximum_discharge_power_kw: nestedSample(5.12, null, true, "derived_bms_limit"),
      },
      power: {
        pv_power_kw: nestedSample(3, "sensor.entry_nested_pv"),
        home_load_power_kw: nestedSample(1, null, true, "entry_local_exact_phase_sum"),
        system_load_power_kw: nestedSample(1.1, "sensor.entry_nested_system_load"),
        battery_power_kw: nestedSample(0.5, "sensor.entry_nested_battery"),
        grid_power_kw: nestedSample(-1.4, "sensor.entry_nested_grid"),
        grid_to_battery_power: nestedSample(null, null, false, "no_confirmed_repo_provider"),
        grid_to_battery_ready: false,
      },
      migration: { version: 1, result: "not_run" },
    },
  },
};
const nestedSummaryCard = new SharedInputs();
nestedSummaryCard.setConfig({
  mode: "summary",
  shared_inputs_entity: "sensor.hoymiles_hit_ems_shared_inputs",
});
nestedSummaryCard.hass = {
  language: "pl",
  states: nestedBrokerStates,
  async callService() {},
};
check(
  nestedSummaryCard._refs.shell.dataset.entryId === "entry-nested-a0" &&
    nestedSummaryCard.shadowRoot.textContent.includes("9 dni") &&
    nestedSummaryCard.shadowRoot.textContent.includes("10 kW"),
  "nested bounded broker schema remains a read-only UI fallback behind flat canonical fields",
);

const POLICIES = ["rce", "tariff", "rcm"];
const START = "2026-09-01T08:00:00.000Z";
const END = "2026-09-01T08:30:00.000Z";
const AXIS_END = "2026-09-01T09:00:00.000Z";
function planPoint(revision, serial = 0, overrides = {}) {
  return {
    key: `canonical|${START}|${END}|${revision}`,
    start: START,
    end: END,
    policyId: "tariff",
    sourcePolicy: "tariff",
    actionCode: "battery_charge",
    selectedAction: "tariff_battery_charge",
    selected: true,
    active: false,
    kind: "forecast_plan",
    pvKw: 3.26,
    loadKw: 1.02,
    batteryKw: 2.12,
    gridKw: 0,
    gridImportKw: 0,
    gridExportKw: 0,
    pvKwh: 1.63 + serial / 1000,
    loadKwh: 0.51,
    batteryKwh: 1.06,
    gridImportKwh: 0,
    gridExportKwh: 0,
    socStartPercent: 61.2,
    socEndPercent: 65.1,
    socPercent: 65.1,
    targetSocPercent: 70,
    protectedSocFloorPercent: 27,
    startEligibility: "eligible",
    quality: "current",
    inputRevision: revision,
    planRevision: revision,
    sourceRevision: revision,
    ...overrides,
  };
}
function baselinePoint(overrides = {}) {
  const point = {
    key: `baseline|${START}|${END}`,
    slotKey: `baseline|${START}|${END}`,
    sourcePolicy: "baseline",
    policyId: "none",
    start: START,
    end: END,
    kind: "baseline_self_use",
    pvKw: 3.257,
    loadKw: 1.019,
    batteryKw: 1.111,
    gridKw: null,
    gridImportKw: 0.123,
    gridExportKw: 0,
    socStartPercent: 61.234,
    socEndPercent: 64.567,
    socPercent: 64.567,
    baselineSocPercent: 64.567,
    protectedSocFloorPercent: 27.25,
    targetSocPercent: null,
    actionCode: "idle",
    selectedAction: null,
    selectedPolicy: "none",
    active: false,
    selected: false,
    quality: "current",
    blockerCode: null,
    source: { pv: "forecast", load: "profile", soc: "bms", capacity: "register" },
    provenance: { pv: "backend", load: "backend", battery: "backend", soc: "backend" },
    ...overrides,
  };
  if (
    Object.prototype.hasOwnProperty.call(overrides, "socEndPercent") &&
    !Object.prototype.hasOwnProperty.call(overrides, "socPercent")
  ) {
    point.socPercent = overrides.socEndPercent;
    point.baselineSocPercent = overrides.socEndPercent;
  }
  return point;
}
function model({
  revision = 1,
  serial = 0,
  actual = 0,
  unrelated = 0,
  age = 1,
  generatedAt = "2026-09-01T07:59:00.000Z",
  valid = true,
  executionState = "active_idle",
  owner = "none",
  physicalMode = "self_use",
  badge = "planned",
  safetyKey = "noAction",
  baselineValid = true,
  baselineState = "current",
  baselineOverrides = {},
  canonicalOverrides = {},
  executionOverrides = {},
  helpers = {},
  masterStopLatched = false,
} = {}) {
  const point = planPoint(revision, serial, canonicalOverrides);
  const points = valid ? [point] : [];
  const {
    currentActualSocPercent,
    ...baselinePointOverrides
  } = baselineOverrides;
  const baseline = baselinePoint(baselinePointOverrides);
  const baselinePoints = baselineValid ? [baseline] : [];
  return {
    serial,
    language: "pl",
    badge: masterStopLatched ? "blocked" : badge,
    badgeText: masterStopLatched ? "blocked" : badge,
    safety: {
      key: masterStopLatched ? "masterStop" : safetyKey,
      text: masterStopLatched ? "masterStop" : safetyKey,
    },
    physicalMode,
    hero: {
      physical: `physical-${physicalMode}`,
      supervisor: masterStopLatched
        ? "supervisor-master-stop"
        : `supervisor-${executionState}`,
      action: `action-${revision}`,
      time: `time-${revision}`,
      reason: `reason-${revision}`,
      effect: `effect-${revision}`,
    },
    planMeaning: `meaning-${revision}`,
    unrelated,
    generatedAt,
    sourceAgesSeconds: { canonical: age },
    execution: {
      enabled: true,
      state: executionState,
      selectedPolicy: null,
      selectedAction: null,
      eligibility: null,
      continuationEligibility: null,
      owner,
      conflict: false,
      phase: null,
      transactionId: null,
      command: null,
      readback: null,
      physicalConfirmation: null,
      blockedReason: null,
      masterStopLatched,
      ...executionOverrides,
    },
    live: {
      pvKw: actual,
      loadKw: 1.02,
      batteryKw: actual / 2,
      gridKw: 0,
      socPercent: 61.2 + actual / 100,
      socAgeSeconds: 1,
      socFresh: true,
      voltageL1: 230 + actual / 100,
      voltageL2: 231,
      voltageL3: 232,
      nowBucket: Math.floor(FakeDate.now() / 5_000),
    },
    force: {
      language: "pl",
      day: "2026-09-01",
      theme: "aurora-dark",
      helpers: "stable",
      selectedPolicy: null,
      blockingPlanContext: { conflict: false, state: null },
    },
    canonicalTimeline: {
      state: valid ? "current" : "unavailable",
      valid,
      ledgerRevision: revision,
      initialSocPercent: 61.2,
      finalSocPercent: valid ? 65.1 : null,
      points,
    },
    emergencyReservePercent:
      baselineValid &&
      Number.isFinite(baseline.protectedSocFloorPercent)
        ? baseline.protectedSocFloorPercent
        : null,
    baselineTimeline: {
      state: baselineValid ? baselineState : "unavailable",
      valid: baselineValid,
      generatedAt,
      quality: baselineValid ? "current" : "unavailable",
      blockerCode: baselineValid ? null : "baseline_unavailable",
      intervalMinutes: 30,
      currentActualSocPercent:
        currentActualSocPercent === undefined
          ? 61.234 + actual / 100
          : currentActualSocPercent,
      source: { producer: "ems_baseline" },
      provenance: { contract: "baseline_v1" },
      points: baselinePoints,
    },
    timelines: Object.fromEntries(POLICIES.map((policyId) => [policyId, {
      valid,
      state: valid ? "current" : "unavailable",
      quality: valid ? "current" : "unavailable",
      planRevision: revision,
      resultCurrent: valid,
      recalculationPending: false,
      points,
    }])),
    policies: Object.fromEntries(POLICIES.map((policyId) => [policyId, {
      policyId,
      points,
      enabled: true,
      active: false,
      activityMismatch: false,
      policyDisabled: false,
      status: "planned",
      statusText: `status-${revision}`,
      presentation: {
        action: `${policyId}-action-${revision}`,
        time: `${policyId}-time-${revision}`,
        reason: `${policyId}-reason-${revision}`,
        effect: `${policyId}-effect-${revision}`,
      },
    }])),
    axis: { start: START, end: AXIS_END },
    helpers,
    supervisor: { state: executionState, selectedPolicy: null },
    conflict: false,
  };
}

const baselineEntity = {
  state: "partial",
  attributes: {
    schema_version: "2.0",
    timeline_kind: "baseline_self_use",
    output_only: true,
    authority: false,
    current: false,
    generated_at: "2026-09-01T07:59:00+00:00",
    quality: "partial",
    blocker_code: "soc_missing",
    point_count: 1,
    interval_minutes: 30,
    current_actual_soc_percent: null,
    battery_sign_convention: "positive_charge",
    source: { producer: "ems_baseline", revision: null },
    provenance: { pv: "forecast", load: "learned_profile" },
    points: [{
      start: START.replace("Z", "+00:00"),
      end: END.replace("Z", "+00:00"),
      pv_kw: 3.257,
      load_kw: 1.019,
      battery_kw: null,
      grid_import_kw: 0.123,
      grid_export_kw: 0,
      soc_start_percent: null,
      soc_end_percent: null,
      reserve_soc_percent: 27.25,
      quality: "partial",
      blocker_code: "soc_missing",
      source: { pv: "forecast", load: "profile", soc: null, capacity: "register" },
      provenance: { pv: "backend", load: "backend", battery: null, soc: null },
    }],
  },
};
const validatorCard = new Planner();
validatorCard.setConfig({});
const partialBaseline = validatorCard._normalizeBaselineTimeline(baselineEntity);
check(
  partialBaseline.valid && partialBaseline.state === "partial",
  "partial baseline renders available series instead of rejecting the payload",
);
check(
  partialBaseline.points[0].pvKw === 3.257 &&
    partialBaseline.points[0].batteryKw === null &&
    partialBaseline.points[0].gridKw === null,
  "baseline normalizer preserves exact backend values and does not synthesize a net grid series",
);
const partialFirstSlot = validatorCard._normalizeBaselineTimeline({
  ...baselineEntity,
  attributes: {
    ...baselineEntity.attributes,
    points: [{
      ...baselineEntity.attributes.points[0],
      start: "2026-09-01T08:07:00+00:00",
      end: "2026-09-01T08:30:00+00:00",
    }],
  },
});
check(
  partialFirstSlot.valid &&
    partialFirstSlot.points[0].start === "2026-09-01T08:07:00.000Z",
  "baseline validator accepts the exact backend partial first interval without interpolation",
);
check(
  !validatorCard._normalizeBaselineTimeline({
    ...baselineEntity,
    attributes: { ...baselineEntity.attributes, provenance: "frontend_guess" },
  }).valid,
  "baseline validator rejects unbounded string provenance in place of the bounded map",
);
check(
  !validatorCard._normalizeBaselineTimeline({
    ...baselineEntity,
    attributes: {
      ...baselineEntity.attributes,
      points: [{ ...baselineEntity.attributes.points[0], synthetic_kw: 7 }],
    },
  }).valid,
  "baseline validator rejects extra point fields that could hide frontend synthesis",
);
check(
  !validatorCard._normalizeBaselineTimeline({
    ...baselineEntity,
    attributes: { ...baselineEntity.attributes, interval_minutes: "30" },
  }).valid,
  "baseline validator rejects coerced numeric header fields",
);

const gateCard = new Planner();
gateCard.setConfig({});
const gateShell = gateCard._refs.shell;
const unavailableModel = model({ valid: false });
gateCard._patchCanonicalAvailabilityGate(model({ valid: true }));
check(
  ["expected", "target", "reserve"].every(
    (key) => gateCard._refs.lineLegendItems[key].item.hidden === false,
  ),
  "current canonical legend DOM is visible before the unavailable transition",
);
gateCard._patchCanonicalAvailabilityGate(unavailableModel);
check(
  gateShell.dataset.canonicalCurrent === "false" &&
    gateShell.dataset.baselineAvailable === "true" &&
    gateShell.dataset.baselineSocAvailable === "true" &&
    gateShell.dataset.baselinePvAvailable === "true" &&
    gateShell.dataset.baselineLoadAvailable === "true" &&
    gateShell.dataset.baselineReserveAvailable === "true" &&
    gateShell.dataset.baselineCurrentSocAvailable === "true",
  "unavailable canonical is gated independently while baseline remains available",
);
check(
  gateCard._refs.availabilityNotice.hidden,
  "unavailable canonical stays diagnostic-only while the baseline remains available",
);
const unavailableBaselineModel = {
  ...unavailableModel,
  baselineTimeline: {
    ...unavailableModel.baselineTimeline,
    valid: false,
    points: [],
    currentActualSocPercent: null,
  },
  emergencyReservePercent: null,
};
gateCard._patchCanonicalAvailabilityGate(unavailableBaselineModel);
check(
  !gateCard._refs.availabilityNotice.hidden &&
    gateCard._refs.availabilityNotice.textContent ===
      "Prognoza bazowa EMS jest chwilowo niedostępna — nie pokazujemy zastępczych danych.",
  "only an unavailable baseline shows the compact Polish availability notice",
);
gateCard._patchCanonicalAvailabilityGate(unavailableModel);
check(
  ["expected", "target"].every(
    (key) => gateCard._refs.lineLegendItems[key].item.hidden === true,
  ) &&
    gateCard._refs.lineLegendItems.reserve.item.hidden === false &&
    gateCard._refs.powerLegendItems.pv.item.hidden === false &&
    gateCard._refs.powerLegendItems.load.item.hidden === false,
  "current→unavailable hides only canonical legends while the baseline reserve remains",
);

const scenarioCard = new Planner();
scenarioCard.setConfig({});
const scenarioShell = scenarioCard._refs.shell;
const renderScenario = (scenarioModel) => {
  scenarioCard._patchReferenceSoc(scenarioModel);
  for (const policyId of POLICIES) {
    scenarioCard._reconcileCanonicalLane(policyId, scenarioModel);
  }
  scenarioCard._patchCanonicalAvailabilityGate(scenarioModel);
  return {
    baselinePath: scenarioCard._refs.socBaseline.getAttribute("d") || "",
    canonicalPath: scenarioCard._refs.socExpected.getAttribute("d") || "",
    bars: [...scenarioCard._refs.planPowerBars.children],
    canonicalBands: [...scenarioCard._bandNodes.values()],
  };
};

const reserveContractModel = model({
  baselineOverrides: { protectedSocFloorPercent: 25 },
  canonicalOverrides: { protectedSocFloorPercent: 70 },
});
scenarioCard._patchReferenceSoc(reserveContractModel);
scenarioCard._patchSocPolicy("rce", reserveContractModel);
equal(
  scenarioCard._refs.socFloor.getAttribute("d"),
  scenarioCard._socTargetPath(
    reserveContractModel.baselineTimeline.points,
    reserveContractModel.axis.start,
    reserveContractModel.axis.end,
    "protectedSocFloorPercent",
  ),
  "main yellow reserve line uses the exact 25% shared Self-Use baseline",
);
equal(
  scenarioCard._refs.socTargets.rce.getAttribute("d"),
  scenarioCard._socTargetPath(
    reserveContractModel.timelines.rce.points,
    reserveContractModel.axis.start,
    reserveContractModel.axis.end,
    "protectedSocFloorPercent",
  ),
  "RCE detail overlay independently uses its 70% policy reserve",
);
check(
  scenarioCard._refs.socFloor.dataset.sourceField
    === "baseline.points[].reserve_soc_percent"
    && scenarioCard._refs.socTargets.rce.dataset.sourceField
      === "policy.points[].protected_soc_floor_percent"
    && cardSource.includes('protectedFloor: "Rezerwa awaryjna"')
    && cardSource.includes('policyOverlayRce: "Rezerwa planu RCE"')
    && !cardSource.includes('protectedFloor: "Minimalna rezerwa"'),
  "emergency reserve and policy thresholds have distinct provenance and labels",
);

// 1. Supervisor Off: backend baseline is output-only and remains visible.
let scenario = renderScenario(model({ executionState: "off" }));
check(
  scenario.baselinePath && scenario.bars.length === 2,
  "case 1 — Supervisor Off keeps backend baseline SOC, PV, and LOAD visible",
);

// 2. Active Supervisor with no owner: lack of a writer does not hide baseline.
scenario = renderScenario(model({ executionState: "active_idle", owner: "none" }));
check(
  scenario.baselinePath && scenarioShell.dataset.baselineAvailable === "true",
  "case 2 — Active Supervisor with owner none keeps baseline visible",
);

// 3. RCEm/canonical unavailable: canonical output is unavailable, but current
// RCE and tariff plans remain independently renderable.
const policyLocalModel = model({ valid: true, executionState: "unavailable" });
policyLocalModel.canonicalTimeline = {
  ...policyLocalModel.canonicalTimeline,
  state: "unavailable",
  valid: false,
  finalSocPercent: null,
  points: [],
};
policyLocalModel.timelines.rce = {
  valid: true,
  state: "current",
  quality: "complete",
  planRevision: 31,
  resultCurrent: true,
  recalculationPending: false,
  points: [planPoint(31, 0, {
    key: `rce|${START}|${END}|31`,
    policyId: "rce",
    sourcePolicy: "rce",
    actionCode: "export",
    selectedAction: null,
    quality: "complete",
  })],
};
policyLocalModel.timelines.tariff = {
  valid: true,
  state: "current",
  quality: "complete",
  planRevision: 32,
  resultCurrent: true,
  recalculationPending: false,
  points: [planPoint(32, 0, {
    key: `tariff|${START}|${END}|32`,
    policyId: "tariff",
    sourcePolicy: "tariff",
    quality: "complete",
  })],
};
policyLocalModel.timelines.rcm = {
  valid: false,
  state: "unavailable",
  quality: "unavailable",
  planRevision: 0,
  resultCurrent: false,
  recalculationPending: false,
  points: [],
};
scenario = renderScenario(policyLocalModel);
const policyLocalBandSources = scenario.canonicalBands.map(
  (record) => record.element.dataset.sourcePolicy,
);
check(
  scenario.baselinePath &&
    !scenario.canonicalPath &&
    scenarioShell.dataset.canonicalCurrent === "false" &&
    scenarioCard._refs.availabilityNotice.hidden &&
    policyLocalBandSources.includes("rce") &&
    policyLocalBandSources.includes("tariff") &&
    !policyLocalBandSources.includes("rcm") &&
    scenarioCard._refs.laneEmpty.rce.hidden &&
    scenarioCard._refs.laneEmpty.tariff.hidden &&
    !scenarioCard._refs.laneEmpty.rcm.hidden,
  "case 3 — RCEm/canonical unavailable keeps independent RCE and tariff plans while only RCEm stays unavailable without a global warning",
);

// 4. A blocked zero-export policy must not mutate the backend baseline series.
const zeroExportBaseline = model({
  valid: false,
  executionState: "blocked",
  baselineOverrides: { gridExportKw: 0 },
});
const beforeBlockedPath = scenario.baselinePath;
scenario = renderScenario(zeroExportBaseline);
check(
  scenario.baselinePath === beforeBlockedPath &&
    zeroExportBaseline.baselineTimeline.points[0].gridExportKw === 0,
  "case 4 — blocked zero-export RCE leaves the exact baseline unchanged",
);

// 5. Below reserve: backend explicitly supplies no discharge, grid deficit,
// and a later PV-rebuild interval. The frontend renders those points verbatim.
const belowReserveModel = model({ valid: false });
belowReserveModel.baselineTimeline.points = [
  baselinePoint({
    batteryKw: 0,
    gridImportKw: 0.9,
    gridExportKw: 0,
    pvKw: 0.4,
    loadKw: 1.3,
    socStartPercent: 20,
    socEndPercent: 20,
    protectedSocFloorPercent: 27,
  }),
  baselinePoint({
    key: `baseline|${END}|${AXIS_END}`,
    slotKey: `baseline|${END}|${AXIS_END}`,
    start: END,
    end: AXIS_END,
    batteryKw: 0.2,
    gridImportKw: 0,
    gridExportKw: 0,
    pvKw: 1.5,
    loadKw: 1.3,
    socStartPercent: 20,
    socEndPercent: 21,
    protectedSocFloorPercent: 27,
  }),
];
scenario = renderScenario(belowReserveModel);
check(
  belowReserveModel.baselineTimeline.points.every((point) => point.batteryKw >= 0) &&
    scenario.bars.length === 4 &&
    scenarioCard._rawPointRows.get(`baseline|${START}|${END}`).cells.gridImport.textContent === "0.9",
  "case 5 — below-reserve baseline shows no further discharge, exact grid deficit, and PV rebuild",
);

// 6. Full battery plus zero export: zero remains an explicit zero, not a
// derived or fabricated export value.
const fullZeroExportModel = model({
  valid: false,
  baselineOverrides: {
    batteryKw: 0,
    gridImportKw: 0,
    gridExportKw: 0,
    socStartPercent: 100,
    socEndPercent: 100,
    currentActualSocPercent: 100,
  },
});
scenario = renderScenario(fullZeroExportModel);
check(
  scenario.baselinePath &&
    scenarioCard._rawPointRows.get(`baseline|${START}|${END}`).cells.gridExport.textContent === "0" &&
    fullZeroExportModel.baselineTimeline.points[0].socEndPercent === 100,
  "case 6 — full zero-export baseline shows SOC 100% and no fake export",
);

// 7. unavailable -> current: patch canonical overlay in place, without reload,
// focus, or scroll.
ledger.reset();
renderScenario(model({ valid: false }));
const baselineBeforeRestore = scenarioCard._refs.socBaseline.getAttribute("d");
scenario = renderScenario(model({ valid: true, revision: 7 }));
check(
  scenarioCard._refs.shell === scenarioShell &&
    scenario.baselinePath === baselineBeforeRestore &&
    scenario.canonicalPath &&
    scenario.canonicalBands.length === 1 &&
    scenario.canonicalBands[0].element.dataset.sourcePolicy === "canonical" &&
    scenarioShell.dataset.canonicalCurrent === "true" &&
    ledger.focusCalls === 0 &&
    ledger.scrollCalls === 0,
  "case 7 — unavailable→current adds the canonical line without reload or scroll",
);

// 8. current -> unavailable: remove only canonical paths and selection; baseline
// remains the exact same backend path.
scenarioCard._selectedKey = `baseline|${START}|${END}`;
scenarioCard._selectedRange = { start: START, end: END };
const baselineBeforeLoss = scenario.baselinePath;
const lostCanonicalModel = model({ valid: false, revision: 8 });
scenarioCard._clearSelection(lostCanonicalModel);
scenario = renderScenario(lostCanonicalModel);
check(
  scenario.baselinePath === baselineBeforeLoss &&
    !scenario.canonicalPath &&
    scenario.canonicalBands.length === 0 &&
    scenarioShell.dataset.canonicalCurrent === "false" &&
    scenarioCard._selectedKey === null &&
    scenarioCard._selectedRange === null,
  "case 8 — current→unavailable removes canonical overlay/selection and keeps baseline",
);

// 9. Missing SOC is series-local: PV and LOAD remain exact and visible.
scenario = renderScenario(model({
  valid: false,
  baselineOverrides: {
    socStartPercent: null,
    socEndPercent: null,
    currentActualSocPercent: null,
  },
}));
check(
  !scenario.baselinePath &&
    scenario.bars.length === 2 &&
    scenario.bars.find((bar) => bar.dataset.series === "pv")?.dataset.rawValue === "3.257" &&
    scenario.bars.find((bar) => bar.dataset.series === "load")?.dataset.rawValue === "1.019",
  "case 9 — missing SOC does not hide backend PV or LOAD",
);

const cadenceCard = new Planner();
cadenceCard._mounted = true;
cadenceCard._c4Decorated = true;
cadenceCard._metrics.fullRenderCount = 1;
let currentModel = model();
cadenceCard._latestHass = { states: {} };
cadenceCard._buildModel = () => currentModel;
for (const method of [
  "_patchLanguage", "_patchC4Language", "_patchHero", "_patchSafety", "_patchPolicy",
  "_patchSummary", "_patchAxisFrame", "_patchReferenceSoc", "_patchSocPolicy",
  "_reconcileCanonicalLane", "_patchExpert", "_reconcileSlotTargets",
  "_updateC4SelectionVisual",
]) cadenceCard[method] = () => {};
cadenceCard._patchHero = function patchTestHero(nextModel) {
  this._testHeroAction = nextModel.hero?.action;
  this._testHeroPhysical = nextModel.hero?.physical;
  this._testHeroSupervisor = nextModel.hero?.supervisor;
};
cadenceCard._patchSafety = function patchTestSafety(nextModel) {
  this._testSafetyKey = nextModel.safety?.key;
};
cadenceCard._patchPolicy = function patchTestPolicy(policyId, nextModel) {
  if (policyId === "rce") {
    this._testRceAction = nextModel.policies.rce.presentation?.action;
  }
};
cadenceCard._patchSelectedDetail = function patchTestSelectedDetail(nextModel) {
  this._testInspectorCanonical = nextModel.canonicalTimeline.valid === true;
  this._testInspectorModel = nextModel;
  this._testInspectorPoint = nextModel.baselineTimeline.valid
    ? nextModel.baselineTimeline.points.find((point) => point.key === this._selectedKey) || null
    : null;
};
cadenceCard._patchCanonicalAvailabilityGate = function patchTestCanonicalGate(nextModel) {
  this._testCanonicalCurrent = nextModel.canonicalTimeline.valid === true;
  this._testSeriesAvailability = this._baselineSeriesAvailability(nextModel);
};
cadenceCard._applyLivePatch = function applyTestLivePatch() {
  if (!this._pendingLiveModel || !this._mounted) return;
  this._renderedLiveModel = this._pendingLiveModel;
  this._renderedLiveSerial = this._pendingLiveModel.serial;
  this._pendingLiveModel = null;
  this._lastLivePatchAt = FakeDate.now();
  this._metrics.livePointPatchCount += 1;
};

cadenceCard._flush();
const baseline = { ...cadenceCard._metrics };
check(baseline.planPathPatchCount === 1, "initial semantic plan is rendered once");

for (let index = 1; index <= 300; index += 1) {
  currentModel = model({ unrelated: index });
  cadenceCard._flush();
}
check(cadenceCard._metrics.planPathPatchCount === baseline.planPathPatchCount, "300 unrelated updates cause zero plan repaint");
check(cadenceCard._metrics.executionPatchCount === baseline.executionPatchCount, "300 unrelated updates cause zero execution patch");
check(cadenceCard._metrics.fullRenderCount === 1, "300 unrelated updates cause zero full render");

for (let index = 1; index <= 100; index += 1) {
  currentModel = model({ age: index });
  cadenceCard._flush();
}
check(cadenceCard._metrics.planPathPatchCount === baseline.planPathPatchCount, "100 age-only updates cause zero plan repaint");

currentModel = model({ generatedAt: "2026-09-01T08:05:00.000Z" });
cadenceCard._flush();
check(cadenceCard._metrics.planPathPatchCount === baseline.planPathPatchCount, "generated_at alone is excluded from planKey");

const beforeActual = { ...cadenceCard._metrics };
for (let index = 1; index <= 60; index += 1) {
  tick(1_000);
  currentModel = model({ actual: index });
  cadenceCard._flush();
}
check(
  cadenceCard._metrics.livePointPatchCount === beforeActual.livePointPatchCount + 12 &&
    cadenceCard._pendingLiveModel?.live.pvKw === 60,
  "60 one-second current_actual updates produce only twelve five-second live patches and retain the newest sample",
);
check(
  cadenceCard._metrics.livePointPatchCount === beforeActual.livePointPatchCount + 12,
  "lightweight current_actual rendering is bounded to one patch per five seconds",
);
check(cadenceCard._metrics.planPathPatchCount === beforeActual.planPathPatchCount, "current_actual never repaints plan");
check(cadenceCard._metrics.executionPatchCount === beforeActual.executionPatchCount, "current_actual never patches execution");
check(cadenceCard._pendingPlanModel === null, "current_actual does not even enqueue a semantic plan");

const beforePresentationNoise = cadenceCard._metrics.executionPatchCount;
currentModel = model({
  actual: 60,
  badge: "pending",
  safetyKey: "recalculating",
  helpers: { tariff: "pending", rce: "current", rcm: "unavailable" },
  executionOverrides: {
    selectedPolicy: "tariff",
    selectedAction: "battery_charge",
    eligibility: true,
    continuationEligibility: true,
  },
});
cadenceCard._flush();
  check(
    cadenceCard._metrics.executionPatchCount === beforePresentationNoise &&
      cadenceCard._pendingPlanModel === null,
  "planner badge/safety/helper churn does not invent a semantic plan update",
  );

const beforeMasterStop = cadenceCard._metrics.executionPatchCount;
currentModel = model({ actual: 60, masterStopLatched: true });
cadenceCard._flush();
check(
  cadenceCard._metrics.executionPatchCount === beforeMasterStop + 1 &&
    cadenceCard._metrics.planPathPatchCount === beforeActual.planPathPatchCount &&
    cadenceCard._testSafetyKey === "masterStop" &&
    cadenceCard._testHeroSupervisor === "supervisor-master-stop",
  "latched-only MASTER STOP patches visibly and immediately with active_idle, owner none and no plan repaint",
);

const beforeExecution = cadenceCard._metrics.executionPatchCount;
currentModel = model({ actual: 60, executionState: "starting" });
cadenceCard._flush();
  check(cadenceCard._metrics.executionPatchCount === beforeExecution + 1, "executionKey patches immediately");
  check(cadenceCard._metrics.planPathPatchCount === beforeActual.planPathPatchCount, "execution-only change does not repaint plan");
  cadenceCard._recordUserActivity();
  const beforeOffGrid = cadenceCard._metrics.executionPatchCount;
currentModel = model({
  revision: 99,
  serial: 99,
  actual: 60,
  executionState: "blocked",
  physicalMode: "off_grid",
  badge: "blocked",
  safetyKey: "offGrid",
});
cadenceCard._flush();
  check(
    cadenceCard._metrics.executionPatchCount === beforeOffGrid + 1 &&
      cadenceCard._metrics.planPathPatchCount === beforeActual.planPathPatchCount &&
      cadenceCard._testHeroPhysical === "physical-off_grid" &&
      cadenceCard._testHeroAction === "action-1" &&
      cadenceCard._testRceAction === "rce-action-1" &&
      cadenceCard._pendingPlanModel?.serial === 99,
  "physical Off-Grid patches immediately while new plan content waits for the short interaction delay",
  );
  currentModel = model({
    revision: 99,
    serial: 99,
    actual: 60,
    masterStopLatched: true,
    physicalMode: "self_use",
    badge: "blocked",
    safetyKey: "masterStop",
  });
  cadenceCard._flush();
  check(
    cadenceCard._testSafetyKey === "masterStop" &&
      cadenceCard._testHeroPhysical === "physical-self_use" &&
      cadenceCard._pendingPlanModel?.serial === 99,
    "newer MASTER STOP and physical state patch immediately while the older pending snapshot remains queued",
  );
  tick(1_499);
  check(
    cadenceCard._metrics.planPathPatchCount === beforeActual.planPathPatchCount &&
      cadenceCard._testSafetyKey === "masterStop",
    "plan content remains deferred only through 1.499 seconds of active interaction",
  );
  tick(1);
  check(
    cadenceCard._metrics.planPathPatchCount === beforeActual.planPathPatchCount + 1 &&
      cadenceCard._renderedPlanModel?.serial === 99 &&
      cadenceCard._pendingPlanModel === null &&
      cadenceCard._testRceAction === "rce-action-99" &&
      cadenceCard._testSafetyKey === "masterStop" &&
      cadenceCard._testHeroPhysical === "physical-self_use",
    "pending plan publishes atomically after the short delay without restoring stale Off-Grid safety state",
  );

  cadenceCard._selectedKey = cadenceCard._renderedPlanModel.baselineTimeline.points[0].key;
  cadenceCard._selectedRange = { start: START, end: END };
  cadenceCard._selectedBandKey = "canonical-band";
  const beforeUnavailable = cadenceCard._metrics.planPathPatchCount;
  cadenceCard._recordUserActivity();
  currentModel = model({ valid: false, revision: 2, serial: 1, actual: 60 });
  cadenceCard._flush();
check(
  cadenceCard._selectedKey !== null &&
    cadenceCard._selectedRange.start === START &&
    cadenceCard._selectedBandKey === "canonical-band",
  "first canonical-unavailable update leaves the atomic rendered selection unchanged",
  );
  check(cadenceCard._testCanonicalCurrent === true, "availability presentation remains on the rendered minute snapshot");
  check(cadenceCard._metrics.planPathPatchCount === beforeUnavailable, "first canonical-unavailable update waits during active interaction");
const unavailableBaselineKey =
  cadenceCard._renderedPlanModel.baselineTimeline.points[0].key;
cadenceCard._selectPoint(unavailableBaselineKey);
check(
  cadenceCard._renderedPlanModel.canonicalTimeline.valid === true &&
    cadenceCard._inspectorModel.canonicalTimeline.valid === true &&
    cadenceCard._testInspectorCanonical === true &&
    cadenceCard._selectedKey === unavailableBaselineKey,
  "baseline click during deferred current→unavailable patch uses the same rendered snapshot as its geometry",
  );
  for (let index = 2; index <= 20; index += 1) {
    tick(50);
    currentModel = model({ valid: false, revision: index + 1, serial: index, actual: 60 });
    cadenceCard._flush();
  }
  tick(549);
  check(cadenceCard._metrics.planPathPatchCount === beforeUnavailable, "20 canonical-unavailable revisions stay atomic through 1.499 seconds of interaction");
  check(cadenceCard._pendingPlanModel?.serial === 20, "canonical-unavailable burst retains only its latest pending snapshot");
  tick(1);
  check(cadenceCard._renderedPlanModel?.serial === 20, "latest canonical-unavailable snapshot renders after the short interaction delay");
  check(cadenceCard._metrics.planPathPatchCount === beforeUnavailable + 1, "canonical-unavailable burst causes exactly one deferred heavy patch");

  const beforeBurst = cadenceCard._metrics.planPathPatchCount;
  cadenceCard._recordUserActivity();
  for (let index = 1; index <= 20; index += 1) {
    if (index > 1) tick(50);
    currentModel = model({ revision: index + 1, serial: index, actual: 60 });
    cadenceCard._flush();
  }
  tick(549);
  check(cadenceCard._testCanonicalCurrent === false, "current restoration stays pending with the rest of the atomic plan snapshot for 1.499 seconds");
  check(
    cadenceCard._metrics.planPathPatchCount === beforeBurst,
    "canonical restoration causes no early in-place or heavy repaint",
  );
  check(cadenceCard._pendingPlanModel?.serial === 20, "only latest pending plan snapshot is retained");
  currentModel = model({ revision: 19, serial: 19, actual: 60 });
  cadenceCard._flush();
  check(
    cadenceCard._pendingPlanModel?.serial === 20 &&
      cadenceCard._planRevision(cadenceCard._pendingPlanModel) === 21,
    "late older canonical response cannot replace the newest pending snapshot",
  );
  tick(1);
  check(cadenceCard._renderedPlanModel?.serial === 20, "latest pending plan renders after the short interaction delay");
check(cadenceCard._metrics.planPathPatchCount === beforeBurst + 1, "restoration performs exactly one atomic deferred plan patch");
cadenceCard._selectedKey = cadenceCard._renderedPlanModel.baselineTimeline.points[0].key;
cadenceCard._selectedRange = { start: START, end: END };
const selectionBeforeLive = cadenceCard._selectedKey;
currentModel = model({ revision: 21, serial: 20, actual: 61 });
cadenceCard._flush();
check(cadenceCard._selectedKey === selectionBeforeLive, "selection survives a live-only update");

const failClosedCard = new Planner();
failClosedCard.setConfig({});
failClosedCard._metrics.fullRenderCount = 1;
let failClosedModel = model({ revision: 101, serial: 101 });
failClosedCard._latestHass = { states: {} };
failClosedCard._buildModel = () => failClosedModel;
for (const method of [
  "_patchLanguage", "_patchC4Language", "_patchHero", "_patchSafety", "_patchPolicy",
  "_patchSummary", "_patchAxisFrame", "_patchSocPolicy",
  "_reconcileCanonicalLane", "_patchExpert", "_reconcileSlotTargets",
  "_updateC4SelectionVisual",
]) failClosedCard[method] = () => {};
failClosedCard._patchSelectedDetail = function patchFailClosedDetail(nextModel) {
  this._testInspectorModel = nextModel;
  this._testInspectorPoint = nextModel.baselineTimeline.valid
    ? nextModel.baselineTimeline.points.find((point) => point.key === this._selectedKey) || null
    : null;
};
failClosedCard._patchCanonicalAvailabilityGate = function patchFailClosedGate(nextModel) {
  this._testCanonicalCurrent = nextModel.canonicalTimeline.valid === true;
  this._testSeriesAvailability = this._baselineSeriesAvailability(nextModel);
};
failClosedCard._applyLivePatch = function applyFailClosedLivePatch() {
  if (!this._pendingLiveModel || !this._mounted) return;
  this._renderedLiveModel = this._pendingLiveModel;
  this._pendingLiveModel = null;
  this._lastLivePatchAt = FakeDate.now();
  this._metrics.livePointPatchCount += 1;
};
failClosedCard._hideC4Tooltip = function hideFailClosedTooltip() {
  this._testTooltipHidden = true;
};
failClosedCard._flush();
failClosedCard._selectedKey = failClosedCard._renderedPlanModel.baselineTimeline.points[0].key;
failClosedCard._selectedRange = { start: START, end: END };
failClosedCard._selectedBandKey = "canonical-band";
const failClosedPlanCount = failClosedCard._metrics.planPathPatchCount;
const renderedCompletePoint = failClosedCard._renderedPlanModel.baselineTimeline.points[0];
const failClosedRawRecord = failClosedCard._rawPointRows.get(renderedCompletePoint.key);

failClosedCard._recordUserActivity();
failClosedModel = model({
  revision: 102,
  serial: 102,
  baselineOverrides: {
    pvKw: null,
    loadKw: null,
    socStartPercent: null,
    socEndPercent: null,
    currentActualSocPercent: null,
  },
});
failClosedCard._flush();
check(
  failClosedCard._testSeriesAvailability.baselineSocAvailable === true &&
    failClosedCard._testSeriesAvailability.baselinePvAvailable === true &&
    failClosedCard._testSeriesAvailability.baselineLoadAvailable === true &&
    failClosedCard._inspectorModel.baselineTimeline.points[0] === renderedCompletePoint &&
    failClosedRawRecord.cells.soc.textContent !== "—" &&
    failClosedRawRecord.cells.pv.textContent === "3.257" &&
    failClosedRawRecord.cells.load.textContent === "1.019" &&
    failClosedCard._renderedPlanModel.baselineTimeline.points[0] === renderedCompletePoint &&
    failClosedCard._pendingPlanModel?.serial === 102 &&
    failClosedCard._metrics.planPathPatchCount === failClosedPlanCount,
  "current→missing baseline data remains wholly pending instead of producing a partial visual frame",
);
tick(1_499);
check(
  failClosedCard._metrics.planPathPatchCount === failClosedPlanCount &&
    failClosedRawRecord.cells.pv.textContent === "3.257" &&
    failClosedRawRecord.cells.load.textContent === "1.019" &&
    failClosedRawRecord.cells.soc.textContent !== "—",
  "missing SOC/PV/LOAD causes no partial mutation during the 1.499-second interaction delay",
);
tick(1);
check(
  failClosedCard._testSeriesAvailability.baselineSocAvailable === false &&
    failClosedCard._testSeriesAvailability.baselinePvAvailable === false &&
    failClosedCard._testSeriesAvailability.baselineLoadAvailable === false &&
    failClosedCard._testSeriesAvailability.baselineCurrentSocAvailable === false &&
    failClosedCard._inspectorModel.baselineTimeline.points[0].loadKw === null &&
    failClosedRawRecord.cells.soc.textContent === "—" &&
    failClosedRawRecord.cells.pv.textContent === "—" &&
    failClosedRawRecord.cells.load.textContent === "—" &&
    failClosedCard._renderedPlanModel.baselineTimeline.points[0].loadKw === null &&
    failClosedCard._metrics.planPathPatchCount === failClosedPlanCount + 1,
  "the short interaction boundary applies missing SOC/PV/LOAD as one atomic visual snapshot",
);

failClosedCard._recordUserActivity();
failClosedModel = model({
  revision: 103,
  serial: 103,
});
failClosedCard._flush();
check(
  failClosedCard._testSeriesAvailability.baselineSocAvailable === false &&
    failClosedCard._pendingPlanModel?.serial === 103 &&
    failClosedRawRecord.cells.pv.textContent === "—" &&
    failClosedCard._metrics.planPathPatchCount === failClosedPlanCount + 1,
  "baseline restoration waits only for the short interaction delay",
);
tick(1_500);
check(
  failClosedCard._testSeriesAvailability.baselineSocAvailable === true &&
    failClosedCard._renderedPlanModel.baselineTimeline.points[0].pvKw === 3.257 &&
    failClosedCard._renderedPlanModel.baselineTimeline.points[0].loadKw === 1.019 &&
    failClosedCard._renderedPlanModel.baselineTimeline.points[0].socEndPercent === 64.567 &&
    failClosedRawRecord.cells.pv.textContent === "3.257" &&
    failClosedRawRecord.cells.load.textContent === "1.019" &&
    failClosedCard._metrics.planPathPatchCount === failClosedPlanCount + 2,
  "restored baseline data appears in exactly one later atomic snapshot",
);

const currentSocCard = new Planner();
currentSocCard.setConfig({});
currentSocCard._metrics.fullRenderCount = 1;
let currentSocModel = model({ revision: 201, serial: 201 });
currentSocCard._latestHass = { states: {} };
currentSocCard._buildModel = () => currentSocModel;
for (const method of [
  "_patchLanguage", "_patchC4Language", "_patchHero", "_patchSafety", "_patchPolicy",
  "_patchAxisFrame", "_patchSocPolicy", "_reconcileCanonicalLane",
  "_patchExpert", "_reconcileSlotTargets",
]) currentSocCard[method] = () => {};
currentSocCard._flush();
const currentSocLiveCount = currentSocCard._metrics.livePointPatchCount;
const currentSocPlanCount = currentSocCard._metrics.planPathPatchCount;
const currentPvSummary = currentSocCard._refs.summaryValues.battery.value.textContent;
check(
  currentSocCard._refs.shell.dataset.baselineCurrentSocAvailable === "true" &&
    currentSocCard._refs.currentSoc.hidden === false &&
    currentSocCard._refs.currentSocPoint.hidden === false &&
    currentSocCard._refs.currentSocValue.textContent.includes("61,2") &&
    currentSocCard._refs.summaryValues.battery.label.textContent === "Prognoza PV" &&
    currentPvSummary.includes("kWh"),
  `fresh physical SOC renders its exact marker while the first compact tile remains PV energy: ${JSON.stringify({
    availability: currentSocCard._refs.shell.dataset.baselineCurrentSocAvailable,
    currentHidden: currentSocCard._refs.currentSoc.hidden,
    pointHidden: currentSocCard._refs.currentSocPoint.hidden,
    current: currentSocCard._refs.currentSocValue.textContent,
    tileLabel: currentSocCard._refs.summaryValues.battery.label.textContent,
    tileValue: currentPvSummary,
  })}`,
);

currentSocCard._recordUserActivity();
currentSocModel = model({
  revision: 202,
  serial: 202,
  baselineValid: false,
  baselineOverrides: { currentActualSocPercent: 61.234 },
});
currentSocCard._flush();
check(
  currentSocCard._refs.shell.dataset.baselineAvailable === "true" &&
    currentSocCard._refs.shell.dataset.baselineCurrentSocAvailable === "true" &&
    currentSocCard._inspectorModel.baselineTimeline.valid === true &&
    currentSocCard._refs.currentSoc.hidden === false &&
    currentSocCard._refs.currentSocPoint.hidden === false &&
    currentSocCard._refs.summaryValues.battery.value.textContent === currentPvSummary &&
    currentSocCard._metrics.livePointPatchCount === currentSocLiveCount &&
    currentSocCard._metrics.planPathPatchCount === currentSocPlanCount &&
    currentSocCard._pendingPlanModel?.baselineTimeline.valid === false,
  "baseline unavailable remains pending without a partial availability or SOC repaint",
);
tick(1_499);
check(
  currentSocCard._refs.shell.dataset.baselineAvailable === "true" &&
    currentSocCard._refs.summaryValues.battery.value.textContent === currentPvSummary &&
    currentSocCard._metrics.planPathPatchCount === currentSocPlanCount,
  "baseline and PV summary presentation remains unchanged through the short interaction delay",
);
tick(1);
check(
  currentSocCard._refs.shell.dataset.baselineAvailable === "false" &&
    currentSocCard._refs.shell.dataset.baselineSocAvailable === "false" &&
    currentSocCard._refs.shell.dataset.baselinePvAvailable === "false" &&
    currentSocCard._refs.shell.dataset.baselineLoadAvailable === "false" &&
    currentSocCard._refs.shell.dataset.baselineCurrentSocAvailable === "true" &&
    currentSocCard._inspectorModel.baselineTimeline.valid === false &&
    currentSocCard._refs.currentSoc.hidden === false &&
    currentSocCard._refs.currentSocPoint.hidden === false &&
    currentSocCard._refs.currentSocValue.textContent.includes("61,2") &&
    currentSocCard._refs.summaryValues.battery.value.textContent === "Brak danych" &&
    currentSocCard._metrics.planPathPatchCount === currentSocPlanCount + 1,
  `short interaction boundary atomically applies baseline unavailable while retaining fresh physical SOC: ${JSON.stringify({
    baseline: currentSocCard._refs.shell.dataset.baselineAvailable,
    futureSoc: currentSocCard._refs.shell.dataset.baselineSocAvailable,
    pv: currentSocCard._refs.shell.dataset.baselinePvAvailable,
    load: currentSocCard._refs.shell.dataset.baselineLoadAvailable,
    currentSoc: currentSocCard._refs.shell.dataset.baselineCurrentSocAvailable,
    currentHidden: currentSocCard._refs.currentSoc.hidden,
    pointHidden: currentSocCard._refs.currentSocPoint.hidden,
    current: currentSocCard._refs.currentSocValue.textContent,
    tile: currentSocCard._refs.summaryValues.battery.value.textContent,
    patches: currentSocCard._metrics.planPathPatchCount,
    expectedPatches: currentSocPlanCount + 1,
  })}`,
);

currentSocCard._recordUserActivity();
currentSocModel = model({
  revision: 203,
  serial: 203,
  baselineOverrides: {
    socStartPercent: null,
    socEndPercent: null,
    currentActualSocPercent: 61.234,
  },
});
currentSocCard._flush();
check(
  currentSocCard._refs.shell.dataset.baselineAvailable === "false" &&
    currentSocCard._pendingPlanModel?.baselineTimeline.valid === true &&
    currentSocCard._metrics.planPathPatchCount === currentSocPlanCount + 1,
  "restored baseline with missing future SOC remains pending for the short interaction delay",
);
tick(1_500);
const capacitySocPlanCount = currentSocCard._metrics.planPathPatchCount;
check(
  currentSocCard._refs.shell.dataset.baselineAvailable === "true" &&
    currentSocCard._refs.shell.dataset.baselineSocAvailable === "false" &&
    currentSocCard._refs.shell.dataset.baselineCurrentSocAvailable === "true" &&
    currentSocCard._refs.currentSoc.hidden === false &&
    currentSocCard._refs.currentSocPoint.hidden === false &&
    currentSocCard._refs.currentSocValue.textContent.includes("61,2") &&
    currentSocCard._refs.summaryValues.battery.value.textContent === currentPvSummary &&
    capacitySocPlanCount === currentSocPlanCount + 2,
  "later atomic snapshot hides only the missing future line and preserves fresh physical SOC",
);

const liveCountBeforeMissingSoc = currentSocCard._metrics.livePointPatchCount;
currentSocModel = model({
  revision: 203,
  serial: 203,
  baselineOverrides: {
    socStartPercent: null,
    socEndPercent: null,
    currentActualSocPercent: null,
  },
});
currentSocCard._flush();
check(
  currentSocCard._refs.shell.dataset.baselineCurrentSocAvailable === "true" &&
    currentSocCard._refs.currentSoc.hidden === false &&
    currentSocCard._refs.currentSocPoint.hidden === false &&
    currentSocCard._refs.currentSocValue.textContent.includes("61,2") &&
    currentSocCard._refs.summaryValues.battery.value.textContent === currentPvSummary &&
    currentSocCard._metrics.livePointPatchCount === liveCountBeforeMissingSoc &&
    currentSocCard._liveTimer !== null &&
    currentSocCard._pendingLiveModel.baselineTimeline.currentActualSocPercent === null &&
    currentSocCard._metrics.planPathPatchCount === capacitySocPlanCount,
  "physical SOC present→missing remains pending instead of blinking between backend samples",
);
tick(4_999);
check(
  currentSocCard._refs.currentSoc.hidden === false &&
    currentSocCard._refs.currentSocValue.textContent.includes("61,2") &&
    currentSocCard._refs.summaryValues.battery.value.textContent === currentPvSummary,
  "missing physical SOC does not mutate the visual before the five-second live boundary",
);
tick(1);
check(
  currentSocCard._refs.shell.dataset.baselineCurrentSocAvailable === "true" &&
    currentSocCard._refs.currentSoc.hidden === true &&
    currentSocCard._refs.currentSocPoint.hidden === true &&
    currentSocCard._refs.currentSocValue.textContent === "—" &&
    currentSocCard._refs.summaryValues.battery.value.textContent === currentPvSummary &&
    currentSocCard._metrics.planPathPatchCount === capacitySocPlanCount,
  "missing physical SOC is applied once by the five-second live patch without repainting the PV tile or plan",
);

const crossTimerCard = new Planner();
crossTimerCard.setConfig({});
crossTimerCard._metrics.fullRenderCount = 1;
let crossTimerModel = model({ revision: 301, serial: 301, actual: 0 });
crossTimerCard._latestHass = { states: {} };
crossTimerCard._buildModel = () => crossTimerModel;
for (const method of [
  "_patchLanguage", "_patchC4Language", "_patchHero", "_patchSafety", "_patchPolicy",
  "_patchSummary", "_patchAxisFrame", "_patchSocPolicy", "_reconcileCanonicalLane",
  "_patchExpert", "_reconcileSlotTargets",
]) crossTimerCard[method] = () => {};
crossTimerCard._flush();
const crossInitialPlanCount = crossTimerCard._metrics.planPathPatchCount;
const crossInitialLiveCount = crossTimerCard._metrics.livePointPatchCount;
crossTimerCard._recordUserActivity();
crossTimerModel = model({ revision: 302, serial: 302, actual: 1 });
crossTimerCard._flush();
check(
  crossTimerCard._liveTimer !== null &&
    crossTimerCard._planTimer !== null,
  "simultaneous semantic and live changes arm independent live and short-interaction deliveries",
);
tick(1_499);
check(
  crossTimerCard._metrics.livePointPatchCount === crossInitialLiveCount &&
    crossTimerCard._metrics.planPathPatchCount === crossInitialPlanCount &&
    crossTimerCard._pendingLiveModel?.live.pvKw === 1 &&
    crossTimerCard._pendingPlanModel?.serial === 302,
  "both complete snapshots remain stable through 1.499 seconds of active interaction",
);
tick(1);
check(
  crossTimerCard._metrics.livePointPatchCount === crossInitialLiveCount + 1 &&
    crossTimerCard._metrics.planPathPatchCount === crossInitialPlanCount + 1 &&
    crossTimerCard._pendingLiveModel === null &&
    crossTimerCard._pendingPlanModel === null &&
    crossTimerCard._renderedPlanModel?.serial === 302 &&
    crossTimerCard._renderedLiveModel?.live.pvKw === 1,
  "short interaction boundary atomically publishes the plan and absorbs its matching live sample",
);

crossTimerModel = model({ revision: 302, serial: 302, actual: 2 });
crossTimerCard._flush();
tick(5_000);
check(
  crossTimerCard._metrics.livePointPatchCount === crossInitialLiveCount + 2 &&
    crossTimerCard._metrics.planPathPatchCount === crossInitialPlanCount + 1 &&
    crossTimerCard._renderedLiveModel?.live.pvKw === 2,
  "a second five-second cycle patches live data without repainting the plan",
);

tick(20_000);
crossTimerModel = model({ revision: 303, serial: 303, actual: 50 });
crossTimerCard._flush();
const beforeManualLiveCount = crossTimerCard._metrics.livePointPatchCount;
crossTimerCard._applyPlanPatch(crossTimerModel, true);
check(
    crossTimerCard._liveTimer === null &&
    crossTimerCard._metrics.livePointPatchCount === beforeManualLiveCount &&
    crossTimerCard._pendingLiveModel === null &&
    crossTimerCard._renderedLiveModel?.live.pvKw === 50,
  "manual plan refresh retains the already-current live sample without creating a fixed plan epoch",
);
tick(1_000);
crossTimerModel = model({ revision: 303, serial: 303, actual: 4 });
crossTimerCard._flush();
const deferredLiveCount = crossTimerCard._metrics.livePointPatchCount;
tick(3_999);
check(
  crossTimerCard._metrics.livePointPatchCount === deferredLiveCount &&
    crossTimerCard._pendingLiveModel?.live.pvKw === 4,
  "a post-refresh live sample remains pending through 4.999 seconds of the new live epoch",
);
tick(1);
check(
  crossTimerCard._metrics.livePointPatchCount === deferredLiveCount + 1 &&
    crossTimerCard._pendingLiveModel === null,
  "the post-refresh live sample publishes exactly at the new five-second boundary",
);

const heartbeatCard = new Planner();
heartbeatCard.setConfig({});
heartbeatCard.connectedCallback();
heartbeatCard._metrics.fullRenderCount = 1;
let heartbeatModel = model({ revision: 351, serial: 351 });
heartbeatCard._latestHass = { states: {} };
heartbeatCard._buildModel = () => heartbeatModel;
for (const method of [
  "_patchLanguage", "_patchC4Language", "_patchHero", "_patchSafety", "_patchPolicy",
  "_patchSummary", "_patchAxisFrame", "_patchSocPolicy", "_reconcileCanonicalLane",
  "_patchExpert", "_reconcileSlotTargets", "_patchSelectedDetail",
]) heartbeatCard[method] = () => {};
heartbeatCard._flush();
const heartbeatShell = heartbeatCard._refs.shell;
const heartbeatExpectedPath = heartbeatCard._refs.socExpected;
const heartbeatInitialPlanCount = heartbeatCard._metrics.planPathPatchCount;
const heartbeatInitialFullCount = heartbeatCard._metrics.fullRenderCount;
const firstHeartbeatTimer = heartbeatCard._planTimer;
check(
  firstHeartbeatTimer === null,
  "initial connected render does not arm an autonomous fixed-interval plan heartbeat",
);
heartbeatModel = model({ revision: 352, serial: 352 });
tick(180_000);
check(
  heartbeatCard._metrics.planPathPatchCount === heartbeatInitialPlanCount &&
    heartbeatCard._renderedPlanModel?.serial === 351,
  "elapsed wall time alone performs no hidden plan mutation",
);
heartbeatCard._flush();
check(
  heartbeatCard._metrics.planPathPatchCount === heartbeatInitialPlanCount + 1 &&
    heartbeatCard._renderedPlanModel?.serial === 352 &&
    heartbeatCard._planTimer === null,
  "a genuine semantic update publishes in its requested visual frame without a fixed delay",
);
heartbeatModel = model({ revision: 353, serial: 353 });
heartbeatCard._flush();
check(
  heartbeatCard._metrics.planPathPatchCount === heartbeatInitialPlanCount + 2 &&
    heartbeatCard._renderedPlanModel?.serial === 353 &&
    heartbeatCard._planTimer === null &&
    heartbeatCard._metrics.fullRenderCount === heartbeatInitialFullCount &&
    heartbeatCard._refs.shell === heartbeatShell &&
    heartbeatCard._refs.socExpected === heartbeatExpectedPath,
  "successive semantic updates preserve the existing shell and SVG node identities",
);
heartbeatCard.isConnected = false;
heartbeatCard.disconnectedCallback();
check(
  heartbeatCard._planTimer === null,
  "disconnect leaves no visual timer behind",
);

const reconnectCard = new Planner();
reconnectCard.setConfig({});
reconnectCard.connectedCallback();
let reconnectModel = model({ revision: 401, serial: 401 });
reconnectCard._latestHass = { states: {} };
reconnectCard._buildModel = () => reconnectModel;
for (const method of [
  "_patchLanguage", "_patchC4Language", "_patchHero", "_patchSafety", "_patchPolicy",
  "_patchSummary", "_patchAxisFrame", "_patchSocPolicy", "_reconcileCanonicalLane",
  "_patchExpert", "_patchSelectedDetail",
]) reconnectCard[method] = () => {};
reconnectCard._flush();
const reconnectSlot = reconnectCard._slotNodes.values().next().value;
const reconnectPowerBar = reconnectCard._powerBarNodes.values().next().value?.pv;
check(Boolean(reconnectPowerBar), "connected card creates an interactive PV power bar");
const reconnectPlanCount = reconnectCard._metrics.planPathPatchCount;
reconnectCard._recordUserActivity();
reconnectModel = model({ revision: 402, serial: 402, actual: 1 });
reconnectCard._flush();
check(
  reconnectCard._pendingPlanModel?.serial === 402 &&
    reconnectCard._planTimer !== null &&
    reconnectCard._liveTimer !== null,
  "connected card has one short-interaction plan/live delivery before disconnect",
);
reconnectCard.isConnected = false;
reconnectCard.disconnectedCallback();
check(
  reconnectCard._planTimer === null &&
    reconnectCard._liveTimer === null &&
    (reconnectCard._refs.refresh.listeners.get("click") || []).length === 0 &&
    (reconnectSlot.listeners.get("click") || []).length === 0 &&
    (reconnectPowerBar.listeners.get("click") || []).length === 0,
  "disconnect clears timers and every C4 listener",
);
reconnectCard.isConnected = true;
reconnectCard.connectedCallback();
check(
  reconnectCard._planTimer !== null &&
    reconnectCard._liveTimer !== null &&
    (reconnectCard._refs.refresh.listeners.get("click") || []).length === 1 &&
    (reconnectCard._refs.viewport.listeners.get("scroll") || []).length === 1 &&
    (reconnectSlot.listeners.get("click") || []).length === 1 &&
    (reconnectPowerBar.listeners.get("click") || []).length === 1 &&
    reconnectCard._resizeObserver !== null,
  "reconnect restores one refresh/activity/slot/power-bar listener, resize observer and pending timers",
);
reconnectCard._refs.refresh.dispatch("click");
check(
  reconnectCard._pendingPlanModel === null &&
    reconnectCard._renderedPlanModel?.serial === 402 &&
    reconnectCard._metrics.planPathPatchCount === reconnectPlanCount + 1 &&
    reconnectCard._refs.refresh.disabled === true &&
    reconnectCard._refs.refresh.getAttribute("aria-busy") === "true",
  "manual refresh after reconnect applies the retained snapshot and guards duplicate clicks until its microtask completes",
);
// This synchronous harness cannot yield to the native Promise microtask used by
// the component. Mirror that settled state before exercising the next reconnect.
reconnectCard._refs.refresh.disabled = false;
reconnectCard._refs.refresh.removeAttribute("aria-busy");
reconnectCard.isConnected = false;
reconnectCard.disconnectedCallback();
reconnectCard.isConnected = true;
reconnectCard.connectedCallback();
check(
  (reconnectCard._refs.refresh.listeners.get("click") || []).length === 1 &&
    (reconnectSlot.listeners.get("click") || []).length === 1 &&
    (reconnectPowerBar.listeners.get("click") || []).length === 1 &&
    reconnectCard._planTimer === null,
  "repeated reconnect does not duplicate C4 listeners or invent an autonomous heartbeat",
);
const reconnectFallbackCount = reconnectCard._metrics.planPathPatchCount;
check(
  reconnectCard._latestHass === null && reconnectCard._pendingPlanModel === null,
  "reconnect fallback scenario has neither a fresh hass object nor a pending snapshot",
);
reconnectCard._refs.refresh.dispatch("click");
check(
  reconnectCard._metrics.planPathPatchCount === reconnectFallbackCount + 1 &&
    reconnectCard._renderedPlanModel?.serial === 402 &&
    reconnectCard._planTimer === null,
  `manual refresh after reconnect falls back to the last rendered snapshot without arming a heartbeat: ${JSON.stringify({
    before: reconnectFallbackCount,
    after: reconnectCard._metrics.planPathPatchCount,
    serial: reconnectCard._renderedPlanModel?.serial,
    timer: reconnectCard._planTimer,
  })}`,
);
reconnectCard.isConnected = false;
reconnectCard.disconnectedCallback();

const selectionCard = new Planner();
selectionCard._mounted = true;
selectionCard._c4Decorated = true;
selectionCard._slotNodes = new Map();
selectionCard._laneSlotNodes = new Map();
selectionCard._powerBarNodes = new Map();
selectionCard._patchSelectedDetail = () => {};
selectionCard._updateC4SelectionVisual = () => {};
let selectionModel = model({ revision: 21, serial: 20 });
selectionCard._renderedPlanModel = selectionModel;
const selectionKey = selectionModel.baselineTimeline.points[0].key;
const targets = Object.fromEntries(
  ["pv", "load", "soc-overlay", "rce", "tariff", "rcm"].map((source) => [source, new FakeElement("button")]),
);
for (const [source, target] of Object.entries(targets)) {
  selectionCard._attachC4SlotTarget(target, selectionKey, source);
  target.click();
  check(selectionCard._selectedKey === selectionKey, `${source} selects the exact backend baseline slot key`);
}
const selectedRange = { ...selectionCard._selectedRange };
selectionModel = model({ revision: 22, serial: 21 });
selectionCard._rebaseSelection(selectionModel);
check(selectionCard._selectedKey === selectionModel.baselineTimeline.points[0].key, "selection rebases by exact baseline start/end across canonical revision");
equal(selectionCard._selectedRange, selectedRange, "rebase preserves exact start/end");
const rebasedKey = selectionCard._selectedKey;
selectionCard._selectPoint("2026-09-01T08:07:00Z|interpolated|22");
check(selectionCard._selectedKey === rebasedKey, "SOC selection rejects synthetic/interpolated timestamps");
const baselineOnlySelectionModel = model({ valid: false, revision: 23 });
selectionCard._renderedPlanModel = baselineOnlySelectionModel;
selectionCard._rebaseSelection(baselineOnlySelectionModel);
selectionCard._selectPoint(baselineOnlySelectionModel.baselineTimeline.points[0].key);
check(
  selectionCard._selectedKey ===
    baselineOnlySelectionModel.baselineTimeline.points[0].key &&
    selectionCard._selectedRange.start === START,
  "canonical-unavailable keeps exact baseline slot selection available to the inspector",
);
selectionCard._rebaseSelection(model({ valid: false, baselineValid: false, revision: 24 }));
check(
  selectionCard._selectedKey === null && selectionCard._selectedRange === null,
  "baseline-unavailable clears the baseline inspector selection",
);
check(ledger.focusCalls === 0 && ledger.scrollCalls === 0, "selection and rebasing never focus or scroll");

const detailCard = new Planner();
detailCard._mounted = true;
detailCard._c4Decorated = true;
detailCard._slotNodes = new Map();
detailCard._laneSlotNodes = new Map();
detailCard._powerBarNodes = new Map();
detailCard._latestExecutionModel = { execution: { state: "executing", physicalConfirmation: "confirmed" } };
const detailModel = model({ revision: 31 });
const detailPoint = detailModel.baselineTimeline.points[0];
detailCard._selectedKey = detailPoint.key;
const simpleKeys = ["range", "pvPower", "loadPower", "batteryPower", "soc", "importPower", "exportPower", "actionEnergy", "action", "balance", "blocker"];
const expertKeys = ["source_policy", "action_code", "kind", "selected", "active", "pv_kw", "load_kw", "battery_kw", "grid_kw", "grid_import_kw", "grid_export_kw", "protected_soc_floor_percent", "target_soc_percent", "quality", "input_revision", "plan_revision", "baseline_soc_start_percent", "baseline_soc_end_percent", "baseline_reserve_soc_percent", "canonical_soc_end_percent", "baseline_source", "baseline_provenance"];
detailCard._refs = {
  selectedStatus: new FakeElement("p"),
  selectedGrid: new FakeElement("dl"),
  selectedExpert: new FakeElement("details"),
  selectedDetail: new FakeElement("section"),
  selectionMarker: new FakeElement("div"),
  selectedFields: Object.fromEntries(simpleKeys.map((key) => [key, { value: new FakeElement("dd") }])),
  selectedExpertFields: Object.fromEntries(expertKeys.map((key) => [key, { value: new FakeElement("dd") }])),
};
detailCard._patchSelectedDetail(detailModel);
check(detailCard._refs.selectedFields.pvPower.value.textContent.includes("1,629") && detailCard._refs.selectedFields.pvPower.value.textContent.includes("kWh"), "simple inspector converts the exact half-hour PV power to interval energy");
check(detailCard._refs.selectedFields.loadPower.value.textContent.includes("0,51") && detailCard._refs.selectedFields.loadPower.value.textContent.includes("kWh"), "simple inspector converts the exact half-hour LOAD power to interval energy");
check(detailCard._refs.selectedFields.batteryPower.value.textContent.includes("0,556") && detailCard._refs.selectedFields.batteryPower.value.textContent.includes("kWh"), "simple inspector preserves the backend positive-charge convention while showing interval energy");
check(detailCard._refs.selectedFields.importPower.value.textContent.includes("0,062") && /^0(?:[,.]0+)?\s*kWh$/u.test(detailCard._refs.selectedFields.exportPower.value.textContent.replace(/\u00a0/g, " ")), "simple inspector converts exact backend import/export series without deriving either one");
check(detailCard._refs.selectedFields.soc.value.textContent.includes("61,2") && detailCard._refs.selectedFields.soc.value.textContent.includes("64,6"), "inspector shows exact baseline SOC endpoints");
check(detailCard._refs.selectedFields.actionEnergy.value.textContent.includes("Brak danych"), "baseline-only selection does not invent planned action energy");
check(detailCard._refs.selectedExpertFields.canonical_soc_end_percent.value.textContent.includes("65,1"), "inspector keeps canonical SOC separate from baseline SOC");
check(detailCard._refs.selectedExpertFields.protected_soc_floor_percent.value.textContent.includes("27"), "reserve comes from selected canonical slot");
check(detailCard._refs.selectedExpertFields.target_soc_percent.value.textContent.includes("70"), "target comes from selected canonical slot");
check(detailCard._refs.selectedExpertFields.baseline_reserve_soc_percent.value.textContent.includes("27,3"), "baseline reserve remains separately inspectable");
check(detailCard._refs.selectedExpertFields.baseline_source.value.textContent.includes('"pv":"forecast"') && detailCard._refs.selectedExpertFields.baseline_provenance.value.textContent.includes('"battery":"backend"'), "inspector preserves exact per-point baseline source and provenance maps");
check(detailCard._refs.selectedExpertFields.plan_revision.value.textContent === "31", "canonical overlays retain their exact plan revision");
check(!detailCard._refs.selectedExpertFields.grid_kw.value.textContent.includes("kW"), "inspector does not synthesize a net-grid field absent from baseline v1");
check(
  detailCard._refs.selectedFields.pvPower.value.textContent.includes("1,629") &&
    detailCard._refs.selectedExpertFields.pv_kw.value.textContent.includes("3,257") &&
    detailCard._refs.selectedExpertFields.canonical_soc_end_percent.value.textContent.includes("65,1") &&
    !detailCard._refs.selectedExpertFields.grid_kw.value.textContent.includes("kW"),
  "case 10 — inspector combines exact baseline and canonical values without interpolation or synthesis",
);
const baselineOnlyDetailModel = model({ valid: false, revision: 32 });
detailCard._selectedKey = baselineOnlyDetailModel.baselineTimeline.points[0].key;
detailCard._patchSelectedDetail(baselineOnlyDetailModel);
check(
    detailCard._refs.selectedFields.pvPower.value.textContent.includes("1,629") &&
    detailCard._refs.selectedFields.action.value.textContent.includes("niedostępny") &&
    !detailCard._refs.selectedGrid.hidden,
  "baseline inspector remains exact and usable while canonical fields are unavailable",
);

const AuroraEms = registry.get("hoymiles-aurora-ems-card");
const emsCard = new AuroraEms();
emsCard.setConfig({ language: "pl" });
const state = (value, attributes = {}) => ({ state: value, attributes });
const bindings = emsCard._config;
const emsStates = {
  [bindings.supervisor_entity]: state("active_idle", {
    owner: "none",
    transaction_owner: "none",
    transaction_id: null,
    selected_action: "none",
    selected_policy: null,
    execution_phase: "idle",
    owner_conflict: false,
    supervisor_execution_authorized: false,
  }),
  [bindings.supervisor_mode_entity]: state("Active", { options: ["Off", "Active"] }),
  [bindings.canonical_timeline_entity]: state("current", { slots: [] }),
  [bindings.physical_mode_entity]: state("self_use"),
  [bindings.control_conflict_entity]: state("off"),
  [bindings.readiness_entity]: state("Gotowe"),
  [bindings.inverter_count_entity]: state("2"),
  [bindings.ems_mode_entity]: state("self_use", { options: ["self_use", "grid_charge", "grid_discharge", "off_grid"] }),
  [bindings.clear_fault_entity]: state("unknown"),
};
const modeCalls = [];
emsCard.hass = {
  language: "pl",
  states: emsStates,
  callService: async (...args) => { modeCalls.push(args); },
};
check(emsCard._refs.invertersValue.textContent === "2", "inverter count is rendered as an integer");
check(emsCard._refs.modeSelect.disabled, "Active idle keeps the raw inverter mode selector disabled");
emsCard._refs.modeSelect.value = "grid_charge";
void emsCard._setInverterMode();
check(modeCalls.length === 0, "Active idle rejects a direct inverter mode service call");
check(!/\b(?:Shadow|Active|Off)\b/.test(emsCard.shadowRoot.textContent), "PL simple EMS card hides raw Shadow/Active/Off enums");
check(emsCard._refs.technicalFields.owner.value.textContent === "Brak", "PL technical owner localizes idle none");
check(emsCard._refs.technicalFields.phase.value.textContent === "Brak działania", "PL technical phase localizes idle");
check(emsCard._refs.technicalFields.source.value.textContent === "Brak danych", "PL technical source localizes missing selection");
for (const supervisorState of ["off", "active_idle", "selected", "starting", "executing", "blocked", "restoring", "fault"]) {
  const text = emsCard._executionText(supervisorState, {
    owner_conflict: false,
    physical_verification_result: supervisorState === "executing" ? "confirmed" : null,
  }, emsCard._copy());
  check(typeof text === "string" && text.length > 0, `main EMS card describes ${supervisorState}`);
}
check(
  emsCard._executionText("executing", { physical_verification_result: "confirmed" }, emsCard._copy()) === emsCard._copy().confirmed,
  "confirmed execution has a dedicated natural-language state",
);
check(
  emsCard._executionText("executing", { owner_conflict: true }, emsCard._copy()) === emsCard._copy().blocked,
  "control conflict is visibly fail-closed",
);
emsStates[bindings.supervisor_mode_entity] = state("Off", { options: ["Off", "Active"] });
emsStates[bindings.supervisor_entity] = state("restoring", {
  owner: "none",
  transaction_owner: "none",
  transaction_id: null,
  execution_phase: "restoring",
  owner_conflict: false,
  supervisor_execution_authorized: false,
});
emsCard.hass = { language: "pl", states: emsStates, callService: async (...args) => { modeCalls.push(args); } };
void emsCard._toggleSupervisor();
check(
  emsCard._refs.modeSelect.disabled &&
    emsCard._refs.toggle.disabled &&
    emsCard._refs.toggle.textContent === "Trwa odtwarzanie ustawień…" &&
    emsCard._refs.state.textContent === "Odtwarzanie ustawień" &&
    modeCalls.length === 0,
  "Helper Off keeps both raw mode and Supervisor toggle closed during explicit restoration",
);
void emsCard._setInverterMode();
check(modeCalls.length === 0, "Restoration rejects a direct inverter mode service call");
emsStates[bindings.supervisor_entity] = state("off", {
  owner: "none",
  observed_owner: "none",
  transaction_owner: "none",
  transaction_id: null,
  execution_phase: "idle",
  owner_conflict: false,
  supervisor_execution_authorized: false,
  master_stop_adapter_latched: false,
  manual_proxy_readback_pending: false,
});
emsCard.hass = { language: "pl", states: emsStates, callService: async (...args) => { modeCalls.push(args); } };
check(!emsCard._refs.modeSelect.disabled, "Confirmed safe Off idle allows the legacy mode selector");
void emsCard._setInverterMode();
check(
  modeCalls.length === 1 &&
    modeCalls[0][0] === "select" &&
    modeCalls[0][1] === "select_option" &&
    modeCalls[0][2]?.entity_id === bindings.ems_mode_entity,
  "Confirmed safe Off idle is the only state that may write the raw inverter mode",
);
emsStates[bindings.inverter_count_entity] = state("2.5");
emsStates[bindings.control_conflict_entity] = state("on");
emsCard.hass = { language: "pl", states: emsStates, callService: async () => {} };
check(emsCard._refs.invertersValue.textContent === emsCard._copy().noData, "fractional inverter count is rejected");
check(emsCard._refs.conflict.hidden === false, "conflict banner becomes visible");

const emsCardEn = new AuroraEms();
emsCardEn.setConfig({ language: "en" });
emsCardEn.hass = { language: "en", states: emsStates, callService: async () => {} };
check(!/\b(?:Shadow|Active|Off)\b/.test(emsCardEn.shadowRoot.textContent), "EN simple EMS card hides raw Shadow/Active/Off enums");
check(/Supervisor|EMS/.test(emsCardEn.shadowRoot.textContent), "EN card exposes natural EMS wording");

const Disclosure = registry.get("hoymiles-aurora-disclosure-card");
const disclosure = new Disclosure();
check(disclosure._expanded === false, "disclosure starts collapsed without an explicit open option");
for (const invalidConfig of [
  {},
  { card: { type: "markdown", content: "nested" }, content: "ambiguous" },
]) {
  let rejected = false;
  try {
    disclosure.setConfig(invalidConfig);
  } catch (_error) {
    rejected = true;
  }
  check(rejected, "disclosure rejects missing or ambiguous child configuration");
}
disclosure._config = {
  title: "Szczegóły techniczne",
  content: "Treść bez dodatkowego opakowania YAML",
  entity_id: ["sensor.example"],
};
equal(
  disclosure._nestedCardConfig(),
  {
    type: "markdown",
    content: "Treść bez dodatkowego opakowania YAML",
    entity_id: ["sensor.example"],
  },
  "disclosure markdown shorthand preserves content and entity dependencies",
);
const disclosureChild = new FakeElement("hui-test-card");
const disclosureBody = new FakeElement("div");
const disclosureButton = new FakeElement("button");
const disclosureAction = new FakeElement("span");
const disclosureChevron = new FakeElement("ha-icon");
disclosure._card = disclosureChild;
disclosure._refs = {
  action: disclosureAction,
  body: disclosureBody,
  button: disclosureButton,
  chevron: disclosureChevron,
  title: new FakeElement("span"),
};
const disclosureIdentity = disclosure._card;
const disclosureHassOne = { language: "pl", states: {} };
const disclosureHassTwo = { language: "pl", states: { "sensor.example": state("1") } };
disclosure.hass = disclosureHassOne;
disclosure.hass = disclosureHassTwo;
check(
  disclosure._card === disclosureIdentity && disclosureChild.hass === disclosureHassTwo,
  "successive HA updates reuse the exact nested disclosure card instance",
);
check(
  disclosureBody.hidden && disclosureButton.getAttribute("aria-expanded") === "false",
  "collapsed disclosure hides only its body and exposes aria-expanded=false",
);
disclosure._toggle();
check(
  !disclosureBody.hidden &&
    disclosureButton.getAttribute("aria-expanded") === "true" &&
    disclosureChevron.getAttribute("icon") === "mdi:chevron-up" &&
    disclosure.hasAttribute("data-expanded"),
  "disclosure toggle opens the existing body and updates its accessible state",
);
disclosure._toggle();
check(
  disclosureBody.hidden &&
    disclosureButton.getAttribute("aria-expanded") === "false" &&
    disclosureChevron.getAttribute("icon") === "mdi:chevron-down" &&
    !disclosure.hasAttribute("data-expanded"),
  "second disclosure toggle closes the existing body without replacing the child",
);

const DiagnosticsDownload = registry.get("hoymiles-diagnostics-download-card");
const diagnosticsDownload = new DiagnosticsDownload();
diagnosticsDownload._language = "pl";
check(
  diagnosticsDownload._formatPackageSize(1.2 * 1024 * 1024) === "1,2 MB",
  "diagnostic package size uses Polish number formatting",
);
diagnosticsDownload._language = "en";
check(
  diagnosticsDownload._formatPackageSize(768 * 1024) === "768 kB",
  "diagnostic package size uses a compact English unit below one megabyte",
);

const Zebra = registry.get("hoymiles-zebra-entities-card");
const zebra = new Zebra();
const zebraNativeCard = new FakeElement("hui-entities-card");
const zebraShadow = new FakeElement("shadow-root");
const zebraSurface = new FakeElement("ha-card");
const zebraStates = new FakeElement("div");
zebraStates.id = "states";
const zebraRow = new FakeElement("div");
zebraStates.append(zebraRow);
zebraSurface.append(zebraStates);
zebraShadow.append(zebraSurface);
zebraNativeCard.shadowRoot = zebraShadow;
zebra._card = zebraNativeCard;
zebra._config = {
  safe_off_only: true,
  safe_off_banner: false,
  readiness_entity: "binary_sensor.policy_ready",
  readiness_title: "Gotowość konfiguracji",
  entities: [{
    entity: "number.example",
    name: "Maksymalna moc działania",
    requirement: "required",
    description: "Ogranicza moc tej automatyki.",
  }],
};
zebra._hass = {
  language: "pl",
  states: {
    "number.example": state("unavailable"),
    "binary_sensor.policy_ready": state("off"),
    "sensor.hoymiles_hit_ems_supervisor": state("active_idle", {
      execution_phase: "idle",
      owner: "none",
      transaction_owner: "none",
      transaction_id: null,
      owner_conflict: false,
      supervisor_execution_authorized: false,
      master_stop_adapter_latched: false,
      manual_proxy_readback_pending: false,
    }),
    "input_select.hoymiles_ems_supervisor_mode": state("Active"),
    "binary_sensor.hoymiles_ems_control_conflict": state("off"),
  },
};
zebra._applySafeOffPresentation();
const scrollCallsBeforeGuidance = ledger.scrollCalls;
const focusCallsBeforeGuidance = ledger.focusCalls;
zebra._applyFieldGuidance();
const zebraHelp = zebraRow.querySelector("[data-hoymiles-field-help]");
const zebraReadiness = zebraSurface.querySelector("[data-hoymiles-readiness]");
const readinessButtons = zebraReadiness?.querySelectorAll("button") || [];
check(
  zebraNativeCard.getAttribute("data-hoymiles-safe-off-write") === "closed" &&
    zebraRow.inert === true &&
    zebraRow.getAttribute("aria-disabled") === "true" &&
    !zebraSurface.hasAttribute("data-hoymiles-safe-off-write"),
  "safe_off_banner=false suppresses only the repeated banner while retaining inert fail-closed rows",
);
check(
  zebraHelp?.textContent.includes("Wymagane") &&
    zebraHelp.textContent.includes("Ogranicza moc tej automatyki.") &&
    zebraHelp.textContent.includes("Uzupełnij to pole") &&
    zebraReadiness?.textContent.includes("Gotowość konfiguracji") &&
    zebraReadiness.textContent.includes("0/1") &&
    readinessButtons.length === 2 &&
    ledger.scrollCalls === scrollCallsBeforeGuidance &&
    ledger.focusCalls === focusCallsBeforeGuidance,
  "guided policy settings render natural help, required inline error and readiness without automatic scrolling",
);
readinessButtons[0]?.click();
check(
  ledger.scrollCalls === scrollCallsBeforeGuidance + 1 &&
    ledger.focusCalls === focusCallsBeforeGuidance + 1,
  "only an explicit click on a missing readiness item scrolls to and focuses its field",
);
zebra._hass.states["number.example"] = state("400");
zebra._hass.states["binary_sensor.policy_ready"] = state("on");
zebra._applyFieldGuidance();
const stableGuidance = zebraRow.querySelector("[data-hoymiles-field-help]");
const stableReadiness = zebraSurface.querySelector("[data-hoymiles-readiness]");
zebra._hass.states["number.example"] = state("401");
zebra._applyFieldGuidance();
check(
  zebraRow.querySelector("[data-hoymiles-field-help]") === stableGuidance &&
    zebraSurface.querySelector("[data-hoymiles-readiness]") === stableReadiness &&
    stableGuidance?.querySelector("[data-hoymiles-field-error]")?.textContent === "\u00a0" &&
    stableGuidance?.querySelector("[data-hoymiles-field-error]")?.getAttribute("aria-hidden") === "true",
  "ordinary live values preserve the guidance and readiness DOM while reserving the inline-error space",
);

const forbiddenBackendMarkers = [
  "custom_components/hoymiles_hit_modbus/",
  "home_assistant/hoymiles_ems_scheduler.yaml",
  "packages/settings.yaml",
  "packages/parallel_network.yaml",
];
const c4OwnedPaths = [cardPath, dashboardPath, "tools/test_c4_final_ui_contract.js"];
check(
  c4OwnedPaths.every((ownedPath) => !forbiddenBackendMarkers.some((marker) => ownedPath.startsWith(marker))),
  "C4 static path allowlist contains no backend, scheduler, or firmware path",
);

console.log(`C4 FINAL UI CONTRACT: GREEN (${checks} checks)`);
