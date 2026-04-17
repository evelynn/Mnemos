using Mnemos.Analyzers.CSharp;

if (args.Length == 0)
{
    Console.Error.WriteLine("usage: ggoss-csharp <probe|inventory|symbols|calls|contracts|data_access|schema> [args...]");
    return 2;
}

var verb = args[0];
var rest = args.Skip(1).ToArray();
try
{
    return verb switch
    {
        "probe"       => await Commands.ProbeAsync(rest),
        "inventory"   => await Commands.InventoryAsync(rest),
        "symbols"     => await Commands.SymbolsAsync(rest),
        "calls"       => await Commands.CallsAsync(rest),
        "contracts"   => await Commands.ContractsAsync(rest),
        "data_access" => await Commands.DataAccessAsync(rest),
        "schema"      => Commands.Schema(),
        _             => UnknownVerb(verb)
    };
}
catch (Exception ex)
{
    Console.Error.WriteLine(System.Text.Json.JsonSerializer.Serialize(new
    {
        level = "error",
        message = ex.Message,
        type = ex.GetType().FullName,
        recoverable = false
    }));
    return 1;
}

static int UnknownVerb(string verb)
{
    Console.Error.WriteLine($"unknown verb: {verb}");
    return 2;
}
