# Recorder, cache cen i ciągłość taryfy — 01.10.2026

## Pochodzenie i autoryzacja

Użytkownik zatwierdził scalenie oraz wdrożenie wspólnej wersji na installation_1,
installation_3 i installation_2. Ograniczenie Recorder wyłącznie installation_1 zostało
zastąpione tą dyspozycją. Odebrany Recorder: `4d75b5c7ffffe84e47d4f8041365059df9a5dab8`.
Sterowanie C1–C4/cache: `62a90f739b7729bc75cf0265d15ffce1d84d875e`.
Merge obu rodziców: `05559e083664cfb0fb9748655d68f5b9bbf058a9`.

## Naprawy

- P2: START taryfy podczas publikacji korzysta z ograniczonego oczekiwania
  i świeżej autoryzacji jak RCE. Pewne odrzucenie przed transportem pojedynczego
  bloku EMS nie jest nieznanym wynikiem zapisu. Tylko nowy FC03 pozwala na jedną
  próbę przygotowania; pierwotny budżet, snapshot i deadline pozostają wiążące.
  Błąd transportu o nieznanym wyniku nie dostaje takiej próby.
- P3: taryfa oczekująca na efekt ładowania może odnowić lease po potwierdzonym
  readback dokładnej komendy i świeżej, kompletnej kohorcie potwierdzającej
  zasilanie domu. Każde pełne TTL 30 s musi zmieścić się w pierwotnym budżecie
  ACK 90 s i hard deadline. Nadal obowiązują bieżący plan, owner, BMS i veto.
  Nie jest to potwierdzenie ładowania ani uprawnienie rozliczeniowe. Brak
  danych lub sprzeczny przepływ nie odnawiają lease.
- P3: lokalnie wygasła lease nie wysyła spóźnionego pierwszego renew.
  Niepewna odpowiedź zachowuje tę samą sekwencję i pierwotny czas wysłania.
  Diagnostyka odrzuconej lease pokazuje zero pozostałej ważności.

## Dowody i granice

Przyczyny i surowe dane: `2026-10-01_TARIFF_GRID_HOME_DIAG` w katalogu raportów.
RED→GREEN i integracja: `2026-10-01_EMS_UNIFIED_CONTROL`.
Nowe testy obejmują START i retarget, efekt po 29/30/30.222421/60 s,
brak efektu, świeżość/FC03/BMS/plan/permission/pauzę/STOP i niezależne
wygaśnięcie modelu ESP. Potwierdzają zachowanie offline, nie odbiór terenowy.

Licznik kontraktu sensora wynosi 671: zachowano sprawdzenie publikacji
Recordera i dwie dodatkowe kontrole kolejności/unikalności natywnego sensora
cen. Żadnej asercji nie usunięto. Historyczny pełny release gate pozostaje
odrębny; nie zmieniono jego wymagań.

Wdrożenie wymaga dokładnego manifestu i testowanego SHA, osobnych świeżych
preflightów, backupów, kontroli konfiguracji, restartów i postflightów.
Bez purge DB, aktywowania retencji, zmian polityk lub firmware.
Naturalny odbiór RCE/taryfy na każdym hoście pozostaje PENDING.
