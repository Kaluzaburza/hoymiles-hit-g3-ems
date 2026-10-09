# Opóźnienie ładowania PV — horyzont jutra i moc zależna od SOC

## Plan i kontrakt

Potwierdzony problem: po południu Pstryk pomijał opóźnienie, a wspólna funkcja
przeszukiwała tylko pierwszy dzień. Bieżący limit BMS przy pełnym magazynie
był rozciągany na wszystkie przyszłe poziomy SOC. Na installation_3 z 15 kWh i prognozą
48,96 kWh dawało to jutrzejsze ładowanie około 1,52 kW przez cały dzień.

1. Wybrać najwyżej jedno okno na każdy zakwalifikowany dzień. Zachować stan
   baterii i pochodzenie energii przez noc oraz ustalone działania BUY/SELL.
2. Udowodnić odzyskanie zapasu przed terminem na niższej prognozie właściwego
   dnia, bez dodatkowego importu ani sprzedaży energii potrzebnej domowi.
3. Wykorzystać istniejące pięciominutowe statystyki SOC, prądu BMS i napięcia
   do małej krzywej mocy w pasmach poniżej 90%, 90–98%, 98–100%. Wymagać
   wszystkich pasm i co najmniej dwóch dni obserwacji w najniższym paśmie.
   Używać dolnego kwintyla i nierosnącego limitu mocy przy wzroście SOC.
4. Uwzględnić czas przejścia przez pasma podczas ładowania. Do bieżącego
   przedziału stosować świeży BMS; przyszła krzywa nie daje zgody na komendę.
   Przy ograniczeniu już przy niskim SOC ograniczać także krzywą przyszłą.
5. Ponownie zweryfikować te same okna przed publikacją. Nie dodawać nowych
   okien w rewalidacji, nie wydłużać czasu odzyskiwania zapasu.
6. Wykresy i plan korzystają z istniejących `pv_charge_hold` w timeline.
   Najbliższe okno zachowuje dotychczasowe pola zgodności sterowania.

## Koszt danych

Brak nowych encji i zapisów historii. Najwyżej trzy indeksowane odczyty
statystyk z sześciu dni raz na sześć godzin, limit 1731 wierszy na encję,
osiem sekund oczekiwania. Przekroczenie limitu odrzuca dane. Trwający
pracownik po przekroczeniu czasu blokuje następny odczyt.
Krzywa jest zapisywana w istniejącym pliku cache LOAD, z jego tożsamością
źródła. Brak danych zachowuje dotychczasowy limit BMS. Ważność: siedem dni.

## Weryfikacja i wdrożenie

- Regresje: popołudnie → jutro, dwa dni, pełny SOC teraz, DST, brak cen lub
  P10 jutra, zero export, BMS=0, spowolnienie końcowego ładowania, ochrona
  domu, rewalidacja i istniejące sterowanie/Recorder.
- Odtworzenie danych installation_3 bez zmiany jego stanu podczas testów offline.
- Po zgłoszeniu braku startu sprzedaży użytkownik zdecydował o wspólnym
  wdrożeniu tej poprawki i naprawy publikacji Pstryka na wszystkich hostach.
- Jeden przetestowany SHA i pakiet dla installation_1, installation_2 i installation_3;
  techniczne sprawdzenie każdego hosta oraz osobny odbiór fizycznego działania.
- installation_1 zachowuje zero export. Wdrożenie nie daje zgody na jego eksport.

Status: zaimplementowano i sprawdzono offline. Odtworzenie pełnego horyzontu
installation_3 na niższej prognozie nie kwalifikuje dodatkowego okna; sama produkcja
P50 większa od pojemności baterii nie gwarantuje opłacalnego, bezpiecznego okna.
Wyniki i dokładne SHA pakietów/odbiorów zapisano osobno. Dokument nie potwierdza
odbioru fizycznego porannego wypływu.
