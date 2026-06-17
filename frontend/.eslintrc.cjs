/* ESLint flat config is intentionally avoided here to keep the pinned 8.57.x
 * toolchain simple; this .eslintrc is the single source of lint rules. */
module.exports = {
  root: true,
  env: { browser: true, es2022: true, node: true },
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  settings: { react: { version: 'detect' } },
  plugins: ['@typescript-eslint', 'react-hooks', 'react-refresh'],
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
    'prettier',
  ],
  ignorePatterns: [
    'dist',
    'node_modules',
    'src/api/generated',
    'playwright-report',
    'test-results',
    '*.cjs',
  ],
  rules: {
    // The api/ boundary forbids raw transport elsewhere; ban `any` per the contract.
    '@typescript-eslint/no-explicit-any': 'error',
    '@typescript-eslint/no-unused-vars': [
      'error',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
    ],
    'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
  },
};
