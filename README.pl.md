# EMS dla Hoymiles — 1.5.8RC2

[English](README.md) · [Polski](README.pl.md)

[☕ Postaw kawę autorowi](https://buycoffee.to/kaluzaaa)

![Hoymiles EMS](https://raw.githubusercontent.com/Kaluzaburza/hoymiles-hit-g3-ems/v1.5.8RC2/custom_components/hoymiles_hit_modbus/brand/logo.png)

Nieoficjalny, lokalny EMS dla instalacji magazynowania energii Hoymiles,
oparty na Home Assistant, ESPHome i Modbus. Obejmuje sprzedaż dynamiczną
(RCE/Pstryk), ładowanie taryfowe, prognozy PV i zużycia domu oraz eksperymentalny RCEm.

Użytkownicy **1.5.7** zgłaszają działanie z **HiOne, HIT-(5–20)L-G3, HAS i HAT**.
HIT-G3 pozostaje platformą referencyjną. [Zakres zgodności](docs/COMPATIBILITY.md#polski)
rozdziela te zgłoszenia od odbioru konkretnego modelu i wersji.

[![Otwórz repozytorium w HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Kaluzaburza&repository=hoymiles-hit-g3-ems&category=integration)
[![Najnowsze wydanie](https://img.shields.io/github/v/release/Kaluzaburza/hoymiles-hit-g3-ems?label=release)](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/releases/latest)
[![Walidacja](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/actions/workflows/validate.yml/badge.svg)](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/actions/workflows/validate.yml)
[![Licencja: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Projekt łączy ESP32 z falownikiem przez Modbus RTU oraz dodaje integrację Home
Assistant z polskimi nazwami encji, panel Aurora i opcjonalne automatyzacje EMS.
Cała logika sterowania działa lokalnie. Funkcje zależne od prognozy korzystają
z Solcast. Źródłem cen jest publiczne API PSE dla RCE albo publiczne godzinowe
ceny netto Pstryk dla zakupu i sprzedaży; Pstryk nie wymaga klucza API konta.

**Nowa instalacja:** skorzystaj z
[instrukcji pięciu kroków](docs/QUICK_START.md#polski--pięć-kroków). Poniżej znajdziesz
również przegląd instalacji i dostępnych funkcji.

## Przegląd

> **1.5.8RC2 — wersja przedpremierowa.** Frontend `1.5.8rc2.122`.
> [Opis i aktualizacja](docs/releases/v1.5.8rc2.md) · [Przejście z 1.5.7](docs/UPGRADE_1_5_7.md).
> W HACS włącz wersje przedpremierowe, aby wybrać RC2.

![Aurora v1.5.8: bieżące przepływy, plan energii EMS, ustawienia taryfy i RCEm](docs/images/dashboard-overview.png)

Cztery widoki z rzeczywistej instalacji z Aurorą `1.5.8.72`, wykonane
9 września 2026 r. Te historyczne obrazy `.72` pokazują układ panelu; nie
przedstawiają aktualnych opisów `.122` ani nie potwierdzają wykonania RC2.
Widoczne wartości i włączone opcje należą do tej instalacji;
nie są ustawieniami świeżej instalacji ani gwarancją przyszłych wyników.
Zrzuty pokazują polski interfejs; panel ma również pełną wersję angielską.
Otwórz oryginały: [Przegląd](docs/images/dashboard-start-v1.5.8.png),
[plan EMS](docs/images/dashboard-ems-v1.5.8.png),
[ustawienia taryfy](docs/images/dashboard-tariff-v1.5.8.png),
[RCEm](docs/images/dashboard-rcem-v1.5.8.png).

Aurora łączy bieżącą moc, naładowanie baterii, planowane działania i diagnostykę
w jednym panelu Home Assistanta. Główne zakładki to **Przegląd, EMS,
Ustawienia, PV, Magazyn, Energia i Zyski**. Ustawienia grupują sprzedaż dynamiczną, ładowanie taryfowe,
zarządzanie napięciem i balansowanie. **Serwis** zawiera diagnostykę techniczną
oraz sterowanie ręczne. Komputer i telefon korzystają z tych samych danych.

### Nowości w 1.5.8RC2

- **Zyski:** widoki dnia, tygodnia, miesiąca i roku korzystają z lokalnego archiwum.
  Zakup i sprzedaż mają osobne kWh, kwoty oraz podział na strefy i dostawców;
  brakujące ceny i historia nie są dopisywane. [Opis Zysków](docs/EMS_PROFITS.md).
- **Sprzedaż dynamiczna:** wspólny opis RCE i Pstryk w głównym panelu,
  planach i powiadomieniach push. Źródło ceny nadal jest widoczne;
  eksperymentalny RCEm pozostaje osobną polityką.
- **Zakup i sprzedaż Pstryk:** jeden powiązany profil używa publicznej
  godzinowej ceny `priceNet` bez doliczania VAT, dystrybucji i opłat.
  Zakup i sprzedaż mają niezależne zgody. Zweryfikowane ceny dobowe są
  zachowywane po restarcie HA; brak ceny nie staje się okazją zakupu za zero.
- **PGE G12e:** miesięczne tanie strefy dzienne, noc 22:00–06:00, weekendy
  i polskie święta ustawowe. Automatyczny profil cenowy na 2026 rok ma określoną
  umowę, obszar i okres ważności — zobacz [szczegóły G12e](docs/PGE_G12E.md).
- **Opóźnienie ładowania PV:** profile Zachowawczy 55% (P10), Zrównoważony 50%
  i Maksymalny 20% określają zapas prognozy na późniejsze ładowanie po eksporcie
  PV. Nowy start jest możliwy przed 14:00 czasu polskiego, jeśli plan pozwala
  odzyskać energię do 16:30 i godzinę przed końcem prognozowanej nadwyżki.
  Stałe okno 180 s liczy się od pierwszej komendy. Potwierdzenie wymaga
  świeżego zgodnego pełnego FC03/Mode5. Moce PV/LOAD/GRID/BAT/BMS są
  diagnostyczne, bez progów i blokady na podstawie bilansu. Topologia, gotowość
  urządzenia/BMS, SOC, zgody, lease i pierwotny deadline nadal obowiązują.
  Kolejna neutralna próba wymaga 180 s przerwy i ponownej kwalifikacji;
  replan nie wydłuża terminu końca.
- **Ciągłość transakcji:** zgodne zmiany mocy lub celu zachowują transakcję.
  Protokół lease 2 ogranicza dzierżawę do 120 s, nominalnie odnawia ją co 20 s
  i kwalifikuje stabilizację fizyczną przez 180 s. Widoczny plan nadal wymaga
  aktualnych danych i fizycznego potwierdzenia polecenia.
- **Pełniejsza diagnostyka:** zdekodowane wykonanie i STOP, rewizje planów
  i wejść, pochodzenie LOAD/PV oraz ograniczony dziennik powiadomień ułatwiają
  analizę algorytmów offline. Paczka jawnie wskazuje granice dostępnych dowodów.
- **Szybsze planowanie RCE:** niezmieniony test 110 przedziałów skrócił się
  z około 1,22 s do 0,767 s przy zachowanym limicie 1 s; lokalnie zaliczono
  wszystkie 86 scenariuszy RCE. To wynik komputera testowego, a nie obietnica
  takiego czasu na każdym hoście HA.
- **Dokumentacja instalacji:** ilustrowany schemat połączeń, instrukcja pięciu
  kroków po polsku i angielsku oraz zakres zgodności poszczególnych rodzin.

Seria 1.5.8 wprowadza również wspólną oś PV/LOAD/sieć/SOC, szczegóły przedziału,
historię wykonania **Wczoraj / Aktualnie**, przyczyny działań przy planie,
nawigację mobilną i okno stanu systemu z historią usterek. Odświeżanie wykresu
jest niezależne od cyklu sterowania i przeliczania planu.

Świeża instalacja otrzymuje **5 punktów procentowych marginesu SOC** dla
sprzedaży dynamicznej i RCEm oraz **5% marginesu zapotrzebowania na energię**
dla taryfy. Początkowe nastawy mocy wynoszą **50%** dla rozładowania dynamicznego,
ładowania taryfowego i limitu eksportu RCEm; minimalna oszczędność taryfy to
**1 PLN/kWh**. Wcześniejsze ustawienia pozostają zachowane. Wysoki próg
oszczędności może prawidłowo wykluczyć wszystkie nieopłacalne cykle ładowania.

Kontrola techniczna wdrożenia i testy offline nie kończą odbioru terenowego.
Zobacz [stan wydania](docs/releases/v1.5.8rc2.md#validation-and-known-limits).

Procent profilu PV oznacza wagę P10 we wzorze `P50 × (1 − w) + P10 × w`,
a nie prawdopodobieństwo lub stałą redukcję produkcji. Zobacz
[profile opóźnienia PV](docs/PV_DELAY_PROFILES_2026-10-02.md).

### Dostępne funkcje

| Obszar | Działanie |
|---|---|
| Monitorowanie lokalne | Pokazuje PV, zużycie domu, sieć, baterię/BMS, GEN, alarmy, temperatury i liczniki energii; w wykrytym układzie równoległym korzysta z sum systemowych |
| Sprzedaż dynamiczna (RCE/Pstryk) | Planuje dozwolony eksport według wybranego źródła cen, dostępnej energii, rezerwy operacyjnej i ograniczeń fizycznych |
| Zakup Pstryk | Koordynuje zakup i sprzedaż ze wspólną godzinową ceną netto i niezależnymi zgodami na wykonanie |
| Ładowanie taryfowe | Planuje potrzebny zakup energii w tańszych strefach, uwzględniając zużycie, PV, straty, koszt eksploatacji baterii i wymagany próg oszczędności |
| Eksperymentalny RCEm | Wyznacza miejsce w baterii na okresy wysokiego napięcia; tryb obserwacyjny liczy plan bez zapisów do falownika |
| Balansowanie LiFePO4 | Planuje ładowanie serwisowe, wykorzystuje najpierw PV, w razie potrzeby kończy z sieci i utrzymuje pełny SOC przez ustawiony czas |
| Sterowanie ręczne | Udostępnia tryby pracy, limity mocy/SOC i harmonogramy, z zachowaniem zaimplementowanych blokad oraz kontroli odczytu zwrotnego |
| Diagnostyka | Wyjaśnia jakość danych, właściciela wykonania, blokady i potwierdzenia fizyczne; eksportuje pakiety ZIP z filtrowaniem danych prywatnych |

### Jak plan staje się rzeczywistym działaniem

1. ESP32 odczytuje falownik przez Modbus. HA łączy odczyty z ustawieniami,
   historią zużycia domu, prognozą PV i cenami potrzebnymi danemu planerowi.
2. Wybrane planery przygotowują propozycje; powiązany zakup i sprzedaż
   Pstryk korzystają ze wspólnego planu. Nadzorca EMS wybiera uprawnione działanie. **Włączony planer, widoczny plan i trwające
   wykonanie to trzy różne stany.**
3. Przed zapisem EMS sprawdza aktualne dane, limity, tryb falownika i właściciela
   sterowania. Transakcję prowadzi jeden wykonawca. Brak lub nieaktualność
   krytycznych danych blokuje nowe wykonanie, nawet jeśli starszy plan pozostaje widoczny.
4. Polecenie musi zostać potwierdzone nowszym fizycznym odczytem rejestrów.
   Po działaniu dotychczasowa procedura przywracania nastaw zwalnia właściciela.
   Zmiana przełącznika w HA nie dowodzi, że falownik wykonał polecenie.

Przykładowo **Teraz: Autokonsumpcja** i **Następnie: Sprzedaż dynamiczna o 19:30**
oznaczają zwykłą pracę na własne potrzeby teraz i sprzedaż zaplanowaną później.
Plan może się zmienić wraz z cenami, prognozą lub limitami fizycznymi.
Komunikat taryfy *ładowanie pominięte: niewystarczająca oszczędność* jest
poprawnym wynikiem obliczeń. Włączenie planera nie wymusza zakupu energii.

### Sterowanie EMS

**Wstrzymaj EMS** zatrzymuje wykonanie Nadzorcy i wszystkie dołączone ścieżki
automatyki legacy przez osobny zatrzask fail-closed. Wybrane polityki RCE,
taryfy, RCEm i balansowania pozostają zapamiętane. **Włącz EMS** przywraca
uprawnienie Active dopiero po zwolnieniu tego zatrzasku. Każdy przycisk
**Używaj tej polityki** ustawia helper planera i zgodę Nadzorcy jako jedną,
serializowaną operację; niepełny zapis pozostawia wykonanie zablokowane.
**MASTER STOP** zachowuje rolę awaryjnej transakcji: kończy aktywną pracę,
przywraca potwierdzone nastawy i czyści helpery wykonawcze. Osobne świadome
**Wznów po STOP** odtwarza wybór polityk zapisany przed zatrzymaniem. Jeden
wskaźnik stanu rozróżnia pauzę, gotowość, wykonanie, oczekiwanie na odczyt,
blokadę, przywracanie i wymaganą interwencję.

Dowód hard-stop jest zamrażany w chwili przyjęcia wyzwalacza. Niezależne, ograniczone kolejki FIFO
zachowują kolejność wyzwalaczy. Już uruchomionego wywołania zweryfikowanego helpera nie można anulować;
jego granica bezpieczeństwa jest sprawdzana ponownie po powrocie. Do jej rozstrzygnięcia trwały rekord
pozostaje surowym `APPLYING`.

### Jak powstaje prognoza LOAD domu

Kanoniczne zużycie domu jest sumą rejestrów fazowych LOAD 2170–2172,
całkowaną przez `sensor.hoymiles_actual_load_energy_today`. Nie obejmuje
autokonsumpcji falownika i nie utożsamia importu z sieci ze zużyciem domu,
dzięki czemu energia dostarczona przez PV lub baterię pozostaje w tej samej
granicy pomiarowej.

RCE i taryfa korzystają ze wspólnej prognozy. Baza dobowa
używa do 28 kompletnych dni z Rejestratora HA, siedmiodniowego okresu połowicznego zaniku wagi
i rzeczywistego wieku kalendarzowego, więc brakujące dni nie zawyżają wagi starszych danych.
Sumy liczników faz oraz półgodzinny kształt mają osobną ocenę jakości. Raporty
skumulowanego licznika kształtu są redukowane w SQL do ostatniej wartości
co 150 s czasu modelu, a maksymalna luka profilu wynosi 10 minut; dłuższa
luka, reset, brak fazy lub błędna wartość wyłącza kształt zamiast wpisywać zera
albo rozkładać nieznane zużycie równomiernie na dobę. Profile dni roboczych i weekendów pozostają
oddzielne, a doby 23/25 h zachowują zmierzoną energię przy zmianie czasu.

Rzadko raportujące liczniki energii fazowej są oceniane osobno: sam odstęp
między wartościami liczbowymi nie oznacza przerwy dostępności. Jawne okresy
niedostępności, resety liczników i wymagane pokrycie doby nadal wpływają na
przyjęcie danych. Zapisany profil musi też być świeży — najwyżej 30 godzin
od wygenerowania. Sama historia w Recorderze nie gwarantuje jej użycia przez
planery. W diagnostyce sprawdź jakość modelu, przyjęte dni i przyczynę fallbacku.

Korekta dzisiejszego dnia porównuje zmierzoną energię z całką profilu w tym
samym przedziale od północy do znacznika próbki. Włącza się po dwóch godzinach
i co najmniej 1 kWh oczekiwanego poboru, ma granice 0,80–1,25 i działa tylko na
pozostałą część dzisiejszego dnia. Świeża różnica mocy staje się trwała po
12 minutach gęstych danych w oknie 20 minut, bez luki większej niż 5 minut,
przy progu jednocześnie 0,25 kW i 20%. Korekta ze znakiem jest ograniczona do
3 kW, stosowana z wagą 25%, wygasa w 60 minut i kończy się po dwóch godzinach.
Krótki impuls wpływa więc na niedokończony bieżący przedział, ale nie skaluje
reszty dnia ani jutra. Po wspólnym oczekiwanym LOAD każda polityka zachowuje
własne bufory rezerwy i opłacalności.

### Jak czytać wykres EMS

| Element | Znaczenie |
|---|---|
| Słupki PV i zużycia | Prognozowana moc w **kW**, według górnej skali mocy |
| Słupki pod wykresem | Planowana energia sieciowa w **kWh** dla danego przedziału: niebieski import, żółty eksport; oba są skierowane w dół, aby oddzielić je od mocy |
| Błękitna linia SOC | Przewidywane naładowanie baterii z dostępnego modelu prognozy; źródło jest podane w szczegółach |
| Przerywana linia SOC | Zachowawcza trajektoria planu automatyki, a nie historia zmierzonego SOC |
| Kolor odcinka SOC i jego cienia | Żółty: sprzedaż dynamiczna; niebieski: taryfa; fioletowy: RCEm; zielony: balansowanie |
| Zaznaczony przedział | Osobno pokazuje przepływy przewidywane i przepływy zachowawczego planu; **sieć → magazyn** różni się od całkowitego poboru z sieci |

Obie linie SOC są prognozami. Dolne słupki nie potwierdzają wskazania licznika
ani aktywnego cyklu taryfowego. Korzystają z rozpoznanych przepływów planu,
więc zwykłe zasilanie domu z baterii nie jest rysowane jako ładowanie z sieci.
Komunikat **Przeliczanie** pozostawia poprzedni plan do wglądu, bez przyznawania
prawa do nowego wykonania. Rzeczywista historia energii jest dostępna
w Przeglądzie, Magazynie i Energii.

Przycisk **Wczoraj** przy wykresie planu otwiera **ostatnie 48 godzin wykonania EMS**;
**Aktualnie** przywraca plan. Historia używa tego samego wykresu: zapisany SOC,
słupki PV i domu, import/eksport oraz kolory wykonanej sprzedaży dynamicznej, taryfy i RCEm.
Kolory tych polityk wymagają zgodnej aktywnej transakcji, odczytu i przepływu;
zieleń balansowania oznacza jego zapisany status. Kliknij czas, aby zobaczyć
pomiary co 5 minut, działanie i przyczyny decyzji. Na telefonie wykres przewija się poziomo.
Pod wykresem są cztery sumy energii z potwierdzonych odcinków: sprzedaż dynamiczna,
rozładowanie RCEm, ładowanie taryfowe z sieci i taryfowe zasilanie domu z sieci.
Są to szacunki z zapisanych mocy. Niejednoznaczny rozdział przy produkcji PV
jest zakresem, a brak kompletnych pomiarów nie jest zerowym zużyciem.
Źródłem jest HA Recorder: trzeba zachować odpowiednie encje i atrybuty nadzorcy.
Nie odtwarzamy brakujących decyzji z prognoz; przerwy rejestracji pozostają lukami.
Pierwsze pobranie dużej historii może potrwać kilkadziesiąt sekund.
Aktualizacja tego widoku wymaga restartu HA i odświeżenia otwartej karty.

## Zgodność i wymagania

| Element | Wymaganie |
|---|---|
| Falownik | Referencja: HIT-(5–20)L-G3, głównie HIT-10L-G3 i HIT-20L-G3. Zgłoszenia użytkowników 1.5.7 obejmują też HiOne, HAS i HAT; zobacz [zakres dla modelu](docs/COMPATIBILITY.md#polski) |
| ESP32 | Domyślnie `esp32dev`, ESP-IDF, rewizja układu co najmniej 3.1; inne warianty wymagają właściwej platformy, płytki i pinów |
| Interfejs RS485 | Konwerter UART/TTL–RS485 z logiką UART **3,3 V**; zalecany jest model z automatycznym przełączaniem kierunku |
| ESPHome | Wersja 2026.9.0 lub nowsza |
| Home Assistant | Wersja 2026.7 lub nowsza |
| HACS | Wersja 2.x |

Planowanie optymalizacji RCE, ładowania taryfowego i RCEm z uwzględnieniem prognozy wymaga
skonfigurowanej integracji
[BJReplay Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar).
Prognoza Solcast na Dzień 3 jest opcjonalna i często domyślnie wyłączona. Włącz
ją, jeśli jest dostępna; znane bieżące i starsze identyfikatory są wykrywane
automatycznie, a własną lub przemianowaną encję można wskazać helperem Dnia 3.
Brak albo nieświeżość Dnia 3 jest jawnie raportowana i nie wyłącza
konserwatywnego planowania na krótszym horyzoncie.
RCE wymaga publicznego API PSE, a Pstryk swojego publicznego źródła cen. Home Assistant Recorder musi
przechowywać historię stanów odpowiednich encji mocy i energii; w standardowej
instalacji jest włączony domyślnie. Dodatkowy licznik zużycia domu nie jest
wymagany.
Podczas instalacji sprawdź [retencję Recordera i miejsce na dysku](docs/RECORDER_AND_STORAGE.md#polski);
sama kompresja kopii zapasowych nie zapewnia zapisu historii EMS.

Domyślne parametry połączenia z falownikiem to `115200 8N1`, adres `1`.
Muszą odpowiadać ustawieniom jego portu. Nie są to parametry osobnego
połączenia z licznikiem energii ani uniwersalne ustawienia każdego modelu.

## Architektura

```text
Falownik Hoymiles ── RS485 / Modbus RTU ── ESP32 / ESPHome
                                                  │
                                         natywne API ESPHome
                                                  │
                                      Integracja Home Assistant
                                         │                   │
                                    Panel Aurora        Lokalny EMS

HACS instaluje i aktualizuje integrację Home Assistant.
ESPHome pobiera wersjonowane pakiety rejestrów bezpośrednio z GitHuba.
```

Integracja tworzy stabilne encje pośredniczące z polskimi i angielskimi nazwami
na podstawie natywnego urządzenia ESPHome. Przekazuje zarówno zmiany stanów,
jak i niezmienione świeże raporty, bez dodawania kolejnego cyklu odpytywania
Modbus.

## Bezpieczeństwo

> [!IMPORTANT]
> **EMS zarządza energią, a nie bezpieczeństwem elektrycznym ani sieciowym.**
> Nie zmienia certyfikowanych profili sieci, progów zabezpieczeń ani ustawienia
> asymetrii trójfazowej. Nie może wyłączyć zabezpieczeń falownika.

> [!WARNING]
> Projekt może zapisywać parametry pracy falownika dużej mocy. Przed włączeniem
> encji zapisywalnych lub automatycznego sterowania sprawdź dokładny model
> falownika, mapę rejestrów, okablowanie RS485, limity baterii i BMS-u oraz
> wymagania operatora sieci dystrybucyjnej. Korzystasz z oprogramowania na własne
> ryzyko.

Zapisy EMS są ograniczone do udokumentowanych ustawień eksploatacyjnych. Zmiana
trybu zapisuje cały blok rejestrów `4300–4306` funkcją Modbus 16 (`0x10`, Write
Multiple Registers). Zapisanie wyłącznie rejestru `4300` może pozostawić
falownik z niespójną konfiguracją EMS.

Projekt realizuje funkcje EMS, które mogą być pomocne podczas udokumentowanego
odbioru technicznego. **Nie jest** formalnym certyfikatem falownika, baterii ani
całej instalacji elektrycznej i nie potwierdza kwalifikacji do żadnego programu
dotacyjnego. Szczegóły opisuje
[dokument bezpieczeństwa i mapowania funkcji](docs/SAFETY_AND_COMPLIANCE.md).

## Instalacja

### 1. Zainstaluj integrację Home Assistant przez HACS

1. Użyj przycisku **Otwórz repozytorium w HACS** na początku tej strony.
2. Aby dodać repozytorium ręcznie, otwórz **HACS → menu z trzema kropkami →
   Niestandardowe repozytoria**, wpisz
   `https://github.com/Kaluzaburza/hoymiles-hit-g3-ems` i wybierz
   kategorię **Integracja**.
3. Zainstaluj **EMS for Hoymiles HIT-(5–20)L-G3** i uruchom Home Assistant ponownie.

HACS instaluje komponent Home Assistant. Firmware ESPHome konfiguruje się w
osobnym kroku i pobiera on własne wersjonowane pakiety z tego repozytorium.

### 2. Podłącz ESP32 i konwerter RS485

Najczęściej spotykane są dwa rodzaje konwerterów. Sprawdź dokumentację
konkretnego modułu zamiast polegać wyłącznie na nazwie produktu.

| Rodzaj konwertera | Typowe piny od strony UART | Konfiguracja ESPHome |
|---|---|---|
| **Konwerter z automatycznym przełączaniem kierunku — zalecany** | `VCC`, `GND`, `TXD`, `RXD` (czasem `DI`, `RO`) | Bez pinu sterującego kierunkiem |
| **Ręczne przełączanie kierunku, np. moduł oparty na MAX3485 lub właściwie dopasowany poziomami moduł MAX485** | `VCC`, `GND`, `DI`, `RO`, `DE`, `/RE` | Połącz `DE` z `/RE`, podłącz je do jednego GPIO ESP32 i ustaw `flow_control_pin` |

#### Konwerter automatyczny

Poniższy schemat ogólny pokazuje konwerter **bez izolacji galwanicznej**.
Odniesienie magistrali podłącz tylko zgodnie z instrukcją falownika.
Dla izolowanego Waveshare użyj ilustracji poniżej i zachowaj rozdzielenie
masy TTL GND od SGND.

```text
ESP32                        Konwerter RS485                   Falownik
GPIO17 (TX)  ------------->  RXD / DI
GPIO16 (RX)  <-------------  TXD / RO
3,3 V        ------------->  VCC  (tylko moduł zgodny z 3,3 V)
GND          --------------  GND (strona TTL)
                              A / D+ -------------------------- A+ / D+
                              B / D- -------------------------- B- / D-
```

Sygnał `TX` musi trafić do wejścia konwertera (`RXD` albo `DI`), a `RX` musi
odbierać sygnał z jego wyjścia (`TXD` albo `RO`). Oznaczenia różnią się między
modułami, dlatego sprawdź kierunek sygnałów w dokumentacji konwertera.

#### Ilustrowany przykład ESP32-S3

**ESP32-S3-DevKitC-1 v1.1 → Waveshare TTL TO RS485 (B) → Hoymiles
HIT-(5–20)L-G3, COM2 / 485_2.** Wcześniejsza, zaakceptowana ilustracja opisuje
sześć połączeń i pokazuje płytkę, konwerter oraz listwę COM2. Podpisy są po polsku.
Kliknij, aby powiększyć.

[![Połączenia ESP32-S3, izolowanego konwertera Waveshare i COM2 falownika Hoymiles](docs/images/esp32-s3-rs485-hoymiles-pl.png)](docs/images/esp32-s3-rs485-hoymiles-pl.png)

W tym przykładzie po stronie TTL: `GPIO17 → RXD`, `GPIO16 ← TXD`,
`3V3 → VCC` oraz `GND → GND`. Po stronie izolowanej: `A+ → 485_2+`
i `B− → 485_2−`; SGND pozostaje niepodłączony. Nie mostkuj SGND z GND ESP
ani nie zastępuj go przypadkowym zaciskiem falownika. Oznaczenia dotyczą wskazanego sprzętu;
fizyczne pozycje pinów potwierdź w jego instrukcji.
Dla S3 N16R8 użyj [hoymiles-inverter-s3.yaml](hoymiles-inverter-s3.yaml).
Zobacz [trzy warianty sprzętowe](docs/ESP32_VARIANTS.md).

Użytkownik potwierdził zgodność z rzeczywistym podłączeniem i zaliczenie testu
**20 września 2026 r.** Zobacz [zakres, odbiór i dokumentację producentów](docs/WIRING_ESP32_S3.md).

#### Konwerter z `DE` i `/RE`

Podłącz zasilanie, linie danych i magistralę RS485 tak jak wyżej, a następnie
połącz piny sterujące kierunkiem:

```text
MAX3485 DE ----+
               +------------ GPIO4 (przykład)
MAX3485 /RE ---+
```

Gotowy wariant to
[`hoymiles-inverter-flow-control.yaml`](hoymiles-inverter-flow-control.yaml).
Ma już prawidłowo dodany pin kierunku do istniejącego UART. Ustaw
`uart_flow_control_pin` na GPIO faktycznie podłączone do DE + /RE.

Jeśli dostosowujesz istniejący plik urządzenia, dodaj poniższy blok na
**głównym poziomie**, poza `packages:`. Jeśli `uart:` już istnieje, uzupełnij
ten blok zamiast tworzyć drugi:

```yaml
uart:
  id: modbus_uart
  flow_control_pin:
    number: GPIO4
    inverted: false
```

Pakiet definiuje `uart:` jako pojedynczy blok, dlatego zachowaj tę samą
strukturę: **bez myślnika listy i bez `!extend`**. Poprzedni przykład powodował
`Source for extension of ID 'modbus_uart' was not found`
([zgłoszenie #33](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/issues/33)).
Wybierz odpowiedni, wolny pin wyjściowy; GPIO4 jest tylko przykładem.
Nie twórz drugiego UART ani koncentratora Modbus. Konwerter automatyczny
korzysta ze standardowego [`hoymiles-inverter.yaml`](hoymiles-inverter.yaml),
bez tego bloku.

#### Kontrola przed włączeniem zasilania

1. Przed zmianą okablowania odizoluj wszystkie źródła energii podłączone do
   falownika hybrydowego: sieć AC, PV, baterię, EPS/zasilanie rezerwowe i GEN,
   jeżeli występuje. Postępuj zgodnie z procedurą wyłączenia producenta. Osoba z
   odpowiednimi kwalifikacjami musi przed rozpoczęciem pracy potwierdzić brak
   napięcia.
2. Sprawdź, czy konwerter używa logiki UART 3,3 V. Nie podłączaj wyjścia `RO`
   5 V ani żadnego innego sygnału 5 V do GPIO ESP32.
3. Połącz `A/D+` z `A+/D+`, `B/D−` z `B−/D−` oraz przewód odniesienia/GND, jeżeli
   wymaga go instrukcja falownika.
4. Nie podłączaj napięcia `3,3 V` z ESP32 ani `VCC` konwertera do żadnego
   zacisku komunikacyjnego falownika.
5. Użyj portu opisanego w dokumentacji konkretnego modelu jako zewnętrzny port
   RS485/Modbus. Nie wybieraj gniazda `Parallel` tylko dlatego, że ma taki sam
   wtyk.
6. Jeżeli po pozostałych kontrolach nadal nie ma komunikacji, powtórz pełną
   procedurę odizolowania i potwierdzenia braku napięcia, a następnie sprawdź,
   czy producent konwertera nie stosuje odwrotnego oznaczenia `A/B`.

### 3. Wgraj firmware do ESP32

1. Otwórz wybrany plik na GitHubie, kliknij **Raw** i skopiuj go do
   `/config/esphome/` przez **File editor**. Dla konwertera automatycznego
   wybierz plik standardowy, a dla DE + /RE — `hoymiles-inverter-flow-control.yaml`.
2. W `/config/esphome/secrets.yaml` dodaj cztery klucze z
   [`secrets.yaml.example`](secrets.yaml.example) i wpisz własne wartości.
   Zachowaj sekrety innych urządzeń. `api_key` musi być prawidłowym kluczem
   32-bajtowym w Base64, a nie tekstem przykładowym.
3. Sprawdź płytkę i piny. Domyślne ustawienia to `esp32dev`, ESP-IDF
   i minimalna rewizja układu 3.1; inny wariant ESP32 wymaga właściwej konfiguracji.
4. Otwórz **ESPHome Device Builder → ⋮ urządzenia → Validate**, a potem
   **Install**. Pierwsze wgrywanie wykonaj przez USB; kolejne mogą być bezprzewodowe.

[Szczegółowa instrukcja pięciu kroków](docs/QUICK_START.md#polski--pięć-kroków)
wyjaśnia tworzenie plików, znaczenie każdego sekretu i wybór połączenia USB.
Nie kopiuj katalogu `packages/` z repozytorium: YAML urządzenia sam pobiera
wersjonowane pakiety ESPHome.

Jeśli kompilacja się udała, ale odczyty Modbus są niedostępne, sprawdź typ
konwertera i piny kierunku, odniesienie/GND, polaryzację A/B, port falownika
oraz adres Modbus. Udane wgranie nie jest potwierdzeniem odczytu falownika.

### 4. Dodaj obie integracje

1. Dodaj wykryty ESP32 przez standardową integrację **ESPHome** w Home
   Assistant.
2. Otwórz **Ustawienia → Urządzenia i usługi → Dodaj integrację**.
3. Wybierz **EMS for Hoymiles HIT-(5–20)L-G3** i wskaż urządzenie ESPHome.

Integracja automatycznie instaluje i rejestruje:

- `/config/dashboard_hoymiles.yaml` dla starszych lub ręcznych instalacji
  panelu;
- `/config/packages/hoymiles_ems_scheduler.yaml`;
- wersjonowany moduł interfejsu Aurora.

Otwórz **File editor → ikona folderu → configuration.yaml** w głównym katalogu
konfiguracji HA. Jeśli pakiety nie są włączone, właściwa instrukcja pojawi się
też w **Ustawienia → System → Naprawy**. Gdy nie ma sekcji `homeassistant:`,
dopisz ten blok bez usuwania istniejącej zawartości:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Jeżeli ta sekcja już istnieje, dopisz pod jej dotychczasową zawartością tylko
wiersz `packages:` z dwiema spacjami wcięcia, bez tabulatorów. Nie powielaj
żadnego klucza ani nie zastępuj istniejącego wczytywania pakietów. Zapisz plik,
otwórz **Ustawienia → Narzędzia → YAML → Sprawdź konfigurację** (starsze wersje:
**Narzędzia deweloperskie → YAML**) i popraw błędy. Następnie wykonaj pełny
restart HA, aby wczytać nowy pakiet. [Szybki start](docs/QUICK_START.md#4-dodaj-źródłowe-urządzenie-i-włącz-pakiety-home-assistanta)
pokazuje przykład uzupełnienia istniejącej sekcji `homeassistant:`.

### 5. Dodaj panel Aurora i sprawdź instalację

1. Otwórz **Ustawienia → Panele → Dodaj panel**.
2. Wybierz panel społecznościowy **EMS for Hoymiles HIT-(5–20)L-G3**.
3. Otwórz integrację i sprawdź, czy **Stan instalacji = Gotowe**.
4. Przed włączeniem automatycznych zapisów sprawdź sekcję **Ustawienia → System
   → Naprawy**.

Strategia panelu ładuje polską albo angielską wersję dostarczoną z aktualnie
zainstalowaną integracją. Po aktualizacji HACS i restarcie Home Assistant panel
aktualizuje się bez ponownego wklejania konfiguracji YAML.

## Aktualizacja

Dla 1.5.8RC2 wykonaj [numerowane kroki aktualizacji](docs/releases/v1.5.8rc2.md#user-update-steps--kroki-po-aktualizacji).
Wstrzymaj automatyczne wykonanie, potwierdź fizyczny stan neutralny i wykonaj
kopię zmienionych plików. Zaktualizuj integrację i zarządzany pakiet, sprawdź
konfigurację HA oraz wykonaj restart lub restarty wskazane przez instalację
albo Naprawy. HACS aktualizuje HA; **nie wgrywa firmware'u do ESP32**.

Przejście z **1.5.7 wymaga zgodnego firmware'u ESPHome** z nowszym kontraktem
transportu, odczytu i lease. Użyj całego pliku urządzenia
z odwołaniem do pakietów `v1.5.8RC2`. Zobacz [migrację z 1.5.7](docs/UPGRADE_1_5_7.md). ESP z już zweryfikowanym protokołem 2 i takimi samymi pakietami
runtime nie wymaga flashowania wyłącznie z powodu oznaczenia RC2 lub opisów.
Zachowaj tożsamość urządzenia, płytkę, piny, sekrety i wariant RS485.
Przed przywróceniem wcześniejszych polityk sprawdź świeży fizyczny odczyt,
Self-Use i brak konfliktu właściciela. Plik DE + `/RE` jest opcjonalnym
wariantem sprzętowym, a nie drugą integracją.

Integracja zachowuje zmodyfikowane przez użytkownika kopie zarządzanych plików.
Wbudowane migracje paneli zapisanych w wewnętrznym trybie `storage` zmieniają
tylko wymagane typy kart, wiersze encji lub ścieżki zasobów. Przed zapisem
powstaje kopia `.pre-<wersja>.bak`.

Aby świadomie zastąpić lokalne kopie zasobów, najpierw wykonaj kopię każdego
zmodyfikowanego panelu i pliku pakietu EMS, a następnie wywołaj usługę:

```yaml
action: hoymiles_hit_modbus.install_assets
data:
  overwrite: true
```

Nie dodawaj ręcznie zasobu Lovelace
`/local/hoymiles-rce-chart-card.js`. Integracja automatycznie rejestruje własny
wersjonowany moduł interfejsu.

## Automatyzacje EMS

### Zasady wspólne dla wszystkich trybów

- Automatyczne sterowanie jest opcjonalne i pozostaje wyłączone do czasu
  skonfigurowania go przez użytkownika.
- Planery mogą równolegle przygotowywać propozycje. Nadzorca wybiera jednego
  uprawnionego wykonawcę; dwa moduły nie mogą wysyłać sprzecznych poleceń.
  Obserwacyjna analityka RCEm nie zapisuje ustawień falownika.
- Off-Grid jest fizycznym trybem należącym do użytkownika/falownika.
  Automatyczne sterowniki nie rozpoczynają ani nie aktualizują zapisów podczas
  jego pracy, a sprzątanie nie wymusza powrotu do Self-Use. Diagnostyka
  właściciela opisuje aktywną transakcję, a nie samo włączenie polityki.
- Cykl wyrównywania LiFePO4 ma tymczasowo wyższy priorytet niż pozostałe plany.
- Każde źródło zachowuje czas odczytu. Brak krytycznych danych, ich
  nieaktualność lub znacznik czasu z przyszłości blokują automatyczne zapisy
  i w razie potrzeby powodują kontrolowany powrót do Self-Use. Odczytany limit
  mocy równy zero oznacza brak dostępnej mocy, a nie brak ograniczenia.
- Wspólne, neutralne względem polityki mechanizmy oczyszczają historię LOAD i
  wyznaczają moc instalacji równoległej z 32-bitowego bilansu PV/Grid/LOAD. Każdy
  planer zachowuje jednak własny cel, model rezerwy i symulację.
- Przed wykonaniem polecenia sprawdzane są limity baterii, falownika, wspólnej
  mocy AC i eksportu, gotowość instalacji równoległej, okna blokady eksportu,
  naturalny eksport PV, zużycie domu oraz limit funkcji ograniczania eksportu
  (Generation Control Function, GCF), bez podwójnego liczenia tej samej mocy.
- Automatyka przejmuje sterowanie EMS przed zapisem. Polecenia mają ograniczoną
  częstotliwość, są podtrzymywane przez wymagany czas, a właściciel zostaje
  zwolniony dopiero wtedy, gdy nowszy, niezależny odczyt FC03 potwierdzi wszystkie
  wymagane rejestry oraz tryb neutralny. Optymistyczne echo stanu Home
  Assistanta/ESPHome nie jest uznawane za potwierdzenie sprzętu. Gdy fizyczny
  odczyt nie nadejdzie albo jest inny, przywracanie jest ponawiane i inny
  sterownik nie może przejąć EMS.
- Integracja nie steruje ustawieniem asymetrii trójfazowej.

### Sprzedaż dynamiczna — RCE i Pstryk

Przy wybranym **RCE** planer maksymalizuje oczekiwany przychód netto ze sprzedaży w dostępnym
horyzoncie cenowym. Łączy 15-minutowe dane cenowe PSE w 30-minutowe przedziały
planowania i analizuje je wspólnie. Modeluje energię dostępną obecnie oraz
energię PV dopiero wtedy,
gdy może fizycznie dotrzeć, naturalny eksport, straty konwersji, pojemność
baterii, limity BMS-u i falownika, wspólne budżety mocy AC i eksportu, GCF oraz
skonfigurowane okna blokady eksportu.

Rezerwa operacyjna jest zachowawczo zaokrąglana do pełnego kroku SOC falownika i
kontrolowana w każdym zaplanowanym oknie eksportu. Dane LOAD oraz informacje na
trzeci dzień pozostają widoczną diagnostyką, ale trzeci dzień nie tworzy celu
końcowego, który po cichu zmieniałby planer sprzedaży w optymalizator taryfy lub
kosztów domu. Planer stosuje ograniczoną heurystykę. Testy porównują małe
scenariusze z niezależnym obliczeniem referencyjnym, ale nie dowodzą, że każdy
rzeczywisty plan osiągnie najwyższy możliwy przychód.

Panel osobno pokazuje wyniki prognozowane i zmierzone oraz rozróżnia sterowany
eksport z baterii, naturalną nadwyżkę PV i eksport historyczny, którego źródła
nie udało się jednoznacznie sklasyfikować. Pokazuje przychód ze sprzedaży przed odjęciem
modelowego kosztu eksploatacji baterii oraz korzyść po jego odjęciu.
„Przed odjęciem kosztu” nie oznacza ceny Pstryk z VAT. Wyniki są szacunkami, a nie fakturą, rozliczeniem sprzedawcy ani
gwarancją zysku.

Przy wybranym **Pstryk** zakup i sprzedaż korzystają bezpośrednio z publicznej
godzinowej ceny netto. Wspólny plan respektuje niezależne zgody na zakup
i sprzedaż oraz rezerwę domu. Nie dolicza dystrybucji, VAT ani innych opłat
i nie zastępuje brakującej ceny netto ceną brutto. Ten uzgodniony model netto
nie odtwarza pełnej faktury. Sam eksport PV nie dowodzi sprzedaży z baterii.

### Automatyczne ładowanie taryfowe

Tanie ładowanie ma inny cel niż RCE: kupić tylko energię, której dom będzie
prawdopodobnie potrzebował, i przenieść ten zakup do najtańszych dostępnych
stref. Planer symuluje zapotrzebowanie domu, produkcję PV i poziom naładowania
baterii w krokach 30-minutowych. Model zimowy korzysta z zachowawczego profilu
wysokiego LOAD i twardej rezerwy domu w Self-Use. Aktualne dane Solcast na trzeci
dzień mogą wydłużyć horyzont symulacji do co najmniej 48 godzin. Jeżeli tego
końca horyzontu brakuje lub jest nieaktualny, system pokazuje krótszy znany
horyzont, a nieznany okres zabezpiecza zerową produkcją PV i zachowawczym
zużyciem domu.

Obliczenia uwzględniają limit ładowania BMS-u, straty konwersji, wspólną moc AC i
limit Grid Charge. W tym trybie falownik najpierw zasila dom, a dopiero pozostałą
mocą ładuje baterię. Planer odrzuca nieopłacalne mikrocykle, może uczyć się
rzeczywistej mocy trafiającej do baterii na podstawie potwierdzonych sesji i
rozpoczyna ładowanie odpowiednio wcześnie, aby zgromadzić wymaganą energię przed
droższą strefą taryfową. Nie optymalizuje przychodu z eksportu.

Margines taryfowy dotyczy energii potrzebnej w chronionym okresie:
**10 kWh + 10% = 11 kWh**, a nie dodatkowych dziesięciu punktów SOC.
Ten zapas można zużyć na potrzeby domu; fizyczna rezerwa i granice pojemności
obowiązują osobno. Nieosiągalny cel jest zgłaszany, a nie uznawany za wykonany.

Gotowe profile obejmują G11, G12, G12w, PGE G12e i G13 tam, gdzie oferują je PGE, TAURON,
ENEA, ENERGA i STOEN. Uwzględniają sezony, weekendy oraz polskie dni ustawowo
wolne od pracy. Wbudowane stawki brutto za kWh na 2026 rok obejmują przyjęte
w modelu składniki zmienne, ale nie opłaty stałe. Traktuj je jako punkt wyjścia
i porównaj z aktualną umową oraz fakturą. Dla innego produktu lub sprzedawcy użyj
profilu **Manual**.

[PGE G12e](docs/PGE_G12E.md) ma miesięczne tanie strefy dzienne oraz tanie
weekendy i święta. Zweryfikowane stawki krańcowe obejmują luty–grudzień 2026,
Cennik Podstawowy PGE Obrót i opublikowane godziny licznika LZO.
Dodanie tej opcji nie zmienia obecnie wybranej taryfy.

### Eksperymentalne zarządzanie napięciem RCEm 253 V+

RCEm ma trzeci, niezależny cel: zachować użyteczne miejsce w baterii wokół
powtarzalnych okresów wysokiego napięcia i ryzyka nadwyżki PV. Analizuje historię
napięć fazowych z poprzednich czterech dni, bieżące napięcia L1/L2/L3,
10-minutową średnią napięcia, prognozy Solcast w przedziałach czasowych, zużycie
domu z rozróżnieniem dni roboczych i weekendów oraz dostępną pojemność baterii.
Wariant dużej produkcji PV i małego zużycia wyznacza potrzebne miejsce.
Wariant małej produkcji i dużego zużycia sprawdza, czy w kolejnych okresach
wystarczy energii dla domu. RCEm korzysta z własnej rezerwy dla domu.

Poza trybem obserwacji regulator może zwiększać moc ładowania baterii wraz ze
wzrostem napięcia. Opcjonalne poranne rozładowanie tworzy tylko użyteczne miejsce
potrzebne przed późniejszym oknem ryzyka, bez naruszania własnej rezerwy
bezpieczeństwa domu. Opcjonalna regulacja eksportu nie przekracza najniższego z
fizycznie dostępnego budżetu eksportu, bieżącego ustawienia falownika i limitu
użytkownika.

RCEm domyślnie uruchamia się w **trybie obserwacji (shadow)**. Oblicza wtedy
plany i diagnostykę, ale nie wykonuje żadnych zapisów do falownika, dlatego może
zbierać obserwacje obok sterowania RCE lub taryfowego. Pozostaw tryb obserwacji
włączony, dopóki RCEm z prawem zapisu nie przejdzie osobnych prób i odbioru
na docelowej instalacji. RCEm nie wyłącza certyfikowanych zabezpieczeń,
nie zmienia progów ochronnych, nie włącza GCF i nie modyfikuje ustawienia
asymetrii trójfazowej. Pozostaje funkcją eksperymentalną.
Nie służy do obchodzenia obowiązujących wymagań
napięciowych kodeksu sieciowego ani operatora systemu dystrybucyjnego.

### Wyrównywanie baterii LiFePO4

Balansowanie to opcjonalny cykl serwisowy uruchamiany z częstotliwością wybraną
przez użytkownika. Plan dnia pokazuje datę kolejnego zaplanowanego cyklu,
zamiast samego przesunięcia względem wschodu słońca. W horyzoncie wykresu
balansowanie ma zielony odcinek SOC i zielony cień.

Najpierw bateria ładuje się z PV w Self-Use. Pierwszy prawidłowy odczyt SOC
wynoszący co najmniej **95%** uruchamia wolniejszą fazę do końca tego cyklu,
z celem nie większym niż około 0,4 kW sumarycznej mocy netto ładowania baterii.
Po zachodzie słońca potwierdzony tryb Grid Charge może dokończyć cykl;
jego cel poboru uwzględnia też zużycie domu. Podtrzymanie pełnego naładowania
rozpoczyna się dopiero po sprawdzeniu trybu i odczytów zwrotnych przy `99,9%`
SOC. Niższy SOC zeruje licznik podtrzymania: wymagany czas trzeba naliczyć
ponownie bez przerwy, ale bez powrotu do ładowania pełną mocą.

Cykl nie balansuje ogniw bezpośrednio i nie zastępuje BMS-u baterii.
Używaj go ze zgodną baterią LiFePO4 i ustawieniami potwierdzonymi podczas
odbioru. Zatrzymania ochronne pozostają aktywne. Przywracanie korzysta
z potwierdzonego zapisu nastaw należącego do bieżącego cyklu; fizyczny Off-Grid
ma pierwszeństwo.

Jeżeli pojawi się **RECOVERY_REQUIRED**, pozostaw balansowanie wyłączone
oraz zachowaj wewnętrzne helpery cyklu i dowody diagnostyczne. Nie zgaduj
poprzednich nastaw ani nie kasuj ręcznie właściciela sterowania. Zapisz powód,
identyfikator cyklu i fizyczne odczyty, pobierz pakiet diagnostyczny i ustal
właściwy stan na podstawie dokumentacji odbioru lub sterowania producenta.
Szczegółowe zasady odzyskiwania i powiadomień pozostają w
[raporcie testów automatyzacji](docs/AUTOMATION_TEST_REPORT.md).
Odbiór terenowy dokładnej wersji jest osobnym etapem względem testów offline.

### Powiadomienia EMS

Wspólny przełącznik `input_boolean.hoymiles_ems_push_notifications_enabled`
i odbiorca `input_text.hoymiles_ems_push_notify_target` sterują wszystkimi
wiadomościami przez `notify.send_message`. Sprzedaż dynamiczna, taryfa i RCEm wysyłają po jednej
wiadomości po fizycznie potwierdzonym początku logicznego zakresu oraz po jednej
po jego potwierdzonym zakończeniu. Zmiana slotu, rewizji planu, mocy, SOC lub
technicznego celu w tym samym ciągłym zakresie nie tworzy kolejnej wiadomości.
Restart w trakcie zakresu nie odtwarza sztucznego początku. RCE i Pstryk mają
wspólny tytuł **Sprzedaż dynamiczna**. Dziennik diagnostyczny zapisuje obsługę
wiadomości; przyjęcie jej przez HA nie potwierdza dostarczenia na telefon.

W dniu zaplanowanego balansowania jedna poranna zapowiedź przypada na 07:00
czasu Home Assistant; po restarcie może zostać odrobiona tylko do 09:00.
Balansowanie nie zgłasza faz pośrednich, a jedynie jedno uczciwe zakończenie.
Niezależne alarmy falownika i zaniku sieci pozostają aktywne; zgodna,
świadomie wybrana praca Off-Grid nie jest alarmem.

## Instalacje z falownikami połączonymi równolegle

Standardowy firmware odczytuje rejestry topologii `6048–6095` i rozpoznaje
pojedynczy falownik, Mastera oraz Slave'a. Nie trzeba ręcznie wpisywać liczby
falowników.

Do sterowania EMS konwerter ESP32, Master **i każdy Slave** muszą być
podłączone do tej samej zewnętrznej magistrali Modbus/RS485. W potwierdzonym
układzie testowym dwóch falowników HIT był to `RS485_2`; właściwy port sprawdź
w instrukcji dokładnego modelu.

```text
ESP32 → konwerter RS485 → zewnętrzny Modbus Mastera → zewnętrzny Modbus Slave'a 1 → …
```

Zachowaj układ przewodów, odniesienie/GND i terminację końców wymagane przez
producenta. Zewnętrzna magistrala Modbus jest oddzielna od wewnętrznej
magistrali Parallel/DTS; nie łącz tych magistral. Przewód tylko do Mastera
nie dostarczy zewnętrznego polecenia do Slave'ów przez sieć wewnętrzną.

Przy potwierdzonej topologii Mastera cały blok EMS `4300–4306` jest wysyłany
jednym rozgłoszeniem FC16 na adres `0`. Rozgłoszenie nie ma odpowiedzi Modbus.
Nowszy odczyt FC03 Mastera musi zawierać żądany blok, zanim HA zaakceptuje
zmianę konfiguracji. **Potwierdza to Mastera, a nie odbiór ani wykonanie
przez każdego Slave'a.** Pojedynczy falownik używa adresowanego FC16;
Slave lub nieprawidłowa topologia blokują zapis.

Osobna diagnostyka sumarycznej odpowiedzi fizycznej sprawdza nowsze, spójne
próbki mocy systemu po poleceniu. Rozróżnia m.in. `pending`, `confirmed`,
`not_confirmed` i `not_evaluable`. To dowód reakcji całego układu, a nie
potwierdzenie każdego Slave'a. Przed zaakceptowaniem startu RCE sprawdza też
cel mocy zapamiętany dla transakcji i wymaganą reakcję fizyczną. Pojedynczy
zarejestrowany impuls przy przełączeniu nie potwierdza ustalonej mocy.

Podczas odbioru potwierdź żądany tryb, moc każdego urządzenia i powrót do
Self-Use osobno na Masterze oraz każdym Slave'ie w aplikacji producenta.
`Gotowe`, sumy systemowe i zgodny FC03 Mastera nie dowodzą poprawnego
podłączenia odgałęzienia Slave'a. Historyczne próby zachowują własny zakres
wersji i dowodów w [raporcie testów automatyzacji](docs/AUTOMATION_TEST_REPORT.md).
Nie stanowią odbioru całego kandydata v1.5.8 ani niesprawdzonej instalacji.

Rejestry `258`, `259` i `306` są poza blokiem EMS. Działania RCEm wymagające
ich zapisu pozostają zablokowane w układach równoległych do potwierdzenia
osobnych zasad sterowania; analityka obserwacyjna jest dostępna. Nie omijaj
tej blokady. Adresy topologii opisują sieć wewnętrzną; ESP32 nie odpytuje ich
jako osobnych adresów Modbus przez zewnętrzny port Mastera. Polecenie rejestru
`3016`, **Parallel Networking Command**, jest domyślnie wyłączone i nigdy
nie jest używane przez automatyzację EMS.

## Zgodność firmware

Integracja tworzy stabilne encje pośredniczące dla całego katalogu. Jeżeli
zainstalowany firmware ESPHome nie zawiera jeszcze nowego rejestru, encja
pozostaje widoczna, ale niedostępna, i zgłasza
`firmware_update_required: true`. Ponowne skompilowanie i wgranie firmware'u
ESP32 z aktualnymi pakietami aktywuje tę samą encję bez zmiany jej
identyfikatora ani unikalnego ID.

Integracja i pakiety ESPHome są wersjonowane niezależnie. Zawsze korzystaj
z informacji o zgodności w opisie wydania i nie zakładaj, że oba numery wersji
muszą być identyczne.

## Diagnostyka i pomoc

Kliknij **System działa / Sprawdź system** na górnym pasku Aurory. Zamykane okno
**Stan systemu** pokazuje nadzorcę EMS, powód stanu, właściciela, przywracanie
nastaw, bieżące błędy falownika/EMS i domyślnie **24 godziny** zapisanej historii
błędów. Historia zależy od Rejestratora HA i dostępnych encji źródłowych.
Nieudane pobranie jest pokazane jako błąd, a nie pusty dziennik bez usterek.

**Wyczyść alarmy falownika** wysyła istniejące polecenie kasowania alarmów.
Nie usuwa przyczyny fizycznej ani historii HA. **Uruchom ponownie EMS**
przeładowuje integrację, zachowuje ustawienia planów i czeka na nowy, zdrowy
stan. Nie jest restartem Home Assistanta ani falownika. Każda akcja wymaga
drugiego kliknięcia w ciągu sześciu sekund. Nieudane przywracanie, np.
`rollback_failed`, pozostaje widoczne do usunięcia jego rzeczywistej przyczyny.

Przed zgłoszeniem problemu zapisz:

- dokładny model i wersję firmware falownika;
- wersję ESPHome i Home Assistant;
- lokalną datę i godzinę zdarzenia;
- oczekiwane i zaobserwowane zachowanie;
- istotne logi po usunięciu haseł i danych osobowych.

Pobierz raport ZIP z automatycznie zamaskowanymi danymi z widoku
**Ustawienia EMS → Serwis** albo użyj natywnej akcji Home Assistant
**Pobierz diagnostykę**.
Przy problemach z
ESPHome, Modbus, uruchamianiem lub pętlą automatyzacji możesz także skorzystać z
rozszerzonego skryptu terminalowego. Zakres raportów i sposób anonimizacji
opisuje dokument [Diagnostyka](docs/DIAGNOSTICS.md).

RC2 eksportuje zdekodowaną kompaktową historię wykonania, zamrożony dowód STOP,
rewizje planów i wejść, jakość prognoz oraz do 32 ostatnich wpisów dziennika
powiadomień, jeśli są dostępne. Analizator zachowuje te atrybuty do porównań.
Brak danych pozostaje niewiadomą. ZIP **nie potwierdza** każdego odnowienia
lease co 20 s, indywidualnego FC03 Slave ani dostarczenia push na telefon;
granice są zapisane w kontrakcie dowodów i [instrukcji diagnostyki](docs/DIAGNOSTICS.md).

Każda instalacja Home Assistant otrzymuje też jeden losowy i trwały UUID v4,
używany wyłącznie do łączenia kolejnych paczek diagnostycznych z tej samej
instalacji. Identyfikator nie pochodzi z falownika, sieci, konta, config entry
ani innych danych użytkownika. Działający lokalnie, bez wysyłania danych
[analizator diagnostyczny](docs/DIAGNOSTICS_ANALYZER.md) może jednocześnie
przetworzyć do 100 paczek ZIP i porównać zachowanie RCE, RCEm oraz ładowania
taryfowego.

Przed dołączeniem archiwum do publicznego zgłoszenia przejrzyj jego zawartość.
Automatyczne filtrowanie nie zastępuje ręcznej kontroli haseł i danych osobowych.

Utwórz [zgłoszenie na GitHubie](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/issues)
albo wyślij raport wraz z opisem problemu na adres
[info@kaluzaaa.com](mailto:info@kaluzaaa.com).

Jeżeli okno logów ESPHome wielokrotnie pokazuje `SocketClosedAPIError`, ale
encje nadal się aktualizują, zamknij zduplikowane strumienie logów, odczekaj
około 15 sekund i uruchom ponownie wyłącznie dodatek ESPHome Device Builder.
Przed ponownym wgraniem firmware sprawdź
[instrukcję diagnostyczną](docs/DIAGNOSTICS.md).

## Dokumentacja

| Dokument | Zastosowanie |
|---|---|
| [Szybki start](docs/QUICK_START.md) | Krótka ścieżka instalacji dla nowych użytkowników |
| [Opis RC2](docs/releases/v1.5.8rc2.md) | Nowości, aktualizacja i granice odbioru |
| [Zgodność](docs/COMPATIBILITY.md) | Modele referencyjne, zgłoszenia użytkowników i zakres odbioru |
| [Schemat połączeń](docs/WIRING_ESP32_S3.md) | ESP32-S3, izolowany konwerter i HIT-G3 COM2 |
| [Diagnostyka](docs/DIAGNOSTICS.md) | Tworzenie raportów, anonimizacja i rozwiązywanie problemów |
| [Analizator diagnostyczny](docs/DIAGNOSTICS_ANALYZER.md) | Lokalne porównywanie maksymalnie 100 paczek diagnostycznych ZIP |
| [Bezpieczeństwo i mapowanie funkcji](docs/SAFETY_AND_COMPLIANCE.md) | Zaimplementowane zabezpieczenia, granice projektu i materiały do audytu |
| [Raport testów automatyzacji](docs/AUTOMATION_TEST_REPORT.md) | Zakres symulacji, statyczne kontrole sterowania i ograniczenia testów terenowych |
| [Historia zmian](CHANGELOG.md) | Zmiany w wydaniach i wymagane kroki aktualizacji |
| [Procedura wydania](RELEASING.md) | Lista kontrolna publikacji w GitHubie i HACS dla opiekuna projektu |

## Rozwój projektu

```text
custom_components/hoymiles_hit_modbus/  Integracja Home Assistant
packages/                               Pakiety rejestrów Modbus dla ESPHome
examples/esphome/                       Przykładowa konfiguracja ESPHome
home_assistant/                         Źródła karty panelu i pakietu EMS
docs/                                   Dokumentacja użytkownika, bezpieczeństwa i testów
tools/                                  Generatory zasobów i testy wydania
```

Aby przebudować katalog encji z tłumaczeniami i dołączone zasoby, uruchom:

```bash
python tools/build_hacs_assets.py
```

GitHub Actions uruchamia walidację HACS, Hassfest, kontrolę interfejsu i testy
projektu. Zmiany muszą być zgodne z [CONTRIBUTING.md](CONTRIBUTING.md) i zawierać
wymagany wpis `Signed-off-by`.

## Wesprzyj projekt

Integracja i funkcje EMS są bezpłatnym oprogramowaniem open source. Jeżeli
projekt jest przydatny, możesz wesprzeć dalszy rozwój, dokumentację i testy:

[☕ Postaw kawę autorowi](https://buycoffee.to/kaluzaaa)

## Licencja

Projekt jest dostępny na [licencji MIT](LICENSE). Dozwolone jest użycie prywatne
i komercyjne, modyfikowanie oraz rozpowszechnianie z zachowaniem informacji o
prawach autorskich i treści zezwolenia. Oprogramowanie jest udostępniane bez
gwarancji.
