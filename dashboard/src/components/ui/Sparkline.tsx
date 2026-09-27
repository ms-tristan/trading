import { cn } from "@/lib/cn";
import { directionGlyph, formatSignedRatioPercent } from "@/lib/format";

export interface SparklineProps {
  /** Tiny series, oldest first. */
  values: number[];
  /** Accessible name of the series, announced before the summary. */
  label: string;
  width?: number;
  height?: number;
  /** Visible summary; computed from the series when omitted. */
  summary?: string;
  className?: string;
}

const PADDING = 2;

/**
 * Build the SVG path of a tiny series inside a `width` x `height` box.
 *
 * A flat series is drawn on the middle line instead of dividing by zero.
 */
export function buildSparklinePath(
  values: number[],
  width: number,
  height: number,
  padding = PADDING,
): string {
  const usable = values.filter((value) => Number.isFinite(value));
  if (usable.length < 2) {
    return "";
  }

  const min = Math.min(...usable);
  const max = Math.max(...usable);
  const span = max - min;
  const usableWidth = Math.max(width - padding * 2, 0);
  const usableHeight = Math.max(height - padding * 2, 0);
  const step = usableWidth / (usable.length - 1);

  return usable
    .map((value, index) => {
      const x = padding + index * step;
      const ratio = span === 0 ? 0.5 : (value - min) / span;
      const y = padding + (1 - ratio) * usableHeight;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
}

/**
 * Tiny inline chart of one series.
 *
 * The SVG is announced: it carries `role="img"`, the accessible name the caller
 * passed as `label` and a `<title>` child with the same wording, so a screen
 * reader reports the series where the chart sits. The visible summary next to it
 * spells the change out for everyone else, and a series of fewer than two finite
 * points renders no chart at all: the wording is then exactly "no data".
 */
export function Sparkline({
  values,
  label,
  width = 120,
  height = 28,
  summary,
  className,
}: SparklineProps) {
  const usable = values.filter((value) => Number.isFinite(value));
  const hasSeries = usable.length >= 2;
  const first = usable.length > 0 ? usable[0] : 0;
  const last = usable.length > 0 ? usable[usable.length - 1] : 0;
  const delta = last - first;
  const ratio = first !== 0 ? delta / Math.abs(first) : 0;

  const summaryText =
    summary ?? (hasSeries ? `${directionGlyph(delta)} ${formatSignedRatioPercent(ratio)}` : "no data");
  const stroke = delta > 0 ? "var(--profit)" : delta < 0 ? "var(--loss)" : "var(--muted-foreground)";

  return (
    <span className={cn("inline-flex min-w-0 items-center gap-2", className)}>
      {hasSeries ? (
        <svg
          role="img"
          aria-label={label}
          focusable="false"
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          className="shrink-0"
        >
          <title>{label}</title>
          <path
            d={buildSparklinePath(usable, width, height)}
            fill="none"
            stroke={stroke}
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      ) : null}
      <span className="text-sm tabular-nums text-muted-foreground">
        <span className="sr-only">{label}: </span>
        {summaryText}
      </span>
    </span>
  );
}
