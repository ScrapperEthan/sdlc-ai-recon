@echo off
REM One-command launch for the MDC assistant.
REM
REM Starts BOTH pieces but you only ever open ONE url: the chat (single entry) reverse-proxies the
REM arch/impact/coverage pages and their data endpoints from the retrieval service, which stays on
REM loopback. So share/open just  http://127.0.0.1:8765 .
REM
REM Env you may set first (all optional, inherited if already set):
REM   SDLC_MIRROR   = full HASE_MDC extract path (so the chat reads whole-estate source for citations)
REM   LLM_STREAM=1  = live token streaming     LLM_MODEL=gpt-5.6-terra = internal model
REM   SDLC_PORT     = chat port (default 8765)  RETRIEVAL_PORT = retrieval port (default 8848)

cd /d "%~dp0"

if "%RETRIEVAL_HOST%"=="" set RETRIEVAL_HOST=127.0.0.1
if "%RETRIEVAL_PORT%"=="" set RETRIEVAL_PORT=8848
if "%RETRIEVAL_UPSTREAM_URL%"=="" set RETRIEVAL_UPSTREAM_URL=http://127.0.0.1:%RETRIEVAL_PORT%
if "%SDLC_PORT%"=="" set SDLC_PORT=8765

echo Starting retrieval service (internal) on %RETRIEVAL_HOST%:%RETRIEVAL_PORT% ...
start "MDC retrieval (internal)" cmd /c "python retrieval_service.py"

echo Waiting for the retrieval service to come up ...
timeout /t 2 /nobreak >nul

echo.
echo ============================================================
echo   MDC assistant is the single entry:  http://127.0.0.1:%SDLC_PORT%
echo   (arch / impact / coverage load same-origin via the chat)
echo ============================================================
echo.
python -m webapp.server
