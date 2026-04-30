"use client";

import { ReactNode, useSyncExternalStore } from "react";
import { Providers } from "./providers";

function useIsHydrated() {
  return useSyncExternalStore(
    () => () => {},
    () => true,
    () => false
  );
}

export function ClientProviders({ children }: { children: ReactNode }) {
  const hydrated = useIsHydrated();
  if (!hydrated) return null;
  return <Providers>{children}</Providers>;
}

