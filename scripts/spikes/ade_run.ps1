# Run ADE-bench's harness from a Linux container on a Windows host. No model calls unless
# you pass a model-backed agent, which this script refuses to do.
#
#   pwsh scripts/spikes/ade_run.ps1 analytics_engineering002.base sage
#
# The checkout is mounted at the path Docker Desktop uses for host files
# (/run/desktop/mnt/host/c/...), so the bind mounts the harness asks for resolve to the
# same folder for the runner and for the task containers.
param(
    [Parameter(Mandatory = $true)][string]$Task,
    [ValidateSet('sage', 'none')][string]$Agent = 'sage'
)
$ErrorActionPreference = 'Stop'

$ade = Join-Path $HOME '.verimend\vendor\ade-bench'
if (-not (Test-Path $ade)) { throw "ADE-bench checkout not found at $ade" }
$drive = $ade.Substring(0, 1).ToLower()
$inside = '/run/desktop/mnt/host/' + $drive + ($ade.Substring(2) -replace '\\', '/')

docker build -q -t verimend-ade-runner (Join-Path $PSScriptRoot 'ade_runner') | Out-Null

$script = @"
set -e
cd '$inside'
uv venv /opt/venv --allow-existing -q
uv pip install -q -e .
ade run $Task --db duckdb --project-type dbt --agent $Agent --no-diffs --n-concurrent-trials 1
"@

docker run --rm `
    -v //var/run/docker.sock:/var/run/docker.sock `
    -v "${ade}:${inside}" `
    -v verimend-ade-venv:/opt/venv `
    verimend-ade-runner bash -c ($script -replace "`r", '')
exit $LASTEXITCODE
