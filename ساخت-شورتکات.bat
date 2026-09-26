@echo off
chcp 65001 >nul
setlocal
set "APP=%~dp0PSD-Label-Exporter.exe"
set "ICON=%~dp0_internal\assets\icon.ico"
if not exist "%ICON%" set "ICON=%APP%"

if not exist "%APP%" (
  echo فایل PSD-Label-Exporter.exe کنار این اسکریپت پیدا نشد.
  pause
  exit /b 1
)

echo در حال ساخت میان بر روی دسکتاپ و منوی استارت...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w = New-Object -ComObject WScript.Shell;" ^
  "$name = 'سامانه خروجی لیبل ها';" ^
  "$targets = @($w.SpecialFolders('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'));" ^
  "foreach ($dir in $targets) {" ^
  "  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }" ^
  "  $lnk = $w.CreateShortcut((Join-Path $dir ($name + '.lnk')));" ^
  "  $lnk.TargetPath = '%APP%';" ^
  "  $lnk.WorkingDirectory = '%~dp0';" ^
  "  $lnk.IconLocation = '%ICON%';" ^
  "  $lnk.Description = 'خروجی گرفتن از لیبل های PSD با فتوشاپ';" ^
  "  $lnk.Save();" ^
  "  Write-Host ('ساخته شد: ' + (Join-Path $dir ($name + '.lnk')))" ^
  "}"

echo.
echo تمام شد. میان بر روی دسکتاپ و در منوی استارت ساخته شد.
timeout /t 4 >nul
