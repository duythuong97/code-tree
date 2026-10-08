import * as fs from 'node:fs';
import * as path from 'node:path';

/** How a sheet of test cases (or QA questions) maps to Excel columns. */
export interface SheetMapping {
  /** Sheet name. Empty → first sheet. */
  sheet: string;
  /** Row that holds the column headers (used to describe the template). */
  headerRow: number;
  /** First data row. */
  startRow: number;
  /** Row whose style/dropdowns are copied to new rows. Default: startRow. */
  styleRow?: number;
  /** field name → column letter, e.g. { id: "B", steps: "F" }. */
  columns: Record<string, string>;
  /** Fields that must be filled. */
  required?: string[];
  /** field → allowed values (merged with dropdown validations found in the template). */
  allowed?: Record<string, string[]>;
  /** Field holding the test case ID. */
  idField?: string;
  /** ID format. Placeholders: {screenId} {ticket} {seq:N}. */
  idFormat?: string;
  /** Field used to detect duplicates when appending (default "title"). */
  titleField?: string;
}

export interface DevflowConfig {
  /** Folder for per-ticket deliverables. */
  workDir: string;
  /** JSON file with the screen index. */
  screenIndex: string;
  requirement: {
    sheets: { requirement: string; asis: string; tobe: string; qa: string };
    qa: SheetMapping;
  };
  testcase: SheetMapping & { template: string };
  regression: SheetMapping;
  search: { exclude: string; maxFileSizeKb: number };
  /** Extra rules appended to every agent prompt (language, conventions, ...). */
  globalRules: string[];
}

export const DEFAULT_CONFIG: DevflowConfig = {
  workDir: 'work',
  screenIndex: '.devflow/screens.json',
  requirement: {
    sheets: { requirement: 'Requirement', asis: 'ASIS', tobe: 'TOBE', qa: 'QA' },
    qa: {
      sheet: 'QA',
      headerRow: 1,
      startRow: 2,
      columns: { no: 'A', screenId: 'B', category: 'C', question: 'D', raisedBy: 'E', status: 'F', answer: 'G' },
      required: ['question'],
      idField: 'no',
      idFormat: '{seq:0}',
      titleField: 'question',
    },
  },
  testcase: {
    template: '.devflow/templates/testcase_template.xlsx',
    sheet: 'TestCase',
    headerRow: 1,
    startRow: 2,
    columns: {
      id: 'A',
      screenId: 'B',
      reqRef: 'C',
      category: 'D',
      title: 'E',
      precondition: 'F',
      steps: 'G',
      testData: 'H',
      expected: 'I',
      priority: 'J',
    },
    required: ['id', 'screenId', 'title', 'steps', 'expected'],
    allowed: { category: ['Normal', 'Abnormal', 'Boundary'], priority: ['High', 'Medium', 'Low'] },
    idField: 'id',
    idFormat: 'TC-{screenId}-{seq:3}',
    titleField: 'title',
  },
  regression: {
    sheet: '',
    headerRow: 1,
    startRow: 2,
    columns: {
      id: 'A',
      category: 'B',
      title: 'C',
      precondition: 'D',
      steps: 'E',
      expected: 'F',
      sourceTicket: 'G',
    },
    required: ['id', 'title', 'steps', 'expected'],
    idField: 'id',
    idFormat: 'RT-{screenId}-{seq:3}',
    titleField: 'title',
  },
  search: {
    exclude: '**/{node_modules,.git,dist,build,out,target,bin,obj}/**',
    maxFileSizeKb: 1024,
  },
  globalRules: [
    'Trả lời bằng tiếng Việt, giữ nguyên thuật ngữ kỹ thuật và mã màn hình.',
    'Không bịa thông tin. Nếu requirement/spec không rõ, ghi thành câu hỏi QA thay vì tự giả định.',
  ],
};

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Deep-merge `override` into `base` (objects merge, arrays/scalars replace). */
export function deepMerge<T>(base: T, override: unknown): T {
  if (!isPlainObject(base) || !isPlainObject(override)) {
    return (override === undefined ? base : override) as T;
  }
  const out: Record<string, unknown> = { ...base };
  for (const [k, v] of Object.entries(override)) {
    const b = (base as Record<string, unknown>)[k];
    // `columns` is a full mapping: replace it instead of merging so removed columns disappear.
    out[k] = k === 'columns' ? v : deepMerge(b, v);
  }
  return out as T;
}

export function loadConfig(root: string, configPath: string): { config: DevflowConfig; found: boolean } {
  const full = path.resolve(root, configPath);
  if (!fs.existsSync(full)) {
    return { config: DEFAULT_CONFIG, found: false };
  }
  const raw = JSON.parse(fs.readFileSync(full, 'utf8'));
  return { config: deepMerge(DEFAULT_CONFIG, raw), found: true };
}
