const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const EXPECTED_GROUP_COUNT = 65;
const EXPECTED_CHECK_COUNT = 913;

const root = path.resolve(process.env.HOYMILES_UI_TEST_ROOT || process.cwd());
const read = (relativePath) =>
  fs.readFileSync(path.join(root, relativePath), "utf8");
const digest = (relativePath) =>
  crypto
    .createHash("sha256")
    .update(fs.readFileSync(path.join(root, relativePath)))
    .digest("hex");
const sourceSegmentDigest = (source, startMarker, endMarker) => {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  if (start < 0 || end <= start) return null;
  return crypto
    .createHash("sha256")
    .update(source.slice(start, end))
    .digest("hex");
};

const cardPath = "home_assistant/www/hoymiles-rce-chart-card.js";
const strategyPath = "home_assistant/www/hoymiles-dashboard-strategy.js";
const dashboardPath = "dashboard_hoymiles.yaml";
const cardSource = read(cardPath);
const strategySource = read(strategyPath);
const dashboardSource = read(dashboardPath);
const componentStart = cardSource.indexOf("const HOYMILES_SUPERVISOR_BINDINGS");
const componentEnd = cardSource.indexOf("class HoymilesAuroraEnergyCard");
const componentSource = cardSource.slice(componentStart, componentEnd);
const supervisorCssStart = cardSource.indexOf("const HOYMILES_EMS_SUPERVISOR_CSS");
const supervisorCssEnd = cardSource.indexOf(
  "class HoymilesEmsSupervisorPanel",
  supervisorCssStart,
);
const supervisorCss = cardSource.slice(supervisorCssStart, supervisorCssEnd);

const EXPECTED_BINDINGS = Object.freeze({
  supervisor_entity: "sensor.hoymiles_hit_ems_supervisor",
  supervisor_mode_entity: "input_select.hoymiles_ems_supervisor_mode",
  supervisor_profile_entity: "input_select.hoymiles_ems_supervisor_profile",
  supervisor_allow_rce_entity:
    "input_boolean.hoymiles_ems_supervisor_allow_rce",
  supervisor_allow_tariff_entity:
    "input_boolean.hoymiles_ems_supervisor_allow_tariff",
  supervisor_allow_rcm_entity:
    "input_boolean.hoymiles_ems_supervisor_allow_rcm",
});
const EXPECTED_MODE_OPTIONS = Object.freeze(["Off", "Shadow"]);
const EXPECTED_PROFILE_OPTIONS = Object.freeze([
  "Balanced",
  "Maximum Profit",
  "High Reserve — Winter",
]);
const EXPECTED_POLICY_IDS = Object.freeze(["rce", "tariff", "rcm"]);
const EXPECTED_CONTROL_TARGETS = Object.freeze({
  mode: "input_select.hoymiles_ems_supervisor_mode",
  profile: "input_select.hoymiles_ems_supervisor_profile",
  allowRce: "input_boolean.hoymiles_ems_supervisor_allow_rce",
  allowTariff: "input_boolean.hoymiles_ems_supervisor_allow_tariff",
  allowRcm: "input_boolean.hoymiles_ems_supervisor_allow_rcm",
});

const EXPECTED_REASON_COPY = Object.freeze({
  candidate_ready: ["Kandydat gotowy", "Candidate ready"],
  live_emergency: ["Pilna reakcja na stan bieżący", "Live emergency response"],
  required_energy_restore: ["Wymagane odtworzenie energii", "Required energy restoration"],
  preventive_voltage_action: ["Prewencyjna reakcja napięciowa", "Preventive voltage action"],
  economic_candidate: ["Kandydat ekonomiczny", "Economic candidate"],
  no_action: ["Brak wymaganej akcji", "No action required"],
  no_eligible_candidate: ["Brak kwalifikującej się polityki", "No eligible policy"],
  not_allowed: ["Brak zgody użytkownika", "Not allowed by user"],
  policy_disabled: ["Istniejąca automatyka wyłączona", "Existing automation disabled"],
  unavailable: ["Dane niedostępne", "Data unavailable"],
  stale_candidate: ["Dane kandydata są nieaktualne", "Candidate data is stale"],
  future_candidate: ["Dane kandydata pochodzą z przyszłości", "Candidate data is future-dated"],
  not_started: ["Okno jeszcze się nie rozpoczęło", "Window has not started"],
  result_not_current: ["Plan nie jest aktualny", "Plan is not current"],
  recalculation_pending_new_start: ["Oczekiwanie na przeliczenie przed nowym startem", "Waiting for recalculation before a new start"],
  expired: ["Okno wygasło", "Window expired"],
  invalid_input: ["Nieprawidłowe dane wejściowe", "Invalid input"],
  invalid_policy_shape: ["Nieprawidłowa struktura polityki", "Invalid policy structure"],
  invalid_action_scope: ["Nieprawidłowy zakres akcji", "Invalid action scope"],
  actuator_unavailable: ["Wymagany element wykonawczy niedostępny", "Required actuator unavailable"],
  direction_unavailable: ["Kierunek działania niedostępny", "Action direction unavailable"],
  confirmed_zero_export: ["Potwierdzony zerowy eksport", "Confirmed zero export"],
  export_prohibited: ["Eksport zabroniony", "Export prohibited"],
  export_unverified: ["Eksport niezweryfikowany", "Export unverified"],
  local_hard_stop: ["Lokalna blokada bezpieczeństwa", "Local hard stop"],
  not_start_eligible: ["Warunki startu niespełnione", "Start conditions not met"],
  not_continuation_eligible: ["Warunki kontynuacji niespełnione", "Continuation conditions not met"],
  external_authority: ["Zewnętrzna automatyka ma pierwszeństwo", "External authority has control"],
  manual_authority: ["Sterowanie ręczne ma pierwszeństwo", "Manual authority has control"],
  off_grid: ["Tryb Off-Grid ma pierwszeństwo", "Off-grid has priority"],
  foreign_owner: ["Steruje inny właściciel", "Another owner has control"],
  balancing_active: ["Trwa wyrównywanie baterii", "Battery balancing is active"],
  owner_conflict: ["Konflikt właściciela sterowania", "Control-owner conflict"],
  transaction_pending: ["Oczekiwanie na zakończenie transakcji", "Waiting for transaction completion"],
  physical_mode_stale: ["Fizyczny tryb jest nieaktualny", "Physical mode is stale"],
  physical_mode_unknown: ["Fizyczny tryb jest nieznany", "Physical mode is unknown"],
  critical_bms_unavailable: ["Krytyczne dane BMS niedostępne", "Critical BMS data unavailable"],
  economic_candidates_not_comparable: ["Kandydatów ekonomicznych nie można porównać", "Economic candidates are not comparable"],
  economic_tie: ["Remis kandydatów ekonomicznych", "Economic candidates are tied"],
  active_not_implemented: ["Tryb Active nie jest zaimplementowany", "Active mode is not implemented"],
  structurally_inconsistent_context: ["Niespójny kontekst wykonania", "Structurally inconsistent execution context"],
  invalid_pending_owner_relationship: ["Nieprawidłowa relacja oczekującej transakcji z właścicielem", "Invalid pending-transaction owner relationship"],
  multiple_active_commitments: ["Wiele aktywnych zobowiązań", "Multiple active commitments"],
  owner_commitment_mismatch: ["Właściciel nie odpowiada aktywnemu zobowiązaniu", "Owner does not match the active commitment"],
  inconsistent_priority_tie: ["Niespójny remis priorytetów", "Inconsistent priority tie"],
});

const EXPECTED_LABELS = Object.freeze({
  pl: Object.freeze({
    title: "Nadzorca EMS",
    observationOnly: "Tylko obserwacja",
    mode: "Tryb",
    profile: "Profil",
    allowRce: "Uwzględniaj RCE",
    allowTariff: "Uwzględniaj tanie ładowanie",
    allowRcm: "Uwzględniaj RCEm",
    state: "Stan",
    selectedPolicy: "Wybrana polityka",
    decisionReason: "Powód decyzji",
    blockedReason: "Powód blokady",
    phase: "Faza",
    physicalExecution: "Fizyczne wykonanie",
    existingAutomation: "Istniejąca automatyka",
  }),
  en: Object.freeze({
    title: "EMS Supervisor",
    observationOnly: "Observation only",
    mode: "Mode",
    profile: "Profile",
    allowRce: "Consider RCE",
    allowTariff: "Consider tariff charging",
    allowRcm: "Consider RCEm",
    state: "State",
    selectedPolicy: "Selected policy",
    decisionReason: "Decision reason",
    blockedReason: "Blocked reason",
    phase: "Phase",
    physicalExecution: "Physical execution",
    existingAutomation: "Existing automation",
  }),
});
const EXPECTED_COPY_SHA256 = Object.freeze({
  pl: "5b039fe8eed77bb7ffcb116ebac4fb5362047c45356f6abe8eb5e04ef57e9f66",
  en: "d40dceefa5352b5d9cce2a74cf8da287ee73966ce54bb549617da8ecd45f0e79",
});
const EXPECTED_FUNCTIONAL_SEGMENTS = Object.freeze({
  controlMap: Object.freeze([
    "const HOYMILES_SUPERVISOR_CONTROL_KINDS",
    "const HOYMILES_SUPERVISOR_REASON_COPY",
    "6ba389eaf4a3ea27e53cb48568b9616455df4bd8ae8e61cb5d02499a8b357e01",
  ]),
  services: Object.freeze([
    "  async _selectOption(key, option)",
    "class HoymilesEmsSupervisorCard extends HTMLElement",
    "4553fd128d735153b7279b1337a34d67b794f417896d029aee3e7de6edd46916",
  ]),
  lifecycle: Object.freeze([
    "  _listen(element, type, listener)",
    "  _clearRequestOwnership()",
    "a3d9aa3dea3d10d90a1b58ab27a2870cd4e128966dab938f66b4f1f8f6fe8993",
  ]),
});

const PROTECTED_HASHES = Object.freeze({
  "custom_components/hoymiles_hit_modbus/ems_supervisor.py": "031d0abf8948d24708b960deb6ee71c7fe952fdebecd915ac6fc766596429ca7",
  "custom_components/hoymiles_hit_modbus/supervisor_runtime.py": "12cf54cb8baeb8a161b4a6b49ac19f91daeb46beef3f949d9782f689f3889d7d",
  "custom_components/hoymiles_hit_modbus/supervisor_sensor.py": "1c18ac1eef5d46e2512574ea3dfc7f0c4bd62c58aae6296b2e7b4c479ba743f8",
  "custom_components/hoymiles_hit_modbus/sensor.py": "fe83d62990150145c3db595e4983f598752751d2488375dd938db961908311bf",
  "custom_components/hoymiles_hit_modbus/const.py": "eee0ffebe1197f0f74b488bce4c7e51066a33b96255944672f3fee288fc1d956",
  "home_assistant/hoymiles_ems_scheduler.yaml": "b66b591372c654424c491206e61815bc5457eca8514f4c1cffc5f205c7367691",
  "custom_components/hoymiles_hit_modbus/resources/home_assistant/en/hoymiles_ems_scheduler.yaml": "3df7345f0ee9649a35160b4817d6d3dd9d0c95ecf93eed2bf07ec1fe2633886a",
  "custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/hoymiles_ems_scheduler.yaml": "b66b591372c654424c491206e61815bc5457eca8514f4c1cffc5f205c7367691",
  "custom_components/hoymiles_hit_modbus/tariff_optimizer.py": "7a77f3885a9f393179d40777770de5eee4ac1918b84a0dba5433531ccc344459",
  "custom_components/hoymiles_hit_modbus/tariff_sensor.py": "f90a54afd2e9f001ad466bb55941e480bbb6dc25f0b2b2c37d7c30fbcfe39ff5",
  "custom_components/hoymiles_hit_modbus/rce_optimizer.py": "f95ca95d8290995016ced33f12a9feec8306ca7e6bf224da385c956774866870",
  "custom_components/hoymiles_hit_modbus/rce_sensor.py": "d2401157dc90ba76069d24cd7d4bdc7ee15947c5173efe209cf986d8e88fef98",
  "custom_components/hoymiles_hit_modbus/rcm_optimizer.py": "ca533110396a2d24c9bf99cc73bb2e8843f782e53b914044dea83c60715b410b",
  "custom_components/hoymiles_hit_modbus/rcm_sensor.py": "5a66f6cdf6eae5a07b877db49c72e498ac65427dace0f3e774b3339363a3a087",
  "custom_components/hoymiles_hit_modbus/translations/en.json": "bed5940eb2d7157e2e8454e2ff305fe8a5b9d86fef8de5a23da410df3d18a1a1",
  "custom_components/hoymiles_hit_modbus/translations/pl.json": "8fe17a4fa44a052620f51f05bd94b07d2803f13a1e39a23775b4043e806b67fe",
  "custom_components/hoymiles_hit_modbus/entity_catalog.json": "39f8187b24fb16f89ff15f871d8e3ce27e6952f4b6196b3ed754f4b7c4989dc3",
  "custom_components/hoymiles_hit_modbus/manifest.json": "74ab0e7b8f8e1189d586a5f904e5bf51078345db640fb57bd9900a01aa46dff3",
});

class FakeStyle {
  constructor() {
    this.values = new Map();
  }
  setProperty(name, value) {
    this.values.set(name, String(value));
  }
}

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = new FakeStyle();
    this.className = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.selected = false;
    this.value = "";
  }
  append(...children) {
    this.children.push(...children);
    for (const child of children) {
      if (child && typeof child === "object") child.parentNode = this;
    }
  }
  prepend(...children) {
    this.children.unshift(...children);
  }
  replaceChildren(...children) {
    this.children = [...children];
    for (const child of children) {
      if (child && typeof child === "object") child.parentNode = this;
    }
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  getAttribute(name) {
    return this.attributes.get(name) ?? null;
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
  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }
  dispatch(type, init = {}) {
    if (this.disabled && ["pointerdown", "pointerup", "click", "keydown", "keyup"].includes(type)) {
      return false;
    }
    const event = {
      type,
      target: this,
      currentTarget: this,
      key: init.key,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
    if (this.tagName === "BUTTON" && type === "keydown" && init.key === "Enter") {
      this.click();
    }
    if (this.tagName === "BUTTON" && type === "keyup" && init.key === " ") {
      this.click();
    }
    return !event.defaultPrevented;
  }
  click() {
    if (this.disabled) return false;
    return this.dispatch("click");
  }
  querySelector() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
}

class TestHTMLElement {
  constructor() {
    this.isConnected = true;
  }
  attachShadow() {
    this.shadowRoot = new FakeElement("shadow-root");
    return this.shadowRoot;
  }
  dispatchEvent() {
    return true;
  }
}

const registry = new Map();
const context = {
  console,
  Date,
  Event: class {},
  CustomEvent: class {},
  HTMLElement: TestHTMLElement,
  Intl,
  URL,
  document: {
    documentElement: { lang: "en" },
    createElement: (tagName) => new FakeElement(tagName),
    createTextNode: (value) => ({ nodeType: 3, textContent: String(value) }),
  },
  window: { customCards: [] },
  customElements: {
    define(name, constructor) {
      if (!registry.has(name)) registry.set(name, constructor);
    },
    get(name) {
      return registry.get(name);
    },
    async whenDefined() {},
  },
  fetch: async () => ({ ok: true, json: async () => ({ views: [] }) }),
};
context.globalThis = context;
const executableSource = cardSource.replaceAll(
  "import.meta.url",
  JSON.stringify("https://homeassistant.example/local/hoymiles-rce-chart-card.js?v=1.5.7.28"),
) + `
globalThis.__supervisorTestExports = {
  HoymilesEmsSupervisorPanel,
  HoymilesEmsSupervisorCard,
  HoymilesAuroraEnergyCard,
  hoymilesNormalizeSupervisor,
  hoymilesNormalizeLanguage,
  hoymilesSupervisorReason,
  HOYMILES_SUPERVISOR_BINDINGS,
  HOYMILES_SUPERVISOR_MODE_OPTIONS,
  HOYMILES_SUPERVISOR_PROFILE_OPTIONS,
  HOYMILES_SUPERVISOR_POLICY_IDS,
  HOYMILES_SUPERVISOR_POLICY_ICONS,
  HOYMILES_SUPERVISOR_REASON_COPY,
  HOYMILES_SUPERVISOR_COPY,
  HOYMILES_EMS_SUPERVISOR_CSS,
};`;
vm.runInNewContext(executableSource, context, { filename: cardPath });
const ui = context.__supervisorTestExports;

let groupCount = 0;
let checkCount = 0;
const groupResults = [];
function check(condition, message) {
  checkCount += 1;
  if (!condition) throw new Error(message);
}
async function group(name, body) {
  groupCount += 1;
  await body();
  groupResults.push(name);
}
function equal(actual, expected, message) {
  check(JSON.stringify(actual) === JSON.stringify(expected), `${message}: ${JSON.stringify(actual)}`);
}
function doesNotThrow(body, message) {
  try {
    body();
    check(true, message);
  } catch (error) {
    check(false, `${message}: ${error.message}`);
  }
}

function walk(root) {
  const nodes = [];
  const visit = (node) => {
    if (!node || typeof node !== "object") return;
    nodes.push(node);
    for (const child of node.children || []) visit(child);
  };
  visit(root);
  return nodes;
}
function nodesWithClass(root, className) {
  return walk(root).filter((node) =>
    String(node.className || "").split(/\s+/).includes(className),
  );
}

function candidate(policyId, overrides = {}) {
  const actions = {
    rce: "rce_export",
    tariff: "tariff_charge",
    rcm: "rcm_absorb_pv",
  };
  return {
    policy_id: policyId,
    allowed_by_user: true,
    enabled: true,
    available: true,
    result_current: true,
    recalculation_pending: false,
    active_latched: false,
    start_eligible: true,
    continuation_eligible: false,
    requested_action: actions[policyId],
    reason_code: "candidate_ready",
    blocked_reason: null,
    rejection_reason: null,
    local_hard_stop: false,
    ...overrides,
  };
}

function supervisorAttributes(overrides = {}) {
  return {
    supervisor_mode: "Shadow",
    profile: "Balanced",
    execution_phase: "idle",
    selected_policy: null,
    selection_reason: "no_eligible_candidate",
    execution_blocked_reason: null,
    supervisor_execution_authorized: false,
    legacy_execution_unchanged: true,
    profile_effects_applied: [],
    candidate_summaries: [candidate("rcm"), candidate("rce"), candidate("tariff")],
    ...overrides,
  };
}

function hassFixture(state = "shadow_idle", attributeOverrides = {}, helperOverrides = {}) {
  const states = {
    [EXPECTED_BINDINGS.supervisor_entity]: {
      state,
      attributes: supervisorAttributes(attributeOverrides),
    },
    [EXPECTED_BINDINGS.supervisor_mode_entity]: {
      state: "Shadow",
      attributes: { options: ["Off", "Shadow"] },
    },
    [EXPECTED_BINDINGS.supervisor_profile_entity]: {
      state: "Balanced",
      attributes: { options: [...EXPECTED_PROFILE_OPTIONS] },
    },
    [EXPECTED_BINDINGS.supervisor_allow_rce_entity]: {
      state: "on",
      attributes: { editable: false, friendly_name: "Supervisor RCE", icon: "mdi:chart-line" },
    },
    [EXPECTED_BINDINGS.supervisor_allow_tariff_entity]: {
      state: "off",
      attributes: { editable: false, friendly_name: "Supervisor tariff", icon: "mdi:clock-outline" },
    },
    [EXPECTED_BINDINGS.supervisor_allow_rcm_entity]: {
      state: "on",
      attributes: { editable: false, friendly_name: "Supervisor RCEm", icon: "mdi:transmission-tower" },
    },
    ...helperOverrides,
  };
  return {
    language: "en",
    states,
    calls: [],
    callService(domain, service, data) {
      this.calls.push({ domain, service, data });
      return Promise.resolve();
    },
  };
}

function panelFixture(
  language = "en",
  hass = hassFixture(),
  config = { ...EXPECTED_BINDINGS },
) {
  const container = new FakeElement("section");
  const panel = new ui.HoymilesEmsSupervisorPanel(container);
  panel.connect();
  panel.update(hass, config, language);
  return { container, panel, hass };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function parentFixture(hass = hassFixture(), language = "en") {
  const parent = new ui.HoymilesEmsSupervisorCard();
  parent.setConfig({ ...EXPECTED_BINDINGS, language });
  parent.hass = hass;
  const panel = parent._panelController;
  const container = panel._container;
  return { parent, panel, container, hass };
}

function renderState(state, overrides = {}, language = "en") {
  const hass = hassFixture(state, overrides);
  return panelFixture(language, hass).panel;
}

async function main() {
  await group("01 exact source and helper entities", async () => {
    equal(JSON.parse(JSON.stringify(ui.HOYMILES_SUPERVISOR_BINDINGS)), EXPECTED_BINDINGS, "binding map");
    for (const [key, entityId] of Object.entries(EXPECTED_BINDINGS)) {
      check(dashboardSource.includes(`        ${key}: ${entityId}`), `dashboard binding ${key}`);
      check(componentSource.includes(JSON.stringify(entityId)), `component binding ${entityId}`);
    }
  });

  await group("02 no Active option or control", async () => {
    equal(Array.from(ui.HOYMILES_SUPERVISOR_MODE_OPTIONS), EXPECTED_MODE_OPTIONS, "mode whitelist");
    check(!/\["Off",\s*"Shadow",\s*"Active"\]/.test(componentSource), "no Active in mode whitelist");
    const { panel } = panelFixture();
    check(panel._controls.mode.element.children.every((option) => option.value !== "Active"), "no rendered Active option");
    const activeDocs = walk(panel._container).filter(
      (node) => node.getAttribute?.("aria-disabled") === "true",
    );
    check(activeDocs.length === 1 && activeDocs[0].tagName === "ARTICLE", "one disabled Active documentation card");
    check(activeDocs[0].getAttribute("tabindex") === null, "Active docs have no tabindex");
    check([...activeDocs[0].listeners.values()].every((listeners) => listeners.length === 0), "Active docs have no listener");
    check(!walk(activeDocs[0]).some((node) => ["BUTTON", "A"].includes(node.tagName)), "Active docs contain no action element");
  });

  await group("03 allowed helper services only", async () => {
    const { panel, hass } = panelFixture();
    panel._controls.mode.element.value = "Off";
    panel._controls.mode.element.dispatch("change");
    panel._controls.profile.element.value = "Maximum Profit";
    panel._controls.profile.element.dispatch("change");
    panel._controls.allowRce.element.dispatch("pointerdown");
    panel._controls.allowRce.element.dispatch("pointerup");
    panel._controls.allowRce.element.click();
    panel._controls.allowTariff.element.click();
    panel._controls.allowRcm.element.click();
    await Promise.resolve();
    equal(hass.calls.map(({ domain, service }) => [domain, service]), [
      ["input_select", "select_option"],
      ["input_select", "select_option"],
      ["input_boolean", "turn_off"],
      ["input_boolean", "turn_on"],
      ["input_boolean", "turn_off"],
    ], "allowed service sequence");
    check(Object.keys(hass.calls[0].data).sort().join(",") === "entity_id,option", "select data keys");
    check(Object.keys(hass.calls[2].data).join(",") === "entity_id", "boolean data keys");
    check(hass.states[EXPECTED_BINDINGS.supervisor_allow_rce_entity].attributes.editable === false, "editable=false does not suppress actual click");
    const guarded = panelFixture();
    guarded.hass.states[EXPECTED_BINDINGS.supervisor_mode_entity].attributes.options = ["Shadow"];
    await guarded.panel._callHelperService("mode", "input_select", "select_option", "supervisor_mode_entity", "Off");
    check(guarded.hass.calls.length === 0, "service boundary rechecks real helper options");

    for (const [key, eventType, eventInit] of [
      ["allowRce", "keydown", { key: "Enter" }],
      ["allowTariff", "keyup", { key: " " }],
    ]) {
      const keyboard = panelFixture();
      keyboard.panel._controls[key].element.dispatch(eventType, eventInit);
      await Promise.resolve();
      check(keyboard.hass.calls.length === 1, `${key} native keyboard activation emits one call`);
    }

    for (const key of ["allowRce", "allowTariff", "allowRcm"]) {
      const transition = panelFixture();
      transition.hass.callService = (domain, service, data) => {
        transition.hass.calls.push({ domain, service, data });
        transition.hass.states[data.entity_id].state = service === "turn_on" ? "on" : "off";
        return Promise.resolve();
      };
      const control = transition.panel._controls[key].element;
      const before = control.getAttribute("aria-checked");
      control.click();
      await new Promise((resolve) => setImmediate(resolve));
      transition.panel.update(transition.hass, transition.panel._config, "en");
      check(transition.hass.calls.length === 1, `${key} actual element emits exactly one HA service`);
      check(control.getAttribute("aria-checked") !== before, `${key} exact helper transition is rendered`);
      check(control.getAttribute("aria-busy") === "false", `${key} busy clears after transition`);
    }
  });

  await group("04 no inverter Modbus or legacy services", async () => {
    for (const forbidden of [
      "number.set_value",
      "select.select_option",
      "button.press",
      "input_boolean.toggle",
      "hoymiles_hit_modbus",
      "modbus.write",
      "legacy_enable",
    ]) {
      check(!componentSource.includes(`\"${forbidden}\"`), `forbidden service ${forbidden}`);
    }
    check(!/callService\([^)]*(owner|grant|handover)/i.test(componentSource), "no authority service call");
  });

  await group("05 dedicated view second and Start restored", async () => {
    const viewMatches = [...dashboardSource.matchAll(/^  - title: (.+)$/gm)];
    check(viewMatches[0]?.[1] === "Start", "Start remains first");
    check(viewMatches[1]?.[1] === "Nadzorca EMS", "Supervisor is second");
    check(viewMatches[2]?.[1] === "RCE i Wyniki", "RCE remains third");
    const startView = dashboardSource.slice(viewMatches[0].index, viewMatches[1].index);
    const supervisorView = dashboardSource.slice(viewMatches[1].index, viewMatches[2].index);
    check(!/supervisor_(entity|mode_entity|profile_entity|allow_)/.test(startView), "Start has no Supervisor bindings");
    check(supervisorView.includes("    path: ems-supervisor"), "dedicated path");
    check(supervisorView.includes("    icon: mdi:eye-circle-outline"), "dedicated icon");
    check(supervisorView.includes("    type: panel"), "dedicated panel type");
    check((supervisorView.match(/custom:hoymiles-ems-supervisor-card/g) || []).length === 1, "one dedicated card");
    const energySource = cardSource.slice(componentEnd, cardSource.indexOf("class HoymilesPowerFlowCard", componentEnd));
    const aurora = energySource.indexOf('<div class="aurora">');
    const daily = energySource.indexOf('<div class="daily">', aurora);
    check(aurora >= 0 && daily > aurora, "Start DOM retains aurora then daily");
    check(!energySource.includes("data-supervisor"), "Start has no inline host or placeholder");
    check(!energySource.includes("_supervisorPanel"), "Start has no Supervisor lifecycle reference");
  });

  await group("06 standalone component registered exactly once", async () => {
    check(componentSource.includes("class HoymilesEmsSupervisorPanel"), "internal class exists");
    check(!cardSource.includes('customElements.define("hoymiles-ems-supervisor-panel"'), "no standalone registration");
    check(componentSource.includes("class HoymilesEmsSupervisorCard extends HTMLElement"), "standalone class exists");
    check((componentSource.match(/customElements\.define\(\s*"hoymiles-ems-supervisor-card"/g) || []).length === 1, "one custom-element definition");
    check(registry.get("hoymiles-ems-supervisor-card") === ui.HoymilesEmsSupervisorCard, "registered constructor exact");
    check(context.window.customCards.filter((entry) => entry.type === "hoymiles-ems-supervisor-card").length === 1, "one custom-card metadata entry");
    const standalone = parentFixture();
    check(standalone.parent.shadowRoot.children.length === 2, "standalone shadow root contains style and one card");
    check(standalone.parent.shadowRoot.children[1].tagName === "HA-CARD", "standalone uses one ha-card");
    check(walk(standalone.parent.shadowRoot).filter((node) => node.tagName === "H1").length === 1, "standalone has exactly one h1");
    const hostile = new ui.HoymilesEmsSupervisorCard();
    hostile.setConfig({ supervisor_allow_rce_entity: "input_boolean.hoymiles_rce_discharge_enabled" });
    check(hostile._config.supervisor_allow_rce_entity === EXPECTED_BINDINGS.supervisor_allow_rce_entity, "standalone forces canonical closed target");
  });

  await group("07 permanent observation badge", async () => {
    for (const [state, overrides] of [
      ["off", {}],
      ["shadow_idle", {}],
      ["shadow_selected", { selected_policy: "rce" }],
      ["blocked", {}],
      ["unavailable", {}],
    ]) {
      const panel = renderState(state, overrides);
      check(panel._scope.textContent === "Observation only", `badge for ${state}`);
      check(panel._scope.hidden === false, `badge visible for ${state}`);
    }
  });

  await group("08 profile limitation preserved in knowledge", async () => {
    const { panel } = panelFixture("pl");
    const limitation = panel._copyNodes.find(([, key]) => key === "profileLimitation");
    check(limitation?.[0].textContent === "Profile wpływają obecnie wyłącznie na decyzję obserwacyjną, a ich skutki fizyczne nie są jeszcze stosowane.", "Polish limitation preserved");
    const knowledgeDetails = walk(panel._container).filter(
      (node) => node.tagName === "DETAILS",
    );
    check(knowledgeDetails.length === 5, "limitation lives in bounded five-detail knowledge area");
    const unverified = renderState("shadow_idle", { profile_effects_applied: ["unexpected"] }, "en");
    check(unverified._technical.profileEffects.textContent === "unexpected", "accepted bounded profile effect remains inspectable");
  });

  await group("09 complete PL EN label contract", async () => {
    for (const language of ["pl", "en"]) {
      const copy = ui.HOYMILES_SUPERVISOR_COPY[language];
      for (const [key, expected] of Object.entries(EXPECTED_LABELS[language])) {
        check(copy[key] === expected, `${language} ${key}`);
      }
      const allCopyEntries = Object.entries(copy);
      const legacyCopyEntries = allCopyEntries
        .slice(0, 146)
        .sort(([left], [right]) => left.localeCompare(right));
      check(allCopyEntries.length === 166, `${language} revision 28 copy key count`);
      check(
        crypto.createHash("sha256").update(JSON.stringify(legacyCopyEntries)).digest("hex") === EXPECTED_COPY_SHA256[language],
        `${language} legacy 146-key literal copy hash`,
      );
    }
    const pl = panelFixture("pl").panel;
    const en = panelFixture("en").panel;
    check(pl._controls.allowTariff.labelText.textContent === "Uwzględniaj tanie ładowanie", "PL tariff control");
    check(en._controls.allowTariff.labelText.textContent === "Consider tariff charging", "EN tariff control");
    for (const key of [
      "heroIntro", "howItWorksTitle", "modesTitle", "profilesTitle",
      "permissionsTitle", "readResultTitle", "safetyTitle", "technicalTitle",
      "modeActiveDescription", "permissionNotMeaning", "safetyScope",
      "actionWarning",
    ]) {
      check(typeof ui.HOYMILES_SUPERVISOR_COPY.pl[key] === "string" && ui.HOYMILES_SUPERVISOR_COPY.pl[key].length > 0, `PL frozen copy ${key}`);
      check(typeof ui.HOYMILES_SUPERVISOR_COPY.en[key] === "string" && ui.HOYMILES_SUPERVISOR_COPY.en[key].length > 0, `EN frozen copy ${key}`);
    }
    const sectionKeys = new Set(en._copyNodes.map(([, key]) => key));
    for (const key of ["liveControls", "currentDecision", "flowPolicies", "howItWorksTitle", "modesTitle", "profilesTitle", "permissionsTitle", "readResultTitle", "safetyTitle", "technicalTitle"]) {
      check(sectionKeys.has(key), `rendered section ${key}`);
    }
  });

  await group("10 reason map 45 of 45", async () => {
    const expectedCodes = Object.keys(EXPECTED_REASON_COPY).sort();
    const implementationCodes = Object.keys(ui.HOYMILES_SUPERVISOR_REASON_COPY).sort();
    check(expectedCodes.length === 45, "oracle has 45 reasons");
    equal(implementationCodes, expectedCodes, "reason key set");
    for (const [code, [pl, en]] of Object.entries(EXPECTED_REASON_COPY)) {
      check(ui.hoymilesSupervisorReason(code, "pl") === pl, `PL reason ${code}`);
      check(ui.hoymilesSupervisorReason(code, "en") === en, `EN reason ${code}`);
    }
  });

  await group("11 unknown reason fallback", async () => {
    check(ui.hoymilesSupervisorReason("future_reason_code", "pl") === "Nieznany powód", "PL unknown reason");
    check(ui.hoymilesSupervisorReason("future_reason_code", "en") === "Unknown reason", "EN unknown reason");
    check(!ui.hoymilesSupervisorReason("future_reason_code", "en").includes("future_reason_code"), "raw unknown code hidden");
  });

  await group("12 missing Supervisor", async () => {
    doesNotThrow(() => ui.hoymilesNormalizeSupervisor(undefined), "normalize missing sensor");
    const hass = hassFixture();
    delete hass.states[EXPECTED_BINDINGS.supervisor_entity];
    const { panel } = panelFixture("en", hass);
    check(panel._summary.state.value.textContent === "Unavailable", "missing state fallback");
    check(panel._scope.textContent === "Observation only", "badge survives missing sensor");
  });

  await group("13 unavailable Supervisor", async () => {
    const panel = renderState("unavailable", {});
    check(panel._summary.state.value.textContent === "Unavailable", "unavailable state text");
    check(panel._container.dataset.tone === "unavailable", "unavailable tone");
    check(panel._summary.physicalExecution.value.textContent === "Not authorized — observation mode", "false auth remains non-authorized");
  });

  await group("14 malformed attributes", async () => {
    for (const attributes of [null, 7, "bad", [], true]) {
      const sensor = { state: "shadow_idle", attributes };
      doesNotThrow(() => ui.hoymilesNormalizeSupervisor(sensor), `normalize attributes ${String(attributes)}`);
      check(ui.hoymilesNormalizeSupervisor(sensor).state === "unavailable", "malformed attributes fail safe");
    }
    const malformedBoolean = renderState("shadow_idle", { supervisor_execution_authorized: "false", legacy_execution_unchanged: 1 });
    check(malformedBoolean._summary.physicalExecution.value.textContent === "Unverified — panel remains observation only", "malformed auth unverified");
    check(malformedBoolean._summary.existingAutomation.value.textContent === "Unverified", "malformed legacy unverified");
    const inconsistent = renderState("shadow_idle", { selected_policy: "rce" });
    check(inconsistent._summary.selectedPolicy.value.textContent === "None", "idle state cannot claim a selection");
  });

  await group("15 malformed candidate collection", async () => {
    for (const candidate_summaries of [null, {}, "bad", [candidate("rce"), 3]]) {
      doesNotThrow(() => renderState("shadow_idle", { candidate_summaries }), "render malformed candidates");
    }
    const tooMany = renderState("shadow_idle", { candidate_summaries: [candidate("rce"), candidate("tariff"), candidate("rcm"), candidate("rce")] });
    check(Object.values(tooMany._policyRows).every((row) => row.badge.textContent === "Data unavailable"), "oversized collection fails safe");
  });

  await group("16 arbitrary candidate ordering", async () => {
    const panel = renderState("shadow_selected", {
      selected_policy: "tariff",
      candidate_summaries: [candidate("rcm"), candidate("tariff"), candidate("rce")],
    });
    equal(Object.keys(panel._policyRows), EXPECTED_POLICY_IDS, "fixed product order");
    check(panel._policyRows.tariff.selected.textContent === "Logically selected", "lookup selection by policy_id");
    check(panel._policyRows.rce.action.textContent === "Requested action: RCE export", "RCE action lookup");
    check(panel._policyRows.rce.facts.children.length === 5, "policy card has five bounded facts plus action and reason");
    check(panel._policyRows.rce.actionWarning.textContent === "Requested action describes a logical result. It does not confirm physical execution.", "permanent action warning");
  });

  await group("17 duplicate policy IDs", async () => {
    const panel = renderState("shadow_idle", {
      candidate_summaries: [candidate("rce"), candidate("rce"), candidate("tariff")],
    });
    check(panel._policyRows.rce.badge.textContent === "Data unavailable", "duplicate RCE unavailable");
    check(panel._policyRows.tariff.badge.textContent === "Ready", "unaffected tariff remains valid");
    check(Object.keys(panel._policyRows).length === 3, "duplicates do not add rows");
  });

  await group("18 maximum three policy rows", async () => {
    const { panel, container } = panelFixture();
    check(Object.keys(panel._policyRows).length === 3, "three internal rows");
    equal(Object.keys(panel._policyRows), EXPECTED_POLICY_IDS, "row IDs");
    check(container.children.length === 1, "single panel root");
  });

  await group("19 Off rendering", async () => {
    const panel = renderState("off", { candidate_summaries: [] });
    check(panel._summary.state.value.textContent === "Off", "Off label");
    check(panel._container.dataset.tone === "off", "Off neutral tone");
    check(panel._icon.getAttribute("icon") === "mdi:eye-off-outline", "Off icon");
  });

  await group("20 Shadow idle rendering", async () => {
    const panel = renderState("shadow_idle");
    check(panel._summary.state.value.textContent === "Observation — no selection", "idle label");
    check(panel._container.dataset.tone === "shadow-idle", "idle tone");
    check(panel._icon.getAttribute("icon") === "mdi:eye-outline", "idle icon");
    check(panel._summary.selectedPolicy.value.textContent === "None", "no selected policy");
    const noNeed = renderState("shadow_idle", {
      candidate_summaries: [candidate("rce", { requested_action: "none", reason_code: "no_action", rejection_reason: "no_action" })],
    });
    check(noNeed._policyRows.rce.badge.textContent === "No need", "no-action rejection is not a hard blocker");
  });

  for (const [number, policyId, label] of [
    [21, "rce", "RCE"],
    [22, "tariff", "Tariff charging"],
    [23, "rcm", "RCEm"],
  ]) {
    await group(`${number} Shadow selected ${policyId}`, async () => {
      const panel = renderState("shadow_selected", { selected_policy: policyId });
      check(panel._summary.state.value.textContent === "Observation — policy selected", `${policyId} selected state`);
      check(panel._summary.selectedPolicy.value.textContent === label, `${policyId} selected label`);
      check(panel._policyRows[policyId].selected.textContent === "Logically selected", `${policyId} marker`);
      check(panel._container.dataset.tone === "shadow-selected", `${policyId} tone`);
    });
  }

  await group("24 blocked rendering", async () => {
    const panel = renderState("blocked", { execution_phase: "blocked", execution_blocked_reason: "owner_conflict" });
    check(panel._summary.state.value.textContent === "Blocked", "blocked label");
    check(panel._summary.blockedReason.value.textContent === "Control-owner conflict", "mapped blocker");
    check(panel._container.dataset.tone === "blocked", "amber semantic tone");
    check(panel._scope.textContent === "Observation only", "badge on blocked");
  });

  await group("25 false authorization presentation", async () => {
    const falsePanel = renderState("shadow_idle", { supervisor_execution_authorized: false });
    check(falsePanel._summary.physicalExecution.value.textContent === "Not authorized — observation mode", "exact false authorization");
    for (const unsafe of [true, null, "false", 0]) {
      const panel = renderState("shadow_idle", { supervisor_execution_authorized: unsafe });
      check(panel._summary.physicalExecution.value.textContent === "Unverified — panel remains observation only", `unsafe auth ${String(unsafe)}`);
    }
  });

  await group("26 legacy unchanged presentation", async () => {
    check(renderState("shadow_idle", { legacy_execution_unchanged: true })._summary.existingAutomation.value.textContent === "Unchanged", "legacy true unchanged");
    for (const value of [false, null, "true", 1]) {
      check(renderState("shadow_idle", { legacy_execution_unchanged: value })._summary.existingAutomation.value.textContent === "Unverified", `legacy ${String(value)} unverified`);
    }
  });

  await group("27 no polling or timers", async () => {
    for (const forbidden of ["setInterval", "requestAnimationFrame", "setTimeout", "heartbeat", "countdown"]) {
      check(!componentSource.includes(forbidden), `component excludes ${forbidden}`);
    }
    check(!/Date\.now\(\)/.test(componentSource), "no per-second time calculation");
    for (const forbidden of ["fetch(", "XMLHttpRequest", "WebSocket", "MutationObserver", "ResizeObserver"]) {
      check(!componentSource.includes(forbidden), `standalone excludes network/observer ${forbidden}`);
    }
  });

  await group("28 no unsafe innerHTML for backend strings", async () => {
    check(!componentSource.includes("innerHTML"), "internal component never uses innerHTML");
    check(componentSource.includes("textContent ="), "dynamic text uses textContent");
    const hostile = '<img src=x onerror="boom">';
    const panel = renderState("blocked", { execution_blocked_reason: hostile });
    check(panel._summary.blockedReason.value.textContent === "Unknown reason", "hostile backend string bounded");
    check(!cardSource.includes("candidate_summaries.map((candidate) => `"), "no candidate template HTML");
    const technicalPanel = renderState("blocked", {
      arbitration_revision: "a".repeat(128),
      profile_effects_applied: [{ secret: "raw" }],
      candidate_summaries: [candidate("rce", { input_revision: {}, candidate_revision: [] })],
    });
    check(technicalPanel._technical.arbitrationRevision.textContent === "Unavailable", "overlong technical value bounded");
    check(technicalPanel._technical.profileEffects.textContent === "Not applied", "structured profile effects hidden");
    check(technicalPanel._technical.candidateRevisions.textContent === "Unavailable", "structured revisions hidden");
    const details = walk(technicalPanel._container).filter((node) => node.tagName === "DETAILS");
    check(details.length === 5 && details.every((detail) => detail.getAttribute("open") === null), "five closed native knowledge details elements");
  });

  await group("29 service call in-flight guard", async () => {
    const hass = hassFixture();
    let release;
    hass.callService = (domain, service, data) => {
      hass.calls.push({ domain, service, data });
      return new Promise((resolve) => { release = resolve; });
    };
    const { panel } = panelFixture("en", hass);
    const first = panel._toggleBoolean("allowRce");
    const second = panel._toggleBoolean("allowRce");
    check(hass.calls.length === 1, "duplicate call suppressed");
    check(panel._controls.allowRce.element.disabled, "affected control disabled");
    check(!panel._controls.allowTariff.element.disabled, "unaffected control enabled");
    release();
    await Promise.all([first, second]);
    check(!panel._pending.has("allowRce"), "pending guard released");
  });

  await group("30 service rejection recovery", async () => {
    const hass = hassFixture();
    hass.callService = async () => { throw new Error("<secret backend exception>"); };
    const { panel } = panelFixture("en", hass);
    await panel._toggleBoolean("allowTariff");
    check(!panel._pending.has("allowTariff"), "rejection clears pending");
    check(!panel._controls.allowTariff.element.disabled, "control recovers");
    check(panel._controls.allowTariff.error.textContent === "Could not save the setting", "bounded local error");
    check(!panel._controls.allowTariff.error.textContent.includes("secret"), "exception text hidden");
  });

  await group("31 parent remount no duplicate DOM", async () => {
    const { panel, container, hass } = panelFixture();
    const initialRoot = container.children[0];
    const initialListeners = panel._controls.mode.element.listenerCount("change");
    panel.connect();
    panel.update(hass, { ...EXPECTED_BINDINGS }, "en");
    check(container.children.length === 1 && container.children[0] === initialRoot, "repeat update keeps one root");
    check(panel._controls.mode.element.listenerCount("change") === initialListeners, "repeat connect adds no listener");
    panel.disconnect();
    check(panel._controls.mode.element.listenerCount("change") === 0, "disconnect removes listener");
    check(panel._hass === null, "disconnect releases old hass");
    panel.connect();
    panel.update(hass, { ...EXPECTED_BINDINGS }, "en");
    check(panel._controls.mode.element.listenerCount("change") === 1, "reconnect restores one listener");
  });

  await group("FINAL_SERVICE_TARGET_ATTESTATION", async () => {
    const exact = panelFixture();
    await exact.panel._selectOption("mode", "Off");
    await exact.panel._selectOption("profile", "Maximum Profit");
    await exact.panel._toggleBoolean("allowRce");
    await exact.panel._toggleBoolean("allowTariff");
    await exact.panel._toggleBoolean("allowRcm");
    equal(
      exact.hass.calls.map((call) => call.data.entity_id),
      [
        "input_select.hoymiles_ems_supervisor_mode",
        "input_select.hoymiles_ems_supervisor_profile",
        "input_boolean.hoymiles_ems_supervisor_allow_rce",
        "input_boolean.hoymiles_ems_supervisor_allow_tariff",
        "input_boolean.hoymiles_ems_supervisor_allow_rcm",
      ],
      "literal final target order",
    );
    equal(
      exact.hass.calls.map((call) => [call.domain, call.service]),
      [
        ["input_select", "select_option"],
        ["input_select", "select_option"],
        ["input_boolean", "turn_off"],
        ["input_boolean", "turn_on"],
        ["input_boolean", "turn_off"],
      ],
      "service derived from closed control kind",
    );
    equal(EXPECTED_CONTROL_TARGETS, {
      mode: "input_select.hoymiles_ems_supervisor_mode",
      profile: "input_select.hoymiles_ems_supervisor_profile",
      allowRce: "input_boolean.hoymiles_ems_supervisor_allow_rce",
      allowTariff: "input_boolean.hoymiles_ems_supervisor_allow_tariff",
      allowRcm: "input_boolean.hoymiles_ems_supervisor_allow_rcm",
    }, "independent target literal oracle");

    const redirects = [
      ["allowRce", "supervisor_allow_rce_entity", "input_boolean.hoymiles_rce_discharge_enabled", "off", null],
      ["allowTariff", "supervisor_allow_tariff_entity", "input_boolean.hoymiles_tariff_charge_enabled", "off", null],
      ["mode", "supervisor_mode_entity", "select.hoymiles_hit_ems_mode", "Shadow", ["Off", "Shadow"]],
      ["mode", "supervisor_mode_entity", "input_select.unrelated_mode", "Shadow", ["Off", "Shadow"]],
      ["mode", "supervisor_mode_entity", "input_select.hoymiles_ems_supervisor_mode_2", "Shadow", ["Off", "Shadow"]],
      ["mode", "supervisor_mode_entity", "input_select.", "Shadow", ["Off", "Shadow"]],
      ["allowRcm", "supervisor_allow_rcm_entity", "sensor.hoymiles_ems_supervisor_allow_rcm", "off", null],
    ];
    for (const [key, configKey, unsafeTarget, state, options] of redirects) {
      const helper = { state, attributes: options === null ? {} : { options } };
      const hass = hassFixture("shadow_idle", {}, { [unsafeTarget]: helper });
      const config = { ...EXPECTED_BINDINGS, [configKey]: unsafeTarget };
      const fixture = panelFixture("en", hass, config);
      if (key === "mode") await fixture.panel._selectOption(key, "Off");
      else await fixture.panel._toggleBoolean(key);
      check(hass.calls.length === 0, `redirect blocked ${unsafeTarget}`);
      check(fixture.panel._controls[key].element.disabled, `redirect disabled ${unsafeTarget}`);
      check(fixture.panel._controls[key].error.textContent === "Could not save the setting", `redirect bounded error ${unsafeTarget}`);
    }
    const direct = panelFixture();
    await direct.panel._callHelperService("mode", "Off", "input_select.unrelated_mode");
    await direct.panel._callHelperService("mode", "input_select.unrelated_mode");
    await direct.panel._callHelperService("allowRce", "input_boolean.hoymiles_rce_discharge_enabled");
    check(direct.hass.calls.length === 0, "caller-provided targets cannot reach service boundary");
  });

  await group("FINAL_HELPER_STATE_ATTESTATION", async () => {
    for (const [current, requested] of [["Off", "Shadow"], ["Shadow", "Off"]]) {
      const hass = hassFixture("shadow_idle", {}, {
        ["input_select.hoymiles_ems_supervisor_mode"]: {
          state: current,
          attributes: { options: ["Off", "Shadow"] },
        },
      });
      const { panel } = panelFixture("en", hass);
      await panel._selectOption("mode", requested);
      check(hass.calls.length === 1, `safe current mode ${current}`);
      check(hass.calls[0].data.option === requested, `exact requested mode ${requested}`);
    }

    for (const unsafeState of ["Active", "Auto", "Automatic", "unknown", "unavailable", "", null, 7, true]) {
      const hass = hassFixture("shadow_idle", {}, {
        ["input_select.hoymiles_ems_supervisor_mode"]: {
          state: unsafeState,
          attributes: { options: ["Off", "Shadow", "Active", "Auto", "Automatic"] },
        },
      });
      const { panel } = panelFixture("en", hass);
      await panel._selectOption("mode", "Off");
      check(hass.calls.length === 0, `unsafe current mode blocked ${String(unsafeState)}`);
      check(panel._controls.mode.element.disabled, `unsafe current mode disabled ${String(unsafeState)}`);
      check(panel._controls.mode.error.textContent === "Could not save the setting", `unsafe current mode bounded error ${String(unsafeState)}`);
    }

    const selectCases = [
      ["current absent", "Shadow", ["Off"], "Off"],
      ["requested absent", "Shadow", ["Shadow"], "Off"],
      ["requested duplicate", "Shadow", ["Shadow", "Off", "Off"], "Off"],
      ["current duplicate", "Shadow", ["Shadow", "Shadow", "Off"], "Off"],
      ["options null", "Shadow", null, "Off"],
      ["options object", "Shadow", {}, "Off"],
      ["options primitive", "Shadow", "Off,Shadow", "Off"],
      ["options non-string", "Shadow", ["Shadow", "Off", 1], "Off"],
    ];
    for (const [label, current, options, requested] of selectCases) {
      const hass = hassFixture("shadow_idle", {}, {
        ["input_select.hoymiles_ems_supervisor_mode"]: {
          state: current,
          attributes: { options },
        },
      });
      const { panel } = panelFixture("en", hass);
      await panel._selectOption("mode", requested);
      check(hass.calls.length === 0, `${label} blocked at final boundary`);
      check(panel._controls.mode.error.textContent === "Could not save the setting", `${label} bounded error`);
    }

    const profile = hassFixture("shadow_idle", {}, {
      ["input_select.hoymiles_ems_supervisor_profile"]: {
        state: "Balanced",
        attributes: { options: ["Balanced", "Maximum Profit", "High Reserve — Winter"] },
      },
    });
    const profilePanel = panelFixture("en", profile).panel;
    await profilePanel._selectOption("profile", "High Reserve — Winter");
    check(profile.calls.length === 1, "exact em-dash profile accepted");
    check(profile.calls[0].data.option === "High Reserve — Winter", "exact em-dash preserved");

    for (const unsafeState of ["unknown", "unavailable", "", null, 4, false, {}, []]) {
      const hass = hassFixture("shadow_idle", {}, {
        ["input_boolean.hoymiles_ems_supervisor_allow_rce"]: {
          state: unsafeState,
          attributes: {},
        },
      });
      const { panel } = panelFixture("en", hass);
      await panel._toggleBoolean("allowRce");
      check(hass.calls.length === 0, `unsafe boolean blocked ${String(unsafeState)}`);
      check(panel._controls.allowRce.element.disabled, `unsafe boolean disabled ${String(unsafeState)}`);
    }

    const bypass = hassFixture("shadow_idle", {}, {
      ["input_select.hoymiles_ems_supervisor_mode"]: {
        state: "Active",
        attributes: { options: ["Off", "Shadow", "Active"] },
      },
    });
    const bypassPanel = panelFixture("en", bypass).panel;
    await bypassPanel._callHelperService("mode", "Off");
    check(bypass.calls.length === 0, "direct Active-to-Off bypass blocked");
    check(bypassPanel._controls.mode.element.disabled, "direct bypass remains fail-safe disabled");
  });

  await group("LIFECYCLE_GENERATION_AND_PROMISES", async () => {
    const ownership = panelFixture();
    const ownershipToken = Symbol("stale-generation-oracle");
    ownership.panel._requestTokens.set("allowRce", ownershipToken);
    check(
      !ownership.panel._requestOwnsCurrentPanel({
        key: "allowRce",
        lifecycleGeneration: ownership.panel._lifecycleGeneration - 1,
        instanceToken: ownership.panel._instanceToken,
        controlToken: ownershipToken,
        hass: ownership.hass,
      }),
      "stale lifecycle generation alone invalidates request ownership",
    );
    ownership.panel._requestTokens.delete("allowRce");

    const sameHass = hassFixture();
    const sameGate = deferred();
    sameHass.callService = (domain, service, data) => {
      sameHass.calls.push({ domain, service, data });
      return sameGate.promise;
    };
    const same = panelFixture("en", sameHass);
    const sameGeneration = same.panel._lifecycleGeneration;
    const sameRequest = same.panel._toggleBoolean("allowRce");
    sameGate.reject(new Error("same lifecycle secret"));
    await sameRequest;
    check(same.panel._lifecycleGeneration === sameGeneration, "same lifecycle keeps generation");
    check(!same.panel._pending.has("allowRce"), "same lifecycle rejection clears own busy");
    check(same.panel._errors.has("allowRce"), "same lifecycle rejection owns bounded error");

    for (const settle of ["resolve", "reject"]) {
      const oldHass = hassFixture();
      const oldGate = deferred();
      oldHass.callService = (domain, service, data) => {
        oldHass.calls.push({ domain, service, data });
        return oldGate.promise;
      };
      const fixture = panelFixture("en", oldHass);
      const generation = fixture.panel._lifecycleGeneration;
      const request = fixture.panel._toggleBoolean("allowRce");
      fixture.panel.disconnect();
      check(fixture.panel._lifecycleGeneration === generation + 1, `disconnect increments generation before ${settle}`);
      check(fixture.panel._hass === null, `disconnect releases hass before ${settle}`);
      check(fixture.panel._pending.size === 0 && fixture.panel._requestTokens.size === 0, `disconnect clears ownership before ${settle}`);
      check(fixture.panel._errors.size === 0, `disconnect clears errors before ${settle}`);
      if (settle === "resolve") oldGate.resolve();
      else oldGate.reject(new Error("stale disconnect secret"));
      await request;
      check(fixture.panel._pending.size === 0 && fixture.panel._errors.size === 0, `stale ${settle} mutates nothing`);
    }

    for (const settle of ["resolve", "reject"]) {
      const oldHass = hassFixture();
      const oldGate = deferred();
      oldHass.callService = (domain, service, data) => {
        oldHass.calls.push({ domain, service, data });
        return oldGate.promise;
      };
      const fixture = panelFixture("en", oldHass);
      const config = fixture.panel._config;
      const oldRequest = fixture.panel._toggleBoolean("allowRce");
      fixture.panel.disconnect();
      fixture.panel.connect();
      const newHass = hassFixture();
      const newGate = deferred();
      newHass.callService = (domain, service, data) => {
        newHass.calls.push({ domain, service, data });
        return newGate.promise;
      };
      fixture.panel.update(newHass, config, "en");
      const newRequest = fixture.panel._toggleBoolean("allowRce");
      const newToken = fixture.panel._requestTokens.get("allowRce");
      if (settle === "resolve") oldGate.resolve();
      else oldGate.reject(new Error("stale reconnect secret"));
      await oldRequest;
      check(fixture.panel._pending.has("allowRce"), `old ${settle} cannot clear new busy`);
      check(fixture.panel._requestTokens.get("allowRce") === newToken, `old ${settle} cannot replace new token`);
      check(!fixture.panel._errors.has("allowRce"), `old ${settle} cannot show error after reconnect`);
      newGate.resolve();
      await newRequest;
      check(!fixture.panel._pending.has("allowRce"), `new request settles after old ${settle}`);
    }

    for (const transition of ["config", "language", "destroyed"]) {
      const hass = hassFixture();
      const gate = deferred();
      hass.callService = (domain, service, data) => {
        hass.calls.push({ domain, service, data });
        return gate.promise;
      };
      const fixture = panelFixture("en", hass);
      const config = fixture.panel._config;
      const generation = fixture.panel._lifecycleGeneration;
      const request = fixture.panel._toggleBoolean("allowRce");
      if (transition === "config") fixture.panel.update(hass, { ...EXPECTED_BINDINGS }, "en");
      if (transition === "language") fixture.panel.update(hass, config, "pl");
      if (transition === "destroyed") fixture.panel.disconnect();
      check(fixture.panel._lifecycleGeneration === generation + 1, `${transition} invalidates generation`);
      check(fixture.panel._pending.size === 0, `${transition} clears request ownership`);
      gate.reject(new Error(`stale ${transition} secret`));
      await request;
      check(fixture.panel._errors.size === 0, `${transition} ignores stale rejection`);
    }

    const normal = panelFixture();
    const normalGeneration = normal.panel._lifecycleGeneration;
    normal.panel.update(hassFixture(), normal.panel._config, "en");
    check(normal.panel._lifecycleGeneration === normalGeneration, "normal hass update does not increment lifecycle generation");
  });

  await group("STANDALONE_RECONNECT_AND_SINGLE_PANEL", async () => {
    const fixture = parentFixture();
    const originalPanel = fixture.panel;
    const originalRoot = fixture.container.children[0];
    fixture.parent.disconnectedCallback();
    check(fixture.parent._hass === null, "parent releases hass on disconnect");
    check(fixture.panel._hass === null, "panel releases hass on parent disconnect");
    check(fixture.panel._controls.mode.element.disabled, "disconnect leaves control disabled");
    check(fixture.panel._controls.mode.element.listenerCount("change") === 0, "disconnect removes parent panel handler");

    fixture.parent.connectedCallback();
    check(fixture.parent._panelController === originalPanel, "reconnect reuses existing panel");
    check(fixture.panel._hass === null, "reconnect before hass keeps panel empty");
    check(Object.values(fixture.panel._controls).every((control) => control.element.disabled), "reconnect before hass keeps five controls disabled");
    await fixture.panel._toggleBoolean("allowRce");
    check(fixture.hass.calls.length === 0, "no old service authority before fresh hass");

    const fresh = hassFixture();
    fixture.parent.hass = fresh;
    check(fixture.parent._panelController === originalPanel, "fresh hass reactivates same panel");
    check(fixture.panel._hass === fresh, "fresh hass reaches existing panel");
    check(!fixture.panel._controls.mode.element.disabled, "fresh hass re-enables safe control");
    check(fixture.container.children.length === 1 && fixture.container.children[0] === originalRoot, "fresh hass keeps one DOM root");

    for (let index = 0; index < 3; index += 1) {
      fixture.parent.disconnectedCallback();
      fixture.parent.connectedCallback();
      fixture.parent.hass = hassFixture();
    }
    check(fixture.parent._panelController === originalPanel, "repeated reconnect keeps one panel instance");
    check(fixture.container.children.length === 1, "repeated reconnect keeps one panel root");
    check(fixture.panel._controls.mode.element.listenerCount("change") === 1, "repeated reconnect keeps one handler");
    fixture.parent.setConfig({ language: "en" });
    fixture.parent.hass = fixture.parent._hass;
    check(fixture.parent._panelController === originalPanel, "repeated config and hass keep one panel");

    fixture.parent.disconnectedCallback();
    fixture.parent.connectedCallback();
    fixture.parent.hass = hassFixture();
    check(fixture.container.children.length === 1, "move between DOM parents keeps one root");

    const first = parentFixture();
    const second = parentFixture();
    check(first.panel !== second.panel, "two cards have independent panels");
    check(first.panel._instanceToken !== second.panel._instanceToken, "two cards have independent instance tokens");
    check(first.panel._requestTokens !== second.panel._requestTokens, "two cards have independent request maps");
    check(first.panel._errors !== second.panel._errors, "two cards have independent errors");
    check(first.panel._hass !== second.panel._hass, "two cards have independent hass references");
  });

  await group("LANGUAGE_FAIL_SAFE_RUNTIME", async () => {
    const cases = [
      ["pl", "pl"],
      ["pl-PL", "pl"],
      ["en", "en"],
      ["en-US", "en"],
      ["unsupported", "en"],
      ["", "en"],
      [null, "en"],
      [undefined, "en"],
      [7, "en"],
      [true, "en"],
      [{ language: "pl" }, "en"],
      [["pl"], "en"],
      [Symbol("pl"), "en"],
    ];
    for (const [value, expected] of cases) {
      let actual;
      doesNotThrow(() => { actual = ui.hoymilesNormalizeLanguage(value); }, `normalizer accepts ${typeof value}`);
      check(actual === expected, `bounded language result ${typeof value}`);
      const parent = new ui.HoymilesEmsSupervisorCard();
      parent._config = { language: value };
      parent._hass = { language: "en-US" };
      doesNotThrow(() => { actual = parent._language(); }, `parent language accepts ${typeof value}`);
      check(actual === expected, `parent bounded language ${typeof value}`);
    }
    const automaticPolish = new ui.HoymilesEmsSupervisorCard();
    automaticPolish._config = {};
    automaticPolish._hass = { language: "pl-PL" };
    check(automaticPolish._language() === "pl", "absent config language uses supported HA Polish language");
    const malformedPanel = panelFixture("en").panel;
    doesNotThrow(() => malformedPanel.update(hassFixture(), malformedPanel._config, Symbol("pl")), "panel update accepts Symbol language");
    check(malformedPanel._language === "en", "panel never uses malformed language as copy key");
  });

  await group("SCHEDULER_DELIVERY_PROTECTION", async () => {
    const releaseValidator = read("tools/validate_release.py");
    for (const marker of [
      "PROTECTED_ASSETS_AST_SHA256",
      "c65e8b73096cb64ff2d21b6e2b05602f71a4a2af",
      "ast.dump(node, annotate_fields=True, include_attributes=False)",
      "assets._attest_scheduler_destination",
      "validate_protected_assets_ast",
      "S29",
    ]) {
      check(releaseValidator.includes(marker), `scheduler protection marker ${marker}`);
    }
    check(!componentSource.includes("_attest_scheduler_destination"), "scheduler protection remains Python-only");
    for (const marker of [
      "PHASE_2_TASK_PATHS = frozenset",
      "SUPERVISOR_BRANCH_PATHS = frozenset",
      "Phase 2 13-path count self-test did not fail",
      "Phase 2 15-path count self-test did not fail",
      "Branch 31-path count self-test did not fail",
      "Branch 33-path count self-test did not fail",
    ]) check(releaseValidator.includes(marker), `exact manifest marker ${marker}`);
    const frontendValidator = read("tools/validate_rce_card.js");
    check(frontendValidator.includes("expectedPhase2Paths") && frontendValidator.includes("branchPaths.length !== 32"), "frontend validator enforces 14/32 manifests");
  });

  await group("32 desktop layout contract", async () => {
    for (const token of [
      "max-width: 1440px",
      "grid-template-columns: repeat(2, minmax(0, 1fr))",
      "grid-template-columns: repeat(3, minmax(0, 1fr))",
      "grid-template-columns: minmax(150px, .85fr) minmax(0, 1.15fr)",
      "width: 100%",
      "max-width: 100%",
    ]) check(supervisorCss.includes(token), `desktop CSS ${token}`);
    check(ui.HoymilesEmsSupervisorCard.prototype.getGridOptions().rows === 18, "standalone grid height");
  });

  await group("33 620 px breakpoint", async () => {
    const mobile = supervisorCss.slice(supervisorCss.indexOf("@container (max-width: 620px)"), supervisorCss.indexOf("@container (max-width: 390px)"));
    check(mobile.includes("grid-template-columns: minmax(0, 1fr)"), "single column at 620");
    check(mobile.includes(".supervisor-select { width: 100%; }"), "full-width mobile select");
    check(mobile.includes("flex-wrap: wrap"), "mobile wrapping");
  });

  await group("34 390 px mobile contract", async () => {
    for (const width of [320, 360, 390, 768, 1366, 1920]) {
      check(width > 0 && supervisorCss.includes("min-width: 0"), `${width}px min-width harness`);
      check(supervisorCss.includes("overflow-wrap: anywhere"), `${width}px reason wrapping harness`);
    }
    const { container } = panelFixture();
    check(container.children.length === 1, "panel remains one rendered container at 390px");
  });

  await group("35 approximately 360 px overflow guard", async () => {
    const narrow = supervisorCss.slice(supervisorCss.indexOf("@container (max-width: 390px)"));
    check(supervisorCss.includes(".supervisor-policies,") && supervisorCss.includes("grid-template-columns: minmax(0, 1fr)"), "narrow policy grid collapses");
    check(!supervisorCss.includes("overflow-x: auto"), "no horizontal-scroll masking");
    check(!/\.supervisor-(select|control)[^{]*\{[^}]*width:\s*[4-9]\d{2}px/s.test(supervisorCss), "no fixed over-wide control");
    check(supervisorCss.includes("overflow-wrap: anywhere"), "long reasons can wrap");
  });

  await group("36 touch focus and accessibility contract", async () => {
    for (const token of [":focus-visible", 'role", "switch"', "aria-label", "min-height: 44px"]) {
      check(cardSource.includes(token), `accessibility token ${token}`);
    }
    const { panel } = panelFixture();
    check(panel._controls.allowRce.element.getAttribute("role") === "switch", "native switch role");
    check(panel._controls.mode.element.getAttribute("aria-label") === "Mode", "select accessible name");
    check(panel._controls.allowRce.element.getAttribute("aria-checked") === "true", "switch checked state");
  });

  await group("37 light theme tokens", async () => {
    for (const token of ["--card-background-color", "--primary-text-color", "--secondary-text-color", "--divider-color"]) {
      check(supervisorCss.includes(token), `light-theme token ${token}`);
    }
    const paletteStart = supervisorCss.indexOf("SUPERVISOR_SEMANTIC_PALETTE_REV28");
    const paletteEnd = supervisorCss.indexOf("  }", paletteStart);
    const cssWithoutPalette =
      supervisorCss.slice(0, paletteStart) + supervisorCss.slice(paletteEnd + 3);
    check(!/#[0-9a-fA-F]{3,8}/.test(cssWithoutPalette), "Supervisor CSS has no hardcoded color outside semantic palette");
  });

  await group("38 dark theme tokens", async () => {
    for (const token of ["--hoymiles-aurora-text", "--hoymiles-aurora-muted", "--hoymiles-aurora-border", "--hoymiles-aurora-shadow"]) {
      check(supervisorCss.includes(token), `Aurora token ${token}`);
    }
    check(supervisorCss.includes("color-mix(in srgb"), "theme-derived layers");
  });

  await group("39 no green physical execution style for Shadow", async () => {
    const selected = renderState("shadow_selected", { selected_policy: "rce" });
    check(selected._container.dataset.tone === "shadow-selected", "selected uses observation tone");
    check(!supervisorCss.includes("--hoymiles-aurora-good"), "no green good token");
    check(supervisorCss.includes(".supervisor-blob { animation: none; }"), "reduced motion disables backdrop animation");
    check(!/\.supervisor-(policy|switch|panel)[^{]*\{[^}]*animation:/s.test(supervisorCss), "no state or control animation suggests execution");
    check(selected._summary.physicalExecution.value.textContent === "Not authorized — observation mode", "selected is not physical execution");
    for (const token of [
      "supervisor-blob-one", "supervisor-blob-two", "supervisor-blob-three",
      "supervisor-points", "supervisor-vignette", "pointer-events: none",
      "@keyframes supervisor-aurora-one", "@media (prefers-reduced-motion: reduce)",
    ]) check(supervisorCss.includes(token), `Aurora backdrop contract ${token}`);
    check(!supervisorCss.includes("filter: blur"), "Aurora backdrop avoids large blur filters");
  });

  await group("40 generated resource parity", async () => {
    check(read("custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js") === cardSource, "packaged card parity");
    check(read("custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js") === strategySource, "packaged strategy parity");
    for (const language of ["pl", "en"]) {
      const yaml = read(`custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_${language}.yaml`);
      const json = JSON.parse(read(`custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_${language}.json`));
      const supervisorView = json.views.find((view) => view.path === "ems-supervisor");
      check(supervisorView?.cards?.length === 1, `${language} one Supervisor card`);
      const supervisorCard = supervisorView.cards[0];
      for (const [key, entityId] of Object.entries(EXPECTED_BINDINGS)) {
        check(yaml.includes(`${key}: ${entityId}`), `${language} YAML ${key}`);
        check(supervisorCard[key] === entityId, `${language} JSON ${key}`);
      }
      const energy = json.views.find((view) => view.path === "start").cards.find((card) => card.type === "custom:hoymiles-aurora-energy-card");
      check(Object.keys(EXPECTED_BINDINGS).every((key) => !(key in energy)), `${language} Start has no Supervisor bindings`);
      check(supervisorView.title === (language === "pl" ? "Nadzorca EMS" : "EMS Supervisor"), `${language} generated title`);
      check(supervisorView.icon === "mdi:eye-circle-outline" && supervisorView.type === "panel", `${language} view identity`);
    }
    const generator = read("tools/build_hacs_assets.py");
    check(generator.includes('"  - title: Nadzorca EMS\\n"'), "generator matches full Polish view title line");
    check(generator.includes('"  - title: EMS Supervisor\\n"'), "generator emits exact English view title line");
    check(generator.includes('"    path: ems-supervisor\\n"') && generator.includes('"    icon: mdi:eye-circle-outline\\n"'), "generator translation is anchored to view identity");
    check(!generator.includes('"Nadzorca EMS": "EMS Supervisor"'), "generator has no broad Supervisor phrase replacement");
  });

  await group("41 frontend revision consistency", async () => {
    check(read("custom_components/hoymiles_hit_modbus/assets.py").includes("FRONTEND_ASSET_REVISION = 28"), "asset revision 28");
    check(strategySource.includes("hoymiles-rce-chart-card.js?v=1.5.7.28"), "strategy cache key");
    check(read("tools/validate_rce_card.js").includes("?v=1.5.7.28"), "frontend validator cache key");
    check(!strategySource.includes("1.5.7.25"), "pre-fix strategy revision absent");
    check(!strategySource.includes("1.5.6.24"), "old strategy revision absent");
  });

  await group("42 protected dashboard behavior outside insertion", async () => {
    const viewMatches = [...dashboardSource.matchAll(/^  - title: (.+)$/gm)];
    const startSource = dashboardSource.slice(viewMatches[0].index, viewMatches[1].index).replaceAll("\r\n", "\n");
    const startDigest = crypto.createHash("sha256").update(startSource).digest("hex");
    check(startDigest === "0fe97805e8c5a20b1d3e760809dac3872b92083c9ee2f8a9eade5b3a38440bc1", "Start YAML exactly matches accepted revision 24");
    const energyStart = cardSource.indexOf("class HoymilesAuroraEnergyCard");
    const energyEnd = cardSource.indexOf("class HoymilesPowerFlowCard", energyStart);
    const energySource = cardSource.slice(energyStart, energyEnd).replaceAll("\r\n", "\n");
    const energyDigest = crypto.createHash("sha256").update(energySource).digest("hex");
    check(energyDigest === "4a810f33c50324e1999810f3d6b71770dd2b184f6878e88cb451ea15e0a1ec67", "Start energy card exactly matches accepted revision 24");
    for (const sentinel of ['data-ribbon="pv"', 'data-flow="battery"', '<div class="daily">', '<div class="grid-import">']) {
      check(cardSource.includes(sentinel), `existing energy behavior ${sentinel}`);
    }
    check((dashboardSource.match(/^  - title:/gm) || []).length > 1, "existing views retained");
  });

  await group("43 no backend or scheduler changes", async () => {
    for (const [relativePath, expectedHash] of Object.entries(PROTECTED_HASHES)) {
      check(digest(relativePath) === expectedHash, `protected hash ${relativePath}`);
    }
  });

  await group("44 physical authority absent", async () => {
    check((componentSource.match(/callService\.call\(/g) || []).length === 1, "one guarded call site");
    check(componentSource.includes('domain: "input_select"'), "select domain derived internally");
    check(componentSource.includes('domain: "input_boolean"'), "boolean domain derived internally");
    check(componentSource.includes("HOYMILES_SUPERVISOR_CONTROL_KINDS"), "closed control-kind map present");
    for (const forbidden of ["owner", "grant", "handover", "modbus", "write_register", "force_active"]) {
      check(!/callService\.call[^;]+/is.exec(componentSource)?.[0].toLowerCase().includes(forbidden), `no ${forbidden} authority in call site`);
    }
    const panel = renderState("shadow_selected", { selected_policy: "rcm", supervisor_execution_authorized: true });
    check(panel._summary.physicalExecution.value.textContent === "Unverified — panel remains observation only", "true authorization never grants UI authority");
  });

  await group("CENTRALIZED_AURORA_PALETTE", async () => {
    check((supervisorCss.match(/SUPERVISOR_SEMANTIC_PALETTE_REV28/g) || []).length === 1, "one semantic palette marker");
    for (const [name, value] of [
      ["cyan", "#43d5ff"],
      ["blue", "#4c91ff"],
      ["violet", "#9b7cff"],
      ["rce", "#f2b84b"],
      ["tariff", "#49a5ff"],
      ["rcm", "#b07cff"],
      ["ready", "#47df91"],
      ["warning", "#f1b84b"],
      ["error", "#ff647c"],
    ]) {
      check(supervisorCss.includes("--supervisor-" + name + ": " + value), "exact palette " + name);
    }
    const paletteStart = supervisorCss.indexOf("SUPERVISOR_SEMANTIC_PALETTE_REV28");
    const paletteEnd = supervisorCss.indexOf("  }", paletteStart);
    const paletteBlock = supervisorCss.slice(paletteStart, paletteEnd);
    const cssOutsidePalette =
      supervisorCss.slice(0, paletteStart) + supervisorCss.slice(paletteEnd);
    check((paletteBlock.match(/#[0-9a-fA-F]{3,8}/g) || []).length === 12, "all twelve raw fallbacks centralized");
    check(!/#[0-9a-fA-F]{3,8}/.test(cssOutsidePalette), "no ad-hoc raw color outside palette");
    check(supervisorCss.includes("--supervisor-surface: color-mix"), "surface derived with color-mix");
    check(supervisorCss.includes("var(--card-background-color"), "surface retains HA theme source");
  });

  await group("COLOR_SEMANTICS", async () => {
    for (const [state, tone] of [
      ["off", "off"],
      ["shadow_idle", "shadow-idle"],
      ["shadow_selected", "shadow-selected"],
      ["blocked", "blocked"],
      ["unavailable", "unavailable"],
    ]) {
      const overrides = state === "shadow_selected" ? { selected_policy: "rce" } : {};
      check(renderState(state, overrides)._container.dataset.tone === tone, "runtime tone " + state);
    }
    check(supervisorCss.includes("--supervisor-tone: var(--supervisor-neutral)"), "Off uses neutral");
    check(supervisorCss.includes('data-tone="shadow-idle"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-cyan)"), "idle uses cyan and blue");
    check(supervisorCss.includes('data-tone="shadow-selected"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-violet)"), "selected uses violet and cyan");
    check(supervisorCss.includes('data-tone="blocked"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-warning)"), "blocked uses warning amber");
    check(supervisorCss.includes('data-tone="unavailable"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-error)"), "unavailable uses restrained red");
    const selectedToneBlock = supervisorCss.slice(
      supervisorCss.indexOf('.supervisor-panel[data-tone="shadow-selected"]'),
      supervisorCss.indexOf('.supervisor-panel[data-tone="blocked"]'),
    );
    check(!selectedToneBlock.includes("supervisor-ready"), "selected Shadow never uses green readiness");
    check(supervisorCss.includes('.supervisor-policy-badge[data-tone="ready"]'), "green reserved for readiness badge");
  });

  await group("HERO_VISUAL_HIERARCHY", async () => {
    const panel = panelFixture("en").panel;
    check(nodesWithClass(panel._container, "supervisor-title").length === 1, "Hero has one primary title");
    check(panel._scope.textContent === "Observation only" && !panel._scope.hidden, "permanent observation badge");
    const authority = nodesWithClass(panel._container, "supervisor-authority");
    check(authority.length === 1, "one prominent physical-authority statement");
    check(panel._copyNodes.some(([node, key]) => key === "physicalAuthority" && node.textContent === "The Supervisor has no physical authority over the inverter."), "authority statement copy");
    check(nodesWithClass(panel._heroCore, "supervisor-policy-chip").length === 3, "three logical input chips in Hero");
    check(nodesWithClass(panel._heroCore, "supervisor-core-orb").length === 1, "one central Supervisor orb");
    check(panel._heroResultText.textContent.length > 0 && panel._heroResultBadge.textContent.length > 0, "bounded dynamic result");
    check(nodesWithClass(panel._heroCore, "supervisor-no-control").length === 1, "permanent no-inverter-control line");
    check(panel._hero.physical.textContent === "No — observation only", "exact prominent physical-control fact");
  });

  await group("HERO_STATE_COPY", async () => {
    const cases = [
      ["off", {}, "heroOffResult"],
      ["shadow_idle", {}, "heroIdleResult"],
      ["shadow_selected", { selected_policy: "rce" }, "heroSelectedResult"],
      ["shadow_selected", { selected_policy: "tariff" }, "heroSelectedResult"],
      ["shadow_selected", { selected_policy: "rcm" }, "heroSelectedResult"],
      ["blocked", { execution_blocked_reason: "owner_conflict" }, "heroBlockedResult"],
      ["unavailable", {}, "heroUnavailableResult"],
    ];
    for (const language of ["pl", "en"]) {
      for (const [state, overrides, copyKey] of cases) {
        const panel = renderState(state, overrides, language);
        const expected = ui.HOYMILES_SUPERVISOR_COPY[language][copyKey].replace(
          "{policy}",
          overrides.selected_policy
            ? panel._policyText(overrides.selected_policy)
            : ui.HOYMILES_SUPERVISOR_COPY[language].none,
        );
        check(panel._heroResultText.textContent === expected, language + " Hero copy " + state + " " + (overrides.selected_policy || ""));
        check(!/\b(started|uruchomiono|wykonano)\b/i.test(panel._heroResultBadge.textContent), language + " badge does not imply execution");
      }
    }
  });

  await group("POLICY_VISUAL_IDENTITIES", async () => {
    equal(
      JSON.parse(JSON.stringify(ui.HOYMILES_SUPERVISOR_POLICY_ICONS)),
      {
        rce: "mdi:chart-line",
        tariff: "mdi:battery-clock-outline",
        rcm: "mdi:transmission-tower",
      },
      "exact policy icons",
    );
    const selected = renderState("shadow_selected", { selected_policy: "tariff" });
    for (const policyId of EXPECTED_POLICY_IDS) {
      const view = selected._policyRows[policyId];
      check(view.row.dataset.policy === policyId, "stable identity " + policyId);
      check(view.policyIcon.getAttribute("icon") === ui.HOYMILES_SUPERVISOR_POLICY_ICONS[policyId], "rendered icon " + policyId);
      check(supervisorCss.includes('.supervisor-policy[data-policy="' + policyId + '"]'), "accent selector " + policyId);
    }
    check(selected._policyRows.tariff.selected.textContent === "Logically selected", "selected marker is logical only");
    const denied = renderState("shadow_idle", {
      candidate_summaries: [
        candidate("rce", { allowed_by_user: false }),
        candidate("tariff"),
        candidate("rcm"),
      ],
    });
    check(denied._policyRows.rce.row.dataset.permitted === "false", "permission-off state explicit");
    check(denied._policyRows.rce.permissionBadge.textContent === "Not considered", "permission-off badge clear");
    check(denied._policyRows.rce.policyIcon.getAttribute("icon") === "mdi:chart-line", "permission-off identity retained");
  });

  await group("POLICY_SWITCH_ACCENTS", async () => {
    const panel = panelFixture("en").panel;
    for (const [key, policyId, checked] of [
      ["allowRce", "rce", "true"],
      ["allowTariff", "tariff", "false"],
      ["allowRcm", "rcm", "true"],
    ]) {
      const control = panel._controls[key];
      check(control.row.dataset.policy === policyId, "row policy accent " + policyId);
      check(control.element.dataset.policy === policyId, "switch policy accent " + policyId);
      check(control.element.getAttribute("aria-checked") === checked, "text-independent switch state " + policyId);
      check(supervisorCss.includes('--policy-accent: var(--supervisor-' + policyId + ')'), "semantic switch variable " + policyId);
    }
    check(supervisorCss.includes('.supervisor-switch[aria-checked="true"]'), "enabled position styled");
    check(supervisorCss.includes("transform: translateX(22px)"), "enabled state has visible thumb position");
    check(componentSource.includes('button.setAttribute("role", "switch")'), "switch semantics unchanged");
  });

  await group("DECISION_HIERARCHY", async () => {
    const panel = renderState("shadow_selected", { selected_policy: "rcm" });
    const prominent = Object.values(panel._summary).filter(
      ({ row }) => row.dataset.priority === "prominent",
    );
    const secondary = Object.values(panel._summary).filter(
      ({ row }) => row.dataset.priority === "secondary",
    );
    check(prominent.length === 4, "four prominent decision facts");
    check(secondary.length === 3, "three secondary decision facts");
    equal(
      Object.keys(panel._summary),
      ["state", "selectedPolicy", "decisionReason", "physicalExecution", "blockedReason", "phase", "existingAutomation"],
      "all seven decision fields retained in hierarchy",
    );
    check(panel._summary.state.row.dataset.tone === "shadow-selected", "state badge gets semantic tone");
    check(panel._summary.selectedPolicy.row.dataset.policy === "rcm", "selected policy gets policy accent");
    check(panel._summary.physicalExecution.value.textContent === "Not authorized — observation mode", "physical observation chip always visible");
    const idle = renderState("shadow_idle");
    check(idle._summary.selectedPolicy.row.dataset.policy === "none", "no selection uses neutral accent");
    check(idle._summary.blockedReason.row.dataset.empty === "true", "empty blocker explicitly subdued");
  });

  await group("VISIBLE_SAFETY_STRIP", async () => {
    const en = panelFixture("en").panel;
    const pl = panelFixture("pl").panel;
    check(nodesWithClass(en._container, "supervisor-safety-strip").length === 1, "one always-visible safety strip");
    check(en._copyNodes.some(([node, key]) => key === "safetyStrip" && node.textContent === "The Supervisor does not control the inverter. It only shows the analysis result and can change only its five UI settings."), "exact EN safety copy");
    check(pl._copyNodes.some(([node, key]) => key === "safetyStrip" && node.textContent === "Nadzorca nie steruje falownikiem. Pokazuje wyłącznie wynik analizy i może zmieniać tylko pięć ustawień własnego interfejsu."), "exact PL safety copy");
    const content = nodesWithClass(en._container, "supervisor-content")[0];
    const safetyIndex = content.children.findIndex((node) => String(node.className).includes("supervisor-safety-strip"));
    const knowledgeIndex = content.children.findIndex((node) => String(node.className).includes("supervisor-knowledge"));
    check(safetyIndex >= 0 && safetyIndex < knowledgeIndex, "safety strip precedes expandable education");
    const safetyCss = supervisorCss.slice(
      supervisorCss.lastIndexOf(".supervisor-safety-strip {"),
      supervisorCss.indexOf(".supervisor-knowledge", supervisorCss.lastIndexOf(".supervisor-safety-strip {")),
    );
    check(safetyCss.includes("var(--supervisor-cyan)") && !safetyCss.includes("var(--supervisor-error)"), "calm cyan information style");
  });

  await group("KNOWLEDGE_DETAILS", async () => {
    const panel = panelFixture("en").panel;
    const details = walk(panel._container).filter((node) => node.tagName === "DETAILS");
    check(details.length === 5, "exactly five native details");
    check(details.every((detail) => detail.getAttribute("open") === null), "all details closed by default");
    check(details.every((detail) => detail.children[0]?.tagName === "SUMMARY"), "each detail uses native summary");
    check(details.every((detail) => detail.listenerCount("click") === 0), "no JavaScript accordion listeners");
    check(nodesWithClass(panel._container, "supervisor-knowledge-summary-hint").length === 5, "five one-line summary descriptions");
    const renderedCopy = new Set(panel._copyNodes.map(([, key]) => key));
    for (const key of [
      "modeOffDescription",
      "modeShadowDescription",
      "modeActiveDescription",
      "balancedDescription",
      "maximumProfitDescription",
      "highReserveDescription",
      "profileLimitation",
      "permissionMeaning",
      "permissionNotMeaning",
      "rcePlain",
      "tariffPlain",
      "rcemPlain",
      "resultIdleDescription",
      "resultSelectedDescription",
      "resultBlockedDescription",
      "resultUnavailableDescription",
      "resultCommitmentDescription",
      "cannotModbus",
      "cannotMode",
      "cannotOwner",
      "cannotGrant",
      "cannotHandover",
      "cannotLegacy",
      "cannotActive",
      "safetyScope",
    ]) {
      check(renderedCopy.has(key), "legacy educational copy retained " + key);
    }
    check(!componentSource.includes("accordionState") && !componentSource.includes("toggleDetails"), "no custom accordion state");
  });

  await group("FIRST_SCREEN_PRIORITY", async () => {
    const panel = panelFixture("en").panel;
    const content = nodesWithClass(panel._container, "supervisor-content")[0];
    const orderedClasses = content.children.map((node) => String(node.className));
    check(orderedClasses[0].includes("supervisor-hero"), "Hero first");
    check(orderedClasses[1].includes("supervisor-live-grid"), "controls and decision second");
    check(orderedClasses[2].includes("supervisor-policy-section"), "policies before education");
    check(orderedClasses[3].includes("supervisor-how"), "How it works remains visible");
    check(orderedClasses[4].includes("supervisor-safety-strip"), "short safety statement before education");
    check(orderedClasses[5].includes("supervisor-knowledge"), "long education last");
    check(Object.keys(panel._controls).length === 5, "five controls on live surface");
    check(Object.keys(panel._summary).length === 7, "decision remains complete before education");
    check(Object.keys(panel._policyRows).length === 3, "three policies before education");
  });

  await group("AURORA_BACKGROUND_REV28", async () => {
    for (const token of [
      "--supervisor-deep",
      "radial-gradient(circle at 12% 0%",
      "radial-gradient(circle at 88% 3%",
      "supervisor-blob-three",
      "supervisor-points",
      "background-size: 26px 26px",
      "supervisor-vignette",
      "overflow: clip",
      "pointer-events: none",
    ]) {
      check(supervisorCss.includes(token), "rev28 background token " + token);
    }
    const panel = panelFixture().panel;
    const backdrop = nodesWithClass(panel._container, "supervisor-backdrop")[0];
    check(backdrop.getAttribute("aria-hidden") === "true", "decorative background aria hidden");
    check(!supervisorCss.includes("filter: blur"), "no expensive full-screen blur");
    for (const duration of ["24s", "28s", "22s"]) {
      check(supervisorCss.includes(duration), "slow backdrop duration " + duration);
    }
  });

  await group("REDUCED_MOTION_REV28", async () => {
    const mediaStart = supervisorCss.lastIndexOf("@media (prefers-reduced-motion: reduce)");
    const mediaEnd = supervisorCss.indexOf("@container", mediaStart);
    const reduced = supervisorCss.slice(mediaStart, mediaEnd);
    check(mediaStart >= 0 && mediaEnd > mediaStart, "dedicated rev28 reduced-motion block");
    check(reduced.includes(".supervisor-blob") && reduced.includes(".supervisor-core-ring"), "all decorative motion targets listed");
    check(reduced.includes("animation: none"), "decorative animations stop");
    check(reduced.includes("transition: none"), "control transitions stop");
    check(!/flash|pulse/i.test(supervisorCss), "no flashing or aggressive pulse");
  });

  await group("LIGHT_DARK_CONTRAST", async () => {
    for (const token of [
      "--supervisor-base: var(--card-background-color",
      "--supervisor-surface",
      "--supervisor-surface-strong",
      "--supervisor-on-deep",
      "var(--primary-text-color)",
      "var(--secondary-text-color)",
      "var(--divider-color)",
    ]) {
      check(supervisorCss.includes(token), "contrast token " + token);
    }
    check(supervisorCss.includes("color-mix(in srgb, var(--supervisor-surface-strong)"), "theme-aware translucent surfaces");
    check(supervisorCss.includes("color: var(--supervisor-on-deep)"), "bounded text color on deep Hero");
    check(new Set(["#f2b84b", "#49a5ff", "#b07cff"]).size === 3, "three distinct policy base accents");
    check(!/opacity:\s*0(?:;|\s)/.test(supervisorCss), "no fully invisible semantic text");
    const lightStart = supervisorCss.indexOf("@media (prefers-color-scheme: light)");
    const lightEnd = supervisorCss.indexOf("@media (prefers-reduced-motion: reduce)", lightStart);
    const lightTheme = supervisorCss.slice(lightStart, lightEnd);
    check(lightStart >= 0 && lightEnd > lightStart, "dedicated non-inverted light-theme branch");
    for (const token of [
      "--supervisor-page-base: color-mix(in srgb, var(--primary-background-color, var(--supervisor-on-deep)) 94%, var(--supervisor-blue) 6%)",
      "--supervisor-page-cyan-glow: color-mix(in srgb, var(--supervisor-cyan) 5%, transparent)",
      "--supervisor-page-violet-glow: color-mix(in srgb, var(--supervisor-violet) 4%, transparent)",
      ".supervisor-blob { opacity: .08; }",
      ".supervisor-points { opacity: .1; }",
    ]) {
      check(lightTheme.includes(token), "light-theme contrast token " + token);
    }
    check(supervisorCss.includes(".supervisor-info-disabled { border-style: dashed; opacity: 1; }"), "disabled Active documentation retains text contrast");
    check(supervisorCss.includes('.supervisor-policy[data-permitted="false"] { filter: saturate(.68); opacity: 1; }'), "disabled policy retains contrast while reducing saturation");
    check(!/opacity:\s*\.(?:64|72)\b/.test(supervisorCss), "no whole-card opacity contrast loss");
  });

  await group("MOBILE_REV28", async () => {
    for (const width of [1920, 1366, 1024, 768, 390, 360, 320]) {
      check(width >= 320 && supervisorCss.includes("min-width: 0"), "bounded width " + width);
    }
    for (const breakpoint of ["1024px", "768px", "620px", "390px"]) {
      check(supervisorCss.includes("@container (max-width: " + breakpoint + ")"), "responsive breakpoint " + breakpoint);
    }
    const mobile = supervisorCss.slice(supervisorCss.lastIndexOf("@container (max-width: 768px)"));
    check(mobile.includes('.supervisor-flow-step:not(:last-child)::after') && mobile.includes('content: "↓"'), "mobile logical flow vertical");
    check(mobile.includes(".supervisor-policies { grid-template-columns: minmax(0, 1fr); }") || mobile.includes(".supervisor-policies { grid-template-columns: minmax(0, 1fr)"), "policy cards stack");
    check(supervisorCss.includes(".supervisor-knowledge-detail { width: 100%; }"), "details full width");
    check(!/supervisor-scope[^}]*display:\s*none/s.test(supervisorCss), "observation badge never hidden on mobile");
    check(!supervisorCss.includes("overflow-x: auto"), "mobile has no horizontal-scroll escape hatch");
    const undersizedSupervisorText = [...supervisorCss.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g)]
      .filter((match) => Number(match[1]) < 11);
    check(undersizedSupervisorText.length === 0, "no Supervisor text below accepted 11px minimum");
    check(mobile.includes(".supervisor-policy-chip { font-size: 11px"), "mobile policy chips retain 11px minimum");
  });

  await group("FUNCTIONAL_INERTNESS", async () => {
    for (const [name, [start, end, expectedHash]] of Object.entries(EXPECTED_FUNCTIONAL_SEGMENTS)) {
      check(sourceSegmentDigest(cardSource, start, end) === expectedHash, "unchanged functional segment " + name);
    }
    const panel = panelFixture().panel;
    for (const [key, entityId] of Object.entries(EXPECTED_CONTROL_TARGETS)) {
      check(panel._controlKind(key)?.entityId === entityId, "exact control target " + key);
    }
    check((componentSource.match(/callService\.call\(/g) || []).length === 1, "one guarded helper call site");
    check(componentSource.includes('domain: "input_select"') && componentSource.includes('domain: "input_boolean"'), "only helper service domains");
    check(componentSource.includes("this._pending.add(key)") && componentSource.includes("this._attestHelperCommand"), "helper action sequence retained");
    for (const forbidden of ["write_register", "modbus", "owner", "grant", "handover", "force_active"]) {
      const callSite = /callService\.call[^;]+/is.exec(componentSource)?.[0].toLowerCase() || "";
      check(!callSite.includes(forbidden), "no physical authority token " + forbidden);
    }
  });

  if (groupCount !== EXPECTED_GROUP_COUNT) {
    throw new Error(`UI group count ${groupCount}, expected ${EXPECTED_GROUP_COUNT}`);
  }
  if (checkCount !== EXPECTED_CHECK_COUNT) {
    throw new Error(`UI check count ${checkCount}, expected ${EXPECTED_CHECK_COUNT}`);
  }
  console.log(`EMS Supervisor Aurora UI contract: ${groupCount}/${EXPECTED_GROUP_COUNT} groups, ${checkCount}/${EXPECTED_CHECK_COUNT} checks PASS`);
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exitCode = 1;
});
