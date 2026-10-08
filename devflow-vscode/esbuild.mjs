import * as esbuild from 'esbuild';
import { readdirSync } from 'node:fs';

const watch = process.argv.includes('--watch');
const test = process.argv.includes('--test');

const common = {
  bundle: true,
  platform: 'node',
  target: 'node20',
  format: 'cjs',
  sourcemap: true,
  external: ['vscode'],
  logLevel: 'warning',
};

if (test) {
  // Tests only exercise src/core (no vscode dependency).
  const entryPoints = readdirSync('test')
    .filter((f) => f.endsWith('.test.ts'))
    .map((f) => `test/${f}`);
  await esbuild.build({ ...common, entryPoints, outdir: 'dist-test', outExtension: { '.js': '.js' } });
} else {
  const ctx = await esbuild.context({ ...common, entryPoints: ['src/extension.ts'], outfile: 'dist/extension.js' });
  if (watch) {
    await ctx.watch();
  } else {
    await ctx.rebuild();
    await ctx.dispose();
  }
}
