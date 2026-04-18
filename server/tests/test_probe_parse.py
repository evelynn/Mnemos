"""Connection-string parsing for the secret TCP probe."""

from app.safety.probe import parse_endpoint


def test_mssql_ado_server_comma_port():
    assert parse_endpoint("mssql", "Server=db.example.com,1433;Database=X;") == (
        "db.example.com",
        1433,
    )


def test_mssql_ado_server_host_only_defaults_port():
    host, port = parse_endpoint("mssql", "Server=db.example.com;Database=X;")
    assert host == "db.example.com"
    assert port == 1433


def test_mssql_ado_named_instance_is_stripped():
    host, port = parse_endpoint("mssql", "Server=db.example.com\\SQL2019,1434;")
    assert host == "db.example.com"
    assert port == 1434


def test_oracle_easyconnect_host_port_service():
    assert parse_endpoint("oracle", "//db.example.com:1522/ORCL") == (
        "db.example.com",
        1522,
    )


def test_oracle_easyconnect_host_service_default_port():
    host, port = parse_endpoint("oracle", "db.example.com/ORCL")
    assert host == "db.example.com"
    assert port == 1521


def test_mssql_url_scheme():
    assert parse_endpoint("mssql", "mssql://user:pw@db.example.com:1450/app") == (
        "db.example.com",
        1450,
    )


def test_unparseable_returns_none():
    assert parse_endpoint("mssql", "definitely not a connection string!") is None
    assert parse_endpoint("mssql", "") is None
