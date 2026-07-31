# Slice 5C live qualification, host half.
#
# Rebuilds the controller image from the CURRENT COMMITTED src/, records the
# build command, source commit, image digest, the complete mount set and the
# container argv, then runs the in-container qualification.
#
# The workcell itself is created by LiveWorkcellSession at --network none; this
# script only stands up the *controller*, which owns the relay.

$ErrorActionPreference = "Stop"
$Repo = "C:\Users\aryam\local harness"
$Eval = "$Repo\.apoapsis-eval\slice5c-2026-07-30"
$Root = "/mnt/docker-desktop-disk/data/apoapsis-slice5c-2026-07-30"
$Tag  = "apoapsis-live-controller:slice5c"

New-Item -ItemType Directory -Force -Path $Eval | Out-Null
Set-Location $Repo

# --- provenance: what exactly is being built ---------------------------
$commit = (git rev-parse HEAD).Trim()
$dirty  = (git status --porcelain -- src/) -join "`n"
if ($dirty) { throw "src/ has uncommitted changes; the image would not match a commit:`n$dirty" }

$dockerfile = "$Eval\Dockerfile"
@"
FROM apoapsis-live-controller:slice2c
COPY src /opt/apoapsis/src
"@ | Set-Content -Encoding ascii $dockerfile

$buildCmd = "docker build -f `"$dockerfile`" -t $Tag `"$Repo`""
Write-Output "== BUILD: $buildCmd"
docker build -f "$dockerfile" -t $Tag "$Repo"
if ($LASTEXITCODE -ne 0) { throw "controller image build failed" }

$imageId = (docker image inspect $Tag --format '{{.Id}}').Trim()
$workcellId = (docker image inspect apoapsis-qwen-workcell:0.21.1 --format '{{.Id}}').Trim()

# --- the container argv, recorded before it is run ---------------------
# $Root must be mounted at the SAME path inside the controller. It is both
# where the controller writes evidence and where it creates the bind-mount
# sources the workcell container will use -- and a bind mount source path is
# resolved by the daemon, not by the controller's namespace, so the two must
# agree. Without this mount the controller's writes land in its own overlay
# and disappear on --rm, while the daemon silently creates the mount sources
# itself; the run then looks like it produced no evidence at all.
$mounts = @(
  "-v", "/var/run/docker.sock:/var/run/docker.sock",
  "-v", "${Root}:${Root}",
  "-v", "${Repo}:/src-repo:ro",
  "-v", "${Eval}:/probe:ro"
)
$argv = @(
  "run","--rm",
  "--add-host","host.docker.internal:host-gateway",
  "-e","SLICE5C_ROOT=$Root",
  "-e","UPSTREAM=http://host.docker.internal:8080",
  "-e","PYTHONPATH=/opt/apoapsis/src"
) + $mounts + @($Tag, "/src-repo/tools/slice5c/qualify.py")

$provenance = [ordered]@{
  recorded_at_utc      = (Get-Date).ToUniversalTime().ToString("o")
  source_commit        = $commit
  src_clean            = $true
  dockerfile           = (Get-Content $dockerfile -Raw)
  build_command        = $buildCmd
  controller_image_tag = $Tag
  controller_image_id  = $imageId
  workcell_image_tag   = "apoapsis-qwen-workcell:0.21.1"
  workcell_image_id    = $workcellId
  controller_mounts    = $mounts
  container_argv       = @("docker") + $argv
  note                 = "The workcell container argv is built by WorkcellController.build_create_argv and is recorded by the run itself in evidence/workcell-config.json."
}
$provenance | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 "$Eval\provenance.json"
Write-Output "== provenance written to $Eval\provenance.json"

# --- clean the run root ------------------------------------------------
# Earlier failed runs leave the daemon-created mount sources behind, and a
# missing bind-mount source is created by the daemon as a DIRECTORY -- so a
# stale `controller/forwarder.py` comes back as a directory and every later
# run dies writing to it. Start from nothing rather than from someone else's
# wreckage.
Write-Output "== cleaning $Root"
docker run --rm --entrypoint sh -v /mnt/docker-desktop-disk/data:/d $Tag -c "rm -rf /d/$(Split-Path $Root -Leaf)"

# --- run ---------------------------------------------------------------
Write-Output "== RUN"
& docker @argv
$code = $LASTEXITCODE
Write-Output "== qualification exit code: $code"

# --- teardown ----------------------------------------------------------
# LiveWorkcellSession freezes and destroys the workcell and stops the relay on
# context exit, including on failure. This sweeps anything a hard kill left.
$stale = docker ps -aq --filter "label=apoapsis.workcell=true"
if ($stale) { Write-Output "== sweeping stale workcells"; docker rm -f $stale | Out-Null }

Write-Output "== evidence:"
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock $Tag -c @"
import os,shutil
src='$Root/evidence'
print('\n'.join(sorted(os.listdir(src))) if os.path.isdir(src) else 'NO EVIDENCE DIR')
"@
exit $code
