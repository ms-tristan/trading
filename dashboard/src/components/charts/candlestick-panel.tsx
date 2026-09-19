'use client';

/**
 * Candles panel of the profile detail page.
 *
 * It polls `GET /api/profiles/{id}/candles` on the **detail cadence** — the same
 * slower multiplier the equity curve, the tables and the metrics already use —
 * so the page keeps exactly two polling loops and never invents a third rhythm.
 *
 * The two overlays a trader needs are built from payloads the page already
 * holds: the entry and exit markers come from the closed trades, and the average
 * price / stop lines come from the open positions. Neither is fetched here.
 *
 * Robustness: an empty series renders the chart's empty state, and a failed poll
 * shows the error banner **while keeping the last known good candles** on screen.
 * A failure is never printed raw: the panel translates it once with
 * {@link failureReport}, so the operator reads a monitoring-level headline (a
 * `502`, `503` or `504` from the proxy, a connection failure and a timeout all
 * say the monitoring API is unreachable) and the underlying status stays as the
 * banner's detail line. The panel carries the machine-readable degraded state
 * (`data-candles-stale`, `data-candles-count`) and offers an explicit retry.
 */

import { useCallback, useMemo } from 'react';
import type { JSX } from 'react';

import { RefreshCw } from 'lucide-react';

import { CandlestickChart } from '@/components/charts/candlestick-chart';
import { CandlestickLegend } from '@/components/charts/candlestick-legend';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ErrorBanner } from '@/components/ui/error-banner';
import { fetchCandles } from '@/lib/api';
import { failureReport } from '@/lib/api-failure';
import { CANDLE_RENDER_LIMIT, positionPriceLines, tradeMarkers } from '@/lib/candles';
import type { CandlesPayload, PositionsPayload, TradesPayload } from '@/lib/types';
import { usePolling } from '@/lib/use-polling';

/**
 * Number of candles requested from the API.
 *
 * The chart renders at most {@link CANDLE_RENDER_LIMIT} bars, so asking for more
 * would only transfer data nobody displays. The API caps the parameter itself.
 */
export const CANDLE_ROUTE_LIMIT = CANDLE_RENDER_LIMIT;

/** Payload rendered before the first poll answers. */
const EMPTY_CANDLES: CandlesPayload = { candles: [], count: 0 };

/** Props of {@link CandlestickPanel}. */
export interface CandlestickPanelProps {
  /** Profile whose candles are displayed. */
  profileId: string;
  /** Open positions of the profile (average price and stop overlays). */
  positions: PositionsPayload;
  /** Closed trades of the profile (entry and exit markers). */
  trades: TradesPayload;
  /** Cadence of the detail loop, in milliseconds. */
  detailIntervalMs: number;
  /**
   * Candles rendered by the Server Component for the first paint.
   *
   * Without them the panel starts on {@link EMPTY_CANDLES} and shows its empty
   * state until the detail loop fires — ten seconds of "No candle yet" on every
   * page load, for data the server already had.
   */
  initialCandles?: CandlesPayload;
  /** Absolute origin used by a Server Component; empty means same origin. */
  baseUrl?: string;
  /** Fetch implementation seam for tests. Defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
  className?: string;
}

/** Symbol of the instrument the profile trades, when a position names it. */
function symbolOf(positions: PositionsPayload): string | null {
  for (const position of positions.positions) {
    const symbol = position.symbol.trim();
    if (symbol !== '') {
      return symbol;
    }
  }
  return null;
}

/** Candles panel: the live candlestick chart of one profile. */
export function CandlestickPanel({
  profileId,
  positions,
  trades,
  detailIntervalMs,
  initialCandles,
  baseUrl,
  fetchImpl,
  className,
}: CandlestickPanelProps): JSX.Element {
  const fetcher = useCallback(
    async (signal: AbortSignal): Promise<CandlesPayload> =>
      fetchCandles(profileId, CANDLE_ROUTE_LIMIT, { signal, baseUrl, fetchImpl }),
    [baseUrl, fetchImpl, profileId],
  );

  const { data, failure, refreshNow } = usePolling<CandlesPayload>({
    fetcher,
    // The server-rendered window is the first paint; the loop only refreshes it.
    initialData: initialCandles ?? EMPTY_CANDLES,
    intervalMs: detailIntervalMs,
  });

  // The single translation layer of the dashboard: the banner shows the
  // monitoring-level headline and keeps the raw status, the server text and the
  // requested path in its detail line. `null` means "no failure on screen".
  const report = failure === null ? null : failureReport(failure);

  const markers = useMemo(() => tradeMarkers(trades.trades), [trades]);
  const priceLines = useMemo(() => positionPriceLines(positions.positions), [positions]);

  const handleRefresh = useCallback((): void => {
    void refreshNow();
  }, [refreshNow]);

  const symbol = symbolOf(positions);
  // The panel is handed the position and trade payloads, neither of which
  // carries the profile timeframe: the description names what it does know.
  const description =
    symbol === null ? 'Persisted candles of this profile' : `${symbol} · persisted candles`;

  return (
    <Card
      title="Candles"
      description={description}
      className={className}
      actions={
        <Button
          size="sm"
          variant="ghost"
          onClick={handleRefresh}
          icon={<RefreshCw className="size-3.5" />}
        >
          Refresh now
        </Button>
      }
    >
      <div
        data-testid="candlestick-panel"
        // Degraded state, readable without looking at the screen: a failed poll
        // leaves the last known good payload in place, so the count stays at the
        // last good one while the stale marker turns on.
        data-candles-stale={report === null ? 'false' : 'true'}
        data-candles-count={String(data.candles.length)}
        className="flex flex-col gap-lg"
      >
        <ErrorBanner
          message={report?.headline ?? null}
          detail={report?.detail ?? null}
          onRetry={handleRefresh}
          retryLabel="Retry candles"
        />
        <CandlestickChart
          candles={data.candles}
          markers={markers}
          priceLines={priceLines}
          ariaLabel={`Candlestick chart of ${profileId}`}
        />
        <CandlestickLegend />
      </div>
    </Card>
  );
}
