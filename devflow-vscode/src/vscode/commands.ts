import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { DEFAULT_CONFIG } from '../core/config';
import { SAMPLE_SCREEN_INDEX, configJson, createTemplateWorkbook, toCopilotAgentFile } from '../core/scaffold';
import { bundledDir, getConfig, getDefinitions, workspaceRoot } from './env';

function copyDir(src: string, dst: string, overwrite: boolean): number {
  let n = 0;
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) n += copyDir(s, d, overwrite);
    else if (overwrite || !fs.existsSync(d)) {
      fs.copyFileSync(s, d);
      n++;
    }
  }
  return n;
}

function writeIfMissing(file: string, content: string, created: string[]): void {
  if (fs.existsSync(file)) return;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content, 'utf8');
  created.push(file);
}

async function initWorkspace(context: vscode.ExtensionContext): Promise<void> {
  const root = workspaceRoot();
  const picks = await vscode.window.showQuickPick(
    [
      { id: 'config', label: 'Config + screen index mẫu', description: '.devflow/config.json, .devflow/screens.json', picked: true },
      { id: 'template', label: 'Template test case mẫu (.xlsx)', description: 'Bỏ qua nếu bạn dùng template có sẵn', picked: true },
      { id: 'defs', label: 'Copy agents & skills để tùy chỉnh', description: '.devflow/agents, .devflow/skills', picked: true },
      { id: 'copilot', label: 'Tạo custom agents cho Copilot Agent mode', description: '.github/agents/*.agent.md', picked: true },
    ],
    { canPickMany: true, title: 'DevFlow: Initialize workspace' },
  );
  if (!picks) return;
  const ids = new Set(picks.map((p) => p.id));
  const created: string[] = [];

  if (ids.has('config')) {
    writeIfMissing(path.join(root, '.devflow', 'config.json'), configJson(DEFAULT_CONFIG), created);
    writeIfMissing(path.join(root, '.devflow', 'screens.json'), JSON.stringify(SAMPLE_SCREEN_INDEX, null, 2) + '\n', created);
  }
  if (ids.has('template')) {
    const { config } = getConfig();
    const tpl = path.join(root, config.testcase.template);
    if (!fs.existsSync(tpl)) {
      await createTemplateWorkbook(tpl, config.testcase, config.testcase.sheet);
      created.push(tpl);
    }
  }
  if (ids.has('defs')) {
    const n =
      copyDir(bundledDir('agents'), path.join(root, '.devflow', 'agents'), false) +
      copyDir(bundledDir('skills'), path.join(root, '.devflow', 'skills'), false);
    if (n) created.push(`.devflow/agents & .devflow/skills (${n} files)`);
  }
  if (ids.has('copilot')) {
    const { agents, skills } = getDefinitions();
    const toolDefs: { name: string; toolReferenceName?: string }[] = context.extension.packageJSON.contributes.languageModelTools;
    const refNames = new Map(toolDefs.map((t) => [t.name, t.toolReferenceName ?? t.name]));
    for (const agent of agents.values()) {
      if (agent.id === 'orchestrator') continue;
      // The developer agent also needs Copilot's built-in edit/search tools.
      const extra = agent.id === 'code' ? ['edit', 'search', 'runCommands', 'problems', 'changes'] : [];
      const file = path.join(root, '.github', 'agents', `${agent.id}.agent.md`);
      writeIfMissing(file, toCopilotAgentFile(agent, skills, refNames, agents, extra), created);
    }
  }

  const rel = created.map((f) => (path.isAbsolute(f) ? path.relative(root, f) : f));
  vscode.window.showInformationMessage(
    rel.length ? `DevFlow: đã tạo ${rel.length} mục: ${rel.join(', ')}` : 'DevFlow: không có gì mới (các file đã tồn tại).',
  );
  const cfg = path.join(root, '.devflow', 'config.json');
  if (fs.existsSync(cfg)) await vscode.window.showTextDocument(vscode.Uri.file(cfg));
}

async function listAgents(): Promise<void> {
  const { agents, skills } = getDefinitions();
  const items = [
    { label: 'Agents', kind: vscode.QuickPickItemKind.Separator, source: '' },
    ...[...agents.values()].map((a) => ({ label: `@devflow /${a.id === 'orchestrator' ? '' : a.id}`, description: a.name, detail: a.description, source: a.source })),
    { label: 'Skills', kind: vscode.QuickPickItemKind.Separator, source: '' },
    ...[...skills.values()].map((s) => ({ label: s.name, detail: s.description, source: s.source })),
  ];
  const pick = await vscode.window.showQuickPick(items, { title: 'DevFlow agents & skills (chọn để mở file định nghĩa)' });
  if (pick?.source) await vscode.window.showTextDocument(vscode.Uri.file(pick.source));
}

export function registerCommands(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand('devflow.init', () => initWorkspace(context)),
    vscode.commands.registerCommand('devflow.listAgents', listAgents),
    vscode.commands.registerCommand('devflow.openConfig', async () => {
      const p = vscode.workspace.getConfiguration('devflow').get<string>('configPath', '.devflow/config.json');
      const file = path.resolve(workspaceRoot(), p);
      if (!fs.existsSync(file)) {
        const ok = await vscode.window.showWarningMessage('Chưa có config. Khởi tạo ngay?', 'Initialize');
        if (ok) await initWorkspace(context);
        return;
      }
      await vscode.window.showTextDocument(vscode.Uri.file(file));
    }),
  );
}
