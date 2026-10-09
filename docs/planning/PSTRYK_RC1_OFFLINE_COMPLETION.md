# Pstryk, opóźnienie PV i stabilizacja — wspólny kandydat offline RC1

Aktualizacja 01.10: [bieżący kandydat i scalenie](PSTRYK_EV_UNIFIED_20261001.md)
są oparte na `9ea98786c4a557fa412baa4ae501018ff64fe298` z gałęzi
`fix/ems-unified-control-20261001`. Ten kandydat rozszerzono o opcjonalny
[filtr EV we wspólnym modelu domu](EV_LOAD_FILTER_1_5_8_OFFLINE.md).
Nowy pakiet i manifest znajdują się w raporcie `2026-10-01_ev_load_filter/unified`.
Poniższe liczby i SHA pakietu opisują zachowany checkpoint z 30.09.

30.09.2026. **Kod i integracja offline PASS; wdrożenie PENDING;
odbiór fizyczny PENDING; historyczne bramki wydania HOLD.**
Baza Recordera: `4d75b5c7ffffe84e47d4f8041365059df9a5dab8`.
Kandydat: `<LOCAL_CHECKOUT>`.
Bez commit/push, zmiany wersji/tagu, wdrożenia, restartu HA, flashowania
lub połączenia z którąkolwiek instalacją w tej turze. Canonical RC1, pierwotny
worktree Pstryka i dirty checkout użytkownika pozostają zachowane.

## Zakres i poprawione pułapki

- Sprzedaż dynamiczna RCE/Pstryk oraz Pstryk w dostawcach ładowania.
  Jeden profil wiąże BUY/SELL, ale nie włącza ich niezależnych zgód.
  Oba kierunki używają publicznego godzinowego `priceNet`, bez klucza,
  opłat, dystrybucji i brutto. Polityka agresywna pozostaje w 1.5.9.
- Wspólny bilans Pstryka z domem, PV, stratami, rezerwą i pochodzeniem kWh.
  Nieznany zapas lub energia zakupiona nie dają prawa do odsprzedaży.
- Opcjonalne, domyślnie wyłączone **Opóźnienie ładowania z PV** jest dostępne
  dla RCE i Pstryka. Model odkłada ładowanie tylko przy odtworzeniu energii
  z późniejszej nadwyżki PV, przed kolejną stałą akcją i z zapasem czasu.
- Pstryk ma teraz prawidłowy 64-znakowy SHA256 podstawy rynku. Poprzedni
  format z prefiksem nie przechodził kontraktu wspólnego Supervisora.
  Podstawa powstaje raz na zaakceptowany plan i jest przechowywana w RAM.
- Przejściowe wstrzymanie sprzedaży Pstryka przez LOAD jest kwalifikowane
  dodatkowym, ograniczonym obliczeniem na tym samym modelu: przywrócenie
  wyłącznie wcześniejszego bieżącego LOAD musi odzyskać opłacalną sprzedaż.
  Zmiana ceny, przyszłego LOAD/PV, limitów, zgód lub rezerwy nie korzysta z wyjątku.
  Wynik po obliczeniu ponownie sprawdza aktualność wejść i profilu.

## Dodatkowe 90 sekund: dwa odrębne kontrakty

**RCE i Pstryk, sprzedaż z baterii:** potwierdzona fizycznie sprzedaż może
zachować istniejące sterowanie przy udowodnionym przejściowym LOAD przez
łącznie 150 s od wysłania komendy (poprzednio 60 s). Ponowne obliczenie
wypada przy 135 s. Rzeczywisty lease ESP jest odnawiany wyłącznie przy
aktualnych bramkach i TTL kończącym się przed granicą 150 s oraz końcem okna.
Późny ACK, powtórny callback i niewysłany retarget nie przesuwają kotwicy.
Zwykły timeout pierwszego ACK i pierwszego potwierdzenia RCE/Pstryk pozostaje
90 s. Poprawka nie ukrywa każdego możliwego błędu rampy lub pomiaru.

**PV_CHARGE_HOLD:** Mode 5, przypięty `ceil(SOC+1)`, 1% limitu rozładowania,
pełne 4300–4306. Na ACK nadal 90 s. Po poprawnym ACK pierwsza fizyka może
ustalać się maksymalnie 180 s od wysłania, zawsze w pierwotnym oknie.
W początkowym przejściu dopuszczone jest spójne, nadal trwające ładowanie PV;
nie jest ono dowodem wykonania. Odnowienie lease w WAITING_READBACK wymaga
nowych, świeżych pomiarów i rozpoznanej rampy albo fizycznie poprawnej próbki.

EXECUTING wymaga dwóch odrębnych spójnych generacji mocy oddalonych o co
najmniej 15 s, eksportu PV i jednocześnie |BAT| oraz |BMS| <=50 W.
Pojedyncza dobra próbka, ten sam cohort lub samo echo rejestrów nie wystarczą.
Import, rzeczywiste rozładowanie baterii, niespójność, brak/stary BMS,
utrata nadwyżki, BMS=0, limit/rezerwa, zmiana planu, STOP i Off-Grid
zachowują swoje blokady. Po potwierdzeniu ponowne ładowanie nie dostaje
kolejnego okna stabilizacji. Restart nie odzyskuje przejściowego uprawnienia.

## Fizyczna moc BMS i koszt zapisu

Źródłem jest istniejąca encja `Battery Power (BMS)`, fizyczny FC04 1914–1915.
Jej dawny cykl 150 s nie spełniał świeżości <=15 s wymaganej dla nowej akcji.
W kandydacie przeniesiono tylko ten krótki odczyt do istniejącego kontrolera
13 s i włączono `force_update`. HA ESPHome pomija niezmienione wartości bez
tej flagi, zatem samo przyspieszenie odczytu nie wystarcza przy stabilnym 0 W.
Nie dodano encji, template heartbeat, sztucznego odświeżania starego pomiaru
ani zmian czasów całej mapy. Maksymalny dodatkowy ruch: jedno FC04 / 13 s.
To ustawiony rytm, nie gwarancja czasu odpowiedzi magistrali.

Pomiary w rzeczywistym izolowanym HA/SQLite, syntetyczne 72 h:

| Zakres pomiaru | Callbacki | Wiersze stanów | Zestawy atrybutów | DB po zamknięciu |
| --- | ---: | ---: | ---: | ---: |
| Nowa encja cen Pstryk | 4320 | 73 | 73 | 192512 B |
| Nowe atrybuty propozycji PV | 4320 | 14 | 3 | 151552 B |
| Istniejąca moc BMS, stałe 0 W co 13 s | 19939 | 19940 | 1 | 3088384 B |

BMS oznacza świadomy koszt około 1,03 MB/dobę w tym minimalnym teście,
zamiast utraty historii lub pozornego dowodu świeżości. Nie jest to przyrost
netto całego hosta ani pomiar oszczędności Recordera; pliki zawierają także
pusty schemat HA. W testach `commit_interval=0` może zwiększać przejściowy WAL.
Każdy końcowy WAL wyniósł 0 B po zatrzymaniu wątku Recordera i zamknięciu bazy.
Nie zmieniano retencji, purge, LTS, liczników, 48 h/Wczoraj ani historii wykonania.
Same okna stabilizacji używają RAM i istniejącego lease/journal; bez nowego
Store, historii tablic, licznika sekund w atrybutach i zapytań do Recordera.

## Walidacja i granice dowodów

- 100 zestawów testów: 95 PASS i 5 odziedziczonych HOLD. W tym pełna macierz
  2064 scenariuszy; nowe testy runtime przechodzą również na CI HA 2026.8.2.
  RED→GREEN obejmuje identyfikator rynku, przedwczesne EXECUTING PV oraz
  źródło BMS. Rzeczywisty adapter HA jest połączony z modelem TTL firmware.
- Domknięto wcześniejszy błąd fixture `test_tariff_pending_dispatch_race.py`:
  brakowało inicjalizacji `_pause_state` w teście pomijającym `_recompute`.
  Produkcyjne warunki taryfy i wszystkie pierwotne asercje pozostają zachowane.
- Ścisły kontrakt źródeł obejmuje dopisany BMS i zachowuje zamrożony pierwotny
  prefix. Testy STOR-01A, historii STOP i rzeczywistego SQLite przechodzą.
  Cała delta Recordera 3483506→4d75b5c jest dodatkowo sprawdzana przy pakowaniu.
- ESPHome 2026.9.0: trzy publiczne konfiguracje, pełne `config` i `compile`
  PASS. Fixture ESP-IDF: RAM 80728/180736 B, obraz 981939/1835008 B.
  To kompilacja konfiguracji CI z fikcyjnymi danymi; pakiet przekazuje źródła,
  a jej binarka nie jest przeznaczona do flashowania instalacji użytkownika.
- Powtórne generowanie zasobów PL/EN jest deterministyczne. Nowe testy Pstryk/PV
  mają osobne zadanie CI, więc błąd historycznego testu alarmów ich nie pomija.
- Historyczne `validate_release.py`, `validate_rce_card.js` oraz
  `test_supervisor_helpers_contract.py` nadal wymagają dawnego freeze/manifestu.
  Dwa testy balansowania oczekują wcześniejszej ścieżki push alarmów:
  `test_battery_balancing_contract.py`, `test_battery_balancing_ha_runtime.py`.
  Ten sam problem alarmu odtworzono na czystym 4d75 i dokładnym HA 2026.8.2;
  aktualne `test_ems_notifications.py` przechodzi. Nie zmieniano alarmów
  ani tych asercji, aby uzyskać pozornie zielone wydanie.
- `validate_pstryk_rc1_candidate.py` sprawdza dokładne bajty nowego pakietu,
  bazę, zamkniętą bramkę PV i wymagane regresje. Nie zastępuje starego freeze,
  HACS/hassfest, zdalnego CI, akceptacji Recordera ani odbioru na urządzeniu.

Wcześniejsze odtworzenie 22–28.09 (336 decyzji) zachowuje ważność dla
niezmienionych rdzeni i wejścia o potwierdzonych SHA256: standardowy Pstryk
2,146382 PLN, z propozycją PV 3,312126 PLN, różnica 1,165744 PLN i jednakowy
zapas końcowy 7,338967 kWh. To scenariusz 12,5 kWh z rzeczywistymi danymi
LOAD/PV i publicznymi cenami, nie pomiar zysku konkretnego magazynu 15 kWh.
Nowe zachowanie ACK/lease/BMS jest testowane osobno; replay nie modeluje RS485.

## Pakiet i kolejność po odbiorze Recordera

Dowody, manifest, patch i ZIP:
`<PRIVATE_EVIDENCE>/2026-09-30_pstryk_rc1_completion/`.
Pakiet `PSTRYK_PV_RC1_OFFLINE.zip` zawiera jeden overlay względem 4d75b5c,
łącznie z wąską zmianą firmware BMS. Stare ZIP-y i addendum są historyczne;
nie należy składać ich przez nadpisanie całego Supervisora.

1. Uzyskać wynik odbioru Recordera z jego właściwego zadania i ponownie ustalić
   bazę RC1 oraz stan installation_1. Ten kandydat nie ogłasza zakończenia obserwacji
   Recordera. Jeżeli baza się zmieni, ponowić integrację i odpowiednie testy.
2. Zamknąć odrębne bramki wydania, przygotować dokładny manifest i kopię
   zmienianych plików oraz rollback. Zgodę na wdrożenie traktować osobno;
   obecne polecenie użytkownika wyraźnie zabrania wdrożenia na installation_1.
3. Odebrać dokładny firmware i rzeczywistą świeżość BMS/cohort na pojedynczym
   falowniku, w tym niezmienione 0 W, utratę komunikacji i obciążenie magistrali.
   Próba na installation_2 w topologii równoległej nie zastępuje tego odbioru.
4. Sprawdzić BUY/SELL Pstryka, STOP/przywracanie, opóźnienie PV w naturalnym
   oknie i zmianę zachowania przy chmurach/LOAD. Dodatkowe 90 s nie może maskować
   przeciwnego przepływu ani przedłużać pierwotnego deadline.
5. Dopiero po udokumentowanym odbiorze nowej fizyki otworzyć
   `pv_charge_delay.EXECUTION_ACCEPTED`, które w tym pakiecie pozostaje **False**.
   UI umożliwia zapisanie opcji i oglądanie propozycji; nie uzyskuje jeszcze
   prawa wykonania PV. Nie potrzeba klucza ani potwierdzania cen konta Pstryk.
6. Porównać DB/WAL, wolne miejsce, wiersze i historię po 24/72 h osobno na
   wdrażanym hoście. Nie przenosić akceptacji pomiędzy installation_1 i innymi hostami.
