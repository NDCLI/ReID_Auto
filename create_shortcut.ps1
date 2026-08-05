$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$appPath = Join-Path $appDirectory "app_gui.pyw"
$iconPath = Join-Path $appDirectory "assets\app_icon.ico"
$desktopDirectory = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopDirectory "RE-ID Auto Draw OSNet.lnk"

$pythonExecutable = (& python -c "import sys; print(sys.executable)").Trim()
$pythonwPath = Join-Path (Split-Path -Parent $pythonExecutable) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonwPath)) {
    $pythonwPath = (Get-Command pythonw.exe -ErrorAction Stop).Source
}

if (-not (Test-Path -LiteralPath $appPath)) {
    throw "Không tìm thấy app_gui.pyw tại $appPath"
}
if (-not (Test-Path -LiteralPath $iconPath)) {
    throw "Không tìm thấy icon tại $iconPath"
}
if (-not (Test-Path -LiteralPath $desktopDirectory)) {
    throw "Không tìm thấy thư mục Desktop."
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwPath
$shortcut.Arguments = '"' + $appPath + '"'
$shortcut.WorkingDirectory = $appDirectory
$shortcut.IconLocation = $iconPath + ",0"
$shortcut.Description = "RE-ID Auto Draw OSNet (TransReID-free variant)"
$shortcut.Save()

Write-Host "Shortcut:" $shortcutPath
