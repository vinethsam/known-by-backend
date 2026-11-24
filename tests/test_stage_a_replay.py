import json
import socket

import httpx
import pytest

from app.db import Store
from benchmarks.stage_a_replay import FIXTURES, evaluate


def test_replay_is_repeatable_without_clients_or_storage(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Replay must not open network clients or job storage")

    for target, name in (
        (socket, "getaddrinfo"),
        (httpx, "Client"),
        (httpx, "AsyncClient"),
        (Store, "__init__"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    evidence = FIXTURES.read_bytes()
    result = evaluate(evidence)
    assert evaluate(evidence) == result
    cases = {case["id"]: case for case in result["cases"]}
    omission = cases["A01-same-institution-prose"]
    assert omission["scores"]["subject"]["missing"] == ["geology"]
    assert omission["scores"]["qualifications"]["fn"] == 1

    partial = cases["A03-award-bio-partial-education"]
    batch = partial["batches"][0]
    rejection = batch["diagnostics"][0]
    assert rejection["rejected"] == {"UNGROUNDED_VALUE": 1}
    assert batch["response"]["claims"][rejection["proposal_index"]]["value"] == "MFA"
    assert not partial["actual_fields"]["degree_type"]

    role = cases["A05-current-role-vs-old-coverage"]
    unselected = set(role["grounded_without_selected_support"])
    historical = [claim for claim in role["claims"] if claim["fact_group"] == "former"]
    assert len(historical) == 2
    assert all(claim["claim_id"] in unselected and claim["end_date"] == "2023-08-31" for claim in historical)
    assert role["scores"]["employment"]["tp"] == 1

    conflict = cases["A06-simultaneous-title-conflict"]
    assert conflict["scores"]["job_title"] is None
    assert conflict["profile"]["review_required"]
    assert "CURRENT_ROLE_CONFLICT" in conflict["profile"]["records"][0]["review_reason_codes"]


def test_recorded_reply_cannot_replay_against_changed_evidence():
    dataset = json.loads(FIXTURES.read_bytes())
    source = dataset["cases"][0]["sources"][0]
    source["html"] = source["html"].replace("Geology", "Physics")
    with pytest.raises(ValueError, match="Recorded reply no longer matches its chunk"):
        evaluate(json.dumps(dataset).encode())
