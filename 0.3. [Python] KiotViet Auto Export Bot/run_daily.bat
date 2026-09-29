@echo off
REM Chạy bot bằng Windows Task Scheduler, ví dụ 07:00 mỗi sáng
cd /d "%~dp0"
python kiotviet_vandon_bot.py >> run_log.txt 2>&1
