"""Build the HACS entity catalog and bundled Home Assistant assets.

The ESPHome YAML files remain the source of truth for entity names.  This script
extracts public entities, creates stable translation keys and writes the English
and Polish translation files used by the HACS integration.
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
TRANSLATIONS = COMPONENT / "translations"
RESOURCES = COMPONENT / "resources"

SUPPORTED_SOURCE_DOMAINS = {
    "button",
    "sensor",
    "text_sensor",
    "number",
    "select",
}
SKIPPED_FILES = {
    "optional_battery_legacy.yaml",
    "optional_battery_pack_diagnostics.yaml",
}

SPECIAL_NAMES = {
    "Tryb EMS": ("ems_mode", "EMS mode", "Tryb EMS"),
}

PHRASE_TRANSLATIONS = {
    "Overview Internal PV Total Power": "Podgląd łącznej mocy wewnętrznych wejść PV",
    "Overview External PV Total Power": "Podgląd łącznej mocy zewnętrznych źródeł PV",
    "Overview Grid Total Active Power": "Podgląd łącznej mocy czynnej sieci",
    "Overview Generator Active Power": "Podgląd mocy czynnej generatora",
    "Overview Load Active Power": "Podgląd mocy czynnej odbiorników",
    "Overview Smart Load Active Power": "Podgląd mocy czynnej Smart Load",
    "Overview Battery Power": "Podgląd mocy baterii",
    "Overview Battery SOC": "Podgląd stanu naładowania baterii",
    "Overview Inverter Active Power": "Podgląd mocy czynnej falownika",
    "Overview PV Total Power": "Podgląd łącznej mocy PV",
    "Generation Control Function": "Funkcja ograniczania eksportu (GCF)",
    "Maximum Export Power Limit": "Maksymalny limit eksportu",
    "GEN Port Mode": "Tryb złącza GEN",
    "SOC Start Charge From Grid": "SOC rozpoczęcia ładowania z sieci",
    "Low SOC Grid Charge Power": "Moc ładowania z sieci przy niskim SOC",
    "Battery Max Charge Power": "Maksymalna moc ładowania baterii",
    "Battery Max Discharge Power": "Maksymalna moc rozładowania baterii",
    "Battery Current (BMS)": "Prąd baterii (BMS)",
    "Battery Power (BMS)": "Moc baterii (BMS)",
    "Battery Voltage (BMS)": "Napięcie baterii (BMS)",
    "Battery Current (Inverter)": "Prąd baterii (falownik)",
    "Battery 1 Voltage": "Napięcie baterii (falownik)",
    "Maximum Charge Power": "Maksymalna moc ładowania z sieci",
    "Maximum Discharge Power": "Maksymalna moc rozładowania do sieci",
    "Force Charge SOC": "Docelowy SOC ładowania z sieci",
    "Force Discharge SOC": "Minimalny SOC rozładowania do sieci",
    "Self-Use SOC": "Rezerwa SOC dla autokonsumpcji",
    "Battery Type Setting": "Typ baterii",
    "BMS Type Setting": "Typ BMS",
    "Parallel Networking Command": "Polecenie sieci równoległej",
    "Parallel Topology": "Topologia sieci równoległej",
    "Parallel EMS Control Status": "Stan sterowania EMS sieci równoległej",
    "Parallel Topology Readback Generation": "Generacja odczytu topologii równoległej",
    "Parallel Aggregate Power Readback Generation": (
        "Generacja odczytu sumarycznej mocy układu równoległego"
    ),
    "EMS Verified Hardware Readback Supported": "Obsługa potwierdzonego odczytu sprzętowego EMS",
    "Direct Register Verified Readback Supported": (
        "Obsługa potwierdzonego odczytu rejestrów bezpośrednich"
    ),
    "EMS Control Readback Generation": "Generacja odczytu sterowania EMS",
    "GCF Control Readback Generation": "Generacja odczytu sterowania GCF",
    "Battery Charge Power Readback Generation": "Generacja odczytu mocy ładowania baterii",
    "EMS Mode Readback Code": "Kod odczytanego trybu EMS",
    "EMS Self-Use SOC Readback": "Odczyt rezerwy SOC autokonsumpcji EMS",
    "EMS Backup SOC Readback": "Odczyt rezerwy SOC Backup EMS",
    "EMS Force Charge SOC Readback": "Odczyt docelowego SOC ładowania EMS",
    "EMS Maximum Charge Power Readback": "Odczyt maksymalnej mocy ładowania EMS",
    "EMS Complete Block Charge Rollback Command": (
        "Polecenie pełnego rollbacku bloku ładowania EMS"
    ),
    "EMS Force Discharge SOC Readback": "Odczyt minimalnego SOC rozładowania EMS",
    "EMS Maximum Discharge Power Readback": "Odczyt maksymalnej mocy rozładowania EMS",
    "GCF Enable Readback Code": "Kod odczytanego stanu GCF",
    "GCF Maximum Export Power Readback": "Odczyt maksymalnego limitu eksportu GCF",
    "Battery Max Charge Power Readback": "Odczyt maksymalnej mocy ładowania baterii",
    "Machines Type": "Rola urządzenia w sieci równoległej",
    "Number of Machines (Master and Slave)": "Liczba falowników (Master i Slave)",
    "Communication Address 1 (Master Device)": "Adres komunikacyjny 1 (Master)",
    "Communication Address 2 (Slave Device 1)": "Adres komunikacyjny 2 (Slave 1)",
    "Communication Address 3 (Slave Device 2)": "Adres komunikacyjny 3 (Slave 2)",
    "Communication Address 4 (Slave Device 3)": "Adres komunikacyjny 4 (Slave 3)",
    "Communication Address 5 (Slave Device 4)": "Adres komunikacyjny 5 (Slave 4)",
    "Communication Address 6 (Slave Device 5)": "Adres komunikacyjny 6 (Slave 5)",
    "Communication Address 7 (Slave Device 6)": "Adres komunikacyjny 7 (Slave 6)",
    "Communication Address 8 (Slave Device 7)": "Adres komunikacyjny 8 (Slave 7)",
    "Communication Address 9 (Slave Device 8)": "Adres komunikacyjny 9 (Slave 8)",
    "Communication Address 10 (Slave Device 9)": "Adres komunikacyjny 10 (Slave 9)",
    "System Operation": "Praca systemu",
    "Inverter Work Status": "Stan pracy falownika",
    "Overview System Work Status": "Podgląd stanu pracy systemu",
    "Overview Battery Work Status": "Podgląd stanu pracy baterii",
    "Battery Work Status (BMS)": "Stan pracy baterii (BMS)",
    "Battery Link Status": "Stan łącza baterii",
    "PV Link Status": "Stan łącza PV",
    "Meter Link Status": "Stan łącza licznika",
    "Battery Type (BMS)": "Typ baterii (BMS)",
    "Battery Fault Code (BMS)": "Kod błędu baterii (BMS)",
    "Overview Battery Faults": "Podgląd błędów baterii",
    "Overview DSP Power Faults": "Podgląd błędów DSP mocy",
    "DSP Safety Faults": "Błędy DSP zabezpieczeń",
    "ARM Communication Faults": "Błędy komunikacji ARM",
    "ARM Peripheral Faults": "Błędy urządzeń peryferyjnych ARM",
    "ARM System Faults": "Błędy systemowe ARM",
    "SW Fault": "Błąd oprogramowania",
    "HW Fault": "Błąd sprzętowy",
    "Clear Fault": "Wyczyść alarmy falownika",
}

WORD_TRANSLATIONS = {
    "Active": "czynna",
    "Address": "adres",
    "Apparent": "pozorna",
    "Battery": "bateria",
    "BMS": "BMS",
    "Bus": "magistrala",
    "Buy": "pobór",
    "Capacity": "pojemność",
    "Charge": "ładowanie",
    "Charging": "ładowanie",
    "Communication": "komunikacja",
    "Current": "prąd",
    "Daily": "dzienna",
    "Day": "dzień",
    "Discharge": "rozładowanie",
    "Energy": "energia",
    "External": "zewnętrzne",
    "Fault": "błąd",
    "Faults": "błędy",
    "Frequency": "częstotliwość",
    "Generator": "generator",
    "Grid": "sieć",
    "Input": "wejście",
    "Internal": "wewnętrzne",
    "Inverter": "falownik",
    "Link": "łącze",
    "Load": "odbiorniki",
    "Maximum": "maksymalna",
    "Minimum": "minimalna",
    "Mode": "tryb",
    "Operation": "praca",
    "Output": "wyjście",
    "Overview": "podgląd",
    "Power": "moc",
    "Reactive": "bierna",
    "Sell": "oddawanie",
    "Setting": "ustawienie",
    "Smart": "Smart",
    "Status": "stan",
    "Temperature": "temperatura",
    "Today": "dzisiaj",
    "Total": "łącznie",
    "Type": "typ",
    "Voltage": "napięcie",
    "Work": "pracy",
}

OPTION_TRANSLATIONS = {
    "Autokonsumpcja (Self-Use)": ("self_use", "Self-Use", "Autokonsumpcja"),
    "Self-Use": ("self_use", "Self-Use", "Autokonsumpcja"),
    "Off-Grid": ("off_grid", "Off-Grid", "Praca wyspowa"),
    "Ładowanie z sieci": ("grid_charge", "Grid charge", "Ładowanie z sieci"),
    "Grid Charge": ("grid_charge", "Grid charge", "Ładowanie z sieci"),
    "Rozładowanie do sieci": (
        "grid_discharge",
        "Grid discharge",
        "Rozładowanie do sieci",
    ),
    "Grid Discharge": (
        "grid_discharge",
        "Grid discharge",
        "Rozładowanie do sieci",
    ),
    "Disabled": ("disabled", "Disabled", "Wyłączone"),
    "Enabled": ("enabled", "Enabled", "Włączone"),
    "PV": ("pv", "Inverter / PV", "Falownik / PV"),
    "Generator": ("generator", "Generator", "Generator"),
    "Smart Load (G3)": ("smart_load_g3", "Smart Load (G3)", "Smart Load (G3)"),
    "Inactive": ("inactive", "Inactive", "Nieaktywne"),
    "Start": ("start", "Start", "Uruchom"),
    "Stop": ("stop", "Stop", "Zatrzymaj"),
    "Create Network": ("create_network", "Create network", "Utwórz sieć"),
    "Disassemble Network": (
        "disassemble_network",
        "Disassemble network",
        "Rozłącz sieć",
    ),
    "No Battery": ("no_battery", "No battery", "Brak baterii"),
    "Lithium": ("lithium", "Lithium-ion", "Litowo-jonowa"),
    "Lead-Acid": ("lead_acid", "Lead-acid", "Kwasowo-ołowiowa"),
    "Not Configured": ("not_configured", "Not configured", "Nieskonfigurowane"),
}

TEXT_STATE_TRANSLATIONS = {
    "unavailable": ("Unavailable", "Niedostępne"),
    "power_initialization": ("Power initialization", "Inicjalizacja zasilania"),
    "standby": ("Standby", "Czuwanie"),
    "grid_test": ("Grid test", "Test sieci"),
    "on_grid": ("On-grid operation", "Praca z siecią"),
    "fault": ("Fault", "Awaria"),
    "off_grid": ("Off-grid operation", "Praca wyspowa"),
    "bypass": ("Bypass", "Bypass"),
    "no_error": ("No error", "Brak błędu"),
    "no_errors": ("No faults", "Brak błędów"),
    "offline": ("Offline", "Offline"),
    "online": ("Online", "Online"),
    "hmid_connected": ("HMID: connected", "HMID: połączenie prawidłowe"),
    "hmid_connection_error": (
        "HMID: connection error",
        "HMID: błąd połączenia",
    ),
    "no_battery": ("No battery", "Brak baterii"),
    "lithium": ("Lithium-ion", "Litowa"),
    "lead_acid": ("Lead-acid", "Kwasowo-ołowiowa"),
    "simulated": ("Simulated", "Symulowana"),
    "charging": ("Charging", "Ładowanie"),
    "discharging": ("Discharging", "Rozładowanie"),
    "single_inverter": ("Single inverter", "Falownik pojedynczy"),
    "parallel_master": ("Master", "Master"),
    "parallel_slave": ("Slave", "Slave"),
    "unknown_topology": ("Unknown topology", "Nieznana topologia"),
    "invalid_topology": ("Invalid topology", "Nieprawidłowa topologia"),
    "parallel_ems_ready_direct": (
        "Ready - direct control",
        "Gotowe - sterowanie bezpośrednie",
    ),
    "parallel_ems_ready_master": (
        "Ready - Master controls the parallel network",
        "Gotowe - Master steruje siecią równoległą",
    ),
    "parallel_ems_blocked_slave": (
        "Blocked - ESP32 connected to Slave",
        "Zablokowane - ESP32 podłączone do Slave",
    ),
    "parallel_ems_waiting": (
        "Waiting for network detection",
        "Oczekiwanie na wykrycie sieci",
    ),
    "parallel_ems_waiting_master_mode": (
        "Waiting for Master EMS mode",
        "Oczekiwanie na tryb EMS Mastera",
    ),
    "parallel_ems_waiting_slave_confirmation": (
        "Waiting for Slave confirmation",
        "Oczekiwanie na potwierdzenie Slave",
    ),
    "parallel_address_error": (
        "Parallel-network address error",
        "Błąd adresów sieci równoległej",
    ),
    "parallel_slave_offline": (
        "Error - Slave is not responding",
        "Błąd - Slave nie odpowiada",
    ),
    "parallel_slave_ems_mismatch": (
        "Error - Slave EMS mode mismatch",
        "Błąd - tryb EMS Slave niezgodny",
    ),
    "parallel_invalid_count": (
        "Invalid inverter count",
        "Nieprawidłowa liczba falowników",
    ),
}

ENGLISH_REPLACEMENTS = {
    (
        "  - title: Nadzorca EMS\n"
        "    path: ems-supervisor\n"
        "    icon: mdi:eye-circle-outline\n"
        "    type: panel"
    ): (
        "  - title: EMS Supervisor\n"
        "    path: ems-supervisor\n"
        "    icon: mdi:eye-circle-outline\n"
        "    type: panel"
    ),
    (
        "  # Pozwala Nadzorcy EMS uwzględniać RCE wyłącznie w arbitrażu Shadow.\n"
        "  # Nie włącza automatyki RCE, nie zmienia jej przełącznika i nie udziela\n"
        "  # prawa do fizycznego zapisu."
    ): (
        "  # Allows EMS Supervisor to consider RCE only in Shadow arbitration. It does\n"
        "  # not enable RCE automation, change its enable helper, or grant physical\n"
        "  # write authority."
    ),
    (
        "  # Pozwala Nadzorcy EMS uwzględniać tanie ładowanie wyłącznie w arbitrażu\n"
        "  # Shadow. Nie włącza ładowania taryfowego, nie zmienia jego przełącznika\n"
        "  # i nie udziela prawa do fizycznego zapisu."
    ): (
        "  # Allows EMS Supervisor to consider tariff charging only in Shadow\n"
        "  # arbitration. It does not enable tariff charging, change its enable\n"
        "  # helper, or grant physical write authority."
    ),
    (
        "  # Pozwala Nadzorcy EMS uwzględniać RCEm wyłącznie w arbitrażu Shadow.\n"
        "  # Nie włącza RCEm, nie zmienia jego przełączników i nie udziela prawa do\n"
        "  # fizycznego zapisu."
    ): (
        "  # Allows EMS Supervisor to consider RCEm only in Shadow arbitration. It\n"
        "  # does not enable RCEm, change its helpers, or grant physical write\n"
        "  # authority."
    ),
    (
        "  # Tryb obserwacyjny Nadzorcy EMS. Off wyłącza arbitraż, a Shadow publikuje\n"
        "  # wyłącznie decyzję obserwacyjną. Żaden wybór nie uruchamia fizycznego\n"
        "  # wykonania ani nie zmienia automatyki legacy."
    ): (
        "  # EMS Supervisor observation mode. Off disables arbitration, while Shadow\n"
        "  # publishes an observation-only decision. Neither option starts physical\n"
        "  # execution or changes legacy automation."
    ),
    (
        "  # Profil jest tylko publikowany w arbitrażu Shadow. Efekty profilu pozostają\n"
        "  # niezaaplikowane i nie zmieniają fizycznego wykonania ani automatyki legacy."
    ): (
        "  # The profile is only published in Shadow arbitration. Profile effects\n"
        "  # remain unapplied and do not change physical execution or legacy\n"
        "  # automation."
    ),
    '"Nadzorca EMS — uwzględniaj tanie ładowanie"': (
        '"EMS Supervisor — consider tariff charging"'
    ),
    '"Nadzorca EMS — uwzględniaj RCE"': '"EMS Supervisor — consider RCE"',
    '"Nadzorca EMS — uwzględniaj RCEm"': '"EMS Supervisor — consider RCEm"',
    '"Nadzorca EMS — profil"': '"EMS Supervisor — profile"',
    '"Nadzorca EMS — tryb"': '"EMS Supervisor — mode"',
    "EMS — potwierdź zbiorczą reakcję mocy po Grid Discharge": (
        "EMS — verify aggregate power response after Grid Discharge"
    ),
    "Po sprzętowym ACK trybu z Mastera szuka trzech kolejnych stabilnych": (
        "After the physical Master mode ACK, looks for three consecutive stable"
    ),
    "bilansów w pięciu nowszych kompletnych generacjach. Przejściowy pik tylko": (
        "balances within five newer complete generations. A transition peak only"
    ),
    "przesuwa okno. Wynik jest dowodem reakcji agregatowej, nie odczytem": (
        "shifts the window. The result is aggregate-response evidence, not a readback"
    ),
    "potwierdzającym stan któregokolwiek pojedynczego falownika.": (
        "confirming the state of any individual inverter."
    ),
    "Topologia przed poleceniem nie była jednoznaczna": (
        "Pre-command topology was not unambiguous"
    ),
    "Agregatowy dowód nie jest wymagany dla tej topologii": (
        "Aggregate evidence is not required for this topology"
    ),
    "Brak kompletnej generacji agregatowego bilansu mocy": (
        "No complete aggregate power-balance generation is available"
    ),
    "Generacja agregatowego odczytu zresetowała się po poleceniu": (
        "Aggregate readback generation reset after the command"
    ),
    "ESP uruchomił się ponownie podczas weryfikacji reakcji": (
        "ESP restarted during response verification"
    ),
    "Brak świeżej, zgodnej kierunkowo reakcji mocy układu równoległego": (
        "No fresh, directionally consistent parallel-system power response"
    ),
    "RCE nie otrzymało zgodnej agregatowej reakcji mocy": (
        "RCE did not receive a matching aggregate power response"
    ),
    "'letni'": "'summer'",
    "'zimowy'": "'winter'",
    "'tak'": "'yes'",
    "'nie'": "'no'",
    "⚠️ Profil": "⚠️ Profile",
    "**Tryb ręczny · {{ group }}**": "**Manual mode · {{ group }}**",
    "Godziny i łączne ceny krańcowe są pobierane z pól ręcznych poniżej.": (
        "Time periods and total marginal prices are taken from the manual fields below."
    ),
    "Założony sprzedawca:": "Assumed supplier:",
    "Cennik:": "Price list:",
    " · sezon:": " · season:",
    "Bieżąca strefa:": "Current period:",
    "Tanie godziny:": "Low-cost hours:",
    "Średnia strefa:": "Medium period:",
    "Weekend w taniej strefie:": "Weekend in low-cost period:",
    "święta:": "public holidays:",
    "| Strefa | Cena poglądowa |": "| Period | Indicative price |",
    "| G11 (porównanie) |": "| G11 (comparison) |",
    "| Tania |": "| Low-cost |",
    "| Średnia |": "| Medium |",
    "| Droga |": "| Peak |",
    "Oficjalne taryfy operatora": "Official DSO tariffs",
    "nie jest obsługiwany.": "is not supported.",
    "Automatyczne ładowanie pozostanie zablokowane. Wybierz inną grupę": (
        "Automatic charging will remain blocked. Select another group"
    ),
    "albo profil **Manual**.": "or the **Manual** profile.",
    "Ceny obejmują energię regionalnego sprzedawcy z urzędu i zmienne": (
        "Prices include energy from the regional incumbent supplier and variable"
    ),
    "składniki dystrybucyjne brutto. Nie obejmują opłat stałych. Przy innym": (
        "gross distribution charges. Fixed fees are excluded. With another"
    ),
    "sprzedawcy wybierz **Manual** i przepisz ceny z własnej faktury.": (
        "supplier, select **Manual** and enter prices from your own bill."
    ),
    "EMS taryfowy — operator / profil cenowy": (
        "Tariff EMS — DSO / price profile"
    ),
    "Energia taniego ładowania — ostatnie 30 dni": (
        "Low-cost charging energy — last 30 days"
    ),
    "Oszczędność taryfowa — ostatnie 30 dni": (
        "Tariff savings — last 30 days"
    ),
    "Automatyczne ładowanie taryfowe — ustawienia": (
        "Automatic tariff charging — settings"
    ),
    "EMS — blokada równoczesnej automatyki RCE i taryfowej": (
        "EMS — RCE and tariff automation interlock"
    ),
    "EMS taryfowy — ładowanie według prognozy i tanich stref": (
        "Tariff EMS — forecast-based charging in low-cost periods"
    ),
    "Pozwala działać tylko jednemu automatowi. Włączenie RCE wyłącza ładowanie": (
        "Allows only one automation to operate. Enabling RCE disables tariff"
    ),
    "taryfowe, a włączenie ładowania taryfowego wyłącza RCE. Po restarcie HA": (
        "charging, while enabling tariff charging disables RCE. After an HA restart"
    ),
    "pierwszeństwo ma RCE, jeśli oba przełączniki zostały odtworzone jako włączone.": (
        "RCE takes priority if both switches are restored as enabled."
    ),
    "Co minutę symuluje bilans domu, PV i baterii do końca jutra. Ładuje tylko": (
        "Every minute it simulates the home, PV, and battery balance through tomorrow."
    ),
    "w zaplanowanym tanim bloku, który występuje przed prognozowanym deficytem.": (
        "It charges only in a planned low-cost slot before the forecast shortage."
    ),
    "Nie działa równocześnie z RCE ani z ręcznymi timerami EMS.": (
        "It cannot run together with RCE or manual EMS timers."
    ),
    "EMS taryfowy — automatyczne ładowanie włączone": (
        "Tariff EMS — automatic charging enabled"
    ),
    "EMS taryfowy — pokaż dane zaawansowane": (
        "Tariff EMS — show advanced data"
    ),
    "EMS taryfowy — trwa automatyczne ładowanie": (
        "Tariff EMS — automatic charging in progress"
    ),
    "Taryfa — tania strefa przez cały weekend": (
        "Tariff — low-cost period for the entire weekend"
    ),
    "Taryfa — polskie dni ustawowo wolne w taniej strefie": (
        "Tariff — Polish public holidays in the low-cost period"
    ),
    "Taryfa — początek taniego okna 1": "Tariff — low-cost window 1 start",
    "Taryfa — koniec taniego okna 1": "Tariff — low-cost window 1 end",
    "Taryfa — początek taniego okna 2": "Tariff — low-cost window 2 start",
    "Taryfa — koniec taniego okna 2": "Tariff — low-cost window 2 end",
    "Taryfa G13 — początek strefy średniej": "G13 tariff — medium period start",
    "Taryfa G13 — koniec strefy średniej": "G13 tariff — medium period end",
    "EMS taryfowy — grupa taryfowa": "Tariff EMS — tariff group",
    "Taryfa — cena referencyjna G11": "Tariff — G11 reference price",
    "Taryfa — łączna cena w strefie taniej": (
        "Tariff — total price in the low-cost period"
    ),
    "Taryfa G13 — łączna cena w strefie średniej": (
        "G13 tariff — total price in the medium period"
    ),
    "Taryfa — łączna cena w strefie drogiej": (
        "Tariff — total price in the peak period"
    ),
    "EMS taryfowy — limit mocy Grid Charge (dom i bateria)": (
        "Tariff EMS — Grid Charge power limit (home and battery)"
    ),
    "EMS taryfowy — wewnętrzne działanie aktywnego bloku": (
        "Tariff EMS — internal active-slot action"
    ),
    "EMS taryfowy — koniec zapamiętanego bloku": (
        "Tariff EMS — latched active-slot end"
    ),
    "EMS taryfowy — zapamiętany cel aktywnego ładowania": (
        "Tariff EMS — latched active charging target"
    ),
    "EMS taryfowy — sprawność ładowania": "Tariff EMS — charge efficiency",
    "EMS taryfowy — sprawność późniejszego rozładowania": (
        "Tariff EMS — subsequent discharge efficiency"
    ),
    "EMS taryfowy — minimalna różnica ceny względem G11": (
        "Tariff EMS — minimum price difference versus G11"
    ),
    "EMS taryfowy — korekta bezpieczeństwa SOC": (
        "Tariff EMS — SOC safety correction"
    ),
    "EMS taryfowy — maksymalny SOC po ładowaniu": (
        "Tariff EMS — maximum SOC after charging"
    ),
    "Wyrównywanie magazynu — automatyka włączona": (
        "Battery balancing — automation enabled"
    ),
    "Wyrównywanie magazynu — trwa cykl serwisowy": (
        "Battery balancing — service cycle in progress"
    ),
    "Wyrównywanie magazynu — ostatni zakończony cykl": (
        "Battery balancing — last completed cycle"
    ),
    "Wyrównywanie magazynu — etap wewnętrzny": (
        "Battery balancing — internal stage"
    ),
    "Wyrównywanie magazynu — odstęp między cyklami": (
        "Battery balancing — interval between cycles"
    ),
    "Wyrównywanie magazynu — czas utrzymania 100% SOC": (
        "Battery balancing — hold time at 100% SOC"
    ),
    "Wyrównywanie magazynu — zapamiętany limit mocy ładowania": (
        "Battery balancing — saved charge-power limit"
    ),
    "Wyrównywanie magazynu — zapamiętany docelowy SOC": (
        "Battery balancing — saved target SOC"
    ),
    "Wyrównywanie magazynu — pozostały czas przy 100% SOC": (
        "Battery balancing — remaining time at 100% SOC"
    ),
    "Wyrównywanie magazynu — limit czasu cyklu": (
        "Battery balancing — cycle time limit"
    ),
    "Wyrównywanie magazynu — rozpocznij cykl": (
        "Battery balancing — start cycle"
    ),
    "Wyrównywanie magazynu — zakończ cykl": (
        "Battery balancing — finish cycle"
    ),
    "Wyrównywanie magazynu — sterowanie cyklem": (
        "Battery balancing — cycle control"
    ),
    "Wyrównywanie magazynu — ustawienia i stan": (
        "Battery balancing — settings and status"
    ),
    "Wyrównywanie magazynu": "Battery balancing",
    "Ładowanie z PV — oczekiwanie na zachód słońca": (
        "Charging from PV — waiting for sunset"
    ),
    "Dobijanie z sieci do 99% SOC": "Grid top-up to 99% SOC",
    "Wolne ładowanie 2 kW od 99% do 100% SOC": (
        "Slow 2 kW charging from 99% to 100% SOC"
    ),
    "Wyrównywanie ogniw przy 100% SOC": "Cell balancing at 100% SOC",
    "Aktywny cykl serwisowy": "Service cycle active",
    "Oczekiwanie na najbliższy okres produkcji PV": (
        "Waiting for the next PV production period"
    ),
    "Zaplanowane": "Scheduled",
    "Procent wspólnego limitu Grid Charge zapewniający około 2 kW dla": (
        "Shared Grid Charge limit percentage providing about 2 kW for the"
    ),
    "baterii po odjęciu bieżącego obciążenia domu.": (
        "battery after subtracting the current home load."
    ),
    "Zawiesza wszystkie pozostałe plany EMS, pozostawia falownik w Self-Use": (
        "Suspends all other EMS plans and keeps the inverter in Self-Use"
    ),
    "podczas produkcji PV i przygotowuje bezpieczny cykl ładowania do 100%.": (
        "during PV production while preparing a safe charge cycle to 100%."
    ),
    "Wraca do Self-Use, odtwarza ustawienia ładowania i zwalnia blokadę": (
        "Returns to Self-Use, restores charge settings, and releases the lock"
    ),
    "pozostałych planów EMS. Zakończony poprawnie cykl zapisuje swoją datę.": (
        "on other EMS plans. A successfully completed cycle stores its date."
    ),
    "Czy cykl osiągnął 100% SOC i zakończył czas wyrównywania": (
        "Whether the cycle reached 100% SOC and completed its balancing time"
    ),
    "Uruchamia cykl co zadaną liczbę dni. Najpierw wykorzystuje PV w Self-Use,": (
        "Runs the cycle at the configured day interval. It first uses PV in Self-Use,"
    ),
    "po zachodzie doładowuje magazyn z sieci, od 99% ogranicza ładowanie": (
        "tops up the battery from the grid after sunset, and from 99% limits"
    ),
    "baterii do około 2 kW i odmierza czas wyrównywania dopiero przy 100% SOC.": (
        "battery charging to about 2 kW; balancing time starts only at 100% SOC."
    ),
    "Okresowy cykl serwisowy dla magazynów LiFePO4. W dniu wykonania": (
        "A periodic service cycle for LiFePO4 batteries. On the scheduled day it"
    ),
    "najpierw wykorzystuje produkcję PV, po zachodzie doładowuje magazyn": (
        "first uses PV production and tops up the battery from the grid after"
    ),
    "z sieci do 100%, a od 99% ogranicza rzeczywistą moc ładowania baterii": (
        "sunset to 100%. From 99% it limits actual battery charging power"
    ),
    "do około 2 kW. Zadany czas wyrównywania jest liczony dopiero od": (
        "to about 2 kW. The configured balancing duration starts only after"
    ),
    "osiągnięcia 100% SOC.": "reaching 100% SOC.",
    "Podczas cyklu RCE, ładowanie taryfowe i ręczne harmonogramy EMS są": (
        "During the cycle, RCE, tariff charging, and manual EMS schedules are"
    ),
    "zawieszone. Ich ustawienia pozostają zapamiętane i wracają do pracy": (
        "suspended. Their settings are preserved and resume operation"
    ),
    "po zakończeniu. Wyłączenie przełącznika przerywa cykl i przywraca": (
        "after completion. Turning the switch off aborts the cycle and restores"
    ),
    "Self-Use. Funkcji używaj zgodnie z zaleceniami producenta baterii.": (
        "Self-Use. Follow the battery manufacturer's recommendations."
    ),
    "Włącz automatyczne wyrównywanie": "Enable automatic balancing",
    "Wykonuj co określoną liczbę dni": "Run every specified number of days",
    "Czas wyrównywania przy 100% SOC": "Balancing time at 100% SOC",
    "Następny planowany cykl": "Next scheduled cycle",
    "Automatyka wyłączona — pierwszy cykl oczekuje": (
        "Automation disabled — first cycle is waiting"
    ),
    "Automatyka wyłączona": "Automation disabled",
    "Cykl w toku": "Cycle in progress",
    "Po wschodzie słońca": "After sunrise",
    "Gotowy do rozpoczęcia": "Ready to start",
    "po wschodzie słońca": "after sunrise",
    "Pozostały czas przy 100% SOC": "Remaining time at 100% SOC",
    "Bieżący limit dla wolnego ładowania": "Current slow-charge limit",
    "Tanie ładowanie": "Low-cost charging",
    "Włącz automatyczne doładowanie z sieci": "Enable automatic grid top-up",
    "Operator i profil cenowy": "DSO and price profile",
    "Grupa taryfowa": "Tariff group",
    "Żądana moc ładowania z sieci": "Requested grid charge power",
    "Limit mocy Grid Charge (dom i bateria)": (
        "Grid Charge power limit (home and battery)"
    ),
    "Maksymalny SOC po doładowaniu": "Maximum SOC after charging",
    "Dodatkowy zapas SOC": "Additional SOC reserve",
    "Strefy i ceny energii": "Energy periods and prices",
    "Wybrany profil — podgląd": "Selected profile — overview",
    "Tryb ręczny — strefy i ceny energii": (
        "Manual mode — periods and energy prices"
    ),
    "Cena referencyjna G11": "G11 reference price",
    "Łączna cena w strefie taniej": "Total low-period price",
    "Łączna cena w strefie średniej G13": "Total G13 medium-period price",
    "Łączna cena w strefie drogiej": "Total peak-period price",
    "Początek taniego okna 1": "Low-cost window 1 start",
    "Koniec taniego okna 1": "Low-cost window 1 end",
    "Początek taniego okna 2": "Low-cost window 2 start",
    "Koniec taniego okna 2": "Low-cost window 2 end",
    "Początek strefy średniej G13": "G13 medium period start",
    "Koniec strefy średniej G13": "G13 medium period end",
    "Cały weekend w taniej strefie": "Entire weekend in low-cost period",
    "Dni ustawowo wolne w taniej strefie": (
        "Public holidays in low-cost period"
    ),
    "Plan i bezpieczeństwo magazynu": "Battery plan and safety",
    "Stan optymalizatora": "Optimizer status",
    "Bieżąca strefa": "Current period",
    "Bieżąca cena zakupu": "Current import price",
    "Bieżący blok wybrany do ładowania": "Current slot selected for charging",
    "Wyliczony docelowy SOC": "Calculated target SOC",
    "Zaplanowany pobór z sieci": "Planned grid import",
    "Prognozowana oszczędność względem G11": "Forecast savings versus G11",
    "Średnie dobowe zużycie odbiorników": "Average daily load consumption",
    "Średnie zużycie nocne": "Average night consumption",
    "Pozostała prognoza PV dzisiaj": "Remaining PV forecast today",
    "Prognoza PV jutro": "PV forecast tomorrow",
    "Pojemność magazynu użyta w obliczeniach": (
        "Battery capacity used in calculations"
    ),
    "Aktualny SOC magazynu": "Current battery SOC",
    "Plan taniego doładowania": "Low-cost top-up plan",
    "Wynik rzeczywisty": "Actual result",
    "Energia doładowania dzisiaj": "Top-up energy today",
    "Oszczędność dzisiaj względem G11": "Savings today versus G11",
    "Energia doładowania w tym miesiącu": "Top-up energy this month",
    "Oszczędność w tym miesiącu względem G11": (
        "Savings this month versus G11"
    ),
    "Energia doładowania w tym roku": "Top-up energy this year",
    "Oszczędność w tym roku względem G11": "Savings this year versus G11",
    "Energia z sieci [kWh]": "Grid energy [kWh]",
    "Oszczędność względem G11 [PLN]": "Savings versus G11 [PLN]",
    "Ustawienia zaawansowane": "Advanced settings",
    "Minimalna różnica ceny względem G11": "Minimum price difference versus G11",
    "Sprawność ładowania": "Charge efficiency",
    "Sprawność późniejszego rozładowania": "Subsequent discharge efficiency",
    "Cel SOC zapisany w falowniku": "SOC target stored in inverter",
    "Limit mocy zapisany w falowniku": "Power limit stored in inverter",
    "Jak działa automatyka taryfowa": "How tariff automation works",
    "Wyłączona": "Disabled",
    "Zablokowana — aktywna automatyka RCE": "Blocked — RCE automation is active",
    "Ładowanie z sieci —": "Grid charging —",
    "Tania": "Low-cost",
    "Średnia": "Medium",
    "Droga": "Peak",
    "Brak danych": "No data",
    "**Tryb planu:**": "**Plan mode:**",
    "'aktywny'": "'active'",
    "'podgląd — automatyczne ładowanie jest wyłączone'": (
        "'preview — automatic charging is disabled'"
    ),
    "**Wynik:**": "**Result:**",
    "**Deficyt bez doładowania:**": "**Shortage without top-up:**",
    "**Pozostały deficyt po planie:**": "**Remaining shortage after plan:**",
    "**Pobór z sieci łącznie:**": "**Total grid import:**",
    "**Bezpośrednio sieć → dom:**": "**Direct grid → home:**",
    "**Zapisane w magazynie po stratach:**": (
        "**Stored in the battery after losses:**"
    ),
    "**Koszt planu:**": "**Plan cost:**",
    "**Koszt bez optymalizacji w tej taryfie:**": (
        "**Cost without optimization in this tariff:**"
    ),
    "**Koszt po optymalizacji w tej taryfie:**": (
        "**Cost after optimization in this tariff:**"
    ),
    "**Oszczędność dzięki przesunięciu poboru:**": (
        "**Savings from shifting grid consumption:**"
    ),
    "**Ten sam pobór w G11:**": "**Same import in G11:**",
    "**Szacowana oszczędność:**": "**Estimated savings:**",
    "**Szacowana oszczędność względem G11:**": (
        "**Estimated savings versus G11:**"
    ),
    "**Model obciążenia:**": "**Load model:**",
    "**Rezerwa planowania:**": "**Planning reserve:**",
    "**Historyczna korekta prognozy PV:**": (
        "**Historical PV forecast correction:**"
    ),
    "**Efektywna korekta prognozy na dzisiaj:**": (
        "**Effective forecast correction for today:**"
    ),
    "**Kontrola produkcji w locie:**": "**Live production check:**",
    "prognoza do teraz": "forecast through now",
    "rzeczywista": "actual",
    "pewność korekty": "correction confidence",
    "**Automatyczna korekta prognozy PV:**": (
        "**Automatic PV forecast correction:**"
    ),
    "dni historii": "history days",
    "**Przewidywany SOC na końcu horyzontu:**": (
        "**Forecast SOC at the end of the horizon:**"
    ),
    "⚠️ **Brak wymaganych danych:**": "⚠️ **Required data missing:**",
    "**Wybrane bloki:**": "**Selected slots:**",
    "'sieć → dom; zachowanie baterii'": "'grid → home; battery preserved'",
    "'ładowanie magazynu'": "'battery charging'",
    "'dom + ładowanie magazynu'": "'home supply + battery charging'",
    "}.get(slot.action, 'ładowanie')": "}.get(slot.action, 'charging')",
    "· cel {{ slot.target_soc_percent }}% SOC": (
        "· target {{ slot.target_soc_percent }}% SOC"
    ),
    "strefa": "period",
    "Brak zaplanowanego doładowania.": "No top-up is planned.",
    "Automatyka korzysta z tej samej prognozy **Solcast PV Forecast** i": (
        "The automation uses the same **Solcast PV Forecast** and"
    ),
    "historii rzeczywistego obciążenia domu co RCE. Co 30 minut symuluje": (
        "actual home-load history as RCE. Every 30 minutes it simulates"
    ),
    "bilans do końca jutra. Najpierw wykorzystuje tanią sieć bezpośrednio": (
        "the balance through tomorrow. It first supplies the home directly"
    ),
    "do zasilania domu, jeżeli pozwala to zachować energię baterii na": (
        "from the low-tariff grid when this preserves battery energy for later"
    ),
    "późniejsze drogie godziny. Dopiero pozostały deficyt uzupełnia przez": (
        "expensive hours. Only the remaining deficit is stored in the battery,"
    ),
    "ładowanie magazynu — w możliwie najpóźniejszych tanich blokach.": (
        "using the latest feasible low-price slots."
    ),
    "bilans do końca jutra. W trybie **Grid Charge** ustawiona moc jest": (
        "the balance through tomorrow. In **Grid Charge** mode, the configured"
    ),
    "wspólnym limitem wejścia AC: falownik najpierw zasila bieżące": (
        "power is a shared AC input limit: the inverter first supplies the"
    ),
    "odbiorniki, a dopiero niewykorzystaną częścią tej mocy ładuje": (
        "current home load and uses only the remaining power to charge the"
    ),
    "magazyn do wyznaczonego SOC. Jeżeli celem jest tylko zachowanie": (
        "battery to its target SOC. If the goal is only to preserve battery"
    ),
    "energii baterii na późniejsze drogie godziny, automat zamraża": (
        "energy for later expensive hours, the automation freezes the current"
    ),
    "bieżący SOC i używa sieci do obsługi domu — nie próbuje ładować": (
        "SOC and uses the grid for the home load — it does not try to charge"
    ),
    "baterii ponad ten poziom.": "the battery above that level.",
    "Rezerwa planowania jest zawsze liczona z aktualnego progu awaryjnego": (
        "The planning reserve is always calculated from the current emergency"
    ),
    "**Self-Use + dodatkowego zapasu SOC**. Nie ma stałej wartości 27%.": (
        "**Self-Use threshold plus the additional SOC margin**. There is no fixed 27% value."
    ),
    "Jeżeli magazyn pozostaje poniżej tego progu, automat uzupełnia tylko": (
        "If the battery remains below that threshold, the automation restores only"
    ),
    "brakującą energię w taniej strefie, o ile PV nie odbuduje jej wcześniej.": (
        "the missing energy in a low-price period unless PV restores it earlier."
    ),
    "Po zebraniu wiarygodnej porcji produkcji dziennej automat porównuje": (
        "After collecting a reliable amount of daytime production, the automation compares"
    ),
    "energię rzeczywistą z krzywą Solcast. Trwały niedobór, np. wyłączony": (
        "actual energy with the Solcast curve. Sustained underproduction, such as a disabled"
    ),
    "string PV, obniża prognozę tylko na dzisiaj i pozwala skorygować plan": (
        "PV string, lowers only today's forecast and lets the plan be corrected"
    ),
    "przed kolejnym tanim oknem. Małe próbki tuż po wschodzie słońca są": (
        "before the next low-price window. Small samples just after sunrise are"
    ),
    "ignorowane.": "ignored.",
    "Zimą, przy niskiej produkcji i dużym zużyciu, może zachować baterię": (
        "In winter, with low PV production and high demand, the plan can hold"
    ),
    "podczas taniej strefy oraz dodatkowo ją naładować na kolejne drogie": (
        "the battery during the low-price period and additionally charge it for"
    ),
    "godziny. Moc, maksymalny SOC, limity BMS i straty obu konwersji są": (
        "the following expensive hours. Charge power, maximum SOC, BMS limits"
    ),
    "uwzględniane w obliczeniach.": (
        "and both conversion losses are included in the calculation."
    ),
    "godziny. Moc całego systemu, maksymalny SOC, osobne limity ładowania": (
        "the following expensive hours. Total system power, maximum SOC and"
    ),
    "i rozładowania BMS oraz straty obu konwersji są uwzględniane w": (
        "separate BMS charge/discharge limits and conversion losses are included in"
    ),
    "obliczeniach. Marginalne przesunięcie energii jest odrzucane, jeżeli": (
        "the calculation. A marginal energy shift is rejected when"
    ),
    "RCE i ładowanie taryfowe są teraz rozdzielone. Włączenie jednego": (
        "RCE and tariff charging are currently separate. Enabling one"
    ),
    "automatu wyłącza drugi. Ręczne harmonogramy EMS mają pierwszeństwo.": (
        "automation disables the other. Manual EMS schedules take priority."
    ),
    "Domyślne ceny są tylko przykładem opartym na średniej cenie sprzedaży": (
        "Default prices are examples based on the average regulated sale price"
    ),
    "zatwierdzonej przez URE na 2026 r. oraz zmiennych stawkach dystrybucji": (
        "approved by URE for 2026 and variable distribution charges from"
    ),
    "TAURON. Wpisz **łączną cenę zmienną 1 kWh ze swojej faktury**: energię,": (
        "TAURON. Enter the **total variable price per kWh from your bill**: energy,"
    ),
    "dystrybucję zmienną, opłatę jakościową, OZE i kogeneracyjną. Opłat": (
        "variable distribution, quality, renewable, and cogeneration charges."
    ),
    "stałych nie uwzględnia się, bo nie zależą od godziny ładowania.": (
        "Fixed charges are excluded because they do not depend on charging time."
    ),
    "Typowe okna G12 to **22:00–06:00 i 13:00–15:00**. G12w obejmuje też": (
        "Typical G12 periods are **22:00–06:00 and 13:00–15:00**. G12w also includes"
    ),
    "całe weekendy i dni ustawowo wolne. G13 ma trzy strefy, a ich godziny": (
        "entire weekends and public holidays. G13 has three periods, whose times"
    ),
    "mogą zależeć od operatora i pory roku — zawsze sprawdź własną umowę.": (
        "may depend on the operator and season — always check your own contract."
    ),
    "Profile **PGE, TAURON, ENEA, ENERGA i STOEN** automatycznie dobierają": (
        "The **PGE, TAURON, ENEA, ENERGA and STOEN** profiles automatically select"
    ),
    "godziny stref, weekendy, polskie święta i — tam, gdzie taryfa tego": (
        "time periods, weekends, Polish public holidays and — where required —"
    ),
    "wymaga — sezon letni lub zimowy. Ceny poglądowe pochodzą z taryf 2026": (
        "the summer or winter season. Indicative prices use the 2026 tariffs"
    ),
    "regionalnego sprzedawcy z urzędu i wybranego operatora.": (
        "of the regional incumbent supplier and selected DSO."
    ),
    "Użytkownik z innym sprzedawcą wybiera **Manual** i wpisuje łączną": (
        "A user with a different supplier selects **Manual** and enters the total"
    ),
    "zmienną cenę 1 kWh ze swojej faktury: energię, dystrybucję zmienną,": (
        "variable price per kWh from their bill: energy, variable distribution,"
    ),
    "opłatę jakościową, OZE i kogeneracyjną. Opłat stałych nie uwzględnia": (
        "quality, RES and cogeneration charges. Fixed charges are excluded"
    ),
    "się, bo nie zależą od godziny ładowania.": (
        "because they do not depend on the charging time."
    ),
    "Taryfy i godziny operatorów": "DSO tariffs and time periods",
    "Porównywarka ofert URE": "URE offer comparison",
    "Taryfy i godziny TAURON": "TAURON tariffs and periods",
    "Rzeczywiste zużycie odbiorników dzisiaj": (
        "Actual load consumption today"
    ),
    "PV → odbiorniki — rejestr diagnostyczny": (
        "PV → loads — diagnostic register"
    ),
    "Energia oddana przez baterię — diagnostycznie": (
        "Energy supplied by battery — diagnostic"
    ),
    "Energia pobrana z sieci — diagnostycznie": (
        "Energy imported from grid — diagnostic"
    ),
    "EMS — powiadomienia push o zmianach statusu": (
        "EMS — push notifications for status changes"
    ),
    "EMS — telefon dla powiadomień (encja notify)": (
        "EMS — notification phone (notify entity)"
    ),
    "EMS — powiadomienie push o zmianie statusu": (
        "EMS — push notification for status change"
    ),
    "Wysyła do wybranego telefonu aktualny tryb EMS oraz stan pracy falownika": (
        "Sends the current EMS mode and inverter operating status to the selected phone"
    ),
    "po zmianie któregokolwiek z tych statusów.": (
        "when either status changes."
    ),
    "Powiadomienia push": "Push notifications",
    "Powiadomienia o zmianach EMS": "EMS change notifications",
    "Telefon (encja notify)": "Phone (notify entity)",
    "Stan powiadomień": "Notification status",
    "Brak poprawnej encji notify": "No valid notify entity",
    "Hoymiles — zmiana statusu": "Hoymiles — status change",
    "Tryb EMS:": "EMS mode:",
    "Stan falownika:": "Inverter status:",
    "Zmieniono:": "Changed:",
    "stan pracy falownika": "inverter operating status",
    "tryb EMS": "EMS mode",
    "Autokonsumpcja (Self-Use)": "Self-Use",
    "Praca wyspowa (Off-Grid)": "Off-grid operation",
    "Gotowe": "Ready",
    "☕ Wesprzyj rozwój projektu": "☕ Support project development",
    "Projekt jest rozwijany niezależnie i testowany na rzeczywistych": (
        "This independent project is tested on real installations."
    ),
    "instalacjach. Twoje wsparcie pomaga rozwijać bezpieczną automatykę": (
        "Your support helps improve safe automation"
    ),
    "EMS/RCE, sprawdzać kolejne konfiguracje falowników i tworzyć lepszą": (
        "for EMS/RCE, test more inverter configurations, and create better"
    ),
    "dokumentację.": "documentation.",
    "Jeśli integracja oszczędziła Ci czas lub pomaga lepiej wykorzystać": (
        "If the integration has saved you time or helps you use"
    ),
    "energię, możesz wesprzeć jej dalszy rozwój:": (
        "your energy more effectively, you can support its continued development:"
    ),
    "☕ Postaw kawę autorowi": "☕ Support the author",
    "Najważniejsze dane do codziennej obsługi falownika. Szczegółowe": (
        "The most important values for everyday inverter operation. Detailed"
    ),
    "rejestry są w osobnych zakładkach.": "registers are available on separate tabs.",
    "Automatyka wymaga zewnętrznej integracji": (
        "The automation requires the external integration"
    ),
    "Wymagane są prognozy **Forecast Today** i **Forecast Tomorrow**.": (
        "**Forecast Today** and **Forecast Tomorrow** are required."
    ),
    "Encje są wykrywane automatycznie w polskiej i angielskiej wersji HA;": (
        "The entities are detected automatically in Polish and English HA;"
    ),
    "w razie niestandardowych nazw można wpisać je u góry.": (
        "custom entity ids can be entered above."
    ),
    "Algorytm symuluje bilans co 30 minut od teraz przez dzisiejszą noc,": (
        "The optimizer simulates the balance every 30 minutes from now through tonight,"
    ),
    "cały kolejny dzień i następną noc do **90 minut po wschodzie**.": (
        "the whole next day and the following night until **90 minutes after sunrise**."
    ),
    "Najpierw odkłada poza sprzedażą rezerwę awaryjną Self-Use, korektę": (
        "It first excludes the Self-Use outage reserve, the safety"
    ),
    "bezpieczeństwa oraz pełne przewidywane zużycie domu do końca": (
        "correction, and the full forecast home consumption through"
    ),
    "najbliższej chronionej nocy. Prognozowane PV przed zachodem nie": (
        "the end of the nearest protected night from export. Forecast PV before sunset does not"
    ),
    "pomniejsza tej nocnej rezerwy. Dopiero pozostałą energię przydziela": (
        "reduce this night reserve. Only the remaining energy is assigned"
    ),
    "Najpierw zabezpiecza zasilanie domu, rezerwę awaryjną Self-Use oraz": (
        "It first protects the home supply, the Self-Use outage reserve, and"
    ),
    "korektę bezpieczeństwa. Dopiero pozostałą energię przydziela do": (
        "the safety correction. Only the remaining energy is assigned to"
    ),
    "najdroższych dostępnych bloków RCE dzisiaj lub jutro.": (
        "the most valuable available RCE slots today or tomorrow."
    ),
    "wykryta liczba falowników × ustawiony procent rozładowania**.": (
        "detected inverter count × configured discharge percentage**."
    ),
    "Przez pierwsze 24 godziny używane jest awaryjne zużycie dobowe z pola": (
        "For the first 24 hours, the fallback daily consumption entered"
    ),
    "u góry; później zastępuje je średnia z ostatnich czterech dni.": (
        "above is used; it is then replaced by the four-day average."
    ),
    "Pole rezerwy Self-Use nie jest zmieniane. Pozostaje energią dostępną": (
        "The Self-Use reserve setting is not changed. This energy remains available"
    ),
    "poniżej normalnego progu wyłącznie podczas zaniku sieci.": (
        "below the normal threshold only during a grid outage."
    ),
    "Cena sprzedaży nie jest ustawiana ręcznie. Optymalizator porównuje": (
        "The export price is not configured manually. The optimizer compares"
    ),
    "pełny wynik finansowy wszystkich dostępnych bloków w horyzoncie,": (
        "the full financial result of every available slot in the horizon,"
    ),
    "wybiera najwyższe ceny i uwzględnia naturalny eksport PV. Wcześniejsze": (
        "selects the highest prices and includes natural PV export. An earlier"
    ),
    "rozładowanie wykona tylko wtedy, gdy zwiększy przychód lub utworzy": (
        "discharge is used only when it increases revenue or creates"
    ),
    "miejsce w baterii przed późniejszą nadwyżką sprzedawaną taniej.": (
        "battery headroom before a later surplus would be sold more cheaply."
    ),
    "Automatyka wróci do **Autokonsumpcji (Self-Use)** po zakończeniu": (
        "The automation returns to **Self-Use** after the selected"
    ),
    "wybranego bloku, po osiągnięciu minimalnego SOC albo po wyłączeniu": (
        "slot ends, the minimum SOC is reached, or the"
    ),
    "przełącznika. Blokada sprzedaży wyklucza wskazane godziny z planu.": (
        "switch is disabled. The export lockout excludes the configured hours."
    ),
    "autokonsumpcji.": "Self-Use.",
    "5% zapasu chroni przed zmianą napięcia i limitu BMS.": (
        "A 5% buffer protects against changes in voltage and the BMS limit."
    ),
    "Automatyka RCE — ustawienia": "RCE automation — settings",
    "Automatyka EMS": "EMS automation",
    "RCE i Wyniki": "RCE and Results",
    "Wyniki sprzedaży — archiwum": "Sales results — archive",
    "Automatyka RCE — konfiguracja wymagana": (
        "RCE automation — required configuration"
    ),
    "Włącz automatyczne rozładowanie według RCE": "Enable automatic discharge using RCE prices",
    "Automatyczna cena graniczna planu": "Automatic plan price floor",
    "Korzyść względem niekontrolowanego eksportu": (
        "Gain versus uncontrolled export"
    ),
    "Korzyść optymalizacji:": "Optimization gain:",
    "brak sprzedaży": "no export planned",
    "Zaplanowany eksport sterowany RCE — od teraz": (
        "Planned RCE-controlled export — from now"
    ),
    "Prognozowana nadwyżka w Self-Use — od teraz": (
        "Forecast Self-Use surplus — from now"
    ),
    "Nieunikniona nadwyżka PV pozostała po optymalizacji": (
        "Unavoidable PV surplus remaining after optimization"
    ),
    "Łączny prognozowany eksport — od teraz": (
        "Total forecast export — from now"
    ),
    "Cała energia wysłana do sieci dzisiaj": (
        "Total energy exported to grid today"
    ),
    "Zrealizowany eksport sterowany RCE dzisiaj": (
        "Realized RCE-controlled export today"
    ),
    "Zrealizowana naturalna nadwyżka PV dzisiaj": (
        "Realized natural PV surplus today"
    ),
    "Eksport nierozpoznany po restarcie lub aktualizacji": (
        "Export unclassified after restart or upgrade"
    ),
    "Przychód ze sterowanego eksportu dzisiaj": (
        "Controlled-export revenue today"
    ),
    "Przychód z naturalnej nadwyżki dzisiaj": (
        "Natural-surplus revenue today"
    ),
    "Łączny zrealizowany przychód dzisiaj": (
        "Total realized revenue today"
    ),
    "Zaplanowany eksport sterowany od teraz:": (
        "Planned controlled export from now:"
    ),
    "Nadwyżka prognozowana przy pozostawieniu Self-Use:": (
        "Forecast surplus if Self-Use remains active:"
    ),
    "Nieunikniona nadwyżka PV pozostała po optymalizacji:": (
        "Unavoidable PV surplus remaining after optimization:"
    ),
    "Łączny przyszły eksport od teraz:": (
        "Total future export from now:"
    ),
    "Łączny przyszły szacunkowy przychód:": (
        "Total future estimated revenue:"
    ),
    "Dane przyszłego planu nie zawierają energii sprzedanej wcześniej": (
        "Future-plan values do not include energy sold earlier"
    ),
    "dzisiaj. Eksport zrealizowany jest rozdzielany wyżej na sprzedaż": (
        "today. Realized export is split above into"
    ),
    "sterowaną RCE i naturalną nadwyżkę w trybie Self-Use.": (
        "RCE-controlled sales and natural surplus exported in Self-Use."
    ),
    "EMS RCE — data rozliczenia eksportu": (
        "EMS RCE — export accounting date"
    ),
    "EMS RCE — zrealizowany eksport sterowany (magazyn)": (
        "EMS RCE — realized controlled export (store)"
    ),
    "EMS RCE — zrealizowana naturalna nadwyżka (magazyn)": (
        "EMS RCE — realized natural surplus (store)"
    ),
    "EMS RCE — przychód ze sterowanego eksportu (magazyn)": (
        "EMS RCE — controlled export revenue (store)"
    ),
    "EMS RCE — przychód z naturalnej nadwyżki (magazyn)": (
        "EMS RCE — natural surplus revenue (store)"
    ),
    "EMS RCE — eksport nierozpoznany (magazyn)": (
        "EMS RCE — unclassified export (store)"
    ),
    "EMS RCE — punkt kontrolny licznika sprzedaży": (
        "EMS RCE — grid sell meter checkpoint"
    ),
    "EMS RCE — rozdzielenie zrealizowanego eksportu": (
        "EMS RCE — realized export accounting"
    ),
    "Dodatni znak rejestru oznacza energię oddawaną do sieci.": (
        "A positive register value means power exported to the grid."
    ),
    "Rozlicza dodatnie przyrosty sprzętowego licznika energii oddanej do": (
        "Accounts for positive increments of the inverter grid-sell energy"
    ),
    "sieci. Eksport podczas aktywnego cyklu RCE jest zapisywany osobno od": (
        "counter. Export during an active RCE cycle is stored separately from"
    ),
    "naturalnej nadwyżki w trybie Self-Use. Znacznik RCE pozostaje aktywny": (
        "the natural Self-Use surplus. The RCE marker remains active"
    ),
    "również podczas przerwy komunikacji z ESP.": (
        "during an ESP communication outage."
    ),
    "Moc znamionowa jednego falownika": "Rated power of one inverter",
    "Moc rozładowania do sieci": "Grid discharge power",
    "EMS RCE — żądana moc rozładowania do sieci": (
        "EMS RCE — requested grid discharge power"
    ),
    "Bezpieczna moc rozładowania według BMS": (
        "Safe discharge power reported by BMS"
    ),
    "Efektywny limit rozładowania": "Effective discharge limit",
    "Encja Solcast — prognoza na dzisiaj": "Solcast entity — today forecast",
    "Minimalny SOC akumulatora": "Minimum battery SOC",
    "Zużycie dobowe przed zebraniem historii": (
        "Daily consumption before history is available"
    ),
    "Dynamiczna rezerwa SOC": "Dynamic SOC reserve",
    "Automatycznie wyliczaj minimalny SOC": "Automatically calculate minimum SOC",
    "Korekta bezpieczeństwa SOC (+)": "SOC safety correction (+)",
    "Obowiązujący minimalny SOC": "Effective minimum SOC",
    "Próg zapisany do falownika": "Threshold written to the inverter",
    "Prognoza energii i rezerwa SOC": "Energy forecast and SOC reserve",
    "Encja Solcast — prognoza na jutro": "Solcast entity — tomorrow forecast",
    "RCE — stan, prognoza i wynik": "RCE — status, forecast and result",
    "Stan optymalizatora": "Optimizer status",
    "Prognozowana produkcja PV dzisiaj": "Forecast PV production today",
    "Pozostała prognozowana produkcja dzisiaj": (
        "Remaining forecast PV production today"
    ),
    "Prognozowana produkcja PV jutro": "Forecast PV production tomorrow",
    "Średnie dobowe zużycie LOAD": "Average daily LOAD",
    "Średnie dobowe zużycie LOAD — 4 dni": "Average daily LOAD — 4 days",
    "Zebrana historia zużycia dobowego": "Collected daily-load history",
    "Pomiar zużycia okna nocnego aktywny": (
        "Night-window consumption sampling active"
    ),
    "Pełne okno nocne — zachód/wschód ± 1,5 h": (
        "Full night window — sunset/sunrise ± 1.5 h"
    ),
    "Chroniony czas pracy domu — najbliższa noc": (
        "Protected home runtime — nearest night"
    ),
    "Średnie zużycie w oknie nocnym — 4 dni": (
        "Average protected-window consumption — 4 days"
    ),
    "Średnie zużycie w oknie nocnym": (
        "Average protected-window consumption"
    ),
    "Zebrana historia zużycia nocnego": "Collected night-load history",
    "PV → odbiorniki dzisiaj": "PV → loads today",
    "Bateria → odbiorniki dzisiaj": "Battery → loads today",
    "Sieć → odbiorniki dzisiaj": "Grid → loads today",
    "PV → odbiorniki teraz": "PV → loads now",
    "Modelowane dzienne zużycie odbiorników": "Modeled daytime load",
    "Energia potrzebna do rozpoczęcia produkcji PV": (
        "Energy required until PV production starts"
    ),
    "Przewidywany deficyt energii jutro": "Expected energy deficit tomorrow",
    "Deficyt dobowy według prognozy PV": "Daily deficit from PV forecast",
    "Energia domu chroniona przed sprzedażą": (
        "Home energy protected from export"
    ),
    "Łączna energia chroniona dla domu": "Total energy protected for the home",
    "Rezerwa bazowa Self-Use i korekta": (
        "Base Self-Use reserve and safety correction"
    ),
    "Chronione zużycie domu do końca najbliższej nocy": (
        "Protected home consumption until the end of the nearest night"
    ),
    "Dodatkowa rezerwa ponad próg awaryjny": (
        "Additional reserve above the outage threshold"
    ),
    "Dodatkowa rezerwa wynikająca z prognozy": (
        "Additional forecast-derived reserve"
    ),
    "Energia dostępna teraz ponad rezerwę": (
        "Energy currently available above the reserve"
    ),
    "Planowane rozładowanie baterii do sieci": (
        "Planned battery discharge to grid"
    ),
    "Naturalna nadwyżka PV do sieci": "Natural PV surplus to grid",
    "Łączny prognozowany eksport": "Total forecast export",
    "Przewidywany SOC na końcu horyzontu": (
        "Forecast SOC at the end of the horizon"
    ),
    "Energia wysłana do sieci dzisiaj": "Energy exported to grid today",
    "Szacunkowy przychód RCE dzisiaj": "Estimated RCE revenue today",
    "Pojemność magazynu z falownika": "Battery capacity from inverter",
    "Rezerwa awaryjna SOC — zanik sieci": "Outage SOC reserve",
    "Wyliczony minimalny SOC dla RCE": "Calculated minimum SOC for RCE",
    "Dane rezerwy kompletne": "Reserve data complete",
    "Stan wyliczania rezerwy": "Reserve calculation status",
    "Wymagane źródło prognozy": "Required forecast source",
    "Dynamiczna rezerwa wymaga zewnętrznej integracji": (
        "Dynamic reserve requires the external integration"
    ),
    "producenta **BJReplay**:": "by **BJReplay**:",
    "instalacja i konfiguracja": "installation and configuration",
    "Domyślna encja to": "The default entity is",
    "(HA po angielsku) albo": "(English HA) or",
    "(HA po polsku).": "(Polish HA).",
    "instalacji ma inną nazwę, wpisz ją w polu": (
        "installation uses a different name, enter it in the"
    ),
    "**Encja Solcast**.": "**Solcast entity** field.",
    "instalacji ma inną nazwę, wpisz ją w polu **Encja Solcast**.": (
        "installation uses a different name, enter it in the **Solcast entity** field."
    ),
    "Jeśli w Twojej": "If your",
    "Wyliczenie:": "Calculation:",
    "**rezerwa awaryjna Self-Use + deficyt LOAD względem prognozy PV +": (
        "**Self-Use outage reserve + LOAD deficit against the PV forecast +"
    ),
    "**rezerwa awaryjna Self-Use + pozostałe zużycie domu w najbliższym": (
        "**Self-Use outage reserve + remaining home demand in the nearest"
    ),
    "oknie nocnym + dodatkowy dobowy deficyt prognozy PV + korekta": (
        "night window + the additional daily PV forecast deficit +"
    ),
    "korekta bezpieczeństwa użytkownika**.": "user safety correction**.",
    "bezpieczeństwa użytkownika**.": "user safety correction**.",
    "Okno nocne rozpoczyna się **90 minut przed zachodem** i kończy": (
        "The night window starts **90 minutes before sunset** and ends"
    ),
    "**90 minut po wschodzie słońca**. W ciągu dnia automatyka chroni": (
        "**90 minutes after sunrise**. During the day, the automation protects"
    ),
    "energię dla całego nadchodzącego okna nocnego. Od 90 minut przed": (
        "energy for the entire upcoming night window. From 90 minutes before"
    ),
    "zachodem chroniona energia maleje wraz z pozostałym czasem, dzięki": (
        "sunset, protected energy decreases with the remaining time, so the"
    ),
    "czemu automat może sprzedać tylko tę część magazynu, która nie będzie": (
        "automation can export only the part of the battery that the home will not"
    ),
    "potrzebna domowi do rozpoczęcia produkcji PV.": (
        "need before PV production starts."
    ),
    "tylko tę część magazynu, która nie będzie potrzebna domowi do": (
        "only the part of the battery that the home will not need before"
    ),
    "rozpoczęcia produkcji PV.": "PV production starts.",
    "Przez pierwszą dobę zużycie nocne jest szacowane z czterodniowej": (
        "For the first day, night consumption is estimated from the four-day"
    ),
    "średniej dobowej. Później automat korzysta z rzeczywistego zużycia": (
        "daily average. Afterwards, the automation uses measured consumption"
    ),
    "z maksymalnie czterech ostatnich chronionych okien. Korekta jest": (
        "from up to four previous protected windows. The correction is"
    ),
    "dodawana w punktach procentowych. Przy brakujących danych Solcast,": (
        "added in percentage points. If Solcast, LOAD, sun, or battery"
    ),
    "LOAD, słońca lub pojemności magazynu sprzedaż RCE zostanie bezpiecznie": (
        "capacity data is missing, RCE export is safely"
    ),
    "zablokowana.": "blocked.",
    "Korekta jest dodawana w punktach procentowych. Przy brakujących": (
        "The correction is added in percentage points. If Solcast data,"
    ),
    "danych Solcast, historii LOAD lub pojemności magazynu automatyczna": (
        "LOAD history, or battery capacity is missing, automatic RCE"
    ),
    "sprzedaż RCE zostanie bezpiecznie zablokowana.": (
        "export is safely blocked."
    ),
    "Blokada sprzedaży": "Export lockout",
    "Włącz blokadę sprzedaży": "Enable export lockout",
    "Początek blokady": "Lockout start",
    "Koniec blokady": "Lockout end",
    "Blokada aktywna teraz": "Lockout active now",
    "Bieżąca cena RCE": "Current RCE price",
    "Stan automatyki": "Automation status",
    "Odbiór — moc": "Loads — power",
    "Odbiór — moc L1": "Load power L1",
    "Odbiór — moc L2": "Load power L2",
    "Odbiór — moc L3": "Load power L3",
    "Odbiorniki — moc łącznie": "Loads — total power",
    "title: Zyski": "title: Profits",
    "title: Produkcja PV": "title: PV Production",
    "Instalacja pracuje na Twój wynik": "Your installation is working for you",
    "Dzisiaj sieć przyjęła": "Today the grid received",
    "a wyliczony": "and the calculated",
    "przychód według cen RCE wynosi": "revenue at RCE prices is",
    "a wyliczony przychód według cen RCE wynosi": (
        "and the calculated revenue at RCE prices is"
    ),
    "Średnia uzyskana cena to": "The average achieved price is",
    "Każda sprzedana kilowatogodzina jest rozliczana z ceną obowiązującą": (
        "Every exported kilowatt-hour is settled at the price applicable"
    ),
    "w chwili eksportu. Liczniki okresowe zachowują dane po restartach": (
        "at the time of export. Period meters retain data after Home Assistant"
    ),
    "Home Assistanta.": "restarts.",
    "Przychód ze sprzedaży energii": "Energy export revenue",
    "Energia oddana do sieci": "Energy exported to the grid",
    "Produkcja PV — bieżące okresy": "PV production — current periods",
    "Najważniejsze podsumowania i historia produkcji z instalacji.": (
        "Key production summaries and installation history."
    ),
    "Wykresy korzystają z długoterminowych statystyk Home Assistanta,": (
        "The charts use Home Assistant long-term statistics,"
    ),
    "dlatego dane pozostają dostępne także po restartach.": (
        "so the data remains available after restarts."
    ),
    "title: Bieżące okresy": "title: Current periods",
    "title: Porównanie rok do roku": "title: Year-over-year comparison",
    "Bieżący rok": "Current year",
    "Poprzedni rok": "Previous year",
    "Dwa lata temu": "Two years ago",
    "Produkcja dzienna — ostatnie 90 dni": (
        "Daily production — last 90 days"
    ),
    "Produkcja tygodniowa — ostatnie 52 tygodnie": (
        "Weekly production — last 52 weeks"
    ),
    "Produkcja miesięczna — ostatnie 3 lata": (
        "Monthly production — last 3 years"
    ),
    "Produkcja roczna — archiwum": "Yearly production — archive",
    "name: Produkcja PV": "name: PV production",
    "Ten tydzień": "This week",
    "Ten miesiąc": "This month",
    "Ten rok": "This year",
    "name: Dzisiaj": "name: Today",
    "Średnia cena sprzedaży": "Average export price",
    "| Okres | Średnia cena | Przychód | Energia |": (
        "| Period | Average price | Revenue | Energy |"
    ),
    "('Dzisiaj', 'daily')": "('Today', 'daily')",
    "('Tydzień', 'weekly')": "('Week', 'weekly')",
    "('Miesiąc', 'monthly')": "('Month', 'monthly')",
    "('Rok', 'yearly')": "('Year', 'yearly')",
    "Przychód — ostatnie 30 dni": "Revenue — last 30 days",
    "Eksport — ostatnie 30 dni": "Export — last 30 days",
    "name: Przychód": "name: Revenue",
    "name: Energia do sieci": "name: Energy to grid",
    "Dzisiejszy wynik — szczegóły": "Today's result — details",
    "Aktualna moc oddawana do sieci": "Current grid export power",
    "Eksport sterowany przez RCE": "RCE-controlled export",
    "Naturalna nadwyżka PV": "Natural PV surplus",
    "Eksport nierozpoznany": "Unclassified export",
    "Przychód ze sterowania RCE": "RCE-controlled revenue",
    "Przychód z naturalnej nadwyżki": "Natural surplus revenue",
    "Prognozowana korzyść optymalizacji": "Forecast optimization benefit",
    "Stan automatyki RCE": "RCE automation status",
    "Wynik od uruchomienia liczników": "Result since meter activation",
    "Przychód łącznie": "Total revenue",
    "Eksport łącznie": "Total export",
    "Licznik falownika": "Inverter meter",
    "RCE — ceny i plan rozładowania": "RCE — prices and discharge plan",
    "RCE — dzisiaj": "RCE — today",
    "RCE — jutro": "RCE — tomorrow",
    "Plan rozładowań RCE": "RCE discharge plan",
    "**Horyzont:**": "**Horizon:**",
    "**Najbardziej opłacalne okresy wybrane przez algorytm:**": (
        "**Most profitable periods selected by the optimizer:**"
    ),
    "Brak danych do przygotowania planu.": "No data available to build a plan.",
    "**Łączny plan:**": "**Total plan:**",
    "bloków po 30 min": "30-minute slots",
    "**Prognozowany eksport:**": "**Forecast export:**",
    "**Szacunkowy przychód:**": "**Estimated revenue:**",
    "**Zakres cen:**": "**Price-data scope:**",
    "**Tryb planu:**": "**Plan mode:**",
    "dzisiaj i jutro": "today and tomorrow",
    "tylko dzisiaj — jutro zostanie dopisane automatycznie": (
        "today only — tomorrow will be added automatically"
    ),
    "podgląd — automatyczne rozładowanie jest wyłączone": (
        "preview — automatic discharge is disabled"
    ),
    "else 'aktywny'": "else 'active'",
    "**Planowane rozładowanie baterii:**": "**Planned battery discharge:**",
    "**Naturalna nadwyżka PV:**": "**Natural PV surplus:**",
    "**Łączny prognozowany eksport:**": "**Total forecast export:**",
    "**Łączny szacunkowy przychód:**": "**Total estimated revenue:**",
    "Do zebrania co najmniej 24 godzin historii używane jest awaryjne": (
        "Until at least 24 hours of history is collected, the configured fallback"
    ),
    "zużycie dobowe z pola u góry. Następnie algorytm korzysta z dostępnej": (
        "daily consumption above is used. The optimizer then uses the available"
    ),
    "historii i stopniowo dochodzi do pełnej średniej z czterech dni;": (
        "history and gradually builds a complete four-day average;"
    ),
    "faktyczne pokrycie historii jest pokazane w karcie stanu.": (
        "the actual history coverage is shown in the status card."
    ),
    "Moc systemu jest liczona jako **moc jednego falownika × automatycznie": (
        "System power is calculated as **one inverter's rated power × the automatically"
    ),
    "wykryta liczba falowników × ustawiony procent rozładowania**.": (
        "detected inverter count × the configured discharge percentage**."
    ),
    "Rozładowanie do sieci": "Grid discharge",
    "Ładowanie z sieci": "Grid charge",
    "Włącz harmonogram codzienny": "Enable daily schedule",
    "Godzina rozpoczęcia": "Start time",
    "Czas trwania": "Duration",
    "Pozostały czas bieżącego cyklu": "Current cycle remaining time",
    "Uruchom rozładowanie teraz": "Start grid discharge now",
    "Uruchom ładowanie teraz": "Start grid charge now",
    "Codzienne harmonogramy EMS": "Daily EMS schedules",
    "Sterowanie i zakończenie EMS": "EMS control and stop",
    "Tryb pracy": "Operating mode",
    "Zatrzymaj i wróć do Self-Use": "Stop and return to Self-Use",
    "Bieżący przepływ mocy": "Current power flow",
    "Bieżący przepływ energii": "Current energy flow",
    "Prognoza PV (Solcast)": "PV forecast (Solcast)",
    "Pozostało PV (Solcast)": "PV remaining (Solcast)",
    "Śr. dobowe zużycie domu": "Avg. daily home use",
    "name: Dom": "name: Load",
    "name: Sieć": "name: Grid",
    "name: Bateria (A)": "name: Battery (A)",
    "name: Bateria": "name: Battery",
    "Napięcie baterii (falownik)": "Battery voltage (inverter)",
    "Prąd baterii (falownik)": "Battery current (inverter)",
    "Podsumowanie dzienne": "Daily summary",
    "Produkcja dzisiaj": "Production today",
    "Do domu": "To home",
    "Do baterii": "To battery",
    "Do sieci": "To grid",
    "Pobór z sieci — dzisiaj": "Grid import — today",
    "name: Łącznie": "name: Total",
    "name: Do odbiorników": "name: To loads",
    "name: Do magazynu": "name: To battery",
    "Stan systemu i łączność": "System and connectivity",
    "title: Sterowanie": "title: Control",
    "Alarmy — szybki podgląd": "Alarms — quick view",
    "Wyczyść alarmy falownika": "Clear inverter faults",
    "Stany komunikacji i pracy": "Communication and operating states",
    "Alarmy falownika i baterii": "Inverter and battery alarms",
    "Stan pracy falownika": "Inverter operating state",
    "Stan pracy systemu": "System operating state",
    "Łącze licznika": "Meter link",
    "Łącze PV": "PV link",
    "Łącze baterii": "Battery link",
    "Stan pracy baterii (BMS)": "Battery operating state (BMS)",
    "Stan pracy baterii": "Battery operating state",
    "Typ baterii (BMS)": "Battery type (BMS)",
    "Parametry ochrony i temperatury": "Protection parameters and temperatures",
    "Temperatura radiatora falownika": "Inverter heatsink temperature",
    "Temperatura radiatora toru baterii": "Battery-stage heatsink temperature",
    "Rezystancja izolacji": "Insulation resistance",
    "Wyprodukowano dzisiaj": "Generated today",
    "Moc — ostatnie 24 godziny [W]": "Power — last 24 hours [W]",
    "Moc — ostatnie 24 godziny": "Power — last 24 hours",
    "Odbiorniki — moc ostatnie 24 godziny [W]": (
        "Loads — power over the last 24 hours [W]"
    ),
    "Zużycie domu — ostatnie 30 dni [kWh]": (
        "Home consumption — last 30 days [kWh]"
    ),
    "name: Zużycie domu": "name: Home consumption",
    "name: Moc": "name: Power",
    "Odbiory — moc": "Loads — power",
    "Odbiory — energia": "Loads — energy",
    "Maksymalna temperatura celi": "Maximum cell temperature",
    "Minimalna temperatura celi": "Minimum cell temperature",
    "Bateria — energia": "Battery — energy",
    "Licznik sieci": "Grid meter",
    "Ustawienia falownika i baterii": "Inverter and battery settings",
    "Błąd oprogramowania": "Software fault",
    "Błąd sprzętowy": "Hardware fault",
    "Błąd baterii (BMS)": "Battery fault (BMS)",
    "Błędy baterii": "Battery faults",
    "Błędy DSP mocy": "DSP power faults",
    "Błędy DSP zabezpieczeń": "DSP safety faults",
    "Błędy komunikacji ARM": "ARM communication faults",
    "Błędy urządzeń peryferyjnych ARM": "ARM peripheral faults",
    "Błędy systemowe ARM": "ARM system faults",
    "Praca z siecią": "On-grid operation",
    "HMID: połączenie prawidłowe": "HMID: connected",
    "Czuwanie": "Standby",
    "Ładowanie": "Charging",
    "Rozładowanie": "Discharging",
    "Parametry ochronne i temperatury": "Protection parameters and temperatures",
    "Stan instalacji": "Installation status",
    "Napięcie L1": "L1 voltage",
    "Napięcie L2": "L2 voltage",
    "Napięcie L3": "L3 voltage",
    "Prąd L1": "L1 current",
    "Prąd L2": "L2 current",
    "Prąd L3": "L3 current",
    "Prąd łączny (suma faz)": "Total current (sum of phases)",
    "Moc L1": "L1 power",
    "Moc L2": "L2 power",
    "Moc L3": "L3 power",
    "Moc łączna": "Total power",
    "Częstotliwość": "Frequency",
    "Temperatura wnętrza falownika (CAV)": "Inverter internal temperature (CAV)",
    "Napięcie PP": "PP voltage",
    "Prąd różnicowy": "Residual current",
    "Moc stringów PV — ostatnie 24 godziny [W]": "PV string power — last 24 hours [W]",
    "Napięcie": "Voltage",
    "Prąd": "Current",
    "PV — moc z bloku energii i źródła zewnętrzne": "PV — energy-block and external-source power",
    "Sieć": "Grid",
    "Przepływy": "Flows",
    "Przepływy — moc": "Flows — power",
    "Przepływy — energia dzisiaj": "Flows — energy today",
    "Przepływy — energia całkowita": "Flows — total energy",
    "Licznik zewnętrznego PV": "External PV meter",
    "EMS — bezpieczny zapis całego bloku 4300–4306": "EMS — safe full-block write 4300–4306",
    "Overview — skrócone rejestry mocy": "Overview — essential power registers",
    "Sieć równoległa falowników": "Parallel inverter network",
    'name: "Topologia sieci"': 'name: "Network topology"',
    'name: "Gotowość sterowania EMS"': 'name: "EMS control readiness"',
    'name: "Moc czynna łącznie"': 'name: "Total active power"',
    'name: "Moc bierna łącznie"': 'name: "Total reactive power"',
    'name: "Typ urządzenia (kod)"': 'name: "Device type (code)"',
    'name: "Wykryta liczba falowników"': 'name: "Detected inverter count"',
    'name: "Adres 1 (Master)"': 'name: "Address 1 (Master)"',
    'name: "Adres 2 (Slave 1)"': 'name: "Address 2 (Slave 1)"',
    'name: "Adres 3 (Slave 2)"': 'name: "Address 3 (Slave 2)"',
    'name: "Adres 4 (Slave 3)"': 'name: "Address 4 (Slave 3)"',
    'name: "Adres 5 (Slave 4)"': 'name: "Address 5 (Slave 4)"',
    'name: "Adres 6 (Slave 5)"': 'name: "Address 6 (Slave 5)"',
    'name: "Adres 7 (Slave 6)"': 'name: "Address 7 (Slave 6)"',
    'name: "Adres 8 (Slave 7)"': 'name: "Address 8 (Slave 7)"',
    'name: "Adres 9 (Slave 8)"': 'name: "Address 9 (Slave 8)"',
    'name: "Adres 10 (Slave 9)"': 'name: "Address 10 (Slave 9)"',
    "Każdy harmonogram działa codziennie, dopóki jego przełącznik jest": (
        "Each schedule runs every day while its switch is"
    ),
    "włączony. Po upływie ustawionego czasu falownik wraca do": (
        "enabled. When the configured duration ends, the inverter returns to"
    ),
    "Nie ustawiaj ładowania i rozładowania na tę samą godzinę.": (
        "Do not schedule charging and discharging at the same time."
    ),
    "Wyłączenie przełącznika blokuje kolejne uruchomienia.": (
        "Turning a schedule off prevents subsequent starts."
    ),
    "Automatyka wróci do **Autokonsumpcji (Self-Use)** po spadku ceny do": (
        "The automation returns to **Self-Use** when the price falls to"
    ),
    "progu lub niżej, po osiągnięciu minimalnego SOC albo po wyłączeniu": (
        "the threshold or below, when minimum SOC is reached, or when the"
    ),
    "przełącznika. W czasie aktywnej blokady sprzedaży okresy są pomijane,": (
        "switch is disabled. Periods inside the active export lockout are skipped,"
    ),
    "a falownik pozostaje w trybie Self-Use.": (
        "and the inverter remains in Self-Use mode."
    ),
    "**Uwaga:** zmiana `System Operation` może zatrzymać falownik.": (
        "**Warning:** changing `System Operation` can stop the inverter."
    ),
    "Encja `select.hoymiles_hit_parallel_networking_command` jest celowo pominięta,": (
        "The `select.hoymiles_hit_parallel_networking_command` entity is intentionally omitted,"
    ),
    "ponieważ w ESPHome ma `disabled_by_default: true`. Włącz ją w Home": (
        "because ESPHome marks it `disabled_by_default: true`. Enable it in Home"
    ),
    "Assistant tylko wtedy, gdy konfigurujesz pracę równoległą.": (
        "Assistant only when configuring parallel operation."
    ),
    "PV — wartości bieżące": "PV — current values",
    "PV — energia dzisiaj": "PV — energy today",
    "PV — energia całkowita": "PV — total energy",
    "Bateria — stan, moc i limity BMS": "Battery — state, power and BMS limits",
    "Sieć — wartości bieżące": "Grid — current values",
    "Sieć — napięcia i częstotliwość": "Grid — voltages and frequency",
    "Sieć — prądy": "Grid — currents",
    "Sieć — moc": "Grid — power",
    "EPS — obciążenie": "EPS — load",
    "Sieć — energia dzisiaj": "Grid — energy today",
    "Sieć — energia całkowita": "Grid — total energy",
    "GEN — wartości bieżące": "GEN — current values",
    "GEN — energia dzisiaj": "GEN — energy today",
    "GEN — energia całkowita": "GEN — total energy",
    "GEN napięcie L1": "GEN voltage L1",
    "GEN napięcie L2": "GEN voltage L2",
    "GEN napięcie L3": "GEN voltage L3",
    "GEN prąd L1": "GEN current L1",
    "GEN prąd L2": "GEN current L2",
    "GEN prąd L3": "GEN current L3",
    "GEN czynna moc L1": "GEN active power L1",
    "GEN czynna moc L2": "GEN active power L2",
    "GEN czynna moc L3": "GEN active power L3",
    "GEN łącznie moc": "GEN total power",
    "GEN moc L1n": "GEN power L1n",
    "GEN moc L2n": "GEN power L2n",
    "GEN moc L3n": "GEN power L3n",
    "GEN energia łącznie dzisiaj": "GEN total energy today",
    "GEN energia L1n dzisiaj": "GEN energy L1n today",
    "GEN energia L2n dzisiaj": "GEN energy L2n today",
    "GEN energia L3n dzisiaj": "GEN energy L3n today",
    "GEN energia łącznie": "GEN total energy",
    "GEN energia L1n łącznie": "GEN total energy L1n",
    "GEN energia L2n łącznie": "GEN total energy L2n",
    "GEN energia L3n łącznie": "GEN total energy L3n",
    "Moc łącznie": "Total power",
    "Złącze GEN": "GEN port",
    "Ograniczanie eksportu i złącze GEN": "Export limiting and GEN port",
    "Limitowanie eksportu (GCF)": "Export limiting (GCF)",
    "Maksymalny limit eksportu": "Maximum export power limit",
    "Tryb złącza GEN": "GEN port mode",
    "Falownik — fazy, moc i magistrala DC": "Inverter — phases, power and DC bus",
    "Falownik — magistrala DC": "Inverter — DC bus",
    "Falownik — fazy AC": "Inverter — AC phases",
    "Falownik — moce AC": "Inverter — AC power",
    "Sterowanie i zakończenie harmonogramu": "Schedule control and stop",
    "Rezerwa SOC — Self-Use": "SOC reserve — Self-Use",
    "Docelowy SOC — ładowanie z sieci": "Target SOC — grid charge",
    "Minimalny SOC — rozładowanie do sieci": "Minimum SOC — grid discharge",
    "Maks. moc ładowania z sieci": "Max. grid charge power",
    "Maks. moc rozładowania do sieci": "Max. grid discharge power",
    "brak aktywności": "inactive",
    "Brak kompletnych danych PSE": "No complete PSE data",
    "Brak danych": "No data",
    "Brak błędu": "No error",
    "Litowa": "Lithium",
    "Kwasowo-fosforanowa": "Lead-acid/phosphate",
    "Symulowana": "Simulated",
    "Brak błędów": "No faults",
    "Niedostępne": "Unavailable",
    "Wyłączona": "Disabled",
    "Wyłączone": "Disabled",
    "wyłączona": "disabled",
    "wyłączono": "disabled",
    "Włączona": "Enabled",
    "Włączone": "Enabled",
    "Autokonsumpcja": "Self-Use",
    "Oczekiwanie": "Waiting",
    "Cena powyżej progu": "Price above threshold",
    "Oczekiwanie na cenę": "Waiting for price",
    "osiągnięto minimalny SOC": "minimum SOC reached",
    "aktywny harmonogram ręczny": "manual schedule active",
    "gotowa": "ready",
    "Dzień:": "Day:",
    "Okresy powyżej ustawionego progu:": "Periods above the configured threshold:",
    "Łączny plan:": "Total plan:",
    "godz.": "h",
    "min": "min",
    "Uruchom": "Run",
    "Zatrzymaj": "Stop",
    "codzienne": "daily",
    "rozładowanie": "discharge",
    "ładowanie": "charge",
    "godzina": "time",
    "początek": "start",
    "koniec": "end",
    "trwa cykl": "cycle active",
    "pozostały czas": "remaining time",
    "włączone": "enabled",
    "wybranych godzinach": "selected hours",
    "rozpocznij": "start",
    "zakończ": "finish",
    "wróć": "return",
    "wymuś": "enforce",
    "sterowanie": "control",
    "według ceny": "by price",
    "odzyskaj cykl po restarcie Home Assistant": "restore cycle after Home Assistant restart",
    "EMS — trwa cykl rozładowania": "EMS — discharge cycle active",
    "EMS — trwa cykl ładowania": "EMS — charge cycle active",
    "EMS — blokada sprzedaży w wybranych godzinach": "EMS — export lockout in selected hours",
    "EMS — godzina rozpoczęcia rozładowania": "EMS — discharge start time",
    "EMS — godzina rozpoczęcia ładowania": "EMS — charge start time",
    "EMS — początek blokady sprzedaży": "EMS — export lockout start",
    "EMS — koniec blokady sprzedaży": "EMS — export lockout end",
    "EMS — czas rozładowania": "EMS — discharge duration",
    "EMS — czas ładowania": "EMS — charge duration",
    "EMS RCE — dynamiczna rezerwa SOC": "EMS RCE — dynamic SOC reserve",
    "EMS RCE — encja prognozy Solcast na dzisiaj": (
        "EMS RCE — Solcast today forecast entity"
    ),
    "EMS RCE — encja prognozy Solcast na jutro": (
        "EMS RCE — Solcast tomorrow forecast entity"
    ),
    "EMS RCE — encja prognozy Solcast na trzeci dzień": (
        "EMS RCE — Solcast Day 3 forecast entity"
    ),
    "Plan RCE oczekuje na ponowne przeliczenie": (
        "The RCE plan is waiting to be recalculated"
    ),
    "Plan RCE nie był raportowany od ponad 5 minut": (
        "The RCE plan has not been reported for more than 5 minutes"
    ),
    "Plan taryfowy oczekuje na ponowne przeliczenie": (
        "The tariff plan is waiting to be recalculated"
    ),
    "Prognoza taryfowa jest nieświeża lub niepełna": (
        "The tariff forecast is stale or incomplete"
    ),
    "Wyrównywanie przerwane przed kolejnym zapisem operacyjnym": (
        "Balancing stopped before the next operational write"
    ),
    "Wyrównywanie przerwane przed zmianą trybu Grid Charge": (
        "Balancing stopped before switching to Grid Charge"
    ),
    "Start taryfowy przerwany przed kolejnym zapisem operacyjnym": (
        "Tariff start stopped before the next operational write"
    ),
    "Aktualizacja taryfowa przerwana przed wydłużeniem okna": (
        "Tariff update stopped before extending the window"
    ),
    "Aktualizacja taryfowa przerwana przed kolejnym zapisem operacyjnym": (
        "Tariff update stopped before the next operational write"
    ),
    "Start RCE przerwany przed kolejnym zapisem operacyjnym": (
        "RCE start stopped before the next operational write"
    ),
    "EMS RCE — moc znamionowa jednego falownika": (
        "EMS RCE — rated power of one inverter"
    ),
    "EMS RCE — dobowe zużycie awaryjne przed zebraniem historii": (
        "EMS RCE — fallback daily consumption before history is available"
    ),
    "EMS RCE — sprawność eksportu z baterii": (
        "EMS RCE — battery export efficiency"
    ),
    "EMS RCE — korekta bezpieczeństwa SOC": (
        "EMS RCE — SOC safety correction"
    ),
    "EMS RCE — ustaw domyślną encję Solcast": (
        "EMS RCE — set default Solcast entity"
    ),
    "EMS RCE — zapisz dynamiczną rezerwę SOC": (
        "EMS RCE — write dynamic SOC reserve"
    ),
    "EMS RCE — sterowanie według zoptymalizowanego planu": (
        "EMS RCE — optimized-plan discharge control"
    ),
    "wybranych najbardziej opłacalnych blokach i ponad SOC potrzebnym do": (
        "the selected most profitable slots and only above the SOC required to"
    ),
    "zasilenia domu. Ręczne timery mają pierwszeństwo.": (
        "supply the home. Manual timers take priority."
    ),
    "# Start RCE: bieżący blok należy do planu, jest zapas SOC i nie trwa": (
        "# Start RCE: the current slot is planned, SOC reserve is available, and"
    ),
    "# żaden ręczny cykl.": "# no manual cycle is active.",
    "# Stop RCE: blok wypadł z planu, osiągnięto minimalny SOC, wyłączono": (
        "# Stop RCE: the slot left the plan, minimum SOC was reached, automation"
    ),
    "# automatykę albo dane PSE stały się niedostępne.": (
        "# was disabled, or PSE data became unavailable."
    ),
    "Tryb ręczny — używany jest próg rozładowania falownika": (
        "Manual mode — using the inverter discharge threshold"
    ),
    "Gotowa — plan zoptymalizowany": "Ready — optimized plan",
    "Oczekiwanie — brak dostępnego okna rynkowego": (
        "Waiting — no available market window"
    ),
    "Zasilanie domu zabezpieczone — brak energii na sprzedaż": (
        "Home supply protected — no energy available for export"
    ),
    "Za mało energii na potrzeby domu — sprzedaż zablokowana": (
        "Insufficient home energy — export blocked"
    ),
    "Brak wymaganych danych — sprzedaż zablokowana": (
        "Required data missing — export blocked"
    ),
    "Błąd obliczeń — sprzedaż zablokowana": (
        "Calculation error — export blocked"
    ),
    "Brak zaplanowanych okresów sprzedaży.": (
        "No export periods are currently planned."
    ),
    "Brak danych Solcast — sprzedaż zablokowana": (
        "No Solcast data — export blocked"
    ),
    "Za mało historii LOAD — wymagane minimum 24 godziny": (
        "Insufficient LOAD history — at least 24 hours required"
    ),
    "Brak danych wschodu lub zachodu słońca — sprzedaż zablokowana": (
        "Missing sunrise or sunset data — export blocked"
    ),
    "Brak pojemności baterii z falownika — sprzedaż zablokowana": (
        "No inverter battery capacity — export blocked"
    ),
    "Nie można obliczyć rezerwy — sprzedaż zablokowana": (
        "Cannot calculate reserve — export blocked"
    ),
    "Gotowa — dynamiczny próg": "Ready — dynamic threshold",
    "Brak danych rezerwy SOC — sprzedaż zablokowana": (
        "No SOC reserve data — export blocked"
    ),
    "Przy pierwszym uruchomieniu wpisuje standardową encję Forecast Tomorrow": (
        "On first start, sets the standard Forecast Tomorrow entity"
    ),
    "integracji Solcast PV Forecast producenta BJReplay. Późniejsze zmiany": (
        "from the BJReplay Solcast PV Forecast integration. Later changes"
    ),
    "użytkownika są zachowywane.": "made by the user are preserved.",
    "Wykrywa angielską albo polską encję Forecast Tomorrow integracji Solcast": (
        "Detects the English or Polish Forecast Tomorrow entity from the Solcast"
    ),
    "Wykrywa angielskie albo polskie encje Forecast Today i Forecast Tomorrow": (
        "Detects the English or Polish Forecast Today and Forecast Tomorrow entities"
    ),
    "integracji Solcast PV Forecast producenta BJReplay. Poprawne własne encje": (
        "from the BJReplay Solcast PV Forecast integration. Valid custom entities"
    ),
    "PV Forecast producenta BJReplay. Poprawna własna encja użytkownika jest": (
        "PV Forecast integration by BJReplay. A valid custom entity entered by the user is"
    ),
    "zachowywana.": "preserved.",
    "Zapisuje do falownika wyliczony minimalny SOC rozładowania. Próg obejmuje": (
        "Writes the calculated minimum discharge SOC to the inverter. The threshold includes"
    ),
    "Zapisuje do falownika wyliczony minimalny SOC rozładowania. Plan chroni": (
        "Writes the calculated minimum discharge SOC to the inverter. The plan protects"
    ),
    "zasilanie domu od teraz, przez dzisiejszą noc, kolejny dzień i następną": (
        "home supply from now through tonight, the next day, and the following"
    ),
    "noc do 90 minut po wschodzie, a następnie wybiera najdroższe okna RCE.": (
        "night until 90 minutes after sunrise, then selects the most valuable RCE slots."
    ),
    "rezerwę awaryjną Self-Use, energię domu chronioną w nocnym oknie,": (
        "the Self-Use outage reserve, home energy protected in the night window,"
    ),
    "dodatkowy prognozowany deficyt energii następnego dnia i korektę": (
        "the additional forecast energy deficit for the next day, and the"
    ),
    "prognozowany deficyt energii i korektę bezpieczeństwa użytkownika.": (
        "the forecast energy deficit, and the user's safety correction."
    ),
    "rezerwę awaryjną Self-Use, prognozowany deficyt energii i korektę": (
        "the Self-Use outage reserve, forecast energy deficit, and the user's"
    ),
    "bezpieczeństwa użytkownika.": "safety correction.",
    "Rozładowuje tylko powyżej progu i dynamicznej rezerwy SOC. Rezerwa": (
        "Discharges only above the price threshold and dynamic SOC reserve. The reserve"
    ),
    "obejmuje awaryjny SOC Self-Use, deficyt prognozy Solcast względem średniego": (
        "includes the Self-Use outage SOC, the Solcast forecast deficit against average"
    ),
    "obejmuje awaryjny SOC Self-Use, pozostałe zużycie domu w chronionym oknie": (
        "includes the Self-Use outage SOC, remaining home demand in the protected"
    ),
    "nocnym, dodatkowy deficyt prognozy Solcast na następny dzień oraz korektę": (
        "night window, the additional Solcast forecast deficit for the next day,"
        " and the safety"
    ),
    "bezpieczeństwa. Ręczne timery mają pierwszeństwo.": (
        "correction. Manual timers take priority."
    ),
    "nocnym, deficyt prognozy Solcast oraz korektę bezpieczeństwa. Ręczne": (
        "night window, the Solcast forecast deficit, and the safety correction. Manual"
    ),
    "timery mają pierwszeństwo.": "timers take priority.",
    "LOAD oraz korektę bezpieczeństwa. Ręczne timery mają pierwszeństwo.": (
        "LOAD, and the safety correction. Manual timers take priority."
    ),
    "Licznik dzienny LOAD zwiększa się w ciągu dnia i zeruje po północy.": (
        "The daily LOAD meter increases during the day and resets at midnight."
    ),
    "sum_differences_nonnegative sumuje wyłącznie dodatnie przyrosty z ostatnich": (
        "sum_differences_nonnegative sums only positive increments from the last"
    ),
    "96 godzin, uwzględniając reset licznika. Recorder odtwarza bufor po restarcie.": (
        "96 hours, accounting for the reset. Recorder restores the buffer after restart."
    ),
    "'brak'": "'none'",
    "EMS — pozostały czas rozładowania": "EMS — discharge time remaining",
    "EMS — pozostały czas ładowania": "EMS — charge time remaining",
    "Przełącza EMS na rozładowanie poza godzinami blokady sprzedaży i uruchamia timer.": (
        "Switches EMS to grid discharge outside the export lockout and starts the timer."
    ),
    "Przełącza EMS na ładowanie i uruchamia timer z czasu ustawionego na dashboardzie.": (
        "Switches EMS to grid charge and starts the timer using the dashboard duration."
    ),
    "Kończy oba timery i bezpiecznie przełącza falownik na autokonsumpcję.": (
        "Stops both timers and safely returns the inverter to Self-Use."
    ),
    "EMS — wymuś blokadę sprzedaży": "EMS — enforce export lockout",
    "W godzinach blokady zatrzymuje każde rozładowanie do sieci i przełącza": (
        "During the lockout, stops every grid-discharge cycle and switches"
    ),
    "falownik na Autokonsumpcję (Self-Use). Ładowanie z sieci pozostaje dozwolone.": (
        "the inverter to Self-Use. Grid charging remains allowed."
    ),
    "Co minutę porównuje bieżący półgodzinny blok RCE z progiem użytkownika.": (
        "Every minute, compares the current 30-minute RCE block with the user threshold."
    ),
    "Co minutę sprawdza dwudniowy plan optymalizatora. Rozładowuje tylko w": (
        "Every minute, checks the optimizer's two-day plan. It discharges only in"
    ),
    "wybranych najdroższych blokach, powyżej progu użytkownika i ponad SOC": (
        "selected highest-value slots, above the user threshold and above the SOC"
    ),
    "potrzebnym do zasilenia domu. Ręczne timery mają pierwszeństwo.": (
        "required to supply the home. Manual timers take priority."
    ),
    "Rozładowuje tylko powyżej progu i powyżej minimalnego SOC. Ręczne timery": (
        "Discharges only above the threshold and minimum SOC. Manual charge and"
    ),
    "ładowania i rozładowania mają pierwszeństwo.": (
        "discharge timers take priority."
    ),
    "Wznawia aktywny cykl albo wraca do Self-Use, jeśli timer upłynął podczas wyłączenia HA.": (
        "Restores an active cycle or returns to Self-Use if its timer expired while HA was offline."
    ),
    "Brak okresów powyżej progu poza blokadą sprzedaży.": (
        "No periods above the threshold outside the export lockout."
    ),
    "bloków po 30 min": "30-minute blocks",
    # RCEm 253 V+ voltage-aware export protection.
    "RCEm 253 V+ — automatyka włączona": "RCEm 253 V+ — automation enabled",
    "RCEm 253 V+ — pokaż dane zaawansowane": (
        "RCEm 253 V+ — show advanced data"
    ),
    "RCEm 253 V+ — tylko obserwacja": "RCEm 253 V+ — observation only",
    "RCEm 253 V+ — trwa aktywna regulacja": "RCEm 253 V+ — active control in progress",
    "RCEm 253 V+ — korekta bezpieczeństwa SOC": "RCEm 253 V+ — SOC safety correction",
    "RCEm 253 V+ — sprawność ładowania magazynu": "RCEm 253 V+ — battery charging efficiency",
    "RCEm 253 V+ — zapamiętany globalny limit ładowania": "RCEm 253 V+ — saved global charging limit",
    "EMS — blokada równoczesnej automatyki RCE, taryfowej i RCEm": "EMS — interlock for RCE, tariff and RCEm automation",
    "RCEm 253 V+ — płynna regulacja ładowania baterii z PV": "RCEm 253 V+ — variable PV-to-battery charging control",
    "Wyłączona — plan działa poglądowo": "Disabled — the plan remains available for preview",
    "Obserwacja — bez zapisu do falownika": "Observation — no inverter writes",
    "Zablokowana — trwa wyrównywanie magazynu": "Blocked — battery balancing is in progress",
    "Aktywna —": "Active —",
    "Automatyka RCEm 253 V+ — ustawienia": "RCEm 253 V+ automation — settings",
    "Włącz automatykę ochrony eksportu": "Enable voltage-aware export protection",
    "Tylko obserwacja — bez zapisu do falownika": "Observation only — no inverter writes",
    "Stan planera napięcia": "Voltage planner status",
    "Napięcie sieci — sterowanie według najwyższej fazy": "Grid voltage — controlled by the highest phase",
    "Maksimum": "Maximum",
    "Średnia 10 min": "10-minute average",
    "Napięcie L1/L2/L3 i średnia 10-minutowa — ostatnie 24 godziny": (
        "L1/L2/L3 voltage and 10-minute average — last 24 hours"
    ),
    "Temperatura ogniw — ostatnie 7 dni": "Cell temperature — last 7 days",
    "Napięcie sieci — ostatnie 7 dni": "Grid voltage — last 7 days",
    "Temperatury falownika — ostatnie 7 dni": (
        "Inverter temperatures — last 7 days"
    ),
    "Radiator falownika": "Inverter heatsink",
    "Radiator toru baterii": "Battery-stage heatsink",
    "Wnętrze falownika (CAV)": "Inverter interior (CAV)",
    "Plan ochrony eksportu": "Export protection plan",
    "Regulator — wartości bieżące": "Controller — current values",
    "Historyczne okno ryzyka aktywne": "Historical risk window active",
    "Ryzyko napięciowe": "Voltage risk",
    "Wymagane wolne miejsce w magazynie": "Required free battery capacity",
    "Zalecany globalny limit ładowania": "Recommended global charging limit",
    "Rzeczywisty globalny limit ładowania baterii": "Actual global battery charging limit",
    "Maksymalny prąd ładowania BMS": "Maximum BMS charging current",
    "Zasady bezpieczeństwa": "Safety rules",
    "Tryb obserwacji jest domyślny i nie zapisuje niczego do falownika.": "Observation mode is the default and does not write anything to the inverter.",
    "Regulator może zmieniać wyłącznie globalny limit ładowania baterii": "The controller may change only the global battery charging limit",
    "Nie zmienia limitu eksportu GCF, asymetrii trójfazowej, Q(U), P(U), cos φ ani progów ochrony.": "It never changes the GCF export limit, three-phase unbalance, Q(U), P(U), power factor or protection thresholds.",
    "RCEm, RCE i ładowanie taryfowe są wzajemnie wykluczane.": "RCEm, RCE and tariff charging are mutually exclusive.",
    "Wyrównywanie magazynu i ręczne cykle EMS mają pierwszeństwo.": "Battery balancing and manual EMS cycles have priority.",
    "Instalacja zero-export nadaje się do sprawdzenia obliczeń, historii i blokad, ale nie do potwierdzenia reakcji napięcia na eksport.": "A zero-export installation can verify calculations, history and interlocks, but cannot validate the voltage response to export.",
    "obserwacja — brak zapisów do falownika": "observation — no inverter writes",
    "aktywna regulacja globalnej mocy ładowania baterii": "active control of the global battery charging power",
    "Bieżące działanie": "Current action",
    "**Tryb:**": "**Mode:**",
    "Najwyższe napięcie": "Highest voltage",
    "Historia": "History",
    "próbek": "samples",
    " dni ·": " days ·",
    "Wolne miejsce potrzebne na szczyt": "Battery headroom required for the peak",
    "Dostępne wolne miejsce": "Available battery headroom",
    "Docelowy maksymalny SOC przed szczytem": "Target maximum SOC before the peak",
    "Szacowana bezpieczna moc eksportu": "Estimated safe export power",
    "Powtarzalne okna ryzyka z ostatnich czterech dni": "Repeated risk windows from the previous four days",
    "Nie wykryto jeszcze powtarzalnego okna wysokiego napięcia.": "No repeated high-voltage window has been detected yet.",
    "Automatyka pozostaje w **Self-Use** i steruje wyłącznie globalnym": "The automation remains in **Self-Use** and controls only the global",
    "parametrem **Battery Max Charge Power**. Nie używa ładowania z sieci.": "**Battery Max Charge Power** parameter. It does not use Grid Charge.",
    "Nie zmienia GCF, maksymalnego limitu eksportu, asymetrii trójfazowej,": "It does not change GCF, maximum export limit, three-phase unbalance,",
    "Q(U), P(U), współczynnika mocy ani progów zabezpieczeń. Najwyższa z": "Q(U), P(U), power factor or protection thresholds. The highest",
    "faz L1/L2/L3 zawsze ma pierwszeństwo.": "L1/L2/L3 phase always has priority.",
    "RCE, ładowanie taryfowe i RCEm 253 V+ wzajemnie się wykluczają.": "RCE, tariff charging and RCEm 253 V+ are mutually exclusive.",
    "Wyrównywanie magazynu oraz ręczne plany EMS mają pierwszeństwo.": "Battery balancing and manual EMS plans have priority.",
    "Podczas pierwszych testów pozostaw przełącznik **Tylko obserwacja**": "During initial testing, leave **Observation only** enabled.",
    "włączony. Wyłączenie go pozwala regulatorowi zapisywać globalny limit": "Disabling it allows the controller to write the global charging limit",
    "ładowania z krokiem najwyżej 10 punktów procentowych na minutę.": "in steps of no more than 10 percentage points per minute.",
    "Wyjątkiem jest przekroczenie 253 V — wtedy od razu używany jest pełny": "The exception is a voltage above 253 V, which immediately applies the full",
    "bezpieczny limit wynikający z BMS.": "BMS-safe limit.",
    "Pozwala działać tylko jednemu automatowi. RCE, ładowanie taryfowe i RCEm": "Allows only one automation to run. RCE, tariff charging and RCEm",
    "253 V+ wzajemnie się wykluczają. Po restarcie pierwszeństwo ma RCE,": "253 V+ are mutually exclusive. After restart, RCE has priority,",
    "następnie automat taryfowy, a na końcu RCEm.": "followed by tariff charging and then RCEm.",
    "Co minutę stosuje rekomendację planera napięcia do globalnego limitu": "Applies the voltage planner recommendation to the global limit every minute",
    "Battery Max Charge Power. Tryb obserwacyjny nigdy nie zapisuje falownika.": "Battery Max Charge Power. Observation mode never writes to the inverter.",
    "Automatyka nie używa Grid Charge, GCF, asymetrii ani nastaw zabezpieczeń.": "The automation does not use Grid Charge, GCF, unbalance or protection settings.",
    "RCEm 253 V+ — płynna regulacja limitu eksportu": "RCEm 253 V+ — variable export-limit control",
    "RCEm 253 V+ — trwa regulacja limitu eksportu": "RCEm 253 V+ — export-limit control active",
    "RCEm 253 V+ — poranne rozładowanie przygotowujące miejsce": "RCEm 253 V+ — morning headroom-preparation discharge",
    "RCEm 253 V+ — trwa poranne przygotowanie miejsca": "RCEm 253 V+ — morning headroom preparation active",
    "RCEm 253 V+ — poranne przygotowanie miejsca w magazynie": "RCEm 253 V+ — morning battery-headroom preparation",
    "RCEm 253 V+ — zapamiętana maksymalna moc rozładowania": "RCEm 253 V+ — saved maximum discharge power",
    "RCEm 253 V+ — zapamiętany próg rozładowania": "RCEm 253 V+ — saved discharge threshold",
    "RCEm 253 V+ — maksymalny dozwolony eksport": "RCEm 253 V+ — maximum permitted export",
    "RCEm 253 V+ — zapamiętany limit eksportu": "RCEm 253 V+ — saved export limit",
    "RCEm 253 V+ — regulacja magazynu i bezpiecznego eksportu": "RCEm 253 V+ — battery and safe-export control",
    "Płynna regulacja maksymalnego eksportu": "Variable maximum-export control",
    "Poranne rozładowanie — przygotowanie miejsca na PV": "Morning discharge — prepare battery headroom for PV",
    "Maksymalny eksport zgodny ze zgłoszeniem": "Maximum export permitted by the grid agreement",
    "Prognoza użyta do następnego okna": "Forecast used for the next window",
    "Zapotrzebowanie domu w oknie ryzyka": "Home demand during the risk window",
    "Przewidywana nadwyżka PV w oknie ryzyka": "Expected PV surplus during the risk window",
    "Chroniony minimalny SOC domu i rezerwy": "Protected minimum SOC for the home and outage reserve",
    "Brakujące wolne miejsce": "Missing battery headroom",
    "Efektywny twardy limit eksportu": "Effective hard export cap",
    "Zalecany bieżący limit eksportu": "Recommended current export limit",
    "Brakujące wolne miejsce w magazynie": "Missing free battery capacity",
    "Przewidywane naturalne rozładowanie na dom przed szczytem": "Expected natural discharge to the home before the peak",
    "Zaplanowane poranne rozładowanie do sieci": "Planned morning discharge to the grid",
    "Docelowy SOC po porannym rozładowaniu": "Target SOC after morning discharge",
    "Automatycznie dobrana moc rozładowania": "Automatically selected discharge power",
    "Czas do dzisiejszego okna ryzyka": "Time until today's risk window",
    "brak dzisiejszego okna": "no risk window today",
    "'today': 'dzisiaj', 'tomorrow': 'jutro', 'none': 'brak',": "'today': 'today', 'tomorrow': 'tomorrow', 'none': 'none',",
    "'missing': 'brak danych'": "'missing': 'data unavailable'",
    "**Poranne rozładowanie:** nie jest potrzebne lub brak dzisiejszego": "**Morning discharge:** is not required or there is no",
    "okna ryzyka.": "risk window today.",
    "Naturalne miejsce zasilając dom przed szczytem": "Natural headroom from supplying the home before the peak",
    "Docelowy SOC porannego rozładowania": "Morning-discharge target SOC",
    "Zalecany limit eksportu": "Recommended export limit",
    "Rzeczywisty maksymalny limit eksportu": "Actual maximum export limit",
    "W czasie normalnej regulacji automatyka pozostaje w **Self-Use**. Łączy": "During normal regulation the automation remains in **Self-Use**. It combines",
    "prognozę Solcast na dziś i": "the Solcast forecast for today and",
    "jutro, rzeczywisty profil LOAD z czterech dni, chronione potrzeby domu,": "tomorrow, the actual four-day LOAD profile, protected home demand,",
    "SOC, pojemność magazynu i historyczne napięcia L1/L2/L3. Od prognozy": "SOC, battery capacity and historical L1/L2/L3 voltages. It subtracts",
    "PV odejmuje zużycie domu, a dopiero pozostałą nadwyżkę traktuje jako": "home consumption from the PV forecast and only treats the remaining surplus as",
    "energię wymagającą miejsca w magazynie.": "energy requiring battery headroom.",
    "Najpierw odejmuje miejsce, które powstanie naturalnie, gdy dom będzie": "It first subtracts the headroom created naturally while the home is",
    "zasilany z baterii przed szczytem. Jeżeli nadal brakuje pojemności,": "supplied from the battery before the peak. If capacity is still missing,",
    "opcja **Poranne rozładowanie** może wcześniej przełączyć EMS na Grid": "**Morning discharge** may switch EMS to Grid Discharge beforehand",
    "Discharge i sprzedać wyłącznie obliczony brak. Moc dobierana jest z": "and sell only the calculated shortfall. Power is selected from",
    "ilości kWh oraz czasu pozostałego do okna, z 30-minutowym zapasem.": "the missing kWh and time remaining until the window, with a 30-minute buffer.",
    "Cykl kończy się po osiągnięciu docelowego SOC albo wcześniej, gdy": "The cycle ends at the target SOC or earlier if",
    "najwyższa faza wzrośnie do 248,4 V lub średnia 10-minutowa do 249,2 V.": "the highest phase reaches 248.4 V or the 10-minute average reaches 249.2 V.",
    "Nigdy nie schodzi poniżej chronionego SOC domu i rezerwy.": "It never discharges below the protected home-and-outage-reserve SOC.",
    "Funkcja porannego rozładowania wymaga aktywnego GCF, rzeczywistego": "Morning discharge requires active GCF, an actual",
    "limitu eksportu większego od 0%, braku blokady sprzedaży i wyłączonego": "export limit above 0%, no export lockout and disabled",
    "trybu obserwacyjnego. Nie zmienia GCF ani **Maximum Export Power Limit**;": "observation mode. It changes neither GCF nor **Maximum Export Power Limit**;",
    "korzysta z wartości zastanej i limitu zgodnego ze zgłoszeniem. Po": "it uses the existing value and grid-agreement cap. After",
    "zakończeniu odtwarza wcześniejszą maksymalną moc rozładowania, próg SOC": "completion it restores the previous maximum discharge power, SOC threshold",
    "oraz tryb Self-Use.": "and Self-Use mode.",
    "Przed przewidywanym szczytem RCEm nadal ogranicza **Battery Max Charge": "Before the predicted peak RCEm still limits **Battery Max Charge",
    "Power**, aby nie napełnić magazynu za wcześnie. Jeżeli poranne": "Power** to avoid filling the battery too early. If morning",
    "rozładowanie pozostaje wyłączone lub eksport ma limit 0%, pole": "discharge remains disabled or export is capped at 0%, the",
    "**Zaplanowane poranne rozładowanie** działa wyłącznie poglądowo.": "**Planned morning discharge** field is informational only.",
    "Przed dzisiejszym oknem podwyższonego napięcia rozładowuje do sieci tylko": "Before today's high-voltage window, it discharges to the grid only",
    "energię, której nie zużyje wcześniej dom i która jest potrzebna jako": "the energy that the home will not consume beforehand and that is required as",
    "miejsce na prognozowaną nadwyżkę PV. Moc dobierana jest do ilości energii": "headroom for the forecast PV surplus. Power is selected from the energy amount",
    "i dostępnego czasu. Automat nie schodzi poniżej chronionego SOC, respektuje": "and available time. It never goes below protected SOC and respects",
    "blokadę sprzedaży, ograniczenie BMS oraz zastany limit eksportu. Nie zmienia": "the export lockout, BMS limit and existing export cap. It changes neither",
    "GCF ani Maximum Export Power Limit.": "GCF nor Maximum Export Power Limit.",
    "Przed przewidywanym szczytem ogranicza **Battery Max Charge Power**,": "Before the predicted peak it limits **Battery Max Charge Power**",
    "aby nie napełnić magazynu za wcześnie. Dom nadal pracuje w Self-Use i": "to avoid filling the battery too early. The home remains in Self-Use and",
    "naturalnie zużywa energię z baterii. RCEm nie wymusza rozładowania do": "naturally consumes battery energy. RCEm does not force discharge to",
    "sieci; jeśli naturalne zużycie nie wystarczy, pole **Brakujące wolne": "the grid; if natural consumption is insufficient, **Missing battery",
    "miejsce** pokaże ryzyko ograniczenia produkcji.": "headroom** reports a possible production-curtailment risk.",
    "Opcjonalna regulacja eksportu zmienia wyłącznie **Maximum Export Power": "Optional export control changes only **Maximum Export Power",
    "Limit**. Nigdy nie przekroczy mniejszej z dwóch wartości: limitu": "Limit**. It never exceeds the lower of two values: the limit",
    "zastanego przy uruchomieniu oraz pola **Maksymalny eksport zgodny ze": "found at activation and **Maximum export permitted by the",
    "zgłoszeniem**. Przykład: falownik 20 kW i limit 50% oznacza maksymalnie": "grid agreement**. Example: a 20 kW inverter with a 50% cap can export at most",
    "10 kW eksportu. RCEm nie włącza GCF — użytkownik robi to świadomie w": "10 kW. RCEm does not enable GCF; the user enables it deliberately in",
    "ustawieniach falownika.": "the inverter settings.",
    "Nie zmienia asymetrii trójfazowej, Q(U), P(U), współczynnika mocy ani": "It does not change three-phase unbalance, Q(U), P(U), power factor or",
    "progów zabezpieczeń. Najwyższa z faz L1/L2/L3 zawsze ma pierwszeństwo.": "protection thresholds. The highest L1/L2/L3 phase always has priority.",
    "włączony. Wyłączenie go pozwala regulatorowi zmieniać limit ładowania": "enabled. Disabling it allows the controller to change the charging limit",
    "najwyżej o 10 punktów procentowych na minutę, a limit eksportu o 5–15": "by at most 10 percentage points per minute and the export limit by 5–15",
    "punktów zależnie od napięcia. Przy 253 V eksport może zostać natychmiast": "points depending on voltage. At 253 V export may be immediately",
    "ograniczony do 0%, a magazyn użyje pełnego bezpiecznego limitu BMS.": "reduced to 0%, while the battery uses the full BMS-safe limit.",
    "Battery Max Charge Power oraz — po osobnym włączeniu — do maksymalnego": "Battery Max Charge Power and, when separately enabled, the maximum",
    "limitu eksportu. Tryb obserwacyjny nigdy nie zapisuje falownika. RCEm nie": "export limit. Observation mode never writes to the inverter. RCEm does not",
    "przełącza GCF, Grid Charge, asymetrii ani nastaw zabezpieczeń.": "switch GCF, Grid Charge, unbalance or protection settings.",
    "Steruje teraz": "Current controller",
    "Konflikt sterowania": "Control conflict",
    "EMS RCE — pokaż dane zaawansowane": "EMS RCE — show advanced data",
    "Pokaż dane zaawansowane": "Show advanced data",
    "Ukryj dane zaawansowane": "Hide advanced data",
    "Dane zaawansowane (tryb ekspercki)": "Advanced data (expert mode)",
    "Dlaczego taki plan?": "Why this plan?",
    "Automat liczy decyzję z **ostrożnych wartości użytych przez model**,": (
        "The automation bases its decision on **conservative values used by the model**,"
    ),
    "a nie z surowej prognozy wyświetlanej przez Solcast.": (
        "not on the raw forecast displayed by Solcast."
    ),
    "Pozostałe PV dzisiaj": "PV remaining today",
    "PV jutro": "PV tomorrow",
    "Bezpieczne dobowe zużycie domu": "Conservative daily home consumption",
    "Rezerwa baterii": "Battery reserve",
    "Przewidywany SOC na końcu planu": "Forecast SOC at the end of the plan",
    "Horyzont jest tymczasowo skrócony, ponieważ prognoza trzeciego dnia": (
        "The horizon is temporarily limited because the day-three forecast"
    ),
    "jest niedostępna lub nieaktualna. Plan zachowuje dodatkowy margines.": (
        "is unavailable or stale. The plan retains an additional safety margin."
    ),
    "model skalibrowany": "model calibrated",
    "zbieranie próbek — używany limit ustawiony": (
        "collecting samples — configured limit is used"
    ),
    "RCEm — gotowość i diagnostyka sterowania": (
        "RCEm — control readiness and diagnostics"
    ),
    "RCEm — decyzja i plan ochrony eksportu": (
        "RCEm — decision and export-protection plan"
    ),
    "Najbliższe przewidywane okno ryzyka": "Next predicted risk window",
    "brak produkcji lub eksportu": "no production or export",
    "P90 napięcia dla bieżącego kwadransa": (
        "Voltage P90 for the current quarter-hour"
    ),
    "Źródło profilu PV": "PV profile source",
    "profil zastępczy": "fallback profile",
    "pewność": "confidence",
    "Profil odbiorników": "Load profile",
    "Nieuniknione ładowanie do chwili szczytu": (
        "Unavoidable charging before the risk peak"
    ),
    "nadwyżka": "surplus",
    "wymagane miejsce": "required headroom",
    "Dodatkowa rezerwa po horyzoncie (deficyt dnia 3)": (
        "Additional reserve after the horizon (day-three deficit)"
    ),
    "brak danych o przyczynie": "reason unavailable",
    "Dane sterujące taryfy są nieświeże:": "Tariff control data is stale:",
    "brak danych": "data unavailable",
    "Dom zasilany z taniej sieci — bateria zachowana": (
        "Home powered from the low-cost grid — battery preserved"
    ),
    "Moc czynna L1": "Active power L1",
    "Moc czynna L2": "Active power L2",
    "Moc czynna L3": "Active power L3",
    "Moc bierna L1": "Reactive power L1",
    "Moc bierna L2": "Reactive power L2",
    "Moc bierna L3": "Reactive power L3",
    "RCE — konfiguracja zaawansowana": "RCE — advanced configuration",
    "Własna encja Solcast — dzisiaj": "Custom Solcast entity — today",
    "Własna encja Solcast — jutro": "Custom Solcast entity — tomorrow",
    "Własna encja Solcast — dzień 3": "Custom Solcast entity — Day 3",
    "**Day 3 — encja:**": "**Day 3 — entity:**",
    "'brak wykrytej encji'": "'no detected entity'",
    "konfiguracja:": "configuration:",
    "'automatyczna'": "'automatic'",
    "**Day 3 — status / świeża:**": "**Day 3 — status / fresh:**",
    "'brak statusu'": "'no status'",
    "{% if day3_fresh is none %}brak danych{% elif day3_fresh %}tak{% else %}nie{% endif %}": (
        "{% if day3_fresh is none %}data unavailable{% elif day3_fresh %}yes{% else %}no{% endif %}"
    ),
    "**Day 3 — wiek ze znakiem:**": "**Day 3 — signed age:**",
    "'brak znacznika czasu'": "'no timestamp'",
    " · powód:": " · reason:",
    "'brak informacji'": "'no information'",
    "RCE — decyzja automatyki": "RCE — automation decision",
    "Aktywne RCE utraciło autoryzację przed zapisem limitu mocy": (
        "Active RCE lost authorization before writing the power limit"
    ),
    "Aktywne RCE utraciło autoryzację przed wydłużeniem okna": (
        "Active RCE lost authorization before extending the execution window"
    ),
    "Aktywne RCE utraciło autoryzację przed zapisem progu SOC": (
        "Active RCE lost authorization before writing the SOC threshold"
    ),
    "RCE przelicza plan; aktywny zatwierdzony cykl pozostaje bez zmian": (
        "RCE is recalculating; the accepted active cycle remains unchanged"
    ),
    "Decyzja optymalizatora": "Optimizer decision",
    "Cena RCE teraz": "Current RCE price",
    "Cena graniczna planu": "Plan price floor",
    "Chroniony minimalny SOC": "Protected minimum SOC",
    "Energia dostępna ponad rezerwę": "Energy available above reserve",
    "Planowany eksport z baterii": "Planned battery export",
    "SOC na końcu horyzontu": "SOC at the end of the horizon",
    "Korzyść optymalizacji": "Optimization benefit",
    "Korzyść optymalizacji netto": "Net optimization benefit",
    "Wzrost przychodu brutto przed kosztem baterii": (
        "Gross revenue increase before battery wear cost"
    ),
    "Przychód zrealizowany dzisiaj": "Revenue realized today",
    "RCE — szczegóły i diagnostyka": "RCE — details and diagnostics",
    "Tanie ładowanie — ustawienia": "Low-cost charging — settings",
    "Co zrobi automat": "What the automation will do",
    "Energia w zaplanowanych blokach Grid Charge": (
        "Energy in scheduled Grid Charge slots"
    ),
    "Podsumowanie planu": "Plan summary",
    "Automatyka wymaga uwagi.": "The automation requires attention.",
    "Nie można odbudować bazowej rezerwy Self-Use.": (
        "The base Self-Use reserve cannot be restored."
    ),
    "Sprzęt albo": "The hardware or",
    "aktywny limit ładowania nie pozwala wykonać wymaganej operacji Grid Charge.": (
        "an active charging limit prevents the required Grid Charge operation."
    ),
    "Automat pozostanie w bezpiecznym trybie. Szczegóły są dostępne w": (
        "The automation will remain in a safe mode. Details are available in"
    ),
    "danych zaawansowanych.": "advanced data.",
    "Plan jest wykonalny tylko częściowo.": "The plan is only partially feasible.",
    "Automat wykorzysta dostępne": "The automation will use the available",
    "tanie okna, ale nie zdoła pokryć całego przewidywanego zapotrzebowania.": (
        "low-cost periods but cannot cover all forecast demand."
    ),
    "Po uwzględnieniu mocy i pojemności magazynu": (
        "After accounting for battery power and capacity,"
    ),
    "pozostanie **{{ expensive | round(2) }} kWh** poboru w droższej strefie.": (
        "there will remain **{{ expensive | round(2) }} kWh** of import in a "
        "higher-cost period."
    ),
    "Do wymaganej rezerwy na końcu horyzontu": (
        "The required reserve at the end of the horizon"
    ),
    "zabraknie jeszcze": "will be short by",
    "Plan jest prawidłowy.": "The plan is correct.",
    "Brakująca energia przypada wyłącznie w taniej": (
        "The remaining energy demand occurs only in the low-cost"
    ),
    "strefie, dlatego dom pobierze ją bezpośrednio z sieci bez strat magazynu.": (
        "period, so the home will import it directly without battery conversion losses."
    ),
    "Nie trzeba doładowywać magazynu.": "The battery does not need charging.",
    "Prognozowane PV i energia baterii": "Forecast PV and stored battery energy",
    "wystarczą do następnego taniego okna z zachowaniem rezerwy.": (
        "will last until the next low-cost period while preserving the reserve."
    ),
    "Taryfa G11 nie ma tańszego okna.": "The G11 tariff has no lower-cost period.",
    "Automat pozostaje w Self-Use,": "The automation remains in Self-Use",
    "ponieważ przesuwanie energii przez baterię nie obniży kosztu.": (
        "because shifting energy through the battery would not reduce cost."
    ),
    "Przed przewidywanym deficytem nie ma taniego okna.": (
        "There is no low-cost period before the forecast shortage."
    ),
    "Automat nie uruchomi Grid Charge w drogiej strefie.": (
        "The automation will not start Grid Charge in a high-cost period."
    ),
    "Ładowanie celowo pominięte.": "Charging intentionally skipped.",
    "Tania strefa jest dostępna, ale": (
        "A low-cost period is available, but"
    ),
    "różnica cen nie pokrywa strat konwersji, kosztu zużycia baterii i": (
        "the price spread does not cover conversion losses, battery wear, and"
    ),
    "wymaganego marginesu oszczędności. Automat pozostaje w Self-Use": (
        "the required savings margin. The automation remains in Self-Use"
    ),
    "zamiast wykonywać nieopłacalny cykl.": (
        "instead of running an uneconomical cycle."
    ),
    "Plan gotowy.": "Plan ready.",
    "Automat pobierze z sieci tylko wyliczoną energię": (
        "The automation will import only the calculated energy"
    ),
    "w wybranych tanich blokach i zachowa wymaganą rezerwę baterii.": (
        "in selected low-cost slots and preserve the required battery reserve."
    ),
    "Brak potrzeby uruchamiania Grid Charge.": "Grid Charge is not needed.",
    "Aktualny bilans nie wymaga": "The current balance does not require",
    "dodatkowego doładowania magazynu.": "additional battery charging.",
    "to przewidywany bezpośredni pobór": "is the forecast direct import",
    "w taniej strefie. Automat wybiera go celowo, aby magazyn nie ponosił": (
        "in the low-cost period. The automation selects it intentionally to avoid"
    ),
    "strat konwersji.": "battery conversion losses.",
    "Tanie ładowanie — diagnostyka": "Low-cost charging — diagnostics",
    "Jak działa tanie ładowanie": "How low-cost charging works",
    "Jak działa automatyka taryfowa — szczegóły": (
        "How tariff automation works — details"
    ),
    "Brakuje danych do pełnego planu.": "Some data required for the complete plan is missing.",
    "Otwórz dane zaawansowane,": "Open advanced data",
    "aby zobaczyć szczegóły diagnostyczne.": "to view diagnostic details.",
    "Automat przewiduje zużycie domu i produkcję PV, a następnie sprawdza,": (
        "The automation forecasts home consumption and PV production, then checks"
    ),
    "czy energii wystarczy do kolejnej taniej strefy. Jeżeli zabraknie,": (
        "whether energy will last until the next low-cost period. If not,"
    ),
    "wybiera najtańsze dostępne bloki i ładuje tylko tyle, ile rzeczywiście": (
        "it selects the cheapest available slots and charges only as much as"
    ),
    "potrzeba. Chroni ustawioną rezerwę SOC, uwzględnia straty oraz nie": (
        "is actually needed. It protects the configured SOC reserve, accounts for losses,"
    ),
    "uruchamia się równocześnie z RCE ani inną automatyką EMS.": (
        "and never runs together with RCE or another EMS automation."
    ),
    "Pojemność ustawiona w falowniku": "Capacity configured in the inverter",
    "Pojemność efektywna (awaryjne źródło BMS)": (
        "Effective capacity (BMS emergency fallback)"
    ),
    "Cennik profilu wygasł. Automatyczne ładowanie jest zablokowane.": (
        "The tariff profile has expired. Automatic charging is blocked."
    ),
    "Zaktualizuj integrację albo wybierz profil **Manual** z aktualnymi cenami.": (
        "Update the integration or select **Manual** with current prices."
    ),
    "Harmonogram ręczny — rozładowanie": "Manual schedule — discharge",
    "Harmonogram ręczny — ładowanie": "Manual schedule — charge",
    "Automatyka RCE": "RCE automation",
    "Tanie ładowanie": "Low-cost charging",
    "RCEm 253 V+ — obserwacja": "RCEm 253 V+ — observation",
    "Brak aktywnej automatyki": "No active automation",
    "Balansowanie": "Balancing",
    "Wyłączone — kończenie aktywnego bloku": (
        "Disabled — finishing the active slot"
    ),
    "Aktywne — dom zasilany z taniej sieci": (
        "Active — home supplied from the low-cost grid"
    ),
    "Aktywne — ładowanie z sieci": "Active — grid charging",
    "Aktywne — sterowanie taryfowe": "Active — tariff control",
    "Niedostępne — trwa inicjalizacja": "Unavailable — initializing",
    "Włączone — zablokowane: włączona polityka RCE": (
        "Enabled — blocked: RCE policy is enabled"
    ),
    "Włączone — zablokowane: plan niedostępny": (
        "Enabled — blocked: plan unavailable"
    ),
    "Włączone — oczekuje na aktualny plan": (
        "Enabled — waiting for a current plan"
    ),
    "Włączone — brak potrzeby ładowania": (
        "Enabled — no charging needed"
    ),
    "Włączone — wybrany blok oczekuje na rozpoczęcie": (
        "Enabled — selected slot waiting to start"
    ),
    "Włączone — oczekuje na wybrany blok": (
        "Enabled — waiting for a selected slot"
    ),
    "Włączone — zablokowane:": "Enabled — blocked:",
    "Włączone — {{ plan_state }}": "Enabled — {{ plan_state }}",
    "Sterowanie ręczne": "Manual control",
    "Dane i sterowanie gotowe": "Data and control ready",
    "Eksport fizycznie dozwolony": "Physical export permitted",
    "Falownik gotowy do zapisu EMS": "Inverter ready for EMS writes",
    "Adaptacyjne dobowe zużycie odbiorników": "Adaptive daily load consumption",
    "Adaptacyjne zużycie w oknie nocnym": "Adaptive protected-night consumption",
    "Pokrycie historii zużycia": "Load history coverage",
    "Pokrycie historii nocnej": "Night-load history coverage",
    "Jeżeli Solcast udostępnia **Forecast Day 3**, integracja wykorzysta ją": (
        "If Solcast provides **Forecast Day 3**, the integration uses it"
    ),
    "automatycznie do wyceny energii, którą warto zachować po końcu": (
        "automatically to value energy worth retaining after the end of the"
    ),
    "48-godzinnego rynku. Brak tej encji nie blokuje pracy. Encje są": (
        "48-hour market horizon. Its absence does not block operation. Entities are"
    ),
    "wykrywane automatycznie w polskiej i angielskiej wersji HA; w razie": (
        "detected automatically in Polish and English HA; for"
    ),
    "niestandardowych nazw można wpisać dzisiaj i jutro u góry.": (
        "custom names, the Today and Tomorrow entities can be entered above."
    ),
    "wybiera najwyższe ceny i uwzględnia naturalny eksport PV, koszt": (
        "selects the highest prices and includes natural PV export, battery"
    ),
    "zużycia baterii oraz wartość energii zachowanej na kolejny dzień.": (
        "wear cost, and the value of energy retained for the following day."
    ),
    "Wcześniejsze rozładowanie wykona tylko wtedy, gdy zwiększy wynik netto": (
        "An earlier discharge is used only when it increases the net result"
    ),
    "lub utworzy miejsce w baterii przed późniejszą nadwyżką sprzedawaną": (
        "or creates battery headroom before a later surplus would be sold"
    ),
    "taniej.": "more cheaply.",
    "zużycie dobowe z pola u góry. Następnie algorytm uczy się maksymalnie": (
        "daily consumption entered above. The algorithm then learns from up to"
    ),
    "z 28 pełnych dni: większą wagę nadaje dniom ostatnim, odrzuca": (
        "28 complete days, gives recent days more weight, rejects"
    ),
    "pojedyncze skoki oraz buduje osobne profile 48 półgodzinnych bloków": (
        "isolated spikes, and builds separate 48 half-hour profiles"
    ),
    "dla dni roboczych i weekendów. Ostatnie cztery dni pozostają szybkim": (
        "for weekdays and weekends. The latest four days remain a fast"
    ),
    "zabezpieczeniem po świeżej instalacji. Faktyczne pokrycie i wybrany": (
        "fallback after a fresh installation. Actual coverage and the selected"
    ),
    "model są pokazane w diagnostyce.": "model are shown in diagnostics.",
    "Rezerwa jest wyznaczana konserwatywnie z przedziałów **P10/P50/P90**": (
        "The reserve is conservatively derived from Solcast **P10/P50/P90**"
    ),
    "Solcast i z błędu prognozy zmierzonego w poprzednich dniach. W ciągu": (
        "bands and forecast error measured on previous days. During"
    ),
    "dnia korekta porównuje prognozę z produkcją rzeczywistą, ale nie": (
        "the day, correction compares forecast with actual production but does not"
    ),
    "obniża ochrony domu na podstawie krótkiej próbki po wschodzie.": (
        "reduce home protection based on a short post-sunrise sample."
    ),
    "Jakość danych:": "Data quality:",
    "uwagi:": "issues:",
    "brak oceny": "not assessed",
    "Prognoza dzisiaj P10 / P50 / P90:": "Today forecast P10 / P50 / P90:",
    "Prognoza jutro P10 / P50 / P90:": "Tomorrow forecast P10 / P50 / P90:",
    "Pewność prognozy / udział wariantu ostrożnego:": (
        "Forecast confidence / conservative-case share:"
    ),
    "Model odbiorników:": "Load model:",
    "dni historii": "history days",
    "tryb awaryjny": "fallback mode",
    "Fizyczna moc planu:": "Physical plan power:",
    "żądana": "requested",
    "żądany": "requested",
    "dostępna": "available",
    "źródło limitu:": "limit source:",
    "GCF:": "GCF:",
    "włączone": "enabled",
    "wyłączone": "disabled",
    "Koszt zużycia baterii w planie:": "Planned battery wear cost:",
    "korzyść netto:": "net benefit:",
    "Energia zachowana po horyzoncie:": "Energy retained after the horizon:",
    "wartość": "value",
    "Horyzont symulacji:": "Simulation horizon:",
    "dni, do": "days, until",
    "Prognoza dnia 3:": "Day 3 forecast:",
    "brak — bezpieczny zapas zastępczy": "unavailable — safe fallback reserve",
    "Budżet mocy Grid Charge:": "Grid Charge power budget:",
    "efektywny": "effective",
    "źródło": "source",
    "Uczenie rzeczywistej mocy:": "Actual power learning:",
    "próbek": "samples",
    "współczynnik": "factor",
    "Rezerwa końcowa z niepewnością:": "Terminal uncertainty reserve:",
    "margines": "margin",
    "Stabilność decyzji:": "Decision stability:",
    "Decyzja:": "Decision:",
    "Energia w wybranych blokach Grid Charge:": (
        "Energy in selected Grid Charge slots:"
    ),
    "Deficyt bez optymalizacji:": "Shortage without optimization:",
    "Pozostały bezpośredni pobór w taniej strefie:": (
        "Remaining direct import in the low-cost period:"
    ),
    "Pozostały pobór w strefie średniej lub drogiej:": (
        "Remaining import in a medium- or high-cost period:"
    ),
    "Niedobór wynikający z mocy lub pojemności:": (
        "Shortfall caused by power or capacity limits:"
    ),
    "Brak rezerwy na końcu horyzontu:": (
        "Missing reserve at the end of the horizon:"
    ),
    "Pozostały deficyt modelu — wartość techniczna:": (
        "Remaining model shortage — technical value:"
    ),
    "min bez istotnej zmiany": "min without a material change",
    "EMS RCE — koniec zapamiętanego bloku": "EMS RCE — latched slot end",
    "EMS RCE — zapamiętany minimalny SOC aktywnego bloku": (
        "EMS RCE — latched minimum SOC for the active slot"
    ),
    "RCEm 253 V+ — zapamiętany koniec przygotowania miejsca": (
        "RCEm 253 V+ — latched headroom-preparation deadline"
    ),
    "RCEm 253 V+ — zapamiętany cel SOC przygotowania miejsca": (
        "RCEm 253 V+ — latched headroom-preparation SOC target"
    ),
    "RCEm 253 V+ — zapamiętana moc przygotowania miejsca": (
        "RCEm 253 V+ — latched headroom-preparation power"
    ),
    "Gotowe - sterowanie bezpośrednie": "Ready - direct control",
    "Gotowe - Master steruje siecią równoległą": (
        "Ready - Master controls the parallel network"
    ),
    "Brak sygnału żywotności ESP32": "No ESP32 liveness signal",
    "ESP32 nie raportował sygnału żywotności od ponad 3 minut": (
        "ESP32 has not reported its liveness signal for more than 3 minutes"
    ),
    "Sieć równoległa nie potwierdziła gotowości": (
        "The parallel network has not confirmed readiness"
    ),
    "Sterowanie EMS lub sieć równoległa nie są gotowe": (
        "EMS control or the parallel network is not ready"
    ),
    "Cena RCE nie była raportowana od ponad 5 minut": (
        "RCE price has not been reported for more than 5 minutes"
    ),
    "Plan RCE i dane PSE są nieaktualne": "The RCE plan and PSE data are stale",
    "Brak potwierdzenia ustawień eksportu": "Export settings are not confirmed",
    "Plan taryfowy nie był raportowany od ponad 5 minut": (
        "The tariff plan has not been reported for more than 5 minutes"
    ),
    "Prognoza taryfowa nie była raportowana od ponad 5 minut": (
        "The tariff forecast has not been reported for more than 5 minutes"
    ),
    "Falownik nie potwierdził limitu ładowania": (
        "The inverter did not confirm the charging limit"
    ),
    "Falownik nie potwierdził celu SOC przed startem": (
        "The inverter did not confirm the target SOC before start"
    ),
    "Falownik nie potwierdził nowego limitu ładowania": (
        "The inverter did not confirm the new charging limit"
    ),
    "Falownik nie potwierdził celu SOC": (
        "The inverter did not confirm the target SOC"
    ),
    "EMS — bezpieczne wyjście po utracie danych lub potwierdzenia": (
        "EMS — safe exit after data or acknowledgement loss"
    ),
    "Aktywny cykl RCE albo taryfowy wraca do Self-Use po dwóch minutach": (
        "An active RCE or tariff cycle returns to Self-Use after two minutes"
    ),
    "nieprzerwanej utraty danych, gotowości Master/Slave lub prawa do eksportu.": (
        "of continuous loss of data, Master/Slave readiness, or export permission."
    ),
    "Przy niedostępnej encji EMS znacznik pozostaje aktywny i próba jest": (
        "When the EMS entity is unavailable, ownership remains active and the attempt is"
    ),
    "ponawiana co minutę po odzyskaniu łączności.": (
        "retried every minute after connectivity is restored."
    ),
    "bilans do końca jutra, a po automatycznym wykryciu Forecast Day 3 —": (
        "the balance through tomorrow, and after automatically detecting Forecast Day 3 —"
    ),
    "również kolejny dzień. W trybie **Grid Charge** ustawiona moc jest": (
        "also the following day. In **Grid Charge**, the configured"
    ),
    "różnica ceny nie pokrywa strat i automatycznie przyjętego kosztu": (
        "the price spread does not cover losses and the automatically assumed"
    ),
    "zużycia magazynu.": "battery wear cost.",
    "Model zużycia wykorzystuje do 28 pełnych dni, większą wagę nadaje": (
        "The load model uses up to 28 complete days, gives greater weight to"
    ),
    "obserwacjom najnowszym i rozdziela profil na dzień roboczy oraz": (
        "recent observations, and separates weekday and"
    ),
    "weekend. Plan korzysta z pasma niepewności P10/P50/P90, zachowuje": (
        "weekend profiles. The plan uses P10/P50/P90 uncertainty bands, retains"
    ),
    "rezerwę na końcu horyzontu i nie reaguje na każdą drobną zmianę": (
        "a terminal reserve, and does not react to every small forecast"
    ),
    "prognozy. Podczas prawdziwego Grid Charge mierzy też osiągniętą moc;": (
        "change. During real Grid Charge it also measures delivered power;"
    ),
    "po kilku wiarygodnych próbkach koryguje wyprzedzenie kolejnych okien,": (
        "after several reliable samples it corrects future lead times,"
    ),
    "nie ucząc się na rozruchu ani na zwalnianiu ładowania blisko 100% SOC.": (
        "without learning from ramp-up or charge taper near 100% SOC."
    ),
    "Samo wsparcie domu tanią siecią ma dodatkowy kontrakt wykonawczy:": (
        "Using low-cost grid power solely to supply the home has an additional "
        "execution contract:"
    ),
    "optymalizator musi zaakceptować cały ciągły blok, a jego intencja": (
        "the optimizer must accept the whole continuous run and its intent"
    ),
    "musi pozostać niezmienna przez 120 s. Pilne ładowanie magazynu nie": (
        "must remain unchanged for 120 s. Urgent battery charging does not"
    ),
    "czeka na ten filtr mikrocykli.": "wait for this micro-cycle filter.",
    "Za mało czasu na bezpieczny start w taniej strefie": (
        "Not enough time for a safe start in the low-cost period"
    ),
    "Warunki bezpiecznego startu Grid Charge wygasły": (
        "Safe Grid Charge start conditions expired"
    ),
    "'ustawienie'": "'configuration'",
    "'Taryfa' if": "'Tariff' if",
}

# Release 1.5.2 adds physical Modbus-readback transactions and stricter
# execution-readiness diagnostics to the EMS package.  Keep these complete
# phrases here (instead of relying on word-by-word substitutions) so the
# generated English package remains natural and cannot contain mixed-language
# safety messages.
ENGLISH_REPLACEMENTS.update(
    {
        "EMS taryfowy — zapamiętany limit mocy ładowania": (
            "Tariff EMS — saved charging-power limit"
        ),
        "EMS taryfowy — zapamiętany cel ładowania": (
            "Tariff EMS — saved charging target"
        ),
        "EMS RCE — zapamiętany limit mocy rozładowania": (
            "RCE EMS — saved discharging-power limit"
        ),
        "EMS RCE — zapamiętany próg rozładowania": (
            "RCE EMS — saved discharge threshold"
        ),
        "Brak obsługi zweryfikowanego odczytu sprzętowego": (
            "Verified hardware readback is not supported"
        ),
        "Blok EMS 4300-4306 nie jest gotowy": (
            "The EMS 4300-4306 block is not ready"
        ),
        "Rejestry 258/259/306 nie mają potwierdzonego broadcastu Master/Slave": (
            "Registers 258/259/306 have no verified Master/Slave broadcast"
        ),
        "Brak fizycznego odczytu trybu EMS": "Physical EMS-mode readback is unavailable",
        "Brak aktualnego SOC magazynu": "Current battery SOC is unavailable",
        "Eksport zablokowany przez GCF lub limit 0%": (
            "Export is blocked by GCF or a 0% limit"
        ),
        "Plan RCE jest nieaktualny albo niekompletny": (
            "The RCE plan is stale or incomplete"
        ),
        "Dzisiejsze dane RCE są nieświeże lub niepełne": (
            "Today's RCE data is stale or incomplete"
        ),
        "Dzisiejsza prognoza PV jest nieświeża lub niepełna": (
            "Today's PV forecast is stale or incomplete"
        ),
        "Stan GCF lub jego limit eksportu są nieświeże": (
            "The GCF state or its export limit is stale"
        ),
        "Brak aktualnej ceny RCE": "Current RCE price is unavailable",
        "GCF wyłączone — eksport bez limitu GCF": (
            "GCF disabled — export is not constrained by a GCF limit"
        ),
        "Eksport dozwolony przez GCF": "Export permitted by GCF",
        "Plan taryfowy jest nieaktualny albo niekompletny": (
            "The tariff plan is stale or incomplete"
        ),
        "Wspólny limit Grid Charge ograniczony świeżą zdolnością ładowania": (
            "The shared Grid Charge limit is constrained by fresh BMS charging capability"
        ),
        "BMS; uwzględnia bieżące obciążenie domu i wszystkie falowniki.": (
            "and accounts for current home load and every inverter."
        ),
        "EMS — zapisz tryb z odczytem sprzętowym": (
            "EMS — set mode with hardware readback"
        ),
        "Wykonuje FC10 dla trybu EMS i potwierdza nową generację odczytu, kod": (
            "Writes the EMS mode with FC10 and confirms a new readback generation, mode code"
        ),
        "trybu oraz niezmienione fizyczne lustra wszystkich sześciu nastaw 430x.": (
            "and unchanged physical mirrors for all six remaining 430x settings."
        ),
        "EMS — zapisz limit ładowania z odczytem sprzętowym": (
            "EMS — set charging limit with hardware readback"
        ),
        "EMS — zapisz próg ładowania z odczytem sprzętowym": (
            "EMS — set charging threshold with hardware readback"
        ),
        "EMS — zapisz limit rozładowania z odczytem sprzętowym": (
            "EMS — set discharging limit with hardware readback"
        ),
        "EMS — zapisz próg rozładowania z odczytem sprzętowym": (
            "EMS — set discharge threshold with hardware readback"
        ),
        "RCEm — zapisz limit ładowania baterii z odczytem sprzętowym": (
            "RCEm — set battery charging limit with hardware readback"
        ),
        "GCF — zapisz limit eksportu z odczytem sprzętowym": (
            "GCF — set export limit with hardware readback"
        ),
        "EMS — zatrzymaj cykl i wróć do Self-Use": (
            "EMS — stop the cycle and return to Self-Use"
        ),
        "EMS taryfowy — wycofaj niepełną transakcję": (
            "Tariff EMS — roll back an incomplete transaction"
        ),
        "EMS RCE — wycofaj niepełną transakcję": (
            "RCE EMS — roll back an incomplete transaction"
        ),
        "Wraca do Self-Use i odtwarza obie nastawy sprzed automatycznego startu.": (
            "Returns to Self-Use and restores both settings from before the automatic start."
        ),
        "Własność pozostaje aktywna, dopóki wszystkie odczyty nie są zgodne.": (
            "Ownership remains active until every readback matches."
        ),
        "Pozwala działać tylko jednemu automatowi wykonawczemu. RCE, ładowanie": (
            "Allows only one execution automation to run. RCE, tariff charging"
        ),
        "taryfowe i aktywne sterowanie RCEm 253 V+ wzajemnie się wykluczają.": (
            "and active RCEm 253 V+ control are mutually exclusive."
        ),
        "RCEm w trybie podglądu może równolegle zbierać dane bez przejmowania EMS.": (
            "RCEm can collect data in observation mode without taking ownership of EMS."
        ),
        "Falownik nie potwierdził limitu rozładowania RCE": (
            "The inverter did not confirm the RCE discharging limit"
        ),
        "Falownik nie potwierdził progu SOC RCE": (
            "The inverter did not confirm the RCE SOC threshold"
        ),
        "Warunki bezpiecznego startu Grid Discharge wygasły": (
            "Safe Grid Discharge start conditions expired"
        ),
        "Kończy ręczny cykl dopiero po potwierdzonym odczycie Self-Use.": (
            "Ends a manual cycle only after a confirmed Self-Use readback."
        ),
        "Minutowy watchdog ponawia niedokończoną finalizację po utracie API": (
            "A one-minute watchdog retries unfinished finalization after API loss"
        ),
        "albo restarcie Home Assistant, nie zwalniając wcześniej własności.": (
            "or a Home Assistant restart without releasing ownership early."
        ),
    }
)


@dataclass
class Entity:
    source_component: str
    source_domain: str
    source_name: str
    source_id: str
    entity_category: str | None
    options: list[str] = field(default_factory=list)


def slugify(value: str) -> str:
    """Return a stable Home Assistant translation/object-id key."""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().replace("%", " percent ")
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value


def polish_name(english_name: str) -> str:
    """Create a useful first-pass Polish entity name."""
    if english_name in PHRASE_TRANSLATIONS:
        return PHRASE_TRANSLATIONS[english_name]

    tokens = re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9]+", english_name)
    translated = "".join(WORD_TRANSLATIONS.get(token, token) for token in tokens)
    if translated:
        translated = translated[0].upper() + translated[1:]
    return translated


def parse_entities(path: Path) -> list[Entity]:
    """Extract top-level ESPHome entities from a package YAML file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    entities: list[Entity] = []
    current_domain = ""
    start_indexes: list[tuple[int, str]] = []

    for index, line in enumerate(lines):
        top_level = re.fullmatch(r"([a-z_]+):\s*", line)
        if top_level:
            current_domain = top_level.group(1)
            continue
        platform = re.match(r"^  - platform:\s*(.+?)\s*$", line)
        if platform and current_domain in SUPPORTED_SOURCE_DOMAINS:
            start_indexes.append((index, current_domain))

    for position, (start, source_domain) in enumerate(start_indexes):
        end = start_indexes[position + 1][0] if position + 1 < len(start_indexes) else len(lines)
        block = lines[start:end]
        name = ""
        source_id = ""
        category: str | None = None
        options: list[str] = []
        option_mode: str | None = None
        nested_entities: list[Entity] = []
        nested_name = ""
        nested_id = ""
        nested_category: str | None = None

        def flush_nested() -> None:
            nonlocal nested_name, nested_id, nested_category
            if nested_name:
                nested_entities.append(
                    Entity(
                        source_component=source_domain,
                        source_domain=(
                            "sensor" if source_domain == "text_sensor" else source_domain
                        ),
                        source_name=nested_name,
                        source_id=nested_id or slugify(nested_name),
                        entity_category=nested_category,
                    )
                )
            nested_name = ""
            nested_id = ""
            nested_category = None

        for line in block:
            name_match = re.match(r'^    name:\s*["\']?(.*?)["\']?\s*$', line)
            if name_match:
                name = name_match.group(1)
                continue
            id_match = re.match(r"^    id:\s*(.+?)\s*$", line)
            if id_match:
                source_id = id_match.group(1).strip("\"'")
                continue
            category_match = re.match(r"^    entity_category:\s*(.+?)\s*$", line)
            if category_match:
                category = category_match.group(1).strip("\"'")
                continue
            if re.match(r"^    options:\s*$", line):
                option_mode = "list"
                continue
            if re.match(r"^    optionsmap:\s*$", line):
                option_mode = "map"
                continue
            if option_mode == "list":
                item = re.match(r'^      -\s*["\'](.*?)["\']\s*$', line)
                if item:
                    options.append(item.group(1))
                elif line.strip() and not line.startswith("      "):
                    option_mode = None
            elif option_mode == "map":
                item = re.match(r'^      ["\'](.*?)["\']:\s*.+$', line)
                if item:
                    options.append(item.group(1))
                elif line.strip() and not line.startswith("      "):
                    option_mode = None
            nested_section = re.match(r"^    [a-z_]+:\s*$", line)
            if nested_section:
                flush_nested()
                continue
            nested_name_match = re.match(
                r'^      name:\s*["\']?(.*?)["\']?\s*$',
                line,
            )
            if nested_name_match:
                nested_name = nested_name_match.group(1)
                continue
            nested_id_match = re.match(r"^      id:\s*(.+?)\s*$", line)
            if nested_id_match:
                nested_id = nested_id_match.group(1).strip("\"'")
                continue
            nested_category_match = re.match(
                r"^      entity_category:\s*(.+?)\s*$",
                line,
            )
            if nested_category_match:
                nested_category = nested_category_match.group(1).strip("\"'")

        flush_nested()

        if not name and nested_entities:
            entities.extend(nested_entities)
            continue

        if name:
            entities.append(
                Entity(
                    source_component=source_domain,
                    source_domain="sensor" if source_domain == "text_sensor" else source_domain,
                    source_name=name,
                    source_id=source_id or slugify(name),
                    entity_category=category,
                    options=options,
                )
            )

    return entities


def option_definition(raw: str) -> dict[str, str]:
    """Return a canonical select option and both UI translations."""
    if raw in OPTION_TRANSLATIONS:
        key, english, polish = OPTION_TRANSLATIONS[raw]
    else:
        key, english, polish = slugify(raw), raw, raw
    return {"key": key, "raw": raw, "en": english, "pl": polish}


def static_translations(language: str) -> dict:
    """Return config-flow and service translations."""
    if language == "pl":
        return {
            "title": "EMS for Hoymiles HIT-(5–20)L-G3",
            "config": {
                "step": {
                    "user": {
                        "title": "Połącz z urządzeniem ESPHome",
                        "description": (
                            "Wybierz urządzenie ESPHome z falownikiem Hoymiles. "
                            "Integracja utworzy lokalizowane encje bez dodatkowego "
                            "odpytywania magistrali Modbus."
                        ),
                        "data": {
                            "source_device_id": "Urządzenie źródłowe ESPHome",
                        },
                        "data_description": {
                            "source_device_id": (
                                "Urządzenie musi używać firmware z tego projektu."
                            ),
                        },
                    }
                },
                "error": {
                    "device_not_found": "Nie znaleziono wybranego urządzenia.",
                    "no_entities": "Urządzenie nie udostępnia obsługiwanych encji.",
                },
                "abort": {
                    "already_configured": "To urządzenie jest już skonfigurowane."
                },
            },
            "issues": {
                "ems_package_not_loaded": {
                    "title": "Automatyka EMS wymaga włączenia pakietów",
                    "description": (
                        "Integracja skopiowała plik automatyki, ale Home Assistant "
                        "go nie wczytał. Dodaj `packages: !include_dir_named packages` "
                        "pod istniejącą sekcją `homeassistant:` w pliku "
                        "`configuration.yaml`, sprawdź konfigurację i uruchom "
                        "Home Assistant ponownie. Nie twórz drugiej sekcji "
                        "`homeassistant:`."
                    ),
                },
                "ems_package_restart_required": {
                    "title": (
                        "Aktualizacja pakietu EMS wymaga ponownego uruchomienia"
                    ),
                    "description": (
                        "Integracja zaktualizowała zarządzany pakiet EMS po "
                        "wczytaniu konfiguracji. Sprawdź konfigurację i uruchom "
                        "Home Assistant ponownie jeszcze raz. Jeśli komunikat "
                        "pozostaje po restarcie, wykonaj kopię własnych zmian i "
                        "użyj akcji `hoymiles_hit_modbus.install_assets` z opcją "
                        "`overwrite: true`, a następnie uruchom Home Assistant "
                        "ponownie."
                    ),
                },
                "frontend_assets_restart_required": {
                    "title": "Zasoby dashboardu wymagajÄ… restartu",
                    "description": (
                        "Integracja utworzyĹ‚a katalog `www` i skopiowaĹ‚a "
                        "zarzÄ…dzane zasoby dashboardu. Uruchom Home Assistant "
                        "ponownie, aby bezpiecznie udostÄ™pniÄ‡ je pod `/local` "
                        "przed zaĹ‚adowaniem dashboardu."
                    ),
                },
                "frontend_assets_install_failed": {
                    "title": "Nie udało się zainstalować zasobów dashboardu",
                    "description": (
                        "Nie udało się zainstalować opcjonalnych zasobów "
                        "dashboardu i EMS, ale encje urządzenia Hoymiles "
                        "pozostają dostępne. Sprawdź wolne miejsce i "
                        "uprawnienia, następnie uruchom "
                        "`hoymiles_hit_modbus.install_assets` z "
                        "`overwrite: true` i uruchom Home Assistant ponownie."
                    ),
                },
            },
            "services": {
                "install_assets": {
                    "name": "Zainstaluj lub zaktualizuj zasoby",
                    "description": (
                        "Kopiuje dashboard, kartę RCE i pakiet automatyki EMS "
                        "do katalogu konfiguracyjnego Home Assistanta."
                    ),
                    "fields": {
                        "overwrite": {
                            "name": "Nadpisz istniejące pliki",
                            "description": (
                                "Zastępuje wcześniej skopiowane zasoby wersją "
                                "dołączoną do integracji."
                            ),
                        }
                    },
                }
            },
        }

    return {
        "title": "EMS for Hoymiles HIT-(5–20)L-G3",
        "config": {
            "step": {
                "user": {
                    "title": "Connect an ESPHome device",
                    "description": (
                        "Select the ESPHome device connected to the Hoymiles "
                        "inverter. The integration creates localized entities "
                        "without additional Modbus polling."
                    ),
                    "data": {
                        "source_device_id": "Source ESPHome device",
                    },
                    "data_description": {
                        "source_device_id": (
                            "The device must run firmware provided by this project."
                        ),
                    },
                }
            },
            "error": {
                "device_not_found": "The selected device was not found.",
                "no_entities": "The device exposes no supported entities.",
            },
            "abort": {
                "already_configured": "This device is already configured."
            },
        },
        "issues": {
            "ems_package_not_loaded": {
                "title": "EMS automation requires Home Assistant packages",
                "description": (
                    "The integration copied the automation file, but Home Assistant "
                    "did not load it. Add `packages: !include_dir_named packages` "
                    "under the existing `homeassistant:` section in "
                    "`configuration.yaml`, validate the configuration and restart "
                    "Home Assistant. Do not create a second `homeassistant:` section."
                ),
            },
            "ems_package_restart_required": {
                "title": "EMS package update requires another restart",
                "description": (
                    "The integration updated the managed EMS package after Home "
                    "Assistant had loaded its configuration. Validate the "
                    "configuration and restart Home Assistant once more. If this "
                    "message remains after the restart, back up your custom "
                    "changes, run `hoymiles_hit_modbus.install_assets` with "
                    "`overwrite: true`, and restart Home Assistant again."
                ),
            },
            "frontend_assets_restart_required": {
                "title": "Dashboard assets require a restart",
                "description": (
                    "The integration created the `www` directory and copied its "
                    "managed dashboard assets. Restart Home Assistant so they are "
                    "safely exposed under `/local` before the dashboard loads."
                ),
            },
            "frontend_assets_install_failed": {
                "title": "Dashboard assets could not be installed",
                "description": (
                    "The optional dashboard and EMS assets could not be "
                    "installed, but Hoymiles device entities remain available. "
                    "Check free disk space and permissions, then run "
                    "`hoymiles_hit_modbus.install_assets` with "
                    "`overwrite: true` and restart Home Assistant."
                ),
            },
        },
        "services": {
            "install_assets": {
                "name": "Install or update assets",
                "description": (
                    "Copies the dashboard, RCE card and EMS automation package "
                    "to the Home Assistant configuration directory."
                ),
                "fields": {
                    "overwrite": {
                        "name": "Overwrite existing files",
                        "description": (
                            "Replace previously copied assets with the version "
                            "bundled with this integration."
                        ),
                    }
                },
            }
        },
    }


def transform_entity_ids(text: str, catalog: list[dict]) -> str:
    """Replace installation-specific ESPHome ids with stable proxy ids."""
    candidates: dict[str, list[dict]] = {
        "button": [],
        "sensor": [],
        "number": [],
        "select": [],
    }
    for record in catalog:
        candidates[record["domain"]].append(record)
    for records in candidates.values():
        records.sort(key=lambda record: len(record["source_object_id"]), reverse=True)

    pattern = re.compile(r"\b(button|sensor|number|select)\.([a-z0-9_]+)\b")

    def replace(match: re.Match[str]) -> str:
        domain, object_id = match.groups()
        if "hoymiles_inverter" not in object_id:
            return match.group(0)
        for record in candidates[domain]:
            source_object_id = record["source_object_id"]
            if object_id == source_object_id or object_id.endswith(
                f"_{source_object_id}"
            ):
                return f"{domain}.hoymiles_hit_{record['translation_key']}"
        return match.group(0)

    return pattern.sub(replace, text)


def add_dashboard_entity_names(
    text: str, catalog: list[dict], language: str
) -> str:
    """Expand entity-card shorthand with short localized dashboard-only names."""
    names: dict[str, str] = {}
    localized_names: dict[str, dict[str, str]] = {}
    for record in catalog:
        domain = record["domain"]
        source_object_id = record["source_object_id"]
        name = record["name"][language]
        aliases = (
            f"{domain}.hoymiles_hit_{record['translation_key']}",
            f"{domain}.hoymiles_inverter_{source_object_id}",
            f"{domain}.pv_hoymiles_inverter_{source_object_id}",
        )
        for alias in aliases:
            names[alias] = name
            localized_names[alias] = record["name"]

    shorthand = re.compile(
        r"^(?P<indent>\s*)-\s+"
        r"(?P<entity>(?:button|sensor|number|select)\.[a-z0-9_]+)"
        r"\s*$"
    )
    expanded: list[str] = []
    entities_indent: int | None = None
    for line in text.splitlines():
        leading = len(line) - len(line.lstrip())
        if line.strip() and entities_indent is not None and leading <= entities_indent:
            entities_indent = None
        if re.match(r"^\s*entities:\s*$", line):
            entities_indent = leading
            expanded.append(line)
            continue

        match = shorthand.match(line)
        if (
            not match
            or entities_indent is None
            or len(match.group("indent")) != entities_indent + 2
        ):
            expanded.append(line)
            continue
        entity_id = match.group("entity")
        name = names.get(entity_id)
        if not name:
            expanded.append(line)
            continue
        indent = match.group("indent")
        expanded.append(f"{indent}- entity: {entity_id}")
        expanded.append(
            f"{indent}  name: {json.dumps(name, ensure_ascii=False)}"
        )
    relocalized: list[str] = []
    entity_row = re.compile(
        r"^(?P<indent>\s*)-\s+entity:\s+"
        r"(?P<entity>(?:button|sensor|number|select)\.[a-z0-9_]+)\s*$"
    )
    name_row = re.compile(r"^(?P<indent>\s*)name:\s+(?P<name>.+?)\s*$")
    index = 0
    while index < len(expanded):
        line = expanded[index]
        match = entity_row.match(line)
        if (
            match
            and index + 1 < len(expanded)
            and match.group("entity") in localized_names
        ):
            following = name_row.match(expanded[index + 1])
            if following:
                raw_name = following.group("name")
                try:
                    current_name = json.loads(raw_name)
                except json.JSONDecodeError:
                    current_name = raw_name.strip("'\"")
                known_names = set(
                    localized_names[match.group("entity")].values()
                )
                if current_name in known_names:
                    relocalized.append(line)
                    relocalized.append(
                        f"{following.group('indent')}name: "
                        f"{json.dumps(names[match.group('entity')], ensure_ascii=False)}"
                    )
                    index += 2
                    continue
        relocalized.append(line)
        index += 1

    return "\n".join(relocalized) + ("\n" if text.endswith("\n") else "")


def translate_asset_to_english(text: str) -> str:
    """Create a first-pass English dashboard/package for the current release."""
    text = text.replace('"Autokonsumpcja (Self-Use)"', '"self_use"')
    text = text.replace('"Ładowanie z sieci"', '"grid_charge"')
    text = text.replace('"Rozładowanie do sieci"', '"grid_discharge"')
    for polish, english in sorted(
        ENGLISH_REPLACEMENTS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        text = text.replace(polish, english)
    return text


def canonicalize_proxy_select_options(text: str) -> str:
    """Use canonical proxy options in the localized Polish package as well."""
    return (
        text.replace('"Autokonsumpcja (Self-Use)"', '"self_use"')
        .replace('"Ładowanie z sieci"', '"grid_charge"')
        .replace('"Rozładowanie do sieci"', '"grid_discharge"')
    )


def build() -> None:
    """Generate catalog/translations and copy public assets."""
    entities: list[Entity] = []
    for path in sorted(PACKAGES.glob("*.yaml")):
        if path.name not in SKIPPED_FILES:
            entities.extend(parse_entities(path))

    seen: set[tuple[str, str]] = set()
    catalog: list[dict] = []
    en = static_translations("en")
    pl = static_translations("pl")
    en["entity"] = {"button": {}, "sensor": {}, "number": {}, "select": {}}
    pl["entity"] = {"button": {}, "sensor": {}, "number": {}, "select": {}}

    for entity in entities:
        if entity.source_name in SPECIAL_NAMES:
            key, english, polish = SPECIAL_NAMES[entity.source_name]
        else:
            key = slugify(entity.source_name)
            english = entity.source_name
            polish = polish_name(entity.source_name)

        identity = (entity.source_domain, key)
        if identity in seen:
            key = f"{key}_{slugify(entity.source_id)}"
            identity = (entity.source_domain, key)
        seen.add(identity)

        options = [option_definition(raw) for raw in entity.options]
        record = {
            "domain": entity.source_domain,
            "source_component": entity.source_component,
            "translation_key": key,
            "source_name": entity.source_name,
            "source_id": entity.source_id,
            "source_object_id": slugify(entity.source_name),
            "entity_category": entity.entity_category,
            "name": {"en": english, "pl": polish},
            "description": {
                "en": (
                    f"Hoymiles HIT xxL G3 Modbus value: {english}."
                    if entity.source_domain == "sensor"
                    else (
                        f"One-shot Hoymiles HIT xxL G3 command: {english}."
                        if entity.source_domain == "button"
                        else f"Writable Hoymiles HIT xxL G3 setting: {english}."
                    )
                ),
                "pl": (
                    f"Wartość Modbus falownika Hoymiles HIT xxL G3: {polish}."
                    if entity.source_domain == "sensor"
                    else (
                        f"Jednorazowe polecenie falownika Hoymiles HIT xxL G3: {polish}."
                        if entity.source_domain == "button"
                        else f"Zapisywalne ustawienie falownika Hoymiles HIT xxL G3: {polish}."
                    )
                ),
            },
            "options": options,
        }
        catalog.append(record)

        en_entity = {"name": english}
        pl_entity = {"name": polish}
        if options:
            en_entity["state"] = {option["key"]: option["en"] for option in options}
            pl_entity["state"] = {option["key"]: option["pl"] for option in options}
        elif entity.source_component == "text_sensor":
            en_entity["state"] = {
                key: value[0] for key, value in TEXT_STATE_TRANSLATIONS.items()
            }
            pl_entity["state"] = {
                key: value[1] for key, value in TEXT_STATE_TRANSLATIONS.items()
            }
        en["entity"][entity.source_domain][key] = en_entity
        pl["entity"][entity.source_domain][key] = pl_entity

    # Integration-native optimizer sensors do not originate in ESPHome YAML,
    # therefore they must be added after the generated proxy catalog.
    en["entity"]["sensor"]["rce_optimized_plan"] = {
        "name": "Optimized RCE plan"
    }
    pl["entity"]["sensor"]["rce_optimized_plan"] = {
        "name": "Zoptymalizowany plan RCE"
    }
    en["entity"]["sensor"]["tariff_charge_plan"] = {
        "name": "Automatic tariff charging plan"
    }
    pl["entity"]["sensor"]["tariff_charge_plan"] = {
        "name": "Plan automatycznego ładowania taryfowego"
    }
    en["entity"]["sensor"]["rcm_voltage_plan"] = {
        "name": "RCEm 253 V+ plan"
    }
    pl["entity"]["sensor"]["rcm_voltage_plan"] = {
        "name": "Plan RCEm 253 V+"
    }
    en["entity"]["sensor"]["setup_status"] = {
        "name": "Installation status"
    }
    pl["entity"]["sensor"]["setup_status"] = {
        "name": "Stan instalacji"
    }
    en["entity"]["sensor"]["ems_supervisor"] = {
        "name": "EMS Supervisor"
    }
    pl["entity"]["sensor"]["ems_supervisor"] = {
        "name": "Nadzorca EMS"
    }

    COMPONENT.mkdir(parents=True, exist_ok=True)
    TRANSLATIONS.mkdir(parents=True, exist_ok=True)
    (COMPONENT / "entity_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (TRANSLATIONS / "en.json").write_text(
        json.dumps(en, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (TRANSLATIONS / "pl.json").write_text(
        json.dumps(pl, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    dashboard = transform_entity_ids(
        (ROOT / "dashboard_hoymiles.yaml").read_text(encoding="utf-8"),
        catalog,
    )
    package = transform_entity_ids(
        (ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml").read_text(
            encoding="utf-8"
        ),
        catalog,
    )

    localized_assets = {
        RESOURCES / "dashboard_hoymiles_pl.yaml": add_dashboard_entity_names(
            dashboard, catalog, "pl"
        ),
        RESOURCES / "dashboard_hoymiles_en.yaml": translate_asset_to_english(
            add_dashboard_entity_names(dashboard, catalog, "en")
        ),
        RESOURCES
        / "home_assistant"
        / "pl"
        / "hoymiles_ems_scheduler.yaml": canonicalize_proxy_select_options(package),
        RESOURCES
        / "home_assistant"
        / "en"
        / "hoymiles_ems_scheduler.yaml": translate_asset_to_english(package),
    }
    for destination, content in localized_assets.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")

    for language in ("pl", "en"):
        dashboard_yaml = (
            RESOURCES / f"dashboard_hoymiles_{language}.yaml"
        ).read_text(encoding="utf-8")
        dashboard_data = yaml.safe_load(dashboard_yaml)
        if (
            not isinstance(dashboard_data, dict)
            or not isinstance(dashboard_data.get("views"), list)
        ):
            raise ValueError(
                f"Generated {language} dashboard has no top-level views list"
            )
        dashboard_json = (
            RESOURCES / "www" / f"dashboard_hoymiles_{language}.json"
        )
        dashboard_json.parent.mkdir(parents=True, exist_ok=True)
        dashboard_json.write_text(
            json.dumps(dashboard_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    for bundled_www_asset in (
        "hoymiles-dashboard-strategy.js",
        "hoymiles-rce-chart-card.js",
        "hoymiles-inverter.png",
    ):
        card_source = ROOT / "home_assistant" / "www" / bundled_www_asset
        card_destination = RESOURCES / "www" / bundled_www_asset
        card_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(card_source, card_destination)

    print(f"Generated {len(catalog)} localized entities.")


if __name__ == "__main__":
    build()
