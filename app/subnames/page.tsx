"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { ConnectButton } from "@rainbow-me/rainbowkit";
import { useAccount } from "wagmi";
import {
  useAccountSubnames,
  useJustaName,
  useReverseResolve,
  useSearchSubnames,
} from "@justaname.id/react";
import { useDebounce } from "@uidotdev/usehooks";
import { useQuery } from "@tanstack/react-query";

const CHAIN_ID = 11155111; // sepolia
const ROOT_DOMAIN = "axl.eth";

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
    addSubname: (
      params: SignatureFreeAddParams,
      headers: SignatureFreeHeaders
    ) => Promise<unknown>;
    isSubnameAvailable: (params: { subname: string; chainId: number }) => Promise<unknown>;
  };
};

function normalizeLabel(input: string) {
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

export default function SubnamesPage() {
  const { address, isConnected } = useAccount();
  const { justaname } = useJustaName();
  const client = justaname as unknown as JustaNameLike;

  const { ensName, isReverseResolveLoading } = useReverseResolve({
    address,
    chainId: CHAIN_ID,
    enabled: isConnected && !!address,
  });

  const { accountSubnames, isAccountSubnamesLoading } = useAccountSubnames({
    enabled: isConnected,
    chainId: CHAIN_ID,
  });

  const ownedRootSubname = useMemo(() => {
    const items = accountSubnames ?? [];
    return (
      items.find((s) => s.ens?.toLowerCase().endsWith(`.${ROOT_DOMAIN}`)) ??
      items[0] ??
      null
    );
  }, [accountSubnames]);

  const resolvedParent = useMemo(() => {
    const maybe = ensName?.toLowerCase() ?? "";
    if (!maybe) return null;
    // Only allow nesting under names that are under our root domain.
    if (!maybe.endsWith(`.${ROOT_DOMAIN}`)) return null;
    return maybe;
  }, [ensName]);

  const fallbackParent = ownedRootSubname?.ens?.toLowerCase() ?? null;
  const parentName = resolvedParent || fallbackParent;
  const [labelInput, setLabelInput] = useState("");
  const label = useMemo(() => normalizeLabel(labelInput), [labelInput]);
  const debouncedLabel = useDebounce(label, 350);

  // Desired behavior:
  // parent = node.axl.eth
  // username = agent1
  // => agent1.node.axl.eth
  const usernameToIssue = debouncedLabel;
  const fullName =
    usernameToIssue && parentName ? `${usernameToIssue}.${parentName}` : "";

  const searchTerm = parentName ? `.${parentName}` : "";
  const { subnames, isSubnamesLoading, refetchSearchSubnames } = useSearchSubnames({
    name: searchTerm,
    chainId: CHAIN_ID,
    take: 50,
    skip: 0,
    data: true,
    ensRegistered: false,
    isClaimed: true,
    enabled: isConnected && !!searchTerm,
  });

  const matchingSubnames = useMemo(() => {
    const suffix = searchTerm;
    const domains = subnames?.domains ?? [];
    const items: string[] = [];

    for (const d of domains as unknown as Array<Record<string, unknown>>) {
      const ensSubname = d["ensSubname"] as Record<string, unknown> | undefined;
      const ens = ensSubname?.["ens"] as string | undefined;
      if (ens && ens.toLowerCase().endsWith(suffix)) items.push(ens);
    }

    // Sort agentN nicely if possible, otherwise alphabetical.
    return items.sort((a, b) => a.localeCompare(b));
  }, [subnames, searchTerm]);

  const availabilityQuery = useQuery({
    queryKey: ["isSubnameAvailable", CHAIN_ID, parentName, fullName],
    enabled: Boolean(justaname) && isConnected && !!fullName && !!parentName,
    queryFn: async () => {
      return client.subnames.isSubnameAvailable({ subname: fullName, chainId: CHAIN_ID });
    },
    staleTime: 15_000,
  });

  const available = coerceAvailable(availabilityQuery.data);

  const [claimError, setClaimError] = useState<string | null>(null);
  const [claimSuccess, setClaimSuccess] = useState(false);
  const [isClaiming, setIsClaiming] = useState(false);

  const canClaim =
    isConnected &&
    !!debouncedLabel &&
    !!parentName &&
    available === true &&
    !availabilityQuery.isPending &&
    !availabilityQuery.isFetching &&
    !isClaiming;

  return (
    <div className="min-h-screen bg-white text-zinc-900">
      <header className="mx-auto flex w-full max-w-4xl items-center justify-between px-6 py-10">
        <div className="flex items-center gap-3">
          <Link
            href="/"
            className="text-sm font-medium text-zinc-700 hover:text-zinc-900"
          >
            Home
          </Link>
          <span className="text-zinc-300">/</span>
          <div className="text-base font-semibold tracking-tight">Subnames</div>
        </div>
        <ConnectButton />
      </header>

      <main className="mx-auto w-full max-w-4xl px-6 pb-16">
        <div className="w-full rounded-3xl border border-zinc-200 bg-white p-8 shadow-[0_20px_60px_-30px_rgba(0,0,0,0.25)]">
          <h1 className="text-xl font-semibold tracking-tight">
            Create subnames under a subname
          </h1>
          <p className="mt-2 text-sm leading-6 text-zinc-600">
            We automatically pick your parent name from your wallet (reverse record first, then your owned{" "}
            <span className="font-medium text-zinc-900">*.{ROOT_DOMAIN}</span> subname).
            Then you can issue names like{" "}
            <span className="font-medium text-zinc-900">agent1.&lt;your-parent&gt;</span>{" "}
            (e.g. <span className="font-medium text-zinc-900">agent1.node.{ROOT_DOMAIN}</span>).
          </p>

          <div className="mt-6 grid gap-4">
            <div>
              <div className="text-sm font-medium text-zinc-900">Parent name</div>
              <div className="mt-2 rounded-2xl border border-zinc-200 bg-zinc-50 px-4 py-3">
                {!isConnected ? (
                  <div className="text-sm text-zinc-600">Connect wallet to detect your parent name.</div>
                ) : isReverseResolveLoading || isAccountSubnamesLoading ? (
                  <div className="text-sm text-zinc-600">Detecting…</div>
                ) : parentName ? (
                  <div className="text-sm font-semibold text-zinc-900">{parentName}</div>
                ) : (
                  <div className="text-sm text-zinc-600">
                    No <span className="font-medium">*.{ROOT_DOMAIN}</span> name found for this wallet yet.
                    Claim one on the Home page first.
                  </div>
                )}
              </div>
              <div className="mt-2 text-xs text-zinc-500">
                This becomes the `ensDomain` for issuance.
              </div>
            </div>

            <div>
              <div className="text-sm font-medium text-zinc-900">New label</div>
              <div className="mt-2 flex items-center rounded-2xl border border-zinc-200 bg-white px-4 py-3 shadow-sm focus-within:ring-4 focus-within:ring-zinc-100">
                <input
                  value={labelInput}
                  onChange={(e) => setLabelInput(e.target.value)}
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

              <div className="mt-2 text-xs text-zinc-500">
                {fullName ? (
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
                  "Enter a label to check availability."
                )}
              </div>
            </div>

            <div className="flex items-center justify-between gap-3">
              <div className="text-xs text-zinc-500">
                Signature-free onboarding is enabled (no wallet pop-up).
              </div>
              <button
                type="button"
                disabled={!canClaim}
                onClick={async () => {
                  setClaimError(null);
                  setClaimSuccess(false);
                  setIsClaiming(true);
                  try {
                    await client.subnames.addSubname(
                      {
                        username: usernameToIssue,
                        ensDomain: parentName,
                        chainId: CHAIN_ID,
                        overrideSignatureCheck: true,
                      },
                      {
                        xApiKey: process.env.NEXT_PUBLIC_JUSTANAME_API_KEY ?? "",
                        xAddress: address,
                      }
                    );
                    setClaimSuccess(true);
                    setLabelInput("");
                    availabilityQuery.refetch();
                    refetchSearchSubnames();
                  } catch (e) {
                    const msg =
                      e instanceof Error ? e.message : "Failed to claim subname";
                    setClaimError(
                      msg.includes("EnsNotFoundException")
                        ? `${msg}. This means JustaName is not configured to issue under "${parentName}". You must enable/configure issuing for "${parentName}" in the JustaName dashboard (or nested issuance is not supported for subnames).`
                        : msg
                    );
                  } finally {
                    setIsClaiming(false);
                  }
                }}
                className="inline-flex h-12 items-center justify-center rounded-2xl bg-zinc-900 px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isClaiming ? "Claiming…" : "Claim"}
              </button>
            </div>

            {claimError && <div className="text-sm text-red-600">{claimError}</div>}
            {claimSuccess && (
              <div className="text-sm text-emerald-700">
                Claimed successfully.
              </div>
            )}

            <div className="mt-2 rounded-2xl border border-zinc-200 bg-white p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-medium text-zinc-900">
                    Subnames under this parent
                  </div>
                  <div className="mt-1 text-xs text-zinc-500">
                    Showing issued names that match <span className="font-medium">{searchTerm || "—"}</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => refetchSearchSubnames()}
                  className="inline-flex h-9 items-center justify-center rounded-xl border border-zinc-200 bg-white px-3 text-sm font-medium text-zinc-800 hover:bg-zinc-50"
                  disabled={!isConnected || !searchTerm}
                >
                  Refresh
                </button>
              </div>

              <div className="mt-4">
                {!isConnected ? (
                  <div className="text-sm text-zinc-600">Connect wallet to load subnames.</div>
                ) : !searchTerm ? (
                  <div className="text-sm text-zinc-600">Detecting parent name…</div>
                ) : isSubnamesLoading ? (
                  <div className="text-sm text-zinc-600">Loading…</div>
                ) : matchingSubnames.length ? (
                  <ul className="space-y-2">
                    {matchingSubnames.map((ens) => (
                      <li
                        key={ens}
                        className="flex items-center justify-between rounded-xl bg-zinc-50 px-3 py-2"
                      >
                        <div className="truncate text-sm font-medium text-zinc-900">
                          {ens}
                        </div>
                        <button
                          type="button"
                          className="text-xs font-medium text-zinc-700 hover:text-zinc-900"
                          onClick={async () => {
                            await navigator.clipboard.writeText(ens);
                          }}
                        >
                          Copy
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="text-sm text-zinc-600">
                    No subnames found yet for this parent.
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

