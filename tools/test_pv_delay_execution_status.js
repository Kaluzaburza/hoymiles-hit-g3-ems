const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('home_assistant/www/hoymiles-rce-chart-card.js', 'utf8');
const start = source.indexOf('  _rceExecutionStatus(');
const end = source.indexOf('  _timeMinutes(', start);
assert(start >= 0 && end > start);
const Card = vm.runInNewContext(`(class { ${source.slice(start, end)} })`);
const card = new Card(); card._language = 'pl'; card._t = key => key;
const attributes = {
  execution_phase: 'executing', selected_policy: 'rce', selected_action: 'pv_charge_hold',
  owner: 'rce', transaction_owner: 'rce', transaction_id: 'pv-hold:1',
  physical_verification_result: 'confirmed', supervisor_execution_authorized: true,
  owner_conflict: false, candidate_summaries: [{policy_id: 'rce'}],
};
const state = a => ({state: 'executing', attributes: {...attributes, ...a}});
const status = (a, conflict='off') => card._rceExecutionStatus(state(a), conflict, false);
assert.equal(status({}).label, 'pvHoldActive');
assert.equal(status({selected_action:'rce_export'}).tone, 'active');
assert.equal(status({physical_verification_result:'pending'}).tone, 'waiting');
assert.equal(status({supervisor_execution_authorized:false}).tone, 'waiting');
assert.equal(status({}, 'on').tone, 'blocked');
assert.equal(status({execution_phase:'restoring'}).tone, 'waiting');
assert.equal(status({candidate_summaries:[{policy_id:'rce',blocked_reason:'no_current_plan'}]}).label, 'pvHoldUpdating');
assert.equal(status({candidate_summaries:[{policy_id:'rce',blocked_reason:'export_blocked'}]}).tone, 'blocked');
console.log('PASS: confirmed PV hold, pending, physical proof, conflict and restore');
