import type { Metadata } from "next";
import { Inter } from "next/font/google";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";
import { AutoRefresh } from "@/components/ui/AutoRefresh";
import { TokenPrompt } from "@/components/ui/TokenPrompt";
import { fetchSettings } from "@/lib/api";
import { DEFAULT_SETTINGS } from "@/lib/types";

/** The dashboard is live data: it is rendered per request, never prerendered. */
export const dynamic = "force-dynamic";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

/**
 * The file-based metadata of the App Router (`icon.svg`, `favicon.ico` and
 * `apple-icon.png`, all co-located in this directory) emits the `<link>` tags
 * on its own: this object deliberately declares no `icons` entry.
 */
export const metadata: Metadata = {
  title: {
    default: "Trading platform",
    template: "%s - Trading platform",
  },
  description: "Multi-profile paper and live trading monitor.",
  applicationName: "Trading platform",
};

const NAV_LINKS = [
  { href: "/", label: "Overview" },
  { href: "/strategies", label: "Strategies" },
  { href: "/operations", label: "Operations" },
] as const;

/**
 * Application shell: 56px header, 240px sidebar, dark theme, Inter.
 *
 * The refresh interval comes from `GET /api/settings` through the mapped
 * fetchers of `@/lib/api`, so an unreachable API degrades to the documented 15 s
 * default instead of breaking the whole shell.
 */
export default async function RootLayout({ children }: { children: ReactNode }) {
  const { data: settings } = await fetchSettings(DEFAULT_SETTINGS);

  return (
    <html lang="en" className={inter.variable}>
      {/* Next.js 16 metadata exports no themeColor: the dark theme colour is rendered here. */}
      <meta name="theme-color" content="#020617" />
      <body className="min-h-screen bg-background text-foreground">
        <header className="sticky top-0 z-10 flex h-14 items-center gap-3 border-b border-border bg-background px-3">
          <p className="text-base font-semibold">Trading platform</p>
          <nav
            aria-label="Main navigation (small screens)"
            className="flex min-w-0 flex-1 gap-2 overflow-x-auto md:hidden"
          >
            {NAV_LINKS.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className="shrink-0 rounded-md border border-border px-2 py-1 text-sm text-muted-foreground transition-smooth hover:text-foreground"
              >
                {link.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-3">
            <AutoRefresh intervalSeconds={settings.refresh_interval_seconds} />
          </div>
        </header>

        <div className="flex min-h-[calc(100vh-56px)]">
          <aside className="hidden w-60 shrink-0 border-r border-border p-3 md:block">
            <nav aria-label="Main navigation" className="flex flex-col gap-1">
              {NAV_LINKS.map((link) => (
                <Link
                  key={link.href}
                  href={link.href}
                  className="rounded-md border border-transparent px-2 py-1 text-base text-muted-foreground transition-smooth hover:border-border hover:text-foreground"
                >
                  {link.label}
                </Link>
              ))}
            </nav>
            <TokenPrompt className="mt-3" />
          </aside>

          <main className="min-w-0 flex-1 p-3">{children}</main>
        </div>
      </body>
    </html>
  );
}
