# RCE/Pstryk: podtrzymanie transakcji a dostarczanie FC03

## Stan i dowody

Baza: `7d2889b39a986e4f1892a143407b1535c3abd15e`. Ta wersja zawierała
podłączenie natywnego `retain_active_rce_slot` do Pstryka i korektę bieżącej
projekcji wykresu przy jutrzejszym timeline RCEm. 108 hashy potwierdzono na
installation_1, installation_2 i installation_3. Sama zgodność wdrożenia nie oznacza M01 PASS.

installation_3 ponownie przerwał sprzedaż 1 października:

- `rce:58930467d8864264be211807fede403d`: komenda 18:35:35 UTC, STOP
  18:36:05 UTC; `authorization_lost`, `no_current_plan`, potwierdzony Mode 5,
  około 4,75 kW eksportu, termin transakcji 19:00 UTC.
- `rce:4816d1e7ad2d4ff1ae88aee2a4891bd7`: komenda 18:36:51 UTC, STOP
  18:37:41 UTC; ten sam powód, około 4,79 kW eksportu.
- Trzecia transakcja zbiegła się z techniczną pauzą. Nie klasyfikujemy jej
  przerwania jako niezależnego dowodu błędu planowania.

EMS został wstrzymany. Przywrócone fizyczne nastawy były poprawne, ale zapis
odzyskiwania pozostał w błędzie. Restart nie skasował go. Oficjalny MASTER STOP
potwierdził neutralny stan i zwolnił właściciela; po dokończeniu potwierdzenia
i rearm przywrócono wcześniejsze zgody RCE/taryfy, pozostawiając pauzę.
Nie kasowano ani nie podmieniano magazynu transakcji.

## Dwa odtworzone mechanizmy

1. Niższa nowa prognoza zużycia dawała niższy próg SOC niż potwierdzony 4305.
   Natywne podtrzymanie odrzucało wtedy bieżący slot. Osobna poprawka
   `3330750` symuluje pozostały budżet przy zachowaniu wyższego progu.
2. Zakończenie obliczeń podczas 100 ms składania odczytu FC03 dawało chwilowo
   `None` z istniejącego czytnika potwierdzonej transakcji. Nowy plan bez
   bieżącej sprzedaży stawał się od razu aktualny i sterownik kończył eksport.
   Test z produkcyjnym Supervisorem, szablonami HA i modelem dzierżawy odtwarza
   `authorization_lost/no_current_plan` w 22. sekundzie, przy poprawnym Mode 5
   i potwierdzonym eksporcie. Wolniejszy wariant obliczeń przechodził.

To odtworzenie mechanizmu zgodnego z objawem. Nie jest pełnym odtworzeniem
historycznej kolejności callbacków installation_3 ani dowodem jednej wyłącznej
przyczyny wszystkich wcześniejszych STOP.

## Zmiana i kontrakt

RCE oraz Pstryk korzystają z tego samego oczekiwania na zakończenie istniejącej
kohorty FC03: maksymalnie 0,5 s, bez blokowania pętli HA. Po oczekiwaniu
ponownie pobierane są wejścia i sprawdzane rewizje, ustawienia, fizyka oraz
świeży dowód transakcji. Nierozstrzygnięta kohorta nie publikuje nowego planu
bez bieżącej sprzedaży. Dotychczasowe ograniczenia aktualności pozostają.

Podtrzymanie wymaga nadal potwierdzonego Mode 5, tej samej dzierżawy i ID,
świeżego dowodu, poprawnych BMS/GCF, energii dla domu i opłacalności. Moc,
pozostała energia oraz termin nie mogą przekroczyć pierwotnej transakcji.
Pauza, STOP, zerowe BMS/GCF, brak świeżości i faktycznie niewykonalny plan
nadal odbierają zgodę. Nie zmieniono timeoutów ESP, transportu ani ramp.

Brak nowych encji, atrybutów Recorder, zapytań historii i zapisów Store.
Oczekiwanie dotyczy publikacji wyniku; nie uruchamia nowego optymalizatora.

## Walidacja offline

- RED→GREEN: spadek wymaganego SOC przy niezerowym LOAD; kończący się podczas
  FC03 wykonawca Pstryka (1 s).
- Pstryk: 270 s wirtualnego czasu, co najmniej dwa pełne przeliczenia,
  oryginalna podstawa planu, ta sama transakcja i deadline, bez restore;
  warianty wykonawcy 1 s i 4 s. Świeże BMS=0 kończy zgodę.
- RCE: oba warianty publikacji, aktywny slot, przebieg przez granice slotów,
  fizyczne veto oraz oddzielny przebieg publikacji i lease.
- Pstryk runtime: 29 testów HA/Store, w tym ograniczone oczekiwanie,
  ponowny odczyt zmienionego BMS i brak nowej publikacji przy timeout.
- Pstryk joint/parity, RCE optimizer (85 scenariuszy), historia RCE,
  executor/startup, macierz automatyki i sterownik (244 sprawdzenia): PASS.

Szczegółowe logi i manifesty hostów:
`PRIVATE_EVIDENCE/2026-10-01_PSTRYK_PV_EV_ACTIVATION/sell-start-hotfix/`
`grid-power-hotfix/retarget-proof-hotfix/chart-coverage-hotfix/cohort-floor-hotfix/`.
Historyczny walidator zamrożonego wydania pozostaje osobną bramką; jego
asercji nie osłabiono. Dokładny wynik po czystym commicie zapisuje paczka.

## Odbiór

Nowa paczka wymaga osobnych hashy, startu HA i obserwacji każdego hosta.
installation_1 musi zachować GCF=1 i limit eksportu=0. Odbiór M01 installation_3 z
`7d2889b` ma wynik FAIL/HOLD. Po tej poprawce wymagany jest nowy naturalny
przebieg: stabilizacja, co najmniej 30 min jednej transakcji, przeliczenia,
granica bloku i zakończenie +2/+10 min. Przyszłe okna opóźnienia PV oraz
wykres podczas nowej fizycznej sprzedaży mają osobne, nadal otwarte odbiory.
