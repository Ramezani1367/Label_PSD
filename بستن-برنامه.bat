@echo off
chcp 65001 >nul
echo بستن همه نسخه های در حال اجرای برنامه...
taskkill /F /IM PSD-Label-Exporter.exe >nul 2>&1
taskkill /F /IM PSD-Label-Exporter-debug.exe >nul 2>&1
echo تمام شد. حالا می توانی برنامه را دوباره اجرا کنی.
timeout /t 3 >nul
