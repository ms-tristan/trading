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

/**
 * The one shared USDT wallet of the platform: the single source of truth for
 * cash, and the only thing that can fund an order.
 *
 * Every profile draws its orders from this ledger; the per-profile figures are
 * *attributed* shares of it (see {@link ProfileSnapshot}). `source` says where
 * the cash actually lives: `'local'` is the persisted paper ledger the platform
 * debits itself, `'venue'` is the account of the exchange in live mode, which
 * the platform mirrors **read-only** and never debits locally.
 */
export interface WalletSnapshot {
  /** Display name of the wallet (`platform` for the shared ledger). */
  name: string;
  /** Whether the wallet drives the simulated ledger or a real venue. */
  mode: RunMode;
  /** Balance the wallet started from; `null` when the venue does not report it. */
  initial_balance: number | null;
  /** USDT available right now to fund an order. */
  cash: number | null;
  /** Cash plus the mark-to-market value of every open position. */
  equity: number | null;
  /** Capital currently deployed in open positions, across every profile. */
  deployed: number | null;
  /** Realized profit and loss of the shared ledger. */
  realized_pnl: number | null;
  /** Unrealized profit and loss of the open positions. */
  unrealized_pnl: number | null;
  /** Total exposure of the open positions, across every profile. */
  total_exposure: number | null;
  /** How many profiles this wallet funds. */
  profiles: number;
  /** Where the cash is held: the local paper ledger or the venue account. */
  source: 'local' | 'venue';
  /** ISO-8601 stamp of the last wallet update. */
  updated_at: string | null;
}

/**
 * One profile as `GET /api/profiles` and `GET /api/profiles/{id}` emit it.
 *
 * Since the platform wallet landed, the money fields are **attributed**: `cash`
 * is the attributed share of the shared wallet the profile has left
 * (`allocation - deployed + realized_pnl`) and `equity` is that share marked to
 * market (`allocation + realized_pnl + unrealized_pnl`). The field names and
 * types are unchanged; only their meaning was refined. The attributed
 * breakdown is optional because a server that does not emit it yet stays a
 * valid producer — the dashboard then renders the em dash placeholder.
 */
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
  /**
   * Attributed share of the shared wallet: what the per-profile risk limits are
   * measured against, and what the per-profile figures are attributed to. When
   * the server does not emit it, `initial_balance` is the allocation.
   */
  allocation?: number | null;
  /** Attributed capital currently deployed in open positions. */
  deployed?: number | null;
  /** Realized profit and loss attributed to this profile. */
  realized_pnl?: number | null;
  /** Unrealized profit and loss attributed to this profile. */
  unrealized_pnl?: number | null;
  /** Reason of the last order the risk layer refused, `null` when none. */
  last_block_reason?: string | null;
}

/**
 * Body of `GET /api/profiles`.
 *
 * `wallet` is the shared platform wallet; it is optional here because a server
 * that does not emit it yet stays a valid producer, and the read routes
 * normalise that absence to an explicit `null`.
 */
export interface ProfilesPayload {
  profiles: ProfileSnapshot[];
  generated_at: string | null;
  wallet?: WalletSnapshot | null;
}

/**
 * One position the startup safety sweep found without any loaded profile and
 * closed at the venue.
 *
 * `quantity` and `price` are `null` when the venue did not report a finite
 * value: the closing order still happened, so the entry is never dropped for a
 * missing number.
 */
export interface OrphanClosure {
  /** Profile the position belonged to; it is not loaded anymore. */
  profile_id: string;
  /** Instrument that was flattened. */
  symbol: string;
  /** Size that was closed, `null` when the venue reported none. */
  quantity: number | null;
  /** Side of the closing order (`buy`/`sell`). */
  side: string;
  /** Price the position was closed at, `null` when the venue reported none. */
  price: number | null;
}

/**
 * One position the startup safety sweep could **not** close.
 *
 * It is the loud half of the report: the row is deliberately left in place
 * rather than deleted, so an unclosable exposure stays visible.
 */
export interface OrphanFailure {
  /** Profile the position belonged to; it is not loaded anymore. */
  profile_id: string;
  /** Instrument that is still open at the venue. */
  symbol: string;
  /** Size that could not be closed, `null` when it is unknown. */
  quantity: number | null;
  /** Why the closing order did not go through, verbatim. */
  error: string;
}

/**
 * Body of `orphaned_positions` in `GET /api/health` and of `GET /api/orphans`.
 *
 * The report of the startup safety sweep: `found` is how many durable positions
 * the sweep looked at, `orphaned` how many of them matched no loaded profile.
 * A `swept_at` of `null` means the platform was never swept — the dashboard
 * then renders no warning at all, because there is nothing to warn about yet.
 */
export interface OrphanReport {
  /** Durable positions the sweep looked at. */
  found: number;
  /** Positions whose `profile_id` matched no loaded profile. */
  orphaned: number;
  /** Orphans that were closed at the venue. */
  closed_count: number;
  /** Orphans that could not be closed and are still open. */
  failed_count: number;
  /** The closures, one entry per flattened position. */
  closed: OrphanClosure[];
  /** The failures, one entry per position that stayed open. */
  failed: OrphanFailure[];
  /** ISO-8601 stamp of the sweep, `null` when the platform was never swept. */
  swept_at: string | null;
}

/**
 * Body of `GET /api/health`.
 *
 * `wallet` is the shared platform wallet (same value as the one of
 * `GET /api/profiles`), optional for the same backward-compatible reason.
 *
 * `orphaned_positions` is the report of the startup safety sweep, optional for
 * that same reason: a server that does not emit the key stays a valid producer,
 * and the dashboard then renders no warning.
 */
export interface HealthPayload {
  status: 'ok' | 'degraded';
  version: string;
  uptime_seconds: number | null;
  profiles_total: number;
  profiles_running: number;
  kill_switch: boolean;
  checked_at: string | null;
  wallet?: WalletSnapshot | null;
  orphaned_positions?: OrphanReport | null;
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

// ---------------------------------------------------------------------------
// candle history, catalog and profile lifecycle (additive part of the contract)
// ---------------------------------------------------------------------------

/**
 * One persisted candle of a profile, as `GET /api/profiles/{id}/candles`
 * emits it. `closed` tells a finished candle from the still-forming one, and
 * every price is `null` when the engine never recorded a finite value.
 */
export interface Candle {
  profile_id: string;
  timestamp: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  closed: boolean;
}

/** Body of `GET /api/profiles/{id}/candles?limit=N`, oldest candle first. */
export interface CandlesPayload {
  candles: Candle[];
  count: number;
}

/** One tradable pair of the configured quote currency. */
export interface CatalogSymbol {
  symbol: string;
  base: string;
  quote: string;
}

/**
 * Body of `GET /api/catalog`: everything the creation form needs to build its
 * pickers. Every list is data-driven — the dashboard never hard-codes a symbol,
 * a strategy name or a timeframe.
 */
export interface CatalogPayload {
  symbols: CatalogSymbol[];
  strategies: string[];
  timeframes: string[];
  modes: RunMode[];
  /**
   * Subset of `strategies` whose strategy cannot be built without an offline
   * forecast artifact. The form asks for its path only for these, and the
   * server reports the rule — the dashboard never hard-codes `timesfm`.
   */
  forecast_strategies?: string[];
}

/** Runtime control state of a single profile (`GET /api/control`). */
export interface ProfileControl {
  profile_id: string;
  paused: boolean;
  running: boolean;
}

/**
 * Body of `GET /api/control`: whether the engine runs, whether the server is
 * read-only, whether mutations are accepted, and the per-profile control state.
 */
export interface ControlPayload {
  engine_running: boolean;
  read_only: boolean;
  mutable: boolean;
  profiles: ProfileControl[];
}

/** Body of a successful pause or resume call. */
export interface LifecyclePayload {
  profile: ProfileSnapshot;
  paused: boolean;
}

/**
 * Body of `GET /api/operator-token`: whether the token a request carried
 * actually authorises mutations.
 *
 * The route never answers `403` — that is the point of it: a wrong token is a
 * successful answer to the question "is this token valid?". `reason` is one of
 * the four documented strings and exists so the two failures an operator must
 * tell apart ("nothing was saved" vs "what was saved is wrong") never read the
 * same. `read_only` is present only when the server cannot authorise anything
 * at all.
 */
export interface OperatorTokenCheckPayload {
  valid: boolean;
  reason: string;
  read_only?: boolean;
}

/**
 * Body of a successful `POST /api/profiles`.
 *
 * Creation answers the started profile only: a brand new profile is never
 * paused, so the route carries no `paused` field (unlike pause/resume).
 */
export interface CreateProfilePayload {
  profile: ProfileSnapshot;
}

/** Body of a successful `DELETE /api/profiles/{id}`. */
export interface DeletePayload {
  profile_id: string;
  deleted: true;
}

/** Body of `POST /api/profiles` — the profile the operator wants to create. */
export interface CreateProfileBody {
  profile_id: string;
  symbol: string;
  timeframe: string;
  strategy: string;
  mode: RunMode;
  initial_balance?: number;
  params?: Record<string, number | string | boolean>;
  /**
   * Path of the offline forecast artifact a forecast-driven strategy consumes
   * (`timesfm`). Omitted for every other strategy; the server refuses a
   * `timesfm` profile that declares none, because building it would resolve a
   * strategy that cannot run without its artifact.
   */
  forecast?: string;
}
