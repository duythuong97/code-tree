import { execFile } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import type { SheetMapping } from '../core/config';
import {
  appendCases,
  describeMapping,
  getSheet,
  loadWorkbook,
  mappedRowsToMarkdown,
  readMappedRows,
  readRequirementMarkdown,
  readWorkbookMarkdown,
  validateCases,
  type AppendResult,
  type CaseRecord,
} from '../core/excel';
import { findScreens, loadScreenIndex, screenToMarkdown, type ScreenEntry } from '../core/screens';
import { getConfig, getDefinitions, relToRoot, resolveRead, resolveWrite, safeName, workspaceRoot } from './env';

/* eslint-disable @typescript-eslint/no-explicit-any */
type Input = Record<string, any>;

interface ToolImpl {
  /** Confirmation shown before the tool runs (for tools that modify files). */
  confirm?: (input: Input) => { title: string; message: string };
  /** Short progress text. */
  progress?: (input: Input) => string;
  run: (input: Input, token: vscode.CancellationToken) => Promise<string>;
}

const MAX_OUTPUT = 80_000;

function truncate(s: string, max = MAX_OUTPUT): string {
  return s.length > max ? `${s.slice(0, max)}\n\n…(truncated ${s.length - max} chars)` : s;
}

function str(v: unknown, name: string): string {
  if (typeof v !== 'string' || !v.trim()) throw new Error(`"${name}" is required.`);
  return v.trim();
}

function workPath(ticket: string, name?: string): string {
  const { config } = getConfig();
  const dir = path.join(config.workDir, safeName(ticket));
  if (name && (name.includes('..') || path.isAbsolute(name))) throw new Error(`Invalid file name: ${name}`);
  return resolveWrite(name ? path.join(dir, name) : dir);
}

function getScreen(screenId: string): ScreenEntry {
  const { config } = getConfig();
  const index = loadScreenIndex(workspaceRoot(), config.screenIndex);
  const s = findScreens(index, { screenId })[0];
  if (!s) throw new Error(`Screen ${screenId} is not in ${config.screenIndex}.`);
  return s;
}

function regressionTarget(screenId: string): { file: string; mapping: SheetMapping } {
  const s = getScreen(screenId);
  if (!s.regression?.file) throw new Error(`Screen ${screenId} has no regression file in the screen index.`);
  const { config } = getConfig();
  const mapping = { ...config.regression, sheet: s.regression.sheet ?? config.regression.sheet };
  return { file: resolveRead(s.regression.file), mapping };
}

function appendSummary(r: AppendResult): string {
  const lines = [
    `Saved ${r.written.length} row(s) to ${relToRoot(r.outputPath)} (sheet "${r.sheet}").`,
    ...r.written.map((w) => `- row ${w.row}: ${w.id} ${w.title}`),
  ];
  if (r.skippedDuplicates.length) lines.push(`Skipped duplicates (title already exists): ${r.skippedDuplicates.join('; ')}`);
  if (r.unknownFields.length) {
    lines.push(`⚠️ Ignored fields not in the mapping: ${r.unknownFields.join(', ')}. Use devflow_describeTestcaseTemplate to get valid field names.`);
  }
  return lines.join('\n');
}

function git(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile('git', args, { cwd: workspaceRoot(), maxBuffer: 50 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) reject(new Error(stderr || err.message));
      else resolve(stdout);
    });
  });
}

const BINARY_EXT = /\.(png|jpe?g|gif|bmp|ico|pdf|zip|gz|jar|war|ear|class|dll|exe|so|dylib|xlsx?|xlsm|docx?|pptx?|woff2?|ttf|eot|mp[34]|lock)$/i;

export const TOOLS: Record<string, ToolImpl> = {
  devflow_readRequirement: {
    progress: (i) => `Đọc requirement ${i.path}`,
    run: async (i) => {
      const { config } = getConfig();
      return truncate(await readRequirementMarkdown(resolveRead(str(i.path, 'path')), config.requirement.sheets, i.maxRowsPerSheet ?? 300));
    },
  },

  devflow_readExcel: {
    progress: (i) => `Đọc Excel ${i.path}`,
    run: async (i) => truncate(await readWorkbookMarkdown(resolveRead(str(i.path, 'path')), i.sheets, i.maxRows ?? 300)),
  },

  devflow_appendQaQuestions: {
    confirm: (i) => ({
      title: 'Ghi câu hỏi vào sheet QA',
      message: `Thêm ${i.questions?.length ?? 0} câu hỏi vào ${i.outputPath ?? `${i.path} (bản copy .qa-updated.xlsx)`}`,
    }),
    run: async (i) => {
      const { config } = getConfig();
      const src = resolveRead(str(i.path, 'path'));
      const out = i.outputPath
        ? resolveWrite(i.outputPath)
        : resolveWrite(src.replace(/(\.qa-updated)?\.xlsx$/i, '') + '.qa-updated.xlsx');
      const questions: CaseRecord[] = (i.questions ?? []).map((q: CaseRecord) => ({ status: 'Open', ...q }));
      const r = await appendCases({
        sourcePath: fs.existsSync(out) ? out : src,
        outputPath: out,
        mapping: config.requirement.qa,
        cases: questions,
        dedupeByTitle: true,
      });
      return appendSummary(r);
    },
  },

  devflow_findScreens: {
    run: async (i) => {
      const { config } = getConfig();
      const index = loadScreenIndex(workspaceRoot(), config.screenIndex);
      const codePath = i.codePath ? relToRoot(resolveRead(i.codePath)) : undefined;
      const found = findScreens(index, { screenId: i.screenId, codePath, keyword: i.keyword });
      if (!found.length) return `No screen matches ${JSON.stringify(i)}. Index has ${index.screens.length} screens.`;
      return truncate(found.map((s) => screenToMarkdown(s, index)).join('\n\n'));
    },
  },

  devflow_getScreenSpec: {
    progress: (i) => `Đọc spec màn hình ${i.screenId}`,
    run: async (i) => {
      const s = getScreen(str(i.screenId, 'screenId'));
      if (!s.spec) return `${s.id} has no spec path in the screen index.`;
      const file = resolveRead(s.spec);
      const header = screenToMarkdown(s);
      if (/\.xlsx$/i.test(file)) return truncate(`${header}\n\n${await readWorkbookMarkdown(file, undefined, i.maxRows ?? 400)}`);
      return truncate(`${header}\n\n${fs.readFileSync(file, 'utf8')}`);
    },
  },

  devflow_getRegressionTests: {
    progress: (i) => `Đọc regression test của ${i.screenId}`,
    run: async (i) => {
      const { file, mapping } = regressionTarget(str(i.screenId, 'screenId'));
      const ws = getSheet(await loadWorkbook(file), mapping.sheet);
      return truncate(`Regression file: ${relToRoot(file)} (sheet "${ws.name}")\n${mappedRowsToMarkdown(readMappedRows(ws, mapping), mapping, i.keyword)}`);
    },
  },

  devflow_describeTestcaseTemplate: {
    run: async (i) => {
      const { config } = getConfig();
      let file: string;
      let mapping: SheetMapping;
      if (i.kind === 'regression') {
        ({ file, mapping } = regressionTarget(str(i.screenId, 'screenId')));
      } else {
        file = resolveRead(config.testcase.template);
        mapping = config.testcase;
      }
      const d = await describeMapping(file, mapping);
      const example: Record<string, string | string[]> = {};
      for (const f of d.fields) {
        if (f.field === mapping.idField) continue;
        example[f.field] = f.field.toLowerCase().includes('step') ? ['...', '...'] : f.allowed[0] ?? '...';
      }
      return [
        `File: ${relToRoot(file)} · sheet "${d.sheet}" · existing rows: ${d.existingRows}`,
        `ID field: ${mapping.idField ?? '(none)'} · ID format: ${mapping.idFormat ?? '(none)'} (leave the ID empty to auto-generate)`,
        '',
        '| field | column | header | required | allowed values |',
        '|---|---|---|---|---|',
        ...d.fields.map((f) => `| ${f.field} | ${f.column} | ${f.header} | ${f.required ? 'yes' : ''} | ${f.allowed.join(', ')} |`),
        '',
        'Example case object:',
        '```json',
        JSON.stringify(example, null, 2),
        '```',
      ].join('\n');
    },
  },

  devflow_writeTestcases: {
    progress: (i) => `Ghi ${i.cases?.length ?? 0} test case vào Excel`,
    run: async (i) => {
      const { config } = getConfig();
      if (!i.outputPath && !i.ticket) throw new Error('Provide "ticket" or "outputPath".');
      const out = i.outputPath ? resolveWrite(i.outputPath) : workPath(i.ticket, `testcases_${safeName(i.ticket)}.xlsx`);
      const exists = fs.existsSync(out);
      const r = await appendCases({
        sourcePath: exists ? out : resolveRead(config.testcase.template),
        outputPath: out,
        mapping: config.testcase,
        cases: i.cases ?? [],
        vars: { screenId: i.screenId, ticket: i.ticket },
        dedupeByTitle: exists,
      });
      return appendSummary(r);
    },
  },

  devflow_appendRegressionTests: {
    confirm: (i) => ({
      title: 'Bổ sung regression test',
      message: `Thêm ${i.cases?.length ?? 0} regression case vào bộ regression của ${i.screenId}${i.outputPath ? ` (ghi ra ${i.outputPath})` : ' (cập nhật trực tiếp, có backup)'}.`,
    }),
    run: async (i) => {
      const { config } = getConfig();
      const screenId = str(i.screenId, 'screenId');
      const { file, mapping } = regressionTarget(screenId);
      let out = file;
      if (i.outputPath) {
        out = resolveWrite(i.outputPath);
      } else {
        resolveWrite(file);
        const stamp = new Date().toISOString().replace(/[:.]/g, '-');
        const backup = resolveWrite(path.join(config.workDir, '_backup', `${path.basename(file, '.xlsx')}.${stamp}.xlsx`));
        fs.mkdirSync(path.dirname(backup), { recursive: true });
        fs.copyFileSync(file, backup);
      }
      const cases: CaseRecord[] = (i.cases ?? []).map((c: CaseRecord) =>
        'sourceTicket' in mapping.columns && i.ticket && !c.sourceTicket ? { ...c, sourceTicket: i.ticket } : c,
      );
      const r = await appendCases({
        sourcePath: fs.existsSync(out) ? out : file,
        outputPath: out,
        mapping,
        cases,
        vars: { screenId, ticket: i.ticket },
        dedupeByTitle: true,
      });
      return appendSummary(r);
    },
  },

  devflow_validateTestcases: {
    run: async (i) => {
      const { config } = getConfig();
      const file = resolveRead(str(i.path, 'path'));
      const mapping = i.kind === 'regression' ? config.regression : config.testcase;
      const r = await validateCases(file, mapping);
      if (!r.issues.length) return `OK: ${r.rows} row(s), no issues.`;
      return `${r.rows} row(s), ${r.issues.length} issue(s):\n${r.issues.map((x) => `- row ${x.row}: ${x.message}`).join('\n')}`;
    },
  },

  devflow_searchCode: {
    progress: (i) => `Tìm "${i.query}"`,
    run: async (i, token) => {
      const { config } = getConfig();
      const query = str(i.query, 'query');
      const max = i.maxResults ?? 200;
      const re = i.isRegex ? new RegExp(query, 'i') : undefined;
      const needle = query.toLowerCase();
      const files = await vscode.workspace.findFiles(i.include || '**/*', config.search.exclude, 20000, token);
      const hits: string[] = [];
      let scanned = 0;
      for (const uri of files) {
        if (token.isCancellationRequested || hits.length >= max) break;
        if (BINARY_EXT.test(uri.fsPath)) continue;
        const stat = fs.statSync(uri.fsPath);
        if (stat.size > config.search.maxFileSizeKb * 1024) continue;
        scanned++;
        const lines = fs.readFileSync(uri.fsPath, 'utf8').split(/\r?\n/);
        for (let n = 0; n < lines.length && hits.length < max; n++) {
          const line = lines[n];
          if (re ? re.test(line) : line.toLowerCase().includes(needle)) {
            hits.push(`${relToRoot(uri.fsPath)}:${n + 1}: ${line.trim().slice(0, 200)}`);
          }
        }
      }
      const head = `${hits.length} match(es) in ${scanned} file(s) scanned${hits.length >= max ? ' (limit reached, refine the query or include glob)' : ''}.`;
      return truncate([head, ...hits].join('\n'));
    },
  },

  devflow_listFiles: {
    run: async (i, token) => {
      const { config } = getConfig();
      const files = await vscode.workspace.findFiles(str(i.glob, 'glob'), config.search.exclude, i.max ?? 300, token);
      return files.length ? files.map((f) => relToRoot(f.fsPath)).sort().join('\n') : 'No files.';
    },
  },

  devflow_readFile: {
    run: async (i) => {
      const file = resolveRead(str(i.path, 'path'));
      if (/\.xlsx$/i.test(file)) return truncate(await readWorkbookMarkdown(file));
      const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/);
      const start = Math.max(1, i.startLine ?? 1);
      const end = Math.min(lines.length, i.endLine ?? start + 1999);
      const body = lines.slice(start - 1, end).map((l, k) => `${start + k}\t${l}`).join('\n');
      return truncate(`${relToRoot(file)} (lines ${start}-${end} of ${lines.length})\n${body}`);
    },
  },

  devflow_gitDiff: {
    run: async (i) => {
      const args = ['diff'];
      if (i.staged) args.push('--staged');
      if (i.stat) args.push('--stat');
      if (i.base) args.push(String(i.base));
      if (Array.isArray(i.paths) && i.paths.length) args.push('--', ...i.paths.map(String));
      const out = await git(args);
      let untracked = '';
      if (!i.base && !i.staged) {
        const u = (await git(['ls-files', '--others', '--exclude-standard'])).trim();
        if (u) untracked = `\n\nUntracked files:\n${u}`;
      }
      return truncate((out.trim() || 'No changes.') + untracked);
    },
  },

  devflow_writeWorkFile: {
    run: async (i) => {
      const file = workPath(str(i.ticket, 'ticket'), str(i.name, 'name'));
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, String(i.content ?? ''), 'utf8');
      return `Saved ${relToRoot(file)}`;
    },
  },

  devflow_readWorkFile: {
    run: async (i) => {
      const ticket = str(i.ticket, 'ticket');
      if (!i.name) {
        const dir = workPath(ticket);
        if (!fs.existsSync(dir)) return `No work files yet for ${ticket} (${relToRoot(dir)}).`;
        return `Files in ${relToRoot(dir)}:\n${fs.readdirSync(dir).sort().map((f) => `- ${f}`).join('\n')}`;
      }
      const file = workPath(ticket, i.name);
      if (!fs.existsSync(file)) return `Not found: ${relToRoot(file)}`;
      if (/\.xlsx$/i.test(file)) return truncate(await readWorkbookMarkdown(file));
      return truncate(fs.readFileSync(file, 'utf8'));
    },
  },

  devflow_createFile: {
    confirm: (i) => ({ title: 'Tạo file', message: `Tạo file mới: ${i.path}` }),
    run: async (i) => {
      const file = resolveWrite(str(i.path, 'path'));
      if (fs.existsSync(file)) throw new Error(`${i.path} already exists. Use devflow_replaceInFile.`);
      await vscode.workspace.fs.writeFile(vscode.Uri.file(file), Buffer.from(String(i.content ?? ''), 'utf8'));
      return `Created ${relToRoot(file)}`;
    },
  },

  devflow_replaceInFile: {
    confirm: (i) => ({ title: 'Sửa file', message: `Sửa ${i.path}:\n\n\`\`\`\n${String(i.newText ?? '').slice(0, 1500)}\n\`\`\`` }),
    run: async (i) => {
      const file = resolveWrite(str(i.path, 'path'));
      const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
      const text = doc.getText();
      const oldText = String(i.oldText ?? '');
      const at = text.indexOf(oldText);
      if (!oldText || at < 0) throw new Error('oldText not found. Read the file again and copy the exact text.');
      if (text.indexOf(oldText, at + 1) >= 0) throw new Error('oldText occurs more than once. Include more surrounding lines.');
      const edit = new vscode.WorkspaceEdit();
      edit.replace(doc.uri, new vscode.Range(doc.positionAt(at), doc.positionAt(at + oldText.length)), String(i.newText ?? ''));
      if (!(await vscode.workspace.applyEdit(edit))) throw new Error('Edit was rejected.');
      await doc.save();
      return `Edited ${relToRoot(file)}`;
    },
  },

  devflow_loadSkill: {
    run: async (i) => {
      const { skills } = getDefinitions();
      const s = skills.get(str(i.name, 'name'));
      if (!s) return `Unknown skill "${i.name}". Available: ${[...skills.keys()].join(', ')}`;
      return `# Skill: ${s.name}\n\n${s.body}`;
    },
  },
};

export function registerTools(context: vscode.ExtensionContext): void {
  for (const [name, impl] of Object.entries(TOOLS)) {
    const tool: vscode.LanguageModelTool<Input> = {
      prepareInvocation(options) {
        const input = options.input ?? {};
        return {
          invocationMessage: impl.progress?.(input) ?? name.replace('devflow_', 'DevFlow: '),
          confirmationMessages: impl.confirm
            ? (({ title, message }) => ({ title, message: new vscode.MarkdownString(message) }))(impl.confirm(input))
            : undefined,
        };
      },
      async invoke(options, token) {
        let text: string;
        try {
          text = await impl.run(options.input ?? {}, token);
        } catch (e) {
          // Return errors as text so the model can correct its input and retry.
          text = `ERROR: ${e instanceof Error ? e.message : String(e)}`;
        }
        return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(text)]);
      },
    };
    context.subscriptions.push(vscode.lm.registerTool(name, tool));
  }
}
