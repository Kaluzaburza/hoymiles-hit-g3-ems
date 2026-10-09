# ESP32-S3 wiring / Okablowanie ESP32-S3 — 1.5.8RC2

[![Six connections / Sześć połączeń: ESP32-S3, isolated converter, Hoymiles COM2](images/esp32-s3-rs485-hoymiles-pl.png)](images/esp32-s3-rs485-hoymiles-pl.png)

[Editable SVG / Edytowalny SVG](images/esp32-s3-rs485-hoymiles-pl.svg)

## Scope / Zakres

This illustrated guide applies to **Espressif ESP32-S3-DevKitC-1 v1.1**,
the isolated **Waveshare TTL TO RS485 (B)** converter and **Hoymiles
HIT-(5–20)L-G3, COM2 / 485_2**. It shows the board, converter and COM2 terminal
strip with Polish annotations. J1 numbers identify the Espressif header
positions; they are not GPIO numbers. Check each device's own labels and manual.

Ilustracja dotyczy wyłącznie powyższych urządzeń i pokazuje ich punkty połączeń.
Oznaczenia J1 dotyczą nagłówka płytki
Espressif, a nie numerów GPIO. Nie przenoś tego pinoutu COM2 na HiOne, HAS,
HAT ani inne warianty bez sprawdzenia ich dokumentacji.

| Wire / Przewód | ESP / converter | Converter / inverter |
|---|---|---|
| 1 — 3.3 V / 3,3 V | 3V3, J1.1 | VCC |
| 2 — TTL ground / masa TTL | GND, J1.22 | GND |
| 3 — ESP TX | GPIO17, J1.10 → | RXD |
| 4 — ESP RX | GPIO16, J1.9 ← | TXD |
| 5 — RS485 A (+) | A+ | COM2: 485_2+ |
| 6 — RS485 B (−) | B− | COM2: 485_2− |

Power the ESP board through USB and the converter from ESP 3V3. Keep UART
logic at 3.3 V. Do not connect VCC to an inverter communication terminal.
In this example, isolated-side SGND is unconnected and is not bridged to TTL
GND. The named COM2 connection does not provide a GND/reference terminal for
485_2; do not substitute 12V−, PE or another port. A/B form one twisted pair;
set termination according to the actual bus. This converter switches direction
automatically: it needs no DE/RE wire or GPIO4.

ESP zasil przez USB, a konwerter z 3V3. Po stronie UART obowiązuje logika
3,3 V. Nie podłączaj VCC do zacisków komunikacyjnych falownika. W tym przykładzie
SGND pozostaje wolny i rozdzielony od GND TTL. COM2 nie udostępnia tu GND dla
485_2; nie zastępuj go 12V−, PE ani innym portem. A/B prowadź skręconą parą
i dobierz terminację do magistrali. Ten konwerter automatycznie zmienia
kierunek, więc nie wymaga DE/RE ani GPIO4.

For ESP32-S3 DevKitC-1 WROOM-1 N16R8 use
[`hoymiles-inverter-s3.yaml`](../hoymiles-inverter-s3.yaml).
Its 16 MB flash and octal PSRAM profile is separate from classic `esp32dev`;
see [board variants and first USB installation](ESP32_VARIANTS.md). Repository
inverter-link defaults are `115200 8N1`, address `1`; they must match the
inverter port and are not settings for the separate energy-meter link.

Dla S3 N16R8 użyj osobnego pliku `hoymiles-inverter-s3.yaml` wskazanego powyżej.
Starsza adnotacja na ilustracji o `esp32dev` dotyczy pliku klasycznego ESP32.
Domyślne parametry połączenia
z falownikiem to `115200 8N1`, adres `1`; muszą odpowiadać konfiguracji portu.
Nie dotyczą osobnego połączenia z licznikiem energii.

Before wiring, a qualified person must isolate **all** energy sources and
verify absence of voltage according to the inverter manual. This schematic
covers communication wiring only. Follow the [installation guide](QUICK_START.md).

Przed pracą osoba z odpowiednimi kwalifikacjami musi odłączyć **wszystkie**
źródła energii i potwierdzić brak napięcia zgodnie z instrukcją falownika.
Rysunek obejmuje tylko komunikację; zobacz [instrukcję instalacji](QUICK_START.md).

## Evidence and artwork / Dowody i grafika

- **20 September 2026:** the user confirmed that these six connections matched
  the actual installation and passed a test. This is attributed user evidence;
  no duration, instrument readings or detailed test log were supplied.
- **9 October 2026:** the accepted **20 September 2026, revision 2** PNG and
  editable SVG were restored from the original illustration. They show the board
  pin layout, hardware photographs and the manufacturer's COM2 drawing.
- The illustration's acceptance refers to that particular example. It is not
  a new live test of RC2, other models or the complete EMS. The current S3 N16R8
  configuration is linked above; the historical graphic does not replace it.
- Embedded third-party graphics retain their owners' rights. The project's
  MIT license does not relicense the Espressif, Waveshare or Hoymiles images.

**PL:** użytkownik potwierdził połączenia i test 20 września. Przywrócono dokładnie
tę zaakceptowaną ilustrację, ze zdjęciami urządzeń i listwą COM2. Źródła materiałów
producentów są podane poniżej; zachowują one prawa swoich właścicieli. Przywrócenie
grafiki nie oznacza ponownego testu sprzętu ani odbioru całego EMS.

## Manufacturer references / Dokumentacja producentów

- [Espressif DevKitC-1 v1.1 guide](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.1.html): J1 header table and board pin-layout graphic.
- [Espressif board photograph](https://raw.githubusercontent.com/espressif/esp-dev-kits/master/docs/_static/esp32-s3-devkitc-1/esp32-s3-devkitc-1-v1.1-isometric.png).
- [Waveshare TTL TO RS485 (B)](https://www.waveshare.com/ttl-to-rs485-b.htm)
  and [manufacturer wiki](https://www.waveshare.com/wiki/TTL_TO_RS485_(B)):
  product photograph, logic levels, isolated terminals and UART directions.
- [Hoymiles HIT-(5–20)L-G3 manual](https://www.hoymiles.com/uploadfile/1/202506/ec8b1b6a09.pdf):
  section 7.8, printed page 39, COM2 drawing and labels 485_2+ / 485_2−. Counted terminal
  positions are not substituted for the manufacturer's printed labels.
