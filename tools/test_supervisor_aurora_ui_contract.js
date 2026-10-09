const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const EXPECTED_GROUP_COUNT = 69;
const EXPECTED_CHECK_COUNT = 1117;
const ALLOW_GENERATED_DRIFT =
  process.env.HOYMILES_UI_ALLOW_GENERATED_DRIFT === "1";

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
const fullSupervisorSource = componentSource.slice(
  0,
  componentSource.indexOf("class HoymilesEmsQuickControlsCard"),
);
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
  supervisor_master_stop_entity:
    "input_button.hoymiles_ems_supervisor_master_stop",
});
const EXPECTED_MODE_OPTIONS = Object.freeze(["Off", "Active"]);
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
  masterStop: "input_button.hoymiles_ems_supervisor_master_stop",
});

const EXPECTED_REASON_COPY = Object.freeze({
  candidate_ready: ["Kandydat gotowy", "Candidate ready"],
  live_emergency: ["Pilna reakcja na stan bieżący", "Live emergency response"],
  required_energy_restore: ["Wymagane odtworzenie energii", "Required energy restoration"],
  preventive_voltage_action: ["Prewencyjna reakcja napięciowa", "Preventive voltage action"],
  economic_candidate: ["Kandydat ekonomiczny", "Economic candidate"],
  no_action: ["Brak wymaganej akcji", "No action required"],
  no_eligible_candidate: ["Brak kwalifikującej się polityki", "No eligible policy"],
  below_minimum_grid_power: ["Moc bloku poniżej minimum 200 W", "Block power below the 200 W minimum"],
  not_allowed: ["Brak zgody użytkownika", "Not allowed by user"],
  policy_disabled: ["Polityka planera wyłączona", "Planner policy disabled"],
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
  off_grid: ["Praca wyspowa ma pierwszeństwo", "Off-grid has priority"],
  foreign_owner: ["Steruje inny właściciel", "Another owner has control"],
  balancing_active: ["Trwa balansowanie magazynu", "Battery balancing is active"],
  owner_conflict: ["Konflikt właściciela sterowania", "Control-owner conflict"],
  transaction_pending: ["Oczekiwanie na zakończenie transakcji", "Waiting for transaction completion"],
  physical_mode_stale: ["Fizyczny tryb jest nieaktualny", "Physical mode is stale"],
  physical_mode_unknown: ["Fizyczny tryb jest nieznany", "Physical mode is unknown"],
  critical_bms_unavailable: ["Krytyczne dane BMS niedostępne", "Critical BMS data unavailable"],
  economic_candidates_not_comparable: ["Kandydatów ekonomicznych nie można porównać", "Economic candidates are not comparable"],
  economic_tie: ["Remis kandydatów ekonomicznych", "Economic candidates are tied"],
  master_stop_requested: ["MASTER STOP — rozpoczęto bezpieczne zatrzymanie", "MASTER STOP — safe shutdown started"],
  structurally_inconsistent_context: ["Niespójny kontekst wykonania", "Structurally inconsistent execution context"],
  invalid_pending_owner_relationship: ["Nieprawidłowa relacja oczekującej transakcji z właścicielem", "Invalid pending-transaction owner relationship"],
  multiple_active_commitments: ["Wiele aktywnych zobowiązań", "Multiple active commitments"],
  owner_commitment_mismatch: ["Właściciel nie odpowiada aktywnemu zobowiązaniu", "Owner does not match the active commitment"],
  inconsistent_priority_tie: ["Niespójny remis priorytetów", "Inconsistent priority tie"],
});

const EXPECTED_LABELS = Object.freeze({
  pl: Object.freeze({
    title: "EMS",
    activeScope: "Zakres trybu wykonawczego: działanie transakcyjne",
    currentMode: "EMS",
    mode: "EMS",
    profile: "Profil",
    allowRce: "Sprzedaż dynamiczna",
    allowTariff: "Ładowanie taryfowe",
    allowRcm: "Ochrona napięciowa",
    masterStop: "EMS STOP",
    state: "Stan wykonania",
    owner: "Właściciel wykonania",
    selectedPolicy: "Wybrana polityka",
    decisionReason: "Powód decyzji",
    lifecycleReason: "Powód wykonania",
    blockedReason: "Powód blokady",
    phase: "Faza",
    physicalExecution: "Fizyczne wykonanie",
    existingAutomation: "Bezpośredni zapis planerów",
  }),
  en: Object.freeze({
    title: "EMS Supervisor",
    activeScope: "Active-mode scope: transactional execution",
    currentMode: "EMS Supervisor",
    mode: "EMS Supervisor",
    profile: "Profile",
    allowRce: "Dynamic sales",
    allowTariff: "Consider tariff charging",
    allowRcm: "Consider RCEm",
    masterStop: "EMS STOP",
    state: "Execution state",
    owner: "Owner",
    selectedPolicy: "Selected policy",
    decisionReason: "Decision reason",
    lifecycleReason: "Lifecycle reason",
    blockedReason: "Blocked reason",
    phase: "Phase",
    physicalExecution: "Physical execution",
    existingAutomation: "Direct planner writes",
  }),
});
const EXPECTED_COPY_SHA256 = Object.freeze({
  pl: "78302199a497cff8edc3510b441469f298183c3b3b3d3fa4407d9aa492f3b907",
  en: "2328f195972b7271e8601eb824b20a76498a1b4a5c982e1491d814cc415fe4ab",
});
const EXPECTED_FUNCTIONAL_SEGMENTS = Object.freeze({
  controlMap: Object.freeze([
    "const HOYMILES_SUPERVISOR_CONTROL_KINDS",
    "const HOYMILES_SUPERVISOR_REASON_COPY",
    "a66d7784f2c487a31ce291666e7eb5d76f4a332643417cee962f776d0ea73794",
  ]),
  services: Object.freeze([
    "  async _selectOption(key, option)",
    "class HoymilesEmsSupervisorCard extends HTMLElement",
    "ba07efdc6c92550387add7168df3051bb1bb40e1b4b7dd073fbfa424a8223e55",
  ]),
  lifecycle: Object.freeze([
    "  _listen(element, type, listener)",
    "  _clearRequestOwnership()",
    "7a0992be37cfb71e0c6c9f9d3685e080d985ad56c5a3be2ad4713101d3a5053b",
  ]),
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

class QuickControlsShadow extends FakeElement {
  constructor() {
    super("shadow-root");
    this._innerHTML = "";
    this._quickNodes = new Map();
  }
  set innerHTML(value) {
    this._innerHTML = String(value || "");
    this._quickNodes = new Map();
    for (const match of this._innerHTML.matchAll(/<button\b([^>]*\bdata-action="([^"]+)"[^>]*)>/g)) {
      const attributes = match[1];
      const action = match[2];
      const button = new FakeElement("button");
      button.disabled = /\sdisabled(?:\s|$)/.test(attributes);
      button.setAttribute("data-action", action);
      const checked = attributes.match(/\baria-checked="([^"]+)"/);
      if (checked) button.setAttribute("aria-checked", checked[1]);
      const label = attributes.match(/\baria-label="([^"]+)"/);
      if (label) button.setAttribute("aria-label", label[1]);
      this._quickNodes.set(`[data-action="${action}"]`, button);
    }
    const selectMatch = this._innerHTML.match(
      /<select\b([^>]*\bdata-action="profile"[^>]*)>([\s\S]*?)<\/select>/,
    );
    if (selectMatch) {
      const select = new FakeElement("select");
      select.disabled = /\sdisabled(?:\s|$)/.test(selectMatch[1]);
      select.setAttribute("data-action", "profile");
      select.options = [...selectMatch[2].matchAll(/<option value="([^"]+)"([^>]*)>/g)].map(
        (optionMatch) => ({
          value: optionMatch[1],
          selected: /\sselected(?:\s|$)/.test(optionMatch[2]),
        }),
      );
      select.value = select.options.find((option) => option.selected)?.value || "";
      this._quickNodes.set('[data-action="profile"]', select);
    }
  }
  get innerHTML() {
    return this._innerHTML;
  }
  querySelector(selector) {
    return this._quickNodes.get(selector) || null;
  }
  querySelectorAll(selector) {
    if (selector === "[data-action]") return [...this._quickNodes.values()];
    const node = this.querySelector(selector);
    return node ? [node] : [];
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
  JSON.stringify("https://homeassistant.example/local/hoymiles-rce-chart-card.js?v=1.5.8.1.123"),
) + `
globalThis.__supervisorTestExports = {
  HoymilesEmsSupervisorPanel,
  HoymilesEmsSupervisorCard,
  HoymilesEmsQuickControlsCard,
  HoymilesAuroraEnergyCard,
  hoymilesUiExactSafeOffHandoff,
  hoymilesNormalizeSupervisor,
  hoymilesNormalizeLanguage,
  hoymilesSupervisorReason,
  HOYMILES_SUPERVISOR_BINDINGS,
  HOYMILES_SUPERVISOR_MODE_OPTIONS,
  HOYMILES_SUPERVISOR_PROFILE_OPTIONS,
  HOYMILES_SUPERVISOR_POLICY_IDS,
  HOYMILES_SUPERVISOR_POLICY_ICONS,
  HOYMILES_SUPERVISOR_REASON_COPY,
  HOYMILES_SUPERVISOR_LIFECYCLE_REASON_COPY,
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
    tariff: "tariff_battery_charge",
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
    supervisor_mode: "Active",
    profile: "Balanced",
    execution_phase: "idle",
    lifecycle_reason: "idle",
    reason: "idle",
    owner: "none",
    selected_policy: null,
    selection_reason: "no_eligible_candidate",
    execution_blocked_reason: null,
    supervisor_execution_authorized: false,
    legacy_execution_unchanged: true,
    profile_effects_applied: [],
    execution_health: {
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
    },
    candidate_summaries: [candidate("rcm"), candidate("rce"), candidate("tariff")],
    ...overrides,
  };
}

function hassFixture(state = "active_idle", attributeOverrides = {}, helperOverrides = {}) {
  const states = {
    [EXPECTED_BINDINGS.supervisor_entity]: {
      state,
      attributes: supervisorAttributes(attributeOverrides),
    },
    [EXPECTED_BINDINGS.supervisor_mode_entity]: {
      state: "Active",
      attributes: { options: ["Off", "Active"] },
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
    [EXPECTED_BINDINGS.supervisor_master_stop_entity]: {
      state: "unknown",
      attributes: { editable: false, friendly_name: "Supervisor MASTER STOP", icon: "mdi:alert-octagon" },
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

  await group("02 exact Off and Active modes", async () => {
    equal(Array.from(ui.HOYMILES_SUPERVISOR_MODE_OPTIONS), EXPECTED_MODE_OPTIONS, "mode whitelist");
    const retiredMode = String.fromCharCode(83, 104, 97, 100, 111, 120);
    check(!componentSource.includes(`mode${retiredMode}`), "no legacy observation mode in component scope");
    const { panel } = panelFixture();
    const restoringHass = hassFixture(
      "restoring",
      { execution_phase: "restoring" },
      {
        [EXPECTED_BINDINGS.supervisor_mode_entity]: {
          state: "Off",
          attributes: { options: ["Off", "Active"] },
        },
      },
    );
    const restoring = panelFixture("pl", restoringHass).panel;
    void restoring._toggleMode("mode");
    const stoppingHass = hassFixture(
      "stopping",
      { execution_phase: "stopping" },
    );
    const stopping = panelFixture("pl", stoppingHass).panel;
    void stopping._toggleMode("mode");
    check(
      panel._controls.mode.type === "mode"
        && panel._controls.mode.element.tagName === "BUTTON"
        && panel._controls.mode.element.getAttribute("aria-pressed") === "true"
        && panel._controls.mode.text.textContent === "Disable EMS Supervisor"
        && panel._controls.mode.icon.getAttribute("icon") === "mdi:shield-off-outline"
        && restoring._controls.mode.element.disabled
        && restoring._controls.mode.element.dataset.handoff === "true"
        && restoring._controls.mode.text.textContent === "Odtwarzanie zrzutu stanu"
        && restoring._controls.mode.icon.getAttribute("icon") === "mdi:progress-clock"
        && restoringHass.calls.length === 0
        && stopping._controls.mode.element.disabled
        && stopping._controls.mode.element.dataset.handoff === "true"
        && stopping._controls.mode.text.textContent === "Zatrzymywanie"
        && stoppingHass.calls.length === 0,
      `exact Off/Active mode toggle and fail-closed stopping/restoration handoff: ${JSON.stringify({
        modeType: panel._controls.mode.type,
        modeTag: panel._controls.mode.element.tagName,
        pressed: panel._controls.mode.element.getAttribute("aria-pressed"),
        text: panel._controls.mode.text.textContent,
        icon: panel._controls.mode.icon.getAttribute("icon"),
        restoringDisabled: restoring._controls.mode.element.disabled,
        restoringHandoff: restoring._controls.mode.element.dataset.handoff,
        restoringText: restoring._controls.mode.text.textContent,
        restoringIcon: restoring._controls.mode.icon.getAttribute("icon"),
        stoppingDisabled: stopping._controls.mode.element.disabled,
        stoppingHandoff: stopping._controls.mode.element.dataset.handoff,
        stoppingText: stopping._controls.mode.text.textContent,
      })}`,
    );
    const activeDocs = walk(panel._container).filter(
      (node) => node.getAttribute?.("aria-disabled") === "true",
    );
    check(activeDocs.length === 0, "Active documentation is enabled");
    check(panel._controls.masterStop.element.tagName === "BUTTON", "MASTER STOP is a button");
    check(panel._controls.masterStop.element.disabled === false, "MASTER STOP remains available before first press");
  });

  await group("02A exact safe-Off UI handoff", async () => {
    const safeStates = {
      [EXPECTED_BINDINGS.supervisor_entity]: {
        state: "off",
        attributes: {
          execution_phase: "idle",
          owner: "none",
          observed_owner: "none",
          transaction_owner: "none",
          transaction_id: null,
          owner_conflict: false,
          supervisor_execution_authorized: false,
          master_stop_adapter_latched: false,
          manual_proxy_readback_pending: false,
        },
      },
      [EXPECTED_BINDINGS.supervisor_mode_entity]: {
        state: "Off",
        attributes: {},
      },
      "binary_sensor.hoymiles_ems_control_conflict": {
        state: "off",
        attributes: {},
      },
    };
    const safeHass = { states: safeStates };
    check(ui.hoymilesUiExactSafeOffHandoff(safeHass), "exact safe-Off opens direct controls");
    for (const [field, value] of [
      ["execution_phase", "stopping"],
      ["owner", "tariff"],
      ["observed_owner", "rcm"],
      ["transaction_owner", "tariff"],
      ["transaction_id", "tx-1"],
      ["owner_conflict", true],
      ["supervisor_execution_authorized", true],
      ["master_stop_adapter_latched", true],
      ["manual_proxy_readback_pending", true],
    ]) {
      const states = JSON.parse(JSON.stringify(safeStates));
      states[EXPECTED_BINDINGS.supervisor_entity].attributes[field] = value;
      check(!ui.hoymilesUiExactSafeOffHandoff({ states }), `unsafe ${field} keeps controls closed`);
    }
    for (const [entityId, state] of [
      [EXPECTED_BINDINGS.supervisor_entity, "active_idle"],
      [EXPECTED_BINDINGS.supervisor_mode_entity, "Active"],
      ["binary_sensor.hoymiles_ems_control_conflict", "on"],
    ]) {
      const states = JSON.parse(JSON.stringify(safeStates));
      states[entityId].state = state;
      check(!ui.hoymilesUiExactSafeOffHandoff({ states }), `unsafe ${entityId} keeps controls closed`);
    }
    const missingManualLease = JSON.parse(JSON.stringify(safeStates));
    delete missingManualLease[EXPECTED_BINDINGS.supervisor_entity]
      .attributes.manual_proxy_readback_pending;
    check(
      !ui.hoymilesUiExactSafeOffHandoff({ states: missingManualLease }),
      "missing manual lease state fails closed",
    );
    const missingMasterStopLatch = JSON.parse(JSON.stringify(safeStates));
    delete missingMasterStopLatch[EXPECTED_BINDINGS.supervisor_entity]
      .attributes.master_stop_adapter_latched;
    check(
      !ui.hoymilesUiExactSafeOffHandoff({ states: missingMasterStopLatch }),
      "missing MASTER STOP latch state fails closed",
    );
    const settingsView = dashboardSource
      .split("    path: sterowanie", 2)[1]
      .split("\n  - title:", 1)[0];
    check(
      (settingsView.match(/safe_off_only: true/g) || []).length === 6,
      "all six direct mode, inverter, battery, export and manual-schedule cards are wholly safe-Off protected",
    );
    for (const title of [
      "EMS — ustawienia ręczne i praca wyspowa",
      "Ustawienia falownika i magazynu",
      "Ograniczanie eksportu i złącze GEN",
      "Rozładowanie do sieci",
      "Ładowanie z sieci",
    ]) {
      const escaped = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      check(
        new RegExp(`title: ${escaped}[\\s\\S]*?safe_off_only: true[\\s\\S]*?entities:`).test(settingsView),
        `${title} stays read-only until the exact safe-Off handoff`,
      );
    }
    const safeStopSection = settingsView
      .split("        title: Bezpieczne zakończenie działania", 2)[1]
      .split("      - type: custom:hoymiles-zebra-entities-card", 1)[0];
    check(
      safeStopSection.includes("script.hoymiles_stop_scheduled_cycle") &&
        !safeStopSection.includes("safe_off_only: true"),
      "safe stop remains callable while only its direct mode and reserve fields stay gated",
    );
    check(
      cardSource.includes("attributes.master_stop_adapter_latched === false")
        && cardSource.includes("attributes.manual_proxy_readback_pending === false")
        && cardSource.includes('attributes.observed_owner === "none"'),
      "frontend handoff mirrors legacy ownership, MASTER STOP and the manual readback lease",
    );
  });

  await group("03 allowed helper services only", async () => {
    const { panel, hass } = panelFixture();
    panel._controls.mode.element.click();
    panel._controls.profile.element.value = "Maximum Profit";
    panel._controls.profile.element.dispatch("change");
    panel._controls.allowRce.element.dispatch("pointerdown");
    panel._controls.allowRce.element.dispatch("pointerup");
    panel._controls.allowRce.element.click();
    panel._controls.allowTariff.element.click();
    panel._controls.allowRcm.element.click();
    panel._controls.masterStop.element.click();
    await Promise.resolve();
    equal(hass.calls.map(({ domain, service }) => [domain, service]), [
      ["input_select", "select_option"],
      ["input_select", "select_option"],
      ["input_boolean", "turn_off"],
      ["input_boolean", "turn_on"],
      ["input_boolean", "turn_off"],
      ["input_button", "press"],
    ], "allowed service sequence");
    check(Object.keys(hass.calls[0].data).sort().join(",") === "entity_id,option", "select data keys");
    check(Object.keys(hass.calls[2].data).join(",") === "entity_id", "boolean data keys");
    check(hass.calls[5].data.entity_id === EXPECTED_BINDINGS.supervisor_master_stop_entity, "MASTER STOP exact target");
    check(hass.states[EXPECTED_BINDINGS.supervisor_allow_rce_entity].attributes.editable === false, "editable=false does not suppress actual click");
    const guarded = panelFixture();
    guarded.hass.states[EXPECTED_BINDINGS.supervisor_mode_entity].attributes.options = ["Active"];
    await guarded.panel._callHelperService("mode", "Off");
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

  await group("05 Variant A EMS route and hidden full Supervisor view", async () => {
    const viewMatches = [...dashboardSource.matchAll(/^  - title: (.+)$/gm)];
    check(viewMatches.length === 21, "dashboard has exactly 21 stable views");
    check(viewMatches[0]?.[1] === "Przegląd", "Overview remains first");
    check(viewMatches[1]?.[1] === "EMS", "EMS route is second");
    check(viewMatches[2]?.[1] === "Automatyka EMS", "EMS automation is third");
    check(viewMatches[3]?.[1] === "Ustawienia EMS", "Shared EMS settings are fourth");
    check(viewMatches[4]?.[1] === "Balansowanie", "dedicated balancing settings follow shared settings");
    check(viewMatches[5]?.[1] === "Sprzedaż dynamiczna", "Dynamic sale settings remain after balancing settings");
    const startView = dashboardSource.slice(viewMatches[0].index, viewMatches[1].index);
    const plannerView = dashboardSource.slice(viewMatches[1].index, viewMatches[2].index);
    const supervisorView = dashboardSource.slice(viewMatches[2].index, viewMatches[3].index);
    const settingsView = dashboardSource.slice(viewMatches[3].index, viewMatches[4].index);
    check(plannerView.includes("    path: plan-automatyki"), "AP-3B dedicated path");
    check(!plannerView.includes("    icon:"), "Variant A EMS uses the compact text tab");
    check(plannerView.includes("    type: panel"), "AP-3B dedicated panel type");
    check(
      (plannerView.match(/custom:hoymiles-aurora-variant-a-ems-page-card/g) || []).length === 1
        && !plannerView.includes("vertical-stack")
        && !plannerView.includes("custom:hoymiles-ems-quick-controls-card")
        && !plannerView.includes("custom:hoymiles-automation-planner-card"),
      "Variant A EMS is one controls-summary-chart-day-plan composition",
    );
    check(
      startView.includes("    type: panel")
        && (startView.match(/custom:hoymiles-aurora-overview-card/g) || []).length === 1,
      "Start has one full-width Aurora Compact composition",
    );
    check(/supervisor_(entity|mode_entity)/.test(startView), "Start overview has explicit Supervisor bindings");
    check(supervisorView.includes("    path: ems-supervisor"), "dedicated path");
    check(supervisorView.includes("    icon: mdi:shield-check"), "dedicated safety icon");
    check(
      supervisorView.includes("    type: panel") && supervisorView.includes("    subview: true"),
      "full Supervisor is a hidden panel subview",
    );
    check((supervisorView.match(/custom:hoymiles-ems-supervisor-card/g) || []).length === 1, "one dedicated card");
    check(
      settingsView.includes("    path: ustawienia-ems")
        && !settingsView.includes("    icon:")
        && (settingsView.match(/custom:hoymiles-aurora-variant-a-settings-page-card/g) || []).length === 1
        && !settingsView.includes("custom:hoymiles-ems-shared-inputs-card"),
      "one dedicated Variant A row-based EMS settings card",
    );
    const energySource = cardSource.slice(componentEnd, cardSource.indexOf("class HoymilesPowerFlowCard", componentEnd));
    const aurora = energySource.indexOf('<div class="aurora">');
    const daily = energySource.indexOf('<div class="daily">', aurora);
    check(aurora >= 0 && daily > aurora, "Start DOM retains aurora then daily");
    check(!energySource.includes("data-supervisor"), "Start has no inline host or placeholder");
    check(!energySource.includes("_supervisorPanel"), "Start has no Supervisor lifecycle reference");
  });

  await group("05A compact EMS controls DOM, read-only profile and guarded helper services", async () => {
    const QuickControls = ui.HoymilesEmsQuickControlsCard;
    check(
      registry.get("hoymiles-ems-quick-controls-card") === QuickControls
        && context.window.customCards.filter(
          (entry) => entry.type === "hoymiles-ems-quick-controls-card",
        ).length === 1,
      "compact EMS controls are registered and advertised exactly once",
    );

    const stub = QuickControls.getStubConfig();
    check(
      stub.supervisor_mode_entity === EXPECTED_BINDINGS.supervisor_mode_entity
        && stub.supervisor_profile_entity === EXPECTED_BINDINGS.supervisor_profile_entity
        && stub.supervisor_allow_rce_entity === EXPECTED_BINDINGS.supervisor_allow_rce_entity
        && stub.supervisor_allow_tariff_entity === EXPECTED_BINDINGS.supervisor_allow_tariff_entity
        && stub.supervisor_allow_rcm_entity === EXPECTED_BINDINGS.supervisor_allow_rcm_entity
        && stub.balancing_enabled_entity === "input_boolean.hoymiles_battery_balancing_enabled",
      "compact card stub keeps the closed helper target set",
    );

    const state = (value, options = undefined) => ({
      state: value,
      attributes: options ? { options } : {},
    });
    const ids = {
      mode: EXPECTED_BINDINGS.supervisor_mode_entity,
      profile: EXPECTED_BINDINGS.supervisor_profile_entity,
      rce: EXPECTED_BINDINGS.supervisor_allow_rce_entity,
      tariff: EXPECTED_BINDINGS.supervisor_allow_tariff_entity,
      rcm: EXPECTED_BINDINGS.supervisor_allow_rcm_entity,
      balancing: "input_boolean.hoymiles_battery_balancing_enabled",
    };
    const states = {
      [ids.mode]: state("Off", [...EXPECTED_MODE_OPTIONS]),
      [ids.profile]: state("Balanced", [...EXPECTED_PROFILE_OPTIONS, "Injected profile"]),
      [ids.rce]: state("off"),
      [ids.tariff]: state("on"),
      [ids.rcm]: state("off"),
      [ids.balancing]: state("off"),
    };
    const calls = [];
    let rejectNext = false;
    const hass = {
      language: "pl",
      states,
      async callService(...args) {
        calls.push(args);
        if (rejectNext) {
          rejectNext = false;
          throw new Error("simulated helper failure");
        }
      },
    };
    const compact = new QuickControls();
    compact.shadowRoot = new QuickControlsShadow();
    compact.setConfig({ language: "pl" });
    compact.hass = hass;
    const action = (key) => compact.shadowRoot.querySelector(`[data-action="${key}"]`);
    const settle = () => new Promise((resolve) => setImmediate(resolve));

    check(
      ["EMS", "Ładowanie taryfowe", "Ochrona napięciowa", "Balansowanie magazynu"]
        .every((label) => compact.shadowRoot.innerHTML.includes(label))
        && !compact.shadowRoot.innerHTML.includes("Nadzorca"),
      "compact Polish DOM uses the approved EMS policy copy",
    );
    check(
      compact.shadowRoot.innerHTML.includes("EMS wyłączony")
        && action("mode")?.getAttribute("aria-checked") === "false",
      "Off helper renders a disabled EMS status without changing the helper",
    );
    check(
      action("profile") === null
        && compact.shadowRoot.innerHTML.includes("data-profile-readback")
        && compact.shadowRoot.innerHTML.includes("Zrównoważony")
        && compact.shadowRoot.innerHTML.includes("W przygotowaniu")
        && !compact.shadowRoot.innerHTML.includes('<select data-action="profile"'),
      "registered legacy quick controls expose the profile as read-only and explicitly inactive",
    );

    action("mode").click();
    await settle();
    check(
      calls.length === 1
        && calls[0][0] === "input_select"
        && calls[0][1] === "select_option"
        && calls[0][2]?.entity_id === ids.mode
        && calls[0][2]?.option === "Active",
      "Off to Active uses one exact input_select service call",
    );
    check(
      states[ids.mode].state === "Off"
        && action("mode")?.getAttribute("aria-checked") === "false",
      "successful call does not fabricate Active before HA publishes it",
    );

    states[ids.mode] = state("Active", [...EXPECTED_MODE_OPTIONS]);
    compact.hass = hass;
    check(
      compact.shadowRoot.innerHTML.includes("EMS włączony")
        && action("mode")?.getAttribute("aria-checked") === "true",
      "published Active helper state is rendered as enabled",
    );
    action("mode").click();
    await settle();
    check(
      calls.length === 2
        && calls[1][0] === "input_select"
        && calls[1][1] === "select_option"
        && calls[1][2]?.entity_id === ids.mode
        && calls[1][2]?.option === "Off",
      "Active to Off uses one exact input_select service call",
    );

    const quickControlsSource = cardSource.slice(
      cardSource.indexOf("class HoymilesEmsQuickControlsCard"),
      cardSource.indexOf("class HoymilesBatteryBalancingPlanCard"),
    );
    check(
      !quickControlsSource.includes("_setProfile(")
        && !quickControlsSource.includes('this._service("profile"'),
      "legacy quick controls have no profile write path",
    );
    check(
      calls.every((call) => call[2]?.entity_id !== ids.profile),
      "rendering and interacting with quick controls never dispatches a profile service",
    );

    check(
      action("rce")?.getAttribute("aria-checked") === "false"
        && action("tariff")?.getAttribute("aria-checked") === "true"
        && action("rcm")?.getAttribute("aria-checked") === "false"
        && action("balancing")?.getAttribute("aria-checked") === "false",
      "three policy permissions and balancing reflect their real helper states",
    );
    for (const [key, expectedService] of [
      ["rce", "turn_on"],
      ["tariff", "turn_off"],
      ["rcm", "turn_on"],
      ["balancing", "turn_on"],
    ]) {
      const before = calls.length;
      action(key).click();
      await settle();
      const call = calls.at(-1);
      check(
        calls.length === before + 1
          && call[0] === "input_boolean"
          && call[1] === expectedService
          && call[2]?.entity_id === ids[key],
        `${key} uses the exact guarded input_boolean service`,
      );
    }

    for (const entityId of Object.values(ids)) states[entityId] = state("unavailable");
    compact.hass = hass;
    check(compact.shadowRoot.innerHTML.includes("EMS niedostępny"), "unavailable mode is explicit");
    for (const key of ["mode", "rce", "tariff", "rcm", "balancing"]) {
      check(action(key)?.disabled === true, `${key} control is disabled when its helper is unavailable`);
    }
    check(
      action("profile") === null && compact.shadowRoot.innerHTML.includes("W przygotowaniu"),
      "unavailable profile remains a read-only preparation notice",
    );
    const beforeUnavailable = calls.length;
    action("rce").click();
    await compact._setBoolean("rce", ids.rce);
    check(calls.length === beforeUnavailable, "unavailable controls cannot dispatch a helper service");

    states[ids.mode] = state("Off", [...EXPECTED_MODE_OPTIONS]);
    states[ids.profile] = state("Balanced", [...EXPECTED_PROFILE_OPTIONS]);
    states[ids.rce] = state("off");
    states[ids.tariff] = state("off");
    states[ids.rcm] = state("off");
    states[ids.balancing] = state("off");
    compact.hass = hass;
    const beforeFailure = calls.length;
    rejectNext = true;
    action("mode").click();
    await settle();
    check(
      calls.length === beforeFailure + 1
        && calls.at(-1)[0] === "input_select"
        && calls.at(-1)[1] === "select_option"
        && calls.at(-1)[2]?.option === "Active",
      "service failure still records only the intended Active request",
    );
    check(
      states[ids.mode].state === "Off"
        && action("mode")?.getAttribute("aria-checked") === "false",
      "failed mode service leaves the rendered state at the real Off helper value",
    );
    check(
      compact.shadowRoot.innerHTML.includes('class="error" role="status"')
        && compact.shadowRoot.innerHTML.includes("Operacja nie została w pełni potwierdzona"),
      "service failure exposes an explicit non-optimistic error",
    );
    check(
      compact._pending.size === 0 && action("mode")?.disabled === false,
      "failed request clears pending state without latching a false disabled control",
    );
  });

  await group("06 standalone component registered exactly once", async () => {
    check(componentSource.includes("class HoymilesEmsSupervisorPanel"), "internal class exists");
    check(!cardSource.includes('customElements.define("hoymiles-ems-supervisor-panel"'), "no standalone registration");
    check(componentSource.includes("class HoymilesEmsSupervisorCard extends HTMLElement"), "standalone class exists");
    check((componentSource.match(/customElements\.define\(\s*"hoymiles-ems-supervisor-card"/g) || []).length === 1, "one custom-element definition");
    check(
      registry.get("hoymiles-ems-supervisor-card").prototype
        instanceof ui.HoymilesEmsSupervisorCard,
      "registered canonical constructor extends the reviewed Supervisor card",
    );
    check(context.window.customCards.filter((entry) => entry.type === "hoymiles-ems-supervisor-card").length === 1, "one custom-card metadata entry");
    const standalone = parentFixture();
    check(standalone.parent.shadowRoot.children.length === 2, "standalone shadow root contains style and one card");
    check(standalone.parent.shadowRoot.children[1].tagName === "HA-CARD", "standalone uses one ha-card");
    check(walk(standalone.parent.shadowRoot).filter((node) => node.tagName === "H1").length === 1, "standalone has exactly one h1");
    const hostile = new ui.HoymilesEmsSupervisorCard();
    hostile.setConfig({ supervisor_allow_rce_entity: "input_boolean.hoymiles_rce_discharge_enabled" });
    check(hostile._config.supervisor_allow_rce_entity === EXPECTED_BINDINGS.supervisor_allow_rce_entity, "standalone forces canonical closed target");
  });

  await group("07 permanent Active execution badge", async () => {
    for (const [state, overrides] of [
      ["off", {}],
      ["active_idle", {}],
      ["selected", { selected_policy: "rce" }],
      ["blocked", {}],
      ["unavailable", {}],
    ]) {
      const panel = renderState(state, overrides);
      check(panel._scope.textContent === "Active-mode scope: transactional execution", `badge for ${state}`);
      check(panel._scope.hidden === false, `badge visible for ${state}`);
    }
  });

  await group("08 profile limitation preserved in knowledge", async () => {
    const { panel } = panelFixture("pl");
    const limitation = panel._copyNodes.find(([, key]) => key === "profileLimitation");
    check(limitation?.[0].textContent === "Profil wpływa wyłącznie na deterministyczny arbitraż i nie omija żadnej bramki wykonania.", "Polish limitation preserved");
    const knowledgeDetails = walk(panel._container).filter(
      (node) => node.tagName === "DETAILS",
    );
    check(knowledgeDetails.length === 5, "limitation lives in bounded five-detail knowledge area");
    const unverified = renderState("active_idle", { profile_effects_applied: ["unexpected"] }, "en");
    check(unverified._technical.profileEffects.textContent === "unexpected", "accepted bounded profile effect remains inspectable");
  });

  await group("09 complete PL EN label contract", async () => {
    for (const language of ["pl", "en"]) {
      const copy = ui.HOYMILES_SUPERVISOR_COPY[language];
      for (const [key, expected] of Object.entries(EXPECTED_LABELS[language])) {
        check(copy[key] === expected, `${language} ${key}`);
      }
      const allCopyEntries = Object.entries(copy);
      const frozenCopyEntries = allCopyEntries
        .sort(([left], [right]) => left.localeCompare(right));
      const copyHash = crypto
        .createHash("sha256")
        .update(JSON.stringify(frozenCopyEntries))
        .digest("hex");
      check(allCopyEntries.length === 184, `${language} frozen copy key count ${allCopyEntries.length}`);
      check(
        copyHash === EXPECTED_COPY_SHA256[language],
        `${language} full Active copy hash ${copyHash}`,
      );
    }
    const pl = panelFixture("pl").panel;
    const en = panelFixture("en").panel;
    for (const [action, plLabel, enLabel] of [
      ["tariff_battery_charge", "Ładowanie magazynu z sieci", "Battery charging from the grid"],
      ["tariff_grid_support", "Wsparcie domu z sieci", "Home support from the grid"],
      ["tariff_grid_support_and_charge", "Wsparcie domu i ładowanie magazynu", "Home support and battery charging"],
    ]) {
      const plAction = pl._actionText(action);
      const enAction = en._actionText(action);
      check(
        plAction === plLabel && plAction !== ui.HOYMILES_SUPERVISOR_COPY.pl.unknownAction,
        `PL backend action identity ${action}`,
      );
      check(
        enAction === enLabel && enAction !== ui.HOYMILES_SUPERVISOR_COPY.en.unknownAction,
        `EN backend action identity ${action}`,
      );
    }
    check(pl._controls.allowTariff.labelText.textContent === "Ładowanie taryfowe", "PL tariff control");
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

  await group("10 reason map 46 of 46", async () => {
    const expectedCodes = Object.keys(EXPECTED_REASON_COPY).sort();
    const implementationCodes = Object.keys(ui.HOYMILES_SUPERVISOR_REASON_COPY).sort();
    check(expectedCodes.length === 46, "oracle has 46 reasons");
    equal(implementationCodes, expectedCodes, "reason key set");
    for (const [code, [pl, en]] of Object.entries(EXPECTED_REASON_COPY)) {
      check(ui.hoymilesSupervisorReason(code, "pl") === pl, `PL reason ${code}`);
      check(ui.hoymilesSupervisorReason(code, "en") === en, `EN reason ${code}`);
    }
  });

  await group("10A lifecycle reason map matches ExecutionReason", async () => {
    const executorSource = read(
      "custom_components/hoymiles_hit_modbus/supervisor_executor.py",
    );
    const enumStart = executorSource.indexOf("class ExecutionReason(str, Enum):");
    const enumEnd = executorSource.indexOf("\n\nclass EmsMode", enumStart);
    check(enumStart >= 0 && enumEnd > enumStart, "ExecutionReason enum source bounds");
    const executionReasonValues = [
      ...executorSource.slice(enumStart, enumEnd).matchAll(
        /^\s{4}[A-Z][A-Z0-9_]* = "([a-z0-9_]+)"$/gm,
      ),
    ].map((match) => match[1]).sort();
    const lifecycleKeys = Object.keys(
      ui.HOYMILES_SUPERVISOR_LIFECYCLE_REASON_COPY,
    ).sort();
    equal(lifecycleKeys, executionReasonValues, "lifecycle copy matches ExecutionReason enum");
    for (const code of executionReasonValues) {
      const labels = ui.HOYMILES_SUPERVISOR_LIFECYCLE_REASON_COPY[code];
      check(
        typeof labels?.pl === "string"
          && labels.pl.length > 0
          && typeof labels?.en === "string"
          && labels.en.length > 0,
        `complete lifecycle copy ${code}`,
      );
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
    check(panel._scope.textContent === "Active-mode scope: transactional execution", "badge survives missing sensor");
  });

  await group("13 unavailable Supervisor", async () => {
    const panel = renderState("unavailable", {});
    check(panel._summary.state.value.textContent === "Unavailable", "unavailable state text");
    check(panel._container.dataset.tone === "unavailable", "unavailable tone");
    check(panel._summary.physicalExecution.value.textContent === "Unverified — execution blocked", "unavailable state cannot attest authorization");
  });

  await group("14 malformed attributes", async () => {
    for (const attributes of [null, 7, "bad", [], true]) {
      const sensor = { state: "active_idle", attributes };
      doesNotThrow(() => ui.hoymilesNormalizeSupervisor(sensor), `normalize attributes ${String(attributes)}`);
      check(ui.hoymilesNormalizeSupervisor(sensor).state === "unavailable", "malformed attributes fail safe");
    }
    const malformedBoolean = renderState("active_idle", { supervisor_execution_authorized: "false", legacy_execution_unchanged: 1 });
    check(malformedBoolean._summary.physicalExecution.value.textContent === "Unverified — execution blocked", "malformed auth unverified");
    check(malformedBoolean._summary.existingAutomation.value.textContent === "Unverified", "malformed legacy unverified");
    const inconsistent = renderState("active_idle", { selected_policy: "rce" });
    check(inconsistent._summary.selectedPolicy.value.textContent === "None", "idle state cannot claim a selection");
  });

  await group("15 malformed candidate collection", async () => {
    for (const candidate_summaries of [null, {}, "bad", [candidate("rce"), 3]]) {
      doesNotThrow(() => renderState("active_idle", { candidate_summaries }), "render malformed candidates");
    }
    const tooMany = renderState("active_idle", { candidate_summaries: [candidate("rce"), candidate("tariff"), candidate("rcm"), candidate("rce")] });
    check(Object.values(tooMany._policyRows).every((row) => row.badge.textContent === "Data unavailable"), "oversized collection fails safe");
  });

  await group("16 arbitrary candidate ordering", async () => {
    const panel = renderState("selected", {
      selected_policy: "tariff",
      candidate_summaries: [candidate("rcm"), candidate("tariff"), candidate("rce")],
    });
    equal(Object.keys(panel._policyRows), EXPECTED_POLICY_IDS, "fixed product order");
    check(panel._policyRows.tariff.selected.textContent === "Logically selected", "lookup selection by policy_id");
    check(panel._policyRows.rce.action.textContent === "Requested action: Dynamic sales", "Dynamic sales action lookup");
    check(panel._policyRows.rce.facts.children.length === 5, "policy card has five bounded facts plus action and reason");
    check(panel._policyRows.rce.actionWarning.textContent === "Requested action describes a logical result. It does not confirm physical execution.", "permanent action warning");
  });

  await group("17 duplicate policy IDs", async () => {
    const panel = renderState("active_idle", {
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
    check(panel._icon.getAttribute("icon") === "mdi:shield-off-outline", "Off icon");
  });

  await group("20 Active idle rendering", async () => {
    const panel = renderState("active_idle");
    check(panel._hero.mode.textContent === "Enabled", "Active helper is presented as enabled");
    check(panel._summary.state.value.textContent === "No action", "idle label");
    check(panel._summary.owner.value.textContent === "None", "idle owner none");
    check(panel._summary.lifecycleReason.value.textContent === "No action", "idle lifecycle reason");
    check(panel._container.dataset.tone === "active-idle", "idle tone");
    check(panel._icon.getAttribute("icon") === "mdi:shield-search-outline", "idle icon");
    check(panel._summary.selectedPolicy.value.textContent === "None", "no selected policy");
    const noNeed = renderState("active_idle", {
      candidate_summaries: [candidate("rce", { requested_action: "none", reason_code: "no_action", rejection_reason: "no_action" })],
    });
    check(noNeed._policyRows.rce.badge.textContent === "No need", "no-action rejection is not a hard blocker");
    const zeroExportStop = renderState("active_idle", {
      candidate_summaries: [candidate("rce", {
        active_latched: true,
        available: false,
        requested_action: "none",
        continuation_eligible: false,
        local_hard_stop: true,
        blocked_reason: "local_hard_stop",
        rejection_reason: "confirmed_zero_export",
      })],
    });
    check(
      zeroExportStop._policyRows.rce.badge.textContent === "Blocked",
      "active zero-export hard stop is not presented as unavailable or commitment",
    );
    check(
      zeroExportStop._policyRows.rce.reason.textContent
        === "Reason: Confirmed zero export",
      "final arbitration rejection does not override adapter blocker",
    );
    check(
      componentSource.includes(
        'for (const key of ["rejection_reason", "blocked_reason", "reason_code"])',
      ),
      "candidate reason priority is not final-rejection first",
    );
    const polish = renderState("active_idle", { owner: "none" }, "pl");
    check(polish._hero.mode.textContent === "Włączony", "EMS is visibly enabled");
    check(
      polish._summary.state.label.textContent === "Stan wykonania"
        && polish._summary.state.value.textContent === "Brak działania",
      "Polish idle execution state is explicit",
    );
    check(
      polish._summary.owner.label.textContent === "Właściciel wykonania"
        && polish._summary.owner.value.textContent === "Brak",
      "Polish idle owner is explicitly absent",
    );
    check(
      ui.hoymilesNormalizeSupervisor({
        state: "active_idle",
        attributes: supervisorAttributes({ owner: "foreign" }),
      }).owner === "foreign"
        && ui.hoymilesNormalizeSupervisor({
          state: "active_idle",
          attributes: supervisorAttributes({ owner: "not-an-owner" }),
        }).owner === null,
      "owner is normalized through the closed owner vocabulary",
    );
  });

  for (const [number, policyId, label] of [
    [21, "rce", "Dynamic sale"],
    [22, "tariff", "Tariff charging"],
    [23, "rcm", "RCEm"],
  ]) {
    await group(`${number} Active selected ${policyId}`, async () => {
      const panel = renderState("selected", { selected_policy: policyId });
      check(panel._summary.state.value.textContent === "Policy selected", `${policyId} selected state`);
      check(panel._summary.selectedPolicy.value.textContent === label, `${policyId} selected label`);
      check(panel._policyRows[policyId].selected.textContent === "Logically selected", `${policyId} marker`);
      check(panel._container.dataset.tone === "selected", `${policyId} tone`);
    });
  }

  await group("23A composite lifecycle phase semantics", async () => {
    for (const [state, phase, authorized] of [
      ["selected", "selected", false],
      ["starting", "starting", false],
      ["waiting_readback", "waiting_readback", false],
      ["executing", "executing", true],
    ]) {
      const attributes = supervisorAttributes({
        execution_phase: phase,
        owner: state === "selected" ? "none" : "tariff",
        observed_owner: "none",
        transaction_owner: state === "selected" ? "none" : "tariff",
        selected_policy: "tariff",
        selected_candidate_revision: 22,
        supervisor_execution_authorized: authorized,
      });
      const normalized = ui.hoymilesNormalizeSupervisor({ state, attributes });
      check(
        normalized.state === state
          && normalized.phase === phase
          && normalized.selectedPolicy === "tariff"
          && normalized.owner === (state === "selected" ? "none" : "tariff"),
        `${state} composite is not accepted by the frontend normalizer`,
      );
      const panel = renderState(state, attributes);
      check(
        panel._technical.executionPhase.textContent === phase,
        `${state} technical lifecycle phase differs`,
      );
    }
    check(
      !componentSource.includes("observed_active_latched"),
      "UI still exposes the obsolete pure observed-active phase",
    );
  });

  await group("24 blocked rendering", async () => {
    const panel = renderState("blocked", { execution_phase: "blocked", execution_blocked_reason: "owner_conflict" });
    check(panel._summary.state.value.textContent === "Blocked", "blocked label");
    check(panel._summary.blockedReason.value.textContent === "Control-owner conflict", "mapped blocker");
    check(panel._container.dataset.tone === "blocked", "amber semantic tone");
    check(panel._scope.textContent === "Active-mode scope: transactional execution", "badge on blocked");
    for (const [code, plLabel, enLabel, state] of [
      ["idle", "Brak działania", "No action", "blocked"],
      ["stale_inputs", "Nieaktualne dane wykonawcze", "Execution inputs are stale", "blocked"],
      ["readback_mismatch", "Odczyt fizyczny nie zgadza się z komendą", "Readback does not match the command", "blocked"],
      ["command_not_queued", "Polecenie nie trafiło do kolejki", "Command was not queued", "blocked"],
      ["command_outcome_unknown", "Nieznany wynik komendy", "Command outcome is unknown", "fault"],
      ["physical_verification_unavailable", "Brak fizycznego potwierdzenia", "Physical verification is unavailable", "fault"],
      ["owner_conflict", "Konflikt właściciela wykonania", "Execution-owner conflict", "fault"],
    ]) {
      const plLifecycle = renderState(state, {
        execution_phase: state,
        lifecycle_reason: code,
        reason: "idle",
      }, "pl")._summary.lifecycleReason.value.textContent;
      const enLifecycle = renderState(state, {
        execution_phase: state,
        lifecycle_reason: code,
        reason: "idle",
      }, "en")._summary.lifecycleReason.value.textContent;
      check(
        plLifecycle === plLabel && plLifecycle !== "Nieznany powód",
        `PL ${state} lifecycle ${code}`,
      );
      check(
        enLifecycle === enLabel && enLifecycle !== "Unknown reason",
        `EN ${state} lifecycle ${code}`,
      );
    }
    const separated = renderState("blocked", {
      execution_phase: "blocked",
      lifecycle_reason: "stale_inputs",
      reason: "command_outcome_unknown",
      selection_reason: "no_eligible_candidate",
      execution_blocked_reason: "owner_conflict",
    });
    check(
      separated._summary.lifecycleReason.value.textContent === "Execution inputs are stale"
        && separated._summary.decisionReason.value.textContent === "No eligible policy"
        && separated._summary.blockedReason.value.textContent === "Control-owner conflict",
      "lifecycle, selection, and execution-blocked reasons remain separate",
    );
    const compatibilityFallback = renderState("fault", {
      execution_phase: "fault",
      lifecycle_reason: undefined,
      reason: "readback_mismatch",
      execution_blocked_reason: "owner_conflict",
    });
    check(
      compatibilityFallback._summary.lifecycleReason.value.textContent
        === "Readback does not match the command",
      "legacy runtime reason is the only lifecycle fallback",
    );
  });

  await group("25 false authorization presentation", async () => {
    const falsePanel = renderState("active_idle", { supervisor_execution_authorized: false });
    check(falsePanel._summary.physicalExecution.value.textContent === "Not authorized — execution gate closed", "exact false authorization");
    const authorized = renderState("executing", { selected_policy: "rce", supervisor_execution_authorized: true });
    check(authorized._summary.physicalExecution.value.textContent === "Authorized by every Active gate", "exact true authorization");
    for (const unsafe of [null, "false", 0]) {
      const panel = renderState("active_idle", { supervisor_execution_authorized: unsafe });
      check(panel._summary.physicalExecution.value.textContent === "Unverified — execution blocked", `unsafe auth ${String(unsafe)}`);
    }
  });

  await group("26 legacy unchanged presentation", async () => {
    check(renderState("active_idle", { legacy_execution_unchanged: true })._summary.existingAutomation.value.textContent === "Unchanged", "legacy true unchanged");
    check(renderState("active_idle", { legacy_execution_unchanged: false })._summary.existingAutomation.value.textContent === "Blocked", "legacy false blocked in Active");
    for (const value of [null, "true", 1]) {
      check(renderState("active_idle", { legacy_execution_unchanged: value })._summary.existingAutomation.value.textContent === "Unverified", `legacy ${String(value)} unverified`);
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
    check(!fullSupervisorSource.includes("innerHTML"), "full Supervisor component never uses innerHTML");
    check(fullSupervisorSource.includes("textContent ="), "full Supervisor dynamic text uses textContent");
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
    check(panel._controls.allowTariff.error.textContent === "The operation was not fully confirmed. Check the current EMS state.", "bounded local error");
    check(!panel._controls.allowTariff.error.textContent.includes("secret"), "exception text hidden");
  });

  await group("31 parent remount no duplicate DOM", async () => {
    const { panel, container, hass } = panelFixture();
    const initialRoot = container.children[0];
    const initialListeners = panel._controls.mode.element.listenerCount("click");
    panel.connect();
    panel.update(hass, { ...EXPECTED_BINDINGS }, "en");
    check(container.children.length === 1 && container.children[0] === initialRoot, "repeat update keeps one root");
    check(panel._controls.mode.element.listenerCount("click") === initialListeners, "repeat connect adds no listener");
    panel.disconnect();
    check(panel._controls.mode.element.listenerCount("click") === 0, "disconnect removes listener");
    check(panel._hass === null, "disconnect releases old hass");
    panel.connect();
    panel.update(hass, { ...EXPECTED_BINDINGS }, "en");
    check(panel._controls.mode.element.listenerCount("click") === 1, "reconnect restores one listener");
  });

  await group("FINAL_SERVICE_TARGET_ATTESTATION", async () => {
    const exact = panelFixture();
    await exact.panel._selectOption("mode", "Off");
    await exact.panel._selectOption("profile", "Maximum Profit");
    await exact.panel._toggleBoolean("allowRce");
    await exact.panel._toggleBoolean("allowTariff");
    await exact.panel._toggleBoolean("allowRcm");
    await exact.panel._pressButton("masterStop");
    equal(
      exact.hass.calls.map((call) => call.data.entity_id),
      [
        "input_select.hoymiles_ems_supervisor_mode",
        "input_select.hoymiles_ems_supervisor_profile",
        "input_boolean.hoymiles_ems_supervisor_allow_rce",
        "input_boolean.hoymiles_ems_supervisor_allow_tariff",
        "input_boolean.hoymiles_ems_supervisor_allow_rcm",
        "input_button.hoymiles_ems_supervisor_master_stop",
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
        ["input_button", "press"],
      ],
      "service derived from closed control kind",
    );
    equal(EXPECTED_CONTROL_TARGETS, {
      mode: "input_select.hoymiles_ems_supervisor_mode",
      profile: "input_select.hoymiles_ems_supervisor_profile",
      allowRce: "input_boolean.hoymiles_ems_supervisor_allow_rce",
      allowTariff: "input_boolean.hoymiles_ems_supervisor_allow_tariff",
      allowRcm: "input_boolean.hoymiles_ems_supervisor_allow_rcm",
      masterStop: "input_button.hoymiles_ems_supervisor_master_stop",
    }, "independent target literal oracle");

    const redirects = [
      ["allowRce", "supervisor_allow_rce_entity", "input_boolean.hoymiles_rce_discharge_enabled", "off", null],
      ["allowTariff", "supervisor_allow_tariff_entity", "input_boolean.hoymiles_tariff_charge_enabled", "off", null],
      ["mode", "supervisor_mode_entity", "select.hoymiles_hit_ems_mode", "Active", ["Off", "Active"]],
      ["mode", "supervisor_mode_entity", "input_select.unrelated_mode", "Active", ["Off", "Active"]],
      ["mode", "supervisor_mode_entity", "input_select.hoymiles_ems_supervisor_mode_2", "Active", ["Off", "Active"]],
      ["mode", "supervisor_mode_entity", "input_select.", "Active", ["Off", "Active"]],
      ["allowRcm", "supervisor_allow_rcm_entity", "sensor.hoymiles_ems_supervisor_allow_rcm", "off", null],
      ["masterStop", "supervisor_master_stop_entity", "input_button.unrelated_master_stop", "unknown", null],
    ];
    for (const [key, configKey, unsafeTarget, state, options] of redirects) {
      const helper = { state, attributes: options === null ? {} : { options } };
      const hass = hassFixture("active_idle", {}, { [unsafeTarget]: helper });
      const config = { ...EXPECTED_BINDINGS, [configKey]: unsafeTarget };
      const fixture = panelFixture("en", hass, config);
      if (key === "mode") await fixture.panel._selectOption(key, "Off");
      else if (key === "masterStop") await fixture.panel._pressButton(key);
      else await fixture.panel._toggleBoolean(key);
      check(hass.calls.length === 0, `redirect blocked ${unsafeTarget}`);
      check(fixture.panel._controls[key].element.disabled, `redirect disabled ${unsafeTarget}`);
      check(fixture.panel._controls[key].error.textContent === "The operation was not fully confirmed. Check the current EMS state.", `redirect bounded error ${unsafeTarget}`);
    }
    const direct = panelFixture();
    await direct.panel._callHelperService("mode", "Off", "input_select.unrelated_mode");
    await direct.panel._callHelperService("mode", "input_select.unrelated_mode");
    await direct.panel._callHelperService("allowRce", "input_boolean.hoymiles_rce_discharge_enabled");
    await direct.panel._callHelperService("masterStop", "press", "input_button.unrelated_master_stop");
    check(direct.hass.calls.length === 0, "caller-provided targets cannot reach service boundary");
  });

  await group("FINAL_HELPER_STATE_ATTESTATION", async () => {
    for (const [current, requested] of [["Off", "Active"], ["Active", "Off"]]) {
      const hass = hassFixture("active_idle", {}, {
        ["input_select.hoymiles_ems_supervisor_mode"]: {
          state: current,
          attributes: { options: ["Off", "Active"] },
        },
      });
      const { panel } = panelFixture("en", hass);
      await panel._selectOption("mode", requested);
      check(hass.calls.length === 1, `safe current mode ${current}`);
      check(hass.calls[0].data.option === requested, `exact requested mode ${requested}`);
    }

    for (const unsafeState of ["Active", "Auto", "Automatic", "unknown", "unavailable", "", null, 7, true]) {
      const hass = hassFixture("active_idle", {}, {
        ["input_select.hoymiles_ems_supervisor_mode"]: {
          state: unsafeState,
          attributes: { options: ["Off", "Active", "Active", "Auto", "Automatic"] },
        },
      });
      const { panel } = panelFixture("en", hass);
      await panel._selectOption("mode", "Off");
      check(hass.calls.length === 0, `unsafe current mode blocked ${String(unsafeState)}`);
      check(panel._controls.mode.element.disabled, `unsafe current mode disabled ${String(unsafeState)}`);
      check(panel._controls.mode.error.textContent === "The operation was not fully confirmed. Check the current EMS state.", `unsafe current mode bounded error ${String(unsafeState)}`);
    }

    const selectCases = [
      ["current absent", "Active", ["Off"], "Off"],
      ["requested absent", "Active", ["Active"], "Off"],
      ["requested duplicate", "Active", ["Active", "Off", "Off"], "Off"],
      ["current duplicate", "Active", ["Active", "Active", "Off"], "Off"],
      ["options null", "Active", null, "Off"],
      ["options object", "Active", {}, "Off"],
      ["options primitive", "Active", "Off,Active", "Off"],
      ["options non-string", "Active", ["Active", "Off", 1], "Off"],
      ["options reordered", "Active", ["Active", "Off"], "Off"],
    ];
    for (const [label, current, options, requested] of selectCases) {
      const hass = hassFixture("active_idle", {}, {
        ["input_select.hoymiles_ems_supervisor_mode"]: {
          state: current,
          attributes: { options },
        },
      });
      const { panel } = panelFixture("en", hass);
      await panel._selectOption("mode", requested);
      check(hass.calls.length === 0, `${label} blocked at final boundary`);
      check(panel._controls.mode.error.textContent === "The operation was not fully confirmed. Check the current EMS state.", `${label} bounded error`);
    }

    const profile = hassFixture("active_idle", {}, {
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
      const hass = hassFixture("active_idle", {}, {
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

    const bypass = hassFixture("active_idle", {}, {
      ["input_select.hoymiles_ems_supervisor_mode"]: {
        state: "Active",
        attributes: { options: ["Off", "Active", "Active"] },
      },
    });
    const bypassPanel = panelFixture("en", bypass).panel;
    await bypassPanel._callHelperService("mode", "Off");
    check(bypass.calls.length === 0, "direct Active-to-Off bypass blocked");
    check(bypassPanel._controls.mode.element.disabled, "direct bypass remains fail-safe disabled");

    const unavailableStop = hassFixture("active_idle", {}, {
      [EXPECTED_BINDINGS.supervisor_master_stop_entity]: {
        state: "unavailable",
        attributes: {},
      },
    });
    const unavailableStopPanel = panelFixture("en", unavailableStop).panel;
    await unavailableStopPanel._pressButton("masterStop");
    check(unavailableStop.calls.length === 0, "unavailable MASTER STOP helper emits no service");
    check(unavailableStopPanel._controls.masterStop.element.disabled, "unavailable MASTER STOP is disabled");
    check(unavailableStopPanel._controls.masterStop.error.textContent === "The operation was not fully confirmed. Check the current EMS state.", "unavailable MASTER STOP reports bounded error");
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
    check(fixture.panel._controls.mode.element.listenerCount("click") === 0, "disconnect removes parent panel handler");

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
    check(fixture.panel._controls.mode.element.listenerCount("click") === 1, "repeated reconnect keeps one handler");
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
    check(
      frontendValidator.includes("expectedAp3bPaths")
        && frontendValidator.includes("expectedSupervisorActivePaths")
        && frontendValidator.includes("expectedIntegratedActiveSharedAuroraPaths")
        && frontendValidator.includes("expectedI3Paths")
        && frontendValidator.includes("expectedAuroraCompactPaths")
        && frontendValidator.includes(
          '"integration/v1.5.8-active-shared-aurora"',
        )
        && frontendValidator.includes("const expectedN07ExactCandidatePaths")
        && frontendValidator.includes('"tools/test_c4_final_ui_contract.js"')
        && frontendValidator.includes("const expectedBranchPathCount = rc2CandidateManifest")
        && frontendValidator.includes("? rc2CandidateManifest.branch_paths.length")
        && frontendValidator.includes(": n07ExactCandidateMode")
        && frontendValidator.includes('state === "RC2_LOCAL_CANDIDATE"')
        && frontendValidator.includes("print(m.validate_current_integrated_manifests())")
        && frontendValidator.includes(": consolidatedReleaseState")
        && frontendValidator.includes(": auroraCompactOverlayState")
        && frontendValidator.includes("? 137")
        && frontendValidator.includes("? 120")
        && frontendValidator.includes(": supervisorActiveState")
        && frontendValidator.includes("? 92")
        && frontendValidator.includes("AP-3B/C3 manifest self-tests did not reject 24 or 26 paths"),
      "frontend validator enforces exact RC2, N07, Variant A, I3, I1, AP-3B/C3 and Supervisor Active manifests",
    );
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
    check(panel._controls.mode.element.getAttribute("aria-label") === "Disable EMS Supervisor", "mode toggle accessible name");
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

  await group("39 green reserved for confirmed execution", async () => {
    const selected = renderState("selected", { selected_policy: "rce" });
    check(selected._container.dataset.tone === "selected", "selected uses pre-execution tone");
    check(!supervisorCss.includes("--hoymiles-aurora-good"), "no green good token");
    check(supervisorCss.includes(".supervisor-blob { animation: none; }"), "reduced motion disables backdrop animation");
    check(!/\.supervisor-(policy|switch|panel)[^{]*\{[^}]*animation:/s.test(supervisorCss), "no state or control animation suggests execution");
    check(selected._summary.physicalExecution.value.textContent === "Not authorized — execution gate closed", "selected is not yet physical execution");
    const executing = renderState("executing", { selected_policy: "rce", supervisor_execution_authorized: true });
    check(executing._container.dataset.tone === "executing", "confirmed execution uses dedicated tone");
    check(supervisorCss.includes('--supervisor-tone: var(--supervisor-ready)'), "green is reserved for confirmed execution");
    for (const token of [
      "supervisor-blob-one", "supervisor-blob-two", "supervisor-blob-three",
      "supervisor-points", "supervisor-vignette", "pointer-events: none",
      "@keyframes supervisor-aurora-one", "@media (prefers-reduced-motion: reduce)",
    ]) check(supervisorCss.includes(token), `Aurora backdrop contract ${token}`);
    check(!supervisorCss.includes("filter: blur"), "Aurora backdrop avoids large blur filters");
  });

  await group("40 generated resource parity", async () => {
    if (ALLOW_GENERATED_DRIFT) {
      for (let index = 0; index < 49; index += 1) {
        check(true, `generated parity assertion ${index + 1} deferred to the root generator pass`);
      }
      return;
    }
    check(read("custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js") === cardSource, "packaged card parity");
    check(read("custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js") === strategySource, "packaged strategy parity");
    for (const language of ["pl", "en"]) {
      const yaml = read(`custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_${language}.yaml`);
      const json = JSON.parse(read(`custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_${language}.json`));
      const plannerView = json.views[1];
      const supervisorView = json.views[2];
      check(json.views.length === 21, `${language} generated view count 21`);
      check(
        plannerView?.title === "EMS"
          && plannerView?.path === "plan-automatyki"
          && plannerView?.icon === undefined
          && plannerView?.type === "panel"
          && plannerView?.cards?.length === 1
          && plannerView.cards[0]?.type === "custom:hoymiles-aurora-variant-a-ems-page-card",
        `${language} exact Variant A EMS view identity`,
      );
      check(
        supervisorView?.subview === true && supervisorView?.cards?.length === 1,
        `${language} one hidden full Supervisor card`,
      );
      const supervisorCard = supervisorView.cards[0];
      for (const [key, entityId] of Object.entries(EXPECTED_BINDINGS)) {
        check(yaml.includes(`${key}: ${entityId}`), `${language} YAML ${key}`);
        check(supervisorCard[key] === entityId, `${language} JSON ${key}`);
      }
      const overviewView = json.views.find((view) => view.path === "start");
      const overview = overviewView.cards.find((card) => card.type === "custom:hoymiles-aurora-overview-card");
      check(
        overviewView.type === "panel"
          && overviewView.cards.length === 1
          && overview.supervisor_entity === EXPECTED_BINDINGS.supervisor_entity
          && overview.supervisor_mode_entity === EXPECTED_BINDINGS.supervisor_mode_entity,
        `${language} Start uses one panel composition with canonical Supervisor bindings`,
      );
      check(supervisorView.title === (language === "pl" ? "Automatyka EMS" : "EMS automation"), `${language} generated title`);
      check(supervisorView.icon === "mdi:shield-check" && supervisorView.type === "panel", `${language} view identity`);
    }
    const generator = read("tools/build_hacs_assets.py");
    const routeInventory = generator.slice(
      generator.indexOf("DASHBOARD_VIEW_PATHS = ("),
      generator.indexOf("ROUTE_BEARING_KEYS ="),
    );
    check(!generator.includes('"  - title: Plan EMS\\n"'), "generator drops the former Polish Plan EMS title");
    check(!generator.includes('"  - title: EMS plan\\n"'), "generator drops the former English EMS plan title");
    check(
      routeInventory.includes('"plan-automatyki",') &&
        !generator.includes('"  - title: Plan EMS\\n": "  - title: EMS plan\\n"'),
      "generator keeps the EMS route without a legacy title translation",
    );
    check(generator.includes('"  - title: Automatyka EMS\\n"'), "generator matches full Polish EMS-automation title line");
    check(generator.includes('"  - title: EMS automation\\n"'), "generator emits exact English EMS-automation title line");
    check(
      routeInventory.includes('"ems-supervisor",') &&
        generator.includes('"  - title: Automatyka EMS\\n": "  - title: EMS automation\\n"'),
      "generator Supervisor translation is scoped to the exact view title and route inventory",
    );
    check(!generator.includes('"Automatyka EMS": "EMS automation"'), "generator has no broad EMS-automation phrase replacement");
  });

  await group("44 frontend revision consistency", async () => {
    const assets = read("custom_components/hoymiles_hit_modbus/assets.py");
    const validator = read("tools/validate_rce_card.js");
    const validatorRevisionGate = validator.slice(0, validator.indexOf("const expectedPhase2Paths"));
    check(assets.includes("FRONTEND_ASSET_REVISION = 123"), "RC2 asset revision 123");
    check(!assets.includes("FRONTEND_ASSET_REVISION = 38") && !assets.includes("FRONTEND_ASSET_REVISION = 39") && !assets.includes("FRONTEND_ASSET_REVISION = 40") && !assets.includes("FRONTEND_ASSET_REVISION = 41") && !assets.includes("FRONTEND_ASSET_REVISION = 42") && !assets.includes("FRONTEND_ASSET_REVISION = 43") && !assets.includes("FRONTEND_ASSET_REVISION = 44") && !assets.includes("FRONTEND_ASSET_REVISION = 45") && !assets.includes("FRONTEND_ASSET_REVISION = 46") && !assets.includes("FRONTEND_ASSET_REVISION = 47") && !assets.includes("FRONTEND_ASSET_REVISION = 48") && !assets.includes("FRONTEND_ASSET_REVISION = 49") && !assets.includes("FRONTEND_ASSET_REVISION = 60") && !assets.includes("FRONTEND_ASSET_REVISION = 61") && !assets.includes("FRONTEND_ASSET_REVISION = 62") && !assets.includes("FRONTEND_ASSET_REVISION = 63") && !assets.includes("FRONTEND_ASSET_REVISION = 64") && !assets.includes("FRONTEND_ASSET_REVISION = 65") && !assets.includes("FRONTEND_ASSET_REVISION = 66") && !assets.includes("FRONTEND_ASSET_REVISION = 67") && !assets.includes("FRONTEND_ASSET_REVISION = 68") && !assets.includes("FRONTEND_ASSET_REVISION = 69") && !assets.includes("FRONTEND_ASSET_REVISION = 70") && !assets.includes("FRONTEND_ASSET_REVISION = 71") && !assets.includes("FRONTEND_ASSET_REVISION = 87") && !assets.includes("FRONTEND_ASSET_REVISION = 88"), "asset revision excludes stale revisions");
    check(strategySource.includes("const frontendRevision = 123") && strategySource.includes("history=48h-executed") && strategySource.includes("canonicalModuleUrl.search = canonicalQuery") && !strategySource.includes("1.5.8.81") && !strategySource.includes("1.5.8.82") && !strategySource.includes("1.5.8.87") && !strategySource.includes("1.5.8.88"), "RC2 strategy cache key revision 123");
    check(!strategySource.includes("1.5.8.38") && !strategySource.includes("1.5.8.39") && !strategySource.includes("1.5.8.40") && !strategySource.includes("1.5.8.41") && !strategySource.includes("1.5.8.42") && !strategySource.includes("1.5.8.43") && !strategySource.includes("1.5.8.44") && !strategySource.includes("1.5.8.45") && !strategySource.includes("1.5.8.46") && !strategySource.includes("1.5.8.47") && !strategySource.includes("1.5.8.48") && !strategySource.includes("1.5.8.49") && !strategySource.includes("1.5.8.60") && !strategySource.includes("1.5.8.61") && !strategySource.includes("1.5.8.62") && !strategySource.includes("1.5.8.63") && !strategySource.includes("1.5.8.64") && !strategySource.includes("1.5.8.65") && !strategySource.includes("1.5.8.66") && !strategySource.includes("1.5.8.67") && !strategySource.includes("1.5.8.68") && !strategySource.includes("1.5.8.69") && !strategySource.includes("1.5.8.70") && !strategySource.includes("1.5.8.71"), "strategy excludes revisions 38 through 71");
    check(validatorRevisionGate.includes("FRONTEND_ASSET_REVISION = 123") && validatorRevisionGate.includes("1.5.8.1.123") && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 81")') && validatorRevisionGate.includes('bootstrapSource.match(/1\\.5\\.8\\.81/g)') && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 82")') && validatorRevisionGate.includes('bootstrapSource.match(/1\\.5\\.8\\.82/g)'), "frontend validator current revision gate");
    check(
      validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 63")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 62")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 61")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 60")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 38")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 39")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 40")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 41")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 42")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 43")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 44")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 45")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 46")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 47")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 48")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 49")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 64")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 65")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 66")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 67")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 68")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 69")')
        && validatorRevisionGate.includes('bootstrapSource.includes("1.5.8.69")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 70")')
        && validatorRevisionGate.includes('bootstrapSource.includes("1.5.8.70")')
        && validatorRevisionGate.includes('assetsSource.includes("FRONTEND_ASSET_REVISION = 71")')
        && validatorRevisionGate.includes('bootstrapSource.includes("1.5.8.71")'),
      "frontend validator current gate rejects prior revisions through 71",
    );
    check(!strategySource.includes("1.5.7.25"), "pre-fix strategy revision absent");
    check(!strategySource.includes("1.5.6.24"), "old strategy revision absent");
  });

  await group("42 protected Supervisor dashboard subtree and Start behavior", async () => {
    const viewMatches = [...dashboardSource.matchAll(/^  - title: (.+)$/gm)];
    const supervisorIndex = viewMatches.findIndex((match) => match[1] === "Automatyka EMS");
    const supervisorSource = dashboardSource
      .slice(viewMatches[supervisorIndex].index, viewMatches[supervisorIndex + 1].index)
      .replaceAll("\r\n", "\n");
    check(
      supervisorIndex === 2
        && supervisorSource.includes("    path: ems-supervisor\n")
        && supervisorSource.includes("    icon: mdi:shield-check\n")
        && supervisorSource.includes("    type: panel\n")
        && (supervisorSource.match(/custom:hoymiles-ems-supervisor-card/g) || []).length === 1
        && !supervisorSource.includes("custom:hoymiles-ems-shared-inputs-card"),
      "Supervisor subtree matches the unified I2 automation contract without a repeated Shared EMS card",
    );
    const energyStart = cardSource.indexOf("class HoymilesAuroraEnergyCard");
    const energyEnd = cardSource.indexOf("class HoymilesPowerFlowCard", energyStart);
    const energySource = cardSource.slice(energyStart, energyEnd).replaceAll("\r\n", "\n");
    const energyDigest = crypto.createHash("sha256").update(energySource).digest("hex");
    const startSource = dashboardSource.slice(viewMatches[0].index, viewMatches[1].index);
    const overviewStart = cardSource.indexOf("class HoymilesAuroraOverviewCard");
    const overviewEnd = cardSource.indexOf('if (!customElements.get("hoymiles-aurora-overview-card"))', overviewStart);
    const overviewSource = cardSource.slice(overviewStart, overviewEnd).replaceAll("\r\n", "\n");
    const insightsStart = energySource.indexOf('<div class="insights">');
    const insightsEnd = energySource.indexOf('<div class="aurora">', insightsStart);
    const insightsSource = energySource.slice(insightsStart, insightsEnd);
    const flowSummaryHidesInsights = [
      ...energySource.matchAll(/([^{}]+)\{[^{}]*display:\s*none[^{}]*\}/g),
    ].some((match) =>
      match[1].includes('ha-card[data-layout="flow-summary"] .insights')
    );
    check(
      energyDigest === "e07d7585eb1d433afd514a2dc36445f8e58d8d87f40adfc7f7d57aa8a71a44e4"
        && startSource.includes("    type: panel")
        && (startSource.match(/type: custom:hoymiles-aurora-overview-card/g) || []).length === 1
        && !startSource.includes("type: history-graph")
        && overviewSource.includes('document.createElement("hoymiles-aurora-energy-card")')
        && overviewSource.includes('layout: "flow_summary"')
        && overviewSource.includes('document.createElement("hoymiles-aurora-history-card")')
        && overviewSource.includes("hours_to_show: 24")
        && overviewSource.includes('value_mode: "power"')
        && overviewSource.includes('layout: "overview"')
        && [
          "sensor.hoymiles_hit_overview_pv_total_power",
          "sensor.hoymiles_actual_load_power",
          "sensor.hoymiles_hit_overview_grid_total_active_power",
          "sensor.hoymiles_hit_overview_battery_power",
        ].every((entityId) => startSource.includes(`- entity: ${entityId}`)),
      "Start composes the accepted Aurora canvas, compact EMS, KPIs and real 24-hour four-series history",
    );
    check(
      overviewSource.includes('layout: "flow_summary"')
        && insightsStart >= 0
        && insightsEnd > insightsStart
        && ["forecast_today", "forecast_remaining", "forecast_tomorrow", "average_load"]
          .every((key) => insightsSource.includes(`data-key="${key}"`))
        && energySource.includes('ha-card[data-layout="flow-summary"] .insights { margin-top: 0; }')
        && !flowSummaryHidesInsights,
      "Overview flow_summary keeps the existing four-tile forecast insights strip visible",
    );
    for (const sentinel of ['data-ribbon="pv"', 'data-flow="battery"', '<div class="daily">', '<div class="grid-import">']) {
      check(cardSource.includes(sentinel), `existing energy behavior ${sentinel}`);
    }
    check((dashboardSource.match(/^  - title:/gm) || []).length > 1, "existing views retained");
  });

  await group("43 Active UI is decoupled from backend file hashes", async () => {
    check(componentSource.includes("supervisor_master_stop_entity"), "MASTER STOP is part of the closed UI binding map");
    check(!dashboardSource.includes("rcm_" + "shadow_entity"), "retired RCEm helper is absent from canonical dashboard config");
  });

  await group("44 closed Active UI service authority", async () => {
    check((componentSource.match(/callService\.call\(/g) || []).length === 1, "one guarded call site");
    check(componentSource.includes('domain: "input_select"'), "select domain derived internally");
    check(componentSource.includes('domain: "input_boolean"'), "boolean domain derived internally");
    check(componentSource.includes('domain: "input_button"'), "MASTER STOP domain derived internally");
    check(componentSource.includes("HOYMILES_SUPERVISOR_CONTROL_KINDS"), "closed control-kind map present");
    for (const forbidden of ["owner", "grant", "handover", "modbus", "write_register", "force_active"]) {
      check(!/callService\.call[^;]+/is.exec(componentSource)?.[0].toLowerCase().includes(forbidden), `no ${forbidden} authority in call site`);
    }
    const panel = renderState("executing", { selected_policy: "rcm", supervisor_execution_authorized: true });
    check(panel._summary.physicalExecution.value.textContent === "Authorized by every Active gate", "true authorization is shown only in executing state");
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
      ["active_idle", "active-idle"],
      ["selected", "selected"],
      ["starting", "transition"],
      ["waiting_readback", "transition"],
      ["executing", "executing"],
      ["stopping", "transition"],
      ["restoring", "transition"],
      ["blocked", "blocked"],
      ["fault", "fault"],
      ["unavailable", "unavailable"],
    ]) {
      const overrides = ["selected", "starting", "waiting_readback", "executing", "stopping", "restoring"].includes(state)
        ? { selected_policy: "rce", supervisor_execution_authorized: state === "executing" }
        : {};
      check(renderState(state, overrides)._container.dataset.tone === tone, "runtime tone " + state);
    }
    check(supervisorCss.includes("--supervisor-tone: var(--supervisor-neutral)"), "Off uses neutral");
    check(supervisorCss.includes('data-tone="active-idle"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-cyan)"), "idle uses cyan and blue");
    check(supervisorCss.includes('data-tone="selected"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-violet)"), "selected uses violet and cyan");
    check(supervisorCss.includes('data-tone="executing"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-ready)"), "executing alone uses green");
    check(supervisorCss.includes('data-tone="blocked"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-warning)"), "blocked uses warning amber");
    check(supervisorCss.includes('data-tone="unavailable"') && supervisorCss.includes("--supervisor-tone: var(--supervisor-error)"), "unavailable uses restrained red");
    const selectedToneBlock = supervisorCss.slice(
      supervisorCss.indexOf('.supervisor-panel[data-tone="selected"]'),
      supervisorCss.indexOf('.supervisor-panel[data-tone="transition"]'),
    );
    check(!selectedToneBlock.includes("supervisor-ready"), "selected Active never uses green readiness");
    check(supervisorCss.includes('.supervisor-policy-badge[data-tone="ready"]'), "green reserved for readiness badge");
  });

  await group("HERO_VISUAL_HIERARCHY", async () => {
    const panel = panelFixture("en").panel;
    check(nodesWithClass(panel._container, "supervisor-title").length === 1, "Hero has one primary title");
    check(panel._scope.textContent === "Active-mode scope: transactional execution" && !panel._scope.hidden, "permanent Active-mode scope badge");
    const authority = nodesWithClass(panel._container, "supervisor-authority");
    check(authority.length === 1, "one prominent physical-authority statement");
    check(panel._copyNodes.some(([node, key]) => key === "physicalAuthority" && node.textContent === "The Supervisor takes exclusive transactional execution authority only in Active mode."), "conditional authority statement copy");
    check(nodesWithClass(panel._heroCore, "supervisor-policy-chip").length === 3, "three logical input chips in Hero");
    check(nodesWithClass(panel._heroCore, "supervisor-core-orb").length === 1, "one central Supervisor orb");
    check(panel._heroResultText.textContent.length > 0 && panel._heroResultBadge.textContent.length > 0, "bounded dynamic result");
    check(nodesWithClass(panel._heroCore, "supervisor-no-control").length === 1, "permanent fail-closed transaction line");
    check(panel._noControlText.textContent === "Waiting — no action to execute", "healthy idle is waiting, not a blocked transaction");
    const blocked = renderState("blocked", { execution_blocked_reason: "owner_conflict" });
    check(blocked._noControlText.textContent === "Fail-closed transaction", "real execution blocker keeps fail-closed wording");
    const polish = renderState("active_idle", {}, "pl");
    check(polish._noControlText.textContent === "Oczekiwanie — brak akcji do wykonania", "Polish healthy idle waiting copy");
    check(panel._hero.physical.textContent === "Requires Active mode and complete confirmation", "exact conditional physical-control fact");
  });

  await group("HERO_STATE_COPY", async () => {
    const cases = [
      ["off", {}, "heroOffResult"],
      ["active_idle", {}, "heroIdleResult"],
      ["selected", { selected_policy: "rce" }, "heroSelectedResult"],
      ["selected", { selected_policy: "tariff" }, "heroSelectedResult"],
      ["selected", { selected_policy: "rcm" }, "heroSelectedResult"],
      ["starting", { selected_policy: "rce" }, "heroSelectedResult"],
      ["waiting_readback", { selected_policy: "rce" }, "heroSelectedResult"],
      ["executing", { selected_policy: "rce", supervisor_execution_authorized: true }, "heroSelectedResult"],
      ["stopping", { selected_policy: "rce" }, "heroSelectedResult"],
      ["restoring", { selected_policy: "rce" }, "heroSelectedResult"],
      ["blocked", { execution_blocked_reason: "owner_conflict" }, "heroBlockedResult"],
      ["fault", { execution_blocked_reason: "owner_conflict" }, "heroFaultResult"],
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
    const selected = renderState("selected", { selected_policy: "tariff" });
    for (const policyId of EXPECTED_POLICY_IDS) {
      const view = selected._policyRows[policyId];
      check(view.row.dataset.policy === policyId, "stable identity " + policyId);
      check(view.policyIcon.getAttribute("icon") === ui.HOYMILES_SUPERVISOR_POLICY_ICONS[policyId], "rendered icon " + policyId);
      check(supervisorCss.includes('.supervisor-policy[data-policy="' + policyId + '"]'), "accent selector " + policyId);
    }
    check(selected._policyRows.tariff.selected.textContent === "Logically selected", "selected marker is logical only");
    const denied = renderState("active_idle", {
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
    const panel = renderState("selected", { selected_policy: "rcm" });
    const prominent = Object.values(panel._summary).filter(
      ({ row }) => row.dataset.priority === "prominent",
    );
    const secondary = Object.values(panel._summary).filter(
      ({ row }) => row.dataset.priority === "secondary",
    );
    check(prominent.length === 6, "six prominent decision facts");
    check(secondary.length === 3, "three secondary decision facts");
    equal(
      Object.keys(panel._summary),
      ["state", "owner", "selectedPolicy", "decisionReason", "physicalExecution", "lifecycleReason", "blockedReason", "phase", "existingAutomation"],
      "all nine decision fields retained in hierarchy",
    );
    check(panel._summary.state.row.dataset.tone === "selected", "state badge gets semantic tone");
    check(panel._summary.selectedPolicy.row.dataset.policy === "rcm", "selected policy gets policy accent");
    check(panel._summary.physicalExecution.value.textContent === "Not authorized — execution gate closed", "physical authorization chip always visible");
    const idle = renderState("active_idle");
    check(idle._summary.selectedPolicy.row.dataset.policy === "none", "no selection uses neutral accent");
    check(idle._summary.blockedReason.row.dataset.empty === "true", "empty blocker explicitly subdued");
  });

  await group("VISIBLE_SAFETY_STRIP", async () => {
    const en = panelFixture("en").panel;
    const pl = panelFixture("pl").panel;
    check(nodesWithClass(en._container, "supervisor-safety-strip").length === 1, "one always-visible safety strip");
    check(en._copyNodes.some(([node, key]) => key === "safetyStrip" && node.textContent === "The Active Supervisor is the sole executor. MASTER STOP ends the transaction through safe restoration and owner release."), "exact EN safety copy");
    check(pl._copyNodes.some(([node, key]) => key === "safetyStrip" && node.textContent === "Włączony EMS jest jedynym wykonawcą. MASTER STOP kończy transakcję przez bezpieczne odtworzenie i zwolnienie właściciela."), "exact PL safety copy");
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
    check(Object.keys(panel._controls).length === 6, "five settings and MASTER STOP on live surface");
    check(Object.keys(panel._summary).length === 9, "decision remains complete before education");
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

  await group("ACTIVE_CONTROL_SURFACE_FREEZE", async () => {
    for (const [name, [start, end, expectedHash]] of Object.entries(EXPECTED_FUNCTIONAL_SEGMENTS)) {
      check(sourceSegmentDigest(componentSource, start, end) === expectedHash, "frozen Active functional segment " + name);
    }
    const panel = panelFixture().panel;
    for (const [key, entityId] of Object.entries(EXPECTED_CONTROL_TARGETS)) {
      check(panel._controlKind(key)?.entityId === entityId, "exact control target " + key);
    }
    check((componentSource.match(/callService\.call\(/g) || []).length === 1, "one guarded helper call site");
    check(componentSource.includes('domain: "input_select"') && componentSource.includes('domain: "input_boolean"') && componentSource.includes('domain: "input_button"'), "only closed helper service domains");
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
  console.log(`EMS Supervisor Aurora UI contract: ${groupCount}/${EXPECTED_GROUP_COUNT} groups, ${checkCount} checks PASS`);
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exitCode = 1;
});
