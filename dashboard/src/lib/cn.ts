/**
 * Minimal class-name joiner.
 *
 * The dashboard needs nothing more than "drop the falsy entries and join with a
 * space", so it carries no `clsx`/`tailwind-merge` dependency.
 */
export type ClassValue = string | false | null | undefined;

/** Join the truthy class names of `values` with a single space. */
export function cn(...values: ClassValue[]): string {
  return values.filter((value): value is string => typeof value === "string" && value !== "").join(" ");
}
