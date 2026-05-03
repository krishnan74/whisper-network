"use client";

import { useMemo, useState } from "react";
import { useAccount } from "wagmi";
import {
  useAccountSubnames,
  useAddSubname,
  useReverseResolve,
  useJustaName,
} from "@justaname.id/react";
import { useDebounce } from "@uidotdev/usehooks";
import { useQuery } from "@tanstack/react-query";
import { createWalletClient, custom } from "viem";
import { sepolia } from "viem/chains";
import { addEnsContracts } from "@ensdomains/ensjs";
import { createSubname } from "@ensdomains/ensjs/wallet";
import { useWalletClient } from "wagmi";

const PARENT_DOMAIN = "axl.eth";
const CHAIN_ID = 11155111; // sepolia

type SignatureFreeAddParams = {
  username: string;
  ensDomain: string;
  chainId: number;
  overrideSignatureCheck?: boolean;
};

type SignatureFreeHeaders = {
  xApiKey: string;
  xAddress?: string;
};

type JustaNameLike = {
  subnames: {
    addSubname: (params: SignatureFreeAddParams, headers: SignatureFreeHeaders) => Promise<unknown>;
    isSubnameAvailable: (params: { subname: string; chainId: number }) => Promise<unknown>;
  };
};

function normalizeUsername(input: string) {
  return input.trim().toLowerCase().replace(/\s+/g, "");
}

function coerceAvailable(value: unknown): boolean | null {
  if (typeof value === "boolean") return value;
  if (value && typeof value === "object") {
    const v = value as Record<string, unknown>;
    if (typeof v.available === "boolean") return v.available;
    if (typeof v.isAvailable === "boolean") return v.isAvailable;
    if (typeof v.isSubnameAvailable === "boolean") return v.isSubnameAvailable;
  }
  return null;
}

export function EnsSubnameManager() {
  const { address, isConnected } = useAccount();
  const { justaname } = useJustaName();
  const client = justaname as unknown as JustaNameLike;
  const { data: walletClient } = useWalletClient({ chainId: CHAIN_ID });
  const { accountSubnames, isAccountSubnamesLoading } = useAccountSubnames({
    enabled: isConnected,
    chainId: CHAIN_ID,
  });

  const existingSubname = useMemo(() => {
    const items = accountSubnames ?? [];
    return (
      items.find((s) => s.ens?.toLowerCase().endsWith(`.${PARENT_DOMAIN}`)) ??
      items[0] ??
      null
    );
  }, [accountSubnames]);

  const { ensName, isReverseResolveLoading } = useReverseResolve({
    address,
    chainId: CHAIN_ID,
    enabled: isConnected && !!address,
  });

  // Resolve-first: if reverse record exists, show that name.
  // If not, fall back to any owned subname under axl.eth.
  const resolvedName = ensName ?? null;
  const ownedName = existingSubname?.ens ?? null;
  const displayedName = resolvedName || ownedName;
  const hasAnyName = !!displayedName;

  const nestedParentName = useMemo(() => {
    const lowerResolved = resolvedName?.toLowerCase() ?? "";
    const lowerOwned = ownedName?.toLowerCase() ?? "";
    if (lowerResolved && lowerResolved.endsWith(`.${PARENT_DOMAIN}`)) return lowerResolved;
    if (lowerOwned && lowerOwned.endsWith(`.${PARENT_DOMAIN}`)) return lowerOwned;
    return null;
  }, [resolvedName, ownedName]);

  const [usernameInput, setUsernameInput] = useState("");
  const username = useMemo(
    () => normalizeUsername(usernameInput),
    [usernameInput]
  );
  const debouncedUsername = useDebounce(username, 400);

  const availabilityQuery = useQuery({
    queryKey: ["isSubnameAvailable", CHAIN_ID, PARENT_DOMAIN, debouncedUsername],
    enabled: Boolean(justaname) && isConnected && !!debouncedUsername && !hasAnyName,
    queryFn: async () => {
      const res = await justaname!.subnames.isSubnameAvailable({
        subname: `${debouncedUsername}.${PARENT_DOMAIN}`,
        chainId: CHAIN_ID,
      });
      return res;
    },
    staleTime: 15_000,
  });

  const { addSubname, isAddSubnamePending } = useAddSubname();
  const [claimError, setClaimError] = useState<string | null>(null);
  const [claimSuccess, setClaimSuccess] = useState(false);
  const [signatureFree] = useState(true);

  const available = coerceAvailable(availabilityQuery.data);

  const canClaim =
    isConnected &&
    !!debouncedUsername &&
    available === true &&
    !availabilityQuery.isPending &&
    !availabilityQuery.isFetching &&
    !isAddSubnamePending &&
    !hasAnyName;

  const [childLabelInput, setChildLabelInput] = useState("");
  const childLabel = useMemo(
    () => normalizeUsername(childLabelInput),
    [childLabelInput]
  );
  const debouncedChildLabel = useDebounce(childLabel, 350);
  const nestedFullName =
    debouncedChildLabel && nestedParentName
      ? `${debouncedChildLabel}.${nestedParentName}`
      : "";

  const nestedAvailabilityQuery = useQuery({
    queryKey: ["isSubnameAvailableNested", CHAIN_ID, nestedParentName, nestedFullName],
    enabled: Boolean(justaname) && isConnected && !!nestedFullName && !!nestedParentName,
    queryFn: async () => {
      return client.subnames.isSubnameAvailable({ subname: nestedFullName, chainId: CHAIN_ID });
    },
    staleTime: 15_000,
  });
  const nestedAvailable = coerceAvailable(nestedAvailabilityQuery.data);

  const [nestedClaimError, setNestedClaimError] = useState<string | null>(null);
  const [nestedClaimSuccess, setNestedClaimSuccess] = useState(false);
  const [isNestedClaiming, setIsNestedClaiming] = useState(false);
  const [nestedTargetAddressInput, setNestedTargetAddressInput] = useState("");
  const nestedTargetAddress = useMemo(() => {
    const v = nestedTargetAddressInput.trim();
    if (!v) return address ?? null;
    return v as `0x${string}`;
  }, [nestedTargetAddressInput, address]);

  const canClaimNested =
    isConnected &&
    !!debouncedChildLabel &&
    !!nestedParentName &&
    nestedAvailable === true &&
    !nestedAvailabilityQuery.isPending &&
    !nestedAvailabilityQuery.isFetching &&
    !isNestedClaiming;

  return (
    <div className="w-full rounded-3xl border border-zinc-200 bg-white p-8 shadow-[0_20px_60px_-30px_rgba(0,0,0,0.25)]">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold tracking-tight text-zinc-900">
            Your ENS subname
          </h2>
          <p className="mt-2 text-sm leading-6 text-zinc-600">
            Create and use a subname under{" "}
            <span className="font-medium text-zinc-900">{PARENT_DOMAIN}</span>.
          </p>
        </div>
      </div>

      <div className="mt-6 rounded-2xl border border-zinc-200 bg-zinc-50 p-5">
        <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          Resolved name
        </div>
        <div className="mt-2 flex items-center justify-between gap-3">
          <div className="min-w-0">
            {!isConnected ? (
              <div className="text-sm text-zinc-600">
                Connect your wallet to see your name.
              </div>
            ) : isReverseResolveLoading || isAccountSubnamesLoading ? (
              <div className="text-sm text-zinc-600">
                Resolving…
              </div>
            ) : displayedName ? (
              <div className="truncate text-lg font-semibold text-zinc-900">
                {displayedName}
              </div>
            ) : (
              <div className="text-sm text-zinc-600">
                No subname found yet.
              </div>
            )}
          </div>
          {address && (
            <div className="shrink-0 rounded-lg border border-zinc-200 bg-white px-2 py-1 text-xs font-medium text-zinc-700">
              {address.slice(0, 6)}…{address.slice(-4)}
            </div>
          )}
        </div>
      </div>

      {!hasAnyName && (
        <div className="mt-5">
          <div className="text-sm font-medium text-zinc-900">
            Claim a subname
          </div>
          <div className="mt-1 text-xs text-zinc-500">
            Signature-free onboarding is enabled (no wallet pop-up).
          </div>
          <div className="mt-2 flex flex-col gap-3 sm:flex-row">
            <div className="flex-1">
              <label className="sr-only" htmlFor="username">
                Username
              </label>
              <div className="flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                <input
                  id="username"
                  value={usernameInput}
                  onChange={(e) => setUsernameInput(e.target.value)}
                  placeholder="yourname"
                  className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                  autoComplete="off"
                  spellCheck={false}
                />
                <span className="ml-2 select-none text-sm text-zinc-500">
                  .{PARENT_DOMAIN}
                </span>
              </div>
              <div className="mt-2 text-xs text-zinc-500">
                {debouncedUsername ? (
                  availabilityQuery.isPending || availabilityQuery.isFetching ? (
                    "Checking availability…"
                  ) : available === true ? (
                    "Available"
                  ) : available === false ? (
                    "Taken"
                  ) : availabilityQuery.error ? (
                    `Error: ${
                      availabilityQuery.error instanceof Error
                        ? availabilityQuery.error.message
                        : "Failed to check"
                    }`
                  ) : (
                    "Couldn’t check availability"
                  )
                ) : (
                  "Type a username to check availability."
                )}
              </div>
            </div>

            <button
              type="button"
              disabled={!canClaim}
              onClick={async () => {
                setClaimError(null);
                setClaimSuccess(false);
                try {
                  if (signatureFree) {
                    // Docs: https://docs.justaname.id/react-sdk/issue-subnames
                    // The current SDK version doesn't type/expose `overrideSignatureCheck`,
                    // so we call the underlying client directly.
                    await client.subnames.addSubname(
                      {
                        username: debouncedUsername,
                        ensDomain: PARENT_DOMAIN,
                        chainId: CHAIN_ID,
                        overrideSignatureCheck: true,
                      },
                      {
                        xApiKey: process.env.NEXT_PUBLIC_JUSTANAME_API_KEY ?? "",
                        xAddress: address,
                      }
                    );
                  } else {
                    await addSubname({
                      username: debouncedUsername,
                      ensDomain: PARENT_DOMAIN,
                      chainId: CHAIN_ID,
                      apiKey: process.env.NEXT_PUBLIC_JUSTANAME_API_KEY ?? "",
                    });
                  }
                  setClaimSuccess(true);
                  setUsernameInput("");
                } catch (e) {
                  setClaimError(e instanceof Error ? e.message : "Failed to claim subname");
                }
              }}
              className="inline-flex h-12 items-center justify-center rounded-2xl bg-zinc-900 px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isAddSubnamePending ? "Claiming…" : "Claim"}
            </button>
          </div>

          {claimError && (
            <div className="mt-3 text-sm text-red-600">
              {claimError}
            </div>
          )}
          {claimSuccess && (
            <div className="mt-3 text-sm text-emerald-700">
              Claimed successfully.
            </div>
          )}
        </div>
      )}

      <div className="mt-8 border-t border-zinc-200 pt-6">
        <div className="text-sm font-medium text-zinc-900">
          Create subnames under your subname
        </div>
        <div className="mt-1 text-xs text-zinc-500">
          Example: if your parent is <span className="font-medium">node.{PARENT_DOMAIN}</span>, you can issue{" "}
          <span className="font-medium">agent1.node.{PARENT_DOMAIN}</span>. Signature-free onboarding is enabled.
        </div>

        <div className="mt-4">
          <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
            Parent name
          </div>
          <div className="mt-2 rounded-2xl border border-zinc-200 bg-zinc-50 px-4 py-3">
            {!isConnected ? (
              <div className="text-sm text-zinc-600">Connect your wallet to detect your parent name.</div>
            ) : isReverseResolveLoading || isAccountSubnamesLoading ? (
              <div className="text-sm text-zinc-600">Detecting…</div>
            ) : nestedParentName ? (
              <div className="truncate text-sm font-semibold text-zinc-900">{nestedParentName}</div>
            ) : (
              <div className="text-sm text-zinc-600">
                No <span className="font-medium">*.{PARENT_DOMAIN}</span> name found for this wallet yet. Claim one above first.
              </div>
            )}
          </div>
        </div>

        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1">
            <div className="text-sm font-medium text-zinc-900">New label</div>
            <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
              <input
                value={childLabelInput}
                onChange={(e) => setChildLabelInput(e.target.value)}
                placeholder="agent1"
                className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                autoComplete="off"
                spellCheck={false}
              />
              {nestedParentName ? (
                <span className="ml-2 select-none text-sm text-zinc-500">
                  .{nestedParentName}
                </span>
              ) : null}
            </div>
            <div className="mt-2 text-xs text-zinc-500">
              {nestedFullName ? (
                nestedAvailabilityQuery.isPending || nestedAvailabilityQuery.isFetching ? (
                  "Checking availability…"
                ) : nestedAvailable === true ? (
                  "Available"
                ) : nestedAvailable === false ? (
                  "Taken"
                ) : nestedAvailabilityQuery.error ? (
                  `Error: ${
                    nestedAvailabilityQuery.error instanceof Error
                      ? nestedAvailabilityQuery.error.message
                      : "Failed to check"
                  }`
                ) : (
                  "Couldn’t check availability"
                )
              ) : (
                "Type a label to check availability."
              )}
            </div>
          </div>

          <div className="flex-1">
            <div className="text-sm font-medium text-zinc-900">Owner (target address)</div>
            <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
              <input
                value={nestedTargetAddressInput}
                onChange={(e) => setNestedTargetAddressInput(e.target.value)}
                placeholder={address ? `${address.slice(0, 6)}…${address.slice(-4)} (default)` : "0x…"}
                className="w-full bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-400"
                autoComplete="off"
                spellCheck={false}
              />
            </div>
            <div className="mt-2 text-xs text-zinc-500">
              This address will control the new subname.
            </div>
          </div>

          <button
            type="button"
            disabled={!canClaimNested}
            onClick={async () => {
              setNestedClaimError(null);
              setNestedClaimSuccess(false);
              setIsNestedClaiming(true);
              try {
                if (!walletClient) {
                  throw new Error("Wallet client not available. Connect wallet first.");
                }
                if (!nestedParentName) {
                  throw new Error("Parent name not detected.");
                }
                if (!nestedTargetAddress) {
                  throw new Error("Target address not set.");
                }

                // ENSjs wallet must be created with ENS contracts attached to the chain.
                const ensWallet = createWalletClient({
                  account: walletClient.account,
                  chain: addEnsContracts(sepolia),
                  transport: custom(walletClient.transport),
                });

                await createSubname(ensWallet, {
                  name: `${debouncedChildLabel}.${nestedParentName}`,
                  owner: nestedTargetAddress,
                  contract: "registry",
                });

                setNestedClaimSuccess(true);
                setChildLabelInput("");
                nestedAvailabilityQuery.refetch();
              } catch (e) {
                const msg = e instanceof Error ? e.message : "Failed to claim subname";
                setNestedClaimError(
                  msg.includes("EnsNotFoundException")
                    ? `${msg}. This message is from JustaName, but nested issuance here uses ENSjs. If you still see it, it may be coming from the availability check.`
                    : msg
                );
              } finally {
                setIsNestedClaiming(false);
              }
            }}
            className="inline-flex h-12 items-center justify-center rounded-2xl bg-zinc-900 px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isNestedClaiming ? "Claiming…" : "Claim"}
          </button>
        </div>

        {nestedClaimError && (
          <div className="mt-3 text-sm text-red-600">{nestedClaimError}</div>
        )}
        {nestedClaimSuccess && (
          <div className="mt-3 text-sm text-emerald-700">Claimed successfully.</div>
        )}

        <div className="mt-4 text-xs text-zinc-500">
          Note: ENSjs issuance is on-chain. You’ll get a wallet transaction pop-up and pay gas. It does not depend on JustaName being configured for the parent.
        </div>
      </div>
    </div>
  );
}

