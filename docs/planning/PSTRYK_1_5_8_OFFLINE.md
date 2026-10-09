> Aktualizacja 01.10.2026: [Pstryk/PV/EV na wspólnej bazie 9ea9878](PSTRYK_EV_UNIFIED_20261001.md).
> Docelowa gałąź: `fix/ems-unified-control-20261001`. Poniżej zachowano poprzednie checkpointy.

> Aktualizacja końcowa 30.09.2026: [połączony kandydat na bazie Recordera
> 4d75b5c](PSTRYK_RC1_OFFLINE_COMPLETION.md) ma wykonane testy integracyjne,
> kwalifikację LOAD Pstryka, dwa fizyczne pomiary PV oraz źródło BMS 13 s.
> Poniższy zapis jest wcześniejszym checkpointem. Zastępują go aktualne
> wyniki i bramki z podlinkowanego raportu; PV nadal EXECUTION_ACCEPTED=False.

# Pstryk 1.5.8RC1 — plan wykonania offline

> Aktualizacja 30.09: wspólny kandydat Pstryk + opcjonalne opóźnienie ładowania
> PV jest opisany w [bieżącym planie](PV_CHARGE_DELAY_1_5_8_OFFLINE.md).
> Poniższy checkpoint 29.09 i jego wyniki pozostają historyczne. Nowy
> replay zastępuje dawną korzyść po poprawce symulacji zerowej sprzedaży.

2026-09-29. Użytkownik zatwierdził analizę, aktualizację planu i rozpoczęcie kodu
offline; dodatkowo wymaga optymalizacji wzrostu bazy. Wdrożenie nastąpi dopiero
po rozliczeniu obserwacji Recordera i przygotowaniu dokładnego kandydata.

Baza: `34835067b832259ca4b28fa34c3db489f1357c2c`.
Izolowany worktree: `<LOCAL_CHECKOUT>`.
Canonical `1.5.8RC1` oraz dirty checkout recovery nie są miejscem tych edycji.
Bez commit/push/PR, zmiany hosta, konfiguracji, restartu i prób energii.

## Zakres wiążący

- RCE + zwykłe taryfy zachowują bieżącą logikę. Nowy wspólny plan tylko dla Pstryk.
- „Sprzedaż dynamiczna”: RCE / Pstryk. Dostawca ładowania: Pstryk jako nowa opcja.
  Jedno źródłowe pole profilu; wybór Pstryk z obu widoków ustawia BUY i SELL razem.
- Zgody zakupu/sprzedaży osobne, nigdy automatycznie włączane wyborem źródła.
  Powrót do RCE przywraca klasyczne ustawienia lub wymaga ich wyboru.
- Publiczne godzinowe `priceNet` Pstryka w PLN/kWh jest wspólną ceną zakupu
  i sprzedaży: BUY = SELL = priceNet w danej godzinie. Bez klucza, konta,
  `priceGross`, opłat, dystrybucji i HACS. To doprecyzowanie użytkownika zastępuje
  wcześniejszy szkic pary brutto z API konta; nie ma bramki weryfikacji takiej pary.
- Chronić potrzeby domu/rezerwę. Bez odsprzedaży zakupionych/nieznanych kWh.
  Polityka agresywna i nowy model pompy ciepła pozostają w 1.5.9.
- Istniejący Supervisor, writer, lease/deadline i fizyczne zabezpieczenia pozostają
  jedyną drogą wykonania. Błędne dane lub stary wynik nie dają nowej zgody.

Pełny zapis ustaleń użytkownika pozostaje w
`<LOCAL_CHECKOUT>/_local/worktrees/v1.5.9/docs/planning/ZAKRES_PSTRYK_1_5_8.md`.
Analiza, źródła lekcji i regresje: [PSTRYK_1_5_8_RISK_REVIEW.md](PSTRYK_1_5_8_RISK_REVIEW.md).

## Kolejność i kryteria zakończenia

| Etap | Zakres | Wymagany dowód / stan początkowy |
| --- | --- | --- |
| P0 | Analiza incydentów RCE/taryfy i Recordera | Wykonana; wnioski, regresje i granice w analizie ryzyk. |
| P1 | Publiczne priceNet, UTC/DST, cache, single-flight | PASS offline: 65 testów fundamentu i odtworzenie 336 h / 14 dni bez klucza. |
| P2 | Jeden profil, atomowe powiązanie selektorów, ABA | PASS offline: trwały profil ładowany przed uruchomieniem planerów; osobne zgody; brak fallbacku mieszanego profilu. |
| P3 | Wspólna trajektoria PV/LOAD/SOC i ekonomia | Implementacja i testy offline PASS; produkcyjny rdzeń odtworzony w 336 krokach na 7 dniach instalacji. |
| P4 | Pochodzenie PV i trwałość | PASS offline: licznikowy, konserwatywny budżet; debet natychmiast, kredyt dopiero po zapisie; niepewność/restart nie tworzą prawa do sprzedaży. |
| P5 | Runtime HA, wspólna rewizja, istniejący Supervisor | PASS testów Pstryk na HA 2026.8.2 oraz 2026.9.2, w tym rzeczywiste szablony gotowości i kandydaci BUY/SELL. Wykonanie na urządzeniu PENDING. |
| P6 | UI PL/EN i publiczna cena netto | PASS offline: wybór dostawcy, „Sprzedaż dynamiczna”, chart netto, zachowanie klasycznych nastaw i zgód; wygenerowane zasoby. |
| P7 | Regresje, koszt, Recorder, pakiet review | Testy funkcji i pomiar nowej encji w rzeczywistym HA/SQLite PASS. Historyczne walidatory wydania / niezależne testy bazowe pozostają HOLD; dokładny wykaz w raporcie implementacji. |
| P8 | Integracja do aktualnego RC1 po Recorderze i wdrożenie | PENDING: odbiór Recordera, aktualny SHA, integracja/retest, manifest, backup/rollback, osobne odbiory BUY/SELL i storage. |

P3–P7 są obecnie kodem offline, a nie samym fundamentem. Oba widoki planu
otrzymują jedną zaakceptowaną rewizję. Nowe selektory nie uruchamiają dwóch
konkurujących optymalizatorów. Szczegóły, wyniki i pozostałe ograniczenia:
[PSTRYK_1_5_8_IMPLEMENTATION.md](PSTRYK_1_5_8_IMPLEMENTATION.md).

## Publiczne źródło i dane testowe

Źródło: [publiczny wykres Pstryka](https://www.pstryk.pl/ceny), cena netto
PLN/kWh. Ten sam anonimowy odczyt `getDailyPrices(YYYY-MM-DD)` służy analizie
i zaimplementowanemu runtime offline. Klient używa `frames[].priceNet` bez przeróbek.
Nie interpretuje brutto, cen aplikacji, średnich ani flag tanio/drogo jako poleceń.

Jest to publiczny odczyt strony przez POST `/ceny`, z identyfikatorem `Next-Action`
odczytanym z jej jawnych plików JS. Nie deklarujemy stałego, dokumentowanego REST.
Klient odkrywa identyfikator, czytając ograniczony zestaw plików tego samego hosta;
nie wykonuje JS. W pamięci utrzymuje identyfikator, bez ponownego pobierania
plików przy każdym pollu. Zmiana kontraktu nie przełącza źródła na konto/PSE.

Jedno współdzielone odświeżenie obejmuje najwyżej dwie daty Warszawy, po jednym
odczycie na dobę. Dane każdej doby muszą pasować do jej rzeczywistych granic UTC.
Null jutra pozostaje brakiem pokrycia. Cena brutto nigdy nie uzupełnia netto.
HA adapter używa własnej anonimowej sesji z DummyCookieJar, bez domyślnego
Authorization; klient nie przyjmuje klucza. Brak danych nie daje zgody sterowania.

Przechwycone 14 dni / 336 h przeszły nowy parser i klient offline: oba kierunki
są identyczne z priceNet, z zachowaniem 4 cen ujemnych i 6 zerowych. To odtworzenie
źródła, osobne od testu wspólnego planera i aktywacji na instalacji.

## Granice kosztu i testy

- Jedna wartość netto na godzinę, współdzielona przez BUY/SELL. Przeliczenie cen po zmianie znaczenia, nie po
  każdej zmianie `received_at`; ważność wieku sprawdzana osobno.
- Cena/cache: maks. 50 ramek (49 na dwie doby z jesiennym DST), 128 KiB odpowiedzi cenowej na dobę,
  brak sumowania kolejnych odpowiedzi. Dla awarii sieci maksymalny wiek cache
  2 h jako jawne konserwatywne założenie pierwszej wersji; nie jest gwarancją API.
- Jedna operacja fetch naraz; twardy timeout, obsługa 401/403/429/5xx, brak
  automatycznego fallbacku do PSE/G11. Runtime stosuje backoff 5–30 minut.
  Discovery: maks. 32 skrypty, 512 KiB HTML, 2 MiB/skrypt, 4 MiB łącznie,
  deadline całego fetch 45 s oraz 15 s/żądanie. Ograniczenia nie dotyczą Recordera.
  Cache schema 2 + jawny kontrakt/baza ceny odrzucają stare dane brutto.
- Historyczna projekcja cen ma mieścić się w 1 KiB i nie zawierać tablic ani
  zmiennego wieku/znaczników odświeżenia. Zapis cache maks. raz na UTC godzinę
  przy niezmienionej treści; korekta treści wymusza nowy zapis. Błąd I/O nie jest ACK.
- Syntetyczne 72 h odświeżeń mają mieć koszt ograniczony liczbą godzin i zmian
  treści, nie liczbą callbacków. Test nie stanowi pomiaru całego HA SQLite/WAL.
- Pełne testy planera, lifecycle, bezpieczeństwa i HA należą do odpowiednich
  etapów P3–P7. Nie zamykać ich samym testem parsera ani statycznym grepem.

## Odtworzenie 7–14 dni na rzeczywistych danych — doprecyzowanie użytkownika

Użytkownik 29.09 wymaga prób offline na oficjalnych cenach Pstryk oraz rzeczywistej
instalacji `installation_3` lub `installation_2`. Okno docelowe: 15–28.09.2026
w Europe/Warsaw (14 pełnych dób), minimalne pierwsze okno 22–28.09 (7 pełnych dób).

1. Zamrozić lokalne surowe dane i SHA256, opis hosta, jednostek, encji, zakresu,
   czasu pobrania, retencji i jakości. Bez tokenów i pełnego backupu HA w repo.
   Pobierać tylko potrzebne encje/liczniki, preferować ograniczone eksporty;
   żadnego skanowania całej bazy ani dodatkowych helperów na hoście.
2. Publiczna seria Pstryk priceNet, ta sama dla BUY/SELL. Weryfikować źródło,
   jednostkę, przedziały i kompletność. Brak klucza/konta nie jest brakiem danych
   tego profilu. PSE ani ceny brutto nie zastępują wybranej serii netto.
3. LOAD/PV z historii: rozróżnić liczniki od mocy, reset dobowy i lukę; nie liczyć
   równocześnie LOAD łącznie i sumy faz. LTS ma mniejszą rozdzielczość i nie
   potwierdza chwilowych BMS/GCF/readback. Daty godzin zawsze z offsetem/UTC.
   Dobrać wcześniejszy kontekst licznika do pierwszej północy; surowe cofnięcie
   nie jest automatycznie resetem. Reuse produkcyjnego kwalifikatora LOAD.
4. W odtworzeniu decyzji wolno użyć tylko cen i prognoz znanych w danym momencie.
   Archiwalna cena pobrana dzisiaj nie dowodzi czasu jej pierwszej publikacji.
   Przyszłe rzeczywiste PV/LOAD są wejściem symulatora instalacji, nie wiedzą planera.
   Przy braku archiwalnych prognoz oznaczyć test jako retrospektywny/scenariuszowy;
   osobno rolling forecast z dni poprzednich, osobno wariant idealnej znajomości.
5. Początkowy rzeczywisty SOC inicjuje symulację. Dalej SOC wylicza nowa trajektoria;
   nie podstawiać co godzinę SOC z instalacji pracującej według starego EMS.
   Nieznane pochodzenie początkowej energii pozostaje nieuprawnione do sprzedaży.
6. Porównać wykonalne warianty: autokonsumpcja, zakup bez sprzedaży, sprzedaż bez
   zakupu, wspólna polityka standardowa Pstryk. Raportować kWh, przychód/koszt
   według użytych cen, wear, unmet demand/margin, końcowy SOC, energię PV odrzuconą,
   liczbę START/STOP/retarget, ograniczenia mocy i pokrycie wejść. Stary rzeczywisty
   wynik jest obserwacją porównawczą, nie równoważnym eksperymentem A/B.
7. Dodać zakłócenia do tego samego zbioru: timeout API, wycofanie jednej ceny,
   korekta publikacji, zmiana profilu podczas workera, utrata BMS/LOAD, restart
   i granice półgodzin/godzin. Pomiar kosztu zapisu/procesora jako osobny wynik.

Stan: pobrano do lokalnego raportu CSV LOAD/PV/SOC z hosta100, 68 834 wiersze,
SHA256 `6fc6d6e4635c541206d952733cdee9a6c583c34a65fa7196db85b5561a4b3444`.
Wybrany eksport: 15–28.09. Brak danych 15.09, 16–18.09 znacznie rzadsze próbki,
od 19.09 gęstsza historia. Pierwsze okno do dalszej kwalifikacji: 22–28.09.
Pobrano też trzy liczniki faz wraz z 2 h kontekstu sprzed tego okna:
SHA256 `2041895897ce0483ee7c241fd57562e1b24ef682458636ef6619ed3c80ff6bf6`.
Niezmieniony LOAD v6 zaakceptował 7/7 dni oraz profili, 113,8 kWh, średnio
16,257 kWh/dobę; wykonanie offline około 0,48 s. Przebieg bez wcześniejszego
kontekstu zachowano: 6/7 dni, carryover pierwszej północy nieudowodniony.
Wykryto 11 małych cofnięć LOAD wewnątrz doby; nie przerobiono ich na reset.
To PASS kwalifikacji historycznego LOAD. Późniejszy rolling replay rdzenia opisano w raporcie implementacji.
Wyjściowe CSV i raport jakości są prywatnymi danymi lokalnymi, nie fixtures produktu.
Wcześniejsze pytanie o klucz/parę konta jest nieaktualne po doprecyzowaniu
źródła publicznego netto. Nie jest warunkiem dalszej implementacji ani odbioru źródła.
Testy syntetyczne fundamentu oraz historyczny replay ekonomii mają osobne
wyniki; żaden nie potwierdza live acceptance.

### Uzupełnienie 29.09 — publiczna historia bez klucza

Użytkownik polecił wykonać tę analizę na danych publicznych. Klucz nie jest
warunkiem jej wykonania. Pobrano z publicznego wykresu Pstryka 15–28.09:
336 kolejnych godzin, bez konta/cookies/API key, przez odczyt `getDailyPrices`
używany przez stronę. Surowe odpowiedzi i SHA256 zachowano lokalnie.

Wartości `priceNet` są wspólną ceną zakupu/sprzedaży w uzgodnionym modelu EMS,
bez dopłat; `priceGross` zachowano w historycznym raporcie wyłącznie jako dane
źródłowe. Po korekcie parser potwierdza pełne pokrycie BUY/SELL na 14/14 dobach.
Brak osobnego pola sprzedaży jest prawidłowy dla tego kontraktu. Brak priceNet
blokuje oba kierunki i nigdy nie jest uzupełniany przez brutto ani zero.

Odtworzono referencyjne warianty dla 7 dni LOAD/PV hosta100, 5/10/15 kWh
pojemności użytecznej i 12 wariantów wrażliwości. Początkowy SOC z ostatniej
próbki przed startem (43%), później wyłącznie wyliczana trajektoria; rezerwa,
pojemność, sprawności i limity są jawnymi założeniami. Przyszłe rzeczywiste
PV/LOAD są znane w tym eksperymencie, więc nie jest to test jakości prognozy.

Wnioski: 24/28 najtańszych godzin przypadało na nadwyżkę PV (założenie PV × 0,95);
ranking cen sam nie wystarcza. Porównanie musi zachować jednakowy zapas końcowy,
oceniać moc po kwantyzacji i umieć zachować autokonsumpcję, gdy handel pogarsza wynik.
12 testów scenariusza PASS; 9 głównych i 12 dodatkowych przebiegów z asercjami
bilansu AC/DC, SOC, pochodzenia i mocy. Referencyjne reguły nie są produkcyjnym
planerem i nie dowodzą globalnego optimum ani oszczędności na fakturze.

Raport: `<PRIVATE_EVIDENCE>/2026-09-29_pstryk_offline/public_history/ANALIZA_PUBLICZNA_PSTRYK.md`.
Publiczny odczyt jest wybranym źródłem docelowym. Jego poprawne działanie
nie nadaje samo w sobie uprawnienia wykonawcy; nadal wymagane są P3–P7.

## Kolejność względem Recordera — checkpoint

Odczytany checkpoint STOR-01: restart installation_1 28.09 22:04:11 CEST,
SHA `34835067b832259ca4b28fa34c3db489f1357c2c`, retencja/purge wyłączone.
`installation_1_ACCEPTANCE=PENDING`, `STORAGE_EFFECT=PENDING`.
+24 h: 29.09 22:04:11 CEST; +72 h: 01.10 22:04:11 CEST.
Źródło: `_local/handoffs/20260925-ems-storage-fixes/execution/20260928T193700Z/NEXT_CHECKPOINT.md`
w głównym repo. Nie łączyć obserwacji różnych SHA/config. Prace offline nie
zmieniają tej serii. Sam upływ 72 h nie oznacza PASS ani zgody na zmianę hosta.

## Aktualny wynik

P0: analiza przygotowana. P1/P2: pierwszy pakiet offline zaimplementowany:
`pstryk_prices.py`, `pstryk_client.py`, `dynamic_price_profile.py`.
65 testów cen/profilu/cache/HTTP i 5 testów jakości wejść historycznych PASS;
regresje classic: price schedule 6 grup, tariff profiles oraz shadow 12 grup PASS.
Testy dopisane do CI; sam CI zdalny nie był uruchamiany.

Benchmark syntetyczny 72 h / 4320 callbacków / 49 przyszłych godzin:
72 kandydaty zapisu cache, 72 różne projekcje cen; cache maks. 4915 B,
projekcja maks. 316 B. Pełna powtarzana odpowiedź dałaby 26 490 240 B tekstu JSON,
unikalne projekcje 22 752 B. To porównanie serializacji, nie oszczędność SQLite/WAL
ani test zachowania rzeczywistego HA Recorder. Konieczny osobny P7.

P3–P8: PENDING. Brak wspólnego planera/pochodzenia kWh/adaptera wykonania i UI
w tym pakiecie; żaden nowy moduł nie jest importowany przez runtime HA. Nie
zmieniono wersji release ani canonical RC1. Publiczna analiza scenariuszowa została
wykonana (uzupełnienie powyżej); test produkcyjnego BUY/SELL nadal wymaga P3/P4
i własnej walidacji. Eksport historii i referencyjne reguły nie zamykają tej bramki.

Powtarzalne komendy pierwszego pakietu:
```
python tools/test_pstryk_offline.py
python tools/test_pstryk_replay_data.py
python tools/benchmark_pstryk_storage.py
python tools/test_tariff_price_schedule.py
python tools/test_tariff_profiles.py
python tools/test_rce_self_consumption_shadow.py
```
Pełne logi, manifest plików i prywatne dane:
`<PRIVATE_EVIDENCE>/2026-09-29_pstryk_offline/`.
