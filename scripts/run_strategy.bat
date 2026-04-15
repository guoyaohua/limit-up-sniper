@echo off
chcp 65001 >nul
echo ========================================
echo 运行打板策略 (limit-up-sniper)
echo ========================================
echo.

cd /d "%~dp0.."

echo 当前工作目录: %CD%
echo.

:: 检查是否传入 --refresh-sector 参数
set REFRESH_SECTOR=0
for %%a in (%*) do (
    if "%%a"=="--refresh-sector" set REFRESH_SECTOR=1
)

if "%REFRESH_SECTOR%"=="1" (
    echo [板块映射] 正在拉取最新问财板块数据...
    echo.
    python scraper\ths_sector_parser.py --auto-download
    if errorlevel 1 (
        echo.
        echo [警告] 板块数据拉取失败，将使用本地缓存数据
        echo.
    ) else (
        echo.
        echo [板块映射] 板块数据更新完成
        echo.
    )
) else (
    echo [板块映射] 跳过板块数据拉取 (如需更新请加 --refresh-sector 参数)
    echo.
)

python main.py

if errorlevel 1 (
    echo.
    echo ========================================
    echo 策略运行出错！
    echo ========================================
    pause
    exit /b 1
)

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
