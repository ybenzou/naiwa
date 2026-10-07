param([switch]$Demo)

$ErrorActionPreference = 'Stop'
$naiwaCandidates = @(
    (Join-Path $PSScriptRoot '.venv/Scripts/pythonw.exe'),
    (Join-Path $env:USERPROFILE 'miniconda3/envs/naiwa/pythonw.exe')
)
$naiwaPythonw = $naiwaCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $naiwaPythonw) {
    $naiwaFound = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($naiwaFound) { $naiwaPythonw = $naiwaFound.Source }
}
if (-not $naiwaPythonw) { throw 'Python environment not found. Install Naiwa as described in README.' }

$naiwaArgs = @('-m', 'naiwa')
if ($Demo) { $naiwaArgs += 'demo' }
$naiwaPreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Join-Path $PSScriptRoot 'src') + [IO.Path]::PathSeparator + $naiwaPreviousPythonPath
    $naiwaProcess = Start-Process -FilePath $naiwaPythonw -ArgumentList $naiwaArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    Write-Output "Naiwa started, PID $($naiwaProcess.Id). Exit from the tray menu."
}
finally {
    $env:PYTHONPATH = $naiwaPreviousPythonPath
}
