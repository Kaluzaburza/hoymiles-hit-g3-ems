const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(process.env.HOYMILES_UI_TEST_ROOT || process.cwd());
const cardPath = "home_assistant/www/hoymiles-rce-chart-card.js";
const dashboardPath = "dashboard_hoymiles.yaml";
const cardSource = fs.readFileSync(process.env.HOYMILES_RCE_TEST_SOURCE || path.join(root, cardPath), "utf8");
const dashboardSource = fs
  .readFileSync(path.join(root, dashboardPath), "utf8")
  .replaceAll("\r", "");

let checks = 0;
const failures = [];
const check = (condition, message) => {
  checks += 1;
  if (!condition) failures.push(message);
};
const scenario = (name, callback) => {
  try {
    callback();
  } catch (error) {
    failures.push(`${name}: ${error?.stack || error}`);
  }
};

class FakeStyle {
  constructor() {
    this.values = new Map();
  }
  setProperty(name, value) {
    this.values.set(name, String(value));
  }
  removeProperty(name) {
    this.values.delete(name);
  }
}

class FakeClassList {
  constructor(owner) {
    this.owner = owner;
  }
  _tokens() {
    return new Set(String(this.owner.className || "").split(/\s+/).filter(Boolean));
  }
  toggle(name, force) {
    const tokens = this._tokens();
    const enabled = force === undefined ? !tokens.has(name) : Boolean(force);
    if (enabled) tokens.add(name);
    else tokens.delete(name);
    this.owner.className = [...tokens].join(" ");
    return enabled;
  }
  contains(name) {
    return this._tokens().has(name);
  }
}

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = new FakeStyle();
    this.className = "";
    this.classList = new FakeClassList(this);
    this.dataset = {};
    this.innerHTML = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
  }
  append(...children) {
    for (const child of children) {
      if (child === null || child === undefined) continue;
      child.parentNode = this;
      this.children.push(child);
    }
  }
  prepend(...children) {
    for (const child of [...children].reverse()) {
      child.parentNode = this;
      this.children.unshift(child);
    }
  }
  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }
  removeAttribute(name) {
    this.attributes.delete(name);
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  removeEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    this.listeners.set(type, listeners.filter((item) => item !== listener));
  }
  querySelector() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
  focus() {}
  getBoundingClientRect() {
    return { left: 0, top: 0, width: 1200, height: 430 };
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

let nowMs = Date.parse("2026-09-01T22:05:00Z");
class FakeDate extends Date {
  constructor(...args) {
    super(...(args.length ? args : [nowMs]));
  }
  static now() {
    return nowMs;
  }
}

const registry = new Map();
const fakeWindow = {
  customCards: [],
  customStrategies: [],
  matchMedia: () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  }),
};
const context = {
  console,
  Date: FakeDate,
  Event: class {},
  CustomEvent: class {},
  HTMLElement: TestHTMLElement,
  Intl,
  URL,
  AbortController,
  document: {
    documentElement: { lang: "pl" },
    createElement: (tagName) => new FakeElement(tagName),
    createElementNS: (_namespace, tagName) => new FakeElement(tagName),
    createTextNode: (value) => ({ nodeType: 3, textContent: String(value) }),
  },
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
  requestAnimationFrame: (callback) => {
    callback(nowMs);
    return 1;
  },
  cancelAnimationFrame() {},
  setTimeout: () => 1,
  clearTimeout() {},
  ResizeObserver: class {
    observe() {}
    disconnect() {}
  },
  MutationObserver: class {
    observe() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  },
  fetch: async () => ({ ok: true, json: async () => ({ views: [] }) }),
  WebSocket: class {
    constructor() {
      throw new Error("RCE price chart must not open a WebSocket");
    }
  },
};
context.globalThis = context;
vm.runInNewContext(
  cardSource.replaceAll(
    "import.meta.url",
    JSON.stringify("https://homeassistant.example/local/hoymiles-rce-chart-card.js?rce-48h-test"),
  ),
  context,
  { filename: cardPath },
);

const RceChartCard = registry.get("hoymiles-rce-chart-card");
check(Boolean(RceChartCard), "hoymiles-rce-chart-card is not registered");

const TIME_ZONE = "Europe/Warsaw";
const QUARTER_MS = 15 * 60_000;
const HALF_HOUR_MS = 30 * 60_000;
const clockFormatter = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIME_ZONE,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

const clock = (timestamp) => clockFormatter.format(new Date(timestamp));
const state = (value, attributes = {}) => ({
  state: String(value),
  attributes,
  last_updated: new Date(nowMs).toISOString(),
});

function officialRows(businessDate, startUtc, endUtc, basePrice = 400) {
  const rows = [];
  for (let start = Date.parse(startUtc), index = 0; start < Date.parse(endUtc); start += QUARTER_MS, index += 1) {
    const end = start + QUARTER_MS;
    rows.push({
      business_date: businessDate,
      period: `${clock(start)} - ${clock(end)}`,
      period_utc: `${new Date(start).toISOString().slice(11, 16)} - ${new Date(end).toISOString().slice(11, 16)}`,
      // PSE defines dtime_utc as the interval end, not its start.
      dtime_utc: new Date(end).toISOString(),
      rce_pln: basePrice + index,
    });
  }
  return rows;
}

function policyPoint(start, selected, energy = 0, revenue = 0, price = 0.45) {
  return {
    start: new Date(start).toISOString(),
    end: new Date(start + HALF_HOUR_MS).toISOString(),
    kind: "forecast_plan",
    pv_kw: 0,
    load_kw: 0.6,
    battery_kw: selected ? -2 : 0,
    grid_kw: selected ? -1.4 : 0.6,
    grid_import_kw: selected ? 0 : 0.6,
    grid_export_kw: selected ? 1.4 : 0,
    soc_percent: 70,
    baseline_soc_percent: 72,
    protected_soc_floor_percent: 25,
    action_code: selected ? "export" : "idle",
    active: false,
    selected,
    quality: "complete",
    policy: {
      sell_price_pln_kwh: price,
      planned_export_kwh: energy,
      planned_battery_withdrawal_kwh: energy / 0.9,
      target_discharge_kw: selected ? 2 : 0,
      command_discharge_power_percent: selected ? 40 : 0,
      target_tolerance_kw: 0.05,
      expected_revenue_pln: revenue,
    },
  };
}

function timelineState(startUtc, endUtc, selectedByStart = new Map()) {
  const points = [];
  for (let start = Date.parse(startUtc); start < Date.parse(endUtc); start += HALF_HOUR_MS) {
    const selected = selectedByStart.get(start);
    points.push(
      policyPoint(
        start,
        Boolean(selected),
        selected?.energy || 0,
        selected?.revenue || 0,
        selected?.price || 0.45,
      ),
    );
  }
  return state("current", {
    schema_version: 2,
    policy_id: "rce",
    generated_at: new Date(nowMs).toISOString(),
    timezone: TIME_ZONE,
    slot_minutes: 30,
    result_current: true,
    recalculation_pending: false,
    input_revision: 11,
    plan_revision: 1,
    plan_revision_scope: "runtime",
    plan_entity_id: "sensor.rce_plan",
    point_count: points.length,
    points,
  });
}

function canonicalState(startUtc, endUtc, selectedByStart = new Map()) {
  const slots = [];
  for (let start = Date.parse(startUtc), index = 0; start < Date.parse(endUtc); start += HALF_HOUR_MS, index += 1) {
    const selected = selectedByStart.get(start);
    const selectedRce = Boolean(selected);
    const action = selectedRce ? "rce_export" : "none";
    const target = selectedRce ? "ems_block_4300_4306" : "none";
    slots.push({
      slot_id: `slot-${index}`,
      starts_at: new Date(start).toISOString(),
      ends_at: new Date(start + HALF_HOUR_MS).toISOString(),
      selected_policy: selectedRce ? "rce" : "none",
      selected_action: action,
      start_eligibility: selectedRce ? "eligible" : "not_applicable",
      owner: selectedRce ? "rce" : "none",
      policy_candidates: [],
      rejected_reasons: [],
      planned: {
        pv_kwh: 0,
        load_kwh: 0,
        battery_kwh: selectedRce ? -Number(selected.energy || 0) : 0,
        grid_kwh_import_positive: 0,
      },
      soc_equation: {
        pv_to_battery_kwh: 0,
        grid_to_battery_kwh: 0,
        battery_to_load_kwh: 0,
        battery_to_grid_kwh: selectedRce ? Number(selected.energy || 0) : 0,
        losses_kwh: 0,
        soc_start_percent: 70,
        soc_end_percent: 70,
        energy_balance_residual_kwh: 0,
        continuity_residual_percent: 0,
        system_balance_residual_kwh: 0,
      },
      protected_reserve: {
        percent: 25,
        margin_end_percent: 45,
        respected: true,
      },
      command_expectation: {
        target,
        values: {},
        source_generation: selectedRce ? 1 : null,
      },
      readback_expectation: {
        target,
        values: {},
        newer_than_source_generation: selectedRce,
        physical_expectation: selectedRce ? "matching_fc03" : "none",
      },
    });
  }
  return state("current", {
    schema_version: 1,
    output_only: true,
    built_at: new Date(Date.parse(startUtc)).toISOString(),
    arbitration_revision: "a".repeat(64),
    ledger_revision: "b".repeat(64),
    usable_capacity_kwh: 10,
    initial_soc_percent: 70,
    final_soc_percent: 70,
    slots,
    audit: {
      max_continuity_residual_percent: 0,
      max_energy_balance_residual_kwh: 0,
      max_system_balance_residual_kwh: 0,
      reserve_violation_count: 0,
    },
  });
}

const config = Object.freeze({
  entity: "sensor.rce_today",
  tomorrow_entity: "sensor.rce_tomorrow",
  plan_entity: "sensor.rce_plan",
  timeline_entity: "sensor.rce_timeline",
  supervisor_entity: "sensor.ems_supervisor",
  control_conflict_entity: "binary_sensor.ems_control_conflict",
  current_price_entity: "sensor.rce_current_price",
  active_entity: "input_boolean.rce_active",
  block_enabled_entity: "input_boolean.sale_block",
  block_start_entity: "input_datetime.sale_block_start",
  block_end_entity: "input_datetime.sale_block_end",
});

function makeCard({
  todayRows,
  tomorrowRows,
  planAttributes = {},
  timeline,
  tomorrowState,
  supervisor,
  canonical,
  layout = "standard",
  legacyActive = false,
  conflict = "off",
  blockEnabled = false,
  blockStart = "22:00:00",
  blockEnd = "06:00:00",
}) {
  const card = new RceChartCard();
  card.setConfig({ ...config, layout });
  card.hass = {
    language: "pl",
    config: { time_zone: TIME_ZONE },
    states: {
      "sensor.rce_today": state(todayRows.length, { value: todayRows }),
      "sensor.rce_tomorrow": tomorrowState || state(tomorrowRows.length, { value: tomorrowRows }),
      "sensor.rce_plan": state("ready", planAttributes),
      "sensor.rce_timeline": timeline,
      ...(canonical
        ? { "sensor.hoymiles_hit_ems_supervisor_canonical_plan": canonical }
        : {}),
      "sensor.ems_supervisor": supervisor || state("active_idle", {
        execution_phase: "idle",
        selected_policy: null,
        owner: "none",
        transaction_owner: "none",
        owner_conflict: false,
        candidate_summaries: [],
      }),
      "binary_sensor.ems_control_conflict": state(conflict),
      "sensor.rce_current_price": state("0.6123"),
      "input_boolean.rce_active": state(legacyActive ? "on" : "off"),
      "input_boolean.sale_block": state(blockEnabled ? "on" : "off"),
      "input_datetime.sale_block_start": state(blockStart),
      "input_datetime.sale_block_end": state(blockEnd),
    },
  };
  return card;
}

const sampleStartMs = (sample) => {
  for (const key of ["startMs", "startTimestampMs", "timestampMs", "instantMs"]) {
    if (Number.isFinite(Number(sample?.[key]))) return Number(sample[key]);
  }
  for (const key of ["start", "startTimestamp", "instant"]) {
    const parsed = Date.parse(sample?.[key]);
    if (Number.isFinite(parsed)) return parsed;
  }
  return Number.NaN;
};

scenario("Aurora compact empty state keeps the compact shell", () => {
  const card = makeCard({
    todayRows: [],
    tomorrowRows: [],
    planAttributes: {},
    layout: "aurora_compact",
  });
  const output = card.shadowRoot?.innerHTML || "";
  check(
    card._rceSamples.length === 0
      && output.includes('data-rce-layout="aurora-compact"')
      && output.includes("Plan cenowy · dziś i jutro")
      && output.includes("Brak kompletnych danych PSE"),
    "compact RCE no-data state must not fall back to the legacy visual shell",
  );
});

scenario("normal two-day market", () => {
  nowMs = Date.parse("2026-09-01T22:05:00Z");
  const todayRows = officialRows(
    "2026-09-02",
    "2026-09-01T22:00:00Z",
    "2026-09-02T22:00:00Z",
  ).reverse();
  const tomorrowRows = officialRows(
    "2026-09-03",
    "2026-09-02T22:00:00Z",
    "2026-09-03T22:00:00Z",
    600,
  ).reverse();
  // Local labels are deliberately false: absolute PSE time must remain authoritative.
  todayRows.at(-1).period = "17:00 - 17:15";

  const selected = new Map([
    [Date.parse("2026-09-02T16:00:00Z"), { energy: 1, revenue: 0.9, price: 0.9123 }],
    [Date.parse("2026-09-02T16:30:00Z"), { energy: 1.25, revenue: 1.02, price: 0.95 }],
    [Date.parse("2026-09-03T04:00:00Z"), { energy: 1.5, revenue: 1.5, price: 1 }],
  ]);
  const card = makeCard({
    todayRows,
    tomorrowRows,
    timeline: timelineState(
      "2026-09-01T22:00:00Z",
      "2026-09-03T22:00:00Z",
      selected,
    ),
    planAttributes: {
      result_current: true,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: 0.7312,
      planned_export_kwh: 3.75,
      planned_revenue_pln: 3.42,
      planning_scope: "today_and_tomorrow",
      tomorrow_data_pending: false,
      planned_slots: [
        { date: "2026-09-02", start: "18:00", end: "18:30", price: 0.9123, energy: 1, revenue: 0.9 },
        { date: "2026-09-02", start: "18:30", end: "19:00", price: 0.95, energy: 1.25, revenue: 1.02 },
        { date: "2026-09-03", start: "06:00", end: "06:30", price: 1, energy: 1.5, revenue: 1.5 },
      ],
    },
  });
  const output = card.shadowRoot?.innerHTML || "";
  check(card._rceSamples.length === 96, "normal today+tomorrow must merge to 96 half-hour blocks");
  check(
    new Set(card._rceSamples.map((sample) => sample.key)).size === 96,
    "normal combined samples must have stable unique keys",
  );
  check(
    sampleStartMs(card._rceSamples[0]) === Date.parse("2026-09-01T22:00:00Z"),
    "dtime_utc must be sorted and interpreted as interval end minus 15 minutes",
  );
  check(
    card._rceSamples.filter((sample) => sample.selected).length === 3,
    "three selected half-hours must mark exactly three control blocks",
  );
  check(
    (output.match(/<svg\b/g) || []).length === 1,
    "today and tomorrow must render in one combined SVG",
  );
  check(
    output.includes("Cena graniczna planu") && output.includes("0,9123 PLN/kWh")
      && output.includes("1,0000 PLN/kWh") && !output.includes("0,7312 PLN/kWh"),
    "combined card must derive independent daily floors from the selected current slots",
  );
  check(
    output.includes("3,75 kWh") && output.includes("3,42 PLN"),
    "combined card must show exact full-horizon planned export and revenue",
  );
  check(
    output.includes("18:00–19:00") && output.includes("06:00–06:30"),
    "contiguous selected exports must render as compact today/tomorrow sale windows",
  );
  check(
    output.includes("Jutro") || output.includes("jutro"),
    "combined chart must expose the tomorrow boundary",
  );
});

scenario("Aurora compact featured plan reuses the authoritative 48-hour model", () => {
  nowMs = Date.parse("2026-09-01T22:05:00Z");
  const todayRows = officialRows(
    "2026-09-02",
    "2026-09-01T22:00:00Z",
    "2026-09-02T22:00:00Z",
  );
  const tomorrowRows = officialRows(
    "2026-09-03",
    "2026-09-02T22:00:00Z",
    "2026-09-03T22:00:00Z",
    600,
  );
  const selected = new Map([
    [Date.parse("2026-09-02T16:00:00Z"), { energy: 1, revenue: 0.9, price: 0.9123 }],
    [Date.parse("2026-09-02T16:30:00Z"), { energy: 1.25, revenue: 1.02, price: 0.95 }],
    [Date.parse("2026-09-03T04:00:00Z"), { energy: 1.5, revenue: 1.5, price: 1 }],
  ]);
  const planAttributes = {
    result_current: true,
    recalculation_pending: false,
    automatic_price_floor_pln_kwh: 0.7312,
    planned_export_kwh: 3.75,
    planned_revenue_pln: 3.42,
    planning_scope: "today_and_tomorrow",
    tomorrow_data_pending: false,
    gcf_execution_data_fresh: true,
    gcf_enabled: true,
    gcf_export_limit_percent: 100,
    planned_slots: [
      { date: "2026-09-02", start: "18:00", end: "18:30", price: 0.9123, energy: 1, revenue: 0.9 },
      { date: "2026-09-02", start: "18:30", end: "19:00", price: 0.95, energy: 1.25, revenue: 1.02 },
      { date: "2026-09-03", start: "06:00", end: "06:30", price: 1, energy: 1.5, revenue: 1.5 },
    ],
  };
  const timeline = timelineState(
    "2026-09-01T22:00:00Z",
    "2026-09-03T22:00:00Z",
    selected,
  );
  const canonical = canonicalState(
    "2026-09-01T22:00:00Z",
    "2026-09-03T22:00:00Z",
    selected,
  );
  const inspectedCanonicalSlot = canonical.attributes.slots.find(
    (slot) => slot.starts_at === "2026-09-02T16:00:00.000Z",
  );
  inspectedCanonicalSlot.soc_equation.soc_start_percent = 68;
  inspectedCanonicalSlot.soc_equation.soc_end_percent = 63.5;
  const card = makeCard({
    todayRows,
    tomorrowRows,
    timeline,
    canonical,
    planAttributes,
    layout: "aurora_compact",
  });
  const output = card.shadowRoot?.innerHTML || "";
  check(card._rceSamples.length === 96, "compact RCE must retain all 96 half-hour blocks");
  check(
    output.includes('data-rce-layout="aurora-compact"')
      && output.includes("Horyzont 48 h · krok 30 min")
      && output.includes("Plan cenowy · dziś i jutro"),
    "compact RCE must render the accepted Variant A header",
  );
  check(
    output.includes("612 zł/MWh") && output.includes("912 zł/MWh") && output.includes("1000 zł/MWh"),
    "compact RCE must convert current price and both selected daily floors to zł/MWh only for display",
  );
  check(
    /<div data-tone="rose"><span>Wybrane przez EMS<\/span><strong>3,75 kWh<\/strong><\/div>/.test(output),
    "a verified positive export limit must retain canonical EMS-selected RCE energy",
  );
  check(
    (output.match(/<article class="rce-plan-interval">/g) || []).length === 2
      && output.includes("18:00–19:00")
      && output.includes("06:00–06:30"),
    "compact RCE must render every contiguous sale window across today and tomorrow",
  );
  check(
    (output.match(/<rect class="rce-sale-window"/g) || []).length === 2
      && output.includes('<polyline class="rce-price-line"')
      && output.includes('class="rce-threshold-line"'),
    "compact RCE must show candidate overlays, the price line and automatic threshold",
  );
  check(
    !output.includes("Potwierdzony limit eksportu wynosi 0%")
      && output.includes("EMS wybrał do wykonania: 3,75 kWh"),
    "positive GCF evidence must not be presented as a zero-export block",
  );
  check(
    output.includes('class="sample-panel compact-sample-panel"')
      && output.includes('data-selected="false" data-rce-panel aria-live="polite"')
      && output.includes("height: 168px")
      && !output.includes("clip-path: inset(50%)")
      && !output.includes('<div class="plan-strip">'),
    "compact RCE must expose a visible, height-stable interval inspector",
  );
  const selectedSample = card._rceSamples.find(
    (sample) => sample.startMs === Date.parse("2026-09-02T16:00:00Z"),
  );
  check(
    selectedSample?.candidateExportKwh === 1
      && selectedSample?.selectedByEmsKwh === 1,
    "a selected RCE interval must keep possible export distinct from canonical EMS export",
  );
  check(
    selectedSample?.socBeforePercent === 68
      && selectedSample?.socAfterPercent === 63.5,
    "RCE interval details must use canonical automatic SOC start and end",
  );
  check(
    selectedSample?.stateLabel === "Kandydat sprzedaży dynamicznej — wybrany przez EMS",
    "the interval state must say when EMS selected the RCE candidate",
  );
  card._selectedRceKey = selectedSample.key;
  const originalRender = card._render.bind(card);
  let identicalConfigRenders = 0;
  card._render = () => {
    identicalConfigRenders += 1;
    originalRender();
  };
  for (let index = 0; index < 50; index += 1) {
    card.setConfig({ ...config, layout: "aurora_compact" });
  }
  check(
    identicalConfigRenders === 0 && card._selectedRceKey === selectedSample.key,
    "50 identical RCE setConfig cycles must preserve the selected slot without rebuilding the card",
  );
  card._render = originalRender;
  check(
    cardSource.includes("previousScrollLeft")
      && cardSource.includes("previousFocusedKey")
      && cardSource.includes('data-rce-key="${this._escape(sample.key)}"'),
    "a genuine RCE update must restore horizontal scroll and focus by semantic slot key",
  );
  check(
    cardSource.includes('{ featured_card: { ...featuredCard, layout: "aurora_compact" } }'),
    "Variant A policy wrapper must opt only its featured RCE chart into compact layout",
  );

  const zeroCanonical = canonicalState(
    "2026-09-01T22:00:00Z",
    "2026-09-03T22:00:00Z",
    new Map(),
  );
  const zeroCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline,
    canonical: zeroCanonical,
    planAttributes: { ...planAttributes, gcf_export_limit_percent: 0 },
    layout: "aurora_compact",
  });
  const zeroOutput = zeroCard.shadowRoot?.innerHTML || "";
  const zeroSample = zeroCard._rceSamples.find(
    (sample) => sample.startMs === Date.parse("2026-09-02T16:00:00Z"),
  );
  check(
    /<div data-tone="rose"><span>Wybrane przez EMS<\/span><strong>0,00 kWh<\/strong><\/div>/.test(zeroOutput)
      && !zeroOutput.includes("Potwierdzony limit eksportu wynosi 0%")
      && !zeroOutput.includes("Plan EMS zgłasza eksport mimo potwierdzonego limitu 0%"),
    "an incoherent GCF 0% source plan must keep the candidate visible without claiming a verified zero-export cohort",
  );
  check(
    zeroSample?.candidateExportKwh === 1
      && zeroSample?.selectedByEmsKwh === 0
      && zeroSample?.socBeforePercent === 70
      && zeroSample?.socAfterPercent === 70
      && zeroSample?.stateLabel === "Kandydat sprzedaży dynamicznej — niewybrany przez EMS",
    "an incoherent GCF 0% slot must not be presented as a verified execution block",
  );

  const emptyTimeline = timelineState(
    "2026-09-01T22:00:00Z",
    "2026-09-03T22:00:00Z",
    new Map(),
  );
  const emptyZeroPlan = {
    ...planAttributes,
    input_revision: 11,
    planned_export_kwh: 0,
    planned_revenue_pln: 0,
    planned_slots: [],
    gcf_export_limit_percent: 0,
  };
  const emptyZeroCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline: emptyTimeline,
    canonical: zeroCanonical,
    planAttributes: emptyZeroPlan,
    layout: "aurora_compact",
  });
  const emptyZeroOutput = emptyZeroCard.shadowRoot?.innerHTML || "";
  check(
    emptyZeroOutput.includes("Sprzedaż zablokowana - 0 export aktywny")
      && !emptyZeroOutput.includes("Brak zaplanowanej sprzedaży"),
    "an empty plan with fresh confirmed GCF 0% must use the explicit blocked-sale tile",
  );
  const unverifiedZeroVariants = [
    { ...emptyZeroPlan, gcf_execution_data_fresh: false },
    { ...emptyZeroPlan, gcf_execution_data_fresh: undefined },
    { ...emptyZeroPlan, gcf_enabled: false },
    { ...emptyZeroPlan, gcf_export_limit_percent: 1 },
    { ...emptyZeroPlan, gcf_export_limit_percent: "0" },
    { ...emptyZeroPlan, result_current: false, recalculation_pending: true },
  ];
  for (const attributes of unverifiedZeroVariants) {
    const unverifiedCard = makeCard({
      todayRows,
      tomorrowRows,
      timeline: emptyTimeline,
      canonical: zeroCanonical,
      planAttributes: attributes,
      layout: "aurora_compact",
    });
    const unverifiedOutput = unverifiedCard.shadowRoot?.innerHTML || "";
    check(
      unverifiedOutput.includes("Brak zaplanowanej sprzedaży")
        && !unverifiedOutput.includes("Sprzedaż zablokowana - 0 export aktywny"),
      "the zero-export sale tile must fail closed when current physical GCF evidence is incomplete",
    );
  }

  const stalePlanCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline: timelineState(
      "2026-09-01T22:00:00Z",
      "2026-09-03T22:00:00Z",
      new Map(),
    ),
    canonical: zeroCanonical,
    planAttributes: emptyZeroPlan,
    layout: "aurora_compact",
  });
  stalePlanCard._states[config.plan_entity].last_updated = new Date(
    nowMs - (300 * 1000 + 1),
  ).toISOString();
  stalePlanCard._render();
  check(
    stalePlanCard.shadowRoot.innerHTML.includes("Brak zaplanowanej sprzedaży")
      && !stalePlanCard.shadowRoot.innerHTML.includes("Sprzedaż zablokowana - 0 export aktywny"),
    "the zero-export sale tile must reject a plan older than the 300-second execution-age contract",
  );

  const futureTimeline = timelineState(
    "2026-09-01T22:00:00Z",
    "2026-09-03T22:00:00Z",
    new Map(),
  );
  futureTimeline.attributes.generated_at = new Date(
    nowMs + (5 * 1000 + 1),
  ).toISOString();
  const futureTimelineCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline: futureTimeline,
    canonical: zeroCanonical,
    planAttributes: emptyZeroPlan,
    layout: "aurora_compact",
  });
  check(
    futureTimelineCard.shadowRoot.innerHTML.includes("Brak zaplanowanej sprzedaży")
      && !futureTimelineCard.shadowRoot.innerHTML.includes("Sprzedaż zablokowana - 0 export aktywny"),
    "the zero-export sale tile must reject a future-dated timeline publication",
  );

  const arbitrationCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline,
    canonical: zeroCanonical,
    planAttributes,
    layout: "aurora_compact",
  });
  const arbitrationSample = arbitrationCard._rceSamples.find(
    (sample) => sample.startMs === Date.parse("2026-09-02T16:00:00Z"),
  );
  check(
    arbitrationSample?.candidateExportKwh === 1
      && arbitrationSample?.selectedByEmsKwh === 0
      && arbitrationSample?.stateLabel === "Kandydat sprzedaży dynamicznej — niewybrany przez EMS",
    "an RCE optimizer candidate rejected by final EMS arbitration must not be called planned export",
  );

  const blockedCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline,
    canonical,
    planAttributes,
    layout: "aurora_compact",
    blockEnabled: true,
    blockStart: "18:00:00",
    blockEnd: "19:00:00",
  });
  const blockedSample = blockedCard._rceSamples.find(
    (sample) => sample.startMs === Date.parse("2026-09-02T16:00:00Z"),
  );
  const blockedPanel = blockedCard._rcePanelHtml(blockedSample);
  check(
    blockedSample?.selected === true
      && blockedSample?.planned === false
      && blockedSample?.candidateExportKwh === 1
      && blockedSample?.stateLabel.includes("Blokada sprzedaży 18:00–19:00")
      && blockedPanel.includes("Kandydat sprzedaży dynamicznej")
      && blockedPanel.includes(">Tak<")
      && blockedPanel.includes("1,00 kWh"),
    "a user export lockout must preserve the optimizer candidate while marking execution blocked",
  );

  const conflictingCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline: emptyTimeline,
    canonical,
    planAttributes: emptyZeroPlan,
    layout: "aurora_compact",
  });
  const conflictingOutput = conflictingCard.shadowRoot?.innerHTML || "";
  const conflictingSample = conflictingCard._rceSamples.find(
    (sample) => sample.startMs === Date.parse("2026-09-02T16:00:00Z"),
  );
  check(
    conflictingOutput.includes("Plan EMS zgłasza eksport mimo potwierdzonego limitu 0%")
      && conflictingSample?.selectedByEmsKwh === 0
      && conflictingSample?.socBeforePercent === null
      && conflictingSample?.socAfterPercent === null,
    "contradictory canonical export under GCF 0% must fail closed and hide its untrustworthy SOC trajectory",
  );

  const invalidCanonical = canonicalState(
    "2026-09-01T22:00:00Z",
    "2026-09-03T22:00:00Z",
    selected,
  );
  invalidCanonical.attributes.schema_version = 2;
  const invalidCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline,
    canonical: invalidCanonical,
    planAttributes,
    layout: "aurora_compact",
  });
  const invalidOutput = invalidCard.shadowRoot?.innerHTML || "";
  check(
    /<div data-tone="rose"><span>Wybrane przez EMS<\/span><strong>—<\/strong><\/div>/.test(invalidOutput),
    "invalid canonical data must render EMS-selected energy as unavailable, never zero",
  );
});

scenario("automatic SOC aggregates complete canonical quarter-hours only", () => {
  const card = new RceChartCard();
  const startMs = Date.parse("2026-09-02T16:00:00Z");
  const midpointMs = startMs + 15 * 60 * 1000;
  const endMs = startMs + HALF_HOUR_MS;
  const group = { absolute: true, startMs, endMs };
  const canonical = {
    valid: true,
    slots: [
      {
        startMs,
        endMs: midpointMs,
        socStartPercent: 68,
        socEndPercent: 65,
        rceEnergyKwh: 0.5,
      },
      {
        startMs: midpointMs,
        endMs,
        socStartPercent: 65,
        socEndPercent: 63.5,
        rceEnergyKwh: 0.5,
      },
    ],
  };
  check(
    JSON.stringify(card._canonicalSocForGroup(canonical, group, TIME_ZONE))
      === JSON.stringify({ before: 68, after: 63.5 }),
    "automatic SOC must use the first start and final end across two continuous 15-minute canonical slots",
  );
  const gap = JSON.parse(JSON.stringify(canonical));
  gap.slots[1].startMs += 1000;
  check(
    card._canonicalSocForGroup(gap, group, TIME_ZONE) === null,
    "a gap in canonical SOC coverage must fail closed",
  );
  const discontinuity = JSON.parse(JSON.stringify(canonical));
  discontinuity.slots[1].socStartPercent = 66;
  check(
    card._canonicalSocForGroup(discontinuity, group, TIME_ZONE) === null,
    "a discontinuity between canonical SOC slots must fail closed",
  );
});

scenario("price history outside the current automation horizon stays unknown", () => {
  nowMs = Date.parse("2026-09-02T10:05:00Z");
  const todayRows = officialRows(
    "2026-09-02",
    "2026-09-01T22:00:00Z",
    "2026-09-02T22:00:00Z",
  );
  const tomorrowRows = officialRows(
    "2026-09-03",
    "2026-09-02T22:00:00Z",
    "2026-09-03T22:00:00Z",
    600,
  );
  const horizonStart = "2026-09-02T10:00:00Z";
  const horizonEnd = "2026-09-03T22:00:00Z";
  const card = makeCard({
    todayRows,
    tomorrowRows,
    timeline: timelineState(horizonStart, horizonEnd),
    canonical: canonicalState(horizonStart, horizonEnd),
    planAttributes: {
      result_current: true,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: 0.8,
      planned_export_kwh: 0,
      planned_revenue_pln: 0,
      gcf_execution_data_fresh: true,
      gcf_enabled: true,
      gcf_export_limit_percent: 100,
      planned_slots: [],
    },
    layout: "aurora_compact",
  });
  const historical = card._rceSamples[0];
  check(
    historical?.candidateExportKwh === null
      && historical?.selectedByEmsKwh === null
      && historical?.socBeforePercent === null
      && historical?.socAfterPercent === null,
    "a price-only historical interval must not invent candidate, EMS export, or automatic SOC",
  );
  check(
    historical?.stateLabel === "Poza bieżącym horyzontem planu"
      && card._rcePanelHtml(historical).includes("Niedostępne → Niedostępne"),
    "a price-only historical interval must explicitly report that it is outside the current plan horizon",
  );
});

scenario("spring DST day", () => {
  nowMs = Date.parse("2026-03-28T23:05:00Z");
  const card = makeCard({
    todayRows: officialRows(
      "2026-03-29",
      "2026-03-28T23:00:00Z",
      "2026-03-29T22:00:00Z",
    ),
    tomorrowRows: officialRows(
      "2026-03-30",
      "2026-03-29T22:00:00Z",
      "2026-03-30T22:00:00Z",
      600,
    ),
    timeline: timelineState("2026-03-28T23:00:00Z", "2026-03-30T22:00:00Z"),
    planAttributes: {
      result_current: true,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: null,
      planned_export_kwh: null,
      planned_revenue_pln: null,
      planning_scope: "today_and_tomorrow",
      tomorrow_data_pending: false,
      planned_slots: [],
    },
  });
  check(card._rceSamples.length === 94, "spring today+tomorrow must contain 46+48 half-hours");
  check(
    new Set(card._rceSamples.map((sample) => sample.key)).size === 94,
    "spring combined samples must stay unique across the skipped local hour",
  );
});

scenario("autumn DST fold", () => {
  nowMs = Date.parse("2026-10-24T22:05:00Z");
  const selectedFirstFold = new Map([
    [Date.parse("2026-10-25T00:00:00Z"), { energy: 0.5, revenue: 0.4, price: 0.8 }],
  ]);
  const card = makeCard({
    todayRows: officialRows(
      "2026-10-25",
      "2026-10-24T22:00:00Z",
      "2026-10-25T23:00:00Z",
    ),
    tomorrowRows: officialRows(
      "2026-10-26",
      "2026-10-25T23:00:00Z",
      "2026-10-26T23:00:00Z",
      600,
    ),
    timeline: timelineState(
      "2026-10-24T22:00:00Z",
      "2026-10-26T23:00:00Z",
      selectedFirstFold,
    ),
    planAttributes: {
      result_current: true,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: 0.8,
      planned_export_kwh: 0.5,
      planned_revenue_pln: 0.4,
      planning_scope: "today_and_tomorrow",
      tomorrow_data_pending: false,
      planned_slots: [
        { date: "2026-10-25", start: "02:00", end: "02:30", price: 0.8, energy: 0.5, revenue: 0.4 },
      ],
    },
  });
  check(card._rceSamples.length === 98, "autumn today+tomorrow must contain 50+48 half-hours");
  check(
    new Set(card._rceSamples.map((sample) => sample.key)).size === 98,
    "both autumn 02:xx folds must retain distinct absolute keys",
  );
  check(
    card._rceSamples.find((sample) => sample.startMs === Date.parse("2026-10-25T00:00:00Z"))?.selected === true
      && card._rceSamples.find((sample) => sample.startMs === Date.parse("2026-10-25T01:00:00Z"))?.selected === false,
    "an exact UTC timeline selection must mark only the intended repeated 02:00 fold",
  );
});

scenario("stale plan withdraws authority and totals", () => {
  nowMs = Date.parse("2026-09-01T22:05:00Z");
  const todayRows = officialRows(
    "2026-09-02",
    "2026-09-01T22:00:00Z",
    "2026-09-02T22:00:00Z",
  );
  const tomorrowRows = officialRows(
    "2026-09-03",
    "2026-09-02T22:00:00Z",
    "2026-09-03T22:00:00Z",
    600,
  );
  const selected = new Map([
    [Date.parse("2026-09-02T16:00:00Z"), { energy: 9.99, revenue: 8.88, price: 1.2 }],
  ]);
  const timeline = timelineState(
    "2026-09-01T22:00:00Z",
    "2026-09-03T22:00:00Z",
    selected,
  );
  const card = makeCard({
    todayRows,
    tomorrowRows,
    timeline,
    planAttributes: {
      result_current: false,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: 1.2,
      planned_export_kwh: 9.99,
      planned_revenue_pln: 8.88,
      planning_scope: "today_and_tomorrow",
      tomorrow_data_pending: false,
      planned_slots: [
        { date: "2026-09-02", start: "18:00", end: "18:30", price: 1.2, energy: 9.99, revenue: 8.88 },
      ],
    },
  });
  const output = card.shadowRoot?.innerHTML || "";
  check(
    card._rceSamples.every((sample) => !sample.planned && !sample.selected && !sample.retained),
    "a stale plan must not mark any current or retained sale block",
  );
  check(
    !output.includes("9,99 kWh") && !output.includes("8,88 PLN"),
    "a stale plan must not expose stale plan totals",
  );
});

scenario("pending plan retains display-only geometry", () => {
  nowMs = Date.parse("2026-09-01T22:05:00Z");
  const todayRows = officialRows("2026-09-02", "2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z");
  const tomorrowRows = officialRows("2026-09-03", "2026-09-02T22:00:00Z", "2026-09-03T22:00:00Z", 600);
  const selectedStart = Date.parse("2026-09-02T16:00:00Z");
  const timeline = timelineState(
    "2026-09-01T22:00:00Z",
    "2026-09-03T22:00:00Z",
    new Map([[selectedStart, { energy: 9.99, revenue: 8.88, price: 1.2 }]]),
  );
  timeline.state = "pending";
  timeline.attributes.result_current = false;
  timeline.attributes.recalculation_pending = true;
  const card = makeCard({
    todayRows,
    tomorrowRows,
    timeline,
    canonical: canonicalState(
      "2026-09-01T22:00:00Z",
      "2026-09-03T22:00:00Z",
      new Map([[selectedStart, { energy: 9.99, revenue: 8.88, price: 1.2 }]]),
    ),
    planAttributes: {
      result_current: true,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: 1.2,
      planned_export_kwh: 9.99,
      planned_revenue_pln: 8.88,
      planned_slots: [
        { date: "2026-09-02", start: "18:00", end: "18:30", price: 1.2, energy: 9.99, revenue: 8.88 },
      ],
    },
  });
  const output = card.shadowRoot?.innerHTML || "";
  const retained = card._rceSamples.filter((sample) => sample.retained);
  check(retained.length === 1 && retained[0].startMs === selectedStart, "pending must retain the exact old 30-minute geometry");
  check(retained.every((sample) => !sample.planned && !sample.selected), "retained geometry must never regain execution authority");
  check(output.includes("rce-pending-hatch") && output.includes("Przeliczanie — pokazano ostatni plan; brak prawa do nowego wykonania."), "pending geometry must use hatch and an explicit no-authority banner");
  check(!output.includes("9,99 kWh") && !output.includes("8,88 PLN") && !output.includes("1,2000 PLN/kWh"), "pending must not present old totals or threshold as current");
  check(
    retained.every(
      (sample) => sample.socBeforePercent === null && sample.socAfterPercent === null,
    ),
    "pending plan must withhold stale canonical SOC together with execution authority",
  );

  const compactCard = makeCard({
    todayRows,
    tomorrowRows,
    timeline,
    layout: "aurora_compact",
    planAttributes: {
      result_current: true,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: 1.2,
      planned_export_kwh: 9.99,
      planned_revenue_pln: 8.88,
      planned_slots: [
        { date: "2026-09-02", start: "18:00", end: "18:30", price: 1.2, energy: 9.99, revenue: 8.88 },
      ],
    },
  });
  const compactOutput = compactCard.shadowRoot?.innerHTML || "";
  check(
    compactOutput.includes('class="bar retained rce-price-bar')
      && compactOutput.includes("Przeliczanie — pokazano ostatni plan; brak prawa do nowego wykonania.")
      && !compactOutput.includes("9,99 kWh")
      && !compactOutput.includes("8,88 zł"),
    "compact pending view must retain only hatched geometry and withhold stale totals",
  );
});

scenario("incomplete half-hour is unavailable, never averaged", () => {
  nowMs = Date.parse("2026-09-01T22:05:00Z");
  const rows = officialRows("2026-09-02", "2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z");
  const missingStart = Date.parse("2026-09-02T00:15:00Z");
  const filtered = rows.filter((row) => Date.parse(row.dtime_utc) - QUARTER_MS !== missingStart);
  const card = makeCard({
    todayRows: filtered,
    tomorrowRows: [],
    timeline: timelineState("2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z"),
    planAttributes: { result_current: true, recalculation_pending: false, planned_slots: [] },
  });
  const incomplete = card._rceSamples.find((sample) => sample.startMs === Date.parse("2026-09-02T00:00:00Z"));
  check(card._rceSamples.length === 48, "one missing quarter must preserve the 48-block day geometry");
  check(incomplete?.complete === false && incomplete?.price === null, "one quarter must not be accepted as a 30-minute average");
  check((card.shadowRoot?.innerHTML || "").includes('class="bar unavailable'), "incomplete block must be visibly hatched as unavailable");

  const compactCard = makeCard({
    todayRows: filtered,
    tomorrowRows: [],
    timeline: timelineState("2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z"),
    planAttributes: { result_current: true, recalculation_pending: false, planned_slots: [] },
    layout: "aurora_compact",
  });
  const compactOutput = compactCard.shadowRoot?.innerHTML || "";
  check(
    compactOutput.includes('class="bar unavailable rce-price-bar')
      && (compactOutput.match(/<polyline class="rce-price-line"/g) || []).length === 2,
    "compact price line must break at an incomplete half-hour instead of interpolating it",
  );
});

scenario("execution label requires supervisor transaction and physical confirmation", () => {
  nowMs = Date.parse("2026-09-01T22:05:00Z");
  const todayRows = officialRows("2026-09-02", "2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z");
  const timeline = timelineState("2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z");
  const planAttributes = { result_current: true, recalculation_pending: false, planned_slots: [] };
  const legacyOnly = makeCard({ todayRows, tomorrowRows: [], timeline, planAttributes, legacyActive: true });
  check((legacyOnly.shadowRoot?.innerHTML || "").includes("marker legacy RCE") && !(legacyOnly.shadowRoot?.innerHTML || "").includes("RCE rozładowuje —"), "legacy helper alone must not claim execution");

  const confirmed = makeCard({
    todayRows,
    tomorrowRows: [],
    timeline,
    planAttributes,
    supervisor: state("executing", {
      execution_phase: "executing",
      selected_policy: "rce",
      selected_action: "rce_export",
      owner: "rce",
      transaction_owner: "rce",
      transaction_id: "tx-rce-1",
      physical_verification_result: "confirmed",
      supervisor_execution_authorized: true,
      owner_conflict: false,
      candidate_summaries: [],
    }),
  });
  check((confirmed.shadowRoot?.innerHTML || "").includes("Sprzedaż dynamiczna trwa — odczyt fizyczny potwierdzony"), "only the fully confirmed RCE transaction may claim execution");

  const unconfirmedExecution = makeCard({
    todayRows,
    tomorrowRows: [],
    timeline,
    planAttributes,
    supervisor: state("executing", {
      execution_phase: "executing",
      selected_policy: "rce",
      selected_action: "rce_export",
      owner: "rce",
      transaction_owner: "rce",
      transaction_id: "tx-rce-pending",
      physical_verification_result: "pending",
      supervisor_execution_authorized: false,
      owner_conflict: false,
      candidate_summaries: [],
    }),
  });
  const unconfirmedOutput = unconfirmedExecution.shadowRoot?.innerHTML || "";
  check(
    unconfirmedOutput.includes("Sprzedaż dynamiczna — wykonanie niepotwierdzone fizycznie")
      && !unconfirmedOutput.includes("Sprzedaż dynamiczna trwa — odczyt fizyczny potwierdzony")
      && !unconfirmedOutput.includes("Sprzedaż dynamiczna nie jest aktywna"),
    "an unconfirmed executing RCE transaction must remain waiting, never active or inactive",
  );

  const waiting = makeCard({
    todayRows,
    tomorrowRows: [],
    timeline,
    planAttributes,
    supervisor: state("waiting_readback", {
      execution_phase: "waiting_readback",
      selected_policy: "rce",
      owner: "rce",
      transaction_owner: "rce",
      owner_conflict: false,
      candidate_summaries: [],
    }),
  });
  check((waiting.shadowRoot?.innerHTML || "").includes("oczekiwanie na odczyt fizyczny"), "waiting_readback must be distinct from confirmed execution");

  const globallyBlocked = makeCard({
    todayRows,
    tomorrowRows: [],
    timeline,
    planAttributes,
    supervisor: state("selected", {
      execution_phase: "selected",
      selected_policy: "rce",
      owner: "none",
      transaction_owner: "none",
      owner_conflict: false,
      execution_blocked_reason: "critical_bms",
      candidate_summaries: [{ policy_id: "rce", blocked_reason: null }],
    }),
  });
  const blockedOutput = globallyBlocked.shadowRoot?.innerHTML || "";
  check(
    blockedOutput.includes("Sprzedaż dynamiczna zablokowana")
      && blockedOutput.includes("critical_bms")
      && !blockedOutput.includes("oczekiwanie na odczyt fizyczny"),
    "a global supervisor execution blocker must override selected/waiting RCE presentation",
  );

  const otherOwner = makeCard({
    todayRows,
    tomorrowRows: [],
    timeline,
    planAttributes,
    supervisor: state("executing", {
      execution_phase: "executing",
      selected_policy: "tariff",
      owner: "tariff",
      transaction_owner: "tariff",
      owner_conflict: false,
      candidate_summaries: [],
    }),
  });
  check((otherOwner.shadowRoot?.innerHTML || "").includes("Steruje: tariff"), "another execution owner must be named instead of claiming RCE activity");
});

scenario("tomorrow pending and missing values", () => {
  nowMs = Date.parse("2026-09-01T22:05:00Z");
  const card = makeCard({
    todayRows: officialRows(
      "2026-09-02",
      "2026-09-01T22:00:00Z",
      "2026-09-02T22:00:00Z",
    ),
    tomorrowRows: [],
    tomorrowState: state(0, { value: [] }),
    timeline: timelineState("2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z"),
    planAttributes: {
      result_current: true,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: null,
      planned_export_kwh: null,
      planned_revenue_pln: null,
      planning_scope: "today_only",
      tomorrow_data_pending: true,
      planned_slots: [],
    },
  });
  const output = card.shadowRoot?.innerHTML || "";
  check(card._rceSamples.length === 48, "missing tomorrow data must keep today's 48 half-hours visible");
  check(
    output.includes("Ceny na jutro nie są jeszcze opublikowane")
      || output.includes("Dane PSE na jutro nie są jeszcze opublikowane"),
    "combined card must explain that tomorrow data are pending",
  );
  check(card._number(null, 2) === "—", "null must render as unavailable, never numeric zero");
  check(card._number("", 2) === "—", "an empty value must render as unavailable, never numeric zero");

  const compactCard = makeCard({
    todayRows: officialRows(
      "2026-09-02",
      "2026-09-01T22:00:00Z",
      "2026-09-02T22:00:00Z",
    ),
    tomorrowRows: [],
    tomorrowState: state(0, { value: [] }),
    timeline: timelineState("2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z"),
    planAttributes: {
      result_current: true,
      recalculation_pending: false,
      automatic_price_floor_pln_kwh: null,
      planned_export_kwh: null,
      planned_revenue_pln: null,
      planned_slots: [],
    },
    layout: "aurora_compact",
  });
  const compactOutput = compactCard.shadowRoot?.innerHTML || "";
  check(
    compactCard._rceSamples.length === 48
      && compactOutput.includes("Ceny na jutro nie są jeszcze opublikowane")
      && /<div data-tone="amber"><span>Próg sprzedaży · auto<\/span><strong class="rce-daily-floor">—<\/strong><\/div>/.test(compactOutput),
    "compact view must keep today's prices and show tomorrow/threshold as unavailable",
  );
});

const dailyFloorLines = (output) => [...output.matchAll(/<line\b[^>]*data-rce-day-floor="([^"]+)"[^>]*>/g)]
  .map((match) => ({
    date: match[1],
    floor: Number(match[0].match(/data-rce-floor="([^"]+)"/)[1]),
    x1: Number(match[0].match(/x1="([^"]+)"/)[1]),
    x2: Number(match[0].match(/x2="([^"]+)"/)[1]),
  }));
const daySummary = (output, date) => output.match(new RegExp(`<div class="plan-day" data-rce-sale-day="${date}">([\\s\\S]*?)<\\/div>`))?.[1] || "";
function dailyCard(layout, selected, alter = () => {}) {
  const timeline = timelineState("2026-09-01T22:00:00Z", "2026-09-03T22:00:00Z", selected);
  const planAttributes = { result_current: true, recalculation_pending: false, automatic_price_floor_pln_kwh: 0.0123, planned_slots: [] };
  alter(timeline, planAttributes);
  return makeCard({
    todayRows: officialRows("2026-09-02", "2026-09-01T22:00:00Z", "2026-09-02T22:00:00Z"),
    tomorrowRows: officialRows("2026-09-03", "2026-09-02T22:00:00Z", "2026-09-03T22:00:00Z"),
    timeline, planAttributes, layout,
    canonical: canonicalState("2026-09-01T22:00:00Z", "2026-09-03T22:00:00Z"),
  });
}

scenario("daily floors stop at midnight in both renderers", () => {
  nowMs = Date.parse("2026-09-02T14:18:00Z");
  const selected = new Map([
    [Date.parse("2026-09-02T15:30:00Z"), { energy: 1.2, revenue: 0.72, price: 0.6 }],
    [Date.parse("2026-09-03T17:30:00Z"), { energy: 2, revenue: 1.8, price: 0.9 }],
  ]);
  for (const layout of ["standard", "aurora_compact"]) {
    const card = dailyCard(layout, selected);
    const output = card.shadowRoot.innerHTML;
    const lines = dailyFloorLines(output);
    check(lines.length === 2 && lines[0].floor === 0.6 && lines[1].floor === 0.9, `${layout}: independent floors must use the authoritative candidate slots`);
    check(lines[0]?.x2 === lines[1]?.x1 && lines[0]?.x1 < lines[0]?.x2 && lines[1]?.x1 < lines[1]?.x2, `${layout}: floor lines must meet at midnight without spanning the other day`);
    check(daySummary(output, "2026-09-02").includes("1,20 kWh") && daySummary(output, "2026-09-02").includes("0,72 zł"), `${layout}: today must have its own energy and revenue`);
    check(daySummary(output, "2026-09-03").includes("2,00 kWh") && daySummary(output, "2026-09-03").includes("1,80 zł"), `${layout}: tomorrow must have its own energy and revenue`);
    check(daySummary(output, "2026-09-02").includes("Wybrane przez EMS: 0,00 kWh"), `${layout}: candidate totals must not grant canonical EMS selection`);
  }
});

scenario("empty or pending daily plans never inherit a global floor", () => {
  nowMs = Date.parse("2026-09-02T14:18:00Z");
  const tomorrowOnly = new Map([[Date.parse("2026-09-03T17:30:00Z"), { energy: 2, revenue: 1.8, price: 0.9 }]]);
  for (const layout of ["standard", "aurora_compact"]) {
    const output = dailyCard(layout, tomorrowOnly).shadowRoot.innerHTML;
    const lines = dailyFloorLines(output);
    check(lines.length === 1 && lines[0].date === "2026-09-03", `${layout}: an empty today must not inherit tomorrow's or the global floor`);
    check(daySummary(output, "2026-09-02").includes("Brak planowanej sprzedaży"), `${layout}: current empty plan must be explicit`);
    const empty = dailyCard(layout, new Map()).shadowRoot.innerHTML;
    check(dailyFloorLines(empty).length === 0 && (empty.match(/Brak planowanej sprzedaży/g) || []).length === 2, `${layout}: two empty days must have no thresholds`);
    for (const missing of ["plan", "timeline"]) {
      const pending = dailyCard(layout, tomorrowOnly, (timeline, plan) => {
        if (missing === "plan") { plan.result_current = false; plan.recalculation_pending = true; }
        else { timeline.state = "pending"; timeline.attributes.result_current = false; timeline.attributes.recalculation_pending = true; }
      }).shadowRoot.innerHTML;
      check(dailyFloorLines(pending).length === 0 && !pending.includes("Brak planowanej sprzedaży"), `${layout}/${missing}: pending is unknown, not a current empty sale plan`);
      check(!daySummary(pending, "2026-09-03").includes("2,00 kWh"), `${layout}/${missing}: current and retained cohorts must not mix daily totals`);
    }
    const mismatch = dailyCard(layout, tomorrowOnly, (timeline, plan) => {
      plan.input_revision = timeline.attributes.input_revision + 1;
    }).shadowRoot.innerHTML;
    check(dailyFloorLines(mismatch).length === 0 && !daySummary(mismatch, "2026-09-03").includes("2,00 kWh"), `${layout}: mismatched current input revisions must not publish daily totals`);
  }
});

scenario("partial current sale uses remaining energy once and excludes ended slots", () => {
  nowMs = Date.parse("2026-09-02T14:18:00Z");
  const selected = new Map([
    [Date.parse("2026-09-02T13:30:00Z"), { energy: 9, revenue: 0.9, price: 0.1 }],
    [Date.parse("2026-09-02T14:00:00Z"), { energy: 0.4, revenue: 0.25, price: 0.625 }],
  ]);
  for (const layout of ["standard", "aurora_compact"]) {
    const card = dailyCard(layout, selected, (timeline) => {
      timeline.attributes.points.find((point) => point.start === "2026-09-02T14:00:00.000Z").start = new Date(nowMs).toISOString();
    });
    const output = card.shadowRoot.innerHTML;
    const lines = dailyFloorLines(output);
    check(lines.length === 1 && lines[0].floor === 0.625, `${layout}: ended cheap sale must not lower today's remaining floor`);
    check(daySummary(output, "2026-09-02").includes("0,40 kWh") && daySummary(output, "2026-09-02").includes("0,25 zł"), `${layout}: partial slot must neither disappear nor be prorated twice`);
    check(card._rceSamples.find((sample) => sample.startMs === Date.parse("2026-09-02T14:00:00Z"))?.selected === true, `${layout}: a partial timeline point must align to its original half-hour`);
  }
});

scenario("daily floors retain distinct DST repeated slots", () => {
  nowMs = Date.parse("2026-10-24T22:05:00Z");
  const selected = new Map([
    [Date.parse("2026-10-25T00:00:00Z"), { energy: 0.5, revenue: 0.4, price: 0.8 }],
    [Date.parse("2026-10-25T01:00:00Z"), { energy: 0.7, revenue: 0.42, price: 0.6 }],
    [Date.parse("2026-10-25T23:00:00Z"), { energy: 1, revenue: 0.9, price: 0.9 }],
  ]);
  for (const layout of ["standard", "aurora_compact"]) {
    const card = makeCard({
      todayRows: officialRows("2026-10-25", "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z"),
      tomorrowRows: officialRows("2026-10-26", "2026-10-25T23:00:00Z", "2026-10-26T23:00:00Z"),
      timeline: timelineState("2026-10-24T22:00:00Z", "2026-10-26T23:00:00Z", selected),
      planAttributes: { result_current: true, recalculation_pending: false }, layout,
    });
    const output = card.shadowRoot.innerHTML;
    const lines = dailyFloorLines(output);
    check(card._rceSamples.length === 98 && lines.length === 2 && lines[0].floor === 0.6 && lines[1].floor === 0.9, `${layout}: autumn day has 50 distinct slots and its own floor`);
    check(daySummary(output, "2026-10-25").includes("1,20 kWh") && daySummary(output, "2026-10-25").includes("0,82 zł"), `${layout}: both repeated 02:00 sales must be counted once`);
    check(lines.length === 2 && Math.abs((lines[0].x2 - lines[0].x1) / (lines[1].x2 - lines[1].x1) - 50 / 48) < 1e-8, `${layout}: midnight divider follows the 25-hour calendar day`);
  }
});

scenario("spring DST and local midnight use calendar dates rather than UTC or 24-hour offsets", () => {
  nowMs = Date.parse("2026-03-28T23:05:00Z");
  const selected = new Map([
    [Date.parse("2026-03-29T20:30:00Z"), { energy: 1, revenue: 0.6, price: 0.6 }],
    [Date.parse("2026-03-29T22:00:00Z"), { energy: 2, revenue: 1.8, price: 0.9 }],
  ]);
  const card = makeCard({
    todayRows: officialRows("2026-03-29", "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z"),
    tomorrowRows: officialRows("2026-03-30", "2026-03-29T22:00:00Z", "2026-03-30T22:00:00Z"),
    timeline: timelineState("2026-03-28T23:00:00Z", "2026-03-30T22:00:00Z", selected),
    planAttributes: { result_current: true, recalculation_pending: false }, layout: "aurora_compact",
  });
  const lines = dailyFloorLines(card.shadowRoot.innerHTML);
  check(card._rceSamples.length === 94 && lines[0]?.date === "2026-03-29" && lines[1]?.date === "2026-03-30", "spring: 46+48 slots must retain local today/tomorrow dates");
  check(lines.length === 2 && Math.abs((lines[0].x2 - lines[0].x1) / (lines[1].x2 - lines[1].x1) - 46 / 48) < 1e-8, "spring: floors must stop at the real 23-hour midnight");
  card.hass = { ...card._hass, language: "en" };
  check(card.shadowRoot.innerHTML.includes("Remaining sales plan") && card.shadowRoot.innerHTML.includes("Tomorrow"), "daily plan labels must also render in English");
});

scenario("missing repeated-hour point cannot borrow the other DST occurrence", () => {
  nowMs = Date.parse("2026-10-24T22:05:00Z");
  const first = Date.parse("2026-10-25T00:00:00Z");
  const second = Date.parse("2026-10-25T01:00:00Z");
  const timeline = timelineState("2026-10-24T22:00:00Z", "2026-10-26T23:00:00Z", new Map([[first, { energy: 0.5, revenue: 0.4, price: 0.8 }]]));
  timeline.attributes.points = timeline.attributes.points.filter((point) => Date.parse(point.start) !== second);
  const card = makeCard({
    todayRows: officialRows("2026-10-25", "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z"),
    tomorrowRows: officialRows("2026-10-26", "2026-10-25T23:00:00Z", "2026-10-26T23:00:00Z"),
    timeline, planAttributes: { result_current: true, recalculation_pending: false },
  });
  check(card._rceSamples.find((sample) => sample.startMs === second)?.selected === false, "missing second02:00 must not borrow first02:00's selected action");
  check(daySummary(card.shadowRoot.innerHTML, "2026-10-25").includes("0,50 kWh"), "DST missing point must not duplicate the remaining sale energy");
});

scenario("calendar rollover cannot relabel yesterday as today's remaining plan", () => {
  nowMs = Date.parse("2026-09-02T22:05:00Z");
  const selected = new Map([[Date.parse("2026-09-03T17:30:00Z"), { energy: 2, revenue: 1.8, price: 0.9 }]]);
  for (const layout of ["standard", "aurora_compact"]) {
    const output = dailyCard(layout, selected).shadowRoot.innerHTML;
    check(!daySummary(output, "2026-09-02") && daySummary(output, "2026-09-03").includes("2,00 kWh"), `${layout}: today is the current local date even before market entities roll over`);
    check(daySummary(output, "2026-09-04").includes("Niedostępne") && !daySummary(output, "2026-09-04").includes("Brak planowanej sprzedaży"), `${layout}: tomorrow without market data remains unavailable`);
  }
});

scenario("legacy single-day card retains its original threshold contract", () => {
  nowMs = Date.parse("2026-09-02T14:18:00Z");
  const card = dailyCard("standard", new Map(), (_timeline, plan) => {
    plan.planned_slots = [{ date: "2026-09-02", start: "18:00", end: "18:30", price: 0.55, energy: 1, revenue: 0.55 }];
  });
  card.setConfig({ ...config, tomorrow_entity: null });
  const output = card.shadowRoot.innerHTML;
  check(dailyFloorLines(output).length === 0 && !output.includes("data-rce-sale-day"), "legacy24h must not adopt the two-day presentation");
  check(output.includes("0,55 PLN/kWh"), "legacy24h must retain the threshold derived by its existing renderer");
});

scenario("consumable tariff margin retains manual-profile visibility beside daily RCE plans", () => {
  const SettingsCard = registry.get("hoymiles-aurora-variant-a-policy-settings-card");
  const settings = new SettingsCard();
  settings.setConfig({ section: "tariff", language: "pl" });
  check(
    settings._root.innerHTML.includes("Margines zapotrzebowania")
      && settings._root.innerHTML.includes("to zapas zużywalny, nie wyższa rezerwa SOC")
      && !settings._root.innerHTML.includes("Margines SOC"),
    "tariff settings must explain the accepted consumable energy-demand margin",
  );
  check(
    settings._root.innerHTML.includes("data-manual-tariff-only hidden"),
    "manual rates must start hidden while the operator state is not yet available",
  );
  const manualGroup = new FakeElement("section");
  const readiness = new FakeElement("span");
  const error = new FakeElement("p");
  const margin = new FakeElement("input");
  margin.dataset = {
    settingEntity: "input_number.hoymiles_tariff_soc_safety_margin",
    kind: "number",
  };
  manualGroup.hidden = true;
  settings._root.querySelector = (selector) => ({
    "[data-readiness]": readiness,
    "[data-error]": error,
  })[selector] || null;
  settings._root.querySelectorAll = (selector) => ({
    "[data-manual-tariff-only]": [manualGroup],
    "[data-setting-entity]": [margin],
  })[selector] || [];
  let serviceCalls = 0;
  for (const operator of ["PGE", "Manual", "TAURON", "Manual", "unavailable"]) {
    settings.hass = {
      language: "pl",
      states: {
        "input_select.hoymiles_tariff_operator": state(operator),
        "input_number.hoymiles_tariff_soc_safety_margin": state(10, {
          min: 0, max: 100, step: 1, unit_of_measurement: "%",
        }),
      },
      callService() { serviceCalls += 1; },
    };
    check(manualGroup.hidden === (operator !== "Manual"), `${operator}: manual rates visibility must follow the current operator`);
  }
  check(margin.value === "10" && !margin.disabled, "operator updates must retain the existing margin value and helper binding");
  check(serviceCalls === 0, "rendering tariff settings must not write helpers or change policy");
  const englishSettings = new SettingsCard();
  englishSettings.setConfig({ section: "tariff", language: "en" });
  check(
    englishSettings._root.innerHTML.includes("Energy-demand margin")
      && englishSettings._root.innerHTML.includes("consumable headroom, not a higher SOC reserve"),
    "English tariff settings must preserve the same energy-demand meaning",
  );
});

scenario("canonical status setup does not call an overview-only timer method", () => {
  const StatusCard = registry.get("hoymiles-aurora-status-card");
  const card = new StatusCard();
  let error = null;
  try { card.setConfig({}); } catch (caught) { error = caught; }
  check(error === null && card._config?.system_entity === "sensor.hoymiles_hit_overview_system_work_status",
    `status card setup must complete without a compatibility alias: ${error || ""}`);
});

scenario("canonical supervisor accepts null hass without an overview-only timer", () => {
  const SupervisorCard = registry.get("hoymiles-ems-supervisor-card");
  const card = new SupervisorCard();
  let error = null;
  try { card.hass = null; } catch (caught) { error = caught; }
  check(error === null && card._hass === null,
    `supervisor must accept an absent HA state without a compatibility alias: ${error || ""}`);
});

scenario("overview still cancels its own next-boundary timer on disconnect", () => {
  const OverviewCard = registry.get("hoymiles-aurora-overview-card");
  const card = new OverviewCard();
  const cleared = [];
  const originalClearTimeout = context.clearTimeout;
  context.clearTimeout = (handle) => cleared.push(handle);
  try {
    card._nextBoundaryTimer = 987;
    card.disconnectedCallback();
    check(card._nextBoundaryTimer === null && cleared.length === 1 && cleared[0] === 987,
      "removing invalid cross-class calls must preserve cleanup on the actual timer owner");
  } finally {
    context.clearTimeout = originalClearTimeout;
  }
});

scenario("canonical dashboard binding", () => {
  const lines = dashboardSource.split("\n");
  const starts = lines
    .map((line, index) => (line.includes("- type: custom:hoymiles-rce-chart-card") ? index : -1))
    .filter((index) => index >= 0);
  check(starts.length === 1, "canonical dashboard must contain one combined RCE chart card");
  const block = starts.length ? lines.slice(starts[0], starts[0] + 24).join("\n") : "";
  check(
    block.includes("entity: sensor.hoymiles_rce_day")
      && block.includes("tomorrow_entity: sensor.hoymiles_rce_day_tomorrow")
      && block.includes("plan_entity: sensor.hoymiles_hit_rce_optimized_plan")
      && block.includes("timeline_entity: sensor.hoymiles_hit_rce_automation_plan_timeline")
      && block.includes("supervisor_entity: sensor.hoymiles_hit_ems_supervisor")
      && block.includes("control_conflict_entity: binary_sensor.hoymiles_ems_control_conflict"),
    "combined RCE card must bind prices, plan, exact timeline and truthful execution evidence",
  );
});

scenario("PV hold is a separate pink window for RCE and Pstryk", () => {
  nowMs = Date.parse("2026-09-02T06:00:00Z");
  const begin = "2026-09-01T22:00:00Z", end = "2026-09-02T22:00:00Z";
  const rows = officialRows("2026-09-02", begin, end);
  const selected = new Map([[nowMs, {energy:0}], [nowMs + HALF_HOUR_MS, {energy:1}]]);
  const timeline = timelineState(begin, end, selected);
  const hold = timeline.attributes.points.find(p => Date.parse(p.start) === nowMs);
  hold.action_code = "pv_charge_hold";
  hold.pv_kw = 4; hold.load_kw = 1; hold.battery_kw = 0;
  hold.grid_kw = -3; hold.grid_export_kw = 3;
  const canonical = canonicalState(begin, end, selected);
  canonical.attributes.slots.find(s => Date.parse(s.starts_at) === nowMs).selected_action = "pv_charge_hold";
  const card = makeCard({todayRows:rows, tomorrowRows:[], timeline, canonical,
    planAttributes:{result_current:true,recalculation_pending:false,planned_export_kwh:1,planned_slots:[]},
    layout:"aurora_compact"});
  for (const pstryk of [false,true]) {
    card._isPstryk = () => pstryk;
    card._priceSources = () => [state(rows.length,{value:rows}),null];
    card._renderCombined();
    const sample = card._rceSamples.find(s => s.startMs === nowMs);
    const html = card.shadowRoot.innerHTML;
    check(sample?.pvChargeHold === true, "PV hold must be selected even with zero battery sale");
    check(sample?.pvExportKwh === 1.5, "PV energy must come from the qualified flow, not battery export");
    check(html.includes('bar pv-hold rce-price-bar'), "The price window must use the pink PV hold style");
    check(html.includes('rce-plan-interval pv-hold-interval'), "PV hold must have its own plan card");
    check(html.includes('data-action="pv_charge_hold"'), "The PV window must retain its action identity");
    check(html.includes("magazyn") || html.includes("Magazyn"), "The window must explain deferred battery charging");
    check(sample.selectedByEmsKwh === 0, "PV export must not increase battery-sale totals");
  }
});

if (failures.length) {
  console.error(`RCE 48H UI CONTRACT: RED (${failures.length}/${checks})`);
  failures.forEach((failure) => console.error(`- ${failure}`));
  process.exitCode = 1;
} else {
  console.log(`RCE 48H UI CONTRACT: GREEN (${checks} checks)`);
}
