import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // Pin Turbopack's workspace root to this directory. Without this,
  // Turbopack walks upward looking for a lockfile and may pick the
  // wrong one in monorepo / parent-dir-with-lockfile setups.
  turbopack: {
    root: path.resolve(__dirname),
  },
};

export default nextConfig;
