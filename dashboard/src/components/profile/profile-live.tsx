'use client';

/**
 * Live section of the profile detail page.
 *
 * Two polling loops, both seeded by the Server Component so the first paint is
 * complete without JavaScript:
 *
 * * the **live loop** (`pollIntervalMs`, the documented 2 s cadence) refreshes
 *   the profile snapshot, the platform health and the kill-switch state — the
 *   cheap payloads that must react immediately;
 * * the **detail loop** (`detailIntervalMs`, 5x slower) refreshes the
 *   SQLite-backed, metric-heavy payloads: equity curve, positions, trades,
 *   orders and metrics. The candle chart polls on that same cadence through
 *   `CandlestickPanel`, so the page never runs a third rhythm.
 *
 * A single {@link LiveToolbar} drives both, a single error banner combines both
 * failures, and a failed poll never clears the screen: the last known good
 * bundles stay rendered.
 */

import { useCallback } from 'react';

import { CandlestickPanel } from '@/components/charts/candlestick-panel';
import { EquityChart } from '@/components/charts/equity-chart';
import { KillSwitchPanel } from '@/components/kill-switch/kill-switch-panel';
import { Card } from '@/components/ui/card';
import { LiveToolbar } from '@/components/ui/live-toolbar';
import {
  fetchEquity,
  fetchHealth,
  fetchKillSwitch,
  fetchMetrics,
  fetchOrders,
  fetchPositions,
  fetchProfile,
  fetchTrades,
} from '@/lib/api';
import { usePolling } from '@/lib/use-polling';
import type { ProfileDetailBundle, ProfileLiveBundle } from '@/lib/types';

import { BenchmarkPanel } from './benchmark-panel';
import { MetricsPanel } from './metrics-panel';
import { OrdersTable } from './orders-table';
import { PositionsTable } from './positions-table';
import { ProfileHeader } from './profile-header';
import { TradesTable } from './trades-table';

/** Props of {@link ProfileLive}. */
export interface ProfileLiveProps {
  /** Profile id of the route (`/profiles/[id]`). */
  profileId: string;
  /** Live bundle rendered by the Server Component. */
  initialLive: ProfileLiveBundle;
  /** Detail bundle rendered by the Server Component. */
  initialDetail: ProfileDetailBundle;
  /** ISO-8601 stamp of the server-side fetch. */
  initialCheckedAt: string;
  /** Cadence of the live loop, in milliseconds. */
  pollIntervalMs: number;
  /** Cadence of the detail loop, in milliseconds. */
  detailIntervalMs: number;
}

/** Fold the two loop failures into the single banner message. */
function combineErrors(liveError: string | null, detailError: string | null): string | null {
  const parts: string[] = [];
  if (liveError !== null) {
    parts.push(`Live updates: ${liveError}`);
  }
  if (detailError !== null) {
    parts.push(`Detail data: ${detailError}`);
  }
  return parts.length === 0 ? null : parts.join(' · ');
}

/** Live profile detail: header, kill switch, equity curve, tables and panels. */
export function ProfileLive({
  profileId,
  initialLive,
  initialDetail,
  initialCheckedAt,
  pollIntervalMs,
  detailIntervalMs,
}: ProfileLiveProps) {
  const liveFetcher = useCallback(
    async (signal: AbortSignal): Promise<ProfileLiveBundle> => {
      const [profile, health, killSwitch] = await Promise.all([
        fetchProfile(profileId, { signal }),
        fetchHealth({ signal }),
        fetchKillSwitch({ signal }),
      ]);
      return { profile, health, killSwitch };
    },
    [profileId],
  );

  const detailFetcher = useCallback(
    async (signal: AbortSignal): Promise<ProfileDetailBundle> => {
      const [equity, positions, trades, orders, metrics] = await Promise.all([
        fetchEquity(profileId, { signal }),
        fetchPositions(profileId, { signal }),
        fetchTrades(profileId, { signal }),
        fetchOrders(profileId, { signal }),
        fetchMetrics(profileId, { signal }),
      ]);
      return { equity, positions, trades, orders, metrics };
    },
    [profileId],
  );

  const live = usePolling<ProfileLiveBundle>({
    fetcher: liveFetcher,
    initialData: initialLive,
    initialCheckedAt,
    intervalMs: pollIntervalMs,
  });

  const detail = usePolling<ProfileDetailBundle>({
    fetcher: detailFetcher,
    initialData: initialDetail,
    intervalMs: detailIntervalMs,
  });

  const isPaused = live.isPaused || detail.isPaused;

  const handleToggle = useCallback((): void => {
    // One control drives both loops: live updates are on or off as a whole.
    if (live.isPaused || detail.isPaused) {
      live.resume();
      detail.resume();
      return;
    }
    live.pause();
    detail.pause();
  }, [detail, live]);

  const handleRefresh = useCallback((): void => {
    void live.refreshNow();
    void detail.refreshNow();
  }, [detail, live]);

  return (
    <div className="flex flex-col gap-2xl">
      <LiveToolbar
        checkedAt={live.checkedAt ?? initialCheckedAt}
        isPaused={isPaused}
        onToggle={handleToggle}
        onRefresh={handleRefresh}
        error={combineErrors(live.error, detail.error)}
        liveLabel="Live updates"
      />

      <ProfileHeader
        profile={live.data.profile}
        health={live.data.health}
        killSwitch={live.data.killSwitch}
      />

      <KillSwitchPanel initialState={initialLive.killSwitch} state={live.data.killSwitch} />

      <Card
        title="Equity curve"
        description="Equity, cash and position value of this profile over time."
      >
        <EquityChart points={detail.data.equity.points} ariaLabel={`Equity curve of ${profileId}`} />
      </Card>

      <CandlestickPanel
        profileId={profileId}
        positions={detail.data.positions}
        trades={detail.data.trades}
        detailIntervalMs={detailIntervalMs}
      />

      <Card
        title="Open positions"
        description="Every position currently held, with its PnL and stop price."
      >
        <PositionsTable positions={detail.data.positions.positions} />
      </Card>

      <Card title="Recent trades" description="Closed round-trips, newest first.">
        <TradesTable trades={detail.data.trades.trades} />
      </Card>

      <Card title="Recent orders" description="Orders sent to the broker, newest first.">
        <OrdersTable orders={detail.data.orders.orders} />
      </Card>

      <Card title="Metrics" description="The frozen metric set of this profile.">
        <MetricsPanel metrics={detail.data.metrics.metrics} />
      </Card>

      <Card title="Benchmark" description="Comparison against a passive strategy.">
        <BenchmarkPanel benchmark={detail.data.metrics.benchmark} />
      </Card>
    </div>
  );
}
