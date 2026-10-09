# RCE: prognoza PV niezależna od publikacji cen

Baza: `c4e8bb56d9c516beb8150c344ebbcfba7eceee4f`.

## Dowód i przyczyna

27.09.2026 o 08:51:44 UTC installation_2 raportował 230 kWh, SOC 70% i
161 kWh energii. Plan wymagał 177,05 kWh, z `p10_missing` i pominięciem
74,64 kWh prognozy PV. Jednocześnie źródło Solcast miało kompletne 48
wierszy jutra i P10 90,5493 kWh. RCE oczekiwało na jutrzejsze ceny.

Adapter zerował prognozę jutra i jej P10, kiedy nie miał cen. Ten sztuczny
brak P10 uruchamiał scenariusz zerowej produkcji także dzisiaj. Powstawał
fałszywy `home_energy_shortage`. Teksty stanów w tym samym pliku zawierały
uszkodzone kodowanie, widoczne w natywnym stanie encji i karcie Aurora.

## Zmiana

- Świeża, poprawnie datowana prognoza jutra i jej P10 są używane do bilansu
  domu także przed publikacją cen. Zachowana jest adaptacja Solcast.
- Sprzedaż jutra nadal wymaga kompletu świeżych cen i świeżej prognozy.
  Brakujące ceny nie są uzupełniane ani zgadywane.
- Faktycznie brakująca, stara lub błędnie datowana prognoza nadal uruchamia
  dotychczasowe zabezpieczenie. Progi SOC, BMS, GCF i pauza pozostają wejściami.
- Poprawiono polskie i angielskie teksty statusów, przyrostek zakresu dnia
  oraz separator godzin okna nocnego.

## Testy i granice dowodu

`tools/test_rce_forecast_market_scope.py` używa produkcyjnego adaptera i
fizyki. Obejmuje oczekiwanie na ceny, nadejście cen bez zmiany PV, rzeczywisty
brak/starość/złą datę/brak P10, rezerwę 138 kWh i natywne tłumaczenia.

Osobne odtworzenie na zapisanych wartościach installation_2 i profilach LOAD
zmienia `home_energy_shortage` na `home_protected`, bez zmiany bazowej rezerwy
138 kWh. Stosuje syntetyczną świeżość i minutową rekonstrukcję słońca, więc
nie jest dokładnym odtworzeniem historycznego solvera ani odbiorem fizycznym.

Dokładny SHA testów, logi RED/GREEN, manifest i wyniki wdrożeń są w raporcie
zadania poza drzewem produktu. Ciągłość sprzedaży wymaga naturalnego cyklu.
