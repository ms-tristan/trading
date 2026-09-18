import type { NextConfig } from 'next';

/**
 * Default origin of the Python monitoring server (its own documented default
 * host/port). Kept in sync with `DEFAULT_API_ORIGIN` in `src/lib/config.ts`:
 * application code cannot import this file without dragging the Next build
 * configuration into the runtime bundle.
 */
export const DEFAULT_API_ORIGIN = 'http://127.0.0.1:8080';

/**
 * Resolve the origin of the Python monitoring server.
 *
 * `API_ORIGIN` wins when it carries a value; surrounding whitespace and trailing
 * slashes are removed so the rewrite destination is always well formed. A blank
 * value falls back to {@link DEFAULT_API_ORIGIN}.
 */
export function apiOrigin(env: NodeJS.ProcessEnv = process.env): string {
  const raw = env.API_ORIGIN;
  if (typeof raw !== 'string') {
    return DEFAULT_API_ORIGIN;
  }
  const trimmed = raw.trim().replace(/\/+$/, '');
  return trimmed === '' ? DEFAULT_API_ORIGIN : trimmed;
}

const nextConfig: NextConfig = {
  // Standalone output: the container image ships only the server bundle and the
  // production dependencies it actually needs.
  output: 'standalone',
  reactStrictMode: true,
  async rewrites() {
    // Resolved once, when this configuration is evaluated: the API origin is
    // baked into the built server (see dashboard/README.md).
    return [{ source: '/api/:path*', destination: `${apiOrigin()}/api/:path*` }];
  },
};

export default nextConfig;
