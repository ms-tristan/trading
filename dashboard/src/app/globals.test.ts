// @vitest-environment node

/*
 * Compiled-stylesheet guard: a named `max-w-*` / `min-w-*` may never resolve to
 * a density token.
 *
 * WHY THIS COMPILES THE REAL STYLESHEET INSTEAD OF ASSERTING IN A UNIT TEST
 *
 * jsdom has no layout engine and no Tailwind pipeline, so a React Testing
 * Library test can only see the class *names* a component renders - never the
 * value those names compute to. The defect this file pins was invisible to
 * every such test: `dashboard/src/app/globals.css` defines a density-8/10
 * `--spacing-*` scale, and in Tailwind v4 a *named* `--spacing-*` token shadows
 * the container namespace on the `max-w-<name>` / `min-w-<name>` lookup chain
 * ([`--max-width` | `--min-width`, `--spacing`, `--container`]). The built
 * stylesheet therefore carried `.max-w-2xl{max-width:var(--spacing-2xl)}`
 * (24px) and `.max-w-md{max-width:var(--spacing-md)}` (8px) while every
 * component still rendered the innocent class `max-w-2xl`. Only the emitted CSS
 * can prove which value a named width resolves to.
 *
 * This runs the project's own compiler (`@tailwindcss/postcss`) over the real
 * entry stylesheet, with the very same automatic source detection `npm run
 * build` uses and explicit `@source inline(...)` candidates on top, so a
 * contributor who writes `max-w-3xl` gets a red test naming the exact
 * declaration instead of a 32px box. No network, no server, no fixture: the
 * whole file runs in a few hundred milliseconds.
 *
 * Node environment: postcss and Tailwind are Node APIs (the project default is
 * jsdom). A future Tailwind upgrade that changes the resolution order turns
 * these tests red rather than silently re-collapsing the profile creation form
 * and both confirmation dialogs.
 */

import { readFileSync } from 'node:fs';
import { readdirSync } from 'node:fs';

import tailwindcss from '@tailwindcss/postcss';
import postcss from 'postcss';
import { describe, expect, it } from 'vitest';

const CSS_URL = new URL('./globals.css', import.meta.url);
const SOURCE = readFileSync(CSS_URL, 'utf8');

/** The dashboard sources scanned for width utilities (`dashboard/src`). */
const SRC_URL = new URL('../', import.meta.url);

/** The container scale a named Tailwind v4 width utility means. */
const EXPECTED_WIDTHS: Record<string, string> = {
  xs: '20rem',
  sm: '24rem',
  md: '28rem',
  lg: '32rem',
  xl: '36rem',
  '2xl': '42rem',
  '3xl': '48rem',
};

/**
 * Every density token declared by the stylesheet, derived from the file: a name
 * added later extends the guard without touching this test.
 */
function spacingTokenNames(css: string): string[] {
  const names = new Set<string>();
  for (const match of css.matchAll(/--spacing-([a-z0-9]+)\s*:/g)) {
    names.add(match[1]);
  }
  return [...names].sort();
}

/** Compiles the real stylesheet with the given utilities force-generated. */
async function compile(candidates: string[]): Promise<string> {
  const probe = `${SOURCE}\n@source inline("${candidates.join(' ')}");\n`;
  const { css } = await postcss([tailwindcss()]).process(probe, { from: CSS_URL.pathname });
  return css;
}

/**
 * The emitted width declarations, keyed `max-w-<name>` / `min-w-<name>`.
 * Arbitrary values (`max-w-[1440px]`) and numeric ones (`min-w-0`) are
 * deliberately not captured: only the named utilities can collide.
 */
function widthRules(css: string): Map<string, string> {
  const rules = new Map<string, string>();
  for (const match of css.matchAll(/\.(max-w|min-w)-([a-z0-9]+)\s*\{\s*([^}]*?)\s*\}/g)) {
    rules.set(`${match[1]}-${match[2]}`, match[3].replace(/\s+/g, ' ').replace(/;\s*$/, ''));
  }
  return rules;
}

describe('named width utilities', () => {
  it('keeps every named width on the container scale, never on a spacing token', async () => {
    const names = spacingTokenNames(SOURCE);
    const candidates = names.flatMap((name) => [`max-w-${name}`, `min-w-${name}`]);
    candidates.push('max-w-prose');

    const css = await compile(candidates);
    const rules = widthRules(css);

    for (const name of names) {
      expect(
        EXPECTED_WIDTHS[name],
        `--spacing-${name} is not in EXPECTED_WIDTHS: add the container value a named max-w-${name} / min-w-${name} means`,
      ).toBeDefined();
      expect(rules.get(`max-w-${name}`)).toBe(`max-width: var(--max-width-${name})`);
      expect(rules.get(`min-w-${name}`)).toBe(`min-width: var(--min-width-${name})`);
      expect(css).toContain(`--max-width-${name}: ${EXPECTED_WIDTHS[name]}`);
      expect(css).toContain(`--min-width-${name}: ${EXPECTED_WIDTHS[name]}`);
    }

    // The one named width that always resolved correctly: `prose` is not a
    // spacing name, so it keeps its literal value and must stay a literal.
    expect(rules.get('max-w-prose')).toBe('max-width: 65ch');

    // No emitted width rule in the whole stylesheet may read a density token.
    expect([...rules.values()].filter((value) => value.includes('var(--spacing'))).toEqual([]);

    // Anti-vacuity: a compiler that stopped scanning must fail loudly instead
    // of passing every assertion above on an empty rule set.
    expect(rules.has('max-w-2xl')).toBe(true);
    expect(rules.size).toBeGreaterThanOrEqual(names.length * 2);
  });

  it('checks every named width utility the dashboard actually uses', async () => {
    // `encoding: 'utf8'` is what makes this overload return relative paths
    // (`string[]`) rather than buffers.
    const sources = readdirSync(SRC_URL, { recursive: true, encoding: 'utf8' }).filter(
      (entry) => entry.endsWith('.ts') || entry.endsWith('.tsx'),
    );

    const used = new Set<string>();
    for (const entry of sources) {
      const file = readFileSync(new URL(entry, SRC_URL), 'utf8');
      for (const match of file.matchAll(/\b(max-w|min-w)-([a-z0-9]+)\b/g)) {
        used.add(match[2]);
      }
    }
    const names = [...used].sort();

    const css = await compile(names.flatMap((name) => [`max-w-${name}`, `min-w-${name}`]));
    const rules = widthRules(css);

    // The documented use sites: the profile creation form, both confirmation
    // dialogs and the empty state.
    expect(names).toEqual(expect.arrayContaining(['2xl', 'md', 'prose']));

    for (const name of names) {
      for (const utility of [`max-w-${name}`, `min-w-${name}`]) {
        const declaration = rules.get(utility);
        // A name is not necessarily valid on both axes (`min-w-prose` emits
        // nothing); only what the compiler emits is checked.
        if (declaration === undefined) {
          continue;
        }
        expect(declaration).not.toContain('var(--spacing');
      }
    }

    expect(widthRules(css).size).toBeGreaterThanOrEqual(3);
  });
});
