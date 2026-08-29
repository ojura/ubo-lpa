# Convenience launcher. All logic lives in ..\ubo-lpa.py.
$candidates = @('python', 'py', 'python3') |
    ForEach-Object { Get-Command $_ -ErrorAction SilentlyContinue } |
    Where-Object { $_ }

foreach ($cmd in $candidates) {
    $version = & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $version) {
        $parts = $version.Trim().Split('.')
        if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 9)) {
            & $cmd.Source (Join-Path $PSScriptRoot '..\ubo-lpa.py') @args
            exit $LASTEXITCODE
        }
        Write-Host "skipping $($cmd.Source): Python $version; 3.9+ is required" -ForegroundColor Yellow
    }
}

Write-Host 'error: no Python 3.9+ found.' -ForegroundColor Red
Write-Host '       winget install Python.Python.3.12'
Write-Host '       or https://www.python.org/downloads/'
exit 1
