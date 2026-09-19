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

  it('renders no profile creation action: it lives with the list it creates into', () => {
    const html = renderLayout();

    // The action moved into the Profiles live toolbar, next to the list it acts
    // on, so the header must not offer a second, orphaned link to that route.
    expect(html).not.toContain('href="/profiles/new"');
    expect(html).not.toContain('New profile');
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
