(function registerHoymilesDashboardStrategy() {
  "use strict";

  const scriptUrl =
    document.currentScript?.src ||
    Array.from(document.scripts || [])
      .reverse()
      .find((script) => {
        try {
          return new URL(script.src, window.location.origin).pathname.endsWith(
            "/hoymiles-dashboard-strategy.js"
          );
        } catch (_error) {
          return false;
        }
      })?.src;
  const canonicalModuleUrl = scriptUrl
    ? new URL("hoymiles-rce-chart-card.js", new URL(".", scriptUrl))
    : new URL(
        "/local/hoymiles-rce-chart-card.js?v=1.5.7.28",
        window.location.origin
      );
  if (scriptUrl) {
    canonicalModuleUrl.search = new URL(scriptUrl).search;
  }
  let canonicalModulePromise;

  const loadCanonicalModule = () => {
    canonicalModulePromise ??= import(canonicalModuleUrl.href);
    return canonicalModulePromise;
  };

  const elementName = "ll-strategy-dashboard-hoymiles-hit-xxl-g3";

  class HoymilesHitDashboardBootstrapStrategy extends HTMLElement {
    static noEditor = true;

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

  if (!customElements.get(elementName)) {
    customElements.define(
      elementName,
      HoymilesHitDashboardBootstrapStrategy
    );
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
