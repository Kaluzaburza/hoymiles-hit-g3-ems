# Lokalny cennik PSE RCE

Baza `7d61601b3d0ad9adb2bac75b133c849f42494e1e`, zakres P1 po M01 z 30.09.

## Kontrakt

- Natywny dostawca integracji zastępuje dwa sensory REST zarządzanego schedulera.
  Zachowuje `sensor.hoymiles_rce_day` i `sensor.hoymiles_rce_day_tomorrow` oraz
  dotychczasowe pola wierszy. Pełne metadane publikacji pozostają w Store.
- `.storage/hoymiles_hit_modbus.rce_daily_prices_v1` mieści wyłącznie dziś/jutro
  według Europe/Warsaw. Przeterminowane doby usuwa przy starcie i rotacji, także
  bez sieci. To retencja magazynu roboczego; historia Recorder nie jest kasowana.
- Walidacja obejmuje kompletną dobę UTC (92/96/100 kwadransów), datę, kolejność,
  duplikaty, skończone ceny, hash i chronologię. Niepełna lub starsza odpowiedź
  nie zastępuje dobrego zestawu; bez dobrego cennika pozostaje blokada wykonania.
- Kompletny cennik obowiązuje na swoją dobę. Oryginalny `fetched_at` pozostaje
  widoczny; `last_checked_at` jest osobny. Stare źródła zachowują limit świeżości.
- Po pełnej odpowiedzi ustają cykliczne żądania tej doby. Brakujący dzień ma
  backoff 15/30/60 minut. Jedna kontrola korekty przy przejściu jutro→dziś;
  timeout tej kontroli nie usuwa poprawnych cen. Nie zakładamy niezmienności PSE.
- Sieć działa w pojedynczym zadaniu poza odnowieniami TX. Unload je anuluje.
  BMS/GCF/SOC, minimum netto 2 kW, hold 90 s, TTL i hard deadline bez zmian.

## Migracja i powrót

Wymagane wspólne wdrożenie komponentu i schedulera oraz restart HA. Rejestracja
przez publiczne API HA przejmuje tylko dwie znane tożsamości REST, zachowując
entity ID i ustawienia encji. Nieznana kolizja blokuje migrację. Jeden config
entry jest właścicielem wspólnego dostawcy, także przy kilku falownikach.

Pakiet wdrożeniowy musi zweryfikować pełną bazę, zachować objęte pliki i dwie
tożsamości rejestru oraz istniejący magazyn. Rollback przy zatrzymanym HA
przywraca pliki i wyłącznie te dwa wpisy, odłącza nowy magazyn, następnie wymaga
startu i odczytu starego źródła. Nie przywraca całego rejestru ani Recordera.

## Dowody i ograniczenia

Testy cache obejmują RED→GREEN starej bramki 20 minut, restart/offline, DST,
retencję i błędne dane. Testy runtime używają HA 2026.9.2: Store na dysku,
rejestr encji, pojedynczy dostawca, anulowanie zadania i ograniczenia HTTP.
Regresje sterowania i pełna bramka wydania mają osobne wyniki w raporcie
`2026-09-30_RCE_PRICE_CACHE`. Offline PASS nie zastępuje naturalnego M01.

P2 (usunięcie slotu przed START) pozostaje PENDING: brak pełnych historycznych
OptimizerInput/OptimizerResult. Do przyszłego odtworzenia potrzebny ograniczony
zapis zmiany wyboru z SHA, wejściami, wynikiem i przyczyną rewalidacji; osobny
zakres, bez spekulacyjnej zmiany algorytmu. P3: pełny test terminal fallback
przeszedł na bazie środowiska HA; brak potwierdzonej potrzeby naprawy sterowania.

Recorder installation_1 `4d75b5c7ffffe84e47d4f8041365059df9a5dab8` pozostaje oddzielny.
Nie przenosić go na installation_3/installation_2. Późniejsze scalenie wymaga nowego SHA,
manifestu i niezależnych testów każdego hosta.
