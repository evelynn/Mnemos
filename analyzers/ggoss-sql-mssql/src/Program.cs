using System.Text.Json;
using Microsoft.Data.SqlClient;

const string SourceName = "ggoss-sql-mssql";
const string SourceVersion = "1.0.0";

if (args.Length == 0)
{
    Console.Error.WriteLine("usage: ggoss-sql-mssql <probe|inventory|live_schema|live_stats|sample|query|schema> [args...]");
    return 2;
}

var verb = args[0];
var rest = args.Skip(1).ToArray();

try
{
    return verb switch
    {
        "probe"       => Probe(rest),
        "inventory"   => Inventory(rest),
        "live_schema" => await LiveSchemaAsync(rest),
        "live_stats"  => LiveStats(rest),
        "sample"      => await SampleAsync(rest),
        "query"       => await QueryAsync(rest),
        "schema"      => PrintSchema(),
        _             => Unknown(verb)
    };
}
catch (Exception ex)
{
    Console.Error.WriteLine(JsonSerializer.Serialize(new
    {
        level = "error", message = ex.Message, type = ex.GetType().Name, recoverable = false
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
    var files = Directory.EnumerateFiles(path, "*.sql", SearchOption.AllDirectories).Count();
    Console.WriteLine(JsonSerializer.Serialize(new { applicable = files > 0, reason = $"found {files} .sql", files_found = files }));
    return 0;
}

static int Inventory(string[] args)
{
    var path = args.FirstOrDefault() ?? ".";
    var files = Directory.Exists(path)
        ? Directory.EnumerateFiles(path, "*.sql", SearchOption.AllDirectories).Select(f => Path.GetRelativePath(path, f)).Take(5000)
        : Array.Empty<string>();
    Console.WriteLine(JsonSerializer.Serialize(new { files, modules = Array.Empty<string>(), errors = Array.Empty<string>() }));
    return 0;
}

static int LiveStats(string[] _)
{
    // Full DMV extraction lands in Week 6 when the runtime correlator is wired in.
    return 0;
}

static int PrintSchema()
{
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        schema = "https://mnemos.dev/analyzer/ggoss-sql-mssql/v1",
        record_types = new[] { "symbol", "data_entity", "edge", "sample" },
    }));
    return 0;
}

static string? FindConnArg(string[] args, string flag)
{
    for (int i = 0; i < args.Length - 1; i++) if (args[i] == flag) return args[i + 1];
    return null;
}

static async Task<int> LiveSchemaAsync(string[] args)
{
    var conn = FindConnArg(args, "--conn") ?? Environment.GetEnvironmentVariable("MNEMOS_MSSQL_CONN");
    if (string.IsNullOrWhiteSpace(conn))
    {
        Console.Error.WriteLine(JsonSerializer.Serialize(new { level = "error", message = "missing_connection_string", recoverable = false }));
        return 2;
    }
    await using var c = new SqlConnection(conn);
    await c.OpenAsync();
    var componentId = $"db.mssql.{c.Database}";

    // Component node
    Emit("data_entity", new
    {
        id = componentId,
        kind = "database",
        component_id = componentId,
        name = c.Database,
        schema = new { },
        sample_available = false,
        is_sensitive = false,
        certainty = "verified",
        created_by = new[] { SourceName },
    });

    const string sql = @"
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION";
    await using var cmd = new SqlCommand(sql, c);
    await using var rdr = await cmd.ExecuteReaderAsync();

    var tables = new Dictionary<string, List<object>>();
    while (await rdr.ReadAsync())
    {
        var key = $"{rdr.GetString(0)}.{rdr.GetString(1)}";
        if (!tables.TryGetValue(key, out var cols)) { cols = new(); tables[key] = cols; }
        cols.Add(new
        {
            name = rdr.GetString(2),
            type = rdr.GetString(3),
            nullable = rdr.GetString(4) == "YES",
        });
    }
    foreach (var (fqn, cols) in tables)
    {
        var id = $"{componentId}.{fqn}";
        Emit("data_entity", new
        {
            id,
            kind = "table",
            component_id = componentId,
            name = fqn,
            schema = new { columns = cols },
            sample_available = false,
            is_sensitive = false,
            certainty = "verified",
            created_by = new[] { SourceName },
        });
    }
    return 0;
}

static async Task<int> SampleAsync(string[] args)
{
    var conn = FindConnArg(args, "--conn") ?? Environment.GetEnvironmentVariable("MNEMOS_MSSQL_CONN");
    var table = FindConnArg(args, "--table");
    var limitStr = FindConnArg(args, "--limit") ?? "10";
    if (string.IsNullOrWhiteSpace(conn) || string.IsNullOrWhiteSpace(table))
    {
        Console.Error.WriteLine("sample requires --conn <...> --table <schema.name>");
        return 2;
    }
    var limit = int.TryParse(limitStr, out var n) ? Math.Clamp(n, 1, 100) : 10;

    await using var c = new SqlConnection(conn);
    await c.OpenAsync();
    var sql = $"SELECT TOP ({limit}) * FROM {table}";
    await using var cmd = new SqlCommand(sql, c) { CommandTimeout = 10 };
    await using var rdr = await cmd.ExecuteReaderAsync();

    var rows = new List<object[]>();
    var cols = new List<object>();
    for (int i = 0; i < rdr.FieldCount; i++)
        cols.Add(new { name = rdr.GetName(i), type = rdr.GetDataTypeName(i) });

    while (await rdr.ReadAsync())
    {
        var row = new object?[rdr.FieldCount];
        for (int i = 0; i < rdr.FieldCount; i++)
            row[i] = rdr.IsDBNull(i) ? null : rdr.GetValue(i)?.ToString();
        rows.Add(row!);
    }
    Emit("sample", new { table, columns = cols, rows });
    return 0;
}

static async Task<int> QueryAsync(string[] args)
{
    var conn = FindConnArg(args, "--conn") ?? Environment.GetEnvironmentVariable("MNEMOS_MSSQL_CONN");
    var sqlFile = FindConnArg(args, "--sql-file");
    if (string.IsNullOrWhiteSpace(conn) || string.IsNullOrWhiteSpace(sqlFile))
    {
        Console.Error.WriteLine("query requires --conn <...> --sql-file <path>");
        return 2;
    }
    var sql = await File.ReadAllTextAsync(sqlFile);
    if (!sql.TrimStart().StartsWith("select", StringComparison.OrdinalIgnoreCase))
    {
        Console.Error.WriteLine(JsonSerializer.Serialize(new { level = "error", message = "only_select_allowed", recoverable = false }));
        return 2;
    }
    await using var c = new SqlConnection(conn);
    await c.OpenAsync();
    await using var cmd = new SqlCommand(sql, c) { CommandTimeout = 30 };
    await using var rdr = await cmd.ExecuteReaderAsync();

    var cols = new List<object>();
    for (int i = 0; i < rdr.FieldCount; i++)
        cols.Add(new { name = rdr.GetName(i), type = rdr.GetDataTypeName(i) });
    var rows = new List<object[]>();
    while (await rdr.ReadAsync())
    {
        var row = new object?[rdr.FieldCount];
        for (int i = 0; i < rdr.FieldCount; i++)
            row[i] = rdr.IsDBNull(i) ? null : rdr.GetValue(i)?.ToString();
        rows.Add(row!);
    }
    Emit("query_result", new { columns = cols, rows });
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
