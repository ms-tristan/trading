'use client';

import { ErrorBanner } from '@/components/ui/error-banner';
import { formatNumber, formatQuantity } from '@/lib/format';
import type { OrphanClosure, OrphanFailure, OrphanReport } from '@/lib/types';

/** Props of {@link OrphanWarning}. */
export interface OrphanWarningProps {
  /**
   * Report of the startup safety sweep, `null` when the platform was never
   * swept or when the dashboard could not read the orphan route.
   */
  report: OrphanReport | null;
}

/** Headline of a sweep that left at least one position open at the venue. */
export const ORPHAN_FAILURE_HEADLINE = 'Orphaned positions could not all be closed';

/** Headline of a sweep that flattened every orphaned position it found. */
export const ORPHAN_SUCCESS_HEADLINE = 'Orphaned positions were closed at the venue';

/** Render the quantity of one entry, em dash included, never a bare `null`. */
function describeQuantity(quantity: number | null): string {
  return formatQuantity(quantity);
}

/**
 * Render the price of one closure, em dash included.
 *
 * The shared formatter of the module is used rather than a local `toFixed`: an
 * absent price becomes the documented em dash placeholder, exactly like every
 * other number of this dashboard.
 */
function describePrice(price: number | null): string {
  return formatNumber(price);
}

/** `profile_id symbol quantity @ price (side)` of one closed position. */
function describeClosure(closure: OrphanClosure): string {
  return `${closure.profile_id} ${closure.symbol} ${describeQuantity(closure.quantity)} @ ${describePrice(
    closure.price,
  )} (${closure.side})`;
}

/** `profile_id symbol quantity — error` of one position that stayed open. */
function describeFailure(failure: OrphanFailure): string {
  return `${failure.profile_id} ${failure.symbol} ${describeQuantity(failure.quantity)} — ${failure.error}`;
}

/**
 * Operator-facing warning about the startup safety sweep.
 *
 * A position whose profile disappeared without going through the delete route
 * is durable SQLite state that nothing tracks anymore: no stop loss, no exit
 * management, no reconciliation. The engine sweeps those orphans at boot and
 * closes them at the venue, and this banner is how the operator learns about
 * it. It says the same thing a log line would, on the surface the operator
 * actually looks at.
 *
 * The banner follows the dashboard's existing conventions: it is the shared
 * {@link ErrorBanner} — the single non-blocking notice surface — and it
 * **never** relies on colour alone, because the copy always states what
 * happened. A failure is never softened and never hidden behind a success line:
 * when both halves are present it takes the failure headline, and the detail
 * lists the failures first. The same component renders both states, so they are
 * visually identical and only the copy distinguishes them.
 *
 * There is deliberately **no** dismiss control: an operator must not be able to
 * hide a position that is still open at the venue. A `null` report renders
 * nothing at all — the platform was never swept — and so does a report that
 * carries neither a closure nor a failure, because a sweep that found no orphan
 * is not an event worth a banner.
 */
export function OrphanWarning({ report }: OrphanWarningProps) {
  if (report === null) {
    return null;
  }

  const closedCount = report.closed_count;
  const failureCount = report.failed_count;

  // A sweep that found nothing to flatten has nothing to say: `swept_at` is
  // still set (the sweep did run), but a "were closed" banner with zero
  // positions would be a false alarm — there is no event for the operator.
  if (failureCount === 0 && closedCount === 0) {
    return null;
  }

  const hasFailures = failureCount > 0;

  // The failures half is the loud one: it comes first in the detail, and it
  // takes the headline. The successes are then appended so a clean sweep of the
  // other positions is still reported rather than swallowed.
  const detail: string[] = [];
  if (hasFailures) {
    detail.push(
      `${failureCount} position(s) COULD NOT be closed: ${report.failed
        .map(describeFailure)
        .join(', ')}`,
    );
  }
  if (closedCount > 0) {
    detail.push(
      `${closedCount} position(s) closed: ${report.closed.map(describeClosure).join(', ')}`,
    );
  }

  return (
    <ErrorBanner
      message={hasFailures ? ORPHAN_FAILURE_HEADLINE : ORPHAN_SUCCESS_HEADLINE}
      detail={detail.length === 0 ? null : detail.join(' · ')}
    />
  );
}
