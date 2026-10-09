#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(
  path.join(root, "home_assistant", "www", "hoymiles-rce-chart-card.js"),
  "utf8"
);
const start = source.indexOf("class HoymilesAuroraCompactPageCard extends HTMLElement");
const end = source.indexOf('if (!customElements.get("hoymiles-aurora-compact-page-card"))', start);
assert.ok(start >= 0 && end > start, "Aurora compact page class must be present");

const context = {
  HTMLElement: class {
    attachShadow() { return {}; }
  },
  hoymilesLanguage: () => "pl",
  Intl,
  Date,
  Number,
  Object,
  Array,
  String,
  Math,
  HOYMILES_AURORA_PV_TELEMETRY_MAX_AGE_SECONDS: 300,
};
vm.createContext(context);
vm.runInContext(
  `${source.slice(start, end)}\nglobalThis.TestCard = HoymilesAuroraCompactPageCard;`,
  context
);

const card = new context.TestCard();
card._config = {
  status_entity: "sensor.pv_link",
  strings: [
    { power_entity: "sensor.pv1" },
    { power_entity: "sensor.pv2" },
    { power_entity: "sensor.pv3" },
    { power_entity: "sensor.pv4" },
  ],
};

function state(value, ageSeconds, unit = undefined) {
  return {
    state: String(value),
    attributes: unit ? { unit_of_measurement: unit } : {},
    last_updated: new Date(Date.now() - ageSeconds * 1000).toISOString(),
    last_changed: new Date(Date.now() - ageSeconds * 1000).toISOString(),
  };
}

function install(link, powers) {
  card._hass = {
    language: "pl",
    states: {
      "sensor.pv_link": link,
      ...Object.fromEntries(
        powers.map((sample, index) => [
          `sensor.pv${index + 1}`,
          state(sample.value, sample.age, sample.unit ?? "W"),
        ])
      ),
    },
  };
}

install(state("Offline", 20), [
  { value: 2100, age: 20 },
  { value: 2200, age: 20 },
  { value: 0, age: 20 },
  { value: 0, age: 20 },
]);
let presentation = card._pvLinkPresentation();
assert.equal(presentation.conflict, true, "fresh positive string power must expose the Offline contradiction");
assert.equal(presentation.heading, "Produkcja PV wykryta");
assert.match(presentation.source, /Offline/, "the raw technical link reading must remain visible");
assert.equal(presentation.tone, "warning", "a contradictory register must not be presented as healthy");

install(state("Offline", 20), [
  { value: 2100, age: 301 },
  { value: 2200, age: 301 },
  { value: 0, age: 301 },
  { value: 0, age: 301 },
]);
presentation = card._pvLinkPresentation();
assert.equal(presentation.conflict, false, "stale positive power must not override the link register");
assert.equal(presentation.heading, "Offline");

install(state("Online", 301), [
  { value: 2100, age: 301 },
  { value: 2200, age: 301 },
  { value: 0, age: 301 },
  { value: 0, age: 301 },
]);
presentation = card._pvLinkPresentation();
assert.equal(presentation.conflict, false);
assert.equal(presentation.heading, "odczyt nieaktualny", "stale Online must not be promoted to a health claim");
assert.match(presentation.source, /Online/);
assert.match(presentation.source, /nieaktualny/);

install(state("Offline", 20), [
  { value: 0, age: 20 },
  { value: 0, age: 20 },
  { value: 0, age: 20 },
  { value: 0, age: 20 },
]);
presentation = card._pvLinkPresentation();
assert.equal(presentation.conflict, false, "fresh zero production must preserve Offline");
assert.equal(presentation.heading, "Offline");

install(state("Offline", 20), [
  { value: 2100, age: 20, unit: "V" },
  { value: "unavailable", age: 20 },
  { value: 0, age: 20 },
  { value: 0, age: 20 },
]);
presentation = card._pvLinkPresentation();
assert.equal(presentation.conflict, false, "wrong-unit and unavailable samples are not production evidence");

card._hass.states["sensor.pv1"] = {
  state: "2100",
  attributes: { unit_of_measurement: "W" },
};
presentation = card._pvLinkPresentation();
assert.equal(presentation.conflict, false, "an undated positive sample must fail closed");

console.log("PASS: PV status presentation preserves raw link state and requires fresh string-power evidence");
