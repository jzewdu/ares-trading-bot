@echo off
title Ares Dashboard Launcher
powershell.exe -NoLogo -NoProfile -File "%~dp0Open Ares Dashboard.ps1"
if errorlevel 1 pause
