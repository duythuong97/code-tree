import * as fs from 'node:fs';
import * as path from 'node:path';

export interface ScreenEntry {
  id: string;
  name?: string;
  description?: string;
  /** Spec document (.md, .txt or .xlsx), workspace-relative. */
  spec?: string;
  /** Source code globs that implement the screen. */
  code?: string[];
  /** Regression workbook of the screen. */
  regression?: { file: string; sheet?: string };
  /** Screens sharing components/data with this one. */
  related?: string[];
  /** API endpoints, DB tables, batches... free text tags used for impact analysis. */
  tags?: string[];
}

export interface ScreenIndex {
  screens: ScreenEntry[];
}

export function loadScreenIndex(root: string, indexPath: string): ScreenIndex {
  const full = path.resolve(root, indexPath);
  if (!fs.existsSync(full)) {
    throw new Error(`Screen index not found: ${indexPath}. Run "DevFlow: Initialize workspace" and fill it in.`);
  }
  const data = JSON.parse(fs.readFileSync(full, 'utf8')) as ScreenIndex;
  if (!Array.isArray(data.screens)) throw new Error(`${indexPath}: expected { "screens": [...] }`);
  return data;
}

/** Convert a glob (supports **, *, ?, {a,b}) to a RegExp over forward-slash paths. */
export function globToRegExp(glob: string): RegExp {
  let re = '';
  const g = glob.replace(/\\/g, '/').replace(/^\.\//, '');
  for (let i = 0; i < g.length; i++) {
    const ch = g[i];
    if (ch === '*') {
      if (g[i + 1] === '*') {
        // "**/" matches zero or more directories
        if (g[i + 2] === '/') {
          re += '(?:.*/)?';
          i += 2;
        } else {
          re += '.*';
          i += 1;
        }
      } else {
        re += '[^/]*';
      }
    } else if (ch === '?') re += '[^/]';
    else if (ch === '{') re += '(?:';
    else if (ch === '}') re += ')';
    else if (ch === ',') re += '|';
    else re += ch.replace(/[.+^$()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${re}$`, 'i');
}

function matchesCode(entry: ScreenEntry, relPath: string): boolean {
  const p = relPath.replace(/\\/g, '/').replace(/^\.\//, '');
  return (entry.code ?? []).some((g) => {
    // A plain folder path ("src/customer") matches everything below it.
    const glob = /[*?{]/.test(g) ? g : `${g.replace(/\/$/, '')}/**`;
    return globToRegExp(glob).test(p) || globToRegExp(g).test(p);
  });
}

export function findScreens(index: ScreenIndex, q: { screenId?: string; codePath?: string; keyword?: string }): ScreenEntry[] {
  let list = index.screens;
  if (q.screenId) {
    const id = q.screenId.trim().toLowerCase();
    list = list.filter((s) => s.id.toLowerCase() === id);
  }
  if (q.codePath) {
    list = list.filter((s) => matchesCode(s, q.codePath!));
  }
  if (q.keyword) {
    const kw = q.keyword.toLowerCase();
    list = list.filter((s) =>
      [s.id, s.name, s.description, ...(s.tags ?? [])].some((v) => v?.toLowerCase().includes(kw)),
    );
  }
  return list;
}

export function screenToMarkdown(s: ScreenEntry, index?: ScreenIndex): string {
  const usedBy = index?.screens.filter((o) => o.related?.includes(s.id)).map((o) => o.id) ?? [];
  return [
    `### ${s.id}${s.name ? ` — ${s.name}` : ''}`,
    s.description ? s.description : '',
    `- Spec: ${s.spec ?? '(none)'}`,
    `- Code: ${(s.code ?? []).join(', ') || '(none)'}`,
    `- Regression: ${s.regression ? `${s.regression.file}${s.regression.sheet ? ` [${s.regression.sheet}]` : ''}` : '(none)'}`,
    `- Related: ${(s.related ?? []).join(', ') || '(none)'}${usedBy.length ? ` · Referenced by: ${usedBy.join(', ')}` : ''}`,
    s.tags?.length ? `- Tags: ${s.tags.join(', ')}` : '',
  ]
    .filter(Boolean)
    .join('\n');
}
