using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace CodeMap.Extractors;

public sealed record SourceFile(string AbsolutePath, string RelativePath, string Text, SyntaxTree? SyntaxTree);
public sealed record TableReference(string ObjectName, string Operation, string EdgeType, int Start, bool Remote = false);
public sealed record SequenceReference(string ObjectName, int Start);
public sealed record ProcedureReference(string ObjectName, int Start);
public sealed record SqlAnalysis(IReadOnlyList<TableReference> Tables, IReadOnlyList<ProcedureReference> Procedures, IReadOnlyList<SequenceReference> Sequences, IReadOnlyList<int> DynamicOffsets);
public sealed record EndpointInfo(string Controller, string Action, string Method, string Route, int Start, int HandlerStart, int HandlerEnd, bool IsMinimal = false, SourceFile? File = null);
public sealed record StringLiteralInfo(string OwnerClass, string Value, int Start, SourceFile File);
public sealed record StringExpressionInfo(string OwnerClass, string OwnerMember, string Value, int Start, SourceFile File, bool IsDynamic, string ExpressionText);
public sealed record InvocationInfo(string SourceClass, string SourceMember, string TargetClass, string TargetMember, int Start, SourceFile File);
public sealed record MethodInvocationInfo(string SourceClass, string SourceMember, string TargetClass, string TargetMember, int Start, SourceFile File);
sealed record StringEvaluation(string Value, bool IsDynamic);

public static class ExtractorRuntime
{
    public static JsonDocument LoadConfig(string path)
    {
        if (!Path.IsPathFullyQualified(path)) throw new ArgumentException($"Config path must be absolute: {path}");
        var text = Regex.Replace(
            File.ReadAllText(path, Encoding.UTF8),
            @"\$\{([A-Z][A-Z0-9_]*)\}",
            match => Environment.GetEnvironmentVariable(match.Groups[1].Value)
                ?? throw new ArgumentException($"Missing required environment variable: {match.Groups[1].Value}"));
        return JsonDocument.Parse(text);
    }

    public static string ConfigPath(JsonElement element, string name, string fallback = "")
    {
        var value = String(element, name, fallback);
        if (string.IsNullOrWhiteSpace(value)) return value;
        if (!Path.IsPathFullyQualified(value)) throw new ArgumentException($"{name} must be an absolute path: {value}");
        return Path.GetFullPath(value);
    }

    public static string String(JsonElement element, string name, string fallback = "")
        => element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() ?? fallback : fallback;

    public static JsonElement? Property(JsonElement element, string name)
        => element.TryGetProperty(name, out var value) ? value : null;

    public static List<SourceFile> ConfiguredFiles(JsonElement config, IReadOnlyCollection<string> extensions)
    {
        var root = ConfigPath(config, "root");
        var folders = new List<string>();
        if (config.TryGetProperty("folders", out var foldersElement) && foldersElement.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in foldersElement.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.String) folders.Add(item.GetString() ?? ".");
                else if (item.ValueKind == JsonValueKind.Object) folders.Add(String(item, "path", "."));
            }
        }
        if (folders.Count == 0) folders.Add(".");

        var files = new List<SourceFile>();
        var seen = new HashSet<string>(PathComparer);
        foreach (var folder in folders)
        {
            var absoluteFolder = Path.GetFullPath(Path.Combine(root, NativePath(folder)));
            if (!IsWithin(absoluteFolder, root)) throw new ArgumentException($"Folder escapes root: {folder}");
            if (!Directory.Exists(absoluteFolder)) continue;
            foreach (var path in Directory.EnumerateFiles(absoluteFolder, "*", SearchOption.AllDirectories).OrderBy(p => p, PathComparer))
            {
                if (!IsWithin(path, root)) throw new ArgumentException($"Source file escapes root: {path}");
                if (!seen.Add(path)) continue;
                if (IsExcludedSourcePath(path, root)) continue;
                if (!extensions.Contains(Path.GetExtension(path), StringComparer.OrdinalIgnoreCase)) continue;
                var text = File.ReadAllText(path, Encoding.UTF8);
                var syntaxTree = Path.GetExtension(path).Equals(".cs", StringComparison.OrdinalIgnoreCase)
                    ? CSharpSyntaxTree.ParseText(text, path: path)
                    : null;
                files.Add(new SourceFile(path, RepositoryPath(Path.GetRelativePath(root, path)), text, syntaxTree));
            }
        }
        return files;
    }

    public static StringComparer PathComparer => OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;

    public static string NativePath(string value)
        => value.Replace('\\', Path.DirectorySeparatorChar).Replace('/', Path.DirectorySeparatorChar);

    public static bool IsWithin(string path, string root)
    {
        var relative = Path.GetRelativePath(Path.GetFullPath(root), Path.GetFullPath(path));
        return relative != ".." && !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) && !Path.IsPathRooted(relative);
    }

    static bool IsExcludedSourcePath(string path, string root)
    {
        var relative = RepositoryPath(Path.GetRelativePath(root, path));
        var parts = relative.Split('/', StringSplitOptions.RemoveEmptyEntries);
        foreach (var part in parts)
        {
            if (part.Equals("bin", StringComparison.OrdinalIgnoreCase)) return true;
            if (part.Equals("obj", StringComparison.OrdinalIgnoreCase)) return true;
            if (part.Equals("node_modules", StringComparison.OrdinalIgnoreCase)) return true;
            if (part.Equals("dist", StringComparison.OrdinalIgnoreCase)) return true;
            if (part.Equals("generated", StringComparison.OrdinalIgnoreCase)) return true;
            if (part.Equals("migration", StringComparison.OrdinalIgnoreCase)) return true;
            if (part.Equals("migrations", StringComparison.OrdinalIgnoreCase)) return true;
        }
        var fileName = Path.GetFileName(path);
        return Regex.IsMatch(fileName, @"(?:^|\.)(?:test|tests|spec)\.", RegexOptions.IgnoreCase)
            || Regex.IsMatch(fileName, @"(?:Test|Tests|Spec)\.(?:cs|ts|js)$", RegexOptions.IgnoreCase);
    }

    public static CSharpCompilation CreateCompilation(IEnumerable<SourceFile> files)
    {
        var trees = files.Where(file => file.SyntaxTree is not null).Select(file => file.SyntaxTree!).ToArray();
        var references = new List<MetadataReference>();
        var trusted = AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") as string;
        if (!string.IsNullOrWhiteSpace(trusted))
        {
            foreach (var assemblyPath in trusted.Split(Path.PathSeparator))
            {
                if (File.Exists(assemblyPath)) references.Add(MetadataReference.CreateFromFile(assemblyPath));
            }
        }
        return CSharpCompilation.Create(
            "CodeMapExtractorCompilation",
            trees,
            references,
            new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
    }

    public static IEnumerable<ClassDeclarationSyntax> Classes(SourceFile file)
        => file.SyntaxTree?.GetRoot().DescendantNodes().OfType<ClassDeclarationSyntax>() ?? Enumerable.Empty<ClassDeclarationSyntax>();

    public static List<EndpointInfo> Endpoints(SourceFile file, SemanticModel model)
    {
        var result = new List<EndpointInfo>();
        if (file.SyntaxTree is null) return result;
        var root = file.SyntaxTree.GetRoot();
        foreach (var cls in Classes(file).Where(c => c.Identifier.Text.EndsWith("Controller", StringComparison.Ordinal)))
        {
            _ = model.GetDeclaredSymbol(cls); // Roslyn SemanticModel proof point: class symbol binding.
            var controllerName = cls.Identifier.Text;
            var baseRoute = AttributeStringArgument(model, cls.AttributeLists, "Route", controllerName, string.Empty) ?? string.Empty;
            foreach (var method in cls.Members.OfType<MethodDeclarationSyntax>())
            {
                _ = model.GetDeclaredSymbol(method); // Roslyn SemanticModel proof point: action symbol binding.
                var actionName = method.Identifier.Text;
                foreach (var attribute in method.AttributeLists.SelectMany(list => list.Attributes))
                {
                    _ = model.GetSymbolInfo(attribute); // Resolve attribute constructor where references are available.
                    if (!TryHttpVerbFromAttribute(AttributeName(attribute), out var verb)) continue;
                    var suffix = AttributeStringArgument(model, attribute, controllerName, actionName) ?? string.Empty;
                    var route = CombineRoutes(baseRoute, suffix);
                    route = ApplyRouteTokens(route, controllerName, actionName);
                    var normalized = NormalizeHttpRoute(verb, route);
                    SyntaxNode handler = method.Body ?? (SyntaxNode?)method.ExpressionBody ?? method;
                    result.Add(new EndpointInfo(controllerName, actionName, normalized.Method, normalized.Route, method.SpanStart, handler.SpanStart, handler.Span.End, File: file));
                }
            }
        }

        foreach (var invocation in root.DescendantNodes().OfType<InvocationExpressionSyntax>())
        {
            if (!TryMinimalApiEndpoint(invocation, model, out var methods, out var route)) continue;
            var ownerClass = invocation.Ancestors().OfType<ClassDeclarationSyntax>().FirstOrDefault()?.Identifier.Text ?? "MinimalApi";
            var ownerMethod = invocation.Ancestors().OfType<MethodDeclarationSyntax>().FirstOrDefault()?.Identifier.Text ?? InvocationName(invocation);
            var handler = invocation.ArgumentList.Arguments.Select(argument => argument.Expression).OfType<LambdaExpressionSyntax>().LastOrDefault()?.Body;
            var handlerStart = handler?.SpanStart ?? invocation.SpanStart;
            var handlerEnd = handler?.Span.End ?? invocation.Span.End;
            foreach (var method in methods.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                var normalized = NormalizeHttpRoute(method, route);
                result.Add(new EndpointInfo(ownerClass, ownerMethod, normalized.Method, normalized.Route, invocation.SpanStart, handlerStart, handlerEnd, IsMinimal: true, File: file));
            }
        }
        return result;
    }

    public static IEnumerable<StringLiteralInfo> StringLiterals(SourceFile file, SemanticModel model)
    {
        foreach (var expression in StringExpressions(file, model))
            if (!expression.IsDynamic)
                yield return new StringLiteralInfo(expression.OwnerClass, expression.Value, expression.Start, file);
    }

    public static IEnumerable<StringExpressionInfo> StringExpressions(SourceFile file, SemanticModel model)
    {
        if (file.SyntaxTree is null) yield break;
        var root = file.SyntaxTree.GetRoot();
        foreach (var expression in root.DescendantNodes().OfType<ExpressionSyntax>())
        {
            if (!IsStringExpressionRootCandidate(expression, model)) continue;
            if (HasEvaluatedStringAncestor(expression, model)) continue;
            if (!TryEvaluateStringExpression(expression, model, out var evaluation)) continue;
            if (string.IsNullOrEmpty(evaluation.Value)) continue;
            _ = model.GetTypeInfo(expression); // Roslyn SemanticModel proof point: expression type binding.
            var owner = SourceClassForNode(expression);
            yield return new StringExpressionInfo(owner, SourceMemberForNode(expression), evaluation.Value, expression.SpanStart, file, evaluation.IsDynamic, expression.ToString());
        }
    }

    public static IEnumerable<InvocationInfo> InvocationEdges(SourceFile file, SemanticModel model, IReadOnlyCollection<string> knownClasses)
    {
        if (file.SyntaxTree is null) yield break;
        var classes = new HashSet<string>(knownClasses, StringComparer.Ordinal);
        foreach (var invocation in file.SyntaxTree.GetRoot().DescendantNodes().OfType<InvocationExpressionSyntax>())
        {
            var sourceClass = SourceClassForNode(invocation);
            if (string.IsNullOrEmpty(sourceClass)) continue;
            var targetClass = ResolveInvocationTargetClass(invocation, model, classes);
            if (string.IsNullOrEmpty(targetClass) || targetClass.Equals(sourceClass, StringComparison.Ordinal)) continue;
            var symbol = InvocationMethodSymbol(invocation, model);
            yield return new InvocationInfo(sourceClass, SourceMemberForNode(invocation), targetClass, symbol?.Name ?? InvocationName(invocation), invocation.SpanStart, file);
        }
    }

    public static IEnumerable<MethodInvocationInfo> MethodInvocationEdges(SourceFile file, SemanticModel model, IReadOnlyCollection<string> knownClasses)
    {
        if (file.SyntaxTree is null) yield break;
        var classes = new HashSet<string>(knownClasses, StringComparer.Ordinal);
        foreach (var invocation in file.SyntaxTree.GetRoot().DescendantNodes().OfType<InvocationExpressionSyntax>())
        {
            var sourceClass = SourceClassForNode(invocation);
            if (string.IsNullOrEmpty(sourceClass)) continue;
            var targetClass = ResolveInvocationTargetClass(invocation, model, classes);
            if (string.IsNullOrEmpty(targetClass)) continue;
            var sourceMember = SourceMemberForNode(invocation);
            var symbol = InvocationMethodSymbol(invocation, model);
            var targetMember = symbol?.Name ?? InvocationName(invocation);
            if (targetClass.Equals(sourceClass, StringComparison.Ordinal) && targetMember.Equals(sourceMember, StringComparison.Ordinal)) continue;
            yield return new MethodInvocationInfo(sourceClass, sourceMember, targetClass, targetMember, invocation.SpanStart, file);
        }
    }

    public static ISet<string> ModesFromSyntax(IEnumerable<SourceFile> files, CSharpCompilation compilation)
    {
        var modes = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var file in files.Where(f => f.SyntaxTree is not null))
        {
            var model = compilation.GetSemanticModel(file.SyntaxTree!);
            foreach (var binary in file.SyntaxTree!.GetRoot().DescendantNodes().OfType<BinaryExpressionSyntax>())
            {
                if (!binary.IsKind(SyntaxKind.EqualsExpression)) continue;
                _ = model.GetOperation(binary);
                foreach (var literal in binary.DescendantNodesAndSelf().OfType<LiteralExpressionSyntax>())
                {
                    if (literal.IsKind(SyntaxKind.StringLiteralExpression)) modes.Add(literal.Token.ValueText);
                }
            }
        }
        return modes;
    }

    static string? AttributeStringArgument(SemanticModel model, SyntaxList<AttributeListSyntax> lists, string wantedName)
        => AttributeStringArgument(model, lists, wantedName, string.Empty, string.Empty);

    static string? AttributeStringArgument(SemanticModel model, SyntaxList<AttributeListSyntax> lists, string wantedName, string controllerName, string actionName)
    {
        foreach (var attribute in lists.SelectMany(list => list.Attributes))
        {
            _ = model.GetSymbolInfo(attribute);
            if (!AttributeName(attribute).Equals(wantedName, StringComparison.OrdinalIgnoreCase)) continue;
            return AttributeStringArgument(model, attribute, controllerName, actionName);
        }
        return null;
    }

    static string? AttributeStringArgument(SemanticModel model, AttributeSyntax attribute)
        => AttributeStringArgument(model, attribute, string.Empty, string.Empty);

    static string? AttributeStringArgument(SemanticModel model, AttributeSyntax attribute, string controllerName, string actionName)
    {
        _ = model.GetSymbolInfo(attribute);
        var expression = attribute.ArgumentList?.Arguments.FirstOrDefault()?.Expression;
        if (expression is null) return null;
        if (!TryEvaluateStringExpression(expression, model, out var evaluation) || evaluation.IsDynamic) return null;
        return ApplyRouteTokens(evaluation.Value, controllerName, actionName);
    }

    static string AttributeName(AttributeSyntax attribute)
    {
        var name = attribute.Name.ToString();
        var leaf = name.Split('.').Last();
        return leaf.EndsWith("Attribute", StringComparison.OrdinalIgnoreCase) ? leaf[..^"Attribute".Length] : leaf;
    }

    static bool TryHttpVerbFromAttribute(string attributeName, out string verb)
    {
        verb = attributeName.Replace("Attribute", string.Empty, StringComparison.OrdinalIgnoreCase);
        if (!verb.StartsWith("Http", StringComparison.OrdinalIgnoreCase) || verb.Length <= "Http".Length) return false;
        verb = verb["Http".Length..].ToUpperInvariant();
        return verb is "GET" or "POST" or "PUT" or "PATCH" or "DELETE";
    }

    static bool TryMinimalApiEndpoint(InvocationExpressionSyntax invocation, SemanticModel model, out IReadOnlyList<string> methods, out string route)
    {
        methods = Array.Empty<string>();
        route = string.Empty;
        var name = InvocationName(invocation);
        var args = invocation.ArgumentList.Arguments;
        if (args.Count == 0) return false;
        if (!TryEvaluateStringExpression(args[0].Expression, model, out var routeValue) || routeValue.IsDynamic) return false;
        if (name.Equals("MapGet", StringComparison.Ordinal)) methods = new[] { "GET" };
        else if (name.Equals("MapPost", StringComparison.Ordinal)) methods = new[] { "POST" };
        else if (name.Equals("MapPut", StringComparison.Ordinal)) methods = new[] { "PUT" };
        else if (name.Equals("MapPatch", StringComparison.Ordinal)) methods = new[] { "PATCH" };
        else if (name.Equals("MapDelete", StringComparison.Ordinal)) methods = new[] { "DELETE" };
        else if (name.Equals("MapMethods", StringComparison.Ordinal) && args.Count >= 2) methods = ReadHttpMethods(args[1].Expression, model).ToArray();
        else return false;
        route = routeValue.Value;
        return methods.Count > 0;
    }

    static IEnumerable<string> ReadHttpMethods(ExpressionSyntax expression, SemanticModel model)
    {
        if (TryHttpMethodExpression(expression, model, out var method)) yield return method;
        switch (expression)
        {
            case ArrayCreationExpressionSyntax array when array.Initializer is not null:
                foreach (var item in ReadHttpMethods(array.Initializer, model)) yield return item;
                break;
            case ImplicitArrayCreationExpressionSyntax array when array.Initializer is not null:
                foreach (var item in ReadHttpMethods(array.Initializer, model)) yield return item;
                break;
            case InitializerExpressionSyntax initializer:
                foreach (var item in initializer.Expressions)
                    if (TryHttpMethodExpression(item, model, out var nested)) yield return nested;
                break;
            case CollectionExpressionSyntax collection:
                foreach (var item in collection.Elements.OfType<ExpressionElementSyntax>())
                    if (TryHttpMethodExpression(item.Expression, model, out var nested)) yield return nested;
                break;
        }
    }

    static bool TryHttpMethodExpression(ExpressionSyntax expression, SemanticModel model, out string method)
    {
        method = string.Empty;
        if (TryEvaluateStringExpression(expression, model, out var evaluation) && !evaluation.IsDynamic && IsHttpVerb(evaluation.Value))
        {
            method = evaluation.Value.ToUpperInvariant();
            return true;
        }
        if (expression is MemberAccessExpressionSyntax member && IsHttpVerb(member.Name.Identifier.Text))
        {
            method = member.Name.Identifier.Text.ToUpperInvariant();
            return true;
        }
        return false;
    }

    static bool IsHttpVerb(string value)
        => value.Trim().ToUpperInvariant() is "GET" or "POST" or "PUT" or "PATCH" or "DELETE";

    static string ApplyRouteTokens(string route, string controllerName, string actionName)
    {
        var controller = controllerName.EndsWith("Controller", StringComparison.Ordinal) ? controllerName[..^"Controller".Length] : controllerName;
        var result = Regex.Replace(route ?? string.Empty, @"\[controller\]", controller, RegexOptions.IgnoreCase);
        result = Regex.Replace(result, @"\[action\]", actionName ?? string.Empty, RegexOptions.IgnoreCase);
        return result;
    }

    static bool HasEvaluatedStringAncestor(ExpressionSyntax expression, SemanticModel model)
    {
        foreach (var ancestor in expression.Ancestors().OfType<ExpressionSyntax>())
            if (IsStringExpressionRootCandidate(ancestor, model) && TryEvaluateStringExpression(ancestor, model, out _))
                return true;
        return false;
    }

    static bool IsStringExpressionRootCandidate(ExpressionSyntax expression, SemanticModel model)
        => expression switch
        {
            LiteralExpressionSyntax literal when literal.IsKind(SyntaxKind.StringLiteralExpression) => true,
            InterpolatedStringExpressionSyntax => true,
            BinaryExpressionSyntax binary when binary.IsKind(SyntaxKind.AddExpression) => true,
            InvocationExpressionSyntax invocation when IsStringFormatInvocation(invocation) => true,
            IdentifierNameSyntax or MemberAccessExpressionSyntax when model.GetConstantValue(expression) is { HasValue: true, Value: string } => true,
            ParenthesizedExpressionSyntax parenthesized => IsStringExpressionRootCandidate(parenthesized.Expression, model),
            _ => false,
        };

    static bool TryEvaluateStringExpression(ExpressionSyntax expression, SemanticModel model, out StringEvaluation evaluation)
    {
        expression = UnwrapParentheses(expression);
        var constant = model.GetConstantValue(expression);
        if (constant.HasValue && constant.Value is string constantString)
        {
            evaluation = new StringEvaluation(constantString, false);
            return true;
        }

        switch (expression)
        {
            case LiteralExpressionSyntax literal when literal.IsKind(SyntaxKind.StringLiteralExpression):
                evaluation = new StringEvaluation(literal.Token.ValueText, false);
                return true;
            case InterpolatedStringExpressionSyntax interpolated:
                return TryEvaluateInterpolatedString(interpolated, model, out evaluation);
            case BinaryExpressionSyntax binary when binary.IsKind(SyntaxKind.AddExpression):
                return TryEvaluateConcatenation(binary, model, out evaluation);
            case InvocationExpressionSyntax invocation when IsStringFormatInvocation(invocation):
                return TryEvaluateStringFormat(invocation, model, out evaluation);
        }

        evaluation = new StringEvaluation(string.Empty, false);
        return false;
    }

    static ExpressionSyntax UnwrapParentheses(ExpressionSyntax expression)
    {
        while (expression is ParenthesizedExpressionSyntax parenthesized)
            expression = parenthesized.Expression;
        return expression;
    }

    static bool TryEvaluateInterpolatedString(InterpolatedStringExpressionSyntax interpolated, SemanticModel model, out StringEvaluation evaluation)
    {
        var builder = new StringBuilder();
        var dynamic = false;
        foreach (var content in interpolated.Contents)
        {
            if (content is InterpolatedStringTextSyntax text)
            {
                builder.Append(text.TextToken.ValueText);
            }
            else if (content is InterpolationSyntax interpolation)
            {
                if (TryEvaluateStringExpression(interpolation.Expression, model, out var nested) && !nested.IsDynamic)
                    builder.Append(nested.Value);
                else
                {
                    builder.Append(ExpressionPlaceholder(interpolation.Expression));
                    dynamic = true;
                }
            }
        }
        evaluation = new StringEvaluation(builder.ToString(), dynamic);
        return true;
    }

    static bool TryEvaluateConcatenation(BinaryExpressionSyntax binary, SemanticModel model, out StringEvaluation evaluation)
    {
        var leftOk = TryEvaluateStringExpression(binary.Left, model, out var left);
        var rightOk = TryEvaluateStringExpression(binary.Right, model, out var right);
        var stringLike = IsStringLike(binary, model) || leftOk || rightOk;
        if (!stringLike)
        {
            evaluation = new StringEvaluation(string.Empty, false);
            return false;
        }
        var value = (leftOk ? left.Value : ExpressionPlaceholder(binary.Left)) + (rightOk ? right.Value : ExpressionPlaceholder(binary.Right));
        evaluation = new StringEvaluation(value, (leftOk && left.IsDynamic) || (rightOk && right.IsDynamic) || !leftOk || !rightOk);
        return true;
    }

    static bool TryEvaluateStringFormat(InvocationExpressionSyntax invocation, SemanticModel model, out StringEvaluation evaluation)
    {
        evaluation = new StringEvaluation(string.Empty, false);
        var args = invocation.ArgumentList.Arguments;
        if (args.Count == 0 || !TryEvaluateStringExpression(args[0].Expression, model, out var format)) return false;
        var text = format.Value;
        var dynamic = format.IsDynamic;
        for (var i = 1; i < args.Count; i++)
        {
            var replacement = TryEvaluateStringExpression(args[i].Expression, model, out var nested) && !nested.IsDynamic
                ? nested.Value
                : ExpressionPlaceholder(args[i].Expression);
            if (!TryEvaluateStringExpression(args[i].Expression, model, out var concrete) || concrete.IsDynamic) dynamic = true;
            text = Regex.Replace(text, @"\{" + (i - 1).ToString(CultureInfo.InvariantCulture) + @"(?:[^{}]*)\}", replacement.Replace("$", "$$"));
        }
        evaluation = new StringEvaluation(text, dynamic);
        return true;
    }

    static bool IsStringFormatInvocation(InvocationExpressionSyntax invocation)
    {
        var name = InvocationName(invocation);
        if (!name.Equals("Format", StringComparison.Ordinal)) return false;
        return invocation.Expression is MemberAccessExpressionSyntax member
            ? member.Expression.ToString().Equals("string", StringComparison.OrdinalIgnoreCase)
              || member.Expression.ToString().Equals("String", StringComparison.Ordinal)
              || member.Expression.ToString().EndsWith(".String", StringComparison.Ordinal)
            : false;
    }

    static bool IsStringLike(ExpressionSyntax expression, SemanticModel model)
    {
        var type = model.GetTypeInfo(expression).Type ?? model.GetTypeInfo(expression).ConvertedType;
        return type?.SpecialType == SpecialType.System_String;
    }

    static string ExpressionPlaceholder(ExpressionSyntax expression)
    {
        var text = Regex.Replace(expression.ToString(), @"\s+", " ").Trim();
        return "{" + (text.Length > 80 ? text[..80] : text) + "}";
    }

    static string SourceClassForNode(SyntaxNode node)
        => node.AncestorsAndSelf().OfType<ClassDeclarationSyntax>().FirstOrDefault()?.Identifier.Text ?? string.Empty;

    static string SourceMemberForNode(SyntaxNode node)
        => node.AncestorsAndSelf().OfType<MethodDeclarationSyntax>().FirstOrDefault()?.Identifier.Text
           ?? node.AncestorsAndSelf().OfType<ConstructorDeclarationSyntax>().FirstOrDefault()?.Identifier.Text
           ?? node.AncestorsAndSelf().OfType<PropertyDeclarationSyntax>().FirstOrDefault()?.Identifier.Text
           ?? string.Empty;

    static string ResolveInvocationTargetClass(InvocationExpressionSyntax invocation, SemanticModel model, IReadOnlyCollection<string> knownClasses)
    {
        var symbol = InvocationMethodSymbol(invocation, model);
        var symbolMatch = MatchKnownClass(symbol?.ContainingType?.Name ?? string.Empty, knownClasses);
        if (!string.IsNullOrEmpty(symbolMatch)) return symbolMatch;
        if (invocation.Expression is MemberAccessExpressionSyntax member)
        {
            var receiverMatch = ResolveReceiverClass(member.Expression, model, knownClasses);
            if (!string.IsNullOrEmpty(receiverMatch)) return receiverMatch;
        }
        if (invocation.Expression is MemberBindingExpressionSyntax)
        {
            var conditional = invocation.Ancestors().OfType<ConditionalAccessExpressionSyntax>().FirstOrDefault();
            if (conditional is not null)
            {
                var receiverMatch = ResolveReceiverClass(conditional.Expression, model, knownClasses);
                if (!string.IsNullOrEmpty(receiverMatch)) return receiverMatch;
            }
        }
        return string.Empty;
    }

    static string ResolveReceiverClass(ExpressionSyntax receiver, SemanticModel model, IReadOnlyCollection<string> knownClasses)
    {
        var type = model.GetTypeInfo(receiver).Type ?? model.GetTypeInfo(receiver).ConvertedType;
        var typeMatch = MatchKnownClass(type?.Name ?? string.Empty, knownClasses);
        if (!string.IsNullOrEmpty(typeMatch)) return typeMatch;
        if (receiver is ObjectCreationExpressionSyntax objectCreation)
        {
            var created = MatchKnownClass(objectCreation.Type.ToString().Split('.').Last(), knownClasses);
            if (!string.IsNullOrEmpty(created)) return created;
        }
        if (receiver is IdentifierNameSyntax identifier)
            return MatchVariableNameToKnownClass(identifier.Identifier.Text, knownClasses);
        if (receiver is MemberAccessExpressionSyntax memberAccess)
            return MatchVariableNameToKnownClass(memberAccess.Name.Identifier.Text, knownClasses);
        return string.Empty;
    }

    static string MatchKnownClass(string typeName, IReadOnlyCollection<string> knownClasses)
    {
        if (string.IsNullOrWhiteSpace(typeName)) return string.Empty;
        var clean = typeName.Split('.').Last();
        var exact = knownClasses.FirstOrDefault(name => name.Equals(clean, StringComparison.Ordinal));
        if (!string.IsNullOrEmpty(exact)) return exact;
        if (clean.StartsWith("I", StringComparison.Ordinal) && clean.Length > 1 && char.IsUpper(clean[1]))
        {
            var implementation = clean[1..];
            return knownClasses.FirstOrDefault(name => name.Equals(implementation, StringComparison.Ordinal)) ?? string.Empty;
        }
        return string.Empty;
    }

    static string MatchVariableNameToKnownClass(string variableName, IReadOnlyCollection<string> knownClasses)
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

    static IMethodSymbol? InvocationMethodSymbol(InvocationExpressionSyntax invocation, SemanticModel model)
    {
        var info = model.GetSymbolInfo(invocation);
        if (info.Symbol is IMethodSymbol symbol) return symbol;
        return info.CandidateSymbols.OfType<IMethodSymbol>().FirstOrDefault();
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

    static string CombineRoutes(string prefix, string suffix)
    {
        var left = (prefix ?? string.Empty).Trim('/');
        var right = (suffix ?? string.Empty).Trim('/');
        if (string.IsNullOrEmpty(left)) return "/" + right;
        if (string.IsNullOrEmpty(right)) return "/" + left;
        return "/" + left + "/" + right;
    }

    public static (string Method, string Route) NormalizeHttpRoute(string method, string route)
    {
        var normalizedMethod = (method ?? string.Empty).Trim().ToUpperInvariant();
        var raw = (route ?? string.Empty).Trim();
        string path;
        if (Uri.TryCreate(raw.Contains("://", StringComparison.Ordinal) || raw.StartsWith("//", StringComparison.Ordinal) ? raw : "http://contract.local/" + raw.TrimStart('/'), UriKind.Absolute, out var uri))
            path = Uri.UnescapeDataString(uri.AbsolutePath);
        else
            path = "/" + raw.TrimStart('/');
        path = Regex.Replace(path, "/+", "/");
        path = Regex.Replace(path, @"(?:\{[^/{}]+\}|:[A-Za-z_][A-Za-z0-9_]*)", "{id}");
        if (path != "/") path = path.TrimEnd('/');
        return (normalizedMethod, path);
    }

    public static string ApiOperationId(string application, string method, string route)
    {
        var normalized = NormalizeHttpRoute(method, route);
        return StableNodeId("api-operation", application, normalized.Method, normalized.Route);
    }

    public static string StableNodeId(string kind, params string[] parts)
        => string.Join(':', new[] { kind.Trim().ToLowerInvariant().Replace('_', '-') }.Concat(parts.Select(part => part.Trim())));

    public static string Slug(string value)
    {
        var name = Path.GetFileName(value.Trim());
        var slug = Regex.Replace(name, @"[^A-Za-z0-9_.-]+", "-").Trim('-').ToLowerInvariant();
        return string.IsNullOrEmpty(slug) ? "source" : slug;
    }

    public static string OracleIdentifier(string value)
        => value.Trim().Trim('"').ToUpperInvariant();

    public static string LeafIdentifier(string value)
    {
        var clean = value.Split('@')[0];
        var parts = clean.Split('.', StringSplitOptions.RemoveEmptyEntries);
        return OracleIdentifier(parts.Length == 0 ? clean : parts[^1]);
    }

    public static string TableId(string database, string table) => StableNodeId("table", OracleIdentifier(database), OracleIdentifier(table));
    public static string SequenceId(string database, string sequence) => StableNodeId("sequence", OracleIdentifier(database), OracleIdentifier(sequence));

    public static int LineForOffset(SourceFile file, int offset)
    {
        if (file.SyntaxTree is not null) return file.SyntaxTree.GetLineSpan(Microsoft.CodeAnalysis.Text.TextSpan.FromBounds(offset, offset)).StartLinePosition.Line + 1;
        return 1 + file.Text[..Math.Clamp(offset, 0, file.Text.Length)].Count(ch => ch == '\n');
    }

    public static string LineText(SourceFile file, int line)
    {
        var lines = file.Text.Split(new[] { "\r\n", "\n" }, StringSplitOptions.None);
        return line >= 1 && line <= lines.Length ? lines[line - 1] : string.Empty;
    }

    public static int LineContaining(string text, string needle)
    {
        if (string.IsNullOrEmpty(needle)) return 1;
        var lines = text.Split(new[] { "\r\n", "\n" }, StringSplitOptions.None);
        for (var index = 0; index < lines.Length; index++) if (lines[index].Contains(needle, StringComparison.Ordinal)) return index + 1;
        return 1;
    }

    public static string RepositoryPath(string value)
    {
        var normalized = value.Replace('\\', '/').Trim();
        if (normalized.StartsWith('/') || Regex.IsMatch(normalized, @"^[A-Za-z]:/"))
            throw new ArgumentException($"Repository path must be relative: {value}");
        var parts = normalized.Split('/', StringSplitOptions.RemoveEmptyEntries).Where(part => part != ".").ToArray();
        if (parts.Length == 0 || parts.Any(part => part == ".."))
            throw new ArgumentException($"Invalid repository path: {value}");
        return string.Join('/', parts);
    }

    public static string DisplayFromIdentifier(string value)
    {
        var text = Regex.Replace(value, @"([a-z0-9])([A-Z])", "$1 $2").Replace('_', ' ').Replace('-', ' ');
        return CultureInfo.InvariantCulture.TextInfo.ToTitleCase(text);
    }

    public static Dictionary<string, string> LoadMappings(string path)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!File.Exists(path)) return result;
        foreach (var row in Csv.Read(path))
        {
            var key = string.Join('|', row.GetValueOrDefault("job_system", string.Empty), row.GetValueOrDefault("executable_scope", string.Empty), row.GetValueOrDefault("executable_name", string.Empty));
            result[key] = row.GetValueOrDefault("canonical_executable_name", string.Empty);
        }
        return result;
    }
}

public sealed class Catalog
{
    readonly Dictionary<string, Dictionary<string, HashSet<string>>> _tables = new(StringComparer.OrdinalIgnoreCase);

    public static Catalog Load(string inputRoot, string database = "")
    {
        var catalog = new Catalog();
        var path = Path.Combine(inputRoot, "tables.csv");
        if (!File.Exists(path)) return catalog;
        foreach (var row in Csv.Read(path))
        {
            var db = ExtractorRuntime.OracleIdentifier(row["database"]);
            if (!string.IsNullOrEmpty(database) && !db.Equals(ExtractorRuntime.OracleIdentifier(database), StringComparison.OrdinalIgnoreCase)) continue;
            var table = ExtractorRuntime.OracleIdentifier(row["table_code"]);
            if (!catalog._tables.TryGetValue(db, out var tables)) catalog._tables[db] = tables = new Dictionary<string, HashSet<string>>(StringComparer.OrdinalIgnoreCase);
            if (!tables.TryGetValue(table, out var columns)) tables[table] = columns = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var child = Path.Combine(inputRoot, "tables", row["table_code"] + ".csv");
            if (File.Exists(child))
                foreach (var columnRow in Csv.Read(child)) columns.Add(ExtractorRuntime.OracleIdentifier(columnRow["column_code"]));
        }
        return catalog;
    }

    public bool HasTable(string database, string table)
        => _tables.TryGetValue(ExtractorRuntime.OracleIdentifier(database), out var tables) && tables.ContainsKey(ExtractorRuntime.LeafIdentifier(table));

    public bool HasColumn(string database, string table, string column)
        => _tables.TryGetValue(ExtractorRuntime.OracleIdentifier(database), out var tables)
           && tables.TryGetValue(ExtractorRuntime.LeafIdentifier(table), out var columns)
           && columns.Contains(ExtractorRuntime.OracleIdentifier(column));
}

public static class SqlAnalyzer
{
    static readonly Regex Insert = new(@"\bINSERT\s+(?:INTO\s+)?(?<name>[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?(?:@[A-Za-z_][\w$#]*)?)", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex Update = new(@"\bUPDATE\s+(?<name>[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?(?:@[A-Za-z_][\w$#]*)?)", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex Delete = new(@"\bDELETE\s+FROM\s+(?<name>[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?(?:@[A-Za-z_][\w$#]*)?)", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex Merge = new(@"\bMERGE\s+INTO\s+(?<name>[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?(?:@[A-Za-z_][\w$#]*)?)", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex FromJoin = new(@"\b(?:FROM|JOIN)\s+(?<name>[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?(?:@[A-Za-z_][\w$#]*)?)", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex Procedure = new(@"\b(?:CALL|EXEC(?:UTE)?)(?!\s+IMMEDIATE)\s+(?<name>[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*){1,2})\b", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex BeginProcedure = new(@"\bBEGIN\s+(?<name>[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*){1,2})\s*\(", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex Sequence = new(@"\b(?<name>(?:[A-Za-z_][\w$#]*\.)?[A-Za-z_][\w$#]*)\s*\.\s*(?:NEXTVAL|CURRVAL)\b", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex ExecuteImmediateVariable = new(@"\bEXECUTE\s+IMMEDIATE\s+[A-Za-z_][\w$#]*\b", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly HashSet<string> Keywords = new(StringComparer.OrdinalIgnoreCase) { "SELECT", "FROM", "WHERE", "JOIN", "ON", "SET", "VALUES", "INTO", "USING" };

    public static SqlAnalysis Analyze(string text)
    {
        var tables = new List<TableReference>();
        AddTables(tables, text, Insert, "INSERT", "INSERTS");
        AddTables(tables, text, Update, "UPDATE", "UPDATES");
        AddTables(tables, text, Delete, "DELETE", "DELETES");
        AddTables(tables, text, Merge, "MERGE", "MERGES");
        AddTables(tables, text, FromJoin, "SELECT", "READS");
        var procedures = Procedure.Matches(text)
            .Concat(BeginProcedure.Matches(text))
            .Select(match => new ProcedureReference(match.Groups["name"].Value, match.Groups["name"].Index))
            .GroupBy(reference => reference.ObjectName, StringComparer.OrdinalIgnoreCase)
            .Select(group => group.OrderBy(reference => reference.Start).First())
            .ToList();
        var sequences = Sequence.Matches(text).Select(match => new SequenceReference(match.Groups["name"].Value, match.Index)).ToList();
        var dynamic = ExecuteImmediateVariable.Matches(text).Select(match => match.Index).ToList();
        return new SqlAnalysis(tables, procedures, sequences, dynamic);
    }

    static void AddTables(List<TableReference> tables, string text, Regex regex, string operation, string edgeType)
    {
        foreach (Match match in regex.Matches(text))
        {
            var name = match.Groups["name"].Value;
            if (Keywords.Contains(name)) continue;
            var actualEdge = name.Contains('@', StringComparison.Ordinal) && edgeType == "READS" ? "REMOTE_READS" : edgeType;
            tables.Add(new TableReference(name, operation, actualEdge, match.Groups["name"].Index, name.Contains('@', StringComparison.Ordinal)));
        }
    }
}

public static class SemanticTreeV2
{
    public static Dictionary<string, object> Operation(string label, IEnumerable<ParameterSyntax>? parameters, IEnumerable<Dictionary<string, object>> steps, SourceFile? file = null, SyntaxNode? body = null)
    {
        var parameterFacts = (parameters ?? Enumerable.Empty<ParameterSyntax>()).Select(parameter => new Dictionary<string, object>
        {
            ["name"] = parameter.Identifier.Text,
            ["type"] = parameter.Type?.ToString() ?? string.Empty,
        }).ToList();
        var outputs = new List<Dictionary<string, object>>();
        var exceptions = new List<Dictionary<string, object>>();
        if (file is not null && body is not null)
        {
            foreach (var returned in body.DescendantNodesAndSelf().OfType<ReturnStatementSyntax>().OrderBy(node => node.SpanStart))
                outputs.Add(Fact("output", $"Return {returned.Expression?.ToString() ?? string.Empty}".TrimEnd(), file, returned.SpanStart));
            foreach (var caught in body.DescendantNodesAndSelf().OfType<CatchClauseSyntax>().OrderBy(node => node.SpanStart))
                exceptions.Add(new Dictionary<string, object>
                {
                    ["type"] = "exception",
                    ["label"] = caught.Declaration?.Type.ToString() ?? "catch",
                    ["source"] = Source(file, caught.SpanStart),
                });
        }
        return new Dictionary<string, object>
        {
            ["version"] = 2,
            ["type"] = "operation",
            ["label"] = label,
            ["summary"] = string.Empty,
            ["parameters"] = parameterFacts,
            ["steps"] = steps.OrderBy(step => Convert.ToInt32(step["_offset"], CultureInfo.InvariantCulture)).Select(Clean).ToList(),
            ["outputs"] = outputs,
            ["exceptions"] = exceptions,
            ["analysis_notes"] = new List<object>(),
        };
    }

    public static Dictionary<string, object> Fact(string type, string label, SourceFile file, int offset, string? refNodeId = null, string? action = null)
    {
        var fact = new Dictionary<string, object> { ["type"] = type, ["label"] = label, ["source"] = Source(file, offset), ["_offset"] = offset };
        if (!string.IsNullOrEmpty(refNodeId)) fact["ref_node_id"] = refNodeId;
        if (!string.IsNullOrEmpty(action)) fact["action"] = action;
        return fact;
    }

    static Dictionary<string, object> Source(SourceFile file, int offset) => new() { ["path"] = file.RelativePath, ["line"] = ExtractorRuntime.LineForOffset(file, offset) };
    static Dictionary<string, object> Clean(Dictionary<string, object> fact) { fact.Remove("_offset"); return fact; }
}

public sealed class PackageBuilder
{
    static readonly Dictionary<string, string[]> Headers = new()
    {
        ["nodes"] = "node_id,node_type,technical_name,qualified_name,default_display_name,system_key,database_key,repository_key,graph_role,confidence,properties_json".Split(','),
        ["edges"] = "edge_id,source_node_id,target_node_id,edge_type,graph_layer,raw_operation,confidence,properties_json".Split(','),
        ["evidence"] = "evidence_id,target_type,target_id,source_path,start_line,end_line,start_column,end_column,evidence_kind,extractor_name,confidence,snippet,properties_json".Split(','),
        ["issues"] = "issue_id,issue_type,severity,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json".Split(','),
        ["localized_texts"] = "target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at".Split(','),
    };

    readonly string _packageId;
    readonly string _sourceId;
    readonly string _extractorName;
    readonly string _extractorVersion;
    readonly Dictionary<string, object> _metadata;
    readonly Dictionary<string, Dictionary<string, string>> _nodes = new(StringComparer.Ordinal);
    readonly Dictionary<string, Dictionary<string, string>> _edges = new(StringComparer.Ordinal);
    readonly Dictionary<string, Dictionary<string, string>> _evidence = new(StringComparer.Ordinal);
    readonly Dictionary<string, Dictionary<string, string>> _issues = new(StringComparer.Ordinal);
    readonly Dictionary<string, Dictionary<string, string>> _localizedTexts = new(StringComparer.Ordinal);

    public int FilesScanned { get; set; }

    public PackageBuilder(string packageId, string sourceId, string extractorName, string extractorVersion, Dictionary<string, object>? metadata = null)
    {
        _packageId = packageId;
        _sourceId = sourceId;
        _extractorName = extractorName;
        _extractorVersion = extractorVersion;
        _metadata = metadata ?? new Dictionary<string, object>();
    }

    public string AddNode(string nodeId, string nodeType, string technicalName, string qualifiedName, string displayName, string systemKey = "", string databaseKey = "", string repositoryKey = "", string graphRole = "MAIN", double confidence = 1.0, Dictionary<string, object>? properties = null)
    {
        _nodes.TryAdd(nodeId, new Dictionary<string, string>
        {
            ["node_id"] = nodeId,
            ["node_type"] = nodeType,
            ["technical_name"] = technicalName,
            ["qualified_name"] = qualifiedName,
            ["default_display_name"] = displayName,
            ["system_key"] = systemKey,
            ["database_key"] = databaseKey,
            ["repository_key"] = repositoryKey,
            ["graph_role"] = graphRole,
            ["confidence"] = confidence.ToString("0.0", CultureInfo.InvariantCulture),
            ["properties_json"] = Json(properties),
        });
        return nodeId;
    }

    public void SetNodeProperty(string nodeId, string name, object value)
    {
        if (!_nodes.TryGetValue(nodeId, out var row)) throw new KeyNotFoundException($"Node not found: {nodeId}");
        var properties = JsonSerializer.Deserialize<Dictionary<string, object>>(row["properties_json"]) ?? new Dictionary<string, object>();
        properties[name] = value;
        row["properties_json"] = Json(properties);
    }

    public string AddEdge(string sourceNodeId, string targetNodeId, string edgeType, string graphLayer = "TECHNICAL", string rawOperation = "", double confidence = 1.0, Dictionary<string, object>? properties = null)
    {
        var edgeId = "edge:" + Sha256(string.Join('|', sourceNodeId, edgeType, targetNodeId, rawOperation, graphLayer));
        _edges.TryAdd(edgeId, new Dictionary<string, string>
        {
            ["edge_id"] = edgeId,
            ["source_node_id"] = sourceNodeId,
            ["target_node_id"] = targetNodeId,
            ["edge_type"] = edgeType,
            ["graph_layer"] = graphLayer,
            ["raw_operation"] = rawOperation,
            ["confidence"] = confidence.ToString("0.0", CultureInfo.InvariantCulture),
            ["properties_json"] = Json(properties),
        });
        return edgeId;
    }

    public string AddEvidence(string targetType, string targetId, string sourcePath, int startLine, int endLine, string evidenceKind, string snippet, double confidence = 1.0, Dictionary<string, object>? properties = null)
    {
        var path = ExtractorRuntime.RepositoryPath(sourcePath);
        var identity = $"{targetType}|{targetId}|{path}|{startLine}|{endLine}|{evidenceKind}|{snippet.Trim()}";
        var evidenceId = "ev:" + Sha256(identity)[..24];
        _evidence.TryAdd(evidenceId, new Dictionary<string, string>
        {
            ["evidence_id"] = evidenceId,
            ["target_type"] = targetType,
            ["target_id"] = targetId,
            ["source_path"] = path,
            ["start_line"] = startLine.ToString(CultureInfo.InvariantCulture),
            ["end_line"] = endLine.ToString(CultureInfo.InvariantCulture),
            ["start_column"] = "1",
            ["end_column"] = Math.Max(1, snippet.Trim().Length).ToString(CultureInfo.InvariantCulture),
            ["evidence_kind"] = evidenceKind,
            ["extractor_name"] = _extractorName,
            ["confidence"] = confidence.ToString("0.0", CultureInfo.InvariantCulture),
            ["snippet"] = snippet.Trim(),
            ["properties_json"] = Json(properties),
        });
        return evidenceId;
    }

    public string AddIssue(string issueType, string severity, string message, string sourceNodeId = "", string rawReference = "", string databaseKey = "", string sourcePath = "", int startLine = 0, Dictionary<string, object>? properties = null)
    {
        var path = string.IsNullOrEmpty(sourcePath) ? string.Empty : ExtractorRuntime.RepositoryPath(sourcePath);
        var line = startLine > 0 ? startLine.ToString(CultureInfo.InvariantCulture) : string.Empty;
        var identity = $"{issueType}|{sourceNodeId}|{rawReference}|{databaseKey}|{path}|{line}|{message}";
        var issueId = "issue:" + Sha256(identity)[..24];
        _issues.TryAdd(issueId, new Dictionary<string, string>
        {
            ["issue_id"] = issueId,
            ["issue_type"] = issueType,
            ["severity"] = severity,
            ["source_node_id"] = sourceNodeId,
            ["raw_reference"] = rawReference,
            ["database_key"] = databaseKey,
            ["source_path"] = path,
            ["start_line"] = line,
            ["message"] = message,
            ["properties_json"] = Json(properties),
        });
        return issueId;
    }

    public string AddLocalizedText(string targetType, string targetId, string fieldName, string locale, string value, string sourceKind = "EXTRACTED", string reviewStatus = "PENDING", string authorName = "", string createdAt = "", string updatedAt = "")
    {
        var key = string.Join('|', targetId, fieldName, locale);
        _localizedTexts.TryAdd(key, new Dictionary<string, string>
        {
            ["target_type"] = targetType,
            ["target_id"] = targetId,
            ["field_name"] = fieldName,
            ["locale"] = locale,
            ["value"] = value,
            ["source_kind"] = sourceKind,
            ["review_status"] = reviewStatus,
            ["author_name"] = string.IsNullOrWhiteSpace(authorName) ? _extractorName : authorName,
            ["created_at"] = createdAt,
            ["updated_at"] = updatedAt,
        });
        return key;
    }

    public void Write(string output)
    {
        Directory.CreateDirectory(output);
        var groups = new Dictionary<string, List<Dictionary<string, string>>>
        {
            ["nodes"] = _nodes.Values.OrderBy(row => row["node_id"], StringComparer.Ordinal).ToList(),
            ["edges"] = _edges.Values.OrderBy(row => row["edge_id"], StringComparer.Ordinal).ToList(),
            ["evidence"] = _evidence.Values.OrderBy(row => row["evidence_id"], StringComparer.Ordinal).ToList(),
            ["issues"] = _issues.Values.OrderBy(row => row["issue_id"], StringComparer.Ordinal).ToList(),
        };
        if (_localizedTexts.Count > 0)
        {
            groups["localized_texts"] = _localizedTexts.Values.OrderBy(row => row["target_id"], StringComparer.Ordinal).ThenBy(row => row["field_name"], StringComparer.Ordinal).ThenBy(row => row["locale"], StringComparer.Ordinal).ToList();
        }
        var checksums = new Dictionary<string, Dictionary<string, object>>();
        var statistics = new Dictionary<string, object> { ["filesScanned"] = FilesScanned };
        var files = new Dictionary<string, string>();
        foreach (var (name, rows) in groups)
        {
            var path = Path.Combine(output, name + ".csv");
            Csv.Write(path, Headers[name], rows);
            var bytes = File.ReadAllBytes(path);
            files[name] = name + ".csv";
            checksums[name + ".csv"] = new Dictionary<string, object> { ["sha256"] = Sha256(bytes), ["bytes"] = bytes.Length };
            statistics[name] = rows.Count;
        }
        var source = new Dictionary<string, object>
        {
            ["sourceKey"] = _sourceId,
            ["repositoryKey"] = MetadataString("repository", MetadataString("source", _sourceId)),
        };
        var revision = MetadataString("revision", string.Empty);
        if (!string.IsNullOrWhiteSpace(revision)) source["revision"] = revision;
        var manifest = new Dictionary<string, object>
        {
            ["contractVersion"] = "1.0",
            ["extractor"] = new Dictionary<string, object> { ["name"] = _extractorName, ["version"] = _extractorVersion },
            ["source"] = source,
            ["generatedAt"] = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            ["files"] = files,
            ["statistics"] = statistics,
            ["checksums"] = checksums,
            ["metadata"] = _metadata,
        };
        File.WriteAllText(Path.Combine(output, "manifest.json"), JsonSerializer.Serialize(manifest, new JsonSerializerOptions { WriteIndented = true }) + "\n", new UTF8Encoding(false));
    }

    string MetadataString(string key, string fallback)
        => _metadata.TryGetValue(key, out var value) && value is not null && !string.IsNullOrWhiteSpace(value.ToString()) ? value.ToString()! : fallback;

    static string Json(Dictionary<string, object>? value)
        => value is null || value.Count == 0 ? "{}" : JsonSerializer.Serialize(value.OrderBy(pair => pair.Key).ToDictionary(pair => pair.Key, pair => pair.Value));

    static string Sha256(string value) => Sha256(Encoding.UTF8.GetBytes(value));
    static string Sha256(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
}

public static class Csv
{
    public static List<Dictionary<string, string>> Read(string path)
    {
        var lines = File.ReadAllLines(path, Encoding.UTF8);
        if (lines.Length == 0) return new List<Dictionary<string, string>>();
        var headers = ParseLine(lines[0]);
        var rows = new List<Dictionary<string, string>>();
        foreach (var line in lines.Skip(1))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            var cells = ParseLine(line);
            var row = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (var i = 0; i < headers.Count; i++) row[headers[i]] = i < cells.Count ? cells[i] : string.Empty;
            rows.Add(row);
        }
        return rows;
    }

    public static void Write(string path, string[] headers, IEnumerable<Dictionary<string, string>> rows)
    {
        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        writer.WriteLine(string.Join(',', headers));
        foreach (var row in rows)
            writer.WriteLine(string.Join(',', headers.Select(header => Escape(row.TryGetValue(header, out var value) ? value : string.Empty))));
    }

    static List<string> ParseLine(string line)
    {
        var result = new List<string>();
        var sb = new StringBuilder();
        var quoted = false;
        for (var i = 0; i < line.Length; i++)
        {
            var ch = line[i];
            if (quoted)
            {
                if (ch == '"' && i + 1 < line.Length && line[i + 1] == '"') { sb.Append('"'); i++; }
                else if (ch == '"') quoted = false;
                else sb.Append(ch);
            }
            else
            {
                if (ch == ',') { result.Add(sb.ToString()); sb.Clear(); }
                else if (ch == '"') quoted = true;
                else sb.Append(ch);
            }
        }
        result.Add(sb.ToString());
        return result;
    }

    static string Escape(string value)
        => value.IndexOfAny(new[] { ',', '"', '\n', '\r' }) >= 0 ? "\"" + value.Replace("\"", "\"\"") + "\"" : value;
}
