@echo off
chcp 65001 >nul
echo ========================================
echo 运行打板策略 (limit-up-sniper)
echo ========================================
echo.

cd /d "%~dp0.."
echo 当前工作目录: %CD%
echo.

if "%1"=="--refresh-sector" goto :refresh
echo [板块映射] 跳过板块数据拉取 (如需更新请加 --refresh-sector 参数)
echo.
goto :run_main

:refresh
echo [板块映射] 正在拉取最新问财板块数据...
echo.
python scraper\ths_sector_parser.py --auto-download
if errorlevel 1 goto :refresh_fail
echo.
echo [板块映射] 板块数据更新完成
echo.
goto :run_main

:refresh_fail
echo.
echo [警告] 板块数据拉取失败，将使用本地缓存数据
echo.

:run_main
python main.py
if errorlevel 1 goto :main_fail

echo.
echo ========================================
echo 策略运行完成，开始复盘分析...
echo ========================================
echo.

python analysis\review_daily.py

echo.
echo ========================================
echo 复盘分析完成！
echo ========================================
pause
exit /b 0

:main_fail
echo.
echo ========================================
echo 策略运行出错！
echo ========================================
pause
exit /b 1
