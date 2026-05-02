"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useAccount, useWalletClient } from "wagmi";
import {
  createPublicClient,
  createWalletClient,
  custom,
  formatEther,
  getAddress,
  http,
  namehash,
  parseAbi,
  type Hex,
  zeroHash,
} from "viem";
import { sepolia } from "viem/chains";
import { addEnsContracts } from "@ensdomains/ensjs";
import { createSubname } from "@ensdomains/ensjs/wallet";

// Sepolia ENS contracts (official deployments)
// Ref: https://docs.ens.domains/learn/deployments/
const ETH_REGISTRAR_CONTROLLER = getAddress(
  "0xfb3ce5d01e0f33f41dbb39035db9745962f1f968"
);
const PUBLIC_RESOLVER = getAddress(
  "0xe99638b40e4fff0129d56f03b55b6bbc4bbe49b5"
);
const ENS_REGISTRY = getAddress("0x00000000000c2e074ec69a0dfb2997ba6c7d2e1e");

const NAME = "kuber12.eth";
const LABEL = "kuber12";
const DEFAULT_DURATION_SECONDS = 365n * 24n * 60n * 60n; // 1 year

const registryAbi = parseAbi([
  "function owner(bytes32 node) view returns (address)",
  "function resolver(bytes32 node) view returns (address)",
]);

type Registration = {
  label: string;
  owner: `0x${string}`;
  duration: bigint;
  secret: `0x${string}`;
  resolver: `0x${string}`;
  data: `0x${string}`[];
  reverseRecord: number; // uint8
  referrer: `0x${string}`;
};

const controllerAbi = parseAbi([
  "function available(string name) view returns (bool)",
  "function minCommitmentAge() view returns (uint256)",
  "function maxCommitmentAge() view returns (uint256)",
  "function rentPrice(string name, uint256 duration) view returns (uint256 base, uint256 premium)",
  "function makeCommitment((string label,address owner,uint256 duration,bytes32 secret,address resolver,bytes[] data,uint8 reverseRecord,bytes32 referrer) registration) pure returns (bytes32)",
  "function commit(bytes32 commitment)",
  "function commitments(bytes32 commitment) view returns (uint256)",
  "function register((string label,address owner,uint256 duration,bytes32 secret,address resolver,bytes[] data,uint8 reverseRecord,bytes32 referrer) registration) payable",
]);

type PersistedCommit = {
  label: string;
  owner: `0x${string}`;
  duration: string; // bigint as string
  secret: `0x${string}`;
  committedAtMs: number;
};

function coerceRentPrice(value: unknown): { base: bigint; premium: bigint } {
  if (Array.isArray(value) && value.length >= 2) {
    const [base, premium] = value as unknown as [bigint, bigint];
    if (typeof base === "bigint" && typeof premium === "bigint") return { base, premium };
  }
  if (value && typeof value === "object") {
    const v = value as Record<string, unknown>;
    const base = v["base"];
    const premium = v["premium"];
    if (typeof base === "bigint" && typeof premium === "bigint") {
      return { base, premium };
    }
  }
  throw new Error("Unexpected rentPrice() return shape");
}

function randomSecret32(): `0x${string}` {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `0x${hex}`;
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

const STORAGE_KEY = "ens-test:sepolia-commit";
const STORAGE_KEY_STATE = "ens-test:sepolia-onchain-state";

type PersistedState = {
  name: string;
  lastOwner?: `0x${string}` | null;
  lastResolver?: `0x${string}` | null;
  subnames?: string[];
  nestedSubnames?: Record<string, string[]>;
};

function loadPersistedCommit(): PersistedCommit | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedCommit;
    if (parsed?.label === LABEL && parsed?.owner) return parsed;
    return null;
  } catch {
    return null;
  }
}

function loadPersistedState(): PersistedState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(STORAGE_KEY_STATE);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedState;
    if (parsed?.name !== NAME) return null;
    return parsed;
  } catch {
    return null;
  }
}

function savePersistedState(state: PersistedState) {
  if (typeof window === "undefined") return;
  localStorage.setItem(STORAGE_KEY_STATE, JSON.stringify(state));
}

export default function NewOnchainEnsPage() {
  const { isConnected, address, chainId } = useAccount();
  const { data: walletClient } = useWalletClient({ chainId: sepolia.id });

  const publicClient = useMemo(
    () =>
      createPublicClient({
        chain: sepolia,
        transport: http(
          process.env.NEXT_PUBLIC_SEPOLIA_RPC_URL ??
            "https://ethereum-sepolia-rpc.publicnode.com"
        ),
      }),
    []
  );

  const [status, setStatus] = useState<
    "idle" | "checking" | "unavailable" | "available" | "committed" | "waiting" | "registering" | "registered" | "error"
  >(() => (loadPersistedCommit() ? "committed" : "idle"));
  const [error, setError] = useState<string | null>(null);

  const [durationSeconds, setDurationSeconds] = useState<bigint>(DEFAULT_DURATION_SECONDS);
  const [commit, setCommit] = useState<PersistedCommit | null>(() => loadPersistedCommit());

  const [minAgeSeconds, setMinAgeSeconds] = useState<bigint>(60n);
  const [maxAgeSeconds, setMaxAgeSeconds] = useState<bigint>(24n * 60n * 60n);

  const [priceDisplay, setPriceDisplay] = useState<string>("");
  const [nowMs, setNowMs] = useState(() => Date.now());

  const [persisted, setPersisted] = useState<PersistedState | null>(() =>
    loadPersistedState()
  );
  const [onchainOwner, setOnchainOwner] = useState<`0x${string}` | null>(
    persisted?.lastOwner ?? null
  );
  const [onchainResolver, setOnchainResolver] = useState<`0x${string}` | null>(
    persisted?.lastResolver ?? null
  );
  const [subnames, setSubnames] = useState<string[]>(
    () => persisted?.subnames ?? []
  );
  const [nestedMap, setNestedMap] = useState<Record<string, string[]>>(
    () => persisted?.nestedSubnames ?? {}
  );

  // Subname creation under kuber12.eth after registration
  const [subLabelInput, setSubLabelInput] = useState("");
  const subLabel = useMemo(() => subLabelInput.trim().toLowerCase().replace(/\s+/g, ""), [subLabelInput]);
  const [subOwnerInput, setSubOwnerInput] = useState("");
  const subOwner = useMemo(() => {
    const v = subOwnerInput.trim();
    if (!v) return address ?? null;
    return v as `0x${string}`;
  }, [subOwnerInput, address]);
  const [subTxHash, setSubTxHash] = useState<`0x${string}` | null>(null);

  // Nested subname creation (e.g. agent1.hehe.kuber12.eth)
  const [parentInput, setParentInput] = useState("");
  const parentName = useMemo(() => {
    const v = parentInput.trim().toLowerCase().replace(/\s+/g, "");
    if (!v) return null;
    return v;
  }, [parentInput]);
  const [nestedLabelInput, setNestedLabelInput] = useState("");
  const nestedLabel = useMemo(
    () => nestedLabelInput.trim().toLowerCase().replace(/\s+/g, ""),
    [nestedLabelInput]
  );
  const [nestedOwnerInput, setNestedOwnerInput] = useState("");
  const nestedOwner = useMemo(() => {
    const v = nestedOwnerInput.trim();
    if (!v) return address ?? null;
    return v as `0x${string}`;
  }, [nestedOwnerInput, address]);
  const [nestedTxHash, setNestedTxHash] = useState<`0x${string}` | null>(null);
  const [parentOnchainOwner, setParentOnchainOwner] = useState<`0x${string}` | null>(null);

  useEffect(() => {
    if (!commit) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [commit]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const node = namehash(NAME);
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
        setOnchainOwner(ownerAddr);
        setOnchainResolver(resolverAddr);
        const next: PersistedState = {
          name: NAME,
          lastOwner: ownerAddr,
          lastResolver: resolverAddr,
          subnames,
          nestedSubnames: nestedMap,
        };
        setPersisted(next);
        savePersistedState(next);
      } catch {
        // ignore
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicClient, subnames, nestedMap]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!parentName) {
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
  }, [publicClient, parentName]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [min, max] = await Promise.all([
          publicClient.readContract({
            address: ETH_REGISTRAR_CONTROLLER,
            abi: controllerAbi,
            functionName: "minCommitmentAge",
          }),
          publicClient.readContract({
            address: ETH_REGISTRAR_CONTROLLER,
            abi: controllerAbi,
            functionName: "maxCommitmentAge",
          }),
        ]);
        if (cancelled) return;
        setMinAgeSeconds(min);
        setMaxAgeSeconds(max);
      } catch {
        // fall back to defaults
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicClient]);

  const needsSepolia = isConnected && chainId !== sepolia.id;

  const commitmentAgeSeconds = useMemo(() => {
    if (!commit) return null;
    return BigInt(Math.max(0, Math.floor((nowMs - commit.committedAtMs) / 1000)));
  }, [commit, nowMs]);

  const secondsUntilReveal = useMemo(() => {
    if (!commitmentAgeSeconds) return null;
    if (commitmentAgeSeconds >= minAgeSeconds) return 0n;
    return minAgeSeconds - commitmentAgeSeconds;
  }, [commitmentAgeSeconds, minAgeSeconds]);

  async function checkAvailability() {
    setError(null);
    if (!isConnected || !address) {
      setError("Connect a wallet first.");
      setStatus("error");
      return;
    }
    if (needsSepolia) {
      setError("Switch your wallet network to Sepolia.");
      setStatus("error");
      return;
    }
    setStatus("checking");
    try {
      const available = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "available",
        args: [LABEL],
      });
      if (!available) {
        setStatus("unavailable");
        setPriceDisplay("");
        return;
      }

      const price = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "rentPrice",
        args: [LABEL, durationSeconds],
      });
      const { base, premium } = coerceRentPrice(price);
      const total = base + premium;
      setPriceDisplay(`${formatEther(total)} SepoliaETH`);
      setStatus("available");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to check availability");
      setStatus("error");
    }
  }

  async function commitName() {
    setError(null);
    setSubTxHash(null);
    if (!walletClient || !address) {
      setError("Wallet client not available. Connect wallet first.");
      setStatus("error");
      return;
    }
    if (needsSepolia) {
      setError("Switch your wallet network to Sepolia.");
      setStatus("error");
      return;
    }

    try {
      const secret = randomSecret32();
      const duration = durationSeconds;
      const registration: Registration = {
        label: LABEL,
        owner: address,
        duration,
        secret,
        resolver: PUBLIC_RESOLVER,
        data: [],
        reverseRecord: 0,
        referrer: zeroHash,
      };
      const commitment = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "makeCommitment",
        args: [registration],
      });

      const hash = await walletClient.writeContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "commit",
        args: [commitment as `0x${string}`],
      });
      await publicClient.waitForTransactionReceipt({ hash });

      const persisted: PersistedCommit = {
        label: LABEL,
        owner: address,
        duration: duration.toString(),
        secret,
        committedAtMs: Date.now(),
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
      setCommit(persisted);
      setStatus("committed");

      // Best-effort refresh price after commit
      try {
        const price = await publicClient.readContract({
          address: ETH_REGISTRAR_CONTROLLER,
          abi: controllerAbi,
          functionName: "rentPrice",
          args: [LABEL, duration],
        });
        const { base, premium } = coerceRentPrice(price);
        const total = base + premium;
        setPriceDisplay(`${formatEther(total)} SepoliaETH`);
      } catch {
        // ignore
      }

      // Quietly show the commit tx hash in UI via error slot? We'll keep it simple.
      void hash;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Commit failed");
      setStatus("error");
    }
  }

  async function registerName() {
    setError(null);
    if (!walletClient || !address) {
      setError("Wallet client not available. Connect wallet first.");
      setStatus("error");
      return;
    }
    if (!commit) {
      setError("No commitment found. Commit first.");
      setStatus("error");
      return;
    }
    if (needsSepolia) {
      setError("Switch your wallet network to Sepolia.");
      setStatus("error");
      return;
    }

    const duration = BigInt(commit.duration);
    const ageSeconds = BigInt(Math.max(0, Math.floor((Date.now() - commit.committedAtMs) / 1000)));
    if (ageSeconds < minAgeSeconds) {
      setStatus("waiting");
      const waitMs = Number((minAgeSeconds - ageSeconds) * 1000n);
      await sleep(Math.min(waitMs, 10_000)); // don’t block too long; user can click again
      setError(`Wait ~${Number(minAgeSeconds - ageSeconds)}s more before registering.`);
      setStatus("committed");
      return;
    }
    if (ageSeconds > maxAgeSeconds) {
      setError("Commitment expired. Commit again.");
      setStatus("error");
      localStorage.removeItem(STORAGE_KEY);
      setCommit(null);
      return;
    }

    setStatus("registering");
    try {
      const available = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "available",
        args: [LABEL],
      });
      if (!available) {
        setError("Name is no longer available.");
        setStatus("unavailable");
        return;
      }

      const price = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "rentPrice",
        args: [LABEL, duration],
      });
      const { base, premium } = coerceRentPrice(price);
      const total = base + premium;

      // Add a small buffer to reduce failures if price shifts between read and tx inclusion.
      const value = (total * 105n) / 100n;

      const registration = {
        label: LABEL,
        owner: commit.owner,
        duration,
        secret: commit.secret,
        resolver: PUBLIC_RESOLVER,
        data: [],
        reverseRecord: 0,
        referrer: zeroHash,
      } satisfies Registration;

      const commitment = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "makeCommitment",
        args: [registration],
      });

      const committedAt = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "commitments",
        args: [commitment as Hex],
      });

      if (committedAt === 0n) {
        throw new Error(
          "Commitment not found on-chain. Re-commit and wait for the commit tx to confirm."
        );
      }

      const block = await publicClient.getBlock();
      const ageFromChain = block.timestamp - committedAt;
      if (ageFromChain < minAgeSeconds) {
        throw new Error(
          `Commitment too new. Wait ~${Number(minAgeSeconds - ageFromChain)}s more before registering.`
        );
      }

      const { request } = await publicClient.simulateContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "register",
        args: [registration],
        value,
        account: walletClient.account,
      });

      const gas =
        typeof request.gas === "bigint"
          ? (request.gas * 130n) / 100n // 30% buffer to avoid OOG
          : undefined;

      await walletClient.writeContract({
        ...request,
        gas,
      });

      setStatus("registered");
      localStorage.removeItem(STORAGE_KEY);
      setCommit(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Register failed");
      setStatus("error");
    }
  }

  async function createSubnameUnderKuber12() {
    setError(null);
    setSubTxHash(null);
    if (!walletClient) {
      setError("Wallet client not available. Connect wallet first.");
      setStatus("error");
      return;
    }
    if (!subLabel) {
      setError("Enter a subname label (e.g. dev).");
      setStatus("error");
      return;
    }
    if (!subOwner) {
      setError("Set an owner address for the new subname.");
      setStatus("error");
      return;
    }
    if (needsSepolia) {
      setError("Switch your wallet network to Sepolia.");
      setStatus("error");
      return;
    }

    try {
      const ensWallet = createWalletClient({
        account: walletClient.account,
        chain: addEnsContracts(sepolia),
        transport: custom(walletClient.transport),
      });

      const txHash = await createSubname(ensWallet, {
        name: `${subLabel}.${NAME}`,
        owner: subOwner,
        contract: "registry",
      });
      setSubTxHash(txHash as `0x${string}`);

      const full = `${subLabel}.${NAME}`;
      setSubnames((prev) => {
        const next = Array.from(new Set([full, ...prev]));
        const nextPersisted: PersistedState = {
          name: NAME,
          lastOwner: onchainOwner,
          lastResolver: onchainResolver,
          subnames: next,
          nestedSubnames: nestedMap,
        };
        setPersisted(nextPersisted);
        savePersistedState(nextPersisted);
        return next;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create subname");
      setStatus("error");
    }
  }

  async function createSubnameUnderSubname() {
    setError(null);
    setNestedTxHash(null);
    if (!walletClient) {
      setError("Wallet client not available. Connect wallet first.");
      setStatus("error");
      return;
    }
    if (!parentName) {
      setError("Enter a parent name (e.g. hehe.kuber12.eth).");
      setStatus("error");
      return;
    }
    if (!nestedLabel) {
      setError("Enter a child label (e.g. agent1).");
      setStatus("error");
      return;
    }
    if (!nestedOwner) {
      setError("Set an owner address for the new nested subname.");
      setStatus("error");
      return;
    }
    if (needsSepolia) {
      setError("Switch your wallet network to Sepolia.");
      setStatus("error");
      return;
    }

    try {
      const ensWallet = createWalletClient({
        account: walletClient.account,
        chain: addEnsContracts(sepolia),
        transport: custom(walletClient.transport),
      });

      const full = `${nestedLabel}.${parentName}`;
      const txHash = await createSubname(ensWallet, {
        name: full,
        owner: nestedOwner,
        contract: "registry",
      });
      setNestedTxHash(txHash as `0x${string}`);

      setNestedMap((prev) => {
        const existing = prev[parentName] ?? [];
        const nextList = Array.from(new Set([full, ...existing]));
        const next = { ...prev, [parentName]: nextList };
        const nextPersisted: PersistedState = {
          name: NAME,
          lastOwner: onchainOwner,
          lastResolver: onchainResolver,
          subnames,
          nestedSubnames: next,
        };
        setPersisted(nextPersisted);
        savePersistedState(nextPersisted);
        return next;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create nested subname");
      setStatus("error");
    }
  }

  return (
    <div className="min-h-screen bg-white text-zinc-900">
      <header className="mx-auto flex w-full max-w-4xl items-center justify-between px-6 py-10">
        <div className="flex items-center gap-3">
          <Link href="/" className="text-sm font-medium text-zinc-700 hover:text-zinc-900">
            Home
          </Link>
          <span className="text-zinc-300">/</span>
          <div className="text-base font-semibold tracking-tight">On-chain ENS (Sepolia)</div>
        </div>
        <div className="text-xs text-zinc-500">
          {isConnected ? (
            <span>
              {address?.slice(0, 6)}…{address?.slice(-4)}{" "}
              {needsSepolia ? <span className="text-red-600">(switch to Sepolia)</span> : <span>(Sepolia)</span>}
            </span>
          ) : (
            "Connect wallet from Home"
          )}
        </div>
      </header>

      <main className="mx-auto w-full max-w-4xl px-6 pb-16">
        <div className="w-full rounded-3xl border border-zinc-200 bg-white p-8 shadow-[0_20px_60px_-30px_rgba(0,0,0,0.25)]">
          <h1 className="text-xl font-semibold tracking-tight">Register `{NAME}` on-chain</h1>
          <p className="mt-2 text-sm leading-6 text-zinc-600">
            This page does <span className="font-medium text-zinc-900">true on-chain</span> `.eth` registration on{" "}
            <span className="font-medium text-zinc-900">Sepolia</span> using the ENS commit-reveal flow (commit → wait → register).
          </p>

          <div className="mt-6 grid gap-4">
            <div className="rounded-2xl border border-zinc-200 bg-white p-5">
              <div className="text-sm font-medium text-zinc-900">
                On-chain status (survives refresh)
              </div>
              <div className="mt-2 text-xs text-zinc-500">
                We re-check the ENS Registry on load and also store the last seen values locally.
              </div>
              <div className="mt-3 grid gap-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-zinc-500">Owner:</span>
                  <span className="font-mono text-zinc-900">
                    {onchainOwner ?? persisted?.lastOwner ?? "—"}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-zinc-500">Resolver:</span>
                  <span className="font-mono text-zinc-900">
                    {onchainResolver ?? persisted?.lastResolver ?? "—"}
                  </span>
                </div>
                {subnames.length ? (
                  <div className="mt-2">
                    <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                      Created subnames
                    </div>
                    <ul className="mt-2 space-y-1">
                      {subnames.map((n) => (
                        <li key={n} className="font-mono text-xs text-zinc-700">
                          {n}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {Object.keys(nestedMap).length ? (
                  <div className="mt-4">
                    <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                      Nested subnames
                    </div>
                    <div className="mt-2 space-y-3">
                      {Object.entries(nestedMap).map(([parent, items]) => (
                        <div key={parent}>
                          <div className="text-xs text-zinc-500">
                            Parent: <span className="font-mono text-zinc-900">{parent}</span>
                          </div>
                          <ul className="mt-1 space-y-1">
                            {items.map((n) => (
                              <li key={n} className="font-mono text-xs text-zinc-700">
                                {n}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            </div>

            <div className="rounded-2xl border border-zinc-200 bg-zinc-50 p-5">
              <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">Step 0</div>
              <div className="mt-2 text-sm text-zinc-700">
                Ensure your wallet is on <span className="font-medium text-zinc-900">Sepolia</span> and has some SepoliaETH.
              </div>
            </div>

            <div className="rounded-2xl border border-zinc-200 bg-white p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-medium text-zinc-900">1) Check availability</div>
                  <div className="mt-1 text-xs text-zinc-500">
                    Duration:{" "}
                    <button
                      type="button"
                      className="underline"
                      onClick={() => setDurationSeconds(DEFAULT_DURATION_SECONDS)}
                    >
                      1 year
                    </button>{" "}
                    · Price (estimate): <span className="font-medium">{priceDisplay || "—"}</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={checkAvailability}
                  className="inline-flex h-9 items-center justify-center rounded-xl border border-zinc-200 bg-white px-3 text-sm font-medium text-zinc-800 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={!isConnected || needsSepolia || status === "checking"}
                >
                  {status === "checking" ? "Checking…" : "Check"}
                </button>
              </div>
              <div className="mt-3 text-sm text-zinc-700">
                {status === "unavailable"
                  ? `${NAME} is not available.`
                  : status === "available"
                    ? `${NAME} is available.`
                    : " "}
              </div>
            </div>

            <div className="rounded-2xl border border-zinc-200 bg-white p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-medium text-zinc-900">2) Commit</div>
                  <div className="mt-1 text-xs text-zinc-500">
                    A secret is generated in-browser and stored in localStorage so you can register after waiting.
                  </div>
                </div>
                <button
                  type="button"
                  onClick={commitName}
                  className="inline-flex h-9 items-center justify-center rounded-xl bg-zinc-900 px-3 text-sm font-semibold text-white hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={!isConnected || needsSepolia || !walletClient}
                >
                  Commit
                </button>
              </div>

              {commit ? (
                <div className="mt-3 text-xs text-zinc-600">
                  Commitment saved. Min wait:{" "}
                  <span className="font-medium text-zinc-900">{Number(minAgeSeconds)}s</span>
                  {" · "}
                  Expires:{" "}
                  <span className="font-medium text-zinc-900">{Number(maxAgeSeconds / 3600n)}h</span>
                  {" · "}
                  Ready in:{" "}
                  <span className="font-medium text-zinc-900">
                    {secondsUntilReveal !== null ? `${Number(secondsUntilReveal)}s` : "—"}
                  </span>
                </div>
              ) : null}
            </div>

            <div className="rounded-2xl border border-zinc-200 bg-white p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-medium text-zinc-900">3) Register</div>
                  <div className="mt-1 text-xs text-zinc-500">
                    Requires the commit to be at least <span className="font-medium">{Number(minAgeSeconds)}s</span> old.
                  </div>
                </div>
                <button
                  type="button"
                  onClick={registerName}
                  className="inline-flex h-9 items-center justify-center rounded-xl bg-emerald-700 px-3 text-sm font-semibold text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={!isConnected || needsSepolia || !walletClient || !commit}
                >
                  {status === "registering" ? "Registering…" : "Register"}
                </button>
              </div>
              <div className="mt-3 text-xs text-zinc-500">
                We send `value = price * 1.05` as a small buffer.
              </div>
            </div>

            <div className="rounded-2xl border border-zinc-200 bg-white p-5">
              <div className="text-sm font-medium text-zinc-900">4) Create subnames under `{NAME}`</div>
              <div className="mt-1 text-xs text-zinc-500">
                After registration, you can create <span className="font-medium">dev.{NAME}</span> and then create{" "}
                <span className="font-medium">test.dev.{NAME}</span> (etc.) using ENSjs `createSubname` (on-chain).
              </div>

              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                <div>
                  <div className="text-sm font-medium text-zinc-900">Label</div>
                  <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                    <input
                      value={subLabelInput}
                      onChange={(e) => setSubLabelInput(e.target.value)}
                      placeholder="dev"
                      className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <span className="ml-2 select-none text-sm text-zinc-500">.{NAME}</span>
                  </div>
                </div>

                <div>
                  <div className="text-sm font-medium text-zinc-900">Owner</div>
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

              <div className="mt-4 flex items-center justify-between gap-3">
                <div className="text-xs text-zinc-500">
                  Contract mode: <span className="font-medium">registry</span>
                </div>
                <button
                  type="button"
                  onClick={createSubnameUnderKuber12}
                  className="inline-flex h-9 items-center justify-center rounded-xl bg-zinc-900 px-3 text-sm font-semibold text-white hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={!isConnected || needsSepolia || !walletClient}
                >
                  Create subname
                </button>
              </div>

              {subTxHash ? (
                <div className="mt-3 text-xs text-zinc-600">
                  Submitted tx: <span className="font-mono">{subTxHash}</span>
                </div>
              ) : null}
            </div>

            <div className="rounded-2xl border border-zinc-200 bg-white p-5">
              <div className="text-sm font-medium text-zinc-900">
                5) Create subnames under a subname
              </div>
              <div className="mt-1 text-xs text-zinc-500">
                Example parent: <span className="font-mono">hehe.{NAME}</span> → issue{" "}
                <span className="font-mono">agent1.hehe.{NAME}</span>,{" "}
                <span className="font-mono">agent2.hehe.{NAME}</span>, etc.
              </div>

              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                <div>
                  <div className="text-sm font-medium text-zinc-900">Parent name</div>
                  <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                    <input
                      value={parentInput}
                      onChange={(e) => setParentInput(e.target.value)}
                      placeholder={`hehe.${NAME}`}
                      className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </div>
                  <div className="mt-2 text-xs text-zinc-500">
                    Parent owner on-chain:{" "}
                    <span className="font-mono">
                      {parentName ? parentOnchainOwner ?? "—" : "—"}
                    </span>
                    {parentName && address && parentOnchainOwner && parentOnchainOwner.toLowerCase() !== address.toLowerCase() ? (
                      <span className="ml-2 text-amber-700">
                        (Warning: your wallet is not the registry owner; tx will likely fail)
                      </span>
                    ) : null}
                  </div>
                </div>

                <div>
                  <div className="text-sm font-medium text-zinc-900">Child label</div>
                  <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                    <input
                      value={nestedLabelInput}
                      onChange={(e) => setNestedLabelInput(e.target.value)}
                      placeholder="agent1"
                      className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                      autoComplete="off"
                      spellCheck={false}
                    />
                    {parentName ? (
                      <span className="ml-2 select-none text-sm text-zinc-500">
                        .{parentName}
                      </span>
                    ) : null}
                  </div>
                </div>

                <div className="sm:col-span-2">
                  <div className="text-sm font-medium text-zinc-900">Owner</div>
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
                  <div className="mt-2 text-xs text-zinc-500">
                    Contract mode: <span className="font-medium">registry</span>
                  </div>
                </div>
              </div>

              <div className="mt-4 flex items-center justify-between gap-3">
                <div className="text-xs text-zinc-500">
                  Full name:{" "}
                  <span className="font-mono">
                    {parentName && nestedLabel ? `${nestedLabel}.${parentName}` : "—"}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={createSubnameUnderSubname}
                  className="inline-flex h-9 items-center justify-center rounded-xl bg-zinc-900 px-3 text-sm font-semibold text-white hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={!isConnected || needsSepolia || !walletClient}
                >
                  Create nested subname
                </button>
              </div>

              {nestedTxHash ? (
                <div className="mt-3 text-xs text-zinc-600">
                  Submitted tx: <span className="font-mono">{nestedTxHash}</span>
                </div>
              ) : null}
            </div>

            {status === "registered" ? (
              <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-5 text-sm text-emerald-900">
                Registered successfully (or tx submitted). You can now manage `{NAME}` and create subnames under it.
              </div>
            ) : null}

            {error ? (
              <div className="rounded-2xl border border-red-200 bg-red-50 p-5 text-sm text-red-700">
                {error}
              </div>
            ) : null}
          </div>
        </div>
      </main>
    </div>
  );
}

