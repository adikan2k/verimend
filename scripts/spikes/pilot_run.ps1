# Run the phase 1 pilot (scripts/spikes/pilot.py) inside the Linux runner. PAID: it calls
# the model. Both caps are required and passed through to the batch guard.
#
#   powershell -File scripts/spikes/pilot_run.ps1 -Limit 3 -MaxUsd 0.75
param(
    [Parameter(Mandatory = $true)][int]$Limit,
    [Parameter(Mandatory = $true)][double]$MaxUsd
)
$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ade = Join-Path $HOME '.verimend\vendor\ade-bench'
if (-not (Test-Path $ade)) { throw "ADE-bench checkout not found at $ade" }
$drive = $ade.Substring(0, 1).ToLower()
$inside = '/run/desktop/mnt/host/' + $drive + ($ade.Substring(2) -replace '\\', '/')
$sha = (git -C $repo rev-parse --short HEAD).Trim()

docker build -q -t verimend-ade-runner (Join-Path $PSScriptRoot 'ade_runner') | Out-Null

$script = @"
set -e
cd '$inside'
uv venv /opt/venv --allow-existing -q
uv pip install -q -e . -e /verimend/verifier -e /verimend
python /verimend/scripts/spikes/pilot.py --limit $Limit --max-usd $MaxUsd
"@

docker run --rm `
    -e VERIMEND_GIT_SHA=$sha `
    -e DEFAULT_AGENT_TIMEOUT_SEC=900 `
    -v //var/run/docker.sock:/var/run/docker.sock `
    -v "${ade}:${inside}" `
    -v "${repo}:/verimend" `
    -v verimend-ade-venv:/opt/venv `
    verimend-ade-runner bash -c ($script -replace "`r", '')
exit $LASTEXITCODE
