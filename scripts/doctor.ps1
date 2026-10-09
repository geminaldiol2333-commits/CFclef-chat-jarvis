# CFclef-chat-jarvis doctor (PowerShell, no Python needed for the llama.cpp check)
#
# Verifies:
#   1. llama-server.exe is findable and which build it is
#   2. whether that build supports clef (/v1/systemone) - PR ggml-org/llama.cpp#29831
#   3. the two GGUF files exist and which architecture they are
#   4. GPU / VRAM
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Section($t) { Write-Host ""; Write-Host "== $t ==" -ForegroundColor Cyan }
function Ok($t)      { Write-Host "  [ok]   $t" -ForegroundColor Green }
function Bad($t)     { Write-Host "  [FAIL] $t" -ForegroundColor Red }
function Warn($t)    { Write-Host "  [warn] $t" -ForegroundColor Yellow }

Section "llama-server"
$exe = $null
$cfgPath = Join-Path $root "config.json"
if (Test-Path $cfgPath) {
    try {
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($cfg.local_llama_server -and (Test-Path $cfg.local_llama_server)) {
            $exe = $cfg.local_llama_server
            Ok "from config.json: $exe"
        }
    } catch { Warn "config.json parse failed: $_" }
}
if (-not $exe) {
    $cmd = Get-Command llama-server -ErrorAction SilentlyContinue
    if ($cmd) { $exe = $cmd.Source; Ok "from PATH: $exe" }
}
if (-not $exe) {
    $guess = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter llama-server.exe -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($guess) { $exe = $guess.FullName; Ok "from WinGet: $exe" }
}
if (-not $exe) { Bad "llama-server.exe not found. Install llama.cpp (winget install llama.cpp) or set local_llama_server in config.json." }

if ($exe) {
    (& $exe --version) 2>&1 | Select-Object -First 3 | ForEach-Object { Write-Host "         $_" }
    $impl = Join-Path (Split-Path -Parent $exe) "llama-server-impl.dll"
    $probe = if (Test-Path $impl) { $impl } else { $exe }
    $hit = Select-String -Path $probe -Pattern "systemone" -Encoding Default -List -ErrorAction SilentlyContinue
    if ($hit) { Ok "this build supports clef (/v1/systemone)" }
    else { Bad "this build does NOT support clef. Upgrade llama.cpp to a build with PR #29831 (e.g. b11515+)." }
}

Section "Models"
function Show-Gguf($path, $label) {
    if (-not $path) { Bad "$label : not configured"; return }
    if (Test-Path $path -PathType Container) {
        $g = Get-ChildItem $path -Filter *.gguf | Select-Object -First 1
        if ($g) { $path = $g.FullName } else { Bad "$label : no .gguf in $path"; return }
    }
    if (Test-Path $path) {
        $size = [math]::Round((Get-Item $path).Length / 1GB, 2)
        Ok "$label : $path ($size GB)"
    } else { Bad "$label : missing $path" }
}
if ($cfg) {
    Show-Gguf $cfg.local_clef_model  "clef (judge)"
    Show-Gguf $cfg.local_draft_model "draft (text)"
    Warn "clef must be the DECISION gguf (ggml-org/Clef-Flash-GGUF), not the language tower."
} else { Warn "no config.json; run the app once and fill the Local model card." }

Section "GPU"
try {
    & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
} catch { Warn "nvidia-smi not available (CPU-only or non-NVIDIA). Expect very slow inference." }

Write-Host ""
Write-Host "Done. For a full end-to-end test: python tools\verify_local.py" -ForegroundColor Cyan
