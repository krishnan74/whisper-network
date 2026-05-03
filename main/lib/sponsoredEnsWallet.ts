import { addEnsContracts } from "@ensdomains/ensjs";
import { createSmartAccountClient } from "permissionless";
import { createPimlicoClient } from "permissionless/clients/pimlico";
import { toAccount } from "viem/accounts";
import { prepareAuthorization } from "viem/actions";
import {
  createPublicClient,
  getAddress,
  http,
  numberToHex,
  parseSignature,
  type Address,
  type AuthorizationRequest,
  type Hex,
  type LocalAccount,
  type PublicClient,
  type SignedAuthorization,
  type WalletClient,
} from "viem";
import { sepolia } from "viem/chains";
import { entryPoint08Address, toSimple7702SmartAccount } from "viem/account-abstraction";

export class GaslessEip7702UnsupportedError extends Error {
  override readonly name = "GaslessEip7702UnsupportedError";
  constructor(
    message = "This wallet cannot sign EIP-7702 delegation (needed once for sponsored txs)."
  ) {
    super(message);
  }
}

/** Best-effort: some wallets expose a non-standard RPC for EIP-7702 auth; viem’s Json-RPC account does not. */
async function tryInjectedProviderSign7702(
  wallet: WalletClient,
  authorization: AuthorizationRequest
): Promise<SignedAuthorization | null> {
  const contractAddress = getAddress(
    "contractAddress" in authorization
      ? (authorization as { contractAddress: Address }).contractAddress
      : authorization.address
  );
  const chainId =
    typeof authorization.chainId === "bigint"
      ? authorization.chainId
      : BigInt(authorization.chainId);
  const nonce =
    typeof authorization.nonce === "bigint"
      ? authorization.nonce
      : BigInt(authorization.nonce);

  const request = wallet.request;
  if (!request) return null;

  const from = wallet.account?.address;
  const payload = {
    from,
    address: from,
    contractAddress,
    chainId: numberToHex(chainId),
    nonce: numberToHex(nonce),
  };

  const attempts: { method: string; params: unknown }[] = [
    { method: "eth_sign7702Authorization", params: [from, payload] },
    { method: "eth_sign7702Authorization", params: [payload] },
    { method: "wallet_signAuthorization", params: [payload] },
    { method: "wallet_signEIP7702Authorization", params: [payload] },
  ];

  for (const { method, params } of attempts) {
    try {
      const result = await (request as (a: {
        method: string;
        params?: unknown;
      }) => Promise<unknown>)({
        method,
        params,
      });
      if (result === null || result === undefined) continue;

      if (typeof result === "string") {
        const sig = parseSignature(result as Hex);
        const yParity =
          sig.yParity ??
          (sig.v === BigInt(28)
            ? 1
            : sig.v === BigInt(27)
              ? 0
              : undefined);
        if (yParity !== 0 && yParity !== 1) continue;
        return {
          address: contractAddress,
          chainId: Number(chainId),
          nonce: Number(nonce),
          r: sig.r,
          s: sig.s,
          yParity,
        } as SignedAuthorization;
      }

      if (typeof result === "object" && result !== null && "r" in result && "s" in result) {
        const o = result as Record<string, unknown>;
        const r = o.r as Hex;
        const s = o.s as Hex;
        let yParity = 0 as 0 | 1;
        if (typeof o.yParity === "number") yParity = o.yParity ? 1 : 0;
        else if (typeof o.yParity === "bigint")
          yParity = o.yParity === BigInt(1) ? 1 : 0;
        else if (typeof o.v === "bigint") yParity = o.v === BigInt(28) ? 1 : 0;
        else if (typeof o.v === "number") yParity = o.v === 28 ? 1 : 0;
        return {
          address: contractAddress,
          chainId: Number(chainId),
          nonce: Number(nonce),
          r,
          s,
          yParity,
        } as SignedAuthorization;
      }
    } catch {
      /* try next method */
    }
  }
  return null;
}

export function isSponsoredTxConfigured(): boolean {
  return Boolean(process.env.NEXT_PUBLIC_PIMLICO_API_KEY?.trim());
}

/** Default Simple7702 implementation bundled with `toSimple7702SmartAccount` for EntryPoint 0.8 (Sepolia). */
export const SIMPLE_7702_IMPLEMENTATION_EP08 = getAddress(
  "0xe6Cae83BdE06E4c305530e199D7217f42808555B"
);

/**
 * `toSimple7702SmartAccount` reads `owner.address`, but a viem WalletClient only exposes the
 * connected address on `wallet.account.address`. Bridge so sender / EIP-7702 checks get a real hex address.
 *
 * EIP-7702 delegation requires `signAuthorization`. Browser JSON-RPC accounts do not implement it; local
 * accounts (e.g. `privateKeyToAccount`) do. Viem’s UserOp flow otherwise uses placeholder `r/s` for gas
 * estimate only — bundlers reject that on submit unless we pass a real signed `authorization` (Pimlico guide).
 */
function walletClientTo7702Owner(wallet: WalletClient) {
  const raw = wallet.account?.address;
  if (!raw) throw new Error("Wallet has no connected account.");
  const acct = wallet.account;
  return toAccount({
    address: getAddress(raw),
    sign: () => {
      throw new Error("EOA owner should not raw-sign hashes for the 7702 smart account path.");
    },
    signMessage: (args) => {
      if (!wallet.account) throw new Error("Wallet has no connected account.");
      return wallet.signMessage({ ...args, account: wallet.account });
    },
    signTypedData: (args) => {
      if (!wallet.account) throw new Error("Wallet has no connected account.");
      // viem’s overloads need `account` explicit here; spread keeps TypedData inference from the smart account.
      return wallet.signTypedData({ ...args, account: wallet.account } as Parameters<
        WalletClient["signTypedData"]
      >[0]);
    },
    signTransaction: () => {
      throw new Error("EOA owner should not sign transactions for the 7702 smart account path.");
    },
    signAuthorization: async (
      authorization: AuthorizationRequest
    ): Promise<SignedAuthorization> => {
      const injected = await tryInjectedProviderSign7702(wallet, authorization);
      if (injected) return injected;

      if (
        acct &&
        typeof acct === "object" &&
        "signAuthorization" in acct &&
        typeof (acct as LocalAccount).signAuthorization === "function"
      ) {
        return (acct as LocalAccount).signAuthorization!(authorization);
      }
      throw new GaslessEip7702UnsupportedError(
        "Gasless EIP-7702 needs a one-time delegation signature. This browser wallet did not sign it " +
          "(no compatible RPC). We will fall back to a normal transaction so you pay Sepolia gas, or turn off “Paymaster gas” yourself."
      );
    },
  });
}

/** Prepare + sign delegation to the Simple7702 implementation used by `toSimple7702SmartAccount` (EP 0.8). */
export async function signSimple7702DelegationAuthorization(params: {
  walletClient: WalletClient;
  publicClient: PublicClient;
}): Promise<SignedAuthorization> {
  const owner = walletClientTo7702Owner(params.walletClient);
  const unsigned = await prepareAuthorization(params.publicClient, {
    account: owner,
    address: SIMPLE_7702_IMPLEMENTATION_EP08,
  });
  if (!owner.signAuthorization) {
    throw new Error("Local account is missing signAuthorization (internal error).");
  }
  return owner.signAuthorization(unsigned);
}

function pimlicoRpcUrl(): string {
  const key = process.env.NEXT_PUBLIC_PIMLICO_API_KEY?.trim();
  if (!key) throw new Error("NEXT_PUBLIC_PIMLICO_API_KEY is not set.");
  return `https://api.pimlico.io/v2/sepolia/rpc?apikey=${key}`;
}

/**
 * ERC-4337 + EIP-7702 Simple Account: execution address matches the connected EOA,
 * so ENS parent ownership checks still pass. Gas is sponsored via Pimlico paymaster.
 */
export async function createSponsoredEnsWriteClient(params: {
  walletClient: WalletClient;
  executionRpcUrl: string;
}): Promise<{
  writeClient: WalletClient;
  smartAccountClient: ReturnType<typeof createSmartAccountClient>;
  publicClient: PublicClient;
}> {
  if (!params.walletClient.account) {
    throw new Error("Wallet is not connected.");
  }
  const chain = addEnsContracts(sepolia);
  const transport = http(params.executionRpcUrl);
  const publicClient = createPublicClient({ chain, transport });

  /** Viem’s Simple7702 wires EIP-7702 auth signing to the wallet correctly; permissionless’ to7702 helper wraps owners without signAuthorization and breaks bundler validation. */
  const account = await toSimple7702SmartAccount({
    client: publicClient,
    // `toAccount` owner is a full LocalAccount; types still overlap permissionless' owner union.
    owner: walletClientTo7702Owner(params.walletClient) as never,
  });

  const pimlico = createPimlicoClient({
    chain,
    transport: http(pimlicoRpcUrl()),
    entryPoint: { address: entryPoint08Address, version: "0.8" },
  });

  const smartAccountClient = createSmartAccountClient({
    account,
    chain,
    client: publicClient,
    bundlerTransport: http(pimlicoRpcUrl()),
    paymaster: pimlico,
    userOperation: {
      estimateFeesPerGas: async () => {
        const g = await pimlico.getUserOperationGasPrice();
        return g.fast;
      },
    },
  });

  return {
    writeClient: smartAccountClient as unknown as WalletClient,
    smartAccountClient,
    publicClient,
  };
}

export type SponsoredSmartAccountClient = ReturnType<typeof createSmartAccountClient>;
