# ESPHome device variants / Warianty urządzenia

Compatible with stable integration **1.5.8**. Firmware is unchanged from RC2;
existing users of these exact packages do not need to flash again.
**PL:** przejście z RC2 na stabilną integrację 1.5.8 nie wymaga nowego OTA.

All three files use the same complete register packages from **`v1.5.8RC2`**,
lease protocol **2**, UART `115200 8N1` and inverter address `1` by default.
Change pins/address only to match your existing installation. HACS does not
select a board or flash firmware.

Wszystkie warianty mają te same funkcje EMS i pakiety. Wybierz plik według
sprzętu, zachowaj własne sekrety i tożsamość urządzenia. Nie wgrywaj obrazu
ESP32 na ESP32-S3 ani odwrotnie.

| Hardware / Sprzęt | Complete device YAML / Pełny YAML | UART / direction |
|---|---|---|
| Classic ESP32/WROOM, automatic-direction converter | [hoymiles-inverter.yaml](../hoymiles-inverter.yaml) | TX GPIO17, RX GPIO16 |
| ESP32-S3 DevKitC-1 WROOM-1 **N16R8** | [hoymiles-inverter-s3.yaml](../hoymiles-inverter-s3.yaml) | TX GPIO17, RX GPIO16; automatic converter |
| Classic ESP32/WROOM, DE + `/RE` converter | [hoymiles-inverter-flow-control.yaml](../hoymiles-inverter-flow-control.yaml) | TX GPIO17, RX GPIO16, DE + `/RE` GPIO4 |

Classic profiles retain `esp32dev`, ESP-IDF, minimum **silicon revision 3.1**
and SRAM1-as-IRAM. A PCB marking such as **v1.1 is not the silicon revision**.
Check ESPHome boot identification; these profiles do not promise compatibility
with older silicon. Preserve a working board/bootloader configuration instead
of assuming all WROOM modules are equivalent.

S3 uses `esp32-s3-devkitc-1`, variant `ESP32S3`, **16 MB flash** and **octal
PSRAM at 80 MHz** for N16R8. It does not inherit classic ESP32-only advanced
flags. Other S3 memory variants require their own validated settings.
For a first installation or different partition layout, use **USB**. Ordinary
application OTA does not silently resize an old 4 MB partition table; retain
an existing compatible table or commission the new layout explicitly over USB.

**PL:** wariant klasyczny wymaga rewizji krzemu 3.1, niezależnie od nadruku
wersji płytki. S3 N16R8 ma 16 MB flash i 8 MB PSRAM; nie kopiuj do niego
zaawansowanych ustawień klasycznego ESP32. Pierwsze wgranie i zmianę układu
partycji wykonaj przez USB. Samo ustawienie `flash_size: 16MB` nie przenosi
partycji istniejącego urządzenia.

Use the complete file and the four keys in [secrets.yaml.example](../secrets.yaml.example).
The default OTA requires encryption with the existing API key. Older firmware
needs the [two-stage upgrade](UPGRADE_1_5_7.md#older-ota-firmware--starsze-ota).
Do not keep an old `packages.ref` or import from moving `main` for a frozen release.

For manual direction, connect DE and `/RE` to the configured direction pin;
do not enable this on an automatic converter. The root UART override is a
mapping, not a list with `!extend`. Validate the **complete device file**.
The [wiring guide](WIRING_ESP32_S3.md) explains electrical labels and limits.

Polling remains **5 s control**, **13 s fast telemetry**, **20 s settings**
and **150 s full map**. Only sparse internal diagnostics retain **1500 s**.
One UART and Modbus hub serve all controllers; these are not extra RS485 readers.

CI parses every public entry and compiles all three complete profiles with
ESPHome **2026.9.0**. It does not upload images or establish physical wiring.
Device-specific binaries contain local secrets and are not public release assets.

References: [ESP32 platform](https://esphome.io/components/esp32/),
[PSRAM configuration](https://esphome.io/components/psram/),
[Espressif DevKitC-1](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide.html).
