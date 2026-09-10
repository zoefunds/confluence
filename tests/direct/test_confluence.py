"""
Direct-mode tests for Confluence.

Direct mode runs only the leader path (validator comparison logic is not
exercised here — see tests/integration for full consensus checks). These
tests cover: escrow bonding, the deterministic structural-conflict gate,
the two evidence-attestation flows (mocked), the LLM-judged commute/conflict
/subsumes verdicts (mocked), and every escrow exit path.
"""

import json

import pytest

GENESIS = json.dumps({
    "auth": {"password_enabled": True, "mfa_required": False},
    "network": {"region": "us-east"},
})

ONE_GEN = 10 ** 18


def deploy(direct_deploy):
    return direct_deploy("confluence.py", GENESIS)


# ---------------------------------------------------------------------------
# Escrow entry
# ---------------------------------------------------------------------------

def test_submit_proposal_requires_min_bond(direct_vm, direct_deploy, direct_alice):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN - 1
    with direct_vm.expect_revert("Must lock at least"):
        contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")


def test_submit_proposal_locks_bond_and_indexes(direct_vm, direct_deploy, direct_alice):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    pid = contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")

    p = contract.get_proposal(pid)
    assert p["status"] == "pending"
    assert p["bond_deposited"] == str(ONE_GEN)
    assert pid in contract.list_pending_for_base("v0")
    assert pid in contract.list_proposals_by_proposer(p["proposer"])


# ---------------------------------------------------------------------------
# Deterministic structural conflict gate — no model call involved
# ---------------------------------------------------------------------------

def test_identical_field_path_is_deterministic_conflict(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = deploy(direct_deploy)

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    p1 = contract.submit_proposal("v0", "network.region", '"eu-west"', "move to EU")

    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    p2 = contract.submit_proposal("v0", "network.region", '"ap-south"', "move to APAC")

    direct_vm.sender = direct_alice
    verdict = contract.attempt_merge(p1, p2)
    assert verdict == "conflict"
    assert contract.get_proposal(p1)["status"] == "blocked"
    assert contract.get_proposal(p2)["status"] == "blocked"
    # bonds stay locked, not paid out
    assert contract.get_proposal(p1)["bond_deposited"] == str(ONE_GEN)


def test_parent_child_field_path_is_deterministic_conflict(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = deploy(direct_deploy)

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    p1 = contract.submit_proposal("v0", "auth", '{"password_enabled": false}', "rewrite whole auth block")

    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    p2 = contract.submit_proposal("v0", "auth.mfa_required", "true", "require MFA")

    direct_vm.sender = direct_alice
    verdict = contract.attempt_merge(p1, p2)
    assert verdict == "conflict"


# ---------------------------------------------------------------------------
# Solo commit — no coordination needed, no model call
# ---------------------------------------------------------------------------

def test_commit_solo_creates_version_and_refunds(direct_vm, direct_deploy, direct_alice):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    pid = contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")

    new_version = contract.commit_solo(pid)
    assert new_version == "v1"
    assert contract.get_state_at("v1")["network"]["region"] == "eu-west"
    assert contract.get_proposal(pid)["status"] == "merged"
    assert contract.get_proposal(pid)["bond_deposited"] == "0"
    assert pid not in contract.list_pending_for_base("v0")


def test_commit_solo_rejects_when_other_pending(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    p1 = contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")
    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    contract.submit_proposal("v0", "auth.mfa_required", "true", "require MFA")

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Other proposals are pending"):
        contract.commit_solo(p1)


# ---------------------------------------------------------------------------
# LLM-judged verdicts (mocked) — commute and subsumes
# ---------------------------------------------------------------------------

def test_commute_verdict_merges_both_proposals(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = deploy(direct_deploy)

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    p1 = contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")

    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    p2 = contract.submit_proposal("v0", "auth.mfa_required", "true", "require MFA")

    direct_vm.mock_llm(
        r".*Classify the relationship.*",
        json.dumps({"verdict": "commute", "subsumed": None, "reasoning": "disjoint, no interaction"}),
    )

    direct_vm.sender = direct_alice
    new_version = contract.attempt_merge(p1, p2)
    assert new_version == "v1"
    state = contract.get_state_at("v1")
    assert state["network"]["region"] == "eu-west"
    assert state["auth"]["mfa_required"] is True
    assert contract.get_proposal(p1)["status"] == "merged"
    assert contract.get_proposal(p2)["status"] == "merged"
    assert contract.get_proposal(p1)["bond_deposited"] == "0"
    assert contract.get_proposal(p2)["bond_deposited"] == "0"


def test_conflict_verdict_blocks_both_without_paying_out(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = deploy(direct_deploy)

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    p1 = contract.submit_proposal("v0", "auth.password_enabled", "false", "disable passwords")

    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    p2 = contract.submit_proposal(
        "v0", "auth.recovery_password_enabled", "true", "allow password for emergency recovery"
    )

    direct_vm.mock_llm(
        r".*Classify the relationship.*",
        json.dumps({"verdict": "conflict", "subsumed": None, "reasoning": "recovery relies on passwords"}),
    )

    direct_vm.sender = direct_alice
    verdict = contract.attempt_merge(p1, p2)
    assert verdict == "conflict"
    assert contract.get_proposal(p1)["status"] == "blocked"
    assert contract.get_proposal(p2)["status"] == "blocked"


def test_subsumes_verdict_refunds_only_the_redundant_side(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = deploy(direct_deploy)

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    p1 = contract.submit_proposal("v0", "auth.mfa_required", "true", "require MFA for all users")

    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    p2 = contract.submit_proposal("v0", "auth.mfa_required_admins", "true", "require MFA for admins only")

    direct_vm.mock_llm(
        r".*Classify the relationship.*",
        json.dumps({"verdict": "subsumes", "subsumed": "b", "reasoning": "A already covers B"}),
    )

    direct_vm.sender = direct_alice
    verdict = contract.attempt_merge(p1, p2)
    assert verdict == "subsumes"
    assert contract.get_proposal(p2)["status"] == "subsumed"
    assert contract.get_proposal(p2)["bond_deposited"] == "0"
    assert contract.get_proposal(p1)["status"] == "pending"
    assert contract.get_proposal(p1)["bond_deposited"] == str(ONE_GEN)


# ---------------------------------------------------------------------------
# Web evidence attestation (mocked strict_eq fetch)
# ---------------------------------------------------------------------------

def test_attest_web_evidence_gates_merge_eligibility(direct_vm, direct_deploy, direct_alice):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    pid = contract.submit_proposal(
        "v0", "network.region", '"eu-west"', "move region",
        evidence_url="https://policy.example.com/region-change.txt",
    )

    with direct_vm.expect_revert("Evidence attestation is still pending"):
        contract.commit_solo(pid)

    direct_vm.mock_web(
        r".*policy\.example\.com/region-change\.txt.*",
        {"status": 200, "body": "Approved: EU region migration"},
    )
    status = contract.attest_web_evidence(pid)
    assert status == "attested"
    assert "Approved" in contract.get_proposal(pid)["web_evidence_digest"]

    new_version = contract.commit_solo(pid)
    assert new_version == "v1"


# ---------------------------------------------------------------------------
# Image evidence attestation (mocked vision LLM)
# ---------------------------------------------------------------------------

def test_attest_image_evidence_gates_merge_eligibility(direct_vm, direct_deploy, direct_alice):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    image_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    pid = contract.submit_proposal(
        "v0", "network.region", '"eu-west"', "move region per signed approval",
        image_b64=image_b64,
    )

    direct_vm.mock_llm(
        r".*Classify this image.*",
        json.dumps({"document_type": "signed_approval", "key_fact": " Signed   migration\napproval form "}),
    )
    status = contract.attest_image_evidence(pid)
    assert status == "attested"
    assert contract.get_proposal(pid)["image_document_type"] == "signed_approval"
    assert contract.get_proposal(pid)["image_key_fact"] == "Signed   migration\napproval form"

    new_version = contract.commit_solo(pid)
    assert new_version == "v1"


def test_merge_excludes_informational_image_fact(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Unverified image prose must never influence semantic adjudication."""
    contract = deploy(direct_deploy)
    image_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    p1 = contract.submit_proposal(
        "v0", "network.region", '"eu-west"', "move region",
        image_b64=image_b64,
    )
    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    p2 = contract.submit_proposal("v0", "auth.mfa_required", "true", "require MFA")

    direct_vm.mock_llm(
        r".*Classify this image.*",
        json.dumps({"document_type": "signed_approval", "key_fact": "Approved EU migration"}),
    )
    assert contract.attest_image_evidence(p1) == "attested"

    direct_vm.mock_llm(
        r"(?s)^(?!.*Approved EU migration).*Attested image document type: signed_approval.*",
        json.dumps({"verdict": "commute", "subsumed": None, "reasoning": "compatible"}),
    )
    direct_vm.sender = direct_alice
    assert contract.attempt_merge(p1, p2) == "v1"


# ---------------------------------------------------------------------------
# Cancellation and timeout — the escrow recovery exits
# ---------------------------------------------------------------------------

def test_cancel_proposal_refunds_bond(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    pid = contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the proposer can cancel"):
        contract.cancel_proposal(pid)

    direct_vm.sender = direct_alice
    contract.cancel_proposal(pid)
    assert contract.get_proposal(pid)["status"] == "cancelled"
    assert contract.get_proposal(pid)["bond_deposited"] == "0"
    assert pid not in contract.list_pending_for_base("v0")


def test_cancel_twice_fails_double_spend(direct_vm, direct_deploy, direct_alice):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    pid = contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")

    contract.cancel_proposal(pid)
    with direct_vm.expect_revert("Cannot cancel from status"):
        contract.cancel_proposal(pid)


def test_timeout_refund_blocked_until_gap_reached(direct_vm, direct_deploy, direct_alice):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    pid = contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")

    with direct_vm.expect_revert("Timeout window not reached"):
        contract.claim_timeout_refund(pid)


# ---------------------------------------------------------------------------
# Version graph provenance
# ---------------------------------------------------------------------------

def test_ancestry_and_diff(direct_vm, direct_deploy, direct_alice):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    pid = contract.submit_proposal("v0", "network.region", '"eu-west"', "move region")
    contract.commit_solo(pid)

    assert contract.get_ancestry("v1") == ["v0", "v1"]
    assert contract.is_ancestor("v0", "v1") is True
    assert contract.get_children("v0") == ["v1"]

    diff = contract.diff_versions("v0", "v1")
    assert diff["changed"]["network.region"] == {"from": "us-east", "to": "eu-west"}
