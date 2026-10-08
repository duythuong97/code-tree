import * as fs from 'node:fs';
import * as path from 'node:path';

export interface AgentDef {
  id: string;
  name: string;
  description: string;
  tools: string[];
  /** Skills whose full text is loaded into the prompt up front. */
  skills: string[];
  /** Agent suggested as the next step (handoff). */
  next?: string;
  nextLabel?: string;
  body: string;
  source: string;
}

export interface SkillDef {
  name: string;
  description: string;
  body: string;
  source: string;
}

/**
 * Minimal front-matter parser: `key: value`, `key: [a, b]` and
 * block lists (`key:` followed by `  - item`). Enough for agent/skill files.
 */
export function parseFrontmatter(text: string): { data: Record<string, string | string[]>; body: string } {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(text);
  if (!m) return { data: {}, body: text };
  const data: Record<string, string | string[]> = {};
  let listKey: string | undefined;
  for (const line of m[1].split(/\r?\n/)) {
    const item = /^\s+-\s+(.*)$/.exec(line);
    if (item && listKey) {
      (data[listKey] as string[]).push(unquote(item[1]));
      continue;
    }
    const kv = /^([\w-]+):\s*(.*)$/.exec(line);
    if (!kv) continue;
    const [, key, value] = kv;
    if (value === '') {
      data[key] = [];
      listKey = key;
    } else if (value.startsWith('[') && value.endsWith(']')) {
      data[key] = value.slice(1, -1).split(',').map((s) => unquote(s.trim())).filter(Boolean);
      listKey = undefined;
    } else {
      data[key] = unquote(value);
      listKey = undefined;
    }
  }
  return { data, body: m[2].trim() };
}

function unquote(s: string): string {
  return s.replace(/^['"]|['"]$/g, '');
}

const asList = (v: string | string[] | undefined) => (Array.isArray(v) ? v : v ? [v] : []);
const asStr = (v: string | string[] | undefined) => (Array.isArray(v) ? v.join(', ') : v ?? '');

export function parseAgent(text: string, source: string): AgentDef {
  const { data, body } = parseFrontmatter(text);
  const id = asStr(data.id) || path.basename(source).replace(/\.(agent\.)?md$/, '');
  return {
    id,
    name: asStr(data.name) || id,
    description: asStr(data.description),
    tools: asList(data.tools),
    skills: asList(data.skills),
    next: asStr(data.next) || undefined,
    nextLabel: asStr(data.nextLabel) || undefined,
    body,
    source,
  };
}

export function parseSkill(text: string, source: string): SkillDef {
  const { data, body } = parseFrontmatter(text);
  return {
    name: asStr(data.name) || path.basename(path.dirname(source)),
    description: asStr(data.description),
    body,
    source,
  };
}

/** Load agents from several directories; later directories override earlier ones by id. */
export function loadAgents(dirs: string[]): Map<string, AgentDef> {
  const agents = new Map<string, AgentDef>();
  for (const dir of dirs) {
    if (!fs.existsSync(dir)) continue;
    for (const f of fs.readdirSync(dir).filter((x) => x.endsWith('.md')).sort()) {
      const file = path.join(dir, f);
      const a = parseAgent(fs.readFileSync(file, 'utf8'), file);
      agents.set(a.id, a);
    }
  }
  return agents;
}

/** Load skills (<dir>/<name>/SKILL.md); later directories override earlier ones by name. */
export function loadSkills(dirs: string[]): Map<string, SkillDef> {
  const skills = new Map<string, SkillDef>();
  for (const dir of dirs) {
    if (!fs.existsSync(dir)) continue;
    for (const sub of fs.readdirSync(dir).sort()) {
      const file = path.join(dir, sub, 'SKILL.md');
      if (!fs.existsSync(file)) continue;
      const s = parseSkill(fs.readFileSync(file, 'utf8'), file);
      skills.set(s.name, s);
    }
  }
  return skills;
}
