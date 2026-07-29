#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const VERSION = '3.0.0';

const CSV_HEADERS = {
  nodes: ['node_id', 'node_type', 'technical_name', 'qualified_name', 'default_display_name', 'system_key', 'database_key', 'repository_key', 'graph_role', 'confidence', 'properties_json'],
  edges: ['edge_id', 'source_node_id', 'target_node_id', 'edge_type', 'graph_layer', 'raw_operation', 'confidence', 'properties_json'],
  evidence: ['evidence_id', 'target_type', 'target_id', 'source_path', 'start_line', 'end_line', 'start_column', 'end_column', 'evidence_kind', 'extractor_name', 'confidence', 'snippet', 'properties_json'],
  issues: ['issue_id', 'issue_type', 'severity', 'source_node_id', 'raw_reference', 'database_key', 'source_path', 'start_line', 'message', 'properties_json'],
  localized_texts: ['target_type', 'target_id', 'field_name', 'locale', 'value', 'source_kind', 'review_status', 'author_name', 'created_at', 'updated_at'],
};

function main() {
  const configPath = argValue('--config');
  if (!configPath) throw new Error('Missing --config');
  const ts = loadTypeScript();
  const config = loadConfig(configPath);
  extract(ts, config);
}

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : '';
}

function loadTypeScript() {
  if (!process.env.TYPESCRIPT_PATH) throw new Error('Missing required environment variable: TYPESCRIPT_PATH');
  try {
    return require(process.env.TYPESCRIPT_PATH);
  } catch {
    throw new Error(`Cannot load TypeScript from TYPESCRIPT_PATH: ${process.env.TYPESCRIPT_PATH}`);
  }
}

function loadConfig(configPath) {
  const absoluteConfig = path.resolve(configPath);
  const raw = readFileSync(absoluteConfig, 'utf8').replace(/\$\{([A-Z][A-Z0-9_]*)\}/g, (_, name) => {
    if (!process.env[name]) throw new Error(`Missing required environment variable: ${name}`);
    return process.env[name];
  });
  const config = JSON.parse(raw);
  for (const key of ['type', 'source', 'root', 'folders', 'appConfig', 'output']) {
    if (!(key in config)) throw new Error(`Missing Angular extractor config key: ${key}`);
  }
  if (config.type !== 'angular') throw new Error(`Unsupported Angular extractor config type: ${config.type}`);
  for (const key of ['root', 'output', 'inputData']) {
    if (config[key]) config[key] = path.resolve(path.dirname(absoluteConfig), config[key]);
  }
  return config;
}

function extract(ts, config) {
  const source = config.source;
  const repository = config.repository || source;
  const systemKey = config.system || source;
  const discovery = discoverAngularProject(config);
  const files = configuredFiles(config, ['.ts', '.html'], discovery);

  // Create TypeScript Program
  const tsconfigPath = discovery.tsconfigPath;
  let program;
  let typeChecker;
  if (tsconfigPath && existsSync(tsconfigPath)) {
    const configFile = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
    const parsedConfig = ts.parseJsonConfigFileContent(configFile.config, ts.sys, path.dirname(tsconfigPath));
    program = ts.createProgram(parsedConfig.fileNames, parsedConfig.options);
    typeChecker = program.getTypeChecker();
  } else {
    // Fallback if no tsconfig
    program = ts.createProgram(files.filter(f => f.absolute.endsWith('.ts')).map(f => f.absolute), {});
    typeChecker = program.getTypeChecker();
  }

  const builder = new PackageBuilder(`angular-${source}`, `extractor:angular/${source}`, 'angular-extractor', VERSION, {
    source,
    repository,
    technology: 'Node.js TypeScript Compiler API',
    parser: 'typescript.createProgram',
    angularJson: discovery.angularJsonPath ? repoPath(path.relative(discovery.root, discovery.angularJsonPath)) : '',
    angularProject: discovery.angularProjectName,
    tsconfig: discovery.tsconfigPath ? repoPath(path.relative(discovery.root, discovery.tsconfigPath)) : '',
    folders: discovery.folders,
    exclusions: discovery.exclusions,
    extractorContract: '3.0',
    capabilities: ['route-input-output', 'api-client-lineage', 'semantic-summary'],
  });
  builder.filesScanned = files.length;

  const appConfig = loadAppConfig(config);
  for (const file of files) {
    if (file.absolute.endsWith('.ts')) {
      // Use program's source file if available, otherwise fallback to parsed
      file.sourceFile = program.getSourceFile(file.absolute) || file.sourceFile;
      Object.assign(appConfig, inlineConfig(ts, file.sourceFile));
    }
  }
  const projectId = stableNodeId('angular-project', repository, source);
  builder.addNode(projectId, 'ANGULAR_PROJECT', source, source, source, { system_key: systemKey, repository_key: repository, graph_role: 'TECHNICAL' });

  const classToScreen = new Map();
  const screenByRoute = new Map();
  let firstScreen = '';

  for (const file of files) {
    if (file.absolute.endsWith('.ts')) {
      visit(file.sourceFile, node => {
        if (ts.isObjectLiteralExpression(node)) {
          const route = stringProp(ts, node, 'path');
          if (route) {
            const normalizedRoute = normalizeHttpRoute('GET', '/' + route.replace(/^\/+|\/+$/g, '')).route;
            const target = routeTarget(ts, node, normalizedRoute);
            if (!target) return;
            const component = target.name;
            const screenId = stableNodeId('screen', source, normalizedRoute);
            firstScreen ||= screenId;
            if (target.kind !== 'loadChildren') classToScreen.set(component, screenId);
            screenByRoute.set(normalizedRoute, screenId);
            builder.addNode(screenId, 'SCREEN', component, `${source}.${normalizedRoute}`, displayFromRoute(normalizedRoute), {
              system_key: systemKey,
              repository_key: repository,
              properties: { route: normalizedRoute, lazy: target.kind !== 'component', routeLoader: target.kind },
            });
            builder.addEdge(projectId, screenId, 'CONTAINS', { graph_layer: 'STRUCTURAL' });
            if (target.kind !== 'loadChildren') {
              const componentId = stableNodeId('angular-component', source, slug(component.replace(/Component$/, '')));
              builder.addNode(componentId, 'ANGULAR_COMPONENT', component, `${source}.${component}`, component, {
                system_key: systemKey,
                repository_key: repository,
                graph_role: 'TECHNICAL',
              });
              builder.addEdge(screenId, componentId, 'BELONGS_TO');
            }
          }
        }
      });
    }
  }

  const serviceIds = new Map();
  const serviceSemantic = new Map();
  const actionSemantic = new Map();
  const methodToApi = new Map();
  const serviceMethodToApi = new Map();
  for (const file of files.filter(f => f.absolute.endsWith('.ts'))) {
    visit(file.sourceFile, node => {
      if (!ts.isClassDeclaration(node) || !node.name) return;
      const className = node.name.text;
      const classText = node.getText(file.sourceFile);
      if (!/\bHttpClient\b|\bHttp\b|\bthis\.http\b/.test(classText)) return;
      const serviceId = stableNodeId('angular-service', source, slug(className.replace(/Service$/, '')));
      serviceIds.set(className, serviceId);
      builder.addNode(serviceId, 'ANGULAR_SERVICE', className, `${source}.${className}`, className, {
        system_key: systemKey,
        repository_key: repository,
        graph_role: 'TECHNICAL',
      });
      const callRefs = new Map();
      visit(node, candidate => {
        const http = httpCall(ts, candidate);
        if (!http) return;
        const line = lineFor(file.sourceFile, candidate.getStart(file.sourceFile));
        const context = localResolutionContext(ts, candidate, appConfig);
        const method = http.method || resolveHttpMethod(ts, http.methodArgument, context);
        if (!method) return;
        const resolved = resolveUrl(ts, http.argument, appConfig, context);
        if (resolved.issue) {
          builder.addIssue('DYNAMIC_CONFIG_KEY', 'WARNING', 'Runtime configuration key cannot be resolved', {
            source_node_id: serviceId,
            raw_reference: resolved.issue,
            source_path: file.relative,
            start_line: line,
          });
          return;
        }
        if (!resolved.route) return;
        const normalized = normalizeHttpRoute(method, resolved.route);
        const callId = apiCallId(source, normalized.method, normalized.route);
        builder.addNode(callId, 'API_CALL_REFERENCE', `${normalized.method} ${normalized.route}`, `${normalized.method} ${normalized.route}`, `${normalized.method} ${normalized.route}`, {
          system_key: systemKey,
          repository_key: repository,
          graph_role: 'TECHNICAL',
          properties: { method: normalized.method, route: normalized.route },
        });
        const edgeId = builder.addEdge(serviceId, callId, 'CALLS');
        builder.addEvidence('EDGE', edgeId, file.relative, line, line, 'HTTP_CALL', lineText(file.text, line));
        const owner = ownerMethodName(ts, candidate);
        callRefs.set(candidate.getStart(file.sourceFile), {
          type: 'http_call',
          label: `${owner ? owner + ': ' : ''}${normalized.method} ${normalized.route}`,
          ref_node_id: callId,
        });
        if (owner) {
          pushMap(methodToApi, owner, callId);
          pushMap(serviceMethodToApi, `${className}::${owner}`, callId);
        }
      });
      const memberSteps = node.members.map(member => projectClassMember(ts, file, member, callRefs));
      serviceSemantic.set(serviceId, semanticOperation(className, [], memberSteps));
    });
  }

  const templateIndex = buildTemplateEventIndex(ts, files);

  for (const file of files.filter(f => f.absolute.endsWith('.ts'))) {
    visit(file.sourceFile, node => {
      if (!ts.isClassDeclaration(node) || !node.name) return;
      const className = node.name.text;
      const screenId = classToScreen.get(className);
      if (!screenId) return;
      const serviceReceivers = componentServiceReceivers(ts, node, serviceIds, typeChecker);
      for (const member of node.members) {
        const callable = callableMember(ts, member);
        if (!callable) continue;
        const methodName = member.name.getText(file.sourceFile);
        const templateEventsForMethod = eventsForComponentMethod(templateIndex, className, methodName);
        if (!templateEventsForMethod.length) continue;
        const actionId = stableNodeId('ui-action', source, slug(className), slug(methodName));
        builder.addNode(actionId, 'UI_ACTION', methodName, `${source}.${className}.${methodName}`, displayFromMethod(methodName), {
          system_key: systemKey,
          repository_key: repository,
        });
        const edgeId = builder.addEdge(screenId, actionId, 'CONTAINS', { graph_layer: 'STRUCTURAL' });
        for (const event of templateEventsForMethod) {
          builder.addEvidence('EDGE', edgeId, event.sourcePath, event.line, event.line, 'TEMPLATE_EVENT', event.snippet);

          // Extract template metadata (routerLink, click, etc)
          if (event.metadata) {
             builder.setNodeProperty(actionId, 'template_metadata', event.metadata);
          }
        }
        const callRefs = new Map();
        visit(callable.body, call => {
          const serviceMethod = serviceCall(ts, call, serviceReceivers, typeChecker);
          if (!serviceMethod) return;
          const scopedApiIds = serviceMethod.serviceClass ? serviceMethodToApi.get(`${serviceMethod.serviceClass}::${serviceMethod.method}`) || [] : [];
          const apiIds = scopedApiIds.length ? scopedApiIds : methodToApi.get(serviceMethod.method) || [];
          for (const apiId of apiIds) builder.addEdge(actionId, apiId, 'CALLS');
          if (apiIds.length) {
            callRefs.set(call.getStart(file.sourceFile), {
              type: 'call',
              label: `Call ${serviceMethod.method}`,
              ref_node_id: apiIds.length === 1 ? apiIds[0] : undefined,
              ref_node_ids: apiIds,
              resolution: apiIds.length === 1 ? 'resolved' : 'partial',
            });
          }
        });
        actionSemantic.set(actionId, semanticOperation(methodName, semanticParameters(callable), projectMethod(ts, file, callable, callRefs)));
      }
    });
  }

  for (const [nodeId, tree] of [...serviceSemantic, ...actionSemantic]) {
    builder.setNodeProperty(nodeId, 'semantic_tree', tree);
  }
  routeQualityIssues(config, builder, files, source);
  builder.write(path.resolve(config.output));
}

function semanticOperation(label, parameters = [], steps = []) {
  return { version: 3, type: 'operation', label, summary: '', parameters, steps, analysis_notes: [] };
}

function semanticParameters(method) {
  return (method.parameters || []).map(parameter => {
    const item = {
      name: parameter.name.getText(),
      type: parameter.type?.getText() || '',
      optional: Boolean(parameter.questionToken),
      rest: Boolean(parameter.dotDotDotToken),
    };
    if (parameter.initializer) item.default = parameter.initializer.getText();
    return item;
  });
}

function semanticSource(file, node) {
  return { path: file.relative, line: lineFor(file.sourceFile, node.getStart(file.sourceFile)) };
}

function callableMember(ts, member) {
  if (ts.isMethodDeclaration(member) && member.body && member.name) return member;
  if (ts.isPropertyDeclaration(member) && member.name && member.initializer && (ts.isArrowFunction(member.initializer) || ts.isFunctionExpression(member.initializer))) return member.initializer;
  return null;
}

function projectMethod(ts, file, method, callRefs = new Map()) {
  const body = method.body;
  if (!body) return [];
  if (ts.isBlock(body)) return projectStatements(ts, file, body.statements, callRefs);
  return [{
    type: 'output',
    label: 'return',
    source: semanticSource(file, body),
    expression: body.getText(file.sourceFile),
    effects: expressionEffects(ts, file, body, callRefs),
  }];
}

function projectClassMember(ts, file, member, callRefs) {
  if ((ts.isMethodDeclaration(member) || ts.isConstructorDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member)) && member.body) {
    return {
      type: 'block',
      label: ts.isConstructorDeclaration(member) ? 'constructor' : member.name?.getText(file.sourceFile) || ts.SyntaxKind[member.kind],
      source: semanticSource(file, member),
      parameters: semanticParameters(member),
      steps: projectMethod(ts, file, member, callRefs),
    };
  }
  if (ts.isPropertyDeclaration(member)) {
    return {
      type: 'assignment', label: 'property', source: semanticSource(file, member), action: 'DECLARE',
      target: member.name.getText(file.sourceFile), expression: member.initializer?.getText(file.sourceFile) || '',
      effects: member.initializer ? expressionEffects(ts, file, member.initializer, callRefs) : [],
    };
  }
  if (ts.isClassStaticBlockDeclaration?.(member)) {
    return { type: 'block', label: 'static', source: semanticSource(file, member), steps: projectStatements(ts, file, member.body.statements, callRefs) };
  }
  return {
    type: 'statement', label: ts.SyntaxKind[member.kind], source: semanticSource(file, member),
    expression: member.getText(file.sourceFile), raw_text: member.getText(file.sourceFile), resolution: 'partial',
    effects: expressionEffects(ts, file, member, callRefs),
  };
}

function projectStatements(ts, file, statements, callRefs) {
  return [...statements].map(statement => projectStatement(ts, file, statement, callRefs));
}

function projectBody(ts, file, body, callRefs) {
  return ts.isBlock(body) ? projectStatements(ts, file, body.statements, callRefs) : [projectStatement(ts, file, body, callRefs)];
}

function projectStatement(ts, file, node, callRefs) {
  const source = semanticSource(file, node);
  const rawText = node.getText(file.sourceFile);
  if (ts.isBlock(node)) return { type: 'block', label: 'block', source, steps: projectStatements(ts, file, node.statements, callRefs) };
  if (ts.isIfStatement(node)) {
    return {
      type: 'branch', label: 'if', source,
      condition: node.expression.getText(file.sourceFile),
      condition_steps: expressionEffects(ts, file, node.expression, callRefs),
      steps: projectBody(ts, file, node.thenStatement, callRefs),
      else_steps: node.elseStatement ? projectBody(ts, file, node.elseStatement, callRefs) : [],
    };
  }
  if (ts.isForStatement(node) || ts.isForOfStatement(node) || ts.isForInStatement(node) || ts.isWhileStatement(node) || ts.isDoStatement(node)) {
    const condition = node.condition || node.expression;
    const iterator = ts.isForOfStatement(node) || ts.isForInStatement(node)
      ? `${node.initializer.getText(file.sourceFile)} ${ts.isForOfStatement(node) ? 'of' : 'in'} ${node.expression.getText(file.sourceFile)}`
      : ts.isForStatement(node)
        ? [node.initializer?.getText(file.sourceFile), node.incrementor?.getText(file.sourceFile)].filter(Boolean).join('; ')
        : '';
    const initialization = ts.isForStatement(node) ? node.initializer : null;
    const incrementor = ts.isForStatement(node) ? node.incrementor : null;
    return {
      type: 'loop', label: ts.SyntaxKind[node.kind], source,
      condition: condition?.getText(file.sourceFile) || '', iterator,
      initialization_steps: initialization ? expressionEffects(ts, file, initialization, callRefs) : [],
      condition_steps: condition ? expressionEffects(ts, file, condition, callRefs) : [],
      increment_steps: incrementor ? expressionEffects(ts, file, incrementor, callRefs) : [],
      steps: projectBody(ts, file, node.statement, callRefs),
    };
  }
  if (ts.isSwitchStatement(node)) {
    return {
      type: 'branch', label: 'switch', source,
      expression: node.expression.getText(file.sourceFile),
      condition_steps: expressionEffects(ts, file, node.expression, callRefs),
      cases: node.caseBlock.clauses.map(clause => ({
        type: 'case',
        label: ts.isDefaultClause(clause) ? 'default' : 'case',
        source: semanticSource(file, clause),
        condition: ts.isDefaultClause(clause) ? '' : clause.expression.getText(file.sourceFile),
        condition_steps: ts.isDefaultClause(clause) ? [] : expressionEffects(ts, file, clause.expression, callRefs),
        steps: projectStatements(ts, file, clause.statements, callRefs),
      })),
      else_steps: [],
    };
  }
  if (ts.isTryStatement(node)) {
    const catches = node.catchClause ? [{
      type: 'catch', label: 'catch', source: semanticSource(file, node.catchClause),
      target: node.catchClause.variableDeclaration?.name.getText(file.sourceFile) || '',
      expression: node.catchClause.variableDeclaration?.type?.getText(file.sourceFile) || '',
      steps: projectStatements(ts, file, node.catchClause.block.statements, callRefs),
    }] : [];
    return {
      type: 'try', label: 'try', source,
      steps: projectStatements(ts, file, node.tryBlock.statements, callRefs), catches,
      finally_steps: node.finallyBlock ? projectStatements(ts, file, node.finallyBlock.statements, callRefs) : [],
    };
  }
  if (ts.isReturnStatement(node)) {
    return {
      type: 'output', label: 'return', source,
      expression: node.expression?.getText(file.sourceFile) || '',
      effects: node.expression ? expressionEffects(ts, file, node.expression, callRefs) : [],
    };
  }
  if (ts.isThrowStatement(node)) {
    return {
      type: 'raise', label: 'throw', source,
      expression: node.expression.getText(file.sourceFile),
      effects: expressionEffects(ts, file, node.expression, callRefs),
    };
  }
  if (ts.isVariableStatement(node)) {
    const assignments = node.declarationList.declarations.map(declaration => ({
      type: 'assignment', label: 'declare', source: semanticSource(file, declaration), action: 'DECLARE',
      target: declaration.name.getText(file.sourceFile),
      expression: declaration.initializer?.getText(file.sourceFile) || '',
      effects: declaration.initializer ? expressionEffects(ts, file, declaration.initializer, callRefs) : [],
    }));
    return assignments.length === 1 ? assignments[0] : { type: 'block', label: 'declarations', source, steps: assignments };
  }
  if (ts.isExpressionStatement(node)) return projectExpressionStatement(ts, file, node, callRefs);
  if (ts.isBreakStatement(node) || ts.isContinueStatement(node)) return { type: 'control', label: rawText, source, action: ts.isBreakStatement(node) ? 'BREAK' : 'CONTINUE' };
  return {
    type: 'statement', label: ts.SyntaxKind[node.kind], source,
    expression: rawText, raw_text: rawText, resolution: 'partial',
    effects: expressionEffects(ts, file, node, callRefs),
  };
}

function projectExpressionStatement(ts, file, statement, callRefs) {
  const expression = statement.expression;
  const source = semanticSource(file, statement);
  if (ts.isBinaryExpression(expression) && isAssignmentOperator(ts, expression.operatorToken.kind)) {
    return {
      type: 'assignment', label: expression.operatorToken.getText(file.sourceFile), source,
      action: ts.SyntaxKind[expression.operatorToken.kind],
      target: expression.left.getText(file.sourceFile), expression: expression.right.getText(file.sourceFile),
      effects: [
        ...expressionEffects(ts, file, expression.left, callRefs),
        ...expressionEffects(ts, file, expression.right, callRefs),
      ],
    };
  }
  if (ts.isPrefixUnaryExpression(expression) || ts.isPostfixUnaryExpression(expression)) {
    return { type: 'effect', label: expression.getText(file.sourceFile), source, action: ts.SyntaxKind[expression.operator], expression: expression.getText(file.sourceFile) };
  }
  if (ts.isCallExpression(expression) || ts.isAwaitExpression(expression) && ts.isCallExpression(expression.expression)) {
    return callFact(ts, file, ts.isAwaitExpression(expression) ? expression.expression : expression, callRefs);
  }
  return {
    type: 'statement', label: 'expression', source,
    expression: expression.getText(file.sourceFile), resolution: 'partial',
    effects: expressionEffects(ts, file, expression, callRefs),
  };
}

function isAssignmentOperator(ts, kind) {
  return kind >= ts.SyntaxKind.FirstAssignment && kind <= ts.SyntaxKind.LastAssignment;
}

function expressionEffects(ts, file, node, callRefs) {
  if (ts.isCallExpression(node)) return [callFact(ts, file, node, callRefs)];
  if (ts.isAwaitExpression(node)) return expressionEffects(ts, file, node.expression, callRefs);
  if (ts.isConditionalExpression(node)) {
    return [{
      type: 'branch', label: 'conditional expression', source: semanticSource(file, node),
      condition: node.condition.getText(file.sourceFile), expression: node.getText(file.sourceFile),
      condition_steps: expressionEffects(ts, file, node.condition, callRefs),
      then_expression: node.whenTrue.getText(file.sourceFile),
      steps: expressionEffects(ts, file, node.whenTrue, callRefs),
      else_expression: node.whenFalse.getText(file.sourceFile),
      else_steps: expressionEffects(ts, file, node.whenFalse, callRefs),
    }];
  }
  if (ts.isBinaryExpression(node) && ['&&', '||', '??'].includes(node.operatorToken.getText(file.sourceFile))) {
    return [{
      type: 'branch', label: node.operatorToken.getText(file.sourceFile), source: semanticSource(file, node),
      condition: node.left.getText(file.sourceFile), expression: node.getText(file.sourceFile),
      condition_steps: expressionEffects(ts, file, node.left, callRefs),
      then_expression: node.right.getText(file.sourceFile),
      steps: expressionEffects(ts, file, node.right, callRefs), else_steps: [],
    }];
  }
  if (ts.isBinaryExpression(node) && isAssignmentOperator(ts, node.operatorToken.kind)) {
    return [{
      type: 'assignment', label: node.operatorToken.getText(file.sourceFile), source: semanticSource(file, node),
      action: ts.SyntaxKind[node.operatorToken.kind], target: node.left.getText(file.sourceFile),
      expression: node.right.getText(file.sourceFile),
      effects: [
        ...expressionEffects(ts, file, node.left, callRefs),
        ...expressionEffects(ts, file, node.right, callRefs),
      ],
    }];
  }
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    const steps = ts.isBlock(node.body)
      ? projectStatements(ts, file, node.body.statements, callRefs)
      : [{ type: 'output', label: 'return', source: semanticSource(file, node.body), expression: node.body.getText(file.sourceFile), effects: expressionEffects(ts, file, node.body, callRefs) }];
    return [{
      type: 'callback', label: node.name?.getText(file.sourceFile) || 'callback', source: semanticSource(file, node),
      parameters: semanticParameters(node), steps,
    }];
  }
  if (ts.isPrefixUnaryExpression(node) || ts.isPostfixUnaryExpression(node)) {
    return [{ type: 'effect', label: node.getText(file.sourceFile), source: semanticSource(file, node), action: ts.SyntaxKind[node.operator], expression: node.getText(file.sourceFile) }];
  }
  const facts = [];
  node.forEachChild(child => facts.push(...expressionEffects(ts, file, child, callRefs)));
  return facts;
}

function callFact(ts, file, call, callRefs) {
  const ref = callRefs.get(call.getStart(file.sourceFile));
  const expression = call.expression.getText(file.sourceFile);
  const fact = {
    type: ref?.type || 'call',
    label: ref?.label || `Call ${expression}`,
    source: semanticSource(file, call),
    target: expression,
    arguments: call.arguments.map(argument => argument.getText(file.sourceFile)),
    awaited: ts.isAwaitExpression(call.parent),
    resolution: ref ? ref.resolution || 'resolved' : 'unresolved',
  };
  const effects = [
    ...expressionEffects(ts, file, call.expression, callRefs),
    ...call.arguments.flatMap(argument => expressionEffects(ts, file, argument, callRefs)),
  ];
  if (effects.length) fact.effects = effects;
  if (ref?.ref_node_id) fact.ref_node_id = ref.ref_node_id;
  if (ref?.ref_node_ids?.length) fact.ref_node_ids = ref.ref_node_ids;
  return fact;
}

function configuredFiles(config, extensions, discovery = discoverAngularProject(config)) {
  const root = discovery.root;
  const folders = discovery.folders.length ? discovery.folders : ['.'];
  const result = [];
  for (const folderValue of folders) {
    const folder = path.resolve(root, folderValue || '.');
    if (!existsSync(folder)) continue;
    for (const absolute of walk(folder)) {
      if (!extensions.includes(path.extname(absolute).toLowerCase())) continue;
      if (excludedPath(absolute, root, discovery.exclusions)) continue;
      const text = readFileSync(absolute, 'utf8');
      result.push({
        absolute,
        relative: repoPath(path.relative(root, absolute)),
        text,
        sourceFile: textSourceFile(absolute, text),
      });
    }
  }
  result.sort((a, b) => a.relative.localeCompare(b.relative));
  return result;
}

function nearestAngularJson(root, folders) {
  const candidates = [...(folders || []).map(value => path.resolve(root, typeof value === 'object' ? value.path || '.' : value || '.')), root];
  for (const candidate of candidates) {
    let current = existsSync(candidate) && statSync(candidate).isFile() ? path.dirname(candidate) : candidate;
    while (current === root || current.startsWith(root + path.sep)) {
      for (const name of ['angular.json', '.angular-cli.json', 'angular-cli.json']) {
        const angularJson = path.join(current, name);
        if (existsSync(angularJson)) return angularJson;
      }
      if (current === root) break;
      current = path.dirname(current);
    }
  }
  for (const name of ['angular.json', '.angular-cli.json', 'angular-cli.json']) {
    const angularJson = path.join(root, name);
    if (existsSync(angularJson)) return angularJson;
  }
  return path.join(root, 'angular.json');
}

function discoverAngularProject(config) {
  const root = path.resolve(config.root);
  const angularJsonPath = nearestAngularJson(root, config.folders);
  const angularJson = readJsonIfExists(angularJsonPath) || {};
  const projectName = angularProjectName(angularJson, config);
  const project = projectName ? angularJson.projects?.[projectName] || {} : {};
  const workspaceRoot = existsSync(angularJsonPath) ? path.dirname(angularJsonPath) : root;
  const tsconfigPath = angularTsconfigPath(config, root, workspaceRoot, project);
  const tsconfig = readJsonIfExists(tsconfigPath) || {};
  return {
    root,
    angularJsonPath: existsSync(angularJsonPath) ? angularJsonPath : '',
    angularProjectName: projectName,
    tsconfigPath: tsconfigPath && existsSync(tsconfigPath) ? tsconfigPath : '',
    folders: scanFolders(config, root, workspaceRoot, project),
    exclusions: scanExclusions(config, tsconfig),
  };
}

function readJsonIfExists(filePath) {
  if (!filePath || !existsSync(filePath)) return null;
  try {
    return JSON.parse(readFileSync(filePath, 'utf8'));
  } catch {
    return null;
  }
}

function angularProjectName(angularJson, config) {
  const projects = angularJson?.projects || {};
  if (config.project && projects[config.project]) return config.project;
  if (config.source && projects[config.source]) return config.source;
  if (angularJson?.defaultProject && projects[angularJson.defaultProject]) return angularJson.defaultProject;
  return Object.keys(projects)[0] || '';
}

function angularTsconfigPath(config, root, workspaceRoot, project) {
  const configured = config.tsconfig || config.tsConfig;
  if (configured) return path.resolve(root, configured);
  const options = project?.architect?.build?.options || project?.targets?.build?.options || {};
  if (options.tsConfig) return path.resolve(workspaceRoot, options.tsConfig);
  for (const candidate of ['tsconfig.app.json', 'tsconfig.json']) {
    const absolute = path.join(workspaceRoot, candidate);
    if (existsSync(absolute)) return absolute;
  }
  return '';
}

function scanFolders(config, root, workspaceRoot, project) {
  const configured = Array.isArray(config.folders) ? config.folders : [];
  const values = configured.map(item => (typeof item === 'string' ? item : item?.path || '')).filter(Boolean);
  if (values.length) return unique(values.map(value => repoPath(value)));
  const workspacePrefix = repoPath(path.relative(root, workspaceRoot));
  const sourceRoot = project?.sourceRoot || (project?.root ? path.posix.join(repoPath(project.root), 'src') : 'src');
  const fallback = path.posix.join(workspacePrefix, repoPath(sourceRoot), 'app');
  return [existsSync(path.resolve(root, fallback)) ? fallback : '.'];
}

function scanExclusions(config, tsconfig) {
  const configured = [config.exclude, config.excludes, config.exclusions].flat().filter(Boolean);
  const tsExcluded = Array.isArray(tsconfig.exclude) ? tsconfig.exclude : [];
  return unique([...configured, ...tsExcluded].map(pattern => repoPath(pattern)).filter(Boolean));
}

function excludedPath(absolute, root, extraExclusions = []) {
  const relative = repoPath(path.relative(root, absolute));
  const parts = relative.split('/').map(part => part.toLowerCase());
  const blocked = new Set(['node_modules', 'dist', '.angular', 'generated', 'bin', 'obj', '__pycache__']);
  if (parts.some(part => blocked.has(part))) return true;
  if (/\.(spec|test)\.[^.]+$/i.test(path.basename(absolute))) return true;
  return extraExclusions.some(pattern => pathMatchesPattern(relative, pattern));
}

function pathMatchesPattern(relative, pattern) {
  let value = repoPath(String(pattern || '').trim()).replace(/^\.\//, '');
  if (!value) return false;
  if (value.endsWith('/')) value += '**';
  const target = repoPath(relative);
  if (!/[?*\[]/.test(value)) {
    return target === value || target.startsWith(`${value}/`) || target.endsWith(`/${value}`) || path.basename(target) === value;
  }
  return new RegExp(`^${globToRegex(value)}$`).test(target);
}

function globToRegex(pattern) {
  let regex = '';
  for (let index = 0; index < pattern.length; index += 1) {
    const char = pattern[index];
    const next = pattern[index + 1];
    if (char === '*' && next === '*') {
      if (pattern[index + 2] === '/') {
        regex += '(?:.*/)?';
        index += 2;
      } else {
        regex += '.*';
        index += 1;
      }
    } else if (char === '*') {
      regex += '[^/]*';
    } else if (char === '?') {
      regex += '[^/]';
    } else {
      regex += char.replace(/[|\\{}()[\]^$+?.]/g, '\\$&');
    }
  }
  return regex;
}

function unique(values) {
  return [...new Set(values)];
}

function textSourceFile(fileName, text) {
  const ts = loadTypeScript();
  return ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true);
}

function* walk(dir) {
  for (const name of readdirSync(dir).sort()) {
    const absolute = path.join(dir, name);
    const stat = statSync(absolute);
    if (stat.isDirectory()) yield* walk(absolute);
    else yield absolute;
  }
}

function visit(node, fn) {
  fn(node);
  node.forEachChild(child => visit(child, fn));
}

function stringProp(ts, object, name) {
  for (const prop of object.properties) {
    if (!ts.isPropertyAssignment(prop)) continue;
    if (prop.name?.getText(object.getSourceFile()).replace(/^['"]|['"]$/g, '') !== name) continue;
    if (ts.isStringLiteralLike(prop.initializer)) return prop.initializer.text;
  }
  return '';
}

function identifierOrStringProp(ts, object, name) {
  const prop = propertyAssignment(ts, object, name);
  if (!prop) return '';
  if (ts.isStringLiteralLike(prop.initializer)) return prop.initializer.text;
  if (ts.isIdentifier(prop.initializer)) return prop.initializer.text;
  return '';
}

function propertyAssignment(ts, object, name) {
  for (const prop of object.properties) {
    if (!ts.isPropertyAssignment(prop)) continue;
    if (prop.name?.getText(object.getSourceFile()).replace(/^['"]|['"]$/g, '') !== name) continue;
    return prop;
  }
  return null;
}

function routeTarget(ts, object, normalizedRoute) {
  const component = identifierOrStringProp(ts, object, 'component');
  if (component) return { name: component, kind: 'component' };
  const loadComponent = propertyAssignment(ts, object, 'loadComponent');
  if (loadComponent) {
    return { name: lazyExportName(ts, loadComponent.initializer) || `${pascalName(displayFromRoute(normalizedRoute))}Component`, kind: 'loadComponent' };
  }
  const loadChildren = propertyAssignment(ts, object, 'loadChildren');
  if (loadChildren) {
    return { name: lazyExportName(ts, loadChildren.initializer) || `${pascalName(displayFromRoute(normalizedRoute))}LazyRoute`, kind: 'loadChildren' };
  }
  return null;
}

function lazyExportName(ts, initializer) {
  let preferred = '';
  let fallback = '';
  visit(initializer, node => {
    if (!ts.isPropertyAccessExpression(node)) return;
    const name = node.name.text;
    if (!/^[A-Z][A-Za-z0-9_]*$/.test(name)) return;
    if (/Component$/.test(name)) preferred ||= name;
    else fallback ||= name;
  });
  return preferred || fallback;
}

function inlineConfig(ts, sourceFile) {
  const values = {};
  visit(sourceFile, node => {
    if (!ts.isVariableDeclaration(node) || !node.initializer || !ts.isObjectLiteralExpression(node.initializer)) return;
    const name = node.name.getText(sourceFile);
    if (!/^(orderApiConfig|apiConfig|appConfig)$/.test(name)) return;
    for (const prop of node.initializer.properties) {
      if (!ts.isPropertyAssignment(prop) || !ts.isStringLiteralLike(prop.initializer)) continue;
      values[prop.name.getText(sourceFile).replace(/^['"]|['"]$/g, '')] = prop.initializer.text;
    }
  });
  return values;
}

function httpCall(ts, node) {
  if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) return null;
  const method = node.expression.name.text.toUpperCase();
  if (!['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'REQUEST'].includes(method)) return null;
  const target = node.expression.expression.getText(node.getSourceFile());
  if (!/\bthis\.http$|\bhttp$/.test(target)) return null;
  if (method === 'REQUEST') return { method: '', methodArgument: node.arguments[0], argument: node.arguments[1] };
  return { method, methodArgument: null, argument: node.arguments[0] };
}

function localResolutionContext(ts, node, config) {
  const context = { urls: new Map(), strings: new Map(), issues: new Map(), configAliases: new Set(['this.config']) };
  const root = nearestResolutionRoot(ts, node);
  visit(root, candidate => {
    if (!ts.isVariableDeclaration(candidate) || !candidate.initializer) return;
    if (ts.isObjectBindingPattern(candidate.name) && configAccessKey(ts, candidate.initializer, context)) {
      for (const element of candidate.name.elements) {
        if (!ts.isIdentifier(element.name)) continue;
        const localName = element.name.text;
        const key = element.propertyName ? element.propertyName.getText(candidate.getSourceFile()).replace(/^['"]|['"]$/g, '') : localName;
        if (config[key]) context.urls.set(localName, config[key]);
        else context.issues.set(localName, `this.config.${key}`);
      }
      return;
    }
    if (!ts.isIdentifier(candidate.name)) return;
    const name = candidate.name.text;
    if (isConfigExpression(candidate.initializer, context)) {
      context.configAliases.add(name);
    }
    if (ts.isStringLiteralLike(candidate.initializer) || ts.isNoSubstitutionTemplateLiteral(candidate.initializer)) {
      context.strings.set(name, candidate.initializer.text);
    }
    const resolved = resolveUrl(ts, candidate.initializer, config, context);
    if (resolved.route) context.urls.set(name, resolved.route);
    else if (resolved.issue) context.issues.set(name, resolved.issue);
  });
  return context;
}

function nearestResolutionRoot(ts, node) {
  let current = node.parent;
  while (current) {
    if (ts.isBlock(current) || ts.isSourceFile(current)) return current;
    current = current.parent;
  }
  return node.getSourceFile();
}

function resolveHttpMethod(ts, expression, context) {
  if (!expression) return '';
  if (ts.isStringLiteralLike(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) return expression.text.toUpperCase();
  if (ts.isIdentifier(expression) && context.strings.has(expression.text)) return String(context.strings.get(expression.text)).toUpperCase();
  return '';
}

function resolveUrl(ts, expression, config, context = { urls: new Map(), strings: new Map(), issues: new Map() }) {
  if (!expression) return { route: '', issue: '' };
  if (ts.isIdentifier(expression)) {
    if (context.urls.has(expression.text)) return { route: context.urls.get(expression.text), issue: '' };
    if (context.issues.has(expression.text)) return { route: '', issue: context.issues.get(expression.text) };
    return { route: '', issue: '' };
  }
  if (ts.isStringLiteralLike(expression)) return { route: expression.text, issue: '' };
  if (ts.isNoSubstitutionTemplateLiteral(expression)) return { route: expression.text, issue: '' };
  if (ts.isTemplateExpression(expression)) return resolveTemplateUrl(ts, expression, config, context);
  if (ts.isBinaryExpression(expression) && expression.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = resolveUrl(ts, expression.left, config, context);
    if (left.issue) return left;
    const right = resolveUrl(ts, expression.right, config, context);
    if (right.issue) return right;
    if (left.route || right.route) return { route: `${left.route || '{id}'}${right.route || '{id}'}`, issue: '' };
  }
  if (ts.isCallExpression(expression) && ts.isPropertyAccessExpression(expression.expression) && expression.expression.name.text === 'replace') {
    return resolveUrl(ts, expression.expression.expression, config, context);
  }
  if (ts.isPropertyAccessExpression(expression)) {
    const key = configAccessKey(ts, expression, context);
    if (key) {
      return { route: config[key] || '', issue: config[key] ? '' : `this.config.${key}` };
    }
  }
  if (ts.isElementAccessExpression(expression)) {
    const key = configAccessKey(ts, expression, context);
    if (key) {
      return { route: config[key] || '', issue: config[key] ? '' : `this.config.${key}` };
    }
    if (isConfigExpression(expression.expression, context) || configAccessKey(ts, expression.expression, context)) {
      const arg = expression.argumentExpression;
      if (arg && ts.isStringLiteralLike(arg)) {
        return { route: config[arg.text] || '', issue: config[arg.text] ? '' : `this.config[${arg.getText(expression.getSourceFile())}]` };
      }
      return { route: '', issue: `this.config[${arg?.getText(expression.getSourceFile()) || ''}]` };
    }
  }
  return { route: '', issue: '' };
}

function resolveTemplateUrl(ts, expression, config, context) {
  let route = expression.head.text;
  let hasResolvedBase = routeLooksLikePath(route);
  for (const span of expression.templateSpans) {
    const resolved = resolveUrl(ts, span.expression, config, context);
    if (resolved.issue) return resolved;
    if (resolved.route) {
      route += resolved.route;
      hasResolvedBase = true;
    } else {
      route += '{id}';
    }
    route += span.literal.text;
    hasResolvedBase ||= routeLooksLikePath(span.literal.text);
  }
  return hasResolvedBase ? { route, issue: '' } : { route: '', issue: '' };
}

function routeLooksLikePath(text) {
  return /^\s*(\/|https?:\/\/|\/\/)/i.test(String(text || ''));
}

function isConfigExpression(expression, context = null) {
  if (!expression) return false;
  const text = expression.getText(expression.getSourceFile());
  if (context?.configAliases?.has(text)) return true;
  return text === 'this.config' || text.endsWith('.config');
}

function configAccessKey(ts, expression, context = null) {
  if (!expression) return '';
  if (isConfigExpression(expression, context)) return '__config__';
  if (ts.isPropertyAccessExpression(expression)) {
    if (isConfigExpression(expression.expression, context) || configAccessKey(ts, expression.expression, context)) return expression.name.text;
  }
  if (ts.isElementAccessExpression(expression)) {
    if (!(isConfigExpression(expression.expression, context) || configAccessKey(ts, expression.expression, context))) return '';
    const arg = expression.argumentExpression;
    if (arg && (ts.isStringLiteralLike(arg) || ts.isNoSubstitutionTemplateLiteral(arg))) return arg.text;
    return '';
  }
  return '';
}

function componentServiceReceivers(ts, classNode, serviceIds, typeChecker) {
  const receivers = new Map();
  const serviceClasses = [...serviceIds.keys()];
  for (const member of classNode.members) {
    if (ts.isConstructorDeclaration(member)) {
      for (const parameter of member.parameters) {
        if (!ts.isIdentifier(parameter.name)) continue;
        const serviceClass = parameterServiceClass(parameter, serviceIds, serviceClasses, typeChecker);
        if (!serviceClass) continue;
        receivers.set(`this.${parameter.name.text}`, serviceClass);
      }
    }
    if (ts.isPropertyDeclaration(member) && member.name && ts.isIdentifier(member.name)) {
      const serviceClass = typedServiceClass(member.type, serviceIds, typeChecker, member) || inferredServiceClass(member.name.text, serviceClasses);
      if (serviceClass) receivers.set(`this.${member.name.text}`, serviceClass);
    }
  }
  if (!receivers.size && serviceClasses.length === 1) receivers.set('this.service', serviceClasses[0]);
  return receivers;
}

function parameterServiceClass(parameter, serviceIds, serviceClasses, typeChecker) {
  return typedServiceClass(parameter.type, serviceIds, typeChecker, parameter) || inferredServiceClass(parameter.name.text, serviceClasses);
}

function typedServiceClass(typeNode, serviceIds, typeChecker, node) {
  if (!typeNode && !typeChecker) return '';

  if (typeChecker && node) {
      try {
          const type = typeChecker.getTypeAtLocation(node);
          if (type && type.symbol) {
              const name = type.symbol.name;
              if (serviceIds.has(name)) return name;
          }
      } catch (e) {
          // Fallback to AST
      }
  }

  if (!typeNode) return '';
  const text = typeNode.getText(typeNode.getSourceFile()).replace(/<.*>$/g, '').trim();
  return serviceIds.has(text) ? text : '';
}

function inferredServiceClass(name, serviceClasses) {
  if (serviceClasses.length !== 1) return '';
  return /service$/i.test(name) || name === 'service' ? serviceClasses[0] : '';
}

function serviceCall(ts, node, serviceReceivers = new Map(), typeChecker) {
  if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) return '';
  const receiver = node.expression.expression.getText(node.getSourceFile());

  if (typeChecker) {
      try {
          const type = typeChecker.getTypeAtLocation(node.expression.expression);
          if (type && type.symbol) {
              const serviceClass = type.symbol.name;
              if (serviceReceivers.has(receiver) || serviceClass.endsWith('Service')) {
                  return { method: node.expression.name.text, serviceClass: serviceClass };
              }
          }
      } catch (e) {
          // Fallback to AST
      }
  }

  if (serviceReceivers.has(receiver)) return { method: node.expression.name.text, serviceClass: serviceReceivers.get(receiver) };
  if (receiver === 'this.service' || receiver.endsWith('._service')) return { method: node.expression.name.text, serviceClass: '' };
  return '';
}

function ownerMethodName(ts, node) {
  let current = node.parent;
  while (current) {
    if (ts.isMethodDeclaration(current) && current.name) return current.name.getText(current.getSourceFile());
    if (ts.isPropertyDeclaration(current) && current.name && current.initializer && (ts.isArrowFunction(current.initializer) || ts.isFunctionExpression(current.initializer))) {
      return current.name.getText(current.getSourceFile());
    }
    current = current.parent;
  }
  return '';
}

function buildTemplateEventIndex(ts, files) {
  const index = { byComponent: new Map(), global: new Map() };
  const byAbsolute = new Map(files.map(file => [file.absolute, file]));
  for (const file of files) {
    for (const event of templateEvents(file.text, file.relative)) pushMap(index.global, event.method, event);
  }
  for (const file of files.filter(item => item.absolute.endsWith('.ts'))) {
    visit(file.sourceFile, node => {
      if (!ts.isClassDeclaration(node) || !node.name) return;
      const className = node.name.text;
      const componentEvents = [];
      for (const template of componentTemplates(ts, node, file, byAbsolute)) {
        componentEvents.push(...templateEvents(template.text, template.sourcePath, template.lineOffset));
      }
      for (const event of componentEvents) pushNestedMap(index.byComponent, className, event.method, event);
    });
  }
  return index;
}

function componentTemplates(ts, classNode, file, byAbsolute) {
  const templates = [];
  for (const decorator of decoratorsOf(ts, classNode)) {
    const metadata = componentDecoratorMetadata(ts, decorator);
    if (!metadata) continue;
    const inline = propertyAssignment(ts, metadata, 'template');
    if (inline?.initializer && (ts.isStringLiteralLike(inline.initializer) || ts.isNoSubstitutionTemplateLiteral(inline.initializer))) {
      templates.push({ text: inline.initializer.text, sourcePath: file.relative, lineOffset: lineFor(file.sourceFile, inline.initializer.getStart(file.sourceFile)) - 1 });
    }
    const external = propertyAssignment(ts, metadata, 'templateUrl');
    if (external?.initializer && ts.isStringLiteralLike(external.initializer)) {
      const templateFile = byAbsolute.get(path.resolve(path.dirname(file.absolute), external.initializer.text));
      if (templateFile) templates.push({ text: templateFile.text, sourcePath: templateFile.relative, lineOffset: 0 });
    }
  }
  if (!templates.length) {
    for (const candidate of matchingHtmlTemplates(classNode.name.text, file, byAbsolute)) {
      templates.push({ text: candidate.text, sourcePath: candidate.relative, lineOffset: 0 });
    }
  }
  return templates;
}

function decoratorsOf(ts, node) {
  if (ts.canHaveDecorators && ts.getDecorators && ts.canHaveDecorators(node)) return ts.getDecorators(node) || [];
  return node.decorators ? [...node.decorators] : [];
}

function componentDecoratorMetadata(ts, decorator) {
  const expression = decorator.expression;
  if (!ts.isCallExpression(expression)) return null;
  const name = expression.expression.getText(expression.getSourceFile()).split('.').pop();
  if (name !== 'Component') return null;
  const [metadata] = expression.arguments;
  return metadata && ts.isObjectLiteralExpression(metadata) ? metadata : null;
}

function matchingHtmlTemplates(className, file, byAbsolute) {
  const stem = path.basename(file.absolute).replace(/\.ts$/i, '');
  const classStem = kebabName(className.replace(/Component$/i, ''));
  const candidates = unique([
    `${stem}.html`,
    `${stem.replace(/\.component$/i, '')}.html`,
    `${classStem}.component.html`,
    `${classStem}.html`,
  ]).map(name => path.resolve(path.dirname(file.absolute), name));
  return candidates.map(candidate => byAbsolute.get(candidate)).filter(Boolean);
}

function eventsForComponentMethod(templateIndex, className, methodName) {
  const scoped = templateIndex.byComponent.get(className)?.get(methodName) || [];
  return scoped.length ? scoped : templateIndex.global.get(methodName) || [];
}

function pushNestedMap(map, outerKey, innerKey, value) {
  if (!map.has(outerKey)) map.set(outerKey, new Map());
  pushMap(map.get(outerKey), innerKey, value);
}

function templateEvents(text, sourcePath = '', lineOffset = 0) {
  const events = [];
  const regex = /\(([A-Za-z][\w:-]*)\)\s*=\s*['"]([^'"]+)['"]/gi;
  for (const match of text.matchAll(regex)) {
    const method = eventMethodName(match[2]);
    if (!method) continue;

    // Extract surrounding element context for metadata
    const beforeMatch = text.slice(0, match.index);
    const elementStart = beforeMatch.lastIndexOf('<');
    let metadata = {};
    if (elementStart >= 0) {
        const elementTagMatch = beforeMatch.slice(elementStart).match(/^<([a-zA-Z0-9-]+)/);
        if (elementTagMatch) {
            metadata.element = elementTagMatch[1];
        }

        // Look for routerLink in the same element
        const elementEnd = text.indexOf('>', match.index);
        if (elementEnd >= 0) {
            const elementContent = text.slice(elementStart, elementEnd);
            const routerLinkMatch = elementContent.match(/routerLink\s*=\s*['"]([^'"]+)['"]/);
            if (routerLinkMatch) {
                metadata.routerLink = routerLinkMatch[1];
            }
            const routerLinkBindingMatch = elementContent.match(/\[routerLink\]\s*=\s*['"]([^'"]+)['"]/);
            if (routerLinkBindingMatch) {
                metadata.routerLinkBinding = routerLinkBindingMatch[1];
            }
        }
    }

    events.push({ method, snippet: match[0], line: lineOffset + text.slice(0, match.index).split(/\r?\n/).length, sourcePath, metadata: Object.keys(metadata).length > 0 ? metadata : undefined });
  }
  return events;
}

function eventMethodName(expression) {
  const match = String(expression || '').trim().match(/^([A-Za-z_$][\w$]*)\s*\(/);
  return match ? match[1] : '';
}

function loadAppConfig(config) {
  const value = config.appConfig;
  if (!value) return {};
  const candidate = path.isAbsolute(value) ? value : path.resolve(config.root, value);
  if (!existsSync(candidate)) return {};
  try {
    const data = JSON.parse(readFileSync(candidate, 'utf8'));
    return Object.fromEntries(Object.entries(data).filter(([, v]) => typeof v === 'string'));
  } catch {
    return {};
  }
}

function routeQualityIssues(config, builder, files, source) {
  const checks = config.routeChecks || [];
  if (!checks.length) return;
  for (const check of checks) {
    const file = check.sourcePath
      ? files.find(f => f.relative === check.sourcePath) || files[0]
      : files.find(f => lineContaining(f.text, check.rawReference || '')) || files[0];
    const sourcePath = check.sourcePath || file?.relative || '';
    const line = check.line || lineContaining(file?.text || '', check.rawReference || '') || 1;
    let sourceNodeId = check.sourceNodeId || '';
    if (!sourceNodeId && check.apiCall) {
      const [method, ...route] = check.apiCall.split(' ');
      sourceNodeId = apiCallId(source, method, route.join(' '));
    }
    builder.addIssue(check.issueType, check.severity || 'WARNING', check.message || 'Route could not be resolved', {
      source_node_id: sourceNodeId,
      raw_reference: check.rawReference || '',
      database_key: check.database || '',
      source_path: sourcePath,
      start_line: Number(line),
    });
  }
}

class PackageBuilder {
  constructor(packageId, sourceId, extractorName, extractorVersion, metadata) {
    this.packageId = packageId;
    this.sourceId = sourceId;
    this.extractorName = extractorName;
    this.extractorVersion = extractorVersion;
    this.metadata = metadata || {};
    this.filesScanned = 0;
    this.nodes = new Map();
    this.edges = new Map();
    this.evidence = new Map();
    this.issues = new Map();
    this.localizedTexts = new Map();
  }

  addNode(nodeId, nodeType, technicalName, qualifiedName, displayName, options = {}) {
    if (!this.nodes.has(nodeId)) {
      this.nodes.set(nodeId, {
        node_id: nodeId,
        node_type: nodeType,
        technical_name: technicalName,
        qualified_name: qualifiedName,
        default_display_name: displayName || technicalName,
        system_key: options.system_key || '',
        database_key: options.database_key || '',
        repository_key: options.repository_key || '',
        graph_role: options.graph_role || 'MAIN',
        confidence: String(Number(options.confidence ?? 1.0)),
        properties_json: json(options.properties || {}),
      });
    }
    return nodeId;
  }

  setNodeProperty(nodeId, name, value) {
    const node = this.nodes.get(nodeId);
    if (!node) throw new Error(`Node not found: ${nodeId}`);
    const properties = JSON.parse(node.properties_json || '{}');
    properties[name] = value;
    node.properties_json = json(properties);
  }

  addEdge(sourceNodeId, targetNodeId, edgeType, options = {}) {
    const graphLayer = options.graph_layer || 'TECHNICAL';
    const rawOperation = options.raw_operation || '';
    const edgeId = canonicalEdgeId(sourceNodeId, edgeType, targetNodeId, rawOperation, graphLayer);
    if (!this.edges.has(edgeId)) {
      this.edges.set(edgeId, {
        edge_id: edgeId,
        source_node_id: sourceNodeId,
        target_node_id: targetNodeId,
        edge_type: edgeType,
        graph_layer: graphLayer,
        raw_operation: rawOperation,
        confidence: String(Number(options.confidence ?? 1.0)),
        properties_json: json(options.properties || {}),
      });
    }
    return edgeId;
  }

  addEvidence(targetType, targetId, sourcePath, startLine, endLine, evidenceKind, snippet, options = {}) {
    const cleanPath = repoPath(sourcePath);
    const identity = `${targetType}|${targetId}|${cleanPath}|${startLine || ''}|${endLine || ''}|${evidenceKind}|${String(snippet).trim()}`;
    const evidenceId = 'ev:' + sha256(identity).slice(0, 24);
    if (!this.evidence.has(evidenceId)) {
      this.evidence.set(evidenceId, {
        evidence_id: evidenceId,
        target_type: targetType,
        target_id: targetId,
        source_path: cleanPath,
        start_line: String(startLine || ''),
        end_line: String(endLine || startLine || ''),
        start_column: String(options.start_column || 1),
        end_column: String(options.end_column || String(snippet).trim().length || ''),
        evidence_kind: evidenceKind,
        extractor_name: this.extractorName,
        confidence: String(Number(options.confidence ?? 1.0)),
        snippet: String(snippet).trim(),
        properties_json: json(options.properties || {}),
      });
    }
    return evidenceId;
  }

  addIssue(issueType, severity, message, options = {}) {
    const cleanPath = options.source_path ? repoPath(options.source_path) : '';
    const identity = `${issueType}|${options.source_node_id || ''}|${options.raw_reference || ''}|${options.database_key || ''}|${cleanPath}|${options.start_line || ''}|${message}`;
    const issueId = 'issue:' + sha256(identity).slice(0, 24);
    if (!this.issues.has(issueId)) {
      this.issues.set(issueId, {
        issue_id: issueId,
        issue_type: issueType,
        severity,
        source_node_id: options.source_node_id || '',
        raw_reference: options.raw_reference || '',
        database_key: options.database_key || '',
        source_path: cleanPath,
        start_line: String(options.start_line || ''),
        message,
        properties_json: json(options.properties || {}),
      });
    }
    return issueId;
  }

  addLocalizedText(targetType, targetId, fieldName, locale, value, options = {}) {
    const key = `${targetId}|${fieldName}|${locale}`;
    if (!this.localizedTexts.has(key)) {
      this.localizedTexts.set(key, {
        target_type: targetType,
        target_id: targetId,
        field_name: fieldName,
        locale,
        value,
        source_kind: options.source_kind || 'EXTRACTED',
        review_status: options.review_status || 'PENDING',
        author_name: options.author_name || this.extractorName,
        created_at: options.created_at || '',
        updated_at: options.updated_at || '',
      });
    }
    return key;
  }

  write(output) {
    mkdirSync(output, { recursive: true });
    const groups = {
      nodes: [...this.nodes.values()].sort((a, b) => a.node_id.localeCompare(b.node_id)),
      edges: [...this.edges.values()].sort((a, b) => a.edge_id.localeCompare(b.edge_id)),
      evidence: [...this.evidence.values()].sort((a, b) => a.evidence_id.localeCompare(b.evidence_id)),
      issues: [...this.issues.values()].sort((a, b) => a.issue_id.localeCompare(b.issue_id)),
    };
    if (this.localizedTexts.size) {
      groups.localized_texts = [...this.localizedTexts.values()].sort((a, b) => `${a.target_id}|${a.field_name}|${a.locale}`.localeCompare(`${b.target_id}|${b.field_name}|${b.locale}`));
    }
    const files = {};
    const checksums = {};
    const statistics = { filesScanned: this.filesScanned };
    for (const [name, rows] of Object.entries(groups)) {
      const out = path.join(output, `${name}.csv`);
      writeCsv(out, CSV_HEADERS[name], rows);
      const bytes = readFileSync(out);
      files[name] = `${name}.csv`;
      checksums[`${name}.csv`] = { sha256: sha256(bytes), bytes: bytes.length };
      statistics[name] = rows.length;
    }
    const source = {
      sourceKey: this.sourceId,
      repositoryKey: String(this.metadata.repository || this.metadata.source || this.sourceId),
    };
    if (this.metadata.revision) source.revision = String(this.metadata.revision);
    const manifest = {
      contractVersion: '1.0',
      extractor: { name: this.extractorName, version: this.extractorVersion },
      source,
      generatedAt: new Date().toISOString(),
      files,
      statistics,
      checksums,
      metadata: this.metadata,
    };
    writeFileSync(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n', 'utf8');
  }
}

function writeCsv(out, headers, rows) {
  const lines = [headers.join(',')];
  for (const row of rows) lines.push(headers.map(header => csv(row[header] ?? '')).join(','));
  writeFileSync(out, lines.join('\n') + '\n', 'utf8');
}

function csv(value) {
  const text = String(value);
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

function json(value) {
  const sort = item => Array.isArray(item)
    ? item.map(sort)
    : item && typeof item === 'object'
      ? Object.fromEntries(Object.keys(item).sort().map(key => [key, sort(item[key])]))
      : item;
  return JSON.stringify(sort(value || {}));
}

function canonicalEdgeId(sourceNodeId, edgeType, targetNodeId, rawOperation, graphLayer) {
  return 'edge:' + sha256([sourceNodeId, edgeType, targetNodeId, rawOperation || '', graphLayer].join('|'));
}

function stableNodeId(kind, ...parts) {
  return [kind.trim().toLowerCase().replaceAll('_', '-'), ...parts.map(part => String(part).trim())].join(':');
}

function normalizeHttpRoute(method, route) {
  const normalizedMethod = String(method || '').trim().toUpperCase();
  let raw = String(route || '').trim();
  let pathname;
  try {
    pathname = new URL(raw.includes('://') || raw.startsWith('//') ? raw : `http://contract.local/${raw.replace(/^\/+/, '')}`).pathname;
  } catch {
    pathname = '/' + raw.replace(/^\/+/, '');
  }
  try {
    pathname = decodeURIComponent(pathname);
  } catch {
    // Keep the raw pathname if it contains malformed escapes.
  }
  pathname = pathname.replace(/\/+/g, '/').replace(/\/:[A-Za-z_][A-Za-z0-9_]*/g, '/{id}').replace(/\{[^/{}]+\}/g, '{id}');
  if (pathname !== '/') pathname = pathname.replace(/\/+$/g, '');
  return { method: normalizedMethod, route: pathname || '/' };
}

function apiCallId(source, method, route) {
  const normalized = normalizeHttpRoute(method, route);
  return stableNodeId('api-call', source, normalized.method, normalized.route);
}

function repoPath(value) {
  return String(value).replaceAll('\\', '/').replace(/^\.\//, '');
}

function slug(value) {
  return String(value || 'source').split(/[\\/]/).pop().replace(/[^A-Za-z0-9_.-]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase() || 'source';
}

function kebabName(value) {
  return String(value || '')
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/[^A-Za-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase();
}

function displayFromRoute(route) {
  const parts = route.split('/').filter(part => part && part !== '{id}');
  return parts.map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(' ') || 'Home';
}

function pascalName(value) {
  const text = String(value || 'Route').replace(/[^A-Za-z0-9]+/g, ' ');
  const result = text.split(' ').filter(Boolean).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join('');
  return result || 'Route';
}

function displayFromMethod(name) {
  const text = String(name).replace(/([a-z0-9])([A-Z])/g, '$1 $2').replaceAll('_', ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function lineFor(sourceFile, offset) {
  return sourceFile.getLineAndCharacterOfPosition(offset).line + 1;
}

function lineText(text, line) {
  return text.split(/\r?\n/)[line - 1] || '';
}

function lineContaining(text, needle) {
  if (!needle) return 0;
  const lines = text.split(/\r?\n/);
  const index = lines.findIndex(line => line.includes(needle));
  return index >= 0 ? index + 1 : 0;
}

function pushMap(map, key, value) {
  if (!map.has(key)) map.set(key, []);
  map.get(key).push(value);
}

export { discoverAngularProject };

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main();
  } catch (error) {
    console.error(error instanceof Error ? error.stack || error.message : String(error));
    process.exit(1);
  }
}
