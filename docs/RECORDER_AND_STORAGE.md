# EMS v1.5.8 — history and storage / historia i miejsce na dysku

[English](#english) · [Polski](#polski) · [Five-step guide / Pięć kroków](QUICK_START.md)

## English

EMS uses Recorder for the 48-hour execution chart, LOAD history and forecast
learning. The accepted RECORDER-WRITE-01 variant removes duplicate decision
fields from recorded attributes while retaining the compact recorded_execution
schema v1. Live publication cadence is unchanged. Diagnostics reconstructs the
historical fields from v1, with explicit legacy/live values taking precedence.
The reader supports legacy and compact records and preserves missing evidence.

STOR-01 candidate (not yet deployed): `recorded_execution` schema v3 keeps
the tariff decision fields and anchors its complete `source_frame` to the last
semantic decision/command transition. Later input samples with an unchanged
decision keep that anchor in Recorder while the live `tariff_decision` remains
fresh. The reader still accepts legacy, v1 and v2 rows. Accounting evidence
fingerprints/provenance and duplicated RCEm phase inputs remain live but are
excluded from routine recorded attributes. A bounded versioned accounting
transition proof with source IDs, fingerprints, reason, counts and interval
energy remains in Recorder and the Store for cold boot. RCEm maximum/rolling voltage, status,
limits, action and safety gates remain recorded. This reduces unique attribute
payloads, not the frequency of live publication or necessarily `states` rows.
STOR-02 currently has only a disabled, exact-entity proposal validator in
`tools/ems_retention_policy.py`. The active global 120-day Recorder setting is
unchanged. An exact host-specific plan, backup, external-use review and
separate approval are required before the first deletion or automatic TTL.

The two-file fix was deployed on both installations on 2026-09-15 and accepted
by the operator on 2026-09-16. Actual Recorder replay retained all 224 updates
and reduced referenced Supervisor attribute bytes from 228,480 to 158,592
(30.59%). This is a sample result, not a whole-database or monthly guarantee.
The earlier six-file variant with a 60-second publication gate and 98.67%
claim was rejected and is not part of the integrated product.

The longest recorded growth window was about nine hours. Full 24-hour and
72-hour observations were not performed. Existing gaps after the SQLite incident remain separate.
Release acceptance and any future-version port require their own checks.

### Configure history in step 4

For a **new installation with adequate storage**, this is a retention target:

```yaml
recorder:
  purge_keep_days: 35
  auto_purge: true
  auto_repack: true
```

The current LOAD reader requests data from midnight 31 days ago to build a
profile using up to 28 complete days. The 35-day target includes a margin;
it is not a requirement to wait 35 days before using EMS. Shorter or incomplete
history gives the model less evidence and may require conservative estimates.
Long-term energy totals alone do not replace the state reports read by this code.

Merge settings into the **existing** `recorder:` section in `configuration.yaml`,
or its included file. Preserve `db_url`, filters and unrelated settings. Do not
reduce an existing longer retention without deciding which older history may
be lost. Check space and write growth before increasing retention. Then use
the configuration check and planned restart already described in step 4.

### Preserve the data EMS reads

| Purpose | Required history in the current implementation |
|---|---|
| Executed actions, last 48 hours | Supervisor state **and decision/transaction evidence attributes**; SOC, PV, grid and battery power; actual household load; balancing status |
| LOAD profile | `sensor.hoymiles_actual_load_energy_today` and the three phase counters `sensor.hoymiles_hit_load_energy_use_l1n_today`, `sensor.hoymiles_hit_load_energy_use_l2n_today`, `sensor.hoymiles_hit_load_energy_use_l3n_today` |
| PV forecast learning | Configured Solcast Today, `sensor.hoymiles_hit_pv_total_energy_today`, `binary_sensor.hoymiles_ems_export_allowed`, `sensor.hoymiles_ems_package_version` |
| RCEm voltage history | The three grid voltage source entities used by RCEm |

Do not apply a blanket `hoymiles*` exclusion or exclude
`sensor.hoymiles_hit_ems_supervisor`. Its attributes currently supply the chart's
proof of executed actions. The table describes dependencies, not a replacement
allowlist for your whole HA installation. Preserve inverter/grid fault history
separately from routine EMS planning diagnostics.

### Maintenance and acceptance

Recorder enables `auto_purge` and `auto_repack` by default. Repack reclaims
unused database space; it does not compress new telemetry. Keep temporary free
space at least equal to the database size. See [HA Recorder](https://www.home-assistant.io/integrations/recorder/).
On a full disk, secure a verified backup outside HA and recover space before
planning database maintenance. Do not delete the database to regain space.

The HA OS full-backup action defaults to compression; it affects the archive,
not live Recorder. Keep a recoverable copy outside the HA disk. See
[HA backup options](https://www.home-assistant.io/actions/hassio.backup_full/).

After setup, check new samples on **Yesterday**, Recorder logs, history quality
and free space over at least 24 hours, then compare again after 72 hours.
Missing history stays a gap, not a zero or proof of an executed plan. A short
file-growth sample cannot establish monthly EMS storage needs. If growth is
excessive, investigate recorded attributes before extending retention or
removing the required measurements.

## Polski

EMS korzysta z Recordera do wykresu ostatnich 48 godzin, historii LOAD i uczenia
prognoz. Zaakceptowany RECORDER-WRITE-01 usuwa zdublowane pola decyzji z zapisu,
zachowując recorded_execution schema v1. Częstotliwość publikacji live pozostaje
bez zmian. Diagnostyka odtwarza historyczne pola z v1, przy zachowaniu
pierwszeństwa jawnych wartości legacy/live. Czytnik obsługuje stare i kompaktowe
rekordy oraz pozostawia braki dowodów jako braki.

Kandydat STOR-01 (jeszcze niewdrożony): `recorded_execution` schema v3
zachowuje pola decyzji taryfowej, a pełny `source_frame` przypina do ostatniej
istotnej zmiany decyzji lub dowodu komendy. Przy niezmienionej decyzji nowsze
próbki wejściowe nie dublują tego pola w Recorderze, choć live pozostaje
świeży. Czytnik nadal obsługuje legacy/v1/v2. Per-próbkowe provenance licznika
energii i powielone wejścia faz RCEm pozostają live, ale są wyłączone z nowych
rutynowych atrybutów historii. Ograniczony, wersjonowany dowód przejścia
rozliczenia zachowuje źródła, fingerprinty, powód, liczniki i energię interwału
w Recorderze oraz Store po restarcie. Maksymalne i średnie napięcie RCEm, status, limity,
działanie i bramki bezpieczeństwa nadal są zapisywane. Zmiana ogranicza nowe
unikalne payloady; nie oznacza mniejszej częstotliwości live ani liczby states.
STOR-02 ma obecnie jedynie wyłączony walidator propozycji dla jawnych encji w
`tools/ems_retention_policy.py`. Globalne 120 dni Recordera pozostaje bez
zmian. Pierwsze usunięcie lub automatyczny TTL wymaga dokładnego planu dla
hosta, backupu, przeglądu innych zastosowań historii i osobnej zgody.

Poprawkę dwóch plików wdrożono na obu instalacjach 15.09.2026, a operator
zaakceptował ją 16.09.2026. Replay prawdziwego Recordera zachował wszystkie
224 aktualizacje i zmniejszył referowane bajty atrybutów Supervisora z 228 480
do 158 592 (30,59%). To wynik próbki, nie oszczędność całej bazy ani gwarancja
miesięczna. Wcześniejszy wariant sześciu plików z bramką publikacji 60 s
i wynikiem 98,67% został odrzucony i nie należy do zintegrowanego produktu.

Najdłuższy zapisany pomiar obejmował około dziewięciu godzin. Pełnych
obserwacji 24 h i 72 h nie wykonano.
Dawne luki po incydencie SQLite pozostają osobnym zagadnieniem. Odbiór wydania
i port do przyszłej wersji wymagają własnych sprawdzeń.

### Ustaw historię w kroku 4

Dla **nowej instalacji z wystarczającą ilością miejsca** przyjmij docelowo:

```yaml
recorder:
  purge_keep_days: 35
  auto_purge: true
  auto_repack: true
```

Obecny odczyt LOAD sięga północy sprzed 31 dni, aby zbudować profil z maksymalnie
28 kompletnych dni. Cel 35 dni zawiera zapas; nie oznacza czekania 35 dni na
uruchomienie EMS. Krótsza lub niepełna historia daje modelowi mniej dowodów
i może wymagać ostrożniejszych oszacowań. Same długoterminowe sumy energii nie
zastępują raportów stanów odczytywanych przez obecny kod.

Połącz ustawienia z **istniejącą** sekcją `recorder:` w `configuration.yaml`
lub dołączonym pliku. Zachowaj `db_url`, filtry i ustawienia innych integracji.
Nie skracaj dłuższej retencji bez decyzji, jaką starszą historię można utracić.
Przed wydłużeniem sprawdź miejsce i tempo zapisu. Następnie wykonaj kontrolę
konfiguracji i zaplanowany restart opisane już w kroku 4.

### Zachowaj dane odczytywane przez EMS

| Zastosowanie | Historia wymagana przez obecną implementację |
|---|---|
| Wykonane działania z ostatnich 48 godzin | Stan nadzorcy **i atrybuty z dowodami decyzji/transakcji**; SOC, moce PV, sieci i baterii; rzeczywiste zużycie domu; status balansowania |
| Profil LOAD | `sensor.hoymiles_actual_load_energy_today` oraz liczniki faz `sensor.hoymiles_hit_load_energy_use_l1n_today`, `sensor.hoymiles_hit_load_energy_use_l2n_today`, `sensor.hoymiles_hit_load_energy_use_l3n_today` |
| Uczenie prognozy PV | Skonfigurowany Solcast Dzisiaj, `sensor.hoymiles_hit_pv_total_energy_today`, `binary_sensor.hoymiles_ems_export_allowed`, `sensor.hoymiles_ems_package_version` |
| Historia napięć RCEm | Trzy źródłowe encje napięcia sieci używane przez RCEm |

Nie stosuj zbiorczego wykluczenia `hoymiles*` ani nie wykluczaj
`sensor.hoymiles_hit_ems_supervisor`. Jego atrybuty dostarczają dziś dowodów
wykonania na wykresie. Tabela opisuje zależności, a nie gotową listę zastępującą
filtry całej instalacji HA. Zachowaj historię alarmów falownika/sieci niezależnie
od rutynowej diagnostyki planowania EMS.

### Konserwacja i sprawdzenie wyniku

Recorder domyślnie włącza `auto_purge` i `auto_repack`. Repack odzyskuje
niewykorzystane miejsce bazy; nie kompresuje nowych odczytów. Pozostaw wolne
miejsce robocze co najmniej wielkości bazy. Zobacz [Recorder HA](https://www.home-assistant.io/integrations/recorder/).
Przy pełnym dysku zabezpiecz zweryfikowaną kopię poza HA i odzyskaj miejsce
przed planowaniem konserwacji. Nie kasuj bazy, aby zwolnić dysk.

Akcja pełnej kopii HA OS domyślnie stosuje kompresję; dotyczy ona archiwum,
a nie działającego Recordera. Przechowuj odtwarzalną kopię poza dyskiem HA.
Zobacz [opcje kopii HA](https://www.home-assistant.io/actions/hassio.backup_full/).

Po konfiguracji sprawdź nowe próbki we **Wczoraj**, logi Recordera, jakość
historii i wolne miejsce przez co najmniej 24 godziny, następnie porównaj wynik
po 72 godzinach. Brak historii pozostaje luką, nie zerem ani dowodem wykonania
planu. Krótki pomiar wzrostu pliku nie określa miesięcznych potrzeb EMS.
Przy nadmiernym przyroście zbadaj zapis atrybutów przed wydłużaniem retencji
lub usuwaniem potrzebnych pomiarów.

## 2026-09-30: installation_1-only rolling STOP storage candidate

STOR-01A adds `recorded_stop_decisions` schema 1, a lossless zlib/base64/JSON
copy of the existing rolling eight STOP frames. Small payloads and compressor
failure use immutable raw JSON in the same versioned envelope. The live
`recent_stop_decisions`, capture semantics, controller, publication cadence,
`recorded_execution` v3 and LTS sources are unchanged. This is not a new durable
journal and cannot recover events the old capture/publication path missed.
The full rolling proof remains present on every publication, avoiding reliance
on an HA-state acknowledgement as a Recorder commit acknowledgement.

The authenticated execution-history endpoint supports opt-in `stop_history=1`.
It reads legacy and compact proof, deduplicates identical frames, preserves
conflicting evidence and labels partial/corrupt/truncated results. Decoding,
rows, time and output are bounded; unsupported SQL dialects return an explicit
status. Normal 48-hour history does not perform this additional query.

Scoped rollback restores the old Supervisor sensor writer and retains the two
additive history modules as a compatibility reader. Its explicit rollback
manifest is distinct from the original base SHA. It never restores DB/Store or
execution ledgers. Deployment is installation_1 ONLY; other hosts HOLD by user,
including after installation_1 PASS. One post-start observation replaces the old
multi-day schedule. Missing natural STOP evidence stays PENDING. RCEm/row
optimizations, STOR-02 activation and STOR-03 are not completed by this change.
