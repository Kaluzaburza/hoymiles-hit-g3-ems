# Profile opóźnienia ładowania PV

Aktualizacja 6.10.2026: **Maksymalny — 20% P10, Zrównoważony — 50%,
Zachowawczy — 55%**. W opcjach interfejsu widoczne są wyłącznie nazwy profili.
Domyślny pozostaje **Zachowawczy**; zapisany wybór jest zachowany. Wagi
70/60/50% z 2.10 (a wcześniej 85/75/65%) stanowią wyłącznie historię.
Nowy kandydat ma osobne RED/GREEN, SHA i receipt w
`<PRIVATE_EVIDENCE>/2026-10-06_PV_PROFILES`. Autoryzowane wdrożenie
dotyczy tylko Instalacji 3; pozostałe instalacje i publikacja pozostają HOLD.
Nowy helper ma tę opcję pierwszą i nie ma `initial`, dzięki czemu kolejne
restarty odtwarzają zapisany wybór. Aurora pokazuje polskie/angielskie nazwy;
wewnętrzne opcje są stabilne: Conservative, Balanced, Maximum.

Waga oznacza mieszankę `P50 * (1-w) + P10 * w`, nie prawdopodobieństwo ani
procent obcięcia produkcji. Dotyczy wyłącznie późniejszego odzyskania energii
po eksporcie PV. Ochrona domu, prognoza sprzedaży baterii, zakup, BMS, GCF,
zero export, STOP, fizyczny odczyt, lease i 180 s stabilizacji pozostają.
Wszystkie profile wymagają świeżego, prawidłowego P10 dla właściwego dnia,
sprawdzenia dokładnego okna i odzyskania pełnego zapasu w dotychczasowym
terminie. To prognoza, nie gwarancja pełnego naładowania przy dowolnej pogodzie.

Zmiana profilu unieważnia plan RCE/Pstryk i uruchamia zwykłe przeliczenie.
Profil jest częścią porównania opcji Pstryka, także gdy P10 i P50 są identyczne.
Brakujący lub nieznany profil nie zezwala na opóźnienie; pozostałe planowanie
BUY/SELL działa według dotychczasowych reguł. Nie dodano historii, modelu,
Store, harmonogramu zapytań ani zapisów Modbus. Dwa stałe pola diagnostyczne
publikują wybrany profil i wagę. Jeden restorable helper zapisuje zmianę wyboru.

Historyczny test poprzednich wag: na zapisanych wejściach installation_3 z 02.10,
06:11 CEST, wariant 85% nie ma okna,
75% daje 08:30–10:30, a 65% 08:30–11:00. Sztywne 09:00–12:00 nie przechodzi
żadnego z tych trzech marginesów. To rekonstrukcja offline zaokrąglonych
wejść, nie obietnica bieżącego planu. Dnia 30.09 pomiary pokazują około
12,3 kWh eksportu 09:00–12:00 przy SOC 10%, później 99% od około 14:15.
Rano prognozowano P50/P10 około 56,44/42,14 kWh, wobec 48,41/18,65 kWh
w odczycie 02.10. Nie można przenieść pewności wcześniejszego dnia na dzisiaj.
Historyczny tryb 7 i SOC 10% nie są odbiorem naszego Mode 5/SOC+1 ani
potwierdzeniem zachowania dzisiejszej automatycznej rezerwy 13%.

Dowody: `PRIVATE_EVIDENCE/2026-10-02_PV_DELAY_REPAIR/installation_3-pv-window-audit`.
Walidacja profili, wdrożenie dokładnego SHA i naturalny odbiór fizyczny mają
osobne wyniki w katalogu `profiles` tej samej paczki raportów.
