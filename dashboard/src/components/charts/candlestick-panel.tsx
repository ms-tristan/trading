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
  baseUrl,
  fetchImpl,
  className,
}: CandlestickPanelProps): JSX.Element {
  const fetcher = useCallback(
    async (signal: AbortSignal): Promise<CandlesPayload> =>
      fetchCandles(profileId, CANDLE_ROUTE_LIMIT, { signal, baseUrl, fetchImpl }),
    [baseUrl, fetchImpl, profileId],
  );

  const { data, error, refreshNow } = usePolling<CandlesPayload>({
    fetcher,
    initialData: EMPTY_CANDLES,
    intervalMs: detailIntervalMs,
  });

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
      <div className="flex flex-col gap-lg">
        <ErrorBanner message={error} onRetry={handleRefresh} />
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
