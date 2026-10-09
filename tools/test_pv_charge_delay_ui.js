const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const read = p => fs.readFileSync(p,'utf8');
const source = read('home_assistant/www/hoymiles-rce-chart-card.js');
const methodStart = source.indexOf('  _actionLabel(action) {');
const methodEnd = source.indexOf('\n  _chartAction(',methodStart);
const sandbox = {};
vm.runInNewContext('this.Card = class { '+source.slice(methodStart,methodEnd)+' };',sandbox);
const card = new sandbox.Card();
card._language=()=> 'pl';
assert.equal(card._actionLabel('pv_charge_hold'),'Opóźnienie ładowania z PV');
card._language=()=> 'en';
assert.equal(card._actionLabel('pv_charge_hold'),'Delay PV charging');
const key='input_boolean.hoymiles_pv_charge_delay_enabled';
const policyStart=source.indexOf('const HOYMILES_AURORA_POLICY_SETTINGS = Object.freeze({');
const policyEnd=source.indexOf('\n});',policyStart)+4;
const policies={};
vm.runInNewContext(source.slice(policyStart,policyEnd)+';this.value=HOYMILES_AURORA_POLICY_SETTINGS;',policies);
const delayRows=policies.value.rce.groups.flatMap(group=>group.rows).filter(row=>row[4]===key);
assert.equal(delayRows.length,1,'The active Aurora dynamic-sale settings need the optional toggle');
assert.equal(delayRows[0][0],'Opóźnienie ładowania magazynu');
assert.equal(delayRows[0][1],'Delay battery charging');
assert.match(delayRows[0][2], /przed 14:00 czasu polskiego/);
assert.match(delayRows[0][3], /before 14:00 Europe\/Warsaw time/);
assert.match(delayRows[0][2], /16:30/);
assert.match(delayRows[0][3], /16:30/);
for (const lang of ['pl','en']) {
  const dashboard=read(`custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_${lang}.yaml`);
  assert.equal(dashboard.split(`entity: ${key}`).length-1,1);
  assert(dashboard.includes('pv_charge_delay_enabled'));
  assert(!dashboard.includes('field_verification_required'));
  const scheduler=read(`custom_components/hoymiles_hit_modbus/resources/home_assistant/${lang}/hoymiles_ems_scheduler.yaml`);
  const helper=scheduler.split('  hoymiles_pv_charge_delay_enabled:')[1]?.split('\n  hoymiles_')[0];
  assert(helper,'Restorable helper missing');
  assert(!helper.includes('initial:'),'Do not reset the user preference on restart');
}
const defaults=read('custom_components/hoymiles_hit_modbus/ems_initial_defaults.py');
assert(defaults.includes('_boolean("pv_charge_delay_enabled", "hoymiles_pv_charge_delay_enabled", "off")'));
for (const [name,expected] of [['HOYMILES_AUTOMATION_PLANNER_CANONICAL_ACTIONS','pv_charge_hold'],
                            ['HOYMILES_AUTOMATION_PLANNER_CANONICAL_ACTION_POLICIES','rce']]) {
  const start=source.indexOf(`const ${name} = Object.freeze({`);
  const end=source.indexOf('\n});',start)+4;
  const box={};vm.runInNewContext(source.slice(start,end)+`;this.result=${name};`,box);
  assert.equal(box.result.pv_charge_hold,expected);
}
assert(source.includes('pv_charge_hold: "pv_export_and_house_self_consumption"'));
const summaryStart=source.indexOf('  _policySummary(policy, slots, allowRetained = false) {');
const summaryEnd=source.indexOf('\n  _',summaryStart+5);
const summaryBox={};
vm.runInNewContext('this.Card = class { '+source.slice(summaryStart,summaryEnd)+' };',summaryBox);
const summaryCard=new summaryBox.Card();
const time=Date.now()+3600000;
const summary=summaryCard._policySummary('rce',[
  {policy:'rce',action:'pv_charge_hold',start:time,end:time+1800000,equation:{battery_to_grid_kwh:0}},
  {policy:'rce',action:'rce_export',start:time+1800000,end:time+3600000,equation:{battery_to_grid_kwh:2}},
]);
assert.equal(summary.valid,true,'PV delay must not hide the shared dynamic-sale plan');
assert.equal(summary.selected.length,2);
assert.equal(summary.next.action,'pv_charge_hold');
assert.equal(summary.rceExport,2,'PV delay is not battery export');
const chartStart=source.indexOf('  _chartSvg(slots, retained = false, recorded = null) {');
const chartEnd=source.indexOf('\n  _policyPlanState(',chartStart);
const chartBox={Date,Number,Math,hoymilesEscape:value=>String(value),
  hoymilesPlannerFinite:value=>typeof value==='number'&&Number.isFinite(value),
  hoymilesPlannerWarsawMidnightAfter:()=>new Date(time+86400000).toISOString()};
vm.runInNewContext('this.Card = class { '+source.slice(chartStart,chartEnd)+' };',chartBox);
const chart=new chartBox.Card();
const holdSlot={start:time,end:time+3600000,socStart:60,soc:60,policy:'rce',
  action:'pv_charge_hold',hasExpectedGridExport:true,expectedGridExportKwh:3};
chart._hourlyModel=()=>({start:time,end:time+3600000,buckets:[
  {start:time,end:time+3600000,pv:4,load:1,gridImportKwh:0,gridExportKwh:3}]});
chart._copy=()=>({});chart._language=()=> 'pl';chart._number=value=>String(value);
chart._formatTime=()=> '08:00';chart._chartAction=slot=>slot;
chart._formatEnergy=value=>String(value);chart._expectedSocSourceLabel=()=> 'P50';
chart._actionLabel=()=> 'Opóźnienie ładowania z PV';chart._chartInspectorHtml=()=> '';
const svg=chart._chartSvg([holdSlot]);
assert.match(svg,/class="soc-action-segment"[^>]*data-soc-action="pv_charge_hold"/,
  'Flat SOC must still show the selected PV automation');
assert.match(svg,/class="soc-action-glass"[^>]*data-soc-action="pv_charge_hold"/);
assert.match(svg,/id="variantAPvHoldGlass"/);
const segment=svg.match(/<line class="soc-action-segment"[^>]+>/)[0];
assert.equal(segment.match(/y1="([^"]+)"/)[1],segment.match(/y2="([^"]+)"/)[1]);
assert(source.includes('data-day="pv_hold"'));
const delaySummaryStart=source.indexOf('  _pvDelaySummary() {');
const delaySummaryEnd=source.indexOf('\n  _',delaySummaryStart+5);
const delayBox={Date,Intl,hoymilesRceTimestampFresh:stamp=>Date.now()-Date.parse(stamp)<300000};
vm.runInNewContext('this.Card = class {'+source.slice(delaySummaryStart,delaySummaryEnd)+'};',delayBox);
const delayCard=new delayBox.Card();delayCard._pl=()=>true;
const delayAttrs={result_current:true,recalculation_pending:false,pv_charge_delay_windows:[],
  pv_charge_delay_reason:'Brak bezpiecznego okna',pv_charge_delay_reason_en:'No safe window'};
const delayState={attributes:delayAttrs,last_reported:new Date().toISOString()};
delayCard._state=id=>id===key?{state:'on'}:delayState;
assert.equal(delayCard._pvDelaySummary(),'Brak bezpiecznego okna');
delayCard._pl=()=>false;assert.equal(delayCard._pvDelaySummary(),'No safe window');
delayCard._pl=()=>true;
delayAttrs.pv_charge_delay_windows=[{start:new Date(time+86400000).toISOString(),end:new Date(time+90000000).toISOString()}];
assert.match(delayCard._pvDelaySummary(),/^Zaplanowany eksport PV:/);
delayAttrs.result_current=false;
assert.equal(delayCard._pvDelaySummary(),'Trwa aktualizacja planu opóźnienia ładowania.');
delayAttrs.result_current=true;delayState.last_reported=new Date(Date.now()-400000).toISOString();
assert.equal(delayCard._pvDelaySummary(),'Trwa aktualizacja planu opóźnienia ładowania.');
delayCard._state=()=>({state:'off'});assert.equal(delayCard._pvDelaySummary(),'');
console.log('PASS PV delay: PL/EN labels, one optional restorable toggle, default off, usable opt-in and separate canonical action');

const profileKey = 'input_select.hoymiles_pv_charge_delay_profile';
assert.equal(policies.value.rce.groups.flatMap(g=>g.rows).filter(r=>r[4]===profileKey).length,1);
const labelStart=source.indexOf('  _settingOptionLabel(entityId, value) {');
const labelEnd=source.indexOf('\n  _',labelStart+5);
const labels={};vm.runInNewContext('this.Card=class {'+source.slice(labelStart,labelEnd)+'};',labels);
const labelCard=new labels.Card();
for (const pl of [true,false]) {
  labelCard._pl=()=>pl;
  assert.equal(labelCard._settingOptionLabel(profileKey,'Conservative'),pl?'Zachowawczy':'Conservative');
  assert.equal(labelCard._settingOptionLabel(profileKey,'Balanced'),pl?'Zrównoważony':'Balanced');
  assert.equal(labelCard._settingOptionLabel(profileKey,'Maximum'),pl?'Maksymalny':'Maximum');
  assert.equal(labelCard._settingOptionLabel('input_select.other','Balanced'),'Balanced');
}
for (const lang of ['pl','en']) {
  const yaml=read(`custom_components/hoymiles_hit_modbus/resources/home_assistant/${lang}/hoymiles_ems_scheduler.yaml`);
  const helper=yaml.split('  hoymiles_pv_charge_delay_profile:')[1]?.split('\n  hoymiles_')[0];
  assert(helper && !helper.includes('initial:'),'Restore user choice across restarts');
  assert.match(helper,/options:\s+- "Conservative"\s+- "Balanced"\s+- "Maximum"/,'First install defaults to Conservative; persisted option keys remain stable');
}
console.log('PASS PV delay profiles: conservative default, restorable choice and PL/EN risk labels');
