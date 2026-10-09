# Pakiety integracji

- `core.yaml` — ESPHome, API, OTA i uptime.
- `network.yaml` — Wi-Fi oraz informacje sieciowe.
- `modbus_connection.yaml` — UART, RS485 i harmonogram odpytywania Modbus.
- `settings.yaml` — zapisywalne ustawienia falownika, baterii i EMS (`number`/`select`).
- `system.yaml` — sterowanie pracą systemu i bezpieczny przycisk kasowania alarmów.
- `states_alarms.yaml` — opisowe stany, łącza i zdekodowane maski alarmów.
- `inverter.yaml` — wyjście falownika.
- `grid.yaml` — sieć AC.
- `backup_load.yaml` — złącze EPS/LOAD.
- `generator.yaml` — złącze GEN.
- `pv.yaml` — fotowoltaika, wyłącznie cztery fizyczne wejścia PV1–PV4.
- `battery.yaml` — bateria i BMS.
- `energy_flow.yaml` — przepływy energii.
- `meters.yaml` — zewnętrzne liczniki.
- `parallel_network.yaml` — automatyczne wykrywanie pracy pojedynczej lub
  równoległej Master/Slave (do 10 falowników) i bezpieczna kontrola toru EMS.
- `overview.yaml` — najważniejsze wartości w jednym bloku.
- `diagnostics.yaml` — temperatury i diagnostyka.

Pliki zaczynające się od `optional_` są domyślnie wyłączone. Włączaj je pojedynczo
w pliku głównym dopiero po potwierdzeniu, że dany model falownika obsługuje te rejestry.

Nazwy encji używają oznaczeń `L1`, `L2`, `L3` dla faz oraz `L1n`, `L2n`, `L3n`
dla pomiarów faza–neutralny. Nazwy przepływów są rozdzielone, np. `PV to Grid`,
`PV to Load` i `PV to Battery`.

Rejestr pojemności baterii `4102` jest odpytywany przez kontroler ustawień co
20 sekund i raportuje również niezmienioną wartość. RCEm wykorzystuje dodatnią
pojemność jako stabilną cechę instalacji, ale nie rozluźnia kontroli świeżości
bieżącego napięcia ani limitów prądowych BMS.

## Wersja firmware i odstępy Modbus

Wersja projektu EMS pochodzi z `core.yaml` (`1.5.8`); numer ESPHome/Buildera
jest osobną wersją kompilatora. Sam numer projektu nie identyfikuje zestawu
pakietów: przy wdrożeniu sprawdzaj ich SHA256 oraz `config_hash` urządzenia.
Pierwszy udokumentowany wariant 100 ms wgrano na instalację 185 dnia
8.09.2026, około 15:50 UTC, z `config_hash=b05237cf` i ESPHome `2026.8.2`.

`modbus.turnaround_time: 100ms` ustala odstęp po odbiorze odpowiedzi dla
wszystkich czterech kontrolerów wspólnej magistrali. Nie jest gwarancją
odpowiedzi falownika w 100 ms. `send_wait_time` pozostaje `250ms`.
Nie dodawaj `command_throttle`: nowe ESPHome ignoruje tę opcję i ostrzega
o jej planowanym usunięciu. Odstęp jest już ustawiony we wspólnym hubie.
Historia i zakres dowodów: [handoff v1.5.8](../docs/releases/EMS_CONSOLIDATION_RELEASE_HANDOFF.md#firmware-and-control-evidence-reconciliation--2026-09-12).

## Bezpieczna zmiana trybu EMS

Encja `Tryb EMS` udostępnia wyłącznie tryby używane w tej instalacji:
autokonsumpcję, Off-Grid, ładowanie z sieci oraz rozładowanie do sieci. Zmiana trybu
wysyła jednym poleceniem FC10 cały blok `4300-4306` (tryb, SOC i limity mocy),
odpowiadając działaniu przycisku **Save** w aplikacji S-Miles. Nie zapisuj samego
rejestru `4300`, ponieważ może to pozostawić falownik z niespójnym zestawem nastaw.
Rejestr `4302` (Backup SOC) jest odczytywany wyłącznie wewnętrznie do zachowania
pełnego zapisu FC10; nie tworzy encji Home Assistant, ponieważ tryb Pure Off-Grid
nie korzysta z tej nastawy.

Cały blok `4300-4306` jest odczytywany jednym zapytaniem co 5 sekund przez
dedykowany kontroler. Dzięki temu zmiany trybu i limitów wykonane w aplikacji
producenta pojawiają się w Home Assistant zwykle w ciągu 5-6 sekund, bez
przyspieszania dużej mapy diagnostycznej i bez dokładania siedmiu osobnych
zapytań Modbus.

Pozostałe ustawienia pokazywane na stronie **Sterowanie** (GCF i limit eksportu,
tryb GEN/EPS/PV Island, ustawienia baterii oraz stan pracy systemu) korzystają
z osobnego cyklu 15 s. Rzadko używana komenda budowania sieci równoległej
pozostaje w wolnym kontrolerze diagnostycznym.

`settings.yaml` korzysta z roli i liczby urządzeń wykrytych w
`parallel_network.yaml`. W standardowej konfiguracji oba pakiety są zawsze
włączone. Zapis EMS jest wykonywany bezpośrednio dla pojedynczego falownika albo
jako broadcast Modbus FC16 na adres `0` dla wykrytej sieci równoległej. Dzięki
temu pełny blok `4300-4306` odbiera Master i wszystkie urządzenia Slave na
wspólnej magistrali `RS485_2`. Warunkiem jest fizyczne doprowadzenie tej samej
pary A/B i wymaganego odniesienia/GND z konwertera ESP32 do Mastera oraz każdego
Slave'a. Adres `0` rozgłasza wyłącznie na tej magistrali; połączenie ESP32 tylko
z Masterem nie jest przekazywane do Slave'ów przez wewnętrzną sieć równoległą.
Broadcast trafia do wspólnej kolejki huba Modbus, lecz zgodnie z protokołem nie
oczekuje odpowiedzi z adresu `0`; po czasie `send_wait_time` hub przechodzi do
następnej ramki i nie ponawia broadcastu. Wynik jest uznawany dopiero po
późniejszym fizycznym FC03 Mastera z dokładnie takim samym blokiem;
potwierdza on wyłącznie Mastera i nie wykrywa brakującego przewodu do Slave'a.
Gdy ESP32 zostanie podłączone do
urządzenia zgłaszającego rolę Slave, zapis jest blokowany.

Rejestry `258`, `259` i `306` nie należą do tego bloku i pozostają zablokowane
na Masterze do czasu osobnego potwierdzenia ich broadcastu. Oznacza to, że RCE,
taryfa, harmonogramy ręczne i balansowanie korzystają z broadcastu EMS, ale
aktywny RCEm pozostaje na instalacji równoległej tylko w trybie shadow.

Po wykryciu Mastera encje podglądu mocy używane przez dashboard przełączają się
automatycznie na rejestry sumaryczne całego systemu: PV, bateria, LOAD i sieć.
Animacja przepływu energii pokazuje dzięki temu bilans wszystkich wykrytych
falowników. Napięcie baterii pozostaje fizycznym pomiarem na złączu DC Mastera,
a prąd baterii jest wyliczany z sumarycznej mocy baterii.

Adresy komunikacyjne z rejestrów `6050-6095` opisują wewnętrzną sieć równoległą
falownika. Są używane tylko do diagnostyki topologii, a nie jako osobne adresy
Modbus odpytywane przez ESP32, i nie zastępują fizycznej wspólnej magistrali
zewnętrznego Modbus prowadzącej do wszystkich falowników.
