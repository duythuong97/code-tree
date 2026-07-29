using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Xml.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace CodeMap.Extractors
{
    public static class NetFrameworkSupport
    {
        public static IEnumerable<string> ExtractPackagesConfig(string text)
        {
            try
            {
                var doc = XDocument.Parse(text);
                return doc.Descendants("package")
                    .Select(x => x.Attribute("id")?.Value)
                    .Where(id => !string.IsNullOrEmpty(id))!;
            }
            catch
            {
                return Enumerable.Empty<string>();
            }
        }

        public static IEnumerable<string> ExtractWebConfig(string text)
        {
            try
            {
                var doc = XDocument.Parse(text);
                return doc.Descendants("add")
                    .Where(x => x.Parent?.Name == "appSettings" || x.Parent?.Name == "connectionStrings")
                    .Select(x => x.Attribute("key")?.Value ?? x.Attribute("name")?.Value)
                    .Where(key => !string.IsNullOrEmpty(key))!;
            }
            catch
            {
                return Enumerable.Empty<string>();
            }
        }

        public static IEnumerable<EndpointInfo> ExtractRouteConfig(SourceFile file, SemanticModel model)
        {
            var endpoints = new List<EndpointInfo>();
            if (file.SyntaxTree is null) return endpoints;

            var invocations = file.SyntaxTree.GetRoot().DescendantNodes().OfType<InvocationExpressionSyntax>();
            foreach (var invocation in invocations)
            {
                if (invocation.Expression is MemberAccessExpressionSyntax memberAccess &&
                    memberAccess.Name.Identifier.Text is "MapRoute" or "MapHttpRoute")
                {
                    var args = invocation.ArgumentList.Arguments;
                    var urlArg = args.FirstOrDefault(a =>
                            a.NameColon?.Name.Identifier.Text is "url" or "routeTemplate")
                        ?? args.ElementAtOrDefault(1);
                    var defaultsArg = args.FirstOrDefault(a => a.NameColon?.Name.Identifier.Text == "defaults") ?? args.ElementAtOrDefault(2);

                    if (urlArg?.Expression is LiteralExpressionSyntax urlLiteral)
                    {
                        var route = urlLiteral.Token.ValueText;
                        var controller = "";
                        var action = "";

                        if (defaultsArg?.Expression is AnonymousObjectCreationExpressionSyntax defaults)
                        {
                            foreach (var prop in defaults.Initializers)
                            {
                                if (prop.NameEquals?.Name.Identifier.Text == "controller" && prop.Expression is LiteralExpressionSyntax ctrlLiteral)
                                    controller = ctrlLiteral.Token.ValueText;
                                if (prop.NameEquals?.Name.Identifier.Text == "action" && prop.Expression is LiteralExpressionSyntax actLiteral)
                                    action = actLiteral.Token.ValueText;
                            }
                        }

                        if (!string.IsNullOrEmpty(controller) && !string.IsNullOrEmpty(action))
                        {
                            endpoints.Add(new EndpointInfo(
                                controller,
                                action,
                                "ANY",
                                route,
                                invocation.SpanStart,
                                invocation.SpanStart,
                                invocation.Span.End,
                                File: file
                            ));
                        }
                    }
                }
            }
            return endpoints;
        }
    }
}
