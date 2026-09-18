import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// No globals in this project (see vitest.config.ts), so React Testing Library's
// automatic cleanup is wired explicitly here.
afterEach(() => {
  cleanup();
});
