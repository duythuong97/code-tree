import * as fs from 'node:fs';
import * as path from 'node:path';
import ExcelJS from 'exceljs';
import type { SheetMapping } from './config';

export type CaseRecord = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Reading
// ---------------------------------------------------------------------------

export async function loadWorkbook(file: string): Promise<ExcelJS.Workbook> {
  if (!fs.existsSync(file)) {
    throw new Error(`File not found: ${file}`);
  }
  const ext = path.extname(file).toLowerCase();
  if (ext === '.xls') {
    throw new Error(`${file} is in the old .xls format. Please save it as .xlsx first.`);
  }
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.readFile(file);
  return wb;
}

function valueText(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (v instanceof Date) {
    const iso = v.toISOString();
    return iso.endsWith('T00:00:00.000Z') ? iso.slice(0, 10) : iso.slice(0, 16).replace('T', ' ');
  }
  if (typeof v === 'object') {
    const o = v as Record<string, unknown>;
    if (Array.isArray(o.richText)) return (o.richText as { text: string }[]).map((r) => r.text).join('');
    if ('formula' in o || 'sharedFormula' in o) return valueText(o.result);
    if ('text' in o) return valueText(o.text); // hyperlink
    if ('error' in o) return String(o.error);
    return JSON.stringify(o);
  }
  return String(v);
}

/** Display text of a cell. Merged "slave" cells return '' unless `followMerge`. */
export function cellText(cell: ExcelJS.Cell, followMerge = false): string {
  if (cell.isMerged && cell.master !== cell && !followMerge) return '';
  const target = cell.isMerged ? cell.master : cell;
  return valueText(target.value).trim();
}

export function getSheet(wb: ExcelJS.Workbook, name: string | undefined): ExcelJS.Worksheet {
  if (!name) {
    const first = wb.worksheets[0];
    if (!first) throw new Error('Workbook has no sheets.');
    return first;
  }
  const norm = (s: string) => s.trim().toLowerCase();
  const ws =
    wb.worksheets.find((w) => w.name === name) ??
    wb.worksheets.find((w) => norm(w.name) === norm(name)) ??
    wb.worksheets.find((w) => norm(w.name).includes(norm(name)));
  if (!ws) {
    throw new Error(`Sheet "${name}" not found. Sheets: ${wb.worksheets.map((w) => w.name).join(', ')}`);
  }
  return ws;
}

function mdEscape(s: string, maxLen = 600): string {
  const t = s.length > maxLen ? s.slice(0, maxLen) + '…' : s;
  return t.replace(/\|/g, '\\|').replace(/\r?\n/g, '<br>');
}

/** Render a worksheet as a Markdown table with Excel row numbers and column letters. */
export function sheetToMarkdown(ws: ExcelJS.Worksheet, maxRows = 300): string {
  const rows: { n: number; cells: Map<number, string> }[] = [];
  const usedCols = new Set<number>();
  let truncated = false;
  ws.eachRow({ includeEmpty: false }, (row, rowNumber) => {
    const cells = new Map<number, string>();
    row.eachCell({ includeEmpty: false }, (cell, col) => {
      const t = cellText(cell);
      if (t) {
        cells.set(col, t);
        usedCols.add(col);
      }
    });
    if (cells.size === 0) return;
    if (rows.length >= maxRows) {
      truncated = true;
      return;
    }
    rows.push({ n: rowNumber, cells });
  });
  const state = ws.state && ws.state !== 'visible' ? ` (${ws.state})` : '';
  const title = `### Sheet: ${ws.name}${state}`;
  if (rows.length === 0) return `${title}\n\n_(empty)_\n`;
  const cols = [...usedCols].sort((a, b) => a - b);
  const letter = (c: number) => ws.getColumn(c).letter;
  const lines = [
    title,
    '',
    `| Row | ${cols.map(letter).join(' | ')} |`,
    `|---|${cols.map(() => '---').join('|')}|`,
    ...rows.map((r) => `| ${r.n} | ${cols.map((c) => mdEscape(r.cells.get(c) ?? '')).join(' | ')} |`),
  ];
  if (truncated) lines.push('', `_Truncated after ${maxRows} non-empty rows (sheet has ${ws.rowCount} rows)._`);
  return lines.join('\n') + '\n';
}

export async function readWorkbookMarkdown(file: string, sheets?: string[], maxRows = 300): Promise<string> {
  const wb = await loadWorkbook(file);
  const selected = sheets && sheets.length ? sheets.map((s) => getSheet(wb, s)) : wb.worksheets;
  const header = `## Workbook: ${path.basename(file)}\nSheets: ${wb.worksheets.map((w) => w.name).join(', ')}\n`;
  return [header, ...selected.map((ws) => sheetToMarkdown(ws, maxRows))].join('\n');
}

export async function readRequirementMarkdown(
  file: string,
  sheetNames: { requirement: string; asis: string; tobe: string; qa: string },
  maxRows = 300,
): Promise<string> {
  const wb = await loadWorkbook(file);
  const out: string[] = [`## Requirement workbook: ${path.basename(file)}`, `All sheets: ${wb.worksheets.map((w) => w.name).join(', ')}`, ''];
  const used = new Set<string>();
  for (const [role, name] of Object.entries(sheetNames)) {
    try {
      const ws = getSheet(wb, name);
      used.add(ws.name);
      out.push(`<!-- role: ${role.toUpperCase()} -->`, sheetToMarkdown(ws, maxRows));
    } catch {
      out.push(`> Sheet for ${role.toUpperCase()} ("${name}") not found.`, '');
    }
  }
  const others = wb.worksheets.filter((w) => !used.has(w.name)).map((w) => w.name);
  if (others.length) out.push(`Other sheets (not read, use devflow_readExcel if needed): ${others.join(', ')}`);
  return out.join('\n');
}

// ---------------------------------------------------------------------------
// Mapped rows (test cases, QA questions)
// ---------------------------------------------------------------------------

function colNumber(ws: ExcelJS.Worksheet, letter: string): number {
  return ws.getColumn(letter.trim().toUpperCase()).number;
}

export interface MappedRow {
  row: number;
  values: Record<string, string>;
}

export function readMappedRows(ws: ExcelJS.Worksheet, mapping: SheetMapping): MappedRow[] {
  const result: MappedRow[] = [];
  const cols = Object.entries(mapping.columns).map(([f, l]) => [f, colNumber(ws, l)] as const);
  for (let r = mapping.startRow; r <= ws.rowCount; r++) {
    const row = ws.getRow(r);
    const values: Record<string, string> = {};
    let any = false;
    for (const [field, c] of cols) {
      const t = cellText(row.getCell(c), true);
      values[field] = t;
      if (t) any = true;
    }
    if (any) result.push({ row: r, values });
  }
  return result;
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Build the ID for the next sequence number, given IDs already used. */
export function nextId(format: string, vars: Record<string, string | undefined>, existing: Iterable<string>): string {
  const seqMatch = /\{seq:(\d+)\}/.exec(format);
  const pad = seqMatch ? Number(seqMatch[1]) : 0;
  const fill = (s: string) => s.replace(/\{(\w+)\}/g, (_m, k: string) => vars[k] ?? '');
  const parts = format.split(/\{seq:\d+\}/);
  const regex = new RegExp('^' + parts.map((p) => escapeRegex(fill(p))).join('(\\d+)') + '$');
  let max = 0;
  for (const id of existing) {
    const m = regex.exec(id.trim());
    if (m && m[1]) max = Math.max(max, Number(m[1]));
  }
  const seq = String(max + 1).padStart(pad, '0');
  return parts.map(fill).join(seq);
}

function formatValue(field: string, v: unknown): string | number {
  if (v === null || v === undefined) return '';
  if (Array.isArray(v)) {
    const items = v.map((x) => valueText(x).trim()).filter(Boolean);
    const numbered = field.toLowerCase().includes('step') || field.toLowerCase().includes('expected');
    return items.map((x, i) => (numbered && !/^\d+[.)]/.test(x) ? `${i + 1}. ${x}` : x)).join('\n');
  }
  if (typeof v === 'number') return v;
  if (typeof v === 'object') return valueText(v);
  return String(v);
}

function copyRowStyle(ws: ExcelJS.Worksheet, from: number, to: number, maxCol: number): void {
  if (from === to) return;
  const src = ws.getRow(from);
  const dst = ws.getRow(to);
  for (let c = 1; c <= maxCol; c++) {
    const s = src.getCell(c);
    const d = dst.getCell(c);
    if (s.style) d.style = JSON.parse(JSON.stringify(s.style));
    const dv = s.dataValidation;
    if (dv) d.dataValidation = JSON.parse(JSON.stringify(dv));
  }
}

const normTitle = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();

export interface AppendOptions {
  /** Workbook to start from (template or existing file). */
  sourcePath: string;
  /** Where to save. May equal sourcePath. */
  outputPath: string;
  mapping: SheetMapping;
  cases: CaseRecord[];
  vars?: { screenId?: string; ticket?: string };
  /** Skip cases whose title already exists in the sheet. */
  dedupeByTitle?: boolean;
}

export interface AppendResult {
  outputPath: string;
  sheet: string;
  written: { row: number; id: string; title: string }[];
  skippedDuplicates: string[];
  unknownFields: string[];
}

export async function appendCases(opts: AppendOptions): Promise<AppendResult> {
  const { mapping } = opts;
  const wb = await loadWorkbook(opts.sourcePath);
  const ws = getSheet(wb, mapping.sheet);
  const existing = readMappedRows(ws, mapping);
  const idField = mapping.idField;
  const titleField = mapping.titleField ?? 'title';
  const ids = existing.map((r) => (idField ? r.values[idField] : '')).filter(Boolean);
  const titles = new Set(existing.map((r) => normTitle(r.values[titleField] ?? '')).filter(Boolean));

  const mappedCols = Object.values(mapping.columns).map((l) => colNumber(ws, l));
  const maxCol = Math.max(ws.columnCount, ...mappedCols);
  const styleRow = mapping.styleRow ?? mapping.startRow;
  let next = existing.length ? existing[existing.length - 1].row + 1 : mapping.startRow;

  const result: AppendResult = { outputPath: opts.outputPath, sheet: ws.name, written: [], skippedDuplicates: [], unknownFields: [] };
  const unknown = new Set<string>();

  for (const raw of opts.cases) {
    const c: CaseRecord = { ...raw };
    for (const k of Object.keys(c)) if (!(k in mapping.columns)) unknown.add(k);
    if (c.screenId === undefined && opts.vars?.screenId && 'screenId' in mapping.columns) c.screenId = opts.vars.screenId;

    const title = valueText(c[titleField] ?? '').trim();
    if (opts.dedupeByTitle && title && titles.has(normTitle(title))) {
      result.skippedDuplicates.push(title);
      continue;
    }
    if (idField && mapping.idFormat && !valueText(c[idField] ?? '').trim()) {
      const vars = { screenId: valueText(c.screenId ?? opts.vars?.screenId ?? ''), ticket: opts.vars?.ticket };
      c[idField] = nextId(mapping.idFormat, vars, ids);
    }
    const id = idField ? valueText(c[idField] ?? '') : '';
    if (id) ids.push(id);
    if (title) titles.add(normTitle(title));

    copyRowStyle(ws, styleRow, next, maxCol);
    for (const [field, letter] of Object.entries(mapping.columns)) {
      if (!(field in c)) continue;
      let v = formatValue(field, c[field]);
      if (field === idField && typeof v === 'string' && /^\d+$/.test(v)) v = Number(v);
      ws.getCell(`${letter.trim().toUpperCase()}${next}`).value = v === '' ? null : v;
    }
    result.written.push({ row: next, id, title });
    next++;
  }
  result.unknownFields = [...unknown];

  fs.mkdirSync(path.dirname(opts.outputPath), { recursive: true });
  await wb.xlsx.writeFile(opts.outputPath);
  return result;
}

// ---------------------------------------------------------------------------
// Template description & validation
// ---------------------------------------------------------------------------

function listFromValidation(wb: ExcelJS.Workbook, dv: ExcelJS.DataValidation | undefined): string[] {
  if (!dv || dv.type !== 'list' || !dv.formulae?.length) return [];
  const f = String(dv.formulae[0]);
  if (f.startsWith('"')) {
    return f.replace(/^"|"$/g, '').split(',').map((s) => s.trim()).filter(Boolean);
  }
  // Range reference such as Lists!$A$1:$A$5 or 'My list'!A1:A5
  const m = /^(?:'?([^'!]+)'?!)?\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)$/.exec(f);
  if (!m) return [];
  const ws = m[1] ? wb.getWorksheet(m[1]) : undefined;
  if (!ws) return [];
  const values: string[] = [];
  const c1 = ws.getColumn(m[2]).number;
  const c2 = ws.getColumn(m[4]).number;
  for (let r = Number(m[3]); r <= Number(m[5]); r++) {
    for (let c = c1; c <= c2; c++) {
      const t = cellText(ws.getCell(r, c), true);
      if (t) values.push(t);
    }
  }
  return values;
}

export interface FieldInfo {
  field: string;
  column: string;
  header: string;
  required: boolean;
  allowed: string[];
}

export async function describeMapping(file: string, mapping: SheetMapping): Promise<{ sheet: string; fields: FieldInfo[]; existingRows: number }> {
  const wb = await loadWorkbook(file);
  const ws = getSheet(wb, mapping.sheet);
  const styleRow = mapping.styleRow ?? mapping.startRow;
  const fields = Object.entries(mapping.columns).map(([field, letter]) => {
    const L = letter.trim().toUpperCase();
    const fromDv = listFromValidation(wb, ws.getCell(`${L}${styleRow}`).dataValidation);
    const allowed = [...new Set([...(mapping.allowed?.[field] ?? []), ...fromDv])];
    return {
      field,
      column: L,
      header: cellText(ws.getCell(`${L}${mapping.headerRow}`), true),
      required: (mapping.required ?? []).includes(field),
      allowed,
    };
  });
  return { sheet: ws.name, fields, existingRows: readMappedRows(ws, mapping).length };
}

export interface ValidationIssue {
  row: number;
  field?: string;
  message: string;
}

export async function validateCases(file: string, mapping: SheetMapping): Promise<{ rows: number; issues: ValidationIssue[] }> {
  const info = await describeMapping(file, mapping);
  const wb = await loadWorkbook(file);
  const ws = getSheet(wb, mapping.sheet);
  const rows = readMappedRows(ws, mapping);
  const issues: ValidationIssue[] = [];
  const seen = new Map<string, number>();
  for (const r of rows) {
    for (const f of info.fields) {
      const v = r.values[f.field];
      if (f.required && !v) issues.push({ row: r.row, field: f.field, message: `Missing required "${f.field}" (${f.column})` });
      if (v && f.allowed.length && !f.allowed.includes(v)) {
        issues.push({ row: r.row, field: f.field, message: `"${v}" is not one of: ${f.allowed.join(', ')}` });
      }
    }
    const id = mapping.idField ? r.values[mapping.idField] : '';
    if (id) {
      const prev = seen.get(id);
      if (prev) issues.push({ row: r.row, field: mapping.idField, message: `Duplicate ID ${id} (also row ${prev})` });
      else seen.set(id, r.row);
    }
  }
  return { rows: rows.length, issues };
}

/** Render mapped rows as Markdown, optionally filtered by keyword. */
export function mappedRowsToMarkdown(rows: MappedRow[], mapping: SheetMapping, keyword?: string, max = 300): string {
  const fields = Object.keys(mapping.columns);
  const kw = keyword?.toLowerCase();
  const filtered = kw ? rows.filter((r) => Object.values(r.values).some((v) => v.toLowerCase().includes(kw))) : rows;
  const shown = filtered.slice(0, max);
  const lines = [
    `| Row | ${fields.join(' | ')} |`,
    `|---|${fields.map(() => '---').join('|')}|`,
    ...shown.map((r) => `| ${r.row} | ${fields.map((f) => mdEscape(r.values[f] ?? '', 400)).join(' | ')} |`),
  ];
  const note = filtered.length > max ? `\n_Showing ${max} of ${filtered.length} rows._` : '';
  return `${filtered.length} row(s)${kw ? ` matching "${keyword}"` : ''}.\n\n${lines.join('\n')}${note}`;
}
