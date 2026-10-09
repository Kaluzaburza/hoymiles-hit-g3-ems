# ESP32 configuration migration — 1.5.8RC2

This is an ESPHome 2026.9 configuration update. It does not change the inverter
register map or interpret an unsupported register as zero/healthy.

- Replace the ignored per-sensor `skip_updates: 9` with one controller at the
  same Modbus address, polling static addresses 6050/6055/.../6095 every 1500 s.
  The shared 100 ms turnaround remains. Topology 6048–6049 still runs at 20 s;
  operational 13 s, full diagnostic 150 s and control 5 s polls are unchanged.
- Remove six redundant `register_count` declarations. Four-byte raw fault
  responses and their integer decoders remain unchanged, including unavailable
  sentinels. This is not a correction of a reported hardware fault.
- Require encrypted OTA with the existing static API key. No key rotation or
  new password is part of this migration.

## Existing-device OTA migration

Follow [ESPHome's OTA migration](https://esphome.io/components/ota/esphome/).
Keep a verified backup of the actual device YAML, local packages, secrets and
previous firmware. Keep backups containing secrets local to their host.

1. Identify the exact MAC/IP/board and running ESPHome version. An older device
   first needs ESPHome 2026.9.0 or newer built with its existing OTA password and
   static API encryption key. Do not remove the password before this first
   installation. The device then offers encrypted OTA.
2. Once encrypted OTA support is confirmed, install this candidate with
   `ota: [{platform: esphome, encryption: {}}]`. It inherits the API key. The
   uploader must authenticate the encrypted connection; never bypass a refusal
   by disabling encryption. Existing unused `ota_password` secret entries may
   remain for rollback, but the new configuration does not reference them.
3. Verify the actual firmware/build identity, encryption, new FC03 generations,
   full 4300–4306 readback, topology and telemetry. Observe the technical
   postflight before restoring the previous EMS settings. OTA resets ESP uptime
   and generation counters; compare generations within the new boot.

Build separately for ESP32 and ESP32-S3, preserving each device's board, pins,
network settings and existing API key. A generic CI image is not a device image.
Never OTA during an active EMS transaction. SHA/manifests cover the exact source,
all included packages, actual configuration and each produced binary. Backend
files unchanged by this firmware update keep their proven hashes; HA restart is
not required merely to change ESP firmware.

## Polski

Aktualizacja usuwa nieczynne opcje ESPHome i przywraca rzadkie odczyty adresów
wewnętrznych co 25 minut. Odczyty sterowania, topologii i przepływów zachowują
swoje okresy. Alarmy nadal są odczytywane jako pełne 32 bity; brak obsługi rejestru
nie oznacza zera ani sprawnego urządzenia.

OTA wymaga szyfrowania obecnym kluczem API. Starszy ESP najpierw trzeba wgrać
z ESPHome 2026.9.0 lub nowszym, zachowując dotychczasowe hasło OTA i klucz API.
Dopiero po potwierdzeniu obsługi szyfrowania można zastosować nową konfigurację.
Nie zmieniaj klucza API ani płytki/pinów. Zachowaj kopię konfiguracji, sekretów
i poprzedniego firmware. Obrazy ESP32 i ESP32-S3 wymagają osobnych kompilacji.
Przed OTA potwierdź bezczynność EMS; po OTA sprawdź pełne nastawy i świeże
generacje FC03, a następnie przywróć zapisane ustawienia.

Sources: [ESPHome Modbus controller](https://esphome.io/components/modbus_controller/)
and [ESPHome 2026.9 changes](https://esphome.io/blog/2026/09/16/esphome-2026-9/).
