@echo off
REM ==========================================================================
REM  Mnemos - local no-Docker launcher (Windows)
REM
REM  Starts Mnemos with SQLite + in-process fakeredis + inline jobs.
REM  Docker Compose launcher lives in docker_run.bat.
REM ==========================================================================
setlocal
cd /d "%~dp0"

set "PORT=16401"
set "DB_PATH=%CD%\data\mnemos-local.db"
set "SEED_FLAG="
set "VENV=%CD%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "UV_CACHE_DIR=%CD%\.uv-cache"
set "UV_PYTHON_INSTALL_DIR=%CD%\.uv-python"
set "UV_LINK_MODE=copy"
set "NPM_CONFIG_CACHE=%CD%\.npm-cache"
set "npm_config_cache=%CD%\.npm-cache"
set "NPM_CONFIG_AUDIT=false"
set "npm_config_audit=false"
set "NPM_CONFIG_FUND=false"
set "npm_config_fund=false"
set "NPM_CONFIG_STRICT_SSL=false"
set "npm_config_strict_ssl=false"
set "NPM_CONFIG_REGISTRY=https://registry.yarnpkg.com"
set "npm_config_registry=https://registry.yarnpkg.com"
set "NPM_CONFIG_REPLACE_REGISTRY_HOST=always"
set "npm_config_replace_registry_host=always"

where uv >nul 2>&1
if errorlevel 1 (
    echo [Mnemos] ERROR: uv was not found on PATH.
    echo [Mnemos] Install uv or add it to PATH, then run this file again.
    exit /b 1
)

if not exist "data" mkdir data
if not exist "%DB_PATH%" set "SEED_FLAG=--seed-demo"

if not exist "%PY%" (
    echo [Mnemos] Creating local Python environment...
    uv venv "%VENV%" --python 3.12 --managed-python
    if errorlevel 1 (
        echo [Mnemos] Failed to create Python 3.12 virtual environment.
        exit /b 1
    )
)

echo [Mnemos] Installing/updating local dependencies...
uv pip install --python "%PY%" -e ".\server[local]"
if errorlevel 1 (
    echo [Mnemos] Dependency install failed.
    exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    echo [Mnemos] WARNING: Node.js was not found. TypeScript analyzer will be unavailable.
    goto SKIP_TS_ANALYZER
)

where npm >nul 2>&1
if errorlevel 1 (
    echo [Mnemos] WARNING: npm was not found. TypeScript analyzer will be unavailable.
    goto SKIP_TS_ANALYZER
)

if not exist "%CD%\analyzers\ggoss-ts\node_modules\typescript\bin\tsc" (
    echo [Mnemos] Installing local TypeScript analyzer dependencies...
    pushd "%CD%\analyzers\ggoss-ts"
    call npm ci
    if errorlevel 1 (
        popd
        echo [Mnemos] TypeScript analyzer install failed.
        exit /b 1
    )
    popd
)

if not exist "%CD%\analyzers\ggoss-ts\node_modules\.bin" mkdir "%CD%\analyzers\ggoss-ts\node_modules\.bin"
if not exist "%CD%\analyzers\ggoss-ts\node_modules\.bin\ggoss-ts.cmd" (
    echo [Mnemos] Creating local ggoss-ts command shim...
    > "%CD%\analyzers\ggoss-ts\node_modules\.bin\ggoss-ts.cmd" echo @echo off
    >> "%CD%\analyzers\ggoss-ts\node_modules\.bin\ggoss-ts.cmd" echo node "%%~dp0..\..\src\index.mjs" %%*
)

set "PATH=%CD%\analyzers\ggoss-ts\node_modules\.bin;%PATH%"

:SKIP_TS_ANALYZER
echo [Mnemos] Starting local server: http://localhost:%PORT%
cd /d "%~dp0server"
"%PY%" -m app.serve_local --host 127.0.0.1 --port %PORT% --db "%DB_PATH%" %SEED_FLAG%
