"use client";

import { useEffect, useMemo, useState } from "react";
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
import { useAccount, useWalletClient } from "wagmi";

// Official Sepolia deployments (ENS docs)
// https://docs.ens.domains/learn/deployments/
const ETH_REGISTRAR_CONTROLLER = getAddress(
  "0xfb3ce5d01e0f33f41dbb39035db9745962f1f968"
);
const PUBLIC_RESOLVER = getAddress(
  "0xe99638b40e4fff0129d56f03b55b6bbc4bbe49b5"
);
const ENS_REGISTRY = getAddress("0x00000000000c2e074ec69a0dfb2997ba6c7d2e1e");

const ROOT_NAME = "kuber12.eth";
const ROOT_LABEL = "kuber12";

const DEFAULT_DURATION_SECONDS = 365n * 24n * 60n * 60n;

type Registration = {
  label: string;
  owner: `0x${string}`;
  duration: bigint;
  secret: `0x${string}`;
  resolver: `0x${string}`;
  data: `0x${string}`[];
  reverseRecord: number;
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

const registryAbi = parseAbi([
  "function owner(bytes32 node) view returns (address)",
  "function resolver(bytes32 node) view returns (address)",
]);

type PersistedCommit = {
  label: string;
  owner: `0x${string}`;
  duration: string;
  secret: `0x${string}`;
  committedAtMs: number;
};

type PersistedState = {
  name: string;
  lastOwner?: `0x${string}` | null;
  lastResolver?: `0x${string}` | null;
  subnames?: string[];
  nestedSubnames?: Record<string, string[]>;
};

const STORAGE_KEY_COMMIT = "ens-test:sepolia-commit";
const STORAGE_KEY_STATE = "ens-test:sepolia-onchain-state";

function loadCommit(): PersistedCommit | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(STORAGE_KEY_COMMIT);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedCommit;
    if (parsed?.label !== ROOT_LABEL) return null;
    return parsed;
  } catch {
    return null;
  }
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

function coerceRentPrice(value: unknown): { base: bigint; premium: bigint } {
  if (Array.isArray(value) && value.length >= 2) {
    const [base, premium] = value as unknown as [bigint, bigint];
    if (typeof base === "bigint" && typeof premium === "bigint") return { base, premium };
  }
  if (value && typeof value === "object") {
    const v = value as Record<string, unknown>;
    const base = v["base"];
    const premium = v["premium"];
    if (typeof base === "bigint" && typeof premium === "bigint") return { base, premium };
  }
  throw new Error("Unexpected rentPrice() return shape");
}

function randomSecret32(): `0x${string}` {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `0x${hex}`;
}

export function OnchainEnsFlow() {
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

  const needsSepolia = isConnected && chainId !== sepolia.id;

  const persisted = useMemo(() => loadState(), []);

  const [commit, setCommit] = useState<PersistedCommit | null>(() => loadCommit());
  const [minAgeSeconds, setMinAgeSeconds] = useState<bigint>(60n);
  const [maxAgeSeconds, setMaxAgeSeconds] = useState<bigint>(24n * 60n * 60n);

  const [nowMs, setNowMs] = useState(() => Date.now());
  const [durationSeconds] = useState<bigint>(DEFAULT_DURATION_SECONDS);

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

  const [availability, setAvailability] = useState<boolean | null>(null);
  const [priceDisplay, setPriceDisplay] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"none" | "checking" | "committing" | "registering" | "creating-sub" | "creating-nested">("none");

  const isRootOwnedByConnectedWallet = useMemo(() => {
    if (!address || !rootOwner) return false;
    return address.toLowerCase() === rootOwner.toLowerCase();
  }, [address, rootOwner]);

  useEffect(() => {
    if (!commit) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [commit]);

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
        // ignore
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicClient]);

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
        saveState({
          name: ROOT_NAME,
          lastOwner: ownerAddr,
          lastResolver: resolverAddr,
          subnames,
          nestedSubnames: nestedMap,
        });
      } catch {
        // ignore
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicClient, subnames, nestedMap]);

  const commitmentAgeSeconds = useMemo(() => {
    if (!commit) return null;
    return BigInt(Math.max(0, Math.floor((nowMs - commit.committedAtMs) / 1000)));
  }, [commit, nowMs]);

  const secondsUntilReveal = useMemo(() => {
    if (!commitmentAgeSeconds) return null;
    if (commitmentAgeSeconds >= minAgeSeconds) return 0n;
    return minAgeSeconds - commitmentAgeSeconds;
  }, [commitmentAgeSeconds, minAgeSeconds]);

  async function check() {
    setError(null);
    if (!isConnected || !address) return setError("Connect your wallet first.");
    if (needsSepolia) return setError("Switch your wallet network to Sepolia.");
    setBusy("checking");
    try {
      const available = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "available",
        args: [ROOT_LABEL],
      });
      setAvailability(available);
      if (!available) {
        setPriceDisplay("");
        return;
      }
      const price = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "rentPrice",
        args: [ROOT_LABEL, durationSeconds],
      });
      const { base, premium } = coerceRentPrice(price);
      setPriceDisplay(`${formatEther(base + premium)} SepoliaETH`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to check availability.");
    } finally {
      setBusy("none");
    }
  }

  async function commitName() {
    setError(null);
    if (!walletClient || !address) return setError("Wallet client not available. Connect wallet first.");
    if (needsSepolia) return setError("Switch your wallet network to Sepolia.");
    setBusy("committing");
    try {
      const secret = randomSecret32();
      const registration: Registration = {
        label: ROOT_LABEL,
        owner: address,
        duration: durationSeconds,
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
      const persistedCommit: PersistedCommit = {
        label: ROOT_LABEL,
        owner: address,
        duration: durationSeconds.toString(),
        secret,
        committedAtMs: Date.now(),
      };
      localStorage.setItem(STORAGE_KEY_COMMIT, JSON.stringify(persistedCommit));
      setCommit(persistedCommit);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Commit failed.");
    } finally {
      setBusy("none");
    }
  }

  async function registerName() {
    setError(null);
    if (!walletClient || !address) return setError("Wallet client not available. Connect wallet first.");
    if (!commit) return setError("No commitment found. Commit first.");
    if (needsSepolia) return setError("Switch your wallet network to Sepolia.");

    setBusy("registering");
    try {
      const duration = BigInt(commit.duration);
      const price = await publicClient.readContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "rentPrice",
        args: [ROOT_LABEL, duration],
      });
      const { base, premium } = coerceRentPrice(price);
      const total = base + premium;
      const value = (total * 105n) / 100n;

      const registration = {
        label: ROOT_LABEL,
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
      if (committedAt === 0n) throw new Error("Commitment not found on-chain. Re-commit.");

      const block = await publicClient.getBlock();
      const ageFromChain = block.timestamp - committedAt;
      if (ageFromChain < minAgeSeconds) {
        throw new Error(`Wait ~${Number(minAgeSeconds - ageFromChain)}s more before registering.`);
      }
      if (ageFromChain > maxAgeSeconds) throw new Error("Commitment expired. Commit again.");

      const { request } = await publicClient.simulateContract({
        address: ETH_REGISTRAR_CONTROLLER,
        abi: controllerAbi,
        functionName: "register",
        args: [registration],
        value,
        account: walletClient.account,
      });

      const gas =
        typeof request.gas === "bigint" ? (request.gas * 130n) / 100n : undefined;

      await walletClient.writeContract({ ...request, gas });

      localStorage.removeItem(STORAGE_KEY_COMMIT);
      setCommit(null);
      await check();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Register failed.");
    } finally {
      setBusy("none");
    }
  }

  const [subLabelInput, setSubLabelInput] = useState("");
  const subLabel = useMemo(
    () => subLabelInput.trim().toLowerCase().replace(/\s+/g, ""),
    [subLabelInput]
  );
  const [subOwnerInput, setSubOwnerInput] = useState("");
  const subOwner = useMemo(() => {
    const v = subOwnerInput.trim();
    if (!v) return address ?? null;
    return v as `0x${string}`;
  }, [subOwnerInput, address]);

  async function createUnderRoot() {
    setError(null);
    if (!walletClient) return setError("Wallet client not available. Connect wallet first.");
    if (!isRootOwnedByConnectedWallet) return setError(`You must own ${ROOT_NAME} to issue subnames under it.`);
    if (!subLabel) return setError("Enter a subname label (e.g. hehe).");
    if (!subOwner) return setError("Set an owner address.");
    if (needsSepolia) return setError("Switch your wallet network to Sepolia.");

    setBusy("creating-sub");
    try {
      const ensWallet = createWalletClient({
        account: walletClient.account,
        chain: addEnsContracts(sepolia),
        transport: custom(walletClient.transport),
      });
      const full = `${subLabel}.${ROOT_NAME}`;
      await createSubname(ensWallet, { name: full, owner: subOwner, contract: "registry" });
      setSubnames((prev) => {
        const next = Array.from(new Set([full, ...prev]));
        saveState({
          name: ROOT_NAME,
          lastOwner: rootOwner,
          lastResolver: rootResolver,
          subnames: next,
          nestedSubnames: nestedMap,
        });
        return next;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create subname.");
    } finally {
      setBusy("none");
    }
  }

  const [parentInput, setParentInput] = useState("");
  const parentName = useMemo(() => {
    const v = parentInput.trim().toLowerCase().replace(/\s+/g, "");
    return v || null;
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
  const [parentOnchainOwner, setParentOnchainOwner] = useState<`0x${string}` | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!parentName) return setParentOnchainOwner(null);
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

  async function createNested() {
    setError(null);
    if (!walletClient) return setError("Wallet client not available. Connect wallet first.");
    if (!parentName) return setError("Enter a parent name (e.g. hehe.kuber12.eth).");
    if (!nestedLabel) return setError("Enter a child label (e.g. agent1).");
    if (!nestedOwner) return setError("Set an owner address.");
    if (needsSepolia) return setError("Switch your wallet network to Sepolia.");

    setBusy("creating-nested");
    try {
      const ensWallet = createWalletClient({
        account: walletClient.account,
        chain: addEnsContracts(sepolia),
        transport: custom(walletClient.transport),
      });
      const full = `${nestedLabel}.${parentName}`;
      await createSubname(ensWallet, { name: full, owner: nestedOwner, contract: "registry" });
      setNestedMap((prev) => {
        const list = prev[parentName] ?? [];
        const nextList = Array.from(new Set([full, ...list]));
        const next = { ...prev, [parentName]: nextList };
        saveState({
          name: ROOT_NAME,
          lastOwner: rootOwner,
          lastResolver: rootResolver,
          subnames,
          nestedSubnames: next,
        });
        return next;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create nested subname.");
    } finally {
      setBusy("none");
    }
  }

  return (
    <div className="w-full rounded-3xl border border-zinc-200 bg-white p-8 shadow-[0_20px_60px_-30px_rgba(0,0,0,0.25)]">
      <div className="flex items-start justify-between gap-6">
        <div className="min-w-0">
          <h2 className="text-xl font-semibold tracking-tight">On-chain ENS flow</h2>
          <p className="mt-2 text-sm leading-6 text-zinc-600">
            Guided flow for Sepolia: own <span className="font-medium text-zinc-900">{ROOT_NAME}</span>, then create{" "}
            <span className="font-medium text-zinc-900">subnames</span> and{" "}
            <span className="font-medium text-zinc-900">nested subnames</span>.
          </p>
        </div>
        <div className="shrink-0 text-right text-xs text-zinc-500">
          <div>
            Network:{" "}
            <span className={needsSepolia ? "font-medium text-red-600" : "font-medium text-zinc-900"}>
              {needsSepolia ? "Wrong network" : "Sepolia"}
            </span>
          </div>
          <div className="mt-1 font-mono">
            {address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "—"}
          </div>
        </div>
      </div>

      <div className="mt-6 rounded-2xl border border-zinc-200 bg-zinc-50 p-5">
        <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          On-chain status (persists after refresh)
        </div>
        <div className="mt-3 grid gap-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-zinc-500">Owner:</span>
            <span className="font-mono text-zinc-900">{rootOwner ?? "—"}</span>
            {isRootOwnedByConnectedWallet ? (
              <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800">
                You own it
              </span>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-zinc-500">Resolver:</span>
            <span className="font-mono text-zinc-900">{rootResolver ?? "—"}</span>
          </div>
        </div>
      </div>

      <div className="mt-6 grid gap-4">
        <div className="rounded-2xl border border-zinc-200 bg-white p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-medium text-zinc-900">Step 1 — Check availability</div>
              <div className="mt-1 text-xs text-zinc-500">
                If you don’t own it yet, we’ll help you register it via commit → wait → register.
              </div>
            </div>
            <button
              type="button"
              onClick={check}
              disabled={!isConnected || needsSepolia || busy !== "none"}
              className="inline-flex h-9 items-center justify-center rounded-xl border border-zinc-200 bg-white px-3 text-sm font-medium text-zinc-800 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy === "checking" ? "Checking…" : "Check"}
            </button>
          </div>
          <div className="mt-3 text-sm text-zinc-700">
            {availability === null
              ? "—"
              : availability
                ? `${ROOT_NAME} is available. Estimated price: ${priceDisplay || "—"}`
                : `${ROOT_NAME} is not available (already registered).`}
          </div>
        </div>

        {!isRootOwnedByConnectedWallet && (
          <div className="rounded-2xl border border-zinc-200 bg-white p-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium text-zinc-900">Step 2 — Commit</div>
                <div className="mt-1 text-xs text-zinc-500">
                  We generate a secret in your browser and save it so refresh won’t break the flow.
                </div>
              </div>
              <button
                type="button"
                onClick={commitName}
                disabled={!isConnected || needsSepolia || !walletClient || busy !== "none"}
                className="inline-flex h-9 items-center justify-center rounded-xl bg-zinc-900 px-3 text-sm font-semibold text-white hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy === "committing" ? "Committing…" : "Commit"}
              </button>
            </div>

            {commit ? (
              <div className="mt-3 text-xs text-zinc-600">
                Commitment saved. Ready in:{" "}
                <span className="font-medium text-zinc-900">
                  {secondsUntilReveal !== null ? `${Number(secondsUntilReveal)}s` : "—"}
                </span>
                {" · "}Expires in ~{Number(maxAgeSeconds / 3600n)}h
              </div>
            ) : null}
          </div>
        )}

        {!isRootOwnedByConnectedWallet && (
          <div className="rounded-2xl border border-zinc-200 bg-white p-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium text-zinc-900">Step 3 — Register</div>
                <div className="mt-1 text-xs text-zinc-500">
                  Requires the commitment to be at least {Number(minAgeSeconds)}s old.
                </div>
              </div>
              <button
                type="button"
                onClick={registerName}
                disabled={!isConnected || needsSepolia || !walletClient || !commit || busy !== "none"}
                className="inline-flex h-9 items-center justify-center rounded-xl bg-emerald-700 px-3 text-sm font-semibold text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy === "registering" ? "Registering…" : "Register"}
              </button>
            </div>
            <div className="mt-3 text-xs text-zinc-500">
              We send `value = price * 1.05` and `gas = estimate * 1.30` to reduce failure risk.
            </div>
          </div>
        )}

        <div className="rounded-2xl border border-zinc-200 bg-white p-5">
          <div className="text-sm font-medium text-zinc-900">Step 4 — Create a subname</div>
          <div className="mt-1 text-xs text-zinc-500">
            Example: <span className="font-mono">hehe.{ROOT_NAME}</span>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div>
              <div className="text-sm font-medium text-zinc-900">Label</div>
              <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                <input
                  value={subLabelInput}
                  onChange={(e) => setSubLabelInput(e.target.value)}
                  placeholder="hehe"
                  className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                />
                <span className="ml-2 select-none text-sm text-zinc-500">.{ROOT_NAME}</span>
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
              onClick={createUnderRoot}
              disabled={!isConnected || needsSepolia || !walletClient || busy !== "none"}
              className="inline-flex h-9 items-center justify-center rounded-xl bg-zinc-900 px-3 text-sm font-semibold text-white hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy === "creating-sub" ? "Creating…" : "Create subname"}
            </button>
          </div>

          {subnames.length ? (
            <div className="mt-4">
              <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                Created subnames
              </div>
              <ul className="mt-2 space-y-1">
                {subnames.slice(0, 8).map((n) => (
                  <li key={n} className="font-mono text-xs text-zinc-700">
                    {n}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>

        <div className="rounded-2xl border border-zinc-200 bg-white p-5">
          <div className="text-sm font-medium text-zinc-900">Step 5 — Create a nested subname</div>
          <div className="mt-1 text-xs text-zinc-500">
            Example: <span className="font-mono">agent1.hehe.{ROOT_NAME}</span>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div>
              <div className="text-sm font-medium text-zinc-900">Parent name</div>
              <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                <input
                  value={parentInput}
                  onChange={(e) => setParentInput(e.target.value)}
                  placeholder={`hehe.${ROOT_NAME}`}
                  className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                />
              </div>
              <div className="mt-2 text-xs text-zinc-500">
                Parent owner:{" "}
                <span className="font-mono">{parentName ? parentOnchainOwner ?? "—" : "—"}</span>
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
                />
                {parentName ? (
                  <span className="ml-2 select-none text-sm text-zinc-500">.{parentName}</span>
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
                />
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
              onClick={createNested}
              disabled={!isConnected || needsSepolia || !walletClient || busy !== "none"}
              className="inline-flex h-9 items-center justify-center rounded-xl bg-zinc-900 px-3 text-sm font-semibold text-white hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy === "creating-nested" ? "Creating…" : "Create nested subname"}
            </button>
          </div>

          {Object.keys(nestedMap).length ? (
            <div className="mt-4">
              <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                Nested subnames
              </div>
              <div className="mt-2 space-y-3">
                {Object.entries(nestedMap).slice(0, 3).map(([parent, items]) => (
                  <div key={parent}>
                    <div className="text-xs text-zinc-500">
                      Parent: <span className="font-mono text-zinc-900">{parent}</span>
                    </div>
                    <ul className="mt-1 space-y-1">
                      {items.slice(0, 6).map((n) => (
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

        {error ? (
          <div className="rounded-2xl border border-red-200 bg-red-50 p-5 text-sm text-red-700">
            {error}
          </div>
        ) : null}
      </div>
    </div>
  );
}

