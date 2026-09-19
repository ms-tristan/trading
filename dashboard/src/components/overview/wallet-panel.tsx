import type { ReactNode } from 'react';

import {
  ArrowUpRight,
  Banknote,
  ChartLine,
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
  /** The shared platform wallet, `null` when the server reported none. */
  wallet: WalletSnapshot | null;
}

const HEADING_ID = 'platform-wallet-heading';

const BADGE_ICON_CLASSES = 'size-3.5';

const TILE_ICON_CLASSES = 'size-4';

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

/** The run mode of the wallet, spelled out instead of left to the colour. */
const MODE_BADGES: Record<RunMode, { label: string; tone: StatusTone; icon: ReactNode }> = {
  paper: {
    label: 'Paper mode',
    tone: 'info',
    icon: <FlaskConical className={BADGE_ICON_CLASSES} />,
  },
  live: {
    label: 'Live mode',
    tone: 'warn',
    icon: <Banknote className={BADGE_ICON_CLASSES} />,
  },
};

/** One labelled value of the wallet, in the tiles and in the table fallback. */
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
 * The eight documented wallet values, each labelled and formatted with the
 * shared formatters, so an absent value renders the em dash placeholder and
 * never `NaN`, `undefined` or an empty cell.
 */
function walletValues(wallet: WalletSnapshot | null): WalletValue[] {
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
 * The shared wallet of the platform.
 *
 * The panel states what the wallet is before it shows a single number: **one**
 * USDT ledger funds every order of every profile, and the per-profile figures
 * of this page are attributed shares of it — a profile never holds a pot of its
 * own. Its eight values carry an explicit label and are formatted with the
 * shared formatters, so an absent value is an em dash and never `NaN` or
 * `undefined`. `source` and `mode` are text badges with an icon: colour never
 * carries the meaning alone. In live mode the wallet mirrors the venue account
 * and the platform never debits it locally, which the panel says in words.
 *
 * Beside the tiles the same eight values are repeated in an accessible table
 * with a caption, so the text fallback never depends on a chart or on a tile's
 * layout. A `null` wallet — an older server that emits no wallet key, or a
 * platform that reported none — renders the em dash state with a short note
 * instead of a blank panel or a thrown error.
 */
export function WalletPanel({ wallet }: WalletPanelProps) {
  // `?? null` tolerates a payload without the key at all: the absence is the
  // documented em-dash state, never a crash.
  const snapshot = wallet ?? null;
  const values = walletValues(snapshot);
  const source = snapshot === null ? null : (SOURCE_BADGES[snapshot.source] ?? null);
  const mode = snapshot === null ? null : (MODE_BADGES[snapshot.mode] ?? null);
  const isLive = snapshot !== null && snapshot.mode === 'live';

  return (
    <section
      aria-labelledby={HEADING_ID}
      className="rounded-card border border-border bg-card p-xl text-card-foreground shadow-md"
    >
      <header className="flex flex-wrap items-start justify-between gap-md">
        <div className="min-w-0">
          <h2
            id={HEADING_ID}
            className="font-mono text-base font-semibold text-card-foreground"
          >
            Platform wallet
          </h2>
          <p className="mt-xs text-sm text-muted-foreground">
            One shared USDT wallet funds every order of every profile. The per-profile figures on
            this page are attributed shares of that single ledger: the cash of a profile is its
            allocation minus the capital it has deployed, plus its realized P&amp;L — a profile
            never holds a pot of its own.
          </p>
          <dl className="mt-sm flex flex-wrap items-baseline gap-md text-xs">
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
        </div>
        <div className="flex flex-wrap items-center gap-sm">
          <StatusBadge
            label={source?.label ?? EMPTY_PLACEHOLDER}
            tone={source?.tone ?? 'neutral'}
            icon={source?.icon ?? <CircleHelp className={BADGE_ICON_CLASSES} />}
          />
          <StatusBadge
            label={mode?.label ?? EMPTY_PLACEHOLDER}
            tone={mode?.tone ?? 'neutral'}
            icon={mode?.icon ?? <CircleHelp className={BADGE_ICON_CLASSES} />}
          />
        </div>
      </header>

      {snapshot === null ? (
        <p className="mt-lg flex flex-wrap items-center gap-sm rounded-button border border-border bg-muted/30 px-lg py-md text-sm text-foreground">
          <CircleHelp aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
          <span className="font-medium">No shared wallet reported</span>
          <span className="min-w-0 break-words text-muted-foreground">
            The monitoring server returned no wallet snapshot: every value below shows the em dash
            placeholder until it reports one.
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

      <div
        data-testid="wallet-tiles"
        className="mt-lg grid gap-md sm:grid-cols-2 xl:grid-cols-4"
      >
        {values.map((entry) => (
          <StatTile
            key={entry.id}
            label={entry.label}
            value={entry.value}
            trend={entry.trend}
            icon={entry.icon}
          />
        ))}
      </div>

      <div className="mt-lg">
        <DataTable
          caption="Platform wallet — the eight shared wallet values as text"
          columns={[
            { key: 'value', header: 'Wallet value', render: (row) => row.label },
            { key: 'amount', header: 'Amount', numeric: true, render: (row) => row.value },
          ]}
          rows={values}
          rowKey={(row) => row.id}
          emptyMessage={EMPTY_PLACEHOLDER}
        />
      </div>
    </section>
  );
}
