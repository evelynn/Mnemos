using System.Text.Json;
using System.Text.Json.Serialization;

namespace Mnemos.Analyzers.CSharp;

/// <summary>
/// Matches the shared analyzer output envelope defined in docs/analyzer-contract.md.
/// </summary>
public sealed class Envelope
{
    public const string SourceName = "ggoss-csharp";
    public const string SourceVersion = "1.0.0";

    [JsonPropertyName("record_type")]
    public string RecordType { get; set; } = "";

    [JsonPropertyName("source_name")]
    public string Source { get; set; } = SourceName;

    [JsonPropertyName("source_version")]
    public string Version { get; set; } = SourceVersion;

    [JsonPropertyName("analyzed_at")]
    public string AnalyzedAt { get; set; } = DateTime.UtcNow.ToString("O");

    [JsonPropertyName("data")]
    public object Data { get; set; } = new();

    private static readonly JsonSerializerOptions s_jsonOpts = new()
    {
        WriteIndented = false,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    public static void WriteLine(TextWriter writer, string recordType, object data)
    {
        var env = new Envelope { RecordType = recordType, Data = data };
        writer.WriteLine(JsonSerializer.Serialize(env, s_jsonOpts));
    }

    public static void ReportError(string file, string message, bool recoverable = true)
    {
        var payload = new { level = "error", file, message, recoverable };
        Console.Error.WriteLine(JsonSerializer.Serialize(payload, s_jsonOpts));
    }
}
