# Documentation images / Ilustracje dokumentacji

## Accepted ESP32-S3 illustration / Zaakceptowana ilustracja ESP32-S3

`esp32-s3-rs485-hoymiles-pl.png` (2400 × 1760) and the editable `.svg` are the
accepted **20 September 2026, revision 2** illustration, restored on
9 October. They show ESP32-S3-DevKitC-1 v1.1, Waveshare TTL TO RS485 (B)
and the Hoymiles COM2 / 485_2 terminal strip with Polish labels.

The six connections retain the map confirmed by the user for that hardware.
This is not a new physical test of RC2 or other devices. Manufacturer images
retain their owners' rights; the repository MIT license does not relicense
them. See [scope, image sources and current S3 configuration](../WIRING_ESP32_S3.md).

**PL:** przywrócono wcześniejszą, zaakceptowaną ilustrację ze zdjęciami płytki,
konwertera i rysunkiem listwy COM2. PNG i SVG zachowują oryginalną treść.
Podpisy są po polsku; źródła grafik producentów pozostają wskazane. Potwierdzenie
użytkownika dotyczy tego konkretnego zestawu i nie stanowi odbioru całego EMS.

## Historical dashboard captures / Historyczne zrzuty panelu

The four source PNGs were captured from a real running Home Assistant
dashboard on **9 September 2026**, using the browser's normal desktop layout.
The page loaded `hoymiles-rce-chart-card.js?v=1.5.8.72`. They show the Polish
interface; labels in the composite are bilingual.

RC2 uses frontend `1.5.8.107`. These original `.72` captures are retained
for provenance and illustrate the layout only. They do not show the latest
labels, prove the current UI build or demonstrate physical execution.
The current text instructions take precedence when a control differs.

The capture only navigated pages and selected a chart interval. No policy,
inverter setting or source reading was changed for the screenshots. Crops
exclude the browser address bar, HA account and unrelated integrations.
Numbers are installation-specific examples captured at different moments;
they are not fresh-install defaults, simultaneous samples, invoices or
evidence that a future plan was executed. In particular, the tariff/RCEm
settings can enable planning while execution remains disabled in EMS.

| Source | View | Dimensions |
|---|---|---|
| `dashboard-start-v1.5.8.png` | Overview: current power and energy | 1532 × 690 |
| `dashboard-ems-v1.5.8.png` | EMS: selected RCE interval, SOC and grid-energy plan | 1532 × 1050 |
| `dashboard-tariff-v1.5.8.png` | Tariff policy settings | 1532 × 690 |
| `dashboard-rcem-v1.5.8.png` | RCEm voltage readings, state and settings | 1532 × 840 |

`dashboard-gallery.html` arranges these unchanged source files as a two-by-two
document. `dashboard-overview.png` is the browser export of that document
(2240 × 1807), used by both READMEs. No UI elements or values were generated
or painted into the source screenshots. To reproduce the layout, open the
HTML using a local server restricted to this directory, then capture the
`main` element at its natural size. No access to a live HA installation is
needed to render the gallery.

The browser returned JPEG bytes; the saved files were re-encoded as PNG to
match their extensions. The decoded pixel data were compared before and after
conversion and were identical. This does not remove the original browser
compression or create extra image detail.

The older `v1.4.4.png` sources displayed an HA account name. They and their
unused legacy collage script were archived privately and removed from the
RC2 source package. The four `.72` sources and their composite were visually
checked for installation names, account names and addresses.

## Polski

Cztery źródłowe pliki PNG pochodzą z działającego panelu Home Assistant
z **9 września 2026 r.**, z interfejsem `1.5.8.72`. Pokazują rzeczywiste
odczyty i ustawienia jednej instalacji w różnych chwilach. Podczas wykonywania
zrzutów otwierano jedynie strony i zaznaczono przedział wykresu. Nie zmieniano
planów, nastaw falownika ani danych źródłowych. Kadry pomijają adres serwera,
konto HA i inne integracje.

RC2 używa frontendu `1.5.8.107`. Oryginały `.72` pokazują historyczny układ,
bez potwierdzania obecnej wersji interfejsu ani wykonania fizycznego.
Jeśli kontrolka różni się od obrazu, obowiązuje aktualna instrukcja tekstowa.

To przykłady, a nie wartości domyślne, jednoczesne próbki pomiarowe, rozliczenia
ani dowód przyszłego wykonania planu. Włączenie planowania w ustawieniach
taryfy lub RCEm nie oznacza włączenia wykonania w EMS.

Plik HTML składa niezmienione zrzuty w układ „4 w 1”, eksportowany następnie
przez przeglądarkę do `dashboard-overview.png`. Podpisy są dwujęzyczne;
źródłowe interfejsy pozostają polskie. Stare obrazy `v1.4.4.png` ujawniały
nazwę konta HA. Przeniesiono je wraz z nieużywanym skryptem ich kolażu do
prywatnego archiwum, poza paczką RC2. Cztery źródła `.72` i ich kolaż
sprawdzono wizualnie pod kątem nazw instalacji, kont i adresów.

Eksport przeglądarki miał format JPEG. Pliki zapisano ponownie jako PNG,
zgodnie z rozszerzeniem, i potwierdzono identyczność zdekodowanych pikseli.
Konwersja nie usuwa pierwotnej kompresji ani nie dodaje szczegółów.
