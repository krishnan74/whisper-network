import Link from "next/link";
import { OnchainEnsFlow } from "../OnchainEnsFlow";

/** Same flow as `/new`, but all transactions sign with `NEXT_PUBLIC_ENS_TEST_PRIVATE_KEY` in `.env.local`. */
export default function EnsPrivateKeyPage() {
  return (
    <div className="min-h-screen bg-white text-zinc-900">
      <header className="mx-auto flex w-full max-w-4xl items-center justify-between px-6 py-10">
        <Link href="/" className="text-sm font-medium text-zinc-700 hover:text-zinc-900">
          ← Home
        </Link>
        <div className="flex items-center gap-3 text-xs text-zinc-500">
          <Link href="/new" className="font-medium text-emerald-700 hover:text-emerald-900">
            Wallet UI →
          </Link>
          <span>axl.eth · Sepolia · env key</span>
        </div>
      </header>
      <main className="mx-auto w-full max-w-4xl px-6 pb-16">
        <OnchainEnsFlow variant="envPrivateKey" />
      </main>
    </div>
  );
}
