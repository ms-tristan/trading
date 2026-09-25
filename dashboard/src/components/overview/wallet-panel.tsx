'use client';

import { useState, type ReactNode } from 'react';

import {
  ArrowUpRight,
  Banknote,
  ChartLine,
  ChevronDown,
  CircleHelp,
  Coins,
  Database,
  FlaskConical,
  Gauge,
  Landmark,
  Scale,
  TrendingUp,
  Users,
  Wallet,
} from 'lucide-react';

import { DataTable } from '@/components/ui/data-table';
import { StatTile, type StatTrend } from '@/components/ui/stat-tile';
import { StatusBadge, type StatusTone } from '@/components/ui/status-badge';
import { cn } from '@/lib/cn';
import {
  EMPTY_PLACEHOLDER,
  formatInteger,
  formatMoney,
  formatSignedMoney,
  formatTimestamp,
  isFiniteNumber,
  trendOf,
} from '@/lib/format';
import type { RunMode, WalletSnapshot } from '@/lib/types';

/** Props of {@link WalletPanel}. */
export interface WalletPanelProps {
  /** The ledger of the selected mode, `null` when that mode holds no ledger row yet. */
  wallet: WalletSnapshot | null;
  /** Which mode the figures belong to (drives the heading and the badge). */
  mode: RunMode;
  className?: string;
}

const HEADING_ID = 'platform-wallet-heading';

const BADGE_ICON_CLASSES = 'size-3.5';

const TILE_ICON_CLASSES = 'size-4';

/**
 * The mode the panel states in words, in its heading and in its badge.
 *
 * Both entries reuse the icon vocabulary the rest of the dashboard already uses
 * for a run mode (`FlaskConical` for the simulated ledger, `Banknote` for the
 * real one), so one mode never reads as two different things on one page.
 */
const MODE_LABELS: Record<RunMode, { label: string; tone: StatusTone; icon: ReactNode }> = {
  paper: {
    label: 'Paper mode',
    tone: 'info',
    icon: <FlaskConical className={BADGE_ICON_CLASSES} />,
  },
  live: {
    label: 'Real mode',
    tone: 'warn',
    icon: <Banknote className={BADGE_ICON_CLASSES} />,
  },
};

/** How the source of the shared cash reads, with its own icon and tone. */
const SOURCE_BADGES: Record<WalletSnapshot['source'], { label: string; tone: StatusTone; icon: ReactNode }> = {
  local: {
    label: 'Local ledger',
    tone: 'info',
    icon: <Database className={BADGE_ICON_CLASSES} />,
  },
  venue: {
    label: 'Venue account',
    tone: 'warn',
    icon: <Landmark className={BADGE_ICON_CLASSES} />,
  },
};

/** One labelled value of the wallet, in the totals, the disclosure and the table. */
interface WalletValue {
  /** Stable identity (`initial_balance`, `cash`, ...). */
  id: string;
  /** Visible label; every value carries one. */
  label: string;
  /** Already formatted through the shared formatters (never blank). */
  value: string;
  /** Trend of the value, and nothing at all when the value is absent. */
  trend?: StatTrend;
  /** Decorative icon of the tile. */
  icon: ReactNode;
}

/** Render `value`, or the em dash placeholder when it is absent or blank. */
function textOrPlaceholder(value: string | null | undefined): string {
  const trimmed = (value ?? '').trim();
  return trimmed === '' ? EMPTY_PLACEHOLDER : trimmed;
}

/**
 * Trend of a profit/loss value.
 *
 * An absent value carries **no** trend claim: the caller passes `undefined`, so
 * the tile renders neither a glyph nor a label instead of claiming "Flat".
 */
function optionalTrend(value: number | null | undefined): StatTrend | undefined {
  return isFiniteNumber(value) ? trendOf(value) : undefined;
}

/**
 * The **three primary totals**, in rendering order.
 *
 * They are the header of the panel — the three questions an operator opens the
 * page with: what can I still deploy, what is currently at risk, and what is the
 * whole thing worth. Everything else is a detail of these three and lives behind
 * the disclosure below. Every value goes through the shared `formatMoney`, so an
 * absent one is the em dash placeholder and never `NaN` or `undefined`.
 */
function totalValues(wallet: WalletSnapshot | null): WalletValue[] {
  return [
    {
      id: 'total_cash',
      label: 'Total available cash',
      value: formatMoney(wallet?.total_cash ?? null),
      icon: <Wallet className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'positions_value',
      label: 'Total value of the positions',
      value: formatMoney(wallet?.positions_value ?? null),
      icon: <ChartLine className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'total_portfolio_value',
      label: 'Total value of the portfolio',
      value: formatMoney(wallet?.total_portfolio_value ?? null),
      icon: <Coins className={TILE_ICON_CLASSES} />,
    },
  ];
}

/**
 * The eight detailed values of the ledger, each labelled and formatted with the
 * shared formatters, so an absent value renders the em dash placeholder and
 * never `NaN`, `undefined` or an empty cell.
 *
 * They are collapsed by default: they are the breakdown of the three totals, not
 * a second header competing with them.
 */
function detailValues(wallet: WalletSnapshot | null): WalletValue[] {
  const initialBalance = wallet?.initial_balance ?? null;
  const cash = wallet?.cash ?? null;
  const equity = wallet?.equity ?? null;
  const deployed = wallet?.deployed ?? null;
  const totalExposure = wallet?.total_exposure ?? null;
  const realizedPnl = wallet?.realized_pnl ?? null;
  const unrealizedPnl = wallet?.unrealized_pnl ?? null;
  const profiles = wallet?.profiles ?? null;

  return [
    {
      id: 'initial_balance',
      label: 'Initial balance',
      value: formatMoney(initialBalance),
      icon: <Coins className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'cash',
      label: 'Cash',
      value: formatMoney(cash),
      icon: <Wallet className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'equity',
      label: 'Equity',
      value: formatMoney(equity),
      icon: <Scale className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'deployed',
      label: 'Deployed',
      value: formatMoney(deployed),
      icon: <ArrowUpRight className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'total_exposure',
      label: 'Total exposure',
      value: formatMoney(totalExposure),
      icon: <Gauge className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'realized_pnl',
      label: 'Realized P&L',
      value: formatSignedMoney(realizedPnl),
      trend: optionalTrend(realizedPnl),
      icon: <TrendingUp className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'unrealized_pnl',
      label: 'Unrealized P&L',
      value: formatSignedMoney(unrealizedPnl),
      trend: optionalTrend(unrealizedPnl),
      icon: <ChartLine className={TILE_ICON_CLASSES} />,
    },
    {
      id: 'profiles',
      label: 'Profiles funded',
      value: formatInteger(profiles),
      icon: <Users className={TILE_ICON_CLASSES} />,
    },
  ];
}

/**
 * The ledger of one run mode.
 *
 * The panel states what the ledger is before it shows a single number: **one**
 * USDT ledger funds every order of every profile *of a mode*, and the
 * per-profile figures of this page are attributed shares of it — a profile never
 * holds a pot of its own. The heading and the mode badge name the mode in words,
 * so a paper total can never be read as real money, and the badge is driven by
 * the `mode` prop so it stays correct even when that mode holds no ledger row.
 *
 * The **three totals** are the panel: total available cash, total value of the
 * positions and the emphasised total portfolio value. Every remaining value —
 * the durable ledger cash, equity, deployed, exposure, both P&L, the funded
 * count and the wallet identity — sits behind a **collapsed-by-default**
 * disclosure, so the header answers the three questions an operator opens the
 * page with and nothing else. The accessible table fallback lives *inside* that
 * disclosure: the text path of the same numbers never disappears, it is folded
 * away with them.
 *
 * A `null` ledger — a mode that was never traded and therefore holds no ledger
 * row, or an older server that emits no mode-keyed ledgers at all — renders the
 * em dash totals and says so **for that mode**, instead of a blank panel, a
 * thrown error, or a claim that the whole platform has no wallet.
 */
export function WalletPanel({ wallet, mode, className }: WalletPanelProps) {
  // `?? null` tolerates a payload without the key at all: the absence is the
  // documented em-dash state, never a crash.
  const snapshot = wallet ?? null;
  const totals = totalValues(snapshot);
  const details = detailValues(snapshot);
  const modeBadge = MODE_LABELS[mode] ?? null;
  const modeWord = MODE_WORDS[mode] ?? EMPTY_PLACEHOLDER;
  const source = snapshot === null ? null : (SOURCE_BADGES[snapshot.source] ?? null);
  const isLive = mode === 'live';

  // Mirror of the native `open` state of the disclosure, for its wording only:
  // the browser remains the owner of whether the content is shown.
  const [expanded, setExpanded] = useState(false);

  return (
    <section
      aria-labelledby={HEADING_ID}
      className={cn(
        'rounded-card border border-border bg-card p-xl text-card-foreground shadow-md',
        className,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-md">
        <div className="min-w-0">
          <h2
            id={HEADING_ID}
            className="font-mono text-base font-semibold text-card-foreground"
          >
            {`Platform wallet — ${modeWord}`}
          </h2>
          <p className="mt-xs text-sm text-muted-foreground">
            One shared USDT wallet funds every order of every profile. The per-profile figures on
            this page are attributed shares of that single ledger: the cash of a profile is its
            allocation minus the capital it has deployed, plus its realized P&amp;L — a profile
            never holds a pot of its own.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-sm">
          <StatusBadge
            label={source?.label ?? EMPTY_PLACEHOLDER}
            tone={source?.tone ?? 'neutral'}
            icon={source?.icon ?? <CircleHelp className={BADGE_ICON_CLASSES} />}
          />
          {/* Driven by the `mode` prop, never by the ledger: a mode that holds
              no row yet must still say which mode it is talking about. */}
          <StatusBadge
            label={modeBadge?.label ?? EMPTY_PLACEHOLDER}
            tone={modeBadge?.tone ?? 'neutral'}
            icon={modeBadge?.icon ?? <CircleHelp className={BADGE_ICON_CLASSES} />}
          />
        </div>
      </header>

      {snapshot === null ? (
        <p className="mt-lg flex flex-wrap items-center gap-sm rounded-button border border-border bg-muted/30 px-lg py-md text-sm text-foreground">
          <CircleHelp aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
          <span className="font-medium">{`No ${modeWord} ledger reported`}</span>
          <span className="min-w-0 break-words text-muted-foreground">
            {`The monitoring server returned no wallet snapshot for ${modeWord}: every value below shows the em dash placeholder until that mode holds a ledger row.`}
          </span>
        </p>
      ) : null}

      {isLive ? (
        <p className="mt-lg flex flex-wrap items-center gap-sm rounded-button border border-warn/40 bg-warn/10 px-lg py-md text-sm text-foreground">
          <Banknote aria-hidden="true" className="size-4 shrink-0 text-warn" />
          <span className="min-w-0 break-words">
            Live mode: this wallet mirrors the balance of the venue account, which is the ledger of
            record — the platform never debits it locally.
          </span>
        </p>
      ) : null}

      {/* The primary block: exactly three totals, the last one emphasised as
          THE total. The grid keeps its `data-testid` so a test scopes to it. */}
      <div
        data-testid="wallet-tiles"
        className="mt-lg grid gap-md sm:grid-cols-2 xl:grid-cols-3"
      >
        {totals.map((entry, index) =>
          index === totals.length - 1 ? (
            <PrimaryTotalTile key={entry.id} label={entry.label} value={entry.value} icon={entry.icon} />
          ) : (
            <StatTile key={entry.id} label={entry.label} value={entry.value} icon={entry.icon} />
          ),
        )}
      </div>

      {/* The details, COLLAPSED by default. A native `<details>`/`<summary>` is
          used rather than a `button aria-expanded` controlling a region: the
          element then carries the disclosure semantics and its own keyboard
          behaviour for free, and — the point here — the folded content is
          genuinely hidden and out of the tab order without this component wiring
          `aria-controls` and a matching id by hand. The rest of the dashboard
          already discloses its text fallbacks that way (see the chart data
          tables).

          `open` is controlled and defaults to `false`: the browser still owns
          the *interaction*, but the panel owns the state, so the visible wording
          is one sentence or the other in the DOM (never two spans swapped by
          CSS, which a screen reader would read out twice) and the default of
          "collapsed" is a value in the code rather than a hope about markup. */}
      <details
        open={expanded}
        className="mt-lg rounded-button border border-border bg-card/50 px-lg py-md"
      >
        <summary
          // A controlled `<details>` does not toggle itself, so the click is
          // handled here: it also keeps the panel the single owner of the state,
          // which is what makes "collapsed by default" a value in the code.
          onClick={(event) => {
            event.preventDefault();
            setExpanded((current) => !current);
          }}
          className={cn(
            'inline-flex cursor-pointer select-none items-center gap-xs font-mono text-xs font-medium text-muted-foreground',
            'rounded-button',
            // A real click target: it acknowledges the press without moving or
            // resizing anything.
            'active:bg-muted-pressed active:text-foreground',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          )}
        >
          <ChevronDown
            aria-hidden="true"
            className={cn('size-3.5 motion-safe:transition-transform', expanded && 'rotate-180')}
          />
          {/* The visible text of the control is exactly "Show details" / "Hide details". */}
          <span>{expanded ? 'Hide details' : 'Show details'}</span>
        </summary>

        <dl className="mt-md flex flex-wrap items-baseline gap-md text-xs">
          <div className="flex flex-wrap items-baseline gap-xs">
            <dt className="text-muted-foreground">Wallet</dt>
            <dd className="font-mono text-foreground">{textOrPlaceholder(snapshot?.name)}</dd>
          </div>
          <div className="flex flex-wrap items-baseline gap-xs">
            <dt className="text-muted-foreground">Updated</dt>
            <dd className="font-mono tabular-nums text-foreground">
              <time dateTime={snapshot?.updated_at ?? undefined}>
                {formatTimestamp(snapshot?.updated_at ?? null)}
              </time>
            </dd>
          </div>
        </dl>

        <div data-testid="wallet-details" className="mt-md grid gap-md sm:grid-cols-2 xl:grid-cols-4">
          {details.map((entry) => (
            <StatTile
              key={entry.id}
              label={entry.label}
              value={entry.value}
              trend={entry.trend}
              icon={entry.icon}
            />
          ))}
        </div>

        <div className="mt-md">
          <DataTable
            caption={`Platform wallet — the eight shared wallet values as text (${modeWord})`}
            columns={[
              { key: 'value', header: 'Wallet value', render: (row) => row.label },
              { key: 'amount', header: 'Amount', numeric: true, render: (row) => row.value },
            ]}
            rows={details}
            rowKey={(row) => row.id}
            emptyMessage={EMPTY_PLACEHOLDER}
          />
        </div>
      </details>
    </section>
  );
}

/**
 * The mode, spelled out for the heading and for the no-ledger note.
 *
 * Lower case on purpose: the heading reads "Platform wallet — paper trading" and
 * the note reads "No paper ledger reported", so the word is embedded in a
 * sentence rather than dropped in as a badge.
 */
const MODE_WORDS: Record<RunMode, string> = {
  paper: 'paper trading',
  live: 'real trading',
};

/** Props of {@link PrimaryTotalTile}. */
interface PrimaryTotalTileProps {
  /** What the total measures. */
  label: string;
  /** Already formatted total (an absent one is the em dash placeholder). */
  value: string;
  /** Decorative icon of the tile. */
  icon: ReactNode;
}

/**
 * The emphasised total of the panel — the portfolio value.
 *
 * It is the one number the panel exists to answer, so it gets an accent border,
 * a lifted surface and a larger value, and it spans the full width of the grid
 * on the wider breakpoints. The emphasis is **never** the meaning: the tile
 * keeps the same label, the same formatted value and the same icon as the other
 * two, so it stays in the accessibility tree and reads identically to a screen
 * reader — and it does not rest on colour alone.
 *
 * It is a local sibling of `StatTile` rather than a configured one because the
 * shared tile carries no styling hook: giving it one would change a design-system
 * component every other panel consumes, which this change has no business doing.
 */
function PrimaryTotalTile({ label, value, icon }: PrimaryTotalTileProps) {
  return (
    <div className="rounded-card border border-accent bg-accent/5 p-lg shadow-sm sm:col-span-2 xl:col-span-1">
      <div className="flex items-start justify-between gap-sm">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        <span aria-hidden="true" className="inline-flex shrink-0 text-accent">
          {icon}
        </span>
      </div>
      <p className="mt-sm font-mono text-2xl tabular-nums text-foreground">
        {value.trim() === '' ? EMPTY_PLACEHOLDER : value}
      </p>
    </div>
  );
}
