import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';

/** Props of {@link AppShell}. */
export interface AppShellProps {
  children: ReactNode;
  className?: string;
}

/**
 * Page frame of the dashboard: the OLED background, the foreground colour and
 * the single centred content column shared by every route. The layout renders
 * the header, the `<main>` landmark and the footer inside it.
 */
export function AppShell({ children, className }: AppShellProps) {
  return (
    <div className={cn('flex min-h-dvh w-full flex-col bg-background text-foreground', className)}>
      <div className="mx-auto flex w-full max-w-[1440px] flex-1 flex-col gap-2xl px-xl py-2xl sm:px-2xl">
        {children}
      </div>
    </div>
  );
}
