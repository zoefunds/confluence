// Shared helpers for the Confluence live test suite, run against the real
// deployed contract on StudioNet.
//
// Why this exists instead of `genlayer write`: the published `genlayer`
// CLI hardcodes `value: 0n` in its write command, so it cannot exercise
// `@gl.public.write.payable` methods (submit_proposal locks a real GEN
// bond). This mirrors the pattern already used by this machine's sibling
// projects (ic9/living-standards-bond/.livetest): drive genlayer-js
// directly instead of the CLI, signing with dedicated named keystores
// instead of whatever account happens to be the CLI's global active one.

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { ethers } from "ethers";

const require = createRequire(import.meta.url);
const { createClient, createAccount } = require("genlayer-js");
const chains = require("genlayer-js/chains");

export const KEYSTORE_DIR = path.join(process.env.HOME, ".genlayer/keystores");
export const KEYSTORE_PASSWORD = process.env.CONFLUENCE_KEYSTORE_PASSWORD || "confluence-test-pw-1";
export const CONTRACT_ADDRESS = process.env.CONFLUENCE_CONTRACT || "0x3a26aa4289B723afF33D241e215D73dD711f0b9A";

export function gen(amount) {
  return ethers.parseUnits(String(amount), 18);
}

const accountCache = new Map();

export async function loadAccount(name) {
  if (accountCache.has(name)) return accountCache.get(name);
  const keystoreJson = fs.readFileSync(path.join(KEYSTORE_DIR, name + ".json"), "utf8");
  const wallet = await ethers.Wallet.fromEncryptedJson(keystoreJson, KEYSTORE_PASSWORD);
  const account = createAccount(wallet.privateKey);
  accountCache.set(name, account);
  return account;
}

export async function clientFor(name) {
  const account = await loadAccount(name);
  return createClient({ chain: chains.studionet, account });
}

export async function addressForKeystore(name) {
  const account = await loadAccount(name);
  return account.address;
}

const jsonReplacer = (_k, v) => (typeof v === "bigint" ? v.toString() : v);

export async function write(client, method, args, { value = 0n, expect = "SUCCESS", label } = {}) {
  const hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: method,
    args,
    value: BigInt(value),
  });
  const receipt = await client.waitForTransactionReceipt({ hash, retries: 100, interval: 5000 });
  const leader = receipt?.consensus_data?.leader_receipt?.[0];
  const got = leader?.execution_result;
  const tag = label || method;
  if (got !== expect) {
    throw new Error(
      `${tag}: expected execution_result=${expect}, got ${got}. ` +
        `status=${receipt.status_name || receipt.status} tx=${hash}\n` +
        JSON.stringify(receipt, jsonReplacer, 2).slice(0, 3000)
    );
  }
  let returnValue = null;
  const readable = leader?.result?.payload?.readable;
  if (typeof readable === "string") {
    try {
      returnValue = JSON.parse(readable);
    } catch {
      returnValue = readable;
    }
  }
  return { hash, receipt, returnValue };
}

export async function call(client, method, args = []) {
  return client.readContract({ address: CONTRACT_ADDRESS, functionName: method, args });
}

export function parseJson(raw) {
  return JSON.parse(raw);
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
