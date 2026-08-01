# Windows sibling of build.sh, step for step.
#
# It exists because Docker Desktop's WSL integration is disabled for the
# Ubuntu-24.04 distro on this host, so `docker` is reachable only from Windows
# while the qualification suite runs in WSL. Both scripts build the same image
# the same way: `git archive` of a pinned commit as the context, provenance
# labelled into the result.
#
#   .\docker\pilot-controller\build.ps1 -Commit ad13cf0
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Commit,
    [string]$Tag
)

$ErrorActionPreference = "Stop"
$repoRoot = (git rev-parse --show-toplevel).Trim()
$dockerfile = Join-Path $repoRoot "docker/pilot-controller/Dockerfile"

$fullCommit = (git -C $repoRoot rev-parse "$Commit^{commit}").Trim()
$tree = (git -C $repoRoot rev-parse "$Commit^{tree}").Trim()
if (-not $Tag) { $Tag = "apoapsis-pilot-controller:$($fullCommit.Substring(0,7))" }

$work = Join-Path ([IO.Path]::GetTempPath()) ("pilot-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    $tar = Join-Path $work "context.tar"
    # Only committed bytes reach the context, and only the declared paths.
    # The pathspec is not a convenience: `spikes/native-shell-tauri` carries
    # ~800MB of committed Rust build artifacts that the controller has no use
    # for, and shipping them would make every build slow and the context
    # digest dominated by bytes nobody reads.
    $paths = @("src", "pyproject.toml", "README.md", "LICENSE.txt")
    git -C $repoRoot archive --format=tar --output=$tar $fullCommit -- @paths
    $contextSha = (Get-FileHash $tar -Algorithm SHA256).Hash.ToLower()
    $dockerfileSha = (Get-FileHash $dockerfile -Algorithm SHA256).Hash.ToLower()

    $ctx = Join-Path $work "ctx"
    New-Item -ItemType Directory -Force -Path $ctx | Out-Null
    tar -C $ctx -xf $tar
    Copy-Item $dockerfile (Join-Path $ctx "Dockerfile.pilot")

    Write-Output "source_commit:        $fullCommit"
    Write-Output "source_tree:          $tree"
    Write-Output "build_context_sha256: $contextSha"
    Write-Output "dockerfile_sha256:    $dockerfileSha"

    # --no-cache is not caution, it is correctness. A cached LABEL layer keeps
    # the build args of whichever build first created it, so a rebuilt image
    # can carry a build-context digest from an entirely different context --
    # observed once here, with the label and the computed digest disagreeing.
    docker build --no-cache --file (Join-Path $ctx "Dockerfile.pilot") `
        --build-arg "SOURCE_COMMIT=$fullCommit" `
        --build-arg "SOURCE_TREE=$tree" `
        --build-arg "BUILD_CONTEXT_SHA256=$contextSha" `
        --tag $Tag $ctx
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

    Write-Output "image_id:             $(docker image inspect $Tag --format '{{.Id}}')"
    Write-Output "labels:               $(docker image inspect $Tag --format '{{json .Config.Labels}}')"
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
