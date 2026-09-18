import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';

const config = [
  ...nextVitals,
  ...nextTs,
  {
    // eslint-plugin-react (bundled with eslint-config-next) still auto-detects
    // the React version through an ESLint API that ESLint 10 removed. Declaring
    // the version skips that detection and keeps every react/* rule working.
    settings: {
      react: {
        version: '19.3.0',
      },
    },
  },
  {
    ignores: ['.next/**', 'coverage/**', 'next-env.d.ts'],
  },
];

export default config;
