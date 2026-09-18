import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Join class names, with later Tailwind utilities winning over earlier ones
 * (`cn('p-md', 'p-xl')` -> `'p-xl'`).
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
