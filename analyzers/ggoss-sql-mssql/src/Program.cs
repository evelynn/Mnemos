using System.Text.Json;
using Microsoft.Data.SqlClient;

const string SourceName = "ggoss-sql-mssql";
const string SourceVersion = "1.0.0";

if (args.Length == 0)
{
    Console.Error.WriteLine("usage: ggoss-sql-mssql <probe|inventory|live_schema|live_stats|sample|query|db_probe|schema> [args...]");
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
        "db_probe"    => await DbProbeAsync(rest),
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

// A table name is interpolated into a FROM clause (an identifier can't
// be a bind parameter), so the only safe defence is to reject anything
// that isn't a plain, optionally schema-qualified, identifier.
static bool IsValidTableName(string? table) =>
    table is not null &&
    System.Text.RegularExpressions.Regex.IsMatch(
        table, @"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$");

// Hard row cap for the query verb (analyzer-contract §1.0).
static int MaxRows() =>
    int.TryParse(Environment.GetEnvironmentVariable("MNEMOS_MAX_ROWS"), out var n)
        && n > 0 ? n : 10000;

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
    if (!IsValidTableName(table))
    {
        Console.Error.WriteLine(JsonSerializer.Serialize(new { level = "error", message = "invalid_table_name", recoverable = false }));
        return 2;
    }

    await using var c = new SqlConnection(conn);
    await c.OpenAsync();
    // table is a validated plain identifier; limit is a clamped int.
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
    // A trailing ; is tolerated and stripped; an embedded one means a
    // second statement (MSSQL runs batches) and is rejected.
    var trimmed = sql.Trim().TrimEnd(';').Trim();
    if (!trimmed.StartsWith("select", StringComparison.OrdinalIgnoreCase)
        && !trimmed.StartsWith("with", StringComparison.OrdinalIgnoreCase))
    {
        Console.Error.WriteLine(JsonSerializer.Serialize(new { level = "error", message = "only_select_allowed", recoverable = false }));
        return 2;
    }
    if (trimmed.Contains(';'))
    {
        Console.Error.WriteLine(JsonSerializer.Serialize(new { level = "error", message = "multi_statement_not_allowed", recoverable = false }));
        return 2;
    }
    var maxRows = MaxRows();
    await using var c = new SqlConnection(conn);
    await c.OpenAsync();
    await using var cmd = new SqlCommand(sql, c) { CommandTimeout = 30 };
    await using var rdr = await cmd.ExecuteReaderAsync();

    var cols = new List<object>();
    for (int i = 0; i < rdr.FieldCount; i++)
        cols.Add(new { name = rdr.GetName(i), type = rdr.GetDataTypeName(i) });
    var rows = new List<object[]>();
    var truncated = false;
    while (await rdr.ReadAsync())
    {
        if (rows.Count >= maxRows) { truncated = true; break; }
        var row = new object?[rdr.FieldCount];
        for (int i = 0; i < rdr.FieldCount; i++)
            row[i] = rdr.IsDBNull(i) ? null : rdr.GetValue(i)?.ToString();
        rows.Add(row!);
    }
    Emit("query_result", new { columns = cols, rows, rows_truncated = truncated });
    return 0;
}

static async Task<int> DbProbeAsync(string[] args)
{
    // Metadata-only read-only credential probe (spec §2.5, §2.8).
    // Does NOT execute INSERT/UPDATE/DELETE — not even rolled-back ones.
    // SQL Server captures rolled-back DML in the transaction log and may
    // fire triggers (audit, mail) that ROLLBACK cannot undo. We never
    // leave a trace in the live DB.
    var stopwatch = System.Diagnostics.Stopwatch.StartNew();
    var conn = FindConnArg(args, "--conn")
               ?? Environment.GetEnvironmentVariable("MNEMOS_DB_CONN")
               ?? Environment.GetEnvironmentVariable("MNEMOS_MSSQL_CONN");
    if (string.IsNullOrWhiteSpace(conn))
    {
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            status = "fail_connect",
            connect_ok = false,
            read_ok = false,
            write_blocked = (bool?)null,
            grants = new Dictionary<string, string[]>(),
            facts = Array.Empty<object>(),
            latency_ms = 0,
            error = "missing_connection_string",
        }));
        return 0;
    }

    var grants = new Dictionary<string, List<string>>();
    var facts = new List<object>();
    var writePrivs = new HashSet<string>(new[] {
        "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE TABLE",
        "ALTER", "DROP TABLE", "TRUNCATE",
    });

    try
    {
        await using var c = new SqlConnection(conn);
        await c.OpenAsync();

        // Engine banner (metadata only).
        await using (var cmd = new SqlCommand("SELECT @@VERSION", c) { CommandTimeout = 5 })
        {
            var version = (await cmd.ExecuteScalarAsync())?.ToString();
            if (!string.IsNullOrEmpty(version))
            {
                facts.Add(new
                {
                    value = version!.Split('\n')[0].Trim(),
                    source = "@@VERSION",
                    confidence = "verified",
                });
            }
        }

        await using (var cmd = new SqlCommand("SELECT 1", c) { CommandTimeout = 5 })
        {
            await cmd.ExecuteScalarAsync();
        }

        // Effective permissions for the current login. ``fn_my_permissions``
        // returns rows like (entity_name, subentity_name, permission_name,
        // class, ...). NULL entity → server-wide; entity set → per-object.
        const string permSql = @"
            SELECT permission_name, ISNULL(entity_name, '') AS entity
              FROM fn_my_permissions(NULL, 'DATABASE')";
        await using (var cmd = new SqlCommand(permSql, c) { CommandTimeout = 10 })
        await using (var rdr = await cmd.ExecuteReaderAsync())
        {
            while (await rdr.ReadAsync())
            {
                var permission = rdr.GetString(0).ToUpperInvariant();
                var entity = rdr.IsDBNull(1) ? "__database__" : rdr.GetString(1);
                if (!grants.TryGetValue(entity, out var perms))
                {
                    perms = new List<string>();
                    grants[entity] = perms;
                }
                perms.Add(permission);
            }
        }
        facts.Add(new
        {
            value = new { entities = grants.Count },
            source = "fn_my_permissions",
            confidence = "verified",
        });

        // Role membership — db_owner alone is enough to write anything.
        await using (var cmd = new SqlCommand(
            "SELECT name FROM sys.database_role_members rm "
            + "JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id "
            + "WHERE rm.member_principal_id = DATABASE_PRINCIPAL_ID()", c) { CommandTimeout = 5 })
        await using (var rdr = await cmd.ExecuteReaderAsync())
        {
            var roles = new List<string>();
            while (await rdr.ReadAsync())
                roles.Add(rdr.GetString(0));
            if (roles.Count > 0)
            {
                grants["__roles__"] = roles;
                facts.Add(new
                {
                    value = roles,
                    source = "sys.database_role_members",
                    confidence = "verified",
                });
            }
        }
    }
    catch (Exception ex)
    {
        stopwatch.Stop();
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            status = "fail_connect",
            connect_ok = false,
            read_ok = false,
            write_blocked = (bool?)null,
            grants = new Dictionary<string, string[]>(),
            facts,
            latency_ms = (int)stopwatch.ElapsedMilliseconds,
            error = $"connect_failed: {ex.Message}",
        }));
        return 0;
    }

    stopwatch.Stop();

    var heldWrite = grants.Values
        .SelectMany(p => p)
        .Where(p => writePrivs.Contains(p) || p == "DB_OWNER")
        .Distinct()
        .ToList();
    var roles = grants.TryGetValue("__roles__", out var rl) ? rl : new List<string>();
    var writeBlocked = heldWrite.Count == 0
                       && !roles.Any(r => r == "db_owner" || r == "db_datawriter" || r == "db_ddladmin");

    facts.Add(new
    {
        value = writeBlocked ? "no write privileges held" : (object)heldWrite,
        source = "verdict",
        confidence = "verified",
    });

    Console.WriteLine(JsonSerializer.Serialize(new
    {
        status = writeBlocked ? "pass" : "fail_rw",
        connect_ok = true,
        read_ok = true,
        write_blocked = writeBlocked,
        grants,
        facts,
        latency_ms = (int)stopwatch.ElapsedMilliseconds,
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
