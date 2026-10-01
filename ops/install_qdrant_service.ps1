<#
.SYNOPSIS
  Registers qdrant.exe as an auto-starting Windows Service, storing data on a
  plain local disk path -- NOT under a OneDrive/KFM-synced profile folder
  (flagged in this project's audit history as a real data-corruption risk;
  don't point -DataDir at anything under a user profile that syncs).

  Native tools only (sc.exe + the registry) -- no NSSM or other extra tooling.
  Qdrant reads its storage/snapshots paths from the QDRANT__STORAGE__* env
  vars (its documented config-override mechanism); this script sets them on
  the service itself via the registry Environment value, since sc.exe has no
  direct flag for per-service environment variables.

.PARAMETER QdrantExe
  Full path to qdrant.exe on this machine.

.PARAMETER DataDir
  Directory to hold storage/ and snapshots/. Created if it doesn't exist.
  Must NOT be under a OneDrive-synced path.

.PARAMETER ServiceName
  Windows service name to register. Default: Qdrant.

.EXAMPLE
  .\install_qdrant_service.ps1 -QdrantExe "D:\qdrant\qdrant.exe" -DataDir "D:\qdrant\data"

.NOTES
  Run as Administrator. This script is REVIEWED, not executed by the
  assistant that wrote it -- there is no access to the target server. Read
  it before running; adjust the -HttpPort/-GrpcPort defaults if they
  conflict with something already listening on this server.
#>
param(
    [Parameter(Mandatory = $true)][string]$QdrantExe,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$ServiceName = "Qdrant",
    [int]$HttpPort = 6333,
    [int]$GrpcPort = 6334
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $QdrantExe)) {
    throw "qdrant.exe not found at '$QdrantExe'. Download it and pass the real path via -QdrantExe."
}

$resolvedData = (Resolve-Path -LiteralPath (Split-Path $DataDir -Parent) -ErrorAction SilentlyContinue)
if ($DataDir -match '\\OneDrive' -or $DataDir -match '\\Desktop\\' -or $DataDir -match '\\Documents\\') {
    throw "DataDir '$DataDir' looks like it's under a synced/redirected folder (OneDrive/Desktop/Documents). " +
          "Pick a plain local path (e.g. D:\qdrant\data) -- KFM sync has corrupted Qdrant storage before."
}

$storagePath   = Join-Path $DataDir "storage"
$snapshotsPath = Join-Path $DataDir "snapshots"
New-Item -ItemType Directory -Force -Path $storagePath   | Out-Null
New-Item -ItemType Directory -Force -Path $snapshotsPath | Out-Null
Write-Host "Data directories ready: $storagePath , $snapshotsPath"

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service '$ServiceName' already exists -- stopping it to reconfigure."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

# qdrant.exe has NO --http-port/--grpc-port flags (verified against its real
# --help output -- an earlier version of this script invented them, which made
# the service fail to start with a useless generic SCM error, since qdrant.exe
# exits immediately on an unrecognized argument). Ports, like storage paths,
# are config-only -- set below via the QDRANT__SERVICE__* env vars.
$binPath = "`"$QdrantExe`""
sc.exe create $ServiceName binPath= $binPath start= auto DisplayName= "Qdrant Vector DB" | Out-Null
if ($LASTEXITCODE -ne 0) {
    # sc.exe failing doesn't raise a PowerShell exception -- check the exit code
    # explicitly, or the script would carry on into Set-ItemProperty against a
    # registry key for a service that was never actually created.
    throw "sc.exe create failed (exit $LASTEXITCODE). Run 'sc.exe create $ServiceName ...' manually to see the real error."
}
sc.exe description $ServiceName "Qdrant vector database for local-rag (data_dictionary + documents collections)" | Out-Null

# Per-service environment variables, via the registry (no NSSM needed). Verified
# against a real qdrant.exe: QDRANT__SERVICE__HTTP_PORT/GRPC_PORT and
# QDRANT__STORAGE__STORAGE_PATH/SNAPSHOTS_PATH all take effect as expected, with
# no cross-talk against another Qdrant instance running on its own ports/paths.
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
Set-ItemProperty -Path $regPath -Name "Environment" -Type MultiString -Value @(
    "QDRANT__STORAGE__STORAGE_PATH=$storagePath",
    "QDRANT__STORAGE__SNAPSHOTS_PATH=$snapshotsPath",
    "QDRANT__SERVICE__HTTP_PORT=$HttpPort",
    "QDRANT__SERVICE__GRPC_PORT=$GrpcPort"
)

Write-Host "Starting service '$ServiceName'..."
Start-Service -Name $ServiceName
Start-Sleep -Seconds 3

try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$HttpPort/collections" -TimeoutSec 10
    Write-Host "Qdrant is up. Collections: $($resp.result.collections.name -join ', ')"
    Write-Host "(Expect this to be empty on a fresh server -- re-run ingest.py / ingest_docs.py against this Qdrant to populate data_dictionary / documents.)"
} catch {
    Write-Warning "Service started but didn't respond on port $HttpPort within 10s -- check Get-Service $ServiceName and Event Viewer > Application for qdrant's own log output."
}

Write-Host ""
Write-Host "Done. Service '$ServiceName' is set to auto-start on boot."
Write-Host "Point the app's .env at QDRANT_HOST=127.0.0.1 (or this machine's address if remote)."
