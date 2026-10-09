# RC2 — kwalifikacja historii PV i spójność testów

Minimalny zakres zatwierdzony 4 października 2026, baza `ee999e8`.
Gałąź robocza: `fix/rc2-history-contracts-20261004`; frontend pozostaje
`1.5.8rc2.109`. To kandydat wyłącznie dla Instalacji 3, bez publikacji.

## Potwierdzona przyczyna i zmiana

`forecast_model.py` odrzucał `1.5.8rc2`, chociaż taką wersję publikuje
pakiet EMS. Parser dopuszcza teraz ograniczony sufiks `rc1`–`rc999`
(wielkość liter jest normalizowana). Granica fizycznej historii 1.5.2,
ciągłe potwierdzenie eksportu przez całą dobę i wszystkie pozostałe bramki
jakości są zachowane. Sama poprawna wersja nie nadaje historii jakości PASS.
To nie jest dowód usunięcia wszystkich problemów uczenia lub gotowości EMS.

Testy uzgodniono z istniejącym runtime: rewizja 109 i klucz cache historii,
opis PL/EN „Sprzedaż dynamiczna” / „Dynamic sales” oraz wspólne minimum
eksportu RCE/Pstryk. Nie zmieniono optimizera, wykonawcy, progów ani firmware.
Fixture starego bootstrapa musi faktycznie zmieniać rewizję, aby sprawdzać
zachowanie przy ładowaniu zasobów w obu kolejnościach.

## Mapa kodu i walidacji

| Zakres | Pliki / testy |
|---|---|
| Jedyna zmiana runtime | `custom_components/hoymiles_hit_modbus/forecast_model.py` |
| Wersje i bramki historii | `tools/test_forecast_history_versions.py`, `test_forecast_history_quality.py`, `test_forecast_learning_runtime_quality.py`, `test_rce_optimizer.py` |
| Wspólny próg | `tools/test_rce_minimum_input_runtime.py`, `test_pstryk_adapter.py` |
| UI i zasoby | `tools/test_aurora_next_block.js`, `test_execution_history_ui.js`, `test_supervisor_aurora_ui_contract.js`, `test_aurora_automation_planner_ui_contract.js`, `validate_rce_card.js` |
| Odłożone funkcje | [Aktualne TODO 1.5.9](planning/v1.5.9-TODO.md) |

Dowody poza Git: `2026-10-04_RC2_HISTORY_TESTS` — RED, logi walidacji,
manifest 110 plików, dokładny SHA/tree, kopia zakresowa, rzeczywiste receipty,
porównanie nastaw i checkpoint. Receipty poprzedniej zmiany nie są receiptami
tego kandydata. Historyczny `FREEZE-LEGACY` pozostaje osobnym HOLD; nie
zmieniono go dla uzyskania zielonego wyniku.

## Kolejność wdrożenia

Świeża tożsamość Instalacji 3, bazowe 110 hashy, stan bezczynny i nastawy;
utrwalona pauza/Off, kopia zakresowa, apply/check/restart, kontrola hashy
i przywrócenie poprzedniego trybu, porównanie nastaw oraz świeży FC03/BMS.
Rozbieżność lub aktywna transakcja oznacza HOLD, bez wymuszonego zakończenia.

Instalacje 1/2 pozostają na ostatnio zweryfikowanej bazie `00ec3f7` do
udanego naturalnego przebiegu opóźnienia PV na Instalacji 3. Monitor pozostaje
PAUSED. Po odbiorze potrzebna jest świeża kontrola i paczka z aktualnego SHA,
potem scalenie do RC2 i osobne domknięcie bramek publikacji. Starszych paczek
nie używać do wyrównania. Nie wykonano push, tagowania ani publikacji.
