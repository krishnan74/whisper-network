"use client";

import { startTransition, useEffect, useMemo, useState } from "react";
import {
  createPublicClient,
  createWalletClient,
  custom,
  getAddress,
  http,
  namehash,
  parseAbi,
} from "viem";
import { sepolia } from "viem/chains";
import { addEnsContracts } from "@ensdomains/ensjs";
import { getNamesForAddress } from "@ensdomains/ensjs/subgraph";
import { createSubname } from "@ensdomains/ensjs/wallet";
import { useAccount, useWalletClient } from "wagmi";

const ENS_REGISTRY = getAddress("0x00000000000c2e074ec69a0dfb2997ba6c7d2e1e");

/** Only `axl.eth` — Sepolia on-chain subnames. */
export const ROOT_NAME = "axl.eth";

const registryAbi = parseAbi([
  "function owner(bytes32 node) view returns (address)",
  "function resolver(bytes32 node) view returns (address)",
]);

type TxLogEntry = {
  kind: "sub" | "nested";
  name: string;
  hash: `0x${string}`;
};

type PersistedState = {
  name: string;
  lastOwner?: `0x${string}` | null;
  lastResolver?: `0x${string}` | null;
  subnames?: string[];
  nestedSubnames?: Record<string, string[]>;
  /** After creating a direct subname, pre-fill step 2 parent field on next visit */
  prefillParent?: string;
  txLog?: TxLogEntry[];
};

const STORAGE_KEY_STATE = "ens-test:sepolia-onchain-state:axl";

function sepoliaTxUrl(hash: `0x${string}`) {
  return `https://sepolia.etherscan.io/tx/${hash}`;
}

/** One label per line and/or comma-separated. Strips a trailing `.{parent}` if pasted full names. */
function parseChildLabelsForParent(raw: string, parentFqdn: string): string[] {
  const suffix = `.${parentFqdn}`;
  const segments = raw.split(/[\n,]+/);
  const seen = new Set<string>();
  const out: string[] = [];
  for (const seg of segments) {
    let s = seg.trim().toLowerCase().replace(/\s+/g, "");
    if (!s) continue;
    if (s.endsWith(suffix)) {
      s = s.slice(0, -suffix.length);
    }
    if (!s || s.includes(".")) continue;
    if (seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

function loadState(): PersistedState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(STORAGE_KEY_STATE);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedState;
    if (parsed?.name !== ROOT_NAME) return null;
    return parsed;
  } catch {
    return null;
  }
}

function saveState(state: PersistedState) {
  if (typeof window === "undefined") return;
  localStorage.setItem(STORAGE_KEY_STATE, JSON.stringify(state));
}

export function OnchainEnsFlow() {
  const { isConnected, address, chainId } = useAccount();
  const { data: walletClient } = useWalletClient({ chainId: sepolia.id });

  const rpcUrl =
    process.env.NEXT_PUBLIC_SEPOLIA_RPC_URL ??
    "https://ethereum-sepolia-rpc.publicnode.com";

  const publicClient = useMemo(
    () =>
      createPublicClient({
        chain: sepolia,
        transport: http(rpcUrl),
      }),
    [rpcUrl]
  );

  /** ENS-aware client (subgraph + contracts) for indexed name queries */
  const ensPublicClient = useMemo(
    () =>
      createPublicClient({
        chain: addEnsContracts(sepolia),
        transport: http(rpcUrl),
      }),
    [rpcUrl]
  );

  const needsSepolia = isConnected && chainId !== sepolia.id;

  const persisted = useMemo(() => loadState(), []);

  const [rootOwner, setRootOwner] = useState<`0x${string}` | null>(
    persisted?.lastOwner ?? null
  );
  const [rootResolver, setRootResolver] = useState<`0x${string}` | null>(
    persisted?.lastResolver ?? null
  );
  const [subnames, setSubnames] = useState<string[]>(() => persisted?.subnames ?? []);
  const [nestedMap, setNestedMap] = useState<Record<string, string[]>>(
    () => persisted?.nestedSubnames ?? {}
  );
  const [txLog, setTxLog] = useState<TxLogEntry[]>(() => persisted?.txLog ?? []);
  /** Persisted pre-fill for step 2 (set when user creates a direct subname) */
  const [prefillParent, setPrefillParent] = useState<string>(() => persisted?.prefillParent ?? "");

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"none" | "creating-sub" | "creating-nested">("none");
  const [lastSuccessTx, setLastSuccessTx] = useState<
    | { kind: "sub"; name: string; hash: `0x${string}` }
    | { kind: "nested"; items: { name: string; hash: `0x${string}` }[] }
    | null
  >(null);
  const [nestedBatchProgress, setNestedBatchProgress] = useState<{
    current: number;
    total: number;
  } | null>(null);

  const [indexedNames, setIndexedNames] = useState<string[] | null>(null);
  const [indexedError, setIndexedError] = useState<string | null>(null);

  const [subLabelInput, setSubLabelInput] = useState("");
  const [subOwnerInput, setSubOwnerInput] = useState("");
  const [parentInput, setParentInput] = useState(() => persisted?.prefillParent ?? "");
  const [nestedLabelsInput, setNestedLabelsInput] = useState("");
  const [nestedOwnerInput, setNestedOwnerInput] = useState("");
  const [parentOnchainOwner, setParentOnchainOwner] = useState<`0x${string}` | null>(null);

  const subLabel = useMemo(
    () => subLabelInput.trim().toLowerCase().replace(/\s+/g, ""),
    [subLabelInput]
  );
  const subOwner = useMemo(() => {
    const v = subOwnerInput.trim();
    if (!v) return address ?? null;
    return v as `0x${string}`;
  }, [subOwnerInput, address]);
  const parentName = useMemo(() => {
    const v = parentInput.trim().toLowerCase().replace(/\s+/g, "");
    return v || null;
  }, [parentInput]);
  const nestedChildLabels = useMemo(() => {
    if (!parentName) return [];
    return parseChildLabelsForParent(nestedLabelsInput, parentName);
  }, [nestedLabelsInput, parentName]);
  const nestedOwner = useMemo(() => {
    const v = nestedOwnerInput.trim();
    if (!v) return address ?? null;
    return v as `0x${string}`;
  }, [nestedOwnerInput, address]);
  const parentIsUnderAxl = useMemo(() => {
    if (!parentName) return false;
    return parentName === ROOT_NAME || parentName.endsWith(`.${ROOT_NAME}`);
  }, [parentName]);

  const isRootOwnedByConnectedWallet = useMemo(() => {
    if (!address || !rootOwner) return false;
    return address.toLowerCase() === rootOwner.toLowerCase();
  }, [address, rootOwner]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const node = namehash(ROOT_NAME);
        const [owner, resolver] = await Promise.all([
          publicClient.readContract({
            address: ENS_REGISTRY,
            abi: registryAbi,
            functionName: "owner",
            args: [node],
          }),
          publicClient.readContract({
            address: ENS_REGISTRY,
            abi: registryAbi,
            functionName: "resolver",
            args: [node],
          }),
        ]);
        if (cancelled) return;
        const ownerAddr = owner as `0x${string}`;
        const resolverAddr = resolver as `0x${string}`;
        setRootOwner(ownerAddr);
        setRootResolver(resolverAddr);
      } catch {
        // ignore
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicClient]);

  useEffect(() => {
    saveState({
      name: ROOT_NAME,
      lastOwner: rootOwner,
      lastResolver: rootResolver,
      subnames,
      nestedSubnames: nestedMap,
      prefillParent: prefillParent || undefined,
      txLog: txLog.length ? txLog : undefined,
    });
  }, [rootOwner, rootResolver, subnames, nestedMap, prefillParent, txLog]);

  /** Subgraph: names for wallet under *.axl.eth (Sepolia indexer) */
  useEffect(() => {
    if (!address) {
      startTransition(() => {
        setIndexedNames(null);
        setIndexedError(null);
      });
      return;
    }
    let cancelled = false;
    (async () => {
      startTransition(() => {
        setIndexedError(null);
        setIndexedNames(null);
      });
      try {
        const rows = await getNamesForAddress(ensPublicClient, {
          address,
          pageSize: 500,
        });
        if (cancelled) return;
        const suffix = `.${ROOT_NAME}`;
        const seen = new Set<string>();
        const under: string[] = [];
        for (const row of rows) {
          const n = row.name;
          if (!n || n === ROOT_NAME) continue;
          if (!n.endsWith(suffix)) continue;
          if (seen.has(n)) continue;
          seen.add(n);
          under.push(n);
        }
        under.sort();
        setIndexedNames(under);
      } catch (e) {
        if (cancelled) return;
        setIndexedError(e instanceof Error ? e.message : "Could not load indexed names.");
        setIndexedNames([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ensPublicClient, address]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!parentName || !parentIsUnderAxl) {
        setParentOnchainOwner(null);
        return;
      }
      try {
        const owner = await publicClient.readContract({
          address: ENS_REGISTRY,
          abi: registryAbi,
          functionName: "owner",
          args: [namehash(parentName)],
        });
        if (cancelled) return;
        setParentOnchainOwner(owner as `0x${string}`);
      } catch {
        if (cancelled) return;
        setParentOnchainOwner(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicClient, parentName, parentIsUnderAxl]);

  const browserNamesList = useMemo(() => {
    const s = new Set<string>();
    for (const n of subnames) s.add(n);
    for (const list of Object.values(nestedMap)) {
      for (const n of list) s.add(n);
    }
    return [...s].sort();
  }, [subnames, nestedMap]);

  const mergedUnderAxl = useMemo(() => {
    const s = new Set<string>();
    for (const n of browserNamesList) s.add(n);
    if (indexedNames) {
      for (const n of indexedNames) s.add(n);
    }
    return [...s].sort();
  }, [browserNamesList, indexedNames]);

  async function createUnderRoot() {
    setError(null);
    if (!walletClient) return setError("Connect your wallet first.");
    if (!isRootOwnedByConnectedWallet) {
      return setError(
        `Your wallet must be the on-chain owner of ${ROOT_NAME} to create subnames. Connect the owner wallet.`
      );
    }
    if (!subLabel) return setError("Enter a label (e.g. hehe).");
    if (!subOwner) return setError("Set an owner address.");
    if (needsSepolia) return setError("Switch your wallet network to Sepolia.");

    setBusy("creating-sub");
    setLastSuccessTx(null);
    try {
      const ensWallet = createWalletClient({
        account: walletClient.account,
        chain: addEnsContracts(sepolia),
        transport: custom(walletClient.transport),
      });
      const full = `${subLabel}.${ROOT_NAME}`;
      const hash = await createSubname(ensWallet, {
        name: full,
        owner: subOwner,
        contract: "registry",
      });
      const receipt = await publicClient.waitForTransactionReceipt({ hash });
      if (receipt.status === "reverted") {
        throw new Error("Transaction reverted on-chain.");
      }
      const entry: TxLogEntry = { kind: "sub", name: full, hash };
      setTxLog((prev) => [entry, ...prev]);
      setLastSuccessTx({ kind: "sub", name: full, hash });
      setSubnames((prev) => Array.from(new Set([full, ...prev])));
      setSubLabelInput("");
      setParentInput(full);
      setPrefillParent(full);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create subname.");
    } finally {
      setBusy("none");
    }
  }

  async function createNested() {
    setError(null);
    if (!walletClient) return setError("Connect your wallet first.");
    if (!parentName) return setError(`Enter a parent under ${ROOT_NAME} (e.g. hehe.${ROOT_NAME}).`);
    if (!parentIsUnderAxl) {
      return setError(`Parent must be ${ROOT_NAME} or a subname ending in .${ROOT_NAME}.`);
    }
    const labels = parseChildLabelsForParent(nestedLabelsInput, parentName);
    if (labels.length === 0) {
      return setError(
        "Enter at least one child label (one per line or comma-separated). You can paste lines like agent12 or full names like agent12.goat.axl.eth."
      );
    }
    if (!nestedOwner) return setError("Set an owner address.");
    if (needsSepolia) return setError("Switch your wallet network to Sepolia.");
    if (
      address &&
      parentOnchainOwner &&
      parentOnchainOwner.toLowerCase() !== address.toLowerCase()
    ) {
      return setError(
        "Your wallet must be the on-chain owner of the parent name to create nested subnames."
      );
    }

    setBusy("creating-nested");
    setLastSuccessTx(null);
    setNestedBatchProgress({ current: 0, total: labels.length });
    const ensWallet = createWalletClient({
      account: walletClient.account,
      chain: addEnsContracts(sepolia),
      transport: custom(walletClient.transport),
    });
    const successes: { name: string; hash: `0x${string}` }[] = [];
    const failures: { label: string; message: string }[] = [];

    try {
      for (let i = 0; i < labels.length; i++) {
        const label = labels[i];
        setNestedBatchProgress({ current: i + 1, total: labels.length });
        try {
          const full = `${label}.${parentName}`;
          const hash = await createSubname(ensWallet, {
            name: full,
            owner: nestedOwner,
            contract: "registry",
          });
          const receipt = await publicClient.waitForTransactionReceipt({ hash });
          if (receipt.status === "reverted") {
            throw new Error("Transaction reverted on-chain.");
          }
          const entry: TxLogEntry = { kind: "nested", name: full, hash };
          setTxLog((prev) => [entry, ...prev]);
          successes.push({ name: full, hash });
          setNestedMap((prev) => {
            const list = prev[parentName] ?? [];
            const nextList = Array.from(new Set([full, ...list]));
            return { ...prev, [parentName]: nextList };
          });
        } catch (e) {
          failures.push({
            label,
            message: e instanceof Error ? e.message : "Failed",
          });
        }
      }

      if (successes.length > 0) {
        setLastSuccessTx({ kind: "nested", items: successes });
      }
      if (failures.length > 0) {
        const detail = failures.map((f) => `${f.label}: ${f.message}`).join(" · ");
        setError(
          failures.length === labels.length
            ? `None created. ${detail}`
            : `Created ${successes.length} of ${labels.length}. Failed: ${detail}`
        );
      }
      if (successes.length === labels.length) {
        setNestedLabelsInput("");
      }
    } finally {
      setNestedBatchProgress(null);
      setBusy("none");
    }
  }

  return (
    <div className="w-full rounded-3xl border border-zinc-200 bg-white p-8 shadow-[0_20px_60px_-30px_rgba(0,0,0,0.25)]">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">
            axl.eth · Sepolia
          </p>
          <h2 className="mt-1 text-xl font-semibold tracking-tight text-zinc-900">
            Subnames for {ROOT_NAME}
          </h2>
          <p className="mt-2 text-sm leading-6 text-zinc-600">
            Two steps: create a direct subname of <span className="font-medium">{ROOT_NAME}</span>,
            then add one or many nested labels under that parent in one go (each label is its own
            transaction).
          </p>
        </div>
        <div className="shrink-0 text-right text-xs text-zinc-500">
          <div>
            Network:{" "}
            <span className={needsSepolia ? "font-medium text-red-600" : "font-medium text-zinc-900"}>
              {needsSepolia ? "Switch to Sepolia" : "Sepolia"}
            </span>
          </div>
          <div className="mt-1 font-mono">
            {address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "Connect wallet"}
          </div>
        </div>
      </div>

      <div className="mt-6 rounded-2xl border border-zinc-200 bg-zinc-50 p-5">
        <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          {ROOT_NAME} on-chain
        </div>
        <div className="mt-3 grid gap-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-zinc-500">Owner:</span>
            <span className="font-mono text-zinc-900">{rootOwner ?? "—"}</span>
            {isRootOwnedByConnectedWallet ? (
              <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800">
                Your wallet
              </span>
            ) : (
              <span className="text-xs text-amber-700">
                Connect the owner wallet to create subnames.
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-zinc-500">Resolver:</span>
            <span className="font-mono text-zinc-900">{rootResolver ?? "—"}</span>
          </div>
        </div>
      </div>

      {lastSuccessTx ? (
        <div className="mt-6 rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-900">
          {lastSuccessTx.kind === "sub" ? (
            <>
              <p className="font-medium">
                Subname created: <span className="font-mono">{lastSuccessTx.name}</span>
              </p>
              <p className="mt-2 break-all font-mono text-xs">
                <a
                  href={sepoliaTxUrl(lastSuccessTx.hash)}
                  target="_blank"
                  rel="noreferrer"
                  className="text-emerald-800 underline decoration-emerald-300 underline-offset-2 hover:text-emerald-950"
                >
                  {lastSuccessTx.hash}
                </a>
              </p>
            </>
          ) : (
            <>
              <p className="font-medium">
                Nested subname{lastSuccessTx.items.length > 1 ? "s" : ""} created (
                {lastSuccessTx.items.length})
              </p>
              <ul className="mt-3 space-y-2">
                {lastSuccessTx.items.map((it) => (
                  <li key={it.hash} className="border-t border-emerald-100 pt-2 first:border-0 first:pt-0">
                    <span className="font-mono text-zinc-900">{it.name}</span>
                    <p className="mt-1 break-all font-mono text-xs">
                      <a
                        href={sepoliaTxUrl(it.hash)}
                        target="_blank"
                        rel="noreferrer"
                        className="text-emerald-800 underline decoration-emerald-300 underline-offset-2 hover:text-emerald-950"
                      >
                        {it.hash}
                      </a>
                    </p>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      ) : null}

      <div className="mt-6 rounded-2xl border border-zinc-200 bg-white p-5">
        <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          Names under {ROOT_NAME} · your wallet
        </div>
        <p className="mt-2 text-sm text-zinc-600">
          After a refresh, indexed names show what the subgraph ties to your address. This browser also
          lists names you created here.
        </p>

        <div className="mt-4 space-y-4">
          <div>
            <p className="text-xs font-medium text-zinc-700">Indexed (Sepolia subgraph)</p>
            {address ? (
              indexedNames === null && !indexedError ? (
                <p className="mt-1 text-sm text-zinc-500">Loading…</p>
              ) : indexedError ? (
                <p className="mt-1 text-sm text-amber-800">{indexedError}</p>
              ) : indexedNames && indexedNames.length > 0 ? (
                <ul className="mt-2 max-h-40 space-y-1 overflow-y-auto">
                  {indexedNames.map((n) => (
                    <li key={n} className="font-mono text-sm text-zinc-800">
                      {n}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1 text-sm text-zinc-500">None found yet in the index.</p>
              )
            ) : (
              <p className="mt-1 text-sm text-zinc-500">Connect a wallet to load indexed names.</p>
            )}
          </div>

          <div className="border-t border-zinc-100 pt-4">
            <p className="text-xs font-medium text-zinc-700">Recorded in this browser</p>
            {browserNamesList.length > 0 ? (
              <ul className="mt-2 max-h-40 space-y-1 overflow-y-auto">
                {browserNamesList.map((n) => (
                  <li key={n} className="font-mono text-sm text-zinc-800">
                    {n}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-sm text-zinc-500">No names stored locally yet.</p>
            )}
          </div>

          {mergedUnderAxl.length > 0 ? (
            <div className="border-t border-zinc-100 pt-4">
              <p className="text-xs font-medium text-zinc-700">Combined (unique)</p>
              <ul className="mt-2 max-h-40 space-y-1 overflow-y-auto">
                {mergedUnderAxl.map((n) => (
                  <li key={n} className="font-mono text-sm text-zinc-800">
                    {n}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>

        {txLog.length > 0 ? (
          <div className="mt-5 border-t border-zinc-100 pt-5">
            <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              Successful transactions (this browser)
            </p>
            <ul className="mt-2 space-y-2">
              {txLog.slice(0, 15).map((t, i) => (
                <li key={`${t.hash}-${i}`} className="text-sm">
                  <span className="text-zinc-500">{t.kind === "sub" ? "Sub" : "Nested"}</span>{" "}
                  <span className="font-mono text-zinc-800">{t.name}</span>
                  <br />
                  <a
                    href={sepoliaTxUrl(t.hash)}
                    target="_blank"
                    rel="noreferrer"
                    className="break-all font-mono text-xs text-emerald-800 underline decoration-emerald-200 underline-offset-2 hover:text-emerald-950"
                  >
                    {t.hash}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>

      <div className="mt-8 grid gap-8">
        {/* Step 1 */}
        <section className="rounded-2xl border border-zinc-200 bg-white p-6">
          <div className="flex items-center gap-3">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-zinc-900 text-sm font-semibold text-white">
              1
            </span>
            <div>
              <h3 className="text-base font-semibold text-zinc-900">Create a subname of axl.eth</h3>
              <p className="mt-0.5 text-sm text-zinc-500">
                Issues <span className="font-mono text-zinc-700">yourlabel.{ROOT_NAME}</span> on-chain.
              </p>
            </div>
          </div>

          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <div>
              <label className="text-sm font-medium text-zinc-900">Label</label>
              <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                <input
                  value={subLabelInput}
                  onChange={(e) => setSubLabelInput(e.target.value)}
                  placeholder="hehe"
                  className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                  autoComplete="off"
                  spellCheck={false}
                />
                <span className="ml-2 select-none text-sm text-zinc-500">.{ROOT_NAME}</span>
              </div>
            </div>
            <div>
              <label className="text-sm font-medium text-zinc-900">Owner</label>
              <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                <input
                  value={subOwnerInput}
                  onChange={(e) => setSubOwnerInput(e.target.value)}
                  placeholder={address ? `${address.slice(0, 6)}…${address.slice(-4)} (default)` : "0x…"}
                  className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>
            </div>
          </div>

          <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-xs text-zinc-500">
              Uses ENS registry · you confirm one transaction in your wallet.
            </p>
            <button
              type="button"
              onClick={createUnderRoot}
              disabled={!isConnected || needsSepolia || !walletClient || busy !== "none"}
              className="inline-flex h-11 items-center justify-center rounded-2xl bg-zinc-900 px-6 text-sm font-semibold text-white shadow-sm transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy === "creating-sub" ? "Creating…" : `Create .${ROOT_NAME}`}
            </button>
          </div>

          {subnames.length > 0 ? (
            <div className="mt-6 border-t border-zinc-100 pt-5">
              <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">Your subnames</p>
              <ul className="mt-2 space-y-1.5">
                {subnames.map((n) => (
                  <li key={n} className="font-mono text-sm text-zinc-800">
                    {n}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </section>

        {/* Step 2 */}
        <section className="rounded-2xl border border-zinc-200 bg-white p-6">
          <div className="flex items-center gap-3">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-zinc-900 text-sm font-semibold text-white">
              2
            </span>
            <div>
              <h3 className="text-base font-semibold text-zinc-900">Create nested subnames</h3>
              <p className="mt-0.5 text-sm text-zinc-500">
                Parent must be <span className="font-mono">{ROOT_NAME}</span> or end with{" "}
                <span className="font-mono">.{ROOT_NAME}</span>.
              </p>
            </div>
          </div>

          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <label className="text-sm font-medium text-zinc-900">Parent name</label>
              <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                <input
                  value={parentInput}
                  onChange={(e) => setParentInput(e.target.value)}
                  placeholder={`hehe.${ROOT_NAME}`}
                  className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>
              <p className="mt-2 text-xs text-zinc-500">
                Registry owner of parent:{" "}
                <span className="font-mono">
                  {parentName && parentIsUnderAxl ? parentOnchainOwner ?? "—" : "—"}
                </span>
                {parentName && !parentIsUnderAxl ? (
                  <span className="ml-2 text-red-600">Must be under {ROOT_NAME}.</span>
                ) : null}
              </p>
            </div>

            <div className="sm:col-span-2">
              <label className="text-sm font-medium text-zinc-900">Child labels</label>
              <p className="mt-0.5 text-xs text-zinc-500">
                One per line or comma-separated. You can use short labels (
                <span className="font-mono">agent2</span>) or full names under the parent above (
                <span className="font-mono">agent2.goat.{ROOT_NAME}</span>).
              </p>
              <textarea
                value={nestedLabelsInput}
                onChange={(e) => setNestedLabelsInput(e.target.value)}
                placeholder={`agent12\nagent2\nagent3`}
                rows={4}
                className="mt-2 w-full resize-y rounded-2xl border border-zinc-200 bg-white px-4 py-3 font-mono text-sm text-zinc-900 shadow-sm outline-none placeholder:text-zinc-400 focus:ring-4 focus:ring-zinc-100"
                autoComplete="off"
                spellCheck={false}
              />
            </div>

            <div className="sm:col-span-2">
              <label className="text-sm font-medium text-zinc-900">Owner</label>
              <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                <input
                  value={nestedOwnerInput}
                  onChange={(e) => setNestedOwnerInput(e.target.value)}
                  placeholder={address ? `${address.slice(0, 6)}…${address.slice(-4)} (default)` : "0x…"}
                  className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>
            </div>
          </div>

          <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-zinc-500">Preview</p>
              <p className="mt-1 break-words font-mono text-xs leading-relaxed text-zinc-700">
                {parentName && nestedChildLabels.length > 0 && parentIsUnderAxl
                  ? nestedChildLabels.map((l) => `${l}.${parentName}`).join(", ")
                  : parentName && parentIsUnderAxl
                    ? "Add labels above — each becomes child.parent."
                    : "Set parent under axl.eth first."}
              </p>
              <p className="mt-2 text-xs text-zinc-500">
                Each name needs its own on-chain transaction; your wallet will prompt once per label.
              </p>
            </div>
            <button
              type="button"
              onClick={createNested}
              disabled={
                !isConnected ||
                needsSepolia ||
                !walletClient ||
                busy !== "none" ||
                !parentName ||
                !parentIsUnderAxl ||
                nestedChildLabels.length === 0
              }
              className="inline-flex h-11 shrink-0 items-center justify-center rounded-2xl bg-zinc-900 px-6 text-sm font-semibold text-white shadow-sm transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {nestedBatchProgress
                ? `Signing ${nestedBatchProgress.current}/${nestedBatchProgress.total}…`
                : busy === "creating-nested"
                  ? "Creating…"
                  : nestedChildLabels.length > 1
                    ? `Create ${nestedChildLabels.length} nested subnames`
                    : "Create nested subname"}
            </button>
          </div>

          {Object.keys(nestedMap).length > 0 ? (
            <div className="mt-6 border-t border-zinc-100 pt-5">
              <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                Nested subnames
              </p>
              <div className="mt-3 space-y-4">
                {Object.entries(nestedMap).map(([parent, items]) => (
                  <div key={parent}>
                    <p className="text-xs text-zinc-500">
                      Under <span className="font-mono text-zinc-800">{parent}</span>
                    </p>
                    <ul className="mt-1 space-y-1">
                      {items.map((n) => (
                        <li key={n} className="font-mono text-sm text-zinc-800">
                          {n}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </section>

        {error ? (
          <div className="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
            {error}
          </div>
        ) : null}
      </div>
    </div>
  );
}
