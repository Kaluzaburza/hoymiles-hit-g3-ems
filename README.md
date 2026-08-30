# EMS for Hoymiles HIT-(5–20)L-G3

[English](README.md) · [Polski](README.pl.md)

Unofficial local EMS for Hoymiles HIT-G3 hybrid inverters — Home Assistant,
ESPHome, Modbus, RCE, tariff optimization and RCEm.

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Kaluzaburza&repository=hoymiles-hit-g3-ems&category=integration)
[![Latest release](https://img.shields.io/github/v/release/Kaluzaburza/hoymiles-hit-g3-ems?label=release)](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/releases/latest)
[![Validate](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/actions/workflows/validate.yml/badge.svg)](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

The project connects an ESP32 to the inverter over Modbus RTU and adds a
localized Home Assistant integration, the Aurora dashboard, and optional EMS
automations. All control logic runs locally. Forecast-based features use
Solcast, and RCE optimization uses the public PSE price API.

**New installation:** follow the [five-step quick start](docs/QUICK_START.md).
An installation and feature overview is also included below.

## Overview

![Aurora dashboard: live energy, RCE, tariff charging and RCEm](docs/images/dashboard-overview.png)

The default dashboard focuses on current operation, planned actions, and
results. Expert sections expose model inputs, calculated reserves, physical
limits, data quality, and the reason why an action was executed or blocked.
The interface is available in English and Polish and adapts to desktop and
mobile screens.

### Included functions

| Area | Included capability |
|---|---|
| Local monitoring | PV1–PV4, household load, grid, battery, BMS, GEN, inverter state, alarms, temperatures, energy totals, and parallel-system totals |
| Inverter control | Self-Use, Off-Grid, Grid Charge, Grid Discharge, charge and discharge limits, SOC targets, schedules, and explicit export control |
| RCE optimization | Uses a revenue-first, bounded joint-horizon plan for permitted 30-minute export intervals while protecting the operating reserve |
| Tariff charging | Moves necessary grid charging into cheaper tariff periods while protecting a hard household reserve, including winter-load risk |
| Experimental RCEm 253 V+ | Detects recurring high-voltage periods and models battery headroom; its public-test default is read-only shadow mode |
| Battery service | Schedules LiFePO4 balancing cycles, prioritizes PV, completes charging from the grid when needed, and holds full SOC for a configured period |
| Diagnostics | Installation status, control conflicts, input freshness, command acknowledgment, privacy-filtered ZIP reports, and detailed optimizer data |

Only one automatic module may write EMS settings at a time. Mutual-exclusion
rules prevent RCE, tariff charging, active RCEm control, battery balancing, and
manual schedules from issuing conflicting commands. RCEm shadow analytics may
run beside another controller because shadow mode performs no writes.

## Compatibility and requirements

| Component | Requirement |
|---|---|
| Inverter | Hoymiles HIT xxL G3 family; development and field testing have focused primarily on HIT-10L-G3 and HIT-20L-G3 installations |
| ESP32 | ESP32 board supported by ESPHome; the public configuration defaults to `esp32dev` |
| RS485 interface | UART/TTL-to-RS485 converter with **3.3 V UART logic**; an automatic-direction model is recommended |
| ESPHome | 2026.7 or newer |
| Home Assistant | 2026.7 or newer |
| HACS | 2.x |

Forecast-based planning for RCE optimization, tariff charging, and RCEm requires
[BJReplay Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar).
Solcast Day 3 is optional and is commonly disabled by default. Enable it when
available; known current and legacy entity IDs are detected automatically,
while a renamed or custom source can be selected with the Day 3 entity helper.
Missing or stale Day 3 is reported explicitly and does not disable the
conservative shorter-horizon fallback.
RCE prices require internet access to the public PSE API. Home Assistant
Recorder must retain the history of the relevant power and energy entities; this is enabled
by default in a standard installation. An additional household energy meter is
not required.

The standard Modbus settings are `115200 8N1`, unit address `1`.

## Architecture

```text
Hoymiles inverter ── RS485 / Modbus RTU ── ESP32 / ESPHome
                                                   │
                                           native ESPHome API
                                                   │
                                      Home Assistant integration
                                          │                  │
                                   Aurora dashboard      Local EMS

HACS installs and updates the Home Assistant integration.
ESPHome downloads the versioned register packages directly from GitHub.
```

The Home Assistant integration creates stable, localized proxy entities from
the native ESPHome device. It forwards both changed states and unchanged fresh
reports, without adding another Modbus polling cycle.

## Safety

> [!IMPORTANT]
> **The EMS manages energy; it does not manage electrical or grid safety.**
> It does not change certified grid profiles, protection thresholds, or the
> three-phase imbalance setting. It cannot disable the inverter's protection
> functions.

> [!WARNING]
> This project can write operating parameters to a high-power inverter. Before
> enabling writable entities or automatic control, verify the exact inverter
> model, register map, RS485 wiring, battery and BMS limits, and distribution
> system operator requirements. Use the software at your own risk.

EMS writes are limited to documented operational controls. Mode changes write
the complete register block `4300–4306` with Modbus function 16 (`0x10`, Write
Multiple Registers). Writing only register `4300` can leave the inverter with
an inconsistent EMS configuration.

The project implements EMS functions that may support a documented technical
acceptance process. It is **not** a formal certificate for the inverter,
battery, or complete electrical installation, and it does not confirm
eligibility for any subsidy program. See the
[safety and functional mapping](docs/SAFETY_AND_COMPLIANCE.md).

## Installation

### 1. Install the Home Assistant integration through HACS

1. Use the **Open this repository in HACS** button at the top of this page.
2. If adding it manually, open **HACS → three-dot menu → Custom repositories**,
   enter
   `https://github.com/Kaluzaburza/hoymiles-hit-g3-ems`, and select
   **Integration**.
3. Install **EMS for Hoymiles HIT-(5–20)L-G3** and restart Home Assistant.

HACS installs the Home Assistant component. ESPHome firmware is configured in
a separate step and downloads its own versioned packages from this repository.

### 2. Connect the ESP32 and RS485 converter

Two converter types are common. Confirm the actual module specification rather
than relying on a product listing.

| Converter type | Typical UART-side pins | ESPHome configuration |
|---|---|---|
| **Automatic direction — recommended** | `VCC`, `GND`, `TXD`, `RXD` (sometimes `DI`, `RO`) | No direction-control pin |
| **Manual direction, such as a MAX3485-based module or a properly level-shifted MAX485 module** | `VCC`, `GND`, `DI`, `RO`, `DE`, `/RE` | Join `DE` and `/RE`, connect them to one ESP32 GPIO, and configure `flow_control_pin` |

#### Automatic-direction converter

```text
ESP32                        RS485 converter                  Inverter
GPIO17 (TX)  ------------->  RXD / DI
GPIO16 (RX)  <-------------  TXD / RO
3.3 V        ------------->  VCC  (only if rated for 3.3 V)
GND          --------------  GND  -------------------------- GND / reference
                              A / D+ ------------------------- A+ / D+
                              B / D- ------------------------- B- / D-
```

`TX` must reach the converter input (`RXD` or `DI`), and `RX` must receive the
converter output (`TXD` or `RO`). Labels differ between modules, so check the
signal direction in the converter documentation.

#### Converter with `DE` and `/RE`

Use the same power, data, and RS485 connections, then join the direction pins:

```text
MAX3485 DE ----+
               +------------ GPIO4 (example)
MAX3485 /RE ---+
```

Add this block outside the existing `packages:` section in
`hoymiles-inverter.yaml`. It extends the UART created by the package:

```yaml
uart:
  - id: !extend modbus_uart
    flow_control_pin:
      number: GPIO4
      inverted: false
```

Use another suitable output-capable GPIO when GPIO4 is unavailable. Do not
create a second Modbus hub.

#### Before powering on

1. Before changing wiring, isolate every energy source connected to the hybrid
   inverter—AC grid, PV, battery, EPS/backup, and GEN where present—by following
   the manufacturer's shutdown procedure. A qualified person must verify the
   absence of voltage before work begins.
2. Verify that the converter uses 3.3 V UART logic. Never connect a 5 V `RO`
   output or apply any other 5 V signal to an ESP32 GPIO.
3. Connect `A/D+` to `A+/D+`, `B/D−` to `B−/D−`, and the Modbus reference/GND
   when required by the inverter manual.
4. Never connect ESP32 `3.3 V` or converter `VCC` to an inverter communication
   terminal.
5. Use the port documented for the exact inverter model as its external
   RS485/Modbus port. Do not select a `Parallel` connector only because the plug
   looks identical.
6. If communication is still unavailable after the other checks, repeat the
   complete isolation and voltage-verification procedure before checking whether
   the converter uses reversed `A/B` labeling.

### 3. Flash the ESP32

1. Create an ESPHome device or copy the public entry configuration
   [`hoymiles-inverter.yaml`](hoymiles-inverter.yaml) into the ESPHome
   configuration directory.
2. Copy the keys from [`secrets.yaml.example`](secrets.yaml.example) to the
   local `secrets.yaml` and replace every example value.
3. Confirm the board, `uart_tx_pin`, and `uart_rx_pin` substitutions.
4. Add the `!extend modbus_uart` block only when the converter needs manual
   `DE`/`/RE` control.
5. Validate, compile, and install the firmware.

Do not copy the repository's `packages` directory. The public entry file
downloads the compatible, versioned ESPHome packages directly from GitHub.

If compilation succeeds but all Modbus entities remain unavailable, check the
converter type and direction pins, common reference/GND, `A/B` polarity,
inverter port, and Modbus address—in that order.

### 4. Add both integrations

1. Add the discovered ESP32 through Home Assistant's standard **ESPHome**
   integration.
2. Open **Settings → Devices & services → Add integration**.
3. Select **EMS for Hoymiles HIT-(5–20)L-G3** and choose the ESPHome device.

The integration automatically installs and registers:

- `/config/dashboard_hoymiles.yaml` for legacy or manual dashboard use;
- `/config/packages/hoymiles_ems_scheduler.yaml`;
- the versioned Aurora frontend module.

If Home Assistant packages are not enabled, **Settings → System → Repairs**
shows the required action. When `configuration.yaml` does not yet contain a
`homeassistant:` section, add:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

When that section already exists, add only the indented `packages:` line below
its existing entries—do not add a second `homeassistant:` key. Validate the
configuration and restart Home Assistant.

### 5. Add the Aurora dashboard and verify the installation

1. Open **Settings → Dashboards → Add dashboard**.
2. Select the community dashboard **EMS for Hoymiles HIT-(5–20)L-G3**.
3. Open the integration and confirm **Installation status = Ready**.
4. Review **Settings → System → Repairs** before enabling automatic writes.

The dashboard strategy loads the English or Polish dashboard bundled with the
installed integration. It therefore updates after a HACS update and Home
Assistant restart without requiring users to paste YAML again.

## Updating

1. Read the **User update steps** in the HACS release notes.
2. Install the update through HACS.
3. Restart Home Assistant once.
4. Check **Installation status** and **Repairs**. An update from an older
   release can require one additional restart after the managed EMS package has
   been copied.
5. Rebuild ESP32 firmware only when the release notes explicitly require it.

The integration preserves user-modified copies of managed files. Built-in
migrations for storage-mode dashboards update only the required managed card types, entity
rows, or asset paths and create a `.pre-<version>.bak` backup before writing.

To replace local asset copies deliberately, first back up every customized
dashboard and EMS package file, then call:

```yaml
action: hoymiles_hit_modbus.install_assets
data:
  overwrite: true
```

Do not add a manual `/local/hoymiles-rce-chart-card.js` Lovelace resource. The
integration registers its versioned frontend module automatically.

## EMS automation

### Operating rules shared by every mode

- Automatic control is optional and disabled until configured by the user.
- RCE, tariff charging, and write-capable RCEm control are mutually exclusive.
  RCEm shadow analytics may remain enabled because they perform no writes.
- Off-Grid is a user/inverter-owned physical mode. Automatic controllers do not
  start or update writes while it is active, and cleanup does not force a return
  to Self-Use. The owner diagnostic describes an active transaction, not merely
  an enabled policy.
- A LiFePO4 balancing cycle temporarily has higher priority than other plans.
- Every source has a signed age. Missing, stale, or future-dated critical data
  block automatic writes and, when necessary, trigger a controlled return to
  Self-Use. A reported zero capability is treated as a real zero limit, never
  as unlimited power.
- Shared, policy-neutral helpers sanitize LOAD history and derive parallel
  system power from the 32-bit PV/Grid/LOAD balance. Each planner still keeps
  its own objective, reserve model, and simulation.
- Battery, inverter, shared AC and export budgets, parallel-system readiness,
  export lockouts, natural PV export, household load, and the Generation
  Control Function (GCF) limit are checked without double-counting capacity.
- EMS ownership is claimed before a write. Commands are rate-limited, latched
  where necessary, and ownership is released only after a newer, independent
  FC03 register read confirms the requested values and neutral mode. An
  optimistic Home Assistant/ESPHome command echo is never accepted as hardware
  acknowledgement. If physical feedback is missing or disagrees, restoration
  is retried and another controller cannot take over.
- The integration does not automate the three-phase imbalance setting.

### RCE market-price optimization

RCE has one purpose: maximize expected net sale revenue within the permitted
30-minute PSE price horizon. Its bounded joint-horizon planner evaluates the
slots together instead of making an isolated greedy choice. It models energy
available now, PV energy only when it can physically arrive, natural export,
conversion losses, battery capacity, BMS and inverter power, shared AC/export
budgets, GCF and configured export lockouts.

The operating reserve is rounded conservatively to a full inverter SOC step
and enforced in every planned export interval. LOAD and Day 3 data remain
visible as diagnostics, but Day 3 does not create a terminal objective that can
silently turn the sale optimizer into a tariff or household-cost optimizer.
The implementation is a bounded active-set heuristic, not an exact solver. An
independent oracle checks small constructed horizons and has found only small
observed gaps in the covered cases; this is regression evidence, not proof of
a formal or global optimum for the full mixed-constraint problem.

The dashboard separates projected and measured results, controlled battery
export, natural PV surplus, and unclassified historical export. It reports
gross sale revenue and an estimated net benefit after modeled battery wear.
These figures are estimates, not a supplier invoice, settlement statement, or
guarantee of profit.

### Tariff-aware grid charging

Tariff charging has a different objective from RCE: buy only the energy the
house is expected to need, and shift that purchase into the lowest-cost
available tariff periods. It simulates household demand, PV generation, and
battery SOC in 30-minute steps. The winter model uses a conservative high-load
profile and a hard Self-Use household reserve. Fresh third-day Solcast data can
extend the simulated horizon to at least 48 hours. If that tail is missing or
stale, the shorter known horizon is reported and the unknown period is
protected with zero PV and conservative household demand.

The calculation includes BMS charge limits, conversion losses, shared AC power,
and the Grid Charge limit, which supplies the house before the remaining power
reaches the battery. It suppresses uneconomic micro-cycles, can learn effective
battery charging power from confirmed sessions, and starts early enough to
store the required energy before a more expensive tariff period. It does not
optimize export revenue.

Bundled profiles cover G11, G12, G12w, and G13 where offered by PGE, TAURON,
ENEA, ENERGA, and STOEN. They include seasons, weekends, and Polish public
holidays. Built-in 2026 variable per-kWh rates include the modeled variable
components, but not fixed charges. Treat them as a starting point and verify
them against the current supplier contract and bill. Use the **Manual** profile
for another product or supplier.

### Experimental RCEm 253 V+ voltage management

RCEm has a third, independent objective: preserve usable battery headroom around
recurring high-voltage and PV-surplus risk. It analyzes the previous four days
of phase-voltage history together with live L1/L2/L3 voltage, the rolling
10-minute average, interval Solcast profiles, weekday/weekend household demand,
and available battery capacity. High-PV/low-LOAD conditions size headroom;
low-PV/high-LOAD stress and a chronological energy balance protect household
energy. RCEm does not inherit the RCE operating floor.

Outside shadow mode, the controller can increase battery charging as voltage
rises. Optional morning discharge can create only the useful headroom needed
before a later risk window, without crossing its household safety reserve.
Optional export regulation never exceeds the lower of the physically available
export budget, the current inverter setting, and the user-defined cap.

RCEm starts in **observation-only (shadow) mode**. In this mode it calculates
plans and diagnostics but performs no inverter writes, so it can collect public
test evidence alongside RCE or tariff control. Keep shadow mode enabled until
write-capable RCEm has passed separate commissioning on the target plant. RCEm
does not disable certified protection, change
protection thresholds, enable GCF, or alter three-phase imbalance. It remains
experimental and requires separate field validation and commissioning before
write-capable use. It is not intended to bypass applicable grid-code or
distribution-system-operator voltage limits.

### LiFePO4 battery balancing

The optional service cycle runs at an interval selected by the user. After
sunrise, normal BMS-safe Self-Use operation lets PV charge the battery to 95%
SOC first. The first valid reading at or above 95% latches a slow region for the
rest of that cycle, targeting no more than approximately 0.4 kW aggregate net
battery charge. Self-Use applies the direct battery cap; after sunset, verified
Grid Charge adds the current household load once to the same 0.4 kW battery
target. The hold timer begins only after the required mode and register
readbacks are acknowledged at `99.9%` SOC. A lower reading cancels the timer
and requires a new uninterrupted hold without returning to full-power charging.
Short freshness-only gaps in Self-Use pause writes for at most 60 seconds;
safety failures are captured as a durable hard-stop request.
Hard-stop evidence is frozen when its trigger is admitted, so a short
fault/recovery transition is not reinterpreted later from the current state.
Independent bounded FIFO admission lanes for each hard-stop priority class
prevent a lower-priority burst from consuming the later higher-priority
admission; the durable merge keeps the first exact reason at equal priority.
An already running verified helper call cannot be cancelled; the durable
request blocks every later
non-restorative write and restoration starts at the first safe serialized
worker boundary. Before the first physical write, both FC03 generation values
must equal—not merely exceed—the trusted snapshot generations. Immediately
before `HOLD_ARMING`, SOC is read again and must still be finite, fresh and at
least 99.9%; the timing-write boundary repeats that check and clears an invalid
arming candidate without starting the timer. The final steady-phase commit
re-reads the current sun state, acknowledged EMS mode, cycle, owner generation
and hard-stop latch; a mismatch permits at most one verified mode correction,
while a second sun change fails closed. Its durable record remains raw
`APPLYING` with a conditional phase/mode token, and the canonical parser exposes
`OPERATIONAL`, `SLOW` or a hold phase only while the current sun, mode, owner,
cycle, timing and no-abort guards still match. Routine internal phases do not
produce generic phone pushes.
A trusted snapshot tied to the current cycle is restored only after matching
physical acknowledgements; physical Off-Grid takes priority. An active legacy
or malformed cycle without that provenance enters `RECOVERY_REQUIRED`: it
performs no guessed restore and does not release an existing balancing owner
automatically. The physical `operational_started` fact is committed independently
of free outbox capacity, so a later abort remains ABORTED even if STARTED had to
wait. Terminal phone events use a durable outbox outside physical closeout. Each
provider attempt receives a durable 15-second lease; expiry enables a bounded
retry, restart recovers an abandoned lease, and a late completion cannot update
a newer attempt. Retries reuse the same stable tag, which can replace/deduplicate
the visible notification where the target supports tags; arbitrary external
push delivery is not mathematically exactly-once. These changes are validated
offline and still require exact-version field acceptance before release.

If `RECOVERY_REQUIRED` is shown, keep balancing disabled and do not clear its
active/lifecycle helpers. Save the transaction reason, cycle identifier,
current physical mode and 4303/4304 readbacks, then export a support bundle. A
qualified operator must establish the intended settings from commissioning
records or the manufacturer's controls—not from the untrusted legacy record—and
confirm fresh physical readbacks. Only after the timers and verified-write
scripts are idle and that evidence has been reviewed may a maintainer release
the retained owner/reset the internal lifecycle and start a new cycle. Until
then the fail-closed owner remains reserved; this release intentionally does
not provide transparent migration of an unverifiable active cycle.

## Parallel inverter systems

The standard ESPHome configuration reads topology registers `6048–6095` and
distinguishes a single inverter, Master, and Slave automatically. No manual
inverter-count setting is required.

For parallel EMS control, the ESP32 converter, the Master and **every Slave**
must share one physical external Modbus/RS485 multidrop bus. On the verified
two-inverter HIT installation this is the `RS485_2` bus. Carry A, B and the
reference/GND required by the manufacturer to every inverter; connecting the
ESP32 only to the Master is not sufficient.

```text
ESP32 -> isolated RS485 converter -> Master external Modbus -> Slave 1 external Modbus -> ... -> Slave N
```

Wire this as a line/daisy chain, not a star, and terminate only the physical
ends as specified by the inverter and converter manuals. The external Modbus
bus used by the ESP32 is separate from the inverter's dedicated internal
Parallel/DTS communication bus; do not bridge the two buses.

The v1.5.6 firmware carries forward the system command verified earlier on a
two-inverter HIT shared-bus test configuration: every change to EMS registers
`4300–4306` is sent as one FC16 broadcast to Modbus address `0`. RCE, tariff
charging, manual schedules, and battery balancing may therefore control a detected
Master system **only when every inverter is physically present on that same
external RS485 bus**. Address `0` is broadcast on the wire where the frame is
sent; the Master does not relay an external Modbus command to Slaves through
the internal parallel network. The command has no Modbus reply; Home Assistant
accepts it only after a newer physical FC03 from the Master contains the exact
requested block. This confirms the Master block, not receipt or execution by
each Slave.

For Grid Discharge on a detected parallel Master, v1.5.6 adds a separate
post-command **aggregate physical-response** diagnostic. After the Master FC03
configuration acknowledgement it applies 20 seconds of transition grace, then
examines five newer complete system-power generations (up to 20 seconds each)
and requires three consecutive stable generations. The diagnostic changes from
`pending` to `confirmed`, `not_confirmed`, or `not_evaluable`. RCE compares that
response with the target frozen before the command, additionally requires grid
export of at least 0.25 kW, and fails closed through the existing neutral
rollback when it is not confirmed. Manual/manual-recovery and RCEm pre-discharge
have no authoritative total-kW target, so they evaluate fresh stable battery-
discharge direction without a grid-export or amplitude rejection. Return to
Self-Use never waits for aggregate discharge evidence.

The two new diagnostics are
`sensor.hoymiles_hit_parallel_aggregate_power_readback_generation` and
`sensor.hoymiles_parallel_aggregate_physical_response`.

This signal confirms only a system-level physical response. It is not an FC03
readback from a Slave and must not be called per-Slave acknowledgement. A sampled
transition peak is recorded for diagnosis but is excluded from the stable
confirmation window and is not, by itself, an execution error.

During commissioning, verify Grid Discharge and the return to Self-Use on the
Master and on every Slave separately in the manufacturer application. A
`Ready` installation status, correct system-wide telemetry and a matching
Master FC03 can all remain present when the ESP32 cable reaches only the
Master, so none of them proves the physical Slave branch.

On 2026-08-15 a shared-bus field run provided additional hardware and protocol
evidence: aggregate battery discharge stabilized at 33.653 kW and 33.863 kW,
and the operator confirmed Grid Discharge on both nodes in the manufacturer
application. That installation was still running managed Home Assistant
package 1.5.4 and ESP32 firmware/project 1.5.3. No per-node power, screenshot,
exact vendor timestamp or exact manual-stop command timestamp was retained.
Home Assistant history for this installation was also inspected over the
8–14 August evening windows (19:00–22:00 local). It shows an approximately
60 kW stop transient on 8 August and an approximately 60 kW start transient
aligned with the stored mode-code change on 14 August. The 9–13 August windows
show repeated discharge plateaus and switching impulses, although recorder
sampling may miss their full peaks. Separately, the 15 August live trace at
18:20 local captured 63.069 kW battery / 65.910 kW inverter during a switch.
This date- and installation-bounded observation supports transition grace and
a stable window; it is not a universal guarantee about all inverters.

The run therefore does not accept the v1.5.5 or v1.5.6 software, does not prove
an automatic stop and does not turn Master FC03 into per-Slave acknowledgement.
An exact-version v1.5.6 retest remains required.

Registers `258`, `259`, and `306` are outside that complete EMS block and do
not yet have separately proven Master/Slave broadcast semantics. Active RCEm
actions that need those registers remain fail-closed on a parallel system;
RCEm shadow analytics remain available. Do not disable either readiness gate.

The Aurora dashboard automatically uses the manufacturer's system-wide power
registers for PV, battery, household load, and grid power. The addresses listed
in topology registers are internal parallel-network diagnostics; the ESP32 does
not poll them as separate Modbus unit IDs through the Master's external port.

The entity that writes register `3016` (**Parallel Networking Command**) remains
disabled by default. This commissioning command creates or disassembles the
parallel network and is never used by the EMS automation.

Refer to the inverter manual for maximum unit counts, contactor requirements,
meter and DTS placement, and termination of the first and last device on the
dedicated parallel communication bus.

## Firmware compatibility

The integration creates stable proxy entities for the complete catalog. If the
installed ESPHome firmware predates a new register, its proxy remains present
but unavailable and reports `firmware_update_required: true`. Recompiling and
flashing the ESP32 firmware with the current packages activates the same entity
without changing its entity ID or unique ID.

The integration release and ESPHome package release are versioned separately.
Always follow the compatibility information in the release notes rather than
assuming both version numbers must match.

## Diagnostics and support

Before reporting a problem, record:

- exact inverter model and firmware version;
- ESPHome and Home Assistant versions;
- local date and time of the event;
- expected and observed behavior;
- relevant logs with credentials and personal data removed.

Download the privacy-filtered diagnostics ZIP from the final **Diagnostics**
dashboard view or use Home Assistant's native **Download diagnostics** action.
For ESPHome, Modbus, startup, or automation-loop problems, the extended terminal
collector is also available. The exact contents and anonymization rules are
documented in [Diagnostics](docs/DIAGNOSTICS.md).

Each Home Assistant installation also receives one random, persistent UUID v4
used only to correlate successive diagnostic packages from that installation.
It is not derived from an inverter, network, account, config entry, or other
user data. The privacy-preserving offline
[diagnostic analyzer](docs/DIAGNOSTICS_ANALYZER.md) can process up to 100 ZIP
packages at once and compare RCE, RCEm, and tariff-charging behavior without
contacting an external service.

Review every archive before attaching it to a public issue. Automated filtering
does not replace a manual check for credentials and personal data.

Open a [GitHub issue](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/issues)
or send the report with a problem description to
[info@kaluzaaa.com](mailto:info@kaluzaaa.com).

If the ESPHome log window repeatedly reports `SocketClosedAPIError` while
entities continue to update, close duplicate log streams, wait approximately
15 seconds, and restart only the ESPHome Device Builder add-on. See the
[diagnostics guide](docs/DIAGNOSTICS.md) before reflashing the ESP32.

## Documentation

| Document | Purpose |
|---|---|
| [Quick start](docs/QUICK_START.md) | Short installation path for new users |
| [Diagnostics](docs/DIAGNOSTICS.md) | Report collection, anonymization, and troubleshooting |
| [Diagnostic analyzer](docs/DIAGNOSTICS_ANALYZER.md) | Offline comparison of up to 100 diagnostic ZIP packages |
| [Safety and functional mapping](docs/SAFETY_AND_COMPLIANCE.md) | Implemented safeguards, boundaries, and audit evidence |
| [Automation test report](docs/AUTOMATION_TEST_REPORT.md) | Simulation scope, static control checks, and field-test limits |
| [Changelog](CHANGELOG.md) | Release history and required update steps |
| [Release procedure](RELEASING.md) | Maintainer checklist for GitHub and HACS releases |

## Development

```text
custom_components/hoymiles_hit_modbus/  Home Assistant integration
packages/                               ESPHome Modbus register packages
examples/esphome/                       ESPHome example configuration
home_assistant/                         Source dashboard card and EMS package
docs/                                   User, safety, and test documentation
tools/                                  Asset generators and release tests
```

Regenerate localized catalogs and bundled assets with:

```bash
python tools/build_hacs_assets.py
```

GitHub Actions run HACS validation, Hassfest, frontend checks, and project
tests. Contributions must follow [CONTRIBUTING.md](CONTRIBUTING.md) and include
the required `Signed-off-by` line.

## Support the project

The integration and EMS functions are free and open-source software. If the
project is useful, you can support continued development, documentation, and
testing:

[☕ Support development](https://buycoffee.to/kaluzaaa)

## License

This project is available under the [MIT License](LICENSE). Private and
commercial use, modification, and distribution are permitted when the
copyright and permission notices are retained. The software is provided
without warranty.
