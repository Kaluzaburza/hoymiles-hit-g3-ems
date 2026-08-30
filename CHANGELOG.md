# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

## [1.5.8] - 2026-08-24

> **Release gate / Bramka wydania:** publish this hotfix only after the exact
> candidate commit passes the complete offline suite, exact-version 30-minute
> field acceptance, required CI, HACS Action and Hassfest; the unchanged
> exhaustive automation matrix must report **2064/2064** scenarios. Offline
> evidence and one installation-specific field run do not replace electrical
> commissioning, certified protection or manufacturer declarations.
>
> Hotfix publikuj wyłącznie po przejściu przez dokładny commit kandydata pełnego
> zestawu offline, 30-minutowej akceptacji terenowej exact-version, wymaganego
> CI, HACS Action i Hassfest; niezmieniona pełna macierz automatyzacji musi
> zgłosić **2064/2064** scenariuszy. Dowody offline oraz test jednej instalacji
> nie zastępują odbioru elektrycznego, certyfikowanych zabezpieczeń ani
> deklaracji producenta.

### User update steps / Kroki po aktualizacji

1. **Safety / Bezpieczeństwo:** disable RCE and every other automatic EMS
   writer before the update. Confirm no discharge/charge cycle is active,
   **Self-Use** is physically read back and there is no active control owner.
   **PL:** przed aktualizacją wyłącz RCE i wszystkie pozostałe automatyczne
   zapisy EMS. Potwierdź brak aktywnego cyklu rozładowania/ładowania, fizyczny
   odczyt **Self-Use** oraz brak aktywnego właściciela sterowania.
2. **HACS:** update **EMS for Hoymiles HIT-(5–20)L-G3** to **v1.5.8**.
   Keep every writer disabled until the verification step passes.
   **PL:** zaktualizuj w HACS integrację
   **EMS for Hoymiles HIT-(5–20)L-G3** do **v1.5.8**. Do zakończenia
   weryfikacji pozostaw wszystkie automatyczne zapisy wyłączone.
3. **Home Assistant:** the empirically verified standard install/update path
   requires exactly **two Home Assistant restarts** after HACS finishes.
   Restart 1 loads the v1.5.8 Python runtime and installs/upgrades an unchanged
   managed scheduler; validate the configuration, then restart 2 to load the
   v1.5.8 package automations (and to expose **/local** after a fresh **www**
   directory was created). The installer deliberately preserves a locally
   modified scheduler. Back up and explicitly rebase/replace such a file with
   the canonical v1.5.8 package between the two restarts; otherwise v1.5.8 RCE
   lifecycle behavior is not active and all writers must remain disabled.
   **PL:** empirycznie zweryfikowana standardowa instalacja/aktualizacja wymaga
   dokładnie **dwóch restartów Home Assistanta** po zakończeniu aktualizacji
   HACS. Restart 1 ładuje runtime Python v1.5.8 i instaluje/aktualizuje
   niezmieniony zarządzany scheduler; sprawdź konfigurację, a następnie wykonaj
   restart 2, aby załadować automatyki pakietu v1.5.8 (oraz udostępnić
   **/local**, gdy katalog **www** powstał dopiero podczas pierwszego startu).
   Instalator celowo zachowuje lokalnie zmodyfikowany scheduler. Wykonaj jego
   kopię i jawnie przenieś zmiany na kanoniczny pakiet v1.5.8 albo zastąp plik
   między restartami; w przeciwnym razie poprawiony lifecycle RCE v1.5.8 nie
   jest aktywny i wszystkie automatyczne zapisy muszą pozostać wyłączone.
4. **ESP32 / ESPHome:** No ESP32 / ESPHome rebuild or upload is required for
   v1.5.8. Firmware from the last compatible immutable tag **v1.5.6** remains
   valid, and HACS does not flash ESP32.
   **PL:** Wersja v1.5.8 nie wymaga ponownej kompilacji ani wgrywania firmware
   ESP32 / ESPHome. Firmware z ostatniego zgodnego, niezmiennego tagu
   **v1.5.6** pozostaje właściwe, a HACS nie wgrywa ESP32.
5. **Verification / Weryfikacja:** confirm integration and managed package
   version **1.5.8**, no relevant Repair/error, physical **Self-Use**, no active
   owner/conflict and no unexpected register write before re-enabling policies.
   For a configured HIT-20L, confirm diagnostics report nameplate
   **inverter_nameplate_power_each_kw=20**, battery-effective
   **inverter_power_each_kw=16**, the correct **system_power_kw** and
   **requested_export_power_kw**. Only then restore the policies that were
   enabled before the update.
   **PL:** przed ponownym włączeniem polityk potwierdź wersję integracji i
   zarządzanego pakietu **1.5.8**, brak istotnych Napraw/błędów, fizyczny
   **Self-Use**, brak aktywnego ownera/konfliktu i brak nieoczekiwanego zapisu
   rejestru. Dla skonfigurowanego HIT-20L sprawdź diagnostykę:
   **inverter_nameplate_power_each_kw=20**,
   **inverter_power_each_kw=16**, właściwe **system_power_kw** oraz
   **requested_export_power_kw**. Dopiero wtedy przywróć polityki aktywne przed
   aktualizacją.

### Fixed / Naprawiono

- RCE now separates pre-ACK start authority from post-ACK continuation. A new
  cycle still requires **current_slot_start_eligible=true**; an already active,
  physically acknowledged cycle no longer rolls back merely because that
  new-start condition expires. Continuation still requires a planned slot,
  continuation eligibility, a valid latched run/slot relationship, exclusive
  RCE ownership, matching physical mode/readback, fresh critical inputs and
  every existing hard stop.
- A newly current optimizer result may precede its dependent execution sensors
  by one event. An active, physically acknowledged RCE cycle may bridge only
  that specific cohort transition for at most **5 seconds** while
  **control_data_ready=off**. The bridge grants no authority, performs zero
  writes, does not repeat Mode 5 or 4306, does not change the latched target
  and ends immediately on readiness, timeout, missing/unavailable data,
  conflict, mode mismatch, loss of plan/continuation or any hard stop.
- The start verifier keeps the transaction-frozen slot, percentage and expected
  battery-power target. It collects six newer complete aggregate generations
  and evaluates the four exact three-sample windows **[1,2,3]**, **[2,3,4]**,
  **[3,4,5]**, **[4,5,6]** within the accepted **155-second** horizon. One
  isolated sampled transition peak remains best-effort diagnostics and cannot
  poison a separate clean window. Two consecutive central peaks, persistent
  mismatch, missing export and absence of a stable window still fail closed
  through the existing neutral rollback.
- Active 4306 retuning now freezes the requested value and the physical
  4300–4305 tuple for the complete verification attempt. If the verified
  helper sees no newer FC03 generation during its existing 60-second wait, the
  existing second minute may observe telemetry recovery. A newer physical
  Mode 5 generation that preserves 4300–4305 and confirms a positive 4306 no
  higher than both the frozen and current safe targets is conservative
  under-command: the active transaction stays in place and the update is
  retried by the next normal trigger. Any fresh generation that mismatches
  during the helper wait, missing recovery, stale data, over-command,
  unconfirmed reduction or hard stop still performs the complete rollback.
  The maximum two-minute horizon, full-block helper and physical-ACK contract
  are unchanged; command echo is never accepted as ACK.
- The configured HIT-20L nameplate remains **20 kW**. Its battery-only RCE
  model now uses **16 kW per inverter**, so two units provide a **32 kW**
  battery base and the requested RCE percentage applies to that base. This
  calibration is not a hardware AC power cap and does not constrain register
  4306. PV and LOAD remain separate in the physical balance, total
  instantaneous AC power may exceed 16 kW per inverter with PV, and charging
  power is unchanged. The 5/10/12/15 kW profiles are unchanged.
- Battery balancing now retains normal BMS-safe charging below **95% SOC** and
  latches an aggregate net target of approximately **0.4 kW** from the first
  valid reading at or above 95%. The full-SOC hold still starts only at
  **99.9%** after physical mode/4303/4304 acknowledgement. Self-Use uses the
  direct battery cap; Grid Charge adds fresh household load exactly once.
  Downward 0.1% quantization never overrides the BMS cap, and unavailable data
  cannot become a positive physical command. A freshness-only Self-Use gap
  pauses writes for at most 60 seconds without start/stop churn. Dedicated,
  bounded FIFO lanes freeze trigger evidence and cycle identity per hard-stop
  priority class, then merge the durable request; a lower-priority burst cannot
  consume admission for the later higher-priority reason, and the first exact
  reason wins at equal priority. The capture path does not wait for the fault to
  remain active. It cannot cancel an already-running verified helper, but the
  durable request blocks later non-restorative writes and restoration starts at
  the first safe serialized boundary. Both snapshot generations require exact
  equality before the first write, and SOC is freshly re-read before
  `HOLD_ARMING` and again across its timing-write boundary. The final
  steady-phase commit rechecks current sun, physical mode, owner generation and
  hard-stop state; it permits at most one verified mode correction and fails
  closed on another sun change. A guarded steady record persists raw `APPLYING`
  plus a conditional phase/mode token, so the canonical parser exposes an
  accepted steady phase only while all live commit guards remain true. The abort retry
  cooldown is 15 minutes. Only a trusted current-cycle snapshot is restored,
  with matching physical ACK and Off-Grid priority. An unverifiable active
  legacy cycle instead remains owned in `RECOVERY_REQUIRED` for the documented
  manual recovery procedure, with no guessed physical write. Terminal events
  use a durable outbox and a dispatcher outside physical closeout. The physical
  `operational_started` fact does not depend on free outbox capacity. A provider
  attempt has a durable 15-second lease; timeout/restart enables a bounded retry,
  late completion is inert, and the stable phone tag is reused. This
  prevents deterministic scheduler duplicates but does not claim mathematical
  exactly-once delivery by an arbitrary external provider. The closed status
  allowlist still suppresses routine phase pushes. This behavior is validated
  offline only and still requires exact-version real-inverter acceptance before
  publication.
- The integrated candidate also carries the observation-only Aurora AP-2
  timelines for RCE, tariff and RCEm, including their exact registration,
  lifecycle and source-convergence contracts. Exact-commit AP-2 live
  acceptance remains frozen and incomplete. AP-3A is a design freeze only;
  the AP-3B custom card and dashboard view are not implemented, and no public
  v1.5.8 release exists yet.
- The integration changes no ESPHome/firmware source, Modbus map, full-block
  write semantics, GCF policy or certified protection setting and adds no
  physical execution authority. Master FC03 remains acknowledgement of Master
  configuration only; no per-Slave protocol acknowledgement is claimed.

- RCE rozdziela teraz autoryzację startu przed ACK od kontynuacji po ACK. Nowy
  cykl nadal wymaga **current_slot_start_eligible=true**; aktywny, fizycznie
  potwierdzony cykl nie wykonuje rollbacku tylko dlatego, że wygasł warunek
  nowego startu. Kontynuacja nadal wymaga planowanego slotu, kwalifikacji
  kontynuacji, ważnej relacji zatrzaśniętego cyklu/slotu, wyłącznej własności
  RCE, zgodnego fizycznego trybu/readbacku, świeżych danych krytycznych i
  wszystkich dotychczasowych hard-stopów.
- Nowy aktualny wynik optymalizatora może zostać opublikowany jedno zdarzenie
  przed zależnymi sensorami wykonawczymi. Aktywny, fizycznie potwierdzony cykl
  RCE może podtrzymać wyłącznie to konkretne przejście kohorty przez maksymalnie
  **5 sekund**, gdy **control_data_ready=off**. Bridge nie nadaje authority,
  wykonuje zero zapisów, nie ponawia Mode 5 ani 4306, nie zmienia
  zatrzaśniętego celu i kończy się natychmiast po gotowości, timeoutcie,
  braku/niedostępności danych, konflikcie, błędnym trybie, utracie
  planu/kontynuacji lub dowolnym hard-stopie.
- Weryfikator startowy zachowuje zatrzaśnięty dla transakcji slot, procent i
  oczekiwaną moc baterii. Zbiera sześć nowszych kompletnych generacji
  sumarycznych i ocenia cztery dokładne trzypróbkowe okna **[1,2,3]**,
  **[2,3,4]**, **[3,4,5]**, **[4,5,6]** w zaakceptowanym horyzoncie
  **155 sekund**. Pojedynczy próbkowany skok przejściowy pozostaje diagnostyką
  best-effort i nie zatruwa osobnego czystego okna. Dwa kolejne centralne skoki,
  trwała niezgodność, brak eksportu i brak stabilnego okna nadal kończą się
  fail-closed przez istniejący neutralny rollback.
- Aktywna korekta 4306 zatrzaskuje teraz żądaną wartość oraz fizyczny blok
  4300–4305 na cały przebieg weryfikacji. Jeżeli helper w istniejącym
  60-sekundowym oczekiwaniu nie zobaczy żadnej nowszej generacji FC03, druga
  istniejąca minuta może potwierdzić powrót telemetrii. Nowsza fizyczna
  generacja Mode 5, która zachowuje 4300–4305 i potwierdza dodatnie 4306 nie
  większe od zatrzaśniętego oraz aktualnego bezpiecznego celu, oznacza
  zachowawcze under-command: aktywna transakcja pozostaje, a zapis zostanie
  ponowiony przy kolejnym zwykłym triggerze. Świeża generacja niezgodna już
  podczas oczekiwania helpera, brak powrotu, dane stale, over-command,
  niepotwierdzone obniżenie lub hard-stop nadal wykonują pełny rollback.
  Maksymalny dwuminutowy horyzont, helper pełnego bloku i kontrakt fizycznego
  ACK pozostają bez zmian; command echo nigdy nie jest uznawane za ACK.
- Skonfigurowana moc znamionowa HIT-20L nadal wynosi **20 kW**. Model RCE
  rozładowania wyłącznie z baterii używa teraz **16 kW na falownik**, więc dwie
  jednostki dają bateryjną bazę **32 kW**, do której stosowany jest procent RCE.
  Kalibracja nie jest sprzętowym ograniczeniem mocy AC i nie ogranicza rejestru
  4306. PV i LOAD pozostają osobnymi składnikami bilansu, chwilowa całkowita moc
  AC z PV może przekroczyć 16 kW/falownik, a moc ładowania się nie zmienia.
  Profile 5/10/12/15 kW pozostają bez zmian.
- Wyrównywanie baterii zachowuje teraz normalne, bezpieczne dla BMS ładowanie
  poniżej **95% SOC** i od pierwszego prawidłowego odczytu co najmniej 95%
  zatrzaskuje sumaryczny cel netto około **0,4 kW**. Podtrzymanie pełnego SOC
  nadal zaczyna się dopiero przy **99,9%**, po fizycznym ACK trybu oraz rejestrów
  4303/4304. Self-Use używa bezpośredniego limitu baterii, a Grid Charge dodaje
  świeże zużycie domu dokładnie raz. Kwantyzacja w dół do 0,1% nigdy nie
  przekracza limitu BMS, a niedostępne dane nie mogą stać się dodatnim poleceniem
  fizycznym. Luka wyłącznie w świeżości danych w Self-Use wstrzymuje zapisy
  najwyżej przez 60 sekund bez pętli start/stop. Osobne, ograniczone kolejki FIFO
  zamrażają dowód triggera i tożsamość cyklu dla każdej klasy priorytetu
  hard-stop, a następnie scalają trwałe żądanie; seria niższego priorytetu nie
  zajmuje przyjęcia późniejszej przyczyny wyższej, a przy równym priorytecie
  wygrywa pierwszy dokładny powód. Ścieżka przechwycenia nie czeka, aż stan błędu
  pozostanie aktywny. Nie anuluje już trwającego zweryfikowanego helpera, lecz
  trwałe żądanie blokuje późniejsze zapisy inne niż odtwarzanie, które zaczyna
  się na pierwszej bezpiecznej granicy serializowanej. Obie generacje migawki
  wymagają dokładnej równości przed pierwszym zapisem, a SOC jest świeżo
  odczytywany przed `HOLD_ARMING` i ponownie na granicy zapisu jego timingu.
  Końcowy commit fazy stabilnej ponownie sprawdza bieżące słońce, tryb fizyczny,
  generację właściciela i hard-stop; dopuszcza najwyżej jedną zweryfikowaną
  korektę trybu i kończy się fail-closed po kolejnej zmianie słońca. Chroniony
  rekord fazy stabilnej utrwala surowe `APPLYING` z warunkowym tokenem
  fazy/trybu, więc parser kanoniczny pokazuje zaakceptowaną fazę tylko wtedy,
  gdy wszystkie bieżące guardy commitu nadal są prawdziwe. Cooldown po
  przerwaniu wynosi 15 minut. Odtwarzana jest tylko zaufana migawka bieżącego
  cyklu, z pasującym fizycznym ACK i pierwszeństwem Off-Grid. Nieweryfikowalny
  aktywny stary cykl pozostaje natomiast własnością balansowania w
  `RECOVERY_REQUIRED` do udokumentowanej procedury ręcznej, bez zgadywanego
  zapisu fizycznego. Zdarzenia terminalne używają trwałej kolejki i dispatchera
  poza fizycznym zamknięciem. Fizyczny fakt `operational_started` nie zależy od
  wolnego miejsca w outboxie. Próba dostawcy ma trwałą 15-sekundową dzierżawę;
  timeout/restart umożliwia ograniczone ponowienie, spóźnione zakończenie jest
  bezskuteczne, a stabilny tag telefonu jest ponownie używany. Zapobiega to
  deterministycznym duplikatom schedulera, ale nie stanowi obietnicy
  matematycznego exactly-once u dowolnego zewnętrznego dostawcy. Zamknięta lista
  statusów nadal usuwa zwykłe powiadomienia etapów. Zachowanie zweryfikowano
  wyłącznie offline i przed publikacją nadal wymaga odbioru exact-version na
  rzeczywistym falowniku.
- Zintegrowany kandydat zawiera także obserwacyjne osie czasu Aurora AP-2 dla
  RCE, taryfy i RCEm wraz z ich dokładnym kontraktem rejestracji, lifecycle i
  zbieżności źródeł. Akceptacja live dokładnego commitu AP-2 pozostaje
  zamrożona i nieukończona. AP-3A jest wyłącznie zamrożeniem projektu; karta i
  widok AP-3B nie są zaimplementowane, a publiczne wydanie v1.5.8 jeszcze nie
  istnieje.
- Integracja nie zmienia źródeł ESPHome/firmware, map Modbus, semantyki zapisu
  pełnego bloku, GCF ani certyfikowanych zabezpieczeń i nie dodaje fizycznego
  prawa wykonania. FC03 Mastera nadal potwierdza wyłącznie konfigurację
  Mastera; nie deklarujemy protokołowego ACK żadnego Slave'a.

## [1.5.7] - 2026-08-22

> **Status:** RCE GCF cohort stability hotfix. Publication is permitted only
> after the exact final commit passes the complete offline, localhost,
> `miernik.com.pl` and CI release gates.
>
> **Status PL:** hotfix stabilności kohorty GCF w RCE. Publikacja jest
> dozwolona dopiero po przejściu przez dokładny finalny commit pełnych bramek
> offline, localhost, `miernik.com.pl` oraz CI.

### User update steps / Kroki po aktualizacji

1. **HACS:** keep RCEm in shadow mode and disable every automatic or scheduled
   EMS writer. Update **EMS for Hoymiles HIT-(5–20)L-G3** to **v1.5.7**.
   **PL:** pozostaw RCEm w trybie shadow i wyłącz wszystkie automatyczne oraz
   harmonogramowe zapisy EMS. Zaktualizuj w HACS integrację
   **EMS for Hoymiles HIT-(5–20)L-G3** do **v1.5.7**.
2. **Home Assistant:** restart after HACS installs the update. The first start
   loads the new Python runtime and copies managed EMS package **1.5.7**;
   validate the configuration and perform the second restart requested by the
   managed-package Repair. Keep all writers disabled during both restarts.
   **PL:** uruchom Home Assistant ponownie po instalacji aktualizacji z HACS.
   Pierwszy start ładuje nowy runtime Python i kopiuje zarządzany pakiet EMS
   **1.5.7**; sprawdź konfigurację i wykonaj drugi restart wymagany przez
   komunikat Napraw pakietu. Podczas obu restartów pozostaw wszystkie zapisy
   wyłączone.
3. **ESP32 / ESPHome:** no firmware rebuild or upload is required. Firmware
   from the last compatible immutable tag **v1.5.6** remains valid; HACS does
   not flash the ESP32.
   **PL:** ponowna kompilacja ani wgrywanie firmware nie są wymagane. Firmware
   z ostatniego zgodnego, niezmiennego tagu **v1.5.6** pozostaje właściwe;
   HACS nie wgrywa ESP32.
4. **Verification / Weryfikacja:** confirm package **1.5.7 / Ready**, exactly
   **294** localized entities, no unexpected owner, mode or Modbus write, and
   no repeated RCE full-plan recalculation for an unchanged completed GCF
   cohort. Where fresh physical GCF readback proves enabled zero export,
   confirm `fixed_zero_export` and factor **0.80**.
   **PL:** potwierdź pakiet **1.5.7 / Gotowe**, dokładnie **294** lokalizowane
   encje, brak nieoczekiwanego właściciela, zmiany trybu lub zapisu Modbus oraz
   brak powtarzanego pełnego przeliczania planu RCE dla niezmienionej kompletnej
   kohorty GCF. Gdy świeży fizyczny odczyt GCF potwierdza włączony zerowy
   eksport, sprawdź `fixed_zero_export` i współczynnik **0,80**.

### Fixed / Naprawiono

- RCE now retains only the last coherent physical GCF cohort for a bounded
  maximum of five seconds while one three-part report finishes. The mixed
  intermediate state does not immediately invalidate the accepted plan.
- One newly completed semantic cohort is consumed at most once. An identical
  completed cohort does not advance the optimizer input revision or create
  `result_current` / pending churn.
- Persistent incoherence still expires fail-closed. Missing, stale, future,
  malformed or unsupported physical GCF readback remains untrusted.
- Exact enabled GCF plus `0.0%` physical export limit still selects
  `fixed_zero_export`, excludes restricted learning and uses exactly
  **0.80 × Solcast**.
- RCE economic optimization, scheduler, Modbus, firmware, tariff, RCEm,
  Aurora/dashboard/frontend and parallel communication behavior are unchanged.
  This release adds neither RCE market charging nor EMS Supervisor.

- RCE zachowuje ostatnią spójną fizyczną kohortę GCF tylko przez ograniczone
  maksimum pięciu sekund, gdy kończy się trzyczęściowy raport. Stan pośredni
  nie unieważnia natychmiast zaakceptowanego planu.
- Nowa kompletna kohorta semantyczna jest używana najwyżej raz. Identyczna
  kompletna kohorta nie zwiększa rewizji wejścia optymalizatora i nie powoduje
  migotania `result_current` / oczekiwania.
- Trwała niespójność nadal wygasa fail-closed. Brakujący, nieświeży, przyszły,
  wadliwy lub nieobsługiwany fizyczny odczyt GCF pozostaje niezaufany.
- Włączony GCF i dokładny fizyczny limit eksportu `0,0%` nadal wybierają
  `fixed_zero_export`, wykluczają uczenie z okresu ograniczenia i stosują
  dokładnie **0,80 × Solcast**.
- Ekonomia RCE, scheduler, Modbus, firmware, taryfa, RCEm,
  Aurora/dashboard/frontend i komunikacja równoległa pozostają bez zmian.
  Wydanie nie dodaje ładowania z rynku RCE ani EMS Supervisora.

## [1.5.6] - 2026-08-21

> **Status:** released; offline/CI PASS; live rollout accepted with a documented
> RCE evidence limit. The final simultaneous 15-minute window had stable RCE
> readiness but no planned active slot. The maintainer accepted publication and
> the next active-slot regression, if any, will be handled as a hotfix.
>
> **Status PL:** wydano; offline/CI PASS; wdrożenie live odebrane z jawną
> granicą dowodu RCE. Końcowe równoległe okno 15-minutowe miało stabilną
> gotowość RCE, ale bez zaplanowanego aktywnego slotu. Opiekun zaakceptował
> publikację, a ewentualna regresja następnego aktywnego slotu otrzyma hotfix.

### User update steps / Kroki po aktualizacji

1. **HACS:** keep RCEm in shadow mode and disable every automatic or scheduled
   EMS writer. Update **EMS for Hoymiles HIT-(5–20)L-G3** to **v1.5.6**.
   **PL:** pozostaw RCEm w trybie shadow i wyłącz wszystkie automatyczne oraz
   harmonogramowe zapisy EMS. Zaktualizuj w HACS integrację
   **EMS for Hoymiles HIT-(5–20)L-G3** do **v1.5.6**.
2. **Home Assistant:** restart after HACS installs the update. The first start
   copies managed EMS package **1.5.6**; validate the configuration and perform
   the second restart requested by the managed-package Repair. Keep all writers
   disabled during both restarts and hard-refresh every open Hoymiles dashboard
   so it loads frontend URL `v=1.5.6.24`.
   **PL:** uruchom Home Assistant ponownie po instalacji aktualizacji z HACS.
   Pierwszy start kopiuje zarządzany pakiet EMS **1.5.6**; sprawdź konfigurację
   i wykonaj drugi restart wymagany przez komunikat Napraw pakietu. Podczas obu
   restartów pozostaw wszystkie zapisy wyłączone i twardo odśwież każdą otwartą
   kartę dashboardu Hoymiles, aby wczytała URL frontendu `v=1.5.6.24`.
3. **ESP32 / ESPHome:** after the Home Assistant no-write check, rebuild and
   upload the firmware from the top-level ESPHome file pinned to immutable ref
   **v1.5.6**. This OTA is mandatory because the hotfix adds the complete
   aggregate-power readback generation used by the Home Assistant verifier.
   HACS does not flash the ESP32. Repeat the no-write check after the OTA.
   **PL:** po odczytowej kontroli Home Assistanta skompiluj i wgraj przez OTA
   firmware z głównego pliku ESPHome przypiętego do niezmiennego ref
   **v1.5.6**. Ten krok jest obowiązkowy, ponieważ hotfiks dodaje kompletną
   generację odczytu mocy sumarycznej używaną przez weryfikator Home Assistanta.
   HACS nie wgrywa ESP32. Po OTA powtórz kontrolę bez zapisów.
4. **Verification / Weryfikacja:** confirm package **1.5.6 / Ready**, no
   unexpected owner or mode/register change, and a fresh
   `sensor.hoymiles_hit_parallel_aggregate_power_readback_generation` on a
   detected parallel Master. Then repeat the shared-bus Grid Discharge test on
   the exact v1.5.6 integration/package/firmware build and retain the aggregate
   response diagnostic, Master FC03, separate Master and Slave mode/power, and
   exact start/stop evidence. Do not enable automatic writers until this check
   passes. **PL:** potwierdź pakiet **1.5.6 / Gotowe**, brak nieoczekiwanego
   właściciela i zmiany trybu/rejestrów oraz świeżą encję
   `sensor.hoymiles_hit_parallel_aggregate_power_readback_generation` dla
   wykrytego Mastera równoległego. Następnie powtórz test Grid Discharge na
   wspólnej magistrali z dokładnym buildem integracji/pakietu/firmware v1.5.6 i
   zachowaj diagnostykę odpowiedzi sumarycznej, FC03 Mastera, osobne tryby/moce
   Mastera i Slave'a oraz dokładne dowody start/stop. Do zakończenia kontroli
   nie włączaj automatycznych zapisów.

### Added / Dodano

- Added a post-command, system-level aggregate physical-response diagnostic
  for Grid Discharge on a detected parallel Master. After the existing Master
  FC03 configuration acknowledgement it allows 20 seconds of transition grace,
  then examines five newer complete aggregate-power generations (up to 20
  seconds each) and requires three consecutive stable generations. Its states
  progress from `pending` to `confirmed`, `not_confirmed`, or `not_evaluable`.
  It is not a per-Slave protocol acknowledgement.
- RCE freezes its authoritative discharge target before the command and fails
  closed through the existing neutral rollback when the required aggregate
  response is not confirmed; its stable window also requires grid export of at
  least 0.25 kW and agreement with the frozen target. Manual/manual-recovery
  and RCEm pre-discharge have no authoritative total-kW target, so they evaluate
  fresh stable battery-discharge direction without grid-export or amplitude
  rejection. Self-Use rollback never waits for aggregate discharge evidence.
- ESPHome now emits a generation only after the complete Master aggregate
  balance has fresh grid, PV and LOAD inputs. Partial callbacks, local writes
  and timers cannot forge that generation.
- Parallel topology is latched before Mode 5; unknown topology blocks the
  command and a change during verification prevents confirmation. Best-effort
  sampled-peak attributes annotate the transition without claiming its true
  instantaneous maximum.
- Clarified EMS ownership on both dashboards. This presentation-only change
  leaves execution authority to the scheduler. When no automatic writer is
  active, **Controls now** reports
  **No active automation** instead of implying a manual command. A separate
  **Low-cost charging** row distinguishes disabled, enabled/waiting, active,
  no-charge-needed and explicitly blocked policy states. The stable diagnostic
  attribute remains `owner_code=manual` for backward compatibility.
- RCE and low-cost charging now protect their independent adaptive Solcast
  models from curtailed PV on persistent zero-export sites. A fresh coherent
  physical GCF readback with GCF enabled and an exact effective export limit
  of `0.0%` disables both live and Recorder-backed learning and uses a fixed
  `0.80 × Solcast` factor. Any stale, missing or incoherent GCF evidence also
  pauses learning conservatively with an explicit reason, but is never called
  zero-export. A historical day is eligible only when the existing physical-
  derived `EMS Export Allowed` entity remained `on` for the whole local day;
  one `off`, `unknown`, `unavailable` or missing boundary excludes that day.
  A second Recorder proof from `sensor.hoymiles_ems_package_version` requires
  managed package v1.5.2 or newer for the complete day: v1.5.0–v1.5.1 used
  UI-derived semantics under the same stable entity ID. A v1.5.5 installation
  therefore reuses only physically qualified boundary rows still retained by
  Recorder; absent, excluded or purged evidence intentionally cold-starts.
  Daily evidence attributes added in v1.5.6 keep regular boundary reports for
  unchanged states going forward, without extending Recorder retention.
- Dodano diagnostykę sumarycznej odpowiedzi fizycznej po poleceniu Grid
  Discharge na wykrytym Masterze równoległym. Po dotychczasowym potwierdzeniu
  konfiguracji przez FC03 Mastera stosuje 20 sekund czasu przejściowego, ocenia
  pięć nowszych kompletnych generacji mocy (maksymalnie po 20 sekund każda) i
  wymaga trzech kolejnych stabilnych generacji. Nie jest protokołowym ACK
  żadnego Slave'a.
- RCE zamraża autorytatywny cel rozładowania przed poleceniem i przy braku
  potwierdzenia odpowiedzi wykonuje istniejący neutralny rollback fail-closed.
  Wymaga też eksportu co najmniej 0,25 kW i zgodności z zamrożonym celem.
  Tryb ręczny/recovery i przygotowanie RCEm bez autorytatywnego celu sumarycznej
  mocy oceniają świeży stabilny kierunek rozładowania baterii bez odrzucenia za
  eksport lub amplitudę. Powrót do Self-Use nie czeka na ten dowód.
- Uporządkowano prezentację właściciela EMS. Ta zmiana wyłącznie prezentacyjna
  pozostawia autorytet wykonania w schedulerze. Gdy żaden automat nie wykonuje
  zapisu, **Steruje teraz** pokazuje
  **Brak aktywnej automatyki**, a osobny wiersz **Tanie ładowanie** rozróżnia
  stan wyłączony, włączony i oczekujący, aktywne ładowanie, brak potrzeby oraz
  jawną blokadę. Stabilny atrybut diagnostyczny pozostaje
  `owner_code=manual` dla zgodności wstecznej.
- RCE i tanie ładowanie chronią teraz swoje niezależne adaptacyjne modele
  Solcast przed produkcją PV ograniczoną w instalacjach z trwałym zero-export.
  Świeży, spójny odczyt fizyczny GCF z włączonym GCF i dokładnym efektywnym
  limitem eksportu `0,0%` wyłącza learning bieżący i historyczny oraz wymusza
  stały współczynnik `0,80 × Solcast`. Nieświeży, brakujący lub niespójny GCF
  również konserwatywnie wstrzymuje uczenie z jawną przyczyną, ale nie jest
  nazywany zero-export. Dzień historyczny uczestniczy w modelu tylko wtedy,
  gdy istniejąca, wyprowadzona z fizycznych odczytów encja **EMS Export
  Allowed** była `on` przez cały lokalny dzień; pojedyncze `off`, `unknown`,
  `unavailable` albo brak stanu granicznego wyklucza cały dzień. Drugim dowodem
  Recordera jest `sensor.hoymiles_ems_package_version` z wersją pakietu
  zarządzanego co najmniej v1.5.2 przez cały dzień: v1.5.0–v1.5.1 używały
  semantyki opartej na UI pod tym samym stabilnym identyfikatorem encji.
  Instalacja v1.5.5 wykorzystuje więc tylko fizycznie kwalifikowane wpisy
  graniczne nadal zachowane w Recorderze; brakujący, wykluczony albo usunięty
  dowód świadomie rozpoczyna model od zera. Dzienne atrybuty dowodowe dodane w
  v1.5.6 zapewniają kolejne wpisy graniczne dla niezmiennych stanów, ale nie
  wydłużają retencji Recordera.

### Fixed / Naprawiono

- Split planning authority for a **new** RCE start from execution authority for
  an **already latched active** cycle. A normal input update may temporarily set
  `result_current=false` and block new starts without rolling the active cycle
  back to Self-Use. Live BMS/GCF, ownership/conflict, Off-Grid, FC03/readback,
  SOC/reserve and inverter/topology hard stops remain independent and immediate
  while recalculation is pending.
- Removed execution-only tariff states such as `tariff_charge_active` and
  non-consumed owner/execution diagnostics from the optimizer fingerprint.
  Actual SOC, PV/LOAD, tariff/profile, BMS and freshness inputs still invalidate
  the result normally.
- Made tariff rollback one physical transaction: derive one final
  `4300–4306` block from the last confirmed physical snapshot plus explicit
  rollback values, skip no-ops, issue at most one FC16, and require its newer
  matching full-block FC03 before completion.
- Require every Home Assistant EMS-block helper to acknowledge all seven
  physical `4300–4306` registers. Stale, missing or mismatching FC03 stays
  fail-closed; no second transaction mechanism or firmware-barrier change was
  added.
- Keep the tariff planner's large live/diagnostic/restore attributes while
  excluding them from Recorder persistence, cancel all overlapping delayed
  recalculation tasks on unload, and watch the fallback daily-LOAD input so a
  real fallback change invalidates the plan.
- Sample continuously changing RCE planning telemetry on the existing one-minute
  timer and coalesce normal tariff/GCF cohorts into a five-minute planning
  cadence. Independent availability and safety gates remain immediate.
- Advance Aurora to frontend revision **24**. The battery node keeps available
  values visible and shows stored energy, physical current, state and bounded
  ETA. The main orbit adds tomorrow's forecast and labelled inverter/battery-
  circuit temperatures, with larger readable text in PL and EN.
- Rozdzielono autorytet nowego startu RCE od autorytetu już aktywnego,
  zalatchowanego cyklu. Zwykłe przeliczenie z `result_current=false` blokuje
  nowe starty, ale nie wymusza Self-Use; niezależne bramki live BMS/GCF,
  właściciela, Off-Grid, FC03, rezerwy SOC i topologii zatrzymują cykl od razu.
- Usunięto z fingerprintu taryfy stany wyłącznie wykonawcze. Prawdziwe wejścia
  SOC, PV/LOAD, taryfy/profilu, BMS i świeżości nadal unieważniają wynik.
- Rollback taryfy tworzy jeden blok `4300–4306` z ostatniego potwierdzonego
  fizycznego snapshotu i jawnych wartości rollbacku, wykonuje najwyżej jeden
  FC16 i kończy się dopiero po nowszym zgodnym FC03 wszystkich siedmiu
  rejestrów. Ten pełny kontrakt ACK obowiązuje każdy helper bloku EMS.
- Duże atrybuty taryfy wyłączono tylko z zapisu do Recordera; nakładające się
  opóźnione przeliczenia są anulowane przy unload, a zmiana zastępczego
  dziennego LOAD prawidłowo unieważnia plan.
- Ciągłą telemetrię planowania RCE próbkuje istniejący minutowy timer, a zwykłe
  kohorty taryfy/GCF są łączone w pięciominutowy rytm. Niezależne bramki
  dostępności i bezpieczeństwa nadal działają natychmiast.
- Aurora rev **24** utrzymuje dostępne dane baterii i pokazuje energię, fizyczny
  prąd, stan oraz ograniczone ETA. Główna orbita dodaje prognozę na jutro i
  podpisane temperatury falownika/toru magazynu oraz większy tekst PL i EN.

These changes deliberately affect execution authority, rollback/readback
lifecycle and presentation. They do not change optimizer objectives, slot
selection, forecast/tariff mathematics, RCEm voltage calculations or direct
register semantics, Off-Grid priority, registers `258/259/306`, or the existing
parallel broadcast/per-Slave acknowledgement claim boundary.

Zmiany celowo dotyczą autorytetu wykonania, rollback/readback i prezentacji.
Nie zmieniają celów optymalizatorów, wyboru slotów, matematyki prognozy/taryfy,
obliczeń napięciowych i rejestrów bezpośrednich RCEm, priorytetu Off-Grid,
rejestrów `258/259/306` ani granicy deklaracji broadcastu/per-Slave.

### Validation / Walidacja

- The tagged source passed the complete applicable offline gate, including
  quick **488/488** and exhaustive **2064/2064** matrices, release/frontend
  validation, deterministic generation, optimizer, history, firmware/readback,
  startup/executor, source-rebind, diagnostics and 100-archive analyzer suites.
- Publication additionally required exact-ref `project-checks`, conditional
  `firmware-compile`, Hassfest and HACS validation in GitHub Actions.
- Otagowane źródło przeszło pełną właściwą bramkę offline oraz wymagane CI dla
  dokładnego ref. To dowód offline/CI, a nie uniwersalny certyfikat instalacji.

### Field evidence boundary / Granica dowodu terenowego

- The v1.5.6 runtime was deployed on localhost and `miernik.com.pl`; Home
  Assistant checks/restarts, fresh FC03/readback and Aurora revision 24 passed.
- During the final simultaneous 15-minute window localhost tariff readiness was
  **16/16** with no status transition or schedule flapping; three subsequent
  five-minute cycles also remained ready.
- An earlier controlled meter run had RCE ownership **16/16** and physical grid
  discharge **13/16**; after operator power adjustments its final eight minutes
  were stable, and the vendor application confirmed discharge and Self-Use
  return. The final scheduler guard was deployed afterward.
- The final meter window was RCE-ready **16/16**, but no active slot was planned
  **16/16**. Publication therefore carries an explicit active exact-final RCE
  evidence gap accepted by the maintainer, with a hotfix path for a regression.
- Runtime v1.5.6 wdrożono na localhost i `miernik.com.pl`; kontrole/restart HA,
  świeży FC03/readback oraz Aurora rev24 przeszły. Taryfa localhost zachowała
  gotowość **16/16** bez skakania planu, także w trzech kolejnych cyklach.
  Wcześniejszy kontrolowany test miernika miał właściciela RCE **16/16** i
  fizyczne rozładowanie **13/16**, a końcowe osiem minut było stabilne.
  Końcowe okno miało gotowość RCE **16/16**, lecz bez planowanego aktywnego
  slotu **16/16**; opiekun jawnie zaakceptował tę lukę z możliwością hotfixu.
- The 2026-08-15 run on HA package **1.5.4** and ESP32 project **1.5.3** remains
  bounded hardware/protocol evidence only, not per-Slave ACK or exact-v1.5.6
  software acceptance.

## [1.5.5] - 2026-08-15

### User update steps / Kroki po aktualizacji

1. **HACS:** before updating, keep RCEm in shadow mode and disable every
   automatic or scheduled EMS writer. Update
   **EMS for Hoymiles HIT-(5–20)L-G3** to **v1.5.5**.
   **PL:** przed aktualizacją pozostaw RCEm w trybie shadow i wyłącz wszystkie
   automatyczne oraz harmonogramowe zapisy EMS. Zaktualizuj integrację
   **EMS for Hoymiles HIT-(5–20)L-G3** w HACS do **v1.5.5**.
2. **Home Assistant:** restart after the HACS update. The integration installs
   managed EMS package **1.5.5** during startup, so validate the configuration
   and restart once more when the Repair notification requests it. Keep the
   writers disabled throughout both restarts and hard-refresh every open
   Hoymiles dashboard tab to load frontend URL `v=1.5.5.17`.
   **PL:** po aktualizacji HACS uruchom Home Assistant ponownie. Integracja
   instaluje zarządzany pakiet EMS **1.5.5** podczas startu, dlatego sprawdź
   konfigurację i wykonaj jeszcze jeden restart, gdy zażąda tego komunikat
   Napraw. Podczas obu restartów pozostaw zapisy wyłączone i twardo odśwież każdą
   otwartą kartę dashboardu Hoymiles, aby wczytała URL frontendu `v=1.5.5.17`.
3. **ESP32 / ESPHome:** only after the Home Assistant no-write verification,
   rebuild and upload firmware from the top-level ESPHome file pinned to
   immutable ref **v1.5.5**. HACS does not flash the ESP32. This firmware step
   is required because v1.5.5 consolidates the previously unpublished firmware
   and managed-package candidate; it does not replace the separate shared-bus
   Master/Slave acceptance test.
   **PL:** dopiero po odczytowej weryfikacji Home Assistanta skompiluj i wgraj
   firmware z głównego pliku ESPHome przypiętego do niezmiennego ref
   **v1.5.5**. HACS nie wgrywa firmware ESP32. Ten krok jest wymagany, ponieważ
   v1.5.5 konsoliduje wcześniej nieopublikowanego kandydata firmware i pakietu
   zarządzanego; nie zastępuje osobnego testu Master/Slave na wspólnej magistrali.
4. **Verification / Weryfikacja:** confirm that the physical EMS mode and the
   complete `4300–4306` block did not change during the update, no execution
   owner became active, the managed package reports **1.5.5 / Ready**, RCE,
   tariff and RCEm publish a current result or an explicit fail-closed reason,
   Solcast Day 3 is fresh when enabled, and all Aurora views render. After the
   firmware upload also confirm repeated fresh register-4102 reports and create
   two diagnostic ZIPs to verify that their anonymous installation ID is
   unchanged. Do not claim per-Slave acknowledgement: every inverter must be
   connected to the same external RS485 bus and observed separately.
   **PL:** potwierdź, że podczas aktualizacji nie zmieniły się fizyczny tryb EMS
   ani pełny blok `4300–4306`, nie pojawił się aktywny właściciel, pakiet
   zarządzany pokazuje **1.5.5 / Gotowe**, RCE, taryfa i RCEm publikują aktualny
   wynik albo jawny bezpieczny powód blokady, opcjonalny Dzień 3 Solcast jest
   świeży, a wszystkie widoki Aurora działają. Po wgraniu firmware potwierdź
   także powtarzające się świeże raporty rejestru 4102 i utwórz dwie paczki
   diagnostyczne, aby sprawdzić niezmienność anonimowego ID instalacji. Nie
   deklaruj potwierdzenia każdego Slave'a: wszystkie falowniki muszą być
   podłączone do tej samej zewnętrznej magistrali RS485 i sprawdzone osobno.

### Added

- Added one random, persistent `anonymous_installation_id` (UUID v4) for the
  entire Home Assistant installation. Schema version `1` is stored in Home
  Assistant Store and the same validated ID is preserved by redaction in native
  diagnostics, `environment.json`, and successive diagnostic ZIPs. It is never
  derived from a device or user configuration and is not exposed as an entity.
- Added a bounded, offline analyzer for up to 100 diagnostic ZIPs. It correlates
  packages by the anonymous installation ID and extracts comparable RCE, RCEm,
  tariff-charging, control-run, data-quality, and log-cluster evidence without
  uploading archives or exposing source paths by default.
- Added regression coverage for first-start identity creation, persistence
  across restarts and packages, UUID v4 format, redaction preservation,
  multi-entry reuse, source independence, hostile archives, deterministic
  analysis, privacy defaults, and transactional output.

### Changed

- Restored parallel Master/Slave control for the complete EMS register block
  `4300–4306`. A detected Master now sends one FC16 system broadcast to Modbus
  address `0`; Home Assistant accepts the command only after a later physical
  FC03 from the Master reports the exact requested block.
- Documented the required parallel wiring: the ESP32 converter, Master and
  every Slave must share one physical external Modbus/RS485 bus. Address `0`
  is broadcast only on that wire; a Master-only connection can still report
  topology and Master FC03 but cannot command a disconnected Slave.
- Split hardware readiness into two capabilities. RCE, tariff charging, manual
  schedules and battery balancing may use the verified EMS-block broadcast,
  while writes to GCF/export/charge registers `258`, `259` and `306` remain
  fail-closed on Master/Slave until their broadcast semantics are proven.
- Kept internal addresses `6050–6095` as topology diagnostics only. They are
  not required for address-`0` broadcast and are never polled as external
  Modbus devices through the Master's port.
- Marked physical BMS voltage and maximum charge/discharge current polls as
  force-updating reports. Stable limits now refresh Home Assistant provenance
  instead of expiring after 300 seconds, while a genuinely silent FC03 source
  still makes RCE fail closed. The Safe BMS helper now mirrors the current
  optimizer limit and exposes its freshness and age.
- RCE and tariff execution readiness now follows the optimizer's authoritative
  `result_current`, plan age and source-freshness attributes instead of a
  derived proxy timestamp. Active tariff writes require the same readiness,
  while the frozen action and SOC target survive Home Assistant restarts.
- Register `4102` now uses the 20-second settings controller and reports
  unchanged values. RCEm treats positive capacity as a stable installation
  property, with Total Capacity as fallback, while live BMS voltage and current
  limits remain strictly freshness-gated.
- Battery balancing completes only after SOC remains at least `99.9%` for the
  complete hold interval; a drop resets the hold. The EMS owner diagnostic now
  reports actual active execution rather than an enabled policy.
- EMS mode code `3` is recognized as Off-Grid. Automatic engines yield while
  Off-Grid is active, and cleanup restores owned registers without forcing
  Self-Use.
- Forecast-aware planners dynamically track configured Solcast entities.
  Current and legacy optional Day 3 identifiers are supported; missing or stale
  Day 3 remains diagnostic and uses the conservative shorter-horizon fallback.
- Hardened every delayed command step against state changes during Modbus ACK
  waits. RCE, tariff charging, balancing and RCEm now revalidate current policy,
  ownership/conflict, execution readiness, physical mode and the committed plan
  or recommendation immediately before each subsequent register write and
  after mode acknowledgement. RCEm emergency charge/export paths retain
  independent actuator-local guards and rollback, while manual handover releases
  an RCEm owner before claiming control.
- Aggregate physical-response acknowledgement for parallel broadcasts is not
  implemented in this release. Shared-bus Master/Slave tests and field data
  remain pending; after review, physical aggregate acknowledgement will be
  completed in a separate hotfix. It must not be described as per-Slave
  acknowledgement, because Master FC03 verifies the Master only.
- Renamed the repository to `Kaluzaburza/hoymiles-hit-g3-ems` and the public
  project title to **EMS for Hoymiles HIT-(5–20)L-G3**, while preserving the Home
  Assistant domain, entities, services, storage keys, dashboard strategy type,
  and ESPHome technical identities.
- Updated the diagnostic frontend privacy text so the longitudinal purpose and
  non-identifying origin of the anonymous installation ID are explicit.

### Polski

- Dodano jeden losowy i trwały `anonymous_installation_id` (UUID v4) dla całej
  instalacji Home Assistant. Wersja schematu `1` i identyfikator są przechowywane
  w HA Store, trafiają do natywnej diagnostyki, `environment.json` i kolejnych
  paczek ZIP, a redakcja zachowuje wyłącznie poprawny UUID. ID nie pochodzi z
  urządzenia ani konfiguracji użytkownika i nie jest encją.
- Dodano ograniczony, działający lokalnie analizator maksymalnie 100 paczek ZIP.
  Porównuje zachowanie RCE, RCEm i ładowania taryfowego oraz dane przebiegów
  sterowania, jakości wejść i klastrów logów, bez wysyłania archiwów i bez
  ujawniania ścieżek źródłowych w trybie domyślnym.
- Repozytorium otrzymało adres `Kaluzaburza/hoymiles-hit-g3-ems`, a publiczna
  nazwa projektu to **EMS for Hoymiles HIT-(5–20)L-G3**. Stabilne identyfikatory
  techniczne pozostały bez zmian, a tekst prywatności frontendu wyjaśnia
  wyłącznie diagnostyczny, longitudinalny cel anonimowego ID.
- Przywrócono sterowanie równoległym układem Master/Slave dla pełnego bloku EMS
  `4300–4306`. Master wysyła jeden broadcast FC16 na wspólny adres Modbus `0`,
  a Home Assistant uznaje polecenie dopiero po późniejszym fizycznym FC03 z
  Mastera zawierającym dokładnie żądany blok.
- Opisano wymagane okablowanie równoległe: konwerter ESP32, Master i każdy Slave
  muszą korzystać z jednej fizycznej magistrali zewnętrznego Modbus/RS485.
  Adres `0` rozgłasza tylko na tym przewodzie; połączenie wyłącznie z Masterem
  może nadal pokazywać topologię i FC03 Mastera, lecz nie steruje odłączonym
  Slave'em.
- Rozdzielono gotowość sprzętową: RCE, taryfa, harmonogramy ręczne i
  balansowanie korzystają z potwierdzonego broadcastu EMS, natomiast zapisy
  rejestrów `258`, `259` i `306` pozostają w układzie Master/Slave fail-closed.
- Adresy `6050–6095` pozostają wyłącznie diagnostyką wewnętrznej topologii i nie
  blokują wspólnego broadcastu na adres `0`.
- Fizyczne odczyty napięcia BMS oraz maksymalnego prądu ładowania i rozładowania
  raportują teraz także niezmienioną wartość. Stabilny limit odświeża więc
  źródłowy znacznik czasu, a rzeczywisty brak FC03 nadal blokuje RCE. Wskaźnik
  Safe BMS pokazuje dokładnie bieżący limit planera wraz z wiekiem danych.
- Gotowość wykonawcza RCE i taryfy korzysta teraz z autorytatywnych atrybutów
  planera: `result_current`, wieku planu i świeżości źródeł, zamiast czasu encji
  proxy. Aktywna taryfa może aktualizować sprzęt tylko przy tej samej gotowości,
  a zamrożone działanie i cel SOC przetrwają restart Home Assistanta.
- Rejestr `4102` jest odczytywany przez 20-sekundowy kontroler ustawień i
  raportuje także niezmienione wartości. RCEm traktuje dodatnią pojemność jako
  stabilną cechę instalacji, z Total Capacity jako fallbackiem, natomiast
  bieżące napięcie i limity prądowe BMS nadal wymagają świeżych danych.
- Balansowanie kończy się dopiero po utrzymaniu SOC co najmniej `99,9%` przez
  cały wymagany czas; każdy spadek zeruje podtrzymanie. Diagnostyka właściciela
  EMS pokazuje rzeczywiście aktywne sterowanie, a nie samo włączenie polityki.
- Kod trybu EMS `3` jest rozpoznawany jako Off-Grid. Automatyczne silniki
  ustępują w tym trybie, a sprzątanie przywraca własne rejestry bez wymuszania
  Self-Use.
- Planery dynamicznie śledzą skonfigurowane encje Solcast. Obsługiwane są
  bieżące i starsze identyfikatory opcjonalnego Dnia 3; jego brak lub
  nieświeżość pozostaje diagnostyką i uruchamia konserwatywny fallback.
- Utwardzono każdy opóźniony etap polecenia na zmianę stanu podczas oczekiwania
  na ACK Modbus. RCE, taryfa, balansowanie i RCEm ponownie sprawdzają bieżącą
  politykę, właściciela/konflikt, gotowość wykonawczą, tryb fizyczny oraz
  zatwierdzony plan lub zalecenie bezpośrednio przed każdym kolejnym zapisem i
  po potwierdzeniu trybu. Awaryjne tory ładowania/eksportu RCEm zachowują
  niezależne bramki aktuatorów i rollback, a ręczne przejęcie najpierw zwalnia
  właściciela RCEm.
- Potwierdzanie broadcastu na podstawie sumarycznej odpowiedzi fizycznej nie
  jest zaimplementowane w tym wydaniu. Testy Master/Slave na wspólnej magistrali
  i dane terenowe pozostają do zebrania; po ich przeglądzie potwierdzenie
  sumarycznej odpowiedzi fizycznej zostanie uzupełnione w osobnym hotfiksie.
  FC03 Mastera potwierdza wyłącznie Mastera, a nie każdego Slave'a.

### Validation / Walidacja

- Automation matrix: **2064/2064** exhaustive scenarios, including nominal
  joint-solver/export coverage and all four explicit BMS fail-closed contracts.
- Diagnostics: identity persistence/redaction tests and the bounded 100-archive
  analyzer suite pass; generated results are deterministic and privacy-safe by
  default.
- Aurora: Polish, English and bootstrap-first validation each render exactly
  **42** frames; generated assets pass the release consistency gate.

## [1.5.3] - 2026-08-14

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.5.3**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.5.3**.
2. **Home Assistant:** restart after the HACS update, then run
   `hoymiles_hit_modbus.install_assets` with `overwrite: true` if Home Assistant
   still reports stale managed assets. Restart once more when requested and
   hard-refresh every open Hoymiles dashboard tab to load frontend revision 17.
   **PL:** po aktualizacji HACS uruchom Home Assistant ponownie. Jeżeli nadal
   zgłasza stare zarządzane zasoby, uruchom akcję
   `hoymiles_hit_modbus.install_assets` z `overwrite: true`, wykonaj wymagany
   restart i twardo odśwież każdą otwartą kartę dashboardu Hoymiles, aby
   wczytać rewizję frontendu 17.
3. **ESP32 / ESPHome:** v1.5.3 does not change inverter firmware behaviour.
   Systems already running the required v1.5.2 FC03-readback firmware do not
   need another flash solely for this Home Assistant/frontend hotfix. A fresh
   build should use the top-level file pinned to **v1.5.3**.
   **PL:** v1.5.3 nie zmienia działania firmware falownika. Instalacje z
   wymaganym firmware v1.5.2 z odczytami FC03 nie wymagają ponownego wgrywania
   wyłącznie z powodu tej poprawki Home Assistant/frontendu. Nowa kompilacja
   powinna korzystać z głównego pliku przypiętego do **v1.5.3**.
4. **Verification / Weryfikacja:** keep RCEm in shadow mode and automatic RCE
   and tariff writes disabled during the restart. Verify `result_current`, the
   physical freshness/readback diagnostics and the complete Aurora dashboard
   before restoring the previous automation policy.
   **PL:** podczas restartu pozostaw RCEm w trybie shadow, a automatyczne zapisy
   RCE i taryfy wyłączone. Przed przywróceniem dotychczasowej polityki sprawdź
   `result_current`, diagnostykę fizycznej świeżości/readback oraz pełny
   dashboard Aurora.

### Corrective candidate / Kandydat poprawkowy

- Make the 2064-scenario automation matrix exercise nominal, non-zero BMS
  charge and discharge capability. It now reports joint-solver and controlled
  export coverage for every representative model, while missing, stale,
  future-dated and exact-zero BMS inputs remain separate fail-closed contracts.
- Reject an optimizer result if any consumed Home Assistant input changes while
  its executor job is running. RCE, tariff and RCEm publish
  `result_current: false` before recalculation; scheduler start/update writes
  require a current result, without diagnostic ping-pong between planners.
- Restore the complete Aurora dashboard after a live strategy-registration
  timeout. Frontend revision 17 removes duplicate/legacy bootstrap resources
  through Lovelace's live collection and upgrades a bootstrap-first strategy
  in place. PL, EN and bootstrap-first validation each produce 42 Aurora frames.

### Polski

- Macierz 2064 scenariuszy korzysta teraz z nominalnych, niezerowych i świeżych
  limitów BMS oraz raportuje rzeczywiste uruchomienia wspólnego solvera i
  planowanego eksportu dla każdego modelu. Brakujące, nieświeże, przyszłe i
  zerowe dane BMS pozostają osobnymi testami fail-closed.
- Wynik RCE, taryfy lub RCEm jest odrzucany, jeżeli podczas pracy w executorze
  zmieni się którekolwiek używane wejście. Nowy zapis lub start wymaga
  `result_current: true`, bez wzajemnej pętli publikacji planerów.
- Rewizja frontendu 17 przywraca pełny dashboard Aurora, usuwa z aktywnej
  kolekcji Lovelace stare/zdublowane bootstrapy i aktualizuje nawet strategię
  zarejestrowaną wcześniej przez stary bootstrap. Walidacja PL, EN i ścieżki
  bootstrap-first potwierdza 42 ramki Aurora.

## [1.5.2] - 2026-08-13

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.5.2**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.5.2**.
2. **Home Assistant:** restart Home Assistant after the HACS update. The managed
   EMS package, bilingual dashboard and frontend assets are synchronized
   automatically. A fresh installation that has just created `config/www`
   raises **Dashboard assets require a restart**; validate the configuration
   and restart once more. After the final restart, hard-refresh any dashboard
   tab that was already open so it loads frontend revision 16.
   **PL:** po aktualizacji HACS uruchom Home Assistant ponownie. Zarządzany
   pakiet EMS, dashboard PL/EN i zasoby interfejsu zsynchronizują się
   automatycznie. Pierwsza instalacja, która właśnie utworzyła `config/www`,
   zgłosi Naprawę **Zasoby dashboardu wymagają restartu**; sprawdź konfigurację
   i wykonaj jeszcze jeden restart. Po ostatnim restarcie wykonaj twarde
   odświeżenie każdej wcześniej otwartej karty dashboardu, aby wczytała rewizję
   frontendową 16.
3. **ESP32 / ESPHome:** rebuild and upload are **required** because v1.5.2
   changes the runtime system-power balance and adds independent FC03 actuator
   mirrors and topology guards in `packages/overview.yaml`,
   `packages/settings.yaml` and `packages/parallel_network.yaml`.
   Refresh the top-level ESPHome file pinned to **v1.5.2**, validate, compile,
   and upload it to the ESP32. HACS does **not** flash ESP32 firmware.
   **PL:** ponowna kompilacja i wgranie są **wymagane**, ponieważ v1.5.2
   zmienia runtime bilansu mocy systemu oraz dodaje niezależne lustra FC03 i
   zabezpieczenia topologii w `packages/overview.yaml`, `packages/settings.yaml`
   i `packages/parallel_network.yaml`. Odśwież
   główny plik ESPHome przypięty do **v1.5.2**, sprawdź go, skompiluj i wgraj
   na ESP32. HACS **nie** wgrywa firmware ESP32.
4. **Verification / Weryfikacja:** keep **RCEm in observation (shadow) mode**.
   Verify the RCE and tariff plans, fresh-data diagnostics and one complete
   start/stop cycle before enabling automatic RCE or tariff writes.
   **PL:** pozostaw **RCEm w trybie obserwacji (shadow)**. Przed włączeniem
   automatycznych zapisów RCE albo taryfy sprawdź plan, diagnostykę świeżości
   danych oraz jeden pełny cykl start/stop.
5. **Parallel Master/Slave:** monitoring and RCEm shadow analytics remain
   available, but protected EMS writes fail closed in v1.5.2 because every
   Slave cannot yet provide an independently verified post-write FC03 readback.
   **PL:** monitoring i analiza RCEm shadow pozostają dostępne, lecz chronione
   zapisy EMS są w v1.5.2 blokowane w układzie Master/Slave do czasu niezależnego
   potwierdzania FC03 z każdego urządzenia.

### Added

- Add policy-neutral freshness, LOAD-profile sanitation and parallel power
  balance helpers shared by all planners without merging their objectives.
- Add signed source ages, BMS availability, plan provenance and explicit
  start/continue eligibility diagnostics for fail-closed commissioning.
- Add a bounded joint-horizon RCE active-set heuristic with an independent
  small-horizon oracle test. Regression cases cover known failures of the former
  greedy planner, while the engine and UI explicitly do not claim exact or
  mathematical global optimality for the full mixed-constraint problem.
- Add winter tariff simulations and regression cases for cold-load profiles,
  missing Day 3, failed PV strings, DST and restricted BMS power.

### Changed

- Keep three independent policies: **RCE maximizes net sale revenue**, tariff
  charging shifts necessary household energy into low-cost periods, and
  experimental **RCEm creates usable PV headroom and regulates high voltage**.
- Make RCE parsing independent of PSE payload order and use the absolute
  `dtime_utc` interval end across repeated DST hours. The parser subtracts the
  fixed 15-minute market interval on the UTC timeline before building
  half-hours. The current price now comes from the same parsed plan as
  scheduling and revenue accounting.
- Apply current-slot duration, energy-arrival timing, natural export, shared
  inverter/BMS/AC/export budgets, signed freshness, monotonic protected SOC and
  positively acknowledged EMS ownership to RCE. Day 3 remains diagnostic and
  is not a terminal sales objective.
- Make tariff charging reject micro-cycles, stale/future forecasts and stale
  BMS/SOC inputs; preserve hard Self-Use reserve and model winter LOAD risk
  separately from RCE sale policy.
- Decouple RCEm from the RCE operational floor. RCEm now uses high-PV/low-LOAD
  risk for headroom, low-PV/high-LOAD stress only as a safety constraint, and
  chronological pre-risk energy balance with BMS/register quantization limits.
- Permit RCEm shadow analytics alongside RCE or tariff control, while active
  controller handover waits for a positive neutral-mode readback.
- Derive parallel inverter and battery power from 32-bit system PV/Grid/LOAD
  balance instead of the wrapping 16-bit battery register.
- Publish the RCE, tariff and RCEm optimizer entities in their fail-closed
  initial state before warming Recorder-backed models. Each optimizer uses one
  tracked, cancel-safe background warmup; Recorder reads are sequential and
  limited to 50,000 reports per source entity plus one sentinel row, with a
  15-second timeout per query.

### Fixed

- Prevent a transient, reverse-proxy/browser-cached `404` from the integration's
  former `static-r2` API route from leaving the dashboard strategy unregistered
  after restart. RC6 publishes one canonical full module from the versioned
  `/local/hoymiles-rce-chart-card.js?v=1.5.2.16` URL.
- Materialize the card, bootstrap, Polish and English dashboard JSON, and image
  atomically under `config/www` before publishing the module URL. Storage mode
  resources are reconciled through Lovelace's live resource collection; runtime
  installation never edits `.storage/lovelace.*` directly. If `config/www` did
  not exist when frontend started, publication is deferred and a localized
  restart Repair is raised.
- Keep Home Assistant device entities available if optional dashboard/EMS asset
  installation fails (for example because the disk is full). The integration
  reports a Repair issue and leaves the explicit install action available for a
  controlled retry instead of failing integration-wide setup.
- Recover stable proxy bindings when Home Assistant 2026.8 splits a legacy
  composite device into a new ESPHome-owned child. The integration preserves
  the original source anchor and rebinds only to one uniquely verified
  successor; ambiguous or mismatched candidates fail closed, and the config
  flow blocks a duplicate entry for the resolved child.
- Scope the parallel-topology availability gate to the two derived overview
  power proxies. A local RC3 deployment showed live native physical EMS
  readbacks but unavailable ordinary stable sensor proxies because the gate
  had incorrectly covered every sensor; RC4 restores ordinary proxies to the
  availability of their native ESPHome sources.
- Interpret the official PSE `dtime_utc` field as the **end** of its 15-minute
  settlement interval, not its start. RC5 validates `period_utc` and
  `business_date`, handles the official `24:00` clock label, and restores all
  48 normal-day, 46 spring-DST and 50 autumn-DST half-hours without shifting
  the current price into the preceding market quarter.

### Safety

- Missing, stale, future-dated or exact-zero control capabilities now fail
  closed instead of being interpreted as unlimited power.
- Actuator ownership is retained after a failed or unavailable readback so the
  scheduler retries restoration instead of leaving orphan Grid Charge or Grid
  Discharge modes.
- Treat only a newer physical FC03 generation with matching read-only register
  mirrors as acknowledgement; optimistic command echoes never release an owner.
- Fail closed on protected EMS writes in Master/Slave systems until every node
  can provide independently verifiable post-write feedback; monitoring and
  shadow analytics remain available.
- The live RCEm 253 V emergency path remains independent of forecast/history,
  but predictive RCEm discharge remains disabled in shadow mode.

### Release-candidate status

- RC5 passed full offline validation and exact-SHA push plus workflow-dispatch
  CI, then was deployed to the local and parallel test installations. The RCE
  interval-end parser and backend safety state were correct live. The parallel
  dashboard exposed a separate frontend P1: an early request to the custom
  `static-r2` route returned a transient `404`, that response remained cached,
  and Lovelace timed out waiting for
  `ll-strategy-dashboard-hoymiles-hit-xxl-g3` after restart.
- RC6 runtime commit `7ce13155533bfa0bf9752a0fd201224dac1a7393`
  completed independent review and the full offline gate with P0=0/P1=0. Both
  exact-SHA push CI and `workflow_dispatch`, including the exhaustive matrix,
  passed.
- On local Home Assistant 2026.8.1, the installed integration and managed assets
  matched the RC6 runtime hashes, integration/package version 1.5.2 reported
  **Ready**, the version-16 `/local` assets returned HTTP 200, and both a fresh
  tab and a hard refresh rendered the dashboard. EMS remained in Self-Use with
  every controller owner off.
- On the parallel Home Assistant 2026.7.4 installation, the runtime and asset
  hashes also matched, every required version-16 `/local` asset returned HTTP
  200, and a hard refresh rendered the Aurora dashboard. RCE and tariff control
  remained off; RCEm remained enabled only in shadow mode; every owner and
  execution remained off because the required ESP32 firmware is still pending.
  No firmware was flashed during this frontend audit.
- One bounded background RCEm Recorder-history query timed out once. The planner
  stayed fail-closed, no write was attempted and the timeout did not repeat in
  the observation window. This is recorded as degraded history availability,
  not a release blocker.
- RC6 therefore has live GO for the tested Home Assistant runtime/frontend, not
  for pending ESP32 commissioning or protected EMS writes. The exact deployed
  runtime remains `7ce13155533bfa0bf9752a0fd201224dac1a7393`; a later
  documentation-only/tag commit will have a different SHA and must not be
  described as the live-tested runtime.

### Polski

- Zachowano trzy osobne cele: RCE maksymalizuje przychód netto ze sprzedaży,
  tanie ładowanie przenosi potrzebną energię domu do tanich godzin, a
  eksperymentalny RCEm przygotowuje miejsce na PV i ogranicza wysokie napięcie.
- RCE otrzymało planowanie całego horyzontu, poprawną obsługę DST i kolejności
  danych PSE, fizyczne limity mocy oraz bezpieczne utrzymanie rezerwy SOC.
- Tanie ładowanie odrzuca mikrocykle i nieświeże dane, zachowuje twardą rezerwę
  Self-Use oraz osobno modeluje zimowe ryzyko zużycia.
- RCEm został odłączony od rezerwy operacyjnej RCE, poprawnie rozróżnia ryzyko
  nadwyżki PV od ochrony domu i nadal pozostaje domyślnie w trybie podglądu.
- Niepotwierdzony zapis nie usuwa już właściciela automatyki; przywrócenie
  ustawień jest ponawiane po odzyskaniu komunikacji.
- Encje optymalizatorów RCE, taniego ładowania i RCEm są rejestrowane od razu w
  bezpiecznym stanie blokującym sterowanie, a rozgrzewanie modeli z danych
  Recorder działa w pojedynczym, śledzonym zadaniu tła, które jest anulowane
  przy wyładowaniu integracji. Każde zapytanie ma limit 50 000 raportów dla
  jednej encji źródłowej plus jeden wiersz kontrolny oraz limit czasu 15 sekund;
  przekroczenie budżetu pozostawia planer w stanie fail-closed.
- RC6 usuwa wyścig startowy interfejsu, w którym przejściowe `404` z dawnej
  trasy API `static-r2` było zapamiętywane przez reverse proxy lub przeglądarkę,
  a Lovelace po restarcie nie doczekał się rejestracji strategii dashboardu.
  Ładowany jest jeden pełny moduł spod wersjonowanego adresu
  `/local/hoymiles-rce-chart-card.js?v=1.5.2.16`.
- Karta, bootstrap, dashboard JSON po polsku i angielsku oraz obraz są kopiowane
  atomowo do `config/www` przed publikacją modułu. Zasób trybu storage jest
  uzgadniany przez aktywną kolekcję Lovelace; instalator w czasie działania nie
  zmienia bezpośrednio `.storage/lovelace.*`. Gdy `config/www` nie istniał przy
  starcie frontendu, publikacja czeka do restartu, a integracja zgłasza Naprawę.
- Po podziale starszego urządzenia złożonego przez Home Assistant 2026.8
  integracja odzyskuje stabilne powiązania encji proxy. Zachowuje pierwotny
  identyfikator źródłowy i przełącza się wyłącznie na jednego jednoznacznie
  zweryfikowanego następcę ESPHome; kandydaci niejednoznaczni lub niezgodni są
  bezpiecznie odrzucani, a kreator blokuje drugi wpis dla rozwiązanego urządzenia.
- Lokalny test RC3 potwierdził działające natywne odczyty fizyczne EMS, ale
  zwykłe stabilne sensory proxy pozostawały niedostępne, ponieważ bramka
  topologii równoległej błędnie obejmowała wszystkie sensory. RC4 ogranicza ją
  do dwóch wyliczanych encji mocy podglądu. RC4 przeszedł CI dokładnego SHA,
  pełną macierz i lokalne wdrożenie; odczyty fizyczne, zwykłe proxy i stan
  instalacji działały poprawnie.
- Dane live PSE potwierdziły, że `dtime_utc` oznacza koniec kwadransa, a nie jego
  początek. RC5 odejmuje 15 minut na osi UTC, sprawdza `period_utc` oraz
  `business_date` i poprawnie odtwarza 48 półgodzin zwykłej doby, 46 wiosennej
  oraz 50 jesiennej doby DST. RC5 przeszedł pełną walidację offline, CI
  dokładnego SHA oraz wdrożenie na obu instalacjach; parser i bezpieczny stan
  backendu działały poprawnie live.
- Wdrożenie RC5 ujawniło niezależny P1 frontendu opisany powyżej. Dokładny commit
  runtime RC6 `7ce13155533bfa0bf9752a0fd201224dac1a7393` przeszedł
  niezależny przegląd, pełną walidację offline bez P0/P1 oraz CI dokładnego SHA
  dla push i `workflow_dispatch`, w tym pełną macierz.
- Na lokalnym Home Assistant 2026.8.1 sumy kontrolne integracji i zasobów były
  zgodne z RC6, wersje integracji i pakietu wynosiły 1.5.2, stan instalacji był **Gotowe**,
  zasoby `/local` rewizji 16 zwracały HTTP 200, a dashboard działał w nowej
  karcie i po twardym odświeżeniu. Tryb pozostał Self-Use, a wszyscy właściciele
  automatyki byli wyłączeni.
- Na równoległej instalacji Home Assistant 2026.7.4 sumy kontrolne runtime'u i
  zasobów także były zgodne, wszystkie wymagane zasoby `/local` rewizji 16
  zwracały HTTP 200,
  a dashboard Aurora działał po twardym odświeżeniu. RCE i taryfa pozostały
  wyłączone, RCEm działał wyłącznie w trybie shadow, a właściciele i wykonanie
  pozostały wyłączone z powodu oczekującego firmware ESP32. Firmware nie było
  wgrywane podczas tego audytu frontendu.
- Jedno ograniczone czasowo zapytanie tła RCEm do historii Recorder zakończyło
  się timeoutem. Planer pozostał fail-closed, nie wykonał zapisu, a zdarzenie nie
  powtórzyło się w oknie obserwacji. Jest to odnotowana ograniczona dostępność
  historii, a nie bloker wydania.
- RC6 otrzymuje zgodę live GO dla przetestowanego runtime Home Assistant i
  frontendu, nie dla oczekującego wdrożenia ESP32 ani chronionych zapisów EMS.
  Dokładnym wdrożonym commitem runtime pozostaje
  `7ce13155533bfa0bf9752a0fd201224dac1a7393`; późniejszy commit wyłącznie z
  dokumentacją/tagiem będzie miał inne SHA i nie może być opisywany jako runtime
  sprawdzony live.

## [1.5.1] - 2026-08-12

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.5.1**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.5.1**.
2. **Home Assistant:** restart Home Assistant once so it loads the new
   integration metadata. The EMS package and frontend logic are unchanged. If
   updating from a release older than 1.5.0 and **Installation status / Repairs**
   requests another restart, validate the configuration and perform that second
   restart.
   **PL:** uruchom Home Assistant ponownie jeden raz, aby wczytał nowe metadane
   integracji. Pakiet EMS i logika interfejsu nie uległy zmianie. Jeżeli
   aktualizujesz wersję starszą niż 1.5.0, a **Stan instalacji / Naprawy** poprosi
   o kolejny restart, sprawdź konfigurację i wykonaj ten drugi restart.
3. **ESP32 / ESPHome:** no firmware rebuild is required. The compatible ESPHome
   package remains **v1.4.4**.
   **PL:** nie trzeba ponownie kompilować ani wgrywać ESP32. Zgodny pakiet
   ESPHome pozostaje w wersji **v1.4.4**.
4. **Verification / Weryfikacja:** open the repository page from HACS and use
   the language switch to review the complete English or Polish documentation.
   Confirm **Installation status / Stan instalacji = Ready / Gotowe**.
   **PL:** otwórz stronę repozytorium z HACS i użyj przełącznika języka, aby
   przeczytać pełną dokumentację angielską albo polską. Sprawdź, czy
   **Stan instalacji = Gotowe**.

### Changed

- Rebuilt the repository front page as concise, product-quality documentation
  with a clear path from compatibility and safety to installation and use.
- Split the documentation into complete, equivalent English (`README.md`) and
  Polish (`README.pl.md`) editions with a visible language switch.
- Rewrote both languages for natural grammar and consistent terminology,
  including household load, three-phase imbalance and Modbus function 16
  (`0x10`).
- Corrected the architecture diagram so HACS is shown as the installer and update
  manager rather than as part of the runtime data path.
- Preserved complete wiring, HACS, ESPHome, EMS, parallel-system, update,
  diagnostics and safety guidance in both languages.

### Polski

- Polska dokumentacja nie jest już skróconym dodatkiem na końcu angielskiego
  README. Otrzymała pełną, równorzędną wersję obejmującą wymagania, instalację,
  aktualizacje, wszystkie tryby EMS, systemy równoległe i diagnostykę.
- Usunięto kalki językowe, mieszanie polskiego z angielskim oraz niejasne hasła
  marketingowe. Ujednolicono terminologię techniczną i kolejność instrukcji.

## [1.5.0] - 2026-08-12

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.5.0**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.5.0**.
2. **Home Assistant:** restart Home Assistant once. The managed EMS package,
   bilingual Aurora dashboard and frontend cards are synchronized automatically.
   If **Installation status / Stan instalacji** or **Repairs / Naprawy**
   requests another restart (most likely after updating from an older release),
   validate the configuration and perform that second restart.
   **PL:** uruchom Home Assistant ponownie jeden raz. Zarządzany pakiet EMS,
   dashboard Aurora PL/EN i karty frontendowe zsynchronizują się automatycznie.
   Jeżeli następnie **Stan instalacji** albo **Naprawy** poprosi o kolejny
   restart — szczególnie po aktualizacji ze starszej wersji — sprawdź
   konfigurację i wykonaj ten drugi restart.
3. **ESP32 / ESPHome:** no firmware rebuild is required for this update.
   **PL:** ta aktualizacja nie wymaga ponownej kompilacji ani wgrywania ESP32.
4. **Verification / Weryfikacja:** verify **Installation status = Ready**, then
   review the new advanced-data switches before enabling automatic writes.
   Keep RCEm in observation mode during commissioning.
   **PL:** sprawdź **Stan instalacji = Gotowe**, następnie przejrzyj nowe
   przełączniki danych zaawansowanych przed włączeniem automatycznych zapisów.
   Podczas odbioru pozostaw RCEm w trybie obserwacji.

### Added

- Add conservative whole-SOC-step planning and explicit day-three terminal
  reserve diagnostics to the rolling RCE optimizer.
- Extend tariff planning with a rolling horizon, explicit degraded-horizon
  status and learned effective Grid Charge power readiness.
- Use interval PV forecasts and weekday/weekend LOAD profiles in RCEm when
  available, with per-risk-window surplus/headroom diagnostics and a safe
  fallback model.
- Add advanced-data mode to RCEm and reorganize RCE/tariff/RCEm views around
  the decision, user benefit and next action.
- Add a documented safety and functional programme-mapping matrix for technical
  acceptance reports without claiming subsidy eligibility or formal equipment
  certification.

### Changed

- Show the **net** RCE optimization benefit after battery-wear cost in the
  primary finance card; retain gross revenue uplift in expert diagnostics.
- Convert a day-three AC shortfall to the required DC battery reserve and value
  it at avoided import cost, preventing a low-price sale followed by a more
  expensive grid purchase.
- Show the conservative PV/load values actually used by tariff planning on the
  basic view instead of unrelated raw Solcast and short-window values.
- Preserve extrema in the Aurora 24-hour chart when reducing Recorder data;
  avoid curve interpolation that could visually overshoot measured power.
- Move controller ownership, raw model inputs, confidence bands, physical
  limits and long safety explanations behind explicit expert-mode switches.

### Safety

- Retain single-owner EMS arbitration, stale-data fail-closed gates, BMS/GCF/
  parallel-system limits, idempotent writes with read-back acknowledgement,
  minimum dwell/latching and safe Self-Use fallback.
- RCEm continues to start in observation mode and never changes grid-code
  profiles, protection thresholds, Q(U), P(U), power factor or unbalance.

### Polski

- RCE planuje sprzedaż konserwatywnie do pełnych kroków SOC, rozróżnia wynik
  netto od wzrostu przychodu brutto i wyjaśnia rezerwę dnia trzeciego.
- Tanie ładowanie pokazuje wartości faktycznie użyte w obliczeniach, stan
  kalibracji mocy Grid Charge i jawnie sygnalizuje skrócony horyzont zastępczy.
- RCEm wykorzystuje — jeśli są dostępne — półgodzinny profil Solcast oraz
  osobne profile LOAD dni roboczych i weekendów; szczegóły każdego okna ryzyka
  są dostępne w trybie eksperckim.
- Podstawowe widoki pokazują decyzję, plan i korzyść. Surowe dane diagnostyczne
  są nadal dostępne, ale nie utrudniają codziennej obsługi.
- Dodano matrycę funkcji EMS, zabezpieczeń i gotowości do odbioru technicznego.
  Dokument jasno oddziela testowalne funkcje projektu od formalnej certyfikacji
  falownika, magazynu i kompletnej instalacji.

## [1.4.8] - 2026-08-10

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.8**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.4.8**.
2. **Home Assistant:** restart Home Assistant once after HACS finishes. The
   managed dashboard refreshes automatically. When updating directly from
   version 1.4.6 or older, follow an additional restart request shown by
   **Installation status / Stan instalacji** or **Repairs / Naprawy**.
   **PL:** po aktualizacji wykonaj jeden restart Home Assistanta. Dashboard
   odświeży się automatycznie. Przy aktualizacji bezpośrednio z wersji 1.4.6
   lub starszej wykonaj dodatkowy restart tylko wtedy, gdy poprosi o niego
   Stan instalacji lub Naprawy.
3. **ESP32 / ESPHome:** no firmware rebuild is required. The compatible ESPHome
   package remains **v1.4.4**. **PL:** nie trzeba przebudowywać ani wgrywać
   ESP32; zgodny pakiet ESPHome pozostaje w wersji **v1.4.4**.
4. **Verification / Weryfikacja:** in **RCE automation / Automatyka RCE**, check
   that **RCE details and diagnostics / RCE — szczegóły i diagnostyka** appears
   in the main column directly below **RCE discharge plan / Plan rozładowań
   RCE**. Confirm **Installation status / Stan instalacji = Ready / Gotowe**.

### Fixed

- Move RCE optimization and diagnostic data from the narrow sidebar into the
  main column directly below the discharge plan.
- Keep dashboard-only releases independent from the managed EMS package schema
  version, preventing an unnecessary second restart after visual hotfixes.
- Preserve the existing RCE, tariff charging and RCM optimizer logic unchanged.

### Polski

- Przeniesiono dane optymalizacji i diagnostyki RCE z wąskiej kolumny bocznej
  pod plan rozładowań, dzięki czemu są czytelniejsze na komputerze i telefonie.
- Rozdzielono wersję integracji od wersji niezmienionego pakietu EMS, aby
  poprawki wizualne nie wymuszały niepotrzebnego drugiego restartu.
- Logika automatyk RCE, taniego ładowania i RCM pozostała bez zmian.

## [1.4.7] - 2026-08-10

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.7**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.4.7**.
2. **Home Assistant:** after HACS finishes, validate the configuration and
   restart Home Assistant. Then check **Installation status / Stan instalacji**
   and **Repairs / Naprawy**. If another restart is requested, validate the
   configuration and restart once more; this is expected when the new package
   was copied after YAML had already loaded. **PL:** po aktualizacji sprawdź
   konfigurację i uruchom Home Assistant ponownie. Jeżeli Stan instalacji lub
   Naprawa wymagają kolejnego restartu, sprawdź konfigurację i wykonaj go jeszcze
   raz. Jest to oczekiwane, gdy nowy pakiet skopiowano po wczytaniu YAML.
3. **ESP32 / ESPHome:** no firmware rebuild is required. The compatible ESPHome
   package remains **v1.4.4**. **PL:** nie trzeba przebudowywać ani wgrywać
   ESP32; zgodny pakiet ESPHome pozostaje w wersji **v1.4.4**.
4. **Verification / Weryfikacja:** confirm **Installation status / Stan
   instalacji = Ready / Gotowe**, `restart_required: false`, EMS package version
   `1.4.7` and no Hoymiles Repair. **PL:** sprawdź stan `Gotowe`, atrybut
   `restart_required: false`, wersję pakietu EMS `1.4.7` oraz brak Naprawy
   Hoymiles.

### Fixed

- Add an explicit version marker to every managed EMS package and compare it
  with the installed integration version.
- Show a bilingual Home Assistant Repair and a precise Installation status
  when a HACS update needs one additional restart before the new YAML package
  becomes active.
- Clear the Repair automatically as soon as the matching package version is
  loaded, while keeping the existing missing-packages diagnosis separate.

### Polski

- Dodano znacznik wersji do zarządzanego pakietu EMS i porównanie go z wersją
  integracji.
- Stan instalacji i Naprawy jasno informują o wymaganym dodatkowym restarcie,
  zamiast przedwcześnie pokazywać `Gotowe` po aktualizacji HACS.
- Komunikat znika automatycznie po wczytaniu zgodnej wersji pakietu; brak
  włączonej obsługi `packages` nadal ma osobną, jednoznaczną diagnostykę.

## [1.4.6] - 2026-08-10

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.6**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.4.6**.
2. **Home Assistant:** restart once after HACS finishes. The managed dashboard,
   EMS package and frontend card are refreshed automatically. Existing
   dashboard customizations remain protected by the normal migration backup.
   **PL:** po zakończeniu aktualizacji wykonaj jeden restart Home Assistanta.
   Zarządzany dashboard, pakiet EMS i karta frontendowa zostaną odświeżone
   automatycznie, a dotychczasowe modyfikacje chroni standardowa kopia migracji.
3. **ESP32 / ESPHome:** no firmware rebuild is required. This release changes
   the Home Assistant integration, managed EMS package, dashboard and
   documentation only; the compatible ESPHome package remains **v1.4.4**.
   HACS does not flash ESP32. **PL:** nie trzeba ponownie kompilować ani wgrywać
   ESP32. Wydanie zmienia integrację Home Assistanta, zarządzany pakiet EMS,
   dashboard i dokumentację; zgodny pakiet ESPHome pozostaje w wersji
   **v1.4.4**. HACS nie aktualizuje ESP32.
4. **Verification / Weryfikacja:** confirm **Installation status / Stan
   instalacji = Ready / Gotowe**. In LOAD/EPS, the 30-day home-consumption chart
   starts a new clean series from the real phase LOAD; previous inflated
   statistics are intentionally not copied. As an administrator, open the last
   Diagnostics view and verify that **Download diagnostic ZIP** produces one
   archive. **PL:** sprawdź **Stan instalacji = Gotowe**. W LOAD/EPS wykres
   zużycia domu rozpocznie nową, czystą serię z rzeczywistej mocy faz LOAD;
   poprzednie zawyżone statystyki celowo nie są kopiowane. Jako administrator
   otwórz ostatnią zakładkę Diagnostyka i sprawdź pobranie jednego ZIP-a.

### Added

- Add Home Assistant's native **Download diagnostics** report with integration
  and firmware versions, entity/catalog coverage, current optimizer and EMS
  state plus 24 hours of significant control history.
- Add a one-command Terminal & SSH collector that creates a single redacted
  support archive with relevant Core/ESPHome logs and host diagnostics.
- Add an administrator-only card to the final Diagnostics dashboard view. One
  click creates an in-memory ZIP and downloads it directly in the browser.
- Show the support email and the required fault description/time directly on
  the diagnostic card and inside the downloaded archive.
- Add bilingual support instructions and automated privacy regression tests.

### Changed

- Reframe the README around the project as a local, explainable EMS while
  retaining the Hoymiles/Modbus repository name and technical search terms.
- Replace the release-specific feature wall with a concise module overview,
  explicit local/cloud boundaries, safety scope and links to detailed material.
- Add a pre-automation commissioning checklist to the bilingual quick start.

### Fixed

- Calculate the LOAD/EPS home-consumption history by integrating the clean
  per-phase LOAD power (registers 2170-2172). The chart no longer uses inverter
  daily counters that can include inverter self-consumption and conversion
  losses, and starts a new clean long-term statistics series.

### Polski

- Dodano natywny raport **Pobierz diagnostykę** z wersjami, kompletnością encji,
  stanami automatyk i 24-godzinną historią istotnych zmian sterowania.
- Dodano jedną komendę terminalową tworzącą odfiltrowaną paczkę z logami Core,
  ESPHome i informacjami systemowymi oraz instrukcję PL/EN.
- Dodano kartę w ostatniej zakładce Diagnostyka. Administrator jednym
  kliknięciem pobiera ZIP tworzony w pamięci, bez pozostawiania go w `/config`.
- Na karcie i w ZIP-ie dodano adres `info@kaluzaaa.com` oraz jasną informację,
  aby do raportu dołączyć opis problemu i dokładny czas wystąpienia błędu.
- Uporządkowano README wokół projektu jako lokalnego, wyjaśnialnego EMS bez
  zmiany nazwy repozytorium i fraz Hoymiles/Modbus używanych przy wyszukiwaniu.
- Skrócono prezentację funkcji, doprecyzowano granice pracy lokalnej,
  bezpieczeństwo, status eksperymentalnego RCEm oraz zasady bezpłatnego projektu
  i opcjonalnego wsparcia autora.
- Do szybkiego startu PL/EN dodano kontrolę uruchomieniową przed włączeniem
  automatycznego sterowania EMS.
- Wykres zużycia domu w zakładce LOAD/EPS jest teraz liczony z czystej sumy
  mocy faz LOAD (rejestry 2170-2172). Nie korzysta już z liczników dobowych,
  które mogły zawierać autokonsumpcję falownika i straty przetwarzania;
  tworzona jest nowa, czysta historia statystyk.

## [1.4.5] - 2026-08-09

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.5**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.4.5**.
2. **Home Assistant:** restart once after HACS finishes. The managed EMS
   package is refreshed automatically and creates the internal target/slot
   memory used by the hotfix. **PL:** po zakończeniu aktualizacji wykonaj jeden
   restart Home Assistanta. Zarządzany pakiet EMS odświeży się automatycznie i
   utworzy wewnętrzną pamięć celu oraz okna używaną przez poprawkę.
3. **ESP32 / ESPHome:** no firmware rebuild is required. This hotfix changes
   only the Home Assistant integration and EMS package; the compatible ESPHome
   package remains **v1.4.4**. HACS does not flash ESP32. **PL:** ponowna
   kompilacja ani wgrywanie firmware ESP32 nie są wymagane. Hotfix zmienia
   wyłącznie integrację Home Assistanta i pakiet EMS, a zgodny pakiet ESPHome
   pozostaje w wersji **v1.4.4**. HACS nie aktualizuje ESP32.
4. **Verification / Weryfikacja:** confirm **Installation status / Stan
   instalacji = Ready / Gotowe**. During the next planned low-cost period,
   Grid Charge should start once, retain one target and return to Self-Use only
   after reaching it or when the latched charging window ends. A stable EMS
   change produces one push notification after 15 seconds. **PL:** sprawdź
   **Stan instalacji = Gotowe**. W kolejnym zaplanowanym tanim okresie Grid
   Charge powinien uruchomić się jeden raz, zachować jeden cel i wrócić do
   Self-Use dopiero po jego osiągnięciu albo zakończeniu zapamiętanego okna.
   Stabilna zmiana EMS wysyła jedno powiadomienie po 15 sekundach.

### Fixed

- Freeze the target SOC, action and contiguous planned-slot end when automatic
  tariff charging starts. Live SOC, load and forecast recalculations can extend
  the accepted window but cannot shorten it or repeatedly stop/restart charging.
- Schedule a missing dynamic reserve backwards from the next expensive period.
  An all-low G12w weekend therefore stays in Self-Use and performs one complete
  charge only in the last blocks actually required before the tariff changes.
- Add a 1% start deadband, a 90-second minimum confirmation period and
  fail-closed handling for genuinely missing optimizer data.
- Stop treating a transient `current_slot_planned = false` result as an
  immediate command to return to Self-Use.
- Debounce EMS and inverter-status push notifications for 15 seconds and reject
  duplicate no-op state transitions.
- Expose the end of the current contiguous optimizer run for deterministic
  controller latching and regression testing.

### Polski

- Usunięto pętlę naprzemiennego przełączania Ładowanie z sieci / Autokonsumpcja
  podczas nocnego ładowania taryfowego.
- Cel SOC, działanie oraz koniec ciągłego okna są zapamiętywane przy starcie
  cyklu i nie zmieniają się od chwilowych przeliczeń prognozy lub obciążenia.
- Brakująca rezerwa jest planowana wstecz od następnej drogiej strefy. Cały
  tani weekend G12w pozostaje w Self-Use, a jeden ciągły cykl zaczyna się
  dopiero w ostatnich blokach niezbędnych do zgromadzenia wymaganej energii.
- Dodano histerezę startu, minimalny czas potwierdzenia oraz 15-sekundową
  stabilizację powiadomień push.

## [1.4.4] - 2026-08-08

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.4**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.4.4**.
2. **Home Assistant:** restart once after HACS finishes. The managed dashboard,
   EMS package and frontend cards are then refreshed automatically. Existing
   dashboard customizations remain preserved. **PL:** po zakończeniu
   aktualizacji wykonaj jeden restart Home Assistanta. Zarządzany dashboard,
   pakiet EMS oraz karty frontendowe zostaną odświeżone automatycznie, a własne
   modyfikacje dashboardu pozostaną zachowane.
3. **ESP32 / ESPHome:** this release changes `packages/battery.yaml`. Refresh
   the top-level `hoymiles-inverter.yaml`, confirm that its remote package is
   pinned to **v1.4.4**, then validate, compile and upload the firmware. HACS
   updates Home Assistant but does **not** flash ESP32. **PL:** ta wersja
   zmienia `packages/battery.yaml`. Odśwież główny plik
   `hoymiles-inverter.yaml`, sprawdź przypięcie pakietu do **v1.4.4**, następnie
   wykonaj walidację, kompilację i wgraj firmware. HACS aktualizuje Home
   Assistanta, ale **nie** wgrywa firmware ESP32.
4. **Verification / Weryfikacja:** confirm **Installation status / Stan
   instalacji = Ready / Gotowe**, check the battery capacity value, open the
   new native charts and verify that **EMS control conflict / Konflikt
   sterowania** is off. Notification and automation switches should retain the
   state selected before restart. **PL:** sprawdź **Stan instalacji = Gotowe**,
   pojemność baterii, nowe wykresy oraz wyłączony **Konflikt sterowania**.
   Przełączniki powiadomień i automatyk powinny zachować stan sprzed restartu.

### Added

- Add high-contrast native Home Assistant history/statistics charts for the
  start page, LOAD/EPS, PV strings, battery, grid, revenue and PV production.
- Use a consistent power-flow palette: PV green, home red, grid yellow and
  battery blue; PV1–PV4 progress from red through orange/yellow to green.
- Add responsive summary cards that wrap cleanly on desktop and mobile without
  relying on Mushroom for layout.
- Add an explicit EMS control-owner sensor and conflict detector covering RCE,
  tariff charging, RCEm, manual schedules and battery balancing.
- Add a current four-panel dashboard overview to the documentation.

### Changed

- Preserve user-selected helper states across Home Assistant restarts instead
  of reapplying YAML `initial` values; this includes mobile notifications and
  automatic-mode controls.
- Coalesce rapid state changes in the RCE and tariff optimizers and suppress
  unchanged RCE/tariff/RCEm state writes, reducing Recorder and frontend churn.
- Block tariff charging when the selected built-in price table has expired.
- Prefer the inverter-configured battery capacity register 4102 over the
  sometimes differently scaled BMS capacity register 1907.
- Use the physical battery-capacity entity consistently in RCEm and the power
  flow card.
- Simplify project licensing documentation to the current MIT terms.

### Fixed

- Schedule the delayed startup Repairs check on the Home Assistant event loop.
- Keep dynamic dashboard cards readable at narrow widths and prevent fixed
  glance columns from overflowing.
- Refresh the bundled dashboard/card asset revision so HACS updates cannot
  retain stale frontend files.

### Polski

- Dodano kontrastowe, natywne wykresy Home Assistanta dla przepływu mocy,
  LOAD/EPS, stringów PV, baterii, sieci, zysków i produkcji.
- Przełączniki powiadomień i automatyk zachowują ustawienie po restarcie HA.
- Dodano wskazanie właściciela sterowania EMS i wykrywanie konfliktu między
  automatykami.
- Ograniczono zbędne przeliczenia oraz zapisy RCE/taryfy/RCEm do Recorder.
- Nieaktualny cennik taryfowy bezpiecznie blokuje automatyczne ładowanie.
- Pojemność baterii jest pobierana przede wszystkim z rejestru falownika 4102,
  a rejestr BMS 1907 pozostaje źródłem awaryjnym.
- Dokumentacja licencji pokazuje teraz prosty, aktualny stan: MIT.

## [1.4.3] - 2026-08-08

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.3**.
   **PL:** zaktualizuj integrację w HACS do wersji **1.4.3**.
2. **Home Assistant:** restart once after HACS finishes so the corrected setup
   diagnostic and Repairs check are reloaded. **PL:** po zakończeniu
   aktualizacji wykonaj jeden restart Home Assistanta, aby przeładować
   poprawioną diagnostykę instalacji i Naprawy.
3. **ESP32 / ESPHome:** no firmware rebuild is required. This hotfix does not
   change ESPHome, Modbus polling or inverter control. **PL:** ponowna
   kompilacja ani wgrywanie firmware ESP32 nie są wymagane; hotfix nie zmienia
   ESPHome, odpytywania Modbus ani sterowania falownikiem.
4. **Verification / Weryfikacja:** open **Diagnostyka** and confirm that
   **Stan instalacji** reports **Gotowe / Ready** when the managed EMS package
   is loaded. The false package warning in Home Assistant Repairs should also
   disappear. **PL:** otwórz **Diagnostykę** i sprawdź, czy **Stan instalacji**
   pokazuje **Gotowe**, a fałszywe ostrzeżenie o brakującym pakiecie znika z
   sekcji Naprawy.

### Fixed

- Use the existing `input_boolean.hoymiles_rce_discharge_enabled` helper as the
  shared EMS-package readiness sentinel.
- Fix the false **Enable packages and restart** diagnostic and matching Home
  Assistant Repair that appeared even while EMS/RCE packages were active.
- Add a structural regression check that verifies the diagnostic sentinel is
  present in the distributed EMS package.

### Polski

- Diagnostyka instalacji i Naprawy korzystają teraz z istniejącej encji
  `input_boolean.hoymiles_rce_discharge_enabled` jako wspólnego znacznika
  załadowania pakietu EMS.
- Usunięto fałszywy komunikat **Wymagane włączenie pakietów i restart**, który
  pojawiał się mimo prawidłowego działania EMS/RCE.
- Dodano test strukturalny chroniący nazwę encji kontrolnej przed ponownym
  rozjechaniem w kolejnych wydaniach.

## [1.4.2] - 2026-08-08

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.2**.
   **PL:** zaktualizuj integrację w HACS do wersji **1.4.2**.
2. **Home Assistant:** restart once after HACS finishes so the version,
   license metadata and local brand assets are reloaded. **PL:** po
   aktualizacji uruchom Home Assistanta ponownie, aby przeładować wersję,
   metadane licencji i lokalne ikony.
3. **ESP32 / ESPHome:** no firmware rebuild is required. Firmware **v1.3.3**
   remains runtime-compatible; this release does not change Modbus polling or
   control. **PL:** ponowne wgrywanie ESP32 nie jest wymagane; firmware
   **v1.3.3** pozostaje zgodne.
4. **Verification / Weryfikacja:** check **Installation status / Stan
   instalacji** and any Home Assistant Repairs. The HACS update-card icon is
   supplied through Home Assistant Brands and may remain cached temporarily
   after the upstream asset is deployed. **PL:** sprawdź **Stan instalacji** i
   ewentualne Naprawy. Ikona karty aktualizacji HACS pochodzi z Home Assistant
   Brands i po wdrożeniu może być jeszcze chwilowo przechowywana w cache.

### Changed

- Re-license v1.4.2 and later under the OSI-approved MIT License. Private and
  commercial use, modification, distribution, sublicensing and sale are
  permitted subject to retaining the MIT copyright and permission notices.
- Restore the official HACS Action as a mandatory, non-ignored release check.
- Add optimized 256 px and 512 px light/dark integration icons and prepare the
  matching official Home Assistant Brands submission used by HACS update
  entities.
- Preserve the complete v1.4 automation and dashboard feature set without an
  ESPHome runtime change.

### Polski

- Wersja v1.4.2 i kolejne przechodzą na zatwierdzoną przez OSI licencję MIT.
  Dozwolone jest użycie prywatne i komercyjne, modyfikowanie,
  rozpowszechnianie, sublicencjonowanie oraz sprzedaż z zachowaniem informacji
  wymaganych przez MIT.
- Oficjalny walidator HACS ponownie jest obowiązkowym testem wydania.
- Dodano zoptymalizowane jasne i ciemne ikony 256 px oraz 512 px i przygotowano
  zgłoszenie do Home Assistant Brands, z którego korzysta karta aktualizacji
  HACS.
- Zachowano cały zakres funkcji v1.4 bez zmian firmware ESP32.

## [1.4.1] - 2026-08-08

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.1**.
   **PL:** zaktualizuj integrację w HACS do wersji **1.4.1**.
2. **Home Assistant:** restart once after HACS finishes. This is also required
   when updating directly from 1.3.4 so the v1.4 automation, dashboard and
   Repairs are loaded. **PL:** po aktualizacji wykonaj jeden restart; dotyczy to
   również przejścia bezpośrednio z 1.3.4.
3. **ESP32 / ESPHome:** no firmware rebuild is required. Firmware **v1.3.3**
   remains runtime-compatible and HACS never flashes ESP32. **PL:** ponowne
   wgrywanie ESP32 nie jest wymagane; firmware **v1.3.3** pozostaje zgodne.
4. **Verification / Weryfikacja:** check **Installation status / Stan
   instalacji** and follow any displayed Repair until it reports **Ready /
   Gotowe**. All new automatic modes remain disabled and RCEm remains in
   observation-only mode by default. **PL:** sprawdź **Stan instalacji** i
   ewentualne Naprawy. Automatyki pozostają wyłączone, a RCEm obserwacyjny.

### Fixed

- Restore the official, byte-identical PolyForm Noncommercial 1.0.0 license
  text and document the intentional HACS official-index limitation. GitHub
  Licensee currently reports PolyForm as `NOASSERTION`, while HACS requires an
  OSI-approved license for its default catalog. Custom-repository installation
  and release-based updates remain supported.
- Preserve all v1.4.0 integration, dashboard and automation functionality; this
  hotfix contains no runtime ESPHome changes.

### Polski

- Przywrócono oficjalny, niezmodyfikowany tekst PolyForm Noncommercial 1.0.0 i
  opisano świadome ograniczenie oficjalnego katalogu HACS. GitHub Licensee
  zgłasza PolyForm jako `NOASSERTION`, a katalog domyślny HACS wymaga licencji
  OSI. Instalacja jako niestandardowe repozytorium i aktualizacje z wydań
  pozostają obsługiwane.
- Hotfix zawiera cały zakres v1.4.0 i nie wymaga ponownego wgrywania ESP32.

## [1.4.0] - 2026-08-08

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.4.0**.
   **PL:** zaktualizuj integrację w HACS do wersji **1.4.0**.
2. **Home Assistant:** restart once after HACS finishes. Managed dashboard and
   EMS files are synchronized automatically; files modified by the user are
   preserved. If Home Assistant shows a package Repair, follow it and perform
   one additional restart. **PL:** po aktualizacji wykonaj jeden restart.
   Zarządzane pliki zsynchronizują się bez nadpisywania własnych zmian. Jeżeli
   pojawi się Naprawa dotycząca pakietów, wykonaj instrukcję i uruchom HA jeszcze
   raz.
3. **ESP32 / ESPHome:** no firmware rebuild is required for existing users;
   firmware **v1.3.3** remains runtime-compatible. Rebuild only if you want the
   new optional ESPHome adoption metadata embedded in the device. HACS never
   flashes ESP32. **PL:** dotychczasowego ESP32 nie trzeba wgrywać ponownie;
   firmware **v1.3.3** pozostaje zgodne. Kompilacja jest opcjonalna wyłącznie
   dla nowych metadanych adopcji ESPHome. HACS nie aktualizuje ESP32.
4. **Verification / Weryfikacja:** open the integration device and check
   **Installation status / Stan instalacji**. Follow a displayed Home Assistant
   Repair, if present, until the status is **Ready / Gotowe**. Existing
   automatic modes remain off after the update and RCEm remains in observation
   mode by default. **PL:** sprawdź **Stan instalacji** oraz ewentualne Naprawy.
   Automatyki pozostają wyłączone, a RCEm domyślnie działa tylko obserwacyjnie.

### Added

- Add a tariff-aware grid-charge planner for G11, G12, G12w and G13 with 2026
  PGE, TAURON, ENEA, ENERGA and STOEN presets, a manual profile, seasons,
  weekends, Polish holidays, physical charge lead time and estimated savings.
- Add experimental **RCEm 253 V+**: four-day L1/L2/L3 voltage history,
  ten-minute voltage control, Solcast/load-aware battery headroom, optional
  morning pre-discharge and an explicitly enabled user-capped export regulator.
  RCEm starts in observation-only mode and does not alter grid protections,
  three-phase unbalance or GCF.
- Add scheduled LiFePO4 storage balancing. The cycle prioritizes PV, finishes
  from the grid when necessary, slows the 99â€“100% stage to approximately 2 kW,
  holds full SOC for the configured duration and restores previous settings.
- Add tariff energy/savings statistics, expanded RCE realized-revenue
  accounting, PV production archives, revenue views, RCEm diagnostics and
  mobile EMS status notifications.
- Add deterministic optimizers and Recorder history reconstruction for RCE,
  tariff charging and RCEm, plus a shared cross-system simulation matrix.

### Changed

- Rebuild RCE as an automatic up-to-48-hour optimizer. It works on today's
  complete prices before tomorrow is published, recalculates when the second
  day appears and chooses the most valuable allowed blocks instead of relying
  on a fixed minimum selling price.
- Protect outage reserve, remaining night demand and weak next-day production;
  account for Solcast live forecast error, true LOAD history, inverter count,
  battery capacity, BMS charge/discharge limits, conversion losses and the
  sale-lockout window.
- Separate true household LOAD from PV-to-LOAD and battery-to-LOAD flow totals,
  preventing duplicated consumer energy in the four-day day/night model.
- Make RCE, tariff charging and RCEm mutually exclusive. Battery balancing has
  higher priority, and manual timers retain their documented interlocks.
- Expand and reorganize the bilingual dashboard with tariff charging, RCEm,
  balancing, revenue and PV-production views; retain responsive strategy-based
  updates and alternating entity-row backgrounds.
- Update the project license for v1.4.0 and later to PolyForm Noncommercial
  1.0.0. Releases through v1.3.4 and history through the documented boundary
  remain MIT-licensed.

### Simplified installation and updates

- Always install the managed dashboard and EMS assets during config flow;
  beginners no longer need to decide whether to copy them.
- Preselect the ESPHome bridge when exactly one compatible, unconfigured device
  is present.
- Add a localized **Installation status** diagnostic with entity coverage,
  ESP32 connectivity, EMS-package readiness and a concrete next step.
- Raise a localized Home Assistant Repair when the copied EMS package is not
  enabled, including the exact safe `configuration.yaml` instruction.
- Add ESPHome `dashboard_import` metadata for easier recognition and adoption;
  firmware updates remain explicit. Add a bilingual five-step quick-start
  guide.

### Uproszczona instalacja i aktualizacja

- Dashboard i automatyka EMS są instalowane automatycznie, a jedyny zgodny
  most ESPHome zostaje podpowiedziany.
- Nowa encja **Stan instalacji** oraz Naprawy Home Assistanta pokazują dokładnie,
  czy wymagany jest restart, włączenie pakietów lub aktualizacja ESP32.
- Dodano pięciostopniowy szybki start PL/EN i metadane adopcji ESPHome.

### Safety and validation

- Preserve user-modified managed files and create rollback copies for supported
  dashboard/resource migrations.
- Keep all new automatic modes disabled after installation; RCEm is additionally
  protected by its default observation-only switch.
- Validate 276 PL/EN entities, HACS layout, Hassfest-compatible structure,
  fresh asset installation, frontend strategy/cards and ESPHome 2026.7.3
  configuration.
- Pass all deterministic optimizer/history suites, the 488-scenario CI matrix
  and the exhaustive **2064/2064** pre-release simulation matrix covering 10,
  15 and 20 kW single systems plus a 2 × 20 kW parallel system.

### Polski â€” najważniejsze zmiany

- Dodano automatyczne tanie ładowanie dla G11/G12/G12w/G13, profile pięciu
  głównych operatorów, wariant ręczny, poprawny czas potrzebny na zgromadzenie
  energii oraz obliczenia kosztów i oszczędności.
- Dodano eksperymentalne RCEm 253 V+ z historią napięć, planowaniem miejsca w
  magazynie, opcjonalnym porannym rozładowaniem i ograniczonym regulatorem
  eksportu. Domyślnie działa wyłącznie obserwacyjnie.
- Dodano okresowe wyrównywanie LiFePO4 z priorytetem PV, doładowaniem sieciowym,
  wolnym etapem 99â€“100% i utrzymaniem pełnego SOC.
- RCE wybiera teraz najbardziej opłacalne bloki z dostępnego horyzontu do 48 h,
  chroniąc dom, noc, rezerwę awaryjną i słabą prognozę kolejnego dnia.
- Poprawiono czterodniowy model LOAD, rozdzielono eksport sterowany i naturalny,
  dodano widoki zysków, produkcji PV oraz pełniejsze statystyki.
- Instalacja jest prostsza: automatyczne zasoby, podpowiedź jedynego ESP32,
  encja **Stan instalacji**, Naprawy HA i szybka instrukcja PL/EN.

## [1.3.4] - 2026-08-02

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.3.4**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.3.4**.
2. **Home Assistant:** restart Home Assistant once after HACS finishes. The
   active storage-mode dashboard and an old `/local` frontend resource are
   migrated automatically. Then refresh the browser, or fully close and reopen
   the Companion App if it still shows the cached dashboard.
   **PL:** po zakończeniu aktualizacji uruchom Home Assistant ponownie. Aktywny
   dashboard zapisany w trybie `storage` oraz stary zasób `/local` zostaną
   naprawione automatycznie. Następnie odśwież przeglądarkę albo całkowicie
   zamknij i ponownie uruchom aplikację Companion, jeżeli nadal pokazuje starą
   wersję.
3. **ESP32 / ESPHome:** no firmware rebuild or upload is required. The
   compatible firmware remains **1.3.3**.
   **PL:** ta poprawka nie wymaga kompilacji ani ponownego wgrywania ESP32.
   Zgodna wersja firmware pozostaje **1.3.3**.
4. **Verification / Weryfikacja:** open several dashboard tabs and confirm that
   entity-list rows have alternating backgrounds and there are no
   custom-card/configuration errors. Existing custom entities and layout must
   remain unchanged.
   **PL:** otwórz kilka zakładek dashboardu i sprawdź naprzemienne tło wierszy
   oraz brak błędów karty niestandardowej lub konfiguracji. Własne encje i układ
   dashboardu powinny pozostać bez zmian.

### Fixed

- Migrate the **active** Home Assistant storage-mode Hoymiles dashboard on the
  first restart after the HACS update. Earlier releases updated the bundled
  YAML/JSON assets but could leave the already imported dashboard unchanged.
- Replace only native `entities` card types with the compatible
  `custom:hoymiles-zebra-entities-card`, preserving view order, card layout,
  custom entities and all non-entities cards.
- Create exact `.pre-1.3.4.bak` rollback copies before touching Lovelace
  storage and skip unrelated dashboards and backup documents.
- Migrate and version-bust the legacy `/local/hoymiles-rce-chart-card.js`
  resource to the module served directly by the integration, then reload
  Lovelace resources when Home Assistant is already running.

### Validation

- Reproduced the HACS upgrade issue on the live miernik.com.pl installation:
  the bundled dashboard contained 50 zebra cards while the active storage
  dashboard contained none.
- Applied the equivalent migration to the live installation and confirmed six
  zebra cards in the visible main view with zero missing-entity,
  custom-element or configuration errors.
- Added fresh-upgrade tests covering layout preservation, unrelated dashboard
  isolation, exact backups, resource cache busting and migration idempotency.

### Polski

- Naprawiono aktualizację aktywnego dashboardu zapisanego w pamięci Home
  Assistanta, której wcześniejszy HACS nie zastępował mimo aktualizacji plików
  integracji.
- Migracja zachowuje kolejność widoków, układ, własne encje i pozostałe karty;
  zmienia jedynie standardowe karty encji na wersję z naprzemiennym tłem.
- Przed zmianą powstają dokładne kopie `.pre-1.3.4.bak`, a stary zasób
  JavaScript `/local` otrzymuje aktualny adres i wersję bez pamięci podręcznej.

## [1.3.3] - 2026-08-02

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.3.3**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.3.3**.
2. **Home Assistant:** restart Home Assistant after HACS finishes installing
   the update. Refresh the dashboard once after the restart.
   **PL:** po zakończeniu instalacji przez HACS uruchom Home Assistant ponownie,
   a następnie jeden raz odśwież dashboard.
3. **ESP32 / ESPHome:** rebuild and upload are **required**. Download or refresh
   `hoymiles-inverter.yaml` from release **v1.3.3**, preserve your local secrets
   and pin substitutions, select **Clean Build Files**, then compile and upload
   the firmware to the ESP32. A HACS update alone does not update ESP32
   firmware.
   **PL:** pobierz lub odśwież `hoymiles-inverter.yaml` z wydania **v1.3.3**,
   zachowaj lokalne sekrety i ustawienia pinów, wybierz **Clean Build Files**,
   a następnie skompiluj i wgraj firmware do ESP32. Sama aktualizacja HACS nie
   aktualizuje firmware ESP32.
4. **Verification / Weryfikacja:** confirm that dashboard values keep updating.
   Open only one ESPHome log window and verify
   `Successful handshake with hoymiles-inverter`. If it loops on
   `SocketClosedAPIError` while entities
   still update, close all log windows and restart only the ESPHome Device
   Builder add-on.
   **PL:** sprawdź odświeżanie danych dashboardu. Otwórz tylko jedno okno logów
   ESPHome i potwierdź `Successful handshake with hoymiles-inverter`.
   Jeżeli pojawia się pętla `SocketClosedAPIError`, ale encje działają, zamknij
   wszystkie okna logów i uruchom ponownie tylko dodatek ESPHome Device Builder.

### Changed

- Stagger ESPHome polling intervals to 13 s for live Modbus blocks, 5 s for EMS
  controls, 20 s for settings and 150 s for the full diagnostic map. This
  prevents overlapping full-map requests while preserving responsive controls.
- Keep API encryption and the eight-client safety limit, disable API-only
  rebooting, and restrict verbose Modbus component logs while retaining useful
  INFO diagnostics.
- Align the Home Assistant integration, ESPHome package, public entry files and
  release validator at version 1.3.3.
- Replace the README dashboard screenshots with the current live energy-flow
  and RCE automation views.
- Document recovery from stale ESPHome Device Builder log sessions without
  rebooting the inverter, reflashing the ESP32 or increasing API client limits.

### Validation

- Compiled and uploaded the v1.3.3 ESPHome configuration on the live parallel
  inverter installation at miernik.com.pl.
- Confirmed uninterrupted dashboard updates and continuously increasing ESP32
  uptime while reproducing and diagnosing the log-viewer failure.
- Confirmed exactly eight stale Device Builder API sessions at failure time;
  restarting only that add-on cleared the sessions and restored the log stream.
- Verified the API encryption key match and excluded inverter, Modbus, Wi-Fi,
  Home Assistant Core and ESP32 restart faults as the cause.

### Polski

- Rozłożono odczyty ESPHome: 13 s dla szybkich bloków Modbus, 5 s dla EMS,
  20 s dla ustawień i 150 s dla pełnej mapy diagnostycznej.
- Zachowano szyfrowanie i limit ośmiu klientów API, wyłączono restart wywołany
  wyłącznie brakiem klienta API oraz ograniczono szczegółowe logi Modbus.
- Ujednolicono wersję integracji, pakietów ESPHome i plików wejściowych do 1.3.3.
- Zaktualizowano zrzuty dashboardu i opisano bezpieczne odzyskanie logów przez
  restart wyłącznie dodatku ESPHome Device Builder.

## [1.3.2] - 2026-08-02

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.3.2**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.3.2**.
2. **Home Assistant:** restart Home Assistant after HACS finishes installing
   the update. If the previous dashboard appearance remains cached, perform a
   hard browser refresh or fully close and reopen the Companion App.
   **PL:** po zakończeniu instalacji przez HACS uruchom Home Assistant ponownie.
   Jeśli nadal widać poprzedni wygląd dashboardu, wykonaj pełne odświeżenie
   przeglądarki albo całkowicie zamknij i ponownie uruchom aplikację Companion.
3. **ESP32 / ESPHome:** no firmware rebuild or upload is required for this
   release. The compatible ESPHome firmware version remains **1.3.1**.
   **PL:** ta wersja nie wymaga ponownej kompilacji ani wgrywania firmware
   ESP32. Zgodna wersja firmware ESPHome pozostaje **1.3.1**.
4. **Verification / Weryfikacja:** open several dashboard tabs and confirm that
   entity-list rows have alternating, theme-aware backgrounds and that the
   dashboard loads without a custom-card error.
   **PL:** otwórz kilka zakładek dashboardu i sprawdź, czy wiersze list encji
   mają naprzemienne tło dopasowane do motywu oraz czy dashboard ładuje się bez
   błędu karty niestandardowej.

### Changed

- Added alternating, theme-aware row backgrounds to every entities card in all
  15 Polish and English dashboard views. The subtle contrast follows light and
  dark Home Assistant themes and improves scanning of long register lists.
- Kept the native Home Assistant entities-card behavior, including row clicks,
  more-info dialogs, history access, state colors and writable controls.
- Included the dashboard-strategy registration and frontend cache-busting fix
  prepared in version 1.3.1, which had not received a public release tag.

### Validation

- Verified all 15 dashboard views on a live Home Assistant installation: all
  50 zebra cards loaded without custom-element, configuration or resource
  errors.
- Verified Polish/English dashboard parity, generated YAML/JSON assets, the
  JavaScript module and the complete HACS release test suite.

### Polski

- Dodano delikatne, naprzemienne tło wierszy do wszystkich kart encji w 15
  zakładkach polskiego i angielskiego dashboardu.
- Zachowano standardowe działanie kart Home Assistant: podgląd historii,
  okna „więcej informacji”, kolory stanów oraz sterowanie encjami.
- Wydanie zawiera również poprawkę automatycznej rejestracji dashboardu i
  odświeżania zasobu JavaScript przygotowaną wcześniej jako 1.3.1.

## [1.3.1] - 2026-08-01

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to version **1.3.1**.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS do
   wersji **1.3.1**.
2. **Home Assistant:** restart Home Assistant. The dashboard strategy module is
   now registered automatically; no manual Lovelace resource is required.
   **PL:** uruchom Home Assistant ponownie. Moduł strategii dashboardu jest od
   tej wersji rejestrowany automatycznie i nie wymaga ręcznego zasobu Lovelace.
3. **ESP32 / ESPHome:** refresh `hoymiles-inverter.yaml`, keep your substitutions
   and secrets, then compile and upload the firmware. This restores the complete
   276-entity set on installations upgraded from older firmware.
   **PL:** odśwież `hoymiles-inverter.yaml`, zachowaj własne podstawienia i
   sekrety, następnie skompiluj oraz wgraj firmware do ESP32. Przywraca to pełny
   zestaw 276 encji po aktualizacji ze starszego firmware.
4. **Verification / Weryfikacja:** wait for ESPHome to reconnect, reload the
   dashboard once and confirm that no strategy timeout or unavailable GCF/GEN
   entities remain.
   **PL:** poczekaj na połączenie ESPHome, przeładuj dashboard jeden raz i
   sprawdź, czy nie ma błędu strategii ani niedostępnych encji GCF/GEN.

### Fixed

- Automatically register the dashboard strategy and custom-card JavaScript
  through Home Assistant's frontend API with a release-version cache buster.
  This fixes dashboard timeouts after HACS upgrades that retained the legacy
  `/local/hoymiles-rce-chart-card.js?v=1.2.0` resource.
- Avoid duplicate custom-card metadata when the frontend module is loaded more
  than once during migration from a legacy manual Lovelace resource.
- Align integration, ESPHome package and release metadata at version 1.3.1.

## [1.3.0] - 2026-08-01

### User update steps / Kroki po aktualizacji

1. **HACS:** update **Hoymiles HIT xxL G3 Modbus** to the new version.
   **PL:** zaktualizuj integrację **Hoymiles HIT xxL G3 Modbus** w HACS.
2. **Home Assistant:** restart Home Assistant after HACS finishes installing
   the update.
   **PL:** po zakończeniu instalacji przez HACS uruchom Home Assistant ponownie.
3. **ESP32 / ESPHome:** this update changes the ESPHome firmware packages.
   Download or refresh `hoymiles-inverter.yaml` from the same release, preserve
   your substitutions and secrets, then compile and upload the firmware to the
   ESP32. A HACS update alone does not update ESP32 firmware.
   **PL:** ta aktualizacja zmienia pakiety firmware ESPHome. Pobierz lub odśwież
   `hoymiles-inverter.yaml` z tego samego wydania, zachowaj własne podstawienia
   i sekrety, a następnie skompiluj i wgraj firmware do ESP32. Sama aktualizacja
   HACS nie aktualizuje ESP32.
4. **Verification / Weryfikacja:** wait until the ESPHome device reconnects,
   reload the integration if necessary and confirm that
   `firmware_update_required` is no longer active and the dashboard has no new
   unavailable entities.
   **PL:** poczekaj na ponowne połączenie ESPHome, w razie potrzeby przeładuj
   integrację i sprawdź, czy `firmware_update_required` nie jest aktywne oraz
   czy dashboard nie pokazuje nowych niedostępnych encji.

### Added

- Added GitHub sponsorship metadata and bilingual support links in the
  repository documentation and Home Assistant dashboard.
- Added a two-day RCE profit optimizer that selects the most valuable allowed
  30-minute export blocks across the complete today/tomorrow horizon instead
  of relying on a fixed user price threshold.
- Added reconstructed four-day LOAD and protected-night history from Home
  Assistant Recorder, including separate PV-to-load, battery-to-load and
  grid-to-load accounting.
- Added BMS-aware discharge-power diagnostics and automatic limiting based on
  the battery voltage/current capability, inverter count and configured power.
- Added realized RCE-controlled export, natural PV surplus, unclassified
  export, revenue and optimization-benefit statistics.
- Added dedicated **Profits** and **PV production** dashboard views with daily,
  weekly, monthly and yearly statistics, plus GEN live values and chart data.
- Added optional mobile push notifications for EMS mode and inverter work-state
  changes.
- Added Generation Control Function export limiting and GEN-port mode controls.

### Changed

- The RCE reserve now protects the emergency Self-Use reserve, user safety
  correction and the modeled home demand through the next protected night,
  while still making room for forecast PV that would otherwise be exported at
  a lower price.
- The RCE plan works safely with today's prices before PSE publishes tomorrow's
  complete dataset and automatically expands to 48 hours when it becomes
  available.
- Dashboard LOAD values and the power-flow animation now use the dedicated
  system load registers `2169-2172`, polled by the fast controller, rather than
  inverter conversion consumption or BMS-derived approximations.
- The dashboard uses compact localized labels without the repeated
  `Hoymiles Inverter` prefix, presents four separate PV string cards, a GEN
  card, clearer grid/load tables and two-decimal power-flow values.
- Battery charge/discharge time uses the reported effective battery capacity,
  so single and parallel storage systems scale automatically.

### Fixed

- EMS mode and the related `4300-4306` SOC/power settings are now refreshed
  as one dedicated Modbus block every 5 seconds. Other user-facing inverter,
  battery, GCF and GEN settings refresh in grouped ranges every 15 seconds.
  Changes made in the S-Miles application are therefore reflected promptly
  without accelerating the full diagnostic register map.
- Increased the ESPHome API connection backlog and send queue for the large
  inverter entity set, reducing disconnects while Home Assistant and ESPHome
  diagnostics are connected at the same time.
- The dashboard energy-flow animation now calculates charging and discharging
  time from the inverter's effective battery-capacity entity instead of a
  hardcoded 21 kWh. The dashboard wrapper converts the entity from kWh to the
  Wh required by the underlying power-flow card and follows later capacity
  changes automatically.
- Parallel EMS mode changes now send the complete `4300-4306` block as an
  FC16 broadcast to Modbus address `0` after a valid Master topology is
  detected. A live two-inverter HIT-20L-G3 test reached 34.86 kW export at an
  80% discharge setting, confirming that both Master and Slave receive the
  command.
- Broadcast frames bypass the response-waiting `ModbusController` queue,
  preventing polling from stalling because Modbus broadcast address `0` never
  returns a response. Single-inverter installations keep the normal addressed
  write path.
- Corrected the sign convention used by the animated battery and grid flows.
- Corrected 0.1% scaling for battery charge/discharge limits and limited the
  low-SOC grid-charge register to the inverter-safe maximum of 1000 W.
- Removed the unverified writable Three Phase Unbalance entity and dashboard
  control. It must not be published again until its Master/Slave write
  semantics are confirmed.
- Improved ESP32 Wi-Fi/API stability and separated fast live registers,
  5-second EMS reads, 15-second user settings and slow diagnostic polling so
  frequent dashboard refreshes do not overload Modbus.

### Safety

- Parallel EMS writes remain blocked for an ESP32 connected to a Slave, for an
  incomplete topology, or for an invalid reported inverter count.
- RCE export cannot reduce the modeled end-of-horizon SOC below the protected
  Self-Use reserve plus safety correction and protected home demand.
- Automatic Three Phase Unbalance control is intentionally absent from this
  release after live testing showed unsafe/ambiguous parallel behavior.

### Validation

- Verified the complete production configuration on a live two-inverter
  HIT-20L-G3 installation: 15 dashboard views, 276 localized entities, complete
  Solcast/PSE data, four-day LOAD history and a two-day optimized RCE plan.
- Compiled and uploaded the matching ESPHome configuration, then verified the
  device reconnect, all dashboard entities and stable Modbus polling.
- Added deterministic optimizer and Recorder-history tests and expanded the
  release validator for generated PL/EN assets, firmware markers, safety
  limits, dashboards and HACS-visible update instructions.

### Polski

- Przebudowano automatykę RCE: wybiera najdroższe bloki z pełnego horyzontu
  dzisiaj/jutro, chroni energię domu do końca kolejnej nocy i rozdziela eksport
  sterowany od naturalnej nadwyżki PV.
- Dodano historię LOAD z czterech dni, ograniczenie mocy według BMS, statystyki
  przychodu, zakładki **Zyski** i **Produkcja PV**, powiadomienia push, sterowanie
  GCF/GEN oraz szybkie rzeczywiste pomiary odbiorników.
- Uporządkowano odświeżanie Modbus i stabilność API ESP32, naprawiono skalowanie
  limitów mocy oraz kierunki animacji przepływu energii.
- Usunięto niezweryfikowaną encję asymetrii trójfazowej, aby nie dopuścić do
  niejednoznacznego zapisu w układach Master/Slave.

## [1.2.1] - 2026-07-29

### Added

- Added an official Home Assistant dashboard strategy. A dashboard created
  from `custom:hoymiles-hit-xxl-g3` always loads the Polish or English
  configuration bundled with the installed HACS version.
- Added a stable, no-cache integration URL for the dashboard card and inverter
  image, removing the need to change resource query strings after updates.
- Added managed-asset metadata. Files installed by an older release are
  updated automatically only while they remain unmodified by the user.
- Added a native two-day RCE optimizer using today's and tomorrow's PSE
  prices, Solcast forecasts, battery capacity, inverter power, parallel
  inverter count and the four-day LOAD model.
- Added forecast export-energy and estimated RCE-revenue statistics.
- Added explicit optimizer diagnostics, including the protected home energy,
  calculated minimum SOC, planning horizon and selected 30-minute export
  blocks.

### Fixed

- New catalog entities now exist as unavailable proxies when ESPHome firmware
  is older than the HACS integration. Dashboards no longer report missing
  entities while waiting for a firmware update.
- Proxy unique IDs are independent of ESPHome source unique IDs, so an entity
  created before a firmware update becomes available in place without being
  duplicated.
- Writable proxy entities now reject commands with a clear firmware-update
  error when their corresponding ESPHome source does not exist.
- Missing or incomplete PSE prices for tomorrow no longer block profitable
  exports today. The optimizer creates a safe today-only plan and
  automatically expands and recalculates it when the complete next-day data
  appears.
- Fixed a numerical rounding error that could make the final export plan
  exceed the protected battery reserve by a fraction of a kilowatt-hour and
  incorrectly end with `optimizer_error`.
- The tomorrow RCE chart now explains that today's plan remains active while
  the next-day prices are waiting for publication.

### Migration

- Existing storage-mode dashboards can be reduced to:
  `strategy: {type: custom:hoymiles-hit-xxl-g3}` after adding the stable
  integration resource URL once.
- ESPHome remains a separate firmware component. New register data becomes
  live after compiling and flashing the matching tagged ESPHome package.

### Validation

- Added deterministic optimizer scenarios for today-only operation, reserve
  safety, price selection across two days, parallel power scaling, lockout
  periods and fractional final export slots.
- Rebuilt and validated 275 localized entities, both dashboard languages,
  Home Assistant packages and the custom RCE chart card.

## [1.2.0] - 2026-07-28

### Added

- Added automatic single/Master/Slave topology detection using registers
  `6048-6095`, including localized topology and EMS-control readiness entities.
- Added dashboard diagnostics for the detected role, inverter count and
  communication addresses of up to nine Slave devices.
- Added a transparent Hoymiles inverter illustration and use it as the central
  device graphic in the animated dashboard power-flow card.

### Changed

- EMS writes now use the detected Master as the single control point for a
  parallel system of 2-10 inverters.
- In a detected Master system, the overview and dashboard power flow now use
  the manufacturer's system-wide PV, battery, LOAD and grid registers. The
  displayed values and energy-flow animation therefore represent all detected
  inverters instead of only the Master.
- Battery current in the overview is derived from the system-wide battery
  power and the physical DC voltage when parallel operation is active.
- Internal parallel-device addresses are no longer polled as external Modbus
  slave IDs. The Master's RS485 port does not route those requests; the
  topology addresses remain available as diagnostics only.
- The standard ESPHome entry files load parallel topology diagnostics before
  writable EMS settings.
- The main dashboard now shows per-phase and total grid active power instead
  of the less useful phase-current card. Home Assistant automatically presents
  the readings in W or kW.
- Grid active power registers `1808-1815` now use the dedicated 5-second
  polling controller together with the other live dashboard values.

### Fixed

- The dynamic RCE reserve now protects the complete upcoming night window
  throughout the daytime. Previously the protected duration fell to zero
  between the morning buffer and 90 minutes before sunset, allowing an
  evening export to reserve too little energy for the home.
- The next-day PV deficit is now added to the protected night energy instead
  of replacing it only when larger. A forecast that merely covers the average
  home demand therefore cannot erase the overnight reserve.
- Clarified that the night-window binary sensor indicates consumption
  sampling, not whether SOC protection is enabled.
- The Hoymiles inverter illustration is now embedded in the power-flow SVG,
  so it scales with the complete diagram on phones, tablets and desktop
  browsers. The original inverter symbol is hidden to prevent overlap.

### Safety

- EMS writes are blocked when the ESP32 is connected to a device reporting the
  Slave role, before topology detection completes, or when the Master reports
  an invalid inverter count.
- Register `3016` remains disabled by default and is never invoked
  automatically, preventing unintended creation or disassembly of a parallel
  network.

### Validation

- Rebuilt and validated 275 localized entities and all Polish/English HACS
  assets.
- Verified the animated power-flow card on desktop and at a 390 x 844 mobile
  viewport.

## [1.1.0] - 2026-07-27

### Added

- Added an optional dynamic RCE minimum-SOC model using the BJReplay Solcast
  forecast for tomorrow, the inverter battery capacity and the trailing
  four-day LOAD average.
- Added a protected home-energy calculation for the period from 90 minutes
  before sunset until 90 minutes after sunrise.
- Added protected-window consumption history. After the first complete window,
  the reserve uses the measured average from up to four previous nights.
- Added an adjustable positive SOC safety correction for forecast uncertainty.
- Added dashboard controls and diagnostics for the Solcast source, forecast,
  LOAD averages, protected night window, calculated energy reserve and
  effective minimum SOC.
- Added automatic detection of Polish and English BJReplay Solcast
  `Forecast Tomorrow` entity IDs, with a configurable entity override.
- Added safe legacy-ID migration for installed dashboard and EMS package files.
  A `.pre-stable-entity-ids.bak` copy is created before the first migration.

### Changed

- RCE export now protects the greater of the remaining night-time home demand
  and the next-day PV energy deficit, in addition to the Self-Use outage
  reserve and the user safety correction.
- The protected night reserve decreases as the installation approaches
  90 minutes after sunrise, allowing only energy not required by the home to
  be exported.
- Distributed Polish and English dashboards and EMS packages now use only
  stable `hoymiles_hit_*` proxy IDs, independent of the ESPHome device name.
- The source dashboard and source EMS package use the same stable IDs as the
  generated HACS assets.
- Updated the public ESPHome package reference and integration metadata to
  `v1.1.0`.

### Fixed

- Fixed missing entities across Start, PV, LOAD/EPS, Battery, Grid, Flows,
  Inverter, Generator, Meters and Diagnostics after updating an installation
  whose ESPHome entity IDs used a device-name prefix.
- Prevented future releases from containing device-name-dependent dashboard
  IDs or stable IDs missing from the generated entity catalog.
- Documented that a dashboard pasted into Home Assistant storage mode must be
  imported again after an asset update because HACS cannot rewrite its stored
  Lovelace configuration.

### Safety

- Dynamic RCE export fails safe when the Solcast forecast, LOAD history,
  battery capacity, sun data or calculated reserve is unavailable.
- Manual EMS schedules retain priority over RCE automation.
- The export lockout window and minimum-SOC checks remain authoritative.

### Validation

- Rebuilt 273 localized entities and all Polish/English HACS assets.
- Added tests for fresh installs, legacy dashboard and EMS migration, exact
  backups, idempotence, stable catalog references and dynamic reserve markers.
- Validated the source and generated YAML assets and Python syntax.

### Polski

- Dodano dynamiczną rezerwę SOC dla automatyki RCE, obliczaną z prognozy
  Solcast BJReplay na jutro, średniego zużycia LOAD z czterech dni, pojemności
  magazynu, rezerwy awaryjnej Self-Use i korekty bezpieczeństwa użytkownika.
- Dodano ochronę energii potrzebnej domowi od 90 minut przed zachodem do
  90 minut po wschodzie słońca oraz średnią z maksymalnie czterech poprzednich
  chronionych okien nocnych.
- Sprzedaż RCE jest blokowana przy brakujących lub nieprawidłowych danych.
- Dashboard i pakiet EMS używają wyłącznie stabilnych identyfikatorów
  `hoymiles_hit_*`, niezależnych od nazwy urządzenia ESPHome.
- Naprawiono braki encji występujące po aktualizacji 1.0.2 we wszystkich
  zakładkach dashboardu.
- Instalator migruje stare identyfikatory w plikach dashboardu i EMS, zachowując
  przed zmianą kopię `.pre-stable-entity-ids.bak`.
- Rozszerzono walidację nowych instalacji, migracji, tłumaczeń PL/EN i pełnej
  zgodności dashboardu z katalogiem encji.

## [1.0.2] - 2026-07-27

### Added

- Added a localized Clear Fault button that writes value `1` to holding
  register `3004`.
- Added inverter-side battery current and voltage entities for installations
  where BMS communication is absent or reports different values.
- Added explicit dashboard names for every visible entity in both Polish and
  English.

### Changed

- Dashboard labels no longer repeat the `Hoymiles Inverter` device prefix.
- Battery current and voltage on the overview now use physical inverter
  measurements instead of BMS telemetry.
- Alarm and connectivity sections use native Home Assistant entity rows again,
  including tap-to-open state history.
- The Polish and English dashboards support both stable HACS entity IDs and
  migrated IDs from existing installations.
- Improved dashboard layout, live power presentation, grid phase tables,
  energy-flow precision and inverter control grouping.

### Fixed

- Restored native alarm display formatting after the temporary custom status
  table caused poor readability.
- Corrected missing or English-only Polish labels for writable battery and EMS
  controls.
- Fixed dashboard references that could show missing entities on a fresh HACS
  installation or after entity-ID migration.
- Removed stale PV5/PV6 and redundant dashboard references from the generated
  assets.

### Validation

- Release validation now checks the Clear Fault register command, physical
  battery entities, localized short dashboard names, legacy/stable entity-ID
  compatibility and fresh Polish/English asset installation.
- Rebuilt and verified all generated catalogs, translations and bundled
  dashboards.

### Polski

- Dodano przycisk „Wyczyść alarm”, zapisujący wartość `1` do rejestru `3004`.
- Dodano fizyczny prąd i napięcie baterii mierzone przez falownik, niezależne od
  telemetrii BMS.
- Usunięto przedrostek „Hoymiles Inverter” z nazw widocznych na dashboardzie.
- Przywrócono standardowy wygląd alarmów i statusów wraz z otwieraniem historii
  po kliknięciu.
- Poprawiono polskie tłumaczenia ustawień baterii i EMS.
- Dashboardy PL/EN obsługują zarówno stałe identyfikatory encji HACS, jak i
  identyfikatory zmigrowane z wcześniejszych instalacji.
- Rozszerzono testy nowych instalacji, tłumaczeń, rejestrów i zgodności
  wygenerowanych zasobów.

## [1.0.1] - 2026-07-26

### Added

- Documented the required HACS-first installation order for the integration and
  ESPHome package.
- Added the ESP32-to-RS485-to-inverter wiring table for GPIO17/GPIO16, A+/B-,
  power and Modbus ground.
- Added dashboard screenshots for the live energy flow and RCE automation.
- Expanded the README with installation, update and troubleshooting guidance.

### Fixed

- Corrected standalone ESPHome installation so public remote packages resolve
  from the tagged GitHub release.
- Entity IDs are now stable and independent of the Home Assistant language.
- Existing localized proxy entity IDs are migrated automatically during setup.
- EMS and RCE packages can resolve the EMS mode, discharge SOC and battery SOC
  entities after installation on Polish and English Home Assistant instances.
- The RCE card supports both the current Home Assistant states context and the
  legacy `hass` setter.
- The dashboard wraps the custom RCE chart in a standard stack to prevent an
  asynchronous custom-card loading race.
- The RCE resource cache key was increased to `v=1.0.1`.

### Validation

- Added a headless RCE card registration and rendering test to CI.
- Added release checks for the stable entity ID property and migration hook.

### Polski

- Opisano wymaganą kolejność instalacji: najpierw repozytorium przez HACS, a
  następnie konfiguracja urządzenia ESPHome.
- Dodano tabelę podłączenia ESP32, konwertera RS485 i falownika dla GPIO17/GPIO16,
  A+/B-, zasilania oraz masy Modbus.
- Dodano zrzuty dashboardu przepływu energii i automatyki RCE.
- Rozszerzono instrukcję instalacji, aktualizacji i diagnostyki.
- Naprawiono samodzielną instalację ESPHome oraz publiczne odwołania do pakietów
  z oznaczonego wydania GitHub.
- Identyfikatory encji są teraz stałe i niezależne od języka Home Assistanta.
- Istniejące, przetłumaczone identyfikatory encji są automatycznie migrowane.
- Naprawiono odwołania automatyki EMS/RCE do trybu EMS, minimalnego SOC
  rozładowania oraz SOC baterii.
- Karta RCE obsługuje aktualny mechanizm przekazywania stanów w Home Assistant.
- Dodano automatyczny test rejestracji i renderowania karty RCE.

## [1.0.0] - 2026-07-24

First public release.

### Added

- HACS-compatible Home Assistant custom integration.
- 271 localized sensor, number and select entities.
- English and Polish entity names, select options, config flow and services.
- ESPHome remote package for Hoymiles HIT xxL G3 Modbus RTU.
- Four PV inputs, grid, EPS/load, battery/BMS, generator and diagnostic data.
- Atomic EMS writes for registers 4300–4306.
- Daily charge and discharge schedules.
- PSE RCE price automation with a configurable export lockout.
- Ready-to-use Home Assistant dashboard and localized RCE chart.
- HACS, Hassfest and project-specific validation workflows.

### Safety

- Writable inverter settings remain the responsibility of the installer.
- Battery, grid-code and Modbus register limits must be verified for the exact
  inverter and firmware before use.

---

## Polski

Pierwsze publiczne wydanie integracji:

- instalacja integracji Home Assistant przez HACS;
- 271 encji z nazwami i opcjami po polsku oraz angielsku;
- firmware ESPHome jako zdalny pakiet GitHub;
- cztery wejścia PV, sieć, EPS/odbiorniki, bateria/BMS, generator i diagnostyka;
- bezpieczny zapis całego bloku EMS 4300–4306;
- harmonogramy ładowania i rozładowania;
- automatyka cenowa RCE PSE z blokadą sprzedaży;
- gotowy dashboard i wykres RCE;
- automatyczne testy HACS, Hassfest i testy struktury projektu.
