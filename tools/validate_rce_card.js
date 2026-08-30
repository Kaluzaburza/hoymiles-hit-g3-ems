const fs = require("node:fs");
const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const vm = require("node:vm");

const source = fs.readFileSync(
  "home_assistant/www/hoymiles-rce-chart-card.js",
  "utf8",
);
const bootstrapSource = fs.readFileSync(
  "home_assistant/www/hoymiles-dashboard-strategy.js",
  "utf8",
);
const assetsSource = fs.readFileSync(
  "custom_components/hoymiles_hit_modbus/assets.py",
  "utf8",
);
if (
  !assetsSource.includes("FRONTEND_ASSET_REVISION = 28") ||
  !bootstrapSource.includes("1.5.7.28") ||
  bootstrapSource.includes("1.5.7.25")
) {
  throw new Error("Frontend revision/cache contract is not exactly 1.5.7.28");
}
if (!source.includes("import.meta.url")) {
  throw new Error("Dashboard strategy no longer resolves assets from its module URL");
}
const expectedPhase2Paths = [
  "custom_components/hoymiles_hit_modbus/assets.py",
  "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_en.yaml",
  "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_pl.yaml",
  "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_en.json",
  "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_pl.json",
  "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js",
  "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js",
  "dashboard_hoymiles.yaml",
  "home_assistant/www/hoymiles-dashboard-strategy.js",
  "home_assistant/www/hoymiles-rce-chart-card.js",
  "tools/build_hacs_assets.py",
  "tools/test_supervisor_aurora_ui_contract.js",
  "tools/validate_rce_card.js",
  "tools/validate_release.py",
].sort();
const expectedCorrectionPaths = [
  "custom_components/hoymiles_hit_modbus/assets.py",
  "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js",
  "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js",
  "home_assistant/www/hoymiles-dashboard-strategy.js",
  "home_assistant/www/hoymiles-rce-chart-card.js",
  "tools/test_supervisor_aurora_ui_contract.js",
  "tools/validate_rce_card.js",
  "tools/validate_release.py",
].sort();
const expectedIntegrationPaths = [
  ".github/workflows/validate.yml",
  "CHANGELOG.md",
  "RELEASING.md",
  "custom_components/hoymiles_hit_modbus/__init__.py",
  "custom_components/hoymiles_hit_modbus/assets.py",
  "custom_components/hoymiles_hit_modbus/automation_plan_timeline.py",
  "custom_components/hoymiles_hit_modbus/ems_supervisor.py",
  "custom_components/hoymiles_hit_modbus/rce_optimizer.py",
  "custom_components/hoymiles_hit_modbus/rce_sensor.py",
  "custom_components/hoymiles_hit_modbus/rcm_sensor.py",
  "custom_components/hoymiles_hit_modbus/rcm_timeline_model.py",
  "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_en.yaml",
  "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_pl.yaml",
  "custom_components/hoymiles_hit_modbus/resources/home_assistant/en/hoymiles_ems_scheduler.yaml",
  "custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/hoymiles_ems_scheduler.yaml",
  "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_en.json",
  "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_pl.json",
  "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js",
  "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js",
  "custom_components/hoymiles_hit_modbus/sensor.py",
  "custom_components/hoymiles_hit_modbus/supervisor_runtime.py",
  "custom_components/hoymiles_hit_modbus/supervisor_sensor.py",
  "custom_components/hoymiles_hit_modbus/tariff_optimizer.py",
  "custom_components/hoymiles_hit_modbus/tariff_sensor.py",
  "custom_components/hoymiles_hit_modbus/timeline_sensor.py",
  "custom_components/hoymiles_hit_modbus/translations/en.json",
  "custom_components/hoymiles_hit_modbus/translations/pl.json",
  "dashboard_hoymiles.yaml",
  "docs/releases/v1.5.8.md",
  "home_assistant/hoymiles_ems_scheduler.yaml",
  "home_assistant/www/hoymiles-dashboard-strategy.js",
  "home_assistant/www/hoymiles-rce-chart-card.js",
  "tests/test_timeline_platform_registration.py",
  "tools/build_hacs_assets.py",
  "tools/test_automation_matrix.py",
  "tools/test_automation_plan_timeline.py",
  "tools/test_ems_supervisor.py",
  "tools/test_optimizer_executor_contract.py",
  "tools/test_optimizer_startup_contract.py",
  "tools/test_rce_optimizer.py",
  "tools/test_rcm_optimizer.py",
  "tools/test_rcm_timeline_model.py",
  "tools/test_supervisor_aurora_ui_contract.js",
  "tools/test_supervisor_helpers_contract.py",
  "tools/test_supervisor_runtime_contract.py",
  "tools/test_supervisor_sensor_contract.py",
  "tools/test_tariff_optimizer.py",
  "tools/validate_rce_card.js",
  "tools/validate_release.py",
].sort();
const expectedIntegrationAddedPaths = [
  "custom_components/hoymiles_hit_modbus/automation_plan_timeline.py",
  "custom_components/hoymiles_hit_modbus/ems_supervisor.py",
  "custom_components/hoymiles_hit_modbus/rcm_timeline_model.py",
  "custom_components/hoymiles_hit_modbus/supervisor_runtime.py",
  "custom_components/hoymiles_hit_modbus/supervisor_sensor.py",
  "custom_components/hoymiles_hit_modbus/timeline_sensor.py",
  "tests/test_timeline_platform_registration.py",
  "tools/test_automation_plan_timeline.py",
  "tools/test_ems_supervisor.py",
  "tools/test_rcm_timeline_model.py",
  "tools/test_supervisor_aurora_ui_contract.js",
  "tools/test_supervisor_helpers_contract.py",
  "tools/test_supervisor_runtime_contract.py",
  "tools/test_supervisor_sensor_contract.py",
].sort();
const integrationBranch = "integration/v1.5.8-bal-r2-aurora-ap2";
const integrationParent = "fa6c32dc1f0178f6ad8cdf5a0e5d1a00e3598ede";
const integrationCommitSubject =
  "feat: integrate Aurora planner with v1.5.8 balancing";
const integrationCommitMessage = [
  integrationCommitSubject,
  "",
  `BAL-R2-F1-Source: ${integrationParent}`,
  "Aurora-AP-2-Source: 42c59f358f46e2c4dd83328aca5e62ac41c749e3",
  "Supervisor-Source: 5fafc961e70b18b8677e58c8bfcc25613d1fd5c5",
  "Integration-Review-SHA256: 08609cd4cfad9bc5aa96964f42d5cfbd35a1b655b51b6eab59a9532751009c0c",
].join("\n");
const gitPaths = (...args) =>
  childProcess.execFileSync("git", args, { encoding: "utf8" })
    .split(/\r?\n/)
    .map((value) => value.trim().replaceAll("\\", "/"))
    .filter(Boolean);
const gitText = (...args) =>
  childProcess.execFileSync("git", args, { encoding: "utf8" }).trim();
const unstagedPaths = gitPaths("diff", "--name-only", "HEAD");
const stagedPaths = gitPaths("diff", "--cached", "--name-only");
const untrackedPaths = gitPaths("ls-files", "--others", "--exclude-standard");
const actualPhase2Paths = [...new Set([...unstagedPaths, ...stagedPaths, ...untrackedPaths])].sort();
const exactPathSet = (actual, expected) =>
  JSON.stringify([...actual].sort()) === JSON.stringify([...expected].sort());
const currentBranch = gitPaths("branch", "--show-current")[0] || "";
const integrationState = currentBranch === integrationBranch;
const currentHead = gitText("rev-parse", "HEAD");
const expectedIntegrationTrackedPaths = expectedIntegrationPaths.filter(
  (path) => !expectedIntegrationAddedPaths.includes(path),
);
const expectedIntegrationCommitStatus = expectedIntegrationPaths.map(
  (path) => `${expectedIntegrationAddedPaths.includes(path) ? "A" : "M"}\t${path}`,
);
const expectedIntegrationModes = expectedIntegrationPaths.map(
  (path) => `100644\t${path}`,
);
if (stagedPaths.length !== 0) {
  throw new Error("Phase 2 validator requires zero staged paths");
}
if (integrationState) {
  const overlayState =
    currentHead === integrationParent
    && exactPathSet(actualPhase2Paths, expectedIntegrationPaths)
    && exactPathSet(unstagedPaths, expectedIntegrationTrackedPaths)
    && exactPathSet(untrackedPaths, expectedIntegrationAddedPaths)
    && gitPaths("diff", "--name-only", "--diff-filter=U").length === 0;
  const parents = gitText("show", "-s", "--format=%P", "HEAD")
    .split(/\s+/)
    .filter(Boolean);
  const committedStatus = gitPaths(
    "diff",
    "--name-status",
    "--find-renames",
    integrationParent,
    "HEAD",
  );
  const committedModes = gitPaths(
    "ls-tree",
    "-r",
    "HEAD",
    "--",
    ...expectedIntegrationPaths,
  ).map((line) => {
    const [metadata, path] = line.split("\t", 2);
    return `${metadata.split(/\s+/, 1)[0]}\t${path}`;
  });
  const committedCleanState =
    currentHead !== integrationParent
    && parents.length === 1
    && parents[0] === integrationParent
    && actualPhase2Paths.length === 0
    && gitText("status", "--porcelain=v1", "--untracked-files=all") === ""
    && exactPathSet(committedStatus, expectedIntegrationCommitStatus)
    && exactPathSet(committedModes, expectedIntegrationModes)
    && gitText("show", "-s", "--format=%s", "HEAD") === integrationCommitSubject
    && gitText("show", "-s", "--format=%B", "HEAD") === integrationCommitMessage;
  if (!overlayState && !committedCleanState) {
    throw new Error(
      "Integrated frontend validation requires the exact reviewed overlay "
      + "or its exact clean single-parent commit",
    );
  }
} else if (!exactPathSet(actualPhase2Paths, expectedPhase2Paths)) {
  throw new Error(`Current frontend manifest differs: ${JSON.stringify(actualPhase2Paths)}`);
}
if (
  expectedCorrectionPaths.length !== 8
  || !expectedCorrectionPaths.every((item) => expectedPhase2Paths.includes(item))
  || exactPathSet(expectedCorrectionPaths.slice(1), expectedCorrectionPaths)
  || exactPathSet(
    [...expectedCorrectionPaths, "__unexpected_correction_path__"],
    expectedCorrectionPaths,
  )
) {
  throw new Error("Revision 28 correction manifest is not exactly 8 paths");
}
const branchPaths = integrationState
  ? gitPaths(
      "diff",
      "--name-only",
      "v1.5.7...5fafc961e70b18b8677e58c8bfcc25613d1fd5c5",
    ).sort()
  : [...new Set([
      ...gitPaths("diff", "--name-only", "v1.5.7...HEAD"),
      ...actualPhase2Paths,
    ])].sort();
if (branchPaths.length !== 32) {
  throw new Error(`Supervisor branch manifest count is ${branchPaths.length}, expected 32`);
}
if (
  exactPathSet(expectedPhase2Paths.slice(1), expectedPhase2Paths)
  || exactPathSet([...expectedPhase2Paths, "__unexpected_phase2_path__"], expectedPhase2Paths)
  || exactPathSet(branchPaths.slice(1), branchPaths)
  || exactPathSet([...branchPaths, "__unexpected_branch_path__"], branchPaths)
) {
  throw new Error("Manifest count self-tests did not reject 13/15 or 31/33 paths");
}
if (
  !bootstrapSource.includes("document.currentScript") ||
  !bootstrapSource.includes("document.scripts") ||
  !bootstrapSource.includes("import(canonicalModuleUrl.href)")
) {
  throw new Error("Early dashboard strategy no longer loads the canonical module");
}
// The card is loaded as an ES module by Home Assistant.  vm.runInNewContext
// executes classic scripts, so inject the same deterministic module URL while
// retaining an explicit assertion above that production code uses import.meta.
const canonicalModuleUrl =
  "https://homeassistant.example/local/hoymiles-rce-chart-card.js?v=1.5.7.28";
const executableSource = source.replaceAll(
  "import.meta.url",
  JSON.stringify(canonicalModuleUrl),
) + `
globalThis.__supervisorValidatorExports = {
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
const registry = new Map();

class FakeNode {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.innerHTML = "";
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.textContent = "";
    this.className = "";
    this.disabled = false;
    this.selected = false;
    this.value = "";
  }

  append(...children) {
    this.children.push(...children);
  }

  prepend(...children) {
    this.children.unshift(...children);
  }

  replaceChildren(...children) {
    this.children = children;
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

  querySelector() {
    return null;
  }

  querySelectorAll() {
    return [];
  }
}

class TestElement {
  attachShadow() {
    this.shadowRoot = new FakeNode("shadow-root");
    return this.shadowRoot;
  }

  dispatchEvent(event) {
    this.lastEvent = event;
    return true;
  }
}

const context = {
  console,
  CustomEvent: class {
    constructor(type, options) {
      this.type = type;
      Object.assign(this, options);
    }
  },
  Date,
  document: {
    documentElement: { lang: "pl" },
    createElement(tagName) {
      return new FakeNode(tagName);
    },
    createTextNode(value) {
      return { nodeType: 3, textContent: String(value) };
    },
  },
  HTMLElement: TestElement,
  Intl,
  URL,
  fetch: async (url, options) => {
    const match = String(url).match(
      /^https:\/\/homeassistant\.example\/local\/dashboard_hoymiles_(pl|en)\.json$/,
    );
    if (!match) {
      throw new Error(`Unexpected dashboard request: ${url}`);
    }
    if (options?.cache !== "no-store") {
      throw new Error("Dashboard strategy must bypass the browser cache");
    }
    const payload = JSON.parse(
      fs.readFileSync(
        `custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_${match[1]}.json`,
        "utf8",
      ),
    );
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      async json() {
        return payload;
      },
    };
  },
  window: {
    async loadCardHelpers() {
      return {
        createCardElement(config) {
          const card = new FakeNode("hui-card");
          card.config = config;
          card.getCardSize = () => 4;
          card.getGridOptions = () => ({ columns: 8, rows: 4 });
          card.updateComplete = Promise.resolve();
          return card;
        },
      };
    },
  },
  customElements: {
    define(name, constructor) {
      registry.set(name, constructor);
    },
    get(name) {
      return registry.get(name);
    },
    async whenDefined() {},
  },
};

vm.runInNewContext(executableSource, context, {
  filename: "hoymiles-rce-chart-card.js",
});

const customCardCount = context.window.customCards?.length ?? 0;
const canonicalStrategy = registry.get(
  "ll-strategy-dashboard-hoymiles-hit-xxl-g3",
);
vm.runInNewContext(
  executableSource,
  {
    ...context,
    window: context.window,
    customElements: context.customElements,
  },
  { filename: "hoymiles-rce-chart-card-second-load.js" },
);
if ((context.window.customCards?.length ?? 0) !== customCardCount) {
  throw new Error("Loading the frontend module twice duplicated card metadata");
}
if (
  !canonicalStrategy ||
  registry.get("ll-strategy-dashboard-hoymiles-hit-xxl-g3") !== canonicalStrategy
) {
  throw new Error("Duplicate module loading replaced the canonical dashboard strategy");
}

// A stale storage resource from an older release can execute the classic
// bootstrap before the canonical module on the first page load after an
// update. The immutable custom-element registration must still be upgraded to
// the full decorated generate() implementation when the canonical module runs.
const bootstrapFirstRegistry = new Map();
const bootstrapFirstWindow = {
  loadCardHelpers: context.window.loadCardHelpers,
};
const bootstrapFirstContext = {
  ...context,
  document: {
    ...context.document,
    currentScript: {
      src: (
        "https://homeassistant.example/local/"
        + "hoymiles-dashboard-strategy.js?v=1.5.7.28"
      ),
    },
  },
  window: bootstrapFirstWindow,
  customElements: {
    define(name, constructor) {
      bootstrapFirstRegistry.set(name, constructor);
    },
    get(name) {
      return bootstrapFirstRegistry.get(name);
    },
    async whenDefined() {},
  },
};
vm.runInNewContext(bootstrapSource, bootstrapFirstContext, {
  filename: "hoymiles-dashboard-strategy-bootstrap-first.js",
});
const bootstrapFirstStrategy = bootstrapFirstRegistry.get(
  "ll-strategy-dashboard-hoymiles-hit-xxl-g3",
);
if (!bootstrapFirstStrategy) {
  throw new Error("Bootstrap-first fixture did not register its legacy strategy");
}
vm.runInNewContext(executableSource, bootstrapFirstContext, {
  filename: "hoymiles-rce-chart-card-after-bootstrap.js",
});
if (
  bootstrapFirstRegistry.get("ll-strategy-dashboard-hoymiles-hit-xxl-g3")
  !== bootstrapFirstStrategy
) {
  throw new Error("Canonical module attempted to redefine a bootstrap-first strategy");
}

const scriptFallbackWindow = {
  location: { origin: "https://homeassistant.example" },
};
const scriptFallbackContext = {
  ...bootstrapFirstContext,
  document: {
    ...context.document,
    currentScript: null,
    scripts: [
      { src: "https://homeassistant.example/local/user-card.js" },
      {
        src: (
          "https://homeassistant.example/local/"
          + "hoymiles-dashboard-strategy.js?v=1.5.7.28"
        ),
      },
    ],
  },
  window: scriptFallbackWindow,
  customElements: {
    define() {},
    get() {},
  },
};
const inspectableBootstrapSource = bootstrapSource.replace(
  "let canonicalModulePromise;",
  "window.__canonicalModuleUrl = canonicalModuleUrl.href; let canonicalModulePromise;",
);
vm.runInNewContext(inspectableBootstrapSource, scriptFallbackContext, {
  filename: "hoymiles-dashboard-strategy-script-fallback.js",
});
if (
  scriptFallbackWindow.__canonicalModuleUrl !==
  "https://homeassistant.example/local/hoymiles-rce-chart-card.js?v=1.5.7.28"
) {
  throw new Error("Bootstrap lost its cache-busting query when currentScript was absent");
}
const noScriptWindow = {
  location: { origin: "https://homeassistant.example" },
};
vm.runInNewContext(inspectableBootstrapSource, {
  ...scriptFallbackContext,
  document: {
    ...context.document,
    currentScript: null,
    scripts: [],
  },
  window: noScriptWindow,
}, {
  filename: "hoymiles-dashboard-strategy-no-script-fallback.js",
});
if (
  noScriptWindow.__canonicalModuleUrl !==
  "https://homeassistant.example/local/hoymiles-rce-chart-card.js?v=1.5.7.28"
) {
  throw new Error("Bootstrap fallback imported an unversioned canonical module");
}

const Card = registry.get("hoymiles-rce-chart-card");
if (!Card) {
  throw new Error("The RCE custom element was not registered");
}

const ZebraEntitiesCard = registry.get("hoymiles-zebra-entities-card");
if (!ZebraEntitiesCard) {
  throw new Error("The zebra entities custom element was not registered");
}
if (
  !context.window.customCards?.some(
    (card) => card.type === "hoymiles-zebra-entities-card",
  )
) {
  throw new Error("The zebra entities card is absent from custom-card metadata");
}
console.log("Zebra entities card: registered without duplicate metadata");

const DiagnosticsDownloadCard = registry.get(
  "hoymiles-diagnostics-download-card",
);
if (!DiagnosticsDownloadCard) {
  throw new Error("The diagnostics download custom element was not registered");
}
for (const expected of [
  "/api/hoymiles_hit_modbus/support-bundle",
  "this._hass.fetchWithAuth(",
  "this._hass.user?.is_admin",
  "response.blob()",
  "URL.createObjectURL(blob)",
  "Zbierz dane i pobierz ZIP",
  "info@kaluzaaa.com",
]) {
  if (!source.includes(expected)) {
    throw new Error(`Diagnostics download card is missing: ${expected}`);
  }
}
console.log("Diagnostics download card: registered with browser ZIP handling");

const ResponsiveGlanceCard = registry.get("hoymiles-responsive-glance-card");
if (!ResponsiveGlanceCard) {
  throw new Error("The responsive glance custom element was not registered");
}
const responsiveGlance = new ResponsiveGlanceCard();
if (!responsiveGlance.shadowRoot) {
  throw new Error("Responsive glance must own its layout shadow root");
}
for (const expected of [
  "grid-template-columns: repeat(auto-fit",
  "overflow-wrap: anywhere",
  'new CustomEvent("hass-more-info"',
]) {
  if (!source.includes(expected)) {
    throw new Error(`Responsive glance is missing: ${expected}`);
  }
}
if (source.includes('document.createElement("hui-glance-card")')) {
  throw new Error("Responsive glance delegated layout back to fixed-column hui-glance-card");
}
console.log("Responsive glance: wrapping layout registered successfully");

for (const token of [
  "--hoymiles-aurora-surface",
  "--hoymiles-aurora-border",
  "--hoymiles-aurora-text",
  "--hoymiles-aurora-muted",
  "--hoymiles-aurora-pv",
  "--hoymiles-aurora-load",
  "--hoymiles-aurora-grid",
  "--hoymiles-aurora-battery",
  "--hoymiles-aurora-good",
  "--hoymiles-aurora-warn",
  "--hoymiles-aurora-error",
]) {
  if (!source.includes(token)) {
    throw new Error(`Shared Aurora theme is missing token: ${token}`);
  }
}
for (const forbidden of ["custom:card-mod", "custom:mushroom-"]) {
  if (source.includes(forbidden)) {
    throw new Error(`Aurora frontend introduced a forbidden dependency: ${forbidden}`);
  }
}

const auroraTypes = [
  "hoymiles-aurora-frame-card",
  "hoymiles-aurora-status-card",
  "hoymiles-aurora-history-card",
  "hoymiles-aurora-finance-card",
];
for (const type of auroraTypes) {
  if (!registry.get(type)) {
    throw new Error(`Aurora custom element was not registered: ${type}`);
  }
  const metadataCount = context.window.customCards?.filter(
    (item) => item.type === type,
  ).length ?? 0;
  if (metadataCount !== 1) {
    throw new Error(`Aurora metadata count for ${type} is ${metadataCount}, expected 1`);
  }
}

const AuroraFrameCard = registry.get("hoymiles-aurora-frame-card");
const frameCard = new AuroraFrameCard();
let missingNestedCardRejected = false;
try {
  frameCard.setConfig({ accent: "pv" });
} catch (_error) {
  missingNestedCardRejected = true;
}
if (!missingNestedCardRejected) {
  throw new Error("Aurora frame accepted a configuration without nested card");
}
frameCard.setConfig({
  accent: "not-a-real-accent",
  view_layout: { position: "sidebar" },
  card: { type: "history-graph", hours_to_show: 24 },
});
if (frameCard._config.accent !== "neutral") {
  throw new Error("Aurora frame did not safely normalize an unknown accent");
}
if (frameCard._config.card.type !== "history-graph") {
  throw new Error("Aurora frame changed its nested native card configuration");
}
const initialFrameHass = { states: { "sensor.ready": { state: "on" } } };
frameCard.hass = initialFrameHass;
frameCard.isConnected = true;
const auroraFrameMountPromise = frameCard._mount().then(() => {
  if (
    frameCard._card?.config?.type !== "history-graph" ||
    frameCard._card?.config?.hours_to_show !== 24
  ) {
    throw new Error("Aurora frame did not pass the nested card config unchanged");
  }
  if (frameCard._card?.hass !== initialFrameHass) {
    throw new Error("Aurora frame did not forward hass supplied before mount");
  }
  if (
    frameCard.getCardSize() !== 4 ||
    frameCard.getGridOptions()?.columns !== 8
  ) {
    throw new Error("Aurora frame did not delegate child card dimensions");
  }
  const updatedHass = { states: { "sensor.ready": { state: "off" } } };
  frameCard.hass = updatedHass;
  if (frameCard._card?.hass !== updatedHass) {
    throw new Error("Aurora frame did not forward hass after mount");
  }
});

const AuroraStatusCard = registry.get("hoymiles-aurora-status-card");
const statusCard = new AuroraStatusCard();
statusCard._config = { language: "pl", details_path: "stany-alarmy" };
for (const [stateValue, alarm, expectedTone] of [
  ["Praca z siecią", false, "good"],
  ["Czuwanie", false, "good"],
  ["Off-grid", false, "warn"],
  ["Brak sieci", false, "warn"],
  ["Offline", false, "error"],
  ["Niedostępne", false, "offline"],
  ["No fault", true, "good"],
  ["no_errors", true, "good"],
  ["Brak błędów", true, "good"],
  ["3", true, "error"],
  ["unavailable", true, "offline"],
]) {
  const tone = statusCard._toneForState({ state: stateValue }, alarm);
  if (tone !== expectedTone) {
    throw new Error(`Aurora status tone for ${stateValue} is ${tone}, expected ${expectedTone}`);
  }
}
if (statusCard._detailsPath() !== "/hoymiles-falownik/stany-alarmy") {
  throw new Error("Aurora status relative details path was resolved incorrectly");
}
statusCard.isConnected = true;
statusCard.setConfig({
  setup_entity: "sensor.setup",
  alarm_entities: ["sensor.optional_alarm"],
});
const goodStatusStates = Object.fromEntries(
  [
    statusCard._config.system_entity,
    statusCard._config.inverter_entity,
    statusCard._config.meter_entity,
    statusCard._config.battery_entity,
    statusCard._config.parallel_entity,
    statusCard._config.setup_entity,
  ].map((entity) => [entity, { state: "OK", last_changed: "2026-08-11T10:00:00Z" }]),
);
goodStatusStates["sensor.optional_alarm"] = {
  state: "unavailable",
  last_changed: "2026-08-11T10:00:00Z",
};
statusCard.hass = { language: "pl", states: goodStatusStates };
if (
  !statusCard.shadowRoot.innerHTML.includes("System działa prawidłowo") ||
  statusCard._summaryTone !== "good"
) {
  throw new Error("Aurora status did not render the compact Polish OK state");
}
statusCard.hass = {
  language: "en",
  states: {
    ...goodStatusStates,
    [statusCard._config.inverter_entity]: {
      state: "Offline",
      last_changed: "2026-08-11T10:00:00Z",
    },
  },
};
if (
  !statusCard.shadowRoot.innerHTML.includes("System fault detected") ||
  statusCard._summaryTone !== "error"
) {
  throw new Error("Aurora status did not expand for an English inverter fault");
}
context.hoymilesDispatchMoreInfo(statusCard, "sensor.target");
if (
  statusCard.lastEvent?.type !== "hass-more-info" ||
  statusCard.lastEvent?.detail?.entityId !== "sensor.target"
) {
  throw new Error("Aurora status emitted an invalid more-info event");
}
for (const expected of [
  'new CustomEvent("hass-more-info"',
  "setup_entity",
  "last_changed",
  "alarm_entities",
]) {
  if (!source.includes(expected)) {
    throw new Error(`Aurora status card is missing: ${expected}`);
  }
}

const AuroraHistoryCard = registry.get("hoymiles-aurora-history-card");
const englishHistoryCard = new AuroraHistoryCard();
englishHistoryCard._loading = true;
const historyRequestVersion = englishHistoryCard._requestVersion;
englishHistoryCard.setConfig({ language: "en", entities: ["sensor.pv"] });
if (
  englishHistoryCard._config.title ||
  englishHistoryCard._copy().title !== "Power — last 24 hours"
) {
  throw new Error("Aurora history did not use its translated default title");
}
if (
  englishHistoryCard._loading ||
  englishHistoryCard._requestVersion !== historyRequestVersion + 1
) {
  throw new Error("Aurora history did not invalidate an in-flight request on reconfigure");
}
const historyCard = new AuroraHistoryCard();
historyCard.setConfig({
  hours_to_show: 24,
  entities: [
    { entity: "sensor.pv", name: "PV", color: "#2de083" },
    { entity: "sensor.grid", name: "Grid", color: "invalid" },
  ],
});
historyCard._hass = {
  states: {
    "sensor.pv": { state: "1550", attributes: { unit_of_measurement: "W" } },
    "sensor.grid": { state: "-2.5", attributes: { unit_of_measurement: "kW" } },
  },
};
if (historyCard._currentValue("sensor.pv") !== 1.55) {
  throw new Error("Aurora history did not convert W history/current data to kW");
}
if (historyCard._currentValue("sensor.grid") !== -2.5) {
  throw new Error("Aurora history changed the signed grid value");
}
if (historyCard._config.entities[1].color !== "#ff5d73") {
  throw new Error("Aurora history did not replace an unsafe series color");
}
const historyEnd = Date.parse("2026-08-11T12:00:00Z");
const normalizedHistory = historyCard._normalizeHistory(
  [[
    {
      entity_id: "sensor.pv",
      state: "1000",
      last_changed: "2026-08-11T11:00:00Z",
    },
  ]],
  historyEnd - 24 * 60 * 60 * 1000,
  historyEnd,
);
if (normalizedHistory.get("sensor.pv")?.[0]?.value !== 1) {
  throw new Error("Aurora history did not normalize recorder data");
}
if (historyCard._powerKw("unavailable", "sensor.pv") !== null) {
  throw new Error("Aurora history did not safely ignore unavailable data");
}
const historySource = source.slice(
  source.indexOf("class HoymilesAuroraHistoryCard"),
  source.indexOf("class HoymilesAuroraFinanceCard"),
);
for (const expected of [
  'this._hass.callApi("GET", path)',
  "linearGradient",
  "@container (max-width: 360px)",
  "prefers-reduced-motion: reduce",
]) {
  if (!historySource.includes(expected)) {
    throw new Error(`Aurora history card is missing: ${expected}`);
  }
}
if (historySource.includes("setInterval(") || historySource.includes("setTimeout(")) {
  throw new Error("Aurora history introduced a timer that can leak after disconnect");
}

const AuroraFinanceCard = registry.get("hoymiles-aurora-finance-card");
const financeCard = new AuroraFinanceCard();
financeCard.setConfig({});
financeCard._hass = {
  states: {
    "sensor.hoymiles_rce_revenue_daily": { state: "unavailable", attributes: {} },
    "sensor.hoymiles_rce_grid_export_energy_daily": {
      state: "12500",
      attributes: { unit_of_measurement: "Wh" },
    },
    "sensor.hoymiles_rce_grid_export_power": {
      state: "3500",
      attributes: { unit_of_measurement: "W" },
    },
  },
};
if (financeCard._numeric("sensor.hoymiles_rce_revenue_daily") !== null) {
  throw new Error("Aurora finance did not safely handle unavailable revenue");
}
if (financeCard._energy("sensor.hoymiles_rce_grid_export_energy_daily") !== 12.5) {
  throw new Error("Aurora finance did not convert exported Wh to kWh");
}
if (financeCard._power("sensor.hoymiles_rce_grid_export_power") !== 3.5) {
  throw new Error("Aurora finance did not convert export W to kW");
}
console.log("Aurora frame/status/history/finance: registration and safe data handling OK");

const AuroraCard = registry.get("hoymiles-aurora-energy-card");
if (!AuroraCard) {
  throw new Error("The Aurora energy custom element was not registered");
}
if (
  !context.window.customCards?.some(
    (item) => item.type === "hoymiles-aurora-energy-card",
  )
) {
  throw new Error("The Aurora card is absent from custom-card metadata");
}
const supervisorValidator = context.__supervisorValidatorExports;
if (!source.includes("class HoymilesEmsSupervisorPanel")) {
  throw new Error("Aurora module is missing the bounded Supervisor controller");
}
if (source.includes('customElements.define("hoymiles-ems-supervisor-panel"')) {
  throw new Error("Supervisor panel was incorrectly registered as a standalone card");
}
const supervisorStart = source.indexOf("const HOYMILES_SUPERVISOR_BINDINGS");
const supervisorEnd = source.indexOf("class HoymilesAuroraEnergyCard");
const supervisorSource = source.slice(supervisorStart, supervisorEnd);
const energyEnd = source.indexOf("class HoymilesPowerFlowCard", supervisorEnd);
const energySource = source.slice(supervisorEnd, energyEnd);
const auroraMarkup = energySource.indexOf('<div class="aurora">');
const dailyMarkup = energySource.indexOf('<div class="daily">', auroraMarkup);
if (
  !(auroraMarkup >= 0 && dailyMarkup > auroraMarkup)
  || energySource.includes("data-supervisor")
  || energySource.includes("_supervisorPanel")
  || energySource.includes("HOYMILES_SUPERVISOR_BINDINGS")
) {
  throw new Error("Start is not restored to Aurora followed by daily energy");
}
const SupervisorCard = registry.get("hoymiles-ems-supervisor-card");
if (
  SupervisorCard !== supervisorValidator.HoymilesEmsSupervisorCard
  || (supervisorSource.match(/customElements\.define\(\s*"hoymiles-ems-supervisor-card"/g) || []).length !== 1
  || context.window.customCards.filter((item) => item.type === "hoymiles-ems-supervisor-card").length !== 1
) {
  throw new Error("Standalone Supervisor card is not registered exactly once");
}
const canonicalDashboard = fs.readFileSync("dashboard_hoymiles.yaml", "utf8");
const dashboardViews = [...canonicalDashboard.matchAll(/^  - title: (.+)$/gm)];
const startViewSource = canonicalDashboard.slice(dashboardViews[0].index, dashboardViews[1].index);
const supervisorViewSource = canonicalDashboard.slice(dashboardViews[1].index, dashboardViews[2].index);
if (
  dashboardViews[0]?.[1] !== "Start"
  || dashboardViews[1]?.[1] !== "Nadzorca EMS"
  || dashboardViews[2]?.[1] !== "RCE i Wyniki"
  || !supervisorViewSource.includes("path: ems-supervisor")
  || !supervisorViewSource.includes("icon: mdi:eye-circle-outline")
  || !supervisorViewSource.includes("type: panel")
  || (supervisorViewSource.match(/custom:hoymiles-ems-supervisor-card/g) || []).length !== 1
  || /supervisor_(entity|mode_entity|profile_entity|allow_)/.test(startViewSource)
) {
  throw new Error("Canonical dedicated Supervisor view identity/order is invalid");
}
for (const [language, expectedTitle] of [["pl", "Nadzorca EMS"], ["en", "EMS Supervisor"]]) {
  const generated = JSON.parse(fs.readFileSync(
    `custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_${language}.json`,
    "utf8",
  ));
  const view = generated.views?.[1];
  if (
    view?.title !== expectedTitle
    || view?.path !== "ems-supervisor"
    || view?.icon !== "mdi:eye-circle-outline"
    || view?.type !== "panel"
    || view?.cards?.length !== 1
    || view.cards[0]?.type !== "custom:hoymiles-ems-supervisor-card"
  ) {
    throw new Error(`Generated ${language} Supervisor view identity is invalid`);
  }
}
for (const [key, entityId] of Object.entries({
  supervisor_entity: "sensor.hoymiles_hit_ems_supervisor",
  supervisor_mode_entity: "input_select.hoymiles_ems_supervisor_mode",
  supervisor_profile_entity: "input_select.hoymiles_ems_supervisor_profile",
  supervisor_allow_rce_entity: "input_boolean.hoymiles_ems_supervisor_allow_rce",
  supervisor_allow_tariff_entity:
    "input_boolean.hoymiles_ems_supervisor_allow_tariff",
  supervisor_allow_rcm_entity: "input_boolean.hoymiles_ems_supervisor_allow_rcm",
})) {
  if (supervisorValidator.HOYMILES_SUPERVISOR_BINDINGS[key] !== entityId) {
    throw new Error(`Supervisor binding ${key} is not canonical`);
  }
  if (!supervisorViewSource.includes(`${key}: ${entityId}`)) {
    throw new Error(`Dedicated Supervisor view is missing binding ${key}`);
  }
}
if (
  JSON.stringify(Array.from(supervisorValidator.HOYMILES_SUPERVISOR_MODE_OPTIONS))
    !== JSON.stringify(["Off", "Shadow"])
  || JSON.stringify(Array.from(supervisorValidator.HOYMILES_SUPERVISOR_PROFILE_OPTIONS))
    !== JSON.stringify(["Balanced", "Maximum Profit", "High Reserve — Winter"])
) {
  throw new Error("Supervisor helper option whitelist changed");
}
const literalSupervisorTargets = {
  mode: "input_select.hoymiles_ems_supervisor_mode",
  profile: "input_select.hoymiles_ems_supervisor_profile",
  allowRce: "input_boolean.hoymiles_ems_supervisor_allow_rce",
  allowTariff: "input_boolean.hoymiles_ems_supervisor_allow_tariff",
  allowRcm: "input_boolean.hoymiles_ems_supervisor_allow_rcm",
};
for (const target of Object.values(literalSupervisorTargets)) {
  if (!supervisorSource.includes(JSON.stringify(target))) {
    throw new Error(`Supervisor closed target map is missing: ${target}`);
  }
}
for (const allowed of [
  "const HOYMILES_SUPERVISOR_CONTROL_KINDS",
  "_attestHelperCommand(request, intendedState)",
  'domain: "input_select"',
  'service: "select_option"',
  'domain: "input_boolean"',
  'service: intendedState === "on" ? "turn_on" : "turn_off"',
  'liveOptions.every((value) => typeof value === "string")',
  "kind.options.includes(currentState)",
  "kind.options.includes(intendedState)",
  "_requestOwnsCurrentPanel(request)",
  "this._lifecycleGeneration += 1",
]) {
  if (!supervisorSource.includes(allowed)) {
    throw new Error(`Supervisor final attestation is missing: ${allowed}`);
  }
}
for (const forbidden of [
  "number.set_value",
  "select.select_option",
  "button.press",
  "input_boolean.toggle",
  "modbus.write",
  "setInterval",
  "requestAnimationFrame",
  "setTimeout",
]) {
  if (supervisorSource.includes(forbidden)) {
    throw new Error(`Supervisor scope contains forbidden authority or polling: ${forbidden}`);
  }
}
if (Object.keys(supervisorValidator.HOYMILES_SUPERVISOR_REASON_COPY).length !== 45) {
  throw new Error("Supervisor reason map is not 45/45");
}
for (const required of [
  "Observation only",
  "Tylko obserwacja",
  "@container (max-width: 620px)",
  "@container (max-width: 390px)",
  "@media (prefers-reduced-motion: reduce)",
  "max-width: 1440px",
  "supervisor-blob-three",
  "modeActiveDescription",
  "technicalTitle",
  "overflow-wrap: anywhere",
  "min-width: 0",
]) {
  if (!supervisorSource.includes(required)) {
    throw new Error(`Supervisor standalone card is missing responsive/status contract: ${required}`);
  }
}
const supervisorCss = supervisorValidator.HOYMILES_EMS_SUPERVISOR_CSS;
const paletteMarker = "SUPERVISOR_SEMANTIC_PALETTE_REV28";
const paletteStart = supervisorCss.indexOf(paletteMarker);
const paletteEnd = supervisorCss.indexOf("  }", paletteStart);
if (
  (supervisorCss.match(/SUPERVISOR_SEMANTIC_PALETTE_REV28/g) || []).length !== 1
  || paletteStart < 0
  || paletteEnd <= paletteStart
) {
  throw new Error("Revision 28 must contain one centralized Supervisor palette");
}
const paletteBlock = supervisorCss.slice(paletteStart, paletteEnd);
const cssOutsidePalette =
  supervisorCss.slice(0, paletteStart) + supervisorCss.slice(paletteEnd);
const lightThemeStart = supervisorCss.indexOf("@media (prefers-color-scheme: light)");
const lightThemeEnd = supervisorCss.indexOf(
  "@media (prefers-reduced-motion: reduce)",
  lightThemeStart,
);
const lightThemeCss = supervisorCss.slice(lightThemeStart, lightThemeEnd);
for (const token of [
  "--supervisor-page-base: color-mix(in srgb, var(--primary-background-color, var(--supervisor-on-deep)) 94%, var(--supervisor-blue) 6%)",
  "--supervisor-page-cyan-glow: color-mix(in srgb, var(--supervisor-cyan) 5%, transparent)",
  "--supervisor-page-violet-glow: color-mix(in srgb, var(--supervisor-violet) 4%, transparent)",
  ".supervisor-blob { opacity: .08; }",
  ".supervisor-points { opacity: .1; }",
]) {
  if (lightThemeStart < 0 || lightThemeEnd <= lightThemeStart || !lightThemeCss.includes(token)) {
    throw new Error("Revision 28 light theme is not independently pale/restrained: " + token);
  }
}
if (
  /opacity:\s*\.(?:64|72)\b/.test(supervisorCss)
  || !supervisorCss.includes(".supervisor-info-disabled { border-style: dashed; opacity: 1; }")
  || !supervisorCss.includes('.supervisor-policy[data-permitted="false"] { filter: saturate(.68); opacity: 1; }')
) {
  throw new Error("Revision 28 disabled content loses light-theme contrast");
}
const undersizedSupervisorText = [...supervisorCss.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g)]
  .filter((match) => Number(match[1]) < 11);
if (undersizedSupervisorText.length !== 0) {
  throw new Error("Revision 28 contains Supervisor text below the accepted 11px minimum");
}
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
  if (!paletteBlock.includes("--supervisor-" + name + ": " + value)) {
    throw new Error("Revision 28 palette lost " + name);
  }
}
if (
  /#[0-9a-fA-F]{3,8}/.test(cssOutsidePalette)
  || !supervisorCss.includes("--supervisor-surface: color-mix")
) {
  throw new Error("Revision 28 contains an ad-hoc color or non-derived surface");
}
if (
  JSON.stringify(supervisorValidator.HOYMILES_SUPERVISOR_POLICY_ICONS)
    !== JSON.stringify({
      rce: "mdi:chart-line",
      tariff: "mdi:battery-clock-outline",
      rcm: "mdi:transmission-tower",
    })
) {
  throw new Error("Revision 28 policy icon map is not exact");
}
for (const required of [
  "supervisor-authority",
  "supervisor-core-inputs",
  "supervisor-core-orb",
  "supervisor-hero-result",
  "supervisor-no-control",
  "supervisor-quick-facts",
  "supervisor-policy-identity",
  "supervisor-policy-permission",
  "supervisor-permission-context",
  "supervisor-safety-strip",
  "supervisor-knowledge",
  "supervisor-knowledge-detail",
  "supervisor-core-ring { animation: none",
  'content: "↓"',
]) {
  if (!supervisorSource.includes(required)) {
    throw new Error("Revision 28 visual hierarchy is missing: " + required);
  }
}
for (const [language, legacyHash] of Object.entries({
  pl: "5b039fe8eed77bb7ffcb116ebac4fb5362047c45356f6abe8eb5e04ef57e9f66",
  en: "d40dceefa5352b5d9cce2a74cf8da287ee73966ce54bb549617da8ecd45f0e79",
})) {
  const entries = Object.entries(
    supervisorValidator.HOYMILES_SUPERVISOR_COPY[language],
  );
  const legacy = entries
    .slice(0, 146)
    .sort(([left], [right]) => left.localeCompare(right));
  const actualHash = crypto
    .createHash("sha256")
    .update(JSON.stringify(legacy))
    .digest("hex");
  if (entries.length !== 166 || actualHash !== legacyHash) {
    throw new Error("Revision 28 did not preserve legacy " + language + " copy");
  }
}
for (const exactCopy of [
  "Nie — tylko obserwacja",
  "No — observation only",
  "Nadzorca jest wyłączony. Istniejące automatyki działają bez zmian.",
  "The Supervisor is off. Existing automations continue to work unchanged.",
  "Zgoda pozwala tylko uwzględnić wynik w analizie Shadow.",
  "Permission only allows the result to be considered in Shadow analysis.",
  "Nadzorca nie steruje falownikiem. Pokazuje wyłącznie wynik analizy i może zmieniać tylko pięć ustawień własnego interfejsu.",
  "The Supervisor does not control the inverter. It only shows the analysis result and can change only its five UI settings.",
]) {
  if (!supervisorSource.includes(exactCopy)) {
    throw new Error("Revision 28 exact copy missing: " + exactCopy);
  }
}
if (
  (supervisorSource.match(/this\._knowledgeDetail\(/g) || []).length !== 5
  || supervisorSource.includes('details.setAttribute("open"')
  || supervisorSource.includes("accordionState")
  || supervisorSource.includes("toggleDetails")
) {
  throw new Error("Revision 28 knowledge area is not exactly five closed native details");
}
const contentAppendStart = supervisorSource.indexOf("    content.append(");
const contentAppendEnd = supervisorSource.indexOf("    panel.append(", contentAppendStart);
const contentAppend = supervisorSource.slice(contentAppendStart, contentAppendEnd);
if (
  !/hero,[\s\S]*liveGrid,[\s\S]*policySection,[\s\S]*howSection,[\s\S]*safetyStrip,[\s\S]*knowledgeSection/.test(contentAppend)
  || /modesSection|profilesSection|permissionsSection|readSection|safetySection|technical\b/.test(contentAppend)
) {
  throw new Error("Revision 28 did not move the documentation wall below live status");
}
const selectedToneStart = supervisorCss.indexOf(
  '.supervisor-panel[data-tone="shadow-selected"]',
);
const selectedToneEnd = supervisorCss.indexOf(
  '.supervisor-panel[data-tone="blocked"]',
  selectedToneStart,
);
if (
  selectedToneStart < 0
  || selectedToneEnd <= selectedToneStart
  || supervisorCss.slice(selectedToneStart, selectedToneEnd).includes("supervisor-ready")
  || !supervisorCss.includes('.supervisor-policy-badge[data-tone="ready"]')
) {
  throw new Error("Revision 28 selected Shadow uses green or readiness lost its bound");
}
for (const token of [
  "--supervisor-deep",
  "supervisor-blob-three",
  "supervisor-points",
  "supervisor-vignette",
  "pointer-events: none",
  "overflow: clip",
  "@container (max-width: 1024px)",
  "@container (max-width: 768px)",
  "@container (max-width: 390px)",
  "@media (prefers-reduced-motion: reduce)",
  ".supervisor-knowledge-detail { width: 100%; }",
]) {
  if (!supervisorCss.includes(token)) {
    throw new Error("Revision 28 responsive/background contract missing: " + token);
  }
}
if (
  supervisorCss.includes("filter: blur")
  || /supervisor-scope[^}]*display:\s*none/s.test(supervisorCss)
  || supervisorSource.includes("setInterval")
  || supervisorSource.includes("setTimeout")
) {
  throw new Error("Revision 28 introduced blur, hidden authority, polling, or timers");
}
for (const sensor of [
  undefined,
  { state: "unavailable", attributes: {} },
  { state: "shadow_idle", attributes: null },
  { state: "shadow_idle", attributes: { candidate_summaries: {} } },
  {
    state: "shadow_idle",
    attributes: { candidate_summaries: [null, 4, "bad"] },
  },
]) {
  let normalized;
  try {
    normalized = supervisorValidator.hoymilesNormalizeSupervisor(sensor);
  } catch (error) {
    throw new Error(`Supervisor normalization threw: ${error.message}`);
  }
  if (!normalized || !Array.isArray(normalized.candidates) || normalized.candidates.length !== 3) {
    throw new Error("Supervisor malformed-state fallback lost its three bounded rows");
  }
}

function supervisorRuntimeHass(helperOverrides = {}) {
  const states = {
    "sensor.hoymiles_hit_ems_supervisor": {
      state: "shadow_idle",
      attributes: {},
    },
    "input_select.hoymiles_ems_supervisor_mode": {
      state: "Shadow",
      attributes: { options: ["Off", "Shadow"] },
    },
    "input_select.hoymiles_ems_supervisor_profile": {
      state: "Balanced",
      attributes: {
        options: ["Balanced", "Maximum Profit", "High Reserve — Winter"],
      },
    },
    "input_boolean.hoymiles_ems_supervisor_allow_rce": {
      state: "on",
      attributes: {},
    },
    "input_boolean.hoymiles_ems_supervisor_allow_tariff": {
      state: "off",
      attributes: {},
    },
    "input_boolean.hoymiles_ems_supervisor_allow_rcm": {
      state: "on",
      attributes: {},
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

function supervisorRuntimePanel(hass, configOverrides = {}) {
  const container = new FakeNode("section");
  const panel = new supervisorValidator.HoymilesEmsSupervisorPanel(container);
  const config = {
    supervisor_entity: "sensor.hoymiles_hit_ems_supervisor",
    supervisor_mode_entity: "input_select.hoymiles_ems_supervisor_mode",
    supervisor_profile_entity: "input_select.hoymiles_ems_supervisor_profile",
    supervisor_allow_rce_entity:
      "input_boolean.hoymiles_ems_supervisor_allow_rce",
    supervisor_allow_tariff_entity:
      "input_boolean.hoymiles_ems_supervisor_allow_tariff",
    supervisor_allow_rcm_entity:
      "input_boolean.hoymiles_ems_supervisor_allow_rcm",
    ...configOverrides,
  };
  panel.connect();
  panel.update(hass, config, "en");
  return { container, panel, config };
}

const exactTargetHass = supervisorRuntimeHass();
const exactTargetPanel = supervisorRuntimePanel(exactTargetHass).panel;
const walkSupervisorDom = (rootNode) => {
  const nodes = [];
  const visit = (node) => {
    if (!node || typeof node !== "object") return;
    nodes.push(node);
    for (const child of node.children || []) visit(child);
  };
  visit(rootNode);
  return nodes;
};
const supervisorDom = walkSupervisorDom(exactTargetPanel._container);
const supervisorDetails = supervisorDom.filter(
  (node) => node.tagName === "details",
);
if (
  Object.keys(exactTargetPanel._controls).length !== 5
  || Object.keys(exactTargetPanel._policyRows).length !== 3
  || supervisorDetails.length !== 5
  || !supervisorDetails.every(
    (detail) =>
      detail.getAttribute("open") === null
      && detail.children[0]?.tagName === "summary",
  )
  || supervisorDom.filter(
    (node) => String(node.className).includes("supervisor-safety-strip"),
  ).length !== 1
  || exactTargetPanel._hero.physical.textContent !== "No — observation only"
  || exactTargetPanel._heroResultText.textContent
    !== "Data required for evaluation is unavailable."
) {
  throw new Error("Revision 28 live DOM hierarchy/count contract failed");
}
void exactTargetPanel._selectOption("mode", "Off");
if (
  exactTargetHass.calls.length !== 1 ||
  exactTargetHass.calls[0].data.entity_id !==
    "input_select.hoymiles_ems_supervisor_mode"
) {
  throw new Error("Supervisor final target was not derived from the literal closed mapping");
}

const redirectedHass = supervisorRuntimeHass({
  "input_boolean.hoymiles_rce_discharge_enabled": {
    state: "off",
    attributes: {},
  },
});
const redirectedPanel = supervisorRuntimePanel(redirectedHass, {
  supervisor_allow_rce_entity:
    "input_boolean.hoymiles_rce_discharge_enabled",
}).panel;
void redirectedPanel._toggleBoolean("allowRce");
if (redirectedHass.calls.length !== 0) {
  throw new Error("Supervisor accepted a legacy helper target from dashboard config");
}

const unsafeCurrentHass = supervisorRuntimeHass({
  "input_select.hoymiles_ems_supervisor_mode": {
    state: "Active",
    attributes: { options: ["Off", "Shadow", "Active"] },
  },
});
const unsafeCurrentPanel = supervisorRuntimePanel(unsafeCurrentHass).panel;
void unsafeCurrentPanel._selectOption("mode", "Off");
if (
  unsafeCurrentHass.calls.length !== 0 ||
  !unsafeCurrentPanel._controls.mode.element.disabled
) {
  throw new Error("Supervisor final boundary accepted unsafe current mode Active");
}

const syntheticHass = supervisorRuntimeHass({
  "input_select.hoymiles_ems_supervisor_mode": {
    state: "Shadow",
    attributes: { options: ["Shadow"] },
  },
});
void supervisorRuntimePanel(syntheticHass).panel._selectOption("mode", "Off");
if (syntheticHass.calls.length !== 0) {
  throw new Error("Supervisor submitted a synthetic option absent from the live helper");
}

for (const [value, expected] of [
  ["pl", "pl"],
  ["pl-PL", "pl"],
  ["en-US", "en"],
  ["unsupported", "en"],
  ["", "en"],
  [null, "en"],
  [undefined, "en"],
  [7, "en"],
  [true, "en"],
  [{}, "en"],
  [[], "en"],
  [Symbol("pl"), "en"],
]) {
  let language;
  try {
    language = supervisorValidator.hoymilesNormalizeLanguage(value);
  } catch (error) {
    throw new Error(`Supervisor language normalization threw: ${error.message}`);
  }
  if (language !== expected) {
    throw new Error(`Supervisor language fallback returned ${language}, expected ${expected}`);
  }
}

const supervisorLifecycleValidationPromise = (async () => {
  const oldHass = supervisorRuntimeHass();
  let rejectOld;
  oldHass.callService = (domain, service, data) => {
    oldHass.calls.push({ domain, service, data });
    return new Promise((_resolve, reject) => {
      rejectOld = reject;
    });
  };
  const fixture = supervisorRuntimePanel(oldHass);
  const panelInstance = fixture.panel;
  const oldRequest = panelInstance._toggleBoolean("allowRce");
  const generation = panelInstance._lifecycleGeneration;
  panelInstance.disconnect();
  if (
    panelInstance._lifecycleGeneration !== generation + 1 ||
    panelInstance._hass !== null ||
    panelInstance._pending.size !== 0
  ) {
    throw new Error("Supervisor disconnect did not invalidate its lifecycle");
  }
  panelInstance.connect();
  const newHass = supervisorRuntimeHass();
  let resolveNew;
  newHass.callService = (domain, service, data) => {
    newHass.calls.push({ domain, service, data });
    return new Promise((resolve) => {
      resolveNew = resolve;
    });
  };
  panelInstance.update(newHass, fixture.config, "en");
  const newRequest = panelInstance._toggleBoolean("allowRce");
  const newToken = panelInstance._requestTokens.get("allowRce");
  rejectOld(new Error("stale rejection"));
  await oldRequest;
  if (
    !panelInstance._pending.has("allowRce") ||
    panelInstance._requestTokens.get("allowRce") !== newToken ||
    panelInstance._errors.has("allowRce")
  ) {
    throw new Error("Stale Promise settlement mutated the reconnected panel");
  }
  resolveNew();
  await newRequest;

  const parent = new supervisorValidator.HoymilesEmsSupervisorCard();
  parent.isConnected = true;
  parent.setConfig(fixture.config);
  parent.hass = newHass;
  const ownedPanel = parent._panelController;
  const ownedRoot = ownedPanel._container.children[0];
  parent.disconnectedCallback();
  if (parent._hass !== null || ownedPanel._hass !== null) {
    throw new Error("Standalone card or Supervisor controller retained hass after disconnect");
  }
  parent.connectedCallback();
  if (
    parent._panelController !== ownedPanel ||
    ownedPanel._container.children.length !== 1 ||
    ownedPanel._container.children[0] !== ownedRoot ||
    !Object.values(ownedPanel._controls).every(
      (control) => control.element.disabled,
    )
  ) {
    throw new Error("Standalone reconnect duplicated or prematurely enabled the panel");
  }
})();

if (
  fs.readFileSync(
    "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js",
    "utf8",
  ) !== source
) {
  throw new Error("Packaged Aurora module differs from the canonical source");
}
console.log("EMS Supervisor card: standalone, bounded and observation-only contract OK");
for (const expected of [
  "container-type: inline-size",
  "prefers-reduced-motion: reduce",
  'data-ribbon="pv"',
  'data-flow="battery"',
  'data-ribbon="battery" d="M450 210 C675 245 685 425 950 390"',
  '<button class="metric battery" data-key="battery">',
  '.metric.battery { --metric-color: var(--orbit-battery); bottom: 2%; min-width: 238px; right: 5%; }',
  'data-label="grid_import_title"',
  'data-key="grid_import_today"',
  'data-key="grid_to_load_today"',
  'data-key="grid_to_battery_today"',
  'data-value="battery_detail"',
  'data-value="battery_eta"',
  'data-key="forecast_tomorrow"',
  'data-value="cav_temperature"',
  'data-value="battery_path_temperature"',
  'grid-template-columns: repeat(4, minmax(0, 1fr))',
  'grid-template-columns: repeat(2, minmax(0, 1fr))',
  'new CustomEvent("hass-more-info"',
]) {
  if (!source.includes(expected)) {
    throw new Error(`Aurora card is missing: ${expected}`);
  }
}
const auroraCard = new AuroraCard();
auroraCard.setConfig({
  pv_entity: "sensor.pv_w",
  grid_entity: "sensor.grid_kw",
});
auroraCard._hass = {
  language: "pl",
  states: {
    "sensor.pv_w": {
      state: "1550",
      attributes: { unit_of_measurement: "W" },
    },
    "sensor.grid_kw": {
      state: "-2.5",
      attributes: { unit_of_measurement: "kW" },
    },
    "sensor.hoymiles_hit_grid_energy_buy_today": {
      state: "12.5",
      attributes: { unit_of_measurement: "kWh" },
    },
    "sensor.hoymiles_solcast_forecast_tomorrow": {
      state: "18.75",
      attributes: { unit_of_measurement: "kWh" },
    },
    "sensor.hoymiles_hit_cav_temp": {
      state: "48.25",
      attributes: { unit_of_measurement: "°C" },
    },
    "sensor.hoymiles_hit_bat_ths_temp": {
      state: "42.75",
      attributes: { unit_of_measurement: "°C" },
    },
  },
};
if (auroraCard._powerKw("pv") !== 1.55) {
  throw new Error("Aurora card did not convert W to kW");
}
if (auroraCard._powerKw("grid") !== -2.5) {
  throw new Error("Aurora card changed the signed kW grid value");
}
if (auroraCard._formatPower(1.55) !== "1,55 kW") {
  throw new Error("Aurora card does not format power with two decimals");
}
if (
  auroraCard._entityId("grid_import_today") !==
    "sensor.hoymiles_hit_grid_energy_buy_today" ||
  auroraCard._entityId("grid_to_load_today") !==
    "sensor.hoymiles_rce_grid_to_load_today" ||
  auroraCard._entityId("grid_to_battery_today") !==
    "sensor.hoymiles_grid_to_battery_today"
) {
  throw new Error("Aurora card grid-import strip defaults changed");
}
if (auroraCard._formatEnergy("grid_import_today") !== "12,5 kWh") {
  throw new Error("Aurora card did not format today's grid import energy");
}
if (
  auroraCard._entityId("forecast_tomorrow") !==
    "sensor.hoymiles_solcast_forecast_tomorrow" ||
  auroraCard._entityId("cav_temperature") !== "sensor.hoymiles_hit_cav_temp" ||
  auroraCard._entityId("battery_path_temperature") !==
    "sensor.hoymiles_hit_bat_ths_temp" ||
  auroraCard._formatEnergy("forecast_tomorrow") !== "18,8 kWh" ||
  auroraCard._formatTemperature("cav_temperature") !== "48,3 °C" ||
  auroraCard._formatTemperature("battery_path_temperature") !== "42,8 °C"
) {
  throw new Error("Aurora tomorrow forecast or temperature defaults changed");
}

const freshAuroraState = (value, unit, ageSeconds = 0) => ({
  state: String(value),
  attributes: unit ? { unit_of_measurement: unit } : {},
  last_reported: new Date(Date.now() - ageSeconds * 1000).toISOString(),
});
const batteryAuroraCard = new AuroraCard();
batteryAuroraCard.setConfig({ language: "pl" });
batteryAuroraCard._hass = {
  language: "pl",
  states: {
    "sensor.hoymiles_hit_overview_battery_power": freshAuroraState(-3200, "W"),
    "sensor.hoymiles_hit_overview_battery_soc": freshAuroraState(62, "%"),
    "sensor.hoymiles_hit_battery_current_bms": freshAuroraState(-61, "A"),
    "sensor.hoymiles_hit_battery_capacity": freshAuroraState(22.7, "kWh"),
    "sensor.hoymiles_hit_ems_mode_readback_code": freshAuroraState(4),
    "sensor.hoymiles_hit_ems_force_charge_soc_readback": freshAuroraState(80, "%"),
    "sensor.hoymiles_hit_ems_force_discharge_soc_readback": freshAuroraState(50, "%"),
    "sensor.hoymiles_hit_ems_self_use_soc_readback": freshAuroraState(30, "%"),
    "number.hoymiles_hit_maximum_soc": freshAuroraState(95, "%"),
    "number.hoymiles_hit_minimum_soc": freshAuroraState(10, "%"),
    "sensor.hoymiles_solcast_forecast_tomorrow": freshAuroraState(18.75, "kWh"),
    "sensor.hoymiles_hit_cav_temp": freshAuroraState(48.25, "°C"),
    "sensor.hoymiles_hit_bat_ths_temp": freshAuroraState(42.75, "°C"),
  },
};
const chargingBattery = batteryAuroraCard._batteryPresentation();
if (
  chargingBattery.direction !== "ładowanie" ||
  chargingBattery.powerKw !== -3.2 ||
  Math.abs(chargingBattery.storedKwh - 14.074) > 0.0001 ||
  chargingBattery.currentA !== 61 ||
  chargingBattery.etaKind !== "target" ||
  Math.abs(chargingBattery.etaHours - 1.276875) > 0.0001 ||
  batteryAuroraCard._formatBatteryEta(
    chargingBattery.etaHours,
    chargingBattery.etaKind,
  ) !== "~1 h 17 min do celu"
) {
  throw new Error("Aurora battery charge view did not use physical 4303 target data");
}
const batteryDomWrites = {};
batteryAuroraCard._mounted = true;
batteryAuroraCard._card = { querySelector: () => null };
batteryAuroraCard._setText = (selector, value) => {
  batteryDomWrites[selector] = value;
};
batteryAuroraCard._setFlow = () => {};
batteryAuroraCard._update();
if (
  batteryDomWrites['[data-value="battery_detail"]'] !== "14,1 kWh · 61,0 A" ||
  batteryDomWrites['[data-value="battery_eta"]'] !== "~1 h 17 min do celu" ||
  batteryDomWrites['[data-value="forecast_tomorrow"]'] !== "18,8 kWh" ||
  batteryDomWrites['[data-value="cav_temperature"]'] !== "48,3 °C" ||
  batteryDomWrites['[data-value="battery_path_temperature"]'] !== "42,8 °C" ||
  batteryDomWrites['[data-label="cav_temperature"]'] !== "Temperatura Falownika" ||
  batteryDomWrites['[data-label="battery_path_temperature"]'] !==
    "Temperatura toru magazynu"
) {
  throw new Error("Aurora battery detail and ETA were not written to the live DOM");
}

const englishBatteryAuroraCard = new AuroraCard();
englishBatteryAuroraCard.setConfig({ language: "en" });
englishBatteryAuroraCard._hass = {
  language: "en",
  states: batteryAuroraCard._hass.states,
};
const englishChargingBattery = englishBatteryAuroraCard._batteryPresentation();
if (
  englishBatteryAuroraCard._copy().cav_temperature !== "Inverter temperature" ||
  englishBatteryAuroraCard._copy().battery_path_temperature !==
    "Battery circuit temperature" ||
  englishBatteryAuroraCard._formatBatteryEta(
    englishChargingBattery.etaHours,
    englishChargingBattery.etaKind,
  ) !== "~1 h 17 min to target"
) {
  throw new Error("Aurora battery ETA English copy changed");
}

const batteryStates = batteryAuroraCard._hass.states;
batteryStates["sensor.hoymiles_hit_overview_battery_power"] =
  freshAuroraState(800, "W");
batteryStates["sensor.hoymiles_hit_overview_battery_soc"] =
  freshAuroraState(81, "%");
batteryStates["sensor.hoymiles_hit_battery_current_bms"] =
  freshAuroraState(-15, "A");
batteryStates["sensor.hoymiles_hit_ems_mode_readback_code"] =
  freshAuroraState(0);
const selfUseDischarge = batteryAuroraCard._batteryPresentation();
if (
  selfUseDischarge.direction !== "rozładowanie" ||
  selfUseDischarge.currentA !== 15 ||
  selfUseDischarge.etaKind !== "reserve" ||
  Math.abs(selfUseDischarge.etaHours - 14.47125) > 0.0001
) {
  throw new Error("Aurora battery discharge view did not use physical 4301 reserve");
}

batteryStates["sensor.hoymiles_hit_ems_mode_readback_code"] =
  freshAuroraState(5);
const forcedDischarge = batteryAuroraCard._batteryPresentation();
if (Math.abs(forcedDischarge.etaHours - 8.79625) > 0.0001) {
  throw new Error("Aurora Grid Discharge ETA did not use the physical 4305 floor");
}

batteryStates["sensor.hoymiles_hit_ems_mode_readback_code"] =
  freshAuroraState(3);
const offGridDischarge = batteryAuroraCard._batteryPresentation();
if (Math.abs(offGridDischarge.etaHours - 20.14625) > 0.0001) {
  throw new Error("Aurora non-EMS discharge ETA did not use configured minimum SOC");
}

batteryStates["sensor.hoymiles_hit_overview_battery_power"] =
  freshAuroraState(-3200, "W");
batteryStates["sensor.hoymiles_hit_overview_battery_soc"] =
  freshAuroraState(62, "%");
const offGridCharge = batteryAuroraCard._batteryPresentation();
if (Math.abs(offGridCharge.etaHours - 2.3409375) > 0.0001) {
  throw new Error("Aurora non-Grid-Charge ETA did not use configured maximum SOC");
}

batteryStates["sensor.hoymiles_hit_overview_battery_power"] =
  freshAuroraState(-3200, "W", 121);
const stalePower = batteryAuroraCard._batteryPresentation();
if (
  Math.abs(stalePower.powerKw + 3.2) > 0.0001 ||
  !Number.isFinite(stalePower.etaHours)
) {
  throw new Error("Aurora battery view hid available unchanged battery power");
}
batteryStates["sensor.hoymiles_hit_overview_battery_power"] =
  freshAuroraState(0, "W");
if (
  batteryAuroraCard._batteryPresentation().etaHours !== null ||
  batteryAuroraCard._formatBatteryEta(null, null) !== "ETA —" ||
  batteryAuroraCard._formatBatteryEta(169, "target") !== "ETA —"
) {
  throw new Error("Aurora battery ETA did not fail closed at idle");
}
batteryStates["sensor.hoymiles_hit_battery_current_bms"] =
  freshAuroraState(15, "A", 301);
if (batteryAuroraCard._batteryPresentation().currentA !== 15) {
  throw new Error("Aurora battery view hid available unchanged BMS current");
}
batteryStates["sensor.hoymiles_hit_overview_battery_power"] =
  freshAuroraState(-3200, "W");
batteryStates["sensor.hoymiles_hit_overview_battery_soc"] =
  freshAuroraState(62, "%", 121);
if (
  !Number.isFinite(batteryAuroraCard._batteryPresentation().storedKwh) ||
  !Number.isFinite(batteryAuroraCard._batteryPresentation().etaHours)
) {
  throw new Error("Aurora battery view hid available unchanged SOC");
}
batteryStates["sensor.hoymiles_hit_overview_battery_soc"] =
  freshAuroraState(62, "%");
batteryStates["sensor.hoymiles_hit_battery_capacity"] =
  freshAuroraState(22.7, "kWh", 121);
if (
  !Number.isFinite(batteryAuroraCard._batteryPresentation().storedKwh) ||
  !Number.isFinite(batteryAuroraCard._batteryPresentation().etaHours)
) {
  throw new Error("Aurora battery view hid available unchanged capacity");
}
batteryStates["sensor.hoymiles_hit_battery_capacity"] =
  freshAuroraState(22.7, "kWh");
batteryStates["sensor.hoymiles_hit_ems_mode_readback_code"] =
  freshAuroraState(4, undefined, 121);
if (!Number.isFinite(batteryAuroraCard._batteryPresentation().etaHours)) {
  throw new Error("Aurora battery view hid available unchanged EMS mode");
}
batteryStates["sensor.hoymiles_hit_ems_mode_readback_code"] =
  freshAuroraState(4);
batteryStates["sensor.hoymiles_hit_ems_force_charge_soc_readback"] =
  freshAuroraState(80, "%", 121);
if (!Number.isFinite(batteryAuroraCard._batteryPresentation().etaHours)) {
  throw new Error("Aurora battery view hid available unchanged 4303 target");
}
batteryStates["sensor.hoymiles_hit_overview_battery_power"] = {
  state: "unavailable",
  attributes: { unit_of_measurement: "W" },
};
if (
  batteryAuroraCard._batteryPresentation().powerKw !== null ||
  batteryAuroraCard._batteryPresentation().etaHours !== null
) {
  throw new Error("Aurora battery view accepted unavailable battery power");
}
batteryStates["sensor.hoymiles_hit_overview_battery_power"] =
  freshAuroraState(21, "W");
batteryStates["sensor.hoymiles_hit_overview_battery_soc"] =
  freshAuroraState(99, "%");
batteryStates["sensor.hoymiles_hit_battery_capacity"] =
  freshAuroraState(10000, "kWh");
batteryStates["sensor.hoymiles_hit_ems_mode_readback_code"] =
  freshAuroraState(5);
batteryStates["sensor.hoymiles_hit_ems_force_discharge_soc_readback"] =
  freshAuroraState(0, "%");
if (batteryAuroraCard._batteryPresentation().etaHours !== null) {
  throw new Error("Aurora battery view exposed an absurd multi-day ETA");
}
console.log("Aurora battery node: physical targets, availability and bounded ETA OK");

for (const language of ["pl", "en"]) {
  const dashboard = JSON.parse(
    fs.readFileSync(
      `custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_${language}.json`,
      "utf8",
    ),
  );
  const start = dashboard.views?.find((view) => view.path === "start");
  const energyCard = start?.cards?.find(
    (item) => item.type === "custom:hoymiles-aurora-energy-card",
  );
  if (!energyCard) {
    throw new Error(`The ${language} Start view does not contain Aurora`);
  }
  const supervisorCard = dashboard.views?.find(
    (view) => view.path === "ems-supervisor",
  )?.cards?.[0];
  for (const [key, entityId] of Object.entries({
    battery_current_entity: "sensor.hoymiles_hit_battery_current_bms",
    battery_capacity_entity: "sensor.hoymiles_hit_battery_capacity",
    ems_mode_readback_entity: "sensor.hoymiles_hit_ems_mode_readback_code",
    battery_self_use_floor_entity:
      "sensor.hoymiles_hit_ems_self_use_soc_readback",
    battery_charge_target_entity:
      "sensor.hoymiles_hit_ems_force_charge_soc_readback",
    battery_discharge_target_entity:
      "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
    battery_max_soc_entity: "number.hoymiles_hit_maximum_soc",
    battery_min_soc_entity: "number.hoymiles_hit_minimum_soc",
    forecast_tomorrow_entity: "sensor.hoymiles_solcast_forecast_tomorrow",
    cav_temperature_entity: "sensor.hoymiles_hit_cav_temp",
    battery_path_temperature_entity: "sensor.hoymiles_hit_bat_ths_temp",
  })) {
    if (energyCard[key] !== entityId) {
      throw new Error(
        `The ${language} Aurora battery ${key} mapping is ${energyCard[key]}, expected ${entityId}`,
      );
    }
  }
  for (const [key, entityId] of Object.entries(
    supervisorValidator.HOYMILES_SUPERVISOR_BINDINGS,
  )) {
    if (supervisorCard?.[key] !== entityId || key in energyCard) {
      throw new Error(
        `The ${language} Supervisor binding ${key} is not isolated to the dedicated card`,
      );
    }
  }
}
console.log("Aurora energy card: registration, units and dashboard payloads OK");

const card = new Card();
card.setConfig({
  type: "custom:hoymiles-rce-chart-card",
  entity: "sensor.hoymiles_rce_day",
  plan_entity: "sensor.hoymiles_hit_rce_optimized_plan",
  current_price_entity: "sensor.hoymiles_rce_current_price",
  active_entity: "input_boolean.hoymiles_rce_discharge_active",
  block_enabled_entity: "input_boolean.hoymiles_sale_block_enabled",
  block_start_entity: "input_datetime.hoymiles_sale_block_start",
  block_end_entity: "input_datetime.hoymiles_sale_block_end",
});

const timestamp = "2026-07-26T10:00:00+02:00";
const state = (value, attributes = {}) => ({
  state: String(value),
  attributes,
  last_updated: timestamp,
});
const rows = Array.from({ length: 96 }, (_, index) => ({
  business_date: "2026-07-26",
  period: `${String(Math.floor(index / 4)).padStart(2, "0")}:${String(
    (index % 4) * 15,
  ).padStart(2, "0")}`,
  rce_pln: 450 + index * 5,
}));

card.hass = {
  language: "pl",
  states: {
    "sensor.hoymiles_rce_day": state("2026-07-26", { value: rows }),
    "sensor.hoymiles_hit_rce_optimized_plan": state("Gotowa", {
      automatic_price_floor_pln_kwh: 0.9,
      planned_slots: [
        {
          date: "2026-07-26",
          start: "20:00",
          end: "20:30",
          price: 0.9,
          energy: 2.5,
          revenue: 2.25,
        },
        {
          date: "2026-07-27",
          start: "06:00",
          end: "06:30",
          price: 1.1,
          energy: 2.5,
          revenue: 2.75,
        },
      ],
    }),
    "sensor.hoymiles_rce_current_price": state("0.669"),
    "input_boolean.hoymiles_rce_discharge_active": state("off"),
    "input_boolean.hoymiles_sale_block_enabled": state("on"),
    "input_datetime.hoymiles_sale_block_start": state("22:00:00"),
    "input_datetime.hoymiles_sale_block_end": state("06:00:00"),
  },
};

const output = card.shadowRoot?.innerHTML || "";
for (const expected of [
  "<svg",
  "PLN/kWh",
  "96 okresów po 15 min",
  "48 bloków sterowania po 30 min",
  "Cena graniczna planu",
  "22:00–06:00",
  "1 × 30 min",
  "2,50 kWh",
  "2,25 PLN",
]) {
  if (!output.includes(expected)) {
    throw new Error(`Rendered RCE card is missing: ${expected}`);
  }
}

console.log("RCE custom card: registered and rendered successfully");

const PowerFlowCard = registry.get("hoymiles-power-flow-card");
if (!PowerFlowCard) {
  throw new Error("The Hoymiles power-flow custom element was not registered");
}
const powerFlowCard = new PowerFlowCard();
powerFlowCard._config = {
  battery: {
    energy: "sensor.hoymiles_hit_battery_capacity",
  },
};
const resolvedCapacity = powerFlowCard._resolveBatteryEnergy({
  states: {
    "sensor.hoymiles_hit_battery_capacity": state("230", {
      unit_of_measurement: "kWh",
    }),
  },
});
if (resolvedCapacity.value !== 230000) {
  throw new Error(
    `Battery capacity was not converted from kWh to Wh: ${resolvedCapacity.value}`,
  );
}
const unavailableCapacity = powerFlowCard._resolveBatteryEnergy({ states: {} });
if (unavailableCapacity.value !== 0) {
  throw new Error("Unavailable battery capacity must disable runtime estimates");
}
console.log("Power-flow battery capacity: entity converted to Wh successfully");

const denseHistory = Array.from({ length: 1000 }, (_value, index) => ({
  time: index * 1000,
  value: index === 337 ? 42 : index === 663 ? -17 : Math.sin(index / 30),
}));
const reducedHistory = historyCard._downsample(denseHistory, 120);
if (
  reducedHistory.length > 120 ||
  !reducedHistory.some((point) => point.value === 42) ||
  !reducedHistory.some((point) => point.value === -17)
) {
  throw new Error("Aurora history downsampling did not preserve extrema");
}
const accuratePath = historyCard._path(
  denseHistory.slice(0, 4),
  (value) => value / 1000,
  (value) => value,
);
if (accuratePath.includes("Q") || !accuratePath.includes("L")) {
  throw new Error("Aurora history path can visually overshoot measured data");
}
console.log("Aurora history: extrema-preserving reduction and accurate path OK");

if (typeof context.hoymilesDecorateCard !== "function") {
  throw new Error("Runtime Aurora dashboard decorator is not exposed");
}
const decoratedLeaf = context.hoymilesDecorateCard(
  {
    type: "markdown",
    content: "Status",
    view_layout: { position: "sidebar" },
    visibility: [{ condition: "state", entity: "binary_sensor.ready", state: "on" }],
    grid_options: { columns: 6 },
  },
  "pv",
);
if (
  decoratedLeaf.type !== "custom:hoymiles-aurora-frame-card" ||
  decoratedLeaf.accent !== "pv" ||
  decoratedLeaf.card?.type !== "markdown" ||
  decoratedLeaf.view_layout?.position !== "sidebar" ||
  decoratedLeaf.grid_options?.columns !== 6 ||
  decoratedLeaf.visibility?.[0]?.entity !== "binary_sensor.ready"
) {
  throw new Error("Runtime Aurora decorator did not wrap and hoist a native leaf card");
}
for (const hoisted of ["view_layout", "visibility", "grid_options"]) {
  if (hoisted in decoratedLeaf.card) {
    throw new Error(`Runtime Aurora decorator left ${hoisted} inside the child card`);
  }
}
if (
  JSON.stringify(context.hoymilesDecorateCard(decoratedLeaf, "load")) !==
  JSON.stringify(decoratedLeaf)
) {
  throw new Error("Runtime Aurora decorator is not idempotent");
}
const decoratedConditional = context.hoymilesDecorateCard(
  {
    type: "conditional",
    conditions: [{ entity: "input_boolean.details", state: "on" }],
    card: { type: "statistic", entity: "sensor.energy" },
  },
  "grid",
);
if (
  decoratedConditional.type !== "conditional" ||
  decoratedConditional.card?.type !== "custom:hoymiles-aurora-frame-card" ||
  decoratedConditional.card?.accent !== "grid"
) {
  throw new Error("Runtime Aurora decorator did not recurse through a conditional card");
}
const existingAurora = { type: "custom:hoymiles-aurora-energy-card", pv_entity: "sensor.pv" };
const decoratedGrid = context.hoymilesDecorateCard(
  {
    type: "grid",
    columns: 2,
    cards: [
      { type: "tile", entity: "switch.ems", tap_action: { action: "toggle" } },
      { type: "button", entity: "switch.notifications", tap_action: { action: "toggle" } },
      { type: "statistic", entity: "sensor.energy" },
      existingAurora,
    ],
  },
  "ems",
);
if (
  decoratedGrid.type !== "grid" ||
  decoratedGrid.cards?.[0]?.type !== "tile" ||
  decoratedGrid.cards?.[0]?.tap_action?.action !== "toggle" ||
  decoratedGrid.cards?.[1]?.type !== "button" ||
  decoratedGrid.cards?.[1]?.tap_action?.action !== "toggle" ||
  decoratedGrid.cards?.[2]?.type !== "custom:hoymiles-aurora-frame-card" ||
  decoratedGrid.cards?.[2]?.accent !== "ems" ||
  decoratedGrid.cards?.[3]?.type !== "custom:hoymiles-aurora-energy-card"
) {
  throw new Error("Runtime Aurora decorator changed interactive controls or rewrapped an Aurora card");
}
for (const [label, expectedAccent] of [
  ["PV stringi", "pv"],
  ["Odbiór LOAD", "load"],
  ["Bateria", "battery"],
  ["Sieć i RCE", "grid"],
  ["Sterowanie EMS", "ems"],
  ["RCEm 253 V", "warning"],
]) {
  if (context.hoymilesAuroraTextAccent(label) !== expectedAccent) {
    throw new Error(`Aurora accent inference failed for ${label}`);
  }
}
console.log("Runtime Aurora decorator: recursion, hoisting and idempotence OK");

const Strategy = canonicalStrategy;
if (!Strategy) {
  throw new Error("The Hoymiles dashboard strategy was not registered");
}
if (
  !context.window.customStrategies?.some(
    (strategy) =>
      strategy.type === "hoymiles-hit-xxl-g3" &&
      strategy.strategyType === "dashboard",
  )
) {
  throw new Error("The dashboard strategy is absent from the community picker");
}

Promise.all([
  Strategy.generate({}, { locale: { language: "pl-PL" } }),
  Strategy.generate({}, { locale: { language: "en-GB" } }),
  bootstrapFirstStrategy.generate({}, { locale: { language: "pl-PL" } }),
  auroraFrameMountPromise,
  supervisorLifecycleValidationPromise,
])
  .then(([polishDashboard, englishDashboard, bootstrapFirstDashboard]) => {
    if (
      polishDashboard.views?.length < 10 ||
      englishDashboard.views?.length < 10
    ) {
      throw new Error("Dashboard strategy returned an incomplete dashboard");
    }
    const polishGrid = polishDashboard.views?.find(
      (view) => view.path === "siec",
    );
    const englishGrid = englishDashboard.views?.find(
      (view) => view.path === "siec",
    );
    if (polishGrid?.title !== "Sieć" || englishGrid?.title !== "Grid") {
      throw new Error("Dashboard strategy did not select the HA language");
    }
    const collectCards = (cards, found = []) => {
      for (const card of cards || []) {
        found.push(card);
        if (card?.type === "conditional" && card.card) {
          collectCards([card.card], found);
        } else if (Array.isArray(card?.cards)) {
          collectCards(card.cards, found);
        }
      }
      return found;
    };
    for (const dashboard of [
      polishDashboard,
      englishDashboard,
      bootstrapFirstDashboard,
    ]) {
      const allCards = dashboard.views.flatMap((view) => collectCards(view.cards));
      const frames = allCards.filter(
        (card) => card?.type === "custom:hoymiles-aurora-frame-card",
      );
      if (frames.length !== 42) {
        throw new Error(
          `Dashboard strategy produced ${frames.length} Aurora frames, expected 42`,
        );
      }
      if (
        frames.some(
          (card) => card.card?.type === "custom:hoymiles-aurora-frame-card",
        )
      ) {
        throw new Error("Dashboard strategy produced nested Aurora frames");
      }
      if (
        !allCards.some(
          (card) => card?.type === "custom:hoymiles-aurora-energy-card",
        )
      ) {
        throw new Error("Dashboard strategy lost the existing Aurora energy card");
      }
    }
    console.log(
      "Dashboard strategy: PL/EN and bootstrap-first paths render 42 Aurora frames",
    );
  })
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
