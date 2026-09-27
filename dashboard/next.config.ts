import type { NextConfig } from "next";

/**
 * Origin of the Python monitoring API.
 *
 * Resolved when this configuration is evaluated, so `API_ORIGIN` is baked into
 * the built server: `deploy/Dockerfile.dashboard` sets it to
 * `http://trading-realtime:8080` at image build time.
 */
const apiOrigin = process.env.API_ORIGIN ?? "http://127.0.0.1:8080";

const nextConfig: NextConfig = {
  // Standalone output: the dashboard image copies `.next/standalone` and runs
  // `node server.js`, so no dependency has to be installed in the runtime image.
  output: "standalone",
  reactStrictMode: true,

  /**
   * Proxy every `/api/*` call to the Python API.
   *
   * The browser therefore always talks to its own origin: no CORS preflight is
   * ever needed, and the `X-Operator-Token` header of the mutating calls flows
   * through the rewrite untouched.
   */
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
