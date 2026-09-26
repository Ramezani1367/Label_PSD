@echo off
chcp 65001 >nul
rem اجرای برنامه در حالت دمو: بدون فتوشاپ، فقط برای امتحان کردن رابط
start "" "%~dp0PSD-Label-Exporter.exe" --demo
