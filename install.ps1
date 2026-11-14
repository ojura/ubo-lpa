# Downloads the latest ubo-lpa MSI and starts its setup wizard:
#
#   irm https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.ps1 | iex
#
# The MSI installs for the current user without administrator rights, brings
# its own Python, schedules the daily update and loads uBO into Chrome. The
# Windows workflow builds and tests it on every push to main and publishes it
# as the "latest" release.
#
# The script block keeps these variables out of the caller's session, and
# exit is never called, because under iex it would close that session.
& {
    $ErrorActionPreference = 'Stop'
    # Windows PowerShell 5.1 redraws its progress bar for every chunk it
    # receives, which makes a download of this size take far longer.
    $ProgressPreference = 'SilentlyContinue'
    $url = 'https://github.com/ojura/ubo-lpa/releases/latest/download/ubo-lpa.msi'
    $msi = Join-Path $env:TEMP 'ubo-lpa.msi'
    Write-Host "Downloading $url"
    Invoke-WebRequest $url -OutFile $msi -UseBasicParsing
    Write-Host 'Starting the setup wizard'
    $setup = Start-Process msiexec.exe -ArgumentList '/i', "`"$msi`"" -Wait -PassThru
    Remove-Item $msi -ErrorAction SilentlyContinue
    # 1602: the wizard was cancelled. 3010: a restart is needed to finish.
    if ($setup.ExitCode -notin 0, 1602, 3010) {
        Write-Host "Setup exited with code $($setup.ExitCode)." -ForegroundColor Red
    }
}
