# Ciągłość sprzedaży Pstryk i wcześniejsze dowody RCE

## Potwierdzone zdarzenie

Na installation_3 pakiet `a732b2f92f20745fd515a0415b0e8d6afbc04744` rozpoczął
rzeczywistą sprzedaż Mode 5. Pierwsza transakcja przetrwała kilka pełnych
przeliczeń, lecz o 17:18:26 UTC zakończyła się
`physical_verification_unavailable`. Przygotowane następne polecenie nie było
wysłane, kontrola przed wysłaniem odrzuciła zmienioną kohortę, a przywrócenie
poprzednika miało `exact_target=true`, komplet dopuszczających bramek i
`attested=false`. Następne transakcje także zakończyły się przed końcem okna.
EMS installation_3 wstrzymano; potwierdzono świeży Mode 0, brak transakcji i lease.
Ten odbiór M01 jest **FAIL/HOLD**, niezależnie od poprawności plików wdrożenia.

installation_2 utrzymał jedną transakcję przez ponad 30 minut, lecz zakończyła się ona
przed terminem o 17:38:16 UTC (`authorization_lost`, `local_hard_stop`).
Po kolejnych próbach także ten EMS wstrzymano. To osobny przypadek, nie dowód
tego samego błędu starego potwierdzenia. Zgłoszenie niewidocznej sprzedaży na
wykresie wymaga porównania opublikowanego timeline i planu kanonicznego;
nie zaliczamy pełnego odbioru installation_2.

## Historia i wspólne zabezpieczenia

- Dowody z 21 i 26 września dotyczą niewysłanej zmiany parametrów i odmowy
  podtrzymania poprzednika. Poprawki zachowania dokładnego poprzednika oraz
  ograniczonego retry znajdują się już w bieżącej gałęzi.
- Raport `2026-09-29_M01_RCE_0edd4c/100-116-217-42/CONTINUITY_CAUSAL_ANALYSIS_20260929.md`
  opisuje zależność od helpera ceny niedostępnego podczas przeliczenia.
  Naprawa jest obecna: zwykły hold przeliczenia opiera się na zaakceptowanej
  transakcji i aktualnych bramkach. Nie wymaga prezentacyjnego helpera.
  Osobny hold stabilizacji pomiarów zachowuje własne ograniczenia.
- Pstryk używa wspólnej ścieżki RCE eksportu: kandydat, minimalna moc netto,
  SOC/rezerwa, BMS, GCF, właściciel, FC03, lease, retarget, deadline i restore.
  Nie jest to niezależna kopia sterownika.
- Publikacja Pstryk ma dodatkowo wspólną rewizję BUY/SELL i odrzuca wynik po
  zmianie zużytych wejść. Nieaktualny plan nie zezwala na nowy START ani nową
  komendę. Podtrzymanie już wykonywanej komendy wymaga osobnych dowodów.

## Odtworzony dodatkowy błąd i minimalna naprawa

Regresja z prawdziwym adapterem HA odtwarza opóźnienie zapisu przed wysłaniem
zmiany: zapisany dowód przepływu poprzednika A ma ponad 15 sekund, ale nowa
kompletna ramka FC03 i świeże moce nadal potwierdzają A. Niewysłany następca B
jest poprawnie odrzucony. Stary kod ponownie sprawdzał stary dowód A i
zatrzymywał sprzedaż mimo dostępnego świeżego potwierdzenia.

Przed przywróceniem A sterownik ponownie ocenia fizyczny przepływ z bieżącej
ramki. Pozostają dokładna tożsamość transakcji, niezmienny termin, pełny blok
4300–4306, wszystkie bramki oraz **15-sekundowy limit świeżości**. Nie odnawia
daty starego dowodu i nie dopuszcza B na podstawie dowodu A. Nie dodaje encji,
zapytań Recorder ani okresowych zapisów.

Odtworzenie pasuje do pierwszego zdarzenia terenowego, ale zapis terenowy nie
zawiera wieku starego dowodu. Nie przypisujemy tej przyczyny automatycznie
wszystkim zaobserwowanym przerwaniom.

## Walidacja i otwarte bramki

- RED→GREEN dla świeżej ramki po powolnym zapisie; brak świeżego przepływu
  i przepływ sprzeczny nadal zatrzymują wykonanie bez wysłania następcy.
- Regresje wcześniejszych RCE: niewysłany retarget, BMS, mała moc,
  wspólny budżet retry, faza potwierdzenia fizycznego, controller/golden,
  taryfa i stabilizacja Pstryk/PV. Wyniki w katalogu dowodów poniżej.
- Macierz automatyki: 488 scenariuszy i 4 negatywne kontrakty BMS PASS.
  Pełne testy wolnego przeliczenia, publikacji i rzeczywistej kadencji lease
  (łącznie z symulowanym naturalnym zakończeniem) także PASS.
- Fixture wspólnego budżetu wymagała zgodnego limitu BMS już przy START.
  Jej wcześniejszy błąd odtworzono na bazowym SHA; korekta danych testowych
  nie zmienia żadnego oczekiwania ani produkcyjnej blokady BMS.
- Okno opóźnienia PV na installation_3 rzeczywiście pojawia się i znika także przy
  aktualnym planie. Analiza wrażliwości pokazała skokowy wybór sprzedaży przy
  zmianie LOAD, lecz nie odtworzyła jednoznacznie zaniku okna. Przyczyna tego
  osobnego zjawiska pozostaje otwarta; nie dodano bezwarunkowego zatrzasku.
- Brak potwierdzenia dostawcy powiadomień na installation_3 jest osobnym stanem
  diagnostycznym. Nie jest dowodem awarii fizycznego sterowania.
- Pełny naturalny M01 na nowym SHA wymaga nowego odbioru. Nie sumuje się
  czasu różnych transakcji ani nie przenosi odbioru installation_2 na installation_3.

Dowody lokalne:
`<PRIVATE_EVIDENCE>/2026-10-01_PSTRYK_PV_EV_ACTIVATION/sell-start-hotfix/grid-power-hotfix/`.
Dokładny następny SHA, manifesty, kopie i odbiory zapisuje osobny podkatalog
`retarget-proof-hotfix`, poza historycznym odbiorem `a732b2f`.
