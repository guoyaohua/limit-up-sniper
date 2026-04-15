"""Quick test: can get-robot-data paginate directly?"""
import json, os, requests

DUMP_DIR = 'output/iwencai_api_debug'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0')
PROJECT_USER_DATA = os.path.join('output', 'playwright', 'iwencai')

def get_cookies():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            channel='msedge', user_data_dir=PROJECT_USER_DATA, headless=False,
            viewport={'width': 1440, 'height': 960}, user_agent=UA,
            locale='zh-CN', timezone_id='Asia/Shanghai',
            args=['--disable-blink-features=AutomationControlled'],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto('https://www.iwencai.com/', wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        cookies = ctx.cookies('https://www.iwencai.com')
        ctx.close()
    return cookies

cookies = get_cookies()
cookie_header = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
hexin_v = next((c['value'] for c in cookies if c['name'] == 'v'), '')

session = requests.Session()
session.trust_env = False
session.proxies = {'http': None, 'https': None}

headers = {
    'Accept': 'application/json, text/plain, */*',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Origin': 'https://www.iwencai.com',
    'Referer': 'https://www.iwencai.com/unifiedwap/result?w=%E8%82%A1%E7%A5%A8%E6%89%80%E5%B1%9E%E8%A1%8C%E4%B8%9A%E5%92%8C%E6%A6%82%E5%BF%B5&querytype=stock',
    'User-Agent': UA,
    'hexin-v': hexin_v,
    'Cookie': cookie_header,
}

url = 'https://www.iwencai.com/unifiedwap/unified-wap/v2/result/get-robot-data'

for page_num in [1, 2, 3]:
    payload = {
        'question': '股票所属行业和概念',
        'perpage': '100',
        'page': str(page_num),
        'secondary_intent': 'stock',
        'log_info': json.dumps({'input_type': 'typewrite'}),
        'source': 'Ths_iwencai_Xuangu',
        'version': '2.0',
        'query_area': '',
        'block_list': '',
        'add_info': json.dumps({'urp': {'scene': 1, 'company': 1, 'business': 1}}),
    }
    r = session.post(url, data=payload, headers=headers, timeout=30)
    data = r.json()

    # Find records
    def find_recs(obj, depth=0):
        if depth > 10: return None
        if isinstance(obj, dict):
            for k in ('datas', 'data'):
                v = obj.get(k)
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    if any(x in v[0] for x in ['股票代码', 'code']):
                        return v
            for v in obj.values():
                r2 = find_recs(v, depth+1)
                if r2: return r2
        return None

    recs = find_recs(data)
    if recs:
        first_code = recs[0].get('股票代码', recs[0].get('code', '?'))
        last_code = recs[-1].get('股票代码', recs[-1].get('code', '?'))
        has_industry = any('所属同花顺行业' in r for r in recs[:3])
        has_concept = any('所属概念' in r for r in recs[:3])
        print(f'Page {page_num}: {len(recs)} records, first={first_code}, last={last_code}, industry={has_industry}, concept={has_concept}')
    else:
        print(f'Page {page_num}: status={data.get("status_code")}, msg={data.get("status_msg")}')
