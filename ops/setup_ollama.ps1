<#
.SYNOPSIS
  Sets up Ollama as the chat/reasoning server for local-rag on Windows Server
  (recommended instead of LM Studio for the server role -- see
  SERVER_MIGRATION.md for the rationale: native-feeling background service,
  already used in this project for embeddings, same OpenAI-compatible /v1
  shape llm.py already targets, and OLLAMA_NUM_PARALLEL gives real concurrent
  request handling that LM Studio's GUI-managed parallel setting doesn't).

  This script does NOT install Ollama itself -- download/run the official
  Windows installer first: https://ollama.com/download/windows

  HONESTY NOTE: I have not run this against your actual server (no access).
  Two things below are marked uncertain and need verifying on your box:
    1. Whether Ollama's installer registers a true Windows Service (SERVICES
       consistently, or a per-user Startup-folder background app. Check with
       `Get-Service -Name "*ollama*"` after installing -- if nothing shows up,
       you likely have the per-user-app variant, and for unattended server
       operation (no interactive login) you'll want to wrap it via Task
       Scheduler ("Run whether user is logged on or not") or NSSM instead.
    2. Whether "qwen3.8-27b" exists in Ollama's public model library under
       that exact tag -- it's a newer/niche model. The script tries `ollama
       pull` first and falls back to a manual Modelfile import from a local
       GGUF if that 404s.

.PARAMETER NumParallel
  Value for OLLAMA_NUM_PARALLEL -- how many requests one loaded model can
  serve concurrently. Match to your expected concurrent-user count; higher
  values split the model's context window across more slots (same tradeoff
  this project already hit locally with LM Studio's "Max Concurrent
  Predictions" -- see README §4).

.PARAMETER ModelTag
  Ollama model tag to pull. Default: qwen3.8-27b

.PARAMETER FallbackGgufPath
  If the `ollama pull` fails, path to a local GGUF file to import via a
  Modelfile instead (e.g. a GGUF copied from wherever it was downloaded for
  LM Studio testing). Optional -- only used if the pull fails.

.NOTES
  Run as Administrator. Reviewed, not executed, by the assistant that wrote
  it -- no access to the target server.
#>
param(
    [int]$NumParallel = 4,
    [string]$ModelTag = "qwen3.8-27b",
    [string]$FallbackGgufPath = ""
)

$ErrorActionPreference = "Stop"

$ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollamaCmd) {
    throw "ollama.exe not found on PATH. Install it first: https://ollama.com/download/windows"
}
Write-Host "Found: $($ollamaCmd.Source)"

$svc = Get-Service -Name "*ollama*" -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "Ollama Windows Service detected: $($svc.Name) [$($svc.Status)]"
} else {
    Write-Warning "No 'ollama' Windows Service found -- it's likely running as a per-user background app instead. For an unattended server (no one logged in), that won't survive a reboot without a logged-in session. See the HONESTY NOTE in this script's header for the Task Scheduler / NSSM alternative."
}

# OLLAMA_NUM_PARALLEL at machine scope so it applies regardless of how Ollama
# is actually running (service or per-user app) -- requires restarting Ollama
# (the service, or sign out/in for the per-user app) to take effect.
[Environment]::SetEnvironmentVariable("OLLAMA_NUM_PARALLEL", "$NumParallel", "Machine")
Write-Host "Set OLLAMA_NUM_PARALLEL=$NumParallel at machine scope. Restart Ollama for it to take effect:"
Write-Host "  - if it's a Service: Restart-Service <name>"
Write-Host "  - if it's the per-user app: quit it from the tray icon and relaunch (or sign out/in)"

Write-Host ""
Write-Host "Pulling model '$ModelTag'..."
# A failed native-exe call does NOT raise a terminating error that try/catch
# would see (that only fires on PowerShell's own errors) -- check the actual
# exit code instead, or this always looks like it succeeded.
& ollama pull $ModelTag
$pullOk = ($LASTEXITCODE -eq 0)

if (-not $pullOk) {
    Write-Warning "'ollama pull $ModelTag' failed -- '$ModelTag' likely isn't in Ollama's public library under that name."
    if ($FallbackGgufPath -and (Test-Path $FallbackGgufPath)) {
        Write-Host "Falling back to a manual Modelfile import from: $FallbackGgufPath"
        $modelfileDir = Join-Path $env:TEMP "ollama_modelfile_$ModelTag"
        New-Item -ItemType Directory -Force -Path $modelfileDir | Out-Null
        $modelfilePath = Join-Path $modelfileDir "Modelfile"
        "FROM $FallbackGgufPath" | Set-Content -Path $modelfilePath -Encoding ASCII
        Write-Host "Wrote $modelfilePath -- creating Ollama model '$ModelTag' from it..."
        & ollama create $ModelTag -f $modelfilePath
    } else {
        Write-Warning "No -FallbackGgufPath given (or the path doesn't exist). Download the GGUF manually and re-run with -FallbackGgufPath, or build a Modelfile yourself: https://github.com/ollama/ollama/blob/main/docs/modelfile.md"
    }
}

Write-Host ""
Write-Host "Verifying the model is servable via the OpenAI-compatible endpoint..."
Start-Sleep -Seconds 2
try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:11434/v1/models" -TimeoutSec 10
    $names = $resp.data.id -join ", "
    Write-Host "Models available: $names"
    if ($names -notmatch [regex]::Escape($ModelTag)) {
        Write-Warning "'$ModelTag' is not in the /v1/models list above -- something didn't complete. Check 'ollama list'."
    } else {
        Write-Host "'$ModelTag' is servable. Set REASON_MODEL=$ModelTag and REASON_BASE_URL=http://127.0.0.1:11434/v1 in .env."
    }
} catch {
    Write-Warning "Couldn't reach http://127.0.0.1:11434/v1/models -- is Ollama actually running? ('ollama serve' runs it in the foreground for a quick manual check.)"
}
