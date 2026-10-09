# Pstryk — brak startu sprzedaży 1 października

Na installation_3 i installation_2 plan sprzedaży od 18:00 był aktualny i dopuszczał
start, lecz nie powstała transakcja ani komenda. Falowniki pozostały w trybie
0, Supervisor bez właściciela, kandydat RCE miał `available=false`.

Publikacja wspólnego planu Pstryka pomijała dwa pola zgodności wymagane przez
istniejące szablony HA: `bms_discharge_power_limit_kw` oraz `minimum_soc`.
W rezultacie nie były dostępne czujniki mocy i minimalnego SOC, a bramka
`hoymiles_rce_control_data_ready` poprawnie zabraniała wykonania.
Nie był to problem stabilizacji falownika ani rampy sieci.

Naprawa publikuje limit BMS z najnowszego wejścia po rewalidacji, używając
tej samej funkcji fizycznej i przeliczenia AC co RCE. Publikuje także nazwę
`minimum_soc` dla już obliczonej, chronionej granicy SOC. Limity, wybór okien,
ochrona domu, zgody użytkownika, GCF i ścieżka pełnego zapisu 4300–4306 pozostają
dotychczasowe. Brak danych oraz BMS=0 nadal odbierają gotowość.

Wcześniejszy test podawał gotową moc i SOC, przez co omijał błędne szablony.
Nowy test uruchamia runtime Pstryka, publikuje wynik, wylicza oba łańcuchy
szablonów HA wraz z dostępnością i dopiero buduje kandydata Supervisora.
Zapisano osobne RED dla BMS i SOC. Odtworzenie odczytów obu instalacji z 18:15
potwierdza zmianę bramki z false na true po uzupełnieniu publikacji; to test
offline, nie dowód fizycznego startu.

Dowody: `PRIVATE_EVIDENCE/2026-10-01_PSTRYK_PV_EV_ACTIVATION/sell-start-hotfix`
oraz `master-economics/m01-installation_3-20261001` w tym samym katalogu raportów.
Odbiór naturalnego startu o 18:00 na dotychczasowym SHA: **FAIL / HOLD**.
Po naprawie wymagany nowy odbiór fizyczny na dokładnym wdrożonym pakiecie.

Nie dodano encji, zapytań do Recordera ani timerów. Pięć małych pól publikacji
uzupełnia istniejący cykl; nie zmieniono retencji ani historii.

Decyzja użytkownika po diagnozie: wdrożyć tę naprawę razem z poprawką okien PV
na wszystkich trzech hostach w jednym pakiecie. installation_1 zachowuje zero export.
