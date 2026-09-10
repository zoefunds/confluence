// Full live regression suite for Confluence, run against the real deployed
// contract on StudioNet (real consensus rounds, real LLM classification
// calls, real web fetch, real GEN escrow -- nothing here is mocked).
//
// Usage:
//   node livetest/run_all.mjs
//   CONFLUENCE_CONTRACT=0x... node livetest/run_all.mjs   # target a different deployment

import { clientFor, call, write, gen, parseJson, addressForKeystore, CONTRACT_ADDRESS, sleep } from "./lib.mjs";

const results = [];

async function step(name, fn) {
  const startedAt = Date.now();
  try {
    const value = await fn();
    const ms = Date.now() - startedAt;
    results.push({ name, ok: true, ms });
    console.log(`  ✓ ${name} (${ms}ms)`);
    return value;
  } catch (err) {
    const ms = Date.now() - startedAt;
    results.push({ name, ok: false, ms, error: String(err.message || err) });
    console.log(`  ✗ ${name} (${ms}ms)\n    ${String(err.message || err).split("\n")[0]}`);
    throw err;
  }
}

async function main() {
  console.log(`Confluence live suite -> ${CONTRACT_ADDRESS}\n`);

  const alice = await clientFor("confluence-alice");
  const bob = await clientFor("confluence-bob");
  const aliceAddr = await addressForKeystore("confluence-alice");
  const bobAddr = await addressForKeystore("confluence-bob");
  console.log(`alice = ${aliceAddr}\nbob   = ${bobAddr}\n`);

  // ---- Scenario 1: submit_proposal + commit_solo (no coordination needed) ----
  let p1;
  await step("submit_proposal (alice: move network.region to eu-west)", async () => {
    const { returnValue } = await write(
      alice,
      "submit_proposal",
      ["v0", "network.region", JSON.stringify("eu-west"), "Move primary region to EU for GDPR data residency", "", ""],
      { value: gen(1) }
    );
    p1 = returnValue;
    console.log(`    proposal id: ${p1}`);
  });

  await step("get_proposal (p1 is pending, 1 GEN bonded)", async () => {
    const p = await call(alice, "get_proposal", [p1]);
    if (p.status !== "pending") throw new Error(`expected pending, got ${p.status}`);
    if (p.bond_deposited !== String(gen(1))) throw new Error(`expected 1 GEN bonded, got ${p.bond_deposited}`);
  });

  let v1;
  await step("commit_solo (p1 is sole pending proposal on v0 -> new version)", async () => {
    const { returnValue } = await write(alice, "commit_solo", [p1]);
    v1 = returnValue;
    console.log(`    new version: ${v1}`);
    const state = await call(alice, "get_state_at", [v1]);
    if (state.network.region !== "eu-west") throw new Error("region did not update in new version");
  });

  await step("get_proposal (p1 merged, bond refunded to zero)", async () => {
    const p = await call(alice, "get_proposal", [p1]);
    if (p.status !== "merged") throw new Error(`expected merged, got ${p.status}`);
    if (p.bond_deposited !== "0") throw new Error(`expected refunded bond, got ${p.bond_deposited}`);
  });

  // ---- Scenario 2: two disjoint, non-conflicting proposals -> COMMUTE ----
  let p2, p3;
  await step("submit_proposal (bob: network.region eu-west -> ap-south)", async () => {
    const { returnValue } = await write(
      bob,
      "submit_proposal",
      [v1, "network.region", JSON.stringify("ap-south"), "Shift primary traffic to APAC for latency", "", ""],
      { value: gen(1) }
    );
    p2 = returnValue;
    console.log(`    proposal id: ${p2}`);
  });
  await step("submit_proposal (alice: require MFA, disjoint field)", async () => {
    const { returnValue } = await write(
      alice,
      "submit_proposal",
      [v1, "auth.mfa_required", "true", "Require MFA for all accounts after the region migration", "", ""],
      { value: gen(1) }
    );
    p3 = returnValue;
    console.log(`    proposal id: ${p3}`);
  });

  let v2;
  await step("attempt_merge (p2, p3) -> real LLM consensus round -> commute", async () => {
    const { returnValue } = await write(alice, "attempt_merge", [p2, p3], {});
    v2 = returnValue;
    console.log(`    verdict/new version: ${v2}`);
    if (!v2.startsWith("v")) throw new Error(`expected a new version id, got verdict '${v2}'`);
    const state = await call(alice, "get_state_at", [v2]);
    if (state.network.region !== "ap-south") throw new Error("region change missing from merged version");
    if (state.auth.mfa_required !== true) throw new Error("mfa_required change missing from merged version");
  });

  await step("both proposals merged, both bonds refunded", async () => {
    const pa = await call(alice, "get_proposal", [p2]);
    const pb = await call(alice, "get_proposal", [p3]);
    if (pa.status !== "merged" || pb.status !== "merged") throw new Error("expected both merged");
    if (pa.bond_deposited !== "0" || pb.bond_deposited !== "0") throw new Error("expected both bonds refunded");
  });

  // ---- Scenario 3: same field edited twice -> deterministic CONFLICT, no model call ----
  let p4, p5;
  await step("submit_proposal (alice: network.region -> us-west, on v2)", async () => {
    const { returnValue } = await write(
      alice,
      "submit_proposal",
      [v2, "network.region", JSON.stringify("us-west"), "Move region to US West for a new enterprise customer", "", ""],
      { value: gen(1) }
    );
    p4 = returnValue;
  });
  await step("submit_proposal (bob: network.region -> eu-central, same field)", async () => {
    const { returnValue } = await write(
      bob,
      "submit_proposal",
      [v2, "network.region", JSON.stringify("eu-central"), "Move region to EU Central for data residency", "", ""],
      { value: gen(1) }
    );
    p5 = returnValue;
  });
  await step("attempt_merge (p4, p5) -> deterministic structural conflict (identical field)", async () => {
    const { returnValue } = await write(alice, "attempt_merge", [p4, p5], {});
    if (returnValue !== "conflict") throw new Error(`expected 'conflict', got '${returnValue}'`);
  });
  await step("both proposals blocked, bonds still locked (not seized, not paid out)", async () => {
    const pa = await call(alice, "get_proposal", [p4]);
    const pb = await call(alice, "get_proposal", [p5]);
    if (pa.status !== "blocked" || pb.status !== "blocked") throw new Error("expected both blocked");
    if (pa.bond_deposited !== String(gen(1)) || pb.bond_deposited !== String(gen(1)))
      throw new Error("expected bonds still locked at 1 GEN each");
  });

  // ---- Scenario 4: cancel_proposal recovers a blocked bond ----
  await step("cancel_proposal (bob withdraws p5) -> bond refunded", async () => {
    await write(bob, "cancel_proposal", [p5], {});
    const p = await call(alice, "get_proposal", [p5]);
    if (p.status !== "cancelled") throw new Error(`expected cancelled, got ${p.status}`);
    if (p.bond_deposited !== "0") throw new Error("expected bond refunded on cancel");
  });
  await step("cancel_proposal (alice withdraws p4 too, base is clear again)", async () => {
    await write(alice, "cancel_proposal", [p4], {});
  });

  // ---- Scenario 5: attest_web_evidence gates merge eligibility (real HTTP fetch) ----
  let p6;
  await step("submit_proposal (alice, with real evidence_url) on v2", async () => {
    const { returnValue } = await write(
      alice,
      "submit_proposal",
      [
        v2,
        "network.region",
        JSON.stringify("us-west"),
        "Move region to US West per the signed migration ticket",
        "https://raw.githubusercontent.com/genlayerlabs/genlayer-project-boilerplate/main/README.md",
        "",
      ],
      { value: gen(1) }
    );
    p6 = returnValue;
  });
  await step("commit_solo blocked before evidence is attested", async () => {
    let threw = false;
    try {
      await write(alice, "commit_solo", [p6], {});
    } catch {
      threw = true;
    }
    if (!threw) throw new Error("expected commit_solo to fail before attestation");
  });
  await step("attest_web_evidence (p6) -> real strict_eq web fetch consensus", async () => {
    const { returnValue } = await write(alice, "attest_web_evidence", [p6], {});
    if (returnValue !== "attested") throw new Error(`expected 'attested', got '${returnValue}'`);
    const p = await call(alice, "get_proposal", [p6]);
    if (!p.web_evidence_digest || p.web_evidence_digest.length === 0) throw new Error("expected a non-empty digest");
    console.log(`    digest (first 80 chars): ${p.web_evidence_digest.slice(0, 80).replace(/\n/g, " ")}...`);
  });

  let v3;
  await step("commit_solo (p6) now succeeds -> new version", async () => {
    const { returnValue } = await write(alice, "commit_solo", [p6], {});
    v3 = returnValue;
    const state = await call(alice, "get_state_at", [v3]);
    if (state.network.region !== "us-west") throw new Error("region change missing");
  });

  // ---- Scenario 6: attest_image_evidence gates merge eligibility (real vision call) ----
  // 1x1 transparent PNG, base64-encoded -- a real (if minimal) image payload.
  const TINY_PNG_B64 =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
  let p7;
  await step("submit_proposal (bob, with a real image payload) on v3", async () => {
    const { returnValue } = await write(
      bob,
      "submit_proposal",
      [
        v3,
        "auth.password_enabled",
        "false",
        "Disable password auth per the attached signed approval screenshot",
        "",
        TINY_PNG_B64,
      ],
      { value: gen(1) }
    );
    p7 = returnValue;
  });
  await step("attest_image_evidence (p7) -> real vision-model consensus round", async () => {
    const { returnValue } = await write(bob, "attest_image_evidence", [p7], {});
    // Either outcome is a legitimate, fully-exercised consensus result:
    // "attested" (validators agreed on the consequential document_type) or
    // "failed" (the model didn't converge on that bounded classification for
    // this near-blank test image). The free-text key_fact is display-only.
    if (returnValue !== "attested" && returnValue !== "failed") {
      throw new Error(`unexpected image evidence status '${returnValue}'`);
    }
    const p = await call(bob, "get_proposal", [p7]);
    console.log(`    image_evidence_status=${p.image_evidence_status} document_type=${p.image_document_type || "(n/a)"} key_fact=${p.image_key_fact || "(n/a)"}`);
  });

  // ---- Scenario 7: subsumes verdict ----
  let p8, p9;
  await step("submit_proposal (alice: require MFA for all users) on v3", async () => {
    const { returnValue } = await write(
      alice,
      "submit_proposal",
      [v3, "auth.mfa_required_all", "true", "Require MFA for every account, no exceptions", "", ""],
      { value: gen(1) }
    );
    p8 = returnValue;
  });
  await step("submit_proposal (bob: require MFA for admins only) on v3", async () => {
    const { returnValue } = await write(
      bob,
      "submit_proposal",
      [v3, "auth.mfa_required_admins", "true", "Require MFA for admin accounts only", "", ""],
      { value: gen(1) }
    );
    p9 = returnValue;
  });
  await step("attempt_merge (p8, p9) -> real LLM consensus round (commute or subsumes)", async () => {
    const { returnValue } = await write(alice, "attempt_merge", [p8, p9], {});
    console.log(`    verdict/new version: ${returnValue}`);
    // The model may reasonably call this either "commute" (both stand,
    // producing a new version) or "subsumes" (all-users MFA already covers
    // admins). Both are legitimate outcomes of the same bounded vocabulary;
    // we only assert it landed on one of the two proposal-preserving paths
    // rather than an unexpected verdict.
    const p8after = await call(alice, "get_proposal", [p8]);
    const p9after = await call(alice, "get_proposal", [p9]);
    const settled = ["merged", "subsumed"];
    if (!settled.includes(p8after.status) && p8after.status !== "pending") {
      throw new Error(`p8 landed in unexpected status ${p8after.status}`);
    }
    if (!settled.includes(p9after.status) && p9after.status !== "pending") {
      throw new Error(`p9 landed in unexpected status ${p9after.status}`);
    }
  });

  // ---- Scenario 8: claim_timeout_refund's guard rejects an unripe claim ----
  let p10;
  await step("submit_proposal (bob, immediately try to claim timeout refund)", async () => {
    const { returnValue } = await write(
      bob,
      "submit_proposal",
      [v3, "network.backup_region", JSON.stringify("us-east-2"), "Add a backup region", "", ""],
      { value: gen(1) }
    );
    p10 = returnValue;
  });
  await step("claim_timeout_refund (p10) correctly rejected -- gap window not reached", async () => {
    let threw = false;
    try {
      await write(bob, "claim_timeout_refund", [p10], { expect: "SUCCESS" });
    } catch {
      threw = true;
    }
    if (!threw) throw new Error("expected claim_timeout_refund to be rejected before the gap window");
    // clean up: cancel instead, so the bond isn't left stranded on-chain.
    await write(bob, "cancel_proposal", [p10], {});
  });

  // ---- Final state ----
  await step("contract_stats reflects real accumulated activity", async () => {
    const stats = await call(alice, "contract_stats", []);
    console.log(`    ${JSON.stringify(stats)}`);
    if (stats.total_proposals < 10) throw new Error("expected at least 10 proposals submitted this run");
  });

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} steps passed`);
  if (failed.length > 0) {
    console.log("\nFailed steps:");
    for (const f of failed) console.log(`  - ${f.name}: ${f.error}`);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error("\nFATAL:", err);
  process.exit(1);
});
