import type { DevflowConfig } from './config';
import type { AgentDef, SkillDef } from './definitions';

/** Ticket IDs look like ABC-123 or are given as "ticket: xxx". */
export function detectTicket(text: string): string | undefined {
  const explicit = /\bticket\s*[:=]\s*([\w.-]+)/i.exec(text);
  if (explicit) return explicit[1];
  const jira = /\b([A-Z][A-Z0-9]+-\d+)\b/.exec(text);
  return jira?.[1];
}

export function buildSystemPrompt(
  agent: AgentDef,
  skills: Map<string, SkillDef>,
  cfg: DevflowConfig,
  opts: { ticket?: string; configFound: boolean; agents: AgentDef[] },
): string {
  const preloaded = agent.skills.map((n) => skills.get(n)).filter((s): s is SkillDef => !!s);
  const others = [...skills.values()].filter((s) => !agent.skills.includes(s.name));
  const parts: string[] = [];

  parts.push(`# Vai trò: ${agent.name}`, agent.body);

  parts.push(
    '## Quy tắc chung',
    ...cfg.globalRules.map((r) => `- ${r}`),
    '- Dùng các tool devflow_* để đọc/ghi file. KHÔNG tự tạo nội dung file .xlsx bằng text: luôn dùng tool Excel.',
    '- Mọi deliverable dạng văn bản phải được lưu bằng devflow_writeWorkFile vào thư mục của ticket.',
    '- Trước khi bắt đầu, đọc kết quả các bước trước bằng devflow_readWorkFile (không có name để liệt kê file).',
    '- Khi kết thúc: tóm tắt ngắn gọn kết quả, liệt kê file đã tạo/sửa, các điểm cần người dùng xác nhận, và bước tiếp theo đề xuất.',
  );

  parts.push(
    '## Cấu hình dự án',
    `- Thư mục deliverable: ${cfg.workDir}/<ticket>/`,
    `- Screen index: ${cfg.screenIndex}`,
    `- Template test case: ${cfg.testcase.template}`,
    opts.configFound ? '' : '- ⚠️ Chưa có file config (.devflow/config.json): đang dùng cấu hình mặc định. Nhắc người dùng chạy "DevFlow: Initialize workspace" nếu tool báo lỗi đường dẫn.',
    opts.ticket
      ? `- Ticket hiện tại: **${opts.ticket}**`
      : '- Chưa xác định được ticket. Lấy từ tên file requirement hoặc hỏi người dùng trước khi lưu file.',
  );

  parts.push(
    '## Các agent trong quy trình',
    ...opts.agents.filter((a) => a.id !== 'orchestrator').map((a) => `- /${a.id} — ${a.name}: ${a.description}`),
  );

  if (others.length) {
    parts.push(
      '## Skills có thể nạp (gọi devflow_loadSkill khi cần)',
      ...others.map((s) => `- ${s.name}: ${s.description}`),
    );
  }
  for (const s of preloaded) {
    parts.push(`## Skill: ${s.name}`, s.body);
  }
  return parts.filter((p) => p !== '').join('\n\n');
}
