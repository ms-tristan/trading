import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * The favicon is a static App Router file (`src/app/icon.svg`): Next.js serves
 * it and injects the `<link rel="icon">` itself, so nothing in the TypeScript
 * sources imports it and the contract has to be pinned on the file.
 *
 * The path is resolved from the project root (the dashboard package, which is
 * the working directory of every documented test command) rather than from
 * `import.meta.url`, which is not a `file:` URL under the jsdom environment.
 */
const svg = readFileSync(resolve(process.cwd(), 'src/app/icon.svg'), 'utf8');

describe('app icon', () => {
  it('is well-formed XML, so the browser renders it instead of a parse error', () => {
    // A substring check is not enough: XML forbids a double hyphen inside a
    // comment, so a comment quoting a CSS custom property (`--color-accent`)
    // makes the whole file unparseable while every `toContain` still passes.
    // jsdom's DOMParser reports that as a <parsererror> document.
    const document = new DOMParser().parseFromString(svg, 'image/svg+xml');

    expect(document.querySelector('parsererror')).toBeNull();
    expect(document.documentElement.tagName).toBe('svg');
  });

  it('is a self-contained SVG, like every other asset of the dashboard', () => {
    expect(svg).toMatch(/^<svg[\s>]/);
    expect(svg).toContain('xmlns="http://www.w3.org/2000/svg"');
    expect(svg).toContain('viewBox="0 0 32 32"');

    // No remote reference: the icon renders offline, exactly like the
    // self-hosted fonts and the hand-written stylesheet.
    const remote = svg.replace(/xmlns="http:\/\/www\.w3\.org\/2000\/svg"/, '');
    expect(remote).not.toMatch(/https?:\/\//);
  });

  it('draws the brand mark of the app shell', () => {
    // Same geometry as the Lucide `Activity` icon rendered next to
    // "Trading Platform" in the layout.
    expect(svg).toContain('M22 12h-4l-3 9L9 3l-3 9H2');
    expect(svg).toContain('stroke-linecap="round"');
  });

  it('uses the design-system colours instead of ad-hoc ones', () => {
    // --color-background (tile) and --color-accent (pulse), both from
    // design-system/trading-monitor/MASTER.md and src/app/globals.css.
    expect(svg).toContain('#020617');
    expect(svg).toContain('#22c55e');
  });
});
