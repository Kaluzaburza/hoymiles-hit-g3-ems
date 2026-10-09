# Diagnostics / Diagnostyka

The integration provides three access methods for support data. Start with the
one-click dashboard ZIP or native Home Assistant report. Use the terminal
archive when the fault concerns disconnects, ESPHome, Modbus communication, an
automation loop or a failed startup.

## Download one ZIP from the dashboard

Open **EMS settings → Service** and press **Collect data and download ZIP**.
An administrator's browser receives a fresh ZIP containing the native report,
a requested 24-hour window of significant control history and filtered Home
Assistant Core logs. The archive is built in memory and is not left in
`/config`.

Email the ZIP to [info@kaluzaaa.com](mailto:info@kaluzaaa.com) together with a
description of the problem, what you expected and the exact local date and time
when it occurred.

Home Assistant Core cannot open a live ESPHome device-log session. For a
low-level UART/Modbus fault, also save the relevant excerpt from ESPHome Device
Builder. The current ESPHome entity states are already present in the ZIP.

## One-click Home Assistant report

1. Open **Settings → Devices & services → EMS for Hoymiles HIT-(5–20)L-G3**.
2. Open the integration entry menu and choose **Download diagnostics**.
3. Attach the downloaded JSON file to the issue together with the exact local
   time of the fault and a short description of the expected behaviour.

The report contains integration/firmware versions, entity coverage, current
Hoymiles states, calculation attributes and a requested 24-hour window of
significant control changes. Fast telemetry history is deliberately omitted so
the report remains small and does not overload Recorder.

For selected EMS entities the control history also records allowlisted
attribute-only transitions: compact RCE, tariff and RCEm decisions and
freshness, plan-timeline context, automation execution times, active-writer
ownership, balancing state, aggregate response and Supervisor transaction
evidence. Repeated states which differ only by their Recorder timestamps are
counted and compacted before retention limits are applied. Each entity reports
the queried and retained time coverage, semantic duplicate count, omitted and
dropped-event count. The export keeps the newest evidence and is bounded to 500
semantic events per entity, 4,000 events and 8 MiB of encoded history-event
payload, with a 64 KiB limit per event. These are history limits, not a claim
that the whole JSON or ZIP is 8 MiB. A selected entity with no Recorder rows is
reported as unknown and makes coverage incomplete; it is never interpreted as
proof that no control action occurred.

The dashboard archive also bounds collection and transfer: at most 32
config-entry reports are exported (with at most one extra iterator probe to
declare truncation), an individual report is limited to 12 MiB, the diagnostic
JSON member to 20 MiB, and the filtered Core-log member to 2 MiB and the newest
2,500 matching lines. Combined uncompressed members are limited to 23 MiB and
the final compressed ZIP to 24 MiB. Data which does not fit is replaced
atomically by an omission marker; metadata reports incomplete collection
instead of treating omitted evidence as healthy. These are resource bounds,
not evidence that every requested historical event was available.

Rich-history profiles currently use the managed default entity IDs. If an
entity was renamed in Home Assistant, its current state remains useful when it
is still selected for the report, but the report may not attach the specialised
history profile. Mention renamed entities when submitting a support archive.
With multiple configured Hoymiles entries, each native report currently
includes the installation-wide managed-state snapshot and selected history;
the per-entry catalog section remains the reliable entry-specific context.

Every Home Assistant installation receives one random UUID v4 named
`anonymous_installation_id`. It is generated without using device, network,
account or config-entry data and is stored by Home Assistant in
`/config/.storage/hoymiles_hit_modbus.installation_identity`. It survives
restarts and integration updates, is shared by every Hoymiles config entry in
the same Home Assistant, and is deliberately retained in support reports only
to correlate archives from that installation over time. Its current
`installation_id_schema_version` is `1`; it is not exposed as an entity.

For offline comparison of many dashboard ZIPs, use the privacy-aware batch
analyzer described in [DIAGNOSTICS_ANALYZER.md](DIAGNOSTICS_ANALYZER.md). It
groups successive archives by the anonymous installation ID, evaluates RCE,
RCEm and tariff charging with versioned rules, and produces JSON, CSV,
Markdown and HTML reports without extracting the input archives.

### Current EMS evidence (2026-10-03)

The exporter decodes Recorder's compact Supervisor execution and compressed
STOP records before privacy filtering. Frozen STOP inputs, transaction proof,
tariff decisions and RCE physical-export evidence remain structured JSON.
Corrupt or unsupported STOP storage is marked unavailable; it is never turned
into an empty, supposedly complete journal. The encoded payload is not copied
into the support report.

Current snapshots include live lease state/renewal, command/readback evidence,
deadlines, planner input/result revisions, Pstryk joint revisions, PV-delay
state and LOAD model provenance. Rich history keeps the corresponding
allowlisted fields **only when Recorder actually retained them**. In particular,
tariff-plan attributes excluded from Recorder cannot be reconstructed from the
current plan; an expired LOAD profile is distinct from loss of Recorder data.
`full_plan_solver_calls` is an attempt counter: pair it with `last_full_plan_at`,
publication and `result_current`. Arbitration revision alone is not a full
replan. Pstryk uses `joint_plan_revision` after optimize/revalidate.

`notification_history` reads the manager's already-loaded last 32 delivery
records, active logical range, transaction IDs and persistence revisions.
Reading it performs no provider call or Store write. Provider success means
acceptance by HA/provider, not verified delivery to a phone. Missing manager
state and incomplete persistence are explicit. This is a retained ledger,
not an unbounded lifetime notification history.

Opaque transaction, lease, range/event and revision identifiers use stable
`diag-id-…` pseudonyms instead of a shared redaction marker, preserving
correlation across evidence sections and repeat ZIP captures. Credentials and
network identifiers remain masked. Repeated sanitization preserves pseudonyms.

`evidence_contract` states the remaining limits: current snapshots describe
export time; historical coverage is bounded; the report does not contain every
accepted 20-second lease renewal, per-Slave FC03, phone delivery confirmation
or deployed source hashes. A last accepted renewal and STOP source frame cannot
prove uninterrupted lease continuity. Use version/hash receipts and bounded
device journals separately when such acceptance is required. This update does
not increase Modbus polling or Recorder writes.

### Planner readiness

Execution authority comes from the optimizer entity itself: `result_current`,
plan report age and source-freshness attributes. A recently updated derived
proxy does not make an old plan executable. For RCEm, battery capacity is a
stable property and its report age is diagnostic; live BMS voltage and current
limits remain fail-closed freshness inputs. The control-owner sensor state
reports active actuator ownership, while `owner_code=manual` remains the stable
machine-readable compatibility fallback when no automatic writer is active;
the `*_policy_enabled` attributes show configuration only. Optional Solcast Day
3 exposes its selected entity, signed age, freshness and fallback reason in the
planner attributes.

RCE and tariff-plan attributes also expose `forecast_learning_enabled`,
`forecast_learning_mode`, `forecast_learning_excluded_reason` and the effective
`forecast_factor_used`. On a physically verified exact 0% GCF export limit the
contract is `false / fixed_zero_export / zero_export / 0.80`. Missing or stale
GCF evidence has its own conservative reason and must not be interpreted as a
confirmed zero-export site.

### Master changes mode but a Slave does not

First inspect the physical external Modbus/RS485 bus. The ESP32 converter,
Master and every Slave must share the same A/B/reference bus; address `0` is a
wire-level broadcast and the Master does not relay it over the inverter's
internal parallel network. Check polarity, continuity and end termination
against the manufacturer manual. A fresh topology, system-wide telemetry and
a matching Master FC03 do not verify the Slave branch. During a conservative
Grid Discharge and return-to-Self-Use test, record each inverter separately in
the manufacturer application.

## Extended terminal archive

In the **Terminal & SSH** add-on run:

```sh
sh /config/custom_components/hoymiles_hit_modbus/collect_diagnostics.sh
```

The command prints the path of one `.tar.gz` file in
`/config/hoymiles_diagnostics/`. Download that file with File editor, Studio
Code Server, Samba or SSH and attach it to the issue.

The archive adds Home Assistant/Supervisor/host versions, storage and memory
information, relevant redacted Core and ESPHome logs, a redacted ESPHome entry
configuration and — when the local API permits it — the native diagnostic JSON.

Passwords, API keys, tokens, Wi-Fi identifiers, URLs, IP/MAC addresses, serial
numbers and the source device ID are masked. The command never copies
`secrets.yaml` or the Home Assistant `.storage` database. Automatic masking is
not a substitute for review: inspect the archive before publishing it in a
public issue.

---

Integracja udostępnia trzy sposoby zebrania danych. Zacznij od ZIP-u z
dashboardu albo raportu natywnego. Paczki terminalowej użyj, gdy problem
dotyczy rozłączeń, ESPHome, komunikacji Modbus, pętli automatyzacji albo
nieudanego uruchomienia.

## Pobranie jednego ZIP-u z dashboardu

Otwórz **Ustawienia EMS → Serwis** i naciśnij **Zbierz dane i pobierz ZIP**.
Przeglądarka administratora otrzyma świeżą paczkę zawierającą natywny raport,
żądane 24-godzinne okno istotnych zmian sterowania i odfiltrowane logi HA Core.
Paczka powstaje w pamięci i nie pozostaje w katalogu `/config`.

Wyślij ZIP na [info@kaluzaaa.com](mailto:info@kaluzaaa.com) razem z opisem
problemu, oczekiwanym zachowaniem oraz dokładną lokalną datą i godziną jego
wystąpienia.

Proces Home Assistant Core nie może sam otworzyć sesji logów urządzenia
ESPHome. Przy niskopoziomowym błędzie UART/Modbus zapisz dodatkowo odpowiedni
fragment z ESPHome Device Builder. Bieżące stany encji ESPHome są już w ZIP-ie.

## Raport Home Assistant jednym kliknięciem

1. Otwórz **Ustawienia → Urządzenia oraz usługi → EMS for Hoymiles HIT-(5–20)L-G3**.
2. Otwórz menu wpisu integracji i wybierz **Pobierz diagnostykę**.
3. Dołącz pobrany plik JSON do zgłoszenia razem z dokładną lokalną godziną
   wystąpienia błędu i krótkim opisem oczekiwanego działania.

Raport zawiera wersje integracji i firmware, kompletność encji, bieżące stany
Hoymiles, parametry obliczeń oraz żądane 24-godzinne okno istotnych zmian
sterowania. Historia szybkiej telemetrii jest celowo pomijana, aby raport
pozostał mały i nie obciążał bazy Recorder.

Dla wybranych encji EMS historia sterowania zapisuje również dozwolone zmiany
samych atrybutów: zwięzłe decyzje i świeżość RCE, taryfy oraz RCEm, kontekst osi
planu, czasy wykonań automatyzacji, właściciela aktywnego sterowania, stan
balansowania, odpowiedź instalacji oraz dowody transakcji Supervisora. Powtórki
różniące się wyłącznie znacznikami czasu Recordera są liczone i scalane przed
zastosowaniem limitów. Każda encja podaje zakres zapytania i zachowanych danych,
liczbę scalonych powtórek, pominięć i odrzuconych zdarzeń. Eksport zachowuje
najnowsze dowody i ma limity: 500 semantycznych zdarzeń na encję, 4000 zdarzeń,
8 MiB zakodowanych zdarzeń samej historii oraz 64 KiB na zdarzenie. Nie oznacza
to, że cały JSON albo ZIP ma 8 MiB. Wybrana encja bez rekordów Recordera jest
oznaczana jako brak znanych dowodów i powoduje niekompletny zakres; nie stanowi
dowodu, że sterowanie nie zadziałało.

ZIP z dashboardu ma też limity zbierania i przesyłania: eksportuje najwyżej 32
raporty config entry (i odczytuje najwyżej jeden dodatkowy element iteratora,
aby uczciwie oznaczyć obcięcie), pojedynczy raport może mieć do 12 MiB, plik
JSON diagnostyki do 20 MiB, a odfiltrowany log Core do 2 MiB i 2500 najnowszych
pasujących linii. Łączny rozmiar nieskompresowanych składników jest ograniczony
do 23 MiB, a końcowego skompresowanego ZIP-u do 24 MiB. Dane niemieszczące się
w limicie są atomowo zastępowane znacznikiem pominięcia, a metadane zgłaszają
niekompletność zamiast uznawać brak dowodów za stan prawidłowy. Są to limity
zasobów, nie dowód dostępności każdego oczekiwanego zdarzenia historycznego.

Rozszerzone profile historii używają obecnie zarządzanych, domyślnych ID encji.
Po ręcznej zmianie nazwy bieżący stan nadal może trafić do raportu, ale profil
rozszerzonej historii może nie zostać przypisany. Przy zgłoszeniu podaj, które
encje zostały przemianowane.
Przy wielu skonfigurowanych wpisach Hoymiles każdy raport natywny zawiera
obecnie ogólnoinstalacyjny snapshot zarządzanych stanów i wybraną historię;
sekcja katalogu danego wpisu pozostaje wiarygodnym kontekstem specyficznym dla
tego config entry.

Każda instalacja Home Assistanta otrzymuje jeden losowy UUID v4 o nazwie
`anonymous_installation_id`. Powstaje on bez użycia danych urządzenia, sieci,
konta ani config entry i jest przechowywany przez Home Assistanta w
`/config/.storage/hoymiles_hit_modbus.installation_identity`. Przetrwa restart
i aktualizację integracji, jest wspólny dla wszystkich wpisów Hoymiles w tym
samym HA i celowo pozostaje w raportach wsparcia wyłącznie do łączenia kolejnych
paczek z tej instalacji. Bieżąca wartość `installation_id_schema_version` to
`1`; identyfikator nie jest wystawiany jako encja.

Do porównywania wielu ZIP-ów z dashboardu służy prywatnościowy analizator
zbiorczy opisany w [DIAGNOSTICS_ANALYZER.md](DIAGNOSTICS_ANALYZER.md). Łączy on
kolejne paczki według anonimowego ID instalacji, ocenia RCE, RCEm i ładowanie
taryfowe za pomocą wersjonowanych reguł oraz tworzy raporty JSON, CSV, Markdown
i HTML bez rozpakowywania archiwów wejściowych.

### Gotowość planera

Uprawnienie do wykonania pochodzi z samej encji optymalizatora:
`result_current`, wieku raportu planu i atrybutów świeżości źródeł. Niedawno
odświeżona encja pośrednia nie nadaje staremu planowi prawa wykonania. W RCEm
pojemność baterii jest stabilną cechą, a wiek jej raportu ma znaczenie
diagnostyczne; bieżące napięcie i limity prądowe BMS nadal działają fail-closed.
Stan sensora właściciela sterowania pokazuje aktywną własność aktuatora,
natomiast `owner_code=manual` pozostaje stabilnym fallbackiem maszynowym dla
zgodności, gdy żaden automatyczny sterownik nie jest aktywny; atrybuty
`*_policy_enabled` opisują wyłącznie konfigurację. Opcjonalny Dzień 3 Solcast
publikuje wybraną encję, wiek ze znakiem, świeżość i przyczynę fallbacku.

Atrybuty planów RCE i taryfy publikują też `forecast_learning_enabled`,
`forecast_learning_mode`, `forecast_learning_excluded_reason` oraz rzeczywiście
użyty `forecast_factor_used`. Dla fizycznie potwierdzonego, dokładnego limitu
GCF 0% kontrakt ma wartości `false / fixed_zero_export / zero_export / 0.80`.
Brak albo nieświeżość GCF ma osobną konserwatywną przyczynę i nie może być
interpretowana jako potwierdzony zero-export.

### Master zmienia tryb, a Slave nie

Najpierw sprawdź fizyczną zewnętrzną magistralę Modbus/RS485. Konwerter ESP32,
Master i każdy Slave muszą korzystać z tej samej pary A/B i odniesienia;
adres `0` jest rozgłoszeniem na przewodzie, a Master nie przekazuje go przez
wewnętrzną sieć równoległą falowników. Sprawdź polaryzację, ciągłość oraz
terminację końców według instrukcji producenta. Świeża topologia, telemetria
sumaryczna i zgodny FC03 Mastera nie weryfikują odgałęzienia do Slave'a. Podczas
ostrożnego testu Grid Discharge i powrotu do Self-Use zapisz stan każdego
falownika osobno w aplikacji producenta.

## Rozszerzona paczka z terminala

W dodatku **Terminal & SSH** wykonaj:

```sh
sh /config/custom_components/hoymiles_hit_modbus/collect_diagnostics.sh
```

Komenda wyświetli ścieżkę jednego pliku `.tar.gz` w katalogu
`/config/hoymiles_diagnostics/`. Pobierz go przez File editor, Studio Code
Server, Sambę albo SSH i dołącz do zgłoszenia.

Paczka dodaje wersje Home Assistant/Supervisor/hosta, stan pamięci i dysku,
odfiltrowane logi Core i ESPHome, oczyszczoną konfigurację wejściową ESPHome
oraz — jeśli lokalne API na to pozwoli — natywny raport diagnostyczny.

Hasła, klucze API, tokeny, dane Wi-Fi, adresy URL, IP/MAC, numery seryjne i ID
urządzenia źródłowego są maskowane. Komenda nigdy nie kopiuje `secrets.yaml`
ani bazy `.storage` Home Assistanta. Automatyczne maskowanie nie zastępuje
kontroli — przejrzyj paczkę przed dodaniem jej do publicznego zgłoszenia.
# Accepted lease renewal evidence (local RC2 candidate)

`accepted_lease_journal` contains only validated ESP arm/renew acknowledgements
observed by the current HA control client. Sending a request does not add a
record. Each record correlates transaction, lease, command generation, sequence,
snapshot generation (renewal), first-send/response monotonic times, projected HA
UTC and the original hard deadline. The existing redactor pseudonymizes control
IDs consistently with other diagnostic records; session IDs and nonces are absent.

The journal is RAM-only, at most 8192 accepted events, exported on demand in
256-record pages. It adds no Recorder writes, polling or persistence. Invalidation
retains evidence; HA restart/reload creates a new journal epoch. `dropped_events`,
`retention_complete`, `gap_before` and `request_correlated` must be examined before
claiming coverage. Complete retention means all observed ACKs in that client,
not proof of a whole cycle. A whole-cycle claim also needs its initial arm,
sequence/timing coverage to its original deadline, FC03 and terminal evidence.
Missing records, process boundaries or archive omission markers mean PARTIAL.
UTC is derived from HA clock anchors, not a timestamp supplied by the ESP.
This cannot reconstruct renewal evidence for any previously completed cycle.
