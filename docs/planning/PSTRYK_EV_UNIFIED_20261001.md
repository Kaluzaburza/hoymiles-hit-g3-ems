# Pstryk, PV i EV na wspólnej bazie 01.10.2026

Docelowa gałąź: `fix/ems-unified-control-20261001`.
Baza: `9ea98786c4a557fa412baa4ae501018ff64fe298`.
Przygotowanie wyłącznie offline, w izolowanym worktree `pstryk-ev-unified`.
Gałąź źródłowa, jej index i hosty pozostały bez zmian w tej pracy.
Pakiet jest nakładką na ten konkretny SHA; nie jest nowym commitem ani wydaniem.

## Co pochodzi z nowej bazy

Scalenie Recordera, trwałego cache cen PSE, minimalnej mocy eksportu RCE,
ciągłości sterowania, ponownej kwalifikacji START taryfy i ograniczonego
odnawiania lease przy potwierdzonym Mode 4.
Odczytano czat „Przeprowadź audyt wersji installation_1” i dokumentację
[wspólnego kandydata](../EMS_UNIFIED_CONTROL_20261001.md).

W czacie źródłowym odnotowano wdrożenie tej bazy na trzech hostach i zgodność
92 plików oraz JS. To dowód z tamtego zadania, nie ponowny preflight tutaj.
Naturalny odbiór poprawek taryfy pozostaje oddzielny. Odnotowane timeouty
rozszerzonej historii LOAD na installation_3 i installation_2 wymagają dalszej diagnozy;
kompletny zachowany model nie dowodzi poprawnego nowego odczytu historii.

## Wynik scalenia

- Zachowano oba natywne sensory cache RCE oraz sensor publicznych cen Pstryk.
- Taryfa w WAITING_READBACK/RETARGETING może odnawiać lease tylko po exact
  ACK Mode 4, kompletnej świeżej kohorcie zasilania domu i wszystkich bramkach.
  Pełny TTL 30 s musi zmieścić się w pierwotnym ACK 90 s i hard deadline.
- PV ma własną kwalifikację rampy i ograniczenia opisane w
  [kontrakcie Pstryk/PV](PSTRYK_RC1_OFFLINE_COMPLETION.md). Nie dostało
  uprawnień taryfy. Bramka `EXECUTION_ACCEPTED=False` pozostaje zamknięta.
- Lokalnie wygasła lease nadal nie może wysłać pierwszego spóźnionego renew.
- RCE zachowuje nowy parametr minimalnej sprzedaży i walidację NaN/Inf/zakresu.
  RED wykazał przypadkowe blokowanie wejść Pstryka przez niedostępny helper
  RCE. GREEN: ten helper jest kwalifikowany wyłącznie w klasycznym RCE;
  Pstryk nadal pobiera publiczne godzinowe `priceNet`, niezależnie od PSE.
- Test adaptera stabilizacji otrzymał prawidłową epokę monotoniczną zamiast
  sztucznego zera, które nowy mechanizm wygasania słusznie odrzucał.
  Nie wyłączono kontroli TTL w produkcji. Oddzielne testy rzeczywistego klienta
  lease obejmują upływ czasu i niezależne wygaśnięcie po stronie modelu ESP.
- [Filtr EV](EV_LOAD_FILTER_1_5_8_OFFLINE.md) dotyczy nauki domu.
  Rzeczywisty LOAD, obecność EV w bieżącym slocie, liczniki i limity pozostają
  fizyczne. Wyłącznik off, typowa moc i ręcznie wpisywany sensor W/kW w PL/EN.

## Walidacja i koszty

121 grup regresji: 115 PASS, 6 odziedziczonych HOLD. W tym 673 kontrole
sensora Supervisora, nowe testy taryfy/cache/minimalnej sprzedaży oraz
Pstryk/PV/EV. Pełna macierz: 2064 scenariusze PASS.
Zasoby wygenerowane dwukrotnie identycznie; formularz EV obejrzany lokalnie
na szerokościach 1440 i 390 px, zapis encji zweryfikowany atrapą usługi HA.

Izolowany HA/SQLite, symulowane 72 h: Pstryk 73 stany, projekcja PV 14,
diagnostyka EV 1 stan przy 4320 wywołaniach każdej z tych ścieżek.
Istniejący pomiar BMS co 13 s: 19940 stanów / około 3,09 MB bazy.
Nie są to pomiary wzrostu całej instalacji. Własna historia sensora EV jest
osobnym kosztem; nie włączamy mu `force_update` ani nowej retencji.
Cache EV: 28 dni, 5848 B w scenariuszu, 30 zapytań przez symulowane 72 h
z początkowym uzupełnieniem historii; brak zapytań przy callbackach mocy.

Cała funkcjonalna delta Recordera przechodzi sprawdzenie odwrotnego patcha;
pełny bazowy WORK_STATE jest zachowany jako dokładny sufiks.
29/37 plików delty nowego sterowania/cache przechodzi ten sam sprawdzian.
Osiem nakładających się plików ma jawną kontrolę scalenia i testy funkcjonalne;
nie przedstawiamy ich jako identycznego bajtowo odwrócenia patcha.
Dowód kompilacji ESPHome 2026.9.0 przenoszony jest tylko przy identycznych
hashach wszystkich wejść fixture/pakietów i oryginalnych binariów.

## Pozostałe bramki

HOLD obecne również na czystej bazie 9ea9878:

- `test_supervisor_helpers_contract.py`: historyczna lista helperów.
- `validate_release.py`: historyczny freeze/parent/subject; nakładka dodatkowo
  jest celowo niezatwierdzonym drzewem roboczym.
- `validate_rce_card.js`: historyczny manifest frontendowy.
- `test_battery_balancing_contract.py` i `test_battery_balancing_ha_runtime.py`:
  dawny kontrakt powiadomień/alarmu falownika.
- `test_rce_run_end_extension.py`: fixture oczekuje jednego odroczonego
  callbacka, nowa baza zwraca dwa. Test nie dowodzi wtedy dalszej ścieżki;
  nie usuwamy asercji ani nie ogłaszamy na tej podstawie błędu falownika.

Dokładne logi, manifest, patch i ZIP:
`PRIVATE_EVIDENCE/2026-10-01_ev_load_filter/unified/`.
Oddzielny ścisły walidator sprawdza bajty i dowody tego pakietu; nie zastępuje
starych bramek release. Odbiór PV/BMS, sprawdzenie EV na rzeczywistym sensorze,
preflight nowej konfiguracji, backup i rollback pozostają przed aktywacją.
Nie uruchamiać drugiego zadania wdrożeniowego przed zakończeniem bieżącego
audytu. Przed zastosowaniem nakładki ponownie sprawdzić SHA docelowej gałęzi.
