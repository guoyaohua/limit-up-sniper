"""
探索同花顺盘面分析 API
"""

import requests
import json
import base64
import os

# 尝试解码 token
token = os.getenv('THS_MARKET_ANALYSIS_TOKEN', '')
try:
    if not token:
        raise ValueError('请先设置 THS_MARKET_ANALYSIS_TOKEN')
    # 添加填充
    padding = '=' * (4 - len(token) % 4) if len(token) % 4 else ''
    decoded = base64.b64decode(token + padding)
    print(f"Token decoded: {decoded}")
except Exception as e:
    print(f"Token decode failed: {e}")

# 尝试不同的 API 接口
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://eq.10jqka.com.cn/',
}

# 可能的 API 接口列表
api_endpoints = [
    # 大盘指数实时行情
    'https://d.10jqka.com.cn/v4/line/hs_000001/01/today.js',  # 上证指数
    'https://d.10jqka.com.cn/v4/line/hs_399001/01/today.js',  # 深证成指
    'https://d.10jqka.com.cn/v4/line/hs_399006/01/today.js',  # 创业板指
    
    # 大盘概况
    'https://d.10jqka.com.cn/v6/line/hs_000001/01/all.js',
    
    # 涨跌停统计
    'https://data.10jqka.com.cn/funds/ggzjl/',  # 个股资金流
    
    # 市场温度计
    'https://dq.10jqka.com.cn/fuyao/thermo/v1/data',
    
    # 盘面分析相关
    'https://dq.10jqka.com.cn/fuyao/senti_index/data/v1/senti_index',  # 情绪指数
    'https://dq.10jqka.com.cn/fuyao/senti_index/data/v1/market_analysis',
    
    # 涨跌家数
    'https://q.10jqka.com.cn/api/market/getdata',
    
    # 板块资金流
    'https://data.10jqka.com.cn/funds/gnzjl/',
    
    # 盘口数据
    'https://dq.10jqka.com.cn/fuyao/market_analysis/v1/overview',
    'https://dq.10jqka.com.cn/fuyao/market_analysis/v1/data',
    
    # 同花顺问财 API
    'https://www.iwencai.com/gateway/urp/v7/landing/getDataList',
]

print("\n测试各个 API 接口:\n")

for url in api_endpoints:
    try:
        print(f"Testing: {url}")
        resp = requests.get(url, headers=headers, timeout=10)
        print(f"  Status: {resp.status_code}")
        
        content_type = resp.headers.get('Content-Type', '')
        print(f"  Content-Type: {content_type}")
        
        if resp.status_code == 200:
            if 'json' in content_type or resp.text.startswith('{') or resp.text.startswith('['):
                try:
                    data = resp.json()
                    print(f"  Response (truncated): {json.dumps(data, ensure_ascii=False)[:500]}")
                except:
                    print(f"  Response (truncated): {resp.text[:500]}")
            else:
                print(f"  Response (truncated): {resp.text[:300]}")
        print()
    except Exception as e:
        print(f"  Error: {e}\n")
