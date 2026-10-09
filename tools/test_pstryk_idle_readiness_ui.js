const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('home_assistant/www/hoymiles-rce-chart-card.js','utf8');
const start=source.indexOf('  _verifiedPstrykIdlePlan() {');
assert(start>0,'Idle Pstryk must have a truthful plan-ready display');
const end=source.indexOf('\n  _',start+5);
const freshStart=source.indexOf('function hoymilesRceTimestampFresh(');
const freshEnd=source.indexOf('\nfunction ',freshStart+5);
const box={Date,HOYMILES_RCE_ZERO_EXPORT_MAX_AGE_SECONDS:300,HOYMILES_RCE_ZERO_EXPORT_FUTURE_TOLERANCE_SECONDS:5};
vm.runInNewContext(source.slice(freshStart,freshEnd)+'\nthis.Card=class{'+source.slice(start,end)+'};',box);
const card=new box.Card();card._config={section:'tariff'};
const now=Date.now();
const attributes={price_provider:'Pstryk',status_code:'ready',result_current:true,recalculation_pending:false,
  forecast_data_fresh:true,control_inputs_fresh:true,current_slot_planned:false,current_action:'none',
  command_charge_power_percent:0,current_slot_end:new Date(now+60000).toISOString(),joint_plan_revision:5,joint_profile_revision:2,price_basis:'public_net_same_buy_sell'};
let states;
function reset(){states={
  'sensor.hoymiles_hit_tariff_charge_plan':{state:'Brak zaplanowanego zakupu — autokonsumpcja',last_reported:new Date(now).toISOString(),attributes:{...attributes}},
  'sensor.hoymiles_hit_rce_optimized_plan':{attributes:{...attributes}},
  'input_select.hoymiles_tariff_operator':{state:'Pstryk'},'input_select.hoymiles_dynamic_sale_provider':{state:'Pstryk'},
  'binary_sensor.hoymiles_ems_execution_ready':{state:'on'},'binary_sensor.hoymiles_ems_control_conflict':{state:'off'},
};}
card._state=id=>states[id];reset();assert.equal(card._verifiedPstrykIdlePlan(),true);
for(const change of [{result_current:false},{recalculation_pending:true},{current_slot_planned:true},{current_action:'grid_support'},
 {command_charge_power_percent:1},{command_charge_power_percent:null},{command_charge_power_percent:'0'},
 {forecast_data_fresh:false},{control_inputs_fresh:false},{current_slot_end:new Date(now-1).toISOString()},
 {joint_plan_revision:6},{price_provider:'RCE'}]){
  reset();Object.assign(states['sensor.hoymiles_hit_tariff_charge_plan'].attributes,change);
  assert.equal(card._verifiedPstrykIdlePlan(),false,JSON.stringify(change));
}
reset();states['sensor.hoymiles_hit_tariff_charge_plan'].last_reported=new Date(now-301000).toISOString();assert.equal(card._verifiedPstrykIdlePlan(),false);
for(const [id,value] of [['binary_sensor.hoymiles_ems_execution_ready','off'],['binary_sensor.hoymiles_ems_control_conflict','on'],['input_select.hoymiles_tariff_operator','PGE']]){
  reset();states[id].state=value;assert.equal(card._verifiedPstrykIdlePlan(),false,id);
}
assert(source.includes('Plan gotowy · autokonsumpcja'));
console.log('PASS idle plan display: current paired Pstryk only; stale/conflict/BUY/invalid command remain blocked; no control gate bypass');

reset();card._config.section='rce';
const sale=states['sensor.hoymiles_hit_rce_optimized_plan'];
sale.last_reported=new Date(now).toISOString();
sale.attributes.current_slot_execution_discharge_power_kw=0;sale.attributes.pv_charge_delay_current=false;
assert.equal(card._verifiedPstrykIdlePlan(),true);
sale.attributes.pv_charge_delay_current=true;assert.equal(card._verifiedPstrykIdlePlan(),false);
sale.attributes.pv_charge_delay_current=false;sale.attributes.current_slot_execution_discharge_power_kw=1;
assert.equal(card._verifiedPstrykIdlePlan(),false);
