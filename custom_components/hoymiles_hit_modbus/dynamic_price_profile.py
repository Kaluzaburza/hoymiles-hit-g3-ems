"""Atomic source selection for classic RCE/tariff or paired Pstryk prices.

This record deliberately has no actuator permissions or API credentials.
Existing classic price/window helpers remain stored separately and unchanged;
the future UI's two selectors are views of this single source profile.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json

try:
    from .tariff_profiles import MANUAL_OPERATOR, SUPPORTED_OPERATORS, SUPPORTED_GROUPS
except ImportError:  # Standalone offline tests.
    from tariff_profiles import MANUAL_OPERATOR, SUPPORTED_OPERATORS, SUPPORTED_GROUPS


def paired_plan_current(rce, tariff, *, pstryk_selected: bool, pstryk_bound: bool) -> bool:
    """Fail closed during a mixed source/profile/result publication cohort."""
    involved = pstryk_selected or any(p.get('price_provider') == 'Pstryk' for p in (rce, tariff))
    if not involved:
        return True
    if not pstryk_bound:
        return False
    revision = rce.get('joint_plan_revision')
    epoch = rce.get('joint_profile_revision')
    return (type(revision) is int and revision > 0 and type(epoch) is int and epoch >= 0
            and all(p.get('price_provider') == 'Pstryk' and p.get('result_current') is True
                    and p.get('recalculation_pending') is False
                    and p.get('joint_plan_revision') == revision
                    and p.get('joint_profile_revision') == epoch for p in (rce, tariff)))


@dataclass(frozen=True, slots=True)
class DynamicPriceProfile:
    mode: str = "classic"
    classic_operator: str | None = None
    classic_tariff: str | None = None
    revision: int = 0

    def __post_init__(self):
        if self.mode not in {"classic", "pstryk"}:
            raise ValueError("profile_mode_invalid")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("profile_revision_invalid")
        if (self.classic_operator is None) != (self.classic_tariff is None):
            raise ValueError("classic_profile_incomplete")
        if self.classic_operator is not None and (
            self.classic_operator not in (*SUPPORTED_OPERATORS, MANUAL_OPERATOR)
            or self.classic_tariff not in SUPPORTED_GROUPS
        ):
            raise ValueError("classic_profile_invalid")

    @property
    def sale_provider(self) -> str:
        return "Pstryk" if self.mode == "pstryk" else "RCE"

    @property
    def purchase_provider(self) -> str | None:
        return "Pstryk" if self.mode == "pstryk" else self.classic_operator

    def select_sale(self, provider: str, *, classic: tuple[str, str] | None = None) -> DynamicPriceProfile:
        if provider not in {"RCE", "Pstryk"}:
            raise ValueError("sale_provider_invalid")
        mode = "pstryk" if provider == "Pstryk" else "classic"
        if classic is not None:
            if provider != "RCE" or len(classic) != 2:
                raise ValueError("classic_selection_invalid")
            # The user can provide the missing classic choice in the same
            # atomic return-to-RCE operation; otherwise an unconfigured install
            # would be stuck between two selectors that both reject it.
            candidate = replace(self, mode="classic", classic_operator=classic[0],
                                classic_tariff=classic[1])
            return candidate if candidate == self else replace(candidate, revision=self.revision + 1)
        if mode == self.mode:
            return self
        if mode == "classic" and self.classic_operator is None:
            raise ValueError("classic_profile_required")
        return replace(self, mode=mode, revision=self.revision + 1)

    def select_purchase(self, provider: str, *, tariff: str | None = None) -> DynamicPriceProfile:
        if provider == "Pstryk":
            if tariff is not None:
                raise ValueError("pstryk_has_no_classic_tariff")
            return self.select_sale("Pstryk")
        if self.mode == "pstryk":
            raise ValueError("pstryk_profile_bound")
        if tariff is None:
            raise ValueError("classic_profile_required")
        if (provider, tariff) == (self.classic_operator, self.classic_tariff):
            return self
        return replace(self, classic_operator=provider, classic_tariff=tariff,
                       revision=self.revision + 1)

    def to_json(self) -> str:
        return json.dumps({"schema": 1, **asdict(self)}, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> DynamicPriceProfile:
        try:
            if not isinstance(raw, str) or len(raw) > 2048:
                raise ValueError("profile_size_invalid")
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {
                "schema", "mode", "classic_operator", "classic_tariff", "revision",
            } or type(data["schema"]) is not int or data.pop("schema") != 1:
                raise ValueError("profile_schema_invalid")
            return cls(**data)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("profile_invalid") from None
