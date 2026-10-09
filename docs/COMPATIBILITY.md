# Compatibility / Zgodność — 1.5.8RC2

## Polski

Według informacji przekazanej przez opiekuna projektu użytkownicy **EMS 1.5.7**
zgłaszają działanie z rodzinami **Hoymiles HiOne, HIT-(5–20)L-G3, HAS i HAT**.
To informacja o doświadczeniach użytkowników. Nie otrzymano tu listy wszystkich
modeli, wersji firmware ani protokołów odbioru każdej funkcji. Dlatego nie
oznaczamy wszystkich falowników hybrydowych Hoymiles jako przetestowanych w RC2.

| Rodzina | Rodzaj urządzenia | Zakres dostępnego potwierdzenia |
|---|---|---|
| HIT-(5–20)L-G3 | Trójfazowy falownik hybrydowy z magazynem niskonapięciowym | Platforma referencyjna kodu i instrukcji; testy instalacyjne głównie HIT-10L-G3 i HIT-20L-G3. Odbiór konkretnej wersji EMS pozostaje osobnym etapem. |
| HiOne | Zintegrowany system magazynowania energii | Zgłoszenia użytkowników 1.5.7; zakres funkcji i wariant wymagają potwierdzenia dla danej instalacji. |
| HAS | Jednofazowy falownik magazynowy AC-coupled | Zgłoszenia użytkowników 1.5.7; bez przenoszenia schematu COM2 i odbioru HIT na tę rodzinę. |
| HAT | Trójfazowy falownik magazynowy AC-coupled | Zgłoszenia użytkowników 1.5.7; bez przenoszenia schematu COM2 i odbioru HIT na tę rodzinę. |
| Pozostałe modele Hoymiles | Według dokumentacji konkretnego urządzenia | Brak szczegółowych dowodów w tej dokumentacji; zgodności nie wyprowadzamy wyłącznie z marki. |

HAS i HAT producent zalicza do urządzeń AC-coupled, dlatego zbiorcze określenie
„wszystkie falowniki hybrydowe” byłoby nieprecyzyjne. Dostępność rejestrów,
portów, trybów pracy, BMS i funkcji równoległych zależy od modelu oraz firmware'u.
Nazwa integracji w HACS i HA pozostaje **EMS for Hoymiles HIT-(5–20)L-G3**;
nie trzeba szukać osobnej integracji dla każdej rodziny.

Przed włączeniem automatycznego sterowania potwierdź model i firmware, właściwy
port RS485, ustawienia transmisji, mapę rejestrów, kierunki przepływów i SOC/BMS.
Odczyty monitoringu nie potwierdzają jeszcze wykonania poleceń. Weryfikacja
sterowania obejmuje fizyczny odczyt po komendzie i przywrócenie nastaw;
w układzie równoległym osobno sprawdza się każdy falownik. Schemat
[ESP32-S3–konwerter–COM2](WIRING_ESP32_S3.md) dotyczy wyłącznie wskazanego HIT-G3.

## English

According to the project maintainer, **EMS 1.5.7 users** report operation with
**Hoymiles HiOne, HIT-(5–20)L-G3, HAS and HAT**. These are community reports;
they do not supply an exhaustive model/firmware list or acceptance records
for every function. They do not establish RC2 acceptance for every Hoymiles hybrid inverter.

HIT-G3 is the reference implementation, with installation testing mainly on
HIT-10L-G3 and HIT-20L-G3. HiOne is an integrated storage system. Hoymiles lists
HAS as single-phase and HAT as three-phase **AC-coupled storage inverters**.
Other models need their own evidence. Register support, ports, BMS data,
operating modes and parallel capability depend on the device and firmware.
The integration is still named **EMS for Hoymiles HIT-(5–20)L-G3** in HACS/HA.

Before enabling automatic control, verify the exact model/firmware, RS485 port,
serial configuration, register map, power-flow signs and SOC/BMS readings.
Monitoring alone does not prove command execution. Control commissioning needs
physical readback after a command and verified restoration; parallel systems
need checks on every inverter. The [COM2 wiring example](WIRING_ESP32_S3.md)
applies only to its named HIT-G3 hardware.

## Sources / Źródła

- Community scope: maintainer's report of users running 1.5.7, recorded for this
  documentation on 3 October 2026. No installation identity or address is published.
- [Hoymiles product families](https://www.hoymiles.com/products.html) and
  [HiOne](https://www.hoymiles.com/hione.html) identify the manufacturer's product categories.
- [Hoymiles product catalogue](https://ec2.hoymiles.com/uploadfile/7/202507/ca99d2e41e.pdf)
  distinguishes hybrid and AC-coupled storage families. These sources describe
  the hardware; they do not certify compatibility with this independent EMS.
