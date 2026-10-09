const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('home_assistant/www/hoymiles-rce-chart-card.js', 'utf8');
const method = source.slice(source.indexOf('  _currentTariffStartBlock(slot) {'), source.indexOf('  _baselineSlots() {'));
const card = new (Function(`return class { ${method} }`)())();
let attrs = {};
card._config = {tariff_plan_entity: 'tariff'};
card._state = () => ({attributes: attrs});
card._tariffPlanEvidenceCurrent = () => true;
card._language = () => 'pl';
const slot = {policy: 'tariff', start: Date.now()-10000, end: Date.now()+10000};
for (const reason of ['insufficient_energy', 'insufficient_benefit', 'insufficient_runtime', 'intent_not_stable', 'pv_covers_load', 'battery_not_discharging']) {
  attrs = {current_run_start_eligible: false, current_run_suppression_reason: reason};
  const text = card._currentTariffStartBlock(slot);
  assert.ok(text.length > 10);
  assert.ok(!/sprawdź stan EMS|restart/i.test(text), reason);
}
attrs = {current_run_start_eligible: false, current_run_suppression_reason: 'live_data_missing'};
assert.match(card._currentTariffStartBlock(slot), /świeże pomiary/);
card._tariffPlanEvidenceCurrent = () => false;
assert.equal(card._currentTariffStartBlock(slot), '');
console.log('PASS: economic suppression, missing measurements and stale-plan UI');
