"use strict";
const fs = require("node:fs");
const assert = require("node:assert/strict");
const source = fs.readFileSync("home_assistant/www/hoymiles-rce-chart-card.js", "utf8");
const start = source.indexOf("class HoymilesAuroraVariantAEmsPageCard extends HTMLElement");
const end = source.indexOf('\nif (!customElements.get(', start);
assert(start > 0 && end > start);
const Card = new Function("HTMLElement", "hoymilesRceTimestampFresh",
  `return (${source.slice(start, end)});`)(class {},
  (at, now) => Number.isFinite(Date.parse(at)) && now-Date.parse(at)>=0 && now-Date.parse(at)<=120000);
const card = Object.create(Card.prototype);
let current = true;
let language = "pl";
const now = Date.now();
const slot = { policy: "tariff", start: now-60000, end: now+60000 };
const plan = { attributes: { input_revision: 3, current_run_start_eligible: false,
  current_run_suppression_reason: "reserve_target_unreachable" } };
const candidate = { policy_id: "tariff", input_revision: 3, available: false,
  start_eligible: false, active_latched: false };
const supervisor = { last_updated: new Date(now).toISOString(), attributes: { candidate_summaries: [candidate] } };
card._config = { tariff_plan_entity: "plan", supervisor_entity: "supervisor" };
card._state = id => ({plan, supervisor}[id]);
card._language = () => language;
card._tariffPlanEvidenceCurrent = () => current;
assert.match(card._currentTariffStartBlock(slot), /rezerwy/);
language = "en";
assert.match(card._currentTariffStartBlock(slot), /reserve/);
assert.equal(card._currentTariffStartBlock({...slot, start:now+10000}), "");
assert.equal(card._currentTariffStartBlock({...slot, policy:"rce"}), "");
current = false;
assert.equal(card._currentTariffStartBlock(slot), "");
current = true;
plan.attributes.current_run_start_eligible = true;
plan.attributes.current_run_suppression_reason = null;
assert.match(card._currentTariffStartBlock(slot), /blocked/);
candidate.input_revision = 2;
assert.equal(card._currentTariffStartBlock(slot), "");
candidate.input_revision = 3;
candidate.active_latched = true;
assert.equal(card._currentTariffStartBlock(slot), "");
candidate.active_latched = false;
supervisor.last_updated = new Date(now-180000).toISOString();
assert.equal(card._currentTariffStartBlock(slot), "");
supervisor.last_updated = new Date(now).toISOString();
candidate.available = true; candidate.start_eligible = true;
assert.equal(card._currentTariffStartBlock(slot), "");
assert(source.includes('const nextStartBlock = this._currentTariffStartBlock(nextSlot)'));
assert(source.includes('[nextStartBlock, nextEnergy].filter(Boolean).join'));
console.log("Pstryk current BUY blocker, fresh revision, future/active isolation, PL/EN: PASS");
