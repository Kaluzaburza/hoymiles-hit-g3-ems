# PV delay: blokada ładowania z zachowaniem zasilania domu

Zapis wcześniejszej zmiany. Późniejszy [kontrakt potwierdzenia trybem](PV_DELAY_MODE_AUTHORITY_2026-10-05.md)
traktuje moce jako diagnostyczne. [Odbiór z 6 października](releases/1.5.8RC2/FIELD_ACCEPTANCE.md)
oddziela techniczny PASS cyklu od braków dowodów.

Aktualne wymaganie właściciela zastępuje wcześniejszą zasadę zerowego
przepływu baterii. PV delay nie ma minimalnej mocy PV ani minimalnej nadwyżki.
PV zasila dom, pozostała moc trafia do sieci, a niedobór domu może pokrywać
magazyn w granicach rezerwy i zabezpieczeń falownika/BMS. Próg 200 W
kwalifikujący rzeczywiste ładowanie magazynu nie dotyczy tej akcji.

## Potwierdzona przyczyna

Stary start wymagał `PV > LOAD + 200 W`. Fizyczne potwierdzenie wymagało
eksportu ponad 200 W oraz jednoczesnego `|BAT| <= 50 W` i `|BMS| <= 50 W`.
Ten sam zakaz rozładowania/niskiej nadwyżki występował w podtrzymaniu podczas
przerwy w telemetrii. Prawidłowe PV=1000 W, dom=1189 W, bateria=189 W,
sieć=0 W było więc sprzecznością. Eksport 125 W również był odrzucany.
Wcześniejsza projekcja SOC już dopuszczała zasilanie domu z baterii.

## Zmiana i granice

- Start sprawdza aktualny plan, zgody, pełny FC03, topologię i świeżość
  istniejącego zestawu przepływów/BMS, bez progu mocy PV lub eksportu.
- Potwierdzenie rozróżnia ładowanie baterii, pobór baterii na dom i sprzedaż
  z baterii. Ostatnia nie jest celem PV delay. Niezależny BMS pozostaje
  wymagany, więc samo wyliczenie LOAD/BAT nie staje się fizycznym dowodem.
- Dotychczasowe tolerancje pomiarowe 50 W dla kierunku baterii i 200 W
  spójności zestawu nie są progami uruchomienia PV delay. Zestaw bez energii
  pokrywającej widoczny deficyt domu nie otrzymuje potwierdzenia.
- Pobór z sieci na dom przy deficycie PV sam nie dowodzi ładowania baterii.
  Potwierdzenie blokady ładowania nie oznacza odbioru automatycznego zasilania
  deficytu z magazynu; tę część trzeba zobaczyć osobno na instalacji.
- Podtrzymanie przez krótki brak pomiarów używa tej samej semantyki. Nie
  tworzy nowego dowodu, odnowienia bez świeżych danych ani dodatkowego czasu.

Komenda falownika pozostaje taka jak wcześniej: pełny Mode 5, cel 4305
powyżej SOC przy starcie, 4306=1%. Nie zwiększono mocy wymuszonego
rozładowania. Tabela Modbus producenta potwierdza znaczenie rejestrów, ale
nie dowodzi podziału tego limitu między eksport a zasilanie domu. Skuteczność
przy rzeczywistym deficycie PV jest nadal osobnym warunkiem odbioru installation_3.
Bez zmiany ESP, próbkowania Modbus, Recordera, rezerwy użytkownika, retry,
TTL lease, 180 s stabilizacji lub pierwotnego terminu końca.

## Dowody i odbiór

`test_pv_delay_house_flows.py`: RED starego kontraktu (24 niezgodności),
GREEN dla zerowej/małej nadwyżki i zasilania domu z baterii; przeciwne
przepływy i fałszywy bilans nadal nie przechodzą. Pełny kontroler zachowuje
jedną komendę i termin przez zmiany nasłonecznienia oraz kolejne replany,
a na końcu wykonuje restore.

`test_pv_delay_house_lease.py`: rzeczywista bramka odnowienia HA, model
firmware lease, trzy przejściowe replany, przerwa zestawu i jawne przeciwne
przepływy. Testy offline nie stanowią fizycznego odbioru falownika.

Jedynym celem wdrożenia jest installation_3. Pozostałe trzy instalacje i wydanie
publiczne pozostają HOLD. Wymagane: dokładny przetestowany SHA, własne
110 bazowych hashy, backup, bezczynność i utrwalone Off/pause, HA check,
receipt, neutralny postflight i przywrócenie wszystkich wcześniejszych
nastaw. Naturalny cykl musi wykazać blokadę ładowania, eksport nadwyżki,
rzeczywiste zasilanie deficytu domu z magazynu, ciągłość lease, co najmniej
trzy replany, nieprzedłużany deadline i naturalny restore z postflightem.
