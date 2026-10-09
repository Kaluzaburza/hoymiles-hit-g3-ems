# EV w modelu domu — wspólny kandydat Pstryk 1.5.8RC1

01.10.2026. Implementacja offline; rozpoczęty kontrolowany odbiór installation_1,
bez zgody na rozszerzenie na pozostałe hosty przed PASS. Wspólna baza
Recordera/sterowania `9ea98786c4a557fa412baa4ae501018ff64fe298` z gałęzi
`fix/ems-unified-control-20261001`. [Dowody scalenia](PSTRYK_EV_UNIFIED_20261001.md).
Ta delta rozszerza
[kandydata Pstryk/PV](PSTRYK_RC1_OFFLINE_COMPLETION.md). Wdrożenie, odbiór
instalacji i publiczne wydanie pozostają oddzielnymi bramkami.

## Ustawienia użytkownika

Ustawienia EMS → **Samochód EV**:

- Filtr EV: domyślnie wyłączony, wybór odtwarzany po restarcie.
- Typowa moc ładowania: użytkownik wpisuje ręcznie 1–50 kW, krok 0,1 kW.
  Nowa instalacja ma puste pole (wewnętrzne zero = brak konfiguracji).
  Bez podania mocy filtr nie koryguje modelu. Nie zakładamy 11 kW.
- Opcjonalne pole na encję `sensor.*` z mocą ładowarki w **W lub kW**.
  Puste pole lub startowe `unknown` oznacza wykrywanie orientacyjne.
  Niedostępność helpera `unavailable` pozostaje błędem. Wpisanie niepoprawnej encji
  nie uruchamia po cichu wykrywania ani odejmowania nominalnej mocy.

Czujnik musi mierzyć tylko auto znajdujące się wewnątrz tego samego bilansu
LOAD. Nie wybierać licznika całego domu, energii kWh ani mocy zadanej ładowarki.
V2G/ujemna moc nie są obsługiwane. Moduł nie steruje autem ani ładowarką.

## Zachowanie i ograniczenia

Jeden model RCE dostarcza prognozę domu również taryfie i Pstrykowi.
Surowy LOAD, liczniki fazowe, kwalifikacja historii, bieżący bilans, limity
mocy i zabezpieczenia sterowania nadal uwzględniają całe rzeczywiste obciążenie.
W Pstryku bieżący slot nadal używa pełnej zmierzonej mocy, również EV.

**Z czujnikiem:** odjęcie wyłącznie zmierzonej energii EV od uprzednio
zakwalifikowanego profilu LOAD. Dzień odrzucany przy brakach danych,
konflikcie znaczników, unavailable/unknown, złej jednostce albo energii EV
przekraczającej LOAD. Dopuszczalny szum porównania to 0,005 kWh na półgodzinę.
Integracja mocy odbywa się w UTC, profile mają 48 pozycji zegarowych;
dobom zmiany czasu odpowiada faktyczne 23/25 godzin energii.

Historia dodatniej mocy musi zawierać raporty w odstępach najwyżej 600 s.
Nie włączamy automatycznie `force_update` i nie dopisujemy pomiarów do bazy.
Czujnik zapisujący tylko początek i koniec wielogodzinnego stałego ładowania
nie wystarczy do kwalifikacji tego dnia. Wtedy pozostaje ostrożna prognoza.
Zapisane 0 W można utrzymać: nie powoduje odejmowania żadnej energii.
Historia jest interpretowana w jednostce wybranego czujnika; po zmianie
jednostki cache jest odrzucany. Należy zapewnić stałą jednostkę historyczną
w analizowanym okresie — stary Recorder nie dostarcza jej w tym odczycie.

Bieżąca korekta prognozy używa LOAD minus EV tylko przy raporcie EV
0–180 s, różnicy czasu EV/LOAD najwyżej 90 s oraz EV <= LOAD + 100 W.
Niepoprawne źródło zatrzymuje tę korektę. Czas `last_updated` widoczny
w przeglądarce nie jest uznawany za dowód braku świeżych raportów
`last_reported`; rozstrzyga backend.

**Bez czujnika:** co najmniej dwie kolejne półgodziny z dużym nadmiarem
względem niskiego kwartylu dnia (65–140% wpisanej mocy) oznaczają dzień
podejrzany. Cały dzień zostaje pominięty w uczeniu. Nie odejmujemy 11 kW
„na oko” ani nie wypełniamy brakujących fragmentów zgadywaną energią.
Duża bieżąca odchyłka wstrzymuje utrwalanie wzrostu w prognozie domu.
To heurystyka: ogrzewanie może ją zmylić, ładowanie przez całą dobę albo
zmienna niewielka nadwyżka PV może pozostać niewykryta. Dla takich przypadków
zalecany jest czujnik rzeczywistej mocy.

Po odrzuceniu danych muszą pozostać co najmniej trzy poprawne dni do pełnego
modelu. Wcześniej prognoza nie spada poniżej dotychczasowej średniej surowego
LOAD lub skonfigurowanej wartości awaryjnej. Surowa historia pozostaje do
dyspozycji po wyłączeniu filtra. Przy włączonym filtrze dobowy licznik,
zawierający EV, nie napędza ponownie korekty prognozy. Surowe astronomiczne
okna nocne nie mieszają się z oczyszczonym profilem: noc jest wyliczana ze
wspólnego modelu domu. Nie prognozujemy przyszłego harmonogramu auta.

## Recorder, wydajność i poprzednie pułapki

- Brak nowej encji pomiarowej, nowej tabeli, dodatkowego writera i nowego Store.
- Do istniejącego cache kwalifikowanego LOAD dochodzi opcjonalny blok EV,
  maksymalnie 28 dni × 48 liczb. Oryginalne dane/cache identity zostają zachowane.
- Maksymalnie cztery zapytania dobowe EV na godzinę, najnowsze dni najpierw;
  10 000 wierszy na dobę, limit 7 s na zapytanie i 8 s na całą serię.
  Odczyt następuje po obowiązkowej historii LOAD. Brak zapytań na callback mocy.
- Jeden trwający odczyt; zmiana konfiguracji unieważnia jego wynik, także A→B→A.
  Źródło EV jest objęte fingerprintem wejść solvera; ustawienia są obserwowane.
- Gotowy profil jest ponownie używany w RAM. Store zapisuje się wyłącznie przy
  zmianie zawartości podczas istniejącego odświeżenia historii. Nie ma zapisu
  na każdy odczyt i nie zwiększamy retencji ani częstotliwości sensorów.
- Test RED wykazał 4321 wierszy przy 4320 zmianach atrybutów, mimo ich
  wykluczenia z Recordera. Dlatego zmienne statusy EV nie są publikowane
  w encji planu. Test GREEN: **1 wiersz / 4320 wywołań / symulowane 72 h**.
  Baza testowa po zamknięciu: 151 552 B, WAL 0 B. To koszt izolowanej projekcji
  diagnostycznej, nie pomiar całego EMS ani dodatkowej historii ładowarki.
- Jeżeli użytkownik zacznie rejestrować wcześniej nierejestrowany sensor EV,
  jego własne wiersze są osobnym kosztem. Nie deklarujemy go jako zerowego.

## Dowody offline i odbiór

Scenariusz syntetyczny 14 dni: dom 1 kW, EV 11 kW przez 2 h w pięciu dniach.
Surowe dni EV: 46 kWh. Filtr z pomiarem zachowuje 14 dni domu po 24 kWh;
heurystyka zachowuje 9 czystych dni po 24 kWh. Dla zmiennego ładowania PV
test potwierdza ograniczenie heurystyki i poprawność odjęcia pomiaru.
To test modelu, nie wynik realnego ładowania ani pomiar oszczędności.

Testy obejmują W/kW, niepoprawne źródła, stale/future/cohort, zmianę ustawień,
restart/cache identity, A→B→A podczas zapytania, braki historii, UTC/DST,
brak powrotu EV przez korektę dobową, wspólny model oraz zachowanie surowego
LOAD w bieżącym slocie Pstryka. Formularz PL/EN sprawdzono testami Node;
renderowanie i wpis encji w lokalnej przeglądarce: 1440 oraz 390 px.

Dowody, logi, nowy manifest i ZIP: `PRIVATE_EVIDENCE/2026-10-01_ev_load_filter/unified/`.
Poprzedni pakiet z 30.09 pozostaje niezmieniony. Pełny raport wymienia
odziedziczone HOLD; nie osłabiamy walidatorów starego freeze.

Przed późniejszym wdrożeniem: potwierdzić zakończenie audytu i dokładny
aktualny SHA gałęzi docelowej, sprawdzić patch, backup oraz rollback.
Po wdrożeniu potwierdzić właściwy licznik EV i faktyczne zapisy historii,
obserwować model bez/ze filtrem oraz przy zaniku sensora. W tej turze nie
połączono się z HA ani falownikiem. Bramka wykonania opóźnienia PV nadal
`EXECUTION_ACCEPTED=False`; nowy filtr jej nie otwiera.
