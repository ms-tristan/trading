import { describe, expect, it } from 'vitest';

import * as ui from './index';

/**
 * The barrel is the import surface siblings use; this test pins every exported
 * name so a rename cannot slip through unnoticed.
 */
describe('ui barrel', () => {
  it('exports the shared components', () => {
    expect(typeof ui.AppShell).toBe('function');
    expect(typeof ui.Button).toBe('function');
    expect(typeof ui.Card).toBe('function');
    expect(typeof ui.DataTable).toBe('function');
    expect(typeof ui.EmptyState).toBe('function');
    expect(typeof ui.ErrorBanner).toBe('function');
    expect(typeof ui.LiveToolbar).toBe('function');
    expect(typeof ui.StatTile).toBe('function');
    expect(typeof ui.StatusBadge).toBe('function');
  });

  it('exports the tone helpers', () => {
    expect(typeof ui.profileStatusTone).toBe('function');
    expect(typeof ui.orderStateTone).toBe('function');
    expect(ui.profileStatusTone('running')).toBe('ok');
    expect(ui.orderStateTone('filled')).toBe('ok');
  });
});
