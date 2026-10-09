# Pstryk — ciągłość bloków sprzedaży

Zakres autoryzacji: poprawka i wdrożenie wyłącznie na Instalacji 3. Instalacje
1/2 czekają na udany naturalny odbiór opóźnienia PV na tym samym kandydacie.
Monitor odbioru pozostaje PAUSED. Bez publikacji.

## Przyczyna i reguła

`pstryk_plan.projections()` ograniczało `current_run_end` do końca godziny
ceny. Dwa przylegające bloki SELL po obu stronach pełnej godziny dostawały
osobne deadline. Kontroler prawidłowo wykonywał STOP po osiągnięciu pierwszego
terminu; ponowny start powodował niepotrzebny powrót do Self-Use.

Nowa transakcja SELL otrzymuje koniec całego ciągłego, zaakceptowanego bloku
SELL, tak jak RCE. Zmiana ceny godzinowej sama nie kończy tego bloku. Luka
w czasie, Self-Use, BUY albo opóźnienie PV kończą ciąg. Godzinowy zakres BUY
pozostaje bez zmian. Ceny, bilans energii, rezerwa domu i pochodzenie energii
pochodzą nadal z jednego zweryfikowanego planu Pstryk.

Już aktywna transakcja zachowuje pierwotny hard deadline. Istniejący
`retain_active_plan()` korzysta z `retain_active_rce_slot()` i ogranicza
przyszłe sloty, moc i energię do potwierdzonej transakcji. Kontroler i lease
nie mogą przedłużyć deadline podczas retargetu. BMS, SOC, GCF, świeżość danych,
zgody i bezpieczny restore pozostają krytycznymi warunkami.

Zdarzenie o 18:04 (`authorization_lost/no_current_plan`) jest osobnym
przypadkiem. Użytkownik pozostawił je do obserwacji przy powtórzeniu.
Ta poprawka nie ustala ani nie usuwa przyczyny tamtego wycofania autoryzacji.

## Mapa kodu i regresji

- Runtime: `custom_components/hoymiles_hit_modbus/pstryk_plan.py`, `projections`.
- `tools/test_pstryk_sell_run.py`: różne ceny godzinowe, przerwy i zmiany
  działania, niezmieniony BUY, północ i zmiana czasu na instancjach UTC.
- `tools/test_pstryk_active_run_commitment.py`: publikowany koniec po replanach
  w minutach 5/31/65/95/119, pierwotny deadline, energia i krytyczne veto.
- `tools/test_pstryk_supervisor_continuity.py 4 3700`: Pstryk publikowany do
  istniejącego kontrolera RCE/Supervisor i modelu firmware lease przez granicę
  godziny; stała transakcja i hard deadline, świeże FC03 i zaakceptowane renew.
  Transport, zegar i pomiary są symulowane; to nie odbiór falownika.
- Krótki wariant tego testu zachowuje sprawdzenie zatrzymania po utracie BMS.

## Kandydat i dowody

Baza Instalacji 3: `b7328d5e8d9a88969765d96132b934d0a9cfb10a`.
Frontend pozostaje `1.5.8rc2.109`; publiczna nazwa `1.5.8RC2`. Zmiana backendu
nie wymaga zmiany zasobów UI ani OTA. Dokładny nowy SHA/tree i hashe są
w prywatnym katalogu `2026-10-03_PSTRYK_SELL_CONTINUITY` poza repo:

```text
RED_SELL_RUN*.log                  wcześniejszy błąd i korekta fixture BUY
VALIDATION_*.json + tests/         wyniki offline i ograniczenia środowiska
CANDIDATE.json                    dokładny commit/tree i 110 hashy runtime
deployment/installation_3/        manifest, backup/apply/receipt, technical pass
remaining-installations/          ten sam kandydat, osobne bazowe hashe, HOLD
REPORT.md + CHECKPOINT.json        końcowy stan i następny krok
```

Wymagany jest świeży preflight tożsamości, 110 bazowych hashy, nastaw i
bezczynnego sterownika przed aktualizacją. Aktywna transakcja lub rozbieżność
oznacza HOLD. Zapisana pauza/Off, backup, kontrola konfiguracji, restart,
receipt 110/110 i porównanie przywróconych nastaw zamykają tylko techniczne
wdrożenie. Nie wykonuj ręcznego startu dla uzyskania wyniku.

Gałąź `1.5.8RC2` pozostaje czysta. Artefakty i odczyty przechowuj poza repo.
Przed pozostałymi wdrożeniami sprawdź SHA zaakceptowanego przebiegu PV
i manifest nowej paczki. Starsze paczki `start-until-14` oraz wcześniejszy
publiczny ZIP nie obejmują tej zmiany i nie mogą służyć do wyrównania.
