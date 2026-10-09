# Batch diagnostics analyzer / Zbiorczy analizator diagnostyki

`tools/analyze_diagnostic_bundles.py` analyzes up to 100 dashboard diagnostic
ZIPs in one offline run. It is intended for comparing repeated captures from
the same installation and finding recurring RCE, RCEm and tariff-charging
problems without manually opening every archive.

The tool uses only the Python standard library, makes no network connections
and never extracts ZIP members to disk. Input archives remain unchanged.

## Run

From the repository root:

```powershell
python tools/analyze_diagnostic_bundles.py C:\Hoymiles\diagnostics --output C:\Hoymiles\analysis
```

You can pass several ZIP files or directories. Directories are scanned
recursively unless `--no-recursive` is used. Existing analyzer outputs are not
overwritten unless `--force` is supplied.

Useful options:

- `--max-archives 100` sets the hard input limit;
- `--fail-on error` returns exit code `10` when an error or critical finding is
  present, which is useful in automated triage;
- `--include-source-paths` adds an explicit `source_map.csv` containing local
  filenames and paths;
- `--include-anonymous-id` includes only the validated anonymous installation
  UUID. By default only a stable hashed alias such as `inst-a1b2c3...` is
  exported; unrelated UUID-like values remain redacted in both modes.

## Results

The output directory contains:

- `report.html` — self-contained interactive overview with filters;
- `report.md` — compact human-readable summary;
- `summary.json` — complete, versioned machine-readable result;
- `packages.csv` and `installations.csv` — package and installation coverage;
- `findings.csv` — grouped findings with stable rule IDs, severity, confidence,
  evidence and recommendation;
- `rce_observations.csv`, `rcem_observations.csv` and
  `tariff_observations.csv` — normalized planner snapshots;
- `control_events.csv` — deduplicated control-state events with the schema,
  profile, availability flag and bounded historical attributes as JSON;
- `control_runs.csv` — starts, stops, active time, longest and short runs,
  transition rate, flapping and open-cycle evidence per helper;
- `log_clusters.csv` — counts of normalized optimizer, Modbus, readback,
  rollback, Recorder and asset-error categories.

Prevalence is reported both per package and per installation, so an
installation with many captures does not dominate a cohort. Repeated ZIPs,
overlapping history and duplicated global snapshots from multiple config
entries are deduplicated deterministically.

## What the rules check

The versioned rule set covers, among other things:

- stale or non-current plans while an actuator is active;
- ownership conflicts, active control without execution readiness and mode
  inconsistencies;
- the RCE physical-limit invariant, including disagreement between requested,
  BMS, GCF/effective caps, `maximum_export_power_kw` and
  `physical_limit_source`;
- RCE operation during a sale block, reserve and data-quality problems;
- unhandled RCEm 253 V emergencies, shadow-mode writes, stale actuator data,
  headroom shortfall and recommendation/readback mismatch;
- tariff charging without a current slot, with stale inputs or outside an
  allowed low zone, reserve conflicts and delivery underperformance;
- recurring optimizer, communication, readback and rollback errors in logs.

Every conclusion carries a confidence level. Missing, unavailable, redacted
or truncated values remain unknown; they are never coerced to numeric zero or
`false`. A healthy verdict is not emitted when required evidence is absent.

## Evidence limits

Each ZIP contains a current snapshot of planner attributes. Analyzer 1.1
also preserves supported schema-1 historical planner/Supervisor/ownership
events, including decoded STOP frames. State-only duplicates do not replace
rich events; memory bounds also apply when enriching an existing event.
Unknown event schemas remain state-only. Historical attributes excluded by
Recorder and fast power telemetry remain unavailable, so the analyzer cannot
reconstruct every historical solver decision or every accepted lease renewal.
The original ZIP also contains the bounded `notification_history` ledger;
notification delivery is not an automatic verdict of the current rule set.

Each Recorder query is treated as an explicit coverage window. Overlapping
windows are merged, while gaps between captures remain unknown and are never
counted as one long control run. An include-start state older than the query is
clamped to the window boundary rather than projected through an unobserved gap.

In particular, one ZIP cannot prove inverter ramp or aggregate Master/Slave
physical response. Such a conclusion remains `not_evaluable` or suspected
until several appropriately timed captures provide enough evidence. Master
FC03 is not described as per-Slave acknowledgement.

Archives without `anonymous_installation_id` are accepted as legacy but remain
unlinked; the analyzer never derives an installation identity from a MAC,
serial number, IP address, hostname, config entry or device data.

## Safety and exit codes

The reader validates member names, sizes, compression ratios, encryption,
duplicate paths, CRC, JSON depth and supported schemas before analysis. One
corrupt archive is reported without discarding valid archives in the same run.

Exit codes are:

- `0` — analysis completed and all archives were accepted;
- `1` — reports were created, but at least one archive was rejected;
- `2` — invalid arguments or discovery/read error;
- `3` — no valid archive could be analyzed;
- `4` — output files could not be written;
- `10` — the selected `--fail-on` severity threshold was reached.

---

Analizator przetwarza do 100 ZIP-ów diagnostycznych w jednym przebiegu,
lokalnie i bez dostępu do sieci. Domyślnie ukrywa pełny anonimowy UUID, ścieżki
plików oraz surowe logi. Raportuje pokrycie dowodów i poziom pewności, dlatego
brak danych nie staje się fałszywym zerem ani fałszywym potwierdzeniem
poprawności. Najważniejszym raportem do szybkiego przeglądu jest `report.html`,
a `summary.json` i pliki CSV nadają się do dalszej analizy automatycznej.
