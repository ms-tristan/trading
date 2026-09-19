'use client';

/**
 * Creation form of a new trading profile.
 *
 * Every picker is fed by `GET /api/catalog`, never by a list hard-coded here:
 * the assets come from the exchange catalogue, the strategies from the Python
 * strategy registry, the timeframes from the supported set. The two long lists
 * (assets, strategies) are searchable comboboxes; timeframe and mode stay small
 * native controls because they have a handful of values.
 *
 * Rules implemented here:
 *
 * * the whole form is validated before any request leaves the browser, and every
 *   message is rendered next to the field it belongs to (`aria-invalid` +
 *   `aria-describedby`) — the server still validates, and its refusal is shown
 *   verbatim as the detail of the mapped failure banner;
 * * the profile id `new` is refused on purpose: `/profiles/new` is the route of
 *   this form, so a profile named `new` would have no reachable detail page;
 * * the submit button is disabled and `aria-busy` while the request is in
 *   flight, the entered values are kept on failure and the operator is navigated
 *   back to the overview on success (which polls, so the new profile appears
 *   without a manual reload);
 * * the operator token is read from `sessionStorage` when the caller has none,
 *   is sent as the `X-Operator-Token` header, and is never rendered nor logged;
 * * the token can be saved from this very page through the shared
 *   {@link OperatorTokenForm} (the kill-switch panel renders the same component),
 *   so reaching the surface that stores the token never depends on another page;
 * * a creation refused with a 403 is not explained by the raw refusal alone: the
 *   banner keeps the mapped headline and the server text verbatim, and the form
 *   adds what the operator must do about it.
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import type { FormEvent, JSX } from 'react';

import { useRouter } from 'next/navigation';

import { OperatorTokenForm } from '@/components/kill-switch/operator-token-form';
import { Button } from '@/components/ui/button';
import { ErrorBanner } from '@/components/ui/error-banner';
import { ApiError, createProfile } from '@/lib/api';
import { failureReport } from '@/lib/api-failure';
import { cn } from '@/lib/cn';
import { readOperatorToken } from '@/lib/operator-token';
import type { CatalogPayload, CreateProfileBody, RunMode } from '@/lib/types';

import { ProfileCombobox, type ProfileComboboxOption } from './profile-combobox';

/** Props of {@link CreateProfileForm}. */
export interface CreateProfileFormProps {
  /** Payload of `GET /api/catalog`: every picker is built from it. */
  catalog: CatalogPayload;
  /** Operator token; an empty string falls back to `sessionStorage`. */
  operatorToken: string;
  /** Absolute origin used by Server Components; empty means same origin. */
  baseUrl?: string;
  /** Fetch implementation seam for tests. Defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
}

/** Profile id accepted by the server. */
export const PROFILE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/** Profile id reserved by the `/profiles/new` route of the dashboard. */
export const RESERVED_PROFILE_ID = 'new';

/** Timeframe selected when the catalogue offers it. */
export const DEFAULT_TIMEFRAME = '1h';

/** Initial balance filled in by default (optional field). */
export const DEFAULT_INITIAL_BALANCE = '10000';

/** Modes offered when the catalogue carries none. */
const FALLBACK_MODES: RunMode[] = ['paper', 'live'];

/** Validation messages of the form, rendered next to their field. */
export const CREATE_PROFILE_MESSAGES = {
  profileIdRequired: 'Profile id is required.',
  profileIdInvalid: 'Profile id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$.',
  profileIdReserved: 'Profile id is reserved.',
  symbolRequired: 'Asset is required.',
  strategyRequired: 'Strategy is required.',
  timeframeRequired: 'Timeframe is required.',
  balanceInvalid: 'Initial balance must be greater than 0.',
  /**
   * Guidance added to the mapped banner when the server refuses the creation
   * with a 403 because no valid operator token was sent. It tells the operator
   * what to do instead of leaving them with the raw refusal.
   */
  tokenRefused:
    'The server refused the request: no valid operator token was sent. Save the operator token in the field above, or from the kill switch on the overview page, then submit again.',
} as const;

/** Operator-facing hint rendered under the token form of this page. */
export const CREATE_PROFILE_TOKEN_HINT =
  'Needed to create a profile: this page and the kill switch on the overview page save the same token.';

/** Whether `failure` is the documented 403 refusal of the operator token. */
function isOperatorTokenRefusal(failure: unknown): boolean {
  return failure instanceof ApiError && failure.status === 403;
}

/** Field names of the form. */
export type CreateProfileField =
  | 'profileId'
  | 'symbol'
  | 'strategy'
  | 'timeframe'
  | 'balance';

/** Validation state of every field (a message, or `null`). */
export type CreateProfileErrors = Record<CreateProfileField, string | null>;

const NO_ERRORS: CreateProfileErrors = {
  profileId: null,
  symbol: null,
  strategy: null,
  timeframe: null,
  balance: null,
};

/** DOM id of every field, used for the labels, the errors and the focus. */
const FIELD_IDS: Record<CreateProfileField, string> = {
  profileId: 'create-profile-id',
  symbol: 'create-profile-asset',
  strategy: 'create-profile-strategy',
  timeframe: 'create-profile-timeframe',
  balance: 'create-profile-balance',
};

const INPUT_CLASSES = cn(
  'w-full rounded-button border bg-background px-lg py-md text-sm text-foreground',
  'placeholder:text-muted-foreground',
  'motion-safe:transition-colors motion-safe:duration-200',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
  'disabled:cursor-not-allowed disabled:opacity-50',
);

/**
 * Parse the optional balance field.
 *
 * Returns `undefined` for an empty field (the server applies its own default),
 * the parsed number when it is a finite value greater than zero, and `null` when
 * the operator typed something the contract cannot accept.
 */
export function parseInitialBalance(raw: string): number | undefined | null {
  const trimmed = raw.trim();
  if (trimmed === '') {
    return undefined;
  }
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null;
  }
  return parsed;
}

/** The timeframe selected on first render (the default, when it is offered). */
function initialTimeframe(timeframes: string[]): string {
  if (timeframes.includes(DEFAULT_TIMEFRAME)) {
    return DEFAULT_TIMEFRAME;
  }
  return timeframes.length > 0 ? (timeframes[0] ?? '') : '';
}

/** One line of validation over the current field values. */
function validate(values: {
  profileId: string;
  symbol: string;
  strategy: string;
  timeframe: string;
  balance: string;
}): CreateProfileErrors {
  const profileId = values.profileId.trim();
  let profileIdError: string | null = null;
  if (profileId === '') {
    profileIdError = CREATE_PROFILE_MESSAGES.profileIdRequired;
  } else if (profileId.toLowerCase() === RESERVED_PROFILE_ID) {
    profileIdError = CREATE_PROFILE_MESSAGES.profileIdReserved;
  } else if (!PROFILE_ID_PATTERN.test(profileId)) {
    profileIdError = CREATE_PROFILE_MESSAGES.profileIdInvalid;
  }

  return {
    profileId: profileIdError,
    symbol: values.symbol.trim() === '' ? CREATE_PROFILE_MESSAGES.symbolRequired : null,
    strategy: values.strategy.trim() === '' ? CREATE_PROFILE_MESSAGES.strategyRequired : null,
    timeframe: values.timeframe.trim() === '' ? CREATE_PROFILE_MESSAGES.timeframeRequired : null,
    balance: parseInitialBalance(values.balance) === null
      ? CREATE_PROFILE_MESSAGES.balanceInvalid
      : null,
  };
}

/** Whether at least one field failed validation. */
function hasErrors(errors: CreateProfileErrors): boolean {
  return Object.values(errors).some((message) => message !== null);
}

/** Move the focus to the first invalid field of `order`. */
function focusFirstInvalidField(errors: CreateProfileErrors): void {
  const order: CreateProfileField[] = ['profileId', 'symbol', 'strategy', 'timeframe', 'balance'];
  const first = order.find((field) => errors[field] !== null);
  if (first === undefined || typeof document === 'undefined') {
    return;
  }
  const node = document.getElementById(FIELD_IDS[first]);
  if (node !== null && typeof node.focus === 'function') {
    node.focus();
  }
}

/** Small labelled error line of one field. */
function FieldError({ field, message }: { field: CreateProfileField; message: string | null }) {
  if (message === null) {
    return null;
  }
  return (
    <p id={`${FIELD_IDS[field]}-error`} className="text-xs text-loss">
      {message}
    </p>
  );
}

/** Searchable creation form of one trading profile. */
export function CreateProfileForm({
  catalog,
  operatorToken,
  baseUrl,
  fetchImpl,
}: CreateProfileFormProps): JSX.Element {
  const router = useRouter();

  const [profileId, setProfileId] = useState<string>('');
  const [symbol, setSymbol] = useState<string>('');
  const [strategy, setStrategy] = useState<string>('');
  const [timeframe, setTimeframe] = useState<string>(() => initialTimeframe(catalog.timeframes));
  const [mode, setMode] = useState<RunMode>('paper');
  const [balance, setBalance] = useState<string>(DEFAULT_INITIAL_BALANCE);
  const [errors, setErrors] = useState<CreateProfileErrors>(NO_ERRORS);
  const [pending, setPending] = useState<boolean>(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<unknown | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);

  const assetOptions = useMemo<ProfileComboboxOption[]>(
    () =>
      catalog.symbols.map((entry) => ({
        value: entry.symbol,
        label: entry.symbol,
        hint: `${entry.base} / ${entry.quote}`,
      })),
    [catalog.symbols],
  );

  const strategyOptions = useMemo<ProfileComboboxOption[]>(
    () => catalog.strategies.map((name) => ({ value: name, label: name })),
    [catalog.strategies],
  );

  const modes = useMemo<RunMode[]>(
    () => (catalog.modes.length > 0 ? catalog.modes : FALLBACK_MODES),
    [catalog.modes],
  );

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (pending) {
        return;
      }

      const values = { profileId, symbol, strategy, timeframe, balance };
      const nextErrors = validate(values);
      setErrors(nextErrors);
      setNotice(null);
      setFailure(null);

      if (hasErrors(nextErrors)) {
        focusFirstInvalidField(nextErrors);
        return;
      }

      const body: CreateProfileBody = {
        profile_id: profileId.trim(),
        symbol: symbol.trim(),
        timeframe: timeframe.trim(),
        strategy: strategy.trim(),
        mode,
      };
      const initialBalance = parseInitialBalance(balance);
      if (typeof initialBalance === 'number') {
        body.initial_balance = initialBalance;
      }

      // The caller may carry the token; the dashboard stores it in sessionStorage.
      const storedToken = operatorToken.trim() === '' ? readOperatorToken() : operatorToken.trim();
      const token = storedToken ?? '';

      setPending(true);
      try {
        await createProfile(body, { operatorToken: token, baseUrl, fetchImpl });
        setNotice(`Profile ${body.profile_id} created.`);
        router.push('/');
      } catch (thrown) {
        // A refused creation speaks the shared operator-facing copy; the raw
        // cause (status, server text, requested path) stays as the detail, and
        // every entered value is kept.
        setFailure(thrown);
      } finally {
        setPending(false);
      }
    },
    [
      balance,
      baseUrl,
      fetchImpl,
      mode,
      operatorToken,
      pending,
      profileId,
      router,
      strategy,
      symbol,
      timeframe,
    ],
  );

  /*
   * A failed creation is never explained by a raw status: the shared mapping
   * produces the headline the operator reads and keeps the underlying cause —
   * the server's own refusal text included — as the detail line.
   */
  const report = failure === null ? null : failureReport(failure);

  // A 403 means the request carried no valid operator token: the form says so
  // and points at the two surfaces that store one.
  const refusedToken = isOperatorTokenRefusal(failure);

  return (
    <form
      ref={formRef}
      onSubmit={(event) => {
        void handleSubmit(event);
      }}
      noValidate
      className="flex w-full max-w-2xl flex-col gap-xl"
    >
      {report === null ? null : <ErrorBanner message={report.headline} detail={report.detail} />}

      {/*
        The refusal is never replaced by the guidance: the banner above keeps the
        mapped headline and the server text verbatim, and this line only adds
        what the operator can do about a missing or invalid token.
      */}
      {refusedToken ? (
        <p
          role="status"
          aria-live="polite"
          className="rounded-card border border-loss/40 bg-loss/10 px-lg py-md text-sm text-foreground"
        >
          {CREATE_PROFILE_MESSAGES.tokenRefused}
        </p>
      ) : null}

      {notice === null ? null : (
        <p
          role="status"
          aria-live="polite"
          className="rounded-card border border-accent/40 bg-accent/10 px-lg py-md text-sm text-foreground"
        >
          {notice}
        </p>
      )}

      {/*
        The token is saved before the fields are filled, on the very page the
        operator arrived on, through the component the kill switch renders too.
      */}
      <OperatorTokenForm
        hint={CREATE_PROFILE_TOKEN_HINT}
        className="rounded-card border border-border bg-card p-lg"
      />

      <div className="flex flex-col gap-sm">
        <label htmlFor={FIELD_IDS.profileId} className="text-xs font-medium text-muted-foreground">
          Profile id
        </label>
        <input
          id={FIELD_IDS.profileId}
          type="text"
          value={profileId}
          required
          autoComplete="off"
          spellCheck={false}
          aria-invalid={errors.profileId === null ? undefined : true}
          aria-describedby={errors.profileId === null ? undefined : `${FIELD_IDS.profileId}-error`}
          onChange={(event) => {
            setProfileId(event.target.value);
          }}
          className={cn(INPUT_CLASSES, errors.profileId === null ? 'border-border' : 'border-loss')}
        />
        <FieldError field="profileId" message={errors.profileId} />
      </div>

      <ProfileCombobox
        id={FIELD_IDS.symbol}
        label="Asset"
        options={assetOptions}
        value={symbol}
        onChange={setSymbol}
        placeholder="BTC/USDT"
        emptyMessage="No asset matches. Try another spelling, for example BTC."
        error={errors.symbol}
      />

      <ProfileCombobox
        id={FIELD_IDS.strategy}
        label="Strategy"
        options={strategyOptions}
        value={strategy}
        onChange={setStrategy}
        placeholder="basic"
        emptyMessage="No strategy matches. Try another spelling, for example basic."
        error={errors.strategy}
      />

      <div className="flex flex-col gap-sm">
        <label
          htmlFor={FIELD_IDS.timeframe}
          className="text-xs font-medium text-muted-foreground"
        >
          Timeframe
        </label>
        <select
          id={FIELD_IDS.timeframe}
          value={timeframe}
          required
          aria-invalid={errors.timeframe === null ? undefined : true}
          aria-describedby={
            errors.timeframe === null ? undefined : `${FIELD_IDS.timeframe}-error`
          }
          onChange={(event) => {
            setTimeframe(event.target.value);
          }}
          className={cn(
            INPUT_CLASSES,
            'cursor-pointer',
            errors.timeframe === null ? 'border-border' : 'border-loss',
          )}
        >
          {catalog.timeframes.map((entry) => (
            <option key={entry} value={entry}>
              {entry}
            </option>
          ))}
        </select>
        <FieldError field="timeframe" message={errors.timeframe} />
      </div>

      <fieldset className="flex flex-col gap-sm">
        <legend className="text-xs font-medium text-muted-foreground">Mode</legend>
        <div className="flex flex-wrap gap-lg">
          {modes.map((entry) => (
            <label
              key={entry}
              htmlFor={`create-profile-mode-${entry}`}
              className="inline-flex cursor-pointer items-center gap-sm text-sm text-foreground"
            >
              <input
                id={`create-profile-mode-${entry}`}
                type="radio"
                name="create-profile-mode"
                value={entry}
                checked={mode === entry}
                onChange={() => {
                  setMode(entry);
                }}
                className="size-4 cursor-pointer accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              />
              <span>{entry}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="flex flex-col gap-sm">
        <label htmlFor={FIELD_IDS.balance} className="text-xs font-medium text-muted-foreground">
          Initial balance
        </label>
        <input
          id={FIELD_IDS.balance}
          type="number"
          inputMode="decimal"
          min="0"
          step="any"
          value={balance}
          aria-invalid={errors.balance === null ? undefined : true}
          aria-describedby={errors.balance === null ? undefined : `${FIELD_IDS.balance}-error`}
          onChange={(event) => {
            setBalance(event.target.value);
          }}
          className={cn(INPUT_CLASSES, errors.balance === null ? 'border-border' : 'border-loss')}
        />
        <FieldError field="balance" message={errors.balance} />
      </div>

      <div className="flex flex-wrap items-center gap-lg">
        <Button
          type="submit"
          variant="primary"
          disabled={pending}
          aria-busy={pending}
          className="cursor-pointer"
        >
          {pending ? 'Creating...' : 'Create profile'}
        </Button>
      </div>
    </form>
  );
}
