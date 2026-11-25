@echo off
cd /d C:\AdTracker
if exist service_account.json (set FLAGS=--sheets) else (set FLAGS=)
echo ==== %date% %time% >> run_log.txt
venv\Scripts\python.exe tracker.py %FLAGS% >> run_log.txt 2>&1
