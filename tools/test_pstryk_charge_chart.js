const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('home_assistant/www/hoymiles-rce-chart-card.js', 'utf8');
const start = source.indexOf('class HoymilesRceChartCard extends HTMLElement');
const end = source.indexOf('const existingRceChartCard = ', start);
const sandbox = { HTMLElement: class {}, Intl, Date, hoymilesVerifiedRceZeroExportPlan: () => false };
vm.runInNewContext(source.slice(start, end) + '\nthis.Card = HoymilesRceChartCard;', sandbox);
const buyId = 'sensor.hoymiles_hit_tariff_charge_plan';
const providerIds = ['input_select.hoymiles_dynamic_sale_provider', 'input_select.hoymiles_tariff_operator'];
const begin = Date.parse('2026-10-25T00:00:00Z'); // The two local 02:00 hours must remain distinct.
const iso = minutes => new Date(begin + minutes * 60000).toISOString();
const shared = { price_provider: 'Pstryk', price_basis: 'public_net_same_buy_sell', result_current: true,
  recalculation_pending: false, joint_plan_revision: 7, joint_profile_revision: 2, joint_benefit_pln: 0 };
const charge = (from, to, extra = {}) => ({ start_utc: iso(from), end_utc: iso(to),
  action: 'grid_support_and_charge', stored_energy_kwh: 1, grid_import_kwh: 1.4, price: -0.02, ...extra });
function makeCard(slots = [charge(0, 30), charge(30, 60), charge(90, 120)]) {
  const card = Object.create(sandbox.Card.prototype);
  card._config = { entity: 'sensor.pse', tomorrow_entity: 'sensor.pse_next', plan_entity: 'sensor.sale',
    timeline_entity: 'sensor.sale_timeline', layout: 'aurora_compact', block_enabled_entity: 'input_boolean.block',
    block_start_entity: 'input_datetime.block_start', block_end_entity: 'input_datetime.block_end' };
  card._language = 'pl'; card._hass = { config: { time_zone: 'Europe/Warsaw' } };
  card._states = Object.fromEntries(providerIds.map(id => [id, { state: 'Pstryk' }]));
  card._states['sensor.sale'] = { attributes: { ...shared, planned_export_kwh: 0, planned_revenue_pln: 0, planned_slots: [] } };
  card._states[buyId] = { attributes: { ...shared, planned_slots: slots } };
  card._states['sensor.sale_timeline'] = { state: 'current', attributes: { policy_id: 'rce',
    result_current: true, recalculation_pending: false, points: [] } };
  card._states['sensor.hoymiles_hit_pstryk_public_net_price'] = { attributes: {
    quality: 'official', basis: 'public_net_same_buy_sell', hours: [0, 60, 120].map(m => ({ start: iso(m), end: iso(m + 60), priceNet: -0.02 })) } };
  card._setContent = value => { card.html = value; };
  return card;
}
let checks = 0;
function check(ok, message) { checks++; assert(ok, message); }
let card = makeCard();
card._renderCombined();
check(card._rceSamples.filter(s => s.tariffCharge).length === 3, 'Three half-hours of battery charging');
check((card.html.match(/class="rce-sale-window" data-action="tariff_charge"/g) || []).length === 2,
  'Adjacent BUY slots merge; a gap and the repeated DST hour stay distinct');
check(card.html.includes('Ładowanie taryfowe') && card.html.includes('Plan ładowania'), 'Polish legend and planned-state copy');
check(card.html.includes('<span class="tariff-charge"><i></i>Ładowanie taryfowe</span>'), 'BUY has a distinct chart legend');
check(card._rceSamples[0].chargeStoredKwh === 1, 'Stored energy is distinct from total grid import');
check(card._rceSamples[2].tariffCharge === false, 'No duplicate local-hour fallback across DST');
check(card._rceTooltipText(card._rceSamples[0]).includes('1,40 kWh'), 'Tooltip includes grid import');
check(!card._rcePanelHtml(card._rceSamples[0]).includes('Przychód'), 'Charging is never sale revenue');
check(card.html.includes('2,00 kWh') && card.html.includes('2,80 kWh'), 'Merged window sums correct energy fields');
card._states['input_boolean.block'] = { state: 'on' };
card._renderCombined();
check(card._rceSamples[0].tariffCharge && /bar tariff-charge rce-price-bar/.test(card.html), 'Sale lockout does not hide BUY');
card._language = 'en'; card._renderCombined();
check(card.html.includes('Tariff charging') && card.html.includes('Charging plan'), 'English labels');
card._config.layout = 'standard'; card._renderCombined();
check(card.html.includes('swatch tariff-charge') && card._rceSamples.filter(s => s.tariffCharge).length === 3, 'Standard chart also shows BUY legend and blocks');
for (const invalid of [{ result_current: false }, { recalculation_pending: true }, { joint_plan_revision: 8 },
  { joint_profile_revision: 9 }, { price_provider: 'RCE' }, { price_basis: 'gross' }, { planned_slots: null }]) {
  card = makeCard(); Object.assign(card._states[buyId].attributes, invalid); card._renderCombined();
  check(!card._rceSamples.some(s => s.tariffCharge), 'Invalid BUY cohort hidden: ' + JSON.stringify(invalid));
  check(card.html.includes('Brak aktualnych bloków ładowania'), 'Missing current BUY layer is explicit');
}
for (const slot of [charge(0, 30, { action: 'grid_support', stored_energy_kwh: 0 }),
  charge(0, 30, { stored_energy_kwh: null }), charge(0, 30, { stored_energy_kwh: -1 }),
  charge(0, 30, { start_utc: '2026-10-25T02:00:00' }), charge(0, 0),
  charge(0, 60), charge(0, 30, { grid_import_kwh: Infinity })]) {
  card = makeCard([slot]); card._renderCombined();
  check(!card._rceSamples.some(s => s.tariffCharge), 'Do not invent charging from support/invalid geometry');
}
card = makeCard([charge(10, 30)]); card._renderCombined();
check(card._rceSamples[0].tariffCharge && card.html.includes('02:10–02:30'), 'Clipped first block preserves actual start');
card = makeCard([charge(0, 30), charge(0, 30)]); card._renderCombined();
check(!card._rceSamples.some(s => s.tariffCharge), 'Ambiguous duplicate charging slot hidden');
card = makeCard(); card._states['sensor.sale_timeline'].attributes.recalculation_pending = true; card._renderCombined();
check(!card._rceSamples.some(s => s.tariffCharge), 'No stale BUY layer while sale cohort updates');
card = makeCard([]); card._renderCombined();
check(card.html.includes('Brak zaplanowanego ładowania') && !card.html.includes('Brak aktualnych bloków ładowania'), 'Empty current plan differs from unavailable data');
card = makeCard(); providerIds.forEach(id => { card._states[id].state = 'RCE'; });
const publicSources = sandbox.Card.prototype._priceSources.call(makeCard());
card._priceSources = () => publicSources; card._renderCombined();
check(!card._rceSamples.some(s => s.tariffCharge) && !card.html.includes('Ładowanie taryfowe'), 'Classic RCE remains isolated');
console.log(`PASS Pstryk charging chart (${checks} checks): current paired plan, BUY energy, gap/DST, stale data, sale lockout and PL/EN`);
