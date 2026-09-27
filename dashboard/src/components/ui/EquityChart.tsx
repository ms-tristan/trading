import { cn } from "@/lib/cn";
import { directionGlyph, formatSignedRatioPercent, formatTimestamp, formatUsdt } from "@/lib/format";

/** One point of one series of an equity chart. */
export interface EquitySeriesPoint {
  timestamp: string;
  value: number;
}

/** One named series (the combined account, a profile, a benchmark, ...). */
export interface EquitySeries {
  id: string;
  label: string;
  points: EquitySeriesPoint[];
}

export interface EquityChartProps {
  series: EquitySeries[];
  height?: number;
  /** Accessible name of the chart; also used as the table caption. */
  caption?: string;
  className?: string;
}

const WIDTH = 720;
const PADDING_X = 8;
const PADDING_Y = 12;
const GRID_LINES = 4;

function finitePoints(points: EquitySeriesPoint[]): EquitySeriesPoint[] {
  return points.filter((point) => Number.isFinite(point.value));
}

/** Geometry of one series inside the box, shared by the SVG and the summary. */
function seriesSummary(series: EquitySeries) {
  const points = finitePoints(series.points);
  const first = points.length > 0 ? points[0] : null;
  const last = points.length > 0 ? points[points.length - 1] : null;
  const delta = first !== null && last !== null ? last.value - first.value : 0;
  const ratio =
    first !== null && first.value !== 0 ? delta / Math.abs(first.value) : 0;
  return {
    points,
    first,
    last,
    delta,
    ratio,
    declining: delta < 0,
    glyph: directionGlyph(delta),
  };
}

/**
 * Hand-rolled SVG equity chart - no charting dependency.
 *
 * Every series is distinguishable without colour: a declining series is dashed
 * and carries the `▼` glyph in the legend, a rising one is solid with `▲`. The
 * chart is always paired with a real `<table>` summarising each series, so the
 * numbers stay readable when the SVG is not (small screen, screen reader,
 * printed page).
 */
export function EquityChart({
  series,
  height = 220,
  caption = "Equity curve",
  className,
}: EquityChartProps) {
  const summaries = series.map((entry) => ({ series: entry, ...seriesSummary(entry) }));
  const allValues = summaries.flatMap((summary) => summary.points.map((point) => point.value));
  const hasData = allValues.length >= 2;
  const min = hasData ? Math.min(...allValues) : 0;
  const max = hasData ? Math.max(...allValues) : 0;
  const span = max - min;
  const longest = summaries.reduce((length, summary) => Math.max(length, summary.points.length), 0);

  const x = (index: number) =>
    PADDING_X + (longest > 1 ? (index * (WIDTH - PADDING_X * 2)) / (longest - 1) : 0);
  const y = (value: number) => {
    const ratio = span === 0 ? 0.5 : (value - min) / span;
    return PADDING_Y + (1 - ratio) * (height - PADDING_Y * 2);
  };

  return (
    <figure className={cn("m-0 min-w-0", className)}>
      {hasData ? (
        <div className="w-full min-w-0 overflow-x-auto">
          <svg
            role="img"
            aria-label={`${caption} (${series.length} series, ${longest} points)`}
            viewBox={`0 0 ${WIDTH} ${height}`}
            width="100%"
            height={height}
            preserveAspectRatio="none"
            className="min-w-[320px]"
          >
            {Array.from({ length: GRID_LINES }, (_unused, index) => {
              const lineY = PADDING_Y + (index * (height - PADDING_Y * 2)) / (GRID_LINES - 1);
              return (
                <line
                  key={index}
                  x1={PADDING_X}
                  x2={WIDTH - PADDING_X}
                  y1={lineY}
                  y2={lineY}
                  stroke="var(--chart-grid)"
                  strokeWidth={1}
                />
              );
            })}
            {summaries.map((summary, index) => {
              if (summary.points.length < 2) {
                return null;
              }
              const path = summary.points
                .map(
                  (point, pointIndex) =>
                    `${pointIndex === 0 ? "M" : "L"}${x(pointIndex).toFixed(2)} ${y(point.value).toFixed(2)}`,
                )
                .join(" ");
              const stroke = summary.declining ? "var(--chart-bear)" : "var(--chart-bull)";
              return (
                <g key={summary.series.id} data-series={summary.series.id}>
                  {index === 0 ? (
                    <path
                      d={`${path} L${x(summary.points.length - 1).toFixed(2)} ${(height - PADDING_Y).toFixed(2)} L${PADDING_X.toFixed(2)} ${(height - PADDING_Y).toFixed(2)} Z`}
                      fill={stroke}
                      opacity={0.12}
                      stroke="none"
                    />
                  ) : null}
                  <path
                    d={path}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={1.75}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeDasharray={summary.declining ? "5 4" : undefined}
                  />
                </g>
              );
            })}
          </svg>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Not enough data to draw the equity curve.</p>
      )}

      <figcaption className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
        <span className="tabular-nums">
          {formatUsdt(min)} - {formatUsdt(max)}
        </span>
        {summaries[0]?.first && summaries[0]?.last ? (
          <span className="tabular-nums">
            {formatTimestamp(summaries[0].first.timestamp)} to{" "}
            {formatTimestamp(summaries[0].last.timestamp)}
          </span>
        ) : null}
        {summaries.map((summary) => (
          <span key={summary.series.id} className="inline-flex items-center gap-1">
            <span aria-hidden="true">{summary.glyph}</span>
            <span className="text-foreground">{summary.series.label}</span>
            <span className="tabular-nums">{formatSignedRatioPercent(summary.ratio)}</span>
          </span>
        ))}
      </figcaption>

      <table className="mt-2 w-full min-w-0 border-collapse text-sm">
        <caption className="sr-only">{caption}: series summary</caption>
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th scope="col" className="px-2 py-1 font-medium">
              Series
            </th>
            <th scope="col" className="px-2 py-1 text-right font-medium">
              First
            </th>
            <th scope="col" className="px-2 py-1 text-right font-medium">
              Last
            </th>
            <th scope="col" className="px-2 py-1 text-right font-medium">
              Change
            </th>
          </tr>
        </thead>
        <tbody>
          {summaries.map((summary) => (
            <tr key={summary.series.id} className="h-9 border-b border-border/60">
              <th scope="row" className="px-2 py-1 text-left font-normal">
                <span aria-hidden="true">{summary.glyph}</span> {summary.series.label}
              </th>
              <td className="px-2 py-1 text-right tabular-nums">
                {summary.first === null ? "—" : formatUsdt(summary.first.value)}
              </td>
              <td className="px-2 py-1 text-right tabular-nums">
                {summary.last === null ? "—" : formatUsdt(summary.last.value)}
              </td>
              <td className="px-2 py-1 text-right tabular-nums">
                {formatSignedRatioPercent(summary.ratio)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
