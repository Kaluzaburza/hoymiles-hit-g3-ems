"""Static contract for Aurora styling inside expandable legacy content.

The dashboard keeps all legacy diagnostic information, but native Home Assistant
cards mounted inside an Aurora disclosure must not reintroduce a second card
surface.  This test intentionally checks the shared composition path instead of
enumerating page-specific CSS overrides.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js"
DASHBOARD = ROOT / "dashboard_hoymiles.yaml"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


def compact(source: str) -> str:
    """Remove insignificant whitespace for formatting-independent checks."""

    return re.sub(r"\s+", " ", source)


def has_embedded_decorator_call(source: str) -> bool:
    """Return true when a mount decorates a config with embedded presentation."""

    normalized = compact(source)
    return bool(
        re.search(
            r"hoymilesDecorateCard\s*\(.{0,900}?embedded\s*:\s*true",
            normalized,
        )
    )


def yaml_view(source: str, path: str) -> str:
    """Return one dashboard view without requiring a YAML dependency."""

    match = re.search(
        rf"(?ms)^  - title:[^\n]*\n    path: {re.escape(path)}\n.*?(?=^  - title:|\Z)",
        source,
    )
    check(match is not None, f"Dashboard view {path!r} is missing")
    return match.group(0)


def yaml_indented_block(source: str, key: str, indent: int) -> str:
    """Return a mapping/list value up to the next peer or parent key."""

    marker = f"{' ' * indent}{key}:"
    lines = source.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.startswith(marker)), None)
    check(start is not None, f"YAML block {key!r} is missing")
    finish = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        leading = len(line) - len(line.lstrip(" "))
        if leading <= indent:
            finish = index
            break
    return "".join(lines[start:finish])


def main() -> None:
    source = CARD.read_text(encoding="utf-8")
    dashboard = DASHBOARD.read_text(encoding="utf-8")
    decorator = section(
        source,
        "function hoymilesDecorateCard",
        "const HOYMILES_LOCAL_NAV_GROUPS",
    )
    frame = section(
        source,
        "class HoymilesAuroraFrameCard",
        'if (!customElements.get("hoymiles-aurora-frame-card"))',
    )
    disclosure = section(
        source,
        "class HoymilesAuroraDisclosureCard",
        'if (!customElements.get("hoymiles-aurora-disclosure-card"))',
    )
    disclosure_flat = compact(disclosure)
    zebra = section(
        source,
        "class HoymilesZebraEntitiesCard",
        'if (!customElements.get("hoymiles-zebra-entities-card"))',
    )
    compact_page = section(
        source,
        "class HoymilesAuroraCompactPageCard",
        'if (!customElements.get("hoymiles-aurora-compact-page-card"))',
    )
    policy = section(
        source,
        "class HoymilesAuroraVariantAPolicySettingsCard",
        'if (!customElements.get("hoymiles-aurora-variant-a-policy-settings-card"))',
    )
    service = section(
        source,
        "class HoymilesAuroraVariantAServicePageCard",
        'if(!customElements.get("hoymiles-aurora-variant-a-service-page-card"))',
    )
    service_composition = section(
        source,
        "function hoymilesAuroraServiceCard",
        "function hoymilesDecorateDashboard",
    )

    decorator_flat = compact(decorator)
    check(
        re.search(r"card\.card.{0,500}?hoymilesDecorateCard", decorator_flat)
        is not None,
        "Aurora decorator must recurse through a nested card property",
    )
    check(
        re.search(r"card\.cards.{0,500}?hoymilesDecorateCard", decorator_flat)
        is not None,
        "Aurora decorator must recurse through cards collections",
    )
    check(
        "detail_cards" in decorator
        and re.search(
            r"detail_cards.{0,900}?hoymilesDecorateCard",
            decorator_flat,
        )
        is not None,
        "Aurora decorator must recurse through compact-page detail_cards",
    )
    check(
        "period_cards" in decorator
        and re.search(
            r"period_cards.{0,900}?hoymilesDecorateCard",
            decorator_flat,
        )
        is not None,
        "Aurora decorator must recurse through period_cards",
    )
    check(
        "embedded" in decorator,
        "Aurora decorator must carry an explicit embedded presentation option",
    )

    check(
        "aurora_embedded" in zebra
        and "data-hoymiles-aurora-embedded" in zebra,
        "Zebra must expose a dedicated aurora_embedded mode and DOM state",
    )
    check(
        re.search(
            r"data-hoymiles-aurora-embedded[^\n{]*:not\(\[data-hoymiles-compact-manual\]\)",
            zebra,
        )
        is not None,
        "Embedded Zebra rules must explicitly exclude aurora_compact_manual",
    )
    zebra_flat = compact(zebra)
    check(
        re.search(
            r"data-hoymiles-aurora-embedded[^{}]*:not\(\[data-hoymiles-compact-manual\]\)[^{}]*#states\s*\{[^{}]*display\s*:\s*grid[^{}]*grid-template-columns\s*:\s*repeat\(2\s*,\s*minmax\(0\s*,\s*1fr\)\)",
            zebra_flat,
        )
        is not None,
        "Embedded non-manual Zebra must use a two-column Aurora grid on desktop",
    )
    check(
        re.search(
            r"@media\s*\(max-width\s*:\s*6[0-9]{2}px\).{0,1800}?data-hoymiles-aurora-embedded.{0,500}?grid-template-columns\s*:\s*minmax\(0\s*,\s*1fr\)",
            zebra_flat,
        )
        is not None,
        "Embedded non-manual Zebra must collapse to one column on mobile",
    )
    check(
        re.search(
            r"data-hoymiles-aurora-embedded.{0,900}?state-badge.{0,180}?display\s*:\s*none",
            zebra_flat,
        )
        is not None,
        "Embedded non-manual Zebra must hide native state badges",
    )

    for name, mount_source in (
        ("Disclosure", disclosure),
        ("CompactPage details", section(compact_page, "_mountDetails()", "_selectPeriod(")),
        ("Policy details", section(policy, "async _mountCards", "async _mountDetails")),
        ("Service details", section(service, "async _mountCards", "_patch()")),
    ):
        check(
            has_embedded_decorator_call(mount_source),
            f"{name} mount must pass nested card configuration through the decorator with embedded: true",
        )

    check(
        "data-hoymiles-aurora-embedded-style" in frame,
        "AuroraFrame must mark its idempotent embedded native-card style",
    )
    check(
        "--hoymiles-aurora-disclosure-accent: #58d6ff" in disclosure
        and "--hoymiles-aurora-surface: var(--hoymiles-aurora-disclosure-surface)" in disclosure
        and "--hoymiles-aurora-border: rgba(88, 214, 255, .22)" in disclosure
        and re.search(
            r"\.disclosure-button::before\s*\{[^{}]*background\s*:\s*var\(--hoymiles-aurora-disclosure-accent\)[^{}]*height\s*:\s*24px[^{}]*width\s*:\s*2px",
            disclosure_flat,
        )
        is not None,
        "Every shared disclosure must use the blue Aurora settings marker",
    )
    check(
        re.search(
            r"\.disclosure-button\s*\{[^{}]*background\s*:\s*rgba\(88\s*,\s*214\s*,\s*255\s*,\s*\.035\)[^{}]*min-height\s*:\s*54px",
            disclosure_flat,
        )
        is not None
        and ':host([data-expanded]) .disclosure-button' in disclosure,
        "Shared disclosure headers must use one blue Aurora surface and a 54 px touch target",
    )
    check(
        re.search(
            r"\.disclosure-affordance\s*\{[^{}]*background\s*:\s*rgba\(88\s*,\s*214\s*,\s*255\s*,\s*\.07\)[^{}]*height\s*:\s*30px[^{}]*width\s*:\s*30px",
            disclosure_flat,
        )
        is not None,
        "Shared disclosure chevrons must use the compact blue Aurora control",
    )
    frame_flat = compact(frame)
    check(
        re.search(r"background\s*:\s*transparent\s*!important", frame_flat)
        is not None
        and re.search(r"border\s*:\s*0(?:px)?\s*!important", frame_flat)
        is not None
        and re.search(r"box-shadow\s*:\s*none\s*!important", frame_flat)
        is not None,
        "Embedded AuroraFrame CSS must eliminate the native card's duplicate surface",
    )
    check(
        re.search(r"min-height\s*:\s*44px", compact(frame + zebra)) is not None,
        "Embedded controls must retain a minimum 44 px interaction target",
    )

    check(
        "aurora_compact_manual: true" in service_composition
        and "safe_off_banner: true" in service_composition,
        "Manual EMS must remain on its dedicated compact and safe-off presentation path",
    )
    check(
        "_applyCompactManualPresentation()" in zebra
        and "data-hoymiles-compact-manual" in zebra
        and "hoymilesUiExactSafeOffHandoff(this._hass" in zebra,
        "Embedded normalization must preserve compact-manual layout and its exact handoff gate",
    )

    service_flat = compact(service + service_composition)
    check(
        "quick_links" in service_composition
        and "quick_links" in service
        and re.search(r"class\s*=\s*[\"'`]quick-links", service) is not None,
        "Service composition must expose quick_links rendered by the Aurora service card",
    )
    check(
        "navigation_path" in service_composition
        and re.search(r"quick_links.{0,1800}?navigation_path", service_flat) is not None,
        "Service quick links must preserve their source navigation_path",
    )

    service_view = yaml_view(dashboard, "diagnostyka")
    expected_service_paths = {
        "/hoymiles-falownik/ems-supervisor",
        "/hoymiles-falownik/automatyka-ems",
        "/hoymiles-falownik/ladowanie-taryfowe",
        "/hoymiles-falownik/rcem-253v",
        "/hoymiles-falownik/stany-alarmy",
        "/hoymiles-falownik/sterowanie",
    }
    missing_paths = sorted(path for path in expected_service_paths if path not in service_view)
    check(
        not missing_paths,
        f"Service source lost navigation paths: {', '.join(missing_paths)}",
    )

    for path in ("pv", "load-eps"):
        view = yaml_view(dashboard, path)
        periods = yaml_indented_block(view, "period_cards", 8)
        details = yaml_indented_block(view, "detail_cards", 8)
        check(
            "type: statistics-graph" in periods,
            f"{path}: period selector must retain the canonical statistics data cards",
        )
        check(
            "type: statistics-graph" not in details,
            f"{path}: detail disclosures must not repeat native statistics-graph cards already available in period_cards",
        )

    print("Aurora disclosure embedded contract: PASS")
    print("  decorator recursion: card/cards/detail_cards/period_cards")
    print("  embedded mounts: Disclosure/CompactPage/Policy/Service")
    print("  native double surface removed; 44 px targets retained")
    print("  service quick links: compact Aurora grid with preserved navigation")
    print("  embedded Zebra: 2 desktop columns, 1 mobile column, badges hidden")
    print("  PV/Energy details: no redundant native statistics graphs")
    print("  compact manual EMS path remains isolated")


if __name__ == "__main__":
    main()
