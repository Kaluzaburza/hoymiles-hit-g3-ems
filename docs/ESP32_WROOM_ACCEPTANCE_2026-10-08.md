# Local ESP32-WROOM acceptance profile

A PCB label such as ESP32-WROOM v1.1 does not identify the silicon revision.
Read the boot log before selecting the hardware profile. Retain the classic
`esp32dev` target and the existing UART pins and credentials for a classic ESP32.

The optional `examples/esphome/esp32-wroom-diagnostic.yaml` profile uses the
conservative classic-ESP32 memory configuration, without the public example's
minimum-3.1/SRAM1-as-IRAM assumption. It does not select an S3 or assume PSRAM.
Its single 60-second interval emits two short local log lines: uptime, reset reason, internal heap,
minimum/largest free allocation and the running ELF hash. It adds no HA entity,
Recorder write, Modbus request or control action. INFO logging must be enabled
on the acceptance host. All 17 inverter packages remain exactly at frozen
base `a29871a5ab07254130235a76e90dbc22598590f5`, including the 2026.9 migration.

`CONFIG_APP_RETRIEVE_LEN_ELF_SHA=64` is required: IDF's hash accessor returns
only the configured prefix even when supplied with a 65-byte destination.
The first diagnostic OTA showed a nine-character prefix; a focused regression
test reproduces that omission. This logging correction does not fix or claim
to reproduce the earlier Modbus/FreeRTOS crashes.

This is an explicitly authorized diagnostic update, not a demonstrated fix
for the two retained LoadProhibited traces. The c2a8786 comparison's matching
ELF places them in ModbusClientHub::sweep_ and xQueueTakeMutexRecursive, but
does not establish cause. The comparison and earlier failed observations
remain preserved. A successful build alone does not pass field acceptance.

Validate the merged profile, actual per-host build and exact artifact hashes;
then upload only to the identified classic ESP32 while neutral/Off/paused. Verify
fresh complete FC03, BMS/topology, unchanged settings and at least ten minutes
of uptime/log continuity. A crash, missing telemetry or gap blocks acceptance.
Restore the user's original Active/unpaused state only after acceptance.
An ESP32-S3 must not receive this classic ESP32 profile or binary.

This optional diagnostic profile is separate from the three public release
profiles. Device-specific evidence and credentials remain outside Git.
