import type { Metadata } from 'next';
import type { ReactNode } from 'react';

import Link from 'next/link';

// Self-hosted fonts (no CDN request at runtime, no network fetch at build).
import '@fontsource/fira-code/latin-400.css';
import '@fontsource/fira-code/latin-500.css';
import '@fontsource/fira-code/latin-600.css';
import '@fontsource/fira-code/latin-700.css';
import '@fontsource/fira-sans/latin-400.css';
import '@fontsource/fira-sans/latin-500.css';
import '@fontsource/fira-sans/latin-600.css';
import '@fontsource/fira-sans/latin-700.css';
import './globals.css';

import { Activity } from 'lucide-react';

import { AppShell } from '@/components/ui/app-shell';

export const metadata: Metadata = {
  title: 'Trading Platform — real-time monitor',
  description:
    'Real-time monitoring of every trading profile: health, equity curve, open positions, trades, orders and metrics.',
};

const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background';

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-background font-sans text-foreground">
        <a
          href="#content"
          className={`sr-only focus:not-sr-only focus:fixed focus:left-xl focus:top-xl focus:z-50 focus:rounded-button focus:border focus:border-border focus:bg-card focus:px-lg focus:py-sm focus:text-sm focus:text-foreground active:bg-muted-pressed ${FOCUS_RING}`}
        >
          Skip to content
        </a>
        <AppShell>
          <header className="flex flex-wrap items-center justify-between gap-md border-b border-border pb-lg">
            <div className="flex flex-wrap items-center gap-xl">
              <Link
                href="/"
                className={`inline-flex items-center gap-sm rounded-button font-mono text-base font-semibold text-foreground motion-safe:transition-colors motion-safe:duration-200 hover:text-accent active:bg-muted-pressed active:text-accent ${FOCUS_RING}`}
              >
                <Activity aria-hidden="true" className="size-4 text-accent" />
                <span>Trading Platform</span>
                <span className="font-normal text-muted-foreground">real-time monitor</span>
              </Link>
            </div>
            <p className="text-xs text-muted-foreground">Read-only by default</p>
          </header>
          <main id="content" className="flex-1">
            {children}
          </main>
          <footer className="border-t border-border pt-lg text-xs text-muted-foreground">
            HTTP polling every 2 seconds · read-only by default · a local-network monitoring surface
          </footer>
        </AppShell>
      </body>
    </html>
  );
}
