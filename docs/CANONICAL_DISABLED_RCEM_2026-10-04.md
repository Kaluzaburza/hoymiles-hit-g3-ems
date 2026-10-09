# Gotowość wykresu EMS przy wyłączonym RCEm

## Przyczyna i zakres

Na Instalacji 3 wykres „Plan energii” zgłaszał brak gotowości i pokazywał tylko
prognozę bazową. Oba plany Pstryk BUY/SELL były aktualne, falownik pozostawał
w Self-Use, bez transakcji i lease, z gotowym FC03 i BMS. Wspólną projekcję
blokowało `rcm_timeline_stale`, mimo jawnie wyłączonych funkcji i zgody RCEm.

Kanoniczny plan wymagał trzech aktualnych osi niezależnie od konfiguracji.
Poprawka pomija zależność od RCEm wyłącznie przy jawnych `false` dla zgody,
włączenia, eksportu, pre-discharge i wszystkich aktywnych flag, bez aktywnego
kandydata RCEm, lokalnego hard-stop, właściciela RCEm lub konfliktu właścicieli.
Brak danych, nieznany właściciel lub włączenie RCEm przywraca pełną walidację.
RCE i taryfa nadal wymagają aktualnych osi; fizyczne bramki nie są zmienione.

Wyłączona oś nie dostarcza energii, rezerwy ani dat świeżości. Jej kandydat
zachowuje rzeczywistą odmowę zgody i stan. Atrybut `excluded_timeline_policies`
wyjaśnia wykluczenie. Zmiana zależności w zdarzeniu Supervisora od razu
unieważnia gotowość projekcji i zleca jej ponowne obliczenie. Timer ważności
obserwuje wymagane osie i nadal odrzuca ich przeterminowane wyniki.

To wyłącznie naprawa projekcji dla wykresu. Nie zmienia autoryzacji wykonania,
optymalizatorów, nastaw, lease, stabilizacji 180 s ani twardych terminów.

## Osobny problem RCEm

W tym samym odczycie historia RCEm była nieświeża. Okresowy timer w
`rcm_sensor.py::_async_full_plan_timer` oddaje sterowanie odświeżeniu historii
po jej progu wieku. Gdy historia pozostaje stara, timer nie oblicza kolejnego
planu; późniejsze zdarzenie wejściowe potrafi go obliczyć. To osobny problem
`RCEM-HISTORY-CADENCE-01`, pozostawiony otwarty. Nie podnoszono częstotliwości
odpytywania Recordera i nie oznaczono tej historii jako poprawnej.

## Mapa kodu i dowodów

- `supervisor_canonical_runtime.py`: `canonical_timeline_dependencies`,
  walidacja wymaganych osi i budowa trajektorii bez danych wyłączonego RCEm.
- `supervisor_canonical_sensor.py`: stan źródeł, ponowne włączenie zależności,
  timer ważności i diagnostyczna przyczyna wykluczenia.
- `tools/test_canonical_disabled_rcm.py`: stara/brakująca oś, brak udziału RCEm
  w energii, wszystkie flagi, nieznany/aktywny właściciel, BMS i wymagane osie.
- `tools/test_canonical_adapter_errors.py`: stany pending/unavailable,
  niepoprawny payload wyłączonej osi, ponowne włączenie i timer.

Pliki Python bez prefiksu znajdują się w `custom_components/hoymiles_hit_modbus/`.
Prywatne dowody: `<PRIVATE_EVIDENCE>/2026-10-04_EMS_PLAN_READINESS`:

```text
installation_3/                   świeże odczyty UI, stan przed/po
RED_REGRESSION.log                odtworzenie błędu przed poprawką
VALIDATION_*.json + tests/        testy i oddzielne ograniczenia release gate
REPLAY_*RESULT.json               replay; luki czasowe nie są PASS
CANDIDATE.json                    dokładny SHA/tree i 110 plików runtime
deployment/installation_3/        scoped backup, apply, receipt i wynik
remaining-installations/          ten sam kandydat, niewdrożony na 1/2
REPORT.md + CHECKPOINT.json        rzeczywisty stan i następne kroki
```

Baza Instalacji 3: `60559a7c16aaafd09b540df0e3ae0004f8d13c08`.
Frontend pozostaje `1.5.8rc2.109`, nazwa publiczna `1.5.8RC2`.
Dokładny nowy commit i rzeczywisty stan wdrożenia wskazują zewnętrzne receipty.
Wdrożenie autoryzowane tylko na Instalacji 3 po świeżym sprawdzeniu bezczynności,
nastaw i 110 bazowych hashy, z utrwaloną pauzą, kopią i weryfikacją po restarcie.
Instalacje 1/2 pozostają HOLD do naturalnego odbioru opóźnienia PV tego samego
kandydata. Monitor pozostaje PAUSED. Bez publikacji; starsze paczki są historyczne.
