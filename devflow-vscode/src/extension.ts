import * as vscode from 'vscode';
import { registerCommands } from './vscode/commands';
import { initEnv } from './vscode/env';
import { registerParticipant } from './vscode/participant';
import { registerTools } from './vscode/tools';

export function activate(context: vscode.ExtensionContext): void {
  initEnv(context);
  registerTools(context);
  registerParticipant(context);
  registerCommands(context);
}

export function deactivate(): void {}
