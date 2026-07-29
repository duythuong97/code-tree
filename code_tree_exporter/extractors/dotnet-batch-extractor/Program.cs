using System.Text.Json;
using System.Text.RegularExpressions;
using CodeMap.Extractors;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

var configPath = Args.Value(args, "--config") ?? throw new ArgumentException("Missing --config");
using var document = ExtractorRuntime.LoadConfig(configPath);
var config = document.RootElement;
if (!ExtractorRuntime.String(config, "type").Equals("dotnet-batch", StringComparison.Ordinal))
    throw new ArgumentException("Config type must be dotnet-batch");

var source = ExtractorRuntime.String(config, "source");
var repository = ExtractorRuntime.String(config, "repository", source);
var scope = ExtractorRuntime.String(config, "executableScope", "batch-system");
var systemKey = ExtractorRuntime.String(config, "system", scope);
var root = ExtractorRuntime.ConfigPath(config, "root");
var defaultDatabase = ExtractorRuntime.String(config, "database", FirstFolderDatabase(config));
var inputRoot = ExtractorRuntime.ConfigPath(config, "inputData");
var output = ExtractorRuntime.ConfigPath(config, "output");
var catalog = Catalog.Load(inputRoot);
var folderContexts = FolderContexts(config, root, defaultDatabase);
var mappings = LoadExecutableMappings(Path.Combine(inputRoot, "executable-mappings.csv"));
var workspaceSnapshot = await DotNetWorkspaceLoader.LoadAsync(config, root);
var files = workspaceSnapshot.Files;
var csFiles = files.Where(file => file.SyntaxTree is not null).ToList();
var projects = DiscoverProjects(files, csFiles, workspaceSnapshot.Projects, root, folderContexts, defaultDatabase, repository);
var compilationByTree = workspaceSnapshot.CompilationByTree;

var builder = new PackageBuilder(
    $"dotnet-batch-{source}",
    $"extractor:dotnet-batch/{source}",
    "dotnet-batch-extractor",
    "3.0.0",
    new Dictionary<string, object>
    {
        ["source"] = source,
        ["technology"] = "C# Roslyn Workspace/SemanticModel",
        ["parser"] = "Microsoft.CodeAnalysis.CSharp",
        ["workspace"] = "MSBuildWorkspace",
        ["workspaceProjectCount"] = workspaceSnapshot.LoadedProjectCount,
        ["fallbackFileCount"] = workspaceSnapshot.FallbackFileCount,
    });
builder.FilesScanned = files.Count;

AddWorkspaceIssues(builder, workspaceSnapshot, csFiles);

var projectsById = projects.ToDictionary(project => project.Id, StringComparer.Ordinal);
foreach (var project in projects)
{
    builder.AddNode(project.Id, "DOTNET_PROJECT", Path.GetFileName(project.RelativePath), $"{repository}/{project.RelativePath}", DisplayProjectName(project), systemKey, project.Database, repository, "TECHNICAL", properties: new Dictionary<string, object> { ["path"] = project.RelativePath, ["outputType"] = project.OutputType });
}
foreach (var project in projects)
{
    foreach (var reference in project.ProjectReferences)
        if (projectsById.TryGetValue(reference, out var target))
            builder.AddEdge(project.Id, target.Id, "PROJECT_REFERENCE", "STRUCTURAL");
}

var methods = DiscoverMethods(csFiles, compilationByTree, projects, root);
var methodLookup = methods
    .GroupBy(method => method.Key)
    .ToDictionary(group => group.Key, group => group.OrderBy(method => method.File.RelativePath, StringComparer.Ordinal).ThenBy(method => method.Start).First());
var invocations = new List<MethodInvocationInfo>();
foreach (var file in csFiles)
{
    var model = SemanticModelFor(compilationByTree, file.SyntaxTree!);
    var visibleClassNames = ExtractorRuntime.CompilationIndex(model.Compilation).VisibleClassNames();
    invocations.AddRange(ExtractorRuntime.MethodInvocationEdges(file, model, visibleClassNames));
    invocations.AddRange(TopLevelInvocationEdges(file, model, visibleClassNames));
}
var commandHandlers = DiscoverCommandHandlers(csFiles, compilationByTree, methodLookup);
var dataFindings = CollectDataFindings(csFiles, compilationByTree, catalog, folderContexts, defaultDatabase, methodLookup);
var executables = DiscoverExecutables(source, scope, repository, defaultDatabase, projects, methods, csFiles, mappings);
ExpandExecutableProjectReferences(executables, projectsById);

var nodeCache = new Dictionary<MethodKey, string>();
foreach (var executable in executables.OrderBy(exe => exe.Name, StringComparer.OrdinalIgnoreCase))
{
    builder.AddNode(
        executable.Id,
        "EXECUTABLE",
        executable.Name,
        $"{scope}.{executable.Name}",
        ExtractorRuntime.DisplayFromIdentifier(executable.Name),
        systemKey,
        executable.Database,
        repository,
        properties: executable.Properties());
    foreach (var project in executable.Projects.OrderBy(project => project.RelativePath, StringComparer.Ordinal))
        builder.AddEdge(executable.Id, project.Id, "PROJECT_REFERENCE", "STRUCTURAL");

    var entryId = ExtractorRuntime.StableNodeId("executable-entry", executable.Key, "main");
    builder.AddNode(entryId, "EXECUTABLE_ENTRY_POINT", "Main", $"{executable.Name}.Main", $"{ExtractorRuntime.DisplayFromIdentifier(executable.Name)} Entry", systemKey, executable.Database, repository, "TECHNICAL");
    builder.AddEdge(executable.Id, entryId, "ENTRY_IN");

    var roots = EntryMethodsForExecutable(executable, methods, csFiles).ToHashSet();
    if (roots.Select(root => methodLookup.GetValueOrDefault(root)).FirstOrDefault(method => method is not null) is { } entryMethod)
    {
        var line = ExtractorRuntime.LineForOffset(entryMethod.File, entryMethod.Start);
        builder.AddEvidence("NODE", entryId, entryMethod.File.RelativePath, line, line, "DECLARATION", ExtractorRuntime.LineText(entryMethod.File, line));
    }
    var reachable = ReachableMethods(roots, invocations, methodLookup);
    var handlers = commandHandlers
        .Where(handler => roots.Contains(handler.Source) && reachable.Contains(handler.Handler) && FileBelongsToExecutable(handler.File, executable))
        .GroupBy(handler => handler.Mode, StringComparer.OrdinalIgnoreCase)
        .Select(group => group.OrderBy(handler => handler.File.RelativePath, StringComparer.Ordinal).ThenBy(handler => handler.Start).First())
        .ToList();

    foreach (var handler in handlers.OrderBy(handler => handler.Mode, StringComparer.OrdinalIgnoreCase))
    {
        var modeId = ExtractorRuntime.StableNodeId("command-mode", executable.Key, ExtractorRuntime.Slug(handler.Mode));
        builder.AddNode(modeId, "COMMAND_MODE", handler.Mode, $"{executable.Key}.{handler.Mode}", $"{Capitalize(handler.Mode)} Mode", systemKey, executable.Database, repository);
        builder.AddEdge(entryId, modeId, "CALLS");
        var handlerNode = NodeForMethod(handler.Handler, builder, nodeCache, methodLookup, repository, systemKey, executable.Database, folderContexts);
        builder.AddEdge(modeId, handlerNode, "CALLS", rawOperation: handler.Handler.MemberName);
    }
    foreach (var configuredMode in StringArray(config, "commandModes").Where(mode => handlers.All(handler => !handler.Mode.Equals(mode, StringComparison.OrdinalIgnoreCase))))
    {
        var modeId = ExtractorRuntime.StableNodeId("command-mode", executable.Key, ExtractorRuntime.Slug(configuredMode));
        builder.AddNode(modeId, "COMMAND_MODE", configuredMode, $"{executable.Key}.{configuredMode}", $"{Capitalize(configuredMode)} Mode", systemKey, executable.Database, repository);
        builder.AddEdge(entryId, modeId, "CALLS");
    }

    foreach (var invocation in invocations.OrderBy(invocation => invocation.File.RelativePath, StringComparer.Ordinal).ThenBy(invocation => invocation.Start))
    {
        var sourceKey = InvocationKey(invocation.SourceClass, invocation.SourceMember, invocation.SourceIdentity, invocation.SourceClassIdentity, methodLookup);
        var targetKey = InvocationKey(invocation.TargetClass, invocation.TargetMember, invocation.TargetIdentity, invocation.TargetClassIdentity, methodLookup);
        if (!reachable.Contains(sourceKey) || !reachable.Contains(targetKey)) continue;
        if (roots.Contains(sourceKey) && handlers.Any(handler => handler.Handler.Equals(targetKey))) continue;
        var sourceNode = roots.Contains(sourceKey)
            ? entryId
            : NodeForMethod(sourceKey, builder, nodeCache, methodLookup, repository, systemKey, executable.Database, folderContexts);
        var targetNode = NodeForMethod(targetKey, builder, nodeCache, methodLookup, repository, systemKey, executable.Database, folderContexts);
        var edgeId = builder.AddEdge(sourceNode, targetNode, "CALLS", rawOperation: invocation.TargetMember, properties: new Dictionary<string, object> { ["member"] = invocation.TargetMember });
        var line = ExtractorRuntime.LineForOffset(invocation.File, invocation.Start);
        builder.AddEvidence("EDGE", edgeId, invocation.File.RelativePath, line, line, "CALL", ExtractorRuntime.LineText(invocation.File, line));
    }

    foreach (var finding in dataFindings.Where(finding => reachable.Contains(finding.Owner)).OrderBy(finding => finding.File.RelativePath, StringComparer.Ordinal).ThenBy(finding => finding.Line))
    {
        var sourceNode = NodeForMethod(finding.Owner, builder, nodeCache, methodLookup, repository, systemKey, finding.Database, folderContexts);
        switch (finding)
        {
            case DataEdgeFinding edge:
                var targetNodeId = string.IsNullOrEmpty(edge.RawReference)
                    ? edge.TargetNodeId
                    : EnsureTableReference(builder, edge.Database, systemKey, repository, edge.RawReference);
                var confidence = string.IsNullOrEmpty(edge.RawReference) ? 1.0 : 0.5;
                var edgeProperties = string.IsNullOrEmpty(edge.RawReference)
                    ? null
                    : new Dictionary<string, object> { ["resolution"] = "deferred_global" };
                var edgeId = builder.AddEdge(sourceNode, targetNodeId, edge.EdgeType, rawOperation: edge.RawOperation, confidence: confidence, properties: edgeProperties);
                builder.AddEvidence("EDGE", edgeId, edge.File.RelativePath, edge.Line, edge.Line, edge.EvidenceKind, edge.Snippet, confidence: confidence);
                break;
            case ProcedureFinding procedure:
                var procedureId = EnsureProcedureReference(builder, procedure.Database, systemKey, repository, source, procedure.RawReference);
                var procedureEdgeId = builder.AddEdge(sourceNode, procedureId, "CALLS", rawOperation: procedure.RawOperation);
                builder.AddEvidence("EDGE", procedureEdgeId, procedure.File.RelativePath, procedure.Line, procedure.Line, procedure.EvidenceKind, procedure.Snippet);
                break;
            case SequenceFinding sequence:
                var sequenceId = ExtractorRuntime.SequenceId(sequence.Database, sequence.SequenceName);
                builder.AddNode(sequenceId, "SEQUENCE", sequence.SequenceName, $"{sequence.Database}.{sequence.SequenceName}", sequence.SequenceName, systemKey, sequence.Database, repository, "TECHNICAL");
                builder.AddEdge(sourceNode, sequenceId, "USES", rawOperation: sequence.Operation);
                break;
            case IssueFinding issue:
                builder.AddIssue(issue.IssueType, issue.Severity, issue.Message, sourceNode, issue.RawReference, issue.Database, issue.File.RelativePath, issue.Line, issue.Properties);
                break;
        }
    }

    SemanticCallResolution? ResolveSemanticCall(InvocationExpressionSyntax invocation)
    {
        var model = SemanticModelFor(compilationByTree, invocation.SyntaxTree);
        var target = ResolveInvocationTarget(invocation, model, ExtractorRuntime.CompilationIndex(model.Compilation).VisibleClassNames());
        if (target is null) return new SemanticCallResolution("unresolved");
        target = CanonicalKey(target, methodLookup);
        var label = $"Call {target.ClassName}.{target.MemberName}";
        if (!methodLookup.ContainsKey(target)) return new SemanticCallResolution("unresolved", Label: label);
        return new SemanticCallResolution("resolved", NodeForMethod(target, builder, nodeCache, methodLookup, repository, systemKey, executable.Database, folderContexts), label);
    }

    var rootMethods = roots
        .Select(key => methodLookup.GetValueOrDefault(key))
        .Where(method => method is not null)
        .Cast<MethodDeclarationInfo>()
        .OrderBy(method => method.File.RelativePath, StringComparer.Ordinal)
        .ThenBy(method => method.Start)
        .ToList();
    if (rootMethods.Count > 0)
    {
        var parameters = rootMethods.Select(method => method.Syntax).OfType<MethodDeclarationSyntax>().FirstOrDefault()?.ParameterList.Parameters;
        builder.SetNodeProperty(entryId, "semantic_tree", SemanticTreeV3.Operation(
            "Main",
            parameters,
            rootMethods.Select(method => (method.File, method.Syntax)),
            ResolveSemanticCall));
    }
    foreach (var handler in handlers)
    {
        var modeId = ExtractorRuntime.StableNodeId("command-mode", executable.Key, ExtractorRuntime.Slug(handler.Mode));
        builder.SetNodeProperty(modeId, "semantic_tree", SemanticTreeV3.Operation(handler.Mode, null, new[] { handler.Syntax }, handler.File, ResolveSemanticCall));
    }
    foreach (var key in reachable.Where(key => !roots.Contains(key)).OrderBy(key => key.ClassName, StringComparer.Ordinal).ThenBy(key => key.MemberName, StringComparer.Ordinal))
    {
        if (!methodLookup.TryGetValue(key, out var method)) continue;
        var methodId = NodeForMethod(key, builder, nodeCache, methodLookup, repository, systemKey, executable.Database, folderContexts);
        var parameters = (method.Syntax as MethodDeclarationSyntax)?.ParameterList.Parameters;
        builder.SetNodeProperty(methodId, "semantic_tree", SemanticTreeV3.Operation(key.MemberName, parameters, new[] { method.Syntax }, method.File, ResolveSemanticCall));
    }
}

AddJobEdges(builder, inputRoot, executables, scope);
builder.Write(output);

static SemanticModel SemanticModelFor(IReadOnlyDictionary<SyntaxTree, CSharpCompilation> compilations, SyntaxTree tree)
    => ExtractorRuntime.CompilationIndex(compilations[tree]).SemanticModel(tree);

static void AddWorkspaceIssues(PackageBuilder builder, DotNetWorkspaceSnapshot snapshot, IReadOnlyCollection<SourceFile> csFiles)
{
    var syntaxErrorCount = csFiles.Sum(file => file.SyntaxTree!.GetDiagnostics().Count(diagnostic => diagnostic.Severity == DiagnosticSeverity.Error));
    if (syntaxErrorCount > 0)
    {
        builder.AddIssue(
            "CSHARP_PARSE_ERROR",
            "WARNING",
            $"Roslyn found {syntaxErrorCount} C# syntax error(s); valid syntax and available semantic links are retained",
            properties: new Dictionary<string, object> { ["syntaxErrorCount"] = syntaxErrorCount });
    }
    if (snapshot.Diagnostics.Count > 0)
    {
        builder.AddIssue(
            "MSBUILD_WORKSPACE_DIAGNOSTIC",
            "WARNING",
            $"MSBuildWorkspace reported {snapshot.Diagnostics.Count} load diagnostic(s); affected projects use best-effort extraction",
            properties: new Dictionary<string, object>
            {
                ["diagnosticCount"] = snapshot.Diagnostics.Count,
                ["samples"] = snapshot.Diagnostics.Take(10).ToArray(),
            });
    }
    if (snapshot.FallbackFileCount > 0)
    {
        builder.AddIssue(
            "MSBUILD_WORKSPACE_FALLBACK",
            "WARNING",
            $"{snapshot.FallbackFileCount} C# file(s) were not loaded by a project and use a fallback compilation",
            properties: new Dictionary<string, object> { ["fallbackFileCount"] = snapshot.FallbackFileCount });
    }
}

static List<ProjectInfo> DiscoverProjects(List<SourceFile> files, List<SourceFile> csFiles, IReadOnlyList<WorkspaceProjectInfo> workspaceProjects, string root, List<FolderContext> folderContexts, string defaultDatabase, string repository)
{
    var projects = new List<ProjectInfo>();
    var workspaceByPath = workspaceProjects.ToDictionary(project => project.FilePath, ExtractorRuntime.PathComparer);
    foreach (var file in files.Where(file => Path.GetExtension(file.AbsolutePath).Equals(".csproj", StringComparison.OrdinalIgnoreCase)).OrderBy(file => file.AbsolutePath, StringComparer.Ordinal))
    {
        var relativePath = ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, file.AbsolutePath));
        var directory = Path.GetDirectoryName(file.AbsolutePath) ?? root;
        workspaceByPath.TryGetValue(file.AbsolutePath, out var workspaceProject);
        var projectFiles = workspaceProject is null
            ? csFiles.Where(source => IsUnderDirectory(source.AbsolutePath, directory)).ToList()
            : csFiles.Where(source => workspaceProject.DocumentPaths.Contains(source.AbsolutePath, ExtractorRuntime.PathComparer)).ToList();
        var sourcePaths = projectFiles.Select(source => source.AbsolutePath).ToHashSet(ExtractorRuntime.PathComparer);
        var outputType = MsBuildProjectXml.Property(file.Text, "OutputType");
        var assemblyName = MsBuildProjectXml.Property(file.Text, "AssemblyName");
        if (string.IsNullOrWhiteSpace(assemblyName)) assemblyName = Path.GetFileNameWithoutExtension(file.AbsolutePath);
        var targetName = MsBuildProjectXml.Property(file.Text, "TargetName");
        if (string.IsNullOrWhiteSpace(targetName)) targetName = assemblyName;
        var hasEntry = projectFiles.Any(HasMainOrTopLevelStatements);
        var explicitlyLibrary = outputType.Equals("Library", StringComparison.OrdinalIgnoreCase);
        var isExecutable = outputType.Equals("Exe", StringComparison.OrdinalIgnoreCase)
            || outputType.Equals("WinExe", StringComparison.OrdinalIgnoreCase)
            || (hasEntry && !explicitlyLibrary);
        projects.Add(new ProjectInfo(
            ExtractorRuntime.StableNodeId("dotnet-project", repository, relativePath),
            relativePath,
            directory,
            DatabaseForPath(file.AbsolutePath, folderContexts, defaultDatabase),
            assemblyName,
            targetName,
            outputType,
            isExecutable,
            sourcePaths,
            ProjectReferenceIds(
                workspaceProject?.ProjectReferencePaths
                    ?? MsBuildProjectXml.Includes(file.Text, "ProjectReference")
                        .Select(include => Path.GetFullPath(Path.Combine(directory, ExtractorRuntime.NativePath(include))))
                        .ToList(),
                root,
                repository)));
    }
    return projects;
}

static List<ExecutableInfo> DiscoverExecutables(string source, string scope, string repository, string defaultDatabase, List<ProjectInfo> projects, List<MethodDeclarationInfo> methods, List<SourceFile> csFiles, List<ExecutableMapping> mappings)
{
    var byName = new Dictionary<string, ExecutableInfo>(StringComparer.OrdinalIgnoreCase);
    foreach (var project in projects.Where(project => project.IsExecutable))
    {
        var name = NormalizeExecutableName(!string.IsNullOrWhiteSpace(project.TargetName) ? project.TargetName : project.AssemblyName);
        var info = GetOrCreateExecutable(byName, name, scope, repository, project.Database, project);
        info.AddProject(project);
        info.AddName(project.AssemblyName);
        info.AddName(project.TargetName);
        info.AddAssembly(project.AssemblyName);
    }

    if (byName.Count == 0)
    {
        var fallback = GetOrCreateExecutable(byName, NormalizeExecutableName(source), scope, repository, defaultDatabase, null);
        foreach (var method in methods.Where(method => method.IsEntryPoint))
            fallback.AddLooseEntryFile(method.File);
        if (fallback.LooseEntryFiles.Count == 0)
            foreach (var file in csFiles.Where(HasMainOrTopLevelStatements)) fallback.AddLooseEntryFile(file);
    }

    foreach (var mapping in mappings)
    {
        var canonical = NormalizeExecutableName(mapping.CanonicalExecutableName);
        if (!byName.TryGetValue(canonical, out var executable)) continue;
        if (!string.IsNullOrWhiteSpace(mapping.Alias)) executable.AddAlias(mapping.Alias);
    }
    return byName.Values.ToList();
}

static void ExpandExecutableProjectReferences(List<ExecutableInfo> executables, Dictionary<string, ProjectInfo> projectsById)
{
    foreach (var executable in executables)
    {
        var queue = new Queue<ProjectInfo>(executable.Projects);
        var seen = executable.Projects.Select(project => project.Id).ToHashSet(StringComparer.Ordinal);
        while (queue.Count > 0)
        {
            var project = queue.Dequeue();
            foreach (var reference in project.ProjectReferences)
            {
                if (!seen.Add(reference) || !projectsById.TryGetValue(reference, out var target)) continue;
                executable.AddProject(target, exposeExecutableNames: false);
                queue.Enqueue(target);
            }
        }
    }
}

static ExecutableInfo GetOrCreateExecutable(Dictionary<string, ExecutableInfo> byName, string name, string scope, string repository, string database, ProjectInfo? project)
{
    if (!byName.TryGetValue(name, out var info))
    {
        info = new ExecutableInfo(name, scope, repository, database);
        byName[name] = info;
    }
    if (project is not null) info.AddProject(project);
    return info;
}

static List<MethodDeclarationInfo> DiscoverMethods(List<SourceFile> csFiles, IReadOnlyDictionary<SyntaxTree, CSharpCompilation> compilationByTree, List<ProjectInfo> projects, string root)
{
    var methods = new List<MethodDeclarationInfo>();
    foreach (var file in csFiles)
    {
        var rootNode = file.SyntaxTree!.GetRoot();
        var model = SemanticModelFor(compilationByTree, file.SyntaxTree);
        var firstGlobalStatement = rootNode.DescendantNodes().OfType<GlobalStatementSyntax>().FirstOrDefault();
        if (firstGlobalStatement is not null)
            methods.Add(new MethodDeclarationInfo(TopLevelKey(file), file, firstGlobalStatement.SpanStart, true, rootNode));
        foreach (var cls in rootNode.DescendantNodes().OfType<ClassDeclarationSyntax>())
        {
            var classIdentity = ScopedTypeIdentity(model.GetDeclaredSymbol(cls), file, projects, root);
            foreach (var method in cls.Members.OfType<MethodDeclarationSyntax>())
            {
                var key = new MethodKey(cls.Identifier.Text, method.Identifier.Text, DeclarationIdentity(method.SyntaxTree, method.SpanStart), classIdentity);
                var isEntry = method.Identifier.Text.Equals("Main", StringComparison.Ordinal) && method.Modifiers.Any(SyntaxKind.StaticKeyword);
                methods.Add(new MethodDeclarationInfo(key, file, method.SpanStart, isEntry, method));
            }
        }
    }
    return methods;
}

static IEnumerable<MethodInvocationInfo> TopLevelInvocationEdges(SourceFile file, SemanticModel model, HashSet<string> classNames)
{
    if (file.SyntaxTree is null) yield break;
    var source = TopLevelKey(file);
    foreach (var invocation in file.SyntaxTree.GetRoot().DescendantNodes().OfType<InvocationExpressionSyntax>())
    {
        if (!invocation.Ancestors().OfType<GlobalStatementSyntax>().Any()) continue;
        var target = ResolveInvocationTarget(invocation, model, classNames);
        if (target is null || target.Equals(source)) continue;
        yield return new MethodInvocationInfo(source.ClassName, source.MemberName, target.ClassName, target.MemberName, invocation.SpanStart, file, source.Identity, target.Identity, source.ClassIdentity, target.ClassIdentity);
    }
}

static List<CommandHandlerInfo> DiscoverCommandHandlers(List<SourceFile> csFiles, IReadOnlyDictionary<SyntaxTree, CSharpCompilation> compilationByTree, IReadOnlyDictionary<MethodKey, MethodDeclarationInfo> methods)
{
    var handlers = new List<CommandHandlerInfo>();
    foreach (var file in csFiles)
    {
        var model = SemanticModelFor(compilationByTree, file.SyntaxTree!);
        var classNames = ExtractorRuntime.CompilationIndex(model.Compilation).VisibleClassNames();
        var root = file.SyntaxTree!.GetRoot();
        foreach (var ifStatement in root.DescendantNodes().OfType<IfStatementSyntax>())
        {
            var source = CanonicalKey(OwnerForNode(file, ifStatement), methods);
            if (!IsValidOwner(source)) continue;
            var target = FirstHandlerTarget(ifStatement.Statement, model, classNames, source, methods);
            if (target is null) continue;
            foreach (var mode in ModesFromCondition(ifStatement.Condition))
                handlers.Add(new CommandHandlerInfo(mode, source, target, file, ifStatement.SpanStart, ifStatement.Statement));
        }

        foreach (var switchStatement in root.DescendantNodes().OfType<SwitchStatementSyntax>())
        {
            var source = CanonicalKey(OwnerForNode(file, switchStatement), methods);
            if (!IsValidOwner(source)) continue;
            foreach (var section in switchStatement.Sections)
            {
                var target = FirstHandlerTarget(section, model, classNames, source, methods);
                if (target is null) continue;
                foreach (var mode in section.Labels.OfType<CaseSwitchLabelSyntax>().SelectMany(label => ModeValues(label.Value)))
                    handlers.Add(new CommandHandlerInfo(mode, source, target, file, section.SpanStart, section));
            }
        }

        foreach (var switchExpression in root.DescendantNodes().OfType<SwitchExpressionSyntax>())
        {
            var source = CanonicalKey(OwnerForNode(file, switchExpression), methods);
            if (!IsValidOwner(source)) continue;
            foreach (var arm in switchExpression.Arms)
            {
                var target = FirstHandlerTarget(arm.Expression, model, classNames, source, methods);
                if (target is null) continue;
                foreach (var mode in ModeValuesFromPattern(arm.Pattern))
                    handlers.Add(new CommandHandlerInfo(mode, source, target, file, arm.SpanStart, arm.Expression));
            }
        }
    }
    return handlers;
}

static MethodKey? FirstHandlerTarget(SyntaxNode node, SemanticModel model, HashSet<string> classNames, MethodKey source, IReadOnlyDictionary<MethodKey, MethodDeclarationInfo> methods)
{
    foreach (var invocation in node.DescendantNodesAndSelf().OfType<InvocationExpressionSyntax>())
    {
        var target = ResolveInvocationTarget(invocation, model, classNames);
        if (target is not null) target = CanonicalKey(target, methods);
        if (target is not null && !target.Equals(source)) return target;
    }
    return null;
}

static IEnumerable<string> ModesFromCondition(ExpressionSyntax condition)
{
    foreach (var binary in condition.DescendantNodesAndSelf().OfType<BinaryExpressionSyntax>().Where(binary => binary.IsKind(SyntaxKind.EqualsExpression)))
        foreach (var mode in ModeValues(binary))
            yield return mode;
    foreach (var invocation in condition.DescendantNodesAndSelf().OfType<InvocationExpressionSyntax>())
    {
        var name = InvocationName(invocation);
        if (!name.Equals("Equals", StringComparison.Ordinal) && !name.Equals("Contains", StringComparison.Ordinal)) continue;
        foreach (var mode in invocation.ArgumentList.Arguments.SelectMany(argument => ModeValues(argument.Expression)))
            yield return mode;
    }
}

static IEnumerable<string> ModeValues(SyntaxNode node)
{
    foreach (var literal in node.DescendantNodesAndSelf().OfType<LiteralExpressionSyntax>())
        if (literal.IsKind(SyntaxKind.StringLiteralExpression) && IsCommandMode(literal.Token.ValueText))
            yield return literal.Token.ValueText;
}

static IEnumerable<string> ModeValuesFromPattern(PatternSyntax pattern)
{
    if (pattern is ConstantPatternSyntax constant)
        foreach (var mode in ModeValues(constant.Expression)) yield return mode;
}

static bool IsCommandMode(string value)
    => !string.IsNullOrWhiteSpace(value) && !value.StartsWith("--", StringComparison.Ordinal);

static bool IsValidOwner(MethodKey owner)
    => !string.IsNullOrEmpty(owner.ClassName) && !string.IsNullOrEmpty(owner.MemberName);

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

static string EnsureTableReference(PackageBuilder builder, string database, string systemKey, string repository, string rawReference)
{
    var raw = rawReference.Trim().ToUpperInvariant();
    var parts = raw.Split('.', StringSplitOptions.RemoveEmptyEntries);
    var schema = parts.Length >= 2 ? parts[^2] : "";
    var table = ExtractorRuntime.LeafIdentifier(raw);
    var identity = string.IsNullOrEmpty(schema) ? $"TABLE:{table}" : $"TABLE:{schema}:{table}";
    var nodeId = ExtractorRuntime.StableNodeId("unresolved-reference", database, identity);
    builder.AddNode(nodeId, "UNRESOLVED_REFERENCE", table, string.IsNullOrEmpty(schema) ? $"{database}.{table}" : $"{database}.{schema}.{table}", table, systemKey, database, repository, "TECHNICAL", confidence: 0.2, properties: new Dictionary<string, object>
    {
        ["database"] = database,
        ["schema"] = schema,
        ["table"] = table,
        ["raw_reference"] = raw,
    });
    return nodeId;
}

static List<DataFinding> CollectDataFindings(List<SourceFile> csFiles, IReadOnlyDictionary<SyntaxTree, CSharpCompilation> compilationByTree, Catalog catalog, List<FolderContext> folderContexts, string defaultDatabase, IReadOnlyDictionary<MethodKey, MethodDeclarationInfo> methods)
{
    var findings = new List<DataFinding>();
    foreach (var file in csFiles)
    {
        var model = SemanticModelFor(compilationByTree, file.SyntaxTree!);
        var database = DatabaseForPath(file.AbsolutePath, folderContexts, defaultDatabase);
        foreach (var expression in ExtractorRuntime.StringExpressions(file, model))
        {
            var owner = CanonicalKey(OwnerForOffset(file, expression.Start), methods);
            if (string.IsNullOrEmpty(owner.ClassName) || string.IsNullOrEmpty(owner.MemberName)) continue;
            if (!IsLikelySql(expression.Value)) continue;
            var line = ExtractorRuntime.LineForOffset(file, expression.Start);
            var snippet = ExtractorRuntime.LineText(file, line);
            SqlAnalysis analysis;
            try
            {
                analysis = SqlAnalyzer.Analyze(expression.Value);
            }
            catch (Exception exception)
            {
                var detail = exception.Message.Length <= 1000 ? exception.Message : exception.Message[..1000];
                findings.Add(new IssueFinding(
                    owner,
                    "SQL_PARSE_ERROR",
                    "WARNING",
                    $"Embedded SQL parser unavailable: {detail}",
                    expression.ExpressionText,
                    file,
                    line,
                    database));
                continue;
            }
            if (!analysis.Recognized) continue;
            if (expression.IsDynamic)
                findings.Add(new IssueFinding(owner, "DYNAMIC_SQL", "WARNING", "Runtime SQL target cannot be resolved", expression.ExpressionText, file, line, database, new Dictionary<string, object> { ["expression"] = expression.ExpressionText, ["evaluated"] = expression.Value }));
            foreach (var parseErrorOffset in analysis.ParseErrorOffsets)
                findings.Add(new IssueFinding(owner, "SQL_PARSE_ERROR", "WARNING", "Embedded SQL could not be parsed completely", expression.ExpressionText, file, ExtractorRuntime.LineForOffset(file, expression.Start + parseErrorOffset), database));
            foreach (var tableRef in analysis.Tables)
            {
                var tableName = ExtractorRuntime.LeafIdentifier(tableRef.ObjectName);
                if (!catalog.HasTable(database, tableName))
                {
                    var unresolvedId = ExtractorRuntime.StableNodeId(
                        "unresolved-reference",
                        database,
                        tableRef.ObjectName.Contains('.')
                            ? $"TABLE:{string.Join(':', tableRef.ObjectName.ToUpperInvariant().Split('.', StringSplitOptions.RemoveEmptyEntries).TakeLast(2))}"
                            : $"TABLE:{tableName}");
                    findings.Add(new DataEdgeFinding(owner, unresolvedId, tableRef.EdgeType, tableRef.Operation, file, line, "SQL", snippet, database, tableRef.ObjectName));
                    findings.Add(new IssueFinding(owner, "TABLE_NOT_IMPORTED", "ERROR", "Table is absent from authoritative catalog", tableName, file, line, database));
                    continue;
                }
                findings.Add(new DataEdgeFinding(owner, ExtractorRuntime.TableId(database, tableName), tableRef.EdgeType, tableRef.Operation, file, line, "SQL", snippet, database));
            }
            foreach (var sequence in analysis.Sequences)
                findings.Add(new SequenceFinding(owner, ExtractorRuntime.LeafIdentifier(sequence.ObjectName), sequence.Operation, file, line, database));
            foreach (var offset in analysis.DynamicOffsets)
                findings.Add(new IssueFinding(owner, "DYNAMIC_SQL", "WARNING", "Runtime SQL target cannot be resolved", expression.ExpressionText, file, ExtractorRuntime.LineForOffset(file, expression.Start + offset), database));
        }
    }
    return findings;
}

static bool IsLikelySql(string text)
{
    if (string.IsNullOrWhiteSpace(text) || text.Length < 10) return false;
    var upper = text.ToUpperInvariant();
    return upper.Contains("SELECT ")
        || upper.Contains("INSERT ")
        || upper.Contains("UPDATE ")
        || upper.Contains("DELETE ")
        || upper.Contains("MERGE ")
        || upper.Contains("EXEC ");
}

static IEnumerable<MethodKey> EntryMethodsForExecutable(ExecutableInfo executable, List<MethodDeclarationInfo> methods, List<SourceFile> csFiles)
{
    var entries = methods.Where(method => method.IsEntryPoint && FileBelongsToExecutable(method.File, executable)).Select(method => method.Key).Distinct().ToList();
    if (entries.Count > 0) return entries;
    if (executable.Projects.Count == 0)
    {
        entries = methods.Where(method => method.IsEntryPoint).Select(method => method.Key).Distinct().ToList();
        if (entries.Count > 0) return entries;
    }
    return Enumerable.Empty<MethodKey>();
}

static HashSet<MethodKey> ReachableMethods(HashSet<MethodKey> roots, List<MethodInvocationInfo> invocations, IReadOnlyDictionary<MethodKey, MethodDeclarationInfo> methods)
{
    var reachable = new HashSet<MethodKey>(roots);
    var queue = new Queue<MethodKey>(roots);
    var outgoing = invocations
        .GroupBy(invocation => InvocationKey(invocation.SourceClass, invocation.SourceMember, invocation.SourceIdentity, invocation.SourceClassIdentity, methods))
        .ToDictionary(group => group.Key, group => group.Select(invocation => InvocationKey(invocation.TargetClass, invocation.TargetMember, invocation.TargetIdentity, invocation.TargetClassIdentity, methods)).Distinct().ToList());
    while (queue.Count > 0)
    {
        var current = queue.Dequeue();
        if (!outgoing.TryGetValue(current, out var targets)) continue;
        foreach (var target in targets)
            if (reachable.Add(target)) queue.Enqueue(target);
    }
    return reachable;
}

static string NodeForMethod(MethodKey key, PackageBuilder builder, Dictionary<MethodKey, string> cache, Dictionary<MethodKey, MethodDeclarationInfo> methods, string repository, string systemKey, string fallbackDatabase, List<FolderContext> folderContexts)
{
    if (cache.TryGetValue(key, out var cached)) return cached;
    var database = methods.TryGetValue(key, out var method) ? DatabaseForPath(method.File.AbsolutePath, folderContexts, fallbackDatabase) : fallbackDatabase;
    var classIdentity = string.IsNullOrEmpty(key.ClassIdentity)
        ? method is null ? string.Empty : $"{method.File.RelativePath}|{key.ClassName}"
        : key.ClassIdentity;
    var (classPrefix, classType) = key.ClassName.EndsWith("Repository", StringComparison.Ordinal)
        ? ("repository", "REPOSITORY")
        : key.ClassName.EndsWith("Service", StringComparison.Ordinal)
            ? ("service", "SERVICE")
            : ("csharp-type", "CSHARP_TYPE");
    var classId = ExtractorRuntime.StableNodeId(classPrefix, repository, classIdentity);
    builder.AddNode(classId, classType, key.ClassName, classIdentity, key.ClassName, systemKey, database, repository, "TECHNICAL");
    var declarationKey = method is null ? $"{classIdentity}.{key.MemberName}" : $"{method.File.RelativePath}:{method.Start}:{key.ClassName}.{key.MemberName}";
    var methodId = ExtractorRuntime.StableNodeId("method", repository, declarationKey);
    builder.AddNode(methodId, "METHOD", key.MemberName, $"{repository}.{declarationKey}", key.MemberName, systemKey, database, repository, "TECHNICAL");
    builder.AddEdge(classId, methodId, "CONTAINS", "STRUCTURAL");
    if (method is not null)
    {
        var line = ExtractorRuntime.LineForOffset(method.File, method.Start);
        builder.AddEvidence("NODE", methodId, method.File.RelativePath, line, ExtractorRuntime.LineForOffset(method.File, method.Syntax.Span.End), "DECLARATION", ExtractorRuntime.LineText(method.File, line));
    }
    return cache[key] = methodId;
}

static void AddJobEdges(PackageBuilder builder, string inputRoot, List<ExecutableInfo> executables, string scope)
{
    var jobnetPath = Path.Combine(inputRoot, "jobnet.csv");
    var mappings = LoadExecutableMappings(Path.Combine(inputRoot, "executable-mappings.csv"));
    if (!File.Exists(jobnetPath)) return;
    var byFilename = executables.SelectMany(exe => exe.Names.Select(name => (name, exe))).GroupBy(item => item.name, StringComparer.OrdinalIgnoreCase).ToDictionary(group => group.Key, group => group.Select(item => item.exe).Distinct().ToList(), StringComparer.OrdinalIgnoreCase);
    var byAssembly = executables.SelectMany(exe => exe.AssemblyNames.Select(name => (name, exe))).GroupBy(item => item.name, StringComparer.OrdinalIgnoreCase).ToDictionary(group => group.Key, group => group.Select(item => item.exe).Distinct().ToList(), StringComparer.OrdinalIgnoreCase);
    var byAlias = executables.SelectMany(exe => exe.Aliases.Select(alias => (alias, exe))).GroupBy(item => item.alias, StringComparer.OrdinalIgnoreCase).ToDictionary(group => group.Key, group => group.Select(item => item.exe).Distinct().ToList(), StringComparer.OrdinalIgnoreCase);
    var rows = Csv.Read(jobnetPath);
    for (var index = 0; index < rows.Count; index++)
    {
        var row = rows[index];
        var jobnetId = row.GetValueOrDefault("jobnet_id", string.Empty);
        var jobIdValue = row.GetValueOrDefault("job_id", string.Empty);
        var jobNetwork = ExtractorRuntime.StableNodeId("job-network", "batch-system", jobnetId);
        var job = ExtractorRuntime.StableNodeId("job", "batch-system", jobnetId, jobIdValue);
        builder.AddEdge(jobNetwork, job, "CONTAINS", "STRUCTURAL");
        var predecessor = row.GetValueOrDefault("predecessor_job_id", string.Empty);
        if (!string.IsNullOrWhiteSpace(predecessor))
            builder.AddEdge(job, ExtractorRuntime.StableNodeId("job", "batch-system", jobnetId, predecessor), "DEPENDS_ON", "STRUCTURAL");
        var rawExecutable = row.GetValueOrDefault("executable_name", string.Empty);
        var matches = ResolveJobExecutable(rawExecutable, "batch-system", scope, mappings, byFilename, byAssembly, byAlias);
        if (matches.Count == 1)
            builder.AddEdge(job, matches[0].Id, "STARTS");
        else
        {
            var issueType = matches.Count > 1 ? "AMBIGUOUS_SYMBOL" : "EXECUTABLE_NOT_MAPPED";
            builder.AddIssue(issueType, "WARNING", matches.Count > 1 ? "Executable mapping is ambiguous" : "Executable not mapped", job, rawExecutable, string.Empty, "input-data/jobnet.csv", index + 2);
        }
    }
}

static List<ExecutableInfo> ResolveJobExecutable(string rawExecutable, string jobSystem, string scope, List<ExecutableMapping> mappings, Dictionary<string, List<ExecutableInfo>> byFilename, Dictionary<string, List<ExecutableInfo>> byAssembly, Dictionary<string, List<ExecutableInfo>> byAlias)
{
    var explicitMapping = mappings.FirstOrDefault(mapping => mapping.JobSystem.Equals(jobSystem, StringComparison.OrdinalIgnoreCase)
        && mapping.ExecutableScope.Equals(scope, StringComparison.OrdinalIgnoreCase)
        && mapping.ExecutableName.Equals(rawExecutable, StringComparison.OrdinalIgnoreCase));
    if (explicitMapping is not null)
        return ResolveCanonicalExecutable(explicitMapping.CanonicalExecutableName, byFilename, byAssembly, byAlias);
    var filename = NormalizeExecutableName(rawExecutable);
    var filenameMatches = Lookup(byFilename, filename);
    if (filenameMatches.Count > 0) return filenameMatches;
    var assemblyMatches = Lookup(byAssembly, NormalizeAssemblyName(rawExecutable));
    if (assemblyMatches.Count > 0) return assemblyMatches;
    var aliasMatches = Lookup(byAlias, NormalizeAlias(rawExecutable));
    if (aliasMatches.Count > 0) return aliasMatches;
    var aliasMapping = mappings.FirstOrDefault(mapping => mapping.JobSystem.Equals(jobSystem, StringComparison.OrdinalIgnoreCase)
        && mapping.ExecutableScope.Equals(scope, StringComparison.OrdinalIgnoreCase)
        && NormalizeAlias(mapping.Alias).Equals(NormalizeAlias(rawExecutable), StringComparison.OrdinalIgnoreCase));
    return aliasMapping is null ? new List<ExecutableInfo>() : ResolveCanonicalExecutable(aliasMapping.CanonicalExecutableName, byFilename, byAssembly, byAlias);
}

static List<ExecutableInfo> ResolveCanonicalExecutable(string canonical, Dictionary<string, List<ExecutableInfo>> byFilename, Dictionary<string, List<ExecutableInfo>> byAssembly, Dictionary<string, List<ExecutableInfo>> byAlias)
{
    var filenameMatches = Lookup(byFilename, NormalizeExecutableName(canonical));
    if (filenameMatches.Count > 0) return filenameMatches;
    var assemblyMatches = Lookup(byAssembly, NormalizeAssemblyName(canonical));
    if (assemblyMatches.Count > 0) return assemblyMatches;
    return Lookup(byAlias, NormalizeAlias(canonical));
}

static List<ExecutableInfo> Lookup(Dictionary<string, List<ExecutableInfo>> index, string key)
    => index.TryGetValue(key, out var values) ? values : new List<ExecutableInfo>();

static List<string> ProjectReferenceIds(IEnumerable<string> paths, string root, string repository)
{
    return paths
        .Where(path => ExtractorRuntime.IsWithin(path, root))
        .Select(path => ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, path)))
        .Select(path => ExtractorRuntime.StableNodeId("dotnet-project", repository, path))
        .ToList();
}

static bool HasMainOrTopLevelStatements(SourceFile file)
{
    if (file.SyntaxTree is null) return false;
    var root = file.SyntaxTree.GetRoot();
    return root.DescendantNodes().OfType<MethodDeclarationSyntax>().Any(method => method.Identifier.Text.Equals("Main", StringComparison.Ordinal))
        || HasTopLevelStatements(file);
}

static bool HasTopLevelStatements(SourceFile file)
    => file.SyntaxTree?.GetRoot().DescendantNodes().OfType<GlobalStatementSyntax>().Any() == true;

static List<FolderContext> FolderContexts(JsonElement config, string root, string fallbackDatabase)
{
    var result = new List<FolderContext>();
    if (config.TryGetProperty("folders", out var folders) && folders.ValueKind == JsonValueKind.Array)
    {
        foreach (var folder in folders.EnumerateArray())
        {
            var relative = folder.ValueKind == JsonValueKind.String
                ? folder.GetString() ?? "."
                : folder.ValueKind == JsonValueKind.Object ? ExtractorRuntime.String(folder, "path", ".") : ".";
            var absolute = Path.GetFullPath(Path.Combine(root, ExtractorRuntime.NativePath(relative)));
            if (!ExtractorRuntime.IsWithin(absolute, root)) throw new ArgumentException($"Folder escapes root: {relative}");
            var database = folder.ValueKind == JsonValueKind.Object ? ExtractorRuntime.String(folder, "database", fallbackDatabase) : fallbackDatabase;
            result.Add(new FolderContext(absolute, database));
        }
    }
    if (result.Count == 0) result.Add(new FolderContext(root, fallbackDatabase));
    return result.OrderByDescending(item => item.AbsolutePath.Length).ToList();
}

static string DatabaseForPath(string absolutePath, List<FolderContext> contexts, string fallbackDatabase)
{
    var full = Path.GetFullPath(absolutePath);
    foreach (var context in contexts)
        if (IsUnderDirectory(full, context.AbsolutePath) || full.Equals(context.AbsolutePath, StringComparison.OrdinalIgnoreCase))
            return context.Database;
    return fallbackDatabase;
}

static bool FileBelongsToExecutable(SourceFile file, ExecutableInfo executable)
{
    if (executable.Projects.Count == 0)
        return executable.LooseEntryFiles.Count == 0 || executable.LooseEntryFiles.Any(entry => entry.AbsolutePath.Equals(file.AbsolutePath, StringComparison.OrdinalIgnoreCase)) || executable.LooseEntryFiles.Any(entry => Path.GetDirectoryName(entry.AbsolutePath)?.Equals(Path.GetDirectoryName(file.AbsolutePath), StringComparison.OrdinalIgnoreCase) == true);
    return executable.Projects.Any(project => project.SourcePaths.Contains(file.AbsolutePath))
        || executable.Projects.Any(project => IsUnderDirectory(file.AbsolutePath, project.Directory));
}

static bool IsUnderDirectory(string path, string directory)
    => ExtractorRuntime.IsWithin(path, directory);

static string NormalizeExecutableName(string value)
{
    return BatchNames.NormalizeExecutableName(value);
}

static string NormalizeAssemblyName(string value)
    => BatchNames.NormalizeAssemblyName(value);

static string NormalizeAlias(string value) => BatchNames.NormalizeAlias(value);

static string DisplayProjectName(ProjectInfo project)
    => ExtractorRuntime.DisplayFromIdentifier(Path.GetFileNameWithoutExtension(project.RelativePath));

static MethodKey? ResolveInvocationTarget(InvocationExpressionSyntax invocation, SemanticModel model, HashSet<string> knownClasses)
{
    var symbol = InvocationMethodSymbol(invocation, model);
    if (symbol is not null)
    {
        var target = ResolveInterfaceImplementation(symbol, model.Compilation, knownClasses) ?? symbol;
        if (symbol.ContainingType.TypeKind == TypeKind.Interface && SymbolEqualityComparer.Default.Equals(target, symbol)) return null;
        var typeName = target.ContainingType?.Name ?? string.Empty;
        var known = MatchKnownClass(typeName, knownClasses);
        var reference = target.DeclaringSyntaxReferences.FirstOrDefault();
        var identity = reference is null ? string.Empty : DeclarationIdentity(reference.SyntaxTree, reference.Span.Start);
        if (!string.IsNullOrEmpty(known)) return new MethodKey(known, target.Name, identity, TypeIdentity(target.ContainingType));
    }
    if (invocation.Expression is MemberAccessExpressionSyntax member)
    {
        var type = model.GetTypeInfo(member.Expression).Type ?? model.GetTypeInfo(member.Expression).ConvertedType;
        var known = MatchKnownClass(type?.Name ?? string.Empty, knownClasses);
        if (!string.IsNullOrEmpty(known)) return new MethodKey(known, member.Name.Identifier.Text);
        if (member.Expression is ObjectCreationExpressionSyntax objectCreation)
        {
            known = MatchKnownClass(objectCreation.Type.ToString().Split('.').Last(), knownClasses);
            if (!string.IsNullOrEmpty(known)) return new MethodKey(known, member.Name.Identifier.Text);
        }
        if (member.Expression is IdentifierNameSyntax identifier)
        {
            known = MatchVariableNameToKnownClass(identifier.Identifier.Text, knownClasses);
            if (!string.IsNullOrEmpty(known)) return new MethodKey(known, member.Name.Identifier.Text);
        }
    }
    return null;
}

static IMethodSymbol? InvocationMethodSymbol(InvocationExpressionSyntax invocation, SemanticModel model)
{
    var info = model.GetSymbolInfo(invocation);
    if (info.Symbol is IMethodSymbol symbol) return symbol;
    var candidates = info.CandidateSymbols.OfType<IMethodSymbol>().ToArray();
    return candidates.Length == 1 ? candidates[0] : null;
}

static IMethodSymbol? ResolveInterfaceImplementation(IMethodSymbol symbol, Compilation compilation, IReadOnlyCollection<string> knownClasses)
{
    if (symbol.ContainingType.TypeKind != TypeKind.Interface) return null;
    var implementations = ExtractorRuntime.CompilationIndex(compilation)
        .InterfaceImplementations(symbol)
        .Where(implementation => knownClasses.Contains(implementation.ContainingType.Name))
        .ToArray();
    return implementations.Length == 1 ? implementations[0] : null;
}

static string MatchKnownClass(string typeName, HashSet<string> knownClasses)
{
    if (string.IsNullOrWhiteSpace(typeName)) return string.Empty;
    var clean = typeName.Split('.').Last();
    if (knownClasses.Contains(clean)) return clean;
    if (clean.StartsWith("I", StringComparison.Ordinal) && clean.Length > 1 && char.IsUpper(clean[1]))
        return knownClasses.Contains(clean[1..]) ? clean[1..] : string.Empty;
    return string.Empty;
}

static string MatchVariableNameToKnownClass(string variableName, HashSet<string> knownClasses)
{
    var clean = variableName.TrimStart('_');
    if (string.IsNullOrWhiteSpace(clean)) return string.Empty;
    var pascal = char.ToUpperInvariant(clean[0]) + clean[1..];
    var exact = MatchKnownClass(pascal, knownClasses);
    if (!string.IsNullOrEmpty(exact)) return exact;
    if (clean.Equals("service", StringComparison.OrdinalIgnoreCase))
    {
        var candidates = knownClasses.Where(name => name.EndsWith("Service", StringComparison.Ordinal)).ToList();
        return candidates.Count == 1 ? candidates[0] : string.Empty;
    }
    if (clean.Equals("repository", StringComparison.OrdinalIgnoreCase))
    {
        var candidates = knownClasses.Where(name => name.EndsWith("Repository", StringComparison.Ordinal)).ToList();
        return candidates.Count == 1 ? candidates[0] : string.Empty;
    }
    return string.Empty;
}

static string InvocationName(InvocationExpressionSyntax invocation)
    => invocation.Expression switch
    {
        MemberAccessExpressionSyntax member => member.Name.Identifier.Text,
        MemberBindingExpressionSyntax memberBinding => memberBinding.Name.Identifier.Text,
        IdentifierNameSyntax identifier => identifier.Identifier.Text,
        GenericNameSyntax generic => generic.Identifier.Text,
        _ => invocation.Expression.ToString().Split('.').Last(),
    };

static string SourceClassForNode(SyntaxNode node)
    => node.AncestorsAndSelf().OfType<ClassDeclarationSyntax>().FirstOrDefault()?.Identifier.Text ?? string.Empty;

static string SourceMemberForNode(SyntaxNode node)
    => node.AncestorsAndSelf().OfType<MethodDeclarationSyntax>().FirstOrDefault()?.Identifier.Text
       ?? node.AncestorsAndSelf().OfType<ConstructorDeclarationSyntax>().FirstOrDefault()?.Identifier.Text
       ?? node.AncestorsAndSelf().OfType<PropertyDeclarationSyntax>().FirstOrDefault()?.Identifier.Text
       ?? string.Empty;

static MethodKey OwnerForOffset(SourceFile file, int offset)
{
    if (file.SyntaxTree is null) return new MethodKey(string.Empty, string.Empty);
    var token = file.SyntaxTree.GetRoot().FindToken(Math.Clamp(offset, 0, file.Text.Length));
    return OwnerForNode(file, token.Parent ?? file.SyntaxTree.GetRoot());
}

static MethodKey OwnerForNode(SourceFile file, SyntaxNode node)
{
    var cls = SourceClassForNode(node);
    var member = SourceMemberForNode(node);
    var method = node.AncestorsAndSelf().OfType<MethodDeclarationSyntax>().FirstOrDefault();
    if (!string.IsNullOrEmpty(cls) && !string.IsNullOrEmpty(member))
        return new MethodKey(cls, member, method is null ? string.Empty : DeclarationIdentity(method.SyntaxTree, method.SpanStart));
    if (node.AncestorsAndSelf().OfType<GlobalStatementSyntax>().Any()) return TopLevelKey(file);
    return new MethodKey(cls, member);
}

static MethodKey TopLevelKey(SourceFile file)
    => new("$TopLevel", ExtractorRuntime.RepositoryPath(file.RelativePath), ClassIdentity: $"top-level:{ExtractorRuntime.RepositoryPath(file.RelativePath)}");

static MethodKey InvocationKey(string className, string memberName, string identity, string classIdentity, IReadOnlyDictionary<MethodKey, MethodDeclarationInfo> methods)
    => CanonicalKey(new MethodKey(className, memberName, identity, classIdentity), methods);

static MethodKey CanonicalKey(MethodKey key, IReadOnlyDictionary<MethodKey, MethodDeclarationInfo> methods)
{
    if (string.IsNullOrEmpty(key.Identity)) return key;
    var matches = methods.Keys.Where(candidate => candidate.Identity.Equals(key.Identity, StringComparison.Ordinal)).Take(2).ToArray();
    return matches.Length == 1 ? matches[0] : key;
}

static string TypeIdentity(INamedTypeSymbol? type)
    => type?.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat) ?? string.Empty;

static string ScopedTypeIdentity(INamedTypeSymbol? type, SourceFile file, IReadOnlyCollection<ProjectInfo> projects, string root)
{
    var project = ProjectForFile(file, projects);
    var scope = project?.RelativePath ?? ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, file.AbsolutePath));
    return $"{scope}|{TypeIdentity(type)}";
}

static ProjectInfo? ProjectForFile(SourceFile file, IReadOnlyCollection<ProjectInfo> projects)
    => projects
        .Where(project => project.SourcePaths.Contains(file.AbsolutePath))
        .OrderByDescending(project => project.Directory.Length)
        .FirstOrDefault()
        ?? projects.Where(project => IsUnderDirectory(file.AbsolutePath, project.Directory)).OrderByDescending(project => project.Directory.Length).FirstOrDefault();

static string DeclarationIdentity(SyntaxTree tree, int start)
    => $"{tree.FilePath}:{start.ToString(System.Globalization.CultureInfo.InvariantCulture)}";

static string FirstFolderDatabase(JsonElement config)
{
    if (!config.TryGetProperty("folders", out var folders) || folders.ValueKind != JsonValueKind.Array) return string.Empty;
    foreach (var folder in folders.EnumerateArray())
        if (folder.ValueKind == JsonValueKind.Object && folder.TryGetProperty("database", out var database) && database.ValueKind == JsonValueKind.String)
            return database.GetString() ?? string.Empty;
    return string.Empty;
}

static IEnumerable<string> StringArray(JsonElement config, string name)
{
    if (!config.TryGetProperty(name, out var array) || array.ValueKind != JsonValueKind.Array) yield break;
    foreach (var item in array.EnumerateArray())
        if (item.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(item.GetString()))
            yield return item.GetString()!;
}

static List<ExecutableMapping> LoadExecutableMappings(string path)
{
    if (!File.Exists(path)) return new List<ExecutableMapping>();
    return Csv.Read(path).Select(row => new ExecutableMapping(
        row.GetValueOrDefault("job_system", string.Empty),
        row.GetValueOrDefault("executable_name", string.Empty),
        row.GetValueOrDefault("executable_scope", string.Empty),
        row.GetValueOrDefault("canonical_executable_name", string.Empty),
        row.GetValueOrDefault("alias", string.Empty))).ToList();
}

static string Capitalize(string value)
    => string.IsNullOrWhiteSpace(value) ? value : char.ToUpperInvariant(value[0]) + value[1..];

sealed record MethodKey(string ClassName, string MemberName, string Identity = "", string ClassIdentity = "");
sealed record MethodDeclarationInfo(MethodKey Key, SourceFile File, int Start, bool IsEntryPoint, SyntaxNode Syntax);
sealed record CommandHandlerInfo(string Mode, MethodKey Source, MethodKey Handler, SourceFile File, int Start, SyntaxNode Syntax);
sealed record FolderContext(string AbsolutePath, string Database);
sealed record ProjectInfo(string Id, string RelativePath, string Directory, string Database, string AssemblyName, string TargetName, string OutputType, bool IsExecutable, HashSet<string> SourcePaths, List<string> ProjectReferences);
sealed record ExecutableMapping(string JobSystem, string ExecutableName, string ExecutableScope, string CanonicalExecutableName, string Alias);

abstract record DataFinding(MethodKey Owner, SourceFile File, int Line, string Database);
sealed record DataEdgeFinding(MethodKey Owner, string TargetNodeId, string EdgeType, string RawOperation, SourceFile File, int Line, string EvidenceKind, string Snippet, string Database, string RawReference = "") : DataFinding(Owner, File, Line, Database);
sealed record ProcedureFinding(MethodKey Owner, string RawReference, string RawOperation, SourceFile File, int Line, string EvidenceKind, string Snippet, string Database) : DataFinding(Owner, File, Line, Database);
sealed record SequenceFinding(MethodKey Owner, string SequenceName, string Operation, SourceFile File, int Line, string Database) : DataFinding(Owner, File, Line, Database);
sealed record IssueFinding(MethodKey Owner, string IssueType, string Severity, string Message, string RawReference, SourceFile File, int Line, string Database, Dictionary<string, object>? Properties = null) : DataFinding(Owner, File, Line, Database);

sealed class ExecutableInfo
{
    public ExecutableInfo(string name, string scope, string repository, string database)
    {
        Name = BatchNames.NormalizeExecutableName(name);
        Scope = scope;
        Repository = repository;
        Database = database;
        Id = ExtractorRuntime.StableNodeId("executable", scope, Name);
        Key = Path.GetFileNameWithoutExtension(Name).ToLowerInvariant();
        AddName(Name);
    }

    public string Name { get; }
    public string Scope { get; }
    public string Repository { get; }
    public string Database { get; private set; }
    public string Id { get; }
    public string Key { get; private set; }
    public List<ProjectInfo> Projects { get; } = new();
    public List<SourceFile> LooseEntryFiles { get; } = new();
    public HashSet<string> Names { get; } = new(StringComparer.OrdinalIgnoreCase);
    public HashSet<string> AssemblyNames { get; } = new(StringComparer.OrdinalIgnoreCase);
    public HashSet<string> Aliases { get; } = new(StringComparer.OrdinalIgnoreCase);

    public void AddProject(ProjectInfo project, bool exposeExecutableNames = true)
    {
        if (Projects.Any(existing => existing.Id.Equals(project.Id, StringComparison.Ordinal))) return;
        Projects.Add(project);
        if (string.IsNullOrWhiteSpace(Database)) Database = project.Database;
        if (!exposeExecutableNames) return;
        AddName(project.AssemblyName);
        AddName(project.TargetName);
        AddAssembly(project.AssemblyName);
    }

    public void AddLooseEntryFile(SourceFile file)
    {
        if (!LooseEntryFiles.Any(existing => existing.AbsolutePath.Equals(file.AbsolutePath, StringComparison.OrdinalIgnoreCase)))
            LooseEntryFiles.Add(file);
    }

    public void AddName(string value)
    {
        var normalized = BatchNames.NormalizeExecutableName(value);
        if (!string.IsNullOrWhiteSpace(normalized)) Names.Add(normalized);
    }

    public void AddAssembly(string value)
    {
        var normalized = BatchNames.NormalizeAssemblyName(value);
        if (!string.IsNullOrWhiteSpace(normalized)) AssemblyNames.Add(normalized);
    }

    public void AddAlias(string value)
    {
        var normalized = BatchNames.NormalizeAlias(value);
        if (string.IsNullOrWhiteSpace(normalized)) return;
        Aliases.Add(normalized);
        Key = ExtractorRuntime.Slug(normalized);
    }

    public Dictionary<string, object> Properties()
        => new()
        {
            ["names"] = Names.OrderBy(item => item, StringComparer.OrdinalIgnoreCase).ToArray(),
            ["assemblyNames"] = AssemblyNames.OrderBy(item => item, StringComparer.OrdinalIgnoreCase).ToArray(),
            ["aliases"] = Aliases.OrderBy(item => item, StringComparer.OrdinalIgnoreCase).ToArray(),
            ["projects"] = Projects.Select(project => project.RelativePath).OrderBy(item => item, StringComparer.Ordinal).ToArray(),
        };
}

static class BatchNames
{
    public static string NormalizeExecutableName(string value)
    {
        var clean = FileName(value).ToLowerInvariant();
        if (string.IsNullOrWhiteSpace(clean)) return string.Empty;
        if (clean.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) return clean;
        if (clean.EndsWith(".dll", StringComparison.OrdinalIgnoreCase)) return $"{Path.GetFileNameWithoutExtension(clean)}.exe";
        return $"{clean}.exe";
    }

    public static string NormalizeAssemblyName(string value)
    {
        var clean = FileName(value).ToLowerInvariant();
        if (clean.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) || clean.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
            return Path.GetFileNameWithoutExtension(clean).ToLowerInvariant();
        return clean;
    }

    static string FileName(string value)
        => (value ?? string.Empty).Trim().Trim('"', '\'').Replace('\\', '/').Split('/').LastOrDefault() ?? string.Empty;

    public static string NormalizeAlias(string value) => (value ?? string.Empty).Trim().ToLowerInvariant();
}

static class Args
{
    public static string? Value(string[] args, string name)
    {
        var index = Array.IndexOf(args, name);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }
}
