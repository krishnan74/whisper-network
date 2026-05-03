import { addEnsContracts } from "@ensdomains/ensjs";
import { createSubname } from "@ensdomains/ensjs/wallet";
import type { Hash, PublicClient, WalletClient } from "viem";
import { sepolia } from "viem/chains";
import {
  GaslessEip7702UnsupportedError,
  signSimple7702DelegationAuthorization,
  type SponsoredSmartAccountClient,
} from "@/lib/sponsoredEnsWallet";

const ensChain = addEnsContracts(sepolia);

/**
 * ENSjs `createSubname` calls viem's wallet `sendTransaction` action, which rejects smart accounts.
 * For paymaster flows we encode with the EOA client (ChainWithEns) and execute via the smart account's
 * `sendTransaction` (UserOperation under the hood).
 *
 * Permissionless's smart-account `sendTransaction` waits for inclusion and returns the **bundle tx hash**.
 */
export async function sendRegistrySubnameCreate(params: {
  ensEncodeWallet: WalletClient;
  sponsored: SponsoredSmartAccountClient | null;
  /** Required for the first gasless tx: real EIP-7702 delegation auth (viem otherwise sends stub r/s). */
  sponsoredPublicClient?: PublicClient | null;
  name: string;
  owner: `0x${string}`;
}): Promise<{ hash: Hash; mode: "gasless" | "eoa" }> {
  const tx = createSubname.makeFunctionData(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    params.ensEncodeWallet as any,
    {
      name: params.name,
      owner: params.owner,
      contract: "registry",
    }
  );

  if (params.sponsored) {
    const { account } = params.sponsored;
    if (!account) throw new Error("Smart account not ready.");
    let authorization: Awaited<
      ReturnType<typeof signSimple7702DelegationAuthorization>
    > | undefined;
    const deployed = await account.isDeployed();
    if (!deployed) {
      if (!params.sponsoredPublicClient) {
        throw new Error(
          "Internal error: gasless client missing execution RPC — cannot prepare EIP-7702 delegation."
        );
      }
      try {
        authorization = await signSimple7702DelegationAuthorization({
          walletClient: params.ensEncodeWallet,
          publicClient: params.sponsoredPublicClient,
        });
      } catch (e) {
        if (e instanceof GaslessEip7702UnsupportedError) {
          const hash = await createSubname(
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            params.ensEncodeWallet as any,
            {
              name: params.name,
              owner: params.owner,
              contract: "registry",
            } as never
          );
          return { hash: hash as Hash, mode: "eoa" };
        }
        throw e;
      }
    }
    const hash = await params.sponsored.sendTransaction({
      account,
      chain: ensChain,
      to: tx.to,
      data: tx.data,
      ...(authorization ? { authorization } : {}),
    });
    return { hash: hash as Hash, mode: "gasless" };
  }

  const hash = await createSubname(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    params.ensEncodeWallet as any,
    {
      name: params.name,
      owner: params.owner,
      contract: "registry",
    } as never
  );
  return { hash: hash as Hash, mode: "eoa" };
}
