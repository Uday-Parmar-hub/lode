import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // lean, self-contained server bundle for Azure App Service (.next/standalone/server.js)
  output: "standalone",
};

export default nextConfig;
