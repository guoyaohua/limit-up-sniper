@echo off
chcp 65001 >nul
echo ========================================
echo 问财股票板块映射数据 - 下载与解析
echo ========================================
echo.
echo 流程: Playwright 提取 Cookie → API 分页拉取 → 解析保存 JSON
echo.

cd /d "%~dp0.."

echo 当前工作目录: %CD%
echo.

python scraper\ths_sector_parser.py --auto-download

if errorlevel 1 (
    echo.
    echo ========================================
    echo [失败] 问财板块数据下载或解析失败！
    echo.
    echo 常见原因:
    echo   1. Edge 浏览器未登录问财 (首次需手动登录)
    echo   2. 网络连接异常
    echo   3. 问财会话已过期 (删除缓存重试)
    echo.
    echo 如需清除缓存重试，请删除:
    echo   output\iwencai\.session_cache.json
    echo ========================================
    pause
    exit /b 1
)

echo.
echo ========================================
echo [成功] 问财行业概念数据处理完成！
echo ========================================
pause
