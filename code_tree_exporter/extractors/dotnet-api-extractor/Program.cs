using System.Text.Json;
using System.Text.RegularExpressions;
using CodeMap.Extractors;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

var configPath = Args.Value(args, "--config") ?? throw new ArgumentException("Missing --config");
using var document = ExtractorRuntime.LoadConfig(configPath);
var config = document.RootElement;
if (!ExtractorRuntime.String(config, "type").Equals("dotnet-api", StringComparison.Ordinal))
    throw new ArgumentException("Config type must be dotnet-api");
var source = ExtractorRuntime.String(config, "source");
var application = ExtractorRuntime.String(config, "application", source);
var repository = ExtractorRuntime.String(config, "repository", source);
var database = ExtractorRuntime.String(config, "database");
var systemKey = ExtractorRuntime.String(config, "system", application);
var root = ExtractorRuntime.ConfigPath(config, "root");
var output = ExtractorRuntime.ConfigPath(config, "output");
var inputRoot = ExtractorRuntime.ConfigPath(config, "inputData");
var catalog = Catalog.Load(inputRoot, database);
var files = ExpandWorkspaceFiles(ExtractorRuntime.ConfiguredFiles(config, new[] { ".cs", ".csproj", ".sln" }), root);
var csFiles = files.Where(file => file.SyntaxTree is not null).ToList();

var builder = new PackageBuilder(
    $"dotnet-api-{source}",
    $"extractor:dotnet-api/{source}",
    "dotnet-api-extractor",
    "3.0.0",
    new Dictionary<string, object>
    {
        ["source"] = source,
        ["technology"] = "C# Roslyn Workspace/SemanticModel",
        ["parser"] = "Microsoft.CodeAnalysis.CSharp",
    });
builder.FilesScanned = files.Count;

var appId = ExtractorRuntime.StableNodeId("api-application", application);
builder.AddNode(appId, "API_APPLICATION", application, application, application, systemKey, database, repository);
var solutions = DiscoverSolutions(files, root, repository);
var projects = DiscoverProjects(files, csFiles, root, database, repository);
var projectsById = projects.ToDictionary(project => project.Id, StringComparer.Ordinal);
var compilationByTree = CreateProjectCompilations(csFiles, projects);
foreach (var solution in solutions)
    builder.AddNode(solution.Id, "DOTNET_SOLUTION", Path.GetFileName(solution.RelativePath), $"{repository}/{solution.RelativePath}", DisplaySolutionName(solution), systemKey, "", repository, "TECHNICAL", properties: new Dictionary<string, object> { ["path"] = solution.RelativePath });
foreach (var project in projects)
    builder.AddNode(project.Id, "DOTNET_PROJECT", Path.GetFileName(project.RelativePath), $"{repository}/{project.RelativePath}", DisplayProjectName(project), systemKey, database, repository, "TECHNICAL", properties: new Dictionary<string, object> { ["path"] = project.RelativePath });
foreach (var solution in solutions)
    foreach (var projectId in SolutionProjectIds(solution, projects, repository, root))
        if (projectsById.ContainsKey(projectId)) builder.AddEdge(solution.Id, projectId, "CONTAINS", "STRUCTURAL");
foreach (var project in projects)
    foreach (var reference in project.ProjectReferences)
        if (projectsById.TryGetValue(reference, out var target)) builder.AddEdge(project.Id, target.Id, "PROJECT_REFERENCE", "STRUCTURAL");

var classesByIdentity = new Dictionary<string, (string Name, SourceFile File, ClassDeclarationSyntax Syntax, string? ProjectId)>(StringComparer.Ordinal);
var classIdentityByMemberDeclaration = new Dictionary<string, string>(StringComparer.Ordinal);
foreach (var file in csFiles)
{
    var model = compilationByTree[file.SyntaxTree!].GetSemanticModel(file.SyntaxTree!);
    foreach (var cls in ExtractorRuntime.Classes(file))
    {
        var project = ProjectForFile(file, projects);
        var typeIdentity = TypeIdentity(model.GetDeclaredSymbol(cls));
        var identity = ScopedClassIdentity(typeIdentity, file, project);
        classesByIdentity[identity] = (cls.Identifier.Text, file, cls, project?.Id);
        foreach (var member in cls.Members)
            classIdentityByMemberDeclaration[DeclarationIdentity(member.SyntaxTree, member.SpanStart)] = identity;
    }
}

var endpoints = new List<EndpointInfo>();
foreach (var file in csFiles)
{
    var model = compilationByTree[file.SyntaxTree!].GetSemanticModel(file.SyntaxTree!);
    endpoints.AddRange(ExtractorRuntime.Endpoints(file, model));
}

var invocationEdges = new List<InvocationInfo>();
foreach (var file in csFiles)
{
    var model = compilationByTree[file.SyntaxTree!].GetSemanticModel(file.SyntaxTree!);
    var visibleClassNames = model.Compilation.SyntaxTrees
        .SelectMany(tree => tree.GetRoot().DescendantNodes().OfType<ClassDeclarationSyntax>())
        .Select(cls => cls.Identifier.Text)
        .ToHashSet(StringComparer.Ordinal);
    invocationEdges.AddRange(ExtractorRuntime.InvocationEdges(file, model, visibleClassNames));
}

var dataAccessClasses = new HashSet<string>(StringComparer.Ordinal);
foreach (var file in csFiles)
{
    var model = compilationByTree[file.SyntaxTree!].GetSemanticModel(file.SyntaxTree!);
    foreach (var expression in ExtractorRuntime.StringExpressions(file, model))
    {
        if (string.IsNullOrEmpty(expression.OwnerClassIdentity)) continue;
        var ownerIdentity = ScopedClassIdentity(expression.OwnerClassIdentity, file, ProjectForFile(file, projects));
        var analysis = SqlAnalyzer.Analyze(expression.Value);
        if (analysis.Tables.Count > 0 || analysis.Procedures.Count > 0 || analysis.DynamicOffsets.Count > 0 || IsStoredProcedureLiteral(file.Text, expression.Value, expression.Start) || (expression.IsDynamic && LooksLikeSql(expression.Value) && HasDynamicSqlTarget(expression.Value)))
            dataAccessClasses.Add(ownerIdentity);
    }
}

var endpointControllerKeys = endpoints.Select(endpoint => endpoint.File is null || string.IsNullOrEmpty(endpoint.ControllerIdentity)
    ? endpoint.Controller
    : ScopedClassIdentity(endpoint.ControllerIdentity, endpoint.File, ProjectForFile(endpoint.File, projects))).ToHashSet(StringComparer.Ordinal);
var classCallEdges = invocationEdges.Select(call => (
    Source: ScopedClassIdentity(call.SourceClassIdentity, call.File, ProjectForFile(call.File, projects)),
    Target: classIdentityByMemberDeclaration.GetValueOrDefault(call.TargetIdentity, string.Empty),
    Call: call)).ToList();
var reachabilityRoots = new HashSet<string>(endpointControllerKeys.Where(classesByIdentity.ContainsKey), StringComparer.Ordinal);
foreach (var endpoint in endpoints.Where(endpoint => endpoint.IsMinimal && endpoint.File is not null))
    foreach (var target in classCallEdges.Where(edge => edge.Call.File.RelativePath == endpoint.File!.RelativePath && edge.Call.Start >= endpoint.HandlerStart && edge.Call.Start < endpoint.HandlerEnd).Select(edge => edge.Target).Where(identity => !string.IsNullOrEmpty(identity)))
        reachabilityRoots.Add(target);
var reachableClasses = ReachableClasses(reachabilityRoots, classCallEdges.Select(edge => (edge.Source, edge.Target)), classesByIdentity.Keys.ToHashSet(StringComparer.Ordinal));
var controllerIds = endpointControllerKeys.ToDictionary(identity => identity, identity => ExtractorRuntime.StableNodeId("controller", application, identity), StringComparer.Ordinal);
var serviceIds = reachableClasses
    .Where(identity => classesByIdentity.TryGetValue(identity, out var cls) && cls.Name.EndsWith("Service", StringComparison.Ordinal))
    .ToDictionary(identity => identity, identity => ExtractorRuntime.StableNodeId("service", application, identity), StringComparer.Ordinal);
var repositoryIds = reachableClasses
    .Where(identity => classesByIdentity.TryGetValue(identity, out var cls) && cls.Name.EndsWith("Repository", StringComparison.Ordinal) && dataAccessClasses.Contains(identity))
    .ToDictionary(identity => identity, identity => ExtractorRuntime.StableNodeId("repository", application, identity), StringComparer.Ordinal);

foreach (var (identity, nodeId) in controllerIds)
{
    if (classesByIdentity.ContainsKey(identity))
        AddClassNode(builder, classesByIdentity, identity, nodeId, "CONTROLLER", application, systemKey, database, repository);
    else
        builder.AddNode(nodeId, "CONTROLLER", identity, $"{application}.{identity}", identity, systemKey, database, repository, "TECHNICAL");
}
foreach (var (identity, nodeId) in serviceIds)
    AddClassNode(builder, classesByIdentity, identity, nodeId, "SERVICE", application, systemKey, database, repository);
foreach (var (identity, nodeId) in repositoryIds)
    AddClassNode(builder, classesByIdentity, identity, nodeId, "REPOSITORY", application, systemKey, database, repository);

var methodNodeIdsByDeclaration = new Dictionary<(SyntaxTree Tree, int Start), string>();
foreach (var file in csFiles)
{
    var model = compilationByTree[file.SyntaxTree!].GetSemanticModel(file.SyntaxTree!);
    foreach (var method in file.SyntaxTree!.GetRoot().DescendantNodes().OfType<MethodDeclarationSyntax>())
    {
        var methodSymbol = model.GetDeclaredSymbol(method);
        var classIdentity = classIdentityByMemberDeclaration.GetValueOrDefault(DeclarationIdentity(method.SyntaxTree, method.SpanStart), string.Empty);
        var className = methodSymbol?.ContainingType.Name ?? method.Ancestors().OfType<ClassDeclarationSyntax>().FirstOrDefault()?.Identifier.Text ?? "";
        var classId = serviceIds.GetValueOrDefault(classIdentity) ?? repositoryIds.GetValueOrDefault(classIdentity);
        if (string.IsNullOrEmpty(classId)) continue;
        var symbolIdentity = methodSymbol?.GetDocumentationCommentId();
        var signature = symbolIdentity ?? $"{file.RelativePath}:{method.SpanStart}:{className}.{method.Identifier.Text}({string.Join(',', method.ParameterList.Parameters.Select(parameter => $"{string.Join(' ', parameter.Modifiers)} {parameter.Type}".Trim()))})";
        var scopedSignature = $"{classIdentity}|{signature}";
        var methodId = ExtractorRuntime.StableNodeId("method", application, scopedSignature);
        methodNodeIdsByDeclaration[(file.SyntaxTree!, method.SpanStart)] = methodId;
        builder.AddNode(methodId, "METHOD", method.Identifier.Text, $"{application}.{scopedSignature}", method.Identifier.Text, systemKey, database, repository, "TECHNICAL");
        builder.AddEdge(classId, methodId, "CONTAINS", "STRUCTURAL");
        var start = ExtractorRuntime.LineForOffset(file, method.SpanStart);
        var end = ExtractorRuntime.LineForOffset(file, method.Span.End);
        builder.AddEvidence("NODE", methodId, file.RelativePath, start, end, "DECLARATION", ExtractorRuntime.LineText(file, start));
    }
}

foreach (var endpoint in endpoints)
{
    var operationId = ScopedApiOperationId(application, endpoint, projects);
    var operationScope = endpoint.File is null ? application : ScopedSourceIdentity(endpoint.File, ProjectForFile(endpoint.File, projects));
    builder.AddNode(operationId, "API_OPERATION", $"{endpoint.Method} {endpoint.Route}", $"{operationScope}.{endpoint.Method}.{endpoint.Route}", $"{endpoint.Action} API", systemKey, database, repository, properties: new Dictionary<string, object> { ["method"] = endpoint.Method, ["route"] = endpoint.Route, ["endpointStyle"] = endpoint.IsMinimal ? "minimal" : "controller" });
    var controllerIdentity = endpoint.File is null || string.IsNullOrEmpty(endpoint.ControllerIdentity)
        ? endpoint.Controller
        : ScopedClassIdentity(endpoint.ControllerIdentity, endpoint.File, ProjectForFile(endpoint.File, projects));
    var controllerId = controllerIds.TryGetValue(controllerIdentity, out var id) ? id : ExtractorRuntime.StableNodeId("controller", application, controllerIdentity);
    builder.AddEdge(controllerId, operationId, "CONTAINS", "STRUCTURAL");
    var edgeId = builder.AddEdge(operationId, controllerId, "HANDLED_BY");
    var endpointFile = endpoint.File ?? csFiles.First(file => file.SyntaxTree?.GetRoot().FullSpan.Contains(endpoint.Start) == true);
    var line = ExtractorRuntime.LineForOffset(endpointFile, endpoint.Start);
    builder.AddEvidence("NODE", operationId, endpointFile.RelativePath, line, line, "DECLARATION", ExtractorRuntime.LineText(endpointFile, line));
    builder.AddEvidence("EDGE", edgeId, endpointFile.RelativePath, line, line, endpoint.IsMinimal ? "MINIMAL_API" : "API_ACTION", ExtractorRuntime.LineText(endpointFile, line));
    foreach (var invocation in invocationEdges.Where(call => call.File.RelativePath == endpointFile.RelativePath && call.Start >= endpoint.HandlerStart && call.Start < endpoint.HandlerEnd))
    {
        var targetMethodId = TargetMethodNodeId(invocation, compilationByTree[invocation.File.SyntaxTree!], methodNodeIdsByDeclaration);
        if (string.IsNullOrEmpty(targetMethodId)) continue;
        var callEdgeId = builder.AddEdge(operationId, targetMethodId, "CALLS", rawOperation: invocation.TargetMember);
        var callLine = ExtractorRuntime.LineForOffset(invocation.File, invocation.Start);
        builder.AddEvidence("EDGE", callEdgeId, invocation.File.RelativePath, callLine, callLine, "CALL", ExtractorRuntime.LineText(invocation.File, callLine));
    }
}

foreach (var invocation in invocationEdges)
{
    var sourceMethodId = SourceMethodNodeId(invocation.File, invocation.Start, methodNodeIdsByDeclaration);
    var targetMethodId = TargetMethodNodeId(invocation, compilationByTree[invocation.File.SyntaxTree!], methodNodeIdsByDeclaration);
    if (string.IsNullOrEmpty(sourceMethodId) || string.IsNullOrEmpty(targetMethodId)) continue;
    var edgeId = builder.AddEdge(sourceMethodId, targetMethodId, "CALLS", rawOperation: invocation.TargetMember);
    var line = ExtractorRuntime.LineForOffset(invocation.File, invocation.Start);
    builder.AddEvidence("EDGE", edgeId, invocation.File.RelativePath, line, line, "CALL", ExtractorRuntime.LineText(invocation.File, line));
}

foreach (var file in csFiles)
{
    var model = compilationByTree[file.SyntaxTree!].GetSemanticModel(file.SyntaxTree!);
    foreach (var expression in ExtractorRuntime.StringExpressions(file, model))
    {
        var ownerId = SourceMethodNodeId(file, expression.Start, methodNodeIdsByDeclaration);
        if (string.IsNullOrEmpty(ownerId)) continue;
        var line = ExtractorRuntime.LineForOffset(file, expression.Start);
        var snippet = ExtractorRuntime.LineText(file, line);
        if (IsStoredProcedureLiteral(file.Text, expression.Value, expression.Start))
        {
            AddProcedureReference(builder, database, systemKey, repository, application, expression.Value, ownerId, file, line, snippet, "STORED_PROCEDURE");
            continue;
        }

        var analysis = SqlAnalyzer.Analyze(expression.Value);
        if (analysis.Tables.Count == 0 && analysis.Procedures.Count == 0 && analysis.DynamicOffsets.Count == 0 && !LooksLikeSql(expression.Value)) continue;
        var inlineSqlId = ExtractorRuntime.StableNodeId("inline-sql", ownerId, Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(expression.ExpressionText)))[..16].ToLowerInvariant());
        builder.AddNode(inlineSqlId, "INLINE_SQL", "inline SQL", $"{ownerId}.inline-sql.{line}", "inline SQL", systemKey, database, repository, "EVIDENCE", properties: new Dictionary<string, object> { ["sql"] = expression.Value, ["dynamic"] = expression.IsDynamic });
        builder.AddEdge(ownerId, inlineSqlId, "CONTAINS", "STRUCTURAL");
        builder.AddEvidence("NODE", inlineSqlId, file.RelativePath, line, line, "INLINE_SQL", snippet);
        var sqlEvidenceKind = SqlEvidenceKind(file, expression);
        if (expression.IsDynamic && LooksLikeSql(expression.Value) && (analysis.Tables.Count == 0 || HasDynamicSqlTarget(expression.Value) || analysis.DynamicOffsets.Count > 0))
        {
            builder.AddIssue("DYNAMIC_SQL", "WARNING", "Runtime SQL target cannot be resolved", ownerId, expression.ExpressionText, database, file.RelativePath, line, properties: new Dictionary<string, object> { ["expression"] = expression.ExpressionText, ["evaluated"] = expression.Value });
            if (HasDynamicSqlTarget(expression.Value)) continue;
        }
        foreach (var procedure in analysis.Procedures)
            AddProcedureReference(builder, database, systemKey, repository, application, procedure.ObjectName, inlineSqlId, file, ExtractorRuntime.LineForOffset(file, expression.Start + procedure.Start), snippet, "RAW_SQL_PROCEDURE");
        foreach (var tableRef in analysis.Tables)
        {
            var tableName = ExtractorRuntime.LeafIdentifier(tableRef.ObjectName);
            if (!catalog.HasTable(database, tableName))
            {
                builder.AddIssue("TABLE_NOT_IMPORTED", "ERROR", "Table is absent from authoritative catalog", inlineSqlId, tableName, database, file.RelativePath, line);
                continue;
            }
            var target = ExtractorRuntime.TableId(database, tableName);
            var edgeId = builder.AddEdge(inlineSqlId, target, tableRef.EdgeType, rawOperation: tableRef.Operation);
            builder.AddEvidence("EDGE", edgeId, file.RelativePath, line, line, sqlEvidenceKind, snippet);
            AddColumnIssues(builder, catalog, database, tableName, expression.Value, inlineSqlId, file.RelativePath, line);
        }
        foreach (var offset in analysis.DynamicOffsets)
            builder.AddIssue("DYNAMIC_SQL", "WARNING", "Runtime SQL target cannot be resolved", ownerId, expression.ExpressionText, database, file.RelativePath, ExtractorRuntime.LineForOffset(file, expression.Start + offset));
    }
}

AttachApiSemanticTrees(builder, endpoints, csFiles, methodNodeIdsByDeclaration, application, projects, compilationByTree);
AttachMethodSemanticTrees(builder, csFiles, methodNodeIdsByDeclaration, compilationByTree);
builder.Write(output);

static string? SourceMethodNodeId(SourceFile file, int offset, IReadOnlyDictionary<(SyntaxTree Tree, int Start), string> methodNodeIds)
{
    if (file.SyntaxTree is null) return null;
    var method = file.SyntaxTree.GetRoot().FindToken(offset).Parent?.AncestorsAndSelf().OfType<MethodDeclarationSyntax>().FirstOrDefault();
    return method is null ? null : methodNodeIds.GetValueOrDefault((file.SyntaxTree, method.SpanStart));
}

static string? TargetMethodNodeId(InvocationInfo invocation, CSharpCompilation compilation, IReadOnlyDictionary<(SyntaxTree Tree, int Start), string> methodNodeIds)
{
    if (invocation.File.SyntaxTree is null) return null;
    var syntax = invocation.File.SyntaxTree.GetRoot().FindToken(invocation.Start).Parent?.AncestorsAndSelf().OfType<InvocationExpressionSyntax>().FirstOrDefault();
    if (syntax is null) return null;
    var info = compilation.GetSemanticModel(invocation.File.SyntaxTree).GetSymbolInfo(syntax);
    var candidates = info.CandidateSymbols.OfType<IMethodSymbol>().ToArray();
    var symbol = info.Symbol as IMethodSymbol ?? (candidates.Length == 1 ? candidates[0] : null);
    return symbol?.DeclaringSyntaxReferences.Select(reference => methodNodeIds.GetValueOrDefault((reference.SyntaxTree, reference.Span.Start))).FirstOrDefault(id => !string.IsNullOrWhiteSpace(id));
}

static void AttachApiSemanticTrees(PackageBuilder builder, IEnumerable<EndpointInfo> endpoints, IReadOnlyCollection<SourceFile> files, IReadOnlyDictionary<(SyntaxTree Tree, int Start), string> methodNodeIds, string application, List<ProjectInfo> projects, IReadOnlyDictionary<SyntaxTree, CSharpCompilation> compilationByTree)
{
    var filesByTree = files.Where(file => file.SyntaxTree is not null).ToDictionary(file => file.SyntaxTree!);
    foreach (var endpoint in endpoints)
    {
        var file = endpoint.File;
        if (file?.SyntaxTree is null) continue;
        var compilation = compilationByTree[file.SyntaxTree];
        var semanticFile = file;
        var root = file.SyntaxTree.GetRoot();
        var node = root.FindToken(endpoint.Start).Parent?.AncestorsAndSelf().FirstOrDefault(candidate => candidate is MethodDeclarationSyntax or InvocationExpressionSyntax);
        IEnumerable<ParameterSyntax> parameters = Enumerable.Empty<ParameterSyntax>();
        SyntaxNode? body = null;
        if (node is MethodDeclarationSyntax method)
        {
            parameters = method.ParameterList.Parameters;
            body = method;
        }
        else if (node is InvocationExpressionSyntax mapping)
        {
            var handlerExpression = mapping.ArgumentList.Arguments.LastOrDefault()?.Expression;
            var handler = handlerExpression as LambdaExpressionSyntax;
            body = handler?.Body;
            parameters = handler switch
            {
                ParenthesizedLambdaExpressionSyntax lambda => lambda.ParameterList.Parameters,
                SimpleLambdaExpressionSyntax lambda => new[] { lambda.Parameter },
                _ => Enumerable.Empty<ParameterSyntax>(),
            };
            if (body is null && handlerExpression is not null)
            {
                var handlerSymbol = compilation.GetSemanticModel(file.SyntaxTree).GetSymbolInfo(handlerExpression).Symbol as IMethodSymbol;
                var declaration = handlerSymbol?.DeclaringSyntaxReferences.Select(reference => reference.GetSyntax()).OfType<MethodDeclarationSyntax>().FirstOrDefault();
                if (declaration is not null && filesByTree.TryGetValue(declaration.SyntaxTree, out var declarationFile))
                {
                    semanticFile = declarationFile;
                    body = (SyntaxNode?)declaration.Body ?? declaration.ExpressionBody;
                    parameters = declaration.ParameterList.Parameters;
                }
                else
                {
                    body = handlerExpression;
                }
            }
        }
        if (body is null) continue;
        var model = compilation.GetSemanticModel(semanticFile.SyntaxTree!);
        SemanticCallResolution? ResolveCall(InvocationExpressionSyntax invocation)
        {
            var info = model.GetSymbolInfo(invocation);
            var symbols = info.Symbol is IMethodSymbol symbol ? new[] { symbol } : info.CandidateSymbols.OfType<IMethodSymbol>().ToArray();
            var ids = symbols
                .SelectMany(candidate => candidate.DeclaringSyntaxReferences)
                .Select(reference => (reference.SyntaxTree, reference.Span.Start))
                .Select(declaration => methodNodeIds.GetValueOrDefault(declaration))
                .Where(id => !string.IsNullOrWhiteSpace(id))
                .Distinct(StringComparer.Ordinal)
                .Cast<string>()
                .ToList();
            var label = symbols.FirstOrDefault() is { } resolved ? $"Call {resolved.ContainingType?.Name}.{resolved.Name}" : null;
            return ids.Count switch
            {
                1 => new SemanticCallResolution("resolved", ids[0], label),
                > 1 => new SemanticCallResolution("partial", Label: label, RefNodeIds: ids),
                _ => new SemanticCallResolution("unresolved", Label: label),
            };
        }
        var operationId = ScopedApiOperationId(application, endpoint, projects);
        builder.SetNodeProperty(operationId, "semantic_tree", SemanticTreeV3.Operation($"{endpoint.Method} {endpoint.Route}", parameters, new[] { body }, semanticFile, ResolveCall));
    }
}

static void AttachMethodSemanticTrees(PackageBuilder builder, IEnumerable<SourceFile> files, IReadOnlyDictionary<(SyntaxTree Tree, int Start), string> methodNodeIds, IReadOnlyDictionary<SyntaxTree, CSharpCompilation> compilationByTree)
{
    SemanticCallResolution? ResolveCall(InvocationExpressionSyntax invocation)
    {
        var model = compilationByTree[invocation.SyntaxTree].GetSemanticModel(invocation.SyntaxTree);
        var info = model.GetSymbolInfo(invocation);
        var symbols = info.Symbol is IMethodSymbol symbol ? new[] { symbol } : info.CandidateSymbols.OfType<IMethodSymbol>().ToArray();
        var ids = symbols.SelectMany(candidate => candidate.DeclaringSyntaxReferences)
            .Select(reference => methodNodeIds.GetValueOrDefault((reference.SyntaxTree, reference.Span.Start)))
            .Where(id => !string.IsNullOrWhiteSpace(id)).Distinct(StringComparer.Ordinal).Cast<string>().ToList();
        var label = symbols.FirstOrDefault() is { } resolved ? $"Call {resolved.ContainingType?.Name}.{resolved.Name}" : null;
        return ids.Count switch
        {
            1 => new SemanticCallResolution("resolved", ids[0], label),
            > 1 => new SemanticCallResolution("partial", Label: label, RefNodeIds: ids),
            _ => new SemanticCallResolution("unresolved", Label: label),
        };
    }

    foreach (var file in files.Where(file => file.SyntaxTree is not null))
    {
        foreach (var method in file.SyntaxTree!.GetRoot().DescendantNodes().OfType<MethodDeclarationSyntax>())
        {
            if (!methodNodeIds.TryGetValue((file.SyntaxTree, method.SpanStart), out var methodId)) continue;
            if (method.Body is null && method.ExpressionBody is null) continue;
            builder.SetNodeProperty(methodId, "semantic_tree", SemanticTreeV3.Operation(method.Identifier.Text, method.ParameterList.Parameters, new SyntaxNode[] { method }, file, ResolveCall));
        }
    }
}

static bool IsStoredProcedureLiteral(string fileText, string literal, int start)
{
    if (!Regex.IsMatch(literal, @"^[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)+$")) return false;
    var windowStart = Math.Max(0, start - 360);
    var window = fileText.Substring(windowStart, Math.Min(720, fileText.Length - windowStart));
    return window.Contains("StoredProcedure", StringComparison.OrdinalIgnoreCase) || literal.StartsWith("PKG_", StringComparison.OrdinalIgnoreCase);
}

static void AddProcedureReference(PackageBuilder builder, string database, string systemKey, string repository, string referenceScope, string rawReference, string ownerId, SourceFile file, int line, string snippet, string evidenceKind)
{
    var raw = rawReference.Trim().ToUpperInvariant();
    var procKey = raw.Contains('.') ? string.Join('.', raw.Split('.').TakeLast(2)) : raw;
    var target = EnsureProcedureReference(builder, database, systemKey, repository, referenceScope, raw);
    var edgeId = builder.AddEdge(ownerId, target, "CALLS", rawOperation: procKey.Split('.').Last());
    builder.AddEvidence("EDGE", edgeId, file.RelativePath, line, line, evidenceKind, snippet);
}

static string EnsureProcedureReference(PackageBuilder builder, string database, string systemKey, string repository, string referenceScope, string rawReference)
{
    var raw = rawReference.Trim().ToUpperInvariant();
    var parts = raw.Split('.', StringSplitOptions.RemoveEmptyEntries);
    var package = parts.Length >= 2 ? parts[^2] : "";
    var routine = parts.Length >= 1 ? parts[^1] : raw;
    var nodeId = ExtractorRuntime.StableNodeId("unresolved-reference", database, referenceScope, raw);
    builder.AddNode(nodeId, "UNRESOLVED_REFERENCE", raw, raw, raw, systemKey, database, repository, "TECHNICAL", properties: new Dictionary<string, object>
    {
        ["database"] = database,
        ["package"] = package,
        ["routine"] = routine,
        ["raw_reference"] = raw,
    });
    return nodeId;
}

static string SqlEvidenceKind(SourceFile file, StringExpressionInfo expression)
{
    var window = SourceWindow(file.Text, expression.Start, 260);
    if (Regex.IsMatch(window, @"\b(?:Query|QueryFirst(?:OrDefault)?|QuerySingle(?:OrDefault)?|Execute(?:Scalar)?)(?:Async)?\s*<|\b(?:Query|QueryFirst(?:OrDefault)?|QuerySingle(?:OrDefault)?|Execute(?:Scalar)?)(?:Async)?\s*\(", RegexOptions.IgnoreCase))
        return "DAPPER_SQL";
    if (Regex.IsMatch(window, @"\b(?:FromSql(?:Raw|Interpolated)?|ExecuteSql(?:Raw|Interpolated)?|SqlQuery(?:Raw|Interpolated)?)\s*\(", RegexOptions.IgnoreCase))
        return "EF_RAW_SQL";
    if (window.Contains("OracleCommand", StringComparison.OrdinalIgnoreCase) || window.Contains("OracleConnection", StringComparison.OrdinalIgnoreCase) || window.Contains("Oracle.ManagedDataAccess", StringComparison.OrdinalIgnoreCase))
        return "ORACLE_SQL";
    if (window.Contains("CommandText", StringComparison.OrdinalIgnoreCase) || window.Contains("SqlCommand", StringComparison.OrdinalIgnoreCase) || window.Contains("DbCommand", StringComparison.OrdinalIgnoreCase) || window.Contains("IDbCommand", StringComparison.OrdinalIgnoreCase))
        return "ADO_SQL";
    return "SQL";
}

static string SourceWindow(string text, int center, int radius)
{
    var start = Math.Clamp(center - radius, 0, text.Length);
    var end = Math.Clamp(center + radius, start, text.Length);
    return text[start..end];
}

static void AddColumnIssues(PackageBuilder builder, Catalog catalog, string database, string tableName, string sql, string ownerId, string sourcePath, int line)
{
    foreach (Match match in Regex.Matches(sql, $@"\b{Regex.Escape(tableName)}\.([A-Za-z_]\w*)\b", RegexOptions.IgnoreCase))
    {
        var column = match.Groups[1].Value.ToUpperInvariant();
        if (!catalog.HasColumn(database, tableName, column))
            builder.AddIssue("COLUMN_NOT_IMPORTED", "WARNING", "Column is absent from authoritative catalog", ownerId, $"{tableName}.{column}", database, sourcePath, line);
    }
}

static List<SourceFile> ExpandWorkspaceFiles(List<SourceFile> seedFiles, string root)
{
    var byPath = new Dictionary<string, SourceFile>(StringComparer.OrdinalIgnoreCase);
    var projectQueue = new Queue<string>();
    var queuedProjects = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    foreach (var file in seedFiles)
    {
        AddSourceFile(byPath, file);
        if (Path.GetExtension(file.AbsolutePath).Equals(".csproj", StringComparison.OrdinalIgnoreCase) && queuedProjects.Add(file.AbsolutePath))
            projectQueue.Enqueue(file.AbsolutePath);
        if (Path.GetExtension(file.AbsolutePath).Equals(".sln", StringComparison.OrdinalIgnoreCase))
        {
            foreach (var projectPath in SolutionProjectPaths(file.Text, Path.GetDirectoryName(file.AbsolutePath) ?? root))
                if (queuedProjects.Add(projectPath)) projectQueue.Enqueue(projectPath);
        }
    }

    while (projectQueue.Count > 0)
    {
        var projectPath = projectQueue.Dequeue();
        if (!ExtractorRuntime.IsWithin(projectPath, root) || !File.Exists(projectPath)) continue;
        var projectFile = ReadSourceFile(projectPath, root);
        AddSourceFile(byPath, projectFile);
        var projectDirectory = Path.GetDirectoryName(projectPath) ?? root;
        foreach (var sourcePath in Directory.Exists(projectDirectory)
            ? Directory.EnumerateFiles(projectDirectory, "*.cs", SearchOption.AllDirectories).Where(path => !IsExcludedProjectPath(path)).OrderBy(path => path, StringComparer.Ordinal)
            : Enumerable.Empty<string>())
            AddSourceFile(byPath, ReadSourceFile(sourcePath, root));
        foreach (var includePath in ProjectIncludePaths(projectFile.Text, projectDirectory, new[] { "Compile" }).Where(path => File.Exists(path)))
            AddSourceFile(byPath, ReadSourceFile(includePath, root));
        foreach (var referencePath in ProjectIncludePaths(projectFile.Text, projectDirectory, new[] { "ProjectReference" }).Where(path => File.Exists(path)))
        {
            AddSourceFile(byPath, ReadSourceFile(referencePath, root));
            if (queuedProjects.Add(referencePath)) projectQueue.Enqueue(referencePath);
        }
    }

    return byPath.Values.OrderBy(file => file.AbsolutePath, StringComparer.Ordinal).ToList();
}

static void AddSourceFile(Dictionary<string, SourceFile> byPath, SourceFile file)
{
    if (!IsExcludedProjectPath(file.AbsolutePath)) byPath.TryAdd(file.AbsolutePath, file);
}

static SourceFile ReadSourceFile(string absolutePath, string root)
{
    var path = Path.GetFullPath(absolutePath);
    var text = File.ReadAllText(path);
    var syntaxTree = Path.GetExtension(path).Equals(".cs", StringComparison.OrdinalIgnoreCase)
        ? CSharpSyntaxTree.ParseText(text, path: path)
        : null;
    return new SourceFile(path, SourceRelativePath(path, root), text, syntaxTree);
}

static string SourceRelativePath(string absolutePath, string root)
{
    return ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, absolutePath));
}

static bool IsExcludedProjectPath(string path)
{
    var parts = path.Replace('\\', '/').Split('/', StringSplitOptions.RemoveEmptyEntries);
    return parts.Any(part => part.Equals("bin", StringComparison.OrdinalIgnoreCase) || part.Equals("obj", StringComparison.OrdinalIgnoreCase));
}

static IEnumerable<string> SolutionProjectPaths(string text, string solutionDirectory)
{
    foreach (Match match in Regex.Matches(text, @"Project\([^)]*\)\s*=\s*""[^""\r\n]+""\s*,\s*""(?<path>[^""\r\n]+\.csproj)""", RegexOptions.IgnoreCase))
        yield return Path.GetFullPath(Path.Combine(solutionDirectory, ProjectPath(match.Groups["path"].Value)));
}

static IEnumerable<string> ProjectIncludePaths(string text, string directory, IReadOnlyCollection<string> itemNames)
{
    foreach (Match match in Regex.Matches(text, @"<\s*(?<item>[A-Za-z_][\w.]*)\b[^>]*\bInclude\s*=\s*(?<quote>['""`])(?<path>[^'""`]+)(\k<quote>)", RegexOptions.IgnoreCase))
    {
        if (!itemNames.Contains(match.Groups["item"].Value, StringComparer.OrdinalIgnoreCase)) continue;
        yield return Path.GetFullPath(Path.Combine(directory, ProjectPath(match.Groups["path"].Value)));
    }
}

static List<SolutionInfo> DiscoverSolutions(List<SourceFile> files, string root, string repository)
{
    return files
        .Where(file => Path.GetExtension(file.AbsolutePath).Equals(".sln", StringComparison.OrdinalIgnoreCase))
        .OrderBy(file => file.AbsolutePath, StringComparer.Ordinal)
        .Select(file =>
        {
            var relativePath = ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, file.AbsolutePath));
            return new SolutionInfo(
                ExtractorRuntime.StableNodeId("dotnet-solution", repository, relativePath),
                relativePath,
                Path.GetDirectoryName(file.AbsolutePath) ?? root,
                file.Text);
        })
        .ToList();
}

static List<ProjectInfo> DiscoverProjects(List<SourceFile> files, List<SourceFile> csFiles, string root, string database, string repository)
{
    var projects = new List<ProjectInfo>();
    foreach (var file in files.Where(file => Path.GetExtension(file.AbsolutePath).Equals(".csproj", StringComparison.OrdinalIgnoreCase)).OrderBy(file => file.AbsolutePath, StringComparer.Ordinal))
    {
        var relativePath = ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, file.AbsolutePath));
        var directory = Path.GetDirectoryName(file.AbsolutePath) ?? root;
        var projectFiles = csFiles.Where(source => IsUnderDirectory(source.AbsolutePath, directory)).ToList();
        projects.Add(new ProjectInfo(
            ExtractorRuntime.StableNodeId("dotnet-project", repository, relativePath),
            relativePath,
            directory,
            projectFiles,
            ProjectReferenceIds(file.Text, directory, root, repository)));
    }
    return projects;
}

static IEnumerable<string> SolutionProjectIds(SolutionInfo solution, List<ProjectInfo> projects, string repository, string root)
{
    var declared = new List<string>();
    foreach (Match match in Regex.Matches(solution.Text, @"Project\([^)]*\)\s*=\s*""[^""\r\n]+""\s*,\s*""(?<path>[^""\r\n]+\.csproj)""", RegexOptions.IgnoreCase))
    {
        var absolute = Path.GetFullPath(Path.Combine(solution.Directory, ProjectPath(match.Groups["path"].Value)));
        var relative = ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, absolute));
        declared.Add(ExtractorRuntime.StableNodeId("dotnet-project", repository, relative));
    }
    if (declared.Count > 0) return declared;
    return projects.Where(project => IsUnderDirectory(project.Directory, solution.Directory)).Select(project => project.Id);
}

static List<string> ProjectReferenceIds(string text, string directory, string root, string repository)
{
    var result = new List<string>();
    foreach (Match match in Regex.Matches(text, @"<\s*ProjectReference\b[^>]*\bInclude\s*=\s*""(?<path>[^""]+)""", RegexOptions.IgnoreCase))
    {
        var absolute = Path.GetFullPath(Path.Combine(directory, ProjectPath(match.Groups["path"].Value)));
        var relative = ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, absolute));
        result.Add(ExtractorRuntime.StableNodeId("dotnet-project", repository, relative));
    }
    return result;
}

static string ProjectPath(string value)
    => value.Replace('\\', Path.DirectorySeparatorChar).Replace('/', Path.DirectorySeparatorChar);

static ProjectInfo? ProjectForFile(SourceFile file, List<ProjectInfo> projects)
{
    return projects
        .Where(project => IsUnderDirectory(file.AbsolutePath, project.Directory))
        .OrderByDescending(project => project.Directory.Length)
        .FirstOrDefault();
}

static Dictionary<SyntaxTree, CSharpCompilation> CreateProjectCompilations(List<SourceFile> csFiles, List<ProjectInfo> projects)
{
    var ownerByTree = csFiles.ToDictionary(file => file.SyntaxTree!, file => ProjectForFile(file, projects));
    var filesByProject = projects.ToDictionary(
        project => project.Id,
        project => csFiles.Where(file => ownerByTree[file.SyntaxTree!]?.Id == project.Id).ToList(),
        StringComparer.Ordinal);
    var projectsById = projects.ToDictionary(project => project.Id, StringComparer.Ordinal);
    var result = new Dictionary<SyntaxTree, CSharpCompilation>();
    foreach (var project in projects)
    {
        var projectIds = new HashSet<string>(StringComparer.Ordinal) { project.Id };
        var pending = new Queue<string>(project.ProjectReferences);
        while (pending.TryDequeue(out var projectId))
        {
            if (!projectIds.Add(projectId) || !projectsById.TryGetValue(projectId, out var referencedProject)) continue;
            foreach (var reference in referencedProject.ProjectReferences) pending.Enqueue(reference);
        }
        var compilationFiles = projectIds.SelectMany(projectId => filesByProject.GetValueOrDefault(projectId) ?? new List<SourceFile>()).ToList();
        var compilation = ExtractorRuntime.CreateCompilation(compilationFiles);
        foreach (var file in filesByProject[project.Id]) result[file.SyntaxTree!] = compilation;
    }
    var looseFiles = csFiles.Where(file => ownerByTree[file.SyntaxTree!] is null).ToList();
    if (looseFiles.Count > 0)
    {
        var compilation = ExtractorRuntime.CreateCompilation(looseFiles);
        foreach (var file in looseFiles) result[file.SyntaxTree!] = compilation;
    }
    return result;
}

static void AddClassNode(PackageBuilder builder, IReadOnlyDictionary<string, (string Name, SourceFile File, ClassDeclarationSyntax Syntax, string? ProjectId)> classes, string identity, string nodeId, string type, string application, string systemKey, string database, string repository)
{
    if (!classes.TryGetValue(identity, out var declaration)) return;
    builder.AddNode(nodeId, type, declaration.Name, identity, declaration.Name, systemKey, database, repository, "TECHNICAL");
    if (!string.IsNullOrEmpty(declaration.ProjectId)) builder.AddEdge(declaration.ProjectId, nodeId, "CONTAINS", "STRUCTURAL");
    var start = ExtractorRuntime.LineForOffset(declaration.File, declaration.Syntax.SpanStart);
    var end = ExtractorRuntime.LineForOffset(declaration.File, declaration.Syntax.Span.End);
    builder.AddEvidence("NODE", nodeId, declaration.File.RelativePath, start, end, "DECLARATION", ExtractorRuntime.LineText(declaration.File, start));
}

static string TypeIdentity(INamedTypeSymbol? type)
    => type?.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat) ?? string.Empty;

static string ScopedClassIdentity(string typeIdentity, SourceFile file, ProjectInfo? project)
    => $"{ScopedSourceIdentity(file, project)}|{typeIdentity}";

static string ScopedSourceIdentity(SourceFile file, ProjectInfo? project)
    => project?.RelativePath ?? file.RelativePath;

static string ScopedApiOperationId(string application, EndpointInfo endpoint, List<ProjectInfo> projects)
{
    var scope = endpoint.File is null ? application : ScopedSourceIdentity(endpoint.File, ProjectForFile(endpoint.File, projects));
    return ExtractorRuntime.StableNodeId("api-operation", application, scope, endpoint.Method, endpoint.Route);
}

static string DeclarationIdentity(SyntaxTree tree, int start)
    => $"{tree.FilePath}:{start.ToString(System.Globalization.CultureInfo.InvariantCulture)}";

static bool IsUnderDirectory(string path, string directory)
{
    var fullPath = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    var fullDirectory = Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    return fullPath.Equals(fullDirectory, StringComparison.OrdinalIgnoreCase)
        || fullPath.StartsWith(fullDirectory + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
        || fullPath.StartsWith(fullDirectory + Path.AltDirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
}

static string DisplaySolutionName(SolutionInfo solution)
    => $"{ExtractorRuntime.DisplayFromIdentifier(Path.GetFileNameWithoutExtension(solution.RelativePath))} Solution";

static string DisplayProjectName(ProjectInfo project)
    => ExtractorRuntime.DisplayFromIdentifier(Path.GetFileNameWithoutExtension(project.RelativePath));

static HashSet<string> ReachableClasses(ISet<string> roots, IEnumerable<(string Source, string Target)> invocations, ISet<string> knownClasses)
{
    var reachable = new HashSet<string>(roots, StringComparer.Ordinal);
    var queue = new Queue<string>(roots);
    var outgoing = invocations
        .Where(invocation => knownClasses.Contains(invocation.Source) && knownClasses.Contains(invocation.Target))
        .GroupBy(invocation => invocation.Source, StringComparer.Ordinal)
        .ToDictionary(group => group.Key, group => group.Select(invocation => invocation.Target).Distinct(StringComparer.Ordinal).ToList(), StringComparer.Ordinal);
    while (queue.Count > 0)
    {
        var current = queue.Dequeue();
        if (!outgoing.TryGetValue(current, out var targets)) continue;
        foreach (var target in targets)
            if (reachable.Add(target)) queue.Enqueue(target);
    }
    return reachable;
}

static bool LooksLikeSql(string text)
{
    return Regex.IsMatch(text, @"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|EXECUTE\s+IMMEDIATE)\b", RegexOptions.IgnoreCase);
}

static bool HasDynamicSqlTarget(string text)
{
    return Regex.IsMatch(text, @"\b(?:FROM|JOIN|INTO|UPDATE|MERGE\s+INTO)\s+(?:[A-Za-z_][\w$#]*\s*\.\s*)?\{", RegexOptions.IgnoreCase);
}

static class Args
{
    public static string? Value(string[] args, string name)
    {
        var index = Array.IndexOf(args, name);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }
}

sealed record SolutionInfo(string Id, string RelativePath, string Directory, string Text);
sealed record ProjectInfo(string Id, string RelativePath, string Directory, List<SourceFile> SourceFiles, List<string> ProjectReferences);
