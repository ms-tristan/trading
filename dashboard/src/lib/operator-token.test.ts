import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  OPERATOR_TOKEN_STORAGE_KEY,
  clearOperatorToken,
  hasOperatorToken,
  readOperatorToken,
  saveOperatorToken,
} from './operator-token';

const TOKEN = 'secret-operator-token';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe('operator token storage', () => {
  it('saves and reads a token in sessionStorage', () => {
    expect(hasOperatorToken()).toBe(false);
    saveOperatorToken(TOKEN);
    expect(readOperatorToken()).toBe(TOKEN);
    expect(hasOperatorToken()).toBe(true);
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBe(TOKEN);
  });

  it('never writes to localStorage', () => {
    saveOperatorToken(TOKEN);
    expect(window.localStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
    expect(window.localStorage.length).toBe(0);
  });

  it('trims the stored value', () => {
    saveOperatorToken(`  ${TOKEN}  `);
    expect(readOperatorToken()).toBe(TOKEN);
  });

  it('removes the key when saving an empty or whitespace-only value', () => {
    saveOperatorToken(TOKEN);
    saveOperatorToken('');
    expect(readOperatorToken()).toBeNull();
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();

    saveOperatorToken(TOKEN);
    saveOperatorToken('   ');
    expect(readOperatorToken()).toBeNull();
  });

  it('treats a whitespace-only stored value as absent', () => {
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, '   ');
    expect(readOperatorToken()).toBeNull();
    expect(hasOperatorToken()).toBe(false);
  });

  it('clears the token', () => {
    saveOperatorToken(TOKEN);
    clearOperatorToken();
    expect(readOperatorToken()).toBeNull();
    expect(hasOperatorToken()).toBe(false);
  });

  it('survives a storage whose methods throw (private mode, quota)', () => {
    const throwing = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => {
        throw new Error('SecurityError');
      },
      clear: () => undefined,
      key: () => null,
      length: 0,
    } as unknown as Storage;
    const original = Object.getOwnPropertyDescriptor(window, 'sessionStorage');
    Object.defineProperty(window, 'sessionStorage', { configurable: true, get: () => throwing });

    try {
      expect(() => saveOperatorToken(TOKEN)).not.toThrow();
      expect(readOperatorToken()).toBeNull();
      expect(hasOperatorToken()).toBe(false);
      expect(() => clearOperatorToken()).not.toThrow();
    } finally {
      if (original === undefined) {
        Reflect.deleteProperty(window, 'sessionStorage');
      } else {
        Object.defineProperty(window, 'sessionStorage', original);
      }
    }
  });

  it('survives a sessionStorage accessor that throws', () => {
    const original = Object.getOwnPropertyDescriptor(window, 'sessionStorage');
    Object.defineProperty(window, 'sessionStorage', {
      configurable: true,
      get() {
        throw new Error('storage is disabled');
      },
    });
    try {
      expect(readOperatorToken()).toBeNull();
      expect(() => saveOperatorToken(TOKEN)).not.toThrow();
      expect(() => clearOperatorToken()).not.toThrow();
      expect(hasOperatorToken()).toBe(false);
    } finally {
      if (original === undefined) {
        Reflect.deleteProperty(window, 'sessionStorage');
      } else {
        Object.defineProperty(window, 'sessionStorage', original);
      }
    }
  });

  it('is inert without a window (server-side evaluation)', () => {
    vi.stubGlobal('window', undefined);
    expect(readOperatorToken()).toBeNull();
    expect(hasOperatorToken()).toBe(false);
    expect(() => saveOperatorToken(TOKEN)).not.toThrow();
    expect(() => clearOperatorToken()).not.toThrow();
  });
});
