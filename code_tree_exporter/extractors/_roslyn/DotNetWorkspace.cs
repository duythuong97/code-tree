using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Build.Locator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.MSBuild;

namespace CodeMap.Extractors;

public sealed record WorkspaceProjectInfo(
    string FilePath,
    string Name,
    IReadOnlyList<string> DocumentPaths,
    IReadOnlyList<string> ProjectReferencePaths);

public sealed class DotNetWorkspaceSnapshot
{
    public required List<SourceFile> Files { get; init; }
    public required Dictionary<SyntaxTree, CSharpCompilation> CompilationByTree { get; init; }
    public required List<WorkspaceProjectInfo> Projects { get; init; }
    public required List<string> Diagnostics { get; init; }
    public int LoadedProjectCount { get; init; }
    public int FallbackFileCount { get; init; }
}

public static class DotNetWorkspaceLoader
{
    static readonly object RegistrationGate = new();

    public static async Task<DotNetWorkspaceSnapshot> LoadAsync(JsonElement config, string root)
    {
        RegisterMSBuild();
        var configuredPaths = ExtractorRuntime.ConfiguredPaths(
            config,
            new[] { ".cs", ".csproj", ".sln" });
        var configuredCs = configuredPaths
            .Where(path => Path.GetExtension(path).Equals(".cs", StringComparison.OrdinalIgnoreCase))
            .ToHashSet(ExtractorRuntime.PathComparer);
        var projectPaths = configuredPaths
            .Where(path => Path.GetExtension(path).Equals(".csproj", StringComparison.OrdinalIgnoreCase))
            .ToHashSet(ExtractorRuntime.PathComparer);
        var solutionPaths = configuredPaths
            .Where(path => Path.GetExtension(path).Equals(".sln", StringComparison.OrdinalIgnoreCase))
            .OrderBy(path => path, ExtractorRuntime.PathComparer)
            .ToList();
        foreach (var solution in solutionPaths)
            foreach (var projectPath in SolutionProjectPaths(solution))
                if (File.Exists(projectPath)
                    && ExtractorRuntime.IsWithin(projectPath, root)
                    && !ExtractorRuntime.IsExcludedSourcePath(projectPath, root))
                    projectPaths.Add(projectPath);

        var diagnostics = new List<string>();
        var projectTimeoutSeconds = ProjectTimeoutSeconds(config);
        var solutionDirectory = solutionPaths.Count == 1
            ? Path.GetDirectoryName(solutionPaths[0]) ?? root
            : root;
        var configuredOutput = ExtractorRuntime.ConfigPath(config, "output", root);
        var workspaceOutput = Path.Combine(
            Path.GetDirectoryName(configuredOutput) ?? root,
            ".msbuild",
            Path.GetFileName(configuredOutput));
        Directory.CreateDirectory(workspaceOutput);
        var outputDirectory = EnsureTrailingSeparator(workspaceOutput);
        using var workspace = MSBuildWorkspace.Create(new Dictionary<string, string>
        {
            ["Configuration"] = "Release",
            ["DesignTimeBuild"] = "true",
            ["BuildingInsideVisualStudio"] = "true",
            ["BuildProjectReferences"] = "false",
            ["SkipCompilerExecution"] = "true",
            ["SolutionDir"] = EnsureTrailingSeparator(solutionDirectory),
            ["OutputPath"] = outputDirectory,
            ["OutDir"] = outputDirectory,
        });
        workspace.SkipUnrecognizedProjects = true;
        workspace.WorkspaceFailed += (_, eventArgs) =>
        {
            lock (diagnostics)
                diagnostics.Add($"{eventArgs.Diagnostic.Kind}: {eventArgs.Diagnostic.Message}");
        };

        var canOpenConfiguredSolution = solutionPaths.Count == 1
            && SolutionProjectPaths(solutionPaths[0]).All(projectPath =>
                File.Exists(projectPath)
                && ExtractorRuntime.IsWithin(projectPath, root)
                && !ExtractorRuntime.IsExcludedSourcePath(projectPath, root));
        if (canOpenConfiguredSolution)
            await TryOpenSolutionAsync(
                workspace, solutionPaths[0], projectTimeoutSeconds, diagnostics);

        foreach (var projectPath in RootProjectPaths(projectPaths))
        {
            if (WorkspaceContainsProject(workspace, projectPath)) continue;
            await TryOpenProjectAsync(
                workspace, projectPath, projectTimeoutSeconds, diagnostics);
        }

        // Structural project detection can miss conditional references. Open any
        // configured project that MSBuild did not already pull into the solution.
        foreach (var projectPath in projectPaths.OrderBy(path => path, ExtractorRuntime.PathComparer))
        {
            if (WorkspaceContainsProject(workspace, projectPath)) continue;
            await TryOpenProjectAsync(
                workspace, projectPath, projectTimeoutSeconds, diagnostics);
        }

        var filesByPath = new Dictionary<string, SourceFile>(ExtractorRuntime.PathComparer);
        var compilationByTree = new Dictionary<SyntaxTree, CSharpCompilation>();
        var projectInfos = new List<WorkspaceProjectInfo>();
        var loadedProjects = workspace.CurrentSolution.Projects
            .Where(project => project.Language == LanguageNames.CSharp
                && !string.IsNullOrWhiteSpace(project.FilePath)
                && ExtractorRuntime.IsWithin(project.FilePath!, root)
                && !ExtractorRuntime.IsExcludedSourcePath(project.FilePath!, root))
            .OrderBy(project => project.FilePath, ExtractorRuntime.PathComparer)
            .ToList();

        foreach (var project in loadedProjects)
        {
            var projectPath = project.FilePath!;
            if (!filesByPath.ContainsKey(projectPath) && File.Exists(projectPath))
            {
                try
                {
                    filesByPath[projectPath] = new SourceFile(
                        projectPath,
                        ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, projectPath)),
                        File.ReadAllText(projectPath, Encoding.UTF8),
                        null);
                }
                catch (Exception exception)
                {
                    diagnostics.Add($"Failed to read project file {projectPath}: {exception.Message}");
                }
            }
            var compilation = await project.GetCompilationAsync() as CSharpCompilation;
            if (compilation is null)
            {
                diagnostics.Add($"Compilation unavailable for {project.FilePath}");
                continue;
            }
            var documentPaths = new List<string>();
            foreach (var document in project.Documents.OrderBy(document => document.FilePath, ExtractorRuntime.PathComparer))
            {
                var path = document.FilePath;
                if (string.IsNullOrWhiteSpace(path)
                    || !Path.GetExtension(path).Equals(".cs", StringComparison.OrdinalIgnoreCase)
                    || !File.Exists(path)
                    || !ExtractorRuntime.IsWithin(path, root)
                    || ExtractorRuntime.IsExcludedSourcePath(path, root)
                    || (configuredCs.Count > 0 && !configuredCs.Contains(path)))
                    continue;
                documentPaths.Add(path);
                if (filesByPath.ContainsKey(path)) continue;
                var syntaxTree = await document.GetSyntaxTreeAsync();
                if (syntaxTree is null) continue;
                var compilationTree = compilation.SyntaxTrees.FirstOrDefault(
                    tree => ExtractorRuntime.PathComparer.Equals(tree.FilePath, path))
                    ?? syntaxTree;
                var text = (await document.GetTextAsync()).ToString();
                var sourceFile = new SourceFile(
                    path,
                    ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, path)),
                    text,
                    compilationTree);
                filesByPath[path] = sourceFile;
                compilationByTree[compilationTree] = compilation;
            }

            var references = project.ProjectReferences
                .Select(reference => workspace.CurrentSolution.GetProject(reference.ProjectId)?.FilePath)
                .Where(path => !string.IsNullOrWhiteSpace(path))
                .Cast<string>()
                .OrderBy(path => path, ExtractorRuntime.PathComparer)
                .ToList();
            projectInfos.Add(new WorkspaceProjectInfo(
                project.FilePath!,
                project.Name,
                documentPaths,
                references));
        }

        var looseFiles = new List<SourceFile>();
        foreach (var path in configuredCs.OrderBy(path => path, ExtractorRuntime.PathComparer))
        {
            if (filesByPath.ContainsKey(path)) continue;
            try
            {
                var text = File.ReadAllText(path, Encoding.UTF8);
                var tree = CSharpSyntaxTree.ParseText(text, path: path);
                var sourceFile = new SourceFile(
                    path,
                    ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, path)),
                    text,
                    tree);
                filesByPath[path] = sourceFile;
                looseFiles.Add(sourceFile);
            }
            catch (Exception exception)
            {
                diagnostics.Add($"Failed to parse loose source {path}: {exception.Message}");
            }
        }
        if (looseFiles.Count > 0)
        {
            var fallbackCompilation = ExtractorRuntime.CreateCompilation(looseFiles);
            foreach (var file in looseFiles)
                compilationByTree[file.SyntaxTree!] = fallbackCompilation;
        }

        foreach (var path in configuredPaths.Where(path => !Path.GetExtension(path).Equals(".cs", StringComparison.OrdinalIgnoreCase)))
        {
            if (filesByPath.ContainsKey(path)) continue;
            try
            {
                filesByPath[path] = new SourceFile(
                    path,
                    ExtractorRuntime.RepositoryPath(Path.GetRelativePath(root, path)),
                    File.ReadAllText(path, Encoding.UTF8),
                    null);
            }
            catch (Exception exception)
            {
                diagnostics.Add($"Failed to read workspace file {path}: {exception.Message}");
            }
        }

        return new DotNetWorkspaceSnapshot
        {
            Files = filesByPath.Values.OrderBy(file => file.AbsolutePath, ExtractorRuntime.PathComparer).ToList(),
            CompilationByTree = compilationByTree,
            Projects = projectInfos,
            Diagnostics = diagnostics.Distinct(StringComparer.Ordinal).ToList(),
            LoadedProjectCount = projectInfos.Count,
            FallbackFileCount = looseFiles.Count,
        };
    }

    static void RegisterMSBuild()
    {
        lock (RegistrationGate)
        {
            if (!MSBuildLocator.IsRegistered)
                MSBuildLocator.RegisterDefaults();
        }
    }

    static async Task TryOpenSolutionAsync(
        MSBuildWorkspace workspace,
        string path,
        int timeoutSeconds,
        List<string> diagnostics)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSeconds));
        try
        {
            await workspace.OpenSolutionAsync(path, cancellationToken: timeout.Token);
        }
        catch (OperationCanceledException)
        {
            diagnostics.Add($"Timed out loading {path} after {timeoutSeconds} seconds");
        }
        catch (Exception exception)
        {
            diagnostics.Add($"Failed to load {path}: {exception.Message}");
        }
    }

    static async Task TryOpenProjectAsync(
        MSBuildWorkspace workspace,
        string path,
        int timeoutSeconds,
        List<string> diagnostics)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSeconds));
        try
        {
            await workspace.OpenProjectAsync(path, cancellationToken: timeout.Token);
        }
        catch (OperationCanceledException)
        {
            diagnostics.Add($"Timed out loading {path} after {timeoutSeconds} seconds");
        }
        catch (Exception exception)
        {
            diagnostics.Add($"Failed to load {path}: {exception.Message}");
        }
    }

    static int ProjectTimeoutSeconds(JsonElement config)
    {
        if (config.TryGetProperty("limits", out var limits)
            && limits.ValueKind == JsonValueKind.Object
            && limits.TryGetProperty("projectTimeoutSeconds", out var value)
            && value.TryGetInt32(out var configured)
            && configured > 0)
            return configured;
        return 900;
    }

    static string EnsureTrailingSeparator(string path)
        => Path.EndsInDirectorySeparator(path) ? path : path + Path.DirectorySeparatorChar;

    static bool WorkspaceContainsProject(MSBuildWorkspace workspace, string projectPath)
        => workspace.CurrentSolution.Projects.Any(project =>
            !string.IsNullOrWhiteSpace(project.FilePath)
            && ExtractorRuntime.PathComparer.Equals(project.FilePath, projectPath));

    static IEnumerable<string> RootProjectPaths(IReadOnlyCollection<string> projectPaths)
    {
        var referenced = new HashSet<string>(ExtractorRuntime.PathComparer);
        foreach (var projectPath in projectPaths)
        {
            string text;
            try { text = File.ReadAllText(projectPath, Encoding.UTF8); }
            catch { continue; }
            var directory = Path.GetDirectoryName(projectPath) ?? Directory.GetCurrentDirectory();
            foreach (var include in MsBuildProjectXml.Includes(text, "ProjectReference"))
            {
                var reference = Path.GetFullPath(Path.Combine(directory, ExtractorRuntime.NativePath(include)));
                if (projectPaths.Contains(reference, ExtractorRuntime.PathComparer))
                    referenced.Add(reference);
            }
        }
        var roots = projectPaths
            .Where(path => !referenced.Contains(path))
            .OrderBy(path => path, ExtractorRuntime.PathComparer)
            .ToList();
        return roots.Count > 0 ? roots : projectPaths.OrderBy(path => path, ExtractorRuntime.PathComparer);
    }

    static IEnumerable<string> SolutionProjectPaths(string solutionPath)
    {
        var directory = Path.GetDirectoryName(solutionPath) ?? Directory.GetCurrentDirectory();
        string text;
        try { text = File.ReadAllText(solutionPath, Encoding.UTF8); }
        catch { yield break; }
        foreach (Match match in Regex.Matches(
            text,
            "Project\\([^)]*\\)\\s*=\\s*\"[^\"\\r\\n]+\"\\s*,\\s*\"(?<path>[^\"\\r\\n]+\\.csproj)\"",
            RegexOptions.IgnoreCase))
        {
            yield return Path.GetFullPath(Path.Combine(
                directory,
                ExtractorRuntime.NativePath(match.Groups["path"].Value)));
        }
    }
}
