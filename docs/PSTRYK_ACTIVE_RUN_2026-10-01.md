# Podtrzymanie zaakceptowanego cyklu Pstryk jak w RCE

## Potwierdzona luka

RCE posiada `retain_active_rce_slot` i zachowuje pierwotną podstawę aktywnego
cyklu (`_active_run_basis`). Wcześniejsze Pstryk BUY/SELL używało wspólnego
wyboru bloków i sterownika, ale nie tego mechanizmu. `continuing_action`
obniżało jedynie próg ponownego wymagania zysku. Aktualny nowy wynik mógł
przenieść całą sprzedaż na późniejszy przedział i wycofać bieżący slot.

Na installation_3 pakiet `03a5826` sprzedawał fizycznie, lecz o 18:01:19 i 18:03:00
UTC zakończył transakcje z `authorization_lost` / `no_current_plan`.
W chwili STOP były potwierdzone odczyty Mode 5 oraz eksport około 5,1 kW.
Nowy plan pozostawiał sprzedaż na jutro. Po powtórzeniach EMS wstrzymano;
Mode 0 i brak właściciela potwierdzono, a diagnostyka zgłosiła osobny ślad
`control_lease_lost:local_soft_deadline`. M01 installation_3 pozostaje **FAIL/HOLD**.
Brak podtrzymania jest potwierdzoną luką kodu, ale nie przypisujemy mu bez
dowodu każdej wcześniejszej awarii lease lub retarget.

## Minimalne ponowne użycie istniejącej naprawy

- Pstryk przekazuje zaakceptowaną i nową alokację do istniejącej funkcji
  `retain_active_rce_slot`; kod RCE pozostaje wspólnym źródłem reguł.
- Podstawa jest związana z transakcją, terminem, rewizją rynku/profilu i
  opcjami. Nie przesuwa się po każdym przeliczeniu ani na granicy półgodziny.
- RCE ponownie sprawdza dowód fizyczny, nieupłyniętą część energii, moc,
  SOC/rezerwę, BMS, GCF, blokady godzin, cenę i niezmienny termin.
- Następnie Pstryk sprawdza ten ograniczony wybór we wspólnym bilansie
  BUY/SELL, także przy niższej prognozie PV. Eksport nie zwiększa importu
  dla domu ani zaplanowanego zakupu. Podtrzymanie nie tworzy nowego START.
- Nowy sąsiedni blok nie przedłuża starej transakcji. Zmiana zabezpieczeń
  nadal może zmniejszyć moc albo odebrać podtrzymanie.
- Jest to ograniczona symulacja stałego wyboru, bez nowego przeszukiwania.
  Podstawa cyklu jest tylko w pamięci; brak nowych encji, zapytań Recorder,
  zapisów historii i zwiększenia kadencji. Publikowany jest istniejący
  znacznik RCE `active_slot_commitment_applied` oraz nazwa metody.

## Walidacja i granice odbioru

Nowe testy obejmują ekonomiczne przesunięcie bieżącej sprzedaży, minuty
5/31/65/95/119, nieodnawianie budżetu, limit mocy, termin, brak lub stary dowód,
zero BMS/GCF, rezerwę, wzrost LOAD, nieopłacalną cenę i zmianę kosztu zużycia.
Test prawdziwej publikacji HA potwierdza zachowanie pierwotnej podstawy i
zgodną rewizję obu planów. Regresje istniejącego podtrzymania RCE przechodzą.

PASS offline: 3 nowe testy podtrzymania, 26 testów runtime, 34 joint,
8 parytetu, 30 stabilizacji (w tym powtórzony runtime), test sterownika
150-sekundowej stabilizacji, 3 zestawy wcześniejszego podtrzymania RCE,
macierz 488 scenariuszy i 4 negatywne kontrakty BMS.

installation_2 na `03a5826` utrzymał transakcję od 17:51:47 do dokładnego terminu
18:00:00 UTC. Mode 0 bez właściciela potwierdzono także po 17 minutach.
To poprawne krótkie zakończenie, **PARTIAL**, nie pełny 30-minutowy M01.
Naprawa wykresu jest osobnym commitem opisana w
`docs/CANONICAL_CURRENT_SALE_2026-10-01.md`.

Wdrożenie dokładnego pakietu i odbiór terenowy nowego kodu są osobnymi bramkami.
Pełnej stabilności przyszłych okien opóźnienia PV jeszcze nie potwierdzono.
Dowody: `PRIVATE_EVIDENCE/2026-10-01_PSTRYK_PV_EV_ACTIVATION/`
`sell-start-hotfix/grid-power-hotfix/retarget-proof-hotfix/chart-coverage-hotfix/`.
# Follow-up: decreasing home reserve during a confirmed run

The first field run after `7d2889b` still stopped early. Offline reproduction
then found that a freshly calculated reserve below the already confirmed 4305
target made native `retain_active_rce_slot` reject an otherwise feasible run.
The shared RCE fixed-schedule simulation now keeps at least the confirmed SOC
floor before testing the remaining energy. It does not lower the physical
floor, increase the original power/energy allowance, or extend the deadline.
The Pstryk joint-balance regression covers decreasing demand with nonzero LOAD;
the existing stale-proof, BMS, GCF, price, reserve and deadline vetoes remain.
This offline result does not convert the failed installation_3 run into M01 PASS.
