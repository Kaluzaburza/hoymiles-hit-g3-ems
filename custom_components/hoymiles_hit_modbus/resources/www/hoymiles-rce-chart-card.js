class HoymilesHitDashboardStrategy extends HTMLElement {
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
    const requestedLanguage = (
      config?.language ??
      hass?.locale?.language ??
      hass?.language ??
      "en"
    ).toLowerCase();
    const language = requestedLanguage.startsWith("pl") ? "pl" : "en";
    const dashboardUrl = new URL(
      `dashboard_hoymiles_${language}.json`,
      import.meta.url
    );
    dashboardUrl.search = "";
    const response = await fetch(dashboardUrl, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(
        `Cannot load Hoymiles dashboard (${response.status} ${response.statusText})`
      );
    }
    const dashboard = hoymilesDecorateDashboard(await response.json());
    if (config?.title) {
      dashboard.title = config.title;
    }
    return dashboard;
  }
}

// Register the dashboard strategy before the larger custom-card bundle. A
// frontend card error must never prevent Lovelace from discovering it. If an
// older classic bootstrap claimed the immutable custom-element name first,
// upgrade that constructor in place so its generate() path still applies the
// complete Aurora decorator once this canonical module arrives.
const HOYMILES_DASHBOARD_STRATEGY_ELEMENT =
  "ll-strategy-dashboard-hoymiles-hit-xxl-g3";
const hoymilesExistingDashboardStrategy = customElements.get(
  HOYMILES_DASHBOARD_STRATEGY_ELEMENT
);
if (hoymilesExistingDashboardStrategy) {
  hoymilesExistingDashboardStrategy.noEditor =
    HoymilesHitDashboardStrategy.noEditor;
  hoymilesExistingDashboardStrategy.getCreateSuggestions =
    HoymilesHitDashboardStrategy.getCreateSuggestions;
  hoymilesExistingDashboardStrategy.generate =
    HoymilesHitDashboardStrategy.generate;
} else {
  customElements.define(
    HOYMILES_DASHBOARD_STRATEGY_ELEMENT,
    HoymilesHitDashboardStrategy
  );
}

const HOYMILES_AURORA_ACCENTS = Object.freeze({
  neutral: "#66d9ff",
  cyan: "#66d9ff",
  pv: "#2de083",
  load: "#ff5d73",
  grid: "#ffc857",
  battery: "#3ea6ff",
  ems: "#a78bfa",
  warning: "#ff8a4c",
});

const HOYMILES_AURORA_THEME_CSS = `
  :host {
    --hoymiles-aurora-surface:
      radial-gradient(circle at 92% -10%, color-mix(in srgb, var(--hoymiles-aurora-accent, #66d9ff) 10%, transparent), transparent 38%),
      linear-gradient(145deg,
        color-mix(in srgb, var(--card-background-color, var(--ha-card-background)) 96%, var(--primary-text-color) 4%),
        var(--card-background-color, var(--ha-card-background)) 70%);
    --hoymiles-aurora-border: color-mix(in srgb, var(--hoymiles-aurora-accent, #66d9ff) 22%, var(--divider-color));
    --hoymiles-aurora-shadow: 0 14px 34px color-mix(in srgb, #000 18%, transparent);
    --hoymiles-aurora-text: var(--primary-text-color);
    --hoymiles-aurora-muted: var(--secondary-text-color);
    --hoymiles-aurora-pv: #2de083;
    --hoymiles-aurora-load: #ff5d73;
    --hoymiles-aurora-grid: #ffc857;
    --hoymiles-aurora-battery: #3ea6ff;
    --hoymiles-aurora-good: #2de083;
    --hoymiles-aurora-warn: #ffc857;
    --hoymiles-aurora-error: #ff5d73;
    --hoymiles-aurora-offline: #8da0b8;
    --ha-card-background: var(--hoymiles-aurora-surface);
    --ha-card-border-color: var(--hoymiles-aurora-border);
    --ha-card-border-radius: 20px;
    --ha-card-box-shadow: var(--hoymiles-aurora-shadow);
  }
`;

function hoymilesAuroraAccent(value) {
  return HOYMILES_AURORA_ACCENTS[value] || HOYMILES_AURORA_ACCENTS.neutral;
}

function hoymilesNormalizeLanguage(value) {
  if (typeof value !== "string") return "en";
  const normalized = value.trim().toLowerCase();
  return normalized === "pl" || normalized.startsWith("pl-") ? "pl" : "en";
}

function hoymilesLanguage(hass, configuredLanguage) {
  const value =
    configuredLanguage !== undefined
      ? configuredLanguage
      : hass?.locale?.language ?? hass?.language;
  return hoymilesNormalizeLanguage(value);
}

function hoymilesEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function hoymilesDispatchMoreInfo(element, entityId) {
  if (!entityId) return;
  element.dispatchEvent(
    new CustomEvent("hass-more-info", {
      bubbles: true,
      composed: true,
      detail: { entityId },
    })
  );
}

function hoymilesAuroraTextAccent(input) {
  const value = String(input || "").toLowerCase();
  if (/rcem|253\s*v/.test(value)) return "warning";
  if (/load|eps|odbior|odbiór/.test(value)) return "load";
  if (/bateri|battery|magazyn/.test(value)) return "battery";
  if (/(^|[\s-])pv([\s-]|$)|produkcj|solar|(^|[\s-])gen([\s-]|$)|generator/.test(value)) return "pv";
  if (/rce|sieć|siec|grid|zysk|profit|revenue/.test(value)) return "grid";
  if (/ems|taryf|tariff|sterowan|control/.test(value)) return "ems";
  return "neutral";
}

function hoymilesAuroraViewAccent(view) {
  return hoymilesAuroraTextAccent(`${view?.path || ""} ${view?.title || ""}`);
}

function hoymilesDecorateCard(card, accent) {
  if (!card || typeof card !== "object" || Array.isArray(card)) return card;
  const type = String(card.type || "");
  if (type === "custom:hoymiles-aurora-frame-card") return card;

  if (type === "conditional" && card.card) {
    return { ...card, card: hoymilesDecorateCard(card.card, accent) };
  }
  if (
    [
      "vertical-stack",
      "horizontal-stack",
      "grid",
      "custom:hoymiles-responsive-stack-card",
    ].includes(type) &&
    Array.isArray(card.cards)
  ) {
    return {
      ...card,
      cards: card.cards.map((child) => hoymilesDecorateCard(child, accent)),
    };
  }

  const skipped =
    type.startsWith("custom:hoymiles-aurora-") ||
    [
      "custom:hoymiles-zebra-entities-card",
      "custom:hoymiles-responsive-glance-card",
      "custom:hoymiles-rce-chart-card",
    ].includes(type);
  if (skipped || !["history-graph", "statistics-graph", "markdown", "glance", "statistic"].includes(type)) {
    return card;
  }

  const {
    view_layout: viewLayout,
    visibility,
    grid_options: gridOptions,
    ...nestedCard
  } = card;
  return {
    type: "custom:hoymiles-aurora-frame-card",
    accent,
    ...(viewLayout ? { view_layout: viewLayout } : {}),
    ...(visibility ? { visibility } : {}),
    ...(gridOptions ? { grid_options: gridOptions } : {}),
    card: nestedCard,
  };
}

function hoymilesDecorateDashboard(dashboard) {
  if (!dashboard || !Array.isArray(dashboard.views)) return dashboard;
  return {
    ...dashboard,
    views: dashboard.views.map((view) => {
      const accent = hoymilesAuroraViewAccent(view);
      return {
        ...view,
        cards: Array.isArray(view.cards)
          ? view.cards.map((card) => hoymilesDecorateCard(card, accent))
          : view.cards,
      };
    }),
  };
}

class HoymilesRceChartCard extends HTMLElement {
  constructor() {
    super();
    this._renderKey = "";
    this._states = {};
    this._language = document.documentElement.lang || "en";
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("entity is required");
    }
    this._config = config;
    this._renderKey = "";
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._states = hass.states || {};
    this._language = hass.language || this._language;
    this._update();
  }

  _update() {
    if (!this._config) return;
    const ids = [
      this._config.entity,
      this._config.plan_entity,
      this._config.current_price_entity,
      this._config.active_entity,
      this._config.block_enabled_entity,
      this._config.block_start_entity,
      this._config.block_end_entity,
    ].filter(Boolean);
    const key = `${this._language}|${ids
      .map((id) => {
        const entity = this._states[id];
        return entity ? `${entity.state}:${entity.last_updated}` : "missing";
      })
      .join("|")}`;

    if (key !== this._renderKey) {
      this._renderKey = key;
      this._render();
    }
  }

  getCardSize() {
    return 7;
  }

  getGridOptions() {
    return {
      columns: 12,
      rows: 7,
      min_columns: 6,
      min_rows: 5,
    };
  }

  static getStubConfig() {
    return {
      entity: "sensor.hoymiles_rce_day",
      plan_entity: "sensor.hoymiles_hit_rce_optimized_plan",
      current_price_entity: "sensor.hoymiles_rce_current_price",
      active_entity: "input_boolean.hoymiles_rce_discharge_active",
      block_enabled_entity: "input_boolean.hoymiles_sale_block_enabled",
      block_start_entity: "input_datetime.hoymiles_sale_block_start",
      block_end_entity: "input_datetime.hoymiles_sale_block_end",
    };
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _number(value, digits = 3) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat(this._language || "en", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(number);
  }

  _strings() {
    const polish = String(this._language || "").toLowerCase().startsWith("pl");
    return polish
      ? {
          defaultTitle: "RCE — ceny na dziś",
          noData: "Brak kompletnych danych PSE",
          noDataHint: "Automatyka nie uruchomi rozładowania bez aktualnego planu.",
          futureNoData: "Dane PSE na jutro nie są jeszcze opublikowane",
          futureNoDataHint:
            "Automatyka realizuje plan dzisiejszy i przeliczy go automatycznie po publikacji danych.",
          current: "Bieżąca",
          threshold: "Cena graniczna planu",
          exportLockout: "Blokada sprzedaży",
          disabled: "wyłączona",
          periods15: "okresów po 15 min",
          blocks30: "bloków sterowania po 30 min",
          average30: "Średnia bloku 30 min",
          lockoutHint: "Blokada sprzedaży",
          planned: "Zaplanowane rozładowanie",
          selfUse: "Autokonsumpcja",
          thresholdShort: "plan od",
          thresholdOutside: "Cena graniczna planu jest poza zakresem dzisiejszych cen",
          chartLabel: "Wykres cen RCE dla",
          belowThreshold: "Poza planem sprzedaży",
          plannedDischarge: "Planowane rozładowanie",
          currentQuarter: "Aktualny kwadrans",
          userThreshold: "Automatyczna cena graniczna",
          minimum: "Min.",
          maximum: "Maks.",
          plan: "Plan:",
          expectedExport: "Prognozowany eksport:",
          expectedRevenue: "Szacunkowy przychód:",
          active: "● RCE rozładowuje",
          inactive: "○ RCE nieaktywne",
        }
      : {
          defaultTitle: "RCE — today's prices",
          noData: "No complete PSE data",
          noDataHint: "The automation will not discharge without a current plan.",
          futureNoData: "Tomorrow's PSE data has not been published yet",
          futureNoDataHint:
            "The automation is following today's plan and will recalculate automatically after publication.",
          current: "Current",
          threshold: "Automatic plan floor",
          exportLockout: "Export lockout",
          disabled: "disabled",
          periods15: "15-minute periods",
          blocks30: "30-minute control blocks",
          average30: "30-minute block average",
          lockoutHint: "Export lockout",
          planned: "Planned discharge",
          selfUse: "Self-Use",
          thresholdShort: "plan from",
          thresholdOutside: "The automatic plan floor is outside today's price range",
          chartLabel: "RCE price chart for",
          belowThreshold: "Outside export plan",
          plannedDischarge: "Planned discharge",
          currentQuarter: "Current quarter-hour",
          userThreshold: "Automatic plan floor",
          minimum: "Min.",
          maximum: "Max.",
          plan: "Plan:",
          expectedExport: "Forecast export:",
          expectedRevenue: "Estimated revenue:",
          active: "● RCE discharging",
          inactive: "○ RCE inactive",
        };
  }

  _t(key) {
    return this._strings()[key] || key;
  }

  _timeMinutes(value, fallback) {
    const match = String(value ?? "").match(/^(\d{1,2}):(\d{2})/);
    if (!match) return fallback;
    const hour = Number(match[1]);
    const minute = Number(match[2]);
    if (!Number.isFinite(hour) || !Number.isFinite(minute)) return fallback;
    return hour * 60 + minute;
  }

  _render() {
    const source = this._states[this._config.entity];
    const planState = this._config.plan_entity
      ? this._states[this._config.plan_entity]
      : undefined;
    const currentState = this._config.current_price_entity
      ? this._states[this._config.current_price_entity]
      : undefined;
    const activeState = this._config.active_entity
      ? this._states[this._config.active_entity]
      : undefined;
    const blockEnabledState = this._config.block_enabled_entity
      ? this._states[this._config.block_enabled_entity]
      : undefined;
    const blockStartState = this._config.block_start_entity
      ? this._states[this._config.block_start_entity]
      : undefined;
    const blockEndState = this._config.block_end_entity
      ? this._states[this._config.block_end_entity]
      : undefined;

    const rows = Array.isArray(source?.attributes?.value)
      ? source.attributes.value
      : [];
    const points = rows
      .map((row, index) => ({
        index,
        period: String(row.period ?? ""),
        date: String(row.business_date ?? ""),
        price: Number(row.rce_pln) / 1000,
      }))
      .filter((point) => Number.isFinite(point.price));

    const title = this._escape(this._config.title || this._t("defaultTitle"));
    if (points.length < 2) {
      const futureData =
        this._config.future_data ||
        String(this._config.entity).endsWith("_tomorrow");
      const noData = futureData
        ? this._t("futureNoData")
        : this._t("noData");
      const noDataHint = futureData
        ? this._t("futureNoDataHint")
        : this._t("noDataHint");
      this._setContent(`
        <ha-card>
          <div class="header">${title}</div>
          <div class="empty">
            <ha-icon icon="mdi:cloud-alert"></ha-icon>
            <div>
              <strong>${noData}</strong>
              <span>${noDataHint}</span>
            </div>
          </div>
        </ha-card>
      `);
      return;
    }

    const currentPrice = Number(currentState?.state);
    const automationActive = activeState?.state === "on";
    const blockEnabled = blockEnabledState?.state === "on";
    const blockStart = this._timeMinutes(blockStartState?.state, 22 * 60);
    const blockEnd = this._timeMinutes(blockEndState?.state, 6 * 60);
    const isBlocked = (minute) =>
      blockEnabled &&
      blockStart !== blockEnd &&
      (blockStart < blockEnd
        ? minute >= blockStart && minute < blockEnd
        : minute >= blockStart || minute < blockEnd);
    const blockWindow = `${String(Math.floor(blockStart / 60)).padStart(2, "0")}:${String(
      blockStart % 60,
    ).padStart(2, "0")}–${String(Math.floor(blockEnd / 60)).padStart(2, "0")}:${String(
      blockEnd % 60,
    ).padStart(2, "0")}`;
    const date = points[0].date;
    const prices = points.map((point) => point.price);
    const minimum = Math.min(...prices);
    const maximum = Math.max(...prices);

    const halfHours = [];
    for (let index = 0; index + 1 < points.length; index += 2) {
      halfHours.push((points[index].price + points[index + 1].price) / 2);
    }
    const optimizedSlots = Array.isArray(planState?.attributes?.planned_slots)
      ? planState.attributes.planned_slots
      : [];
    const optimizedForDay = optimizedSlots.filter(
      (slot) => String(slot?.date ?? "") === date,
    );
    const optimizedPrices = optimizedForDay
      .map((slot) => Number(slot?.price))
      .filter(Number.isFinite);
    const threshold = optimizedPrices.length
      ? Math.min(...optimizedPrices)
      : Number.NaN;
    const optimizedStarts = new Set(
      optimizedForDay.map((slot) => String(slot?.start ?? "").slice(0, 5)),
    );
    const hasOptimizedPlan = Boolean(this._config.plan_entity && planState);
    const plannedBlocks = hasOptimizedPlan
      ? optimizedStarts.size
      : Number.isFinite(threshold)
        ? halfHours.filter(
            (price, index) => price > threshold && !isBlocked(index * 30),
          ).length
        : 0;
    const expectedExport = optimizedForDay.reduce(
      (total, slot) => total + (Number(slot?.energy) || 0),
      0,
    );
    const expectedRevenue = optimizedForDay.reduce(
      (total, slot) => total + (Number(slot?.revenue) || 0),
      0,
    );

    const now = new Date();
    const localDate = [
      now.getFullYear(),
      String(now.getMonth() + 1).padStart(2, "0"),
      String(now.getDate()).padStart(2, "0"),
    ].join("-");
    const currentQuarter =
      date === localDate ? now.getHours() * 4 + Math.floor(now.getMinutes() / 15) : -1;

    const width = 1000;
    const height = 430;
    const left = 70;
    const right = 24;
    const top = 22;
    const bottom = 65;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;

    let domainMin = Math.min(0, minimum);
    let domainMax = Math.max(0, maximum);
    const rawSpan = Math.max(0.05, domainMax - domainMin);
    domainMin -= rawSpan * 0.08;
    domainMax += rawSpan * 0.08;
    const domainSpan = domainMax - domainMin;
    const y = (value) => top + ((domainMax - value) / domainSpan) * plotHeight;
    const zeroY = y(0);

    const horizontalGrid = [];
    const yTickCount = 6;
    for (let index = 0; index <= yTickCount; index += 1) {
      const value = domainMax - (domainSpan * index) / yTickCount;
      const position = y(value);
      horizontalGrid.push(`
        <line class="grid-line" x1="${left}" y1="${position}" x2="${width - right}" y2="${position}" />
        <text class="axis-label y-label" x="${left - 10}" y="${position + 4}">${this._number(value, 2)}</text>
      `);
    }

    const verticalGrid = [];
    for (let hour = 0; hour <= 24; hour += 3) {
      const position = left + (plotWidth * hour) / 24;
      const anchor = hour === 0 ? "start" : hour === 24 ? "end" : "middle";
      verticalGrid.push(`
        <line class="grid-line vertical" x1="${position}" y1="${top}" x2="${position}" y2="${top + plotHeight}" />
        <text class="axis-label x-label" text-anchor="${anchor}" x="${position}" y="${top + plotHeight + 28}">
          ${String(hour).padStart(2, "0")}:00
        </text>
      `);
    }

    const slotWidth = plotWidth / points.length;
    const bars = points.map((point, index) => {
      const pair = Math.floor(index / 2);
      const blocked = isBlocked(pair * 30);
      const pairStart = String(points[pair * 2]?.period ?? "")
        .split(" - ")[0]
        .slice(0, 5);
      const planned = hasOptimizedPlan
        ? optimizedStarts.has(pairStart)
        : !blocked && Number.isFinite(threshold) && halfHours[pair] > threshold;
      const barX = left + index * slotWidth + slotWidth * 0.09;
      const valueY = y(point.price);
      const barY = Math.min(valueY, zeroY);
      const barHeight = Math.max(1.5, Math.abs(zeroY - valueY));
      const current = index === currentQuarter;
      const classes = [
        "bar",
        blocked ? "blocked" : planned ? "planned" : "normal",
        point.price < 0 ? "negative" : "",
        current ? "current" : "",
      ]
        .filter(Boolean)
        .join(" ");
      const pairAverage = halfHours[pair];
      return `
        <rect class="${classes}" x="${barX}" y="${barY}"
              width="${Math.max(1.5, slotWidth * 0.82)}" height="${barHeight}" rx="1">
          <title>${this._escape(point.period)}: ${this._number(point.price, 4)} PLN/kWh
${this._t("average30")}: ${this._number(pairAverage, 4)} PLN/kWh
${blocked ? `${this._t("lockoutHint")} ${blockWindow} — Self-Use` : planned ? this._t("planned") : this._t("selfUse")}</title>
        </rect>
      `;
    });

    const thresholdVisible =
      Number.isFinite(threshold) && threshold >= domainMin && threshold <= domainMax;
    const thresholdLine = thresholdVisible
      ? `
        <line class="threshold-line" x1="${left}" y1="${y(threshold)}"
              x2="${width - right}" y2="${y(threshold)}" />
        <rect class="threshold-label-bg" x="${width - right - 116}" y="${y(threshold) - 20}"
              width="112" height="18" rx="5" />
        <text class="threshold-label" text-anchor="end" x="${width - right - 8}"
              y="${y(threshold) - 7}">
          ${this._t("thresholdShort")} ${this._number(threshold, 2)}
        </text>
      `
      : "";

    const thresholdHint = !thresholdVisible && Number.isFinite(threshold)
      ? `<span class="range-note">${this._t("thresholdOutside")}</span>`
      : "";

    this._setContent(`
      <ha-card>
        <div class="top">
          <div>
            <div class="header">${title}</div>
            <div class="subheader">${this._escape(date)} • ${points.length} ${this._t("periods15")} • ${halfHours.length} ${this._t("blocks30")}</div>
          </div>
          <div class="badges">
            ${
              date === localDate
                ? `<div class="badge">
              <span>${this._t("current")}</span>
              <strong>${this._number(currentPrice, 4)} PLN/kWh</strong>
            </div>`
                : ""
            }
            <div class="badge threshold">
              <span>${this._t("threshold")}</span>
              <strong>${this._number(threshold, 2)} PLN/kWh</strong>
            </div>
            <div class="badge block ${blockEnabled ? "enabled" : ""}">
              <span>${this._t("exportLockout")}</span>
              <strong>${blockEnabled ? blockWindow : this._t("disabled")}</strong>
            </div>
          </div>
        </div>

        <div class="chart-wrap">
          <svg viewBox="0 0 ${width} ${height}" role="img"
               aria-label="${this._t("chartLabel")} ${this._escape(date)}">
            <text class="axis-title" transform="rotate(-90)" text-anchor="middle"
                  x="${-(top + plotHeight / 2)}" y="18">PLN/kWh</text>
            ${horizontalGrid.join("")}
            ${verticalGrid.join("")}
            <line class="zero-line" x1="${left}" y1="${zeroY}"
                  x2="${width - right}" y2="${zeroY}" />
            ${bars.join("")}
            ${thresholdLine}
          </svg>
        </div>

        <div class="legend">
          <span><i class="swatch normal"></i>${this._t("belowThreshold")}</span>
          <span><i class="swatch planned"></i>${this._t("plannedDischarge")}</span>
          <span><i class="swatch blocked"></i>${this._t("exportLockout")}</span>
          <span><i class="swatch current"></i>${this._t("currentQuarter")}</span>
          <span><i class="line"></i>${this._t("userThreshold")}</span>
        </div>

        <div class="footer">
          <span>${this._t("minimum")} <strong>${this._number(minimum, 3)}</strong></span>
          <span>${this._t("maximum")} <strong>${this._number(maximum, 3)}</strong></span>
          <span>${this._t("plan")} <strong>${plannedBlocks} × 30 min</strong></span>
          <span>${this._t("expectedExport")} <strong>${this._number(expectedExport, 2)} kWh</strong></span>
          <span>${this._t("expectedRevenue")} <strong>${this._number(expectedRevenue, 2)} PLN</strong></span>
          <span class="${automationActive ? "active" : "inactive"}">
            ${automationActive ? this._t("active") : this._t("inactive")}
          </span>
          ${thresholdHint}
        </div>
      </ha-card>
    `);
  }

  _setContent(content) {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        ${HOYMILES_AURORA_THEME_CSS}
        :host { --hoymiles-aurora-accent: ${hoymilesAuroraAccent("grid")}; }
        ha-card {
          background: var(--hoymiles-aurora-surface);
          border: 1px solid var(--hoymiles-aurora-border);
          border-radius: var(--ha-card-border-radius);
          box-shadow: var(--hoymiles-aurora-shadow);
          overflow: hidden;
          padding: 18px 18px 14px;
        }
        .top {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 16px;
          margin-bottom: 10px;
        }
        .header {
          color: var(--primary-text-color);
          font-size: 22px;
          font-weight: 500;
          line-height: 1.25;
        }
        .subheader {
          color: var(--secondary-text-color);
          font-size: 12px;
          margin-top: 4px;
        }
        .badges {
          display: flex;
          flex-wrap: wrap;
          justify-content: flex-end;
          gap: 8px;
        }
        .badge {
          background: color-mix(in srgb, var(--hoymiles-aurora-accent) 15%, var(--card-background-color));
          border: 1px solid color-mix(in srgb, var(--hoymiles-aurora-accent) 42%, transparent);
          border-radius: 9px;
          min-width: 112px;
          padding: 6px 9px;
        }
        .badge.threshold {
          background: color-mix(in srgb, var(--hoymiles-aurora-warn) 14%, var(--card-background-color));
          border-color: color-mix(in srgb, var(--hoymiles-aurora-warn) 42%, transparent);
        }
        .badge.block.enabled {
          background: color-mix(in srgb, #8d8d8d 18%, var(--card-background-color));
          border-color: color-mix(in srgb, #8d8d8d 48%, transparent);
        }
        .badge span {
          color: var(--secondary-text-color);
          display: block;
          font-size: 10px;
          text-transform: uppercase;
        }
        .badge strong {
          color: var(--primary-text-color);
          display: block;
          font-size: 13px;
          margin-top: 2px;
          white-space: nowrap;
        }
        .chart-wrap {
          overflow-x: auto;
          width: 100%;
        }
        svg {
          display: block;
          min-width: 620px;
          width: 100%;
        }
        .grid-line {
          stroke: var(--divider-color);
          stroke-width: 1;
          opacity: 0.62;
        }
        .grid-line.vertical {
          opacity: 0.4;
        }
        .zero-line {
          stroke: var(--secondary-text-color);
          stroke-width: 1.4;
          opacity: 0.8;
        }
        .axis-label,
        .axis-title {
          fill: var(--secondary-text-color);
          font-family: Roboto, sans-serif;
          font-size: 13px;
        }
        .axis-title {
          font-size: 14px;
          font-weight: 500;
        }
        .bar {
          transition: opacity 120ms ease;
        }
        .bar:hover {
          opacity: 0.72;
        }
        .bar.normal {
          fill: var(--hoymiles-aurora-accent);
        }
        .bar.planned {
          fill: var(--hoymiles-aurora-good);
        }
        .bar.negative {
          fill: var(--hoymiles-aurora-error);
        }
        .bar.blocked {
          fill: #777b82;
          opacity: 0.78;
        }
        .bar.current {
          stroke: #ffd54f;
          stroke-width: 3;
          paint-order: stroke;
        }
        .threshold-line {
          stroke: var(--hoymiles-aurora-warn);
          stroke-dasharray: 8 5;
          stroke-width: 2.5;
        }
        .threshold-label-bg {
          fill: color-mix(in srgb, #ff9800 85%, #000);
        }
        .threshold-label {
          fill: #fff;
          font-family: Roboto, sans-serif;
          font-size: 12px;
          font-weight: 600;
        }
        .legend,
        .footer {
          color: var(--secondary-text-color);
          display: flex;
          flex-wrap: wrap;
          gap: 8px 18px;
          font-size: 12px;
          margin-top: 8px;
        }
        .legend span,
        .footer span {
          align-items: center;
          display: inline-flex;
          gap: 6px;
        }
        .footer {
          border-top: 1px solid var(--divider-color);
          padding-top: 10px;
        }
        .footer strong {
          color: var(--primary-text-color);
        }
        .swatch {
          border-radius: 2px;
          display: inline-block;
          height: 10px;
          width: 16px;
        }
        .swatch.normal { background: var(--hoymiles-aurora-accent); }
        .swatch.planned { background: var(--hoymiles-aurora-good); }
        .swatch.blocked { background: #777b82; }
        .swatch.current {
          background: transparent;
          border: 2px solid #ffd54f;
          box-sizing: border-box;
        }
        .line {
          border-top: 2px dashed var(--hoymiles-aurora-warn);
          display: inline-block;
          width: 18px;
        }
        .active { color: var(--hoymiles-aurora-good); font-weight: 600; }
        .inactive { color: var(--secondary-text-color); }
        .range-note { color: var(--hoymiles-aurora-warn); }
        .empty {
          align-items: center;
          color: var(--secondary-text-color);
          display: flex;
          gap: 14px;
          padding: 28px 4px 18px;
        }
        .empty ha-icon {
          color: var(--hoymiles-aurora-warn);
          --mdc-icon-size: 36px;
        }
        .empty strong,
        .empty span {
          display: block;
        }
        .empty strong {
          color: var(--primary-text-color);
          margin-bottom: 4px;
        }
        @media (max-width: 620px) {
          ha-card { padding: 14px 12px 12px; }
          .top { display: block; }
          .badges { justify-content: flex-start; margin-top: 10px; }
          .badge { flex: 1 1 120px; }
          .header { font-size: 20px; }
        }
      </style>
      ${content}
    `;
  }
}

if (!customElements.get("hoymiles-rce-chart-card")) {
  customElements.define("hoymiles-rce-chart-card", HoymilesRceChartCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "hoymiles-rce-chart-card")) {
  window.customCards.push({
    type: "hoymiles-rce-chart-card",
    name: "Hoymiles RCE Chart",
    description: "Readable chart of 96 RCE prices with a 48-block EMS plan.",
    preview: true,
  });
}

class HoymilesDiagnosticsDownloadCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._busy = false;
    this._status = "";
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    const previousLanguage = this._language;
    this._hass = hass;
    this._language = String(hass?.language || "en").toLowerCase();
    if (previousLanguage !== this._language || !this.shadowRoot?.childElementCount) {
      this._render();
    }
  }

  connectedCallback() {
    this._render();
  }

  _translations() {
    if (this._language?.startsWith("pl")) {
      return {
        title: "Pakiet diagnostyczny",
        description:
          "Jednym kliknięciem zbierz stan integracji, historię sterowania z 24 godzin i odfiltrowane logi Home Assistanta.",
        privacy:
          "Identyfikatory urządzeń i konfiguracji są maskowane. Losowy anonimowy ID pozostaje wyłącznie do łączenia kolejnych paczek wsparcia. Przejrzyj ZIP przed publicznym udostępnieniem.",
        contact:
          "W razie błędu pobierz ZIP i wyślij go wraz z opisem problemu oraz dokładną datą i godziną wystąpienia na:",
        button: "Zbierz dane i pobierz ZIP",
        preparing: "Przygotowywanie pakietu…",
        downloaded: "Pakiet został pobrany.",
        adminOnly: "Pobranie pakietu wymaga konta administratora Home Assistanta.",
        error: "Nie udało się utworzyć pakietu. Sprawdź uprawnienia administratora i log HA.",
      };
    }
    return {
      title: "Diagnostic package",
      description:
        "Collect integration state, 24 hours of control history and filtered Home Assistant logs with one click.",
      privacy:
        "Device and configuration identifiers are masked. A random anonymous ID is retained only to correlate later support archives. Review the ZIP before sharing it publicly.",
      contact:
        "If an error occurs, download the ZIP and email it with a problem description and the exact date and time to:",
      button: "Collect data and download ZIP",
      preparing: "Preparing package…",
      downloaded: "The package has been downloaded.",
      adminOnly: "Downloading the package requires a Home Assistant administrator account.",
      error:
        "The package could not be created. Check administrator access and the HA log.",
    };
  }

  async _download() {
    if (this._busy || !this._hass) return;
    const text = this._translations();
    if (!this._hass.user?.is_admin) {
      this._status = text.adminOnly;
      this._updateAction();
      return;
    }
    this._busy = true;
    this._status = text.preparing;
    this._updateAction();
    try {
      const response = await this._hass.fetchWithAuth(
        "/api/hoymiles_hit_modbus/support-bundle",
        {
          method: "GET",
          cache: "no-store",
        }
      );
      if (!response.ok) {
        throw new Error(`Diagnostic download failed (${response.status})`);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="?([^";]+)"?/i);
      const filename = match?.[1] || "hoymiles_diagnostics.zip";
      const downloadUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = downloadUrl;
      anchor.download = filename;
      anchor.style.display = "none";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 30_000);
      this._status = text.downloaded;
    } catch (error) {
      console.error("Hoymiles diagnostic download failed", error);
      this._status = text.error;
    } finally {
      this._busy = false;
      this._updateAction();
    }
  }

  _updateAction() {
    const button = this.shadowRoot?.querySelector("button");
    const status = this.shadowRoot?.querySelector(".status");
    if (button) {
      button.disabled = this._busy || Boolean(
        this._hass?.user && !this._hass.user.is_admin
      );
      button.textContent = this._busy
        ? this._translations().preparing
        : this._translations().button;
    }
    if (status) {
      status.textContent = this._status;
      status.hidden = !this._status;
    }
  }

  _render() {
    if (!this.isConnected || !this._config) return;
    const text = this._translations();
    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="content">
          <div class="heading">
            <ha-icon icon="mdi:file-download-outline"></ha-icon>
            <div>
              <h2>${text.title}</h2>
              <p>${text.description}</p>
            </div>
          </div>
          <div class="privacy">
            <ha-icon icon="mdi:shield-lock-outline"></ha-icon>
            <span>${text.privacy}</span>
          </div>
          <div class="contact">
            <span>${text.contact}</span>
            <a href="mailto:info@kaluzaaa.com?subject=Hoymiles%20HIT%20-%20raport%20diagnostyczny">info@kaluzaaa.com</a>
          </div>
          <button type="button">${text.button}</button>
          <div class="status" role="status" aria-live="polite" hidden></div>
        </div>
      </ha-card>
      <style>
        ${HOYMILES_AURORA_THEME_CSS}
        :host { display: block; --hoymiles-aurora-accent: ${hoymilesAuroraAccent("cyan")}; }
        ha-card {
          background: var(--hoymiles-aurora-surface);
          border: 1px solid var(--hoymiles-aurora-border);
          border-radius: var(--ha-card-border-radius);
          box-shadow: var(--hoymiles-aurora-shadow);
          overflow: hidden;
        }
        .content { padding: 18px; }
        .heading { align-items: flex-start; display: flex; gap: 14px; }
        .heading > ha-icon {
          color: var(--hoymiles-aurora-accent);
          margin-top: 2px;
          --mdc-icon-size: 34px;
        }
        h2 { font-size: 20px; margin: 0 0 6px; }
        p { color: var(--secondary-text-color); margin: 0; }
        .privacy {
          align-items: center;
          background: color-mix(in srgb, var(--hoymiles-aurora-accent) 10%, transparent);
          border-radius: 9px;
          display: flex;
          gap: 9px;
          margin: 16px 0;
          padding: 10px 12px;
        }
        .privacy ha-icon { color: var(--hoymiles-aurora-accent); flex: 0 0 auto; }
        .contact {
          display: flex;
          flex-wrap: wrap;
          gap: 5px 8px;
          margin: 0 0 16px;
        }
        .contact a {
          color: var(--hoymiles-aurora-accent);
          font-weight: 700;
          text-decoration: none;
        }
        .contact a:hover { text-decoration: underline; }
        button {
          background: var(--hoymiles-aurora-accent);
          border: 0;
          border-radius: 8px;
          color: var(--text-primary-color, white);
          cursor: pointer;
          font: inherit;
          font-weight: 600;
          min-height: 42px;
          padding: 0 18px;
        }
        button:hover { filter: brightness(1.08); }
        button:disabled { cursor: wait; opacity: 0.65; }
        .status { margin-top: 11px; }
        @media (max-width: 520px) {
          :host { --ha-card-border-radius: 16px; }
          button { width: 100%; }
        }
      </style>
    `;
    this.shadowRoot.querySelector("button")?.addEventListener(
      "click",
      () => this._download()
    );
    this._updateAction();
  }

  getCardSize() {
    return 3;
  }
}

if (!customElements.get("hoymiles-diagnostics-download-card")) {
  customElements.define(
    "hoymiles-diagnostics-download-card",
    HoymilesDiagnosticsDownloadCard
  );
}

if (
  !window.customCards.some(
    (card) => card.type === "hoymiles-diagnostics-download-card"
  )
) {
  window.customCards.push({
    type: "hoymiles-diagnostics-download-card",
    name: "Hoymiles Diagnostic Download",
    description: "Download a privacy-filtered support ZIP.",
    preview: false,
  });
}

class HoymilesZebraEntitiesCard extends HTMLElement {
  constructor() {
    super();
    this._renderVersion = 0;
  }

  setConfig(config) {
    if (!config?.entities) {
      throw new Error("Zebra entities card requires an entities list");
    }
    this._config = { ...config };
    this._mount();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._card) {
      this._card.hass = hass;
    }
  }

  connectedCallback() {
    this._mount();
  }

  async _mount() {
    if (!this.isConnected || !this._config) return;

    const renderVersion = ++this._renderVersion;
    await customElements.whenDefined("hui-entities-card");
    if (renderVersion !== this._renderVersion) return;

    const card = document.createElement("hui-entities-card");
    card.setConfig({
      ...this._config,
      type: "entities",
    });
    if (this._hass) {
      card.hass = this._hass;
    }

    this.replaceChildren(card);
    this._card = card;
    await card.updateComplete;
    if (renderVersion !== this._renderVersion || this._card !== card) return;

    const style = document.createElement("style");
    style.dataset.hoymilesZebraRows = "";
    style.textContent = `
      ${HOYMILES_AURORA_THEME_CSS}
      :host { --hoymiles-aurora-accent: ${hoymilesAuroraAccent(this._config.accent || hoymilesAuroraTextAccent(this._config.title))}; }
      ha-card {
        background: var(--hoymiles-aurora-surface);
        border-color: var(--hoymiles-aurora-border);
        border-radius: var(--ha-card-border-radius);
        box-shadow: var(--hoymiles-aurora-shadow);
        overflow: hidden;
      }
      #states > div {
        border-radius: 8px;
        box-sizing: border-box;
        margin-left: -8px;
        margin-right: -8px;
        padding: 4px 8px;
      }
      #states > div:nth-child(odd) {
        background: transparent;
      }
      #states > div:nth-child(even) {
        background: color-mix(
          in srgb,
          var(--card-background-color, var(--ha-card-background)) 91%,
          var(--hoymiles-aurora-accent) 9%
        );
      }
      @media (max-width: 420px) {
        :host { --ha-card-border-radius: 16px; }
      }
    `;
    card.shadowRoot?.append(style);
  }

  getCardSize() {
    return this._card?.getCardSize?.() ?? this._config?.entities?.length ?? 1;
  }

  getGridOptions() {
    return this._card?.getGridOptions?.();
  }
}

if (!customElements.get("hoymiles-zebra-entities-card")) {
  customElements.define(
    "hoymiles-zebra-entities-card",
    HoymilesZebraEntitiesCard
  );
}

if (
  !window.customCards.some(
    (card) => card.type === "hoymiles-zebra-entities-card"
  )
) {
  window.customCards.push({
    type: "hoymiles-zebra-entities-card",
    name: "Hoymiles Zebra Entities",
    description: "Entities card with alternating row backgrounds.",
    preview: false,
  });
}

class HoymilesResponsiveGlanceCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    if (!config?.entities) {
      throw new Error("Responsive glance card requires an entities list");
    }
    this._config = { ...config };
    this._mount();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  connectedCallback() {
    this._render();
  }

  _mount() {
    if (!this.isConnected || !this._config) return;
    const style = document.createElement("style");
    const minimumWidth = Math.max(Number(this._config.minimum_width) || 112, 80);
    style.textContent = `
      ${HOYMILES_AURORA_THEME_CSS}
      :host {
        container-type: inline-size;
        display: block;
        --hoymiles-aurora-accent: ${hoymilesAuroraAccent(this._config.accent || hoymilesAuroraTextAccent(this._config.title))};
      }
      ha-card {
        background: var(--hoymiles-aurora-surface);
        border: 1px solid var(--hoymiles-aurora-border);
        border-radius: var(--ha-card-border-radius);
        box-shadow: var(--hoymiles-aurora-shadow);
        overflow: hidden;
      }
      .title {
        color: var(--ha-card-header-color, var(--primary-text-color));
        font-size: var(--ha-card-header-font-size, 24px);
        line-height: 1.2;
        padding: 20px 16px 10px;
      }
      .entities {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(min(100%, ${minimumWidth}px), 1fr));
        gap: 8px;
        padding: ${this._config.title ? "6px 12px 16px" : "16px 12px"};
      }
      .entity {
        appearance: none;
        background: transparent;
        border: 0;
        border-radius: 10px;
        color: var(--primary-text-color);
        cursor: pointer;
        display: grid;
        grid-template-rows: 28px auto auto;
        justify-items: center;
        min-width: 0;
        padding: 8px 6px;
        text-align: center;
      }
      .entity:hover { background: color-mix(in srgb, var(--hoymiles-aurora-accent) 9%, transparent); }
      ha-state-icon {
        color: var(--state-icon-color);
        height: 24px;
        width: 24px;
      }
      .name {
        color: var(--secondary-text-color);
        font-size: 13px;
        line-height: 1.25;
        margin-top: 5px;
        max-width: 100%;
        overflow-wrap: anywhere;
        white-space: normal;
      }
      .state {
        font-size: 14px;
        line-height: 1.3;
        margin-top: 4px;
        max-width: 100%;
        overflow-wrap: anywhere;
        white-space: normal;
      }
      @container (max-width: 420px) {
        :host { --ha-card-border-radius: 16px; }
        .entities { gap: 5px; padding-left: 8px; padding-right: 8px; }
        .entity { padding-left: 4px; padding-right: 4px; }
      }
      @media (prefers-reduced-motion: reduce) {
        .entity { transition: none; }
      }
    `;
    this._card = document.createElement("ha-card");
    this._title = document.createElement("div");
    this._title.className = "title";
    this._title.textContent = this._config.title || "";
    this._entities = document.createElement("div");
    this._entities.className = "entities";
    this._card.append(
      ...(this._config.title ? [this._title] : []),
      this._entities
    );
    this.shadowRoot.replaceChildren(style, this._card);
    this._render();
  }

  _render() {
    if (!this.isConnected || !this._config) return;
    if (!this._entities) {
      this._mount();
      return;
    }

    const hass = this._hass;
    const rows = this._config.entities.map((entry) => {
      const entityConfig = typeof entry === "string" ? { entity: entry } : entry;
      const entityId = entityConfig.entity;
      const stateObj = hass?.states?.[entityId];
      const button = document.createElement("button");
      button.type = "button";
      button.className = "entity";
      button.title = entityConfig.name || stateObj?.attributes?.friendly_name || entityId;
      button.addEventListener("click", () => {
        this.dispatchEvent(
          new CustomEvent("hass-more-info", {
            bubbles: true,
            composed: true,
            detail: { entityId },
          })
        );
      });

      const icon = document.createElement("ha-state-icon");
      icon.hass = hass;
      icon.stateObj = stateObj;
      if (entityConfig.icon) icon.icon = entityConfig.icon;

      const name = document.createElement("div");
      name.className = "name";
      name.textContent =
        entityConfig.name || stateObj?.attributes?.friendly_name || entityId;

      const state = document.createElement("div");
      state.className = "state";
      state.textContent = stateObj
        ? hass?.formatEntityState?.(stateObj) ?? stateObj.state
        : "—";

      if (this._config.show_name === false) name.hidden = true;
      if (this._config.show_state === false) state.hidden = true;
      button.append(icon, name, state);
      return button;
    });
    this._entities.replaceChildren(...rows);
  }

  getCardSize() {
    return this._card?.getCardSize?.() ?? 2;
  }
}

if (!customElements.get("hoymiles-responsive-glance-card")) {
  customElements.define(
    "hoymiles-responsive-glance-card",
    HoymilesResponsiveGlanceCard
  );
}

class HoymilesResponsiveStackCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cards = [];
    this._renderVersion = 0;
  }

  setConfig(config) {
    if (!Array.isArray(config?.cards) || config.cards.length === 0) {
      throw new Error("Responsive stack card requires a cards list");
    }
    this._config = { ...config };
    this._mount();
  }

  set hass(hass) {
    this._hass = hass;
    for (const card of this._cards) card.hass = hass;
  }

  connectedCallback() {
    this._mount();
  }

  async _mount() {
    if (!this.isConnected || !this._config) return;
    const renderVersion = ++this._renderVersion;
    const helpers = await window.loadCardHelpers();
    if (renderVersion !== this._renderVersion) return;
    const cards = this._config.cards.map((config) =>
      helpers.createCardElement(config)
    );
    if (this._hass) {
      for (const card of cards) card.hass = this._hass;
    }
    const minimumWidth = Math.max(Number(this._config.minimum_width) || 280, 180);
    const gap = Math.max(Number(this._config.gap) || 8, 0);
    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; }
      .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(min(100%, ${minimumWidth}px), 1fr));
        gap: ${gap}px;
        align-items: start;
      }
    `;
    const grid = document.createElement("div");
    grid.className = "grid";
    grid.append(...cards);
    this.shadowRoot.replaceChildren(style, grid);
    this._cards = cards;
  }

  getCardSize() {
    return Math.max(...this._cards.map((card) => card.getCardSize?.() ?? 1), 1);
  }
}

if (!customElements.get("hoymiles-responsive-stack-card")) {
  customElements.define(
    "hoymiles-responsive-stack-card",
    HoymilesResponsiveStackCard
  );
}

for (const card of [
  {
    type: "hoymiles-responsive-glance-card",
    name: "Hoymiles Responsive Glance",
    description: "Glance card that wraps cleanly on phones.",
  },
  {
    type: "hoymiles-responsive-stack-card",
    name: "Hoymiles Responsive Stack",
    description: "Multi-card grid that becomes one column on narrow screens.",
  },
]) {
  if (!window.customCards.some((item) => item.type === card.type)) {
    window.customCards.push({ ...card, preview: false });
  }
}

class HoymilesAuroraFrameCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._card = null;
    this._renderVersion = 0;
  }

  setConfig(config) {
    if (!config?.card || typeof config.card !== "object") {
      throw new Error("Aurora frame card requires a nested card configuration");
    }
    this._config = {
      ...config,
      accent: Object.hasOwn(HOYMILES_AURORA_ACCENTS, config.accent)
        ? config.accent
        : "neutral",
      card: { ...config.card },
    };
    this._mount();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._card) this._card.hass = hass;
  }

  connectedCallback() {
    this._mount();
  }

  disconnectedCallback() {
    this._renderVersion += 1;
  }

  async _mount() {
    if (!this.isConnected || !this._config) return;
    const renderVersion = ++this._renderVersion;
    const helpers = await window.loadCardHelpers();
    if (renderVersion !== this._renderVersion || !this.isConnected) return;

    const card = helpers.createCardElement({ ...this._config.card });
    if (this._hass) card.hass = this._hass;
    const accent = hoymilesAuroraAccent(this._config.accent);
    const style = document.createElement("style");
    style.textContent = `
      ${HOYMILES_AURORA_THEME_CSS}
      :host {
        container-type: inline-size;
        display: block;
        --hoymiles-aurora-accent: ${accent};
      }
      .surface {
        border-radius: var(--ha-card-border-radius, 20px);
        isolation: isolate;
        position: relative;
      }
      .surface::after {
        border: 1px solid var(--hoymiles-aurora-border);
        border-radius: inherit;
        box-shadow: inset 0 1px 0 color-mix(in srgb, #fff 4%, transparent);
        content: "";
        inset: 0;
        pointer-events: none;
        position: absolute;
        z-index: 2;
      }
      .surface > * {
        position: relative;
        z-index: 1;
      }
      @container (max-width: 420px) {
        :host {
          --ha-card-border-radius: 16px;
          --hoymiles-aurora-shadow: 0 8px 22px color-mix(in srgb, #000 14%, transparent);
        }
        .surface::after {
          box-shadow: none;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .surface::after { transition: none; }
      }
    `;
    const surface = document.createElement("div");
    surface.className = "surface";
    surface.append(card);
    this.shadowRoot.replaceChildren(style, surface);
    this._card = card;
  }

  getCardSize() {
    return this._card?.getCardSize?.() ?? 1;
  }

  getGridOptions() {
    return this._card?.getGridOptions?.();
  }
}

if (!customElements.get("hoymiles-aurora-frame-card")) {
  customElements.define("hoymiles-aurora-frame-card", HoymilesAuroraFrameCard);
}

if (!window.customCards.some((card) => card.type === "hoymiles-aurora-frame-card")) {
  window.customCards.push({
    type: "hoymiles-aurora-frame-card",
    name: "Hoymiles Aurora Frame",
    description: "Theme-aware Aurora surface around a native Home Assistant card.",
    preview: false,
  });
}

class HoymilesAuroraStatusCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._renderKey = "";
  }

  setConfig(config) {
    this._config = {
      system_entity: "sensor.hoymiles_hit_overview_system_work_status",
      inverter_entity: "sensor.hoymiles_hit_inverter_work_status",
      meter_entity: "sensor.hoymiles_hit_meter_link_status",
      battery_entity: "sensor.hoymiles_hit_battery_link_status",
      parallel_entity: "sensor.hoymiles_hit_parallel_ems_control_status",
      setup_entity: null,
      alarm_entities: [],
      details_path: "stany-alarmy",
      ...config,
    };
    if (!Array.isArray(this._config.alarm_entities)) {
      throw new Error("Aurora status card alarm_entities must be a list");
    }
    this._renderKey = "";
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  connectedCallback() {
    this._update();
  }

  _copy() {
    const pl = hoymilesLanguage(this._hass, this._config?.language) === "pl";
    return pl
      ? {
          title: "Stan instalacji",
          allGood: "System działa prawidłowo",
          warning: "System wymaga uwagi",
          error: "Wykryto błąd systemu",
          unavailable: "Część danych jest niedostępna",
          system: "System",
          inverter: "Falownik",
          meter: "Licznik",
          battery: "Bateria",
          parallel: "Sieć równoległa",
          setup: "Integracja",
          alarms: "Alarmy",
          details: "Stany i alarmy",
          changed: "zmiana",
          now: "przed chwilą",
          minute: "min temu",
          hour: "godz. temu",
          day: "dni temu",
          noData: "Brak danych",
          noAlarm: "Brak aktywnych alarmów",
        }
      : {
          title: "Installation status",
          allGood: "System operating normally",
          warning: "System needs attention",
          error: "System fault detected",
          unavailable: "Some data is unavailable",
          system: "System",
          inverter: "Inverter",
          meter: "Meter",
          battery: "Battery",
          parallel: "Parallel network",
          setup: "Integration",
          alarms: "Alarms",
          details: "States and alarms",
          changed: "changed",
          now: "just now",
          minute: "min ago",
          hour: "h ago",
          day: "days ago",
          noData: "No data",
          noAlarm: "No active alarms",
        };
  }

  _alarmEntityId(entry) {
    return typeof entry === "string" ? entry : entry?.entity;
  }

  _entityRows() {
    const copy = this._copy();
    const rows = [
      ["system", copy.system, this._config.system_entity, false],
      ["inverter", copy.inverter, this._config.inverter_entity, false],
      ["meter", copy.meter, this._config.meter_entity, false],
      ["battery", copy.battery, this._config.battery_entity, false],
      ["parallel", copy.parallel, this._config.parallel_entity, false],
    ];
    if (this._config.setup_entity) {
      rows.push(["setup", copy.setup, this._config.setup_entity, false]);
    }
    for (const entry of this._config.alarm_entities) {
      const entityId = this._alarmEntityId(entry);
      if (!entityId) continue;
      const state = this._hass?.states?.[entityId];
      rows.push([
        "alarm",
        typeof entry === "object" && entry?.name
          ? entry.name
          : state?.attributes?.friendly_name || copy.alarms,
        entityId,
        true,
      ]);
    }
    return rows;
  }

  _toneForState(state, alarm = false) {
    if (alarm) {
      if (!state) return "offline";
      const value = String(state.state).trim().toLowerCase();
      if (["unknown", "unavailable", ""].includes(value) || /niedostępn|niedostepn/.test(value)) return "offline";
      if (/^(0|0\.0|ok|normal|none|no_error|no_errors)$/.test(value)) return "good";
      if (/brak (błęd|bled|alarm)|no[ _-]?(fault|error|errors|alarm)|bez błęd/.test(value)) return "good";
      return "error";
    }
    if (!state || ["unknown", "unavailable", ""].includes(String(state.state).toLowerCase()) || /niedostępn|niedostepn/.test(String(state.state).toLowerCase())) {
      return "offline";
    }
    const value = String(state.state).trim().toLowerCase();
    if (/błąd|blad|fault|error|alarm|awari|offline|rozłącz|rozlacz|disconnected|niegotow|not ready/.test(value)) {
      return "error";
    }
    if (/ostrzeż|ostrzez|warning|wyspow|off.?grid|brak sieci|no grid|oczek|partial|część|czesc|degraded/.test(value)) {
      return "warn";
    }
    if (/^ok$|online|gotow|ready|normal|running|operating|pracuj|praca|grid.?connected|connected|połącz|polacz|self.?use|autokonsump|ładow|ladow|rozładow|rozladow|czuwan|idle|standby/.test(value)) {
      return "good";
    }
    return "warn";
  }

  _relativeTime(value) {
    const copy = this._copy();
    const timestamp = new Date(value).getTime();
    if (!Number.isFinite(timestamp)) return copy.noData;
    const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (seconds < 60) return copy.now;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} ${copy.minute}`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} ${copy.hour}`;
    return `${Math.round(hours / 24)} ${copy.day}`;
  }

  _detailsPath() {
    const configured = String(this._config?.details_path || "stany-alarmy").trim();
    if (!configured) return "";
    if (configured.startsWith("/")) return configured;
    const first = String(globalThis.location?.pathname || "")
      .split("/")
      .filter(Boolean)[0];
    return `/${first || "hoymiles-falownik"}/${configured.replace(/^\/+/, "")}`;
  }

  _navigate(event) {
    event?.preventDefault?.();
    const path = this._detailsPath();
    if (!path || !globalThis.history?.pushState) return;
    globalThis.history.pushState(null, "", path);
    globalThis.window?.dispatchEvent?.(new Event("location-changed"));
  }

  _update() {
    if (!this._config || !this.isConnected) return;
    const rows = this._entityRows();
    const key = `${hoymilesLanguage(this._hass, this._config.language)}|${rows
      .map(([, , entityId]) => {
        const state = this._hass?.states?.[entityId];
        return `${entityId}:${state?.state || "missing"}:${state?.last_changed || ""}`;
      })
      .join("|")}`;
    if (key === this._renderKey) return;
    this._renderKey = key;
    this._render(rows);
  }

  _render(rows) {
    const copy = this._copy();
    const evaluated = rows.map(([key, label, entityId, alarm]) => {
      const state = this._hass?.states?.[entityId];
      return { key, label, entityId, state, tone: this._toneForState(state, alarm), alarm };
    });
    const alarmRows = evaluated.filter((row) => row.alarm);
    const healthRows = evaluated.filter((row) => !row.alarm || row.tone !== "offline");
    const errors = healthRows.filter((row) => row.tone === "error");
    const warnings = healthRows.filter((row) => row.tone === "warn");
    const offline = healthRows.filter((row) => row.tone === "offline");
    const summaryTone = errors.length ? "error" : warnings.length || offline.length ? "warn" : "good";
    const summary = errors.length
      ? copy.error
      : offline.length
        ? copy.unavailable
        : warnings.length
          ? copy.warning
          : copy.allGood;
    const statusRows = evaluated.filter((row) => !row.alarm);
    const activeAlarms = alarmRows.filter((row) => row.tone === "error" || row.tone === "warn");
    const compact = summaryTone === "good";
    const problemRows = healthRows.filter((row) => row.tone !== "good");
    this._summaryTone = summaryTone;

    this.shadowRoot.innerHTML = `
      <style>
        ${HOYMILES_AURORA_THEME_CSS}
        :host { container-type: inline-size; display: block; --hoymiles-aurora-accent: ${hoymilesAuroraAccent(summaryTone === "error" ? "load" : summaryTone === "warn" ? "warning" : "cyan")}; }
        * { box-sizing: border-box; }
        button, a { font: inherit; }
        ha-card { background: var(--hoymiles-aurora-surface); border: 1px solid var(--hoymiles-aurora-border); border-radius: var(--ha-card-border-radius); box-shadow: var(--hoymiles-aurora-shadow); color: var(--hoymiles-aurora-text); overflow: hidden; padding: 18px; }
        .top { align-items: center; display: flex; gap: 12px; justify-content: space-between; }
        .eyebrow { color: var(--hoymiles-aurora-muted); font-size: 11px; letter-spacing: .09em; text-transform: uppercase; }
        .summary { align-items: center; display: flex; font-size: 18px; font-weight: 650; gap: 9px; margin-top: 3px; }
        .summary-dot, .dot { background: var(--tone); border-radius: 50%; box-shadow: 0 0 12px color-mix(in srgb, var(--tone) 75%, transparent); flex: 0 0 auto; height: 8px; width: 8px; }
        .summary.good, .item.good, .chip.good { --tone: var(--hoymiles-aurora-good); }
        .summary.warn, .item.warn, .chip.warn { --tone: var(--hoymiles-aurora-warn); }
        .summary.error, .item.error, .chip.error { --tone: var(--hoymiles-aurora-error); }
        .item.offline, .chip.offline { --tone: var(--hoymiles-aurora-offline); }
        .details { align-items: center; background: color-mix(in srgb, var(--hoymiles-aurora-accent) 11%, transparent); border: 1px solid color-mix(in srgb, var(--hoymiles-aurora-accent) 28%, transparent); border-radius: 999px; color: var(--hoymiles-aurora-text); cursor: pointer; display: inline-flex; min-height: 38px; padding: 7px 12px; text-decoration: none; }
        .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 11px; }
        .chip { align-items: center; background: color-mix(in srgb, var(--card-background-color) 91%, var(--hoymiles-aurora-good) 9%); border: 1px solid color-mix(in srgb, var(--hoymiles-aurora-good) 20%, var(--divider-color)); border-radius: 999px; color: inherit; cursor: pointer; display: inline-flex; gap: 6px; min-height: 30px; min-width: 0; padding: 5px 9px; }
        .chip .dot { height: 6px; width: 6px; }
        .chip-label { color: var(--hoymiles-aurora-muted); font-size: 9px; }
        .chip-value { font-size: 10px; font-weight: 600; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .grid { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 176px), 1fr)); margin-top: 15px; }
        .item { align-items: center; background: color-mix(in srgb, var(--card-background-color) 88%, var(--primary-text-color) 12%); border: 1px solid color-mix(in srgb, var(--divider-color) 75%, transparent); border-radius: 13px; color: inherit; cursor: pointer; display: grid; gap: 3px; grid-template-columns: 11px minmax(0, 1fr); min-height: 78px; padding: 10px; text-align: left; }
        .item:hover, .details:hover { border-color: color-mix(in srgb, var(--tone, var(--hoymiles-aurora-accent)) 38%, transparent); }
        .label, .changed { color: var(--hoymiles-aurora-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .label { font-size: 10px; }
        .value { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .changed { font-size: 9px; }
        .copy { min-width: 0; }
        .alarms { border-top: 1px solid var(--divider-color); color: ${activeAlarms.length ? "var(--hoymiles-aurora-error)" : "var(--hoymiles-aurora-muted)"}; font-size: 11px; margin-top: 11px; padding-top: 9px; }
        @container (max-width: 700px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .item:last-child:nth-child(odd) { grid-column: 1 / -1; } }
        @container (max-width: 420px) { ha-card { border-radius: 16px; padding: 14px 12px; } .summary { font-size: 16px; } .details { min-height: 36px; padding: 6px 10px; } .chips { gap: 5px; } .chip { flex: 1 1 135px; justify-content: center; } .grid { gap: 6px; margin-top: 12px; } .item { min-height: 72px; padding: 8px; } }
        @media (prefers-reduced-motion: reduce) { .summary-dot, .dot { box-shadow: none; } }
      </style>
      <ha-card>
        <div class="top">
          <div><div class="eyebrow">${hoymilesEscape(copy.title)}</div><div class="summary ${summaryTone}"><span class="summary-dot"></span>${hoymilesEscape(summary)}</div></div>
          <button class="details" type="button">${hoymilesEscape(copy.details)} →</button>
        </div>
        ${compact
          ? `<div class="chips">${statusRows.map((row) => `<button class="chip ${row.tone}" type="button" data-entity="${hoymilesEscape(row.entityId)}"><span class="dot"></span><span class="chip-label">${hoymilesEscape(row.label)}</span><span class="chip-value">${hoymilesEscape(row.state?.state || copy.noData)}</span></button>`).join("")}</div>`
          : `<div class="grid">${problemRows.map((row) => `<button class="item ${row.tone}" type="button" data-entity="${hoymilesEscape(row.entityId)}"><span class="dot"></span><span class="copy"><span class="label">${hoymilesEscape(row.label)}</span><span class="value">${hoymilesEscape(row.state?.state || copy.noData)}</span><span class="changed">${hoymilesEscape(copy.changed)}: ${hoymilesEscape(this._relativeTime(row.state?.last_changed))}</span></span></button>`).join("")}</div>`}
        ${compact ? "" : `<div class="alarms">${activeAlarms.length ? `${hoymilesEscape(copy.alarms)}: ${activeAlarms.length}` : hoymilesEscape(copy.noAlarm)}</div>`}
      </ha-card>`;
    this.shadowRoot.querySelectorAll("[data-entity]").forEach((button) => {
      button.addEventListener("click", () => hoymilesDispatchMoreInfo(this, button.dataset.entity));
    });
    this.shadowRoot.querySelector(".details")?.addEventListener("click", (event) => this._navigate(event));
  }

  getCardSize() { return this._summaryTone === "good" ? 2 : 4; }
  getGridOptions() { return { columns: 12, rows: 4, min_columns: 6 }; }
}

if (!customElements.get("hoymiles-aurora-status-card")) {
  customElements.define("hoymiles-aurora-status-card", HoymilesAuroraStatusCard);
}
if (!window.customCards.some((card) => card.type === "hoymiles-aurora-status-card")) {
  window.customCards.push({ type: "hoymiles-aurora-status-card", name: "Hoymiles Aurora Status", description: "Compact health, connectivity and alarm summary.", preview: false });
}

class HoymilesAuroraHistoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._history = new Map();
    this._requestVersion = 0;
    this._lastFetch = 0;
    this._loading = false;
    this._error = "";
    this._renderKey = "";
  }

  setConfig(config) {
    this._requestVersion += 1;
    this._loading = false;
    const defaults = [
      { entity: "sensor.hoymiles_hit_overview_pv_total_power", name: "PV", color: HOYMILES_AURORA_ACCENTS.pv },
      { entity: "sensor.hoymiles_actual_load_power", name: "Dom", name_en: "Home", color: HOYMILES_AURORA_ACCENTS.load },
      { entity: "sensor.hoymiles_hit_overview_grid_total_active_power", name: "Sieć", name_en: "Grid", color: HOYMILES_AURORA_ACCENTS.grid },
      { entity: "sensor.hoymiles_hit_overview_battery_power", name: "Bateria", name_en: "Battery", color: HOYMILES_AURORA_ACCENTS.battery },
    ];
    const entities = config?.entities || defaults;
    if (!Array.isArray(entities) || entities.length === 0) {
      throw new Error("Aurora history card requires an entities list");
    }
    this._config = {
      title: null,
      hours_to_show: 24,
      refresh_interval: 300,
      ...config,
      entities: entities.map((entry, index) => {
        const item = typeof entry === "string" ? { entity: entry } : { ...entry };
        if (!item.entity) throw new Error("Aurora history entity is missing entity id");
        const fallback = defaults[index % defaults.length];
        return {
          ...item,
          color: /^#[0-9a-f]{3,8}$/i.test(String(item.color || ""))
            ? item.color
            : fallback.color,
        };
      }),
    };
    this._history.clear();
    this._lastFetch = 0;
    this._error = "";
    this._renderKey = "";
    this._render();
    this._ensureHistory();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
    this._ensureHistory();
  }

  connectedCallback() {
    this._render();
    this._ensureHistory();
  }

  disconnectedCallback() {
    this._requestVersion += 1;
    this._loading = false;
  }

  _copy() {
    return hoymilesLanguage(this._hass, this._config?.language) === "pl"
      ? {
          title: "Moc — ostatnie 24 godziny",
          subtitle: "Dane z rejestratora Home Assistant",
          loading: "Pobieranie historii…",
          noData: "Brak zapisanych danych z wybranego okresu",
          error: "Nie udało się pobrać historii",
          current: "teraz",
          ago: "temu",
        }
      : {
          title: "Power — last 24 hours",
          subtitle: "Home Assistant recorder data",
          loading: "Loading history…",
          noData: "No recorded data for the selected period",
          error: "History could not be loaded",
          current: "now",
          ago: "ago",
        };
  }

  _hours() {
    return Math.min(Math.max(Number(this._config?.hours_to_show) || 24, 1), 168);
  }

  _refreshMs() {
    return Math.min(Math.max(Number(this._config?.refresh_interval) || 300, 30), 3600) * 1000;
  }

  _powerKw(value, entityId, attributes = {}) {
    let number = Number(value);
    if (!Number.isFinite(number)) return null;
    const unit = String(
      attributes.unit_of_measurement ||
      this._hass?.states?.[entityId]?.attributes?.unit_of_measurement ||
      "W"
    ).toLowerCase();
    if (unit === "mw") number *= 1000;
    else if (unit !== "kw") number /= 1000;
    return number;
  }

  _currentValue(entityId) {
    const state = this._hass?.states?.[entityId];
    return this._powerKw(state?.state, entityId, state?.attributes);
  }

  _formatPower(value) {
    if (!Number.isFinite(value)) return "—";
    return `${new Intl.NumberFormat(hoymilesLanguage(this._hass, this._config?.language) === "pl" ? "pl-PL" : "en-GB", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value)} kW`;
  }

  _normalizeHistory(result, startTime, endTime) {
    const groups = Array.isArray(result) ? result : [];
    const normalized = new Map();
    for (const series of this._config.entities) {
      const group = groups.find((items) =>
        Array.isArray(items) && items.some((item) => item?.entity_id === series.entity)
      ) || [];
      const points = group
        .map((item) => ({
          time: new Date(item?.last_changed || item?.last_updated).getTime(),
          value: this._powerKw(item?.state, series.entity, item?.attributes || {}),
        }))
        .filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value) && point.time >= startTime && point.time <= endTime)
        .sort((left, right) => left.time - right.time);
      const current = this._currentValue(series.entity);
      if (Number.isFinite(current)) points.push({ time: endTime, value: current });
      normalized.set(series.entity, this._downsample(points, 420));
    }
    return normalized;
  }

  _downsample(points, maximum) {
    if (points.length <= maximum) return points;
    const first = points[0];
    const last = points[points.length - 1];
    const bucketCount = Math.max(1, Math.floor((maximum - 2) / 2));
    const duration = Math.max(last.time - first.time, 1);
    const buckets = Array.from({ length: bucketCount }, () => []);
    for (let index = 1; index < points.length - 1; index += 1) {
      const position = Math.min(
        bucketCount - 1,
        Math.max(0, Math.floor(((points[index].time - first.time) / duration) * bucketCount))
      );
      buckets[position].push(points[index]);
    }
    const sampled = [first];
    for (const bucket of buckets) {
      if (!bucket.length) continue;
      let minimum = bucket[0];
      let maximumPoint = bucket[0];
      for (const point of bucket) {
        if (point.value < minimum.value) minimum = point;
        if (point.value > maximumPoint.value) maximumPoint = point;
      }
      const extrema = minimum === maximumPoint
        ? [minimum]
        : [minimum, maximumPoint].sort((left, right) => left.time - right.time);
      for (const point of extrema) {
        const previous = sampled[sampled.length - 1];
        if (point.time !== previous.time || point.value !== previous.value) sampled.push(point);
      }
    }
    sampled.push(last);
    return sampled;
  }

  async _ensureHistory(force = false) {
    if (!this.isConnected || !this._config || !this._hass?.callApi || this._loading) return;
    const now = Date.now();
    if (!force && this._lastFetch && now - this._lastFetch < this._refreshMs()) return;
    const version = ++this._requestVersion;
    const end = new Date(now);
    const start = new Date(now - this._hours() * 60 * 60 * 1000);
    const ids = this._config.entities.map((item) => item.entity).join(",");
    const path = `history/period/${encodeURIComponent(start.toISOString())}?filter_entity_id=${encodeURIComponent(ids)}&end_time=${encodeURIComponent(end.toISOString())}&minimal_response&no_attributes`;
    this._loading = true;
    this._error = "";
    this._render();
    try {
      const result = await this._hass.callApi("GET", path);
      if (version !== this._requestVersion || !this.isConnected) return;
      this._history = this._normalizeHistory(result, start.getTime(), end.getTime());
      this._lastFetch = Date.now();
    } catch (error) {
      if (version !== this._requestVersion || !this.isConnected) return;
      this._error = String(error?.message || error || "history_error");
    } finally {
      if (version === this._requestVersion) {
        this._loading = false;
        this._render();
      }
    }
  }

  _path(points, x, y) {
    if (!points.length) return "";
    return points.map((point, index) =>
      `${index ? "L" : "M"}${x(point.time).toFixed(2)} ${y(point.value).toFixed(2)}`
    ).join(" ");
  }

  _render() {
    if (!this._config || !this.isConnected) return;
    const renderKey = [
      hoymilesLanguage(this._hass, this._config.language),
      this._config.title || "",
      this._hours(),
      this._lastFetch,
      this._loading,
      this._error,
      ...this._config.entities.map((item) => {
        const state = this._hass?.states?.[item.entity];
        return `${item.entity}:${state?.state || "missing"}:${state?.last_updated || ""}`;
      }),
      ...this._config.entities.map((item) => `${item.entity}:${this._history.get(item.entity)?.length || 0}`),
    ].join("|");
    if (renderKey === this._renderKey) return;
    this._renderKey = renderKey;
    const copy = this._copy();
    const now = Date.now();
    const start = now - this._hours() * 60 * 60 * 1000;
    const width = 720;
    const height = 360;
    const left = 55;
    const right = 14;
    const top = 18;
    const bottom = 42;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const seriesData = this._config.entities.map((series) => ({
      ...series,
      points: (this._history.get(series.entity) || []).filter((point) => point.time >= start && point.time <= now),
      current: this._currentValue(series.entity),
    }));
    const values = seriesData.flatMap((series) => series.points.map((point) => point.value));
    let minimum = Math.min(0, ...(values.length ? values : [0]));
    let maximum = Math.max(0, ...(values.length ? values : [1]));
    if (minimum === maximum) maximum = minimum + 1;
    const padding = Math.max((maximum - minimum) * 0.08, 0.1);
    minimum -= padding;
    maximum += padding;
    const x = (time) => left + ((time - start) / Math.max(now - start, 1)) * plotWidth;
    const y = (value) => top + ((maximum - value) / (maximum - minimum)) * plotHeight;
    const zeroY = y(0);
    const yGrid = Array.from({ length: 5 }, (_, index) => {
      const value = maximum - ((maximum - minimum) * index) / 4;
      const position = y(value);
      return `<line class="grid-line" x1="${left}" y1="${position}" x2="${width - right}" y2="${position}"/><text class="axis y-label" x="${left - 8}" y="${position + 4}" text-anchor="end">${value.toFixed(1)}</text>`;
    }).join("");
    const xGrid = Array.from({ length: 5 }, (_, index) => {
      const position = left + (plotWidth * index) / 4;
      const hoursAgo = Math.round(this._hours() * (1 - index / 4));
      return `<line class="grid-line vertical" x1="${position}" y1="${top}" x2="${position}" y2="${top + plotHeight}"/><text class="axis" x="${position}" y="${height - 12}" text-anchor="${index === 0 ? "start" : index === 4 ? "end" : "middle"}">${index === 4 ? copy.current : `${hoursAgo} h`}</text>`;
    }).join("");
    const graph = seriesData.map((series, index) => {
      if (!series.points.length) return "";
      const line = this._path(series.points, x, y);
      const firstX = x(series.points[0].time).toFixed(2);
      const lastX = x(series.points[series.points.length - 1].time).toFixed(2);
      const fill = `${line} L${lastX} ${zeroY.toFixed(2)} L${firstX} ${zeroY.toFixed(2)} Z`;
      return `<defs><linearGradient id="aurora-fill-${index}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${series.color}" stop-opacity=".22"/><stop offset="1" stop-color="${series.color}" stop-opacity=".015"/></linearGradient></defs><path class="area" d="${fill}" fill="url(#aurora-fill-${index})"/><path class="series-line" d="${line}" stroke="${series.color}" style="color:${series.color}"/>`;
    }).join("");
    const title = this._config.title || copy.title;
    const stateMessage = this._loading
      ? copy.loading
      : this._error
        ? copy.error
        : values.length
          ? ""
          : copy.noData;

    this.shadowRoot.innerHTML = `
      <style>
        ${HOYMILES_AURORA_THEME_CSS}
        :host { container-type: inline-size; display: block; --hoymiles-aurora-accent: ${hoymilesAuroraAccent("cyan")}; }
        * { box-sizing: border-box; }
        button { font: inherit; }
        ha-card { background: var(--hoymiles-aurora-surface); border: 1px solid var(--hoymiles-aurora-border); border-radius: var(--ha-card-border-radius); box-shadow: var(--hoymiles-aurora-shadow); color: var(--hoymiles-aurora-text); overflow: hidden; padding: 18px 18px 13px; }
        .header { align-items: flex-start; display: flex; gap: 10px; justify-content: space-between; }
        h2 { font-size: 20px; font-weight: 600; line-height: 1.25; margin: 0; }
        .subtitle, .message { color: var(--hoymiles-aurora-muted); font-size: 11px; margin-top: 3px; }
        .period { color: var(--hoymiles-aurora-muted); font-size: 11px; white-space: nowrap; }
        .chart { height: clamp(220px, 36vw, 330px); margin-top: 8px; width: 100%; }
        svg { display: block; height: 100%; overflow: visible; width: 100%; }
        .grid-line { stroke: var(--divider-color); stroke-width: 1; opacity: .45; vector-effect: non-scaling-stroke; }
        .grid-line.vertical { opacity: .25; }
        .axis { fill: var(--hoymiles-aurora-muted); font-family: Roboto, sans-serif; font-size: 10px; }
        .series-line { fill: none; filter: drop-shadow(0 0 4px color-mix(in srgb, currentColor 24%, transparent)); stroke-linecap: round; stroke-linejoin: round; stroke-width: 2.2; vector-effect: non-scaling-stroke; }
        .area { pointer-events: none; }
        .legend { display: grid; gap: 7px; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 7px; }
        .legend button { align-items: center; background: color-mix(in srgb, var(--card-background-color) 90%, var(--primary-text-color) 10%); border: 1px solid var(--divider-color); border-radius: 11px; color: inherit; cursor: pointer; display: grid; grid-template-columns: 8px minmax(0, 1fr); min-width: 0; padding: 8px 9px; text-align: left; }
        .legend button:hover { border-color: var(--series-color); }
        .legend-dot { background: var(--series-color); border-radius: 999px; box-shadow: 0 0 9px color-mix(in srgb, var(--series-color) 65%, transparent); height: 7px; width: 7px; }
        .legend-copy { min-width: 0; }
        .legend-name { color: var(--hoymiles-aurora-muted); display: block; font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .legend-value { display: block; font-size: 13px; font-variant-numeric: tabular-nums; font-weight: 650; margin-top: 1px; white-space: nowrap; }
        .message { padding: 14px 2px 4px; }
        @container (max-width: 520px) { ha-card { border-radius: 16px; padding: 14px 10px 11px; } h2 { font-size: 17px; } .chart { height: 230px; } .legend { gap: 5px; grid-template-columns: repeat(2, minmax(0, 1fr)); } .legend button { padding: 7px; } .y-label { display: none; } }
        @container (max-width: 360px) { .period { display: none; } .chart { height: 215px; } }
        @media (prefers-reduced-motion: reduce) { .series-line { filter: none; } .legend-dot { box-shadow: none; } }
      </style>
      <ha-card>
        <div class="header"><div><h2>${hoymilesEscape(title)}</h2><div class="subtitle">${hoymilesEscape(copy.subtitle)}</div></div><div class="period">${this._hours()} h</div></div>
        <div class="chart"><svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${hoymilesEscape(title)}">${yGrid}${xGrid}<line class="grid-line" x1="${left}" y1="${zeroY}" x2="${width - right}" y2="${zeroY}"/>${graph}</svg></div>
        <div class="legend">${seriesData.map((series) => `<button type="button" data-entity="${hoymilesEscape(series.entity)}" style="--series-color:${series.color}"><span class="legend-dot"></span><span class="legend-copy"><span class="legend-name">${hoymilesEscape(hoymilesLanguage(this._hass, this._config.language) === "en" && series.name_en ? series.name_en : series.name || series.entity)}</span><span class="legend-value">${hoymilesEscape(this._formatPower(series.current))}</span></span></button>`).join("")}</div>
        ${stateMessage ? `<div class="message">${hoymilesEscape(stateMessage)}</div>` : ""}
      </ha-card>`;
    this.shadowRoot.querySelectorAll("[data-entity]").forEach((button) => button.addEventListener("click", () => hoymilesDispatchMoreInfo(this, button.dataset.entity)));
  }

  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, rows: 6, min_columns: 6 }; }
}

if (!customElements.get("hoymiles-aurora-history-card")) {
  customElements.define("hoymiles-aurora-history-card", HoymilesAuroraHistoryCard);
}
if (!window.customCards.some((card) => card.type === "hoymiles-aurora-history-card")) {
  window.customCards.push({ type: "hoymiles-aurora-history-card", name: "Hoymiles Aurora History", description: "Responsive 24-hour power history with native recorder data.", preview: false });
}

class HoymilesAuroraFinanceCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._renderKey = "";
  }

  setConfig(config) {
    this._config = {
      daily_revenue_entity: "sensor.hoymiles_rce_revenue_daily",
      weekly_revenue_entity: "sensor.hoymiles_rce_revenue_weekly",
      monthly_revenue_entity: "sensor.hoymiles_rce_revenue_monthly",
      yearly_revenue_entity: "sensor.hoymiles_rce_revenue_yearly",
      total_revenue_entity: "sensor.hoymiles_rce_revenue_total",
      daily_export_entity: "sensor.hoymiles_rce_grid_export_energy_daily",
      weekly_export_entity: "sensor.hoymiles_rce_grid_export_energy_weekly",
      monthly_export_entity: "sensor.hoymiles_rce_grid_export_energy_monthly",
      yearly_export_entity: "sensor.hoymiles_rce_grid_export_energy_yearly",
      total_export_entity: "sensor.hoymiles_rce_grid_export_energy_total",
      current_price_entity: "sensor.hoymiles_rce_current_price",
      export_power_entity: "sensor.hoymiles_rce_grid_export_power",
      optimization_gain_entity: "sensor.hoymiles_rce_optimization_gain",
      optimization_plan_entity: "sensor.hoymiles_hit_rce_optimized_plan",
      ...config,
    };
    this._renderKey = "";
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  connectedCallback() {
    this._update();
  }

  _copy() {
    return hoymilesLanguage(this._hass, this._config?.language) === "pl"
      ? {
          eyebrow: "Wynik energetyczny",
          title: "Energia, która pracuje na Twój wynik",
          currentPrice: "Cena RCE teraz",
          exportPower: "Eksport teraz",
          gain: "Korzyść optymalizacji netto",
          day: "Dzisiaj",
          week: "Tydzień",
          month: "Miesiąc",
          year: "Rok",
          revenue: "przychodu",
          export: "wysłano",
          average: "średnio",
          lifetime: "Łącznie od uruchomienia statystyk",
          noData: "brak danych",
        }
      : {
          eyebrow: "Energy performance",
          title: "Energy working for your result",
          currentPrice: "RCE price now",
          exportPower: "Export now",
          gain: "Net optimization benefit",
          day: "Today",
          week: "Week",
          month: "Month",
          year: "Year",
          revenue: "revenue",
          export: "exported",
          average: "average",
          lifetime: "Since statistics started",
          noData: "no data",
        };
  }

  _state(entityId) {
    return entityId ? this._hass?.states?.[entityId] : undefined;
  }

  _numeric(entityId) {
    const value = Number(this._state(entityId)?.state);
    return Number.isFinite(value) ? value : null;
  }

  _format(value, digits = 2) {
    if (!Number.isFinite(value)) return "—";
    return new Intl.NumberFormat(
      hoymilesLanguage(this._hass, this._config?.language) === "pl" ? "pl-PL" : "en-GB",
      { minimumFractionDigits: digits, maximumFractionDigits: digits }
    ).format(value);
  }

  _currency(entityId) {
    const value = this._numeric(entityId);
    return Number.isFinite(value) ? `${this._format(value)} PLN` : "—";
  }

  _energy(entityId) {
    const state = this._state(entityId);
    let value = Number(state?.state);
    if (!Number.isFinite(value)) return null;
    const unit = String(state?.attributes?.unit_of_measurement || "kWh").toLowerCase();
    if (unit === "wh") value /= 1000;
    else if (unit === "mwh") value *= 1000;
    return value;
  }

  _power(entityId) {
    const state = this._state(entityId);
    let value = Number(state?.state);
    if (!Number.isFinite(value)) return null;
    const unit = String(state?.attributes?.unit_of_measurement || "W").toLowerCase();
    if (unit === "mw") value *= 1000;
    else if (unit !== "kw") value /= 1000;
    return value;
  }

  _periods() {
    const copy = this._copy();
    return [
      { key: "day", label: copy.day, revenue: this._config.daily_revenue_entity, export: this._config.daily_export_entity },
      { key: "week", label: copy.week, revenue: this._config.weekly_revenue_entity, export: this._config.weekly_export_entity },
      { key: "month", label: copy.month, revenue: this._config.monthly_revenue_entity, export: this._config.monthly_export_entity },
      { key: "year", label: copy.year, revenue: this._config.yearly_revenue_entity, export: this._config.yearly_export_entity },
    ];
  }

  _update() {
    if (!this._config || !this.isConnected) return;
    const ids = [
      ...this._periods().flatMap((period) => [period.revenue, period.export]),
      this._config.total_revenue_entity,
      this._config.total_export_entity,
      this._config.current_price_entity,
      this._config.export_power_entity,
      this._config.optimization_gain_entity,
      this._config.optimization_plan_entity,
    ];
    const key = `${hoymilesLanguage(this._hass, this._config.language)}|${ids.map((id) => `${id}:${this._state(id)?.state || "missing"}`).join("|")}`;
    if (key === this._renderKey) return;
    this._renderKey = key;
    this._render();
  }

  _render() {
    const copy = this._copy();
    const currentPrice = this._numeric(this._config.current_price_entity);
    const exportPower = this._power(this._config.export_power_entity);
    const plan = this._state(this._config.optimization_plan_entity);
    const netGain = Number(plan?.attributes?.net_optimization_gain_pln);
    const gain = Number.isFinite(netGain)
      ? netGain
      : this._numeric(this._config.optimization_gain_entity);
    const totalRevenue = this._numeric(this._config.total_revenue_entity);
    const totalExport = this._energy(this._config.total_export_entity);
    const periods = this._periods().map((period) => {
      const revenue = this._numeric(period.revenue);
      const energy = this._energy(period.export);
      return {
        ...period,
        revenue,
        energy,
        average: Number.isFinite(revenue) && Number.isFinite(energy) && energy > 0
          ? revenue / energy
          : null,
      };
    });
    this.shadowRoot.innerHTML = `
      <style>
        ${HOYMILES_AURORA_THEME_CSS}
        :host { container-type: inline-size; display: block; --hoymiles-aurora-accent: ${hoymilesAuroraAccent("grid")}; }
        * { box-sizing: border-box; }
        button { font: inherit; }
        ha-card { background: var(--hoymiles-aurora-surface); border: 1px solid var(--hoymiles-aurora-border); border-radius: var(--ha-card-border-radius); box-shadow: var(--hoymiles-aurora-shadow); color: var(--hoymiles-aurora-text); overflow: hidden; padding: 20px; }
        .eyebrow { color: var(--hoymiles-aurora-grid); font-size: 10px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase; }
        h2 { font-size: 21px; font-weight: 620; line-height: 1.25; margin: 4px 0 0; }
        .headline { display: grid; gap: 8px; grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 15px; }
        .headline-item { background: color-mix(in srgb, var(--card-background-color) 89%, var(--hoymiles-aurora-grid) 11%); border: 1px solid color-mix(in srgb, var(--hoymiles-aurora-grid) 18%, var(--divider-color)); border-radius: 12px; min-width: 0; padding: 10px 11px; }
        .label { color: var(--hoymiles-aurora-muted); display: block; font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .headline-value { display: block; font-size: 15px; font-variant-numeric: tabular-nums; font-weight: 650; margin-top: 3px; white-space: nowrap; }
        .periods { display: grid; gap: 8px; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 13px; }
        .period { background: color-mix(in srgb, var(--card-background-color) 92%, var(--primary-text-color) 8%); border: 1px solid var(--divider-color); border-radius: 14px; color: inherit; cursor: pointer; min-width: 0; padding: 12px; text-align: left; transition: border-color .16s ease, transform .16s ease; }
        .period:hover { border-color: color-mix(in srgb, var(--hoymiles-aurora-grid) 42%, transparent); transform: translateY(-1px); }
        .period-title { color: var(--hoymiles-aurora-muted); display: block; font-size: 10px; text-transform: uppercase; }
        .revenue { color: var(--hoymiles-aurora-grid); display: block; font-size: 21px; font-variant-numeric: tabular-nums; font-weight: 700; letter-spacing: -.035em; margin-top: 7px; white-space: nowrap; }
        .detail { color: var(--hoymiles-aurora-muted); display: block; font-size: 10px; line-height: 1.45; margin-top: 6px; }
        .detail strong { color: var(--hoymiles-aurora-text); font-weight: 600; }
        .lifetime { align-items: center; border-top: 1px solid var(--divider-color); display: flex; flex-wrap: wrap; gap: 8px 22px; justify-content: space-between; margin-top: 14px; padding-top: 12px; }
        .lifetime-label { color: var(--hoymiles-aurora-muted); font-size: 10px; }
        .lifetime-values { display: flex; flex-wrap: wrap; gap: 8px 20px; font-size: 14px; font-variant-numeric: tabular-nums; font-weight: 650; }
        @container (max-width: 620px) { ha-card { border-radius: 16px; padding: 15px 12px; } h2 { font-size: 18px; } .headline { grid-template-columns: 1fr; gap: 5px; } .headline-item { align-items: center; display: flex; justify-content: space-between; padding: 8px 10px; } .headline-value { font-size: 13px; margin: 0; } .periods { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; } .period { padding: 10px; } .revenue { font-size: 18px; } }
        @container (max-width: 360px) { .lifetime { display: block; } .lifetime-values { margin-top: 6px; } }
        @media (prefers-reduced-motion: reduce) { .period { transition: none; } }
      </style>
      <ha-card>
        <div class="eyebrow">${hoymilesEscape(copy.eyebrow)}</div>
        <h2>${hoymilesEscape(this._config.title || copy.title)}</h2>
        <div class="headline">
          <div class="headline-item"><span class="label">${hoymilesEscape(copy.currentPrice)}</span><strong class="headline-value">${Number.isFinite(currentPrice) ? `${this._format(currentPrice, 3)} PLN/kWh` : "—"}</strong></div>
          <div class="headline-item"><span class="label">${hoymilesEscape(copy.exportPower)}</span><strong class="headline-value">${Number.isFinite(exportPower) ? `${this._format(Math.abs(exportPower))} kW` : "—"}</strong></div>
          <div class="headline-item"><span class="label">${hoymilesEscape(copy.gain)}</span><strong class="headline-value">${Number.isFinite(gain) ? `${this._format(gain)} PLN` : "—"}</strong></div>
        </div>
        <div class="periods">${periods.map((period) => `<button class="period" type="button" data-entity="${hoymilesEscape(period.revenue)}"><span class="period-title">${hoymilesEscape(period.label)}</span><strong class="revenue">${Number.isFinite(period.revenue) ? `${this._format(period.revenue)} PLN` : "—"}</strong><span class="detail">${hoymilesEscape(copy.export)}: <strong>${Number.isFinite(period.energy) ? `${this._format(period.energy)} kWh` : "—"}</strong><br>${hoymilesEscape(copy.average)}: <strong>${Number.isFinite(period.average) ? `${this._format(period.average, 3)} PLN/kWh` : "—"}</strong></span></button>`).join("")}</div>
        <div class="lifetime"><span class="lifetime-label">${hoymilesEscape(copy.lifetime)}</span><span class="lifetime-values"><span>${Number.isFinite(totalRevenue) ? `${this._format(totalRevenue)} PLN` : "—"}</span><span>${Number.isFinite(totalExport) ? `${this._format(totalExport)} kWh` : "—"}</span></span></div>
      </ha-card>`;
    this.shadowRoot.querySelectorAll("[data-entity]").forEach((button) => button.addEventListener("click", () => hoymilesDispatchMoreInfo(this, button.dataset.entity)));
  }

  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, rows: 6, min_columns: 6 }; }
}

if (!customElements.get("hoymiles-aurora-finance-card")) {
  customElements.define("hoymiles-aurora-finance-card", HoymilesAuroraFinanceCard);
}
if (!window.customCards.some((card) => card.type === "hoymiles-aurora-finance-card")) {
  window.customCards.push({ type: "hoymiles-aurora-finance-card", name: "Hoymiles Aurora Finance", description: "Premium RCE export and revenue summary.", preview: false });
}

const HOYMILES_SUPERVISOR_BINDINGS = Object.freeze({
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

const HOYMILES_SUPERVISOR_MODE_OPTIONS = Object.freeze(["Off", "Shadow"]);
const HOYMILES_SUPERVISOR_PROFILE_OPTIONS = Object.freeze([
  "Balanced",
  "Maximum Profit",
  "High Reserve — Winter",
]);
const HOYMILES_SUPERVISOR_CONTROL_KINDS = Object.freeze({
  mode: Object.freeze({
    type: "select",
    configKey: "supervisor_mode_entity",
    entityId: "input_select.hoymiles_ems_supervisor_mode",
    options: HOYMILES_SUPERVISOR_MODE_OPTIONS,
  }),
  profile: Object.freeze({
    type: "select",
    configKey: "supervisor_profile_entity",
    entityId: "input_select.hoymiles_ems_supervisor_profile",
    options: HOYMILES_SUPERVISOR_PROFILE_OPTIONS,
  }),
  allowRce: Object.freeze({
    type: "boolean",
    configKey: "supervisor_allow_rce_entity",
    entityId: "input_boolean.hoymiles_ems_supervisor_allow_rce",
  }),
  allowTariff: Object.freeze({
    type: "boolean",
    configKey: "supervisor_allow_tariff_entity",
    entityId: "input_boolean.hoymiles_ems_supervisor_allow_tariff",
  }),
  allowRcm: Object.freeze({
    type: "boolean",
    configKey: "supervisor_allow_rcm_entity",
    entityId: "input_boolean.hoymiles_ems_supervisor_allow_rcm",
  }),
});
const HOYMILES_SUPERVISOR_POLICY_IDS = Object.freeze([
  "rce",
  "tariff",
  "rcm",
]);

const HOYMILES_SUPERVISOR_REASON_COPY = Object.freeze({
  candidate_ready: Object.freeze({
    pl: "Kandydat gotowy",
    en: "Candidate ready",
  }),
  live_emergency: Object.freeze({
    pl: "Pilna reakcja na stan bieżący",
    en: "Live emergency response",
  }),
  required_energy_restore: Object.freeze({
    pl: "Wymagane odtworzenie energii",
    en: "Required energy restoration",
  }),
  preventive_voltage_action: Object.freeze({
    pl: "Prewencyjna reakcja napięciowa",
    en: "Preventive voltage action",
  }),
  economic_candidate: Object.freeze({
    pl: "Kandydat ekonomiczny",
    en: "Economic candidate",
  }),
  no_action: Object.freeze({
    pl: "Brak wymaganej akcji",
    en: "No action required",
  }),
  no_eligible_candidate: Object.freeze({
    pl: "Brak kwalifikującej się polityki",
    en: "No eligible policy",
  }),
  not_allowed: Object.freeze({
    pl: "Brak zgody użytkownika",
    en: "Not allowed by user",
  }),
  policy_disabled: Object.freeze({
    pl: "Istniejąca automatyka wyłączona",
    en: "Existing automation disabled",
  }),
  unavailable: Object.freeze({
    pl: "Dane niedostępne",
    en: "Data unavailable",
  }),
  stale_candidate: Object.freeze({
    pl: "Dane kandydata są nieaktualne",
    en: "Candidate data is stale",
  }),
  future_candidate: Object.freeze({
    pl: "Dane kandydata pochodzą z przyszłości",
    en: "Candidate data is future-dated",
  }),
  not_started: Object.freeze({
    pl: "Okno jeszcze się nie rozpoczęło",
    en: "Window has not started",
  }),
  result_not_current: Object.freeze({
    pl: "Plan nie jest aktualny",
    en: "Plan is not current",
  }),
  recalculation_pending_new_start: Object.freeze({
    pl: "Oczekiwanie na przeliczenie przed nowym startem",
    en: "Waiting for recalculation before a new start",
  }),
  expired: Object.freeze({
    pl: "Okno wygasło",
    en: "Window expired",
  }),
  invalid_input: Object.freeze({
    pl: "Nieprawidłowe dane wejściowe",
    en: "Invalid input",
  }),
  invalid_policy_shape: Object.freeze({
    pl: "Nieprawidłowa struktura polityki",
    en: "Invalid policy structure",
  }),
  invalid_action_scope: Object.freeze({
    pl: "Nieprawidłowy zakres akcji",
    en: "Invalid action scope",
  }),
  actuator_unavailable: Object.freeze({
    pl: "Wymagany element wykonawczy niedostępny",
    en: "Required actuator unavailable",
  }),
  direction_unavailable: Object.freeze({
    pl: "Kierunek działania niedostępny",
    en: "Action direction unavailable",
  }),
  confirmed_zero_export: Object.freeze({
    pl: "Potwierdzony zerowy eksport",
    en: "Confirmed zero export",
  }),
  export_prohibited: Object.freeze({
    pl: "Eksport zabroniony",
    en: "Export prohibited",
  }),
  export_unverified: Object.freeze({
    pl: "Eksport niezweryfikowany",
    en: "Export unverified",
  }),
  local_hard_stop: Object.freeze({
    pl: "Lokalna blokada bezpieczeństwa",
    en: "Local hard stop",
  }),
  not_start_eligible: Object.freeze({
    pl: "Warunki startu niespełnione",
    en: "Start conditions not met",
  }),
  not_continuation_eligible: Object.freeze({
    pl: "Warunki kontynuacji niespełnione",
    en: "Continuation conditions not met",
  }),
  external_authority: Object.freeze({
    pl: "Zewnętrzna automatyka ma pierwszeństwo",
    en: "External authority has control",
  }),
  manual_authority: Object.freeze({
    pl: "Sterowanie ręczne ma pierwszeństwo",
    en: "Manual authority has control",
  }),
  off_grid: Object.freeze({
    pl: "Tryb Off-Grid ma pierwszeństwo",
    en: "Off-grid has priority",
  }),
  foreign_owner: Object.freeze({
    pl: "Steruje inny właściciel",
    en: "Another owner has control",
  }),
  balancing_active: Object.freeze({
    pl: "Trwa wyrównywanie baterii",
    en: "Battery balancing is active",
  }),
  owner_conflict: Object.freeze({
    pl: "Konflikt właściciela sterowania",
    en: "Control-owner conflict",
  }),
  transaction_pending: Object.freeze({
    pl: "Oczekiwanie na zakończenie transakcji",
    en: "Waiting for transaction completion",
  }),
  physical_mode_stale: Object.freeze({
    pl: "Fizyczny tryb jest nieaktualny",
    en: "Physical mode is stale",
  }),
  physical_mode_unknown: Object.freeze({
    pl: "Fizyczny tryb jest nieznany",
    en: "Physical mode is unknown",
  }),
  critical_bms_unavailable: Object.freeze({
    pl: "Krytyczne dane BMS niedostępne",
    en: "Critical BMS data unavailable",
  }),
  economic_candidates_not_comparable: Object.freeze({
    pl: "Kandydatów ekonomicznych nie można porównać",
    en: "Economic candidates are not comparable",
  }),
  economic_tie: Object.freeze({
    pl: "Remis kandydatów ekonomicznych",
    en: "Economic candidates are tied",
  }),
  active_not_implemented: Object.freeze({
    pl: "Tryb Active nie jest zaimplementowany",
    en: "Active mode is not implemented",
  }),
  structurally_inconsistent_context: Object.freeze({
    pl: "Niespójny kontekst wykonania",
    en: "Structurally inconsistent execution context",
  }),
  invalid_pending_owner_relationship: Object.freeze({
    pl: "Nieprawidłowa relacja oczekującej transakcji z właścicielem",
    en: "Invalid pending-transaction owner relationship",
  }),
  multiple_active_commitments: Object.freeze({
    pl: "Wiele aktywnych zobowiązań",
    en: "Multiple active commitments",
  }),
  owner_commitment_mismatch: Object.freeze({
    pl: "Właściciel nie odpowiada aktywnemu zobowiązaniu",
    en: "Owner does not match the active commitment",
  }),
  inconsistent_priority_tie: Object.freeze({
    pl: "Niespójny remis priorytetów",
    en: "Inconsistent priority tie",
  }),
});

const HOYMILES_SUPERVISOR_COPY = Object.freeze({
  pl: Object.freeze({
    title: "Nadzorca EMS",
    observationOnly: "Tylko obserwacja",
    heroIntro:
      "Nadzorca EMS porównuje dostępne automatyki i pokazuje, która z nich byłaby najlepsza w danej chwili. W obecnej wersji działa wyłącznie obserwacyjnie i nie steruje falownikiem.",
    currentMode: "Bieżący tryb",
    currentProfile: "Bieżący profil",
    currentSupervisorState: "Stan Nadzorcy",
    liveControls: "Ustawienia Nadzorcy",
    currentDecision: "Bieżąca decyzja",
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
    legacyExecution: "Istniejąca automatyka",
    existingAutomation: "Istniejąca automatyka",
    notAuthorizedObservation: "Niedozwolone — tryb obserwacyjny",
    legacyUnchanged: "Bez zmian",
    howItWorksTitle: "Jak to działa",
    howItWorksIntro:
      "Nadzorca tworzy wynik obserwacyjny w pięciu prostych krokach.",
    flowPolicies: "RCE / tanie ładowanie / RCEm",
    flowValidation: "Sprawdzenie danych i warunków bezpieczeństwa",
    flowComparison: "Porównanie priorytetu, potrzeby i opłacalności",
    flowWinner: "Logiczny zwycięzca",
    flowObservation: "Wynik obserwacyjny — bez sterowania",
    modesTitle: "Tryby",
    modeOffTitle: "OFF",
    modeOffDescription:
      "Nadzorca jest wyłączony. Istniejące automatyki działają dokładnie tak jak wcześniej.",
    modeShadowTitle: "SHADOW",
    modeShadowDescription:
      "Nadzorca analizuje dostępne automatyki, wybiera teoretycznego zwycięzcę i pokazuje powód decyzji. Nie zatrzymuje, nie uruchamia i nie przełącza żadnej automatyki.",
    modeActiveTitle: "ACTIVE — PRZYSZŁY / NIEDOSTĘPNY",
    modeActiveDescription:
      "Tryb planowany na późniejszy etap. Nie jest dostępny w tej wersji i nie można go wybrać.",
    profilesTitle: "Profile",
    balancedTitle: "BALANCED",
    balancedDescription:
      "Profil ogólnego przeznaczenia, który równoważy wynik ekonomiczny i rezerwę energii.",
    maximumProfitTitle: "MAXIMUM PROFIT",
    maximumProfitDescription: "Planowany nacisk na wynik ekonomiczny.",
    highReserveTitle: "HIGH RESERVE — WINTER",
    highReserveDescription:
      "Planowany nacisk na wyższą rezerwę baterii.",
    permissionsTitle: "Co oznaczają zgody",
    permissionMeaning:
      "Zgoda Nadzorcy oznacza: „ta automatyka może być uwzględniona w porównaniu Shadow”.",
    permissionNotMeaning:
      "Zgoda nie włącza istniejącej automatyki, nie rozpoczyna ładowania ani rozładowania, nie zmienia ustawień falownika i nie daje fizycznej władzy.",
    rcePlain:
      "RCE ocenia plan sprzedaży energii na podstawie cen rynkowych. Zgoda pozwala tylko uwzględnić jego wynik w porównaniu Shadow.",
    tariffPlain:
      "Tanie ładowanie ocenia, czy zaplanować ładowanie baterii w tańszych godzinach taryfy. Zgoda tylko dopuszcza ten wynik do porównania.",
    rcemPlain:
      "RCEm analizuje potrzebę ograniczenia eksportu przy podwyższonym napięciu sieci. Zgoda tylko dopuszcza ten wynik do porównania.",
    permissionDoesNotEnable: "Nie włącza istniejącej automatyki",
    permissionDoesNotStart: "Nie uruchamia ładowania ani rozładowania",
    permissionDoesNotWrite: "Nie zmienia ustawień falownika",
    permissionDoesNotGrant: "Nie przyznaje fizycznego sterowania",
    readResultTitle: "Jak czytać wynik",
    resultIdleTitle: "Shadow idle",
    resultIdleDescription:
      "Brak obecnie kandydata, którego Nadzorca mógłby logicznie wybrać. Nic nie jest uruchamiane.",
    resultSelectedTitle: "Shadow selected — RCE",
    resultSelectedDescription:
      "RCE jest logicznym zwycięzcą porównania, ale żadna automatyka nie została fizycznie uruchomiona.",
    resultBlockedTitle: "Zablokowany",
    resultBlockedDescription:
      "Dane, tryb, warunki bezpieczeństwa lub stan sterowania uniemożliwiają wiarygodną decyzję.",
    resultUnavailableTitle: "Niedostępny",
    resultUnavailableDescription:
      "Brakuje wymaganych danych źródłowych albo są one niespójne.",
    resultCommitmentTitle: "Trwająca decyzja istniejącej automatyki",
    resultCommitmentDescription:
      "Jedna z istniejących automatyk już działa lub utrzymuje rozpoczętą decyzję. Nadzorca tylko to pokazuje.",
    safetyTitle: "Bezpieczeństwo i ograniczenia",
    safetyIntro: "Obecny Nadzorca nie może:",
    cannotModbus: "zapisywać rejestrów Modbus ani innych ustawień falownika;",
    cannotMode: "zmieniać trybu pracy falownika;",
    cannotOwner: "przejmować sterowania;",
    cannotGrant: "udzielać zgody na fizyczne wykonanie;",
    cannotHandover: "przekazywać sterowania między automatykami;",
    cannotLegacy: "uruchamiać ani zatrzymywać istniejących automatyk;",
    cannotActive: "wykonywać trybu Active.",
    safetyScope:
      "Odczytuje ograniczone podsumowania i steruje wyłącznie pięcioma ustawieniami własnego interfejsu.",
    technicalTitle: "Szczegóły techniczne",
    arbitrationRevision: "Rewizja arbitrażu",
    executionPhase: "Faza wykonania",
    reasonCode: "Kod powodu",
    profileEffects: "Zastosowane skutki profilu",
    candidateRevisions: "Rewizje kandydatów",
    notApplied: "Nie zastosowano",
    dataOrPlan: "Dane lub plan",
    readiness: "Gotowość",
    supervisorSelection: "Wybór Nadzorcy",
    requestedAction: "Żądana akcja",
    actionWarning:
      "Żądana akcja opisuje wynik logiczny. Nie potwierdza fizycznego wykonania.",
    available: "dostępna",
    notAvailable: "niedostępna",
    upToDate: "aktualne",
    outOfDate: "nieaktualne",
    notReady: "niegotowe",
    notSelected: "niewybrane",
    none: "Brak",
    unavailable: "Niedostępny",
    unknownReason: "Nieznany powód",
    off: "Wyłączony",
    shadow: "Obserwacja",
    balanced: "Zrównoważony",
    maximumProfit: "Maksymalny zysk",
    highReserveWinter: "Wysoka rezerwa — zima",
    shadowIdle: "Obserwacja — brak wyboru",
    shadowSelected: "Obserwacja — wybrano politykę",
    blocked: "Zablokowany",
    idle: "Bezczynna",
    observedActiveLatched: "Zaobserwowane aktywne zobowiązanie",
    phaseBlocked: "Zablokowana",
    rce: "RCE",
    tariff: "Tanie ładowanie",
    rcm: "RCEm",
    actionNone: "Brak",
    actionRceExport: "Eksport RCE",
    actionTariffCharge: "Tanie ładowanie",
    actionRcmAbsorbPv: "Absorpcja PV RCEm",
    actionRcmLimitExport: "Ograniczenie eksportu RCEm",
    actionRcmPreDischarge: "Wstępne rozładowanie RCEm",
    unknownAction: "Nieznana akcja",
    selected: "Wybrane",
    ready: "Gotowe",
    noNeed: "Brak potrzeby",
    notAllowed: "Niedozwolone",
    automationDisabled: "Automatyka wyłączona",
    dataUnavailable: "Dane niedostępne",
    waitingCurrentPlan: "Oczekiwanie na aktualny plan",
    candidateBlocked: "Zablokowane",
    activeCommitment: "Aktywne zobowiązanie",
    permission: "Zgoda",
    automation: "Automatyka",
    dataPlan: "Dane/plan",
    startReadiness: "Start",
    continuationReadiness: "Kontynuacja",
    reason: "Powód",
    action: "Akcja",
    yes: "tak",
    no: "nie",
    enabled: "wł.",
    disabled: "wył.",
    current: "gotowe",
    waiting: "oczekiwanie",
    unverified: "Niepotwierdzone",
    authorizedFalse: "Niedozwolone — tryb obserwacyjny",
    authorizationUnverified:
      "Niepotwierdzone — panel pozostaje tylko do obserwacji",
    unchanged: "Bez zmian",
    profileLimitation:
      "Profile wpływają obecnie wyłącznie na decyzję obserwacyjną, a ich skutki fizyczne nie są jeszcze stosowane.",
    controlUnavailable: "Sterowanie niedostępne",
    serviceError: "Nie udało się zapisać ustawienia",
    physicalAuthority: "Nadzorca nie ma fizycznej władzy nad falownikiem.",
    physicalControl: "Sterowanie fizyczne",
    physicalControlValue: "Nie — tylko obserwacja",
    noInverterControl: "Bez sterowania falownikiem",
    heroOffResult:
      "Nadzorca jest wyłączony. Istniejące automatyki działają bez zmian.",
    heroIdleResult:
      "Nadzorca analizuje sytuację. Obecnie nie ma logicznego zwycięzcy.",
    heroSelectedResult:
      "Logicznie wybrano: {policy}. Nic nie zostało fizycznie uruchomione.",
    heroBlockedResult:
      "Nie można obecnie wydać wiarygodnej decyzji. Zobacz powód poniżej.",
    heroUnavailableResult: "Brakuje danych potrzebnych do oceny.",
    permissionContext:
      "Zgoda pozwala tylko uwzględnić wynik w analizie Shadow.",
    logicallySelected: "Wybrane logicznie",
    notConsidered: "Poza analizą",
    knowledgeTitle: "Dowiedz się, jak działa Nadzorca",
    modesProfilesDetailsTitle: "Tryby i profile",
    modesProfilesDetailsHint: "OFF, Shadow oraz znaczenie profilu.",
    permissionsDetailsHint: "Co zgoda dopuszcza do analizy Shadow.",
    resultDetailsHint: "Znaczenie stanów i logicznego wyboru.",
    safetyDetailsHint: "Granice sterowania i bezpieczny zakres.",
    technicalDetailsHint: "Ograniczone dane diagnostyczne decyzji.",
    safetyStrip:
      "Nadzorca nie steruje falownikiem. Pokazuje wyłącznie wynik analizy i może zmieniać tylko pięć ustawień własnego interfejsu.",
  }),
  en: Object.freeze({
    title: "EMS Supervisor",
    observationOnly: "Observation only",
    heroIntro:
      "EMS Supervisor compares the available automations and shows which one would be the best choice at a given moment. In this version it works in observation mode only and does not control the inverter.",
    currentMode: "Current mode",
    currentProfile: "Current profile",
    currentSupervisorState: "Supervisor state",
    liveControls: "Supervisor controls",
    currentDecision: "Current decision",
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
    legacyExecution: "Existing automation",
    existingAutomation: "Existing automation",
    notAuthorizedObservation: "Not authorized — observation mode",
    legacyUnchanged: "Unchanged",
    howItWorksTitle: "How it works",
    howItWorksIntro:
      "The Supervisor produces an observation-only result in five simple steps.",
    flowPolicies: "RCE / tariff charging / RCEm",
    flowValidation: "Validation of data and safety conditions",
    flowComparison: "Comparison of priority, need, and economics",
    flowWinner: "Logical winner",
    flowObservation: "Observation-only result — no control",
    modesTitle: "Modes",
    modeOffTitle: "OFF",
    modeOffDescription:
      "The Supervisor is off. Existing automations work exactly as before.",
    modeShadowTitle: "SHADOW",
    modeShadowDescription:
      "The Supervisor analyzes the available automations, chooses a theoretical winner, and shows the reason for the decision. It does not stop, start, or switch any automation.",
    modeActiveTitle: "ACTIVE — FUTURE / UNAVAILABLE",
    modeActiveDescription:
      "A mode planned for a later phase. It is not available in this version and cannot be selected.",
    profilesTitle: "Profiles",
    balancedTitle: "BALANCED",
    balancedDescription:
      "General-purpose profile balancing economic value and reserve.",
    maximumProfitTitle: "MAXIMUM PROFIT",
    maximumProfitDescription: "Future intended emphasis on economic result.",
    highReserveTitle: "HIGH RESERVE — WINTER",
    highReserveDescription:
      "Future intended emphasis on higher battery reserve.",
    permissionsTitle: "What the permissions mean",
    permissionMeaning:
      "A Supervisor permission means: “this policy may be considered in the Shadow comparison”.",
    permissionNotMeaning:
      "Permission does not enable the existing automation, start charging or discharging, change an inverter setting, or grant physical authority.",
    rcePlain:
      "RCE evaluates an energy-sale plan using market prices. Permission only allows its result to be considered in the Shadow comparison.",
    tariffPlain:
      "Tariff charging evaluates whether battery charging should be planned for lower-price tariff hours. Permission only admits that result to the comparison.",
    rcemPlain:
      "RCEm analyzes whether export should be limited when grid voltage is elevated. Permission only admits that result to the comparison.",
    permissionDoesNotEnable: "Does not enable the existing automation",
    permissionDoesNotStart: "Does not start charging or discharging",
    permissionDoesNotWrite: "Does not change inverter settings",
    permissionDoesNotGrant: "Does not grant physical control",
    readResultTitle: "How to read the result",
    resultIdleTitle: "Shadow idle",
    resultIdleDescription:
      "There is currently no candidate the Supervisor can logically select. Nothing is started.",
    resultSelectedTitle: "Shadow selected — RCE",
    resultSelectedDescription:
      "RCE is the logical winner, but nothing was physically started.",
    resultBlockedTitle: "Blocked",
    resultBlockedDescription:
      "Data, mode, safety, or control-state facts prevent a valid decision.",
    resultUnavailableTitle: "Unavailable",
    resultUnavailableDescription:
      "Required source data is missing or inconsistent.",
    resultCommitmentTitle: "Active commitment",
    resultCommitmentDescription:
      "An existing legacy automation is already running or latched; the Supervisor only reports it.",
    safetyTitle: "Safety and limitations",
    safetyIntro: "The current Supervisor cannot:",
    cannotModbus: "write Modbus registers or other inverter settings;",
    cannotMode: "change inverter Mode;",
    cannotOwner: "acquire control ownership;",
    cannotGrant: "grant physical execution;",
    cannotHandover: "hand over control between automations;",
    cannotLegacy: "start or stop existing automations;",
    cannotActive: "perform Active execution.",
    safetyScope:
      "It reads bounded summaries and controls only its five UI settings.",
    technicalTitle: "Technical details",
    arbitrationRevision: "Arbitration revision",
    executionPhase: "Execution phase",
    reasonCode: "Reason code",
    profileEffects: "Profile effects applied",
    candidateRevisions: "Candidate revisions",
    notApplied: "Not applied",
    dataOrPlan: "Data or plan",
    readiness: "Readiness",
    supervisorSelection: "Supervisor selection",
    requestedAction: "Requested action",
    actionWarning:
      "Requested action describes a logical result. It does not confirm physical execution.",
    available: "available",
    notAvailable: "unavailable",
    upToDate: "current",
    outOfDate: "stale",
    notReady: "not ready",
    notSelected: "not selected",
    none: "None",
    unavailable: "Unavailable",
    unknownReason: "Unknown reason",
    off: "Off",
    shadow: "Shadow",
    balanced: "Balanced",
    maximumProfit: "Maximum Profit",
    highReserveWinter: "High Reserve — Winter",
    shadowIdle: "Observation — no selection",
    shadowSelected: "Observation — policy selected",
    blocked: "Blocked",
    idle: "Idle",
    observedActiveLatched: "Observed active commitment",
    phaseBlocked: "Blocked",
    rce: "RCE",
    tariff: "Tariff charging",
    rcm: "RCEm",
    actionNone: "None",
    actionRceExport: "RCE export",
    actionTariffCharge: "Tariff charging",
    actionRcmAbsorbPv: "RCEm PV absorption",
    actionRcmLimitExport: "RCEm export limiting",
    actionRcmPreDischarge: "RCEm pre-discharge",
    unknownAction: "Unknown action",
    selected: "Selected",
    ready: "Ready",
    noNeed: "No need",
    notAllowed: "Not allowed",
    automationDisabled: "Automation disabled",
    dataUnavailable: "Data unavailable",
    waitingCurrentPlan: "Waiting for a current plan",
    candidateBlocked: "Blocked",
    activeCommitment: "Active commitment",
    permission: "Permission",
    automation: "Automation",
    dataPlan: "Data/plan",
    startReadiness: "Start",
    continuationReadiness: "Continuation",
    reason: "Reason",
    action: "Action",
    yes: "yes",
    no: "no",
    enabled: "on",
    disabled: "off",
    current: "ready",
    waiting: "waiting",
    unverified: "Unverified",
    authorizedFalse: "Not authorized — observation mode",
    authorizationUnverified:
      "Unverified — panel remains observation only",
    unchanged: "Unchanged",
    profileLimitation:
      "Profiles currently affect only the observation decision and their physical effects are not yet applied.",
    controlUnavailable: "Control unavailable",
    serviceError: "Could not save the setting",
    physicalAuthority: "The Supervisor has no physical authority over the inverter.",
    physicalControl: "Physical control",
    physicalControlValue: "No — observation only",
    noInverterControl: "No inverter control",
    heroOffResult:
      "The Supervisor is off. Existing automations continue to work unchanged.",
    heroIdleResult:
      "The Supervisor is analyzing the situation. There is currently no logical winner.",
    heroSelectedResult:
      "Logically selected: {policy}. Nothing was physically started.",
    heroBlockedResult:
      "A reliable decision cannot currently be made. See the reason below.",
    heroUnavailableResult: "Data required for evaluation is unavailable.",
    permissionContext:
      "Permission only allows the result to be considered in Shadow analysis.",
    logicallySelected: "Logically selected",
    notConsidered: "Not considered",
    knowledgeTitle: "Learn how the Supervisor works",
    modesProfilesDetailsTitle: "Modes and profiles",
    modesProfilesDetailsHint: "OFF, Shadow, and what the profile means.",
    permissionsDetailsHint: "What permission admits to Shadow analysis.",
    resultDetailsHint: "Meaning of states and logical selection.",
    safetyDetailsHint: "Control boundaries and safe scope.",
    technicalDetailsHint: "Bounded decision diagnostics.",
    safetyStrip:
      "The Supervisor does not control the inverter. It only shows the analysis result and can change only its five UI settings.",
  }),
});

const HOYMILES_SUPERVISOR_POLICY_ICONS = Object.freeze({
  rce: "mdi:chart-line",
  tariff: "mdi:battery-clock-outline",
  rcm: "mdi:transmission-tower",
});

function hoymilesSupervisorIsRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hoymilesSupervisorReason(code, language) {
  if (typeof code !== "string" || !code) {
    return HOYMILES_SUPERVISOR_COPY[language].none;
  }
  return (
    HOYMILES_SUPERVISOR_REASON_COPY[code]?.[language] ||
    HOYMILES_SUPERVISOR_COPY[language].unknownReason
  );
}

function hoymilesNormalizeSupervisor(sensor) {
  const unavailableStates = new Set(["", "unknown", "unavailable"]);
  const rawState = String(sensor?.state ?? "").trim().toLowerCase();
  const attributes = hoymilesSupervisorIsRecord(sensor?.attributes)
    ? sensor.attributes
    : null;
  const candidateInput = attributes?.candidate_summaries;
  const collectionValid =
    Array.isArray(candidateInput) && candidateInput.length <= 3;
  const candidates = new Map();
  const duplicates = new Set();
  if (collectionValid) {
    for (const candidate of candidateInput) {
      if (!hoymilesSupervisorIsRecord(candidate)) continue;
      const policyId = candidate.policy_id;
      if (!HOYMILES_SUPERVISOR_POLICY_IDS.includes(policyId)) continue;
      if (candidates.has(policyId)) {
        duplicates.add(policyId);
        candidates.delete(policyId);
      } else if (!duplicates.has(policyId)) {
        candidates.set(policyId, candidate);
      }
    }
  }
  for (const policyId of duplicates) candidates.delete(policyId);

  const requestedSelection = HOYMILES_SUPERVISOR_POLICY_IDS.includes(
    attributes?.selected_policy
  )
    ? attributes.selected_policy
    : null;
  const selectedPolicy =
    rawState === "shadow_selected" &&
    requestedSelection &&
    candidates.has(requestedSelection)
      ? requestedSelection
      : null;
  const knownState = ["off", "shadow_idle", "shadow_selected", "blocked"].includes(
    rawState
  );
  const selectionConsistent =
    rawState !== "shadow_selected" || selectedPolicy !== null;
  const malformed =
    !sensor ||
    unavailableStates.has(rawState) ||
    !knownState ||
    !attributes ||
    !collectionValid ||
    !selectionConsistent;
  const state = malformed ? "unavailable" : rawState;
  const tone =
    state === "off"
      ? "off"
      : state === "shadow_idle"
        ? "shadow-idle"
        : state === "shadow_selected"
          ? "shadow-selected"
          : state === "blocked"
            ? "blocked"
            : "unavailable";
  return {
    state,
    tone,
    attributes: attributes || {},
    candidates: HOYMILES_SUPERVISOR_POLICY_IDS.map((policyId) => ({
      policyId,
      candidate:
        collectionValid && !duplicates.has(policyId)
          ? candidates.get(policyId) || null
          : null,
    })),
    selectedPolicy,
    selectionReason:
      typeof attributes?.selection_reason === "string"
        ? attributes.selection_reason
        : null,
    blockedReason:
      typeof attributes?.execution_blocked_reason === "string"
        ? attributes.execution_blocked_reason
        : null,
    phase:
      typeof attributes?.execution_phase === "string"
        ? attributes.execution_phase
        : null,
    authorizationFalse: attributes?.supervisor_execution_authorized === false,
    legacyUnchanged: attributes?.legacy_execution_unchanged === true,
    profileEffectsVerified:
      Array.isArray(attributes?.profile_effects_applied) &&
      attributes.profile_effects_applied.length === 0,
  };
}

const HOYMILES_EMS_SUPERVISOR_CSS = `
  :host {
    container-type: inline-size;
    display: block;
    /* SUPERVISOR_SEMANTIC_PALETTE_REV28 — the only raw color block. */
    --supervisor-cyan: #43d5ff;
    --supervisor-blue: #4c91ff;
    --supervisor-violet: #9b7cff;
    --supervisor-rce: #f2b84b;
    --supervisor-tariff: #49a5ff;
    --supervisor-rcm: #b07cff;
    --supervisor-ready: #47df91;
    --supervisor-warning: #f1b84b;
    --supervisor-error: #ff647c;
    --supervisor-deep: #081425;
    --supervisor-neutral: #8491a6;
    --supervisor-on-deep: #f5f9ff;
    --hoymiles-aurora-accent: var(--supervisor-cyan);
    --supervisor-tone: var(--supervisor-neutral);
    --supervisor-tone-secondary: var(--supervisor-blue);
    --supervisor-muted: var(--hoymiles-aurora-muted, var(--secondary-text-color));
    --supervisor-base: var(--card-background-color, var(--primary-background-color));
    --supervisor-page-base: color-mix(in srgb, var(--primary-background-color, var(--supervisor-deep)) 91%, var(--supervisor-blue) 9%);
    --supervisor-page-cyan-glow: color-mix(in srgb, var(--supervisor-cyan) 13%, transparent);
    --supervisor-page-violet-glow: color-mix(in srgb, var(--supervisor-violet) 11%, transparent);
    --supervisor-surface: color-mix(in srgb, var(--supervisor-base) 91%, var(--supervisor-blue) 9%);
    --supervisor-surface-strong: color-mix(in srgb, var(--supervisor-base) 95%, var(--supervisor-cyan) 5%);
    --supervisor-border: color-mix(in srgb, var(--supervisor-tone) 30%, var(--divider-color));
    --supervisor-soft: color-mix(in srgb, var(--supervisor-tone) 12%, transparent);
  }
  * { box-sizing: border-box; }
  ha-card {
    background:
      radial-gradient(circle at 12% 0%, var(--supervisor-page-cyan-glow), transparent 34%),
      radial-gradient(circle at 88% 3%, var(--supervisor-page-violet-glow), transparent 38%),
      linear-gradient(155deg, var(--supervisor-page-base), color-mix(in srgb, var(--supervisor-page-base) 93%, var(--supervisor-violet) 7%));
    border: 1px solid color-mix(in srgb, var(--supervisor-cyan) 22%, var(--divider-color));
    border-radius: 24px;
    box-shadow: var(--hoymiles-aurora-shadow);
    color: var(--hoymiles-aurora-text);
    display: block;
    overflow: clip;
  }
  .ems-supervisor,
  .supervisor-panel {
    max-width: 100%;
    min-width: 0;
    width: 100%;
  }
  .supervisor-panel {
    isolation: isolate;
    overflow: clip;
    position: relative;
  }
  .supervisor-panel[data-tone="shadow-idle"] {
    --supervisor-tone: var(--supervisor-cyan);
    --supervisor-tone-secondary: var(--supervisor-blue);
  }
  .supervisor-panel[data-tone="shadow-selected"] {
    --supervisor-tone: var(--supervisor-violet);
    --supervisor-tone-secondary: var(--supervisor-cyan);
  }
  .supervisor-panel[data-tone="blocked"] {
    --supervisor-tone: var(--supervisor-warning);
    --supervisor-tone-secondary: var(--supervisor-rce);
  }
  .supervisor-panel[data-tone="unavailable"] {
    --supervisor-tone: var(--supervisor-error);
    --supervisor-tone-secondary: var(--supervisor-error);
  }
  .supervisor-backdrop,
  .supervisor-blob,
  .supervisor-points,
  .supervisor-vignette {
    inset: 0;
    pointer-events: none;
    position: absolute;
  }
  .supervisor-backdrop { overflow: hidden; z-index: 0; }
  .supervisor-blob {
    height: 62%;
    opacity: .2;
    width: 62%;
    will-change: transform, opacity;
  }
  .supervisor-blob-one {
    animation: supervisor-aurora-one 24s ease-in-out infinite alternate;
    background: radial-gradient(circle at 45% 45%, color-mix(in srgb, var(--supervisor-cyan) 62%, transparent), transparent 68%);
    left: -18%;
    top: -17%;
  }
  .supervisor-blob-two {
    animation: supervisor-aurora-two 28s ease-in-out infinite alternate;
    background: conic-gradient(from 90deg, transparent, color-mix(in srgb, var(--supervisor-violet) 45%, transparent), color-mix(in srgb, var(--supervisor-blue) 26%, transparent), transparent 62%);
    left: auto;
    right: -18%;
    top: 10%;
  }
  .supervisor-blob-three {
    animation: supervisor-aurora-three 22s ease-in-out infinite alternate;
    background: radial-gradient(circle, color-mix(in srgb, var(--supervisor-violet) 34%, transparent), color-mix(in srgb, var(--supervisor-cyan) 12%, transparent), transparent 70%);
    bottom: -22%;
    left: 20%;
    top: auto;
  }
  .supervisor-points {
    background-image:
      radial-gradient(circle, color-mix(in srgb, var(--supervisor-cyan) 28%, transparent) 1px, transparent 1.3px),
      linear-gradient(color-mix(in srgb, var(--supervisor-blue) 12%, transparent) 1px, transparent 1px),
      linear-gradient(90deg, color-mix(in srgb, var(--supervisor-violet) 10%, transparent) 1px, transparent 1px);
    background-position: 0 0, 0 0, 0 0;
    background-size: 26px 26px, 104px 104px, 104px 104px;
    opacity: .2;
  }
  .supervisor-vignette {
    background: radial-gradient(circle at center, transparent 42%, color-mix(in srgb, var(--supervisor-page-base) 82%, transparent));
  }
  .supervisor-content {
    display: grid;
    gap: 18px;
    margin: 0 auto;
    max-width: 1440px;
    min-width: 0;
    padding: clamp(14px, 2.4vw, 34px);
    position: relative;
    z-index: 1;
  }
  .supervisor-surface {
    background: color-mix(in srgb, var(--card-background-color, var(--ha-card-background)) 88%, transparent);
    border: 1px solid color-mix(in srgb, var(--supervisor-tone) 18%, var(--hoymiles-aurora-border));
    border-radius: 18px;
    box-shadow: var(--hoymiles-aurora-shadow);
    min-width: 0;
    padding: clamp(14px, 2vw, 22px);
  }
  .supervisor-hero {
    align-items: stretch;
    background:
      radial-gradient(circle at 95% 5%, color-mix(in srgb, var(--supervisor-tone) 15%, transparent), transparent 48%),
      color-mix(in srgb, var(--card-background-color, var(--ha-card-background)) 91%, transparent);
    display: grid;
    gap: 22px;
    grid-template-columns: minmax(0, 1.2fr) minmax(290px, .8fr);
  }
  .supervisor-heading {
    align-items: center;
    display: flex;
    gap: 10px;
  }
  .supervisor-icon {
    color: var(--supervisor-tone);
    flex: 0 0 auto;
    height: 28px;
    width: 28px;
  }
  .supervisor-title {
    color: var(--hoymiles-aurora-text);
    font-size: clamp(25px, 3vw, 40px);
    font-weight: 720;
    letter-spacing: -.035em;
    line-height: 1.05;
    margin: 0;
  }
  .supervisor-scope,
  .supervisor-policy-badge,
  .supervisor-selected-marker,
  .supervisor-limitation-status {
    align-items: center;
    border-radius: 999px;
    display: inline-flex;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .04em;
    line-height: 1.2;
    padding: 6px 10px;
  }
  .supervisor-scope {
    background: color-mix(in srgb, var(--supervisor-tone) 13%, transparent);
    border: 1px solid color-mix(in srgb, var(--supervisor-tone) 38%, transparent);
    color: color-mix(in srgb, var(--supervisor-tone) 78%, var(--hoymiles-aurora-text));
    margin-top: 16px;
  }
  .supervisor-hero-intro,
  .supervisor-section-intro,
  .supervisor-info-description,
  .supervisor-callout {
    color: var(--hoymiles-aurora-muted);
    line-height: 1.55;
  }
  .supervisor-hero-intro { font-size: 14px; margin: 16px 0 0; max-width: 72ch; }
  .supervisor-hero-state {
    display: grid;
    gap: 8px;
    margin: 0;
  }
  .supervisor-hero-state-item {
    background: color-mix(in srgb, var(--card-background-color, var(--ha-card-background)) 86%, transparent);
    border: 1px solid var(--hoymiles-aurora-border);
    border-radius: 13px;
    display: grid;
    gap: 8px;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    padding: 11px 13px;
  }
  .supervisor-hero-state-label,
  .supervisor-hero-state-value { font-size: 12px; line-height: 1.35; margin: 0; }
  .supervisor-hero-state-label { color: var(--hoymiles-aurora-muted); }
  .supervisor-hero-state-value { color: var(--hoymiles-aurora-text); font-weight: 700; text-align: right; }
  .supervisor-live-grid {
    display: grid;
    gap: 18px;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    min-width: 0;
  }
  .supervisor-section-title {
    color: var(--hoymiles-aurora-text);
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -.015em;
    margin: 0 0 14px;
  }
  .supervisor-controls,
  .supervisor-summary { display: grid; gap: 9px; margin: 0; }
  .supervisor-control,
  .supervisor-summary-row {
    align-items: center;
    display: grid;
    gap: 12px;
    grid-template-columns: minmax(150px, .85fr) minmax(0, 1.15fr);
    min-width: 0;
  }
  .supervisor-control-label,
  .supervisor-summary-label,
  .supervisor-summary-value { font-size: 12px; line-height: 1.35; margin: 0; }
  .supervisor-control-label,
  .supervisor-summary-label { color: var(--hoymiles-aurora-muted); font-weight: 620; }
  .supervisor-summary-value { color: var(--hoymiles-aurora-text); font-weight: 600; }
  .supervisor-control-field { display: grid; gap: 4px; justify-items: stretch; min-width: 0; }
  .supervisor-select {
    appearance: auto;
    background: color-mix(in srgb, var(--card-background-color, var(--ha-card-background)) 91%, var(--supervisor-tone) 4%);
    border: 1px solid color-mix(in srgb, var(--supervisor-tone) 28%, var(--divider-color));
    border-radius: 11px;
    color: var(--primary-text-color);
    font: inherit;
    min-height: 44px;
    min-width: 0;
    padding: 8px 10px;
    width: 100%;
  }
  .supervisor-select:disabled,
  .supervisor-switch:disabled { cursor: not-allowed; opacity: .55; }
  .supervisor-select:focus-visible,
  .supervisor-switch:focus-visible,
  .supervisor-technical-summary:focus-visible {
    outline: 2px solid var(--supervisor-tone);
    outline-offset: 2px;
  }
  .supervisor-switch {
    align-items: center;
    appearance: none;
    background: color-mix(in srgb, var(--hoymiles-aurora-offline) 28%, transparent);
    border: 1px solid color-mix(in srgb, var(--hoymiles-aurora-offline) 42%, var(--divider-color));
    border-radius: 999px;
    cursor: pointer;
    display: inline-flex;
    height: 44px;
    justify-self: end;
    padding: 10px 3px;
    transition: background .16s ease, border-color .16s ease;
    width: 52px;
  }
  .supervisor-switch[aria-checked="true"] {
    background: color-mix(in srgb, var(--hoymiles-aurora-accent) 50%, transparent);
    border-color: color-mix(in srgb, var(--hoymiles-aurora-accent) 70%, var(--divider-color));
  }
  .supervisor-switch-thumb {
    background: var(--primary-text-color);
    border-radius: 50%;
    box-shadow: var(--hoymiles-aurora-shadow);
    display: block;
    height: 22px;
    transform: translateX(0);
    transition: transform .16s ease;
    width: 22px;
  }
  .supervisor-switch[aria-checked="true"] .supervisor-switch-thumb { transform: translateX(22px); }
  .supervisor-control-error { color: var(--hoymiles-aurora-error); font-size: 11px; }
  .supervisor-control-error:empty { display: none; }
  .supervisor-limitation,
  .supervisor-callout {
    background: color-mix(in srgb, var(--hoymiles-aurora-accent) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--hoymiles-aurora-accent) 20%, var(--divider-color));
    border-radius: 12px;
    margin: 14px 0 0;
    padding: 11px 12px;
  }
  .supervisor-limitation { align-items: flex-start; color: var(--hoymiles-aurora-muted); display: flex; font-size: 12px; gap: 8px; line-height: 1.45; }
  .supervisor-limitation-icon { color: var(--hoymiles-aurora-accent); flex: 0 0 auto; height: 18px; width: 18px; }
  .supervisor-limitation-text { flex: 1 1 auto; min-width: 0; }
  .supervisor-limitation-status { color: var(--hoymiles-aurora-warn); flex: 0 0 auto; }
  .supervisor-policy-section { min-width: 0; }
  .supervisor-policies {
    display: grid;
    gap: 12px;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    min-width: 0;
  }
  .supervisor-policy,
  .supervisor-info-card {
    background: color-mix(in srgb, var(--card-background-color, var(--ha-card-background)) 84%, transparent);
    border: 1px solid var(--hoymiles-aurora-border);
    border-radius: 14px;
    min-width: 0;
    padding: 13px;
  }
  .supervisor-policy[data-tone="selected"] { border-color: color-mix(in srgb, var(--hoymiles-aurora-accent) 58%, var(--divider-color)); }
  .supervisor-policy[data-tone="blocked"],
  .supervisor-policy[data-tone="waiting"] { border-color: color-mix(in srgb, var(--hoymiles-aurora-warn) 46%, var(--divider-color)); }
  .supervisor-policy[data-tone="unavailable"] { border-color: color-mix(in srgb, var(--hoymiles-aurora-error) 38%, var(--divider-color)); }
  .supervisor-policy-top { align-items: start; display: flex; gap: 8px; justify-content: space-between; min-width: 0; }
  .supervisor-policy-name,
  .supervisor-info-title { color: var(--hoymiles-aurora-text); font-size: 13px; font-weight: 700; margin: 0; }
  .supervisor-policy-badges { display: flex; flex: 0 1 auto; flex-wrap: wrap; gap: 5px; justify-content: flex-end; }
  .supervisor-policy-badge { color: var(--hoymiles-aurora-muted); }
  .supervisor-selected-marker { color: var(--hoymiles-aurora-accent); }
  .supervisor-policy-facts { display: grid; gap: 6px; margin-top: 12px; }
  .supervisor-policy-fact { color: var(--hoymiles-aurora-muted); display: block; font-size: 11px; line-height: 1.4; }
  .supervisor-policy-fact strong { color: var(--hoymiles-aurora-text); font-weight: 620; }
  .supervisor-policy-action,
  .supervisor-policy-reason,
  .supervisor-policy-action-warning { font-size: 11px; line-height: 1.42; margin: 8px 0 0; overflow-wrap: anywhere; }
  .supervisor-policy-action { color: var(--hoymiles-aurora-text); font-weight: 620; }
  .supervisor-policy-reason,
  .supervisor-policy-action-warning { color: var(--hoymiles-aurora-muted); }
  .supervisor-flow {
    display: grid;
    gap: 10px;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    list-style: none;
    margin: 14px 0 0;
    padding: 0;
  }
  .supervisor-flow-step {
    align-items: center;
    background: color-mix(in srgb, var(--supervisor-tone) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--supervisor-tone) 22%, var(--divider-color));
    border-radius: 12px;
    color: var(--hoymiles-aurora-text);
    display: flex;
    font-size: 11px;
    justify-content: center;
    line-height: 1.4;
    min-height: 64px;
    padding: 9px;
    text-align: center;
  }
  .supervisor-doc-grid { display: grid; gap: 10px; grid-template-columns: repeat(2, minmax(0, 1fr)); min-width: 0; }
  .supervisor-doc-grid-three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .supervisor-info-description { font-size: 12px; margin: 8px 0 0; }
  .supervisor-info-disabled { border-style: dashed; opacity: 1; }
  .supervisor-info-disabled .supervisor-info-description { color: var(--primary-text-color); }
  .supervisor-section-intro { font-size: 13px; margin: 0 0 12px; }
  .supervisor-callout { font-size: 12px; }
  .supervisor-callout-warning { border-color: color-mix(in srgb, var(--hoymiles-aurora-warn) 32%, var(--divider-color)); }
  .supervisor-limit-list { display: grid; gap: 7px; margin: 14px 0 0; padding-left: 20px; }
  .supervisor-limit-item { color: var(--hoymiles-aurora-muted); font-size: 12px; line-height: 1.45; }
  .supervisor-technical { padding: 0; }
  .supervisor-technical-summary { color: var(--hoymiles-aurora-text); cursor: pointer; font-size: 15px; font-weight: 700; min-height: 44px; padding: 15px 18px; }
  .supervisor-technical-values { display: grid; gap: 8px; margin: 0; padding: 0 18px 18px; }
  .supervisor-technical-row { display: grid; gap: 12px; grid-template-columns: minmax(150px, .7fr) minmax(0, 1.3fr); min-width: 0; }
  .supervisor-technical-label,
  .supervisor-technical-value { font-size: 11px; line-height: 1.4; margin: 0; }
  .supervisor-technical-label { color: var(--hoymiles-aurora-muted); }
  .supervisor-technical-value { color: var(--hoymiles-aurora-text); overflow-wrap: anywhere; }
  @keyframes supervisor-aurora-one { to { opacity: .3; transform: translate3d(9%, 8%, 0) scale(1.08); } }
  @keyframes supervisor-aurora-two { to { opacity: .12; transform: translate3d(-10%, 7%, 0) rotate(12deg); } }
  @keyframes supervisor-aurora-three { to { opacity: .28; transform: translate3d(6%, -9%, 0) scale(.94); } }
  @media (prefers-reduced-motion: reduce) {
    .supervisor-blob { animation: none; }
    .supervisor-select,
    .supervisor-switch,
    .supervisor-switch-thumb { transition: none; }
  }
  @container (max-width: 900px) {
    .supervisor-hero,
    .supervisor-live-grid { grid-template-columns: minmax(0, 1fr); }
    .supervisor-policies,
    .supervisor-doc-grid-three { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .supervisor-flow { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .supervisor-flow-step:last-child { grid-column: 1 / -1; }
  }
  @container (max-width: 620px) {
    ha-card { border-radius: 18px; }
    .supervisor-content { gap: 12px; padding: 11px; }
    .supervisor-surface { border-radius: 15px; padding: 13px; }
    .supervisor-hero-state-item,
    .supervisor-control,
    .supervisor-summary-row,
    .supervisor-technical-row { grid-template-columns: minmax(0, 1fr); }
    .supervisor-hero-state-value { text-align: left; }
    .supervisor-control-field,
    .supervisor-select { width: 100%; }
    .supervisor-switch { justify-self: start; }
    .supervisor-policies,
    .supervisor-doc-grid,
    .supervisor-doc-grid-three,
    .supervisor-flow { grid-template-columns: minmax(0, 1fr); }
    .supervisor-flow-step:last-child { grid-column: auto; }
    .supervisor-policy-badges { justify-content: flex-start; }
    .supervisor-limitation { flex-wrap: wrap; }
    .supervisor-technical { padding: 0; }
  }
  @container (max-width: 390px) {
    .supervisor-content { padding: 8px; }
    .supervisor-surface { padding: 11px 10px; }
    .supervisor-title { font-size: 24px; }
    .supervisor-heading { align-items: flex-start; }
    .supervisor-policy-top { display: grid; }
    .supervisor-technical { padding: 0; }
    .supervisor-technical-summary { padding: 13px 11px; }
    .supervisor-technical-values { padding: 0 11px 13px; }
  }

  /* Revision 28 hierarchy and Aurora presentation. */
  .supervisor-surface {
    background: linear-gradient(145deg, color-mix(in srgb, var(--supervisor-surface-strong) 96%, transparent), color-mix(in srgb, var(--supervisor-surface) 91%, transparent));
    border-color: color-mix(in srgb, var(--supervisor-tone) 24%, var(--divider-color));
    box-shadow: 0 18px 48px color-mix(in srgb, var(--supervisor-deep) 18%, transparent), inset 0 1px color-mix(in srgb, var(--supervisor-cyan) 8%, transparent);
  }
  .supervisor-hero {
    background:
      radial-gradient(circle at 12% 0%, color-mix(in srgb, var(--supervisor-cyan) 15%, transparent), transparent 38%),
      radial-gradient(circle at 92% 12%, color-mix(in srgb, var(--supervisor-violet) 14%, transparent), transparent 44%),
      linear-gradient(140deg, color-mix(in srgb, var(--supervisor-surface-strong) 97%, transparent), color-mix(in srgb, var(--supervisor-surface) 91%, var(--supervisor-blue) 9%));
    gap: clamp(22px, 3vw, 42px);
    grid-template-columns: minmax(0, 1.08fr) minmax(330px, .92fr);
    min-height: 286px;
    overflow: hidden;
    padding: clamp(20px, 3vw, 38px);
    position: relative;
  }
  .supervisor-hero::before {
    background: linear-gradient(90deg, var(--supervisor-cyan), var(--supervisor-blue), var(--supervisor-violet));
    content: "";
    height: 3px;
    inset: 0 0 auto;
    opacity: .84;
    position: absolute;
  }
  .supervisor-hero-copy { align-content: center; display: grid; min-width: 0; }
  .supervisor-icon { color: var(--supervisor-tone); height: 34px; width: 34px; }
  .supervisor-scope {
    background: color-mix(in srgb, var(--supervisor-cyan) 11%, transparent);
    border-color: color-mix(in srgb, var(--supervisor-cyan) 42%, transparent);
    color: color-mix(in srgb, var(--supervisor-cyan) 72%, var(--primary-text-color));
    margin-top: 13px;
  }
  .supervisor-authority {
    align-items: center;
    background: linear-gradient(90deg, color-mix(in srgb, var(--supervisor-cyan) 14%, transparent), color-mix(in srgb, var(--supervisor-blue) 7%, transparent));
    border: 1px solid color-mix(in srgb, var(--supervisor-cyan) 42%, var(--divider-color));
    border-left: 4px solid var(--supervisor-cyan);
    border-radius: 13px;
    color: var(--primary-text-color);
    display: flex;
    font-size: 14px;
    font-weight: 720;
    gap: 9px;
    line-height: 1.45;
    margin: 18px 0 0;
    max-width: 68ch;
    padding: 12px 14px;
  }
  .supervisor-authority ha-icon,
  .supervisor-no-control ha-icon,
  .supervisor-safety-strip ha-icon {
    color: var(--supervisor-cyan);
    flex: 0 0 auto;
    height: 21px;
    width: 21px;
  }
  .supervisor-quick-facts {
    display: grid;
    gap: 8px;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 17px 0 0;
  }
  .supervisor-quick-fact {
    background: color-mix(in srgb, var(--supervisor-surface-strong) 89%, transparent);
    border: 1px solid color-mix(in srgb, var(--supervisor-blue) 19%, var(--divider-color));
    border-radius: 12px;
    display: grid;
    gap: 5px;
    min-width: 0;
    padding: 10px 11px;
  }
  .supervisor-quick-fact[data-fact="physical"] {
    background: color-mix(in srgb, var(--supervisor-cyan) 10%, var(--supervisor-surface-strong));
    border-color: color-mix(in srgb, var(--supervisor-cyan) 42%, var(--divider-color));
  }
  .supervisor-quick-fact-label,
  .supervisor-quick-fact-value { font-size: 11px; line-height: 1.35; margin: 0; }
  .supervisor-quick-fact-label { color: var(--supervisor-muted); font-weight: 620; }
  .supervisor-quick-fact-value { color: var(--primary-text-color); font-size: 12px; font-weight: 720; overflow-wrap: anywhere; }
  .supervisor-quick-fact[data-fact="physical"] .supervisor-quick-fact-value { color: color-mix(in srgb, var(--supervisor-cyan) 74%, var(--primary-text-color)); }
  .supervisor-hero-core {
    align-content: center;
    background:
      radial-gradient(circle at 50% 43%, color-mix(in srgb, var(--supervisor-tone) 20%, transparent), transparent 38%),
      linear-gradient(150deg, color-mix(in srgb, var(--supervisor-deep) 86%, var(--supervisor-blue) 14%), color-mix(in srgb, var(--supervisor-deep) 86%, var(--supervisor-violet) 14%));
    border: 1px solid color-mix(in srgb, var(--supervisor-tone) 40%, transparent);
    border-radius: 22px;
    box-shadow: 0 22px 54px color-mix(in srgb, var(--supervisor-deep) 34%, transparent), inset 0 0 36px color-mix(in srgb, var(--supervisor-tone) 8%, transparent);
    color: color-mix(in srgb, var(--supervisor-cyan) 16%, var(--supervisor-on-deep));
    display: grid;
    gap: 13px;
    min-height: 240px;
    min-width: 0;
    overflow: hidden;
    padding: 17px;
    position: relative;
  }
  .supervisor-core-diagram { align-items: center; display: grid; gap: 13px; justify-items: center; min-width: 0; position: relative; }
  .supervisor-core-inputs { display: flex; flex-wrap: wrap; gap: 7px; justify-content: center; position: relative; z-index: 2; }
  .supervisor-policy-chip {
    --policy-accent: var(--supervisor-cyan);
    align-items: center;
    background: color-mix(in srgb, var(--policy-accent) 14%, var(--supervisor-deep));
    border: 1px solid color-mix(in srgb, var(--policy-accent) 55%, transparent);
    border-radius: 999px;
    color: color-mix(in srgb, var(--policy-accent) 38%, var(--supervisor-on-deep));
    display: inline-flex;
    font-size: 11px;
    font-weight: 720;
    gap: 5px;
    letter-spacing: .025em;
    min-height: 30px;
    padding: 6px 9px;
  }
  .supervisor-policy-chip[data-policy="rce"],
  .supervisor-policy[data-policy="rce"],
  .supervisor-control[data-policy="rce"] { --policy-accent: var(--supervisor-rce); }
  .supervisor-policy-chip[data-policy="tariff"],
  .supervisor-policy[data-policy="tariff"],
  .supervisor-control[data-policy="tariff"] { --policy-accent: var(--supervisor-tariff); }
  .supervisor-policy-chip[data-policy="rcm"],
  .supervisor-policy[data-policy="rcm"],
  .supervisor-control[data-policy="rcm"] { --policy-accent: var(--supervisor-rcm); }
  .supervisor-policy-chip ha-icon { color: var(--policy-accent); height: 15px; width: 15px; }
  .supervisor-core-orb {
    align-items: center;
    background:
      radial-gradient(circle at 38% 32%, color-mix(in srgb, var(--supervisor-cyan) 56%, transparent), transparent 26%),
      linear-gradient(145deg, color-mix(in srgb, var(--supervisor-blue) 64%, var(--supervisor-deep)), color-mix(in srgb, var(--supervisor-violet) 68%, var(--supervisor-deep)));
    border: 1px solid color-mix(in srgb, var(--supervisor-cyan) 68%, transparent);
    border-radius: 50%;
    box-shadow: 0 0 28px color-mix(in srgb, var(--supervisor-tone) 34%, transparent), inset 0 0 18px color-mix(in srgb, var(--supervisor-cyan) 22%, transparent);
    display: flex;
    height: 66px;
    justify-content: center;
    position: relative;
    width: 66px;
    z-index: 2;
  }
  .supervisor-core-orb ha-icon { color: var(--supervisor-on-deep); height: 31px; width: 31px; }
  .supervisor-core-ring {
    animation: supervisor-core-drift 26s linear infinite;
    border: 1px solid color-mix(in srgb, var(--supervisor-tone) 32%, transparent);
    border-radius: 50%;
    height: 112px;
    position: absolute;
    width: 112px;
  }
  .supervisor-core-ring-two {
    animation-direction: reverse;
    animation-duration: 22s;
    border-color: color-mix(in srgb, var(--supervisor-violet) 28%, transparent);
    height: 148px;
    width: 148px;
  }
  .supervisor-hero-result {
    background: color-mix(in srgb, var(--supervisor-tone) 10%, var(--supervisor-deep));
    border: 1px solid color-mix(in srgb, var(--supervisor-tone) 44%, transparent);
    border-radius: 13px;
    display: grid;
    gap: 6px;
    padding: 10px 12px;
    position: relative;
    z-index: 2;
  }
  .supervisor-hero-result-badge {
    align-items: center;
    background: color-mix(in srgb, var(--supervisor-tone) 22%, transparent);
    border: 1px solid color-mix(in srgb, var(--supervisor-tone) 54%, transparent);
    border-radius: 999px;
    color: color-mix(in srgb, var(--supervisor-tone) 42%, var(--supervisor-on-deep));
    display: inline-flex;
    font-size: 11px;
    font-weight: 760;
    justify-self: start;
    letter-spacing: .04em;
    padding: 5px 8px;
  }
  .supervisor-hero-result-text { color: var(--supervisor-on-deep); font-size: 12px; font-weight: 620; line-height: 1.45; margin: 0; }
  .supervisor-no-control { align-items: center; color: color-mix(in srgb, var(--supervisor-cyan) 45%, var(--supervisor-on-deep)); display: flex; font-size: 11px; font-weight: 720; gap: 7px; justify-content: center; letter-spacing: .02em; }
  .supervisor-live-grid { align-items: start; }
  .supervisor-control {
    --policy-accent: var(--supervisor-cyan);
    background: color-mix(in srgb, var(--supervisor-surface-strong) 92%, transparent);
    border: 1px solid color-mix(in srgb, var(--policy-accent) 15%, var(--divider-color));
    border-radius: 12px;
    min-height: 54px;
    padding: 6px 8px 6px 11px;
  }
  .supervisor-control-label { align-items: center; color: var(--primary-text-color); display: flex; gap: 8px; }
  .supervisor-control-icon { color: var(--policy-accent); flex: 0 0 auto; height: 19px; width: 19px; }
  .supervisor-switch { --control-accent: var(--policy-accent); }
  .supervisor-switch[aria-checked="true"] {
    background: color-mix(in srgb, var(--control-accent) 56%, transparent);
    border-color: color-mix(in srgb, var(--control-accent) 76%, var(--divider-color));
    box-shadow: 0 0 16px color-mix(in srgb, var(--control-accent) 21%, transparent);
  }
  .supervisor-permission-context {
    background: color-mix(in srgb, var(--supervisor-cyan) 8%, transparent);
    border-color: color-mix(in srgb, var(--supervisor-cyan) 28%, var(--divider-color));
  }
  .supervisor-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .supervisor-summary-row {
    align-content: start;
    background: color-mix(in srgb, var(--supervisor-surface-strong) 90%, transparent);
    border: 1px solid color-mix(in srgb, var(--supervisor-blue) 14%, var(--divider-color));
    border-radius: 12px;
    display: grid;
    gap: 6px;
    grid-template-columns: minmax(0, 1fr);
    min-height: 68px;
    padding: 10px 11px;
  }
  .supervisor-summary-row[data-priority="prominent"] { border-color: color-mix(in srgb, var(--supervisor-tone) 26%, var(--divider-color)); }
  .supervisor-summary-row[data-priority="secondary"] { background: color-mix(in srgb, var(--supervisor-surface) 83%, transparent); min-height: 56px; opacity: .79; }
  .supervisor-summary-row[data-fact="decisionReason"] { grid-column: 1 / -1; }
  .supervisor-summary-row[data-fact="state"] .supervisor-summary-value,
  .supervisor-summary-row[data-fact="physicalExecution"] .supervisor-summary-value,
  .supervisor-summary-row[data-fact="selectedPolicy"] .supervisor-summary-value {
    align-items: center;
    background: color-mix(in srgb, var(--fact-accent, var(--supervisor-tone)) 14%, transparent);
    border: 1px solid color-mix(in srgb, var(--fact-accent, var(--supervisor-tone)) 42%, transparent);
    border-radius: 999px;
    display: inline-flex;
    font-weight: 740;
    justify-self: start;
    min-height: 28px;
    padding: 5px 9px;
  }
  .supervisor-summary-row[data-fact="physicalExecution"] { --fact-accent: var(--supervisor-cyan); }
  .supervisor-summary-row[data-policy="rce"] { --fact-accent: var(--supervisor-rce); }
  .supervisor-summary-row[data-policy="tariff"] { --fact-accent: var(--supervisor-tariff); }
  .supervisor-summary-row[data-policy="rcm"] { --fact-accent: var(--supervisor-rcm); }
  .supervisor-summary-row[data-policy="none"] { --fact-accent: var(--supervisor-neutral); }
  .supervisor-summary-row[data-priority="secondary"] .supervisor-summary-label,
  .supervisor-summary-row[data-priority="secondary"] .supervisor-summary-value { font-size: 11px; }
  .supervisor-policies { gap: 14px; }
  .supervisor-policy {
    --policy-accent: var(--supervisor-cyan);
    background: color-mix(in srgb, var(--supervisor-surface) 94%, transparent);
    border-color: color-mix(in srgb, var(--policy-accent) 27%, var(--divider-color));
    border-radius: 16px;
    box-shadow: inset 0 3px var(--policy-accent);
    overflow: hidden;
    padding: 0;
    position: relative;
  }
  .supervisor-policy[data-permitted="false"] { filter: saturate(.68); opacity: 1; }
  .supervisor-policy[data-tone="selected"] {
    border-color: color-mix(in srgb, var(--policy-accent) 72%, var(--divider-color));
    box-shadow: inset 0 3px var(--policy-accent), 0 0 26px color-mix(in srgb, var(--policy-accent) 22%, transparent);
    opacity: 1;
  }
  .supervisor-policy[data-tone="blocked"],
  .supervisor-policy[data-tone="waiting"] { border-color: color-mix(in srgb, var(--supervisor-warning) 48%, var(--policy-accent)); }
  .supervisor-policy[data-tone="unavailable"] { border-color: color-mix(in srgb, var(--supervisor-error) 48%, var(--divider-color)); }
  .supervisor-policy-top {
    align-items: center;
    background: color-mix(in srgb, var(--policy-accent) 11%, var(--supervisor-surface-strong));
    border-bottom: 1px solid color-mix(in srgb, var(--policy-accent) 20%, var(--divider-color));
    padding: 13px;
  }
  .supervisor-policy-identity { align-items: center; display: flex; gap: 8px; min-width: 0; }
  .supervisor-policy-icon { color: var(--policy-accent); flex: 0 0 auto; height: 24px; width: 24px; }
  .supervisor-policy-permission {
    background: color-mix(in srgb, var(--policy-accent) 13%, transparent);
    border: 1px solid color-mix(in srgb, var(--policy-accent) 44%, transparent);
    color: color-mix(in srgb, var(--policy-accent) 65%, var(--primary-text-color));
  }
  .supervisor-policy-permission[data-allowed="false"] { background: color-mix(in srgb, var(--supervisor-neutral) 10%, transparent); border-color: color-mix(in srgb, var(--supervisor-neutral) 34%, transparent); color: var(--supervisor-muted); }
  .supervisor-selected-marker { background: color-mix(in srgb, var(--policy-accent) 16%, transparent); border: 1px solid color-mix(in srgb, var(--policy-accent) 50%, transparent); color: color-mix(in srgb, var(--policy-accent) 72%, var(--primary-text-color)); }
  .supervisor-policy-badge { border: 1px solid color-mix(in srgb, var(--supervisor-neutral) 28%, transparent); }
  .supervisor-policy-badge[data-tone="ready"] { background: color-mix(in srgb, var(--supervisor-ready) 13%, transparent); border-color: color-mix(in srgb, var(--supervisor-ready) 42%, transparent); color: color-mix(in srgb, var(--supervisor-ready) 64%, var(--primary-text-color)); }
  .supervisor-policy-badge[data-tone="blocked"],
  .supervisor-policy-badge[data-tone="waiting"] { border-color: color-mix(in srgb, var(--supervisor-warning) 44%, transparent); color: color-mix(in srgb, var(--supervisor-warning) 62%, var(--primary-text-color)); }
  .supervisor-policy-badge[data-tone="unavailable"] { border-color: color-mix(in srgb, var(--supervisor-error) 44%, transparent); color: color-mix(in srgb, var(--supervisor-error) 64%, var(--primary-text-color)); }
  .supervisor-policy-body { background: color-mix(in srgb, var(--supervisor-surface) 93%, transparent); padding: 12px 13px 13px; }
  .supervisor-policy-facts { margin-top: 0; }
  .supervisor-flow { align-items: stretch; grid-template-columns: 1.3fr .95fr .8fr .8fr 1fr; margin-top: 12px; }
  .supervisor-flow-step {
    --flow-accent: var(--supervisor-cyan);
    align-content: center;
    background: color-mix(in srgb, var(--flow-accent) 9%, var(--supervisor-surface-strong));
    border-color: color-mix(in srgb, var(--flow-accent) 34%, var(--divider-color));
    display: grid;
    gap: 7px;
    justify-items: center;
    min-height: 94px;
    padding: 10px;
    position: relative;
  }
  .supervisor-flow-step:not(:last-child)::after { color: var(--supervisor-blue); content: "→"; font-size: 18px; position: absolute; right: -16px; top: calc(50% - 13px); z-index: 2; }
  .supervisor-flow-step[data-stage="validation"] { --flow-accent: var(--supervisor-cyan); }
  .supervisor-flow-step[data-stage="comparison"] { --flow-accent: var(--supervisor-blue); }
  .supervisor-flow-step[data-stage="winner"] { --flow-accent: var(--supervisor-violet); }
  .supervisor-flow-step[data-stage="observation"] { --flow-accent: var(--supervisor-cyan); }
  .supervisor-flow-icon { color: var(--flow-accent); height: 23px; width: 23px; }
  .supervisor-flow-label { color: var(--primary-text-color); font-weight: 650; }
  .supervisor-flow-policy-chips { display: flex; flex-wrap: wrap; gap: 5px; justify-content: center; }
  .supervisor-flow-policy-chips .supervisor-policy-chip { font-size: 11px; min-height: 28px; padding: 4px 7px; }
  .supervisor-safety-strip {
    align-items: center;
    background: linear-gradient(90deg, color-mix(in srgb, var(--supervisor-cyan) 14%, var(--supervisor-surface-strong)), color-mix(in srgb, var(--supervisor-blue) 8%, var(--supervisor-surface)));
    border-color: color-mix(in srgb, var(--supervisor-cyan) 42%, var(--divider-color));
    display: flex;
    font-size: 13px;
    font-weight: 650;
    gap: 10px;
    line-height: 1.48;
  }
  .supervisor-knowledge { display: grid; gap: 9px; }
  .supervisor-knowledge .supervisor-section-title { margin-bottom: 3px; }
  .supervisor-knowledge-detail {
    background: color-mix(in srgb, var(--supervisor-surface-strong) 92%, transparent);
    border: 1px solid color-mix(in srgb, var(--supervisor-blue) 18%, var(--divider-color));
    border-radius: 13px;
    min-width: 0;
    overflow: hidden;
    width: 100%;
  }
  .supervisor-knowledge-detail[open] { border-color: color-mix(in srgb, var(--supervisor-cyan) 34%, var(--divider-color)); }
  .supervisor-knowledge-summary {
    align-items: center;
    color: var(--primary-text-color);
    cursor: pointer;
    display: grid;
    gap: 10px;
    grid-template-columns: 24px minmax(0, 1fr);
    min-height: 56px;
    padding: 10px 13px;
  }
  .supervisor-knowledge-summary::marker { color: var(--supervisor-cyan); }
  .supervisor-knowledge-summary ha-icon { color: var(--supervisor-cyan); height: 21px; width: 21px; }
  .supervisor-knowledge-summary-copy { display: grid; gap: 2px; min-width: 0; }
  .supervisor-knowledge-summary-title { font-size: 13px; font-weight: 720; }
  .supervisor-knowledge-summary-hint { color: var(--supervisor-muted); font-size: 11px; font-weight: 480; line-height: 1.35; }
  .supervisor-knowledge-content { border-top: 1px solid color-mix(in srgb, var(--supervisor-cyan) 13%, var(--divider-color)); display: grid; gap: 13px; padding: 14px; }
  .supervisor-knowledge-subtitle { color: var(--primary-text-color); font-size: 14px; font-weight: 700; margin: 0; }
  .supervisor-technical-values { padding: 0; }
  .supervisor-technical-row { background: color-mix(in srgb, var(--supervisor-surface) 94%, transparent); border-radius: 9px; padding: 8px 10px; }
  .supervisor-select:focus-visible,
  .supervisor-switch:focus-visible,
  .supervisor-knowledge-summary:focus-visible { outline: 2px solid var(--supervisor-cyan); outline-offset: 2px; }
  @keyframes supervisor-core-drift { to { transform: rotate(360deg); } }
  @media (prefers-color-scheme: light) {
    :host {
      --supervisor-page-base: color-mix(in srgb, var(--primary-background-color, var(--supervisor-on-deep)) 94%, var(--supervisor-blue) 6%);
      --supervisor-page-cyan-glow: color-mix(in srgb, var(--supervisor-cyan) 5%, transparent);
      --supervisor-page-violet-glow: color-mix(in srgb, var(--supervisor-violet) 4%, transparent);
      --supervisor-surface: color-mix(in srgb, var(--card-background-color, var(--primary-background-color)) 96%, var(--supervisor-blue) 4%);
      --supervisor-surface-strong: color-mix(in srgb, var(--card-background-color, var(--primary-background-color)) 97%, var(--supervisor-cyan) 3%);
      --supervisor-muted: color-mix(in srgb, var(--secondary-text-color) 88%, var(--primary-text-color) 12%);
    }
    .supervisor-blob { opacity: .08; }
    .supervisor-points { opacity: .1; }
    .supervisor-vignette { background: radial-gradient(circle at center, transparent 42%, color-mix(in srgb, var(--supervisor-page-base) 62%, transparent)); }
    .supervisor-surface { box-shadow: 0 14px 34px color-mix(in srgb, var(--supervisor-deep) 8%, transparent), inset 0 1px color-mix(in srgb, var(--supervisor-cyan) 5%, transparent); }
  }
  @media (prefers-reduced-motion: reduce) {
    .supervisor-blob,
    .supervisor-core-ring { animation: none; }
    .supervisor-select,
    .supervisor-switch,
    .supervisor-switch-thumb,
    .supervisor-policy { transition: none; }
  }
  @container (max-width: 1024px) {
    .supervisor-hero { grid-template-columns: minmax(0, 1fr) minmax(300px, .86fr); }
    .supervisor-quick-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .supervisor-quick-fact[data-fact="physical"] { grid-column: 1 / -1; }
    .supervisor-flow { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .supervisor-flow-step:nth-child(3)::after { display: none; }
  }
  @container (max-width: 768px) {
    .supervisor-hero { grid-template-columns: minmax(0, 1fr); }
    .supervisor-hero-core { min-height: 224px; }
    .supervisor-live-grid,
    .supervisor-policies { grid-template-columns: minmax(0, 1fr); }
    .supervisor-flow { grid-template-columns: minmax(0, 1fr); }
    .supervisor-flow-step:nth-child(n) { grid-column: auto; min-height: 74px; }
    .supervisor-flow-step:not(:last-child)::after { bottom: -20px; content: "↓"; left: calc(50% - 6px); right: auto; top: auto; }
  }
  @container (max-width: 620px) {
    .supervisor-summary { grid-template-columns: minmax(0, 1fr); }
    .supervisor-summary-row[data-fact="decisionReason"] { grid-column: auto; }
    .supervisor-control { grid-template-columns: minmax(0, 1fr) auto; }
    .supervisor-policy-top { align-items: flex-start; }
    .supervisor-knowledge-detail { width: 100%; }
  }
  @container (max-width: 390px) {
    .supervisor-hero { gap: 17px; padding: 15px 12px; }
    .supervisor-quick-facts { grid-template-columns: minmax(0, 1fr); }
    .supervisor-quick-fact[data-fact="physical"] { grid-column: auto; }
    .supervisor-core-inputs { gap: 4px; }
    .supervisor-policy-chip { font-size: 11px; padding: 5px 7px; }
    .supervisor-control { grid-template-columns: minmax(0, 1fr); }
    .supervisor-control-field { width: 100%; }
    .supervisor-switch { justify-self: start; }
    .supervisor-policy-top { display: grid; }
    .supervisor-policy-badges { justify-content: flex-start; }
    .supervisor-knowledge-summary { padding: 10px; }
  }
`;

class HoymilesEmsSupervisorPanel {
  constructor(container) {
    this._container = container;
    this._hass = null;
    this._config = null;
    this._language = "en";
    this._instanceToken = Object.freeze({});
    this._lifecycleGeneration = 0;
    this._requestSequence = 0;
    this._requestTokens = new Map();
    this._pending = new Set();
    this._errors = new Map();
    this._listeners = [];
    this._connected = false;
    this._mount();
  }

  _element(tagName, className, textValue) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (textValue !== undefined) element.textContent = textValue;
    return element;
  }

  _copyElement(tagName, className, copyKey) {
    const element = this._element(tagName, className);
    this._copyNodes.push([element, copyKey]);
    return element;
  }

  _section(className, titleKey) {
    const section = this._element("section", `supervisor-surface ${className}`);
    const title = this._copyElement("h2", "supervisor-section-title", titleKey);
    section.append(title);
    return section;
  }

  _infoCard(parent, titleKey, descriptionKey, className = "") {
    const card = this._element(
      "article",
      `supervisor-info-card ${className}`.trim()
    );
    const title = this._copyElement("h3", "supervisor-info-title", titleKey);
    const description = this._copyElement(
      "p",
      "supervisor-info-description",
      descriptionKey
    );
    card.append(title, description);
    parent.append(card);
    return card;
  }

  _knowledgeDetail(parent, titleKey, hintKey, iconName, className) {
    const details = this._element(
      "details",
      ("supervisor-knowledge-detail " + className).trim()
    );
    const summary = this._element("summary", "supervisor-knowledge-summary");
    const icon = this._element("ha-icon");
    icon.setAttribute("icon", iconName);
    const summaryCopy = this._element(
      "span",
      "supervisor-knowledge-summary-copy"
    );
    summaryCopy.append(
      this._copyElement(
        "span",
        "supervisor-knowledge-summary-title",
        titleKey
      ),
      this._copyElement(
        "span",
        "supervisor-knowledge-summary-hint",
        hintKey
      )
    );
    summary.append(icon, summaryCopy);
    const body = this._element("div", "supervisor-knowledge-content");
    details.append(summary, body);
    parent.append(details);
    return body;
  }

  _mount() {
    if (this._panel) return;
    this._copyNodes = [];
    const panel = this._element("div", "supervisor-panel");
    const backdrop = this._element("div", "supervisor-backdrop");
    backdrop.setAttribute("aria-hidden", "true");
    backdrop.append(
      this._element("span", "supervisor-blob supervisor-blob-one"),
      this._element("span", "supervisor-blob supervisor-blob-two"),
      this._element("span", "supervisor-blob supervisor-blob-three"),
      this._element("span", "supervisor-points"),
      this._element("span", "supervisor-vignette")
    );
    const content = this._element("div", "supervisor-content");

    const hero = this._element("header", "supervisor-hero supervisor-surface");
    const heroCopy = this._element("div", "supervisor-hero-copy");
    const heading = this._element("div", "supervisor-heading");
    const icon = this._element("ha-icon", "supervisor-icon");
    icon.setAttribute("icon", "mdi:eye-outline");
    const title = this._element("h1", "supervisor-title");
    heading.append(icon, title);
    const scope = this._element("span", "supervisor-scope");
    const heroIntro = this._copyElement("p", "supervisor-hero-intro", "heroIntro");
    const authority = this._element("p", "supervisor-authority");
    const authorityIcon = this._element("ha-icon");
    authorityIcon.setAttribute("icon", "mdi:shield-eye-outline");
    authority.append(
      authorityIcon,
      this._copyElement("span", "", "physicalAuthority")
    );
    const quickFacts = this._element("dl", "supervisor-quick-facts");
    this._hero = {};
    for (const [key, copyKey] of [
      ["mode", "currentMode"],
      ["profile", "currentProfile"],
      ["physical", "physicalControl"],
    ]) {
      const item = this._element("div", "supervisor-quick-fact");
      item.dataset.fact = key;
      const label = this._copyElement(
        "dt",
        "supervisor-quick-fact-label",
        copyKey
      );
      const value =
        key === "physical"
          ? this._copyElement(
              "dd",
              "supervisor-quick-fact-value",
              "physicalControlValue"
            )
          : this._element("dd", "supervisor-quick-fact-value");
      item.append(label, value);
      quickFacts.append(item);
      this._hero[key] = value;
    }
    heroCopy.append(heading, scope, heroIntro, authority, quickFacts);

    const heroCore = this._element("div", "supervisor-hero-core");
    const coreDiagram = this._element("div", "supervisor-core-diagram");
    coreDiagram.setAttribute("aria-hidden", "true");
    const coreInputs = this._element("div", "supervisor-core-inputs");
    for (const policyId of HOYMILES_SUPERVISOR_POLICY_IDS) {
      const chip = this._element("span", "supervisor-policy-chip");
      chip.dataset.policy = policyId;
      const chipIcon = this._element("ha-icon");
      chipIcon.setAttribute("icon", HOYMILES_SUPERVISOR_POLICY_ICONS[policyId]);
      chip.append(chipIcon, this._copyElement("span", "", policyId));
      coreInputs.append(chip);
    }
    const coreOrb = this._element("div", "supervisor-core-orb");
    coreOrb.append(
      this._element("span", "supervisor-core-ring supervisor-core-ring-one"),
      this._element("span", "supervisor-core-ring supervisor-core-ring-two")
    );
    const coreIcon = this._element("ha-icon");
    coreIcon.setAttribute("icon", "mdi:eye-outline");
    coreOrb.append(coreIcon);
    coreDiagram.append(coreInputs, coreOrb);
    const heroResult = this._element("div", "supervisor-hero-result");
    heroResult.setAttribute("role", "status");
    heroResult.setAttribute("aria-live", "polite");
    const heroResultBadge = this._element(
      "span",
      "supervisor-hero-result-badge"
    );
    const heroResultText = this._element("p", "supervisor-hero-result-text");
    heroResult.append(heroResultBadge, heroResultText);
    const noControl = this._element("div", "supervisor-no-control");
    const noControlIcon = this._element("ha-icon");
    noControlIcon.setAttribute("icon", "mdi:power-plug-off-outline");
    noControl.append(
      noControlIcon,
      this._copyElement("span", "", "noInverterControl")
    );
    heroCore.append(coreDiagram, heroResult, noControl);
    hero.append(heroCopy, heroCore);
    this._heroResultBadge = heroResultBadge;
    this._heroResultText = heroResultText;
    this._heroCore = heroCore;

    const liveGrid = this._element("div", "supervisor-live-grid");
    const controlsSection = this._section(
      "supervisor-controls-section",
      "liveControls"
    );
    const controls = this._element("div", "supervisor-controls");
    this._controls = {};
    this._createSelectControl(
      controls,
      "mode",
      "supervisor_mode_entity",
      HOYMILES_SUPERVISOR_MODE_OPTIONS
    );
    this._createSelectControl(
      controls,
      "profile",
      "supervisor_profile_entity",
      HOYMILES_SUPERVISOR_PROFILE_OPTIONS
    );
    this._createSwitchControl(
      controls,
      "allowRce",
      "supervisor_allow_rce_entity"
    );
    this._createSwitchControl(
      controls,
      "allowTariff",
      "supervisor_allow_tariff_entity"
    );
    this._createSwitchControl(
      controls,
      "allowRcm",
      "supervisor_allow_rcm_entity"
    );
    controlsSection.append(controls);

    const limitation = this._element(
      "p",
      "supervisor-limitation supervisor-permission-context"
    );
    const limitationIcon = this._element("ha-icon", "supervisor-limitation-icon");
    limitationIcon.setAttribute("icon", "mdi:information-outline");
    const limitationText = this._copyElement(
      "span",
      "supervisor-limitation-text",
      "permissionContext"
    );
    limitation.append(limitationIcon, limitationText);
    controlsSection.append(limitation);

    const decisionSection = this._section(
      "supervisor-decision-section",
      "currentDecision"
    );
    const summary = this._element("dl", "supervisor-summary");
    this._summary = {};
    for (const key of [
      "state",
      "selectedPolicy",
      "decisionReason",
      "physicalExecution",
      "blockedReason",
      "phase",
      "existingAutomation",
    ]) {
      const row = this._element("div", "supervisor-summary-row");
      row.dataset.fact = key;
      row.dataset.priority = [
        "state",
        "selectedPolicy",
        "decisionReason",
        "physicalExecution",
      ].includes(key)
        ? "prominent"
        : "secondary";
      const label = this._element("dt", "supervisor-summary-label");
      const value = this._element("dd", "supervisor-summary-value");
      row.append(label, value);
      summary.append(row);
      this._summary[key] = { row, label, value };
    }
    decisionSection.append(summary);
    liveGrid.append(controlsSection, decisionSection);

    const policySection = this._section(
      "supervisor-policy-section",
      "flowPolicies"
    );
    const policies = this._element("div", "supervisor-policies");
    this._policyRows = {};
    for (const policyId of HOYMILES_SUPERVISOR_POLICY_IDS) {
      const row = this._element("article", "supervisor-policy");
      row.dataset.policyId = policyId;
      row.dataset.policy = policyId;
      const top = this._element("div", "supervisor-policy-top");
      const identity = this._element("div", "supervisor-policy-identity");
      const policyIcon = this._element("ha-icon", "supervisor-policy-icon");
      policyIcon.setAttribute(
        "icon",
        HOYMILES_SUPERVISOR_POLICY_ICONS[policyId]
      );
      const name = this._element("h3", "supervisor-policy-name");
      identity.append(policyIcon, name);
      const badges = this._element("div", "supervisor-policy-badges");
      const selected = this._element("span", "supervisor-selected-marker");
      const permissionBadge = this._element(
        "span",
        "supervisor-policy-badge supervisor-policy-permission"
      );
      const badge = this._element("span", "supervisor-policy-badge");
      badges.append(selected, permissionBadge, badge);
      top.append(identity, badges);
      const body = this._element("div", "supervisor-policy-body");
      const facts = this._element("div", "supervisor-policy-facts");
      const action = this._element("p", "supervisor-policy-action");
      const actionWarning = this._element(
        "p",
        "supervisor-policy-action-warning"
      );
      const reason = this._element("p", "supervisor-policy-reason");
      body.append(facts, action, actionWarning, reason);
      row.append(top, body);
      policies.append(row);
      this._policyRows[policyId] = {
        row,
        policyIcon,
        name,
        selected,
        permissionBadge,
        badge,
        action,
        actionWarning,
        facts,
        reason,
      };
    }
    policySection.append(policies);

    const howSection = this._section("supervisor-how", "howItWorksTitle");
    howSection.append(
      this._copyElement("p", "supervisor-section-intro", "howItWorksIntro")
    );
    const flow = this._element("ol", "supervisor-flow");
    const policiesStep = this._element("li", "supervisor-flow-step");
    policiesStep.dataset.stage = "policies";
    const flowPolicyChips = this._element(
      "span",
      "supervisor-flow-policy-chips"
    );
    for (const policyId of HOYMILES_SUPERVISOR_POLICY_IDS) {
      const chip = this._element("span", "supervisor-policy-chip");
      chip.dataset.policy = policyId;
      const chipIcon = this._element("ha-icon");
      chipIcon.setAttribute("icon", HOYMILES_SUPERVISOR_POLICY_ICONS[policyId]);
      chip.append(chipIcon, this._copyElement("span", "", policyId));
      flowPolicyChips.append(chip);
    }
    policiesStep.append(flowPolicyChips);
    flow.append(policiesStep);
    for (const [stage, iconName, key] of [
      ["validation", "mdi:shield-check-outline", "flowValidation"],
      ["comparison", "mdi:compare", "flowComparison"],
      ["winner", "mdi:trophy-outline", "flowWinner"],
      ["observation", "mdi:eye-outline", "flowObservation"],
    ]) {
      const step = this._element("li", "supervisor-flow-step");
      step.dataset.stage = stage;
      const stepIcon = this._element("ha-icon", "supervisor-flow-icon");
      stepIcon.setAttribute("icon", iconName);
      step.append(
        stepIcon,
        this._copyElement("span", "supervisor-flow-label", key)
      );
      flow.append(step);
    }
    howSection.append(flow);

    const safetyStrip = this._element(
      "aside",
      "supervisor-safety-strip supervisor-surface"
    );
    safetyStrip.setAttribute("role", "note");
    const safetyStripIcon = this._element("ha-icon");
    safetyStripIcon.setAttribute("icon", "mdi:shield-eye-outline");
    safetyStrip.append(
      safetyStripIcon,
      this._copyElement("span", "", "safetyStrip")
    );

    const knowledgeSection = this._section(
      "supervisor-knowledge",
      "knowledgeTitle"
    );
    const modesProfilesBody = this._knowledgeDetail(
      knowledgeSection,
      "modesProfilesDetailsTitle",
      "modesProfilesDetailsHint",
      "mdi:tune-variant",
      "supervisor-knowledge-modes"
    );
    modesProfilesBody.append(
      this._copyElement("h3", "supervisor-knowledge-subtitle", "modesTitle")
    );
    const modes = this._element(
      "div",
      "supervisor-doc-grid supervisor-doc-grid-three"
    );
    this._infoCard(modes, "modeOffTitle", "modeOffDescription");
    this._infoCard(modes, "modeShadowTitle", "modeShadowDescription");
    const activeCard = this._infoCard(
      modes,
      "modeActiveTitle",
      "modeActiveDescription",
      "supervisor-info-disabled"
    );
    activeCard.setAttribute("aria-disabled", "true");
    modesProfilesBody.append(
      modes,
      this._copyElement("h3", "supervisor-knowledge-subtitle", "profilesTitle")
    );
    const profiles = this._element(
      "div",
      "supervisor-doc-grid supervisor-doc-grid-three"
    );
    this._infoCard(profiles, "balancedTitle", "balancedDescription");
    this._infoCard(
      profiles,
      "maximumProfitTitle",
      "maximumProfitDescription"
    );
    this._infoCard(
      profiles,
      "highReserveTitle",
      "highReserveDescription"
    );
    modesProfilesBody.append(
      profiles,
      this._copyElement("p", "supervisor-callout", "profileLimitation")
    );

    const permissionsBody = this._knowledgeDetail(
      knowledgeSection,
      "permissionsTitle",
      "permissionsDetailsHint",
      "mdi:check-decagram-outline",
      "supervisor-knowledge-permissions"
    );
    permissionsBody.append(
      this._copyElement("p", "supervisor-section-intro", "permissionMeaning"),
      this._copyElement(
        "p",
        "supervisor-callout supervisor-callout-warning",
        "permissionNotMeaning"
      )
    );
    const permissionCards = this._element(
      "div",
      "supervisor-doc-grid supervisor-doc-grid-three"
    );
    for (const [titleKey, copyKey] of [
      ["rce", "rcePlain"],
      ["tariff", "tariffPlain"],
      ["rcm", "rcemPlain"],
    ]) {
      const card = this._element("article", "supervisor-info-card");
      card.append(
        this._copyElement("h3", "supervisor-info-title", titleKey),
        this._copyElement("p", "supervisor-info-description", copyKey)
      );
      permissionCards.append(card);
    }
    const permissionLimits = this._element("ul", "supervisor-limit-list");
    for (const key of [
      "permissionDoesNotEnable",
      "permissionDoesNotStart",
      "permissionDoesNotWrite",
      "permissionDoesNotGrant",
    ]) {
      permissionLimits.append(
        this._copyElement("li", "supervisor-limit-item", key)
      );
    }
    permissionsBody.append(permissionCards, permissionLimits);

    const resultBody = this._knowledgeDetail(
      knowledgeSection,
      "readResultTitle",
      "resultDetailsHint",
      "mdi:eye-check-outline",
      "supervisor-knowledge-result"
    );
    const resultCards = this._element("div", "supervisor-doc-grid");
    for (const [titleKey, descriptionKey] of [
      ["resultIdleTitle", "resultIdleDescription"],
      ["resultSelectedTitle", "resultSelectedDescription"],
      ["resultBlockedTitle", "resultBlockedDescription"],
      ["resultUnavailableTitle", "resultUnavailableDescription"],
      ["resultCommitmentTitle", "resultCommitmentDescription"],
    ]) {
      this._infoCard(resultCards, titleKey, descriptionKey);
    }
    resultBody.append(resultCards);

    const safetyBody = this._knowledgeDetail(
      knowledgeSection,
      "safetyTitle",
      "safetyDetailsHint",
      "mdi:shield-check-outline",
      "supervisor-knowledge-safety"
    );
    safetyBody.append(
      this._copyElement("p", "supervisor-section-intro", "safetyIntro")
    );
    const safetyList = this._element("ul", "supervisor-limit-list");
    for (const key of [
      "cannotModbus",
      "cannotMode",
      "cannotOwner",
      "cannotGrant",
      "cannotHandover",
      "cannotLegacy",
      "cannotActive",
    ]) {
      safetyList.append(this._copyElement("li", "supervisor-limit-item", key));
    }
    safetyBody.append(
      safetyList,
      this._copyElement("p", "supervisor-callout", "safetyScope")
    );

    const technicalBody = this._knowledgeDetail(
      knowledgeSection,
      "technicalTitle",
      "technicalDetailsHint",
      "mdi:code-json",
      "supervisor-knowledge-technical"
    );
    const technicalValues = this._element("dl", "supervisor-technical-values");
    this._technical = {};
    for (const [key, copyKey] of [
      ["arbitrationRevision", "arbitrationRevision"],
      ["executionPhase", "executionPhase"],
      ["reasonCode", "reasonCode"],
      ["profileEffects", "profileEffects"],
      ["candidateRevisions", "candidateRevisions"],
    ]) {
      const row = this._element("div", "supervisor-technical-row");
      const label = this._copyElement("dt", "supervisor-technical-label", copyKey);
      const value = this._element("dd", "supervisor-technical-value");
      row.append(label, value);
      technicalValues.append(row);
      this._technical[key] = value;
    }
    technicalBody.append(technicalValues);

    content.append(
      hero,
      liveGrid,
      policySection,
      howSection,
      safetyStrip,
      knowledgeSection
    );
    panel.append(backdrop, content);
    this._container.replaceChildren(panel);
    this._panel = panel;
    this._title = title;
    this._scope = scope;
    this._icon = icon;
  }

  _createControlShell(parent, key) {
    const row = this._element("div", "supervisor-control");
    row.dataset.control = key;
    const policyId = {
      allowRce: "rce",
      allowTariff: "tariff",
      allowRcm: "rcm",
    }[key];
    if (policyId) row.dataset.policy = policyId;
    const label = this._element("label", "supervisor-control-label");
    const labelIcon = this._element("ha-icon", "supervisor-control-icon");
    labelIcon.setAttribute(
      "icon",
      {
        mode: "mdi:eye-settings-outline",
        profile: "mdi:tune-variant",
        allowRce: HOYMILES_SUPERVISOR_POLICY_ICONS.rce,
        allowTariff: HOYMILES_SUPERVISOR_POLICY_ICONS.tariff,
        allowRcm: HOYMILES_SUPERVISOR_POLICY_ICONS.rcm,
      }[key]
    );
    const labelText = this._element("span", "supervisor-control-label-text");
    label.append(labelIcon, labelText);
    const field = this._element("div", "supervisor-control-field");
    const error = this._element("span", "supervisor-control-error");
    error.setAttribute("role", "status");
    field.append(error);
    row.append(label, field);
    parent.append(row);
    return { row, label, labelText, field, error };
  }

  _createSelectControl(parent, key, configKey, whitelist) {
    const control = this._createControlShell(parent, key);
    const select = this._element("select", "supervisor-select");
    const selectId = `hoymiles-supervisor-${key}`;
    select.id = selectId;
    control.label.htmlFor = selectId;
    control.field.prepend(select);
    this._controls[key] = {
      ...control,
      element: select,
      type: "select",
      configKey,
      whitelist,
    };
  }

  _createSwitchControl(parent, key, configKey) {
    const control = this._createControlShell(parent, key);
    const button = this._element("button", "supervisor-switch");
    button.type = "button";
    button.setAttribute("role", "switch");
    const policyId = {
      allowRce: "rce",
      allowTariff: "tariff",
      allowRcm: "rcm",
    }[key];
    if (policyId) button.dataset.policy = policyId;
    const thumb = this._element("span", "supervisor-switch-thumb");
    button.append(thumb);
    control.field.prepend(button);
    this._controls[key] = {
      ...control,
      element: button,
      type: "switch",
      configKey,
    };
  }

  _listen(element, type, listener) {
    element.addEventListener(type, listener);
    this._listeners.push([element, type, listener]);
  }

  connect() {
    if (this._connected) return;
    this._connected = true;
    for (const [key, control] of Object.entries(this._controls)) {
      if (control.type === "select") {
        this._listen(control.element, "change", () => {
          void this._selectOption(key, control.element.value);
        });
      } else {
        this._listen(control.element, "click", () => {
          void this._toggleBoolean(key);
        });
      }
    }
  }

  disconnect() {
    for (const [element, type, listener] of this._listeners) {
      element.removeEventListener(type, listener);
    }
    this._listeners = [];
    this._connected = false;
    this._invalidateLifecycle();
    this._hass = null;
    this._render();
  }

  update(hass, config, language) {
    const nextHass = hass || null;
    const nextConfig = config || null;
    const nextLanguage = hoymilesNormalizeLanguage(language);
    const lifecycleChanged =
      this._config !== nextConfig || this._language !== nextLanguage;
    if (lifecycleChanged) {
      this._invalidateLifecycle();
    } else if (this._hass !== nextHass && this._pending.size > 0) {
      this._clearRequestOwnership();
    }
    this._hass = nextHass;
    this._config = nextConfig;
    this._language = nextLanguage;
    if (!this._connected) return;
    this._render();
  }

  _clearRequestOwnership() {
    this._requestTokens.clear();
    this._pending.clear();
  }

  _invalidateLifecycle() {
    this._lifecycleGeneration += 1;
    this._clearRequestOwnership();
    this._errors.clear();
  }

  _copy() {
    return HOYMILES_SUPERVISOR_COPY[this._language];
  }

  _controlKind(key) {
    if (!Object.prototype.hasOwnProperty.call(HOYMILES_SUPERVISOR_CONTROL_KINDS, key)) {
      return null;
    }
    const kind = HOYMILES_SUPERVISOR_CONTROL_KINDS[key];
    return this._config?.[kind.configKey] === kind.entityId ? kind : null;
  }

  _helper(key) {
    const kind = this._controlKind(key);
    return kind ? this._hass?.states?.[kind.entityId] : undefined;
  }

  _validHelperState(helper) {
    return (
      Boolean(helper) &&
      typeof helper.state === "string" &&
      !["", "unknown", "unavailable"].includes(helper.state.trim().toLowerCase())
    );
  }

  _optionLabel(value) {
    const copy = this._copy();
    return (
      {
        Off: copy.off,
        Shadow: copy.shadow,
        Balanced: copy.balanced,
        "Maximum Profit": copy.maximumProfit,
        "High Reserve — Winter": copy.highReserveWinter,
      }[value] || copy.unavailable
    );
  }

  _renderSelect(key, control) {
    const copy = this._copy();
    const kind = this._controlKind(key);
    const helper = this._helper(key);
    const configuredOptions = helper?.attributes?.options;
    const options =
      kind?.type === "select" &&
      Array.isArray(configuredOptions) &&
      configuredOptions.every((value) => typeof value === "string")
      ? kind.options.filter(
          (option) =>
            configuredOptions.includes(option) &&
            configuredOptions.filter((value) => value === option).length === 1
        )
      : [];
    const current = options.includes(helper?.state) ? helper.state : null;
    control.element.replaceChildren();
    if (!current) {
      const placeholder = this._element("option", "", copy.controlUnavailable);
      placeholder.value = "";
      placeholder.selected = true;
      control.element.append(placeholder);
    }
    for (const optionValue of options) {
      const option = this._element("option", "", this._optionLabel(optionValue));
      option.value = optionValue;
      option.selected = optionValue === current;
      control.element.append(option);
    }
    const available =
      Boolean(kind) &&
      this._validHelperState(helper) &&
      options.length > 0 &&
      current !== null;
    control.element.disabled = !available || this._pending.has(key);
    control.element.setAttribute("aria-label", control.labelText.textContent);
    control.element.setAttribute("aria-busy", String(this._pending.has(key)));
  }

  _renderSwitch(key, control) {
    const kind = this._controlKind(key);
    const helper = this._helper(key);
    const available =
      kind?.type === "boolean" &&
      this._validHelperState(helper) &&
      ["on", "off"].includes(helper?.state);
    const checked = available && helper.state === "on";
    control.element.disabled = !available || this._pending.has(key);
    control.element.setAttribute("aria-checked", String(checked));
    control.element.setAttribute("aria-label", control.labelText.textContent);
    control.element.setAttribute("aria-busy", String(this._pending.has(key)));
  }

  _reason(code) {
    return hoymilesSupervisorReason(code, this._language);
  }

  _stateText(state) {
    const copy = this._copy();
    return (
      {
        off: copy.off,
        shadow_idle: copy.shadowIdle,
        shadow_selected: copy.shadowSelected,
        blocked: copy.blocked,
      }[state] || copy.unavailable
    );
  }

  _phaseText(phase) {
    const copy = this._copy();
    return (
      {
        idle: copy.idle,
        observed_active_latched: copy.observedActiveLatched,
        blocked: copy.phaseBlocked,
      }[phase] || copy.unavailable
    );
  }

  _policyText(policyId) {
    return this._copy()[policyId] || this._copy().none;
  }

  _actionText(action) {
    const copy = this._copy();
    return (
      {
        none: copy.actionNone,
        rce_export: copy.actionRceExport,
        tariff_charge: copy.actionTariffCharge,
        rcm_absorb_pv: copy.actionRcmAbsorbPv,
        rcm_limit_export: copy.actionRcmLimitExport,
        rcm_pre_discharge: copy.actionRcmPreDischarge,
      }[action] || copy.unknownAction
    );
  }

  _booleanText(value, trueText, falseText) {
    const copy = this._copy();
    if (value === true) return trueText;
    if (value === false) return falseText;
    return copy.unverified;
  }

  _candidatePresentation(candidate, selected) {
    const copy = this._copy();
    if (!hoymilesSupervisorIsRecord(candidate)) {
      return { tone: "unavailable", badge: copy.dataUnavailable };
    }
    if (candidate.active_latched === true) {
      return { tone: "commitment", badge: copy.activeCommitment };
    }
    if (selected) return { tone: "selected", badge: copy.selected };
    if (candidate.allowed_by_user !== true) {
      return { tone: "muted", badge: copy.notAllowed };
    }
    if (candidate.enabled !== true) {
      return { tone: "muted", badge: copy.automationDisabled };
    }
    if (candidate.available !== true) {
      return { tone: "unavailable", badge: copy.dataUnavailable };
    }
    if (
      candidate.result_current !== true ||
      candidate.recalculation_pending === true
    ) {
      return { tone: "waiting", badge: copy.waitingCurrentPlan };
    }
    const hardRejection =
      typeof candidate.rejection_reason === "string" &&
      candidate.rejection_reason &&
      ![
        "no_action",
        "not_allowed",
        "policy_disabled",
        "unavailable",
        "result_not_current",
        "recalculation_pending_new_start",
      ].includes(candidate.rejection_reason);
    if (
      candidate.local_hard_stop === true ||
      (typeof candidate.blocked_reason === "string" && candidate.blocked_reason) ||
      hardRejection
    ) {
      return { tone: "blocked", badge: copy.candidateBlocked };
    }
    if (
      candidate.requested_action === "none" ||
      candidate.reason_code === "no_action"
    ) {
      return { tone: "muted", badge: copy.noNeed };
    }
    if (candidate.start_eligible === true) {
      return { tone: "ready", badge: copy.ready };
    }
    return { tone: "blocked", badge: copy.candidateBlocked };
  }

  _candidateReason(candidate) {
    if (!hoymilesSupervisorIsRecord(candidate)) return "unavailable";
    for (const key of ["blocked_reason", "rejection_reason", "reason_code"]) {
      if (typeof candidate[key] === "string" && candidate[key]) {
        return candidate[key];
      }
    }
    return null;
  }

  _appendFact(container, label, value) {
    const fact = this._element("span", "supervisor-policy-fact");
    const factLabel = this._element("span", "supervisor-policy-fact-label", label);
    const factValue = this._element("strong", "", value);
    fact.append(factLabel, document.createTextNode(": "), factValue);
    container.append(fact);
  }

  _renderPolicy(entry, selectedPolicy) {
    const copy = this._copy();
    const { policyId, candidate } = entry;
    const view = this._policyRows[policyId];
    const selected = selectedPolicy === policyId;
    const presentation = this._candidatePresentation(candidate, selected);
    view.row.dataset.tone = selected ? "selected" : presentation.tone;
    const permitted = candidate?.allowed_by_user === true;
    view.row.dataset.permitted = String(permitted);
    view.name.textContent = this._policyText(policyId);
    view.selected.textContent = selected ? copy.logicallySelected : "";
    view.selected.hidden = !selected;
    view.permissionBadge.dataset.allowed = String(permitted);
    view.permissionBadge.textContent = permitted
      ? copy.permission + ": " + copy.yes
      : copy.notConsidered;
    view.badge.textContent = presentation.badge;
    view.badge.dataset.tone = presentation.tone;
    view.facts.replaceChildren();
    this._appendFact(
      view.facts,
      copy.permission,
      this._booleanText(candidate?.allowed_by_user, copy.yes, copy.no)
    );
    this._appendFact(
      view.facts,
      copy.existingAutomation,
      !hoymilesSupervisorIsRecord(candidate)
        ? copy.unavailable
        : `${this._booleanText(
            candidate?.available,
            copy.available,
            copy.notAvailable
          )} / ${this._booleanText(
            candidate?.enabled,
            copy.enabled,
            copy.disabled
          )}`
    );
    const dataPlan =
      !hoymilesSupervisorIsRecord(candidate)
        ? copy.dataUnavailable
        : candidate.available === true &&
      candidate?.result_current === true &&
      candidate?.recalculation_pending === false
        ? copy.current
        : candidate?.available === false
          ? copy.dataUnavailable
          : candidate?.result_current === false ||
              candidate?.recalculation_pending === true
            ? copy.waiting
            : copy.unverified;
    this._appendFact(view.facts, copy.dataOrPlan, dataPlan);
    const commitment = candidate?.active_latched === true;
    this._appendFact(
      view.facts,
      copy.readiness,
      this._booleanText(
        commitment ? candidate?.continuation_eligible : candidate?.start_eligible,
        copy.ready,
        copy.notReady
      )
    );
    this._appendFact(
      view.facts,
      copy.supervisorSelection,
      selected ? copy.selected : copy.notSelected
    );
    view.action.textContent = `${copy.requestedAction}: ${this._actionText(
      candidate?.requested_action
    )}`;
    view.actionWarning.textContent = copy.actionWarning;
    view.reason.textContent = `${copy.reason}: ${this._reason(
      this._candidateReason(candidate)
    )}`;
  }

  _render() {
    const copy = this._copy();
    for (const [node, key] of this._copyNodes) {
      node.textContent = copy[key] || copy.unavailable;
    }
    const supervisorEntity =
      this._config?.supervisor_entity ===
      HOYMILES_SUPERVISOR_BINDINGS.supervisor_entity
        ? this._config.supervisor_entity
        : null;
    const normalized = hoymilesNormalizeSupervisor(
      supervisorEntity ? this._hass?.states?.[supervisorEntity] : undefined
    );
    this._container.dataset.tone = normalized.tone;
    this._panel.dataset.tone = normalized.tone;
    this._icon.setAttribute(
      "icon",
      {
        off: "mdi:eye-off-outline",
        "shadow-idle": "mdi:eye-outline",
        "shadow-selected": "mdi:eye-check-outline",
        blocked: "mdi:alert-outline",
        unavailable: "mdi:alert-circle-outline",
      }[normalized.tone]
    );
    this._title.textContent = copy.title;
    this._scope.textContent = copy.observationOnly;

    const modeHelper = this._helper("mode");
    const profileHelper = this._helper("profile");
    this._hero.mode.textContent = HOYMILES_SUPERVISOR_MODE_OPTIONS.includes(
      modeHelper?.state
    )
      ? this._optionLabel(modeHelper.state)
      : copy.unavailable;
    this._hero.profile.textContent =
      HOYMILES_SUPERVISOR_PROFILE_OPTIONS.includes(profileHelper?.state)
        ? this._optionLabel(profileHelper.state)
        : copy.unavailable;
    this._heroCore.dataset.tone = normalized.tone;
    this._heroResultBadge.dataset.tone = normalized.tone;
    this._heroResultBadge.textContent = this._stateText(normalized.state);
    this._heroResultText.textContent =
      normalized.tone === "off"
        ? copy.heroOffResult
        : normalized.tone === "shadow-idle"
          ? copy.heroIdleResult
          : normalized.tone === "shadow-selected"
            ? copy.heroSelectedResult.replace(
                "{policy}",
                normalized.selectedPolicy
                  ? this._policyText(normalized.selectedPolicy)
                  : copy.none
              )
            : normalized.tone === "blocked"
              ? copy.heroBlockedResult
              : copy.heroUnavailableResult;

    const labels = {
      mode: copy.mode,
      profile: copy.profile,
      allowRce: copy.allowRce,
      allowTariff: copy.allowTariff,
      allowRcm: copy.allowRcm,
    };
    for (const [key, control] of Object.entries(this._controls)) {
      control.labelText.textContent = labels[key];
      control.error.textContent = this._errors.has(key) ? copy.serviceError : "";
      if (control.type === "select") this._renderSelect(key, control);
      else this._renderSwitch(key, control);
    }

    const summaryValues = {
      state: this._stateText(normalized.state),
      selectedPolicy: normalized.selectedPolicy
        ? this._policyText(normalized.selectedPolicy)
        : copy.none,
      decisionReason: this._reason(normalized.selectionReason),
      blockedReason: this._reason(normalized.blockedReason),
      phase: this._phaseText(normalized.phase),
      physicalExecution: normalized.authorizationFalse
        ? copy.authorizedFalse
        : copy.authorizationUnverified,
      existingAutomation: normalized.legacyUnchanged
        ? copy.unchanged
        : copy.unverified,
    };
    for (const [key, target] of Object.entries(this._summary)) {
      target.label.textContent =
        key === "existingAutomation" ? copy.legacyExecution : copy[key];
      target.value.textContent = summaryValues[key];
      if (key === "state") target.row.dataset.tone = normalized.tone;
      if (key === "selectedPolicy") {
        target.row.dataset.policy = normalized.selectedPolicy || "none";
      }
      if (key === "blockedReason") {
        target.row.dataset.empty = String(!normalized.blockedReason);
      }
    }
    for (const entry of normalized.candidates) {
      this._renderPolicy(entry, normalized.selectedPolicy);
    }

    const boundedScalar = (value) => {
      if (typeof value === "string") {
        const trimmed = value.trim();
        return trimmed && trimmed.length <= 96 ? trimmed : null;
      }
      return typeof value === "number" && Number.isSafeInteger(value)
        ? String(value)
        : null;
    };
    const attributes = normalized.attributes;
    const candidateRevisions = normalized.candidates
      .map(({ policyId, candidate }) => {
        const input = boundedScalar(candidate?.input_revision);
        const revision = boundedScalar(candidate?.candidate_revision);
        return input && revision ? `${policyId}: ${input}/${revision}` : null;
      })
      .filter(Boolean)
      .join(" · ");
    const effects = Array.isArray(attributes.profile_effects_applied)
      ? attributes.profile_effects_applied
          .slice(0, 3)
          .map(boundedScalar)
          .filter(Boolean)
          .join(" · ")
      : "";
    this._technical.arbitrationRevision.textContent =
      boundedScalar(attributes.arbitration_revision) || copy.unavailable;
    this._technical.executionPhase.textContent =
      boundedScalar(attributes.execution_phase) || copy.unavailable;
    this._technical.reasonCode.textContent =
      boundedScalar(normalized.blockedReason || normalized.selectionReason) ||
      copy.none;
    this._technical.profileEffects.textContent = effects || copy.notApplied;
    this._technical.candidateRevisions.textContent =
      candidateRevisions || copy.unavailable;
  }

  async _selectOption(key, option) {
    const control = this._controls[key];
    if (!control || control.type !== "select") return;
    await this._callHelperService(key, option);
  }

  async _toggleBoolean(key) {
    const control = this._controls[key];
    if (!control || control.type !== "switch") return;
    const helper = this._helper(key);
    const targetState =
      helper?.state === "on" ? "off" : helper?.state === "off" ? "on" : null;
    await this._callHelperService(key, targetState);
  }

  _requestOwnsCurrentPanel(request) {
    return (
      this._connected &&
      this._lifecycleGeneration === request.lifecycleGeneration &&
      this._instanceToken === request.instanceToken &&
      this._requestTokens.get(request.key) === request.controlToken &&
      this._hass === request.hass
    );
  }

  _attestHelperCommand(request, intendedState) {
    if (!this._requestOwnsCurrentPanel(request)) return null;
    const kind = this._controlKind(request.key);
    if (!kind) return null;
    const helper = request.hass?.states?.[kind.entityId];
    const currentState = helper?.state;
    const liveOptions = helper?.attributes?.options;
    if (!this._requestOwnsCurrentPanel(request)) return null;

    if (kind.type === "select") {
      const liveOptionsSafe =
        Array.isArray(liveOptions) &&
        liveOptions.every((value) => typeof value === "string");
      const currentSafe =
        typeof currentState === "string" &&
        kind.options.includes(currentState) &&
        liveOptionsSafe &&
        liveOptions.filter((value) => value === currentState).length === 1;
      const requestedSafe =
        typeof intendedState === "string" &&
        kind.options.includes(intendedState) &&
        liveOptionsSafe &&
        liveOptions.filter((value) => value === intendedState).length === 1;
      return currentSafe && requestedSafe
        ? {
            domain: "input_select",
            service: "select_option",
            data: { entity_id: kind.entityId, option: intendedState },
          }
        : null;
    }

    const currentSafe = currentState === "on" || currentState === "off";
    const targetSafe = intendedState === "on" || intendedState === "off";
    const exactToggle =
      targetSafe && intendedState === (currentState === "on" ? "off" : "on");
    return currentSafe && exactToggle
      ? {
          domain: "input_boolean",
          service: intendedState === "on" ? "turn_on" : "turn_off",
          data: { entity_id: kind.entityId },
        }
      : null;
  }

  _rejectCurrentRequest(request) {
    if (!this._requestOwnsCurrentPanel(request)) return;
    this._requestTokens.delete(request.key);
    this._pending.delete(request.key);
    this._errors.set(request.key, true);
    this._render();
  }

  async _callHelperService(key, intendedState) {
    if (arguments.length !== 2) return;
    const hass = this._hass;
    const callService = hass?.callService;
    if (
      !this._connected ||
      this._pending.has(key) ||
      !this._controls[key] ||
      typeof callService !== "function"
    ) {
      return;
    }
    const controlToken = Object.freeze({
      key,
      sequence: ++this._requestSequence,
    });
    const request = Object.freeze({
      lifecycleGeneration: this._lifecycleGeneration,
      instanceToken: this._instanceToken,
      controlToken,
      hass,
      key,
    });
    this._requestTokens.set(key, controlToken);
    this._pending.add(key);
    this._errors.delete(key);
    this._render();
    const command = this._attestHelperCommand(request, intendedState);
    if (!command || !this._requestOwnsCurrentPanel(request)) {
      this._rejectCurrentRequest(request);
      return;
    }
    try {
      await Promise.resolve(
        callService.call(hass, command.domain, command.service, command.data)
      );
    } catch (_error) {
      if (this._requestOwnsCurrentPanel(request)) {
        this._errors.set(key, true);
      }
    } finally {
      if (this._requestOwnsCurrentPanel(request)) {
        this._requestTokens.delete(key);
        this._pending.delete(key);
        this._render();
      }
    }
  }
}

class HoymilesEmsSupervisorCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._mounted = false;
  }

  setConfig(config) {
    const source = hoymilesSupervisorIsRecord(config) ? config : {};
    this._config = {
      language: source.language,
      ...HOYMILES_SUPERVISOR_BINDINGS,
    };
    this._mount();
    this._update();
  }

  set hass(hass) {
    this._hass = hass || null;
    this._update();
  }

  connectedCallback() {
    this._mount();
    this._panelController?.connect();
    this._update();
  }

  disconnectedCallback() {
    this._panelController?.disconnect();
    this._hass = null;
  }

  _mount() {
    if (!this.isConnected || !this._config || this._mounted) return;
    const style = document.createElement("style");
    style.textContent = `${HOYMILES_AURORA_THEME_CSS}\n${HOYMILES_EMS_SUPERVISOR_CSS}`;
    const card = document.createElement("ha-card");
    const host = document.createElement("section");
    host.className = "ems-supervisor";
    host.setAttribute("data-supervisor-card", "");
    card.append(host);
    this.shadowRoot.replaceChildren(style, card);
    this._panelController = new HoymilesEmsSupervisorPanel(host);
    this._panelController.connect();
    this._card = card;
    this._mounted = true;
    this._update();
  }

  _language() {
    return hoymilesLanguage(this._hass, this._config?.language);
  }

  _update() {
    if (!this._mounted || !this._config) return;
    this._panelController?.update(
      this._hass,
      this._config,
      this._language()
    );
  }

  getCardSize() {
    return 16;
  }

  getGridOptions() {
    return { columns: 12, rows: 18, min_columns: 6 };
  }

  static getStubConfig() {
    return { ...HOYMILES_SUPERVISOR_BINDINGS };
  }
}

if (!customElements.get("hoymiles-ems-supervisor-card")) {
  customElements.define(
    "hoymiles-ems-supervisor-card",
    HoymilesEmsSupervisorCard
  );
}

if (
  !window.customCards.some(
    (card) => card.type === "hoymiles-ems-supervisor-card"
  )
) {
  window.customCards.push({
    type: "hoymiles-ems-supervisor-card",
    name: "Hoymiles EMS Supervisor",
    description: "Observation-only EMS policy comparison and UI controls.",
    preview: false,
  });
}

class HoymilesAuroraEnergyCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._mounted = false;
    this._hass = null;
    this._config = null;
  }

  setConfig(config) {
    this._config = {
      pv_entity: "sensor.hoymiles_hit_overview_pv_total_power",
      load_entity: "sensor.hoymiles_actual_load_power",
      grid_entity: "sensor.hoymiles_hit_overview_grid_total_active_power",
      battery_entity: "sensor.hoymiles_hit_overview_battery_power",
      battery_soc_entity: "sensor.hoymiles_hit_overview_battery_soc",
      battery_current_entity: "sensor.hoymiles_hit_battery_current_bms",
      battery_current_inverter_entity:
        "sensor.hoymiles_hit_battery_current_inverter",
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
      inverter_entity: "sensor.hoymiles_hit_overview_inverter_active_power",
      ems_mode_entity: "select.hoymiles_hit_ems_mode",
      forecast_today_entity: "sensor.hoymiles_solcast_forecast_today",
      forecast_remaining_entity:
        "sensor.hoymiles_solcast_forecast_remaining_today",
      forecast_tomorrow_entity: "sensor.hoymiles_solcast_forecast_tomorrow",
      average_load_entity: "sensor.hoymiles_load_average_4_days",
      cav_temperature_entity: "sensor.hoymiles_hit_cav_temp",
      battery_path_temperature_entity: "sensor.hoymiles_hit_bat_ths_temp",
      pv_today_entity: "sensor.hoymiles_hit_pv_total_energy_today",
      pv_to_load_today_entity: "sensor.hoymiles_hit_pv_to_load_energy_today",
      pv_to_battery_today_entity:
        "sensor.hoymiles_hit_pv_to_battery_energy_today",
      pv_to_grid_today_entity: "sensor.hoymiles_hit_pv_to_grid_energy_today",
      grid_import_today_entity: "sensor.hoymiles_hit_grid_energy_buy_today",
      grid_to_load_today_entity: "sensor.hoymiles_rce_grid_to_load_today",
      grid_to_battery_today_entity: "sensor.hoymiles_grid_to_battery_today",
      inverter_image: "/local/hoymiles-inverter.png",
      ...config,
    };
    this._mount();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  connectedCallback() {
    this._mount();
  }

  _mount() {
    if (!this.isConnected || !this._config || this._mounted) return;

    const style = document.createElement("style");
    style.textContent = `
      ${HOYMILES_AURORA_THEME_CSS}
      :host {
        container-type: inline-size;
        display: block;
        --hoymiles-aurora-accent: ${hoymilesAuroraAccent("cyan")};
        --orbit-pv: var(--hoymiles-aurora-pv);
        --orbit-grid: var(--hoymiles-aurora-grid);
        --orbit-load: var(--hoymiles-aurora-load);
        --orbit-battery: var(--hoymiles-aurora-battery);
        --orbit-muted: var(--hoymiles-aurora-offline);
      }
      * { box-sizing: border-box; }
      button { font: inherit; }
      ha-card {
        background:
          radial-gradient(circle at 50% 42%, rgba(31, 123, 255, .16), transparent 31%),
          radial-gradient(circle at 10% 0%, rgba(52, 229, 139, .08), transparent 32%),
          linear-gradient(145deg, #111925 0%, #0a111b 54%, #080d15 100%);
        border: 1px solid rgba(151, 177, 209, .14);
        border-radius: 24px;
        box-shadow: 0 24px 70px rgba(0, 0, 0, .34);
        color: #f6f9ff;
        overflow: hidden;
        position: relative;
      }
      ha-card::before {
        background-image:
          linear-gradient(rgba(255,255,255,.018) 1px, transparent 1px),
          linear-gradient(90deg, rgba(255,255,255,.018) 1px, transparent 1px);
        background-size: 30px 30px;
        content: "";
        inset: 0;
        mask-image: linear-gradient(to bottom, black, transparent 82%);
        pointer-events: none;
        position: absolute;
      }
      .shell { padding: 22px 24px 20px; position: relative; z-index: 1; }
      .header {
        align-items: center;
        display: flex;
        justify-content: space-between;
        min-height: 28px;
      }
      .brand {
        color: rgba(223, 235, 249, .63);
        font-size: 10px;
        font-weight: 700;
        letter-spacing: .22em;
        text-transform: uppercase;
      }
      .system-state {
        align-items: center;
        background: rgba(255,255,255,.055);
        border: 1px solid rgba(255,255,255,.08);
        border-radius: 999px;
        color: #d9e6f5;
        display: inline-flex;
        font-size: 11px;
        gap: 7px;
        max-width: 58%;
        overflow: hidden;
        padding: 6px 10px;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .system-state .dot {
        background: #34e58b;
        border-radius: 50%;
        box-shadow: 0 0 12px rgba(52,229,139,.9);
        flex: 0 0 auto;
        height: 7px;
        width: 7px;
      }
      .system-state.warn .dot { background: #ffd45a; box-shadow: 0 0 12px rgba(255,212,90,.85); }
      .system-state.offline .dot { background: #ff6577; box-shadow: 0 0 12px rgba(255,101,119,.85); }
      .insights {
        display: grid;
        gap: 10px;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        margin-top: 15px;
      }
      .insight, .daily-item, .grid-import-item, .metric {
        appearance: none;
        border: 0;
        color: inherit;
        cursor: pointer;
      }
      .insight {
        background: rgba(255,255,255,.045);
        border: 1px solid rgba(255,255,255,.075);
        border-radius: 13px;
        min-width: 0;
        padding: 10px 12px;
        text-align: left;
        transition: background .18s ease, border-color .18s ease, transform .18s ease;
      }
      .insight:hover, .daily-item:hover, .grid-import-item:hover {
        background: rgba(255,255,255,.075);
        border-color: rgba(255,255,255,.14);
        transform: translateY(-1px);
      }
      .insight-label, .daily-label {
        color: #8496ad;
        display: block;
        font-size: 11px;
        letter-spacing: .025em;
        line-height: 1.25;
      }
      .insight-value {
        display: block;
        font-size: 17px;
        font-variant-numeric: tabular-nums;
        font-weight: 650;
        margin-top: 3px;
      }
      .aurora {
        height: 420px;
        margin: 2px auto 0;
        max-width: 1000px;
        position: relative;
      }
      .flows { height: 100%; inset: 0; overflow: visible; position: absolute; width: 100%; }
      .ribbon {
        fill: none;
        filter: blur(.2px) drop-shadow(0 0 12px var(--flow-color));
        opacity: var(--ribbon-opacity, .22);
        stroke: var(--flow-color);
        stroke-linecap: round;
        stroke-width: var(--ribbon-width, 11);
        transition: opacity .35s ease, stroke-width .35s ease;
      }
      .flow {
        fill: none;
        filter: drop-shadow(0 0 5px var(--flow-color));
        opacity: .94;
        stroke: var(--flow-color);
        stroke-dasharray: 2 13;
        stroke-linecap: round;
        stroke-width: var(--flow-width, 2.6);
        animation: orbit-flow var(--flow-speed, 1.8s) linear infinite;
      }
      .flow.reverse { animation-direction: reverse; }
      .flow.inactive { animation-play-state: paused; opacity: .12; }
      @keyframes orbit-flow { to { stroke-dashoffset: -60; } }
      .metric {
        align-items: center;
        appearance: none;
        backdrop-filter: blur(12px);
        background: rgba(13, 20, 30, .82);
        border: 1px solid rgba(255,255,255,.075);
        border-radius: 13px;
        color: var(--metric-color);
        cursor: pointer;
        display: flex;
        gap: 9px;
        min-width: 144px;
        padding: 11px 13px;
        position: absolute;
        text-align: left;
        transition: background .18s ease, border-color .18s ease, transform .18s ease;
        z-index: 4;
      }
      .metric:hover {
        background: rgba(25, 36, 51, .9);
        border-color: color-mix(in srgb, var(--metric-color) 35%, transparent);
        transform: translateY(-2px);
      }
      .metric-dot {
        background: currentColor;
        border-radius: 50%;
        box-shadow: 0 0 11px currentColor;
        flex: 0 0 auto;
        height: 7px;
        width: 7px;
      }
      .metric-copy { min-width: 0; }
      .metric-name {
        color: #8fa0b5;
        display: block;
        font-size: 13px;
        font-weight: 600;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .metric-value {
        color: #fff;
        display: block;
        font-size: 18px;
        font-variant-numeric: tabular-nums;
        font-weight: 650;
        letter-spacing: -.02em;
        margin-top: 1px;
        white-space: nowrap;
      }
      .metric.pv { --metric-color: var(--orbit-pv); left: 5%; top: 14%; }
      .metric.grid { --metric-color: var(--orbit-grid); right: 5%; top: 14%; }
      .metric.home { --metric-color: var(--orbit-load); bottom: 2%; left: 5%; }
      .metric.battery { --metric-color: var(--orbit-battery); bottom: 2%; min-width: 238px; right: 5%; }
      .metric-battery-detail,
      .metric-battery-eta {
        color: #91a4bc;
        display: block;
        font-size: 13px;
        font-variant-numeric: tabular-nums;
        line-height: 1.25;
        margin-top: 3px;
        white-space: nowrap;
      }
      .metric-battery-eta {
        color: #a8b8ca;
        margin-top: 2px;
        max-width: 230px;
        white-space: normal;
      }
      .core {
        align-items: center;
        appearance: none;
        backdrop-filter: blur(9px);
        background: radial-gradient(circle, rgba(29,48,69,.96), rgba(11,18,28,.94) 68%, rgba(11,18,28,.62));
        border: 1px solid rgba(255,255,255,.13);
        border-radius: 50%;
        box-shadow: 0 0 0 16px rgba(98,213,255,.025), 0 0 86px rgba(33,168,255,.18), inset 0 0 32px rgba(255,255,255,.04);
        color: #fff;
        cursor: pointer;
        display: flex;
        height: 148px;
        justify-content: center;
        left: 50%;
        padding: 0;
        position: absolute;
        text-align: center;
        top: 51%;
        transform: translate(-50%, -50%);
        width: 148px;
        z-index: 3;
      }
      .core-copy { align-items: center; display: flex; flex-direction: column; }
      .core-value { display: block; font-size: 32px; font-variant-numeric: tabular-nums; font-weight: 680; letter-spacing: -.06em; line-height: 1; }
      .core-name { color: #8fa3bc; display: block; font-size: 12px; margin-top: 4px; }
      .core-temperature {
        background: rgba(13, 20, 30, .9);
        border: 1px solid rgba(255,255,255,.1);
        border-radius: 999px;
        color: #b6c8dc;
        display: block;
        font-size: 12px;
        font-variant-numeric: tabular-nums;
        font-weight: 600;
        left: 50%;
        line-height: 1.15;
        padding: 5px 9px;
        pointer-events: none;
        position: absolute;
        transform: translateX(-50%);
        white-space: nowrap;
      }
      .core-temperature.cav { bottom: calc(100% + 10px); }
      .core-temperature.battery-path { top: calc(100% + 10px); }
      .core-status { color: #83f0d0; display: block; font-size: 11px; margin-top: 6px; }
      .daily {
        border-top: 1px solid rgba(255,255,255,.075);
        display: grid;
        gap: 1px;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        margin-top: -3px;
        padding-top: 15px;
      }
      .daily-item {
        background: transparent;
        border: 1px solid transparent;
        border-radius: 12px;
        min-width: 0;
        padding: 9px 12px;
        text-align: center;
        transition: background .18s ease, border-color .18s ease, transform .18s ease;
      }
      .daily-value {
        display: block;
        font-size: 16px;
        font-variant-numeric: tabular-nums;
        font-weight: 650;
        margin-top: 3px;
      }
      .daily-item:nth-child(1) .daily-value { color: var(--orbit-pv); }
      .daily-item:nth-child(2) .daily-value { color: var(--orbit-load); }
      .daily-item:nth-child(3) .daily-value { color: var(--orbit-battery); }
      .daily-item:nth-child(4) .daily-value { color: var(--orbit-grid); }
      .grid-import {
        align-items: center;
        background: rgba(255, 200, 87, .035);
        border: 1px solid rgba(255, 200, 87, .10);
        border-radius: 14px;
        display: grid;
        gap: 10px;
        grid-template-columns: minmax(135px, .8fr) minmax(0, 2.2fr);
        margin-top: 11px;
        padding: 8px 10px;
      }
      .grid-import-title {
        color: rgba(255, 200, 87, .84);
        font-size: 10px;
        font-weight: 700;
        letter-spacing: .075em;
        text-transform: uppercase;
      }
      .grid-import-values {
        display: grid;
        gap: 4px;
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
      .grid-import-item {
        background: transparent;
        border: 1px solid transparent;
        border-radius: 10px;
        min-width: 0;
        padding: 6px 8px;
        text-align: center;
        transition: background .18s ease, border-color .18s ease, transform .18s ease;
      }
      .grid-import-label {
        color: #8496ad;
        display: block;
        font-size: 9px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .grid-import-value {
        color: var(--orbit-grid);
        display: block;
        font-size: 13px;
        font-variant-numeric: tabular-nums;
        font-weight: 650;
        margin-top: 2px;
        white-space: nowrap;
      }
      @media (prefers-reduced-motion: reduce) {
        .flow { animation: none; }
        .insight, .daily-item, .grid-import-item, .metric { transition: none; }
      }
      @container (max-width: 620px) {
        ha-card { border-radius: 19px; }
        .shell { padding: 16px 13px 14px; }
        .header { padding: 0 2px; }
        .brand { font-size: 9px; letter-spacing: .16em; }
        .system-state { font-size: 10px; max-width: 62%; padding: 5px 8px; }
        .insights { gap: 6px; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 11px; }
        .insight { border-radius: 11px; padding: 8px 8px; text-align: center; }
        .insight-label { font-size: 11px; }
        .insight-value { font-size: 15px; }
        .aurora { height: 390px; margin-top: -2px; }
        .metric { min-width: 118px; padding: 9px 10px; }
        .metric-name { font-size: 11px; }
        .metric-value { font-size: 16px; }
        .metric.pv { left: 2%; top: 15%; }
        .metric.grid { right: 2%; top: 15%; }
        .metric.home { bottom: 2%; left: 2%; }
        .metric.battery { bottom: 2%; min-width: 198px; right: 2%; }
        .metric-battery-detail, .metric-battery-eta { font-size: 11px; }
        .metric-battery-eta { max-width: 190px; }
        .core { height: 132px; top: 51%; width: 132px; }
        .core-value { font-size: 28px; }
        .core-name { font-size: 11px; }
        .core-temperature { font-size: 10px; }
        .core-temperature.cav { bottom: calc(100% + 8px); }
        .core-temperature.battery-path { top: calc(100% + 8px); }
        .core-status { font-size: 10px; }
        .daily { gap: 4px; grid-template-columns: repeat(2, minmax(0, 1fr)); padding-top: 11px; }
        .daily-item { background: rgba(255,255,255,.025); padding: 8px 6px; }
        .daily-label { font-size: 9px; }
        .daily-value { font-size: 14px; }
        .grid-import { grid-template-columns: 1fr; gap: 4px; padding: 8px; }
        .grid-import-title { padding-left: 4px; }
        .grid-import-item { padding: 6px 3px; }
        .grid-import-label { font-size: 8px; }
        .grid-import-value { font-size: 12px; }
      }
    `;

    const card = document.createElement("ha-card");
    card.dataset.auroraVersion = "1.0.0";
    card.innerHTML = `
      <div class="shell">
        <div class="header">
          <span class="brand">Hoymiles · Energy aurora</span>
          <span class="system-state"><span class="dot"></span><span data-value="system"></span></span>
        </div>
        <div class="insights">
          <button class="insight" data-key="forecast_today"><span class="insight-label" data-label="forecast_today"></span><span class="insight-value" data-value="forecast_today"></span></button>
          <button class="insight" data-key="forecast_remaining"><span class="insight-label" data-label="forecast_remaining"></span><span class="insight-value" data-value="forecast_remaining"></span></button>
          <button class="insight" data-key="forecast_tomorrow"><span class="insight-label" data-label="forecast_tomorrow"></span><span class="insight-value" data-value="forecast_tomorrow"></span></button>
          <button class="insight" data-key="average_load"><span class="insight-label" data-label="average_load"></span><span class="insight-value" data-value="average_load"></span></button>
        </div>
        <div class="aurora">
          <svg class="flows" viewBox="0 0 900 420" preserveAspectRatio="none" aria-hidden="true">
            <path class="ribbon" data-ribbon="pv" d="M-40 28 C230 28 220 202 450 210"/><path class="flow" data-flow="pv" d="M-40 28 C230 28 220 202 450 210"/>
            <path class="ribbon" data-ribbon="grid" d="M450 210 C660 205 680 32 940 56"/><path class="flow" data-flow="grid" d="M450 210 C660 205 680 32 940 56"/>
            <path class="ribbon" data-ribbon="home" d="M450 210 C250 240 210 425 -40 386"/><path class="flow" data-flow="home" d="M450 210 C250 240 210 425 -40 386"/>
            <path class="ribbon" data-ribbon="battery" d="M450 210 C675 245 685 425 950 390"/><path class="flow" data-flow="battery" d="M450 210 C675 245 685 425 950 390"/>
          </svg>
          <button class="metric pv" data-key="pv"><span class="metric-dot"></span><span class="metric-copy"><span class="metric-name" data-label="pv"></span><strong class="metric-value" data-value="pv"></strong></span></button>
          <button class="metric grid" data-key="grid"><span class="metric-dot"></span><span class="metric-copy"><span class="metric-name" data-label="grid_live"></span><strong class="metric-value" data-value="grid"></strong></span></button>
          <button class="metric home" data-key="load"><span class="metric-dot"></span><span class="metric-copy"><span class="metric-name" data-label="load"></span><strong class="metric-value" data-value="load"></strong></span></button>
          <button class="metric battery" data-key="battery"><span class="metric-dot"></span><span class="metric-copy"><span class="metric-name" data-label="battery_live"></span><strong class="metric-value" data-value="battery"></strong><span class="metric-battery-detail" data-value="battery_detail"></span><span class="metric-battery-eta" data-value="battery_eta"></span></span></button>
          <button class="core" data-key="pv"><span class="core-copy"><small class="core-temperature cav"><span data-label="cav_temperature"></span> <span data-value="cav_temperature"></span></small><strong class="core-value" data-value="core"></strong><span class="core-name" data-label="core"></span><small class="core-temperature battery-path"><span data-label="battery_path_temperature"></span> <span data-value="battery_path_temperature"></span></small><small class="core-status" data-value="core_status"></small></span></button>
        </div>
        <div class="daily">
          <button class="daily-item" data-key="pv_today"><span class="daily-label" data-label="pv_today"></span><span class="daily-value" data-value="pv_today"></span></button>
          <button class="daily-item" data-key="pv_to_load_today"><span class="daily-label" data-label="pv_to_load_today"></span><span class="daily-value" data-value="pv_to_load_today"></span></button>
          <button class="daily-item" data-key="pv_to_battery_today"><span class="daily-label" data-label="pv_to_battery_today"></span><span class="daily-value" data-value="pv_to_battery_today"></span></button>
          <button class="daily-item" data-key="pv_to_grid_today"><span class="daily-label" data-label="pv_to_grid_today"></span><span class="daily-value" data-value="pv_to_grid_today"></span></button>
        </div>
        <div class="grid-import">
          <span class="grid-import-title" data-label="grid_import_title"></span>
          <div class="grid-import-values">
            <button class="grid-import-item" data-key="grid_import_today"><span class="grid-import-label" data-label="grid_import_today"></span><strong class="grid-import-value" data-value="grid_import_today"></strong></button>
            <button class="grid-import-item" data-key="grid_to_load_today"><span class="grid-import-label" data-label="grid_to_load_today"></span><strong class="grid-import-value" data-value="grid_to_load_today"></strong></button>
            <button class="grid-import-item" data-key="grid_to_battery_today"><span class="grid-import-label" data-label="grid_to_battery_today"></span><strong class="grid-import-value" data-value="grid_to_battery_today"></strong></button>
          </div>
        </div>
      </div>`;

    card.querySelectorAll("button[data-key]").forEach((button) => {
      button.addEventListener("click", () => this._showMoreInfo(button.dataset.key));
    });
    this.shadowRoot.replaceChildren(style, card);
    this._card = card;
    this._mounted = true;
    this._update();
  }

  _language() {
    return String(this._config?.language || this._hass?.language || "en")
      .toLowerCase()
      .startsWith("pl")
      ? "pl"
      : "en";
  }

  _copy() {
    const pl = {
      forecast_today: "Prognoza PV dzisiaj",
      forecast_remaining: "Pozostała produkcja",
      forecast_tomorrow: "Prognoza PV jutro",
      average_load: "Średnie zużycie domu",
      cav_temperature: "Temperatura Falownika",
      battery_path_temperature: "Temperatura toru magazynu",
      pv: "PV",
      grid: "Sieć",
      load: "Dom",
      battery: "Magazyn",
      inverter: "Falownik",
      core: "kW produkcji PV",
      core_status: "● energia zoptymalizowana",
      pv_today: "Wyprodukowano dzisiaj",
      pv_to_load_today: "PV do domu",
      pv_to_battery_today: "PV do magazynu",
      pv_to_grid_today: "PV do sieci",
      grid_import_title: "Pobór z sieci dzisiaj",
      grid_import_today: "Łącznie",
      grid_to_load_today: "Do domu",
      grid_to_battery_today: "Do magazynu",
      production: "produkcja",
      consumption: "zużycie",
      export: "eksport",
      import: "pobór",
      charge: "ładowanie",
      discharge: "rozładowanie",
      ready: "gotowość",
      idle: "spoczynek",
      noData: "brak danych",
      etaUnavailable: "ETA —",
      toReserve: "do rezerwy",
      toTarget: "do celu",
      toFullCharge: "do pełnego naładowania",
      toFullDischarge: "do pełnego rozładowania",
      online: "Dane na żywo",
      partial: "Część danych niedostępna",
      offline: "Brak danych falownika",
      self_use: "Autokonsumpcja",
      grid_charge: "Ładowanie z sieci",
      grid_discharge: "Rozładowanie do sieci",
      off_grid: "Praca wyspowa",
    };
    const en = {
      forecast_today: "PV forecast today",
      forecast_remaining: "Remaining production",
      forecast_tomorrow: "PV forecast tomorrow",
      average_load: "Average home use",
      cav_temperature: "Inverter temperature",
      battery_path_temperature: "Battery circuit temperature",
      pv: "PV",
      grid: "Grid",
      load: "Home",
      battery: "Battery",
      inverter: "Inverter",
      core: "kW of PV production",
      core_status: "● optimized energy",
      pv_today: "Produced today",
      pv_to_load_today: "PV to home",
      pv_to_battery_today: "PV to battery",
      pv_to_grid_today: "PV to grid",
      grid_import_title: "Grid import today",
      grid_import_today: "Total",
      grid_to_load_today: "To home",
      grid_to_battery_today: "To battery",
      production: "production",
      consumption: "consumption",
      export: "export",
      import: "import",
      charge: "charging",
      discharge: "discharging",
      ready: "ready",
      idle: "idle",
      noData: "no data",
      etaUnavailable: "ETA —",
      toReserve: "to reserve",
      toTarget: "to target",
      toFullCharge: "to full charge",
      toFullDischarge: "to full discharge",
      online: "Live data",
      partial: "Some data unavailable",
      offline: "Inverter data unavailable",
      self_use: "Self-use",
      grid_charge: "Grid charge",
      grid_discharge: "Grid discharge",
      off_grid: "Off-grid",
    };
    return this._language() === "pl" ? pl : en;
  }

  _entityId(key) {
    const map = {
      pv: "pv_entity",
      load: "load_entity",
      grid: "grid_entity",
      battery: "battery_entity",
      inverter: "inverter_entity",
      forecast_today: "forecast_today_entity",
      forecast_remaining: "forecast_remaining_entity",
      forecast_tomorrow: "forecast_tomorrow_entity",
      average_load: "average_load_entity",
      cav_temperature: "cav_temperature_entity",
      battery_path_temperature: "battery_path_temperature_entity",
      pv_today: "pv_today_entity",
      pv_to_load_today: "pv_to_load_today_entity",
      pv_to_battery_today: "pv_to_battery_today_entity",
      pv_to_grid_today: "pv_to_grid_today_entity",
      grid_import_today: "grid_import_today_entity",
      grid_to_load_today: "grid_to_load_today_entity",
      grid_to_battery_today: "grid_to_battery_today_entity",
    };
    return this._config?.[map[key]];
  }

  _showMoreInfo(key) {
    const entityId = this._entityId(key);
    if (!entityId) return;
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        bubbles: true,
        composed: true,
        detail: { entityId },
      })
    );
  }

  _state(key) {
    const entityId = this._entityId(key);
    return entityId ? this._hass?.states?.[entityId] : undefined;
  }

  _numeric(key) {
    const state = this._state(key);
    const value = Number(state?.state);
    return Number.isFinite(value) ? value : null;
  }

  _powerKw(key) {
    const state = this._state(key);
    const value = Number(state?.state);
    if (!Number.isFinite(value)) return null;
    const unit = String(state?.attributes?.unit_of_measurement || "W").toLowerCase();
    if (unit === "mw") return value * 1000;
    if (unit === "kw") return value;
    return value / 1000;
  }

  _number(value, digits = 2) {
    if (!Number.isFinite(value)) return "—";
    return new Intl.NumberFormat(this._language() === "pl" ? "pl-PL" : "en-GB", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }

  _formatPower(value, absolute = true) {
    if (!Number.isFinite(value)) return "—";
    return `${this._number(absolute ? Math.abs(value) : value, 2)} kW`;
  }

  _formatEnergy(key) {
    const state = this._state(key);
    let value = Number(state?.state);
    if (!Number.isFinite(value)) return "—";
    const unit = String(state?.attributes?.unit_of_measurement || "kWh").toLowerCase();
    if (unit === "wh") value /= 1000;
    if (unit === "mwh") value *= 1000;
    return `${this._number(Math.abs(value), 1)} kWh`;
  }

  _formatTemperature(key) {
    const state = this._state(key);
    const raw = String(state?.state ?? "").trim().toLowerCase();
    if (!raw || ["unknown", "unavailable", "none", "null"].includes(raw)) {
      return "—";
    }
    const value = Number(raw);
    if (!Number.isFinite(value)) return "—";
    const unit = String(state?.attributes?.unit_of_measurement || "°C");
    return `${this._number(value, 1)} ${unit}`;
  }

  _availableNumericStateById(
    entityId,
    minimum = Number.NEGATIVE_INFINITY,
    maximum = Number.POSITIVE_INFINITY
  ) {
    const state = entityId ? this._hass?.states?.[entityId] : undefined;
    const raw = String(state?.state ?? "").trim().toLowerCase();
    if (!raw || ["unknown", "unavailable", "none", "null"].includes(raw)) {
      return null;
    }
    const value = Number(raw);
    if (
      !Number.isFinite(value) ||
      value < minimum ||
      value > maximum
    ) {
      return null;
    }
    return value;
  }

  _freshPowerKwById(entityId) {
    const state = entityId ? this._hass?.states?.[entityId] : undefined;
    let value = this._availableNumericStateById(entityId);
    if (value === null) return null;
    const unit = String(state?.attributes?.unit_of_measurement || "W").toLowerCase();
    if (!["w", "kw", "mw"].includes(unit)) return null;
    if (unit === "mw") value *= 1000;
    else if (unit !== "kw") value /= 1000;
    return Math.abs(value) <= 1000 ? value : null;
  }

  _freshCapacityKwh() {
    const entityId = this._config?.battery_capacity_entity;
    const state = entityId ? this._hass?.states?.[entityId] : undefined;
    let value = this._availableNumericStateById(entityId);
    if (value === null) return null;
    const unit = String(state?.attributes?.unit_of_measurement || "kWh").toLowerCase();
    if (!["wh", "kwh", "mwh"].includes(unit)) return null;
    if (unit === "wh") value /= 1000;
    else if (unit === "mwh") value *= 1000;
    return value > 0 && value <= 10000 ? value : null;
  }

  _freshBatteryCurrentA() {
    const candidates = [
      this._config?.battery_current_entity,
      this._config?.battery_current_inverter_entity,
    ].filter(Boolean);
    for (const entityId of candidates) {
      const state = entityId ? this._hass?.states?.[entityId] : undefined;
      const value = this._availableNumericStateById(entityId);
      if (value === null) continue;
      const unit = String(state?.attributes?.unit_of_measurement || "A").toLowerCase();
      if (!["a", "ma", "ka"].includes(unit)) continue;
      let currentA = value;
      if (unit === "ma") currentA /= 1000;
      else if (unit === "ka") currentA *= 1000;
      currentA = Math.abs(currentA);
      if (currentA <= 10000) return currentA;
    }
    return null;
  }

  _batteryTarget(powerKw, soc) {
    const mode = this._availableNumericStateById(
      this._config?.ems_mode_readback_entity,
      0,
      5
    );
    if (![0, 3, 4, 5].includes(mode)) return null;

    let entityId;
    let kind;
    if (powerKw < -0.02) {
      entityId =
        mode === 4
          ? this._config?.battery_charge_target_entity
          : this._config?.battery_max_soc_entity;
      kind = "target";
    } else if (powerKw > 0.02) {
      entityId =
        mode === 5
          ? this._config?.battery_discharge_target_entity
          : mode === 0
            ? this._config?.battery_self_use_floor_entity
            : this._config?.battery_min_soc_entity;
      kind = "reserve";
    } else {
      return null;
    }

    const value = this._availableNumericStateById(entityId, 0, 100);
    if (
      value === null ||
      (kind === "target" && value <= soc) ||
      (kind === "reserve" && value >= soc)
    ) {
      return null;
    }
    return { value, kind };
  }

  _batteryPresentation() {
    const copy = this._copy();
    const powerKw = this._freshPowerKwById(this._config?.battery_entity);
    const mode = this._availableNumericStateById(
      this._config?.ems_mode_readback_entity,
      0,
      5
    );
    const soc = this._availableNumericStateById(
      this._config?.battery_soc_entity,
      0,
      100
    );
    const capacityKwh = this._freshCapacityKwh();
    const currentA = this._freshBatteryCurrentA();
    const storedKwh =
      capacityKwh !== null && soc !== null ? (capacityKwh * soc) / 100 : null;
    const target =
      powerKw !== null && soc !== null ? this._batteryTarget(powerKw, soc) : null;
    let etaKind = target?.kind || null;
    let etaHours = null;
    if (target && capacityKwh !== null && Math.abs(powerKw) > 0.02) {
      const energyKwh = (capacityKwh * Math.abs(target.value - soc)) / 100;
      const candidateHours = energyKwh / Math.abs(powerKw);
      if (Number.isFinite(candidateHours) && candidateHours > 0 && candidateHours <= 168) {
        etaHours = candidateHours;
        etaKind = target.kind;
      }
    }
    if (etaHours === null && capacityKwh !== null && soc !== null && Math.abs(powerKw) > 0.02) {
      const targetSoc = powerKw < 0 ? 100 : 0;
      const energyKwh = (capacityKwh * Math.abs(soc - targetSoc)) / 100;
      const candidateHours = energyKwh / Math.abs(powerKw);
      if (
        Number.isFinite(candidateHours) &&
        candidateHours > 0 &&
        candidateHours <= 168
      ) {
        etaHours = candidateHours;
        etaKind = powerKw < 0 ? "full_charge" : "full_discharge";
      }
    }

    const direction =
      powerKw === null
        ? copy.noData
        : Math.abs(powerKw) <= 0.02
          ? [0, 3, 4, 5].includes(mode)
            ? copy.ready
            : copy.idle
          : powerKw > 0
            ? copy.discharge
            : copy.charge;
    return {
      powerKw,
      soc,
      currentA,
      storedKwh,
      etaHours,
      etaKind,
      direction,
    };
  }

  _formatBatteryEta(etaHours, kind) {
    const copy = this._copy();
    if (
      !Number.isFinite(etaHours) ||
      etaHours <= 0 ||
      etaHours > 168 ||
      ![
        "reserve",
        "target",
        "full_charge",
        "full_discharge",
      ].includes(kind)
    ) {
      return copy.etaUnavailable;
    }
    const totalMinutes = Math.max(1, Math.round(etaHours * 60));
    const suffixMap = {
      reserve: copy.toReserve,
      target: copy.toTarget,
      full_charge: copy.toFullCharge,
      full_discharge: copy.toFullDischarge,
    };
    const suffix = suffixMap[kind] || copy.etaUnavailable;
    if (suffix === copy.etaUnavailable) {
      return suffix;
    }
    if (totalMinutes < 60) return `~${totalMinutes} min ${suffix}`;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = String(totalMinutes % 60).padStart(2, "0");
    return `~${hours} h ${minutes} min ${suffix}`;
  }

  _setText(selector, value) {
    const element = this._card?.querySelector(selector);
    if (element) element.textContent = value;
  }

  _setFlow(name, active, reverse, powerKw) {
    const flow = this._card?.querySelector(`[data-flow="${name}"]`);
    const ribbon = this._card?.querySelector(`[data-ribbon="${name}"]`);
    if (!flow || !ribbon) return;
    flow.classList.toggle("inactive", !active);
    flow.classList.toggle("reverse", Boolean(reverse));
    const magnitude = Math.max(Math.abs(powerKw || 0), 0.02);
    const normalized = Math.min(Math.log10(1 + magnitude) / Math.log10(11), 1);
    flow.style.setProperty("--flow-width", `${1.9 + normalized * 2.1}`);
    flow.style.setProperty("--flow-speed", `${2.5 - normalized * 1.45}s`);
    const colors = {
      pv: "var(--orbit-pv)",
      grid: "var(--orbit-grid)",
      home: "var(--orbit-load)",
      battery: "var(--orbit-battery)",
    };
    flow.style.setProperty("--flow-color", colors[name]);
    ribbon.style.setProperty("--flow-color", colors[name]);
    ribbon.style.setProperty("--ribbon-width", `${7 + normalized * 8}`);
    ribbon.style.setProperty(
      "--ribbon-opacity",
      active ? `${0.11 + normalized * 0.2}` : ".045"
    );
  }

  _update() {
    if (!this._mounted || !this._card || !this._config || !this._hass) return;
    const copy = this._copy();
    Object.keys(copy).forEach((key) => {
      if (["forecast_today", "forecast_remaining", "forecast_tomorrow", "average_load", "cav_temperature", "battery_path_temperature", "pv", "grid", "load", "battery", "inverter", "core", "pv_today", "pv_to_load_today", "pv_to_battery_today", "pv_to_grid_today", "grid_import_title", "grid_import_today", "grid_to_load_today", "grid_to_battery_today"].includes(key)) {
        this._setText(`[data-label="${key}"]`, copy[key]);
      }
    });

    const pv = this._powerKw("pv");
    const load = this._powerKw("load");
    const grid = this._powerKw("grid");
    const batteryPresentation = this._batteryPresentation();
    const battery = batteryPresentation.powerKw;
    const inverter = this._powerKw("inverter");
    const threshold = 0.02;

    this._setText('[data-value="pv"]', this._formatPower(pv));
    this._setText('[data-value="load"]', this._formatPower(load));
    this._setText('[data-value="grid"]', this._formatPower(grid));
    this._setText('[data-value="battery"]', this._formatPower(battery));
    const storedText = Number.isFinite(batteryPresentation.storedKwh)
      ? `${this._number(batteryPresentation.storedKwh, 1)} kWh`
      : "—";
    const currentText = Number.isFinite(batteryPresentation.currentA)
      ? `${this._number(batteryPresentation.currentA, 1)} A`
      : "—";
    this._setText('[data-value="battery_detail"]', `${storedText} · ${currentText}`);
    this._setText(
      '[data-value="battery_eta"]',
      this._formatBatteryEta(
        batteryPresentation.etaHours,
        batteryPresentation.etaKind
      )
    );
    this._setText('[data-value="core"]', Number.isFinite(pv) ? this._number(Math.abs(pv), 2) : "—");
    this._setText(
      '[data-value="cav_temperature"]',
      this._formatTemperature("cav_temperature")
    );
    this._setText(
      '[data-value="battery_path_temperature"]',
      this._formatTemperature("battery_path_temperature")
    );
    this._setText('[data-value="core_status"]', copy.core_status);
    const gridDirection =
      grid === null
        ? copy.noData
        : Math.abs(grid) <= threshold
          ? copy.idle
          : grid > 0
            ? copy.export
            : copy.import;
    this._setText('[data-label="grid_live"]', gridDirection);
    const socText = Number.isFinite(batteryPresentation.soc)
      ? `${this._number(batteryPresentation.soc, 0)}%`
      : "—";
    this._setText(
      '[data-label="battery_live"]',
      `${batteryPresentation.direction} · ${socText}`
    );

    this._setFlow("pv", pv !== null && pv > threshold, false, pv);
    this._setFlow("home", load !== null && Math.abs(load) > threshold, false, load);
    this._setFlow("grid", grid !== null && Math.abs(grid) > threshold, grid < 0, grid);
    this._setFlow(
      "battery",
      battery !== null && Math.abs(battery) > threshold,
      battery > 0,
      battery
    );

    this._setText('[data-value="forecast_today"]', this._formatEnergy("forecast_today"));
    this._setText('[data-value="forecast_remaining"]', this._formatEnergy("forecast_remaining"));
    this._setText('[data-value="forecast_tomorrow"]', this._formatEnergy("forecast_tomorrow"));
    this._setText('[data-value="average_load"]', this._formatEnergy("average_load"));
    for (const key of ["pv_today", "pv_to_load_today", "pv_to_battery_today", "pv_to_grid_today"]) {
      this._setText(`[data-value="${key}"]`, this._formatEnergy(key));
    }
    for (const key of ["grid_import_today", "grid_to_load_today", "grid_to_battery_today"]) {
      this._setText(`[data-value="${key}"]`, this._formatEnergy(key));
    }

    const coreValues = [pv, load, grid, battery];
    const available = coreValues.filter(Number.isFinite).length;
    const system = this._card.querySelector(".system-state");
    system?.classList.toggle("warn", available > 0 && available < coreValues.length);
    system?.classList.toggle("offline", available === 0);
    const emsState = this._hass.states?.[this._config.ems_mode_entity]?.state;
    const emsLabel = copy[emsState] || "";
    this._setText(
      '[data-value="system"]',
      available === 0 ? copy.offline : available < coreValues.length ? copy.partial : emsLabel || copy.online
    );
  }

  _numericStateById(entityId) {
    const value = Number(this._hass?.states?.[entityId]?.state);
    return Number.isFinite(value) ? value : null;
  }

  getCardSize() {
    return 9;
  }

  getGridOptions() {
    return { columns: 12, rows: 9, min_columns: 6 };
  }
}

if (!customElements.get("hoymiles-aurora-energy-card")) {
  customElements.define("hoymiles-aurora-energy-card", HoymilesAuroraEnergyCard);
}

if (!window.customCards.some((card) => card.type === "hoymiles-aurora-energy-card")) {
  window.customCards.push({
    type: "hoymiles-aurora-energy-card",
    name: "Hoymiles Energy Aurora",
    description: "Premium minimalist live energy-flow card for Hoymiles HIT systems.",
    preview: false,
  });
}

class HoymilesPowerFlowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._renderVersion = 0;
    this._inverterObserver = null;
    this._batteryEnergySignature = null;
  }

  setConfig(config) {
    if (!config) {
      throw new Error("Power-flow card configuration is required");
    }
    this._config = { ...config };
    this._mount();
  }

  set hass(hass) {
    this._hass = hass;
    const batteryEnergySignature =
      this._resolveBatteryEnergy(hass).signature;
    if (
      this._card &&
      batteryEnergySignature === this._batteryEnergySignature
    ) {
      this._card.hass = hass;
    } else if (this._config) {
      this._mount();
    }
  }

  connectedCallback() {
    this._mount();
  }

  disconnectedCallback() {
    this._inverterObserver?.disconnect();
    this._inverterObserver = null;
  }

  _resolveBatteryEnergy(hass = this._hass) {
    const configuredEnergy = this._config?.battery?.energy;
    if (typeof configuredEnergy !== "string") {
      return {
        value: configuredEnergy,
        signature: `fixed:${configuredEnergy ?? ""}`,
      };
    }

    const state = hass?.states?.[configuredEnergy];
    const numericValue = Number(state?.state);
    const unit = String(
      state?.attributes?.unit_of_measurement ?? ""
    ).trim();
    const unitMultipliers = {
      Wh: 1,
      kWh: 1000,
      MWh: 1000000,
    };
    const multiplier = unitMultipliers[unit] ?? 1;
    const value =
      Number.isFinite(numericValue) && numericValue > 0
        ? numericValue * multiplier
        : 0;

    return {
      value,
      signature: `${configuredEnergy}:${state?.state ?? "unavailable"}:${unit}`,
    };
  }

  _installInverterImage(card, inverterImage) {
    const inverterGroup = card.shadowRoot?.querySelector("svg#Inverter");
    if (!inverterGroup) return false;

    // Hide both variants of the inverter supplied by the underlying card.
    // Leaving them in the SVG keeps the original layout and connection points.
    inverterGroup
      .querySelectorAll(
        'svg[width="54"][height="79"], image[width="54"][height="72"]'
      )
      .forEach((element) => {
        element.style.display = "none";
      });

    let image = inverterGroup.querySelector("#hoymiles-inverter-image");
    if (!image) {
      image = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "image"
      );
      image.id = "hoymiles-inverter-image";
      inverterGroup.append(image);
    }

    // Coordinates are expressed in the original 720 x 405 SVG viewBox.
    // The image therefore follows the diagram scale automatically on phones,
    // tablets and desktop browsers.
    image.setAttribute("x", "205");
    image.setAttribute("y", "177");
    image.setAttribute("width", "70");
    image.setAttribute("height", "84");
    image.setAttribute("preserveAspectRatio", "xMidYMid meet");
    image.setAttribute("href", inverterImage);
    image.setAttribute("aria-hidden", "true");
    image.style.pointerEvents = "none";
    return true;
  }

  async _mount() {
    if (!this.isConnected || !this._config) return;

    const renderVersion = ++this._renderVersion;
    this._inverterObserver?.disconnect();
    this._inverterObserver = null;
    await customElements.whenDefined("sunsynk-power-flow-card");
    if (renderVersion !== this._renderVersion) return;

    const powerFlowConfig = { ...this._config };
    const batteryEnergy = this._resolveBatteryEnergy();
    this._batteryEnergySignature = batteryEnergy.signature;
    if (
      powerFlowConfig.battery &&
      typeof powerFlowConfig.battery.energy === "string"
    ) {
      powerFlowConfig.battery = {
        ...powerFlowConfig.battery,
        energy: batteryEnergy.value,
      };
    }
    const inverterImage =
      powerFlowConfig.inverter_image ??
      "/local/hoymiles-inverter.png";
    delete powerFlowConfig.inverter_image;
    delete powerFlowConfig.inverter_image_left;
    delete powerFlowConfig.inverter_image_top;
    delete powerFlowConfig.inverter_image_width;

    const card = document.createElement("sunsynk-power-flow-card");
    card.setConfig({
      ...powerFlowConfig,
      type: "custom:sunsynk-power-flow-card",
    });
    if (this._hass) {
      card.hass = this._hass;
    }

    const wrapper = document.createElement("div");
    wrapper.className = "wrapper";
    wrapper.append(card);

    const style = document.createElement("style");
    style.textContent = `
      :host {
        display: block;
      }
      .wrapper {
        position: relative;
      }
    `;

    this.shadowRoot.replaceChildren(style, wrapper);
    this._card = card;

    await card.updateComplete;
    if (renderVersion !== this._renderVersion || this._card !== card) return;

    this._installInverterImage(card, inverterImage);
    if (card.shadowRoot) {
      this._inverterObserver = new MutationObserver(() => {
        this._installInverterImage(card, inverterImage);
      });
      this._inverterObserver.observe(card.shadowRoot, {
        childList: true,
        subtree: true,
      });
    }
  }

  getCardSize() {
    return this._card?.getCardSize?.() ?? 6;
  }

  getGridOptions() {
    return this._card?.getGridOptions?.();
  }
}

if (!customElements.get("hoymiles-power-flow-card")) {
  customElements.define("hoymiles-power-flow-card", HoymilesPowerFlowCard);
}

if (!window.customCards.some((card) => card.type === "hoymiles-power-flow-card")) {
  window.customCards.push({
    type: "hoymiles-power-flow-card",
    name: "Hoymiles Power Flow",
    description:
      "Sunsynk power-flow card wrapper with a Hoymiles inverter illustration.",
    preview: false,
  });
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
