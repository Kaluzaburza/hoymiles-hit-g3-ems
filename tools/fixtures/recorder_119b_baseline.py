"""Pinned Recorder projection from 119b0b0; test baseline only."""
from typing import Any, Mapping
RECORDED_EXECUTION_ATTRIBUTE = "recorded_execution"

SUPERVISOR_UNRECORDED_ATTRIBUTES = frozenset({
    "candidate_summaries", "transaction_evidence", "execution_context_evidence",
    "execution_health", "control_lease", "control_lease_renewal",
    "arbitration_revision", "execution_last_valid_read_at",
    "profile_effects_not_applied",
})

_RECORDED_DECISION_KEYS = (
    "execution_phase", "selected_policy", "selected_action", "owner", "reason",
    "lifecycle_reason", "selection_reason", "execution_blocked_reason",
    "transaction_id", "transaction_evidence_scope", "rejected_reasons",
)

def supervisor_recorder_projection(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the original history proof, without runtime snapshots or input ages."""
    result = {key: attributes.get(key) for key in _RECORDED_DECISION_KEYS}
    result["schema_version"] = 1
    candidates = attributes.get("candidate_summaries")
    result["candidate_summaries"] = [
        {key: candidate.get(key) for key in ("policy_id", "reason_code")}
        for candidate in candidates[:3] if isinstance(candidate, Mapping)
    ] if isinstance(candidates, (list, tuple)) else []
    transaction = attributes.get("transaction_evidence")
    transaction = transaction if isinstance(transaction, Mapping) else {}
    proof = transaction.get("physical_verification")
    proof = proof if isinstance(proof, Mapping) else {}
    result["transaction_evidence"] = {
        **{key: transaction.get(key) for key in (
            "transaction_id", "command_sent_at", "readback_result",
        )},
        "physical_verification": {key: proof.get(key) for key in (
            "status", "observed_at", "transaction_id", "action",
        )},
    }
    return result
