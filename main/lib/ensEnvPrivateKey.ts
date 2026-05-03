import { privateKeyToAccount } from "viem/accounts";
import type { PrivateKeyAccount } from "viem/accounts";

/**
 * Client-readable key for `/ens` (Next.js inlines `NEXT_PUBLIC_*` into the bundle).
 * Dev-only: never commit real funds’ keys; use a throwaway Sepolia account.
 */
export function getEnsTestPrivateKeyHex(): `0x${string}` | null {
  const raw = process.env.NEXT_PUBLIC_ENS_TEST_PRIVATE_KEY?.trim();
  if (!raw) return null;
  return (raw.startsWith("0x") ? raw : `0x${raw}`) as `0x${string}`;
}

export function getEnsTestPrivateKeyAccount(): PrivateKeyAccount | null {
  const pk = getEnsTestPrivateKeyHex();
  if (!pk) return null;
  try {
    return privateKeyToAccount(pk);
  } catch {
    return null;
  }
}
