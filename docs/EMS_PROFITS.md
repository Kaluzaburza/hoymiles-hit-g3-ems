# Zyski — archiwum ekonomii EMS

Zakładka Aurora „Zyski” jest ostatnią główną zakładką, po „Energii”.
Pokazuje dzień, tydzień od poniedziałku, miesiąc i rok w strefie czasowej instalacji.
To odczyt ekonomii; nie steruje falownikiem i nie zmienia decyzji EMS.
Domyślnie otwiera bieżący miesiąc. Dwa główne panele rozdzielają zakup z sieci
i sprzedaż do sieci: kWh, kwotę netto i średnią cenę ważoną wycenioną energią.
Zakupy dzielimy według zapisanej taryfy i strefy (tania, średnia, droga, jedna
stawka), a sprzedaż według źródła ceny (np. Pstryk, RCE). Przy cenach dynamicznych
nie tworzymy umownego podziału na tanie i drogie godziny. Historyczna zmiana
dostawcy zostaje widoczna. Podsumowania zawsze obejmują cały okres, niezależnie
od strony tabeli cen. Wykres pozwala przełączyć kwoty na kWh.

Saldo usunięto z interfejsu. Szczegółowe listy działań i godzin oraz opis
metody są domyślnie zwinięte. W API pozostają dotychczasowe pola dla zgodności.

## Co oznaczają kwoty

- Koszt zakupu i przychód ze sprzedaży są sumami wycenionych przepływów,
  liczonymi osobno. Brakująca cena pozostawia kWh bez kwoty; przy częściowej
  wycenie podajemy brakujące kWh i opisujemy kwotę jako dotyczącą wycenionej części.
  Nie dzielimy takiej kwoty przez energię, której cena jest nieznana.
- W szczegółach autokonsumpcja oznacza brak aktywnego właściciela EMS.
  Pozostałe kategorie obejmują zmierzone przepływy podczas potwierdzonego wykonania:
  sprzedaż sterowaną przez EMS, opóźnienie PV, ładowanie taryfowe, taryfowe zasilanie domu i RCEm.
  Sterowanie ręczne i nierozpoznane pozostają osobno. Sama intencja lub plan nie
  uprawniają do przypisania przepływu do EMS.
- Szacowana korzyść EMS jest porównaniem z modelem autokonsumpcji na podstawie
  zaobserwowanych PV i obciążenia domu. Oba warianty zaczynają z tym samym SOC.
  Model uwzględnia sprawności, rezerwę, limity mocy i eksportu. Różnica energii
  pozostającej w baterii jest wyceniana nieujemną ceną zakupu z początku ciągłego
  porównania, po sprawności rozładowania. Zapobiega to traktowaniu ubytku magazynu
  jako darmowego zysku. Porównanie trwa również podczas późniejszej autokonsumpcji.
  Brak danych, restart lub zmiana parametrów baterii przerywa porównanie.

Korzyść nie jest już głównym wskaźnikiem. Sekcja „Czy EMS przyniósł oszczędności?”
pokazuje czas porównania i jego udział w czasie z pomiarami. API zwraca
`benefit_status`: `unavailable` bez porównania, `partial` dla fragmentów,
`complete` dopiero przy pokryciu całego upływającego okresu (tolerancja 1 s).
Wynik fragmentu jest opisany osobno i nie jest ekstrapolowany na dobę ani miesiąc.
Procent pokrycia modelu ma inny mianownik niż kompletność pomiarów okresu.

Szacunek korzyści zależy od modelu i rozdzielczości SOC; nie dowodzi przyczynowości
ani rozliczenia sprzedawcy. Przychód podczas działania EMS nie jest w całości
dodatkowym przychodem EMS. Import podczas ładowania może również zasilać dom.
Opłaty stałe, amortyzacja baterii i korekty rozliczeń prosumenckich są wyłączone.

## Ceny netto i historia

Pstryk: publiczne `priceNet`, bez dodatkowych opłat. RCE: PLN/MWh ÷ 1000.
Wbudowane profile: istniejąca cena krańcowa brutto ÷ 1,23 (stawka w tych profilach),
z zachowaniem zawartych opłat zmiennych. Ręczne ceny domyślnie są **netto**, zgodnie
z dyspozycją użytkownika. Osobny selektor podstawy ceny dotyczy wyłącznie nowych
zapisów ekonomii; nie zmienia cen sterowania. Nieznana podstawa oznacza brak wyceny.
Ceny ujemne i zerowe zachowują znaczenie. Źródło, strefa i zastosowana cena są
utrwalane podczas obserwacji; edycja konfiguracji nie przelicza przeszłości.

Archiwum zaczyna się po instalacji tej wersji. Starsza historia Recorder nie ma
kompletnej historycznej konfiguracji zakupu i przypisania działań, więc nie jest
przeliczana po dzisiejszych cenach. Braki są widoczne, nigdy zastępowane zerowym
zyskiem. Godziny zapisujemy jako UTC wraz z lokalną datą i przesunięciem UTC;
obie godziny podczas jesiennej zmiany czasu pozostają rozróżnialne.

## Koszt przechowywania i granice pomiaru

Źródłem jest istniejący pomiar mocy sieci (plus = eksport), bez nowych odczytów
Modbus, zmian ESPHome ani zapisów per próbka w Recorderze. Energia jest całką
próbkowanej mocy, nie licznikiem rozliczeniowym. Obserwator reaguje na istniejące
zmiany stanów i sprawdza świeżość co 30 s; przerwa ponad 120 s tworzy lukę.

Oddzielny plik `.storage/hoymiles_hit_modbus_profits_<entry_id>.sqlite` zawiera
agregaty godzinowe rozdzielone ceną i potwierdzonym rodzajem działania. Zapisuje
partie co pięć minut i przy zamknięciu HA. Powtórzenie tej samej partii jest
idempotentne. Nagła utrata zasilania może utracić ostatnią niezapisaną partię;
po restarcie nie dopisujemy jej retrospektywnie. Nie ma automatycznego usuwania
starych godzin. Zapytania używają indeksu dat i agregacji SQL; tabela cen jest
stronicowana po 24 pozycje, a wynik okresu buforowany przez 30 s.

Archiwum jest przypisane do wpisu integracji, urządzenia źródłowego i strefy
czasowej. Zmiana tej tożsamości blokuje jego mieszanie z poprzednimi danymi.
Kopia HA powinna obejmować plik archiwum. Błąd ekonomii nie blokuje sterowania.

Walidacja: `tools/test_profit_archive.py`, `tools/test_profit_http.py`,
`tools/test_profit_ui_playwright.js` oraz dotychczasowe testy integracji i Aurora.
Zakładkę wdrożono na czterech instalacjach w produkcie `4720a1f`.
To historyczny zakres pierwszej wersji, nie dowód odbioru nowego projektu.
Przeprojektowanie z 7.10 ma frontend 120 i jest lokalną zmianą na bazie
`8e143f6fba9bc707df9b7157c1b47f216d9c48f6`, bez wdrożenia i publikacji.
Nie zmienia schematu SQLite, obserwatora, cen, modelu neutralnego ani reguł sterowania.
Diagnoza z trzech instalacji wykazała fragmentaryczne albo zerowe pokrycie
porównaniem; na jednej aktualny wspólny snapshot miał nieświeże granice SOC.
Test odtwarza odrzucenie modelu przy zachowaniu energii i pieniędzy. To nie jest
naprawa źródła świeżości ani odzyskanie brakujących danych historycznych.
Dowody i liczby pozostają w prywatnym raporcie poza repo.
Nowy kandydat wymaga osobnego zamrożenia i pełnych bramek publikacyjnych.
