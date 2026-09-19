import type { ReactNode } from 'react';

import {
  Ban,
  Banknote,
  CircleCheck,
  CirclePause,
  CircleX,
  FlaskConical,
  LoaderCircle,
  Minus,
  TrendingDown,
  TrendingUp,
  TriangleAlert,
} from 'lucide-react';
import Link from 'next/link';

import { ProfileActions } from '@/components/profile/profile-actions';
import { StatusBadge, profileStatusTone, type StatusTone } from '@/components/ui/status-badge';
import { cn } from '@/lib/cn';
import {
  EMPTY_PLACEHOLDER,
  formatDuration,
  formatInteger,
  formatMoney,
  formatRatioAsPercent,
  formatSignedMoney,
  formatTimestamp,
  isFiniteNumber,
  trendLabel,
  trendOf,
} from '@/lib/format';
import type { ProfileSnapshot, ProfileStatus, RunMode } from '@/lib/types';

/** Props of {@link ProfileCard}. */
export interface ProfileCardProps {
  /** One profile as `GET /api/profiles` emits it. */
  profile: ProfileSnapshot;
  className?: string;
}

const BADGE_ICON_CLASSES = 'size-3.5';

const MODE_BADGES: Record<RunMode, { label: string; tone: StatusTone; icon: ReactNode }> = {
  paper: {
    label: 'Paper',
    tone: 'info',
    icon: <FlaskConical className={BADGE_ICON_CLASSES} />,
  },
  live: {
    label: 'Live',
    tone: 'warn',
    icon: <Banknote className={BADGE_ICON_CLASSES} />,
  },
};

const STATUS_BADGES: Record<ProfileStatus, { label: string; icon: ReactNode; tone: StatusTone }> = {
  starting: { label: 'Starting', tone: 'info', icon: <LoaderCircle className={BADGE_ICON_CLASSES} /> },
  running: { label: 'Running', tone: 'ok', icon: <CircleCheck className={BADGE_ICON_CLASSES} /> },
  degraded: {
    label: 'Degraded',
    tone: 'warn',
    icon: <TriangleAlert className={BADGE_ICON_CLASSES} />,
  },
  halted: { label: 'Halted', tone: 'error', icon: <CirclePause className={BADGE_ICON_CLASSES} /> },
  stopped: { label: 'Stopped', tone: 'neutral', icon: <Ban className={BADGE_ICON_CLASSES} /> },
  error: { label: 'Error', tone: 'error', icon: <CircleX className={BADGE_ICON_CLASSES} /> },
};

const TREND_TEXT_CLASSES: Record<'up' | 'down' | 'flat', string> = {
  up: 'text-profit',
  down: 'text-loss',
  flat: 'text-muted-foreground',
};

const TREND_ICONS: Record<'up' | 'down' | 'flat', ReactNode> = {
  up: <TrendingUp className={BADGE_ICON_CLASSES} />,
  down: <TrendingDown className={BADGE_ICON_CLASSES} />,
  flat: <Minus className={BADGE_ICON_CLASSES} />,
};

/** Render `value`, or the em dash placeholder when it is absent or blank. */
function textOrPlaceholder(value: string | null | undefined): string {
  const trimmed = (value ?? '').trim();
  return trimmed === '' ? EMPTY_PLACEHOLDER : trimmed;
}

/** Deterministic, hydration-safe DOM id of the card heading. */
function headingId(profileId: string): string {
  return `profile-${profileId.replace(/[^a-zA-Z0-9_-]+/g, '-')}`;
}

interface FieldProps {
  /** Visible label of the value (never optional: every value is labelled). */
  label: string;
  /** Already formatted value; blank or absent renders an em dash. */
  value?: string;
  /** Rich value (a trend indicator, a link, ...); wins over `value`. */
  children?: ReactNode;
}

/** One labelled value of the card's definition grid. */
function Field({ label, value, children }: FieldProps) {
  const text = value ?? EMPTY_PLACEHOLDER;
  return (
    <div className="rounded-button border border-border/60 bg-muted/30 px-lg py-md">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-xs break-words font-mono text-sm tabular-nums text-foreground">
        {children ?? (text.trim() === '' ? EMPTY_PLACEHOLDER : text)}
      </dd>
    </div>
  );
}

interface TrendValueProps {
  /** Already formatted value, rendered next to its trend claim. */
  text: string;
  /** Raw value the trend is read from. */
  value: number | null | undefined;
}

/**
 * A signed value with its direction.
 *
 * The tone is never the only signal: the direction is carried by the colour,
 * by an icon and by the explicit {@link trendLabel} text together.
 */
function TrendValue({ text, value }: TrendValueProps) {
  const trend = trendOf(value);
  return (
    <span className="inline-flex flex-wrap items-center gap-xs">
      <span aria-hidden="true" className={TREND_TEXT_CLASSES[trend]}>
        {TREND_ICONS[trend]}
      </span>
      <span className={TREND_TEXT_CLASSES[trend]}>{text}</span>
      <span className="text-muted-foreground">{trendLabel(value)}</span>
    </span>
  );
}

/**
 * One profile of the live overview.
 *
 * The card is labelled by its heading, whose link goes to the per-profile detail
 * route. That link is **stretched** over the whole card, so a click anywhere on
 * it opens the profile — clicking only the heading text was too small a target
 * and left the rest of the card inert. The heading itself stays free of nested
 * interactive content: the lifecycle island lives in the footer, which is lifted
 * above the stretched overlay so its buttons stay clickable, because a link may
 * never wrap a button. The footer renders the shared lifecycle island (pause,
 * resume, delete) of the profile, exactly like the detail header, so both entry
 * points offer the same controls.
 *
 * Money is **attributed**: every profile funds its orders from the one shared
 * platform wallet, so `equity` and `cash` are labelled "Attributed ..." — they
 * are this profile's share of the shared ledger, never a pot of its own. The
 * attributed breakdown (allocation, deployed capital, realized and unrealized
 * P&L) and the reason of the last refused order are rendered beside them.
 * Every value carries an explicit label, every state pairs its tone with a text
 * label and an icon, and every absent value renders the em dash placeholder —
 * never `NaN`, `undefined` or an empty cell.
 */
export function ProfileCard({ profile, className }: ProfileCardProps) {
  const mode = MODE_BADGES[profile.mode] ?? {
    label: textOrPlaceholder(profile.mode),
    tone: 'neutral' as StatusTone,
    icon: <FlaskConical className={BADGE_ICON_CLASSES} />,
  };
  const status = STATUS_BADGES[profile.status] ?? {
    label: textOrPlaceholder(profile.status),
    tone: profileStatusTone(profile.status),
    icon: <CircleX className={BADGE_ICON_CLASSES} />,
  };
  const hasReturn = isFiniteNumber(profile.total_return);
  const hasRealized = isFiniteNumber(profile.realized_pnl);
  const hasUnrealized = isFiniteNumber(profile.unrealized_pnl);
  const lastError = profile.health.last_error;
  const lastBlockReason = profile.last_block_reason;

  return (
    <article
      aria-labelledby={headingId(profile.profile_id)}
      className={cn(
        'group relative flex flex-col rounded-card border border-border bg-card p-xl text-card-foreground shadow-md',
        'hover:border-accent/50 focus-within:border-accent/50',
        // Press feedback of the stretched link: the card lights up only while
        // the overlay itself is pressed. A ring is a box-shadow, so nothing
        // reflows — the design system forbids a pressed state that shifts layout.
        'has-[a:active]:border-accent has-[a:active]:ring-1 has-[a:active]:ring-accent',
        'motion-safe:transition-colors motion-safe:duration-200',
        className,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-md">
        <div className="min-w-0">
          <h3
            id={headingId(profile.profile_id)}
            className="font-mono text-base font-semibold text-card-foreground"
          >
            <Link
              href={`/profiles/${encodeURIComponent(profile.profile_id)}`}
              className={cn(
                'rounded-button underline decoration-border underline-offset-4',
                'hover:text-accent hover:decoration-accent',
                'active:text-accent active:decoration-accent',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
                'motion-safe:transition-colors motion-safe:duration-200',
                // Stretched link: the pseudo-element covers the whole card, so a
                // click anywhere on it opens the profile instead of only the
                // heading text. The card's own controls are lifted above it (see
                // the footer), because a link may never wrap a button.
                'after:absolute after:inset-0 after:content-[""]',
              )}
            >
              {textOrPlaceholder(profile.profile_id)}
            </Link>
          </h3>
        </div>
        <div className="flex flex-wrap items-center gap-sm">
          <StatusBadge label={mode.label} tone={mode.tone} icon={mode.icon} />
          <StatusBadge label={status.label} tone={status.tone} icon={status.icon} />
        </div>
      </header>

      <dl className="mt-lg grid gap-md sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <Field label="Profile id" value={textOrPlaceholder(profile.profile_id)} />
        <Field label="Symbol" value={textOrPlaceholder(profile.symbol)} />
        <Field label="Timeframe" value={textOrPlaceholder(profile.timeframe)} />
        <Field label="Strategy" value={textOrPlaceholder(profile.strategy)} />
        <Field label="Attributed equity" value={formatMoney(profile.equity)} />
        <Field label="Attributed cash" value={formatMoney(profile.cash)} />
        <Field label="Allocation" value={formatMoney(profile.allocation)} />
        <Field label="Deployed" value={formatMoney(profile.deployed)} />
        <Field label="Position value" value={formatMoney(profile.position_value)} />
        <Field label="Initial balance" value={formatMoney(profile.initial_balance)} />
        <Field label="Total return" value={formatRatioAsPercent(profile.total_return, { signed: true })}>
          {hasReturn ? (
            <TrendValue
              text={formatRatioAsPercent(profile.total_return, { signed: true })}
              value={profile.total_return}
            />
          ) : undefined}
        </Field>
        <Field label="Realized P&L" value={formatSignedMoney(profile.realized_pnl)}>
          {hasRealized ? (
            <TrendValue
              text={formatSignedMoney(profile.realized_pnl)}
              value={profile.realized_pnl}
            />
          ) : undefined}
        </Field>
        <Field label="Unrealized P&L" value={formatSignedMoney(profile.unrealized_pnl)}>
          {hasUnrealized ? (
            <TrendValue
              text={formatSignedMoney(profile.unrealized_pnl)}
              value={profile.unrealized_pnl}
            />
          ) : undefined}
        </Field>
        <Field label="Last blocked order" value={textOrPlaceholder(lastBlockReason)} />
        <Field label="Trades" value={formatInteger(profile.n_trades)} />
        <Field label="Open positions" value={formatInteger(profile.open_positions)} />
        <Field label="Last candle" value={formatTimestamp(profile.health.last_candle_at)} />
        <Field label="Candle lag" value={formatDuration(profile.health.lag_seconds)} />
        <Field label="Started at" value={formatTimestamp(profile.started_at)} />
        <Field label="Updated at" value={formatTimestamp(profile.updated_at)} />
      </dl>

      {lastError !== null ? (
        <p
          role="status"
          className="mt-lg flex flex-wrap items-center gap-sm rounded-button border border-warn/40 bg-warn/10 px-lg py-md text-sm text-foreground"
        >
          <TriangleAlert aria-hidden="true" className="size-4 shrink-0 text-warn" />
          <span className="font-medium">Last error</span>
          <span className="min-w-0 break-words">{textOrPlaceholder(lastError)}</span>
        </p>
      ) : null}

      <footer className="relative z-10 mt-lg border-t border-border/60">
        <ProfileActions profileId={profile.profile_id} className="mt-lg" />
      </footer>
    </article>
  );
}
