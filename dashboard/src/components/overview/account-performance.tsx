'use client';

import type { ReactNode } from 'react';

import {
  ArrowUpRight,
  Banknote,
  ChartLine,
  Coins,
  FlaskConical,
  Layers,
  TrendingDown,
  TrendingUp,
  Trophy,
} from 'lucide-react';

import { StatTile, type StatTrend } from '@/components/ui/stat-tile';
import { StatusBadge, type StatusTone } from '@/components/ui/status-badge';
import { cn } from '@/lib/cn';
import {
  EMPTY_PLACEHOLDER,
  formatInteger,
  formatMoney,
  formatRatioAsPercent,
  formatSignedMoney,
  isFiniteNumber,
  trendOf,
} from '@/lib/format';
import {
  accountPerformance,
  ledgerPerformance,
  resolveModeWallet,
  type RankedProfile,
} from '@/lib/performance';
import type { ProfilesPayload, RunMode } from '@/lib/types';

/** Props of {@link AccountPerformance}. */
export interface AccountPerformanceProps {
  /** Payload of `GET /api/profiles`, the one the polling cycle refreshes. */
  profiles: ProfilesPayload;
  /** Mode the figures belong to (drives the heading, the badge and the ledger). */
  mode: RunMode;
}

const HEADING_ID = 'account-performance-heading';

const LEDGERS_HEADING_ID = 'account-performance-ledgers-heading';

const TILE_ICON_CLASSES = 'size-4';

const BADGE_ICON_CLASSES = 'size-3.5';

/**
 * The mode, spelled out for the heading and the badge.
 *
 * Lower case on purpose, exactly like the wallet panel: the heading reads
 * "Account performance — paper trading", so the mode is embedded in a sentence
 * rather than dropped in as a second label.
 */
const MODE_WORDS: Record<RunMode, string> = {
  paper: 'paper trading',
  live: 'real trading',
};

/**
 * The badge of the selected mode, with the icon and the tone vocabulary the rest
 * of the dashboard already uses for a run mode (`FlaskConical` for the simulated
 * ledger, `Banknote` for the real one).
 *
 * The label is deliberately not one of the labels another block already renders
 * ("Paper mode" / "Real mode" belong to the wallet panel): every state of this
 * page stays greppable as one unique piece of text.
 */
const MODE_BADGES: Record<RunMode, { label: string; tone: StatusTone; icon: ReactNode }> = {
  paper: {
    label: 'Paper trading account',
    tone: 'info',
    icon: <FlaskConical className={BADGE_ICON_CLASSES} />,
  },
  live: {
    label: 'Real trading account',
    tone: 'warn',
    icon: <Banknote className={BADGE_ICON_CLASSES} />,
  },
};

/** The two ledgers of the platform, in reading order: the simulated one first. */
const LEDGER_MODES: readonly RunMode[] = ['paper', 'live'];

/** How each ledger reads in the side-by-side comparison. */
const LEDGER_LABELS: Record<RunMode, string> = {
  paper: 'Paper ledger',
  live: 'Real ledger',
};

/**
 * Trend of a profit/loss value.
 *
 * An absent value carries **no** trend claim at all: the caller passes
 * `undefined`, so the tile renders neither a glyph nor a label instead of
 * claiming "Flat" about a figure that does not exist.
 */
function optionalTrend(value: number | null | undefined): StatTrend | undefined {
  return isFiniteNumber(value) ? trendOf(value) : undefined;
}

/**
 * The value of the total return, as one readable pair.
 *
 * The USDT amount is the value and the percentage is the hint under it: the
 * percentage is *derived* (see `ledgerPerformance`), so it is shown next to the
 * amount it was derived from and never as a figure of its own. Both are `null`
 * at once when the ledger has no usable initial balance, and both then render
 * the em dash placeholder — a percentage is never invented.
 */
function returnText(
  totalReturn: number | null,
  totalReturnRatio: number | null,
): { value: string; hint: string } {
  return {
    value: formatSignedMoney(totalReturn),
    hint: formatRatioAsPercent(totalReturnRatio, { signed: true }),
  };
}

/**
 * The caption under the best / worst tile: the name of the ranked profile.
 *
 * A rank that does not exist yet says so in words instead of leaving the caption
 * blank — an empty cell would read as "the name is missing" rather than "there
 * is no ranked profile", and a blank id falls back to the shared placeholder.
 */
function rankCaption(ranked: RankedProfile | null): string {
  if (ranked === null) {
    return 'No profile with a return yet';
  }
  const name = ranked.profileId.trim();
  return name === '' ? EMPTY_PLACEHOLDER : name;
}

/**
 * Global performance of the account — the first block of the live region.
 *
 * **This component is presentational.** It receives the profiles payload and the
 * selected mode, and renders the figures `accountPerformance` derives from them:
 * it never calls `fetch*`, it owns no `useState` and no `useEffect`, and it
 * therefore starts **no second poll**. It lives *inside* the existing live
 * region precisely so that it refreshes on the polling cycle that is already
 * running and follows the selected run mode without a request of its own.
 *
 * **Anti-duplication rule.** The wallet panel directly below renders the shared
 * ledger in full — available cash, position value, cash, equity, exposure, the
 * both P&L and the funded count, behind its disclosure. This block therefore
 * renders **performance only**: the portfolio value as the hero number, the
 * total return (USDT and percent), both P&L, the deployed capital, how many
 * profiles run out of how many are configured, and the best and the worst
 * profile by total return. Exactly **one** number legitimately appears in both
 * blocks — the portfolio value, the total this block exists to lead with. No
 * cash figure, no exposure, no per-profile money breakdown is copied here.
 *
 * **Both ledgers at once.** Next to the hero, the paper and the real ledger are
 * compared side by side (portfolio value and total return each), read out of the
 * same payload, so the account is readable without switching the mode back and
 * forth — switching stays a pure presentation change and still issues no request.
 *
 * **Absent is never invented.** Every figure goes through the shared formatters,
 * so a value the payload does not carry renders the em dash placeholder: never
 * `NaN`, never `undefined`, never an empty cell. The percentage in particular is
 * derived from the ledger's own `initial_balance` and portfolio value only, and
 * is an em dash when the initial balance is missing or zero.
 */
export function AccountPerformance({ profiles, mode }: AccountPerformanceProps) {
  const summary = accountPerformance(profiles, mode);
  const totals = returnText(summary.ledger.totalReturn, summary.ledger.totalReturnRatio);

  return (
    <section
      aria-labelledby={HEADING_ID}
      data-testid="account-performance"
      className="rounded-card border border-border bg-card p-xl text-card-foreground shadow-md"
    >
      <header className="flex flex-wrap items-start justify-between gap-md">
        <div className="min-w-0">
          <h2 id={HEADING_ID} className="font-mono text-base font-semibold text-card-foreground">
            {`Account performance — ${MODE_WORDS[mode]}`}
          </h2>
          <p className="mt-xs text-sm text-muted-foreground">
            {`Global performance of the ${MODE_WORDS[mode]} account: what the shared ledger is worth, what it has returned since inception and which profiles lead it. One shared USDT ledger funds every profile of a mode, so these figures are the sum of their attributed shares.`}
          </p>
        </div>
        <StatusBadge
          label={MODE_BADGES[mode].label}
          tone={MODE_BADGES[mode].tone}
          icon={MODE_BADGES[mode].icon}
        />
      </header>

      <div className="mt-lg grid gap-lg xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-md">
          {/* The hero number of the page: what the whole account is worth. */}
          <HeroValueTile
            label="Total portfolio value"
            value={formatMoney(summary.ledger.portfolioValue)}
            icon={<Coins className="size-5" />}
          />

          <div
            data-testid="account-performance-tiles"
            className="grid gap-md sm:grid-cols-2 xl:grid-cols-3"
          >
            <StatTile
              label="Total return"
              value={totals.value}
              hint={totals.hint}
              trend={optionalTrend(summary.ledger.totalReturn)}
              icon={<TrendingUp className={TILE_ICON_CLASSES} />}
            />
            <StatTile
              label="Realized P&L"
              value={formatSignedMoney(summary.realizedPnl)}
              trend={optionalTrend(summary.realizedPnl)}
              icon={<TrendingUp className={TILE_ICON_CLASSES} />}
            />
            <StatTile
              label="Unrealized P&L"
              value={formatSignedMoney(summary.unrealizedPnl)}
              trend={optionalTrend(summary.unrealizedPnl)}
              icon={<ChartLine className={TILE_ICON_CLASSES} />}
            />
            <StatTile
              label="Deployed capital"
              value={formatMoney(summary.deployed)}
              icon={<ArrowUpRight className={TILE_ICON_CLASSES} />}
            />
            <StatTile
              label="Profiles running / configured"
              value={`${formatInteger(summary.profilesRunning)} / ${formatInteger(
                summary.profilesConfigured,
              )}`}
              hint="running of configured"
              icon={<Layers className={TILE_ICON_CLASSES} />}
            />
            <StatTile
              label="Best profile"
              value={formatRatioAsPercent(summary.best?.totalReturn ?? null, { signed: true })}
              hint={rankCaption(summary.best)}
              trend={optionalTrend(summary.best?.totalReturn ?? null)}
              icon={<Trophy className={TILE_ICON_CLASSES} />}
            />
            <StatTile
              label="Worst profile"
              value={formatRatioAsPercent(summary.worst?.totalReturn ?? null, { signed: true })}
              hint={rankCaption(summary.worst)}
              trend={optionalTrend(summary.worst?.totalReturn ?? null)}
              icon={<TrendingDown className={TILE_ICON_CLASSES} />}
            />
          </div>
        </div>

        {/* The two ledgers of the platform, side by side. Rendered as an
            `aside` with its own level-3 heading rather than through `Card`,
            whose heading is a level-2 `<h2>`: a nested h2 would compete with
            the heading of this very section in the outline. */}
        <aside
          aria-labelledby={LEDGERS_HEADING_ID}
          data-testid="account-performance-ledgers"
          className="min-w-0 rounded-card border border-border bg-background/40 p-lg"
        >
          <h3
            id={LEDGERS_HEADING_ID}
            className="font-mono text-sm font-semibold text-card-foreground"
          >
            Both ledgers
          </h3>
          <p className="mt-xs text-xs text-muted-foreground">
            The paper and the real ledger read from the same payload, so one can be compared with
            the other without switching mode.
          </p>

          {/* The two ledgers are literally side by side, each with the two
              figures that make them comparable: their portfolio value and their
              total return. */}
          <div className="mt-md grid gap-lg sm:grid-cols-2">
            {LEDGER_MODES.map((ledgerMode) => {
              const performance = ledgerPerformance(resolveModeWallet(profiles, ledgerMode));
              const ledgerReturn = returnText(
                performance.totalReturn,
                performance.totalReturnRatio,
              );
              return (
                <div key={ledgerMode} data-testid={`ledger-${ledgerMode}`} className="min-w-0">
                  <p
                    className={cn(
                      'text-xs font-medium uppercase tracking-wide',
                      ledgerMode === mode ? 'text-foreground' : 'text-muted-foreground',
                    )}
                  >
                    {/* Which ledger the hero figures above belong to is stated in
                        words, not by the brighter colour alone. */}
                    {ledgerMode === mode
                      ? `${LEDGER_LABELS[ledgerMode]} · current mode`
                      : LEDGER_LABELS[ledgerMode]}
                  </p>
                  <div className="mt-sm grid gap-md">
                    <StatTile
                      label="Portfolio value"
                      value={formatMoney(performance.portfolioValue)}
                    />
                    <StatTile
                      label="Total return"
                      value={ledgerReturn.value}
                      hint={ledgerReturn.hint}
                      trend={optionalTrend(performance.totalReturn)}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </aside>
      </div>
    </section>
  );
}

/** Props of {@link HeroValueTile}. */
interface HeroValueTileProps {
  /** What the number measures. */
  label: string;
  /** Already formatted value (an absent one is the em dash placeholder). */
  value: string;
  /** Decorative icon of the tile. */
  icon: ReactNode;
}

/**
 * The hero number of the page — the total value of the portfolio.
 *
 * It is the one figure the block exists to answer, so it gets an accent border,
 * a lifted surface and a larger value than the tiles under it. The emphasis is
 * **never** the meaning: the tile keeps a visible label, a decorative icon that
 * is hidden from assistive technology and the same formatted value as every
 * other tile, so it stays in the accessibility tree and reads identically to a
 * screen reader.
 *
 * It is a local sibling of `StatTile` rather than a configured one because the
 * shared tile carries no styling hook: giving it one would change a
 * design-system component every other panel consumes, which this change has no
 * business doing. `WalletPanel` emphasises its own total the same way, with the
 * same reasoning.
 */
function HeroValueTile({ label, value, icon }: HeroValueTileProps) {
  return (
    <div
      data-testid="account-performance-hero"
      className="rounded-card border border-accent bg-accent/5 p-lg shadow-sm"
    >
      <div className="flex items-start justify-between gap-sm">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        <span aria-hidden="true" className="inline-flex shrink-0 text-accent">
          {icon}
        </span>
      </div>
      <p className="mt-sm font-mono text-3xl tabular-nums text-foreground">
        {value.trim() === '' ? EMPTY_PLACEHOLDER : value}
      </p>
    </div>
  );
}
