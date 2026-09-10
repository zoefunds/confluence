# Confluence

A reusable GenLayer Intelligent Contract primitive: **semantic merge
coordination for multi-agent systems** sharing one piece of state (a config
file, a project plan, a protocol manifest, a policy document — anything
modeled as a JSON object).

Live deployment (StudioNet): [`0x0175e2c837E2b9516e1c2F79fC6435aA894EF94B`](https://studio.genlayer.com/)

## Table of contents

- [The problem it solves](#the-problem-it-solves)
- [How GenLayer consensus is used](#how-genlayer-consensus-is-used)
- [Web fetch and image interpretation](#web-fetch-and-image-interpretation-are-pulled-into-consensus-once-up-front)
- [Escrow](#escrow)
- [Version graph](#version-graph)
- [What this is not](#what-this-is-not)
- [Repository layout](#repository-layout)
- [Deploying](#deploying)
- [Running the tests](#running-the-tests)
  - [Direct-mode tests (fast, mocked)](#direct-mode-tests-fast-mocked)
  - [Live test suite (real network, nothing mocked)](#live-test-suite-real-network-nothing-mocked)
- [A note on the `genlayer` CLI](#a-note-on-the-genlayer-cli)
- [Example flow](#example-flow)

## The problem it solves

Multiple autonomous agents can each propose a change against a known base
version of shared state without a central coordinator deciding, by fiat,
whether two concurrent proposals are safe to apply together. Two proposals
can be **structurally disjoint** (they touch different JSON fields) yet
still **semantically conflict** — e.g. "disable password authentication"
vs. "allow password authentication for emergency recovery accounts."
Detecting that requires reading intent, which a plain diff can't do, but it
also can't be handed to one off-chain LLM call, because whoever runs that
call would unilaterally control the shared state.

## How GenLayer consensus is used

`attempt_merge(proposal_a, proposal_b)`:

1. **Deterministic gate first.** If the two proposals' field paths are
   identical, or one is a path-prefix of the other (editing `auth` wholesale
   while another proposal edits `auth.password`), the contract calls it
   `conflict` with zero model calls. Deterministic code can't disagree with
   itself, so this path can never produce an `Undetermined` consensus result.
2. **Bounded classification only when actually needed.** If the proposals
   are genuinely disjoint, GenLayer validators classify the pair using
   `gl.vm.run_nondet_unsafe` with a hand-written comparative validator. The
   model returns **one label from a closed set** — `commute`, `conflict`,
   `subsumes`, or `ambiguous` (plus, for `subsumes`, which side is
   redundant) — never a rewritten merged value. The validator reruns the
   same prompt construction independently and the two calldata payloads are
   compared field-for-field on just those bounded fields, which is what
   keeps independent validators converging instead of diverging over
   free-form prose.
3. **Deterministic settlement.** On `commute`, contract code — not the
   model — applies both original proposals, verbatim, on top of the shared
   base to produce a new, immutable version node. Every other verdict only
   changes proposal status; it never touches the version graph.

### Web fetch and image interpretation are pulled into consensus once, up front

`attest_web_evidence` and `attest_image_evidence` run **before** a proposal
is eligible for `commit_solo` or `attempt_merge`, each behind their own
equivalence principle:

- `attest_web_evidence` uses `gl.eq_principle.strict_eq` — the fetched page
  text itself is the fact, so validators must reproduce it exactly. Intended
  for stable/static evidence sources, as noted in the contract's docstring.
- `attest_image_evidence` sends the (base64-decoded) image to a vision model
  via `gl.nondet.exec_prompt(images=[...])`, with a custom comparative
  validator that gates consensus on the bounded `document_type` enum
  (`config_screenshot`, `diagram`, `signed_approval`, `chat_or_email_excerpt`,
  `chart_or_dashboard`, `other`). The model's free-text `key_fact` is stored
  solely as an informational display field and is never supplied to merge
  adjudication or any other consequential decision. This prevents
  contradictory image prose from influencing a merge while avoiding fragile
  exact-equality comparisons between independently generated LLM sentences.

Pending evidence blocks `commit_solo` and `attempt_merge`. If an evidence
source fails, it supplies no evidence-derived data to either operation; the
contract never retries a live fetch or vision call from inside merge
adjudication.

The contract stores evidence once. Merge adjudication reads the consensus
attested web digest and image document type rather than re-fetching a page or
re-describing an image. The informational image fact is specifically excluded.
Thus a changed page or a different image caption cannot become a fresh source
of disagreement inside the merge itself. This keeps consequential
nondeterministic work bounded while avoiding fragile exact comparison of LLM
prose.

## Escrow

Every `submit_proposal` call is `@gl.public.write.payable` and requires a
1 GEN anti-spam bond, read only from `gl.message.value` (never a caller
-supplied parameter). The bond is custody, and it always returns to the
proposer through exactly one of five exits — see `_refund` and the module
docstring in [`confluence.py`](confluence.py) for the full enumeration:

| Exit | Trigger | Result |
|---|---|---|
| Merged | `commit_solo` or a `commute` verdict | Full refund |
| Subsumed | `subsumes` verdict names this side redundant | Full refund |
| Cancelled | Proposer calls `cancel_proposal` | Full refund |
| Timed out | Nobody resolved it within `TIMEOUT_PROPOSAL_GAP` submissions | Proposer reclaims via `claim_timeout_refund` |
| Blocked | `conflict` or `ambiguous` verdict | Bond stays locked (not seized, not paid to anyone) until the proposer cancels or the timeout window opens |

Every exit zeroes the `bond_deposited` ledger field and persists state
**before** calling the single `_send_gen` transfer function, so a bond can
never be paid out twice.

## Version graph

Every successful resolution creates an immutable version node recording its
base version id and the exact proposal id(s) that produced it. `get_version`,
`get_children`, `get_ancestry`, `is_ancestor`, `list_versions`, and
`diff_versions` let any external agent or indexer reconstruct full
provenance without trusting one party's account of history.

## What this is not

This is a coordination primitive, not a product. It does not own agent
authentication ("who may propose what"), a UI for browsing the graph, or
notification of interested parties — those belong in a client built on top.

## Repository layout

```
confluence.py              the contract (single-file GenVM Python)
tests/direct/               fast, mocked, in-process pytest suite
livetest/lib.mjs            genlayer-js helpers for driving the real network
livetest/run_all.mjs        full live regression suite (real consensus, no mocks)
```

## Deploying

Constructor takes a single `genesis_state_json` string argument — the
starting JSON object that becomes version `v0`:

```json
{"auth": {"password_enabled": true, "mfa_required": false}, "network": {"region": "us-east"}}
```

Deploy via the GenLayer Studio UI, or with `genlayer deploy --contract confluence.py --args '<json string>'`.

## Running the tests

### Direct-mode tests (fast, mocked)

In-process, no network — leader path only, web/LLM calls mocked.

```bash
pip install genlayer-test
pytest tests/direct/ -v
```

16 tests cover: bond validation, the deterministic structural/overlap
conflict gate, `commute`/`conflict`/`subsumes` verdicts, both evidence
-attestation flows (including exclusion of informational image facts from
merge adjudication), every escrow exit (merged, subsumed, cancelled,
blocked-then-cancelled, timeout-guard), and version-graph provenance.

### Live test suite (real network, nothing mocked)

Exercises all seven write methods against a real deployment on **StudioNet**
— real consensus rounds, a real LLM classification call, a real HTTP fetch,
a real vision-model call, and real GEN escrow.

```bash
npm install

# Create two dedicated keystores once (StudioNet is gasless, so a 0 GEN
# balance is fine — no faucet needed):
genlayer account create --name confluence-alice --password "<your-password>"
genlayer account create --name confluence-bob   --password "<your-password>"

CONFLUENCE_KEYSTORE_PASSWORD="<your-password>" \
CONFLUENCE_CONTRACT="0x0175e2c837E2b9516e1c2F79fC6435aA894EF94B" \
node livetest/run_all.mjs
```

The suite runs 26 steps end-to-end: solo commit, a real LLM-judged `commute`
merge, a deterministic `conflict` (no model call), bond cancellation, web
-evidence attestation gating a commit, image-evidence attestation, a real
LLM-judged `subsumes` verdict, and a timeout-guard rejection — printing
pass/fail per step and failing the process (exit code 1) if anything
regresses. The StudioNet deployment above was verified on 2026-09-10: all
26 steps passed, including a successful image attestation (`document_type:
other`). The two intentionally pending test proposals were then cancelled,
leaving `total_locked_wei` at `0`.

## A note on the `genlayer` CLI

The published `genlayer` CLI hardcodes `value: 0n` on `genlayer write`, so it
cannot drive `@gl.public.write.payable` methods (`submit_proposal` locks a
real GEN bond). The live suite therefore uses `genlayer-js`
(`createClient`/`createAccount`) and dedicated named keystores loaded from
`~/.genlayer/keystores/<name>.json` rather than the CLI's global active
account.

## Example flow

```js
import { clientFor, write, call, gen, parseJson } from "./livetest/lib.mjs";

const alice = await clientFor("confluence-alice");
const bob = await clientFor("confluence-bob");

const { returnValue: p1 } = await write(
  alice, "submit_proposal",
  ["v0", "network.region", '"eu-west"', "Move to EU", "", ""],
  { value: gen(1) }
);
const { returnValue: v1 } = await write(alice, "commit_solo", [p1]);

const { returnValue: p2 } = await write(
  alice, "submit_proposal",
  [v1, "network.region", '"eu-west"', "Move to EU", "", ""],
  { value: gen(1) }
);
const { returnValue: p3 } = await write(
  bob, "submit_proposal",
  [v1, "auth.mfa_required", "true", "Require MFA", "", ""],
  { value: gen(1) }
);
// Disjoint, non-conflicting fields -> real LLM consensus round -> "commute" -> new version
await write(alice, "attempt_merge", [p2, p3]);

const { returnValue: p4 } = await write(
  alice, "submit_proposal",
  [v1, "auth.password_enabled", "false", "Disable passwords", "", ""],
  { value: gen(1) }
);
const { returnValue: p5 } = await write(
  bob, "submit_proposal",
  [v1, "auth.recovery_password_enabled", "true", "Allow password for emergency recovery", "", ""],
  { value: gen(1) }
);
// Structurally disjoint, semantically conflicting -> validators return "conflict"
await write(alice, "attempt_merge", [p4, p5]);
```
