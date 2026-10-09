"""Frozen Recorder projection from canonical 6484a8249780d144756e83a6c293ebebed2a9fae.

Only the projection and its key list are copied. Keep this fixture unchanged
when evaluating later storage candidates against this base.
"""

from collections.abc import Mapping
from typing import Any


_RECORDED_DECISION_KEYS = (
    "execution_phase", "selected_policy", "selected_action", "owner", "reason",
    "lifecycle_reason", "selection_reason", "execution_blocked_reason",
    "transaction_id", "transaction_evidence_scope", "rejected_reasons",
)


def supervisor_recorder_projection(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the original history proof, without runtime snapshots or input ages."""
    result = {key: attributes.get(key) for key in _RECORDED_DECISION_KEYS}
    result["schema_version"] = 2
    candidates = attributes.get("candidate_summaries")
    result["candidate_summaries"] = [
        {key: candidate.get(key) for key in (
            "policy_id", "reason_code", "start_eligible", "continuation_eligible"
        )}
        for candidate in candidates[:3] if isinstance(candidate, Mapping)
    ] if isinstance(candidates, (list, tuple)) else []
    transaction = attributes.get("transaction_evidence")
    transaction = transaction if isinstance(transaction, Mapping) else {}
    proof = transaction.get("physical_verification")
    proof = proof if isinstance(proof, Mapping) else {}
    result["transaction_evidence"] = {
        **{key: transaction.get(key) for key in (
            "transaction_id", "command_sent_at", "readback_result", "reason",
            "rollback_status", "rollback_result",
        )},
        "physical_verification": {key: proof.get(key) for key in (
            "status", "observed_at", "transaction_id", "action", "evidence",
        )},
    }
    tariff_decision = attributes.get("tariff_decision")
    result["tariff_decision"] = (
        dict(tariff_decision) if isinstance(tariff_decision, Mapping) else None
    )
    return result
