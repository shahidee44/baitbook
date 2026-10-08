# Installs baitbook on Windows. In PowerShell:
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/shahidee44/baitbook/main/install.ps1 | iex"
#
# 1. installs uv (https://docs.astral.sh/uv) if it isn't there;
# 2. picks the build of llama-cpp-python for this PC's GPU: CUDA (NVIDIA), HIP (AMD Radeon),
#    Vulkan (any other GPU) or CPU;
# 3. installs baitbook with it (`uv tool install`), in an environment of its own with Python 3.12;
# 4. puts the `baitbook` command on PATH;
# 5. runs `baitbook setup`: it looks for the models and ComfyUI, and adds the Start menu icon.
# Run it again to upgrade. Your settings and data (in %LOCALAPPDATA%\baitbook) are left alone.
#
# Environment variables that change what it does:
#   BAITBOOK_GPU=cuda|hip|vulkan|cpu  use this build, don't look at the GPU (or name a wheel
#                                     index: cu118, cu124, cu125, cu130, cu132, hip-radeon)
#   BAITBOOK_SOURCE=<folder or URL>   install from here instead of GitHub (a local checkout, say)
#   BAITBOOK_NO_SETUP=1               don't run `baitbook setup` at the end
#   BAITBOOK_NO_MODIFY_PATH=1         don't change PATH
# uv's own variables (UV_TOOL_DIR, UV_TOOL_BIN_DIR, UV_PYTHON_INSTALL_DIR, ...) work as usual.
#
# This file is run with `iex`, in the caller's session: it never calls `exit` (that would
# close their window), and keeps its variables inside a function.

function Install-Baitbook {
    $ErrorActionPreference = 'Continue'  # failures are checked one by one; a caller's 'Stop' would trip on native stderr
    $Repo = 'https://github.com/shahidee44/baitbook'
    $Wheels = 'https://abetlen.github.io/llama-cpp-python/whl'
    $origPath = $env:Path  # the caller's PATH, before uv's folder is added to it here

    function Say([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }
    function Note([string]$text) { Write-Host "    $text" }
    function Find-Uv {
        $cmd = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) { return $cmd.Source }
        # installed but not on this session's PATH yet: where astral's installer puts it
        $dirs = @($env:UV_INSTALL_DIR, $(if ($env:UV_INSTALL_DIR) { Join-Path $env:UV_INSTALL_DIR 'bin' }), $env:XDG_BIN_HOME,
                  $(if ($env:XDG_DATA_HOME) { Join-Path $env:XDG_DATA_HOME '..\bin' }), (Join-Path $HOME '.local\bin'),
                  (Join-Path $(if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $HOME '.cargo' }) 'bin'))
        foreach ($dir in $dirs) {
            if ($dir -and (Test-Path -LiteralPath (Join-Path $dir 'uv.exe'))) {
                $env:Path = "$dir;$env:Path"
                return (Join-Path $dir 'uv.exe')
            }
        }
        return $null
    }

    # ---- 1. uv ----
    $uv = Find-Uv
    if ($uv) {
        Say "Using uv: $uv"
    } else {
        Say 'Installing uv, the Python package manager that installs baitbook (https://docs.astral.sh/uv)'
        $hadNoModify = Test-Path Env:UV_NO_MODIFY_PATH
        if ($env:BAITBOOK_NO_MODIFY_PATH -eq '1') { $env:UV_NO_MODIFY_PATH = '1' }
        try {
            & powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
            $code = $LASTEXITCODE
        } finally {
            if (-not $hadNoModify) { Remove-Item Env:UV_NO_MODIFY_PATH -ErrorAction SilentlyContinue }
        }
        $uv = Find-Uv
        if ($code -or -not $uv) {
            throw ("uv didn't install. Install it by hand (https://docs.astral.sh/uv/getting-started/installation/), " +
                   'then run this again.')
        }
        Say "Installed uv: $uv"
    }

    # ---- 2. which llama-cpp-python build ----
    Say 'Choosing the llama-cpp-python build for this PC'
    $gpu = "$env:BAITBOOK_GPU".Trim().ToLower()
    $smi = $null
    foreach ($candidate in @((Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source),
                             (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe'))) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $smi = $candidate; break }
    }
    if ($gpu) {
        Note "BAITBOOK_GPU=$gpu"
    } else {
        $cards = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue)
        foreach ($c in $cards) { Note "found: $($c.Name)" }
        $nvidia = $null
        if ($smi) { $nvidia = @(& $smi -L 2>$null) | Where-Object { $_ -match '^GPU \d' } | Select-Object -First 1 }
        if ($nvidia) {
            $gpu = 'cuda'; Note "NVIDIA driver answers: $nvidia"
        } elseif ($cards | Where-Object { "$($_.Name) $($_.AdapterCompatibility)" -match 'AMD|Radeon|Advanced Micro Devices' -or $_.PNPDeviceID -match 'VEN_1002' }) {
            $gpu = 'hip'
        } elseif ($cards | Where-Object { $_.PNPDeviceID -match '^PCI\\VEN_(10DE|1002|1022|8086)' }) {
            $gpu = 'vulkan'  # a real GPU (NVIDIA, AMD or Intel) whose driver brings Vulkan
        } else {
            $gpu = 'cpu'
        }
    }
    switch -regex ($gpu) {
        '^cuda$' {
            $cuda = $null; $cc = $null
            if ($smi) {
                if ((@(& $smi 2>$null) -join "`n") -match 'CUDA Version:\s*(\d+\.\d+)') { $cuda = [version]$Matches[1] }
                $capText = @(& $smi --query-gpu=compute_cap --format=csv,noheader 2>$null) | Select-Object -First 1
                if ("$capText".Trim() -match '^\d+\.\d+$') { $cc = [version]"$capText".Trim() }
            }
            # the newest CUDA the driver runs, against the wheels there are for 0.3.36 (cu118, cu124,
            # cu125, cu130, cu132); the CUDA 13 wheels need compute capability 7.5 (Turing) or newer
            if (-not $cuda) { $backend = 'cu124'; Note "couldn't read the driver's CUDA version; trying CUDA 12.4" }
            elseif ($cuda -ge [version]'13.0' -and $cc -and $cc -ge [version]'7.5') { $backend = 'cu130' }
            elseif ($cuda -ge [version]'12.5') { $backend = 'cu125' }
            elseif ($cuda -ge [version]'12.0') { $backend = 'cu124' }
            elseif ($cuda -ge [version]'11.8') { $backend = 'cu118' }
            else { throw "The NVIDIA driver only runs CUDA $cuda; update it (https://www.nvidia.com/drivers), or set BAITBOOK_GPU=vulkan." }
            if ($cuda) { Note "the driver runs CUDA $cuda$(if ($cc) { ", compute capability $cc" })" }
            $what = 'NVIDIA CUDA'
        }
        '^(hip|rocm|amd)$' { $backend = 'hip-radeon'; $what = 'AMD Radeon (HIP)' }
        '^vulkan$' { $backend = 'vulkan'; $what = 'Vulkan' }
        '^cpu$' { $backend = 'cpu'; $what = 'CPU only (slow for big models)' }
        '^(cu\d+|hip-radeon)$' { $backend = $gpu; $what = $gpu }
        '^metal$' { throw 'BAITBOOK_GPU=metal is for Macs (install.sh). On Windows: cuda, hip, vulkan or cpu.' }
        default { throw "BAITBOOK_GPU=$gpu isn't one of cuda, hip, vulkan, cpu." }
    }
    $index = "$Wheels/$backend"
    Note "build: $what, from $index"
    Note 'not right? run this again with $env:BAITBOOK_GPU set to cuda, hip, vulkan or cpu'

    # ---- 3. baitbook ----
    $src = $env:BAITBOOK_SOURCE
    if (-not $src) {
        $src = "$Repo/archive/refs/heads/main.zip"  # needs no git, unlike git+https://
    } elseif (Test-Path -LiteralPath $src -PathType Container) {
        $src = ([Uri](Resolve-Path -LiteralPath $src).ProviderPath).AbsoluteUri  # a folder: file:///C:/...
    }
    Say "Installing baitbook from $src"
    Note 'the first time, this downloads Python 3.12 and a few hundred MB of packages'
    & $uv tool install --python 3.12 --force --reinstall "baitbook[llm] @ $src" --extra-index-url $index
    if ($LASTEXITCODE) {
        throw "uv couldn't install baitbook (exit status $LASTEXITCODE; the reason is above)."
    }

    # ---- 4. PATH ----
    $bin = "$(& $uv tool dir --bin)".Trim()
    $exe = Join-Path $bin 'baitbook.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw "uv says it installed baitbook, but there's no $exe." }
    $folders = { param($list) @("$list" -split ';' | ForEach-Object { $_.TrimEnd('\') }) }
    $newTerminal = $false
    if ((& $folders $origPath) -notcontains $bin.TrimEnd('\')) {
        if ($env:BAITBOOK_NO_MODIFY_PATH -eq '1') {
            Note "$bin isn't on PATH (BAITBOOK_NO_MODIFY_PATH is set): add it yourself to type ``baitbook``"
        } else {
            Say "Adding $bin to your PATH"
            & $uv tool update-shell  # your user PATH, in the registry; does nothing if it's there already
            if ($LASTEXITCODE) { Note "that didn't work: add $bin to PATH yourself to type ``baitbook``" } else { $newTerminal = $true }
        }
        if ((& $folders $env:Path) -notcontains $bin.TrimEnd('\')) { $env:Path = "$bin;$env:Path" }  # this session, at least
    }
    $version = "$(& $exe --version)".Trim()
    if ($LASTEXITCODE -or -not $version) { throw "baitbook was installed but doesn't start: try $exe --version" }
    Say "Installed $version"

    # ---- 5. setup ----
    if ($env:BAITBOOK_NO_SETUP -eq '1') {
        Note 'skipping `baitbook setup` (BAITBOOK_NO_SETUP=1)'
    } else {
        Say 'Running baitbook setup'
        & $exe setup
        if ($LASTEXITCODE) { Note 'setup has more to do: run `baitbook setup` again once the files it names are in place.' }
    }

    # ---- 6. what's next ----
    Write-Host ''
    Say 'Done. Next:'
    Note 'baitbook            open the app (or click baitbook in the Start menu)'
    Note 'baitbook setup      check the models and ComfyUI again, add the Start menu icon'
    Note 'baitbook status     where your settings and data live'
    Note 'baitbook --help     everything else'
    if ($newTerminal) { Note 'If a terminal can''t find `baitbook`, open a new one: terminals opened before now don''t know the new PATH.' }
    Note 'To upgrade, run the install command again.'
}

$baitbookOk = $true
try {
    Install-Baitbook
} catch {
    $baitbookOk = $false
    Write-Host ''
    Write-Host "baitbook wasn't installed: $($_.Exception.Message)" -ForegroundColor Red
}
if (-not $baitbookOk) {
    # a failure exit status, but only when PowerShell was started just for this (-c / -File):
    # in an open window, setting it would close the window
    $baitbookArgs = [Environment]::GetCommandLineArgs() | Select-Object -Skip 1
    if (($baitbookArgs -match '^[-/](c|f|ec|en|e$)') -and -not ($baitbookArgs -match '^[-/]noe')) { $Host.SetShouldExit(1) }
}
Remove-Variable baitbookOk, baitbookArgs -ErrorAction SilentlyContinue
