import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

/**
 * ESLint flat configuration (ESLint 9) for the dashboard.
 *
 * `eslint-config-next` ships its flat configs directly since Next 16, so no
 * `FlatCompat` bridge is needed. `.next/`, `coverage/` and the generated
 * `next-env.d.ts` are build output, never sources.
 */
const config = [
  ...nextVitals,
  ...nextTs,
  {
    settings: {
      // Declaring the React version keeps eslint-plugin-react from probing for
      // it at lint time.
      react: {
        version: "19",
      },
    },
  },
  {
    ignores: [".next/**", "coverage/**", "node_modules/**", "next-env.d.ts"],
  },
];

export default config;
