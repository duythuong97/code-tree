import * as fs from 'node:fs';
import * as path from 'node:path';
import ExcelJS from 'exceljs';
import type { DevflowConfig, SheetMapping } from './config';
import type { AgentDef, SkillDef } from './definitions';

export const SAMPLE_SCREEN_INDEX = {
  screens: [
    {
      id: 'SCR001',
      name: 'Customer search',
      description: 'Search customers by name, phone, status.',
      spec: 'docs/specs/SCR001_CustomerSearch.xlsx',
      code: ['src/main/java/**/customer/search/**', 'src/main/webapp/customer/search*.jsp'],
      regression: { file: 'docs/regression/SCR001_regression.xlsx', sheet: 'Regression' },
      related: ['SCR002'],
      tags: ['API:/api/customers', 'TABLE:M_CUSTOMER'],
    },
    {
      id: 'SCR002',
      name: 'Customer detail',
      spec: 'docs/specs/SCR002_CustomerDetail.md',
      code: ['src/main/java/**/customer/detail/**'],
      regression: { file: 'docs/regression/SCR002_regression.xlsx' },
      related: [],
      tags: ['TABLE:M_CUSTOMER', 'TABLE:T_CONTRACT'],
    },
  ],
};

/** Create a simple template workbook matching a mapping (header row + styled first data row with dropdowns). */
export async function createTemplateWorkbook(file: string, mapping: SheetMapping, sheetName: string): Promise<void> {
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet(sheetName || 'Sheet1');
  const border: Partial<ExcelJS.Borders> = {
    top: { style: 'thin' },
    left: { style: 'thin' },
    bottom: { style: 'thin' },
    right: { style: 'thin' },
  };
  for (const [field, letter] of Object.entries(mapping.columns)) {
    const h = ws.getCell(`${letter}${mapping.headerRow}`);
    h.value = field;
    h.font = { bold: true, color: { argb: 'FFFFFFFF' } };
    h.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF305496' } };
    h.border = border;
    h.alignment = { vertical: 'middle', horizontal: 'center', wrapText: true };
    const d = ws.getCell(`${letter}${mapping.styleRow ?? mapping.startRow}`);
    d.border = border;
    d.alignment = { vertical: 'top', wrapText: true };
    const allowed = mapping.allowed?.[field];
    if (allowed?.length) {
      d.dataValidation = { type: 'list', allowBlank: true, formulae: [`"${allowed.join(',')}"`] };
    }
    ws.getColumn(letter).width = /step|expected|precondition|title|question/i.test(field) ? 40 : 14;
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  await wb.xlsx.writeFile(file);
}

export function configJson(cfg: DevflowConfig): string {
  return JSON.stringify(cfg, null, 2) + '\n';
}

function yamlStr(s: string): string {
  return `'${s.replace(/'/g, "''")}'`;
}

/**
 * Render an agent as a GitHub Copilot custom agent (.github/agents/<id>.agent.md),
 * so the same agent can run in Copilot's native Agent mode with the extension's tools.
 */
export function toCopilotAgentFile(
  agent: AgentDef,
  skills: Map<string, SkillDef>,
  toolRefNames: Map<string, string>,
  agents: Map<string, AgentDef>,
  extraTools: string[],
): string {
  const tools = agent.tools
    .flatMap((t) => (t === 'devflow_*' ? [...toolRefNames.keys()] : [t]))
    .map((t) => toolRefNames.get(t) ?? t);
  if (!tools.includes('loadSkill')) tools.push('loadSkill');
  const fm = ['---', `description: ${yamlStr(agent.description)}`, `tools: [${[...tools, ...extraTools].map(yamlStr).join(', ')}]`];
  const next = agent.next ? agents.get(agent.next) : undefined;
  if (next) {
    fm.push(
      'handoffs:',
      `  - label: ${yamlStr(agent.nextLabel ?? next.name)}`,
      `    agent: ${yamlStr(next.id)}`,
      `    prompt: ${yamlStr('Tiếp tục ticket ở trên.')}`,
      '    send: false',
    );
  }
  fm.push('---');
  const preloaded = agent.skills.map((n) => skills.get(n)).filter((s): s is SkillDef => !!s);
  const others = [...skills.values()].filter((s) => !agent.skills.includes(s.name));
  const body = [
    `# ${agent.name}`,
    agent.body,
    '## Quy tắc chung',
    '- Dùng các tool DevFlow (#readRequirement, #writeTestcases, ...) để đọc/ghi Excel. Không tự tạo nội dung .xlsx.',
    '- Lưu deliverable bằng #writeWorkFile vào thư mục của ticket; đọc kết quả bước trước bằng #readWorkFile.',
    others.length ? `## Skills có thể nạp bằng #loadSkill\n${others.map((s) => `- ${s.name}: ${s.description}`).join('\n')}` : '',
    ...preloaded.map((s) => `## Skill: ${s.name}\n\n${s.body}`),
  ];
  return `${fm.join('\n')}\n\n${body.filter(Boolean).join('\n\n')}\n`;
}
