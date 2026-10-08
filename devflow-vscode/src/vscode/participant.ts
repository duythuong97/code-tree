import * as fs from 'node:fs';
import * as vscode from 'vscode';
import type { AgentDef } from '../core/definitions';
import { buildSystemPrompt, detectTicket } from '../core/prompt';
import { getConfig, getDefinitions } from './env';

interface DevflowResultMeta {
  agentId: string;
  ticket?: string;
}

/** Find the ticket mentioned in this request or earlier in the conversation. */
function findTicket(request: vscode.ChatRequest, context: vscode.ChatContext): string | undefined {
  const fromPrompt = detectTicket(request.prompt);
  if (fromPrompt) return fromPrompt;
  for (let i = context.history.length - 1; i >= 0; i--) {
    const turn = context.history[i];
    if (turn instanceof vscode.ChatResponseTurn) {
      const t = (turn.result.metadata as DevflowResultMeta | undefined)?.ticket;
      if (t) return t;
    } else if (turn instanceof vscode.ChatRequestTurn) {
      const t = detectTicket(turn.prompt);
      if (t) return t;
    }
  }
  return undefined;
}

function historyMessages(context: vscode.ChatContext): vscode.LanguageModelChatMessage[] {
  const msgs: vscode.LanguageModelChatMessage[] = [];
  for (const turn of context.history) {
    if (turn instanceof vscode.ChatRequestTurn) {
      msgs.push(vscode.LanguageModelChatMessage.User(`${turn.command ? `/${turn.command} ` : ''}${turn.prompt}`));
    } else if (turn instanceof vscode.ChatResponseTurn) {
      const text = turn.response
        .map((p) => (p instanceof vscode.ChatResponseMarkdownPart ? p.value.value : ''))
        .join('');
      if (text.trim()) msgs.push(vscode.LanguageModelChatMessage.Assistant(text));
    }
  }
  return msgs;
}

/** Describe #file / #selection references attached to the request. */
async function referencesText(request: vscode.ChatRequest): Promise<string> {
  const parts: string[] = [];
  for (const ref of request.references) {
    const v = ref.value;
    if (v instanceof vscode.Uri) {
      if (/\.(xlsx|xlsm|xls)$/i.test(v.fsPath)) {
        parts.push(`Attached Excel file: ${v.fsPath} (read it with the devflow Excel tools).`);
      } else if (fs.existsSync(v.fsPath) && fs.statSync(v.fsPath).isFile() && fs.statSync(v.fsPath).size < 200_000) {
        parts.push(`Attached file ${v.fsPath}:\n\`\`\`\n${fs.readFileSync(v.fsPath, 'utf8')}\n\`\`\``);
      } else {
        parts.push(`Attached path: ${v.fsPath}`);
      }
    } else if (v instanceof vscode.Location) {
      const doc = await vscode.workspace.openTextDocument(v.uri);
      parts.push(`Attached selection ${v.uri.fsPath}:${v.range.start.line + 1}-${v.range.end.line + 1}:\n\`\`\`\n${doc.getText(v.range)}\n\`\`\``);
    } else if (typeof v === 'string') {
      parts.push(`Attached: ${v}`);
    }
  }
  return parts.join('\n\n');
}

async function pickModel(request: vscode.ChatRequest): Promise<vscode.LanguageModelChat> {
  if (request.model) return request.model;
  const [model] = await vscode.lm.selectChatModels({ vendor: 'copilot' });
  if (!model) throw new Error('No Copilot chat model available.');
  return model;
}

function toolsFor(agent: AgentDef): vscode.LanguageModelChatTool[] {
  const wanted = new Set(agent.tools);
  const all = vscode.lm.tools;
  const selected = wanted.size
    ? all.filter((t) => wanted.has(t.name) || (wanted.has('devflow_*') && t.name.startsWith('devflow_')))
    : all.filter((t) => t.name.startsWith('devflow_'));
  // devflow_loadSkill is always available so agents can pull in skills on demand.
  if (!selected.some((t) => t.name === 'devflow_loadSkill')) {
    const ls = all.find((t) => t.name === 'devflow_loadSkill');
    if (ls) selected.push(ls);
  }
  return selected.map((t) => ({ name: t.name, description: t.description, inputSchema: t.inputSchema }));
}

export function registerParticipant(context: vscode.ExtensionContext): void {
  const handler: vscode.ChatRequestHandler = async (request, chatContext, stream, token) => {
    const { agents, skills } = getDefinitions();
    const agentId = request.command ?? 'orchestrator';
    const agent = agents.get(agentId);
    if (!agent) {
      stream.markdown(`Không tìm thấy agent \`${agentId}\`. Agents: ${[...agents.keys()].join(', ')}`);
      return {};
    }
    const { config, found } = getConfig();
    const ticket = findTicket(request, chatContext);
    const model = await pickModel(request);
    const tools = toolsFor(agent);
    const maxRounds = vscode.workspace.getConfiguration('devflow').get<number>('maxToolRounds', 25);

    stream.progress(`${agent.name}${ticket ? ` · ${ticket}` : ''}`);

    const system = buildSystemPrompt(agent, skills, config, { ticket, configFound: found, agents: [...agents.values()] });
    const refs = await referencesText(request);
    const messages: vscode.LanguageModelChatMessage[] = [
      vscode.LanguageModelChatMessage.User(system),
      vscode.LanguageModelChatMessage.Assistant('Đã hiểu vai trò và quy tắc. Tôi sẵn sàng.'),
      ...historyMessages(chatContext),
      vscode.LanguageModelChatMessage.User([request.prompt, refs].filter(Boolean).join('\n\n')),
    ];

    for (let round = 0; round < maxRounds; round++) {
      if (token.isCancellationRequested) break;
      const response = await model.sendRequest(messages, { tools, toolMode: vscode.LanguageModelChatToolMode.Auto }, token);
      const textParts: vscode.LanguageModelTextPart[] = [];
      const calls: vscode.LanguageModelToolCallPart[] = [];
      for await (const part of response.stream) {
        if (part instanceof vscode.LanguageModelTextPart) {
          stream.markdown(part.value);
          textParts.push(part);
        } else if (part instanceof vscode.LanguageModelToolCallPart) {
          calls.push(part);
        }
      }
      if (!calls.length) break;

      messages.push(vscode.LanguageModelChatMessage.Assistant([...textParts, ...calls]));
      const results: vscode.LanguageModelToolResultPart[] = [];
      for (const call of calls) {
        let content: (vscode.LanguageModelTextPart | vscode.LanguageModelPromptTsxPart | unknown)[];
        try {
          const r = await vscode.lm.invokeTool(call.name, { input: call.input, toolInvocationToken: request.toolInvocationToken }, token);
          content = r.content;
        } catch (e) {
          // e.g. the user declined the confirmation
          content = [new vscode.LanguageModelTextPart(`Tool ${call.name} failed or was cancelled: ${e instanceof Error ? e.message : String(e)}`)];
        }
        results.push(new vscode.LanguageModelToolResultPart(call.callId, content));
      }
      messages.push(vscode.LanguageModelChatMessage.User(results));
      if (round === maxRounds - 1) {
        stream.markdown(`\n\n_Đã đạt giới hạn ${maxRounds} vòng gọi tool (devflow.maxToolRounds). Gõ "tiếp tục" để chạy tiếp._`);
      }
    }

    const meta: DevflowResultMeta = { agentId, ticket };
    return { metadata: meta };
  };

  const participant = vscode.chat.createChatParticipant('devflow.agent', handler);
  participant.iconPath = new vscode.ThemeIcon('workflow');
  participant.followupProvider = {
    provideFollowups(result) {
      const meta = result.metadata as DevflowResultMeta | undefined;
      if (!meta) return [];
      const agent = getDefinitions().agents.get(meta.agentId);
      if (!agent?.next) return [];
      return [
        {
          command: agent.next,
          prompt: meta.ticket ? `Tiếp tục ticket ${meta.ticket}` : 'Tiếp tục',
          label: agent.nextLabel ?? `→ /${agent.next}`,
        },
      ];
    },
  };
  context.subscriptions.push(participant);
}
