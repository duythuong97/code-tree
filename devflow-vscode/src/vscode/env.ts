import * as path from 'node:path';
import * as vscode from 'vscode';
import { loadConfig, type DevflowConfig } from '../core/config';
import { loadAgents, loadSkills, type AgentDef, type SkillDef } from '../core/definitions';

let extensionRoot = '';

export function initEnv(context: vscode.ExtensionContext): void {
  extensionRoot = context.extensionPath;
}

export function bundledDir(...p: string[]): string {
  return path.join(extensionRoot, 'resources', ...p);
}

export function workspaceRoot(): string {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) throw new Error('Open a workspace folder first.');
  return folder.uri.fsPath;
}

export function getConfig(): { config: DevflowConfig; found: boolean } {
  const configPath = vscode.workspace.getConfiguration('devflow').get<string>('configPath', '.devflow/config.json');
  return loadConfig(workspaceRoot(), configPath);
}

/** Resolve a user/model supplied path. Relative paths are relative to the workspace root. */
export function resolveRead(p: string): string {
  return path.isAbsolute(p) ? path.normalize(p) : path.resolve(workspaceRoot(), p);
}

/** Like resolveRead, but refuses paths outside the workspace. */
export function resolveWrite(p: string): string {
  const root = workspaceRoot();
  const full = resolveRead(p);
  const rel = path.relative(root, full);
  if (rel.startsWith('..') || path.isAbsolute(rel)) {
    throw new Error(`Refusing to write outside the workspace: ${p}`);
  }
  return full;
}

export function relToRoot(full: string): string {
  return path.relative(workspaceRoot(), full).replace(/\\/g, '/');
}

/** Bundled agents/skills, overridden by the workspace's .devflow/agents and .devflow/skills. */
export function getDefinitions(): { agents: Map<string, AgentDef>; skills: Map<string, SkillDef> } {
  let wsDevflow: string | undefined;
  try {
    wsDevflow = path.join(workspaceRoot(), '.devflow');
  } catch {
    wsDevflow = undefined;
  }
  const agentDirs = [bundledDir('agents'), ...(wsDevflow ? [path.join(wsDevflow, 'agents')] : [])];
  const skillDirs = [bundledDir('skills'), ...(wsDevflow ? [path.join(wsDevflow, 'skills')] : [])];
  return { agents: loadAgents(agentDirs), skills: loadSkills(skillDirs) };
}

/** Make a ticket id safe to use as a folder name. */
export function safeName(s: string): string {
  return s.replace(/[\\/:*?"<>|]+/g, '_').trim();
}
