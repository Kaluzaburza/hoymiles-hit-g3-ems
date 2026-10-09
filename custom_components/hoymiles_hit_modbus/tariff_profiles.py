"""Official 2026 Polish household tariff profiles used by the TOU optimizer.

The profiles contain marginal gross prices only: energy, variable distribution,
quality, RES and cogeneration charges.  Fixed monthly/capacity fees are omitted
because they do not change when a battery charge is moved between time zones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


PROFILE_YEAR = 2026
PROFILE_DATA_VERSION = "2026.1"
PROFILE_VALID_FROM = date(PROFILE_YEAR, 1, 1)
PROFILE_VALID_UNTIL = date(PROFILE_YEAR, 12, 31)
MANUAL_OPERATOR = "Manual"
SUPPORTED_OPERATORS = ("PGE", "TAURON", "ENEA", "ENERGA", "STOEN")
SUPPORTED_GROUPS = ("G11", "G12", "G12w", "G12e", "G13")


@dataclass(frozen=True, slots=True)
class TariffProfile:
    """One DSO, incumbent supplier and household tariff combination."""

    operator: str
    operator_name: str
    supplier_name: str
    tariff_type: str
    g11_price_pln_kwh: float
    low_price_pln_kwh: float
    medium_price_pln_kwh: float
    peak_price_pln_kwh: float
    schedule_key: str
    weekend_low_price: bool
    polish_holidays_low_price: bool
    source_url: str
    valid_from: date
    valid_until: date
    data_version: str
    price_source_urls: tuple[str, ...] = ()


def _profile(
    operator: str,
    operator_name: str,
    supplier_name: str,
    tariff_type: str,
    g11: float,
    low: float,
    medium: float,
    peak: float,
    schedule: str,
    source_url: str,
    *,
    weekend_low: bool = False,
    holiday_low: bool = False,
    valid_from: date = PROFILE_VALID_FROM,
    data_version: str = PROFILE_DATA_VERSION,
    price_source_urls: tuple[str, ...] = (),
) -> TariffProfile:
    return TariffProfile(
        operator=operator,
        operator_name=operator_name,
        supplier_name=supplier_name,
        tariff_type=tariff_type,
        g11_price_pln_kwh=g11,
        low_price_pln_kwh=low,
        medium_price_pln_kwh=medium,
        peak_price_pln_kwh=peak,
        schedule_key=schedule,
        weekend_low_price=weekend_low,
        polish_holidays_low_price=holiday_low,
        source_url=source_url,
        valid_from=valid_from,
        valid_until=PROFILE_VALID_UNTIL,
        data_version=data_version,
        price_source_urls=price_source_urls,
    )


_PGE_SOURCE = (
    "https://pgedystrybucja.pl/strefa-klienta/energia-elektryczna/"
    "taryfy-i-cenniki"
)
_TAURON_SOURCE = (
    "https://www.tauron-dystrybucja.pl/uslugi-dystrybucyjne/"
    "strefy-czasowe"
)
_ENEA_SOURCE = "https://www.operator.enea.pl/uslugidystrybucyjne/taryfa"
_ENERGA_SOURCE = "https://energa-operator.pl/uslugi/taryfa"
_STOEN_SOURCE = "https://www.stoen.pl/strona/taryfa"
_PGE_G12E_SOURCE = (
    "https://pgedystrybucja.pl/uslugi-dystrybucyjne/"
    "taryfa-i-cenniki/taryfa-elastyczna"
)
_PGE_G12E_PRICE_SOURCES = (
    "https://www.gkpge.pl/content/download/33731b58d959b9d5a7df60579844f80e/file/cennik-podstawowy-g12e.pdf",
    "https://www.gkpge.pl/content/download/9d2e5e9068ef4f63ff716a5562d8df20/file/a5_wyciag_z_taryfy_osd_02_2026_web.pdf",
)


# Gross all-in marginal prices for 2026, PLN/kWh.  The energy component uses
# the incumbent supplier for the selected DSO.  Values are intentionally kept
# in code so a profile is deterministic and can be audited/released together
# with the integration.
_PROFILES: dict[tuple[str, str], TariffProfile] = {
    ("PGE", "G11"): _profile(
        "PGE", "PGE Dystrybucja S.A.", "PGE Obrót S.A.", "G11",
        1.0991, 1.0991, 1.0991, 1.0991, "flat", _PGE_SOURCE,
    ),
    ("PGE", "G12"): _profile(
        "PGE", "PGE Dystrybucja S.A.", "PGE Obrót S.A.", "G12",
        1.0991, 0.6111, 1.2490, 1.2490, "pge_g12", _PGE_SOURCE,
    ),
    ("PGE", "G12w"): _profile(
        "PGE", "PGE Dystrybucja S.A.", "PGE Obrót S.A.", "G12w",
        1.0991, 0.6845, 1.3015, 1.3015, "pge_g12", _PGE_SOURCE,
        weekend_low=True, holiday_low=True,
    ),
    # PGE Obrót G12e price list (2026-01-01), table 1, gross energy:
    # low 0.5005 / peak 0.8363. PGE DSO (2026-02-01), sections 7.9-7.12:
    # net network 0.0349 / 0.3851 + quality 0.0332 + RES 0.0073 + CHP 0.0030.
    # Add 23% VAT to distribution only; energy already includes VAT/excise.
    # Applies to the seven PGE branches listed at _PGE_G12E_SOURCE, with
    # an LZO meter maintaining the published local-time zones across DST.
    # Fixed commercial/network/capacity/subscription fees are not marginal.
    ("PGE", "G12e"): _profile(
        "PGE", "PGE Dystrybucja S.A.", "PGE Obrót S.A.", "G12e",
        1.0991, 0.5969, 1.3635, 1.3635, "pge_g12e", _PGE_G12E_SOURCE,
        weekend_low=True, holiday_low=True,
        valid_from=date(2026, 2, 1), data_version="2026.2",
        price_source_urls=_PGE_G12E_PRICE_SOURCES,
    ),
    ("TAURON", "G11"): _profile(
        "TAURON", "TAURON Dystrybucja S.A.", "TAURON Sprzedaż Sp. z o.o.",
        "G11", 0.9741, 0.9741, 0.9741, 0.9741, "flat", _TAURON_SOURCE,
    ),
    ("TAURON", "G12"): _profile(
        "TAURON", "TAURON Dystrybucja S.A.", "TAURON Sprzedaż Sp. z o.o.",
        "G12", 0.9741, 0.6362, 1.0769, 1.0769, "standard_g12",
        _TAURON_SOURCE,
    ),
    ("TAURON", "G12w"): _profile(
        "TAURON", "TAURON Dystrybucja S.A.", "TAURON Sprzedaż Sp. z o.o.",
        "G12w", 0.9741, 0.6306, 1.2304, 1.2304, "standard_g12",
        _TAURON_SOURCE, weekend_low=True, holiday_low=True,
    ),
    ("TAURON", "G13"): _profile(
        "TAURON", "TAURON Dystrybucja S.A.", "TAURON Sprzedaż Sp. z o.o.",
        "G13", 0.9741, 0.6257, 0.9048, 1.4961, "tauron_g13",
        _TAURON_SOURCE, weekend_low=True, holiday_low=True,
    ),
    ("ENEA", "G11"): _profile(
        "ENEA", "Enea Operator Sp. z o.o.", "Enea S.A.", "G11",
        0.9743, 0.9743, 0.9743, 0.9743, "flat", _ENEA_SOURCE,
    ),
    ("ENEA", "G12"): _profile(
        "ENEA", "Enea Operator Sp. z o.o.", "Enea S.A.", "G12",
        0.9743, 0.5865, 1.1124, 1.1124, "standard_g12", _ENEA_SOURCE,
    ),
    ("ENEA", "G12w"): _profile(
        "ENEA", "Enea Operator Sp. z o.o.", "Enea S.A.", "G12w",
        0.9743, 0.5858, 1.1939, 1.1939, "enea_g12w", _ENEA_SOURCE,
        weekend_low=True, holiday_low=True,
    ),
    ("ENERGA", "G11"): _profile(
        "ENERGA", "Energa-Operator S.A.", "Energa Obrót S.A.", "G11",
        1.0994, 1.0994, 1.0994, 1.0994, "flat", _ENERGA_SOURCE,
    ),
    ("ENERGA", "G12"): _profile(
        "ENERGA", "Energa-Operator S.A.", "Energa Obrót S.A.", "G12",
        1.0994, 0.6230, 1.2446, 1.2446, "standard_g12", _ENERGA_SOURCE,
    ),
    ("ENERGA", "G12w"): _profile(
        "ENERGA", "Energa-Operator S.A.", "Energa Obrót S.A.", "G12w",
        1.0994, 0.6490, 1.2989, 1.2989, "standard_g12", _ENERGA_SOURCE,
        weekend_low=True, holiday_low=True,
    ),
    ("STOEN", "G11"): _profile(
        "STOEN", "Stoen Operator Sp. z o.o.", "E.ON Polska S.A.", "G11",
        0.9628, 0.9628, 0.9628, 0.9628, "flat", _STOEN_SOURCE,
    ),
    ("STOEN", "G12"): _profile(
        "STOEN", "Stoen Operator Sp. z o.o.", "E.ON Polska S.A.", "G12",
        0.9628, 0.6501, 1.0300, 1.0300, "stoen_g12", _STOEN_SOURCE,
    ),
    ("STOEN", "G12w"): _profile(
        "STOEN", "Stoen Operator Sp. z o.o.", "E.ON Polska S.A.", "G12w",
        0.9628, 0.7306, 1.0208, 1.0208, "stoen_g12w", _STOEN_SOURCE,
        weekend_low=True, holiday_low=True,
    ),
}


def get_tariff_profile(operator: str, tariff_type: str) -> TariffProfile | None:
    """Return a supported official profile or ``None``."""
    return _PROFILES.get((operator.strip().upper(), tariff_type.strip()))


def profile_is_valid(profile: TariffProfile, value: date) -> bool:
    """Return whether a versioned price profile covers ``value``.

    Automatic profiles deliberately fail closed outside this interval.  A new
    annual data set can therefore be added without changing optimizer logic,
    while an old price table can never silently be used in a later year.
    """
    return profile.valid_from <= value <= profile.valid_until


def _in_window(minute: int, start: int, end: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _summer(value: date) -> bool:
    return date(value.year, 4, 1) <= value <= date(value.year, 9, 30)


def _windows(profile: TariffProfile, value: date) -> tuple[
    tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]
]:
    """Return low and medium windows for the date."""
    key = profile.schedule_key
    if key == "pge_g12e":
        if value.month in {1, 2, 11, 12}:
            daytime = (13 * 60, 15 * 60)
        elif value.month in {3, 10}:
            daytime = (11 * 60, 15 * 60)
        elif value.month in {4, 9}:
            daytime = (10 * 60, 17 * 60)
        else:
            daytime = (9 * 60, 17 * 60)
        return (daytime, (22 * 60, 6 * 60)), ()
    if key == "pge_g12":
        low = ((15 * 60, 17 * 60), (22 * 60, 6 * 60)) if _summer(value) else (
            (13 * 60, 15 * 60), (22 * 60, 6 * 60)
        )
        return low, ()
    if key == "standard_g12":
        return ((13 * 60, 15 * 60), (22 * 60, 6 * 60)), ()
    if key == "enea_g12w":
        return ((21 * 60, 6 * 60),), ()
    if key == "stoen_g12":
        return ((13 * 60, 15 * 60), (22 * 60, 6 * 60)), ()
    if key == "stoen_g12w":
        return ((22 * 60, 6 * 60),), ()
    if key == "tauron_g13":
        if _summer(value):
            return ((13 * 60, 19 * 60), (22 * 60, 7 * 60)), (
                (7 * 60, 13 * 60),
            )
        return ((13 * 60, 16 * 60), (21 * 60, 7 * 60)), (
            (7 * 60, 13 * 60),
        )
    return (), ()


def profile_rate(
    start: datetime,
    profile: TariffProfile,
    *,
    is_public_holiday: bool,
) -> tuple[float, str]:
    """Return the marginal gross rate and logical zone for a slot."""
    if profile.tariff_type == "G11":
        return profile.g11_price_pln_kwh, "g11"
    low_day = (
        profile.weekend_low_price and start.weekday() >= 5
    ) or (profile.polish_holidays_low_price and is_public_holiday)
    if low_day:
        return profile.low_price_pln_kwh, "low"
    minute = start.hour * 60 + start.minute
    low_windows, medium_windows = _windows(profile, start.date())
    if any(_in_window(minute, begin, end) for begin, end in low_windows):
        return profile.low_price_pln_kwh, "low"
    if any(_in_window(minute, begin, end) for begin, end in medium_windows):
        return profile.medium_price_pln_kwh, "medium"
    return profile.peak_price_pln_kwh, "peak"


def profile_summary(
    profile: TariffProfile,
    now: datetime,
    *,
    is_public_holiday: bool,
) -> dict[str, object]:
    """Return presentation metadata for the dashboard and diagnostics."""
    low_windows, medium_windows = _windows(profile, now.date())
    current_price, current_zone = profile_rate(
        now,
        profile,
        is_public_holiday=is_public_holiday,
    )

    def fmt(windows: tuple[tuple[int, int], ...]) -> str:
        return ", ".join(
            f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}"
            for start, end in windows
        ) or "—"

    return {
        "tariff_operator": profile.operator,
        "tariff_operator_name": profile.operator_name,
        "tariff_supplier_assumption": profile.supplier_name,
        "tariff_profile_year": PROFILE_YEAR,
        "tariff_profile_data_version": profile.data_version,
        "tariff_profile_valid_from": profile.valid_from.isoformat(),
        "tariff_profile_valid_until": profile.valid_until.isoformat(),
        "tariff_profile_valid_now": profile_is_valid(profile, now.date()),
        "tariff_profile_supported": True,
        "tariff_profile_season": "summer" if _summer(now.date()) else "winter",
        "tariff_profile_low_windows": fmt(low_windows),
        "tariff_profile_medium_windows": fmt(medium_windows),
        "tariff_profile_weekend_low": profile.weekend_low_price,
        "tariff_profile_holiday_low": profile.polish_holidays_low_price,
        "tariff_profile_g11_price": profile.g11_price_pln_kwh,
        "tariff_profile_low_price": profile.low_price_pln_kwh,
        "tariff_profile_medium_price": profile.medium_price_pln_kwh,
        "tariff_profile_peak_price": profile.peak_price_pln_kwh,
        "tariff_profile_current_zone": current_zone,
        "tariff_profile_current_price": current_price,
        "tariff_profile_source_url": profile.source_url,
        "tariff_profile_price_source_urls": list(profile.price_source_urls),
        "tariff_profile_fixed_fees_excluded": True,
    }


def _easter_sunday(year: int) -> date:
    """Return Gregorian Easter Sunday using Meeus/Jones/Butcher."""

    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    length = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * length) // 451
    month = (h + length - 7 * m + 114) // 31
    day = (h + length - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def is_polish_public_holiday(value: date) -> bool:
    """Return whether ``value`` is a statutory Polish public holiday."""

    if (value.month, value.day) in {
        (1, 1),
        (1, 6),
        (5, 1),
        (5, 3),
        (8, 15),
        (11, 1),
        (11, 11),
        (12, 24),
        (12, 25),
        (12, 26),
    }:
        return True
    easter = _easter_sunday(value.year)
    return value in {
        easter,
        easter + timedelta(days=1),
        easter + timedelta(days=49),
        easter + timedelta(days=60),
    }


def configured_tariff_rate(
    start: datetime,
    *,
    operator: str,
    tariff_type: str,
    g11_price_pln_kwh: float,
    low_price_pln_kwh: float,
    medium_price_pln_kwh: float,
    peak_price_pln_kwh: float,
    cheap_windows: tuple[tuple[int, int], ...],
    medium_windows: tuple[tuple[int, int], ...] = (),
    weekend_low_price: bool = False,
    polish_holidays_low_price: bool = False,
) -> tuple[float, str]:
    """Return the configured marginal price without any planner state."""

    if operator != MANUAL_OPERATOR:
        profile = get_tariff_profile(operator, tariff_type)
        if profile is None and tariff_type.strip().casefold() == "g12e":
            raise ValueError("G12e official profile is supported only for PGE")
        if profile is not None:
            if not profile_is_valid(profile, start.date()):
                raise ValueError(
                    "official tariff profile does not cover pricing interval "
                    f"{start.date().isoformat()}"
                )
            return profile_rate(
                start,
                profile,
                is_public_holiday=is_polish_public_holiday(start.date()),
            )
    normalized_type = tariff_type.casefold().replace(" ", "")
    if normalized_type == "g11":
        return g11_price_pln_kwh, "g11"

    low_day = (
        weekend_low_price and start.weekday() >= 5
    ) or (
        polish_holidays_low_price
        and is_polish_public_holiday(start.date())
    )
    minute = start.hour * 60 + start.minute
    if low_day or any(
        _in_window(minute, window_start, window_end)
        for window_start, window_end in cheap_windows
    ):
        return low_price_pln_kwh, "low"
    if normalized_type == "g13" and any(
        _in_window(minute, window_start, window_end)
        for window_start, window_end in medium_windows
    ):
        return medium_price_pln_kwh, "medium"
    return peak_price_pln_kwh, "peak"
