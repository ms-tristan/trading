import { cn } from "@/lib/cn";
import { directionGlyph, formatSignedUsdt } from "@/lib/format";
import type { DailyBarPoint } from "@/lib/types";

export interface DailyBarsProps {
  bars: DailyBarPoint[];
  height?: number;
  /** Accessible name of the chart; also used as the table caption. */
  caption?: string;
  className?: string;
}

const WIDTH = 720;
const PADDING_Y = 10;

/**
 * Daily profit bars - hand-rolled SVG, no charting dependency.
 *
 * A profitable day is drawn above the zero line in `--chart-bull`, a losing day
 * below it in `--chart-bear`, and the sign is spelled out again with a glyph in
 * the table fallback: colour is never the only carrier of the information.
 */
export function DailyBars({
  bars,
  height = 160,
  caption = "Daily profit",
  className,
}: DailyBarsProps) {
  const usable = bars.filter((bar) => Number.isFinite(bar.profit_usdt));
  const values = usable.map((bar) => bar.profit_usdt);
  const high = values.length > 0 ? Math.max(0, ...values) : 0;
  const low = values.length > 0 ? Math.min(0, ...values) : 0;
  const span = high - low;
  const band = usable.length > 0 ? (WIDTH - 8) / usable.length : WIDTH;
  const barWidth = Math.max(band * 0.62, 2);

  const y = (value: number) => {
    const ratio = span === 0 ? 0.5 : (value - low) / span;
    return PADDING_Y + (1 - ratio) * (height - PADDING_Y * 2);
  };
  const zeroY = y(0);
  const total = values.reduce((sum, value) => sum + value, 0);

  return (
    <figure className={cn("m-0 min-w-0", className)}>
      {usable.length > 0 ? (
        <div className="w-full min-w-0 overflow-x-auto">
          <svg
            role="img"
            aria-label={`${caption}: ${usable.length} bars, total ${formatSignedUsdt(total)}`}
            viewBox={`0 0 ${WIDTH} ${height}`}
            width="100%"
            height={height}
            preserveAspectRatio="none"
            className="min-w-[320px]"
          >
            <line
              x1={0}
              x2={WIDTH}
              y1={zeroY}
              y2={zeroY}
              stroke="var(--chart-grid)"
              strokeWidth={1}
            />
            {usable.map((bar, index) => {
              const x = 4 + index * band + (band - barWidth) / 2;
              const valueY = y(bar.profit_usdt);
              const top = Math.min(valueY, zeroY);
              const barHeight = Math.max(Math.abs(zeroY - valueY), 1);
              const positive = bar.profit_usdt >= 0;
              return (
                <rect
                  key={`${bar.date}-${index}`}
                  x={x.toFixed(2)}
                  y={top.toFixed(2)}
                  width={barWidth.toFixed(2)}
                  height={barHeight.toFixed(2)}
                  fill={positive ? "var(--chart-bull)" : "var(--chart-bear)"}
                />
              );
            })}
          </svg>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">No daily profit published yet.</p>
      )}

      <figcaption className="mt-2 text-sm text-muted-foreground tabular-nums">
        {usable.length} {usable.length === 1 ? "day" : "days"} - total {formatSignedUsdt(total)}
      </figcaption>

      <table className="mt-2 w-full min-w-0 border-collapse text-sm">
        <caption className="sr-only">{caption}: daily values</caption>
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th scope="col" className="px-2 py-1 font-medium">
              Day
            </th>
            <th scope="col" className="px-2 py-1 text-right font-medium">
              Profit
            </th>
            <th scope="col" className="px-2 py-1 text-right font-medium">
              Trades
            </th>
          </tr>
        </thead>
        <tbody>
          {usable.map((bar, index) => (
            <tr key={`${bar.date}-${index}`} className="h-9 border-b border-border/60">
              <th scope="row" className="px-2 py-1 text-left font-normal">
                <span aria-hidden="true">{directionGlyph(bar.profit_usdt)}</span> {bar.date}
              </th>
              <td className="px-2 py-1 text-right tabular-nums">
                {formatSignedUsdt(bar.profit_usdt)}
              </td>
              <td className="px-2 py-1 text-right tabular-nums">{bar.trades}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
