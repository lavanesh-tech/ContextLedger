import type { NextConfig } from "next";

// The browser only talks to this Next.js server. Requests to /api/v1/* are
// forwarded to the FastAPI backend, so the API needs no CORS configuration and
// its address is never baked into the client bundle.
const apiUrl = process.env.CONTEXTLEDGER_API_URL ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  reactStrictMode: true,
  // A self-contained server in .next/standalone for the container image.
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${apiUrl}/api/v1/:path*` }];
  },
};

export default config;
