> Aktualizacja końcowa 30.09.2026: [połączony kandydat na bazie Recordera
> 4d75b5c](PSTRYK_RC1_OFFLINE_COMPLETION.md) ma wykonane testy integracyjne,
> kwalifikację LOAD Pstryka, dwa fizyczne pomiary PV oraz źródło BMS 13 s.
> Poniższy zapis jest wcześniejszym checkpointem. Zastępują go aktualne
> wyniki i bramki z podlinkowanego raportu; PV nadal EXECUTION_ACCEPTED=False.

# Pstryk 1.5.8RC1 — implementacja offline, 29.09.2026

> Aktualizacja 30.09: wspólny kandydat Pstryk + opcjonalne opóźnienie ładowania
> PV jest opisany w [bieżącym planie](PV_CHARGE_DELAY_1_5_8_OFFLINE.md).
> Poniższy checkpoint 29.09 i jego wyniki pozostają historyczne. Nowy
> replay zastępuje dawną korzyść po poprawce symulacji zerowej sprzedaży.

Status: **zaimplementowane offline; testy funkcji PASS; wydanie/integracja HOLD;
wdrożenie i odbiór na urządzeniu PENDING**. Nie wykonywano commit/push/PR,
restartów ani sterowania instalacją. Baza: `34835067b832259ca4b28fa34c3db489f1357c2c`.
Kod znajduje się w izolowanym `pstryk-offline`, poza canonical RC1 i dirty root.

## Co jest gotowe

- Publiczny klient Pstryka: anonimowy odczyt strony `/ceny`, dynamiczne discovery
  `getDailyPrices`, godziny UTC z granicami doby Warszawy. BUY = SELL = `priceNet`
  PLN/kWh. Bez konta, klucza, brutto, dystrybucji i dodatkowych opłat.
- Jeden trwały profil wybierany z ustawień sprzedaży lub dostawcy ładowania.
  Pstryk wiąże oba kierunki; osobne zgody pozostają osobne. Powrót do RCE
  odtwarza poprzednią klasyczną taryfę. Profil ładowany przed startem planerów.
- Wspólny planer liczy jeden bilans AC/DC, PV, domu, SOC, strat, zużycia baterii,
  fizycznych mocy, miejsca na PV i końcowego zapasu. Zakup jest ograniczony do
  domu/rezerwy. Sprzedaż nie narusza zapasu domu dla późniejszego tańszego importu.
  Ta agresywna polityka pozostaje w 1.5.9.
- Konserwatywny budżet pochodzenia opiera się na licznikach wejścia/wyjścia,
  PV→bateria i importu. Początkowy SOC nie jest dowodem PV. Debety działają od
  razu, dodatni kredyt dopiero po potwierdzeniu zapisu Store. Bez rosnącej historii.
- Dwa istniejące sensory są projekcjami jednej zaakceptowanej rewizji.
  Supervisor odrzuca mieszaną, nieaktualną lub niespójną parę. Istniejące
  blokady i pełny protokół wykonania nadal są jedyną drogą do urządzenia.
- UI PL/EN: „Sprzedaż dynamiczna”, RCE/Pstryk, Pstryk na liście dostawców,
  wykres publicznej ceny netto. Korzyść zakupu i sprzedaży jest wspólna;
  brak drugiej korzyści taryfy do przypadkowego dodania.

## Kontrakty i ograniczenia

`pstryk_prices/client` → `pstryk_runtime` → `pstryk_plan` → `pstryk_joint`.
Adapter wykorzystuje istniejący kwalifikowany LOAD/Solcast/GCF w RCE. Nie dodaje
drugiego modelu uczenia ani skanowania Recordera. GCF=0 zachowuje istniejącą
politykę fixed_zero_export; wyłączone, zweryfikowane GCF pozostawia ograniczenie AC.

Planowane polecenia mają całkowite procenty SOC oraz mocy. Koszt porównuje się
na pełnej trajektorii z co najmniej bazowym zapasem końcowym. Metoda to ograniczone
przeszukiwanie współrzędnych: do 3 przebiegów, do 100 półgodzin, nie dowód globalnego
optimum. Bieżący przedział uwzględnia pozostały czas, bieżące LOAD i PV.
Nieznane ceny kolejnej nocy nie usuwają jej potrzeb energii i nie stają się ceną zero.

Brak bieżącej ceny, błąd kontraktu, nieświeże dane fizyczne, mieszany profil
albo błąd zapisu cofają zgodę. Cache ma maksymalnie 50 godzin i 2 h ważności.
Odmowa/zmiana kontraktu źródła nie używa starego cache. Przejściowa awaria sieci
może użyć tylko jeszcze ważnego cache, z jego oryginalnym czasem pobrania.
Publiczny odczyt strony nie jest gwarantowanym, dokumentowanym REST API.

Jeden worker działa poza pętlą HA; wynik jest sprawdzany ponownie po obliczeniu.
Zmiana danych/profilu odrzuca stary wynik; maksymalnie dwie próby na wywołanie.
Odświeżenie źródła jest ograniczone, z retry 5–30 min i coalescing. Niezmieniony
plan nie jest liczony ponownie przed 120 s; istotne zmiany wymuszają wcześniejsze
przeliczenie. Wartości bezpieczeństwa nie czekają na następną godzinę ceny.

Minimalna korzyść START nie jest wymagana ponownie w każdej minucie fizycznie
potwierdzonej transakcji. Wyłącznie świeże zobowiązanie tego samego Supervisora,
profilu i wersji cen może obniżyć próg korzyści kontynuacji bieżącej półgodziny.
Takie polecenie nie ma zgody na nowy START. Nadal wymaga dodatniej opłacalności,
pochodzenia PV i wszystkich ograniczeń; nie zmienia deadline ani lease.

Zerowa cena pozostaje prawidłową ceną; ujemna nie otwiera sprzedaży baterii.
Naturalny eksport PV przy cenie ujemnej pozostaje kosztem, zgodnie z fizyką/GCF.
Nie kupujemy spekulacyjnie tylko dla przychodu z ceny ujemnej.

Budżet pochodzenia jest celowo ostrożny: luka >300 s, reset, nowa doba lub
niespójność liczników zerują prawo do sprzedaży, nie energię fizyczną. Cztery
liczniki 0,1 kWh mają jeden stały bufor 0,4 kWh. Jednoczesny przyrost ładowania
i rozładowania rozlicza debet po kredycie. Instalacja równoległa bez kwalifikacji
liczników całego systemu ma zerowy budżet sprzedaży baterii; zakup może działać.
Weryfikacja jednostek/timingu liczników na dokładnej instalacji pozostaje polowa.

## Wyniki offline

| Warstwa | Dowód |
| --- | --- |
| Źródło, cache, profil | 65 testów PASS; 336 godzin z 14 oficjalnych odpowiedzi. BUY=SELL=netto, 4 ujemne i 6 zerowych. Bez sieci podczas powtórnego odtworzenia. |
| Wspólna fizyka i pochodzenie | 26 testów PASS: headroom, rezerwa, straty, CCL/DCL, kwanty, ceny ujemne, bilanse, restart, zapis, luki i ciągłość. |
| Adapter produkcyjny | 4 testy PASS, m.in. bez PSE, brak cen jutra nie usuwa PV, brak zmian od 50 ms upływu czasu, 49 h / 98 różnych półgodzin UTC przy DST. |
| Runtime HA | 18 testów PASS na HA 2026.9.2 i przypiętym CI 2026.8.2. Rzeczywisty Store, pętla, szablony YAML i kandydaci Supervisora, bez sieci/urządzeń. |
| Pozostałe regresje | Szeroki przebieg: 110 grup/skryptów PASS, 4 niezakończone kontrole opisane niżej. Osobno realna rejestracja platformy timeline pytest PASS. |
| UI | Pstryk, Aurora, Supervisor, disclosure i 48 h PASS. Regeneracja PL/EN oraz manifest plików. |
| LOAD historyczne | 7/7 dni przy niezmienionym kwalifikatorze v6; 113,8 kWh, z wcześniejszym kontekstem licznika. 5 testów kwalifikacji replay PASS. |

Rolling replay produkcyjnego rdzenia: LOAD/PV hosta100 z 22–28.09 i publiczne
ceny Pstryka, 336 decyzji. Prognozy z poprzednich zakończonych dni, pierwszy dzień
ze stałym fallbackiem; bieżąca moc zastąpiona ostatnim zakończonym przedziałem.
Przyszłe pomiary służą wyłącznie do symulacji skutków decyzji. Założono dostępność
cen bieżącej doby, bez udawania znajomości historycznych dat publikacji jutra.

Scenariusz: bateria 12,5 kWh, początkowo 43%, rezerwa 20%, maksimum 90%, moce
3 kW, AC/GCF 5 kW, sprawności 0,95, zużycie baterii 0,08 PLN/kWh DC. Przeliczenie
PV ×0,95 oraz interpolacja liczników są jawnymi założeniami. Oba warianty kończą
z 7,338967 kWh. Korzyść scenariusza: **2,443069 PLN** względem autokonsumpcji;
31 decyzji zakupu, 6 sprzedaży, 299 autokonsumpcji. To nie prognoza rachunku,
pomiar rzeczywistego działania ani A/B starego i nowego EMS.

## Wzrost bazy i wydajność

Rzeczywisty izolowany HA Recorder/SQLite: 72 h / 4320 callbacków, 72 publikacje
cen i checkpointy cache, 73 wiersze stanu i atrybutów (pierwszy unknown).
Maksymalne zapisane atrybuty 391 B, pełny odczyt live 4883 B. Tablice godzin
nie trafiają do Recordera. Baza ma 47 stron po 4096 B, freelist=0; przed
zamknięciem DB=4096 B i WAL=3 176 552 B, po zamknięciu DB=192 512 B i WAL=0.
WAL to zapisy stron, nie równoważna ilość nowych danych historycznych.

To pomiar kosztu nowej encji ceny, nie oszczędności ani całkowitego przyrostu HA.
RCE zachowuje istniejące wykluczenia dużych tablic, taryfa live-only atrybuty,
a rolling schedule wyklucza tablice i ruchome czasy. Nie zmieniono retencji,
liczników/LTS, historii wykonania v3 ani istniejących 48 h/Wczoraj.
Pochodzenie i ceny mają zastępowany, ograniczony Store, bez listy próbek.
1083 semantyczne zmiany checkpointu pochodzenia w replay 7 dni; jest to symulacja
liczników, nie pomiar częstotliwości zapisu konkretnego hosta.

## Cztery niezakończone kontrole wydania

1. `test_supervisor_helpers_contract.py`: historyczne oczekiwanie nowych
   `input_boolean` względem HEAD. Ten sam błąd odtworzono na czystej bazie 3483506.
   Test uwzględnia nowy selektor Pstryk, ale nie poluzowano dawnego kontraktu.
2. `test_tariff_pending_dispatch_race.py`: fixture nie inicjuje `_pause_state`.
   Ten sam błąd odtworzono na niezmienionym canonical 3483506; brak związku z
   nową parą cen. Nie zmieniano produkcyjnej ochrony pause ani obcego zakresu.
3. `validate_rce_card.js`: historyczny manifest dokładnego zestawu plików nie
   obejmuje nowego kandydata Pstryk. Pozostawiono ścisłą asercję.
4. `validate_release.py`: historyczny Task 02 wymaga czystego, dokładnego freeze.
   Reviewowany patch jest niezatwierdzony; ten walidator nie podpisuje go jako
   starego wydania. Osobny manifest bieżącego pakietu zawiera SHA256 plików.

Nie oznaczamy całej bramki release jako PASS. Weryfikacja integracji RC1,
pełny wymagany zestaw wydania, HACS/hassfest oraz ewentualny wymagany compile
firmware należą do P8. Sam upływ czasu obserwacji Recordera nie zamyka jej odbioru.

## Przekazanie do integracji po Recorderze

Raport lokalny: `<PRIVATE_EVIDENCE>/2026-09-29_pstryk_offline/`.
`PSTRYK_OFFLINE_READY_MANIFEST.json` identyfikuje bieżący zakres i pliki;
`PSTRYK_OFFLINE_READY.zip` jest pakietem review, bez prywatnych próbek instalacji.
`joint_validation/RESULTS.json`, `joint_replay_final/REPLAY.json` oraz
`joint_recorder_final/RECORDER.json` rozdzielają warstwy dowodów.

Przed integracją: potwierdzić odbiór Recordera na installation_1 i aktualny RC1,
przenieść tylko zakres Pstryk, rozwiązać konflikty i ponowić właściwe testy.
Następnie dokładny manifest hosta, kopia/rollback i kontrolowane odbiory zakupu,
sprzedaży, STOP, odtworzenia po awarii źródła oraz DB/WAL 24/72 h. Nie wymaga to
klucza Pstryk ani potwierdzania dodatkowej pary cen konta.
