import { describe, expect, it } from 'vitest';

import { cn } from './cn';

describe('cn', () => {
  it('joins class names', () => {
    expect(cn('a', 'b')).toBe('a b');
  });

  it('skips falsy inputs', () => {
    expect(cn('a', false, null, undefined, '', 'b')).toBe('a b');
  });

  it('accepts arrays and objects through clsx', () => {
    expect(cn(['a', 'b'], { c: true, d: false })).toBe('a b c');
  });

  it('lets a later Tailwind utility win over an earlier one', () => {
    // tailwind-merge knows the default Tailwind scale. The design-system tokens
    // (p-md, p-xl, ...) are custom theme values, so they are carried through
    // untouched and CSS source order decides between them.
    expect(cn('p-2', 'p-4')).toBe('p-4');
    expect(cn('text-foreground', 'text-loss')).toBe('text-loss');
    expect(cn('p-md', 'p-xl')).toBe('p-md p-xl');
  });
});
