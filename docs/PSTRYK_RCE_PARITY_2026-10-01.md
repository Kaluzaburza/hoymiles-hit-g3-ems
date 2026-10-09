# Wspólna sprzedaż RCE/Pstryk — poprawka 1.5.8RC1

## Potwierdzony problem

Pstryk korzystał z osobnej zachłannej selekcji BUY/SELL, wymagał zachowania
końcowego zapasu identycznego z autokonsumpcją i odrzucał zapas wejściowy przy
więcej niż jednym falowniku. installation_2 ma zweryfikowany bilans całego układu
Master; brak takiego bilansu nie był przyczyną obserwowanego planu.

## Końcowy kontrakt

- RCE i Pstryk wywołują tę samą ograniczoną funkcję wyboru bloków sprzedaży
  i pakowania małych końcówek. RCE zachowuje swoją fizykę i funkcję celu.
  Pstryk rozlicza zakup, sprzedaż, straty, zużycie baterii i wypływ PV w jednym
  bilansie; dlatego jego wynik finansowy nie musi być identyczny z RCE.
- Pstryk pobiera chronioną rezerwę domu/nocy i ostrożny scenariusz z fizycznego
  przeliczenia RCE bez wyszukiwania sprzedaży. Zachowuje kwalifikację Mastera,
  świeżość danych, dostępność i rzeczywiste zera BMS, procent bezpieczeństwa,
  GCF, blokadę godzin i ustawione minimum mocy eksportu.
- Zapas początkowy jest fizycznym, zakwalifikowanym SOC systemu, jak w RCE.
  Nie jest deklarowany jako historycznie potwierdzona energia PV. Stary licznik
  pochodzenia pozostaje czytelny w magazynie danych dla zgodności, ale nie jest
  uaktualniany ani używany jako dodatkowa bramka Pstryka.
- Najpierw powstaje plan zakupu potrzebnego domowi/rezerwie. Wyszukiwanie
  sprzedaży nie może dodać zakupu ani zwiększyć importu w żadnym przedziale
  tego planu, także przy ostrożnej prognozie PV. Nowy zakup nie tworzy zapasu
  sprzedaży wewnątrz tego samego horyzontu. Ta sama kontrola działa podczas
  ponownej walidacji komend na świeżych danych.
- Energia ponad chroniony zapas może zostać sprzedana bez odtworzenia całego
  zapasu końcowego wariantu autokonsumpcji. Chroniona energia domu nie jest
  sprzedawana w zamian za późniejszy zakup; polityka agresywna pozostaje 1.5.9.
- Przeliczana z bieżącego LOAD/SOC rezerwa nie zmienia identyfikatora rynku
  podczas stabilizacji. Próba kontrolna nadal stosuje świeżą rezerwę, a zmiana
  jawnych nastaw, prognoz, cen lub uprawnień wycofuje kwalifikację.
- Opóźnienie ładowania PV zachowuje własną potwierdzoną prognozę odtworzenia
  zapasu jeszcze dziś. Scenariusz zerowego PV dla ochrony sprzedaży nie usuwa
  tej prognozy. Skrócenie horyzontu przez inną akcję skraca wszystkie mapy.
- Brak sprzedaży ma jednoznaczny kod przyczyny i polski/angielski stan planu:
  potrzeby domu/rezerwy, ostrożna prognoza, eksport zablokowany, niedostępna moc,
  zajęta moc AC przez PV, blokada godzin, zbyt niska cena lub minimum mocy/zysku.
  Ten sam sensor jest wyświetlany w ustawieniach i dashboardzie.

## Walidacja i odbiór

Regresje obejmują RED→GREEN dla wolnego zapasu, brak dodatkowego importu,
rewalidację, Master, brak/zero BMS, zero export, minimum eksportu, blokady cen,
zmianę czasu, stabilizację i opóźnienie PV. Pełny test RCE zawiera 85 scenariuszy.
Tygodniowe odtworzenie korzysta z zapisanych publicznych cen netto i danych
instalacji; prognoza używa wyłącznie wcześniejszych dni. To symulacja, nie pomiar
zysku na rachunku. Osobny test Recorder obejmuje 72 godziny nowego sensora cen;
nie mierzy całego produkcyjnego pliku bazy ani jego długoterminowego wzrostu.

Każdy host wymaga osobnego sprawdzenia: identyczny commit i hasze 106 plików
pakietu, scoped backup, check konfiguracji, uruchomienie, zgodność rewizji planów
oraz minimum 180 sekund obserwacji. installation_1 zachowuje GCF włączone z limitem
eksportu 0. Odbiór techniczny pakietu nie zastępuje naturalnego cyklu BUY/SELL.
Dowody i wyniki są zapisywane w zewnętrznym katalogu raportu, z SHA commitu.
Historyczna bramka Task 02 pozostaje oddzielną bramką publikacji; jej utrwalone
asercje nie są zmieniane na potrzeby tej poprawki.

## Odtworzenie ostatnich dni installation_3

Odczyt statystyk z 25–30 września, historyczne ograniczenia BMS i publiczne
godzinowe ceny netto posłużyły do analizy 28–30 września (144 decyzje co pół
godziny). Prognoza używa wcześniejszych ukończonych dni. Oddzielny wariant
diagnostyczny zna przyszłe zużycie/PV, aby rozdzielić błąd prognozy od bilansu.
To niezależne decyzje z obserwowanym SOC, nie odtworzenie rzeczywistych zarobków.

Pierwszy przebieg znalazł cztery odrzucenia niezmienionego planu podczas
rewalidacji. Przy mniejszym przyszłym PV ta sama komenda rozładowania pokrywa
więcej potrzeb domu i eksportuje mniej. Walidator wymagał identycznego eksportu
w obu prognozach, mimo zachowanej rezerwy i braku dodatkowego importu. Regresja
sprawdza tę sytuację; walidacja chroni import i koszt względem wariantu dla domu,
bez wymuszania identycznego udziału eksportu. Istniejący test wzrostu potrzeb
domu nadal nie pozwala finansować sprzedaży późniejszym importem.
