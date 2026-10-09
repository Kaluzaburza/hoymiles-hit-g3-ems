(function registerHoymilesDashboardStrategy() {
  "use strict";

  const frontendRevision = 123;
  const frontendVersion = `1.5.8.1.${frontendRevision}`;
  const canonicalQuery = `?v=${frontendVersion}&history=48h-executed`;
  const isStrategyScript = (script) => {
    try {
      return new URL(script?.src, window.location.origin).pathname.endsWith(
        "/hoymiles-dashboard-strategy.js"
      );
    } catch (_error) {
      return false;
    }
  };
  const scriptRevision = (script) => {
    try {
      const version = new URL(script.src, window.location.origin).searchParams.get("v");
      const revision = Number(version?.split(".").at(-1));
      return Number.isSafeInteger(revision) ? revision : -1;
    } catch (_error) {
      return -1;
    }
  };
  const currentStrategyScript = isStrategyScript(document.currentScript)
    ? document.currentScript
    : null;
  const matchingScripts = Array.from(document.scripts || [])
    .filter(isStrategyScript)
    .sort((left, right) => scriptRevision(right) - scriptRevision(left));
  const scriptUrl = currentStrategyScript?.src || matchingScripts[0]?.src;
  const canonicalModuleUrl = scriptUrl
    ? new URL("hoymiles-rce-chart-card.js", new URL(".", scriptUrl))
    : new URL(
        "/local/hoymiles-rce-chart-card.js",
        window.location.origin
      );
  canonicalModuleUrl.search = canonicalQuery;
  let canonicalModulePromise;

  const loadCanonicalModule = () => {
    canonicalModulePromise ??= import(canonicalModuleUrl.href);
    return canonicalModulePromise;
  };

  const elementName = "ll-strategy-dashboard-hoymiles-hit-xxl-g3";

  class HoymilesHitDashboardBootstrapStrategy extends HTMLElement {
    static noEditor = true;
    static hoymilesFrontendRevision = frontendRevision;

    static getCreateSuggestions(hass) {
      const language = (
        hass?.locale?.language ??
        hass?.language ??
        "en"
      ).toLowerCase();
      return {
        title: language.startsWith("pl")
          ? "Hoymiles — falownik"
          : "Hoymiles — inverter",
        icon: "mdi:solar-power-variant",
      };
    }

    static async generate(config, hass) {
      const bootstrapGenerate = this.generate;
      const registeredStrategy = customElements.get(elementName);
      if (
        Number(registeredStrategy?.hoymilesFrontendRevision) >= frontendRevision &&
        registeredStrategy?.generate &&
        registeredStrategy.generate !== bootstrapGenerate
      ) {
        return registeredStrategy.generate(config, hass);
      }
      await loadCanonicalModule();
      const canonicalStrategy = customElements.get(elementName);
      if (
        canonicalStrategy?.generate &&
        canonicalStrategy.generate !== bootstrapGenerate
      ) {
        return canonicalStrategy.generate(config, hass);
      }
      throw new Error(
        "Canonical Hoymiles frontend module did not upgrade the dashboard strategy"
      );
    }
  }

  const existingStrategy = customElements.get(elementName);
  if (!existingStrategy) {
    customElements.define(
      elementName,
      HoymilesHitDashboardBootstrapStrategy
    );
  } else if (
    Number(existingStrategy.hoymilesFrontendRevision ?? -1) < frontendRevision
  ) {
    existingStrategy.noEditor = HoymilesHitDashboardBootstrapStrategy.noEditor;
    existingStrategy.hoymilesFrontendRevision = frontendRevision;
    // Replacing a previously canonical generate() with this bootstrap means
    // the new canonical module still has to upgrade the same constructor.
    existingStrategy.hoymilesCanonicalModule = false;
    existingStrategy.getCreateSuggestions =
      HoymilesHitDashboardBootstrapStrategy.getCreateSuggestions;
    existingStrategy.generate = HoymilesHitDashboardBootstrapStrategy.generate;
  }

  window.customStrategies = window.customStrategies || [];
  if (
    !window.customStrategies.some(
      (strategy) =>
        strategy.type === "hoymiles-hit-xxl-g3" &&
        strategy.strategyType === "dashboard"
    )
  ) {
    window.customStrategies.push({
      type: "hoymiles-hit-xxl-g3",
      strategyType: "dashboard",
      name: "EMS for Hoymiles HIT-(5–20)L-G3",
      description:
        "Unofficial local EMS for Hoymiles HIT-G3 hybrid inverters — Home Assistant, ESPHome, Modbus, RCE, tariff optimization and RCEm.",
      documentationURL:
        "https://github.com/Kaluzaburza/hoymiles-hit-g3-ems",
    });
  }
})();
