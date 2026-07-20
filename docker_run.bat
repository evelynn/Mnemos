@echo off
REM ==========================================================================
REM  Mnemos - Docker Compose launcher (Windows)
REM
REM  run.bat is the no-Docker local launcher.
REM  Stop Docker services with: docker compose down
REM ==========================================================================
setlocal
cd /d "%~dp0"

where docker >nul 2>&1
if errorlevel 1 (
    echo [Mnemos] ERROR: Docker CLI was not found on PATH.
    echo [Mnemos] Install/start Docker Desktop, then run this file again.
    exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
    echo [Mnemos] ERROR: Docker is not running. Start Docker Desktop first.
    exit /b 1
)

if not exist ".env" (
    if not exist ".env.example" (
        echo [Mnemos] ERROR: .env.example was not found.
        exit /b 1
    )
    echo [Mnemos] Creating .env from .env.example...
    copy ".env.example" ".env" >nul
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$p='.env';" ^
        "$s=Get-Content -Raw -LiteralPath $p;" ^
        "$secret=python -c \"import secrets; print(secrets.token_urlsafe(48))\";" ^
        "$fernet=python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\";" ^
        "$s=$s -replace 'SECRET_KEY=change-me-to-a-random-32-byte-secret',('SECRET_KEY='+$secret);" ^
        "$s=$s -replace 'FERNET_KEY=',('FERNET_KEY='+$fernet);" ^
        "Set-Content -LiteralPath $p -Value $s -Encoding UTF8"
    if errorlevel 1 (
        echo [Mnemos] Failed to populate .env.
        exit /b 1
    )
)

echo [Mnemos] Building Docker images...
docker compose build
if errorlevel 1 (
    echo [Mnemos] docker compose build failed.
    exit /b 1
)

echo [Mnemos] Starting Docker services...
docker compose up -d
if errorlevel 1 (
    echo [Mnemos] docker compose up failed.
    exit /b 1
)

echo [Mnemos] Waiting for platform health...
set /a RETRY=0
:HEALTH_LOOP
set /a RETRY+=1
if %RETRY% gtr 12 (
    echo [Mnemos] WARNING: platform did not respond within 60 seconds.
    echo [Mnemos] Check logs with: docker compose logs platform
    goto SKIP_MIGRATION
)
timeout /t 5 /nobreak >nul
curl -sf http://localhost:16401/api/v1/health >nul 2>&1
if errorlevel 1 goto HEALTH_LOOP
echo [Mnemos] Platform responded after %RETRY% checks.

echo [Mnemos] Applying database migrations...
docker compose exec platform alembic upgrade head
if errorlevel 1 (
    echo [Mnemos] WARNING: migration failed or database is already current.
)

:SKIP_MIGRATION
echo.
echo [Mnemos] Docker startup complete
echo Dashboard:   http://localhost:16401
echo Healthcheck: http://localhost:16401/api/v1/health/ready
echo Stop:        docker compose down
echo.
