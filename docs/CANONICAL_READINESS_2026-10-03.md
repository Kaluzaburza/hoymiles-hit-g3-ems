# Brak gotowości planu przy pełnym magazynie

## Potwierdzona przyczyna

Instalacja 3, baza runtime `44800ff`, frontend `1.5.8rc2.108`:
przy SOC 100% plan sprzedaży zawierał przyszłe opóźnienie PV. Kanoniczny
plan EMS wyznaczał jego cel z bieżącego SOC zamiast z SOC osiągniętego
po poprzednich przedziałach prognozy. `hold_soc_target(100)` poprawnie
odrzuca brak miejsca na wymagany jeden punkt procentowy, lecz nieobsłużony
`ValueError: pv_delay_soc_headroom_missing` unieważniał cały plan.
Sensor publikował tylko `canonical_adapter_error` i pustą listę przedziałów.
Świeży odczyt sprzętu był gotowy: Self-Use, bez ownera, transakcji i lease.
Nie był to dowód awarii komunikacji ani kolejnego fizycznego startu PV.

## Zakres poprawki

- `supervisor_canonical_runtime.py`: przyszły cel PV pochodzi z SOC na początku
  danego przedziału kanonicznego. Cel już zatrzaśniętego bieżącego epizodu
  pozostaje zachowany. Polecenie i oczekiwany odczyt używają tego samego celu.
- Brak miejsca przy SOC powyżej 99% odrzuca kandydata PV. Pozostały plan
  powstaje tylko wtedy, gdy ma poprawną niezależną podstawę bilansu energii.
  Nie ograniczamy sztucznie niepoprawnego celu do 100%.
- `supervisor_canonical_sensor.py`: nieoczekiwany błąd zachowuje typ i etap
  w atrybutach oraz traceback w logu, bez powtarzania identycznego tracebacku
  do czasu zmiany błędu albo poprawnego przeliczenia. Błąd nadal zamyka plan.
- `diagnostics.py`: ograniczona historia planu zawiera też stan, przyczynę,
  typ i etap błędu. Pełna tablica przedziałów pozostaje poza Recorderem.

To zmiana projekcji i diagnostyki. Nie zmienia wykonawcy, stabilizacji 180 s,
lease, dat końca, blokady ponowień, zgód ani częstotliwości odczytu Modbus.
Przyszły przedział pozostaje `unverified` i nie zezwala na zapis sterujący.

## Dowody i powtarzanie

Prywatny katalog: `<PRIVATE_EVIDENCE>/2026-10-03_EMS_READINESS`.

- `installation_3/CANONICAL_INPUTS_20261003T131500Z.json`: ograniczony zrzut
  aktualnych stanów i osi czasu przez HA Template. Osie pobrano kolejno,
  więc zrzut nie jest atomowy; brakujące źródła są jawne w replayu.
- `RED_REPLAY.log` odtwarza dokładny wyjątek; `GREEN_REPLAY.log` potwierdza
  z tych samych danych 83 przedziały księgi i projekcji.
- `RED_REGRESSION.log`, `VALIDATION_INITIAL.json`, `VALIDATION_FINAL.json`
  i wskazane logi zachowują wyniki negatywne oraz poprawione środowisko testów.
- `CANDIDATE.json` wiąże commit/tree, 110 hashy i logi z paczką.
- `deployment/installation_3/`: manifest różnic względem `44800ff`,
  backup, preflight, receipt oraz postflight. Sam manifest nie dowodzi wdrożenia.

Testy wejściowe: `test_pv_charge_delay_projection.py`,
`test_canonical_adapter_errors.py`, `test_pv_charge_delay_control.py`,
`test_supervisor_canonical_runtime.py`, `test_supervisor_canonical_dual_track.py`,
`test_supervisor_canonical_ledger.py`, `test_diagnostics.py`.
Pełna lista 18 sprawdzonych zestawów jest w zewnętrznym manifeście walidacji.

## Wdrożenie i nadal otwarte bramki

Użytkownik zezwolił na wdrożenie wyłącznie na Instalacji 3: świeża tożsamość
i 110 hashy bazy, porównanie nastaw, bezczynny sterownik, utrwalona pauza,
kopia zmienianych plików, kontrola konfiguracji HA, restart oraz przywrócenie
wcześniejszego trybu. Weryfikacja po restarcie obejmuje zgodność 110 hashy,
aktualny niepusty plan, gotowość sprzętu i brak niezamierzonej transakcji.
Rzeczywisty wynik i dokładny SHA rozstrzygają końcowe receipty poza repo.

Instalacje 1/2 pozostają HOLD do udanego naturalnego odbioru PV Instalacji 3.
Monitor pozostaje PAUSED do następnego dnia na życzenie użytkownika.
Naprawa gotowości nie jest takim odbiorem. Wcześniejsze FAIL/HOLD i PARTIAL
pozostają. Starsze paczki pozostałych instalacji oraz publiczny ZIP z
`21d97c2` nie zawierają tej poprawki; nie używaj ich do wyrównania ani publikacji.
