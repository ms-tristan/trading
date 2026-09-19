import Link from 'next/link';

import { Compass } from 'lucide-react';

import { EmptyState } from '@/components/ui/empty-state';

/**
 * 404 page of the dashboard.
 *
 * The monitoring API answers an unknown route with a JSON 404, so this page only
 * ever renders when a dashboard route itself does not exist.
 */
export default function NotFound() {
  return (
    <div className="flex flex-col gap-xl">
      <h1 className="font-mono text-xl font-semibold text-foreground">Page not found</h1>
      <EmptyState
        icon={<Compass className="size-5" />}
        title="This page does not exist"
        description="The dashboard has an overview page listing every profile, and one detail page per profile."
      />
      <p>
        <Link
          href="/"
          className="inline-flex cursor-pointer items-center gap-sm rounded-button border border-border bg-secondary px-lg py-md text-sm font-medium text-on-secondary motion-safe:transition-colors motion-safe:duration-200 hover:bg-secondary/80 active:bg-secondary-pressed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          Back to the overview
        </Link>
      </p>
    </div>
  );
}
