import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import ExcelJS from 'exceljs';
import { DEFAULT_CONFIG, deepMerge } from '../src/core/config';
import { loadAgents, loadSkills, parseFrontmatter } from '../src/core/definitions';
import {
  appendCases,
  describeMapping,
  getSheet,
  loadWorkbook,
  nextId,
  readMappedRows,
  readRequirementMarkdown,
  validateCases,
} from '../src/core/excel';
import { buildSystemPrompt, detectTicket } from '../src/core/prompt';
import { createTemplateWorkbook, toCopilotAgentFile } from '../src/core/scaffold';
import { findScreens, globToRegExp } from '../src/core/screens';

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'devflow-test-'));
const res = path.join(__dirname, '..', 'resources');

test('nextId continues sequence per screen', () => {
  assert.equal(nextId('TC-{screenId}-{seq:3}', { screenId: 'SCR001' }, ['TC-SCR001-001', 'TC-SCR001-007', 'TC-SCR002-020']), 'TC-SCR001-008');
  assert.equal(nextId('TC-{screenId}-{seq:3}', { screenId: 'SCR003' }, ['TC-SCR001-001']), 'TC-SCR003-001');
  assert.equal(nextId('{seq:0}', {}, ['1', '2', '5']), '6');
});

test('requirement workbook is rendered with roles, merged cells and row numbers', async () => {
  const file = path.join(tmp, 'REQ-100_requirement.xlsx');
  const wb = new ExcelJS.Workbook();
  const req = wb.addWorksheet('Requirement');
  req.addRow(['Req ID', 'Screen', 'Description']);
  req.addRow(['REQ-01', 'SCR001', 'Add phone | search']);
  req.mergeCells('A4:C4');
  req.getCell('A4').value = 'Merged note';
  wb.addWorksheet('ASIS ').addRow(['Old behaviour']);
  wb.addWorksheet('tobe').addRow(['New', { formula: '1+1', result: 2 }]);
  wb.addWorksheet('Cover').addRow(['x']);
  await wb.xlsx.writeFile(file);

  const md = await readRequirementMarkdown(file, DEFAULT_CONFIG.requirement.sheets);
  assert.match(md, /role: REQUIREMENT/);
  assert.match(md, /\| 2 \| REQ-01 \| SCR001 \| Add phone \\\| search \|/);
  assert.match(md, /\| 4 \| Merged note \|  \|  \|/);
  assert.match(md, /Sheet: tobe[\s\S]*\| 1 \| New \| 2 \|/);
  assert.match(md, /Sheet for QA \("QA"\) not found/);
  assert.match(md, /Other sheets .*Cover/);
});

test('template → write test cases → append → validate', async () => {
  const cfg = DEFAULT_CONFIG.testcase;
  const tpl = path.join(tmp, 'tpl.xlsx');
  await createTemplateWorkbook(tpl, cfg, cfg.sheet);

  const desc = await describeMapping(tpl, cfg);
  const cat = desc.fields.find((f) => f.field === 'category')!;
  assert.deepEqual(cat.allowed, ['Normal', 'Abnormal', 'Boundary']);
  assert.equal(desc.fields.find((f) => f.field === 'title')!.header, 'title');

  const out = path.join(tmp, 'work', 'REQ-100', 'testcases.xlsx');
  const r1 = await appendCases({
    sourcePath: tpl,
    outputPath: out,
    mapping: cfg,
    vars: { screenId: 'SCR001', ticket: 'REQ-100' },
    cases: [
      { reqRef: 'REQ-01', category: 'Normal', title: 'Search ok', steps: ['Open', 'Click search'], expected: 'List shown', bogus: 1 },
      { reqRef: 'REQ-01', category: 'Abnormal', title: 'Too long', steps: ['Type 12 chars'], expected: 'E0012' },
    ],
  });
  assert.deepEqual(r1.written.map((w) => [w.row, w.id]), [[2, 'TC-SCR001-001'], [3, 'TC-SCR001-002']]);
  assert.deepEqual(r1.unknownFields, ['bogus']);

  const r2 = await appendCases({
    sourcePath: out,
    outputPath: out,
    mapping: cfg,
    vars: { screenId: 'SCR001' },
    dedupeByTitle: true,
    cases: [
      { category: 'Boundary', title: 'search OK', steps: ['x'], expected: 'y' },
      { category: 'Wrong', title: 'Max length', steps: ['x'], expected: '' },
    ],
  });
  assert.deepEqual(r2.skippedDuplicates, ['search OK']);
  assert.deepEqual(r2.written.map((w) => [w.row, w.id]), [[4, 'TC-SCR001-003']]);

  const ws = getSheet(await loadWorkbook(out), cfg.sheet);
  const rows = readMappedRows(ws, cfg);
  assert.equal(rows.length, 3);
  assert.equal(rows[0].values.steps, '1. Open\n2. Click search');
  assert.equal(rows[0].values.screenId, 'SCR001');
  // style and dropdown copied from the template's first data row
  assert.equal(ws.getCell('G4').alignment?.wrapText, true);
  assert.equal(ws.getCell('D4').dataValidation?.type, 'list');

  const v = await validateCases(out, cfg);
  const msgs = v.issues.map((i) => `${i.row}:${i.field}`);
  assert.deepEqual(msgs.sort(), ['4:category', '4:expected']);
});

test('dropdown values from a range on another sheet', async () => {
  const file = path.join(tmp, 'range.xlsx');
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet('TC');
  const lists = wb.addWorksheet('Lists');
  ['High', 'Low'].forEach((v, k) => (lists.getCell(`A${k + 1}`).value = v));
  ws.getCell('A1').value = 'Priority';
  ws.getCell('A2').dataValidation = { type: 'list', allowBlank: true, formulae: ['Lists!$A$1:$A$2'] };
  await wb.xlsx.writeFile(file);
  const d = await describeMapping(file, { sheet: 'TC', headerRow: 1, startRow: 2, columns: { priority: 'A' } });
  assert.deepEqual(d.fields[0].allowed, ['High', 'Low']);
});

test('screen lookup by id, code path and keyword', () => {
  const index = {
    screens: [
      { id: 'SCR001', name: 'Customer search', code: ['src/customer/search/**', 'web/search*.jsp'], tags: ['TABLE:M_CUSTOMER'] },
      { id: 'SCR002', name: 'Detail', code: ['src/customer/detail'] },
    ],
  };
  assert.equal(findScreens(index, { screenId: 'scr001' })[0].id, 'SCR001');
  assert.equal(findScreens(index, { codePath: 'src/customer/search/a/B.java' })[0].id, 'SCR001');
  assert.equal(findScreens(index, { codePath: 'web/searchList.jsp' })[0].id, 'SCR001');
  assert.equal(findScreens(index, { codePath: 'src/customer/detail/X.java' })[0].id, 'SCR002');
  assert.equal(findScreens(index, { keyword: 'm_customer' })[0].id, 'SCR001');
  assert.ok(globToRegExp('**/*.{ts,js}').test('a/b/c.js'));
});

test('config deep merge replaces columns', () => {
  const c = deepMerge(DEFAULT_CONFIG, { testcase: { columns: { id: 'B' }, startRow: 8 } });
  assert.deepEqual(c.testcase.columns, { id: 'B' });
  assert.equal(c.testcase.startRow, 8);
  assert.equal(c.testcase.template, DEFAULT_CONFIG.testcase.template);
});

test('bundled agents and skills load and reference each other', () => {
  const agents = loadAgents([path.join(res, 'agents')]);
  const skills = loadSkills([path.join(res, 'skills')]);
  assert.deepEqual([...agents.keys()].sort(), ['analyze', 'code', 'impact', 'orchestrator', 'regression', 'review', 'solution', 'testcase']);
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  const toolNames = new Set<string>(pkg.contributes.languageModelTools.map((t: { name: string }) => t.name));
  const commands = new Set<string>(pkg.contributes.chatParticipants[0].commands.map((c: { name: string }) => c.name));
  for (const a of agents.values()) {
    for (const s of a.skills) assert.ok(skills.has(s), `${a.id}: unknown skill ${s}`);
    for (const t of a.tools) assert.ok(t === 'devflow_*' || toolNames.has(t), `${a.id}: unknown tool ${t}`);
    if (a.next) assert.ok(agents.has(a.next), `${a.id}: unknown next ${a.next}`);
    if (a.id !== 'orchestrator') assert.ok(commands.has(a.id), `${a.id}: not a chat command`);
  }
  assert.equal(agents.get('analyze')!.nextLabel, '→ /impact: Phân tích impact');

  const prompt = buildSystemPrompt(agents.get('testcase')!, skills, DEFAULT_CONFIG, { ticket: 'REQ-1', configFound: true, agents: [...agents.values()] });
  assert.match(prompt, /## Skill: testcase-writing/);
  assert.match(prompt, /- impact-analysis:/);

  const refs = new Map<string, string>(pkg.contributes.languageModelTools.map((t: { name: string; toolReferenceName: string }) => [t.name, t.toolReferenceName]));
  const md = toCopilotAgentFile(agents.get('analyze')!, skills, refs, agents, []);
  assert.match(md, /^---\ndescription: '.*'\ntools: \['readRequirement'/);
  assert.match(md, /handoffs:\n  - label: '→ \/impact: Phân tích impact'\n    agent: 'impact'/);
});

test('frontmatter and ticket detection', () => {
  const { data, body } = parseFrontmatter('---\na: 1\nlist:\n  - x\n  - "y"\nb: [p, q]\n---\nBody');
  assert.deepEqual(data, { a: '1', list: ['x', 'y'], b: ['p', 'q'] });
  assert.equal(body, 'Body');
  assert.equal(detectTicket('please analyze ABC-123 now'), 'ABC-123');
  assert.equal(detectTicket('ticket: cr_2024_01'), 'cr_2024_01');
  assert.equal(detectTicket('nothing here'), undefined);
});
