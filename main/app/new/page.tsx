import Link from "next/link";
import { OnchainEnsFlow } from "../OnchainEnsFlow";

export default function NewPage() {
  return (
    <div className="min-h-screen bg-white text-zinc-900">
      <header className="mx-auto flex w-full max-w-4xl items-center justify-between px-6 py-10">
        <Link href="/" className="text-sm font-medium text-zinc-700 hover:text-zinc-900">
          ← Home
        </Link>
        <div className="text-xs text-zinc-500">axl.eth · Sepolia</div>
      </header>
      <main className="mx-auto w-full max-w-4xl px-6 pb-16">
        <OnchainEnsFlow />
      </main>
    </div>
  );
}
