@echo off
chcp 65001 >nul
echo ========================================
echo 运行打板策略 (limit-up-sniper)
echo ========================================
echo.

cd /d "%~dp0.."

echo 当前工作目录: %CD%
echo.

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
