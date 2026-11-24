import socket

import httpx
import pytest

from app.db import Store
from app.research.deterministic import VERSION
from app.schemas import ProfileField
from benchmarks.stage_b_evaluation import FIXTURES, STAGE_A_FIXTURES, run


def test_evaluation_grounds_all_seven_fields_without_clients_or_storage(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The offline evaluation must not open network clients or job storage")

    for target, name in (
        (socket, "getaddrinfo"),
        (httpx, "Client"),
        (httpx, "AsyncClient"),
        (Store, "__init__"),
    ):
        monkeypatch.setattr(target, name, forbidden)

    report = run(STAGE_A_FIXTURES.read_bytes(), FIXTURES.read_bytes())
    assert report["dataset"]["live_model_calls"] == 0
    assert set(report["field_flow"]["unique_grounded_by_producer"][VERSION]) == set(ProfileField)
    assert report["matched_stage_a"]["correct_value_counts"] == {
        "both": 25,
        "deterministic_only": 1,
        "recorded_only": 0,
    }
    assert report["direct_claim_scores"]["profile_link_score"]["tp"] == 4
    assert report["direct_claim_scores"]["employment_claim_pair_score"]["tp"] == 2
    assert report["direct_claim_scores"]["identity_rejection_score"]["tp"] == 1
    assert not any(case["forbidden_claims_grounded"] for case in report["direct_claim_audit"])
