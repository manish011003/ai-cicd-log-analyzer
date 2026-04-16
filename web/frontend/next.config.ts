import type { NextConfig } from "next";

const backendBase = process.env.NEXT_PUBLIC_API_TARGET ?? "http://127.0.0.1:8095";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${backendBase}/api/:path*` },
      { source: "/agent/:path*", destination: `${backendBase}/agent/:path*` },
      { source: "/health", destination: `${backendBase}/health` },
    ];
  },
};

export default nextConfig;
