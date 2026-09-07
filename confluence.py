# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
================================================================================
 CONFLUENCE — a semantic merge coordination contract for multi-agent systems
================================================================================

WHAT THIS IS
------------
Confluence is a reusable GenLayer primitive for coordinating concurrent
proposals against one shared piece of state: a config file, a project plan,
a protocol manifest, a policy document, an agent fleet's runbook, anything
modeled as a JSON object. Any number of autonomous agents can each propose a
change against a specific base version, without a central coordinator ever
deciding by fiat whether two concurrent proposals are safe to apply together.

WHY GENLAYER CONSENSUS IS ACTUALLY LOAD-BEARING HERE
-----------------------------------------------------
Two changes can be structurally disjoint (different JSON fields) yet still
conflict in *meaning* — "disable password authentication" and "allow password
authentication for emergency recovery accounts" touch different fields but
cannot both hold. Deciding that requires reading natural-language intent, so
it cannot be resolved by a deterministic diff alone. But it also cannot be
left to a single off-chain LLM call, because whoever controls that call
controls the shared state. GenLayer's validator set is used here to reach
byzantine-fault-tolerant agreement on a *bounded classification* — never on
free-form merged text:

    COMMUTE    -- both proposals may be applied verbatim, together
    CONFLICT   -- applying both would contradict or undermine one another
    SUBSUMES   -- one proposal's effect already covers the other's intent
    AMBIGUOUS  -- not enough information to decide safely

The model is only ever a *classifier over a closed label set*. It never
authors the merged state. When validators agree on COMMUTE, deterministic
contract code — not the model — applies the two original proposals, exactly
as submitted, to produce the new version. This is the core design
constraint that keeps the contract auditable: you can always answer "what
does version v7 actually contain?" by re-reading the two proposals that
produced it, not by trusting a generated blob.

WHY THIS AVOIDS "UNDETERMINED" CONSENSUS RESULTS
--------------------------------------------------
A GenLayer round goes Undetermined when validators keep disagreeing across
every funded rotation. Two design choices in this contract are specifically
aimed at keeping validator agreement high:

1. Structural conflicts (the same field edited by two proposals) are caught
   by pure deterministic code before any model is ever invoked. No
   nondeterministic call is made unless it is actually needed, and
   deterministic code can't disagree with itself.

2. External facts (a fetched web page, an uploaded image) are pulled into
   consensus *once*, at proposal-submission time, through their own
   dedicated equivalence-principle calls (`attest_web_evidence`,
   `attest_image_evidence`). The result is written to the proposal as a
   short, stable digest. The later semantic-merge judgment
   (`attempt_merge`) reads that already-agreed digest instead of
   re-fetching a live page or re-describing an image — so a page that
   changed between two calls, or two slightly different image captions,
   can never become a source of disagreement inside the merge judgment
   itself. Every nondeterministic block in this contract compares a small
   number of bounded, stable fields (an enum, a boolean, a short capped
   string) rather than free-form prose, which is what actually determines
   whether independent validators converge.

ESCROW
------
Every proposal is bonded in GEN. The bond is custody, not a fee — it always
comes back to the proposer through exactly one of five exits, and the
ledger field backing it is zeroed before any transfer happens (see
`_refund`), so a bond can never be paid out twice:

    merged     -- proposal's change lands in a new version -> full refund
    subsumed   -- proposal's effect was already covered elsewhere -> refund
    cancelled  -- proposer withdraws before resolution -> refund
    timed out  -- nobody ever resolved it -> proposer reclaims after a
                  deterministic, sequence-based waiting window
    blocked    -- CONFLICT or AMBIGUOUS: bond stays locked (not seized, not
                  paid to anyone) until the proposer cancels or the timeout
                  window opens, so a loser can't be resubmitted for free
                  while still occupying a slot in the graph

VERSION GRAPH
-------------
Every successful resolution (solo commit or COMMUTE merge) creates a new
immutable version node recording its base version and the exact proposal
ids that produced it. `get_version`, `list_versions`, `get_children`, and
`get_ancestry` let any external agent or indexer reconstruct the full
provenance of the current state without trusting a single party's account
of history.

FRONTEND / OFF-CHAIN BOUNDARY
------------------------------
This contract does not own: authentication of "which agent may propose
what", UI for browsing the graph, or notification of interested parties.
Those belong in a client. This contract owns exactly the state transition
that needs consensus: is a base version real, do two proposals structurally
collide, are they semantically compatible, and the settlement of the bonds
that back every proposal.
================================================================================
"""

from genlayer import *
import base64
import json
import typing

# ---------------------------------------------------------------------------
# Error classification. Every raised UserError carries one of these prefixes
# so that a validator comparing its own outcome against a leader's raised
# error can classify it correctly instead of guessing:
#   EXPECTED / EXTERNAL  -> deterministic, must match exactly
#   TRANSIENT            -> validators agree if *both* hit a transient fault
#   LLM_ERROR             -> always disagree, forcing leader rotation
# ---------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"

# ---------------------------------------------------------------------------
# The bounded verdict vocabulary. Validators may return ONLY one of these
# four labels for a pairwise semantic judgment — the model classifies, it
# never authors a merged result.
# ---------------------------------------------------------------------------
VERDICT_COMMUTE = "commute"
VERDICT_CONFLICT = "conflict"
VERDICT_SUBSUMES = "subsumes"
VERDICT_AMBIGUOUS = "ambiguous"
VALID_VERDICTS = (VERDICT_COMMUTE, VERDICT_CONFLICT, VERDICT_SUBSUMES, VERDICT_AMBIGUOUS)

# Image classification vocabulary used by attest_image_evidence. Also a
# closed, bounded label set — the compared field in that consensus round.
IMAGE_DOC_TYPES = (
    "config_screenshot",
    "diagram",
    "signed_approval",
    "chat_or_email_excerpt",
    "chart_or_dashboard",
    "other",
)

# ---------------------------------------------------------------------------
# Proposal lifecycle statuses.
# ---------------------------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_MERGED = "merged"
STATUS_BLOCKED = "blocked"
STATUS_SUBSUMED = "subsumed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMED_OUT = "timed_out"

EVIDENCE_NONE = "none"                 # no evidence_url / image supplied
EVIDENCE_PENDING = "pending"           # supplied but not yet attested
EVIDENCE_ATTESTED = "attested"         # consensus reached on the digest
EVIDENCE_FAILED = "failed"             # source unreachable / unusable

# ---------------------------------------------------------------------------
# Economic / temporal constants.
# ---------------------------------------------------------------------------
MIN_BOND_WEI = 10 ** 18       # 1 GEN anti-spam bond per proposal
MAX_EVIDENCE_DIGEST_CHARS = 1000
MAX_IMAGE_FACT_CHARS = 240
MAX_FIELD_PATH_DEPTH = 16
MAX_DESCRIPTION_CHARS = 2000

# GenVM has no reliable wall clock inside deterministic code, so submission
# order is used as a monotonic, deterministic stand-in for elapsed time: a
# proposal may be reclaimed once this many *other* proposals have since been
# submitted contract-wide.
TIMEOUT_PROPOSAL_GAP = 200


# ---------------------------------------------------------------------------
# Escrow emission point. Every GEN transfer out of this contract funnels
# through this single function, so the entire payout surface can be audited
# by grepping one name. See module docstring "ESCROW" for the five exits
# that call it, all following zero-ledger-then-transfer ordering.
# ---------------------------------------------------------------------------
@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


def _send_gen(to_address: str, amount: int) -> None:
    if not to_address:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Missing recipient address")
    if amount <= 0:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Transfer amount must be positive")
    _Recipient(Address(to_address)).emit_transfer(value=u256(amount))


# ---------------------------------------------------------------------------
# Small deterministic helpers shared by several entry points.
# ---------------------------------------------------------------------------
def _parse_json_object(text: str) -> dict:
    """Best-effort extraction of a JSON object from LLM output."""
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        raise gl.vm.UserError(f"{ERROR_LLM} No JSON object found in model output")
    return json.loads(text[first:last + 1])


def _coerce_str(value: typing.Any, cap: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value)
    text = text.strip()
    return text[:cap]


def _split_field_path(field_path: str) -> list:
    parts = [p for p in field_path.strip().split(".") if p]
    if not parts:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} field_path is required")
    if len(parts) > MAX_FIELD_PATH_DEPTH:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} field_path exceeds max depth {MAX_FIELD_PATH_DEPTH}")
    for p in parts:
        if not p.strip():
            raise gl.vm.UserError(f"{ERROR_EXPECTED} field_path has an empty segment")
    return parts


def _field_paths_overlap(path_a: str, path_b: str) -> bool:
    """
    Deterministic structural-conflict rule.

    Two field paths structurally collide if they are identical, OR if one
    is a prefix of the other. Editing `auth` wholesale and editing
    `auth.password` are a real structural collision even though the raw
    strings differ, because writing the parent key can silently clobber
    the child. This is intentionally stricter than exact-string equality
    so the semantic judge is only ever invoked for genuinely disjoint
    proposals.
    """
    a = _split_field_path(path_a)
    b = _split_field_path(path_b)
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


def _apply_field_path(state: dict, field_path: str, new_value: typing.Any) -> dict:
    """Deterministically set a dotted field path on a deep copy of state."""
    out = json.loads(json.dumps(state))
    parts = _split_field_path(field_path)
    cursor = out
    for p in parts[:-1]:
        if p not in cursor or not isinstance(cursor[p], dict):
            cursor[p] = {}
        cursor = cursor[p]
    cursor[parts[-1]] = new_value
    return out


def _handle_leader_error(leaders_res: gl.vm.Result, leader_fn: typing.Callable) -> bool:
    """Canonical validator-side error reconciliation (see write-contract guide)."""
    leader_msg = getattr(leaders_res, "message", "") or ""
    try:
        leader_fn()
        return False  # leader errored, validator succeeded -> disagree
    except gl.vm.UserError as e:
        validator_msg = str(e)
        if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(ERROR_EXTERNAL):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERROR_TRANSIENT) and str(leader_msg).startswith(ERROR_TRANSIENT):
            return True
        return False
    except Exception:
        return False


class Confluence(gl.Contract):
    # -- version graph ----------------------------------------------------
    versions: TreeMap[str, str]         # version_id -> json (see _create_version)
    version_children: TreeMap[str, str]  # base_version_id -> json array of child version ids
    version_seq: u256

    # -- proposals ----------------------------------------------------------
    proposals: TreeMap[str, str]         # proposal_id -> json (see submit_proposal)
    proposal_seq: u256
    pending_by_base: TreeMap[str, str]   # base_version_id -> json array of pending proposal ids
    proposals_by_proposer: TreeMap[str, str]  # proposer address -> json array of proposal ids

    # -- aggregate counters, kept for O(1) stats instead of O(n) scans -----
    total_merged: u256
    total_blocked: u256
    total_cancelled: u256
    total_timed_out: u256
    total_locked_wei: u256

    def __init__(self, genesis_state_json: str):
        try:
            genesis_state = json.loads(genesis_state_json)
        except Exception:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} genesis_state_json must be valid JSON")
        if not isinstance(genesis_state, dict):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} genesis state must be a JSON object")

        self.version_seq = u256(0)
        self.proposal_seq = u256(0)
        self.total_merged = u256(0)
        self.total_blocked = u256(0)
        self.total_cancelled = u256(0)
        self.total_timed_out = u256(0)
        self.total_locked_wei = u256(0)

        root_id = "v0"
        self.versions[root_id] = json.dumps({
            "version_id": root_id,
            "base_version_id": "",
            "state_json": json.dumps(genesis_state),
            "via_proposals": [],
            "seq": 0,
        })

    # ======================================================================
    # VIEWS
    # ======================================================================
    @gl.public.view
    def get_version(self, version_id: str) -> dict:
        return self._get_version_dict(version_id)

    @gl.public.view
    def get_proposal(self, proposal_id: str) -> dict:
        return self._get_proposal_dict(proposal_id)

    @gl.public.view
    def list_pending_for_base(self, base_version_id: str) -> list:
        return self._pending_ids(base_version_id)

    @gl.public.view
    def get_children(self, version_id: str) -> list:
        """Version ids created directly from `version_id` as their base."""
        raw = self.version_children.get(version_id)
        return json.loads(raw) if raw else []

    @gl.public.view
    def get_ancestry(self, version_id: str) -> list:
        """Root-to-`version_id` chain of version ids, for provenance display."""
        chain = []
        current = version_id
        seen = set()
        while current:
            if current in seen:
                raise gl.vm.UserError(f"{ERROR_EXPECTED} Cycle detected in version graph")
            seen.add(current)
            v = self._get_version_dict(current)
            chain.append(current)
            current = v["base_version_id"]
        chain.reverse()
        return chain

    @gl.public.view
    def get_state_at(self, version_id: str) -> dict:
        """Convenience accessor: the materialized JSON state of a version."""
        v = self._get_version_dict(version_id)
        return json.loads(v["state_json"])

    @gl.public.view
    def diff_versions(self, from_version_id: str, to_version_id: str) -> dict:
        """
        Flat-key diff between two versions' materialized states. Purely a
        read-side convenience for clients — no consensus involved, since
        both states are already-agreed, stored facts.
        """
        a = json.loads(self._get_version_dict(from_version_id)["state_json"])
        b = json.loads(self._get_version_dict(to_version_id)["state_json"])

        def flatten(d: dict, prefix: str = "") -> dict:
            out = {}
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    out.update(flatten(v, key))
                else:
                    out[key] = v
            return out

        flat_a, flat_b = flatten(a), flatten(b)
        added = {k: flat_b[k] for k in flat_b.keys() - flat_a.keys()}
        removed = {k: flat_a[k] for k in flat_a.keys() - flat_b.keys()}
        changed = {
            k: {"from": flat_a[k], "to": flat_b[k]}
            for k in flat_a.keys() & flat_b.keys()
            if flat_a[k] != flat_b[k]
        }
        return {"added": added, "removed": removed, "changed": changed}

    @gl.public.view
    def get_root_version(self) -> str:
        """The genesis version id. Always `"v0"`, exposed so callers never
        need to hardcode it."""
        return "v0"

    @gl.public.view
    def list_versions(self, offset: int, limit: int) -> list:
        """
        Paginated listing of version ids in creation order, oldest first.
        Bounded so a client can't force an unbounded scan in one call.
        """
        if offset < 0 or limit <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} offset must be >= 0 and limit must be > 0")
        limit = min(limit, 200)
        total = int(self.version_seq) + 1  # + genesis
        ids = []
        for seq in range(offset, min(offset + limit, total)):
            ids.append("v0" if seq == 0 else f"v{seq}")
        return ids

    @gl.public.view
    def list_proposals_by_proposer(self, proposer_address: str) -> list:
        raw = self.proposals_by_proposer.get(proposer_address)
        return json.loads(raw) if raw else []

    @gl.public.view
    def is_ancestor(self, candidate_version_id: str, version_id: str) -> bool:
        """
        True if `candidate_version_id` lies on the root-to-`version_id`
        chain (including equality). Lets an external contract or agent
        check "is this proposal still based on state we already trust"
        without re-deriving the whole ancestry chain client-side.
        """
        return candidate_version_id in self.get_ancestry(version_id)

    @gl.public.view
    def contract_stats(self) -> dict:
        return {
            "total_versions": int(self.version_seq) + 1,  # +1 for genesis v0
            "total_proposals": int(self.proposal_seq),
            "total_merged": int(self.total_merged),
            "total_blocked": int(self.total_blocked),
            "total_cancelled": int(self.total_cancelled),
            "total_timed_out": int(self.total_timed_out),
            "total_locked_wei": int(self.total_locked_wei),
        }

    # ======================================================================
    # Internal storage helpers
    # ======================================================================
    def _get_version_dict(self, version_id: str) -> dict:
        raw = self.versions.get(version_id)
        if raw is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown version {version_id}")
        return json.loads(raw)

    def _get_proposal_dict(self, proposal_id: str) -> dict:
        raw = self.proposals.get(proposal_id)
        if raw is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown proposal {proposal_id}")
        return json.loads(raw)

    def _save_proposal(self, p: dict) -> None:
        self.proposals[p["proposal_id"]] = json.dumps(p)

    def _pending_ids(self, base_version_id: str) -> list:
        raw = self.pending_by_base.get(base_version_id)
        return json.loads(raw) if raw else []

    def _add_pending(self, base_version_id: str, proposal_id: str) -> None:
        ids = self._pending_ids(base_version_id)
        ids.append(proposal_id)
        self.pending_by_base[base_version_id] = json.dumps(ids)

    def _remove_pending(self, base_version_id: str, proposal_id: str) -> None:
        ids = [i for i in self._pending_ids(base_version_id) if i != proposal_id]
        self.pending_by_base[base_version_id] = json.dumps(ids)

    def _create_version(self, state: dict, base_version_id: str, via: list) -> str:
        seq = int(self.version_seq) + 1
        self.version_seq = u256(seq)
        version_id = f"v{seq}"
        self.versions[version_id] = json.dumps({
            "version_id": version_id,
            "base_version_id": base_version_id,
            "state_json": json.dumps(state),
            "via_proposals": via,
            "seq": seq,
        })
        children = self.get_children(base_version_id)
        children.append(version_id)
        self.version_children[base_version_id] = json.dumps(children)
        return version_id

    # ----------------------------------------------------------------
    # Escrow: zero the ledger field, persist, THEN transfer. Never the
    # reverse — see module docstring / ESCROW.
    # ----------------------------------------------------------------
    def _refund(self, p: dict) -> None:
        bond = int(p["bond_deposited"])
        if bond <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No bond deposited for {p['proposal_id']}")
        p["bond_deposited"] = "0"
        self._save_proposal(p)
        self.total_locked_wei = u256(max(0, int(self.total_locked_wei) - bond))
        _send_gen(p["proposer"], bond)

    # ======================================================================
    # WRITE: submit_proposal — the escrow entry point.
    # ======================================================================
    @gl.public.write.payable
    def submit_proposal(
        self,
        base_version_id: str,
        field_path: str,
        new_value_json: str,
        description: str,
        evidence_url: str = "",
        image_b64: str = "",
    ) -> str:
        """
        Register a proposed change against `base_version_id`.

        Only `gl.message.value` is trusted as the bond amount — never a
        parameter. The proposal starts PENDING and, if it carries external
        evidence or an image, EVIDENCE_PENDING until `attest_web_evidence`
        / `attest_image_evidence` is called. Evidence must be attested
        before the proposal is eligible for `commit_solo` or
        `attempt_merge`, so every semantic judgment is grounded in
        consensus-agreed facts rather than a live re-fetch.
        """
        self._get_version_dict(base_version_id)  # raises if base is unknown

        if gl.message.value < u256(MIN_BOND_WEI):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Must lock at least {MIN_BOND_WEI} wei bond")
        try:
            json.loads(new_value_json)
        except Exception:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} new_value_json must be valid JSON")
        _split_field_path(field_path)  # validates shape, raises on bad input
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} description exceeds {MAX_DESCRIPTION_CHARS} chars")
        if image_b64:
            try:
                base64.b64decode(image_b64, validate=True)
            except Exception:
                raise gl.vm.UserError(f"{ERROR_EXPECTED} image_b64 is not valid base64")

        seq = int(self.proposal_seq) + 1
        self.proposal_seq = u256(seq)
        proposal_id = f"p{seq}"
        bond = int(gl.message.value)

        needs_evidence = bool(evidence_url) or bool(image_b64)
        p = {
            "proposal_id": proposal_id,
            "base_version_id": base_version_id,
            "proposer": str(gl.message.sender_address),
            "field_path": field_path,
            "new_value_json": new_value_json,
            "description": description,
            "status": STATUS_PENDING,
            "verdict": "",
            "bond_wei": str(bond),
            "bond_deposited": str(bond),
            "created_seq": seq,
            # -- evidence subsystem --
            "evidence_url": evidence_url,
            "web_evidence_status": EVIDENCE_PENDING if evidence_url else EVIDENCE_NONE,
            "web_evidence_digest": "",
            "image_b64": image_b64,
            "image_evidence_status": EVIDENCE_PENDING if image_b64 else EVIDENCE_NONE,
            "image_document_type": "",
            "image_key_fact": "",
            "evidence_required": needs_evidence,
        }
        self._save_proposal(p)
        self._add_pending(base_version_id, proposal_id)
        self._index_by_proposer(p["proposer"], proposal_id)
        self.total_locked_wei = u256(int(self.total_locked_wei) + bond)
        return proposal_id

    def _index_by_proposer(self, proposer: str, proposal_id: str) -> None:
        raw = self.proposals_by_proposer.get(proposer)
        ids = json.loads(raw) if raw else []
        ids.append(proposal_id)
        self.proposals_by_proposer[proposer] = json.dumps(ids)

    # ======================================================================
    # WRITE: attest_web_evidence — nondeterministic web fetch brought to
    # consensus via the STRICT equality principle. Static/stable sources
    # only (see module docstring); this is intentionally not an LLM call:
    # the fetched text itself is the fact, so validators must reproduce it
    # byte-for-byte rather than merely agree on a judgment about it.
    # ======================================================================
    @gl.public.write
    def attest_web_evidence(self, proposal_id: str) -> str:
        p = self._get_proposal_dict(proposal_id)
        if p["web_evidence_status"] != EVIDENCE_PENDING:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No pending web evidence for {proposal_id}")

        url = p["evidence_url"]

        def fetch_digest() -> str:
            response = gl.nondet.web.get(url)
            if response.status >= 500:
                raise gl.vm.UserError(f"{ERROR_TRANSIENT} Evidence source returned {response.status}")
            if response.status >= 400:
                raise gl.vm.UserError(f"{ERROR_EXTERNAL} Evidence source returned {response.status}")
            raw_body = response.body or b""
            body = raw_body.decode("utf-8", errors="replace")
            return body[:MAX_EVIDENCE_DIGEST_CHARS]

        try:
            digest = gl.eq_principle.strict_eq(fetch_digest)
            p["web_evidence_digest"] = digest
            p["web_evidence_status"] = EVIDENCE_ATTESTED
        except gl.vm.UserError as e:
            msg = str(e)
            if msg.startswith(ERROR_TRANSIENT):
                # leave PENDING so it can be retried once the source recovers
                raise
            p["web_evidence_status"] = EVIDENCE_FAILED
            p["web_evidence_digest"] = msg[:MAX_EVIDENCE_DIGEST_CHARS]

        self._save_proposal(p)
        return p["web_evidence_status"]

    # ======================================================================
    # WRITE: attest_image_evidence — vision-model classification brought to
    # consensus via a custom comparative validator. Only the bounded
    # `document_type` enum is compared field-for-field; the free-text
    # `key_fact` is informational only and is never used as a consensus
    # gate, exactly to avoid free-form-text disagreement.
    # ======================================================================
    @gl.public.write
    def attest_image_evidence(self, proposal_id: str) -> str:
        p = self._get_proposal_dict(proposal_id)
        if p["image_evidence_status"] != EVIDENCE_PENDING:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No pending image evidence for {proposal_id}")

        try:
            image_bytes = base64.b64decode(p["image_b64"], validate=True)
        except Exception:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Stored image_b64 is not valid base64")

        allowed = ", ".join(IMAGE_DOC_TYPES)

        def leader_fn() -> dict:
            prompt = (
                "Classify this image for a change-management record. "
                f"Respond as JSON with exactly two fields: "
                f'"document_type" (one of: {allowed}) and '
                '"key_fact" (a single factual sentence, under 200 characters, '
                "describing only what is visibly shown — no speculation). "
                'Example: {"document_type": "config_screenshot", "key_fact": "..."}'
            )
            raw = gl.nondet.exec_prompt(prompt, images=[image_bytes], response_format="json")
            parsed = raw if isinstance(raw, dict) else _parse_json_object(str(raw))
            doc_type = _coerce_str(parsed.get("document_type"), 64).lower()
            if doc_type not in IMAGE_DOC_TYPES:
                doc_type = "other"
            key_fact = _coerce_str(parsed.get("key_fact"), MAX_IMAGE_FACT_CHARS)
            return {"document_type": doc_type, "key_fact": key_fact}

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _handle_leader_error(leaders_res, leader_fn)
            mine = leader_fn()
            # Only the bounded enum field is a consensus gate.
            return mine["document_type"] == leaders_res.calldata["document_type"]

        try:
            result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
            p["image_document_type"] = result["document_type"]
            p["image_key_fact"] = result["key_fact"]
            p["image_evidence_status"] = EVIDENCE_ATTESTED
        except gl.vm.UserError as e:
            p["image_evidence_status"] = EVIDENCE_FAILED
            p["image_key_fact"] = str(e)[:MAX_IMAGE_FACT_CHARS]

        self._save_proposal(p)
        return p["image_evidence_status"]

    def _evidence_ready(self, p: dict) -> bool:
        if p["web_evidence_status"] == EVIDENCE_PENDING:
            return False
        if p["image_evidence_status"] == EVIDENCE_PENDING:
            return False
        return True

    # ======================================================================
    # WRITE: commit_solo — deterministic fast path. Legal only when a
    # proposal is the sole pending item on its base, so there is nothing to
    # coordinate and no model call is warranted.
    # ======================================================================
    @gl.public.write
    def commit_solo(self, proposal_id: str) -> str:
        p = self._get_proposal_dict(proposal_id)
        if p["status"] != STATUS_PENDING:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Proposal is not pending")
        if not self._evidence_ready(p):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence attestation is still pending")

        pending = self._pending_ids(p["base_version_id"])
        if pending != [proposal_id]:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Other proposals are pending on this base; use attempt_merge"
            )

        base = self._get_version_dict(p["base_version_id"])
        new_state = _apply_field_path(
            json.loads(base["state_json"]), p["field_path"], json.loads(p["new_value_json"])
        )
        new_version_id = self._create_version(new_state, p["base_version_id"], [proposal_id])

        p["status"] = STATUS_MERGED
        p["verdict"] = "solo"
        self._save_proposal(p)
        self._remove_pending(p["base_version_id"], proposal_id)
        self.total_merged = u256(int(self.total_merged) + 1)
        self._refund(p)
        return new_version_id

    # ======================================================================
    # WRITE: cancel_proposal / claim_timeout_refund — the recovery exits.
    # ======================================================================
    @gl.public.write
    def cancel_proposal(self, proposal_id: str) -> None:
        p = self._get_proposal_dict(proposal_id)
        if str(gl.message.sender_address) != p["proposer"]:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only the proposer can cancel")
        if p["status"] not in (STATUS_PENDING, STATUS_BLOCKED):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Cannot cancel from status {p['status']}")

        p["status"] = STATUS_CANCELLED
        self._save_proposal(p)
        self._remove_pending(p["base_version_id"], proposal_id)
        self.total_cancelled = u256(int(self.total_cancelled) + 1)
        self._refund(p)

    @gl.public.write
    def claim_timeout_refund(self, proposal_id: str) -> None:
        p = self._get_proposal_dict(proposal_id)
        if p["status"] not in (STATUS_PENDING, STATUS_BLOCKED):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Proposal is not stuck")
        gap = int(self.proposal_seq) - int(p["created_seq"])
        if gap < TIMEOUT_PROPOSAL_GAP:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Timeout window not reached ({gap}/{TIMEOUT_PROPOSAL_GAP})"
            )

        p["status"] = STATUS_TIMED_OUT
        self._save_proposal(p)
        self._remove_pending(p["base_version_id"], proposal_id)
        self.total_timed_out = u256(int(self.total_timed_out) + 1)
        self._refund(p)

    # ======================================================================
    # WRITE: attempt_merge — the core coordination primitive.
    # ======================================================================
    @gl.public.write
    def attempt_merge(self, proposal_id_a: str, proposal_id_b: str) -> str:
        """
        Attempt to reconcile two PENDING proposals sharing the same base.

        Deterministic structural conflict (identical or overlapping field
        paths) is resolved without any model call. Otherwise, GenLayer
        validators classify the pair using the bounded verdict vocabulary
        and only that classification — never a rewritten value — reaches
        consensus. See module docstring for the full rationale.
        """
        if proposal_id_a == proposal_id_b:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Cannot merge a proposal with itself")

        a = self._get_proposal_dict(proposal_id_a)
        b = self._get_proposal_dict(proposal_id_b)
        for p in (a, b):
            if p["status"] != STATUS_PENDING:
                raise gl.vm.UserError(f"{ERROR_EXPECTED} Proposal {p['proposal_id']} is not pending")
            if not self._evidence_ready(p):
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} Proposal {p['proposal_id']} evidence attestation is pending"
                )
        if a["base_version_id"] != b["base_version_id"]:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Proposals must share the same base version")

        subsumed_key = None
        if _field_paths_overlap(a["field_path"], b["field_path"]):
            verdict = VERDICT_CONFLICT
        else:
            verdict, subsumed_key = self._judge_semantic_compatibility(a, b)

        return self._finalize_verdict(a, b, verdict, subsumed_key)

    # ----------------------------------------------------------------
    # Nondeterministic semantic judgment. Both leader and validator run
    # the identical, purely-computational prompt construction below
    # (reading only already-attested, stored evidence — no live fetch, no
    # fresh vision call), then compare exactly two bounded fields:
    # `verdict` (a 4-way enum) and, when applicable, `subsumed` (a 2-way
    # enum). This is the comparative equivalence principle applied by
    # hand for full control over the tolerance rule.
    # ----------------------------------------------------------------
    def _judge_semantic_compatibility(self, a: dict, b: dict) -> tuple:
        def describe(p: dict, label: str) -> str:
            lines = [
                f"{label} — field `{p['field_path']}` -> {p['new_value_json']}",
                f"Description: {p['description']}",
            ]
            if p["web_evidence_status"] == EVIDENCE_ATTESTED:
                lines.append(f"Attested web evidence: {p['web_evidence_digest']}")
            if p["image_evidence_status"] == EVIDENCE_ATTESTED:
                lines.append(
                    f"Attested image ({p['image_document_type']}): {p['image_key_fact']}"
                )
            return "\n".join(lines)

        prompt = f"""You are a structural/semantic merge judge for a shared state store.
Two proposals target the SAME base version but touch DIFFERENT, non-overlapping
fields. Decide whether they are safe to apply together, exactly as submitted,
with no changes to either proposal's content.

{describe(a, "Proposal A")}

{describe(b, "Proposal B")}

Classify the relationship using EXACTLY one of these labels:
- "commute": both proposals may be applied together with no conflict in meaning.
- "conflict": applying both together would contradict or undermine one another,
  even though they touch different fields (e.g. one disables a capability that
  the other relies on).
- "subsumes": one proposal's effect already fully covers the other's intent,
  making the other redundant. If chosen, set "subsumed" to "a" or "b" to name
  which proposal is the redundant one.
- "ambiguous": there is not enough information in the descriptions and
  evidence above to decide safely.

Respond as JSON with exactly these fields:
{{"verdict": "commute|conflict|subsumes|ambiguous", "subsumed": "a"|"b"|null, "reasoning": "<one sentence>"}}"""

        def leader_fn() -> dict:
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            parsed = raw if isinstance(raw, dict) else _parse_json_object(str(raw))
            verdict = _coerce_str(parsed.get("verdict"), 32).lower()
            if verdict not in VALID_VERDICTS:
                raise gl.vm.UserError(f"{ERROR_LLM} Invalid verdict label: {verdict!r}")
            subsumed = parsed.get("subsumed")
            subsumed = subsumed if subsumed in ("a", "b") else None
            if verdict == VERDICT_SUBSUMES and subsumed is None:
                raise gl.vm.UserError(f"{ERROR_LLM} subsumes verdict missing 'subsumed' side")
            return {"verdict": verdict, "subsumed": subsumed}

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _handle_leader_error(leaders_res, leader_fn)
            mine = leader_fn()
            leader_out = leaders_res.calldata
            if mine["verdict"] != leader_out["verdict"]:
                return False
            if mine["verdict"] == VERDICT_SUBSUMES:
                return mine["subsumed"] == leader_out["subsumed"]
            return True

        outcome = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        return outcome["verdict"], outcome.get("subsumed")

    # ----------------------------------------------------------------
    # Deterministic settlement of a verdict. The model's output is never
    # written into state beyond the bounded label itself; every state
    # mutation here (version creation, status changes, refunds) is plain
    # contract code.
    # ----------------------------------------------------------------
    def _finalize_verdict(
        self, a: dict, b: dict, verdict: str, subsumed_key: typing.Optional[str]
    ) -> str:
        if verdict == VERDICT_COMMUTE:
            base = self._get_version_dict(a["base_version_id"])
            state = json.loads(base["state_json"])
            state = _apply_field_path(state, a["field_path"], json.loads(a["new_value_json"]))
            state = _apply_field_path(state, b["field_path"], json.loads(b["new_value_json"]))
            new_version_id = self._create_version(
                state, a["base_version_id"], [a["proposal_id"], b["proposal_id"]]
            )
            for p in (a, b):
                p["status"] = STATUS_MERGED
                p["verdict"] = verdict
                self._save_proposal(p)
                self._remove_pending(p["base_version_id"], p["proposal_id"])
            self.total_merged = u256(int(self.total_merged) + 2)
            self._refund(a)
            self._refund(b)
            return new_version_id

        if verdict in (VERDICT_CONFLICT, VERDICT_AMBIGUOUS):
            # Neither proposal is discarded automatically: bonds stay
            # locked until the author cancels or the timeout window opens.
            for p in (a, b):
                p["status"] = STATUS_BLOCKED
                p["verdict"] = verdict
                self._save_proposal(p)
            self.total_blocked = u256(int(self.total_blocked) + 2)
            return verdict

        if verdict == VERDICT_SUBSUMES:
            subsumed, remaining = (a, b) if subsumed_key == "a" else (b, a)
            subsumed["status"] = STATUS_SUBSUMED
            subsumed["verdict"] = verdict
            self._save_proposal(subsumed)
            self._remove_pending(subsumed["base_version_id"], subsumed["proposal_id"])
            self._refund(subsumed)
            # `remaining` stays PENDING: it still needs its own commit_solo
            # (once it is the sole pending item) or a future attempt_merge.
            remaining["verdict"] = verdict
            self._save_proposal(remaining)
            return verdict

        raise gl.vm.UserError(f"{ERROR_LLM} Unhandled verdict {verdict!r}")
