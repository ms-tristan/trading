/**
 * Number, timestamp and metric formatting of the dashboard.
 *
 * The locale is pinned to `en-US` on purpose: a Server Component and the browser
 * must produce the exact same string, otherwise React reports a hydration
 * mismatch. No formatter ever renders `NaN`, `Infinity`, `undefined` or `null`:
 * an absent value becomes {@link EMPTY_PLACEHOLDER} (an em dash).
 */

/** Placeholder rendered for every absent, null or non-finite value. */
export const EMPTY_PLACEHOLDER = '—';

/** Locale of every `Intl` formatter (server and client render identically). */
export const FORMAT_LOCALE = 'en-US';

/** Options shared by the numeric formatters. */
export interface DecimalOptions {
  /** Number of fraction digits rendered (fixed, not trimmed). */
  decimals?: number;
}

/** Options of {@link formatMoney} and {@link formatSignedMoney}. */
export interface MoneyOptions extends DecimalOptions {
  /** Currency symbol placed in front of the amount (`$` by default). */
  currency?: string;
}

/** Options of {@link formatRatioAsPercent}. */
export interface PercentOptions extends DecimalOptions {
  /** Render an explicit `+` sign for a positive value. */
  signed?: boolean;
}

/** Options of {@link formatQuantity}. */
export interface QuantityOptions {
  /** Maximum number of fraction digits kept (trailing zeros are trimmed). */
  decimals?: number;
}

/** The five formatting families a metric name can resolve to. */
export type MetricFormat = 'percent' | 'money' | 'ratio' | 'count' | 'duration';

/** Whether `value` is a usable, finite number. */
export function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function fractionDigits(decimals: number): { minimumFractionDigits: number; maximumFractionDigits: number } {
  const safe = Number.isInteger(decimals) && decimals >= 0 ? decimals : 0;
  return { minimumFractionDigits: safe, maximumFractionDigits: safe };
}

function plainNumber(value: number, decimals: number): string {
  return new Intl.NumberFormat(FORMAT_LOCALE, fractionDigits(decimals)).format(value);
}

/**
 * Render `value` with thousands separators and a fixed number of decimals
 * (`12345.678` -> `12,345.68`).
 */
export function formatNumber(value: number | null | undefined, options: DecimalOptions = {}): string {
  if (!isFiniteNumber(value)) {
    return EMPTY_PLACEHOLDER;
  }
  return plainNumber(value, options.decimals ?? 2);
}

/** Render `value` as a grouped integer (`12345` -> `12,345`). */
export function formatInteger(value: number | null | undefined): string {
  return formatNumber(value, { decimals: 0 });
}

/** Render `value` as an amount with a currency symbol (`-1234.5` -> `-$1,234.50`). */
export function formatMoney(value: number | null | undefined, options: MoneyOptions = {}): string {
  if (!isFiniteNumber(value)) {
    return EMPTY_PLACEHOLDER;
  }
  const currency = options.currency ?? '$';
  const sign = value < 0 ? '-' : '';
  return `${sign}${currency}${plainNumber(Math.abs(value), options.decimals ?? 2)}`;
}

/**
 * Render a profit/loss amount with an explicit sign (`12.34` -> `+$12.34`,
 * `-12.34` -> `-$12.34`). A zero amount carries no sign.
 */
export function formatSignedMoney(
  value: number | null | undefined,
  options: MoneyOptions = {},
): string {
  if (!isFiniteNumber(value)) {
    return EMPTY_PLACEHOLDER;
  }
  const rendered = formatMoney(Math.abs(value), options);
  if (value > 0) {
    return `+${rendered}`;
  }
  if (value < 0) {
    return `-${rendered}`;
  }
  return rendered;
}

/**
 * Render a fraction as a percentage (`0.1234` -> `12.34%`).
 *
 * The metrics payload of this API carries fractions (`total_return`, `pnl_pct`,
 * `max_drawdown`, `win_rate`, `exposure`, ...), so the value is multiplied by
 * 100 here — once, at the rendering boundary.
 */
export function formatRatioAsPercent(
  value: number | null | undefined,
  options: PercentOptions = {},
): string {
  if (!isFiniteNumber(value)) {
    return EMPTY_PLACEHOLDER;
  }
  const percent = value * 100;
  const rendered = `${plainNumber(Math.abs(percent), options.decimals ?? 2)}%`;
  if (percent > 0 && options.signed) {
    return `+${rendered}`;
  }
  if (percent < 0) {
    return `-${rendered}`;
  }
  return rendered;
}

/**
 * Render a quantity without noisy trailing zeros (`1.500000` -> `1.5`,
 * `2` -> `2`, `1234.5` -> `1,234.5`).
 */
export function formatQuantity(
  value: number | null | undefined,
  options: QuantityOptions = {},
): string {
  if (!isFiniteNumber(value)) {
    return EMPTY_PLACEHOLDER;
  }
  const decimals = Number.isInteger(options.decimals) && (options.decimals ?? 0) >= 0
    ? (options.decimals as number)
    : 6;
  const rendered = new Intl.NumberFormat(FORMAT_LOCALE, {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals,
  }).format(value);
  // `-0` would otherwise render as `-0`.
  return rendered === '-0' ? '0' : rendered;
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

/**
 * Render a duration given in seconds (`90061` -> `1d 01:01:01`,
 * `3661` -> `01:01:01`, `45` -> `45s`).
 *
 * Note: `max_drawdown_duration` of the metrics payload counts *candles*, not
 * seconds; it is routed here because it is the duration-shaped metric of the
 * frozen metric set.
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (!isFiniteNumber(seconds) || seconds < 0) {
    return EMPTY_PLACEHOLDER;
  }
  const total = Math.floor(seconds);
  if (total < 60) {
    return `${total}s`;
  }
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const clock = `${pad2(hours)}:${pad2(minutes)}:${pad2(secs)}`;
  return days > 0 ? `${days}d ${clock}` : clock;
}

/**
 * Render an ISO-8601 timestamp in UTC (`2024-01-01T00:00:00+00:00` ->
 * `2024-01-01 00:00:00 UTC`). An absent or unparsable value becomes
 * {@link EMPTY_PLACEHOLDER}.
 */
export function formatTimestamp(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return EMPTY_PLACEHOLDER;
  }
  const date = value instanceof Date ? value : new Date(value);
  const time = date.getTime();
  if (!Number.isFinite(time)) {
    return EMPTY_PLACEHOLDER;
  }
  const day = `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`;
  const clock = `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}:${pad2(date.getUTCSeconds())}`;
  return `${day} ${clock} UTC`;
}

/** Direction of a number: `'up'` for a positive, `'down'` for a negative, `'flat'` otherwise. */
export function trendOf(value: number | null | undefined): 'up' | 'down' | 'flat' {
  if (!isFiniteNumber(value) || value === 0) {
    return 'flat';
  }
  return value > 0 ? 'up' : 'down';
}

/**
 * Text label matching {@link trendOf}. Colour never carries the meaning alone:
 * every trend indicator pairs its colour with this label and an icon.
 */
export function trendLabel(value: number | null | undefined): string {
  const trend = trendOf(value);
  if (trend === 'up') {
    return 'Up';
  }
  if (trend === 'down') {
    return 'Down';
  }
  return 'Flat';
}

/** Metric names of the frozen metric set, grouped by the format they render with. */
export const METRIC_NAMES: Record<MetricFormat, readonly string[]> = {
  percent: [
    'total_return',
    'cagr',
    'max_drawdown',
    'volatility',
    'win_rate',
    'exposure',
    'best_trade_pct',
    'worst_trade_pct',
  ],
  money: [
    'avg_trade_pnl',
    'avg_win',
    'avg_loss',
    'largest_win',
    'largest_loss',
    'total_fees',
    'final_balance',
    'expectancy',
  ],
  count: ['n_trades', 'max_drawdown_duration'],
  duration: ['max_drawdown_duration'],
  ratio: [
    'sharpe_ratio',
    'sortino_ratio',
    'calmar_ratio',
    'profit_factor',
    'recovery_factor',
  ],
};

/** Readable label of a known metric name (`max_drawdown` -> `Max drawdown`). */
const METRIC_LABELS: Record<string, string> = {
  total_return: 'Total return',
  cagr: 'CAGR',
  sharpe_ratio: 'Sharpe ratio',
  sortino_ratio: 'Sortino ratio',
  max_drawdown: 'Max drawdown',
  max_drawdown_duration: 'Max drawdown duration',
  calmar_ratio: 'Calmar ratio',
  volatility: 'Volatility',
  win_rate: 'Win rate',
  profit_factor: 'Profit factor',
  expectancy: 'Expectancy',
  avg_trade_pnl: 'Average trade PnL',
  avg_win: 'Average win',
  avg_loss: 'Average loss',
  largest_win: 'Largest win',
  largest_loss: 'Largest loss',
  n_trades: 'Trades',
  exposure: 'Exposure',
  best_trade_pct: 'Best trade',
  worst_trade_pct: 'Worst trade',
  recovery_factor: 'Recovery factor',
  total_fees: 'Total fees',
  final_balance: 'Final balance',
};

/**
 * Turn a snake_case metric name into a readable label
 * (`max_drawdown` -> `Max drawdown`).
 */
export function humaniseMetricName(name: string): string {
  const trimmed = name.trim();
  if (trimmed === '') {
    return EMPTY_PLACEHOLDER;
  }
  const known = METRIC_LABELS[trimmed];
  if (known !== undefined) {
    return known;
  }
  const words = trimmed.replace(/[_-]+/g, ' ').trim();
  if (words === '') {
    return EMPTY_PLACEHOLDER;
  }
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Resolve the formatting family of a metric name.
 *
 * Unknown names fall back to `'ratio'`, the neutral presentation (a plain
 * number, no sign, no unit).
 */
export function metricFormatFor(name: string): MetricFormat {
  if (METRIC_NAMES.percent.includes(name)) {
    return 'percent';
  }
  if (METRIC_NAMES.money.includes(name)) {
    return 'money';
  }
  if (METRIC_NAMES.duration.includes(name)) {
    return 'duration';
  }
  if (METRIC_NAMES.count.includes(name)) {
    return 'count';
  }
  if (METRIC_NAMES.ratio.includes(name)) {
    return 'ratio';
  }
  return 'ratio';
}

/** Format `value` with the formatter matching the metric `name`. */
export function formatMetric(name: string, value: number | null | undefined): string {
  switch (metricFormatFor(name)) {
    case 'percent':
      return formatRatioAsPercent(value);
    case 'money':
      return formatMoney(value);
    case 'count':
      return formatInteger(value);
    case 'duration':
      return formatDuration(value);
    default:
      return formatNumber(value);
  }
}
