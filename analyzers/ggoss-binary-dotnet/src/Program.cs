using System.Text.Json;
using Mono.Cecil;

const string SourceName = "ggoss-binary-dotnet";
const string SourceVersion = "1.0.0";

if (args.Length == 0)
{
    Console.Error.WriteLine("usage: ggoss-binary-dotnet <probe|inventory|symbols|schema> [args...]");
    return 2;
}

var verb = args[0];
var rest = args.Skip(1).ToArray();

try
{
    return verb switch
    {
        "probe"     => Probe(rest),
        "inventory" => Inventory(rest),
        "symbols"   => Symbols(rest),
        "schema"    => PrintSchema(),
        "calls" or "contracts" or "data_access" => 0, // opaque binaries expose surface only
        _ => Unknown(verb)
    };
}
catch (Exception ex)
{
    Console.Error.WriteLine(JsonSerializer.Serialize(new
    {
        level = "error", message = ex.Message, recoverable = false
    }));
    return 1;
}

static int Unknown(string verb) { Console.Error.WriteLine($"unknown verb: {verb}"); return 2; }

static int Probe(string[] args)
{
    var path = args.FirstOrDefault();
    if (path is null || !Directory.Exists(path))
    {
        Console.WriteLine(JsonSerializer.Serialize(new { applicable = false, reason = "path_not_found", files_found = 0 }));
        return 0;
    }
    var files = Directory.EnumerateFiles(path, "*.dll", SearchOption.AllDirectories)
        .Concat(Directory.EnumerateFiles(path, "*.exe", SearchOption.AllDirectories))
        .Count();
    Console.WriteLine(JsonSerializer.Serialize(new { applicable = files > 0, reason = $"found {files} .dll/.exe", files_found = files }));
    return 0;
}

static int Inventory(string[] args)
{
    var path = args.FirstOrDefault() ?? ".";
    var files = Directory.Exists(path)
        ? Directory.EnumerateFiles(path, "*.dll", SearchOption.AllDirectories)
            .Concat(Directory.EnumerateFiles(path, "*.exe", SearchOption.AllDirectories))
            .Select(f => Path.GetRelativePath(path, f)).Take(5000)
        : Array.Empty<string>();
    Console.WriteLine(JsonSerializer.Serialize(new { files, modules = Array.Empty<string>(), errors = Array.Empty<string>() }));
    return 0;
}

static int Symbols(string[] args)
{
    var path = args.FirstOrDefault();
    if (path is null || !Directory.Exists(path))
    {
        Console.Error.WriteLine("symbols requires <path>");
        return 2;
    }
    foreach (var file in Directory.EnumerateFiles(path, "*.dll", SearchOption.AllDirectories)
                 .Concat(Directory.EnumerateFiles(path, "*.exe", SearchOption.AllDirectories)))
    {
        AssemblyDefinition asm;
        try { asm = AssemblyDefinition.ReadAssembly(file); }
        catch (Exception ex)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new
            {
                level = "error", file, message = ex.Message, recoverable = true
            }));
            continue;
        }

        var componentId = $"bin.{asm.Name.Name}.dll";
        Emit("data_entity", new
        {
            id = componentId,
            kind = "binary",
            component_id = componentId,
            name = asm.Name.Name,
            schema = new { },
            sample_available = false,
            is_sensitive = false,
            metadata = new
            {
                version = asm.Name.Version.ToString(),
                references = asm.MainModule.AssemblyReferences.Select(r => r.Name).ToArray()
            },
            certainty = "verified",
            created_by = new[] { SourceName },
        });

        foreach (var type in asm.MainModule.Types)
        {
            if (!type.IsPublic) continue;
            foreach (var method in type.Methods)
            {
                if (!method.IsPublic) continue;
                var id = $"dll.{asm.Name.Name}.{type.FullName}.{method.FullName}";
                Emit("symbol", new
                {
                    id,
                    kind = "export",
                    name = method.Name,
                    component_id = componentId,
                    signature = method.FullName,
                    location = (object?)null,
                    visibility = "public",
                    is_entry_point = method.IsConstructor,
                    xml_doc = (string?)null,
                    metadata = new { assembly = asm.Name.Name },
                    certainty = "verified",
                    created_by = new[] { SourceName },
                });
            }
        }
    }
    return 0;
}

static int PrintSchema()
{
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        schema = "https://mnemos.dev/analyzer/ggoss-binary-dotnet/v1",
        record_types = new[] { "symbol", "data_entity" },
    }));
    return 0;
}

static void Emit(string recordType, object data)
{
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        record_type = recordType,
        source_name = SourceName,
        source_version = SourceVersion,
        analyzed_at = DateTime.UtcNow.ToString("O"),
        data,
    }));
}
