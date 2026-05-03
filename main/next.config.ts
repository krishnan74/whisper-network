import type { NextConfig } from "next";
import path from "node:path";
import { fileURLToPath } from "node:url";

const mainDir = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  turbopack: {
    root: mainDir,
    // Git root is the parent of `main/`; Turbopack can still resolve CSS imports from there.
    // Pin packages to this app’s node_modules so `pnpm dev` works without a repo-root install.
    resolveAlias: {
      tailwindcss: path.join(mainDir, "node_modules/tailwindcss"),
      "@tailwindcss/postcss": path.join(
        mainDir,
        "node_modules/@tailwindcss/postcss",
      ),
    },
  },
};

export default nextConfig;
