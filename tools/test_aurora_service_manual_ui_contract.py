"""Static contract for the compact, native Aurora manual-EMS service panel."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js"


def section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    source = CARD.read_text(encoding="utf-8")
    service = section(
        source,
        "function hoymilesAuroraServiceCard",
        "function hoymilesDecorateDashboard",
    )
    zebra = section(
        source,
        "class HoymilesZebraEntitiesCard",
        'if (!customElements.get("hoymiles-zebra-entities-card"))',
    )

    check(
        "aurora_compact_manual: true" in service
        and "safe_off_banner: true" in service
        and "const manualEms = [compactManual].filter(Boolean);" in service,
        "Service must opt only the primary six-row EMS card into compact rendering",
    )
    check(
        service.count('"Tryb EMS falownika"') == 1
        and service.count('"SOC Self-Use"') == 1
        and service.count('"Force Charge SOC"') == 2
        and service.count('"Force Discharge SOC"') == 2
        and service.count('"Inverter EMS mode"') == 1,
        "Compact manual controls must retain six concise bilingual field labels",
    )
    check(
        "pełnego bloku 4300–4306" in service
        and "full-block 4300–4306" in service
        and "manualDescriptions[index]" in service,
        "Every compact native control must receive short bilingual guidance",
    )
    check(
        "const systemCards = [manual[3], manual[4], ...manual.slice(5)].filter(Boolean);"
        in service,
        "Safe-stop and system controls must remain retained in a collapsed disclosure",
    )
    for title in (
        "Falownik i magazyn",
        "Sieć, eksport i port GEN",
        "Ręczne harmonogramy",
        "System i powiadomienia",
        "Pełne dane pracy falownika",
        "Źródła zewnętrzne i liczniki",
        "Magazyn — dane techniczne",
    ):
        check(title in service, f"Retained Service disclosure is missing: {title}")

    check(
        "aurora_compact_manual: _auroraCompactManual" in zebra
        and "language: _language" in zebra,
        "Opt-in presentation keys must not leak into the native entities-card config",
    )
    check(
        'type: "entities"' in zebra
        and 'document.createElement("hui-entities-card")' in zebra,
        "Compact mode must keep Home Assistant's native entities card and controls",
    )
    check(
        'this._config?.aurora_compact_manual === true' in zebra
        and 'data-hoymiles-compact-manual' in zebra
        and (
            'card.toggleAttribute(' in zebra
            or 'setBooleanAttribute(' in zebra
        )
        and "_applyCompactManualPresentation()" in zebra,
        "Compact styling must remain explicit opt-in and idempotently reapplied",
    )
    check(
        "style[data-hoymiles-compact-native-style]" in zebra
        and "style[data-hoymiles-compact-generic-style]" in zebra,
        "Nested native shadow rows must receive one stable Aurora style each",
    )
    check(
        "grid-template-columns: minmax(220px, 1fr) minmax(210px, 360px)" in zebra
        and "@media (max-width: 620px)" in zebra
        and "grid-template-columns: minmax(0, 1fr)" in zebra,
        "Manual rows must be compact on desktop and single-column on mobile",
    )
    check(
        "hoymilesUiExactSafeOffHandoff(this._hass" in zebra
        and 'const gate = allowed ? "open" : "closed";' in zebra
        and "row.inert = !allowed" in zebra
        and 'row.setAttribute?.("aria-disabled", String(!allowed))' in zebra,
        "Compact presentation must retain the exact fail-closed handoff gate",
    )
    check(
        "data-hoymiles-safe-off-footer" in zebra
        and "Ręczne sterowanie jest zablokowane" in zebra
        and "Ręczne sterowanie jest dostępne" in zebra
        and "data-hoymiles-compact-write-badge" in zebra
        and '"Fizyczny zapis" : "Physical write"' in zebra,
        "The compact footer must expose the live safe-handoff state without replacing controls",
    )
    check(
        "callService" not in zebra and "hass.callWS" not in zebra,
        "The presentation wrapper must never introduce a second write path",
    )

    print("Aurora Service manual UI contract: PASS")
    print("  native controls retained: 6")
    print("  exact safe-off presentation gate retained")
    print("  desktop/mobile compact layout present")
    print("  remaining Service controls retained in disclosures")


if __name__ == "__main__":
    main()
