"use client";

import { ConnectButton } from "@rainbow-me/rainbowkit";
import { OnchainEnsFlow } from "./OnchainEnsFlow";

export default function Home() {
  return (
    <div className="min-h-screen bg-white text-zinc-900">
      <header className="mx-auto flex w-full max-w-4xl items-center justify-between px-6 py-10">
        <div className="flex flex-col">
          <div className="text-base font-semibold tracking-tight">axl.eth</div>
          <div className="mt-1 text-sm text-zinc-500">
            Create subnames and nested subnames on Sepolia.
          </div>
        </div>
        <div className="flex items-center gap-3">
          <ConnectButton />
        </div>
      </header>

      <main className="mx-auto flex w-full max-w-4xl px-6 pb-16">
        <OnchainEnsFlow />
      </main>
    </div>
  );
}
