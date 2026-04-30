"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RainbowKitProvider, getDefaultConfig } from "@rainbow-me/rainbowkit";
import { WagmiProvider } from "wagmi";
import { sepolia } from "wagmi/chains";
import { ReactNode, useMemo, useState } from "react";
import { JustaNameProvider } from "@justaname.id/react";
import type { JustaNameProviderConfig } from "@justaname.id/react";

const CHAIN_ID = sepolia.id; // 11155111
const ENS_DOMAIN = "notdocker.eth";

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(() => new QueryClient());

  const config = useMemo(() => {
    const projectId =
      process.env.NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID ?? "YOUR_PROJECT_ID";

    // Rendered client-only (see ClientProviders), so no SSR needed here.
    return getDefaultConfig({
      appName: "ens-test",
      projectId,
      chains: [sepolia],
      ssr: false,
    });
  }, []);

  const justaNameConfig = useMemo<JustaNameProviderConfig>(() => {
    const origin = window.location.origin;
    const domain = window.location.hostname;

    const sepoliaProviderUrl =
      process.env.NEXT_PUBLIC_SEPOLIA_RPC_URL ??
      "https://ethereum-sepolia-rpc.publicnode.com";

    return {
      config: { origin, domain },
      networks: [{ chainId: CHAIN_ID, providerUrl: sepoliaProviderUrl }],
      ensDomains: [
        {
          ensDomain: ENS_DOMAIN,
          chainId: CHAIN_ID,
          apiKey: process.env.NEXT_PUBLIC_JUSTANAME_API_KEY ?? "",
        },
      ],
    };
  }, []);

  return (
    <WagmiProvider config={config}>
      <QueryClientProvider client={queryClient}>
        <RainbowKitProvider>
          <JustaNameProvider config={justaNameConfig}>{children}</JustaNameProvider>
        </RainbowKitProvider>
      </QueryClientProvider>
    </WagmiProvider>
  );
}

