"use strict";

const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(process.env.HOYMILES_UI_TEST_ROOT || process.cwd());
const cardPath = path.join(root, "home_assistant", "www", "hoymiles-rce-chart-card.js");
const source = fs.readFileSync(cardPath, "utf8");
const start = source.indexOf("class HoymilesAuroraOverviewCard extends HTMLElement");
const end = source.indexOf('\nif (!customElements.get("hoymiles-aurora-overview-card"))', start);
if (start < 0 || end < 0) throw new Error("Aurora overview class boundaries changed");
const Card = new Function(
  "HTMLElement",
  "hoymilesLanguage",
  `return (${source.slice(start, end)});`,
)(class {}, (_hass, language) => language || "pl");

function check(condition, message) {
  if (!condition) throw new Error(message);
}

function state(value, attributes = {}) {
  return { state: value, attributes };
}

function slot(startAt, endAt, policy = "rce", action = "rce_export", energy = 1, soc = 30, extra = {}) {
  return {
    starts_at: startAt,
    ends_at: endAt,
    selected_policy: policy,
    selected_action: action,
    soc_equation: {
      battery_to_grid_kwh: energy,
      grid_to_battery_kwh: energy,
      soc_end_percent: soc,
    },
    planned: { grid_kwh_import_positive: energy },
    ...extra,
  };
}

function fixture(nowIso, slots, options = {}) {
  const now = Date.parse(nowIso);
  const builtAt = options.builtAt || new Date(now - 60_000).toISOString();
  const mode = options.mode || "current";
  const entity = mode === "pending"
    ? state("pending", {
        canonical_status: "pending",
        canonical_blocker_code: options.blocker || "source_recalculation_pending",
        result_current: false,
        recalculation_pending: true,
        built_at: builtAt,
        slots,
      })
    : state("current", {
        result_current: true,
        recalculation_pending: false,
        built_at: builtAt,
        slots,
      });
  const states = {
    timeline: entity,
    supervisor: state(options.supervisorState || "idle", {
      ems_paused: options.paused === true,
      ...(options.masterStop ? { master_stop: { status: options.masterStop } } : {}),
    }),
    mode: state(options.enabled === false ? "Off" : "Active"),
    paused: state(options.paused ? "on" : "off"),
    conflict: state(options.conflict ? "on" : "off"),
    allow_rce: state(options.rceAllowed === false ? "off" : "on"),
    allow_tariff: state(options.tariffAllowed === false ? "off" : "on"),
    allow_rcm: state(options.rcmAllowed === false ? "off" : "on"),
  };
  const card = Object.create(Card.prototype);
  card._config = {
    language: options.language || "pl",
    canonical_timeline_entity: "timeline",
    supervisor_entity: "supervisor",
    supervisor_mode_entity: "mode",
    supervisor_paused_entity: "paused",
    control_conflict_entity: "conflict",
    supervisor_allow_rce_entity: "allow_rce",
    supervisor_allow_tariff_entity: "allow_tariff",
    supervisor_allow_rcm_entity: "allow_rcm",
  };
  card._hass = { states, config: { time_zone: options.timeZone || "Europe/Warsaw" } };
  return { card, now };
}

function withNow(now, callback) {
  const original = Date.now;
  Date.now = () => now;
  try { return callback(); } finally { Date.now = original; }
}

const rce = [
  slot("2026-09-15T17:00:00+02:00", "2026-09-15T18:00:00+02:00", "rce", "rce_export", 0.5, 38, { price: 1, revision: 1 }),
  slot("2026-09-15T18:00:00+02:00", "2026-09-15T19:00:00+02:00", "rce", "rce_export", 0.7, 35, { price: 2, revision: 2 }),
  slot("2026-09-15T19:00:00+02:00", "2026-09-15T20:00:00+02:00", "rce", "rce_export", 0.8, 32, { price: 3, revision: 3 }),
  slot("2026-09-15T22:00:00+02:00", "2026-09-15T23:00:00+02:00", "rce", "rce_export", 2.5, 27),
];

{
  const { card, now } = fixture("2026-09-15T18:00:00+02:00", rce);
  withNow(now, () => {
    const model = card._nextBlockModel();
    check(model.status === "current", "current plan status");
    check(model.block?.starts_at === "2026-09-15T22:00:00+02:00", "must skip the whole 17-20 block");
    check(model.block.slots.length === 1, "future block slot count");
    check(card._slotPlan(model.block) === "Plan 2,5 kWh · cel 27% SOC", "whole-block energy and final SOC");
    check(card._formatBlockRange(model.block, "pl-PL", card._copy()) === "15.09 22:00–23:00", "Polish date and whole range");
  });
}

{
  const tariff = [
    slot("2026-09-15T20:00:00+02:00", "2026-09-15T20:30:00+02:00", "tariff", "tariff_battery_charge", 0.4, 42, { price: 0.1 }),
    slot("2026-09-15T20:30:00+02:00", "2026-09-15T21:00:00+02:00", "tariff", "tariff_battery_charge", 0.6, 47, { price: 0.2 }),
    slot("2026-09-15T21:00:00+02:00", "2026-09-15T21:30:00+02:00", "tariff", "tariff_grid_support", 0.3, 47),
    slot("2026-09-15T22:00:00+02:00", "2026-09-15T22:30:00+02:00", "tariff", "tariff_battery_charge", 0.2, 49),
  ];
  const { card, now } = fixture("2026-09-15T19:00:00+02:00", tariff);
  withNow(now, () => {
    const normalized = card._normalizeNextBlocks(tariff);
    check(normalized.valid && normalized.blocks.length === 2, "only a real gap splits one tariff window");
    check(normalized.blocks[0].slots.length === 3, "price/SOC/tariff-phase changes do not split a window");
    check(normalized.blocks[0].selected_action === "tariff_grid_support_and_charge", "mixed window describes both tariff phases");
    check(card._slotPlan(normalized.blocks[0]) === "Plan 1,3 kWh · cel 47% SOC", "whole-window grid import includes home and battery");
    check(card._planActionText("tariff_grid_support", card._copy()) === "Zasilanie domu z taryfy", "tariff actions stay distinct");
  });
}

{
  const idlePlan = [
    slot("2026-09-15T18:00:00+02:00", "2026-09-15T19:00:00+02:00", "none", "none", 0, 40),
    slot("2026-09-15T22:00:00+02:00", "2026-09-15T23:00:00+02:00"),
  ];
  const { card, now } = fixture("2026-09-15T20:00:00+02:00", idlePlan);
  withNow(now, () => check(card._nextSlot()?.starts_at.includes("22:00"), "idle selects nearest future action block"));
}

for (const nowIso of ["2026-09-15T17:00:00+02:00", "2026-09-15T18:00:00+02:00", "2026-09-15T19:59:59+02:00"]) {
  const { card, now } = fixture(nowIso, rce);
  withNow(now, () => check(card._nextSlot()?.starts_at.includes("22:00"), `half-open/current boundary ${nowIso}`));
}

{
  const trimmed = [
    slot("2026-09-15T18:00:00+02:00", "2026-09-15T19:00:00+02:00"),
    slot("2026-09-15T19:00:00+02:00", "2026-09-15T20:00:00+02:00"),
  ];
  const { card, now } = fixture("2026-09-15T18:00:00+02:00", trimmed);
  withNow(now, () => check(card._nextBlockModel().status === "empty", "trimmed current continuation is not future"));
}

{
  const midnight = [slot("2026-09-16T23:30:00+02:00", "2026-09-17T01:00:00+02:00")];
  const { card, now } = fixture("2026-09-16T20:00:00+02:00", midnight);
  withNow(now, () => check(
    card._formatBlockRange(card._nextSlot(), "pl-PL", card._copy()) === "16.09 23:30–17.09 01:00",
    "midnight date labels",
  ));
  const dst = [slot("2026-10-25T02:00:00+02:00", "2026-10-25T02:00:00+01:00")];
  const dstFixture = fixture("2026-10-25T00:30:00+02:00", dst);
  withNow(dstFixture.now, () => {
    const text = dstFixture.card._formatBlockRange(dstFixture.card._nextSlot(), "pl-PL", dstFixture.card._copy());
    check(
      (text.includes("GMT+2") && text.includes("GMT+1"))
      || (text.includes("CEST") && text.includes("CET")),
      `DST offset distinction: ${text}`,
    );
  });
}

{
  const currentEmpty = fixture("2026-09-15T18:00:00+02:00", []);
  withNow(currentEmpty.now, () => check(currentEmpty.card._nextBlockModel().status === "empty", "complete empty plan"));
  const missing = fixture("2026-09-15T18:00:00+02:00", []);
  delete missing.card._hass.states.timeline;
  withNow(missing.now, () => check(missing.card._nextBlockModel().status === "unavailable", "missing source"));
  const missingControl = fixture("2026-09-15T18:00:00+02:00", rce);
  delete missingControl.card._hass.states.conflict;
  withNow(missingControl.now, () => check(missingControl.card._nextBlockModel().status === "unavailable", "missing control state cannot sustain a recommendation"));
  const missingPolicy = fixture("2026-09-15T18:00:00+02:00", rce);
  delete missingPolicy.card._hass.states.allow_rce;
  withNow(missingPolicy.now, () => check(missingPolicy.card._nextBlockModel().status === "unavailable", "missing policy state cannot sustain a recommendation"));
}

{
  const pending = fixture("2026-09-15T18:00:00+02:00", rce, { mode: "pending" });
  withNow(pending.now, () => check(pending.card._nextBlockModel().status === "retained", "strict retained pending plan"));
  const pendingEmpty = fixture("2026-09-15T18:00:00+02:00", [], { mode: "pending" });
  withNow(pendingEmpty.now, () => check(pendingEmpty.card._nextBlockModel().status === "retained_empty", "retained empty stays distinct from current empty"));
  const expired = fixture("2026-09-15T18:00:00+02:00", rce, {
    mode: "pending", builtAt: "2026-09-15T17:44:59+02:00",
  });
  withNow(expired.now, () => check(expired.card._nextBlockModel().status === "unavailable", "retained TTL"));
  const future = fixture("2026-09-15T18:00:00+02:00", rce, {
    mode: "pending", builtAt: "2026-09-15T18:00:06+02:00",
  });
  withNow(future.now, () => check(future.card._nextBlockModel().status === "unavailable", "future built_at"));
  const wrongBlocker = fixture("2026-09-15T18:00:00+02:00", rce, { mode: "pending", blocker: "missing_data" });
  withNow(wrongBlocker.now, () => check(wrongBlocker.card._nextBlockModel().status === "unavailable", "only recalculation pending may retain"));
}

for (const [name, options, expected] of [
  ["current off is informational", { enabled: false }, "disabled"],
  ["current pause is informational", { paused: true }, "paused"],
  ["conflict rejects recommendation", { conflict: true }, "conflict"],
  ["STOP rejects recommendation", { masterStop: "completed" }, "stopped"],
  ["policy off rejects recommendation", { rceAllowed: false }, "policy_off"],
]) {
  const { card, now } = fixture("2026-09-15T18:00:00+02:00", rce, options);
  withNow(now, () => {
    const model = card._nextBlockModel();
    check(model.status === expected, name);
    if (["conflict", "stopped", "policy_off"].includes(expected)) check(model.block === null, `${name} has no block`);
    else check(model.block?.starts_at.includes("22:00"), `${name} retains only a current informational plan`);
  });
}

for (const options of [{ enabled: false }, { paused: true }]) {
  const { card, now } = fixture("2026-09-15T18:00:00+02:00", rce, { mode: "pending", ...options });
  withNow(now, () => {
    const model = card._nextBlockModel();
    check(model.block !== null && model.retained, "off/pause keeps the same informational plan as the chart");
    check(model.status === (options.paused ? "paused" : "disabled"), "retained informational plan clearly identifies off/pause");
  });
}

{
  const invalidSets = [
    [slot("bad", "2026-09-15T19:00:00+02:00")],
    [slot("2026-09-15T18:00:00+02:00", "2026-09-15T19:00:00+02:00", "rce", "unknown")],
    [rce[0], { ...rce[0] }],
    [rce[0], slot("2026-09-15T17:30:00+02:00", "2026-09-15T18:30:00+02:00")],
  ];
  for (const slots of invalidSets) {
    const { card, now } = fixture("2026-09-15T16:00:00+02:00", slots);
    withNow(now, () => check(card._nextBlockModel().status === "invalid", "invalid/overlap/duplicate fails deterministically"));
  }
  const unsorted = [rce[3], rce[1], rce[0], rce[2]];
  const { card, now } = fixture("2026-09-15T18:00:00+02:00", unsorted);
  withNow(now, () => check(card._nextSlot()?.starts_at.includes("22:00"), "unsorted plan is normalized without mutation"));
  check(unsorted[0] === rce[3], "HA slots were not mutated");
}

{
  const nullEnergy = slot("2026-09-15T22:00:00+02:00", "2026-09-15T23:00:00+02:00", "rce", "rce_export", 1, 30);
  nullEnergy.soc_equation.battery_to_grid_kwh = null;
  nullEnergy.soc_equation.soc_end_percent = null;
  const { card, now } = fixture("2026-09-15T18:00:00+02:00", [nullEnergy]);
  withNow(now, () => check(card._slotPlan(card._nextSlot()) === "", "null energy/SOC is omitted, never zero"));
}

{
  const { card, now } = fixture("2026-09-15T18:00:00+02:00", rce, { language: "en" });
  withNow(now, () => {
    const block = card._nextSlot();
    check(card._formatBlockRange(block, "en-GB", card._copy()) === "15/09 22:00–23:00", "English date and range");
    check(card._planActionText(block.selected_action, card._copy()) === "Dynamic sales", "English action");
  });
}

{
  const plan = [
    slot("2026-09-20T05:30:00+02:00", "2026-09-20T06:00:00+02:00", "tariff", "tariff_battery_charge", 1),
    slot("2026-09-20T06:00:00+02:00", "2026-09-20T07:00:00+02:00", "tariff", "tariff_grid_support", 2),
    slot("2026-09-21T04:00:00+02:00", "2026-09-21T05:00:00+02:00", "tariff", "tariff_grid_support", 3),
    slot("2026-09-21T20:00:00+02:00", "2026-09-21T21:00:00+02:00", "rce", "rce_export", 20),
    slot("2026-09-21T22:00:00+02:00", "2026-09-21T23:00:00+02:00", "tariff", "tariff_battery_charge", 4),
    slot("2026-09-22T04:00:00+02:00", "2026-09-22T05:00:00+02:00", "tariff", "tariff_battery_charge", 5),
  ];
  const { card, now } = fixture("2026-09-20T05:45:00+02:00", plan);
  withNow(now, () => {
    const model = card._nextBlockModel();
    check(model.blocks?.length === 3, "show exactly the nearest three future windows");
    check(model.blocks[0].starts_at === plan[2].starts_at, "skip all of the current tariff window, including its later grid-support phase");
    check(model.blocks[1].selected_action === "rce_export", "use chronological supervisor-selected actions");
    check(card._slotPlan(model.blocks[1]).includes("20,0 kWh"), "whole RCE block energy");
    const stable = JSON.stringify(model.blocks.map(({ retained, ...block }) => block));
    for (let index = 0; index < 100; index += 1) {
      const attributes = card._hass.states.timeline.attributes;
      card._hass.states.timeline = state("pending", {
        ...attributes, canonical_status: "pending", canonical_blocker_code: "source_recalculation_pending",
        result_current: false, recalculation_pending: true,
      });
      const held = card._nextBlockModel();
      check(held.retained && JSON.stringify(held.blocks.map(({ retained, ...block }) => block)) === stable, "pending retains one whole, unchanged plan");
      card._hass.states.timeline = state("current", { ...attributes, result_current: true, recalculation_pending: false });
      check(JSON.stringify(card._nextBlockModel().blocks.map(({ retained, ...block }) => block)) === stable, "repeated current publication does not change displayed blocks");
    }
    // A newly committed selected plan replaces all rows, not just the first row.
    card._hass.states.timeline.attributes.slots = plan.slice(3);
    check(card._nextBlockModel().blocks[0].selected_action === "rce_export", "real accepted plan change updates the list immediately");
    card._hass.states.allow_rce = state("off");
    const filtered = card._nextBlockModel();
    check(filtered.blocks.length === 2 && filtered.blocks.every(block => block.selected_policy === "tariff"), "disabled policy cannot survive in the retained list; show fewer than three when appropriate");
    card._hass.states.timeline.attributes.slots = [slot("2026-09-21T20:00:00+02:00", "2026-09-21T21:00:00+02:00", "none", "none", 0, 30, { rejected_candidates: [{ selected_action: "rce_export" }] })];
    check(card._nextBlockModel().block === null, "rejected optimizer candidates are never presented as selected actions");
  });
}

{
  // Canonical sensor publishes these flags only while pending, not on a completed ledger.
  const { card, now } = fixture("2026-09-15T18:00:00+02:00", rce);
  delete card._hass.states.timeline.attributes.result_current;
  delete card._hass.states.timeline.attributes.recalculation_pending;
  withNow(now, () => {
    check(card._nextBlockModel().block?.starts_at.includes("22:00"), "real current canonical payload without pending-only flags remains visible");
    card._hass.states.timeline.attributes.result_current = false;
    check(card._nextBlockModel().block === null, "explicit contradictory freshness flag fails closed");
  });
}

console.log("NEXT-THREE-01 selector: behavioral matrix PASS");
