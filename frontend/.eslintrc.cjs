// Lint rules for the frontend. Run with: npm run lint (also part of scripts/verify.sh).
module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  ignorePatterns: ['dist', 'node_modules', '.eslintrc.cjs'],
  parser: '@typescript-eslint/parser',
  plugins: ['react-refresh'],
  rules: {
    'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
    // About 80 uses of `any` remain. They are type debt, not bugs, and are
    // tracked as a finding; switch this back on once they are typed.
    '@typescript-eslint/no-explicit-any': 'off',
  },
}
