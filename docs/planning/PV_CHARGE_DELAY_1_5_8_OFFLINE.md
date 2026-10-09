> Aktualizacja końcowa 30.09.2026: [połączony kandydat na bazie Recordera
> 4d75b5c](PSTRYK_RC1_OFFLINE_COMPLETION.md) ma wykonane testy integracyjne,
> kwalifikację LOAD Pstryka, dwa fizyczne pomiary PV oraz źródło BMS 13 s.
> Poniższy zapis jest wcześniejszym checkpointem. Zastępują go aktualne
> wyniki i bramki z podlinkowanego raportu; PV nadal EXECUTION_ACCEPTED=False.

# Opóźnienie ładowania z PV + Pstryk — kandydat offline 1.5.8RC1

Stan 30.09.2026: **implementacja i testy funkcji offline PASS; integracja wydania
HOLD; odbiór falownika i wdrożenie PENDING**. Baza worktree:
`34835067b832259ca4b28fa34c3db489f1357c2c`. Zmiany są niezatwierdzonym patchem.
Przygotowanie offline nie zmieniało hostów ani canonical RC1. Późniejsze
autoryzowane próby na installation_2 opisano na końcu; odbiór funkcji PARTIAL/HOLD.

## Opcja użytkownika

W ustawieniach **Sprzedaży dynamicznej** znajduje się przełącznik
**Opóźnienie ładowania z PV** (`input_boolean.hoymiles_pv_charge_delay_enabled`).
Domyślnie wyłączony; przy restarcie HA zachowuje wybór użytkownika. Dotyczy RCE
oraz Pstryka. Włączenie wymaga również zgody na sprzedaż i działania Supervisora.
Nie włącza zakupu, nie zmienia dostawcy i nie sprzedaje zapasu domu.

Plan pokazuje okno oraz szacowaną dodatkową korzyść. W tym kandydacie przełącznik
włącza obliczanie propozycji, a wykonanie blokuje jawna bramka
`pv_charge_delay.EXECUTION_ACCEPTED = False`. UI wyjaśnia oczekiwanie na
potwierdzenie działania falownika. Bramka dotyczy wyłącznie nowej akcji;
dotychczasowy zakup/sprzedaż Pstryk zachowuje własne warunki wykonania.

Nie usuwać bramki na podstawie samej symulacji, zgodnych rejestrów lub włączenia
opcji. Potrzebny jest odbiór Mode 5 / SOC+1 na dokładnym firmware: PV zasila dom
i wypływa do sieci, bateria nie ładuje się i nie rozładowuje. Szczególnie należy
sprawdzić, czy limit mocy rozładowania 1% nie ogranicza również eksportu PV.

## Algorytm i współpraca polityk

Przeszukiwane jest jedno ciągłe okno poranne z rozpoczęciem przed 12:00,
końcem przed 14:00, najwyżej 48 półgodzinami dzisiejszej doby Warszawy.
Odtworzenie energii musi nastąpić przed końcem prognozowanej nadwyżki z godzinnym
zapasem czasu, najpóźniej o 16:30. Doba, w której nawet wariant bazowy nie
osiąga skonfigurowanego maksimum baterii, nie dostaje opóźnienia.
Nie jest to dowód globalnego optimum ani gwarancja pogody. Doba DST >48
półgodzin jest dla tej opcji konserwatywnie pomijana; podstawowy Pstryk nadal
obsługuje 23/25-godzinne doby.

Wykorzystywane są istniejące kwalifikowane wejścia PV/LOAD, BMS i GCF. W modelu
opóźnione kWh można uzupełnić wyłącznie z późniejszej nadwyżki PV, z rzeczywistym
limitem ładowania i sprawnością. Nie wolno finansować opóźnienia dodatkowym
importem, rozładowaniem baterii ani naruszeniem fizycznej rezerwy. Oryginalny
zapas energii i pochodzenie PV muszą zostać odtworzone przed kolejną stałą akcją.
Minimalna dodatkowa korzyść wynosi 0,05 PLN. Ujemne/zerowe ceny nie otwierają okna.

**Pstryk:** wspólny planer porównuje standardowy BUY/SELL oraz wariant
z zachowaniem porannego PV na bezpośredni eksport. Wybiera lepszy pełny bilans,
z takim samym końcowym zapasem. Bramka odbioru zamknięta oznacza, że propozycja
opóźnienia nie zmienia wykonalnego BUY/SELL ani jego deklarowanej korzyści.

**RCE + taryfa:** istniejąca sprzedaż i ładowanie pozostają stałymi ograniczeniami.
Opóźnienie musi odtworzyć zapas przed pierwszą z tych akcji. Nieaktualny,
niekompletny lub starszy niż 120 s plan aktywnej taryfy blokuje propozycję.
Okno opóźnienia jest osobnym podsumowaniem w ustawieniach; dotychczasowa pełna
oś planu RCE zachowuje swój zakres. Dla Pstryka oś wspólnego planu rozpoznaje
odrębną akcję po otwarciu bramki odbioru.

Profil Pstryk nadal wiąże zakup ze sprzedażą, a obie zgody pozostają niezależne.
BUY=SELL=publiczne godzinowe `priceNet`, bez klucza, brutto, dystrybucji czy opłat.
Polityka agresywnej sprzedaży zapasu domu pozostaje w 1.5.9.

## Kontrakt wykonania

Nowa akcja `pv_charge_hold` korzysta z właściciela RCE i istniejącego writera,
lease, przywracania oraz fizycznego FC03. Jest rozróżniana od `rce_export`
w modelu, serializacji, rozliczeniu i UI. Nie udaje sprzedaży energii z baterii.

- Pełny świeży blok 4300–4306, Mode 5, 4305=`ceil(SOC+1)`, 4306=1%.
  Pozostałe rejestry bloku zachowane. Brak nowych zapisów 258/259/306.
- Cel SOC i deadline zostają przypięte do transakcji, również podczas oczekiwania
  na ACK. Wahanie SOC np. 60→60,1% nie zamienia przypiętego 61% na 62%.
  Brak okresowego przesuwania celu i przedłużania okna.
- Pierwszy zakres: pojedynczy falownik; SOC ≤99%, dodatnie świeże CCL/DCL,
  nadwyżka PV >200 W, brak importu >50 W, świeże spójne PV/LOAD/GRID/BAT.
- Potwierdzenie wymaga nowszego fizycznego FC03 i późniejszych pomiarów:
  eksport >200 W, |moc baterii| ≤50 W oraz spójny bilans mocy (tolerancja 200 W).
  Samo echo polecenia lub Mode 5 nie wystarcza.
- Zanik PV, import, ładowanie/rozładowanie baterii, stary pomiar, BMS=0,
  wyłączenie zgody lub koniec okna wycofują sterowanie i uruchamiają istniejące
  przywracanie. Fizyczny Off-Grid zachowuje pierwszeństwo.

Progi przepływów oraz zachowanie przy przejściu trybu wymagają terenowego odbioru.
Powrót słońca po zachmurzeniu może dać nowe okno, jeżeli nadal spełnia cały model;
nie ma obietnicy utrzymania pierwotnego czasu ani pełnego SOC przy błędzie prognozy.

## Pułapki znalezione offline

1. Symulator Pstryka po niewykonalnej, skwantowanej do zera sprzedaży pomijał
   ładowanie PV mimo oznaczenia kroku jako autokonsumpcja. Dawało to pozorny zysk
   z komendy, której nie było. Naprawiono bilans i dodano regresję. Historyczny
   wynik 2,443069 PLN z 29.09 jest zastąpiony aktualnym odtworzeniem poniżej.
2. Wykluczone atrybuty Recordera nadal mogą powodować nowe wiersze stanów.
   Samo `_unrecorded_attributes` nie wystarcza. Drobne zmiany wyświetlanego
   zysku/energii <0,10 PLN/kWh stabilizuje projekcja w RAM. Okno, status,
   gotowość i pełna precyzja obliczeń nie czekają na ten próg.
3. Kwalifikacja wejść musi przejść przez ścisłą listę atrybutów Supervisora.
   Dodano i przetestowano cztery flagi świeżości, odrzucenie tekstowego „true”,
   brakujących danych oraz przypięcie celu podczas oczekiwania na odczyt.
4. Klasyczna taryfa publikuje datę/czas lokalny, Pstryk również UTC. Adapter
   rozpoznaje oba formaty, brak/mieszanie/stare dane blokują propozycję.

## Dowody

Nowe testy obejmują model, RCE/Pstryk, pełny cykl wykonawcy, fizyczne sprzeczności,
wycofanie, źródła Supervisora, przypięcie SOC i UI PL/EN. Logi i pełna lista
bieżących regresji są w `validation/results.json` w pakiecie dowodowym.
Testy nowej funkcji i powiązanych regresji PASS. Runtime sprawdzony na lokalnym
HA 2026.9.2 oraz przypiętym środowisku CI 2026.8.2; zdalny CI nie był uruchamiany.

Rolling replay publicznych cen Pstryka i LOAD/PV hosta100, **22–28.09.2026**:
336 decyzji na wariant, prognoza tylko z wcześniej zakończonych dni. Przyszłe
pomiary służą do oceny skutków, nie wyboru planu. Jawne założenia tego scenariusza:
12,5 kWh, SOC początkowy 43%, rezerwa 20%, maksimum 90%, ładowanie/rozładowanie
3 kW, AC/GCF 5 kW, sprawność 0,95, zużycie baterii 0,08 PLN/kWh DC.
To scenariusz z tygodniowego pakietu, nie odtworzenie dokładnych limitów
15-kWh instalacji odczytanych 30.09 ani archiwalnej prognozy Solcast.

| Miara | Standardowy Pstryk | Pstryk + opóźnienie, założony odbiór fizyki |
| --- | ---: | ---: |
| Korzyść względem autokonsumpcji, PLN | 2,146382 | 3,312126 |
| Zapas końcowy, kWh | 7,338967 | 7,338967 |
| Import, kWh | 12,000293 | 12,157421 |
| Eksport, kWh | 88,653724 | 88,876442 |
| Eksport z baterii, kWh AC | 0,900000 | 0,450000 |
| Decyzje opóźnienia | 0 | 10 |

Dodatkowy wynik: **1,165744 PLN / tydzień** w tym scenariuszu. Niewielki wzrost
importu 0,157128 kWh pokazuje różnicę między prognozą a późniejszym pomiarem.
Replay nie emuluje sekundowych zabezpieczeń, czasu odpowiedzi falownika,
transportu, FC03 ani lease. Nie jest prognozą faktury ani dowodem polowego zysku.
Wcześniejsze ok. 2,51 PLN za 29.09 i okno 08:00–11:30 były inną analizą
z wiedzą o późniejszych pomiarach — nie wynikiem tego tygodniowego algorytmu.

Recorder: rzeczywisty HA/SQLite, 4320 callbacków / 72 h, **14 stanów,
3 zestawy atrybutów, maks. 90 B** zapisanych atrybutów nowej opcji, baza po
zamknięciu procesu 151552 B, 37 stron. Tak samo na HA 2026.9.2 i 2026.8.2.
Pomiar obejmuje nową projekcję na minimalnej encji z produkcyjnymi wykluczeniami;
nie jest pomiarem całego EMS ani oszczędności dysku na installation_1. Nie dodano
historii, trwałego Store, zapytań do Recordera, timerów próbkowania lub retencji.
Istniejące LTS, liczniki, 48 h/Wczoraj i historia wykonania pozostają zachowane.

## Przekazanie po zakończeniu Recordera

Aktualny pakiet: `PSTRYK_PV_DELAY_OFFLINE.zip`, manifest i odwracalny patch w
`<PRIVATE_EVIDENCE>/2026-09-30_pstryk_pv_delay/`.
Zastępuje zakres przeglądu starego `PSTRYK_OFFLINE_READY.zip`; stary pakiet zostaje
historycznym dowodem. Paczka zawiera cały zakres Pstryka oraz tej opcji.

Przy pakowaniu canonical RC1 wskazywał już
`4d75b5c7ffffe84e47d4f8041365059df9a5dab8` (STOR-01A). Osobny
`PSTRYK_PV_DELAY_RC1_INTEGRATION.patch` jest przygotowany dokładnie na tę bazę;
kontrole nałożenia/odwrócenia w izolowanym indeksie PASS. Konflikt dokumentacji
rozwiązano, zachowując wpisy obu prac. Automatyczne połączenie Supervisora
zachowuje nowe kompaktowanie STOP Recordera; osobna kontrola potwierdza obecność
całego upstreamowego fragmentu STOR-01A. W paczce są dwa uzupełniające pliki
`integration_overlay/` oraz manifest tego wariantu. Nie kopiować starego
`overlay/` bezpośrednio na nowszy RC1 — cofnęłoby to fragment Recordera.
Pełne testy połączonego wariantu na tej bazie pozostają częścią integracji po
odbiorze Recordera; obecne PASS funkcji dotyczą worktree na bazie 3483506.

Nadal HOLD: cztery wcześniejsze kontrole (`test_supervisor_helpers_contract.py`,
`test_tariff_pending_dispatch_race.py`, `validate_rce_card.js`, `validate_release.py`).
Zachowano asercje starego zakresu/freeze; logi odtwarzają wcześniejsze blokady.
Nowy helper jest dopisany do ścisłego kontraktu, a istniejący błąd różnicy względem
HEAD pozostaje jawny. Błąd taryfy: „tariff predecessor is physically confirmed
before retarget”, ten sam wzorzec w dowodzie czystej bazy 3483506 z 29.09.

Po odbiorze Recordera: ustalić aktualny SHA/config installation_1 i RC1, przenieść
wyłącznie zakres pakietu, rozwiązać konflikty, ponowić testy oraz ścisły manifest
nowego kandydata. Nie kopiować całego drzewa. Przed aktywacją potrzebne są
hostowa kopia/rollback, kontrolowany odbiór nowej fizyki, STOP/odtworzenie,
obserwacja naturalnego okna i DB/WAL 24/72 h. Otworzyć bramkę odbioru dopiero
w przetestowanym kandydacie powiązanym z tym dowodem. Nie wymaga to klucza API
ani dodatkowego potwierdzania pary publicznych cen netto.

## Próby terenowe 30.09.2026 — installation_2 i porównanie z hostem100

Po przygotowaniu offline użytkownik autoryzował krótkie próby fizyczne na
installation_2 oraz odczyt na installation_3. To odrębny zakres od wdrożenia pakietu.
**RESTORE PASS; funkcja PARTIAL/HOLD; EXECUTION_ACCEPTED=False.**

- installation_2 jest układem Master + Slave. Pierwsza sekwencja 08:55:43–08:56:25
  CEST trwała 41,69 s; odnowienia zatrzymano po skoku wyliczanego BAT +2513 W.
- Powtórka z limitem 180 s: 09:14:22–09:15:15 CEST, faktycznie 52,44 s.
  START `[5,20,90,70,50,64,1]`, SOC 63%. Po 25,2 s LOAD=PV=9530 W,
  GRID=8429 W i wyliczany BAT=8429 W zatrzymały odnowienia. Kolejny pełny
  odczyt podczas Mode 5: PV 9576 W, LOAD 1147 W, GRID 8448 W, BAT 19 W.
- Moc baterii Master jest wyliczana z LOAD+GRID-PV, więc sam bilans nie
  potwierdza niezależnie przepływu z ogniw. Świeża generacja nie gwarantuje
  poprawności LOAD podczas przejścia. Zapisano też LOAD 64265 W po powrocie.
  BMS odświeżał się co około 150 s i nie dał próbki z czasu Mode 5.
- Automatyczny fallback obu prób odtworzył pełne `[0,20,90,70,50,30,99]`;
  terminal proof powtórki: tożsamość `pv-delay-long-20260930T071422`,
  FC03 69953, terminal_valid/lease_cleared=true. EMS wznowiony, potwierdzenie
  po ponad minucie; jednorazowy skrypt zastąpiony samym stop i sprawdzony po
  ponownym otwarciu. Bez restartu HA, flashowania lub wdrożenia nowego pakietu.
- installation_3 wyłącznie odczytowo: EMS paused/Off, Mode 7 ustawiony przez
  aplikację producenta według użytkownika; SOC 10%, próg rozładowania 60%,
  limit 80%. O 09:15 PV 1935 W, LOAD 377 W, eksport 1558 W, szybki BAT 0 W.
  Nie utożsamiać Mode 7 z Mode 5 ani tego przypadku z testem SOC+1.

Przed dalszą długą próbą trzeba wyjaśnić LOAD 2169 podczas przełączeń i mieć
niezależny, odpowiednio szybki pomiar baterii. Nie korygować automatycznie
64265 przez odejmowanie 65536 bez dowodu; nie maskować błędów średnią ani
podwyższaniem progów. Kryteria ograniczonej diagnostyki nie zmieniają progów
produkcyjnych. Odbiór pojedynczego falownika oraz naturalnego okna pozostaje
PENDING; Master FC03 nie potwierdza każdego Slave. Standardowy Pstryk zachowuje
własny zakres, a integracja nadal wymaga odbioru Recordera.

Dowody lokalne: `<PRIVATE_EVIDENCE>/2026-09-30_pv_delay_installation_2_field/`,
`RAPORT_PROBA_180S.md`, `trace-long-attempt.json`, `result-long.json` i
`SHA256_FIELD_FINAL.json`. Nie dodano nowej encji cyklicznej ani retencji.
ZIP offline i jego manifest pozostają niezmienionym wcześniejszym dowodem;
niniejsza sekcja jest późniejszym uzupełnieniem planu.

## Próba 240 s i STABILIZATION-01 — dodatkowe 90 s dla sprzedaży RCE

Aktualizacja po trzeciej, autoryzowanej próbie: **zasada Mode 5/SOC+1 w tych
warunkach PASS; odtworzenie PASS; wykonawca produkcyjny PARTIAL/HOLD**.
To nowszy wynik niż opisane wyżej dwie krótsze próby; nie zmienia ich zapisów.

- installation_2 30.09.2026 **09:37:02,636–09:41:05,244 CEST**, sekwencja 242,608 s,
  ESP hard deadline 240 s, testowe 45 s tolerancji przejściowego bilansu.
  SOC 64%, cel 65%, Mode 5, limit rozładowania 1%.
- 42 próbki: dobry bilans już +5,1 s, następnie LOAD=PV i BAT wyliczany
  10742 W przy +20,1/+25,2 s. Stabilny szereg **+30,4…+214,8 s = 184,4 s**.
  Eksport 10609…11046 W, BAT wyliczany −95…+128 W. Nie utożsamiać pierwszej
  poprawnej próbki ze stabilizacją; nie traktować wyliczanego skoku jako
  udowodnionego przepływu z ogniw. Kolejna odrębna dobra generacja: +45,6 s.
- Dwie świeże próbki niezależnego BMS w Mode 5: **15 W o 09:38:05** i
  **10 W o 09:40:35**. Cykl BMS około 150 s nie mierzy szybkości rampy.
  Po odtworzeniu BMS o 09:43:05: **−10151 W**, ładowanie przywrócone.
- Pełny fallback `[0,20,90,70,50,30,99]`, terminal epoch 32 / FC03 70263,
  `terminal_valid/lease_cleared=true`, EMS active_idle/pauza off. Skrypt
  testowy zastąpiony samym stop i sprawdzony po reloadzie. Test zakończony.
- Tabela producenta Modbus V2.4, strony 54–55: 4300=5 Force Discharge,
  4300=7 TOU Mode; tryb 7 jest harmonogramem (instrukcja HIT 4.5.5), nie był
  użyty w próbie Mode 5. Nie przenosić wyniku installation_3 na installation_2.

### Uzgodniona zmiana i wykonana część offline

Ostatnia decyzja użytkownika: **dodatkowe 90 s** zamiast wcześniejszych
30–40 s; poprawka ma objąć również sprzedaż z magazynu przez RCE.
Wykonano minimalną zmianę istniejącego podtrzymania potwierdzonej sprzedaży:

- `RCE_POST_COMMAND_SETTLING_SECONDS`: **60→150 s**;
  `RCE_POST_COMMAND_REPLAN_SECONDS`: **45→135 s**. Jedno przeliczenie po
  dłuższej stabilizacji, 15 s na ukończenie, zawsze przed końcem transakcji.
- Kotwica pozostaje **czasem rzeczywiście wysłanej komendy**, nie kolejnym
  FC03. Jest to świadome doprecyzowanie wcześniejszej propozycji liczenia
  dodatkowego okna od ACK: późny/powtarzany ACK nie może dokładać czasu.
  Nowszy potwierdzony cel ma własną kotwicę; niewysłany retarget jej nie odnawia.
- Naprawiono brak odnowienia ESP podczas rozpoznanego przejściowego LOAD:
  sama zmiana licznika HA nie wystarczała przy TTL 30 s. Odnowienie wymaga
  świeżego FC03, rzeczywiście potwierdzonego przepływu, zgody, ceny, BMS,
  tej samej tożsamości/ramki i niezmienionej podstawy ekonomicznej. Jego TTL
  musi zakończyć się **przed** granicą 150 s i pierwotnym końcem sprzedaży.
- Brak tolerancji dla złych rejestrów, braku komunikacji, rzeczywiście
  przeciwnego BAT, BMS=0/spadku limitu, rezerwy, GCF, Off-Grid, MASTER STOP,
  wyłączenia zgody lub zmiany rynku. Zwykły timeout ACK/initial proof 90 s
  nie został wydłużony. Ta poprawka dotyczy rozpoznanego przejściowego LOAD
  po fizycznym potwierdzeniu RCE, nie każdego możliwego rollbacku/rampy.

**Offline PASS: 17 zestawów**, w tym 172 kontrole istniejącego hold, rzeczywisty
adapter HA, model TTL ESP, start, retarget, koniec budżetu, restart, opóźniony
ACK, ponowione callbacki, świeżość, 85 scenariuszy optymalizatora, historia,
matryca automatyki i regresja PV hold. Test regresyjny przed poprawką RED
na odmowie odnowienia przy +25 s, po poprawce GREEN. Test AST sensora wymagał
naprawy kontekstu względnego importu nowego adaptera PV; wszystkie pierwotne
asercje zachowano. Dodano odpowiedni krok CI; zdalnego CI nie uruchamiano.

### Granice przeniesienia na Pstryk i opóźnienie PV

Wspólny tor wykonuje sprzedaż Pstryka jako RCE_EXPORT, ale dedykowany wyjątek
`post_command_rce_measurement_hold_authorized` nadal wymaga udowodnionego
powodu LOAD i zgodnej ekonomiki. Projekcja Pstryka obecnie nie publikuje
takiej kwalifikacji (`current_slot_load_exhausts_requested_discharge_budget=False`).
**Nie deklarować, że sam wzrost stałej automatycznie daje Pstrykowi ten wyjątek.**
Nie dopisywać fikcyjnego powodu LOAD tylko po to, by utrzymać sprzedaż po
zmianie planu. Rozszerzenie tej kwalifikacji na wspólny model Pstryka pozostaje
osobnym PENDING w integracji; zatrzymanie planu z powodu ceny/rezerwy pozostaje
natychmiastowe. Dotychczasowa sprzedaż/zakup Pstryka zachowuje własne warunki.

Nowe PV_CHARGE_HOLD nadal ma `EXECUTION_ACCEPTED=False` i ścisły próg |BAT|≤50 W.
Test installation_2 ma inną topologię niż pierwszy zakres wykonawcy. Wniosek o
dodatkowych 90 s zapisano również dla tej funkcji, ale nie zamieniono nim
niepewnego BAT w potwierdzenie nieruchomej baterii. Przed otwarciem bramki
potrzebne są kwalifikowane źródło/cohort, rozdzielenie ACK/fizyki/planera,
co najmniej dwie odrębne świeże generacje po przejściu, pomiar niezależnego
BMS, próby chmur/LOAD i kontrola deadline/restore na dokładnym kandydacie.

### Recorder, pakiet i dalsze bramki

Zmiana używa istniejących callbacków/lease; nie dodaje encji, historii,
licznika sekund w atrybutach, Store ani cyklicznego zapytania do Recordera.
Dodatkowe odnowienia ESP są ograniczone oknem i nie zastępują dowodu fizyki.
Pomiar DB/WAL 24/72 h na docelowym hoście nadal wymagany; stary benchmark
PV/Recorder nie jest nowym pomiarem tej poprawki.

Dowody: `<PRIVATE_EVIDENCE>/2026-09-30_pv_delay_installation_2_field/`:
`RAPORT_PROBA_240S.md`, `result-settling.json`, `trace-settling.json`,
`stabilization-validation*.json`, `RCE_STABILIZATION_90S_ADDENDUM.zip`.
Poprzedni ZIP `PSTRYK_PV_DELAY_OFFLINE.zip` pozostaje niezmieniony. Addendum
zawiera ograniczony patch względem zapisanych hashy wcześniejszego kandydata;
nie kopiować całego starego Supervisora na nowszy Recorder RC1. Po odbiorze
Recordera trzeba ponownie ustalić bazę i zweryfikować połączony kandydat.
Bez nowego wdrożenia, commit/push, restartu HA lub flashowania.
