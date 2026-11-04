[CmdletBinding()]
param(
    [string]$Python = $env:AHAB_PYTHON,
    [switch]$Dev
)

function Install-Ahab {
    $ErrorActionPreference = 'Stop'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    function Test-Python {
        param([string]$Executable, [string[]]$Prefix = @())
        try {
            & $Executable @Prefix -c 'import sys; sys.exit(sys.version_info < (3, 11))' *> $null
            return $LASTEXITCODE -eq 0
        } catch { return $false }
    }
    function Test-Venv {
        param([string]$Executable, [string[]]$Prefix = @())
        & $Executable @Prefix -c 'import venv, ensurepip' *> $null
        return $LASTEXITCODE -eq 0
    }
    function Assert-Success([string]$Message) {
        if ($LASTEXITCODE -ne 0) { throw $Message }
    }
    function Download([string]$Url, [string]$Destination) {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination
    }

    $RemoteInstall = -not ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'pyproject.toml')))
    $Root = $PSScriptRoot
    if ($RemoteInstall) {
        $Root = if ($env:AHAB_INSTALL_DIR) { $env:AHAB_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'Ahab' }
    }
    $PythonArgs = @()
    if ($Python) {
        if (-not (Test-Python $Python)) { throw 'Python 3.11+ is required.' }
    } elseif ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python py @('-3'))) {
        $Python = 'py'
        $PythonArgs = @('-3')
    } else {
        foreach ($Candidate in @('python', 'python3', 'python3.14', 'python3.13', 'python3.12', 'python3.11')) {
            if ((Get-Command $Candidate -ErrorAction SilentlyContinue) -and (Test-Python $Candidate)) {
                $Python = $Candidate
                break
            }
        }
    }

    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $Root = (Resolve-Path $Root).Path
    $Temp = Join-Path ([IO.Path]::GetTempPath()) ('ahab-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Temp | Out-Null
    $OldUvInstall = $env:UV_UNMANAGED_INSTALL
    $OldPythonDir = $env:UV_PYTHON_INSTALL_DIR
    $OldPythonBin = $env:UV_PYTHON_BIN_DIR
    Push-Location $Root
    try {
        $VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
        if (Test-Path '.venv') {
            if (-not (Test-Path $VenvPython) -or -not (Test-Python $VenvPython)) {
                throw 'Existing .venv is incompatible. Rename it and run the installer again.'
            }
        } elseif (-not $Python -or -not (Test-Venv $Python $PythonArgs)) {
            Write-Host 'Installing a private Python runtime...'
            $env:UV_UNMANAGED_INSTALL = Join-Path $Temp 'uv'
            $env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'python'
            $env:UV_PYTHON_BIN_DIR = Join-Path $Root 'python-bin'
            $UvInstaller = Join-Path $Temp 'uv-install.ps1'
            Download 'https://astral.sh/uv/install.ps1' $UvInstaller
            & powershell -NoProfile -ExecutionPolicy Bypass -File $UvInstaller
            Assert-Success 'Could not install uv.'
            $Uv = Join-Path $env:UV_UNMANAGED_INSTALL 'uv.exe'
            & $Uv python install 3.13 --no-progress
            Assert-Success 'Could not install Python.'
            $Python = (& $Uv python find --managed-python 3.13 | Out-String).Trim()
            Assert-Success 'Could not locate Python.'
            $PythonArgs = @()
        }

        if ($RemoteInstall) {
            $Repo = if ($env:AHAB_REPOSITORY) { $env:AHAB_REPOSITORY } else { 'ignarsoft/ahab' }
            $Ref = if ($env:AHAB_REF) { $env:AHAB_REF } else { 'main' }
            Write-Host "Downloading Ahab ($Ref)..."
            $Archive = Join-Path $Temp 'ahab.zip'
            $Extract = Join-Path $Temp 'source'
            Download "https://api.github.com/repos/$Repo/zipball/$Ref" $Archive
            Expand-Archive -Path $Archive -DestinationPath $Extract
            $Source = @(Get-ChildItem -Path $Extract -Directory)
            if ($Source.Count -ne 1 -or -not (Test-Path (Join-Path $Source[0].FullName 'pyproject.toml'))) {
                throw 'Invalid source archive.'
            }
            $Source = $Source[0].FullName
            New-Item -ItemType Directory -Force -Path (Join-Path $Root 'ahab') | Out-Null
            Copy-Item -Path (Join-Path $Source 'ahab\*') -Destination (Join-Path $Root 'ahab') -Recurse -Force
            foreach ($Name in @('pyproject.toml', 'ahab.example.toml', 'README.md', 'LICENSE')) {
                Copy-Item (Join-Path $Source $Name) $Root -Force
            }
        }
        if (-not (Test-Path '.venv')) {
            & $Python @PythonArgs -m venv .venv
            Assert-Success 'Could not create .venv.'
        }
        $Package = $Root
        if ($Dev) { $Package += '[dev]' }
        Write-Host 'Installing Ahab...'
        & $VenvPython -m pip install --no-input --disable-pip-version-check --editable $Package
        Assert-Success 'Ahab installation failed.'
        if (-not (Test-Path 'ahab.toml')) { Copy-Item 'ahab.example.toml' 'ahab.toml' }

        if ($RemoteInstall) {
            $Bin = Join-Path $Root 'bin'
            New-Item -ItemType Directory -Force -Path $Bin | Out-Null
            $Launcher = Join-Path $Bin 'ahab.cmd'
            $SafeRoot = $Root.Replace('%', '%%')
            $Content = "@echo off`r`nsetlocal DisableDelayedExpansion`r`ncd /d `"$SafeRoot`" || exit /b 1`r`n`"$SafeRoot\.venv\Scripts\ahab.exe`" %*`r`n"
            [IO.File]::WriteAllText($Launcher, $Content, [Text.Encoding]::Default)
            $UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            if ($Bin -notin ($UserPath -split ';')) {
                [Environment]::SetEnvironmentVariable('Path', "$Bin;$UserPath", 'User')
            }
            if ($Bin -notin ($env:Path -split ';')) { $env:Path = "$Bin;$env:Path" }
            Write-Host 'Installed. Run: ahab discover'
        } else {
            Write-Host 'Installed. Run: .\.venv\Scripts\ahab.exe discover'
        }
        Write-Host "Configuration: $(Join-Path $Root 'ahab.toml')"
    } finally {
        Pop-Location
        $env:UV_UNMANAGED_INSTALL = $OldUvInstall
        $env:UV_PYTHON_INSTALL_DIR = $OldPythonDir
        $env:UV_PYTHON_BIN_DIR = $OldPythonBin
        Remove-Item -LiteralPath $Temp -Recurse -Force
    }
}

Install-Ahab
