<#
.SYNOPSIS
  Registers qdrant.exe as an auto-starting Windows Service via NSSM, storing
  data on a plain local disk path -- NOT under a OneDrive/KFM-synced profile
  folder (flagged in this project's audit history as a real data-corruption
  risk; don't point -DataDir at anything under a user profile that syncs).

  NSSM is required -- qdrant.exe is a plain console application that does not
  implement the Windows Service Control Protocol, so registering it directly
  via sc.exe create does NOT work: SCM launches the process, gets no response
  on the service control channel, and fails after a 30s timeout (Event ID
  7000/7009, "did not respond to the start or control request in a timely
  fashion"). This was discovered the hard way on a real run of an earlier
  version of this script, which tried sc.exe create directly. NSSM wraps any
  console exe and correctly speaks the SCM protocol on its behalf.

  Download NSSM first: https://nssm.cc/download (get nssm.exe for your
  architecture -- usually the win64 build -- and note its path).

.PARAMETER QdrantExe
  Full path to qdrant.exe on this machine.

.PARAMETER NssmExe
  Full path to nssm.exe on this machine.

.PARAMETER DataDir
  Directory to hold storage/ and snapshots/ (and the service's own log file).
  Created if it doesn't exist. Must NOT be under a OneDrive-synced path.

.PARAMETER ServiceName
  Windows service name to register. Default: Qdrant.

.EXAMPLE
  .\install_qdrant_service.ps1 -QdrantExe "C:\qdrant\qdrant.exe" -NssmExe "C:\nssm\nssm.exe" -DataDir "C:\local-rag-data\qdrant"

.NOTES
  Run as Administrator. The NSSM multi-value AppEnvironmentExtra syntax used
  below (space-separated KEY=VALUE arguments in one `nssm set` call) is
  sourced from https://nssm.cc/usage and https://nssm.cc/commands -- verified
  against NSSM's own docs, not assumed. The qdrant.exe env vars themselves
  (QDRANT__STORAGE__*, QDRANT__SERVICE__*) were verified directly against a
  real qdrant.exe process before this script was written. What's NOT been
  verified end-to-end is this exact script against a real NSSM install --
  this is still its first real run. Read it before running.
#>
param(
    [Parameter(Mandatory = $true)][string]$QdrantExe,
    [Parameter(Mandatory = $true)][string]$NssmExe,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$ServiceName = "Qdrant",
    [int]$HttpPort = 6333,
    [int]$GrpcPort = 6334
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $QdrantExe)) {
    throw "qdrant.exe not found at '$QdrantExe'. Download it and pass the real path via -QdrantExe."
}
if (-not (Test-Path $NssmExe)) {
    throw "nssm.exe not found at '$NssmExe'. Download it from https://nssm.cc/download and pass the real path via -NssmExe."
}

if ($DataDir -match '\\OneDrive' -or $DataDir -match '\\Desktop\\' -or $DataDir -match '\\Documents\\') {
    throw "DataDir '$DataDir' looks like it's under a synced/redirected folder (OneDrive/Desktop/Documents). " +
          "Pick a plain local path (e.g. C:\local-rag-data) -- KFM sync has corrupted Qdrant storage before."
}

$storagePath   = Join-Path $DataDir "storage"
$snapshotsPath = Join-Path $DataDir "snapshots"
$logPath       = Join-Path $DataDir "qdrant-service.log"
New-Item -ItemType Directory -Force -Path $storagePath   | Out-Null
New-Item -ItemType Directory -Force -Path $snapshotsPath | Out-Null
Write-Host "Data directories ready: $storagePath , $snapshotsPath"

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service '$ServiceName' already exists -- removing it to reconfigure."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    # sc.exe delete works regardless of how the service was originally
    # registered (sc.exe create or nssm install) -- safe either way, unlike
    # `nssm remove`, which expects NSSM's own registry structure to already
    # be present (not true if an earlier non-NSSM version of this script ran).
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host "Installing '$ServiceName' via NSSM..."
& $NssmExe install $ServiceName $QdrantExe
if ($LASTEXITCODE -ne 0) {
    throw "nssm install failed (exit $LASTEXITCODE). Run it manually to see the real error: `"$NssmExe`" install $ServiceName `"$QdrantExe`""
}

& $NssmExe set $ServiceName AppDirectory (Split-Path $QdrantExe -Parent)
& $NssmExe set $ServiceName AppEnvironmentExtra `
    "QDRANT__STORAGE__STORAGE_PATH=$storagePath" `
    "QDRANT__STORAGE__SNAPSHOTS_PATH=$snapshotsPath" `
    "QDRANT__SERVICE__HTTP_PORT=$HttpPort" `
    "QDRANT__SERVICE__GRPC_PORT=$GrpcPort"
& $NssmExe set $ServiceName AppStdout $logPath
& $NssmExe set $ServiceName AppStderr $logPath
& $NssmExe set $ServiceName Start SERVICE_AUTO_START
& $NssmExe set $ServiceName DisplayName "Qdrant Vector DB"
& $NssmExe set $ServiceName Description "Qdrant vector database for local-rag (data_dictionary + documents collections), run via NSSM"

Write-Host "Starting service '$ServiceName'..."
Start-Service -Name $ServiceName
Start-Sleep -Seconds 3

try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$HttpPort/collections" -TimeoutSec 10
    Write-Host "Qdrant is up. Collections: $($resp.result.collections.name -join ', ')"
    Write-Host "(Expect this to be empty on a fresh server -- re-run ingest.py / ingest_docs.py against this Qdrant to populate data_dictionary / documents.)"
} catch {
    Write-Warning "Service started but didn't respond on port $HttpPort within 10s -- check 'Get-Service $ServiceName' and the service's own log at $logPath (that log is new: it didn't exist under the old sc.exe-based approach, since a non-service-aware process never actually ran long enough to log anything useful)."
}

Write-Host ""
Write-Host "Done. Service '$ServiceName' is set to auto-start on boot."
Write-Host "Point the app's .env at QDRANT_HOST=127.0.0.1 (or this machine's address if remote)."
