# RCE: przerwanie po challenge przy zmianie mocy

## Dowód z instalacji installation_3

SHA 6faaaccfa8ea33aa68417de7f5975baa3158a4fc, 27.09.2026, CEST:

- Restart zlecony 21:31:29; pierwsza transakcja wybrana 21:36:43,
  fizyczne wykonanie potwierdzone 21:36:47.
- Nowy plan ready/current o 21:37:04.967, Control Data Ready i Reserve Ready
  włączone przed retargetem. O 21:37:05.095 active_retargeting,
  następnie o 21:37:05.273 STOP authorization_lost.
- Log 21:37:05.248: retarget_not_authorized przed transportem. Odrzucony
  następnik 19% -> 19% zmieniał inny parametr; log mocy sam nie dowodzi
  identyczności całej komendy.
- Ten sam rodzaj odmowy przed transportem powtórzył się o 21:47:06.566
  (26% -> 24%) i 21:53:01.711 (43% -> 34%). O 21:47 log wskazuje
  oczekujące state_reported, świeże wejścia/BMS, prawidłową topologię,
  brak konfliktu właściciela i dispatched_count=0.

To nie jest wyłącznie etap rozruchu HA. Dane z pierwszego STOP również
pokazują gotowy plan i działającą wcześniej transakcję. Log odmowy jest
komunikatem adaptera HA; nie dowodzi odmowy elektrycznej falownika.

## Odtworzenie i minimalna poprawka

Produkcja sprawdza autoryzację przed i po asynchronicznym challenge ESP.
Aktualizacja spójnej grupy odczytów po pierwszej walidacji może poprawnie
zablokować niewysłanego następnika. Gałąź AtomicWriteNotQueued zatrzymywała
jednak cały run zamiast ponownie potwierdzić działającego poprzednika.
Gałąź wcześniejszego _PreDispatchRejected już miała taki mechanizm.

Regresja challenge_publication_cohort na niezmienionym runtime 6faaacc
odtwarza STOP authorization_lost bez restartu HA. Dodatkowy przypadek
challenge_publication_generation sprawdza nowszy pełny FC03 po challenge.

Nowa gałąź używa istniejącego potwierdzenia poprzednika wyłącznie dla RCE
i jednoznacznego retarget_not_authorized przed transportem. Po challenge
odczytuje ponownie rzeczywiste wejścia, również gdy callback HA jeszcze
nie powstał. Poprzednik musi mieścić się w aktualnym ilościowym limicie BMS.
Master STOP, pauza, cofnięcie zgody, Off-Grid, limit eksportu i brak dowodu
nadal zatrzymują lub blokują wykonanie. Brak powtórzenia odrzuconego zapisu,
zmiany transaction_id, terminu końca i bazowego rollbacku.

Nieznany wynik transportu i inne odmowy ESP zachowują dotychczasową obsługę.
Nie zmienia się firmware, Recorder, cen, ustawień ani bloków optymalizatora.
supervisor_sensor.py przecina plik osobnego niewdrożonego kandydata Recorder;
obie zmiany wymagają późniejszej jawnej integracji, nie kopiowania pliku.

## Granice odbioru

Testy używają produkcyjnego adaptera i kontrolera oraz modelu transportu.
Naturalny cykl na nowym SHA pozostaje PENDING do osobnego dowodu.
Zarejestrowane przerwania na 6faaacc oznaczają HOLD ciągłości terenowej.
# Slow replan / lease boundary, 27 September 22:15 CEST

The third host's last accepted lease renewal was 20:14:45.813233Z, sequence 21,
with 29.991953 seconds remaining. Recorder shows plan pending at 20:15:00,
ready at 20:15:19.878134Z, Mode 0 at 20:15:21.067503Z, then Supervisor
authorization_lost at 20:15:21.564540Z. Restore was confirmed without sending a
restore command. This is consistent with autonomous ESP expiry; its durable
terminal reason has not been retrieved and must not be presented as observed.

The production adapter test with a 28-second worker reproduces expiry at
virtual second 45: Supervisor still executing, ESP model restoring, renewal
denied as authorization_mismatch since second 20. The former four-second
worker test did not cover this interval.

The adapter can now renew the already sent RCE command during an explicit
replan only after the controller has anchored its existing bounded hold and
reattested the same run, current permissions, price threshold, SOC, BMS and
physical block. Fresh FC03, physical proof, export and ownership gates still
apply. A full TTL must fit inside the existing hold and transaction deadline.
No hold anchor, hard deadline or successor write is extended by this path.

Regression: test_rce_slow_replan_lease.py, 270 virtual seconds and pending
negative cases for permission, disabled policy, price, sale block, reserve,
BMS, stale FC03, Off-Grid, export, pause, Master STOP and hold boundary.
