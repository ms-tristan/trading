/**
 * TypeScript mirrors of the frozen JSON contract of the Python monitoring
 * server (`src/trading_platform/web/routes.py`, layer 7).
 *
 * The shapes here are a faithful transcription of the payloads the server
 * emits: field names, enum values and nullability are identical, because the
 * dashboard is a pure consumer of that contract and never redefines it. Every
 * optional number is `number | null` exactly where the server maps a non-finite
 * value (or a missing column) to `null`.
 */

/** Lifecycle status of a single profile. */
export type ProfileStatus = 'starting' | 'running' | 'degraded' | 'halted' | 'stopped' | 'error';

/** Whether a profile trades on a simulated or on a real venue. */
export type RunMode = 'paper' | 'live';

/** Side of an order. */
export type OrderSide = 'buy' | 'sell';

/** Order type supported by the gateway. */
export type OrderType = 'market' | 'limit';

/** Persisted state of an order. */
export type OrderState =
  | 'pending'
  | 'submitted'
  | 'partially_filled'
  | 'filled'
  | 'cancelled'
  | 'rejected';

/** Direction of a position or of a closed trade. */
export type Direction = 'long' | 'short';

/** Reason a round-trip trade was closed. */
export type ExitReason =
  | 'stop_loss'
  | 'take_profit'
  | 'signal'
  | 'end_of_data'
  | 'max_duration';

/** In-process counters of a profile engine (`EngineCounters.to_dict()`). */
export interface EngineCounters {
  candles_processed: number;
  orders_submitted: number;
  orders_filled: number;
  orders_rejected: number;
  stream_reconnects: number;
  risk_rejections: number;
  errors: number;
}

/** Health block of a profile snapshot. */
export interface ProfileHealth {
  profile_id: string;
  status: ProfileStatus;
  last_candle_at: string | null;
  lag_seconds: number | null;
  last_error: string | null;
  reconnect_count: number;
  counters: EngineCounters;
}

/** One profile as `GET /api/profiles` and `GET /api/profiles/{id}` emit it. */
export interface ProfileSnapshot {
  profile_id: string;
  symbol: string;
  timeframe: string;
  strategy: string;
  mode: RunMode;
  status: ProfileStatus;
  initial_balance: number | null;
  equity: number | null;
  cash: number | null;
  position_value: number | null;
  total_return: number | null;
  n_trades: number;
  open_positions: number;
  health: ProfileHealth;
  started_at: string | null;
  updated_at: string | null;
}

/** Body of `GET /api/profiles`. */
export interface ProfilesPayload {
  profiles: ProfileSnapshot[];
  generated_at: string | null;
}

/** Body of `GET /api/health`. */
export interface HealthPayload {
  status: 'ok' | 'degraded';
  version: string;
  uptime_seconds: number | null;
  profiles_total: number;
  profiles_running: number;
  kill_switch: boolean;
  checked_at: string | null;
}

/** One point of a profile equity curve. */
export interface EquityPoint {
  timestamp: string;
  equity: number | null;
  cash: number | null;
  position_value: number | null;
}

/** Body of `GET /api/profiles/{id}/equity`. */
export interface EquityPayload {
  points: EquityPoint[];
}

/** One open position of a profile. */
export interface Position {
  profile_id: string;
  symbol: string;
  quantity: number | null;
  average_price: number | null;
  direction: Direction;
  opened_at: string;
  updated_at: string;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  stop_price: number | null;
}

/** Body of `GET /api/profiles/{id}/positions`. */
export interface PositionsPayload {
  positions: Position[];
}

/** One closed round-trip trade. */
export interface TradeRecord {
  entry_time: string;
  exit_time: string;
  entry_price: number;
  exit_price: number;
  size: number;
  direction: Direction;
  pnl: number;
  pnl_pct: number;
  fees: number;
  exit_reason: ExitReason;
  duration_minutes: number;
  stop_price: number | null;
  take_profit_price: number | null;
  params_id: string;
}

/** Body of `GET /api/profiles/{id}/trades`. */
export interface TradesPayload {
  trades: TradeRecord[];
  count: number;
}

/** One order as persisted by the realtime layer. */
export interface Order {
  client_order_id: string;
  profile_id: string;
  symbol: string;
  side: OrderSide;
  type: OrderType;
  quantity: number | null;
  state: OrderState;
  mode: RunMode;
  created_at: string;
  updated_at: string;
  filled_quantity: number | null;
  price: number | null;
  average_fill_price: number | null;
  broker_order_id: string | null;
  reject_reason: string;
}

/** Body of `GET /api/profiles/{id}/orders`. */
export interface OrdersPayload {
  orders: Order[];
}

/** Benchmark comparison block of the metrics payload. */
export interface BenchmarkPayload {
  variant: string;
  initial_balance: number;
  final_balance: number;
  n_periods: number;
  timeframe: string;
  metrics: Record<string, number | null>;
}

/** Body of `GET /api/profiles/{id}/metrics`. */
export interface MetricsPayload {
  metrics: Record<string, number | null>;
  benchmark: BenchmarkPayload | null;
  generated_at: string | null;
}

/** Body of `GET /api/kill-switch` and of a successful `POST /api/kill-switch`. */
export interface KillSwitchPayload {
  kill_switch: boolean;
  reason: string;
  changed_at: string | null;
}

/**
 * Everything the live overview of one profile needs, refreshed on the fast
 * polling cadence (`ProfileSnapshot`, platform health, kill-switch state).
 */
export interface ProfileLiveBundle {
  profile: ProfileSnapshot;
  health: HealthPayload;
  killSwitch: KillSwitchPayload;
}

/**
 * Everything the per-profile detail view needs: the slow-changing collections of
 * one profile. Refreshed on the detail cadence.
 */
export interface ProfileDetailBundle {
  equity: EquityPayload;
  positions: PositionsPayload;
  trades: TradesPayload;
  orders: OrdersPayload;
  metrics: MetricsPayload;
}
