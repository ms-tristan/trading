'use client';

/**
 * Searchable single-select combobox of the profile creation form.
 *
 * It implements the WAI-ARIA 1.2 combobox pattern with a listbox popup:
 * `role="combobox"` on the input, `aria-expanded` / `aria-controls` /
 * `aria-activedescendant` kept in sync with the popup, and one `role="option"`
 * per rendered row. Two catalogue lists (assets, strategies) hold hundreds of
 * entries, so filtering is memoised, the rendered window is capped
 * ({@link DEFAULT_MAX_VISIBLE_OPTIONS}) and the operator always sees how many
 * matches exist instead of a blank panel.
 *
 * The page override of the design system forbids a blank "0 results" screen and
 * forbids requiring the full typed value before anything can be picked: the
 * first match is activated while typing, so Enter picks it immediately.
 *
 * Accessibility rules implemented here:
 *
 * * the input always shows the selected label when the list is closed and the
 *   operator is not typing, so a selected value stays readable;
 * * ArrowDown / ArrowUp move the active option (wrapping) and open the list when
 *   it is closed, Home / End jump to the first / last rendered match, Enter picks
 *   the active option, Escape closes and reverts the text, Tab closes and moves
 *   on — focus is never trapped;
 * * arrow navigation only ever activates a rendered row, so
 *   `aria-activedescendant` never points at a missing element;
 * * no match renders one explicit, non-selectable row carrying the empty
 *   message, and the listbox stays expanded;
 * * the active option is scrolled into view (guarded: jsdom and old browsers
 *   have no `scrollIntoView`).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, JSX, KeyboardEvent } from 'react';

import { Check, Search } from 'lucide-react';

import { cn } from '@/lib/cn';

/** One selectable entry of a {@link ProfileCombobox}. */
export interface ProfileComboboxOption {
  /** Value sent to the API when the entry is picked. */
  value: string;
  /** Human-readable text, shown in the field when selected and in the list. */
  label: string;
  /** Optional secondary text (for example the base and quote of a pair). */
  hint?: string;
}

/** Props of {@link ProfileCombobox}. */
export interface ProfileComboboxProps {
  /** Id of the input; the listbox and the option ids are derived from it. */
  id: string;
  /** Visible label, linked to the input and used as the listbox name. */
  label: string;
  /** Every selectable entry, in display order. */
  options: ProfileComboboxOption[];
  /** Currently selected value. */
  value: string;
  /** Called once with the value of the entry the operator picked. */
  onChange: (value: string) => void;
  /** Placeholder of the empty field. */
  placeholder?: string;
  /** Row rendered when nothing matches. */
  emptyMessage?: string;
  /** Disables the field: the list can never be opened. */
  disabled?: boolean;
  /** Validation message rendered next to the field, or `null`. */
  error?: string | null;
  /** Maximum number of rendered rows (defaults to 50). */
  maxVisibleOptions?: number;
}

/** Default cap on the number of rendered rows. */
export const DEFAULT_MAX_VISIBLE_OPTIONS = 50;

/** Default message of the empty-result row. */
export const DEFAULT_EMPTY_MESSAGE = 'No match. Try another spelling, for example BTC.';

const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background';

/**
 * Case-insensitive substring filter over the value and the label.
 *
 * Exported for the tests and for the memoised list of the component; a blank
 * query returns every option.
 */
export function filterComboboxOptions(
  options: ProfileComboboxOption[],
  query: string,
): ProfileComboboxOption[] {
  const needle = query.trim().toLowerCase();
  if (needle === '') {
    return options;
  }
  return options.filter(
    (option) =>
      option.value.toLowerCase().includes(needle) || option.label.toLowerCase().includes(needle),
  );
}

/** Searchable single-select field built on the ARIA combobox pattern. */
export function ProfileCombobox({
  id,
  label,
  options,
  value,
  onChange,
  placeholder,
  emptyMessage = DEFAULT_EMPTY_MESSAGE,
  disabled = false,
  error = null,
  maxVisibleOptions = DEFAULT_MAX_VISIBLE_OPTIONS,
}: ProfileComboboxProps): JSX.Element {
  const inputId = id;
  const listboxId = `${id}-listbox`;
  const errorId = `${id}-error`;

  const [open, setOpen] = useState<boolean>(false);
  /** Typed text; `null` means "the field shows the selected label". */
  const [query, setQuery] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState<number>(-1);

  const optionRefs = useRef<Array<HTMLLIElement | null>>([]);

  const selected = useMemo(
    () => options.find((option) => option.value === value) ?? null,
    [options, value],
  );
  /** Readable field text even when the value has no matching option. */
  const selectedLabel = selected === null ? value : selected.label;

  const matches = useMemo(() => filterComboboxOptions(options, query ?? ''), [options, query]);
  const windowSize = Math.max(1, maxVisibleOptions);
  const visible = useMemo(() => matches.slice(0, windowSize), [matches, windowSize]);
  const total = matches.length;

  const isOpen = open && !disabled;
  const activeOptionId =
    isOpen && activeIndex >= 0 && activeIndex < visible.length
      ? `${id}-option-${activeIndex}`
      : undefined;
  const errorMessage = error === null || error === '' ? null : error;

  /*
   * The active row is always a rendered row, and it is kept on screen while the
   * operator walks the list with the arrow keys. `scrollIntoView` is optional:
   * jsdom and a few embedded browsers do not implement it.
   */
  useEffect(() => {
    if (!isOpen || activeIndex < 0) {
      return;
    }
    const node = optionRefs.current[activeIndex];
    if (node !== null && node !== undefined && typeof node.scrollIntoView === 'function') {
      node.scrollIntoView({ block: 'nearest' });
    }
  }, [isOpen, activeIndex]);

  /** Open the list and activate the first match (or nothing when empty). */
  const openList = useCallback(
    (preferredIndex: number) => {
      const count = matches.length;
      setOpen(true);
      setActiveIndex(count === 0 ? -1 : Math.min(Math.max(preferredIndex, 0), count - 1));
    },
    [matches.length],
  );

  const closeList = useCallback((nextQuery: string | null) => {
    setOpen(false);
    setQuery(nextQuery);
    setActiveIndex(-1);
  }, []);

  const choose = useCallback(
    (option: ProfileComboboxOption) => {
      onChange(option.value);
      closeList(null);
    },
    [closeList, onChange],
  );

  const handleChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const nextQuery = event.target.value;
      setQuery(nextQuery);
      setOpen(true);
      setActiveIndex(filterComboboxOptions(options, nextQuery).length === 0 ? -1 : 0);
    },
    [options],
  );

  const handleClick = useCallback(() => {
    if (disabled || isOpen) {
      return;
    }
    openList(0);
  }, [disabled, isOpen, openList]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLInputElement>) => {
      if (disabled) {
        return;
      }
      switch (event.key) {
        case 'ArrowDown': {
          event.preventDefault();
          if (!isOpen) {
            openList(0);
            return;
          }
          if (visible.length === 0) {
            return;
          }
          setActiveIndex((previous) => (previous < 0 ? 0 : (previous + 1) % visible.length));
          return;
        }
        case 'ArrowUp': {
          event.preventDefault();
          if (!isOpen) {
            openList(visible.length - 1);
            return;
          }
          if (visible.length === 0) {
            return;
          }
          setActiveIndex((previous) =>
            previous <= 0 ? visible.length - 1 : previous - 1,
          );
          return;
        }
        case 'Home': {
          // Home and End keep their caret meaning while the list is closed.
          if (!isOpen) {
            return;
          }
          event.preventDefault();
          setActiveIndex(visible.length === 0 ? -1 : 0);
          return;
        }
        case 'End': {
          if (!isOpen) {
            return;
          }
          event.preventDefault();
          setActiveIndex(visible.length === 0 ? -1 : visible.length - 1);
          return;
        }
        case 'Enter': {
          const option = activeIndex >= 0 ? visible[activeIndex] : undefined;
          if (!isOpen || option === undefined) {
            // The form keeps its normal Enter behaviour.
            return;
          }
          event.preventDefault();
          choose(option);
          return;
        }
        case 'Escape': {
          if (!isOpen) {
            return;
          }
          event.preventDefault();
          // The selection is untouched: only the typed filter is dropped.
          closeList(null);
          return;
        }
        case 'Tab': {
          // Never trap the keyboard: the list closes and focus moves on.
          if (isOpen) {
            closeList(null);
          }
          return;
        }
        default:
          return;
      }
    },
    [activeIndex, choose, closeList, disabled, isOpen, openList, visible],
  );

  const handleBlur = useCallback(() => {
    // Focus left the field: the list closes and the selection is kept.
    closeList(null);
  }, [closeList]);

  const text = query ?? selectedLabel;

  return (
    <div className="flex min-w-0 flex-col gap-sm">
      <label htmlFor={inputId} className="text-xs font-medium text-muted-foreground">
        {label}
      </label>

      <div className="relative">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute left-lg top-1/2 -translate-y-1/2 text-muted-foreground"
        >
          <Search className="size-4" />
        </span>
        <input
          id={inputId}
          type="text"
          role="combobox"
          aria-expanded={isOpen}
          aria-controls={listboxId}
          aria-activedescendant={activeOptionId}
          aria-autocomplete="list"
          aria-invalid={errorMessage === null ? undefined : true}
          aria-describedby={errorMessage === null ? undefined : errorId}
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
          placeholder={placeholder}
          value={text}
          onChange={handleChange}
          onClick={handleClick}
          onKeyDown={handleKeyDown}
          onBlur={handleBlur}
          className={cn(
            'w-full rounded-button border bg-background py-md pl-3xl pr-lg text-sm text-foreground',
            'placeholder:text-muted-foreground',
            'motion-safe:transition-colors motion-safe:duration-200',
            FOCUS_RING,
            'disabled:cursor-not-allowed disabled:opacity-50',
            errorMessage === null ? 'border-border' : 'border-loss',
          )}
        />
      </div>

      {isOpen ? (
        <ul
          id={listboxId}
          role="listbox"
          aria-label={label}
          className="max-h-72 overflow-y-auto rounded-card border border-border bg-card p-sm shadow-lg"
        >
          {visible.map((option, index) => {
            const isActive = index === activeIndex;
            return (
              <li
                key={`${option.value}-${String(index)}`}
                id={`${id}-option-${String(index)}`}
                ref={(node) => {
                  optionRefs.current[index] = node;
                }}
                role="option"
                aria-selected={isActive}
                data-active={isActive ? 'true' : undefined}
                onMouseDown={(event) => {
                  // Keep the input focused so the click can complete.
                  event.preventDefault();
                }}
                onClick={() => {
                  choose(option);
                }}
                className={cn(
                  'flex cursor-pointer items-center justify-between gap-md rounded-button px-lg py-sm text-sm',
                  'motion-safe:transition-colors motion-safe:duration-200',
                  isActive ? 'bg-muted text-foreground' : 'text-foreground hover:bg-muted',
                )}
              >
                <span className="flex min-w-0 flex-col">
                  <span className="truncate">{option.label}</span>
                  {option.hint === undefined || option.hint === '' ? null : (
                    <span className="truncate text-xs text-muted-foreground">{option.hint}</span>
                  )}
                </span>
                {option.value === value ? (
                  <Check aria-hidden="true" className="size-4 shrink-0 text-accent" />
                ) : null}
              </li>
            );
          })}

          {total === 0 ? (
            <li role="presentation" className="px-lg py-md text-sm text-muted-foreground">
              {emptyMessage}
            </li>
          ) : null}
        </ul>
      ) : null}

      {isOpen && total > 0 ? (
        <p className="text-xs text-muted-foreground">{`${String(visible.length)} of ${String(total)} matches`}</p>
      ) : null}

      {errorMessage === null ? null : (
        <p id={errorId} className="text-xs text-loss">
          {errorMessage}
        </p>
      )}
    </div>
  );
}
