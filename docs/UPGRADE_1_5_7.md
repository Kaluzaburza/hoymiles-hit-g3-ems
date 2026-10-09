# Upgrade 1.5.7 → 1.5.8RC2 / Aktualizacja

This is an in-place update: keep the existing HA integration, ESPHome device,
entity registry, Recorder and `.storage`. Do not remove/re-add the integration
to update it. HACS updates the HA component; it does not update the ESP firmware.

To aktualizacja istniejącej instalacji. Zachowaj integrację, urządzenie ESPHome,
encje, Recorder i `.storage`. Nie usuwaj integracji i nie twórz jej ponownie.
Aktualizacja w HACS nie wgrywa firmware'u ESP.

## Before updating / Przed aktualizacją

- Back up HA, the actual device YAML, secrets, local packages and firmware.
  Record tariff/provider, permissions, SOC limits, battery capacity, forecast
  entities, efficiencies, requested power and cheap-period times.
  **PL:** wykonaj kopię i zapisz te ustawienia; zachowaj też własne zmiany YAML.
- Stop automatic execution and wait for confirmed neutral readback with no
  active owner. On 1.5.7 disable the individual RCE/tariff/RCEm/balancing writers;
  on versions with Supervisor use persistent pause. Keep them stopped throughout
  the update. Physical Off-Grid must not be forced into Self-Use.
  **PL:** wyłącz działające automatyki lub użyj trwałej pauzy, jeśli jest dostępna;
  zaczekaj na potwierdzony koniec. Nie wymuszaj Self-Use podczas pracy wyspowej.
- HA minimum is **2026.7.0** (`hacs.json`); ESPHome firmware/compiler minimum
  **2026.9.0**. Public compile checks pin ESPHome **2026.9.0**.
  **PL:** starsze środowisko wymaga najpierw aktualizacji platformy.
- Check the **silicon revision** in the ESP boot log. The public classic ESP32
  profiles require **3.1 or newer**; a PCB label such as v1.1 does not identify
  that revision. Do not upload this default profile to older silicon. Keep its
  working board/memory configuration and validate a suitable build first.
  **PL:** klasyczny profil wymaga rewizji krzemu 3.1+. Starszy układ wymaga
  osobno zweryfikowanej konfiguracji; samo przejście na USB tego nie zmienia.

## Update order / Kolejność

1. In HACS allow prereleases and explicitly choose **1.5.8RC2**. Restart HA fully.
   **PL:** włącz widoczność wersji przedpremierowych, wybierz RC2 i zrestartuj HA.
2. Open **Settings → System → Repairs** and the integration's installation
   status. The integration installs `hoymiles_ems_scheduler.yaml` and the new
   `hoymiles_ems_shared_inputs.yaml` in `/config/packages/`.
   **PL:** sprawdź Naprawy i status instalacji; oba pakiety muszą zostać załadowane.
3. If using `packages: !include_dir_named packages`, both files are included
   automatically. With individually listed packages, add the shared-input file
   to the existing package mapping. Do not create a second `homeassistant:` key
   or include the same package twice. Check configuration, then restart again
   when Repairs requests it: the first restart can copy YAML after HA has
   already parsed its old configuration.
   **PL:** przy ręcznej liście dopisz nowy pakiet, sprawdź konfigurację i wykonaj
   drugi restart, jeśli wskazują go Naprawy. Nie duplikuj sekcji ani helperów.
4. Update the **complete** [device file for your board](ESP32_VARIANTS.md), not
   only its version label. Its package `ref` and `dashboard_import` must both
   use **`v1.5.8RC2`**. The public 1.5.7 device file still pinned packages to
   `v1.5.6`; leaving that reference does not upgrade the firmware.
   **PL:** podmień cały właściwy plik urządzenia z zachowaniem własnych parametrów.
   Stary `ref: v1.5.6` pozostawiał stare pakiety mimo nowszej integracji.
5. Preserve hostname, API key, Wi-Fi secrets, UART pins, RS485 address/direction,
   board and partition layout. Build/upload compatible **lease 2** firmware.
   New proxies can remain unavailable and execution blocked until ESP catches up.
   **PL:** zachowaj tożsamość i parametry sprzętu. Brak nowych funkcji starego ESP
   nie naprawi się przez odświeżenie przeglądarki — potrzebne jest firmware.
6. Reload the dashboard without cache. Confirm integration/package **1.5.8rc2**,
   frontend **1.5.8rc2.122**, ready installation, current plans, complete fresh
   physical FC03, topology, BMS/SOC and no active/conflicting writer. Review the
   copied settings before restoring only your previous permissions/policies.
   **PL:** sprawdź wersje, Naprawy, nastawy i fizyczny odczyt. Dopiero wtedy
   przywróć własne automatyki. Aktualizacja nie powinna włączać nowych zgód.

### Older OTA firmware / Starsze OTA

With password-only OTA, first build/install using ESPHome **2026.9.0**, the
existing configuration/password and the same static API key. The boot log must
show `Encryption: offered, plaintext accepted`. Only then use the RC2 config
with `encryption: {}` and no OTA password; confirm `Encryption: required`.
Do not combine password and encryption, change the API key, or bypass a refusal.
Use USB if the old configuration cannot be built or the device cannot be reached.

**PL:** najpierw przygotuj starsze ESP do szyfrowania, zachowując stare hasło OTA
i klucz API. Dopiero po potwierdzeniu obsługi szyfrowania wgraj RC2. Nie usuwaj
hasła przed pierwszym krokiem i nie generuj nowego klucza dla istniejącego ESP.
Przy problemie użyj USB. [Official OTA migration](https://esphome.io/components/ota/esphome/#enabling-encryption-on-an-existing-device).

### Modified files and conflicts / Własne pliki i kolizje

Unmodified PL/EN 1.5.7 scheduler bytes are recognized and backed up before
replacement. A customized scheduler is deliberately preserved. Back it up,
review its changes, then follow the Repair instructions for
`hoymiles_hit_modbus.install_assets` with `overwrite: true` if replacing it is
intended. This replaces the whole managed file; it is not a three-way merge.
An existing conflicting `.pre-ems-supervisor-1b3.bak` also requires preserving
and moving that backup as directed by Repairs. Never delete backups blindly.

**PL:** własny pakiet nie zostanie automatycznie nadpisany. Wykonaj kopię,
porównaj zmiany i dopiero świadomie użyj wskazanej akcji. Zachowaj wcześniejsze
kopie. Kolizję helpera UI rozwiąż zgodnie z Naprawą — nie usuwaj wszystkich
helperów ani rejestru encji. Po zmianach sprawdź konfigurację i zrestartuj HA.

## What is preserved / Co zostaje

- All **294** existing catalog source identities, config-entry identity and
  proxy unique-ID logic match public 1.5.7. Firmware adds capabilities to the
  same device. A physical ESP replacement is a separate migration.
- The seven shared inputs are copied once from existing valid legacy values:
  three PV forecasts, fallback daily LOAD, inverter rating, charge and discharge
  efficiencies. New user selections are preserved; invalid/missing inputs remain
  unresolved rather than receiving invented values.
- Existing helper values restore through HA. Old unconditional YAML defaults
  are removed so they no longer reset settings on every restart. Fresh-install
  defaults are separately authorized and do not overwrite upgrades.
- The obsolete `hoymiles_rcm_shadow_mode` helper is retired. RCEm remains a
  separate function; check any private automations referencing that old helper.
- History remains subject to Recorder retention and the evidence stored by the
  old version. New earnings accounting does not manufacture old prices or energy.
  PV-delay profile meanings and shared LOAD/reserve behavior changed; review the
  displayed choices. G12e and Pstryk are options, not automatic provider switches.

**PL:** zachowane są identyfikatory 294 encji i ustawienia. Siedem wspólnych
parametrów jest przenoszonych jednorazowo; dalsza edycja odbywa się w ustawieniach
EMS. Sprawdź własne automatyzacje używające wycofanego helpera Shadow, profil
opóźnienia PV i rezerwę SOC. Historia z brakującymi dawnymi danymi nie zostanie
uzupełniona fikcyjnymi wartościami.

## Verification scope / Zakres weryfikacji

`tools/test_upgrade_from_157.py` loads immutable public commit
`6617bc4de6592439ea2c64889b0a25bbe5bfa45e` and exercises the current installer
against real PL/EN package bytes, backups, modified-file preservation,
idempotence, identity and the seven copy-once mappings. The migration/defaults,
HA storage and source-device tests additionally cover restore and collision
cases. These are offline checks, not a guarantee for arbitrary private YAML.

For rollback, restore the matched HA backup and previous compatible firmware
while execution remains stopped; do not downgrade only one controlling half
or manually erase migration ledgers.
