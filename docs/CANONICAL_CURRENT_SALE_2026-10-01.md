# Bieżąca sprzedaż znika z wykresu EMS

## Dowód i przyczyna

installation_2, 1 października 2026, 17:52 UTC, pakiet `03a5826`: fizyczny Mode 5,
potwierdzona transakcja RCE/Pstryk i wybrany eksport w timeline RCE, ale
bieżący slot planu kanonicznego pokazywał `selected_action=none`.
Timeline RCEm zaczynał się od następnej północy, ponieważ jego prognoza
rezerwy miejsca dotyczyła jutra. Wymóg pokrycia bieżącego przedziału przez
wszystkie trzy timeline odrzucał także poprawny plan sprzedaży.

W tej samej ramce bieżący kandydat RCEm był aktualny, dostępny, bez
przeliczenia, aktywnego sterowania i blokad, z jawnym `no_action`.
Brak przyszłej prognozy dla dzisiejszego przedziału nie oznaczał więc
nieznanego bieżącego stanu regulatora.

## Minimalna naprawa

Tylko brak bieżącego odcinka RCEm może zostać rozstrzygnięty przez jawny
brak działania w aktualnej ramce. Stan źródła jest ponownie sprawdzany
istniejącym adapterem RCEm w chwili publikacji, z jego niezmiennym limitem
świeżości 60 sekund. Bieżące timeline RCE i taryfy nadal muszą pokrywać
przedział, a rzeczywiste blokady wykonania nadal obowiązują.

Nie uzupełniamy brakującej prognozy RCEm zerami ani energią z innego źródła.
Jej kandydat pozostaje niedostępny. Poprawka dotyczy wyłącznie projekcji
wykresu i wspólnego planu EMS, dla RCE i Pstryka. Nie daje uprawnień
sterowania, nie zmienia optymalizacji, BMS, GCF, lease ani terminów.
Nie dodaje encji, pól historii, zapytań Recorder ani częstotliwości zapisu.

## Walidacja

- RED: bieżąca sprzedaż znika przy jutrzejszym początku prognozy RCEm.
- GREEN: sprzedaż wraca do planu, przepływ bateria–sieć jest dodatni,
  SOC spada, bilans energii jest zachowany.
- Nieznany, przyszły, nieaktualny, przeliczany lub aktywny RCEm nadal
  blokuje uzupełnienie; także stan, który wygasł pomiędzy ramką a publikacją.
- Brak bieżącego RCE nadal blokuje projekcję; zero export nadal blokuje
  sprzedaż. Blokada rzeczywistego kandydata nie znika po ponownym odczycie.
- Canonical runtime: 133 sprawdzenia PASS. Dual track: 4887 PASS.
  Ledger: PASS, 22/22 mutacje wykryte. Projekcja opóźnienia PV: 3 testy PASS.

Dowody: `PRIVATE_EVIDENCE/2026-10-01_PSTRYK_PV_EV_ACTIVATION/`
`sell-start-hotfix/grid-power-hotfix/retarget-proof-hotfix/chart-coverage-hotfix/`.
Manifest i odbiór wdrożenia są osobne od tej walidacji offline.
