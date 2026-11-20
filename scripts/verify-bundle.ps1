<#
.SYNOPSIS
  Verify a LocalOps offline bundle on a Windows transfer machine.

.DESCRIPTION
  Checks the archive against its .sha256 file, or an extracted bundle directory against
  checksums/SHA256SUMS. Installation itself runs on the Linux host (scripts/install.sh).

.EXAMPLE
  .\verify-bundle.ps1 -Archive .\localops-airgap-1.0.0-linux-amd64.tar.gz
.EXAMPLE
  .\verify-bundle.ps1 -BundleDir .\localops-airgap-1.0.0-linux-amd64
#>
[CmdletBinding(DefaultParameterSetName = 'Dir')]
param(
  [Parameter(ParameterSetName = 'Archive', Mandatory = $true)][string]$Archive,
  [Parameter(ParameterSetName = 'Dir')][string]$BundleDir = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = 'Stop'

function Get-Sha256([string]$Path) {
  (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

if ($PSCmdlet.ParameterSetName -eq 'Archive') {
  $sumFile = "$Archive.sha256"
  if (-not (Test-Path -LiteralPath $sumFile)) { throw "Missing $sumFile next to the archive" }
  $expected = ((Get-Content -LiteralPath $sumFile -TotalCount 1) -split '\s+')[0].ToLowerInvariant()
  $actual = Get-Sha256 $Archive
  if ($expected -ne $actual) {
    Write-Error "Checksum mismatch for $Archive`n  expected $expected`n  actual   $actual"
    exit 1
  }
  Write-Host "OK  $Archive matches its .sha256" -ForegroundColor Green
  exit 0
}

$sums = Join-Path $BundleDir 'checksums/SHA256SUMS'
if (-not (Test-Path -LiteralPath $sums)) { throw "$BundleDir is not a LocalOps bundle (no checksums/SHA256SUMS)" }
$failures = 0
$count = 0
foreach ($line in Get-Content -LiteralPath $sums) {
  if (-not $line.Trim()) { continue }
  $hash, $rel = $line -split '\s+', 2
  $rel = $rel.TrimStart('*')
  $path = Join-Path $BundleDir $rel
  $count++
  if (-not (Test-Path -LiteralPath $path)) {
    Write-Host "FAIL missing $rel" -ForegroundColor Red; $failures++; continue
  }
  if ((Get-Sha256 $path) -ne $hash.ToLowerInvariant()) {
    Write-Host "FAIL checksum mismatch: $rel" -ForegroundColor Red; $failures++
  }
}
if ($failures -gt 0) {
  Write-Error "$failures of $count files failed verification"
  exit 1
}
Write-Host "OK  all $count files match checksums/SHA256SUMS" -ForegroundColor Green
