# 数据采集模块

本文档说明 `scraper/` 目录下的数据采集模块，涵盖东方财富和同花顺两大数据源。

## 一、模块总览

| 文件 | 数据源 | 功能 |
|------|--------|------|
| `em_scraper_api.py` | 东方财富 | 板块数据（概念/行业涨幅、领涨股） |
| `em_stock_capital_flow_scraper.py` | 东方财富 | 个股资金流向数据 |
| `dfcf_ztlb_parser.py` | 东方财富 | 涨停/炸板 MHTML 页面解析 |
| `tonghuashun_monitor.py` | 同花顺 | 实时板块数据监控 |
| `ths_sector_parser.py` | 同花顺 | 板块分类 Excel 解析 |
| `anti_ban_helper.py` | - | 反爬策略辅助 |

---

## 二、东方财富板块数据 (`em_scraper_api.py`)

### 2.1 功能概述

通过东方财富的 JSONP 接口获取实时板块行情数据，包括概念板块和行业板块的涨幅排行、领涨股等。

### 2.2 核心类

#### `SectorType` (枚举)

```python
class SectorType(Enum):
    CONCEPT = 'concept'    # 概念板块
    INDUSTRY = 'industry'  # 行业板块
```

#### `SectorConfig`

API 请求参数配置，包括：
- API 端点 URL
- 字段映射（f12=代码, f14=名称, f3=涨幅, f128=领涨股等）
- 过滤参数

#### `EnhancedAntiSpiderConfig`

反爬参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| MIN_DELAY | 1.0s | 最小请求间隔 |
| MAX_DELAY | 3.0s | 最大请求间隔 |
| MAX_RETRIES | 3 | 最大重试次数 |
| RETRY_DELAY_BASE | 2.0s | 重试基础延迟 |
| User-Agent 池 | 8 个 | 随机轮换 |
| Referer 池 | 5 个 | 随机轮换 |
| 浏览器指纹 | 3 套 | 模拟不同浏览器 |

#### `UtTokenGenerator`

动态生成 32 字符十六进制 UT 令牌，用于绕过简单的 Token 验证。

#### `SessionManager`

管理多个 HTTP Session，支持 Cookie 轮换。

#### `EnhancedSectorDataFetcher`

**核心采集类**，主要方法：

- `fetch_quotes_page(page_num, page_size)` — 获取单页板块数据
- `fetch_all_quotes(max_pages)` — 分页获取全部板块数据
- 动态构造 JSONP 回调参数
- JSONP 响应解析

### 2.3 数据流

```
构造请求参数（含动态 UT Token、随机 UA、JSONP 回调名）
    │
    ▼
HTTP GET → 东方财富 API 端点
    │
    ▼
解析 JSONP 响应 → 提取 JSON 数据
    │
    ▼
转换为 DataFrame → 返回给调用方
```

---

## 三、个股资金流向 (`em_stock_capital_flow_scraper.py`)

### 3.1 功能概述

采集东方财富的个股资金流向数据，包括超大单/大单/中单/小单的买入卖出金额。

### 3.2 更强的反爬配置

| 参数 | 值 | 对比板块接口 |
|------|-----|-----------|
| MIN_DELAY | 3.0s | 1.0s → 3.0s |
| MAX_DELAY | 8.0s | 3.0s → 8.0s |
| 并发数 | 1 | 更保守 |
| Session 池 | 5 个 | Session 生命周期 300s，最多 50 次请求 |

### 3.3 核心类

#### `StockCapitalFlowFetcher`

原始数据获取：
- JSONP 接口请求
- 分页翻页（页间延迟 200%+  ）
- 异常处理与重试

#### `StockCapitalFlowParser`

数据解析与转换：
- JSON → pandas DataFrame
- 单位转换：元 → 万元
- 字段中文映射

#### `StockCapitalFlowScraper`

编排类：
- `scrape_all_data(max_pages)` — 获取并解析
- `save_data(df)` — 保存 CSV (UTF-8-sig BOM 编码)
- `analyze_market_summary(df)` — 计算市场整体净流入统计

### 3.4 采集的字段

| API字段 | 中文名 | 单位 |
|---------|--------|------|
| f12 | 股票代码 | - |
| f14 | 股票名称 | - |
| f62 | 主力净流入 | 万元 |
| f184 | 主力净流入占比 | % |
| f66 | 超大单净流入 | 万元 |
| f69 | 超大单净流入占比 | % |
| f72 | 大单净流入 | 万元 |
| f75 | 大单净流入占比 | % |

---

## 四、涨停/炸板解析 (`dfcf_ztlb_parser.py`)

### 4.1 功能概述

解析从东方财富涨停板页面保存的 MHTML 文件，提取涨停和炸板股票的详细信息。

### 4.2 `DFCFZtlbParser` 类

**主要方法**：

#### `parse_mhtml_file(file_path) -> list[dict]`

**解析流程**：
1. 打开 MHTML 文件（UTF-8 编码）
2. 定位 HTML 内容起始位置
3. 如需要，进行 Quoted-Printable 解码
4. BeautifulSoup 解析 HTML
5. 提取所有 `data-stockcode` 属性的表格行
6. 解析每行数据：

```python
{
    '代码': '600000',
    '名称': 'XX股份',
    '市场': 'SH',
    '涨幅': 10.01,
    '最新价': 15.23,
    '封板时间': '09:42:15',
    '炸板次数': 0,
    '统计': '3/5',        # 3天内5次涨停
    '板块标签': '首板',     # 首板 / 2连板 / 3连板
    '行业': '计算机',
}
```

#### `classify_stocks(stocks) -> tuple[list, list]`

将解析结果分为：
- **首板股票**：`板块标签 == '首板'`
- **连板股票**：`板块标签` 包含 '连板'

#### `parse_and_classify(input_file, date_str, file_type)`

完整流程：解析 → 分类 → 保存到文件。

---

## 五、同花顺实时监控 (`tonghuashun_monitor.py`)

### 5.1 功能概述

基于同花顺 API（通过 `deps/ai_hotspot_trader/ths_scraper`）实时监控指定板块的股票表现。

### 5.2 `TonghuashunMonitor` 类

**初始化参数**：
- `sector_codes`: 要监控的板块代码列表
- `headless`: 是否无头浏览器模式
- `interval`: 监控间隔（默认 30 秒）

**使用模式**：

```python
monitor = TonghuashunMonitor(sector_codes=['883993', '883986'])
monitor.set_callback(my_callback)
monitor.start()    # 启动监控线程
# ... 运行中 ...
monitor.stop()     # 停止
```

或使用 Context Manager：

```python
with TonghuashunMonitor(sector_codes=[...]) as monitor:
    monitor.set_callback(callback)
    monitor.start()
```

**监控循环逻辑**：
1. 遍历 `sector_codes`
2. 调用 `api.get_sector_info(code)` 获取板块实时数据
3. 触发回调函数处理数据
4. 多板块间等待 1 秒
5. 根据循环耗时调整等待时间
6. 异常时自动延迟重连

---

## 六、板块分类解析 (`ths_sector_parser.py`)

### 6.1 功能概述

解析同花顺导出的板块分类 Excel 文件，生成结构化的 JSON 映射。

### 6.2 `THSSectorParser` 类

**解析流程**：

```
同花顺 Excel / HTML 文件
    │
    ├── parse_excel() 解析
    │   ├── 支持 .xls / .xlsx / HTML 表格格式
    │   ├── 提取: 股票代码, 股票名称, 概念板块名, 行业板块名
    │   └── 分别填充四个映射字典
    │
    ├── 输出映射关系
    │   ├── stock_to_industry:  {股票 → [行业板块列表]}
    │   ├── stock_to_concept:   {股票 → [概念板块列表]}
    │   ├── industry_to_stocks: {行业板块 → {name, stocks[]}}
    │   └── concept_to_stocks:  {概念板块 → {name, stocks[]}}
    │
    └── save_to_json() 保存
        ├── output/industry_sectors/THS/
        │   ├── stock_to_industry_mapping.json              # 股票→行业
        │   ├── sector_to_stocks_mapping_latest.json        # 行业→股票 (最新)
        │   └── sector_to_stocks_mapping_{date}.json        # 行业→股票 (历史)
        │
        └── output/concept_sectors/THS/
            ├── stock_to_concept_mapping.json               # 股票→概念
            ├── sector_to_stocks_mapping_latest.json        # 概念→股票 (最新)
            └── sector_to_stocks_mapping_{date}.json        # 概念→股票 (历史)
```

---

## 七、反爬辅助 (`anti_ban_helper.py`)

### 7.1 `BrowserSimulator`

模拟真实浏览器行为，通过先访问首页、数据中心等页面来"预热" Session，避免直接访问 API 接口引起封禁。

### 7.2 `IPRotationHelper`

代理 IP 轮换工具（轮询方式）。

### 7.3 `RequestThrottler`

智能请求频率控制：

| 场景 | 行为 |
|------|------|
| 正常请求 | uniform(min_interval, max_interval) |
| 超过 50 次请求 | 间隔乘以 0.8-1.5 倍 |
| 检测到封禁 | 等待 4-8 倍最大间隔 |

### 7.4 `check_response_for_ban(response) -> bool`

**封禁检测信号**：
- HTTP 状态码：403 / 429 / 503
- 响应内容包含："access denied"、"captcha"、"验证码"、"请稍后再试" 等

### 7.5 `get_safe_scraper_config() -> dict`

返回保守的采集配置：

```python
{
    'interval': (30, 60),       # 30-60 秒请求间隔
    'max_pages': 2,             # 单次最多 2 页
    'delays': (5, 15),          # 操作间延迟 5-15 秒
    'warmup': True,             # 开启浏览器预热
}
```
