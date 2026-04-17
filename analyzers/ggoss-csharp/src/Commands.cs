using System.Text.Json;
using Microsoft.Build.Locator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.MSBuild;

namespace Mnemos.Analyzers.CSharp;

public static class Commands
{
    private static bool s_msbuildRegistered;

    private static void EnsureMsBuild()
    {
        if (s_msbuildRegistered) return;
        try { MSBuildLocator.RegisterDefaults(); }
        catch (InvalidOperationException) { /* already registered */ }
        s_msbuildRegistered = true;
    }

    private static (string? path, string? output) ParseCommon(string[] args)
    {
        string? path = null;
        string? output = null;
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--output" && i + 1 < args.Length) { output = args[++i]; }
            else if (path is null) { path = args[i]; }
        }
        return (path, output);
    }

    public static Task<int> ProbeAsync(string[] args)
    {
        var (path, _) = ParseCommon(args);
        if (path is null) { Console.Error.WriteLine("probe requires <path>"); return Task.FromResult(2); }
        if (!Directory.Exists(path))
        {
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                applicable = false, reason = "path_not_found", files_found = 0
            }));
            return Task.FromResult(0);
        }

        var csFiles = Directory.EnumerateFiles(path, "*.cs", SearchOption.AllDirectories).Take(10000).Count();
        var slnFiles = Directory.EnumerateFiles(path, "*.sln", SearchOption.AllDirectories).Count();
        var csprojFiles = Directory.EnumerateFiles(path, "*.csproj", SearchOption.AllDirectories).Count();

        var applicable = csFiles > 0 || csprojFiles > 0 || slnFiles > 0;
        var reason = applicable
            ? $"found {csFiles} .cs / {csprojFiles} .csproj / {slnFiles} .sln"
            : "no_csharp_sources";

        Console.WriteLine(JsonSerializer.Serialize(new
        {
            applicable, reason, files_found = csFiles
        }));
        return Task.FromResult(0);
    }

    public static Task<int> InventoryAsync(string[] args)
    {
        var (path, _) = ParseCommon(args);
        if (path is null) { Console.Error.WriteLine("inventory requires <path>"); return Task.FromResult(2); }

        var files = Directory.EnumerateFiles(path, "*.cs", SearchOption.AllDirectories).ToList();
        var projects = Directory.EnumerateFiles(path, "*.csproj", SearchOption.AllDirectories).ToList();
        var solutions = Directory.EnumerateFiles(path, "*.sln", SearchOption.AllDirectories).ToList();

        Console.WriteLine(JsonSerializer.Serialize(new
        {
            files = files.Select(f => Path.GetRelativePath(path, f)).Take(5000),
            modules = projects.Select(p => Path.GetRelativePath(path, p)),
            solutions = solutions.Select(s => Path.GetRelativePath(path, s)),
            errors = Array.Empty<object>()
        }));
        return Task.FromResult(0);
    }

    public static async Task<int> SymbolsAsync(string[] args)
    {
        var (path, output) = ParseCommon(args);
        if (path is null) { Console.Error.WriteLine("symbols requires <path>"); return 2; }

        using var writer = OpenOutput(output);
        var compilations = await LoadAsync(path);

        foreach (var compilation in compilations)
        {
            foreach (var tree in compilation.SyntaxTrees)
            {
                SemanticModel model;
                try { model = compilation.GetSemanticModel(tree); }
                catch (Exception ex) { Envelope.ReportError(tree.FilePath, ex.Message); continue; }

                var root = await tree.GetRootAsync();
                foreach (var node in root.DescendantNodesAndSelf())
                {
                    ISymbol? sym = null;
                    string kind = "";
                    switch (node)
                    {
                        case ClassDeclarationSyntax:
                        case StructDeclarationSyntax:
                        case InterfaceDeclarationSyntax:
                        case RecordDeclarationSyntax:
                        case EnumDeclarationSyntax:
                            sym = model.GetDeclaredSymbol(node);
                            kind = sym switch
                            {
                                INamedTypeSymbol t => t.TypeKind switch
                                {
                                    TypeKind.Interface => "interface",
                                    TypeKind.Struct => "struct",
                                    TypeKind.Enum => "enum",
                                    _ => "class"
                                },
                                _ => "class"
                            };
                            break;
                        case MethodDeclarationSyntax:
                        case ConstructorDeclarationSyntax:
                        case DestructorDeclarationSyntax:
                        case OperatorDeclarationSyntax:
                        case ConversionOperatorDeclarationSyntax:
                            sym = model.GetDeclaredSymbol(node);
                            kind = "method";
                            break;
                        case PropertyDeclarationSyntax:
                            sym = model.GetDeclaredSymbol(node);
                            kind = "property";
                            break;
                    }
                    if (sym is null) continue;
                    EmitSymbol(writer, sym, kind, compilation.Assembly.Name);
                }
            }
        }
        return 0;
    }

    public static Task<int> CallsAsync(string[] args)
    {
        // Full call graph arrives in Week 3; Week 2 ships a stub that emits zero edges.
        var (_, output) = ParseCommon(args);
        using var writer = OpenOutput(output);
        return Task.FromResult(0);
    }

    public static Task<int> ContractsAsync(string[] args)
    {
        // ASP.NET contract extraction is Week 3/4 work per spec §15.2.
        var (_, output) = ParseCommon(args);
        using var writer = OpenOutput(output);
        return Task.FromResult(0);
    }

    public static Task<int> DataAccessAsync(string[] args)
    {
        var (_, output) = ParseCommon(args);
        using var writer = OpenOutput(output);
        return Task.FromResult(0);
    }

    public static int Schema()
    {
        var schema = new
        {
            schema = "https://mnemos.dev/analyzer/ggoss-csharp/v1",
            record_types = new[] { "symbol", "edge", "contract", "data_entity" },
            data = new
            {
                symbol = new[]
                {
                    "id", "kind", "name", "component_id", "signature", "location",
                    "visibility", "is_entry_point", "xml_doc", "metadata", "certainty",
                    "created_by"
                },
            }
        };
        Console.WriteLine(JsonSerializer.Serialize(schema));
        return 0;
    }

    private static async Task<List<Compilation>> LoadAsync(string path)
    {
        var results = new List<Compilation>();

        var solutions = Directory.EnumerateFiles(path, "*.sln", SearchOption.AllDirectories).ToList();
        var projects = Directory.EnumerateFiles(path, "*.csproj", SearchOption.AllDirectories).ToList();

        if (solutions.Count > 0 || projects.Count > 0)
        {
            EnsureMsBuild();
            using var ws = MSBuildWorkspace.Create();
            ws.WorkspaceFailed += (_, e) =>
            {
                if (e.Diagnostic.Kind == WorkspaceDiagnosticKind.Failure)
                    Envelope.ReportError(path, e.Diagnostic.Message);
            };

            try
            {
                if (solutions.Count > 0)
                {
                    var sln = await ws.OpenSolutionAsync(solutions[0]);
                    foreach (var proj in sln.Projects)
                    {
                        var c = await proj.GetCompilationAsync();
                        if (c is not null) results.Add(c);
                    }
                }
                else
                {
                    foreach (var p in projects)
                    {
                        var proj = await ws.OpenProjectAsync(p);
                        var c = await proj.GetCompilationAsync();
                        if (c is not null) results.Add(c);
                    }
                }
                if (results.Count > 0) return results;
            }
            catch (Exception ex)
            {
                Envelope.ReportError(path, $"msbuild_load_failed: {ex.Message}");
            }
        }

        // Fallback: parse raw .cs files without build context.
        var trees = new List<SyntaxTree>();
        foreach (var file in Directory.EnumerateFiles(path, "*.cs", SearchOption.AllDirectories))
        {
            try
            {
                var text = await File.ReadAllTextAsync(file);
                trees.Add(CSharpSyntaxTree.ParseText(text, path: file));
            }
            catch (Exception ex) { Envelope.ReportError(file, ex.Message); }
        }
        var compilation = CSharpCompilation.Create(
            Path.GetFileName(path),
            trees,
            references: new[]
            {
                MetadataReference.CreateFromFile(typeof(object).Assembly.Location),
            });
        results.Add(compilation);
        return results;
    }

    private static void EmitSymbol(TextWriter writer, ISymbol sym, string kind, string assemblyName)
    {
        var loc = sym.Locations.FirstOrDefault(l => l.IsInSource);
        var location = loc is null ? null : new
        {
            file = loc.SourceTree?.FilePath,
            line = loc.GetLineSpan().StartLinePosition.Line + 1,
            col = loc.GetLineSpan().StartLinePosition.Character + 1,
        };

        var id = $"csharp:{sym.GetDocumentationCommentId() ?? sym.ToDisplayString()}";
        var data = new
        {
            id,
            kind,
            name = sym.Name,
            component_id = $"svc.{assemblyName}",
            signature = sym.ToDisplayString(),
            location,
            visibility = sym.DeclaredAccessibility.ToString().ToLowerInvariant(),
            is_entry_point = false,
            xml_doc = sym.GetDocumentationCommentXml(),
            metadata = new { assembly = assemblyName },
            certainty = "asserted",
            created_by = new[] { Envelope.SourceName },
        };
        Envelope.WriteLine(writer, "symbol", data);
    }

    private static TextWriter OpenOutput(string? output)
    {
        if (string.IsNullOrEmpty(output)) return Console.Out;
        Directory.CreateDirectory(Path.GetDirectoryName(output)!);
        return new StreamWriter(output);
    }
}
