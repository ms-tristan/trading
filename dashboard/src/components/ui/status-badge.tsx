import type { ReactNode } from 'react';

import { CircleCheck, CircleHelp, CircleX, Info, TriangleAlert } from 'lucide-react';

import { cn } from '@/lib/cn';
import type { OrderState, ProfileStatus } from '@/lib/types';

/** Semantic tone of a {@link StatusBadge}. */
export type StatusTone = 'ok' | 'warn' | 'error' | 'info' | 'neutral';

/** Props of {@link StatusBadge}. */
export interface StatusBadgeProps {
  /** Text label of the state. */
  label: string;
  /** Semantic tone driving the (never sole) colour signal. */
  tone: StatusTone;
  /** Decorative icon override; each tone has a sensible default. */
  icon?: ReactNode;
}

const TONE_CLASSES: Record<StatusTone, string> = {
  ok: 'border-profit/40 bg-profit/10 text-profit',
  warn: 'border-warn/40 bg-warn/10 text-warn',
  error: 'border-loss/40 bg-loss/10 text-loss',
  info: 'border-info/40 bg-info/10 text-info',
  neutral: 'border-border bg-muted text-muted-foreground',
};

const TONE_ICON_CLASSES = 'size-3.5';

/**
 * State pill of the dashboard.
 *
 * The tone is always paired with the text label and an icon: meaning never rests
 * on colour alone.
 */
export function StatusBadge({ label, tone, icon }: StatusBadgeProps) {
  return (
    <span
      data-tone={tone}
      className={cn(
        'inline-flex items-center gap-xs rounded-button border px-md py-xs text-xs font-medium',
        TONE_CLASSES[tone],
      )}
    >
      <span aria-hidden="true" className="inline-flex shrink-0 items-center">
        {icon ?? defaultToneIcon(tone)}
      </span>
      {label}
    </span>
  );
}

function defaultToneIcon(tone: StatusTone): ReactNode {
  switch (tone) {
    case 'ok':
      return <CircleCheck className={TONE_ICON_CLASSES} />;
    case 'warn':
      return <TriangleAlert className={TONE_ICON_CLASSES} />;
    case 'error':
      return <CircleX className={TONE_ICON_CLASSES} />;
    case 'info':
      return <Info className={TONE_ICON_CLASSES} />;
    default:
      return <CircleHelp className={TONE_ICON_CLASSES} />;
  }
}

/** Tone of a profile lifecycle status. */
export function profileStatusTone(status: ProfileStatus): StatusTone {
  switch (status) {
    case 'running':
      return 'ok';
    case 'starting':
      return 'info';
    case 'degraded':
      return 'warn';
    case 'halted':
    case 'error':
      return 'error';
    case 'stopped':
      return 'neutral';
    default:
      return 'neutral';
  }
}

/** Tone of a persisted order state. */
export function orderStateTone(state: OrderState): StatusTone {
  switch (state) {
    case 'filled':
      return 'ok';
    case 'submitted':
      return 'info';
    case 'partially_filled':
      return 'warn';
    case 'rejected':
      return 'error';
    case 'pending':
    case 'cancelled':
      return 'neutral';
    default:
      return 'neutral';
  }
}
