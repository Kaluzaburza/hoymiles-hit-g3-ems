# EMS for Hoymiles HIT-(5–20)L-G3

[English](README.md) · [Polski](README.pl.md)

Nieoficjalny, lokalny EMS dla hybrydowych falowników Hoymiles HIT-G3 — Home
Assistant, ESPHome, Modbus, RCE, optymalizacja taryfowa i RCEm.

[![Otwórz repozytorium w HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Kaluzaburza&repository=hoymiles-hit-g3-ems&category=integration)
[![Najnowsze wydanie](https://img.shields.io/github/v/release/Kaluzaburza/hoymiles-hit-g3-ems?label=release)](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/releases/latest)
[![Walidacja](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/actions/workflows/validate.yml/badge.svg)](https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/actions/workflows/validate.yml)
[![Licencja: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Projekt łączy ESP32 z falownikiem przez Modbus RTU oraz dodaje integrację Home
Assistant z polskimi nazwami encji, panel Aurora i opcjonalne automatyzacje EMS.
Cała logika sterowania działa lokalnie. Funkcje zależne od prognozy korzystają
z Solcast, a optymalizacja RCE pobiera ceny z publicznego API PSE.

**Nowa instalacja:** skorzystaj z
[pięcioetapowej instrukcji szybkiego startu](docs/QUICK_START.md). Poniżej znajdziesz
również przegląd instalacji i dostępnych funkcji.

## Przegląd

![Panel Aurora: przepływ energii, RCE, ładowanie taryfowe i RCEm](docs/images/dashboard-overview.png)

Podstawowe widoki panelu pokazują bieżącą pracę instalacji, zaplanowane działania
i ich wyniki. Sekcje eksperckie zawierają dane wejściowe modeli, obliczone
rezerwy, ograniczenia fizyczne, ocenę jakości danych oraz przyczynę wykonania
albo zablokowania działania. Interfejs jest dostępny po polsku i angielsku oraz
dopasowuje się do ekranu komputera i telefonu.

### Dostępne funkcje

| Obszar | Możliwości |
|---|---|
| Monitorowanie lokalne | PV1–PV4, zużycie domu, sieć, bateria, BMS, GEN, stan falownika, alarmy, temperatury, liczniki energii i wartości sumaryczne instalacji równoległej |
| Sterowanie falownikiem | Self-Use, Off-Grid, Grid Charge, Grid Discharge, limity ładowania i rozładowania, docelowy SOC, harmonogramy oraz jawnie włączana regulacja eksportu |
| Optymalizacja RCE | Tworzy ograniczony plan całego horyzontu, którego nadrzędnym celem jest przychód ze sprzedaży w dozwolonych blokach 30-minutowych, przy zachowaniu rezerwy operacyjnej |
| Ładowanie taryfowe | Przenosi niezbędne ładowanie z sieci do tańszych stref, zachowując twardą rezerwę domu i uwzględniając zimowe ryzyko zużycia |
| Eksperymentalny RCEm 253 V+ | Wykrywa powtarzalne okresy wysokiego napięcia i modeluje miejsce w baterii; w testach publicznych domyślnie działa tylko obserwacyjnie |
| Obsługa baterii | Planuje cykle wyrównywania LiFePO4, wykorzystuje najpierw PV, w razie potrzeby kończy ładowanie z sieci i utrzymuje pełny SOC przez ustawiony czas |
| Diagnostyka | Stan instalacji, konflikty sterowania, aktualność danych, potwierdzanie poleceń odczytem zwrotnym, raporty ZIP z automatycznie zamaskowanymi danymi i szczegółowe dane optymalizatorów |

W danej chwili ustawienia EMS może zapisywać tylko jeden moduł automatyzacji.
Blokady wzajemne zapobiegają jednoczesnemu wysyłaniu sprzecznych poleceń przez
RCE, ładowanie taryfowe, aktywne sterowanie RCEm, wyrównywanie baterii i
harmonogramy ręczne. Analityka RCEm może działać obok innego sterownika, ponieważ
w trybie obserwacji nie wykonuje żadnych zapisów.

## Zgodność i wymagania

| Element | Wymaganie |
|---|---|
| Falownik | Rodzina Hoymiles HIT xxL G3; rozwój i testy terenowe skupiały się głównie na instalacjach HIT-10L-G3 i HIT-20L-G3 |
| ESP32 | Płytka ESP32 obsługiwana przez ESPHome; konfiguracja publiczna domyślnie używa `esp32dev` |
| Interfejs RS485 | Konwerter UART/TTL–RS485 z logiką UART **3,3 V**; zalecany jest model z automatycznym przełączaniem kierunku |
| ESPHome | Wersja 2026.7 lub nowsza |
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
Ceny RCE wymagają dostępu do publicznego API PSE. Home Assistant Recorder musi
przechowywać historię stanów odpowiednich encji mocy i energii; w standardowej
instalacji jest włączony domyślnie. Dodatkowy licznik zużycia domu nie jest
wymagany.

Domyślne parametry Modbus to `115200 8N1`, adres urządzenia `1`.

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

```text
ESP32                        Konwerter RS485                   Falownik
GPIO17 (TX)  ------------->  RXD / DI
GPIO16 (RX)  <-------------  TXD / RO
3,3 V        ------------->  VCC  (tylko moduł zgodny z 3,3 V)
GND          --------------  GND  --------------------------- GND / odniesienie
                              A / D+ -------------------------- A+ / D+
                              B / D- -------------------------- B- / D-
```

Sygnał `TX` musi trafić do wejścia konwertera (`RXD` albo `DI`), a `RX` musi
odbierać sygnał z jego wyjścia (`TXD` albo `RO`). Oznaczenia różnią się między
modułami, dlatego sprawdź kierunek sygnałów w dokumentacji konwertera.

#### Konwerter z pinami `DE` i `/RE`

Podłącz zasilanie, linie danych i magistralę RS485 tak jak wyżej, a następnie
połącz piny sterujące kierunkiem:

```text
MAX3485 DE ----+
               +------------ GPIO4 (przykład)
MAX3485 /RE ---+
```

Dodaj poniższy blok poza istniejącą sekcją `packages:` w pliku
`hoymiles-inverter.yaml`. Rozszerza on konfigurację UART utworzoną przez pakiet:

```yaml
uart:
  - id: !extend modbus_uart
    flow_control_pin:
      number: GPIO4
      inverted: false
```

Jeżeli GPIO4 jest niedostępny, wybierz inny pin GPIO obsługujący wyjście. Nie
twórz drugiego koncentratora Modbus.

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

1. Utwórz urządzenie ESPHome albo skopiuj publiczny plik wejściowy konfiguracji
   [`hoymiles-inverter.yaml`](hoymiles-inverter.yaml) do katalogu konfiguracji
   ESPHome.
2. Skopiuj klucze z [`secrets.yaml.example`](secrets.yaml.example) do lokalnego
   `secrets.yaml` i zastąp wszystkie wartości przykładowe.
3. Sprawdź ustawienia płytki oraz `uart_tx_pin` i `uart_rx_pin`.
4. Dodaj blok `!extend modbus_uart` tylko wtedy, gdy konwerter wymaga ręcznego
   sterowania liniami `DE`/`/RE`.
5. Sprawdź poprawność konfiguracji, skompiluj ją i zainstaluj firmware.

Nie kopiuj katalogu `packages` z repozytorium. Publiczny plik wejściowy pobiera
bezpośrednio z GitHuba zgodne, wersjonowane pakiety ESPHome.

Jeżeli kompilacja kończy się poprawnie, ale wszystkie encje Modbus są
niedostępne, sprawdź kolejno: rodzaj konwertera i piny sterujące kierunkiem,
wspólne odniesienie/GND, polaryzację `A/B`, port falownika oraz adres Modbus.

### 4. Dodaj obie integracje

1. Dodaj wykryty ESP32 przez standardową integrację **ESPHome** w Home
   Assistant.
2. Otwórz **Ustawienia → Urządzenia oraz usługi → Dodaj integrację**.
3. Wybierz **EMS for Hoymiles HIT-(5–20)L-G3** i wskaż urządzenie ESPHome.

Integracja automatycznie instaluje i rejestruje:

- `/config/dashboard_hoymiles.yaml` dla starszych lub ręcznych instalacji
  panelu;
- `/config/packages/hoymiles_ems_scheduler.yaml`;
- wersjonowany moduł interfejsu Aurora.

Jeżeli pakiety Home Assistant nie są włączone, w sekcji **Ustawienia → System →
Naprawy** pojawi się właściwa instrukcja. Gdy plik `configuration.yaml` nie
zawiera jeszcze sekcji `homeassistant:`, dodaj:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Jeżeli ta sekcja już istnieje, dopisz pod jej dotychczasową zawartością tylko
odpowiednio wcięty wiersz `packages:` — nie dodawaj drugiego klucza
`homeassistant:`. Sprawdź poprawność konfiguracji i uruchom Home Assistant
ponownie.

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

1. Przeczytaj sekcję **Kroki po aktualizacji** w opisie wydania HACS.
2. Zainstaluj aktualizację przez HACS.
3. Wykonaj jeden restart Home Assistant.
4. Sprawdź **Stan instalacji** i sekcję **Naprawy**. Aktualizacja ze starszej
   wersji może wymagać jeszcze jednego restartu po skopiowaniu zarządzanego
   pakietu EMS.
5. Ponownie kompiluj firmware ESP32 wyłącznie wtedy, gdy opis wydania wyraźnie
   tego wymaga.

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
- RCE, ładowanie taryfowe i aktywne sterowanie RCEm wzajemnie się wykluczają.
  Analityka RCEm może pozostać włączona w trybie obserwacji, bo nie wykonuje
  żadnych zapisów.
- Off-Grid jest fizycznym trybem należącym do użytkownika/falownika.
  Automatyczne sterowniki nie rozpoczynają ani nie aktualizują zapisów podczas
  jego pracy, a sprzątanie nie wymusza powrotu do Self-Use. Diagnostyka
  właściciela opisuje aktywną transakcję, a nie samo włączenie polityki.
- Cykl wyrównywania LiFePO4 ma tymczasowo wyższy priorytet niż pozostałe plany.
- Wiek danych każdego źródła jest liczony ze znakiem. Brak krytycznych danych,
  ich nieaktualność lub znacznik czasu z przyszłości blokują automatyczne zapisy
  i w razie potrzeby powodują kontrolowany powrót do Self-Use. Zgłoszona zerowa
  możliwość jest rzeczywistym limitem równym zero, a nie nieograniczoną mocą.
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

### Optymalizacja cen RCE

RCE ma jeden cel: maksymalizować oczekiwany przychód netto ze sprzedaży w
dostępnym horyzoncie 30-minutowych cen PSE. Ograniczony planer wspólnego
horyzontu analizuje bloki łącznie, zamiast podejmować osobną zachłanną decyzję
dla każdego z nich. Modeluje energię dostępną obecnie, energię PV dopiero wtedy,
gdy może fizycznie dotrzeć, naturalny eksport, straty konwersji, pojemność
baterii, limity BMS-u i falownika, wspólne budżety mocy AC i eksportu, GCF oraz
skonfigurowane okna blokady eksportu.

Rezerwa operacyjna jest zachowawczo zaokrąglana do pełnego kroku SOC falownika i
kontrolowana w każdym zaplanowanym oknie eksportu. Dane LOAD oraz informacje na
trzeci dzień pozostają widoczną diagnostyką, ale trzeci dzień nie tworzy celu
końcowego, który po cichu zmieniałby planer sprzedaży w optymalizator taryfy lub
kosztów domu. Implementacja jest ograniczoną heurystyką active-set, a nie
dokładnym solverem. Niezależny test referencyjny (oracle) sprawdza małe,
skonstruowane horyzonty i w objętych przypadkach wykazał jedynie małe
zaobserwowane różnice. To potwierdzenie w testach regresyjnych, a nie formalny
dowód optimum globalnego dla pełnego problemu z mieszanymi ograniczeniami.

Panel osobno pokazuje wyniki prognozowane i zmierzone oraz rozróżnia sterowany
eksport z baterii, naturalną nadwyżkę PV i eksport historyczny, którego źródła
nie udało się jednoznacznie sklasyfikować. Pokazuje przychód brutto ze sprzedaży
i szacowaną korzyść netto po uwzględnieniu modelowego kosztu eksploatacji
baterii. Wyniki są szacunkami, a nie fakturą, rozliczeniem sprzedawcy ani
gwarancją zysku.

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

Gotowe profile obejmują G11, G12, G12w i G13 tam, gdzie oferują je PGE, TAURON,
ENEA, ENERGA i STOEN. Uwzględniają sezony, weekendy oraz polskie dni ustawowo
wolne od pracy. Wbudowane krańcowe ceny brutto za kWh na 2026 rok obejmują
przyjęte w modelu składniki zmienne, ale nie opłaty stałe. Traktuj je jako punkt wyjścia i
porównaj z aktualną umową oraz fakturą. Dla innego produktu lub sprzedawcy użyj
profilu **Manual**.

### Eksperymentalne zarządzanie napięciem RCEm 253 V+

RCEm ma trzeci, niezależny cel: zachować użyteczne miejsce w baterii wokół
powtarzalnych okresów wysokiego napięcia i ryzyka nadwyżki PV. Analizuje historię
napięć fazowych z poprzednich czterech dni, bieżące napięcia L1/L2/L3,
10-minutową średnią napięcia, prognozy Solcast w przedziałach czasowych, zużycie
domu z rozróżnieniem dni roboczych i weekendów oraz dostępną pojemność baterii.
Scenariusz wysokiego PV i niskiego LOAD wyznacza potrzebne miejsce, a stres
niskiego PV i wysokiego LOAD wraz z chronologicznym bilansem energii chronią
energię domu. RCEm nie dziedziczy rezerwy operacyjnej RCE.

Poza trybem obserwacji regulator może zwiększać moc ładowania baterii wraz ze
wzrostem napięcia. Opcjonalne poranne rozładowanie tworzy tylko użyteczne miejsce
potrzebne przed późniejszym oknem ryzyka, bez naruszania własnej rezerwy
bezpieczeństwa domu. Opcjonalna regulacja eksportu nie przekracza najniższego z
fizycznie dostępnego budżetu eksportu, bieżącego ustawienia falownika i limitu
użytkownika.

RCEm domyślnie uruchamia się w **trybie obserwacji (shadow)**. Oblicza wtedy
plany i diagnostykę, ale nie wykonuje żadnych zapisów do falownika, dlatego może
zbierać dane testowe obok sterowania RCE lub taryfowego. Pozostaw tryb
obserwacji włączony, dopóki RCEm z prawem zapisu nie przejdzie osobnego odbioru
na docelowej instalacji. RCEm nie wyłącza certyfikowanych
zabezpieczeń, nie zmienia progów ochronnych, nie włącza GCF i nie modyfikuje
ustawienia asymetrii trójfazowej. Pozostaje funkcją eksperymentalną i przed
użyciem zapisów wymaga osobnej walidacji terenowej oraz uruchomienia próbnego na
docelowej instalacji. Nie służy do obchodzenia obowiązujących wymagań
napięciowych kodeksu sieciowego ani operatora systemu dystrybucyjnego.

### Wyrównywanie baterii LiFePO4

Opcjonalny cykl serwisowy jest uruchamiany z częstotliwością wybraną przez
użytkownika. Po wschodzie słońca normalne, bezpieczne dla BMS ładowanie w
Self-Use wykorzystuje najpierw PV do 95% SOC. Pierwszy prawidłowy odczyt co
najmniej 95% zatrzaskuje wolny etap do końca cyklu, z celem nie większym niż
około 0,4 kW sumarycznej mocy netto ładowania baterii. Self-Use używa
bezpośredniego limitu baterii, a po zachodzie zweryfikowany Grid Charge dodaje
zużycie domu dokładnie raz do tego samego celu 0,4 kW. Czas podtrzymania zaczyna
się dopiero po potwierdzeniu wymaganych odczytów trybu i rejestrów przy `99,9%`
SOC. Niższy odczyt anuluje licznik i wymaga nowego nieprzerwanego podtrzymania,
bez powrotu do ładowania pełną mocą. Krótkie luki wyłącznie w świeżości danych
w Self-Use wstrzymują zapisy najwyżej przez 60 sekund; błędy bezpieczeństwa są
przechwytywane jako trwałe żądanie hard-stop.
Dowód hard-stop jest zamrażany w chwili przyjęcia wyzwalacza, więc krótkie
przejście błąd/powrót nie jest później reinterpretowane z bieżącego stanu.
Niezależne, ograniczone kolejki FIFO dla każdej klasy priorytetu hard-stop
sprawiają, że seria niższego priorytetu nie zajmuje przyjęcia późniejszej
przyczyny wyższej; trwałe scalanie zachowuje pierwszy dokładny powód przy
równym priorytecie.
Już uruchomionego wywołania zweryfikowanego helpera nie można anulować; trwałe
żądanie blokuje wszystkie kolejne zapisy inne
niż odtwarzanie, które zaczyna się na pierwszej bezpiecznej granicy
serializowanego workera. Przed pierwszym zapisem fizycznym obie generacje FC03
muszą być dokładnie równe — nie tylko większe — generacjom zaufanej migawki.
Bezpośrednio przed `HOLD_ARMING` SOC jest odczytywany ponownie i nadal musi być
liczbowy, skończony, świeży oraz wynosić co najmniej 99,9%; granica zapisu
terminu powtarza tę kontrolę i czyści nieważną próbę uzbrojenia bez uruchamiania
timera. Końcowy commit fazy stabilnej ponownie odczytuje bieżący stan słońca,
potwierdzony tryb EMS, cykl, generację właściciela i zatrzask hard-stop;
rozbieżność dopuszcza najwyżej jedną zweryfikowaną korektę trybu, a druga zmiana
słońca kończy się fail-closed. Trwały rekord pozostaje surowym `APPLYING` z
warunkowym tokenem fazy/trybu, a parser kanoniczny pokazuje `OPERATIONAL`,
`SLOW` lub fazę podtrzymania tylko tak długo, jak bieżące słońce, tryb,
właściciel, cykl, timing i brak abortu nadal są zgodne. Zwykłe etapy wewnętrzne
nie generują ogólnych powiadomień push. Zaufana migawka
powiązana z bieżącym cyklem jest odtwarzana
wyłącznie z pasującymi fizycznymi ACK; fizyczny Off-Grid ma pierwszeństwo.
Aktywny stary albo uszkodzony cykl bez tej proweniencji przechodzi do
`RECOVERY_REQUIRED`: nie wykonuje zgadywanego odtworzenia i nie zwalnia
automatycznie istniejącego właściciela balansowania. Fizyczny fakt
`operational_started` jest utrwalany niezależnie od wolnego miejsca w outboxie,
więc późniejsze przerwanie pozostaje ABORTED, nawet jeśli STARTED musiało czekać.
Zdarzenia terminalne telefonu trafiają do trwałej kolejki poza fizycznym
zamknięciem. Każda próba dostawcy otrzymuje trwałą dzierżawę na 15 sekund;
wygaśnięcie umożliwia ograniczone ponowienie, restart odzyskuje porzuconą
dzierżawę, a spóźnione zakończenie nie może zmienić nowszej próby. Ponowienia
używają tego samego stabilnego tagu, który może zastąpić/zdeduplikować widoczne
powiadomienie, jeśli odbiorca obsługuje tagi; dostarczenie przez dowolnego
zewnętrznego dostawcę nie jest matematycznie exactly-once. Zmiany zweryfikowano
offline; przed wydaniem nadal wymagają akceptacji terenowej dokładnej wersji.

Jeżeli widoczny jest stan `RECOVERY_REQUIRED`, pozostaw balansowanie wyłączone i
nie czyść jego helperów aktywności/lifecycle. Zapisz powód transakcji,
identyfikator cyklu, bieżący fizyczny tryb oraz odczyty 4303/4304, a następnie
wyeksportuj pakiet wsparcia. Wykwalifikowany operator musi ustalić właściwe
nastawy z dokumentacji uruchomienia albo sterowania producenta — nie z
niezaufanego starego rekordu — i potwierdzić świeże odczyty fizyczne. Dopiero po
zatrzymaniu timerów i zweryfikowanych skryptów zapisu oraz przeglądzie tych
dowodów opiekun może zwolnić zachowanego właściciela/zresetować wewnętrzny
lifecycle i rozpocząć nowy cykl. Do tego czasu fail-closed zachowuje własność;
ta wersja celowo nie migruje automatycznie nieweryfikowalnego aktywnego cyklu.

## Instalacje z falownikami połączonymi równolegle

Standardowa konfiguracja ESPHome odczytuje rejestry topologii `6048–6095` i
automatycznie rozpoznaje pojedynczy falownik, Master oraz Slave. Użytkownik nie
musi ręcznie podawać liczby falowników.

W układzie równoległym konwerter ESP32, Master i **każdy Slave** muszą być
podłączone do jednej wspólnej fizycznej magistrali zewnętrznego Modbus/RS485.
W zweryfikowanej instalacji 2×HIT jest to magistrala `RS485_2`. Doprowadź A, B
oraz wymagane przez producenta odniesienie/GND do każdego falownika;
podłączenie ESP32 wyłącznie do Mastera nie wystarcza.

```text
ESP32 -> izolowany konwerter RS485 -> zewnętrzny Modbus Mastera -> zewnętrzny Modbus Slave 1 -> ... -> Slave N
```

Prowadź przewód liniowo/magistralowo, nie w gwiazdę, a terminację stosuj tylko
na fizycznych końcach zgodnie z instrukcją falownika i konwertera. Zewnętrzny
Modbus ESP32 jest inną magistralą niż dedykowana wewnętrzna komunikacja
Parallel/DTS falowników — nie mostkuj tych dwóch magistral.

Firmware v1.5.6 zachowuje polecenie systemowe zweryfikowane wcześniej w
testowej konfiguracji 2×HIT na wspólnej magistrali: każda zmiana rejestrów EMS
`4300–4306` jest wysyłana jako jeden broadcast FC16 na adres Modbus `0`. RCE,
tanie ładowanie, harmonogramy ręczne i balansowanie baterii mogą dzięki temu sterować wykrytym
układem z Masterem **tylko wtedy, gdy każdy falownik jest fizycznie obecny na
tej samej zewnętrznej magistrali RS485**. Adres `0` jest rozgłoszeniem na
przewodzie, którym wysłano ramkę; Master nie przekazuje zewnętrznego polecenia
Modbus do Slave'ów przez wewnętrzną sieć równoległą. Broadcast nie odpowiada;
Home Assistant uznaje polecenie dopiero wtedy, gdy późniejszy fizyczny FC03 z
Mastera zawiera dokładnie żądany blok. Potwierdza to blok Mastera, a nie odbiór
lub wykonanie polecenia przez każdego Slave'a.

Dla Grid Discharge na wykrytym Masterze równoległym v1.5.6 dodaje osobną
diagnostykę **sumarycznej odpowiedzi fizycznej** po poleceniu. Po potwierdzeniu
konfiguracji przez FC03 Mastera stosuje 20 sekund czasu przejściowego, a potem
ocenia pięć nowszych kompletnych generacji mocy systemu (maksymalnie po 20
sekund każda) i wymaga trzech kolejnych stabilnych generacji. Stan diagnostyki
przechodzi z `pending` do `confirmed`, `not_confirmed` albo `not_evaluable`.
RCE porównuje odpowiedź z celem zamrożonym przed poleceniem, wymaga też eksportu
co najmniej 0,25 kW, a przy braku potwierdzenia wykonuje istniejący neutralny
rollback fail-closed. Tryb ręczny/recovery i przygotowanie RCEm bez
autorytatywnego celu sumarycznej mocy oceniają świeży stabilny kierunek
rozładowania baterii bez odrzucenia za eksport lub amplitudę. Powrót do Self-Use
nigdy nie czeka na sumaryczny dowód rozładowania.

Dwie nowe encje diagnostyczne to
`sensor.hoymiles_hit_parallel_aggregate_power_readback_generation` oraz
`sensor.hoymiles_parallel_aggregate_physical_response`.

Ten sygnał potwierdza wyłącznie odpowiedź fizyczną całego systemu. Nie jest
odczytem FC03 ze Slave'a i nie wolno nazywać go potwierdzeniem każdego Slave'a.
Zarejestrowany pik przejściowy jest zapisywany diagnostycznie, lecz nie należy
do stabilnego okna potwierdzenia i sam w sobie nie jest błędem wykonania.

Podczas odbioru sprawdź Grid Discharge i powrót do Self-Use osobno na Masterze
i na każdym Slave w aplikacji producenta. Stan instalacji `Gotowe`, poprawna
telemetria sumaryczna i zgodny FC03 Mastera mogą pozostać widoczne także wtedy,
gdy przewód ESP32 dochodzi tylko do Mastera, więc nie dowodzą fizycznego
podłączenia Slave'a.

Test wspólnej magistrali z 2026-08-15 dostarczył dodatkowego dowodu sprzętowego
i protokołowego: rozładowanie baterii ustabilizowało się na 33,653 kW i
33,863 kW, a operator potwierdził Grid Discharge na obu węzłach w aplikacji
producenta. Instalacja nadal korzystała jednak z zarządzanego pakietu Home
Assistant 1.5.4 i firmware/projektu ESP32 1.5.3. Nie zapisano osobnej mocy
węzłów, zrzutów, dokładnych czasów aplikacji ani dokładnego czasu ręcznego
polecenia zatrzymania.
Historia Home Assistanta tej instalacji została też sprawdzona dla wieczornych
okien 8–14 sierpnia, 19:00–22:00 czasu lokalnego. Pokazuje pik stop około 60 kW
8 sierpnia oraz pik start około 60 kW zgodny z zapisaną zmianą kodu trybu 14
sierpnia. Okna 9–13 sierpnia pokazują powtarzające się plateau rozładowania i
impulsy przełączeń, choć próbkowanie mogło nie uchwycić maksimum. Osobno ślad
live z 15 sierpnia, 18:20 lokalnie, zawiera 63,069 kW baterii / 65,910 kW
falownika podczas przejścia. To obserwacja ograniczona do tej instalacji i tych
dat: wspiera czas przejściowy i stabilne okno, lecz nie jest uniwersalną
gwarancją dla wszystkich falowników.

Wynik nie stanowi więc odbioru oprogramowania v1.5.5 ani
v1.5.6, nie potwierdza zatrzymania automatycznego i nie zmienia FC03 Mastera w
potwierdzenie każdego Slave'a. Nadal wymagany jest test dokładnej wersji v1.5.6.

Rejestry `258`, `259` i `306` leżą poza wspólnym blokiem EMS i nie mają jeszcze
osobno potwierdzonej semantyki broadcastu Master/Slave. Aktywne działania RCEm,
które ich wymagają, pozostają w układzie równoległym fail-closed; analiza RCEm
shadow nadal działa. Nie wyłączaj żadnej z dwóch bramek gotowości.

Panel Aurora automatycznie korzysta z systemowych rejestrów mocy producenta dla
PV, baterii, zużycia domu i sieci. Adresy widoczne w rejestrach topologii są
danymi diagnostycznymi wewnętrznej sieci równoległej. ESP32 nie odpytuje ich
jako osobnych adresów Modbus przez zewnętrzny port Mastera.

Encja zapisująca rejestr `3016` (**Parallel Networking Command**) pozostaje
domyślnie wyłączona. To polecenie uruchomieniowe służy do tworzenia lub
rozłączania sieci równoległej i nigdy nie jest używane przez automatyzacje EMS.

Maksymalną liczbę urządzeń, wymagania dotyczące styczników, miejsce podłączenia
licznika i DTS oraz terminację pierwszego i ostatniego urządzenia na dedykowanej
magistrali równoległej sprawdź w instrukcji falownika.

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

Przed zgłoszeniem problemu zapisz:

- dokładny model i wersję firmware falownika;
- wersję ESPHome i Home Assistant;
- lokalną datę i godzinę zdarzenia;
- oczekiwane i zaobserwowane zachowanie;
- istotne logi po usunięciu haseł i danych osobowych.

Pobierz raport ZIP z automatycznie zamaskowanymi danymi z ostatniej zakładki
**Diagnostyka** albo użyj natywnej akcji Home Assistant **Pobierz diagnostykę**.
Przy problemach z
ESPHome, Modbus, uruchamianiem lub pętlą automatyzacji możesz także skorzystać z
rozszerzonego skryptu terminalowego. Zakres raportów i sposób anonimizacji
opisuje dokument [Diagnostyka](docs/DIAGNOSTICS.md).

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
