import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import RootLayout from './layout';

/**
 * The root layout is a Server Component rendering the whole document, so it is
 * asserted on the server-rendered markup (no DOM nesting of `<html>` in jsdom).
 */
function renderLayout(): string {
  return renderToStaticMarkup(
    <RootLayout>
      <p>Dashboard body</p>
    </RootLayout>,
  );
}

/**
 * Class tokens of the first element matching `selector` in the rendered markup.
 *
 * The layout renders a whole document, which jsdom cannot host as a live tree,
 * so the markup is parsed standalone and its class attributes are read as plain
 * strings instead of through element matchers.
 */
function classTokens(html: string, selector: string): string[] {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  const element = parsed.querySelector(selector);
  if (element === null) {
    throw new Error(`no element matching ${selector}`);
  }
  return (element.getAttribute('class') ?? '').split(/\s+/).filter(Boolean);
}

describe('RootLayout', () => {
  it('renders an English dark-theme document', () => {
    const html = renderLayout();
    expect(html).toContain('<html lang="en">');
    expect(html).toContain('Dashboard body');
  });

  it('provides a skip link and the single main landmark', () => {
    const html = renderLayout();
    expect(html).toContain('Skip to content');
    expect(html).toContain('href="#content"');
    expect(html).toContain('id="content"');
  });

  it('renders the brand link back to the overview', () => {
    const html = renderLayout();
    expect(html).toContain('Trading Platform');
    expect(html).toContain('real-time monitor');
    expect(html).toContain('href="/"');
  });

  it('renders the documented footer', () => {
    const html = renderLayout();
    expect(html).toContain(
      'HTTP polling every 2 seconds · read-only by default · a local-network monitoring surface',
    );
  });

  it('links to the profile creation route next to the brand link', () => {
    const html = renderLayout();
    expect(html).toContain('href="/profiles/new"');
    expect(html).toContain('New profile');
    expect(html).toContain('aria-label="Profiles"');
  });

  it('acknowledges a press on the New profile action without moving it', () => {
    const classes = classTokens(renderLayout(), 'a[href="/profiles/new"]');

    expect(classes).toContain('active:border-accent');
    expect(classes).toContain('active:bg-muted-pressed');
    expect(classes).toContain('active:text-accent');
    // The pressed state is a colour change only: the action stays clickable,
    // keyboard reachable and hover-styled exactly as before.
    expect(classes).toContain('cursor-pointer');
    expect(classes).toContain('hover:text-accent');
    expect(classes).toContain('focus-visible:ring-2');
    expect(classes).toContain('motion-safe:transition-colors');
    expect(classes).toContain('motion-safe:duration-200');
  });

  it('acknowledges a press on the brand link', () => {
    const classes = classTokens(renderLayout(), 'a[href="/"]');

    expect(classes).toContain('active:text-accent');
    expect(classes).toContain('hover:text-accent');
    // The pressed shade is a background change, so the press stays perceivable
    // even though the hovered text colour is already the accent one.
    expect(classes).toContain('active:bg-muted-pressed');
    expect(classes).toContain('focus-visible:ring-2');
  });

  it('acknowledges a press on the skip link', () => {
    const classes = classTokens(renderLayout(), 'a[href="#content"]');

    expect(classes).toContain('active:bg-muted-pressed');
    // The skip link keeps its focus styling and its off-screen resting state.
    expect(classes).toContain('sr-only');
    expect(classes).toContain('focus:not-sr-only');
    expect(classes).toContain('focus-visible:ring-2');
  });
});
