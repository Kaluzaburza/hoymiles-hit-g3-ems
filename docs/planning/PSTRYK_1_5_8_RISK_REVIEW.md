# Pstryk w 1.5.8RC1 — analiza przed integracją

Data: 2026-09-29. Baza kodu: `34835067b832259ca4b28fa34c3db489f1357c2c`,
tree `fab0ef93a13c753311dfbf58c092d1289e3e17f4`.
Zakres: analiza kodu, istniejących regresji i zapisanych dowodów, następnie
autoryzowany odczyt historii hosta100 do testów offline. Bez sterowania,
restartu i wdrożenia. Przeczytano AGENTS.md.

## Najważniejsze wnioski

Pstryk wymaga jednego planu dla dwóch kierunków energii. Dopisanie nazwy do
listy dostawców i zamiana cen RCE pozostawiłyby dwa niezależne plany walczące
o tę samą baterię. Obecny `evaluate_sale_vs_preserve` jedynie ogranicza
wcześniej wybraną sprzedaż; nie wybiera ładowania z sieci. Nie jest gotowym
wspólnym optymalizatorem.

Drugą pułapką jest utożsamienie planu z uprawnieniem do wykonania. Historie RCE
i taryfy zawierają zarówno prawidłowe STOP po utracie uprawnienia, jak i błędy
planera/publication, które niepotrzebnie to uprawnienie odbierały. Nie naprawiać
ich wspólnym „ignoruj brak planu”, przedłużeniem TTL ani nowym writerem.

Trzecia pułapka łączy wydajność i poprawność: nowy czas publikacji nie oznacza
nowej ceny. Hashowanie diagnostycznych czasów/pełnych stanów powoduje zbędne
przeliczenia, wyścigi i zapis dużych atrybutów. Wartości ekonomiczne, świeżość,
pokrycie, dane fizyczne i dowód wykonania potrzebują osobnych kontraktów.

## Lekcje z potwierdzonych problemów

| Dowód / mechanizm | Ryzyko dla Pstryka | Rozwiązanie i regresja obowiązkowa przed wykonaniem |
| --- | --- | --- |
| TARYFA-CIAGLOSC-03: zakończone przeliczenie po 120 s przenosiło bieżące ładowanie do równoważnej późniejszej godziny. Potwierdzono fizyczne Mode 4 i STOP; nie był to koniec lease. | Plan co godzinę wygląda dobrze, ale rzeczywiste ładowanie ciągle startuje i zatrzymuje się. | Ten sam aktywny commitment tylko ze świeżym fizycznym dowodem tej transakcji; nie wskrzeszać po restarcie. Test zamkniętej pętli plan → wykonawca → energia → replan, również realny brak potrzeby wymagający STOP. `tools/test_tariff_active_controller.py`, `tools/test_tariff_pending_dispatch_race.py`. |
| RCE 26–27.09: publikacja pending, skrócony koniec bloku i aktualizacja podczas challenge przerywały eksport. Dokładny historyczny predicate nie zawsze był zapisany. | Dwa osobne publikatory BUY/SELL mogą widzieć różne rewizje albo kasować aktywny blok w połowie godziny. | Jedna niezmienna para cen i jeden wynik; wspólne revision/fingerprint. Zachować ograniczony hold tylko potwierdzonego poprzednika, wcześniejszy z końców i nieprzedłużalny deadline. `docs/RCE_CONTINUITY_20260926.md`, `docs/RCE_CONTINUITY_20260927.md`. |
| Niewysłany retarget RCE i RCEm pending: następca bez command_sent_at to inna sytuacja niż wysłany/nieznany wynik lub sprzeczny przepływ. | „Naprawa ciągłości” mogłaby wyłączyć prawdziwe STOP. | Osobne scenariusze: przed transportem, po transporcie, timeout, zmieniony FC03, niespójny przepływ, no_action. Żadnego ogólnego allowlistowania `lease_already_active`. Zachować obecne testy unsent/lease/retarget. |
| RCE 27.09: upływ 50 ms bez nowych pomiarów zmieniał adaptowaną mapę PV; stary wynik nigdy nie stawał się aktualny. | Poll API, wiek ceny lub czas renderowania UI wywołuje burzę zmian. | Rewizja z normalizowanych przedziałów/cen i kontraktu źródła i profilu; fetched_at nie w hashu. Nowy timestamp pomiaru fizycznego nadal ma znaczenie dla świeżości. Test identycznej odpowiedzi po odświeżeniu oraz rzeczywistej korekty ceny. `tools/test_rce_daylight_publication.py`. |
| RCE: brak cen jutra usuwał kompletną prognozę PV jutra i P10. | Nieopublikowane ceny wywołują fałszywy niedobór domu lub zakup przed PV. | Rozdzielić horyzont fizyki od pokrycia cen. Brak ceny blokuje nową decyzję ekonomiczną dla tego przedziału, nie usuwa prognozy. `docs/RCE_FORECAST_MARKET_SCOPE_20260927.md`. |
| RCE: spadek SOC o 0,05 p.p. odrzucał całą sztywną selekcję, a diagnostyka podawała missing_data. | Minimalna zmiana LOAD/SOC zatrzymuje całą wspólną optymalizację. | Po workerze świeża rewalidacja tej samej selekcji; może zmniejszyć energię/moc, nie dodać okna ani przesunąć deadline. Osobny powód odrzucenia i publikowalny aktualny wynik zerowy. `docs/RCE_REVALIDATION_ENERGY_20260927.md`. |
| F03/F04: PV sprzed okresu liczone dwukrotnie; kupowany margines niewykonalny przy małej mocy oddawania. | Tania cena zachęca do zakupu, którego bateria nie zdąży oddać domowi. | Jeden chronologiczny bilans AC/DC, margines jako zużywalne kWh; ograniczenia ładowania, rozładowania, LOAD i mostka na każdym kroku. `tools/test_tariff_consumable_margin.py`, `tools/test_tariff_margin_protected_window.py`. |
| F01/F08: utrzymany stary limit po spadku CCL; świeże 0 V mylone z poprawnym pomiarem. | Aktualne ceny mogłyby przesłonić zerową zdolność BMS lub niewiarygodną fizykę. | Zero limitu jest zerem; osobna walidacja wartości i wieku. Godzinny plan nie spowalnia bezpieczeństwa. Zachować BMS/GCF/Off-Grid/STOP i pełne 4300–4306/FC03. |
| F05–F07: skończony budżet odzyskiwania LOAD, żyjący worker po timeout, różne kryteria świeżości. | Nieograniczone retry API/solverów albo permanentne utknięcie po awarii. | Jedno połączenie BUY/SELL, single-flight, deadline, kontrolowane anulowanie i backoff. Pusta odpowiedź nie odświeża starego cache. Nie dokładać drugiego odczytu LOAD z Recordera. |
| Solcast auto_dampen i EMS wykonywały dwie korekty tej samej prognozy. | Doliczenie dystrybucji/podatku albo podwójna konwersja ceny. | Używać publicznego priceNet PLN/kWh identycznie dla BUY/SELL, bez dodatków EMS. Źródło/baza ceny jawne; flagi cheap/expensive nie są poleceniami. `tools/test_solcast_adaptation_contract.py`. |
| M01 nadal miał niezamknięty odbiór; zapisane obserwacje dotyczyły różnych hostów i SHA. | Uznanie offline PASS za dowód stabilnego eksportu albo odziedziczenie nieistniejącego odbioru. | Osobne poziomy: kontrakt, algorytm, HA, field i release. Nie przenosić odbioru między installation_1/installation_2/trzecim hostem. Najnowszy konkretny M01 trzeba ponownie rozliczyć przed integracją. |
| RCE tails 28.09: matematycznie dodatnia sprzedaż była za mała po odjęciu LOAD/PV i kwantyzacji 4306. | Nowy planer może produkować dziesiątki drobnych START/STOP bez użytecznej sprzedaży i z dużą liczbą zapisów/pushy. | Ocenić wykonanie po kwantyzacji: aktualny próg startu plus kwant mocy, osobny próg kontynuacji, pakowanie do już wybranych sąsiednich okien bez zwiększania energii i szkody dla pełnej ekonomii. Nie dodawać nowych subslot deadline. `docs/RCE_TAIL_START_PUSH.md`. |
| STOR-01: częste zmiany czasów, źródłowych ramek i dowodów tworzyły nowe duże atrybuty. Po wdrożeniu ich liczba spadła, ale przyrost całej bazy nie miał jeszcze PASS. | Dwie kopie 48 godzin cen i planu zapisane co tick niwelują oszczędności Recordera. | Jedna współdzielona tabela, mała projekcja historyczna, pełne dane poza Recorderem; mierzyć wiersze i unikalne atrybuty, nie sam JSON. Chronić istniejące recorded_execution v3. |

Dowody F01–F08 są podsumowane w `docs/WORK_STATE.MD`, wpis 2026-09-21.
Historyczne notatki o lease (pierwszy readback pod koniec TTL 30 s) i niesłanym
retargetowaniu były dodatkowym kontekstem; nie są nowym pomiarem installation_1.
Raport TARYFA-CIAGLOSC-03: `<PRIVATE_EVIDENCE>/2026-09-16_taryfa-ciaglosc-03/WYNIK_TARYFA_CIAGLOSC_03.md`.

Odczytany także zapis M01 installation_2 z 29.09: transakcja od 06:30:05 do 06:37:53
CEST (około 7 min 48 s), `authorization_lost` po aktualnym planie bez bieżącego
okna, przy wcześniej odnowionej lease. Dokładna przyczyna usunięcia okna pozostaje
PENDING, nie wolno podpisać jej jako ponownie udowodnionego race. Późniejszy wpis
użytkownika anulował tę obserwację installation_2 na rzecz osobnego hosta100.
Źródła: `PRIVATE_EVIDENCE/2026-09-29_M01_RCE_0edd4c/installation_2/STOP_ANALYSIS_20260929T044032Z.md`
i `M01_CANCELLED_20260929.md`. To dodatkowy wymagany przypadek do odtworzenia,
nie powód do zmiany działającej instalacji w tym zadaniu.

## Pułapki specyficzne dla nowego profilu

1. **Atomowość i ABA.** Dwa selektory pokazują jeden profil. Powrót Pstryk → RCE →
   Pstryk nadal unieważnia wcześniejszy worker, nawet gdy końcowy tekst jest taki sam.
   Obecne zgody ładowania/sprzedaży są osobne. Ich stan nie zmienia się wraz ze źródłem.
2. **Restart i baza ceny.** Publiczne źródło nie wymaga konta ani klucza.
   Cache wiązać z kontraktem netto i rewizją profilu; stary cache brutto odrzucić,
   nie zmieniać jedynie etykiety. Błędny format i odmowa źródła blokują użycie cache.
   Timeout może użyć wyłącznie jawnie ważnych godzin z oryginalnym czasem pobrania;
   nigdy odmłodzenia datą pliku/restartu. Identyfikator odczytu strony może zmienić
   się przy jej wdrożeniu: ograniczone discovery, bez nieskończonego retry,
   bez wykonywania JS, obcych URL, poświadczeń, ani fallbacku do brutto/PSE.
3. **Czas.** Klucz przedziału to UTC start/end; jesienne 02:00 występuje dwa razy.
   Dwie doby Warszawy mają 47/48/49 godzin. Nie wymagać zawsze 24 ani 48 rekordów.
   Półgodziny mapować przez przecięcie przedziałów, bieżącą godzinę przycinać do
   pozostałego czasu. Dziura, nieciągłość, duplikat sprzeczny i puste dane nie są zerem.
4. **Ceny ujemne.** Nie usuwać znaku ani podstawiać domyślnej ceny. Nie kupować na
   zapas wyłącznie dla przychodu z ujemnej ceny. Cel zakupu pozostaje potrzebą domu
   i rezerwy. SELL < 0 nie otwiera eksportu ekonomicznego; fizyczny naturalny eksport
   PV może być kosztem w modelu netto i wymaga istniejących ograniczeń GCF.
5. **Pochodzenie energii.** Sam SOC nie rozróżnia zakupu i PV. Najmniejsza bezpieczna
   wersja nie eksportuje baterii bez potwierdzonego budżetu PV. Bilans z liczników
   trzeba trwale rozliczać przed udzieleniem nowego eksportu, z obsługą resetu,
   luki, restartu, ubytku i mieszania; nie zapisywać tabeli próbek. Niepewność zeruje
   uprawnienie do sprzedaży, nie fizyczną energię baterii.
6. **Ekonomia.** Wszystkie warianty zaczynają od tego samego SOC i danych PV/LOAD,
   mają te same straty, zużycie baterii, ograniczenia oraz wycenę końca horyzontu.
   Liczyć koszt importu minus przychód eksportu według wspólnej ceny netto plus wear; uniknięty
   import jest wynikiem porównania, nie drugim bonusem. Zachować naturalny eksport
   PV i miejsce na PV. Ranking samych cen lub dwóch osobnych zysków nie wystarcza.
7. **Rezerwa standardowa.** Sprzedaż miękkiego zapasu dla domu uzasadniana tańszym
   późniejszym zakupem pozostaje polityką agresywną w 1.5.9. Optymalizacja 1.5.8
   nie może wprowadzić jej pod inną nazwą. Zakup z przeznaczeniem na odsprzedaż
   również poza zakresem.
8. **Integracja.** `tariff_price_schedule` jest interfejsem ceny niezależnym od zgody,
   a `tariff_optimizer` ma statyczne okna i odniesienie G11. Nie przepuszczać Pstryka
   przez domyślne G11/manual. Nowa gałąź wyłącznie dla Pstryk; classic RCE i taryfa
   działają także przy awarii źródła oraz bez załadowania nowego klienta.

## Budżet zapisu i koszt obliczeń

Obecny `tariff_price_sensor.py` publikuje rolling schedule z generated_at i
przedziałami co minutę. To konkretny wzorzec, którego nie należy kopiować do
Pstryka. W wersji offline wykluczono duże/zmienne atrybuty rolling schedule z Recorder; wartości i działanie classic pozostają bez zmian. Instalacji nie zmieniano.

| Dane | Miejsce i moment zapisu | Granica / dowód |
| --- | --- | --- |
| Publiczna seria netto dla BUY/SELL | Jeden cache entry-local; zapis po zmianie treści albo okresowym checkpointcie świeżości. Sukces zapisu potwierdzany dopiero po I/O. | Maks. 50 ramek w odpowiedzi, limit bajtów HTTP; bez historii kolejnych polli. |
| Pełny przyszły plan/ceny i debug | Pamięć + ograniczony endpoint/eksport na żądanie, atrybuty live jawnie wyłączone z Recorder. | Żadnej pełnej tabeli per tick, ani osobnych kopii BUY i SELL. |
| Bieżąca cena i stan źródła | Mała stabilna projekcja: godzina UTC, netto, jednostka, rewizja, jakość/powód. | Bez received_at, age_seconds, liczników retry i runtime_ms. Zapis przy zmianie znaczenia. |
| Historia rzeczywistego wykonania | Istniejące recorded_execution v3 bez zmiany schematu; źródło w historii selektora i nowej encji ceny, korelowane przez UTC. | Nie tracić START/STOP/readback ani 48 h/Wczoraj; korekta ceny nie przepisuje historii. |
| Pochodzenie kWh | Mały checkpoint stanu + zaksięgowane znaczniki/liczniki; bez rozszerzanej listy. | Przed udzieleniem eksportu po restarcie dowód musi być trwały; awaria zapisu blokuje nową zgodę. |
| LOAD, PV, LTS | Obecny wspólny broker i cache, obecne liczniki/statystyki. | Zero nowych skanów Recorder per replan; nie wyłączać liczników/history wymaganych przez modele. |

Test offline ma policzyć: liczbę zdarzeń publikacji, unikalnych projekcji, sumę
bajtów JSON, liczbę żądań/zapisów cache i limit rozmiaru. To nie jest pomiar
DB/WAL ani oszczędność całego HA. Przed integracją potrzebny test z rzeczywistym
izolowanym HA Recorder, a po wdrożeniu osobne 24/72 h DB+WAL, page/freelist,
wiersze encji/hosta, unikalne atrybuty, LTS, rozmiary Store/logów i wolny dysk.
Zmiana retencji lub purge nie jest środkiem do ukrywania nadmiernych zapisów.

Solver: single-flight; nie blokować event loop. Nie uruchamiać dwóch pełnych
optymalizatorów Pstryka. Osobny szybki revalidator/bezpieczeństwo. Test czasu i
pamięci dla 49 godzin, luki, korekty ceny i częstego LOAD; wynik ma podawać
ograniczenie metody, nie deklarować globalnego optimum bez dowodu.

## Braki dowodowe i granica tego etapu

Publiczna analiza 29.09, wykonana po dodatkowym poleceniu użytkownika bez klucza,
dostarczyła 336 godzin cen 15–28.09 oraz odtworzenia scenariuszy LOAD/PV 22–28.09.
Doprecyzowanie użytkownika wybiera ten sam publiczny priceNet do docelowego
planera, dla obu kierunków. Nie ma wymagania klucza ani pary cen konta.
Źródło cen nadal jest osobnym dowodem od gotowego planera i fizycznego wykonania.

- 24/28 najtańszych godzin miało nadwyżkę PV przy przyjętym PV × 0,95. Zakup do
  pełna może wypierać własną energię. Model musi uwzględnić headroom oraz pełną
  trajektorię, a nie tylko różnicę dwóch cen.
- `priceNet = -0,01` współistniało z publicznym `priceGross = 0,09`. Model
  ma używać -0,01 po obu stronach, bez brutto, dopłat lub domyślnego zera.
  Średnie nagłówkowe i flagi tanio/drogo nie zastępują godzinowego priceNet.
- Bez jednakowego zapasu końcowego ostatni wieczór pozornie poprawiał wynik
  przez opróżnienie baterii przed końcem zbioru. Dodano warunek co najmniej takiego
  samego zapasu końcowego jak w autokonsumpcji, bez osłabiania rezerwy domu.
- Małe dodatnie intencje znikały po ograniczeniu do kwantu 100 W i minimum 300 W
  w scenariuszu. Próg ceny przed kwantyzacją nie jest dowodem wykonalnej korzyści.
- Przy 5 kWh użytecznych reguły cenowe minimalnie przegrywały z autokonsumpcją.
  Konieczny jest wybór po porównaniu pełnych wariantów, z możliwością zera handlu.

12 testów referencyjnego scenariusza PASS; jawne założenia baterii, przyszłe
rzeczywiste PV/LOAD znane z góry, jednakowa cena referencyjna netto po obu stronach.
Nie są to prognozy faktury, produkcyjny planer ani dowód działania w polu.
Pełny raport: `PRIVATE_EVIDENCE/2026-09-29_pstryk_offline/public_history/ANALIZA_PUBLICZNA_PSTRYK.md`.

Odczyt rzeczywistej historii 22–28.09 hosta100 ujawnił dodatkowe pułapki wejść:

- Zbiorczy licznik LOAD miał 11 małych cofnięć wewnątrz doby (0,001–0,015 kWh).
  Nie traktować każdej ujemnej różnicy jako resetu i dodania całego nowego stanu.
  Zachować kwalifikator LOAD oraz jego istniejący próg szumu, sumę faz i niezależną
  kontrolę profilu zamiast budować drugi, uproszczony estimator.
- Eksport od samej północy zawierał jeszcze stan poprzedniej doby. Bez wcześniejszej
  próbki kwalifikator słusznie nie mógł potwierdzić carryover (6/7 dni). Po pobraniu
  dodatkowych 2 h kontekstu niezmieniony `load_history_v6_bounded_phase_windows`
  zaakceptował 7/7 dni i profili, 113,8 kWh łącznie, średnio 16,257 kWh/dobę.
  Nie usuwano próbek ani nie osłabiano bramki resetu. Granica eksportu jest częścią
  jakości danych, a chwilę resetu trzeba dowieść, nie zgadywać z samej godziny.
- Dłuższy eksport 15–28.09 nie miał pierwszej doby, a starsze punkty były rzadsze.
  Widoczny wykres 14 dni nie dowodzi 14 dni danych w rozdzielczości sterowania.

To wykonanie istniejącego kodu LOAD na rzeczywistym eksporcie, nie próba nowego
planera Pstryk, odbiór modelu na hoście ani potwierdzenie prognozy dostępnej wtedy.
Dowody: `ACTUAL_LOAD_QUALIFICATION.json` (bez kontekstu),
`ACTUAL_LOAD_QUALIFICATION_WITH_ANCHOR.json` oraz `DATA_ACQUISITION.md`
w prywatnym raporcie `PRIVATE_EVIDENCE/2026-09-29_pstryk_offline/`.

Klient publiczny odtwarza przechwycony odczyt bez poświadczeń. Potwierdzono 336 h
netto po obu stronach. Testy syntetyczne obejmują zmianę kontraktu, DST, brak
jutra, błędne daty, timeout i bounded cache. Bramka konta/API key została usunięta;
pozostaje odbiór pełnego planera, HA i wykonawcy na konkretnym kandydacie.

Pierwszy pakiet implementacji obejmuje źródło godzinowych cen, powiązany model
profilu i ograniczony cache/projekcję zapisu oraz ich regresje. Następne bramki
to wspólny planer, trwała proweniencja energii, HA/UI, zamknięta pętla i Recorder.
Żaden z nowych modułów pierwszego pakietu nie nadaje uprawnienia wykonania ani
nie jest rejestrowany w runtime HA. To rozpoczęta implementacja, nie gotowy deploy.


## Zamknięcie implementacji offline 29.09

Wyniki wcześniejszej analizy referencyjnej powyżej pozostają historycznym
eksperymentem. Nowy [raport implementacji](PSTRYK_1_5_8_IMPLEMENTATION.md) opisuje
produkcyjny rdzeń, runtime HA i właściwy rolling replay bez przyszłych pomiarów.

Dodatkowe RED → GREEN znalezione podczas implementacji:

- Zweryfikowane GCF OFF oznacza brak dodatkowego limitu, a nie brak danych.
  Nadal obowiązuje limit AC; odczytane zero i brak świeżego GCF blokują zgodę.
- CCL, DCL, moc PV/ładowania oraz moc sprzedaży są osobne. Niski limit użytkownika
  dla zakupu nie ogranicza fizyki naturalnego PV ani zasilania domu. DCL=0 nie
  uzasadnia kupowania niewykonalnego marginesu domu.
- Połączone ładowanie i oddawanie między odczytami licznika rozlicza się jako
  kredyt PV, a następnie pełny debet. Odwrotna kolejność tworzyła pozorny PV.
- Próg minimalnej korzyści startu nie jest ponownie wymagany od każdej minuty
  już potwierdzonej transakcji. Tylko świeże same-entry zobowiązanie Supervisora
  pozwala na dodatni, mniejszy zysk bieżącej kontynuacji. Taki wynik nie daje
  zgody na nowy START. Brak pochodzenia PV, GCF=0 i pozostałe STOP nadal wygrywają.
- Bieżąca cena musi istnieć; pierwszy przyszły przedział nie może udawać obecnego.
  Cele ładowania są kwantowane do całych procentów także w bilansie ekonomicznym.
- Jesienny DST przechodzi pełen adapter: 49 godzin / 98 różnych półgodzin UTC;
  powtórzone 02:00 zachowuje dwie różne ceny. Samo przesunięcie zegara o 50 ms
  bez zmiany pomiarów nie powoduje wiecznego odrzucania wyniku.
- Korzyść jest wspólna; nie publikujemy drugiej, dodawanej korzyści taryfy.
- Izolowany HA/SQLite potwierdza wykluczenie tablic cen: 73 wiersze/72 h,
  <=391 B na unikalny zestaw atrybutów. Pomiar DB/WAL całego installation_1 pozostaje
  osobną bramką Recordera, a obserwacja po wdrożeniu Pstryk będzie kolejną.

Ograniczenia konserwatywne: brak kwalifikacji liczników dla instalacji równoległej
oznacza zerowy budżet sprzedaży z baterii; zakup może działać. Luka >300 s,
nieciągłość lub zmiana dnia zerują dowód pochodzenia. To może ograniczyć sprzedaż
PV, ale nie tworzy uprawnień na podstawie samego SOC. Nie kwalifikujemy tych
kompromisów jako potwierdzonej skuteczności na wszystkich instalacjach.
