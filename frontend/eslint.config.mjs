import { defineConfig, globalIgnores } from 'eslint/config';
import js from '@eslint/js';
import ts from 'typescript-eslint';
import hooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
export default defineConfig([
  globalIgnores(['.next/**', 'next-env.d.ts']),
  js.configs.recommended,
  ...ts.configs.recommended,
  { files: ['**/*.{ts,tsx}'], plugins: { 'react-hooks': hooks }, rules: hooks.configs.recommended.rules },
  { files: ['public/*.js'], languageOptions: { globals: globals.serviceworker } },
  { files: ['tests/*.mjs'], languageOptions: { globals: globals.node } },
]);
