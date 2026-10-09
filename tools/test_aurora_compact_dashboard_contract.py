"""Static Variant A contract for the compact Aurora dashboard.

The test intentionally uses only the Python standard library.  It reads the
canonical dashboard and frontend source without importing Home Assistant or a
YAML parser, so it can guard the navigation and card composition in fast
offline validation.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(os.environ.get("HOYMILES_UI_TEST_ROOT", Path(__file__).resolve().parents[1]))
DASHBOARD_PATH = ROOT / "dashboard_hoymiles.yaml"
CARD_PATH = ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js"
EN_DASHBOARD_PATH = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "resources"
    / "www"
    / "dashboard_hoymiles_en.json"
)

LEGACY_PATHS = {
    "start",
    "plan-automatyki",
    "ems-supervisor",
    "ustawienia-ems",
    "ustawienia-balansowania",
    "automatyka-ems",
    "ladowanie-taryfowe",
    "rcem-253v",
    "zyski",
    "produkcja-pv",
    "pv",
    "load-eps",
    "bateria",
    "siec",
    "przeplywy",
    "falownik",
    "generator",
    "liczniki",
    "sterowanie",
    "stany-alarmy",
    "diagnostyka",
}
MAIN_PATHS = [
    "start",
    "plan-automatyki",
    "ustawienia-ems",
    "pv",
    "bateria",
    "load-eps",
    "zyski",
]
SETTINGS_CONTEXT_PATHS: list[str] = []
SETTINGS_LINK_PATHS = [
    "ustawienia-ems",
    "automatyka-ems",
    "ladowanie-taryfowe",
    "rcem-253v",
    "ustawienia-balansowania",
    "diagnostyka",
]
SERVICE_PATHS = [
    "ems-supervisor",
    "automatyka-ems",
    "ladowanie-taryfowe",
    "rcem-253v",
    "stany-alarmy",
    "sterowanie",
    "falownik",
    "siec",
    "przeplywy",
    "generator",
    "liczniki",
    "zyski",
    "produkcja-pv",
]
ENGLISH_VIEW_TITLES = {
    "start": "Overview",
    "plan-automatyki": "EMS",
    "ems-supervisor": "EMS automation",
    "ustawienia-ems": "EMS settings",
    "ustawienia-balansowania": "Balancing",
    "automatyka-ems": "Dynamic export",
    "ladowanie-taryfowe": "Tariff charging",
    "rcem-253v": "Voltage management",
    "zyski": "Earnings",
    "produkcja-pv": "PV production",
    "pv": "PV",
    "bateria": "Battery storage",
    "load-eps": "Energy",
    "siec": "Grid",
    "przeplywy": "Energy flows",
    "falownik": "Inverter",
    "generator": "Other sources",
    "liczniki": "Energy meters",
    "sterowanie": "Controls",
    "stany-alarmy": "Status and alarms",
    "diagnostyka": "Service",
}

CHECKS = 0
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record one contract assertion and keep collecting useful failures."""

    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)


def scalar(value: str) -> str:
    """Normalize the simple scalar forms used by the canonical dashboard."""

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


@dataclass(frozen=True)
class View:
    title: str
    path: str
    subview: bool
    source: str


def dashboard_views(source: str) -> list[View]:
    """Extract only root-level Lovelace views by their fixed indentation."""

    headers = list(re.finditer(r"(?m)^  - title:\s*(.+?)\s*$", source))
    views: list[View] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(source)
        block = source[header.start() : end]
        path_match = re.search(r"(?m)^    path:\s*(\S+)\s*$", block)
        check(path_match is not None, f"view {scalar(header.group(1))!r} has no path")
        if path_match is None:
            continue
        subview_match = re.search(r"(?m)^    subview:\s*(\S+)\s*$", block)
        subview_value = scalar(subview_match.group(1)).lower() if subview_match else "false"
        check(
            subview_value in {"true", "false"},
            f"view {scalar(path_match.group(1))!r} has a non-boolean subview value",
        )
        views.append(
            View(
                title=scalar(header.group(1)),
                path=scalar(path_match.group(1)),
                subview=subview_value == "true",
                source=block,
            )
        )
    return views


def js_section(source: str, start_token: str, end_token: str) -> str:
    """Return a bounded frontend constant/function section."""

    start = source.find(start_token)
    end = source.find(end_token, start + len(start_token)) if start >= 0 else -1
    check(start >= 0, f"missing frontend token: {start_token}")
    check(end >= 0, f"missing frontend boundary after {start_token}: {end_token}")
    return source[start:end] if start >= 0 and end >= 0 else ""


def card_blocks_at_indent(source: str, indent: int) -> list[str]:
    """Extract YAML card blocks, ending at the next card of equal/lower depth."""

    lines = source.splitlines(keepends=True)
    starts: list[int] = []
    type_line = re.compile(r"^(\s*)- type:\s*\S+")
    for index, line in enumerate(lines):
        match = type_line.match(line)
        if match and len(match.group(1)) == indent:
            starts.append(index)

    blocks: list[str] = []
    for start in starts:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            match = type_line.match(lines[index])
            if match and len(match.group(1)) <= indent:
                end = index
                break
        blocks.append("".join(lines[start:end]))
    return blocks


def keyed_blocks_at_indent(source: str, indent: int) -> list[str]:
    """Extract ``- key:`` blocks used by compact period selectors."""

    lines = source.splitlines(keepends=True)
    starts: list[int] = []
    key_line = re.compile(r"^(\s*)- key:\s*\S+")
    for index, line in enumerate(lines):
        match = key_line.match(line)
        if match and len(match.group(1)) == indent:
            starts.append(index)

    blocks: list[str] = []
    for start in starts:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            candidate_indent = len(lines[index]) - len(lines[index].lstrip())
            if lines[index].strip() and candidate_indent < indent:
                end = index
                break
            match = key_line.match(lines[index])
            if match and len(match.group(1)) == indent:
                end = index
                break
        blocks.append("".join(lines[start:end]))
    return blocks


def yaml_field(source: str, name: str) -> str | None:
    match = re.search(rf"(?m)^\s+{re.escape(name)}:\s*(.+?)\s*$", source)
    return scalar(match.group(1)) if match else None


def card_type(source: str) -> str | None:
    match = re.search(r"(?m)^\s*- type:\s*(\S+)\s*$", source)
    return scalar(match.group(1)) if match else None


def block_key(source: str) -> str | None:
    match = re.search(r"(?m)^\s*- key:\s*(\S+)\s*$", source)
    return scalar(match.group(1)) if match else None


def entity_ids(source: str) -> list[str]:
    """Inventory both native entity rows and compact-card ``*_entity`` binds."""

    return re.findall(
        r"(?m)^\s*(?:(?:-\s+)?entity|entity_id|[a-z][a-z0-9_]*_entity):\s*(\S+)\s*$",
        source,
    )


def yaml_sequence_section(source: str, name: str) -> str:
    """Return one indented YAML sequence/config section by its field name."""

    lines = source.splitlines(keepends=True)
    pattern = re.compile(rf"^(?P<indent>\s*){re.escape(name)}:\s*$")
    for index, line in enumerate(lines):
        match = pattern.match(line.rstrip("\r\n"))
        if match is None:
            continue
        indent = len(match.group("indent"))
        end = len(lines)
        for cursor in range(index + 1, len(lines)):
            candidate = lines[cursor]
            if not candidate.strip():
                continue
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate_indent <= indent:
                end = cursor
                break
        return "".join(lines[index + 1 : end])
    return ""


def compact_page_root(view: View, page: str) -> str:
    """Assert and return the sole full-width Aurora page composition."""

    check(
        re.search(r"(?m)^    type:\s*panel\s*$", view.source) is not None,
        f"{view.path} must use a full-width Lovelace panel root",
    )
    cards = card_blocks_at_indent(view.source, 6)
    check(len(cards) == 1, f"{view.path} panel must contain exactly one top-level card")
    if not cards:
        return ""
    root = cards[0]
    check(
        card_type(root) == "custom:hoymiles-aurora-compact-page-card",
        f"{view.path} must use the Aurora compact page composition",
    )
    check(yaml_field(root, "page") == page, f"{view.path} compact page type must be {page!r}")
    return root


def entity_name_pairs(source: str) -> list[tuple[str, str]]:
    pattern = re.compile(
        r"(?m)^(?P<indent>\s*)- entity:\s*(?P<entity>\S+)\s*$"
        r"\n(?P=indent)  name:\s*(?P<name>.+?)\s*$"
    )
    return [(match.group("entity"), scalar(match.group("name"))) for match in pattern.finditer(source)]


def check_views(dashboard: str, card_source: str) -> dict[str, View]:
    views = dashboard_views(dashboard)
    paths = [view.path for view in views]
    check(len(views) == 21, f"expected exactly 21 stable views, got {len(views)}")
    check(len(paths) == len(set(paths)), "dashboard view paths must be unique")
    check(set(paths) == LEGACY_PATHS, f"legacy path set changed: {sorted(set(paths) ^ LEGACY_PATHS)}")

    by_path = {view.path: view for view in views}
    visible_order = [view.path for view in views if not view.subview]
    check(visible_order == MAIN_PATHS, f"visible view order is {visible_order}")
    for path in LEGACY_PATHS - set(MAIN_PATHS):
        check(by_path.get(path) is not None and by_path[path].subview, f"legacy view {path!r} must be a subview")
    for path in MAIN_PATHS:
        check(by_path.get(path) is not None and not by_path[path].subview, f"main view {path!r} must remain visible")

    main_match = re.search(
        r"const HOYMILES_MAIN_VIEW_PATHS\s*=\s*Object\.freeze\(\[(?P<body>.*?)\]\);",
        card_source,
        flags=re.DOTALL,
    )
    frontend_main_paths = re.findall(
        r'"([a-z0-9-]+)"', main_match.group("body") if main_match else ""
    )
    check(frontend_main_paths == MAIN_PATHS, f"visible Variant A order is {frontend_main_paths}")
    ordering_section = js_section(
        card_source,
        "const mainRank = new Map",
        "class HoymilesLocalNavCard",
    )
    check("decoratedViews.sort" in ordering_section, "dashboard strategy must sort main views")
    check(
        re.search(r"return\s+leftRank\s*-\s*rightRank", ordering_section) is not None,
        "dashboard strategy must preserve HOYMILES_MAIN_VIEW_PATHS order",
    )
    decorator = js_section(
        card_source,
        "function hoymilesDecorateDashboard",
        "class HoymilesLocalNavCard",
    )
    check(
        '...(useAuroraShell ? { type: "panel", subview: true } : {})' in decorator,
        "every Aurora app-shell view must be a HA subview so the shell is the only tab bar",
    )

    diagnostics = by_path.get("diagnostyka")
    check(diagnostics is not None and diagnostics.title == "Serwis", "diagnostyka view title must be Serwis")
    return by_path


def check_settings_navigation(card_source: str) -> None:
    diagnostics_mount = js_section(
        card_source,
        "function hoymilesMountDiagnosticsDownload",
        "const HOYMILES_UI_SAFE_OFF_BINDINGS",
    )
    check(
        'document.createElement("hoymiles-diagnostics-download-card")'
        in diagnostics_mount
        and "new HoymilesDiagnosticsDownloadCard()" not in diagnostics_mount,
        "diagnostics support card must be constructed through its registered tag for fresh-tab compatibility",
    )
    groups = js_section(
        card_source,
        "const HOYMILES_LOCAL_NAV_GROUPS",
        "const HOYMILES_LOCAL_NAV_CONTEXT_PATHS",
    )
    objects = re.findall(r"Object\.freeze\(\{([^{}]+)\}\)", groups, flags=re.DOTALL)
    settings_items: list[tuple[str, bool]] = []
    for body in objects:
        path_match = re.search(r'path:\s*"([a-z0-9-]+)"', body)
        if path_match:
            settings_items.append((path_match.group(1), "related: true" in body))
    check(
        [path for path, _related in settings_items] == SETTINGS_LINK_PATHS,
        f"settings links are {settings_items}",
    )
    check(
        [path for path, related in settings_items if related]
        == ["diagnostyka"],
        "only Service remains a related link; balancing is a first-class EMS setting",
    )

    contexts = js_section(
        card_source,
        "const HOYMILES_LOCAL_NAV_CONTEXT_PATHS",
        "const HOYMILES_MAIN_VIEW_PATHS",
    )
    context_paths = re.findall(r'"([a-z0-9-]+)"', contexts)
    check(context_paths == SETTINGS_CONTEXT_PATHS, f"settings nav contexts are {context_paths}")
    nav_function = js_section(card_source, "function hoymilesLocalNavGroup", "function hoymilesDecorateDashboard")
    check(
        "if (!HOYMILES_LOCAL_NAV_CONTEXT_PATHS.has(path)) return null;" in nav_function,
        "local settings navigation must be limited by its context path set",
    )
    decorator = js_section(card_source, "function hoymilesDecorateDashboard", "class HoymilesLocalNavCard")
    check(
        'type: "custom:hoymiles-local-nav-card"' not in decorator,
        "the obsolete coloured settings strip must never be auto-injected",
    )


def check_app_shell_return_menu(card_source: str) -> None:
    """Keep the Aurora-only tab bar navigable back to the wider HA UI."""

    shell = js_section(
        card_source,
        "class HoymilesAuroraAppShellCard extends HTMLElement",
        'if(!customElements.get("hoymiles-aurora-app-shell-card"))',
    )
    item_match = re.search(r"const items=\[(?P<body>.*?)\];", shell, flags=re.DOTALL)
    item_paths = re.findall(
        r'\["([a-z0-9-]+)"\s*,', item_match.group("body") if item_match else ""
    )
    check(
        item_paths == MAIN_PATHS,
        f"Aurora app-shell navigation order changed: {item_paths}",
    )
    check(
        'path==="ustawienia-ems"?["ustawienia-ems","automatyka-ems","ladowanie-taryfowe","rcem-253v","ustawienia-balansowania"].includes(current):current===path' in shell,
        "Aurora app shell must keep the Settings tab active for every policy settings view",
    )
    check(
        'const serviceActive=this._config.current_path==="diagnostyka";' in shell
        and 'class="more ${serviceActive?"active":""}"' in shell
        and "${serviceActive?' aria-current=\"page\"':\"\"}" in shell,
        "Aurora app shell must keep both the overflow button and Service link active on Service",
    )

    check(
        '<button class="more ${serviceActive?"active":""}" type="button" data-more' in shell
        and 'aria-expanded="false"' in shell
        and 'aria-controls="hoymiles-aurora-overflow"' in shell
        and ">•••</button>" in shell
        and 'event.target?.closest?.("[data-more]")' in shell
        and "this._setMenuOpen(!this._menuOpen);" in shell
        and '<a class="more' not in shell,
        "Aurora overflow affordance must remain an accessible three-dot button, not a direct link",
    )
    check(
        'id="hoymiles-aurora-overflow" data-overflow-menu' in shell
        and 'aria-label="${pl?"Dodatkowa nawigacja":"Additional navigation"}" hidden' in shell,
        "Aurora overflow button must control an initially hidden, labelled navigation region",
    )
    check(
        'href="/hoymiles-falownik/diagnostyka"' in shell
        and '${pl?"Serwis":"Service"}' in shell
        and 'href="/config">Home Assistant' in shell,
        "Aurora overflow menu must expose Service and the exact /config Home Assistant return target",
    )
    check(
        ':host{isolation:isolate;min-width:0}' in shell
        and '.shell{height:auto;inset:auto;min-height:calc(100vh - var(--header-height,56px));min-height:calc(100dvh - var(--header-height,56px));overflow:visible;position:relative;width:100%;z-index:auto}' in shell
        and '@media(max-width:620px){:host{display:block;height:calc(100vh - var(--header-height,56px));height:calc(100dvh - var(--header-height,56px));overflow:hidden}' in shell
        and '.shell{-webkit-overflow-scrolling:touch;height:calc(100vh - var(--header-height,56px));height:calc(100dvh - var(--header-height,56px));min-height:0;overflow-anchor:none;overflow-x:hidden;overflow-y:auto;overscroll-behavior-y:contain}' in shell
        and "_captureMobileScroll()" in shell
        and "this._pendingMobileScroll" in shell
        and "let frames=4;" in shell
        and "this._scrollIntentRevision!==intentRevision" in shell
        and "this._scrollGestureActive" in shell
        and "this._scrollUserSessionActive" in shell
        and "this._mobileScrollWriteEcho" in shell
        and 'root.addEventListener("scroll",this._onMobileSurfaceScroll,{passive:true})' in shell
        and "this._scheduleMobileScrollIdle();" in shell
        and 'root.style.overflowAnchor="none"' not in shell,
        "Aurora shell must stay inside native HA chrome, own mobile scrolling and preserve refresh position without overriding touch or momentum scrolling",
    )
    check(
        'const mobileIcons=["mdi:view-dashboard-outline","mdi:chart-timeline-variant","mdi:tune-variant","mdi:solar-power-variant","mdi:battery-high","mdi:lightning-bolt","mdi:cash-multiple"]' in shell
        and 'class="mobile-icon" aria-hidden="true"' in shell
        and 'min-height:64px' in shell
        and 'height:42px' in shell
        and 'env(safe-area-inset-bottom)' in shell,
        "Aurora mobile navigation must expose seven large iOS-style icon targets including Earnings with safe-area spacing",
    )

    check(
        "event.composedPath().includes(this)" in shell
        and "this._setMenuOpen(false);" in shell,
        "Aurora overflow menu must close on a pointer event outside its shadow host",
    )
    check(
        'event.key!=="Escape"' in shell
        and "event.preventDefault();" in shell
        and "this._setMenuOpen(false,true);" in shell
        and "if(restoreFocus)button?.focus();" in shell,
        "Aurora overflow menu must close on Escape and restore focus to its trigger",
    )
    check(
        'event.target?.closest?.("a[href]")' in shell
        and "this._setMenuOpen(false);" in shell,
        "Aurora overflow menu must close as soon as either navigation link is chosen",
    )
    check(
        re.search(
            r"disconnectedCallback\(\)\{\s*this\._setMenuOpen\(false\);",
            shell,
        )
        is not None
        and "document.removeEventListener(\"pointerdown\",this._onDocumentPointerDown,true);" in shell
        and "document.removeEventListener(\"keydown\",this._onDocumentKeyDown,true);" in shell
        and "this._documentListening=false;" in shell,
        "Aurora app shell must close the menu and remove both global listeners when disconnected",
    )


def check_canonical_settings_navigation(by_path: dict[str, View]) -> None:
    """Keep manual-YAML dashboards navigable without strategy decoration."""

    settings = by_path["ustawienia-ems"]
    top_level = card_blocks_at_indent(settings.source, 6)
    check(len(top_level) == 1, "ustawienia-ems panel must have one top-level page card")
    if top_level:
        root = top_level[0]
        check(
            card_type(root) == "custom:hoymiles-aurora-variant-a-settings-page-card",
            "ustawienia-ems must use the single Variant A settings composition",
        )
        expected = {
            "sensor.hoymiles_hit_ems_supervisor",
            "input_select.hoymiles_ems_supervisor_mode",
            "input_select.hoymiles_ems_supervisor_profile",
            "input_button.hoymiles_ems_supervisor_master_stop",
            "binary_sensor.hoymiles_ems_execution_ready",
            "sensor.hoymiles_ems_hardware_mode",
            "binary_sensor.hoymiles_ems_control_conflict",
            "sensor.hoymiles_hit_ems_shared_inputs",
            "input_select.hoymiles_ems_inverter_rated_power_each",
            "input_text.hoymiles_ems_pv_forecast_today_entity",
            "input_text.hoymiles_ems_pv_forecast_tomorrow_entity",
            "input_text.hoymiles_ems_pv_forecast_day_3_entity",
            "input_boolean.hoymiles_ev_load_filter_enabled",
            "input_number.hoymiles_ev_charge_power",
            "input_text.hoymiles_ev_power_sensor",
            "input_number.hoymiles_ems_fallback_daily_home_load",
            "input_number.hoymiles_ems_pv_to_battery_efficiency",
            "input_number.hoymiles_ems_battery_to_home_efficiency",
            "input_boolean.hoymiles_ems_push_notifications_enabled",
            "input_text.hoymiles_ems_push_notify_target",
        }
        check(
            expected == set(entity_ids(root)),
            "Variant A settings must retain every shared, Supervisor and notification binding",
        )

    for path in SETTINGS_CONTEXT_PATHS[1:]:
        cards = card_blocks_at_indent(by_path[path].source, 6)
        if len(cards) == 1 and card_type(cards[0]) == "vertical-stack":
            cards = card_blocks_at_indent(cards[0], 10)
        check(bool(cards), f"{path} must contain cards")
        if not cards:
            continue
        check(
            card_type(cards[0]) == "custom:hoymiles-local-nav-card",
            f"{path} navigation must be the first top-level card",
        )
        check(yaml_field(cards[0], "group") == "settings", f"{path} nav group must be settings")
        check(yaml_field(cards[0], "current_path") == path, f"{path} nav must mark the current path")


def check_policy_settings_compaction(by_path: dict[str, View], card_source: str) -> None:
    """Keep policy settings readable while retaining every functional card."""

    rce = by_path["automatyka-ems"].source
    rce_settings = next(
        (
            block
            for block in card_blocks_at_indent(rce, 6)
            if yaml_field(block, "title") == "Sprzedaż dynamiczna"
        ),
        "",
    )
    check(
        0 <= rce.find("type: custom:hoymiles-rce-chart-card")
        < rce.find("        title: Sprzedaż dynamiczna"),
        "RCE price plan must precede the policy settings",
    )
    check(
        "position: sidebar" in rce_settings,
        "RCE policy settings must remain in the compact sidebar",
    )
    check(
        re.search(
            r"type: custom:hoymiles-aurora-disclosure-card\s+"
            r"title: Plan sprzedaży w skrócie",
            rce,
        )
        is not None,
        "RCE summary must be available as a collapsed Aurora disclosure",
    )
    check(
        re.search(
            r"type: custom:hoymiles-aurora-disclosure-card\s+"
            r"title: Przychód — ostatnie 30 dni[\s\S]+?"
            r"type: custom:hoymiles-responsive-stack-card",
            rce,
        )
        is not None,
        "RCE 30-day charts must be grouped in one Aurora disclosure",
    )

    tariff = by_path["ladowanie-taryfowe"].source
    tariff_settings = next(
        (
            block
            for block in card_blocks_at_indent(tariff, 6)
            if yaml_field(block, "title") == "Ładowanie taryfowe — ustawienia"
        ),
        "",
    )
    check(
        "position: sidebar" in tariff_settings,
        "tariff policy settings must remain in the compact sidebar",
    )
    for title in (
        "Plan, gotowość i wykonanie",
        "Rozliczenie — pomiar fizyczny niezweryfikowany",
    ):
        check(
            re.search(
                rf"type: custom:hoymiles-aurora-disclosure-card\s+"
                rf"title: {re.escape(title)}",
                tariff,
            )
            is not None,
            f"tariff section {title!r} must be collapsed into an Aurora disclosure",
        )
    manual_wrapper = next(
        (
            block
            for block in card_blocks_at_indent(tariff, 6)
            if "title: Tryb ręczny — strefy i ceny energii" in block
        ),
        "",
    )
    check(
        bool(manual_wrapper) and "position: sidebar" in manual_wrapper,
        "manual tariff controls must stay in the settings sidebar",
    )

    rcm = by_path["rcem-253v"].source
    rcm_settings = next(
        (
            block
            for block in card_blocks_at_indent(rcm, 6)
            if yaml_field(block, "title") == "Ochrona napięciowa — ustawienia"
        ),
        "",
    )
    check(
        "position: sidebar" in rcm_settings,
        "voltage-management settings must remain in the compact sidebar",
    )
    phase_index = rcm.find("title: Napięcia fazowe używane przez RCEm")
    history_index = rcm.find(
        "title: Napięcie L1/L2/L3 i średnia 10-minutowa — ostatnie 24 godziny"
    )
    state_index = rcm.find("title: Stan teraz")
    decision_index = rcm.find("title: Decyzja i najbliższy plan")
    check(
        0 <= phase_index < history_index < state_index < decision_index,
        "voltage-management view must read phases, history, state, then decision",
    )
    check(
        rcm.count("accent: warning") >= 8,
        "voltage-management disclosures must use the Aurora warning accent",
    )

    balance = by_path["ustawienia-balansowania"]
    balance_cards = card_blocks_at_indent(balance.source, 6)
    check(
        balance.subview
        and re.search(r"(?m)^    type:\s*panel\s*$", balance.source) is not None
        and len(balance_cards) == 1,
        "balancing must be one dedicated panel subview",
    )
    if balance_cards:
        check(
            card_type(balance_cards[0]) == "custom:hoymiles-aurora-variant-a-policy-settings-card"
            and yaml_field(balance_cards[0], "section") == "balance",
            "balancing subview must use the common Variant A policy-settings shell",
        )
    policy_settings = js_section(
        card_source,
        "class HoymilesAuroraVariantAPolicySettingsCard extends HTMLElement",
        'if (!customElements.get("hoymiles-aurora-variant-a-policy-settings-card"))',
    )
    check(
        "RECOVERY_REQUIRED" in policy_settings
        and "RESTORE_FAILED" in policy_settings
        and "data-recovery-alert" in policy_settings
        and 'this._href("diagnostyka")' in policy_settings,
        "balancing settings must expose both exact recovery failures and link to Service",
    )
    check(
        "script.hoymiles_start_battery_balancing" not in policy_settings
        and "script.hoymiles_stop_battery_balancing" not in policy_settings
        and "_cycleAction" not in policy_settings,
        "manual balancing actions must not exist in policy settings",
    )
    controls = by_path["sterowanie"].source
    check(
        controls.count("script.hoymiles_start_battery_balancing") == 1
        and controls.count("script.hoymiles_stop_battery_balancing") == 1
        and "Balansowanie magazynu — działania serwisowe" in controls
        and controls.count("confirmation:") >= 4,
        "manual balancing start/stop must exist once in Service controls with confirmation",
    )


def check_plan_view(view: View) -> None:
    card = "type: custom:hoymiles-aurora-variant-a-ems-page-card"
    check(view.source.count(card) == 1, "plan-automatyki must contain one Variant A EMS page card")
    check(
        "type: vertical-stack" not in view.source,
        "Variant A EMS must be one controlled composition rather than independent stacked cards",
    )
    check(
        "supervisor_entity: sensor.hoymiles_hit_ems_supervisor" in view.source,
        "Variant A EMS controls must be backed by the canonical Supervisor entity",
    )
    expected = {
        "sensor.hoymiles_hit_ems_supervisor",
        "input_select.hoymiles_ems_supervisor_mode",
        "input_select.hoymiles_ems_supervisor_profile",
        "input_boolean.hoymiles_ems_supervisor_allow_rce",
        "input_boolean.hoymiles_ems_supervisor_allow_tariff",
        "input_boolean.hoymiles_ems_supervisor_allow_rcm",
        "input_button.hoymiles_ems_supervisor_master_stop",
        "sensor.hoymiles_ems_baseline_energy_timeline",
        "sensor.hoymiles_hit_ems_supervisor_canonical_plan",
        "sensor.hoymiles_hit_tariff_charge_plan",
        "sensor.hoymiles_hit_tariff_automation_plan_timeline",
        "input_boolean.hoymiles_tariff_charge_active",
        "input_text.hoymiles_tariff_active_action",
        "sensor.hoymiles_hit_rcm_automation_plan_timeline",
        "input_boolean.hoymiles_rcm_active",
        "input_boolean.hoymiles_rcm_export_control_active",
        "input_boolean.hoymiles_rcm_pre_discharge_active",
        "sensor.hoymiles_ems_hardware_mode",
        "binary_sensor.hoymiles_ems_control_conflict",
        "binary_sensor.hoymiles_ems_execution_ready",
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_hit_battery_capacity",
        "sensor.hoymiles_hit_overview_battery_power",
        "sensor.hoymiles_hit_grid_voltage_l1",
        "input_boolean.hoymiles_battery_balancing_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "sensor.hoymiles_battery_balancing_status",
        "sensor.hoymiles_battery_balancing_next_run",
        "input_number.hoymiles_battery_balancing_hold_hours",
    }
    root = next(
        (block for block in card_blocks_at_indent(view.source, 6) if card_type(block) == "custom:hoymiles-aurora-variant-a-ems-page-card"),
        "",
    )
    check(
        expected == set(entity_ids(root)),
        "Variant A EMS must retain Supervisor, canonical plan, battery and balancing bindings",
    )


def check_pv_view(view: View) -> None:
    root = compact_page_root(view, "pv")
    if not root:
        return

    bindings = {
        "status_entity": "sensor.hoymiles_hit_pv_link_status",
        "power_entity": "sensor.hoymiles_hit_pv_total_power_direct",
        "today_energy_entity": "sensor.hoymiles_hit_pv_total_energy_today",
        "total_energy_entity": "sensor.hoymiles_hit_pv_total_energy",
        "remaining_forecast_entity": "sensor.hoymiles_solcast_forecast_remaining_today",
        "tomorrow_forecast_entity": "sensor.hoymiles_solcast_forecast_tomorrow",
    }
    for field, entity in bindings.items():
        check(yaml_field(root, field) == entity, f"PV {field} must bind {entity}")

    strings = yaml_sequence_section(root, "strings")
    check(
        re.findall(r"(?m)^          - name:\s*(PV[1-4])\s*$", strings) == ["PV1", "PV2", "PV3", "PV4"],
        "PV compact page must contain four ordered string tiles",
    )
    for number in range(1, 5):
        prefix = f"sensor.hoymiles_hit_pv{number}"
        expected_string = {
            prefix + "_voltage",
            prefix + "_current",
            prefix + "_power_direct",
            prefix + "_today_energy",
        }
        check(
            expected_string.issubset(set(entity_ids(strings))),
            f"PV{number} tile must retain voltage, current, power and daily energy",
        )

    external = yaml_sequence_section(root, "external")
    check(
        {
            "sensor.hoymiles_hit_overview_external_pv_total_power",
            "sensor.hoymiles_hit_external_pv_total_energy_today",
            "sensor.hoymiles_hit_external_pv_energy_total",
        }.issubset(set(entity_ids(external))),
        "PV compact page must retain power, daily energy and total energy for external PV",
    )
    gen = yaml_sequence_section(root, "gen")
    expected_gen = {
        "select.hoymiles_hit_gen_port_mode",
        "sensor.hoymiles_hit_overview_generator_active_power",
        "sensor.hoymiles_hit_generator_active_power_l1",
        "sensor.hoymiles_hit_generator_active_power_l2",
        "sensor.hoymiles_hit_generator_active_power_l3",
        "sensor.hoymiles_hit_generator_energy_total_today",
        "sensor.hoymiles_hit_generator_energy_total",
    }
    check(expected_gen.issubset(set(entity_ids(gen))), "PV GEN bind must retain mode, power, phases, daily and total energy")

    history = yaml_sequence_section(root, "history_entities")
    check(yaml_field(root, "history_hours") == "24", "PV source chart must cover 24 hours")
    check(yaml_field(root, "history_value_mode") == "power", "PV source chart must normalize source power to kW")
    check(yaml_field(root, "history_digits") == "2", "PV source chart must retain compact kW precision")
    check(
        entity_ids(history)
        == [
            "sensor.hoymiles_hit_pv1_power_direct",
            "sensor.hoymiles_hit_pv2_power_direct",
            "sensor.hoymiles_hit_pv3_power_direct",
            "sensor.hoymiles_hit_pv4_power_direct",
            "sensor.hoymiles_hit_overview_generator_active_power",
        ],
        f"PV chart must contain exactly PV1-PV4 and GEN, got {entity_ids(history)}",
    )

    periods = keyed_blocks_at_indent(yaml_sequence_section(root, "period_cards"), 10)
    check(
        [block_key(block) for block in periods]
        == ["energy30d", "energy12m", "energyArchive"],
        "PV period selector must expose ordered 30-day, 12-month and annual archive charts",
    )
    pv_period_counters = [
        "sensor.hoymiles_hit_pv1_total_energy",
        "sensor.hoymiles_hit_pv2_total_energy",
        "sensor.hoymiles_hit_pv3_total_energy",
        "sensor.hoymiles_hit_pv4_total_energy",
        "sensor.hoymiles_hit_generator_energy_total",
    ]
    for index, (period, days) in enumerate((("day", "30"), ("month", "365"))):
        if index >= len(periods):
            break
        check(yaml_field(periods[index], "type") == "statistics-graph", "PV period card must use recorder statistics")
        check(yaml_field(periods[index], "period") == period, f"PV period {index} must aggregate by {period}")
        check(yaml_field(periods[index], "days_to_show") == days, f"PV period {index} must cover {days} days")
        check(
            entity_ids(periods[index]) == pv_period_counters,
            f"PV period {index} must contain exactly four string counters and GEN",
        )
    if len(periods) >= 3:
        check(yaml_field(periods[2], "type") == "statistics-graph", "PV archive must use recorder statistics")
        check(yaml_field(periods[2], "period") == "year", "PV archive must aggregate by year")
        check(yaml_field(periods[2], "days_to_show") == "3650", "PV archive must retain ten years")
        check(
            entity_ids(periods[2]) == ["sensor.hoymiles_hit_pv_total_energy"],
            "PV archive must retain the authoritative total-production counter",
        )

    details = yaml_sequence_section(root, "detail_cards")
    detail_cards = card_blocks_at_indent(details, 10)
    check(len(detail_cards) == 2, "PV compact page must keep the two non-duplicated Variant A disclosures")
    check(
        all(card_type(block) == "custom:hoymiles-aurora-disclosure-card" for block in detail_cards),
        "PV detail_cards must contain only Aurora disclosures",
    )
    check(
        re.findall(r"(?m)^            title:\s*(.+?)\s*$", details)
        == ["Energia stringów i przepływy", "Zewnętrzne PV i złącze GEN"],
        "PV disclosures must follow the exact Variant A order",
    )
    check(
        "select.hoymiles_hit_gen_port_mode" in entity_ids(details),
        "PV GEN disclosure must retain the configured source mode",
    )
    expected_counters = {
        "sensor.hoymiles_hit_pv1_today_energy",
        "sensor.hoymiles_hit_pv2_today_energy",
        "sensor.hoymiles_hit_pv3_today_energy",
        "sensor.hoymiles_hit_pv4_today_energy",
        "sensor.hoymiles_hit_pv_total_energy",
        "sensor.hoymiles_hit_pv1_total_energy",
        "sensor.hoymiles_hit_pv2_total_energy",
        "sensor.hoymiles_hit_pv3_total_energy",
        "sensor.hoymiles_hit_pv4_total_energy",
        "sensor.hoymiles_hit_pv_to_battery_energy_total",
        "sensor.hoymiles_hit_pv_to_load_energy_total",
        "sensor.hoymiles_hit_pv_to_grid_energy_total",
        "sensor.hoymiles_hit_pv_power",
        "sensor.hoymiles_hit_pv_total_power_energy_block",
        "sensor.hoymiles_hit_pv1_power_energy_block",
        "sensor.hoymiles_hit_pv2_power_energy_block",
        "sensor.hoymiles_hit_pv3_power_energy_block",
        "sensor.hoymiles_hit_pv4_power_energy_block",
    }
    check(
        expected_counters.issubset(set(entity_ids(details))),
        "PV details must retain all string, flow and raw-block counters",
    )


def check_load_view(view: View) -> None:
    root = compact_page_root(view, "energy")
    if not root:
        return

    bindings = {
        "production_today_entity": "sensor.hoymiles_hit_pv_total_energy_today",
        "consumption_today_entity": "sensor.hoymiles_hit_load_energy_use_today",
        "import_today_entity": "sensor.hoymiles_hit_grid_energy_buy_today",
        "export_today_entity": "sensor.hoymiles_hit_grid_energy_sell_today",
    }
    for field, entity in bindings.items():
        check(yaml_field(root, field) == entity, f"energy {field} must bind {entity}")

    groups = yaml_sequence_section(root, "groups")
    check(
        re.findall(r"(?m)^          - key:\s*(pv|load|grid|settlement)\s*$", groups)
        == ["pv", "load", "grid", "settlement"],
        "energy compact page must use PV, home, grid and settlement groups",
    )
    group_entities = {
        "sensor.hoymiles_hit_overview_pv_total_power",
        "sensor.hoymiles_actual_load_power",
        "sensor.hoymiles_hit_load_active_power_direct",
        "sensor.hoymiles_hit_overview_grid_total_active_power",
        "sensor.hoymiles_rce_realized_revenue_today",
        "sensor.hoymiles_hit_pv_total_energy_today",
        "sensor.hoymiles_hit_pv_to_grid_energy_today",
    }
    check(group_entities.issubset(set(entity_ids(groups))), "energy groups must retain live PV, home, grid and settlement binds")
    check(
        "kind: self_consumption_percent" in groups
        and "export_entity: sensor.hoymiles_hit_pv_to_grid_energy_today" in groups
        and "sensor.hoymiles_rce_pv_self_consumption_today" not in groups,
        "energy settlement must calculate a true PV self-consumption percentage from production and PV export",
    )

    history = yaml_sequence_section(root, "history_entities")
    check(yaml_field(root, "history_hours") == "24", "energy balance chart must cover 24 hours")
    check(yaml_field(root, "history_value_mode") == "power", "energy balance chart must use power semantics")
    check(
        entity_ids(history)
        == [
            "sensor.hoymiles_hit_overview_pv_total_power",
            "sensor.hoymiles_actual_load_power",
            "sensor.hoymiles_hit_overview_battery_power",
        ],
        f"energy 24-hour balance entities are {entity_ids(history)}",
    )
    check(yaml_field(root, "history_visual_mode") == "energy_mix", "energy chart must use the Variant A energy_mix renderer")

    periods = keyed_blocks_at_indent(yaml_sequence_section(root, "period_cards"), 10)
    check(
        [block_key(block) for block in periods] == ["energy30d", "energy12m"],
        "energy period selector must expose ordered 30-day and 12-month charts",
    )
    energy_period_counters = [
        "sensor.hoymiles_hit_pv_total_energy",
        "sensor.hoymiles_actual_load_energy_total",
        "sensor.hoymiles_hit_grid_energy_buy_total",
        "sensor.hoymiles_hit_grid_energy_sell_total",
    ]
    for index, (period, days) in enumerate((("day", "30"), ("month", "365"))):
        if index >= len(periods):
            break
        check(yaml_field(periods[index], "type") == "statistics-graph", "energy period card must use recorder statistics")
        check(yaml_field(periods[index], "period") == period, f"energy period {index} must aggregate by {period}")
        check(yaml_field(periods[index], "days_to_show") == days, f"energy period {index} must cover {days} days")
        check(
            entity_ids(periods[index]) == energy_period_counters,
            f"energy period {index} must contain exactly production, consumption, import and export",
        )

    details = yaml_sequence_section(root, "detail_cards")
    detail_cards = card_blocks_at_indent(details, 10)
    check(len(detail_cards) == 2, "energy compact page must keep history and technical data in two disclosures")
    check(
        all(card_type(block) == "custom:hoymiles-aurora-disclosure-card" for block in detail_cards),
        "energy detail_cards must contain only Aurora disclosures",
    )
    check("type: custom:hoymiles-aurora-history-card" in details, "energy details must retain the phase-load 24-hour chart")
    check(
        "type: statistics-graph" not in details
        and any(block_key(block) == "energy30d" for block in periods),
        "energy 30-day consumption must live once in the period selector, not as a native disclosure duplicate",
    )
    expected_technical = {
        "sensor.hoymiles_hit_load_power_l1n",
        "sensor.hoymiles_hit_load_power_l2n",
        "sensor.hoymiles_hit_load_power_l3n",
        "sensor.hoymiles_hit_backup_voltage_l1",
        "sensor.hoymiles_hit_backup_voltage_l2",
        "sensor.hoymiles_hit_backup_voltage_l3",
        "sensor.hoymiles_hit_backup_current_l1",
        "sensor.hoymiles_hit_backup_current_l2",
        "sensor.hoymiles_hit_backup_current_l3",
        "sensor.hoymiles_hit_backup_apparent_power",
        "sensor.hoymiles_hit_backup_apparent_power_l1",
        "sensor.hoymiles_hit_backup_apparent_power_l2",
        "sensor.hoymiles_hit_backup_apparent_power_l3",
        "sensor.hoymiles_hit_backup_active_power",
        "sensor.hoymiles_hit_backup_active_power_l1",
        "sensor.hoymiles_hit_backup_active_power_l2",
        "sensor.hoymiles_hit_backup_active_power_l3",
        "sensor.hoymiles_hit_load_power_total",
        "sensor.hoymiles_hit_load_energy_use_total",
        "sensor.hoymiles_hit_load_energy_use_l1n_total",
        "sensor.hoymiles_hit_load_energy_use_l2n_total",
        "sensor.hoymiles_hit_load_energy_use_l3n_total",
        "sensor.hoymiles_hit_load_energy_use_l1n_today",
        "sensor.hoymiles_hit_load_energy_use_l2n_today",
        "sensor.hoymiles_hit_load_energy_use_l3n_today",
        "sensor.hoymiles_actual_load_energy_total",
    }
    check(
        expected_technical.issubset(set(entity_ids(root))),
        "energy page must retain all phase, EPS, raw-energy and history entities",
    )


def check_battery_view(view: View) -> None:
    root = compact_page_root(view, "battery")
    if not root:
        return

    bindings = {
        "soc_entity": "sensor.hoymiles_hit_battery_soc_bms",
        "overview_soc_entity": "sensor.hoymiles_hit_overview_battery_soc",
        "power_entity": "sensor.hoymiles_hit_battery_power_bms",
        "overview_power_entity": "sensor.hoymiles_hit_overview_battery_power",
        "status_entity": "sensor.hoymiles_hit_battery_link_status",
        "charge_today_entity": "sensor.hoymiles_hit_battery_charge_energy_today",
        "discharge_today_entity": "sensor.hoymiles_hit_battery_discharge_energy_today",
        "voltage_entity": "sensor.hoymiles_hit_battery_voltage_bms",
        "current_entity": "sensor.hoymiles_hit_battery_current_bms",
        "temperature_entity": "sensor.hoymiles_hit_cell_max_temperature",
        "soh_entity": "sensor.hoymiles_hit_battery_soh",
        "capacity_entity": "sensor.hoymiles_hit_battery_capacity",
        "reserve_entity": "sensor.hoymiles_hit_ems_self_use_soc_readback",
        "raw_power_entity": "sensor.hoymiles_hit_battery_total_power_energy_block",
        "shared_inputs_entity": "sensor.hoymiles_hit_ems_shared_inputs",
    }
    for field, entity in bindings.items():
        check(yaml_field(root, field) == entity, f"battery {field} must bind {entity}")
    check(
        yaml_field(root, "settings_path") == "ustawienia-balansowania",
        "battery page must link directly to the dedicated balancing settings subview",
    )

    balancing = yaml_sequence_section(root, "balancing")
    balancing_entities = {
        "input_boolean.hoymiles_battery_balancing_enabled",
        "input_number.hoymiles_battery_balancing_interval_days",
        "input_number.hoymiles_battery_balancing_hold_hours",
        "sensor.hoymiles_battery_balancing_status",
        "sensor.hoymiles_battery_balancing_next_run",
    }
    check(balancing_entities == set(entity_ids(balancing)), "battery balancing bind must retain all controls and status")

    history = yaml_sequence_section(root, "history_entities")
    secondary = yaml_sequence_section(root, "secondary_history_entities")
    check(yaml_field(root, "history_hours") == "24", "battery power history must cover 24 hours")
    check(yaml_field(root, "history_value_mode") == "power", "battery chart must normalize power to kW")
    check(yaml_field(root, "history_digits") == "2", "battery chart must keep compact kW precision")
    check(
        entity_ids(history)
        == [
            "sensor.hoymiles_hit_overview_battery_power",
            "sensor.hoymiles_hit_overview_battery_soc",
        ],
        f"battery_soc history must contain ordered power and SOC series, got {entity_ids(history)}",
    )
    check(yaml_field(root, "history_visual_mode") == "battery_soc", "battery chart must use the Variant A battery_soc renderer")
    check(entity_ids(secondary) == [], "battery SOC must not be duplicated in a second legacy chart")

    technical_bms = yaml_sequence_section(root, "technical_bms_entities")
    expected_technical_bms = {
        "sensor.hoymiles_hit_battery_capacity",
        "sensor.hoymiles_hit_total_capacity",
        "sensor.hoymiles_hit_charge_cut_off_voltage",
        "sensor.hoymiles_hit_discharge_cut_off_voltage",
        "sensor.hoymiles_hit_maximum_charge_current",
        "sensor.hoymiles_hit_maximum_discharge_current",
        "sensor.hoymiles_hit_cell_max_temperature",
        "sensor.hoymiles_hit_cell_min_temperature",
        "sensor.hoymiles_hit_battery_power_bms",
        "sensor.hoymiles_hit_battery_total_power_energy_block",
        "sensor.hoymiles_hit_battery_soh",
        "sensor.hoymiles_hit_battery_link_status",
    }
    check(
        set(entity_ids(technical_bms)) == expected_technical_bms,
        "battery page must retain the complete technical BMS set behind one compact disclosure",
    )

    details = yaml_sequence_section(root, "detail_cards")
    detail_cards = card_blocks_at_indent(details, 10)
    check(len(detail_cards) == 3, "battery compact page must keep controls, explanation and BMS data in three disclosures")
    check(
        all(card_type(block) == "custom:hoymiles-aurora-disclosure-card" for block in detail_cards),
        "battery detail_cards must contain only Aurora disclosures",
    )
    native_control_rows = set(re.findall(r"(?m)^\s+- entity:\s*((?:input_boolean|input_number)\.[a-z0-9_]+)\s*$", details))
    check(
        {
            "input_boolean.hoymiles_battery_balancing_enabled",
            "input_number.hoymiles_battery_balancing_interval_days",
            "input_number.hoymiles_battery_balancing_hold_hours",
        }.issubset(native_control_rows),
        "battery details must retain native balancing toggle and number controls",
    )
    expected_bms = {
        "sensor.hoymiles_hit_battery_capacity",
        "sensor.hoymiles_hit_total_capacity",
        "sensor.hoymiles_hit_charge_cut_off_voltage",
        "sensor.hoymiles_hit_discharge_cut_off_voltage",
        "sensor.hoymiles_hit_maximum_charge_current",
        "sensor.hoymiles_hit_maximum_discharge_current",
        "sensor.hoymiles_hit_cell_max_temperature",
        "sensor.hoymiles_hit_cell_min_temperature",
        "sensor.hoymiles_hit_ems_shared_inputs",
    }
    check(expected_bms.issubset(set(entity_ids(details))), "battery details must retain BMS limits and fresh power evidence")
    check("type: custom:hoymiles-aurora-history-card" in details, "battery details must retain the 7-day cell-temperature chart")


def check_compact_page_frontend(card_source: str) -> None:
    """Guard the one reusable renderer used by PV, battery and energy panels."""

    class_token = "class HoymilesAuroraCompactPageCard extends HTMLElement"
    registration_token = 'if (!customElements.get("hoymiles-aurora-compact-page-card"))'
    compact_source = js_section(card_source, class_token, registration_token)
    check(card_source.count(class_token) == 1, "frontend must define one Aurora compact page class")
    check(
        len(
            re.findall(
                r"customElements\.define\(\s*[\"']hoymiles-aurora-compact-page-card[\"']\s*,\s*"
                r"HoymilesAuroraCompactPageCard\s*\)",
                card_source,
                flags=re.DOTALL,
            )
        )
        == 1,
        "frontend must register the Aurora compact page exactly once",
    )
    check(
        '["pv", "battery", "energy"].includes(page)' in compact_source,
        "compact renderer must explicitly support exactly the three redesigned pages",
    )
    check(
        'document.createElement("hoymiles-aurora-history-card")' in compact_source
        and "history_visual_mode" in compact_source
        and "this._config.history_hours" in compact_source
        and "this._config.history_value_mode" in compact_source
        and "this._config.history_unit" in compact_source
        and "this._config.history_digits" in compact_source
        and "this._config.secondary_history_hours" in compact_source
        and "this._config.secondary_history_value_mode" in compact_source
        and "this._config.secondary_history_unit" in compact_source
        and "this._config.secondary_history_digits" in compact_source,
        "compact renderer must mount recorder history and honor the configured mixed-chart contracts",
    )
    period_tabs = js_section(compact_source, "  _periodTabs()", "  _pvMarkup()")
    check(
        'data-period="${hoymilesEscape(period.key)}"' in period_tabs
        and "data-entity" not in period_tabs,
        "PV and energy period buttons must select charts instead of opening entity details",
    )
    check(
        "_historyRenderVersion" in compact_source
        and "renderVersion !== this._historyRenderVersion" in compact_source
        and "this._periodKey !== period.key" in compact_source
        and "host.replaceChildren()" in compact_source
        and "_mountPeriodHistory" in compact_source,
        "period chart replacement must reject stale asynchronous card mounts",
    )
    check(
        'row.kind === "self_consumption_percent"' in compact_source
        and "this._energyValue(row.export_entity)" in compact_source
        and "exported > production" in compact_source,
        "energy renderer must calculate self-consumption as a fail-closed percentage",
    )
    check(
        'document.createElement("hoymiles-aurora-disclosure-card")' in compact_source
        and "detail_cards" in compact_source,
        "compact renderer must mount nested technical disclosures",
    )
    check(
        "group.rows" in compact_source and "group.entities" in compact_source,
        "energy renderer must consume real configured group rows",
    )
    check(
        'return this._config?.overview_power_entity || this._config?.power_entity || null;' in compact_source,
        "battery summary must prefer aggregate system power and use direct BMS power only as fallback",
    )
    check(
        'class="bms-technical"' in compact_source
        and "this._config.technical_bms_entities" in compact_source
        and 'data-bms-extra="${index}"' in compact_source,
        "battery composition must keep complete BMS data inside one compact technical disclosure",
    )
    check(
        'aria-label="${hoymilesEscape(copy.stringsPower)}"' in compact_source
        and 'aria-label="${hoymilesEscape(this._copy().expand)}"' in compact_source,
        "compact PV and energy controls must expose localized accessible labels",
    )
    check(
        'typeof value === "number"' in compact_source
        and "sample?.fresh === true" in compact_source
        and "Number.isFinite(value)" in compact_source,
        "BMS power availability must remain fail-closed for null, empty and non-numeric samples",
    )


def check_overview(view: View, card_source: str) -> None:
    overview_card_token = "type: custom:hoymiles-aurora-overview-card"
    check(view.source.count(overview_card_token) == 1, "overview must contain exactly one Aurora Compact composition card")
    check(
        re.search(r"(?m)^    type:\s*panel\s*$", view.source) is not None,
        "overview must use one full-width Lovelace panel root",
    )
    check("custom:hoymiles-power-flow-card" not in view.source, "overview must not restore the old power-flow card")
    check(re.search(r"<\s*/?\s*svg\b", view.source, flags=re.IGNORECASE) is None, "overview must not embed duplicate SVG")
    check("custom:hoymiles-aurora-status-card" not in view.source, "overview must replace the full status card with compact conditional health")
    check("custom:hoymiles-aurora-ems-card" not in view.source, "overview must use the read-only compact EMS brief")
    check(
        len(re.findall(r"class\s+HoymilesAuroraEnergyCard\s+extends\s+HTMLElement", card_source)) == 1,
        "frontend must define the existing HoymilesAuroraEnergyCard exactly once",
    )
    registration = re.compile(
        r"customElements\.define\(\s*[\"']hoymiles-aurora-energy-card[\"']\s*,\s*"
        r"HoymilesAuroraEnergyCard\s*\)",
        flags=re.DOTALL,
    )
    check(len(registration.findall(card_source)) == 1, "Aurora energy card must be registered exactly once")

    cards = card_blocks_at_indent(view.source, 6)
    overview_cards = [block for block in cards if card_type(block) == "custom:hoymiles-aurora-overview-card"]
    check(len(cards) == 1 and len(overview_cards) == 1, "overview panel must have one composite root and no loose cards")
    if overview_cards:
        overview = overview_cards[0]
        bindings = {
            "forecast_today_entity": "sensor.hoymiles_solcast_forecast_today",
            "forecast_remaining_entity": "sensor.hoymiles_solcast_forecast_remaining_today",
            "forecast_tomorrow_entity": "sensor.hoymiles_solcast_forecast_tomorrow",
            "average_load_entity": "sensor.hoymiles_load_average_4_days",
            "cav_temperature_entity": "sensor.hoymiles_hit_cav_temp",
            "battery_path_temperature_entity": "sensor.hoymiles_hit_bat_ths_temp",
            "battery_capacity_entity": "sensor.hoymiles_hit_battery_capacity",
            "battery_current_entity": "sensor.hoymiles_hit_battery_current_bms",
            "load_today_entity": "sensor.hoymiles_actual_load_energy_today",
            "grid_export_today_entity": "sensor.hoymiles_hit_grid_energy_sell_today",
            "supervisor_entity": "sensor.hoymiles_hit_ems_supervisor",
            "canonical_timeline_entity": "sensor.hoymiles_hit_ems_supervisor_canonical_plan",
            "readiness_entity": "binary_sensor.hoymiles_ems_execution_ready",
            "clear_fault_entity": "button.hoymiles_hit_clear_fault",
        }
        for field, entity in bindings.items():
            check(yaml_field(overview, field) == entity, f"overview {field} must bind {entity}")
        history_match = re.search(
            r"(?ms)^        history_entities:\s*\n(?P<body>(?:^          .+\n?)+)",
            overview,
        )
        check(history_match is not None, "overview must configure recorder history entities")
        history_source = history_match.group("body") if history_match else ""
        check(
            entity_ids(history_source)
            == [
                "sensor.hoymiles_hit_overview_pv_total_power",
                "sensor.hoymiles_actual_load_power",
                "sensor.hoymiles_hit_overview_grid_total_active_power",
                "sensor.hoymiles_hit_overview_battery_power",
            ],
            f"overview 24-hour chart entities are {entity_ids(history_source)}",
        )

    overview_source = js_section(
        card_source,
        "class HoymilesAuroraOverviewCard extends HTMLElement",
        'if (!customElements.get("hoymiles-aurora-overview-card"))',
    )
    check(
        len(re.findall(r"class\s+HoymilesAuroraOverviewCard\s+extends\s+HTMLElement", card_source)) == 1,
        "frontend must define the Aurora Compact overview composition exactly once",
    )
    overview_registration = re.compile(
        r"customElements\.define\(\s*[\"']hoymiles-aurora-overview-card[\"']\s*,\s*"
        r"HoymilesAuroraOverviewCard\s*\)",
        flags=re.DOTALL,
    )
    check(len(overview_registration.findall(card_source)) == 1, "Aurora Compact overview must be registered exactly once")
    check(re.search(r"<\s*/?\s*svg\b", overview_source, flags=re.IGNORECASE) is None, "overview wrapper must reuse and never duplicate the Aurora SVG")
    check(
        'document.createElement("hoymiles-aurora-energy-card")' in overview_source
        and 'layout: "flow_summary"' in overview_source,
        "overview wrapper must reuse the canonical Aurora canvas with its four forecast summaries",
    )
    check(
        'document.createElement("hoymiles-aurora-history-card")' in overview_source
        and "hours_to_show: 24" in overview_source
        and 'value_mode: "power"' in overview_source
        and 'layout: "overview"' in overview_source,
        "overview wrapper must retain compact real recorder history",
    )
    ordered_tokens = [
        'class="page-heading"',
        'class="overview-grid"',
        'class="overview-kpis"',
        "data-history-host",
    ]
    positions = [overview_source.find(token) for token in ordered_tokens]
    check(all(position >= 0 for position in positions) and positions == sorted(positions), f"overview visual hierarchy changed: {positions}")
    check(
        'class="heading-actions"><span class="state-pill"' in overview_source
        and ".heading-actions { align-items: center; display: flex;" in overview_source,
        "overview readiness pill must keep the prototype heading grouping instead of drifting to the far edge",
    )
    check(
        "_statusIssues()" in overview_source and "data-status-alert hidden" in overview_source,
        "overview must retain health and alarm evidence without permanent visual clutter",
    )
    check(
        'this._hass.callService("button", "press", { entity_id: entityId })' in overview_source
        and 'data-copy="clear-fault"' in overview_source,
        "overview alarm banner must retain the inverter fault-clear action without healthy-state clutter",
    )
    check(
        "@container (max-width: 1180px)" in overview_source
        and "@container (max-width: 820px)" in overview_source
        and ".status-alert { align-items: flex-start; flex-wrap: wrap; }" in overview_source,
        "overview must retain the prototype-aligned desktop/tablet breakpoints and a wrapping mobile alert",
    )

    history_component_source = js_section(
        card_source,
        "class HoymilesAuroraHistoryCard extends HTMLElement",
        'if (!customElements.get("hoymiles-aurora-history-card"))',
    )
    check(
        'const width = overviewLayout ? 1120 : 720;' in history_component_source
        and 'const height = pvSourcesLayout || mixedLayout ? 300 : overviewLayout ? 268 : 360;' in history_component_source
        and 'ha-card[data-layout="overview"] .chart { aspect-ratio: 1120 / 268; height: auto; }'
        in history_component_source,
        "overview history must retain the compact prototype geometry without changing other history views",
    )
    check(
        '["pv_sources", "energy_mix", "battery_soc"].includes(config?.visual_mode)' in history_component_source
        and '.series-line[data-dashed="true"] { stroke-dasharray: 8 5; }' in history_component_source
        and 'ha-card[data-visual-mode="pv_sources"] .chart,ha-card[data-visual-mode="energy_mix"] .chart,ha-card[data-visual-mode="battery_soc"] .chart { aspect-ratio:1120 / 300; }'
        in history_component_source,
        "PV, battery_soc and energy_mix history must share the compact Variant A geometry",
    )
    check(
        'nextConfig.visual_mode === "energy_mix"' in history_component_source
        and 'nextConfig.visual_mode === "battery_soc"' in history_component_source
        and 'this._timeWeightedBins(rawPoints, startTime, endTime, 48)' in history_component_source
        and 'coverage / binDuration >= 0.5' in history_component_source
        and 'const batteryPath = this._segmentedPath(battery, x, y);' in history_component_source
        and 'const socPath = this._segmentedPath(soc, x, ySoc);' in history_component_source,
        "mixed charts must enforce exact series counts, time-weighted bins and visible recorder gaps",
    )
    check(
        'const displayValue = -point.value;' in history_component_source
        and 'const color = point.value < 0 ? "#37d991" : "#ff617d";' in history_component_source
        and 'class="axis secondary-axis"' in history_component_source,
        "battery_soc must invert only presentation, distinguish charge/discharge and keep SOC on the right axis",
    )
    check(
        "--hoymiles-aurora-text: #f3f7fa;" in history_component_source
        and "--hoymiles-aurora-muted: #8fa4b5;" in history_component_source
        and ".series-endpoint" in history_component_source,
        "overview history must remain Aurora-dark and show live series endpoints independently of the HA theme",
    )
    history_render_gate = js_section(
        history_component_source,
        "  _render() {",
        "    const copy = this._copy();",
    )
    check(
        "this._patchHistoryLiveValues();" in history_render_gate
        and "last_updated" not in history_render_gate
        and "state?.state" not in history_render_gate
        and 'data-history-live-value="${hoymilesEscape(series.entity)}"' in history_component_source,
        "live telemetry must patch the stable history legend instead of replacing the full chart DOM",
    )

    energy_source = js_section(
        card_source,
        "class HoymilesAuroraEnergyCard extends HTMLElement",
        'if (!customElements.get("hoymiles-aurora-energy-card"))',
    )
    check(
        all(
            f'data-key="{key}"' in energy_source
            for key in (
                "forecast_today",
                "forecast_remaining",
                "forecast_tomorrow",
                "average_load",
            )
        )
        and 'ha-card[data-layout="flow-summary"] .insights { margin-top: 0; }' in energy_source
        and 'ha-card[data-layout="flow-summary"] .header,' in energy_source
        and 'ha-card[data-layout="flow-summary"] .daily,' in energy_source
        and 'ha-card[data-layout="flow-summary"] .grid-import { display: none; }' in energy_source
        and 'ha-card[data-layout="flow-summary"] .insights,' not in energy_source
        and 'grid-template-columns: repeat(2, minmax(0, 1fr))' in energy_source,
        "overview Aurora must expose four forecast/load tiles above the unchanged animation and keep a 2x2 phone layout",
    )
    paths = re.findall(
        r'<path class="flow" data-flow="([a-z]+)" d="([^"]+)"/?>',
        energy_source,
    )
    check(
        paths
        == [
            ("pv", "M-40 28 C230 28 220 202 450 210"),
            ("grid", "M450 210 C660 205 680 32 940 56"),
            ("home", "M450 210 C250 240 210 425 -40 386"),
            ("battery", "M450 210 C675 245 685 425 950 390"),
        ],
        f"Aurora animation paths changed: {paths}",
    )
    check(
        'class="core-temperature cav"' in energy_source
        and 'data-value="cav_temperature"' in energy_source,
        "Aurora animation must show inverter temperature",
    )
    check(
        'class="core-temperature battery-path"' in energy_source
        and 'data-value="battery_path_temperature"' in energy_source,
        "Aurora animation must show battery-path temperature",
    )
    check(
        'data-value="battery_detail"' in energy_source
        and "storedKwh" in energy_source
        and "currentA" in energy_source
        and " kWh`" in energy_source
        and " A`" in energy_source,
        "Aurora battery tile must retain stored-energy and current data",
    )
    check(
        'data-value="battery_eta"' in energy_source
        and "_formatBatteryEta" in energy_source
        and "etaHours" in energy_source,
        "Aurora battery tile must retain charge/discharge time estimation",
    )
    check(
        all(
            token in energy_source
            for token in (
                '{ label: "PV1", entity: "sensor.hoymiles_hit_pv1_power_direct" }',
                '{ label: "PV2", entity: "sensor.hoymiles_hit_pv2_power_direct" }',
                '{ label: "PV3", entity: "sensor.hoymiles_hit_pv3_power_direct" }',
                '{ label: "PV4", entity: "sensor.hoymiles_hit_pv4_power_direct" }',
                '{ label: "GEN", entity: "sensor.hoymiles_hit_overview_generator_active_power" }',
                'data-value="pv_sources"',
            )
        )
        and "HOYMILES_AURORA_PV_TELEMETRY_MAX_AGE_SECONDS = 300" in card_source
        and "state?.last_reported" in energy_source
        and "state?.last_updated" in energy_source
        and "state?.last_changed" in energy_source
        and "ageSeconds < -5" in energy_source
        and "ageSeconds > HOYMILES_AURORA_PV_TELEMETRY_MAX_AGE_SECONDS" in energy_source
        and '["unknown", "unavailable", "none", "null", "nan"].includes(raw)' in energy_source
        and ".filter((item) => item.label && Number.isFinite(item.powerKw) && item.powerKw > 0.02)" in energy_source,
        "Aurora PV tile must list only fresh PV1-PV4/GEN sources producing strictly above 0.02 kW",
    )
    check(
        all(
            token in energy_source
            for token in (
                '{ label: "L1", entity: "sensor.hoymiles_hit_backup_active_power_l1" }',
                '{ label: "L2", entity: "sensor.hoymiles_hit_backup_active_power_l2" }',
                '{ label: "L3", entity: "sensor.hoymiles_hit_backup_active_power_l3" }',
                'data-value="eps_phases"',
                "value: this._formatBreakdownPower(powerKw)",
            )
        )
        and 'if (!Number.isFinite(value)) return "—";' in energy_source
        and '.metric-breakdown[hidden] { display: none; }' in energy_source
        and '.metric-breakdown { font-size: 7.5px;' in energy_source,
        "Aurora Home tile must keep compact EPS L1-L3 rows and render stale/unknown phases as an em dash on mobile",
    )


def check_service_view(view: View) -> None:
    navigation_paths = re.findall(
        r"(?m)^\s+navigation_path:\s*/hoymiles-falownik/([a-z0-9-]+)\s*$",
        view.source,
    )
    check(
        navigation_paths == SERVICE_PATHS,
        f"service navigation paths are {navigation_paths}",
    )
    check(
        re.search(r"(?m)^    type:\s*panel\s*$", view.source) is not None,
        "service view must use a full-width panel root",
    )
    top_level = card_blocks_at_indent(view.source, 6)
    check(len(top_level) == 1, "service panel must contain one top-level card")
    check(
        bool(top_level) and card_type(top_level[0]) == "vertical-stack",
        "service panel must use one vertical stack",
    )
    nested = card_blocks_at_indent(top_level[0], 10) if top_level else []
    check(
        len(nested) >= 9
        and card_type(nested[0]) == "custom:hoymiles-local-nav-card"
        and card_type(nested[1]) == "custom:hoymiles-diagnostics-download-card",
        "service stack must begin with local navigation and diagnostic download",
    )
    disclosure_titles = [
        yaml_field(block, "title")
        for block in nested
        if card_type(block) == "custom:hoymiles-aurora-disclosure-card"
    ]
    check(
        disclosure_titles
        == [
            "EMS i automatyka",
            "Diagnostyka techniczna",
            "Skrócone rejestry mocy",
            "Falownik — fazy, moc i magistrala DC",
            "Parametry ochronne i temperatury",
            "Komunikacja — ESP32 i Wi-Fi",
            "Sieć równoległa — podsumowanie",
            "Wszystkie adresy sieci równoległej",
        ],
        f"service disclosure order is {disclosure_titles}",
    )
    check(
        len(nested) > 2 and nested[2].count("open: true") == 0,
        "service shortcuts and technical sections must start collapsed",
    )
    check(
        all(
            card_type(block) != "custom:hoymiles-zebra-entities-card"
            for block in nested
        ),
        "service technical entity cards must not remain exposed at stack level",
    )


def check_variant_a_compositions(card_source: str) -> None:
    """Freeze the accepted Variant A page geometry and safe helper-only controls."""

    ems = js_section(
        card_source,
        "class HoymilesAuroraVariantAEmsPageCard extends HTMLElement",
        'if (!customElements.get("hoymiles-aurora-variant-a-ems-page-card"))',
    )
    settings = js_section(
        card_source,
        "class HoymilesAuroraVariantASettingsPageCard extends HTMLElement",
        'if (!customElements.get("hoymiles-aurora-variant-a-settings-page-card"))',
    )
    policy_settings = js_section(
        card_source,
        "class HoymilesAuroraVariantAPolicySettingsCard extends HTMLElement",
        'if (!customElements.get("hoymiles-aurora-variant-a-policy-settings-card"))',
    )
    service = js_section(
        card_source,
        "class HoymilesAuroraVariantAServicePageCard extends HTMLElement",
        'if(!customElements.get("hoymiles-aurora-variant-a-service-page-card"))',
    )
    check(
        card_source.count("class HoymilesAuroraVariantAEmsPageCard extends HTMLElement") == 1
        and card_source.count("class HoymilesAuroraVariantASettingsPageCard extends HTMLElement") == 1,
        "Variant A EMS and settings compositions must each be defined exactly once",
    )
    check(
        "function hoymilesExecutionHealthContract(state)" in card_source
        and "health.controlStatus === \"healthy\"" in ems
        and "health.controlStatus === \"healthy\"" in settings,
        "main EMS and Settings controls must consume the same backend health contract",
    )
    check(
        'const recovering = health.controlStatus === "recovering";' in ems
        and 'const intervention = health.controlStatus === "unhealthy";' in ems
        and "controlReady" in ems,
        "main EMS controls must fail closed for unknown, unhealthy, and recovery states",
    )
    check(
        '[{ ...downloadCard, compact: true }]' in card_source
        and 'compactButton: "Pobierz pakiet diagnostyczny ZIP"' in card_source
        and 'compactButton: "Download diagnostic ZIP"' in card_source,
        "Variant A Service must use the compact diagnostic ZIP action in both languages",
    )
    check(
        'id.split(".")[1]' not in service,
        "Variant A Service status tiles must not expose raw Home Assistant entity IDs",
    )
    ems_order = [
        ems.find('class="page-heading"'),
        ems.find('class="control-strip card"'),
        ems.find('class="summary-line card"'),
        ems.find('class="chart-card card"'),
        ems.find('class="day-plan"'),
    ]
    check(
        all(position >= 0 for position in ems_order) and ems_order == sorted(ems_order),
        f"Variant A EMS hierarchy changed: {ems_order}",
    )
    controls = [
        ems.find('data-action="mode"'),
        ems.find('_policyControl("rce"'),
        ems.find('_policyControl("tariff"'),
        ems.find('_policyControl("voltage"'),
        ems.find('_policyControl("balance"'),
        ems.find('data-action="stop"'),
    ]
    check(
        all(position >= 0 for position in controls) and controls == sorted(controls),
        f"Variant A six-control strip changed: {controls}",
    )
    ems_mount = js_section(ems, "  _mount() {", "  _syncHistoryView() {")
    check(
        'data-action="profile"' not in ems_mount
        and 'class="mode-select"' not in ems_mount,
        "Variant A must hide no-op strategy/profile controls from the main strip",
    )
    check(
        "grid-template-columns:190px repeat(4,minmax(142px,1fr)) 118px" in ems
        and "grid-template-columns:1fr 1.18fr 1fr" in ems
        and "const width = 1120;" in ems
        and "const height = 480;" in ems
        and 'class="soc-action-segment"' in ems
        and 'class="soc-action-glass"' in ems
        and 'class="grid-flow-bar"' in ems,
        "Variant A EMS must retain the accepted controls and enlarged action-aware chart geometry",
    )
    check(
        all(token in ems for token in ('_dayCard("rcm"', '_dayCard("tariff"', '_dayCard("rce"', '_dayCard("balance"'))
        and "grid-template-columns:repeat(4,minmax(0,1fr))" in ems
        and "@container (max-width:1180px)" in ems
        and "@container (max-width:820px)" in ems
        and "@container (max-width:620px)" in ems,
        "Variant A EMS must retain four compact day-plan cards and responsive breakpoints",
    )
    check(
        'tariff_plan_entity: "sensor.hoymiles_hit_tariff_charge_plan"' in card_source
        and "function hoymilesVerifiedTariffNoChargePlan" in card_source
        and 'planAttributes.status_code !== "no_charge_needed"' in card_source
        and "!hoymilesRceTimestampFresh(planReportedAt, nowMs)" in card_source
        and "!hoymilesRceTimestampFresh(timelineAttributes.generated_at, nowMs)" in card_source
        and 'timelineAttributes.policy_id !== "tariff"' in card_source
        and 'timelineAttributes.input_revision !== inputRevision' in card_source
        and 'point.action_code === "idle"' in card_source
        and 'tariffNoNeed: "Brak potrzeby doładowania — PV i bateria wystarczą"' in ems
        and 'tariffNoImport: "0,0 kWh poboru"' in ems
        and "this._retainedTariffNoChargeAt = Date.now();" in ems
        and "Date.now() - this._retainedTariffNoChargeAt <= 15 * 60_000" in ems
        and "const tariffEvidenceCurrent = canonicalCurrent" in ems
        and "else if (tariffEvidenceCurrent || (!canonicalCurrent && !canonicalRetained && !canonicalPending))" in ems
        and "(canonicalRetained && summaries.tariff.valid && summaries.tariff.selected.length === 0)" in ems
        and "(canonicalCurrent && !tariffEvidenceCurrent)" in ems
        and 'tariffNoCharge ? "no-charge" : planStates.tariff ? "source-status" : "default"' in ems
        and '[data-plan-status="no-charge"] small,.day-plan-card[data-plan-status="source-status"] small' in ems,
        "Variant A EMS must show verified no-charge tariff results as completed plans, retain only a previously verified result during recalculation, and fail closed for stale or incoherent evidence",
    )
    check(
        'const current = entity?.state === "current"' in ems
        and 'const retained = allowRetained' in ems
        and 'entity?.state === "pending"' in ems
        and 'attrs.result_current === false' in ems
        and 'attrs.recalculation_pending === true' in ems
        and 'attrs.timeline_kind !== "baseline_self_use"' in ems
        and "attrs.output_only !== true" in ems
        and "attrs.authority !== false" in ems
        and "HoymilesAutomationPlannerCanonicalCard.prototype" in ems
        and "._normalizeCanonicalTimeline.call(this, entity)" in ems
        and 'const canonicalSlots = this._canonicalSlots();' in ems
        and 'const canonicalCurrent = Array.isArray(canonicalSlots);' in ems
        and 'const retainedSlots = canonicalCurrent ? null : this._canonicalSlots("retained");' in ems
        and 'const displayPlanSlots = canonicalCurrent' in ems
        and '? canonicalPlanSlots' in ems
        and '? retainedSlots' in ems
        and 'const baselineSlots = canonicalCurrent || canonicalRetained ? [] : this._baselineSlots();' in ems
        and 'const chartSlots = canonicalCurrent ? canonicalPlanSlots : canonicalRetained ? retainedSlots : baselineSlots;' in ems
        and 'this._policySummary("rce", displayPlanSlots, canonicalRetained)' in ems
        and 'const nextSlot = displayPlanSlots.find(' in ems
        and 'const chartSource = canonicalCurrent ? "current" : canonicalRetained ? "retained" : baselineSlots.length ? "baseline" : "unavailable";' in ems
        and "chart.dataset.source = chartSource;" in ems
        and 'attrs.canonical_blocker_code === "source_recalculation_pending"' in ems
        and 'chart.innerHTML = this._chartSvg(this._chartSlots, false);' in ems
        and 'this._setAttribute(chartPlanStatus, "aria-hidden", !canonicalRetained);' in ems
        and 'class="chart-subtitle-reserve" aria-hidden="true">${copy.retainedSubtitle}' in ems
        and 'data-chart-plan-status aria-hidden="true">${copy.chartRetained}' in ems
        and "Przeliczanie — pokazano ostatni plan; brak prawa do nowego wykonania." in ems
        and "Prognoza bazowa PV, zużycia i SOC" in ems
        and "this._stopConfirmUntil = Date.now() + 6000" in ems
        and 'this._service("stop", "input_button", "press"' in ems
        and 'this._service("stop", "hoymiles_hit_modbus", "resume_after_master_stop", {})' in ems,
        "Variant A EMS must use output-only baseline only for the chart, keep canonical decisions fail closed, and retain two-step MASTER STOP",
    )
    chart_click_handler = js_section(
        ems,
        "  _handleClick(event) {",
        "  async _service(",
    )
    chart_selection = js_section(
        ems,
        "  _selectChartSlot(",
        "  _handleChartKeyDown(",
    )
    desktop_stage_rule = re.search(
        r"\.chart-stage\s*\{(?P<body>[^}]*)\}",
        ems,
    )
    desktop_inspector_rule = re.search(
        r"\.chart-inspector\s*\{(?P<body>[^}]*)\}",
        ems,
    )
    desktop_stage_css = desktop_stage_rule.group("body") if desktop_stage_rule else ""
    desktop_inspector_css = (
        desktop_inspector_rule.group("body") if desktop_inspector_rule else ""
    )
    inspector_column_match = re.search(
        r"grid-template-columns\s*:\s*minmax\(0,1fr\)\s+([0-9.]+)px",
        desktop_stage_css,
    )
    inspector_column_px = (
        float(inspector_column_match.group(1)) if inspector_column_match else 0.0
    )
    mobile_ems_css_match = re.search(
        r"@container\s*\(max-width:820px\)\s*\{(?P<body>.*?)"
        r"@container\s*\(max-width:620px\)",
        ems,
        flags=re.DOTALL,
    )
    mobile_ems_css = mobile_ems_css_match.group("body") if mobile_ems_css_match else ""
    mobile_inspector_rule = re.search(
        r"\.chart-inspector\s*\{(?P<body>[^}]*)\}",
        mobile_ems_css,
    )
    mobile_inspector_css = (
        mobile_inspector_rule.group("body") if mobile_inspector_rule else ""
    )
    mobile_min_height_match = re.search(
        r"min-height\s*:\s*([0-9.]+)px",
        mobile_inspector_css,
    )
    mobile_min_height_px = (
        float(mobile_min_height_match.group(1)) if mobile_min_height_match else 0.0
    )
    check(
        all(
            token in ems
            for token in (
                "data-chart-slot",
                "data-chart-inspector",
                "_selectChartSlot(index, focus = false)",
                "_updateChartSelection()",
                "_handleChartKeyDown(event)",
                "this._selectedChartStart",
                "this._chartSource",
                'aria-live="polite"',
            )
        )
        and all(key in ems for key in ('"Enter"', '" "', '"ArrowLeft"', '"ArrowRight"'))
        and chart_click_handler.find("[data-chart-slot]") >= 0
        and chart_click_handler.find("[data-action]") >= 0
        and chart_click_handler.find("[data-chart-slot]")
        < chart_click_handler.find("[data-action]")
        and "_service(" not in chart_selection
        and "callService" not in chart_selection
        and 340 <= inspector_column_px <= 480
        and re.search(r"overflow(?:-y)?\s*:\s*(?:auto|scroll)\b", desktop_inspector_css) is None
        and "pointer-events:auto; position:static; touch-action:pan-y; width:auto;" in ems
        and '.chart-inspector[aria-hidden="true"]' in ems
        and ".chart-stage { grid-template-columns:minmax(0,1fr); }" in ems
        and mobile_min_height_px >= 180
        and re.search(r"overflow(?:-y)?\s*:\s*(?:auto|scroll)\b", mobile_inspector_css) is None,
        "Variant A EMS SOC inspection must keep the enlarged desktop/mobile panel without internal scrolling or acquiring execution authority",
    )
    check(
        'class="chart-day-jumps" data-chart-day-jumps role="group"' in ems
        and 'data-chart-day="today"' in ems
        and 'data-chart-day="tomorrow"' in ems
        and 'class="chart-viewport" data-chart-viewport' in ems
        and '.chart-day-jumps button' in ems
        and 'min-height:44px' in ems
        and '.chart-viewport { -webkit-overflow-scrolling:touch;' in ems
        and 'overflow-x:auto; overflow-y:hidden;' in ems
        and '.chart-viewport .energy-chart { height:500px;' in ems
        and 'min-width:1120px; width:1120px;' in ems
        and '.chart-inspector { min-height:96px;' in ems
        and '.chart-inspector[data-visible="true"] { min-height:500px;' in ems
        and 'const previousScrollLeft = previousViewport' in ems
        and 'viewport.scrollLeft = Math.max(' in ems
        and 'this._setChartDay(chartDay);' in chart_click_handler,
        "Variant A EMS must preserve its desktop chart while exposing a legible, swipeable 48-hour chart with Today/Tomorrow jumps on phones",
    )
    check(
        'class="plan-band"' not in ems
        and 'data-plans-track' not in ems
        and "const bandHeight" not in ems
        and "const plansBottom" not in ems,
        "Variant A EMS must omit the duplicated PLANY track after action-aware SOC glass was added",
    )
    check(
        'class="plan-legend" data-plan-legend' in ems
        and ems.count('data-plan-policy="rcm"') == 1
        and ems.count('data-plan-policy="tariff"') == 1
        and ems.count('data-plan-policy="rce"') == 1
        and ".plan-legend" in ems
        and re.search(r"\.plan-legend\s*\{[^}]*display\s*:\s*none", ems) is None,
        "Variant A EMS must show one always-visible RCE/Tariff/Voltage legend instead of labels on every plan band",
    )
    check(
        "const flowBottom" in ems
        and 'height="${flowBottom - top}"' in ems
        and 'data-chart-slot="${index}"' in ems,
        "Variant A EMS slot hit areas must extend through the energy-flow track so a plan interval is tappable",
    )
    check(
        "hoymilesPlannerWarsawMidnightAfter(start, 1)" in ems
        and 'data-midnight-boundary="true"' in ems
        and 'class="day-boundary-line"' in ems
        and "`${copy.chartToday} | ${copy.chartTomorrow}`" in ems
        and '>00:00</text>' in ems
        and ".day-boundary-line" in ems
        and "stroke:#ff315b" in ems
        and "drop-shadow(0 0 5px rgba(255,49,91,.96))" in ems
        and ".day-boundary { pointer-events:none; }" in ems,
        "Variant A EMS must mark local midnight with a non-interactive red-neon Today | Tomorrow boundary on the shared desktop/mobile SVG",
    )
    check(
        "socStart:" in ems
        and "slot.socStart" in ems
        and "slot.soc" in ems
        and '"current"' in ems
        and '"retained"' in ems
        and '"baseline"' in ems
        and "chartCanonical" in ems
        and "chartRetained" in ems
        and "chartBaseline" in ems,
        "Variant A EMS inspector must use exact slot SOC boundaries and identify current, retained, and baseline sources",
    )
    check(
        'this._service("mode", "hoymiles_hit_modbus", "set_ems_paused"' in ems
        and 'this._service(action, "hoymiles_hit_modbus", "set_policy_enabled"' in ems,
        "Variant A controls must use the paired integration services",
    )
    check(
        'status: "sent"' in ems
        and '"confirmed"' in ems
        and '"partial"' in ems
        and '"unknown"' in ems
        and "_settleOperationEvidence" in ems
        and "The operation was not fully confirmed. Check the current EMS state." in ems
        and "Installation state was not changed" not in card_source
        and "Stan instalacji nie został zmieniony" not in card_source,
        "Variant A must reread bounded state and report sent, confirmed, partial, or unknown outcomes without an unchanged-installation promise",
    )
    for forbidden in ('"modbus.', '"toggle"', "number.set_value", "select.select_option"):
        check(forbidden not in ems, f"Variant A EMS contains forbidden direct-control scope: {forbidden}")

    check(
        'action === "rce_export" || action === "rcm_pre_discharge"' in card_source
        and '? ["soc_equation", "battery_to_grid_kwh"]' in card_source
        and 'action?.startsWith("rcm_")' not in card_source,
        "Overview may show RCEm energy only for canonical pre-discharge, never synthetic energy for every RCEm action",
    )

    settings_order = [
        settings.find('class="page-heading"'),
        settings.find('class="settings-layout"'),
        settings.find('class="settings-menu card"'),
        settings.find('class="settings-content"'),
        settings.find('class="technical card"'),
    ]
    check(
        all(position >= 0 for position in settings_order)
        and settings_order[:2] == sorted(settings_order[:2])
        and settings_order[2] < settings_order[3] < settings_order[4],
        f"Variant A settings hierarchy changed: {settings_order}",
    )
    for name, section in (("general", settings), ("policy", policy_settings)):
        coffee = section.find('class="support-coffee"')
        diagnostics = section.find('data-settings-diagnostics')
        check(
            coffee >= 0
            and diagnostics >= 0
            and coffee < diagnostics
            and section.count('href="https://buycoffee.to/kaluzaaa"') == 1
            and section.count('target="_blank"') == 1
            and section.count('rel="noopener noreferrer"') == 1,
            f"Variant A {name} settings must mount exactly one safe BuyCoffee link before diagnostics",
        )
        check(
            ".support-coffee:focus-visible" in section
            and "min-height:" in section
            and "Postaw kawę autorowi" in section
            and "Support the author" in section
            and "Jeśli EMS Ci pomaga, wesprzyj jego dalszy rozwój." in section
            and "If EMS helps you, support its continued development." in section
            and ".support-coffee strong" in section
            and ".support-coffee small" in section,
            f"Variant A {name} settings BuyCoffee link must be accessible and bilingual",
        )
    check(
        "grid-template-columns:244px minmax(0,1fr)" in settings
        and "position:sticky" in settings
        and "top:76px" in settings
        and "grid-template-columns:minmax(240px,1fr) minmax(190px,320px)" in settings
        and "min-height:58px" in settings,
        "Variant A settings must retain the accepted sticky sidebar and row layout",
    )
    check(
        "@container (min-width:621px)" in settings
        and ".eyebrow { font-size:14px; }" in settings
        and ".state-pill { font-size:15px; }" in settings
        and ".settings-nav strong { font-size:16px; }" in settings
        and ".settings-nav small { font-size:13px; }" in settings
        and ".settings-card-head h2 { font-size:20px; }" in settings
        and ".setting-copy strong { font-size:16px; }" in settings
        and ".setting-copy small { font-size:14px; }" in settings
        and "select,input { font-size:16px; min-height:44px; }" in settings
        and ".readback { font-size:15px; }" in settings
        and ".stop-button,.error { font-size:15px; }" in settings
        and ".technical summary strong { font-size:16px; }" in settings
        and ".technical summary small { font-size:14px; }" in settings
        and "@container(min-width:621px)" in policy_settings
        and ".eyebrow{font-size:14px}" in policy_settings
        and ".state-pill{font-size:15px}" in policy_settings
        and ".settings-nav strong{font-size:16px}" in policy_settings
        and ".settings-nav small{font-size:13px}" in policy_settings
        and ".settings-card-head h2{font-size:20px}" in policy_settings
        and ".status-grid span{font-size:14px}" in policy_settings
        and ".status-grid strong{font-size:17px}" in policy_settings
        and ".setting-copy strong{font-size:16px}" in policy_settings
        and ".setting-copy small{font-size:14px}" in policy_settings
        and "select,input{font-size:16px;min-height:44px}" in policy_settings
        and ".readback{font-size:15px}" in policy_settings
        and ".cycle-actions button,.error{font-size:15px}" in policy_settings
        and ".advanced>summary strong{font-size:16px}" in policy_settings
        and ".advanced>summary small{font-size:14px}" in policy_settings
        and "@container (max-width:1180px)" in settings
        and "@container(max-width:1180px)" in policy_settings,
        "Variant A general and policy settings must enlarge all desktop/tablet typography without changing the phone rules",
    )
    check(
        ".nav::-webkit-scrollbar{display:none}" in card_source
        and ".nav{-ms-overflow-style:none;gap:5px;overflow:auto;scrollbar-width:none}" in card_source
        and ".rce-price-scroll::-webkit-scrollbar { display: none; }" in card_source
        and "scrollbar-width: none" in card_source,
        "Variant A horizontal navigation and RCE chart must remain swipeable without native scrollbar chrome",
    )
    check(
        'data-control="execution"' in settings
        and 'data-control="strategy"' in settings
        and 'data-control="forecastToday"' in settings
        and 'data-control="pvEfficiency"' in settings
        and 'data-action="push"' in settings
        and 'class="technical card"' in settings,
        "Variant A settings must keep essential controls visible and raw diagnostics collapsed",
    )
    check(
        "this._stopConfirmUntil = Date.now() + 6000" in settings
        and 'this._service("stop", "input_button", "press"' in settings
        and 'this._service(key, "input_text", "set_value"' in settings
        and 'this._service(key, "input_number", "set_value"' in settings,
        "Variant A settings must use existing helpers and retain two-step MASTER STOP",
    )
    check(
        'execution_readiness_entity: "binary_sensor.hoymiles_ems_execution_ready"' in card_source
        and 'readiness_entity: "binary_sensor.hoymiles_ems_execution_ready"' in card_source
        and 'const ready = noConflict && executionReady;' in card_source
        and 'const controlReady = executionReady && modeAvailable && noConflict && controlHealthy;' in card_source
        and 'const ready = controlReady && canonicalCurrent;' in card_source
        and 'const preparing = controlReady && canonicalPending;' in card_source
        and 'return executionReady&&conflictState==="off"&&this._healthContract(supervisor).controlStatus==="healthy";' in card_source
        and "noConflict && modeReady && executionReady && controlHealthy" in settings
        and "gotowość wymaga świeżej generacji FC03" in settings
        and "Nie realizuje Q(U), P(U) ani PF" in card_source,
        "Variant A settings must not claim physical/control readiness without the execution gate and must state the voltage-management boundary",
    )
    for forbidden in ("modbus", "hoymiles_hit_modbus", '"toggle"', "number.set_value", "select.select_option"):
        check(forbidden not in settings, f"Variant A settings contains forbidden direct-control scope: {forbidden}")


def check_generated_english_dashboard() -> None:
    source = EN_DASHBOARD_PATH.read_text(encoding="utf-8")
    generated = json.loads(source)
    titles = {view.get("path"): view.get("title") for view in generated.get("views", [])}
    check(titles == ENGLISH_VIEW_TITLES, f"generated English view titles are {titles}")

    def without_polish_metadata(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: without_polish_metadata(child)
                for key, child in value.items()
                if not str(key).endswith("_pl")
            }
        if isinstance(value, list):
            return [without_polish_metadata(child) for child in value]
        return value

    # The compact cards intentionally carry paired *_pl/*_en metadata and select
    # only *_en in this generated dashboard. Validate everything that can render.
    visible_source = json.dumps(
        without_polish_metadata(generated), ensure_ascii=False, sort_keys=True
    )
    check(
        re.search(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", visible_source) is None,
        "generated English dashboard must not retain visible Polish diacritics",
    )
    forbidden_hybrids = (
        "Charging taryfowe",
        "Tariff charging — diagnostyka",
        "przyready",
        "Runić",
        "finish aktywn",
        "Meters i",
        "Balancing magazynu",
        "Magazyn — ostatnie",
        "Moc magazynu",
        "Poziom magazynu",
        '"name": "Eksport"',
        "EMS wybiera",
        "Plan i wykonanie",
        "Poranne discharge",
        '"title": "Serwis"',
        "EMS i automatyka",
        "Stan i decyzje EMS",
        "Diagnostyka techniczna",
    )
    for token in forbidden_hybrids:
        check(token not in visible_source, f"generated English dashboard contains hybrid copy: {token}")


def main() -> int:
    dashboard = DASHBOARD_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    card_source = CARD_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")

    views = check_views(dashboard, card_source)
    check_settings_navigation(card_source)
    check_app_shell_return_menu(card_source)
    check_canonical_settings_navigation(views)
    check_policy_settings_compaction(views, card_source)
    check_compact_page_frontend(card_source)
    check_variant_a_compositions(card_source)
    for path, probe in (
        ("plan-automatyki", check_plan_view),
        ("pv", check_pv_view),
        ("load-eps", check_load_view),
        ("bateria", check_battery_view),
    ):
        if path in views:
            probe(views[path])
    if "start" in views:
        check_overview(views["start"], card_source)
    if "diagnostyka" in views:
        check_service_view(views["diagnostyka"])
    check_generated_english_dashboard()

    if FAILURES:
        print(f"FAIL Aurora compact dashboard Variant A: {len(FAILURES)} of {CHECKS} checks failed")
        for failure in FAILURES:
            print(f"- {failure}")
        return 1
    print(f"PASS Aurora compact dashboard Variant A: {CHECKS} static checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
