const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('home_assistant/www/hoymiles-rce-chart-card.js','utf8');
const start = source.indexOf('class HoymilesRceChartCard extends HTMLElement');
const end = source.indexOf('const existingRceChartCard = ',start);
assert(start > 0 && end > start);
const sandbox = { HTMLElement: class {}, Intl, Date, hoymilesVerifiedRceZeroExportPlan:()=>false };
vm.runInNewContext(source.slice(start,end)+'\nthis.Card=HoymilesRceChartCard;',sandbox);
const card = Object.create(sandbox.Card.prototype);
card._config={entity:'sensor.pse_today',tomorrow_entity:'sensor.pse_tomorrow'};
card._language='pl';
card._hass={config:{time_zone:'Europe/Warsaw'}};
card._states={
  'input_select.hoymiles_dynamic_sale_provider': {state:'Pstryk'},
  'input_select.hoymiles_tariff_operator': {state:'Pstryk'},
  'sensor.pse_today': {attributes:{value:[{rce_pln:99999}]}},
};
const sourceId='sensor.hoymiles_hit_pstryk_public_net_price';
const attrs={quality:'official',basis:'public_net_same_buy_sell',hours:[
  {start:'2026-10-25T00:00:00Z',end:'2026-10-25T01:00:00Z',priceNet:'-0.01'},
  {start:'2026-10-25T01:00:00Z',end:'2026-10-25T02:00:00Z',priceNet:'0'},
]};
card._states[sourceId]={attributes:attrs};
let rows=card._priceSources()[0].attributes.value;
assert.equal(rows.length,8);
assert.equal(new Set(rows.map(r=>r.dtime_utc)).size,8);
assert.equal(rows[0].period,rows[4].period);
assert.equal(rows[0].rce_pln,-10);
assert.equal(rows[4].rce_pln,0);
assert.match(card._t('compactPriceSeries'),/netto/);
card._config.layout='aurora_compact';
let rendered;
card._renderCombinedCompact=(data)=>{rendered=data;};
assert.doesNotThrow(()=>card._renderCombined(),'A published Pstryk day must render without a tomorrow source');
assert.equal(rendered.tomorrowPending,true);
attrs.hours.push({start:'2026-10-26T00:00:00Z',end:'2026-10-26T01:00:00Z',priceNet:'0.55'});
assert.equal(card._priceSources()[0].attributes.value.length,8);
assert.equal(card._priceSources()[1].attributes.value.length,4);
assert.doesNotThrow(()=>card._renderCombined());
assert.equal(rendered.tomorrowPending,false);
attrs.hours.pop();
for (const invalid of [null,undefined,true,'',NaN,Infinity]) {
  attrs.hours=[{...attrs.hours[0],priceNet:invalid}];
  assert.equal(card._priceSources()[0].attributes.value.length,0);
}
attrs.quality='unavailable';
assert.equal(card._priceSources().length,0,'No PSE fallback after public source failure');
attrs.quality='cached'; attrs.basis='gross';
assert.equal(card._priceSources().length,0);
card._states['input_select.hoymiles_dynamic_sale_provider'].state='RCE';
assert.equal(card._priceSources().length,0,'Mixed helpers cannot restore PSE chart');
card._states['input_select.hoymiles_tariff_operator'].state='PGE';
assert.equal(card._priceSources()[0],card._states['sensor.pse_today']);
assert(source.includes('input_select.hoymiles_dynamic_sale_provider'));
assert(source.includes('Sprzedaż dynamiczna'));
card._states['input_select.hoymiles_dynamic_sale_provider'].state='Pstryk';
card._states['input_select.hoymiles_tariff_operator'].state='Pstryk';
const shared={price_provider:'Pstryk',price_basis:'public_net_same_buy_sell',result_current:true,
  recalculation_pending:false,joint_plan_revision:7,joint_profile_revision:2,joint_benefit_pln:3.45};
const pairId='sensor.hoymiles_hit_tariff_charge_plan';
card._states[pairId]={attributes:{...shared}};
assert.equal(card._pstrykJointBenefit({attributes:shared},true),3.45);
for(const bad of [{joint_plan_revision:8},{joint_profile_revision:3},{price_provider:'RCE'},
  {recalculation_pending:true},{result_current:false},{joint_benefit_pln:4},{price_basis:'gross'}]) {
  card._states[pairId]={attributes:{...shared,...bad}};
  assert.equal(card._pstrykJointBenefit({attributes:shared},true),null,JSON.stringify(bad));
}
card._states[pairId]={attributes:{...shared}};
assert.equal(card._pstrykJointBenefit({attributes:shared},false),null);
let html='';card._setContent=value=>{html=value;};
card._rceSamples=[];
sandbox.Card.prototype._renderCombinedCompact.call(card,{...rendered,
  cohortCurrent:true,currentPrice:.06,planState:{attributes:shared},plannedExport:2,plannedRevenue:1.5});
assert.match(html,/0,06 zł\/kWh/);
assert.match(html,/Korzyść wspólnego planu Pstryk/);
assert.match(html,/3,45 PLN|3,45 zł/);
assert(!html.includes('Próg sprzedaży · auto'));
assert(!html.includes('Planowany eksport RCE'));
card._language='en';
assert.match(card._t('compactCurrentPrice'),/Net purchase and sale/);
card._states['input_select.hoymiles_dynamic_sale_provider'].state='RCE';
card._states['input_select.hoymiles_tariff_operator'].state='PGE';
assert.equal(card._t('pricePerMwhUnit'),'PLN/MWh');
for (const language of ['pl', 'en']) {
  card._language = language;
  const copy = {};
  for (const provider of ['RCE', 'Pstryk']) {
    card._states['input_select.hoymiles_dynamic_sale_provider'].state = provider;
    for (const key of ['active', 'inactive', 'executionWaiting', 'executionBlocked',
      'executionUnavailable', 'executionUnconfirmed', 'candidateSelectedByEms',
      'candidateNotSelectedByEms', 'candidateEmsUnavailable', 'noRceExport']) {
      const text = card._t(key);
      assert(!/\bRCE\b|Pstryk/.test(text), `${language}/${provider}/${key}: ${text}`);
      if (provider === 'RCE') copy[key] = text;
      else assert.equal(text, copy[key], `${key}: supplier changed execution wording`);
    }
  }
}
console.log('PASS Pstryk chart: net, zero, negative, DST fold, missing data, no PSE fallback, classic restore');
