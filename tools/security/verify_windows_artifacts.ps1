param(
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [Parameter(Mandatory = $true)]
    [string]$HashOutput,

    [string]$ReportOutput
)

$ErrorActionPreference = "Stop"

$target = (Resolve-Path -LiteralPath $Path).Path
$hashOutputFull = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $HashOutput))
$hashOutputDir = Split-Path -Parent $hashOutputFull
New-Item -ItemType Directory -Force $hashOutputDir | Out-Null

$reportOutputFull = $null
$reportLines = [System.Collections.Generic.List[string]]::new()
if ($ReportOutput) {
    $reportOutputFull = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $ReportOutput))
    $reportOutputDir = Split-Path -Parent $reportOutputFull
    New-Item -ItemType Directory -Force $reportOutputDir | Out-Null
}

function Write-ReportLine {
    param([string]$Text)
    Write-Host $Text
    if ($script:reportOutputFull) {
        $script:reportLines.Add($Text)
    }
}

function Save-Report {
    if ($script:reportOutputFull) {
        [System.IO.File]::WriteAllLines(
            $script:reportOutputFull,
            $script:reportLines,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
}

Write-ReportLine "H145 Local OSM - Microsoft Defender artifact verification"
Write-ReportLine "Commit: $env:GITHUB_SHA"
Write-ReportLine "UTC started: $([DateTimeOffset]::UtcNow.ToString('u'))"
Write-ReportLine "Scan target: $target"
Write-ReportLine ""

# GitHub-hosted Windows runners include Microsoft Defender Antivirus. Treat
# a disabled/unavailable Defender service as a hard failure: publishing an
# unscanned binary would defeat the purpose of this release gate.
$status = Get-MpComputerStatus
Write-ReportLine "Antivirus enabled: $($status.AntivirusEnabled)"
Write-ReportLine "Real-time protection enabled: $($status.RealTimeProtectionEnabled)"
Write-ReportLine "Defender engine: $($status.AMEngineVersion)"
Write-ReportLine "Defender platform: $($status.AMProductVersion)"
Write-ReportLine "Defender signatures before update: $($status.AntivirusSignatureVersion)"
Write-ReportLine "Signature timestamp before update: $($status.AntivirusSignatureLastUpdated)"

if (-not $status.AntivirusEnabled) {
    Write-ReportLine "Result: FAIL - Microsoft Defender Antivirus is not enabled on this runner."
    Save-Report
    throw "Microsoft Defender Antivirus is not enabled on this runner."
}

try {
    Update-MpSignature | Out-Null
    $updated = Get-MpComputerStatus
    Write-ReportLine "Defender signatures after update: $($updated.AntivirusSignatureVersion)"
    Write-ReportLine "Signature timestamp after update: $($updated.AntivirusSignatureLastUpdated)"
}
catch {
    Write-ReportLine "Result: FAIL - Microsoft Defender signature update failed: $($_.Exception.Message)"
    Save-Report
    throw "Microsoft Defender signature update failed: $($_.Exception.Message)"
}

# Capture any detections that pre-date this build so the test only fails for
# detections created by scanning the artifact directory.
$beforeKeys = @{}
foreach ($detection in @(Get-MpThreatDetection -ErrorAction SilentlyContinue)) {
    $resources = (@($detection.Resources) -join "|")
    $key = "$($detection.ThreatID)|$resources|$($detection.InitialDetectionTime)"
    $beforeKeys[$key] = $true
}

Write-ReportLine ""
Write-ReportLine "Running Microsoft Defender custom scan..."
$scanStarted = [DateTimeOffset]::UtcNow
Start-MpScan -ScanType CustomScan -ScanPath $target
$scanFinished = [DateTimeOffset]::UtcNow
Write-ReportLine "Scan started UTC: $($scanStarted.ToString('u'))"
Write-ReportLine "Scan finished UTC: $($scanFinished.ToString('u'))"

$newDetections = @()
foreach ($detection in @(Get-MpThreatDetection -ErrorAction SilentlyContinue)) {
    $resources = (@($detection.Resources) -join "|")
    $key = "$($detection.ThreatID)|$resources|$($detection.InitialDetectionTime)"
    if (-not $beforeKeys.ContainsKey($key)) {
        $newDetections += $detection
    }
}

Write-ReportLine "New detections: $($newDetections.Count)"
if ($newDetections.Count -gt 0) {
    foreach ($detection in $newDetections) {
        Write-ReportLine "Threat ID: $($detection.ThreatID)"
        foreach ($resource in @($detection.Resources)) {
            Write-ReportLine "  $resource"
        }
    }
    Write-ReportLine "Result: FAIL - Defender detected one or more threats in the release artifacts."
    Save-Report
    throw "Artifact malware scan failed."
}

Write-ReportLine "Result: PASS - Microsoft Defender reported no new detections."

# Hash the Windows executables and distributable archives that users actually
# consume. .pyd files are native Python extension modules and are therefore
# treated as binaries just like .exe and .dll.
$extensions = @(".exe", ".dll", ".pyd", ".zip")
$files = @(
    Get-ChildItem -LiteralPath $target -Recurse -File |
        Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() } |
        Sort-Object FullName
)

if ($files.Count -eq 0) {
    Write-ReportLine "Hash result: FAIL - no executable/archive files found."
    Save-Report
    throw "No executable/archive files were found under $target; refusing to create an empty hash manifest."
}

$lines = foreach ($file in $files) {
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $relative = [System.IO.Path]::GetRelativePath($target, $file.FullName).Replace("\", "/")
    "$hash  $relative"
}

[System.IO.File]::WriteAllLines(
    $hashOutputFull,
    $lines,
    [System.Text.UTF8Encoding]::new($false)
)

Write-ReportLine ""
Write-ReportLine "SHA-256 manifest: $hashOutputFull"
Write-ReportLine "Hashed files: $($files.Count)"
Write-ReportLine "UTC completed: $([DateTimeOffset]::UtcNow.ToString('u'))"
Save-Report

Write-Host "Wrote SHA-256 manifest: $hashOutputFull"
Get-Content -LiteralPath $hashOutputFull | ForEach-Object { Write-Host $_ }
if ($reportOutputFull) {
    Write-Host "Wrote Defender report: $reportOutputFull"
}
