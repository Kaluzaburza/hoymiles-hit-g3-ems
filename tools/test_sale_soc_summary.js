"use strict";
const fs = require('node:fs'), assert = require('node:assert/strict');
const source = fs.readFileSync('home_assistant/www/hoymiles-rce-chart-card.js', 'utf8');
const start = source.indexOf('class HoymilesAuroraVariantAPolicySettingsCard extends HTMLElement');
const end = source.indexOf('\nif (!customElements.get("hoymiles-aurora-variant-a-policy-settings-card"))', start);
const Card = new Function('HTMLElement', `return (${source.slice(start, end)});`)(class {});
const card = Object.create(Card.prototype);
let pl = true, states;
card._pl = () => pl;
card._state = id => states[id];
const now = Date.parse('2026-10-09T04:00:00Z');
const entity = value => ({state:String(value), last_reported:new Date(now).toISOString()});
function setup(reserve=20, extra=60, mode='on') {
  states = {
    'sensor.hoymiles_hit_ems_self_use_soc_readback':entity(reserve),
    'input_number.hoymiles_rce_soc_safety_margin':entity(extra),
    'input_boolean.hoymiles_rce_dynamic_soc_enabled':entity(mode),
  };
}
setup();
assert.match(card._saleReserveSummary(now), /20%.*60%.*80%/);
assert.match(card._saleReserveSummary(now), /maksymalnie 20%/);
assert.match(card._saleReserveSummary(now), /aktualnego naładowania/);
setup(20,90); assert.match(card._saleReserveSummary(now), /100%/);
assert.match(card._saleReserveSummary(now), /maksymalnie 0%/);
assert.match(card._saleReserveSummary(now), /ograniczona do 100%/);
setup(10,90); assert.match(card._saleReserveSummary(now), /maksymalnie 0%/);
setup(20,0); assert.match(card._saleReserveSummary(now), /maksymalnie 80%/);
// HA frontend updates may omit last_reported for an unchanged register value.
// A newer complete FC03 generation still confirms that reserve readback.
function unchangedReserve(generation=123, age=5000) {
  setup();
  const reserve = states['sensor.hoymiles_hit_ems_self_use_soc_readback'];
  delete reserve.last_reported;
  reserve.last_updated = new Date(now-3600000).toISOString();
  states['sensor.hoymiles_hit_ems_control_readback_generation'] = {
    state:String(generation), last_updated:new Date(now-age).toISOString(),
  };
}
unchangedReserve();
assert.match(card._saleReserveSummary(now), /20%.*60%.*80%/,
  'unchanged reserve stays readable with a fresh complete FC03 generation');
for (const bad of ['unknown','unavailable','',NaN,Infinity,0,-1,1.5]) {
  unchangedReserve(bad); assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
}
for (const age of [30001,-1]) {
  unchangedReserve(123,age); assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
}
unchangedReserve(); delete states['sensor.hoymiles_hit_ems_control_readback_generation'];
assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
for (const stamp of ['', 'invalid', new Date(now+1).toISOString()]) {
  unchangedReserve(); states['sensor.hoymiles_hit_ems_self_use_soc_readback'].last_updated=stamp;
  assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
}
// Explicit stale/future report evidence must never be hidden by the cohort.
for (const age of [301000,-1000]) {
  unchangedReserve(); states['sensor.hoymiles_hit_ems_self_use_soc_readback'].last_reported=new Date(now-age).toISOString();
  assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
}
for (const bad of ['unknown','unavailable','',NaN,Infinity,-1,101]) {
  setup(bad); assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
}
for (const bad of [-1,91,NaN,'']) {
  setup(20,bad); assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
}
setup(); states['sensor.hoymiles_hit_ems_self_use_soc_readback'].last_reported=new Date(now-301000).toISOString();
assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
setup(); states['sensor.hoymiles_hit_ems_self_use_soc_readback'].last_reported=new Date(now+1000).toISOString();
assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
setup(20,60,'off'); assert.match(card._saleReserveSummary(now), /nie jest doliczana/);
assert.doesNotMatch(card._saleReserveSummary(now), /80%/);
setup(20,60,'unavailable'); assert.match(card._saleReserveSummary(now), /Brak aktualnego/);
setup(); pl=false; assert.match(card._saleReserveSummary(now), /20%.*60%.*80%/);
assert.match(card._saleReserveSummary(now), /At most 20%/);
assert(source.includes('data-sale-reserve-summary'), 'summary must be mounted and updated');
console.log('PASS PL/EN live reserve explanation, 0/60/90, caps, missing/stale/future data, disabled automatic protection');
