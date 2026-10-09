# Widoczność opóźnionego ładowania PV w RCE/Pstryk

Zakres: publikacja już wybranego `pv_charge_hold` i jego prezentacja. Bez zmiany
wyboru okna, nastaw falownika, zgód, granic bezpieczeństwa, 180 s stabilizacji
lub ekonomii BUY/SELL. Eksport installation_1 nadal zablokowany.

## Przyczyny potwierdzone testami RED → GREEN

1. Canonical runtime nie mapował `PV_CHARGE_HOLD` do oczekiwanego zachowania
   fizycznego. Kwalifikowane okno kończyło się `KeyError` zamiast publikacji.
2. Oczekiwana trajektoria SOC nadal dodawała nadwyżkę P50, mimo wstrzymania
   ładowania. Test odtwarzał wzrost 60 → 70%. Obecnie nadwyżka trafia do
   eksportu, a SOC jest płaski. Rzeczywisty niedobór PV dla domu nadal obniża SOC.
3. RCE publikowało opis okna, ale nie akcję w swoim timeline. Projekcja na
   istniejącym przebiegu usuwa ładowanie w oknie i odzyskuje je z późniejszej
   nadwyżki PV, przestrzegając mocy i BMS. Nie modyfikuje wyniku optymalizatora;
   brak bezpiecznego odtworzenia lub kolizja wycofuje propozycję.
4. EMS pomijał wyróżnienie akcji przy płaskim SOC. RCE/Pstryk rozpoznawały
   w wybranych blokach wykresu wyłącznie sprzedaż z magazynu.

## Widok użytkownika

- Różowe okno z poświatą na wykresie cen i różowa linia SOC z cieniem w EMS.
- Osobna pozycja „Opóźnienie ładowania z PV” w planie: godziny, energia PV
  wysyłana do sieci i wyjaśnienie, że magazyn czeka z ładowaniem.
- Sprzedaż energii z magazynu pozostaje bursztynowa. Energia PV nie powiększa
  licznika sprzedaży z magazynu. Sąsiadujące różne akcje nie łączą się w blok.
- Zasoby frontend revision 101 odświeżają zarządzany URL Lovelace.

## Walidacja i ograniczenia

PASS offline: projekcja SOC (P50, niedobór PV, zero export), RCE replay i
odtworzenie zapasu, canonical runtime/dual track/ledger, kontrola PV delay,
pełna macierz automatyki, regresje RCE/Pstryk i mobilny panel w Chromium.
Test UI obejmuje oba źródła cen, płaski różowy SOC, brak migania i zachowanie
przewijania. Ręczny podgląd syntetycznego okna 08:00–10:00 potwierdza 5,2 kWh
PV, cztery płaskie odcinki i osobną kartę planu. To przykład offline.

Brak nowych encji, zapytań do historii, timerów i częstotliwości zapisu.
Projekcja wykorzystuje ograniczony istniejący timeline w RAM. Istniejące
deduplikowanie publikacji i zasady Recordera pozostają bez zmian.

Historyczne walidatory release zachowują swoje zamrożone kontrakty. Nie
przepisujemy ich manifestów na ten kandydat. Aktualny pakiet wymaga osobnego
manifestu wszystkich 106 plików, zgodności kopii generowanych i czystego SHA.

Dowody i odbiór wdrożenia per host znajdują się poza repo:
`PRIVATE_EVIDENCE/2026-10-01_PSTRYK_PV_EV_ACTIVATION/pv-hold-display/`.
Techniczny PASS wdrożenia nie zastępuje naturalnego odbioru okna PV ani M01
sprzedaży z magazynu. W chwili zapisu tej dokumentacji wdrożenie jest PENDING.
