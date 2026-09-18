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
});
