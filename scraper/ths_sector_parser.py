"""
同花顺板块数据解析器。
通过 Playwright 打开问财结果页并复用项目内持久化会话获取 Cookie / hexin-v，
再经 iwencai API 以 100 条/页分页拉取全量股票行业与概念映射，
输出打板策略兼容的 THS JSON 文件。
"""

import argparse
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import requests


DEFAULT_IWENCAI_SECTOR_URL = (
    'https://www.iwencai.com/unifiedwap/result?'
    'w=%E8%82%A1%E7%A5%A8%E6%89%80%E5%B1%9E%E8%A1%8C%E4%B8%9A%E5%92%8C%E6%A6%82%E5%BF%B5'
    '&querytype=stock&sign=<redacted>'
)
DEFAULT_IWENCAI_DOWNLOAD_DIR = os.path.join('output', 'iwencai')
DEFAULT_IWENCAI_USER_DATA_DIR = os.path.join('output', 'playwright', 'iwencai')
DESKTOP_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0'
)
SYSTEM_BROWSER_CANDIDATES = [
    {
        'name': 'Edge 默认配置',
        'channel': 'msedge',
        'user_data_dir': os.path.join(os.path.expanduser('~'), 'AppData',
                                      'Local', 'Microsoft', 'Edge',
                                      'User Data'),
        'profile_directory': 'Default'
    },
]

DEFAULT_API_URL = 'https://www.iwencai.com/gateway/urp/v7/landing/getDataList'
DEFAULT_QUERY = '股票所属行业和概念'
DEFAULT_REFERER = (
    'https://www.iwencai.com/unifiedwap/result?'
    'w=%E8%82%A1%E7%A5%A8%E6%89%80%E5%B1%9E%E8%A1%8C%E4%B8%9A%E5%92%8C%E6%A6%82%E5%BF%B5'
    '&querytype=stock'
)
DEFAULT_SOURCE = 'Ths_iwencai_Xuangu'
DEFAULT_VERSION = '2.0'
DEFAULT_PER_PAGE = 100
DEFAULT_TIMEOUT = 30

CODE_FIELD_CANDIDATES = ['股票代码', 'code', 'stock_code', 'symbol', '证券代码']
INDUSTRY_FIELD_CANDIDATES = [
    '所属同花顺行业', '所属行业', '同花顺行业', 'industry', 'industry_name'
]
CONCEPT_FIELD_CANDIDATES = [
    '所属概念', '概念', 'concept', 'concept_list', 'concept_name'
]


def _normalize_key(value: str) -> str:
    return re.sub(r'\s+', '', str(value)).lower()


def _first_present_value(record: Dict[str, Any], candidate_keys: Sequence[str]) -> Any:
    for key in candidate_keys:
        if key in record:
            return record.get(key)
    normalized_map = {_normalize_key(k): v for k, v in record.items()}
    for key in candidate_keys:
        normalized = _normalize_key(key)
        if normalized in normalized_map:
            return normalized_map[normalized]
    return None


# ---------------------------------------------------------------------------
# Playwright session capture
# ---------------------------------------------------------------------------

class THSIWenCaiSessionCapture:
    """用 Playwright 打开问财页面，从浏览器 cookie 中提取凭据。

    只提取 cookies，hexin-v 等于 cookie ``v`` 的值。
    API 调用由 THSIWenCaiAPIFetcher 自行构建请求参数。
    """

    COOKIE_CACHE_FILE = os.path.join('output', 'iwencai', '.session_cache.json')

    def __init__(self,
                 url: str = DEFAULT_IWENCAI_SECTOR_URL,
                 user_data_dir: str = DEFAULT_IWENCAI_USER_DATA_DIR,
                 headless: bool = False,
                 timeout_ms: int = 120000):
        self.url = url
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.timeout_ms = timeout_ms

    @staticmethod
    def _resolve_edge_profile() -> Optional[Dict[str, str]]:
        for candidate in SYSTEM_BROWSER_CANDIDATES:
            if os.path.exists(candidate['user_data_dir']):
                return candidate
        return None

    # ------------------------------------------------------------------
    # Session cache
    # ------------------------------------------------------------------
    def _load_session_cache(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.COOKIE_CACHE_FILE):
            return None
        try:
            with open(self.COOKIE_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            import time
            if time.time() - cache.get('saved_at', 0) > 4 * 3600:
                print('会话缓存已过期，将重新从浏览器获取')
                return None
            if not cache.get('cookie_header') or not cache.get('hexin_v'):
                return None
            print(f"使用缓存的会话 (hexin-v={cache['hexin_v'][:16]}...)")
            return cache
        except Exception:
            return None

    def _save_session_cache(self, result: Dict[str, Any]):
        import time
        os.makedirs(os.path.dirname(self.COOKIE_CACHE_FILE), exist_ok=True)
        result['saved_at'] = time.time()
        with open(self.COOKIE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Main capture
    # ------------------------------------------------------------------
    def capture(self, skip_cache: bool = False) -> Dict[str, Any]:
        if not skip_cache:
            cached = self._load_session_cache()
            if cached:
                return cached

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "未安装 playwright，请先执行 'pip install playwright' 并安装浏览器"
            ) from exc

        with sync_playwright() as playwright:
            context = self._launch_context(playwright)
            try:
                page = self._prepare_page(context)
                self._navigate_and_wait(page)

                cookies = context.cookies('https://www.iwencai.com')
                has_login = any(c['name'] in ('user', 'userid', 'ticket')
                                for c in cookies)

                if not has_login and not self.headless:
                    print('\n检测到未登录问财，请在弹出的浏览器中完成登录。')
                    print('登录成功后页面会自动显示查询结果。')
                    try:
                        input('登录完成后按回车继续...')
                    except EOFError:
                        page.wait_for_timeout(30000)
                    self._navigate_and_wait(page)
                    cookies = context.cookies('https://www.iwencai.com')
            finally:
                context.close()

        cookie_header = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
        hexin_v = next((c['value'] for c in cookies if c['name'] == 'v'), '')

        if not hexin_v:
            raise RuntimeError(
                '未能从浏览器 cookie 中获取 hexin-v (cookie "v")，'
                '请确认问财页面已正常加载'
            )

        result = {'cookie_header': cookie_header, 'hexin_v': hexin_v}
        print(f'成功提取会话信息 (hexin-v={hexin_v[:20]}...)')
        self._save_session_cache(result)
        return result

    def _launch_context(self, playwright):
        """启动 Edge 持久化上下文：优先系统配置，失败回退项目目录。"""
        base_args = [
            '--disable-blink-features=AutomationControlled',
            '--start-maximized',
        ]
        launch_kwargs: Dict[str, Any] = {
            'headless': self.headless,
            'accept_downloads': False,
            'viewport': {'width': 1440, 'height': 960},
            'user_agent': DESKTOP_USER_AGENT,
            'locale': 'zh-CN',
            'timezone_id': 'Asia/Shanghai',
        }

        edge_profile = self._resolve_edge_profile()
        if edge_profile is not None:
            print(f"尝试复用系统浏览器配置: {edge_profile['name']}")
            try:
                return playwright.chromium.launch_persistent_context(
                    channel=edge_profile['channel'],
                    user_data_dir=edge_profile['user_data_dir'],
                    args=base_args + [
                        f"--profile-directory={edge_profile['profile_directory']}"
                    ],
                    **launch_kwargs,
                )
            except Exception as exc:
                print(f'复用系统 Edge 失败（Edge 可能正在运行），回退到项目内目录: {exc}')

        os.makedirs(self.user_data_dir, exist_ok=True)
        print(f'使用项目内持久化会话目录: {self.user_data_dir}')
        return playwright.chromium.launch_persistent_context(
            channel='msedge',
            user_data_dir=self.user_data_dir,
            args=base_args,
            **launch_kwargs,
        )

    @staticmethod
    def _prepare_page(context):
        """复用或新建标签页并清理多余页。"""
        if context.pages:
            page = context.pages[0]
            for extra in context.pages[1:]:
                try:
                    extra.close()
                except Exception:
                    pass
            return page
        return context.new_page()

    def _navigate_and_wait(self, page):
        """导航到问财首页以确保 cookie 被设置。"""
        try:
            page.goto('https://www.iwencai.com/',
                      wait_until='domcontentloaded',
                      timeout=self.timeout_ms)
        except Exception:
            pass
        try:
            page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)


# ---------------------------------------------------------------------------
# API Fetcher — two-phase: get-robot-data + getDataList
# ---------------------------------------------------------------------------

ROBOT_DATA_URL = 'https://www.iwencai.com/unifiedwap/unified-wap/v2/result/get-robot-data'
PAGINATION_API_URL = 'https://www.iwencai.com/gateway/urp/v7/landing/getDataList'


class THSIWenCaiAPIFetcher:
    """通过 iwencai API 分页拉取股票行业与概念数据。

    第一页: POST get-robot-data (返回首页数据 + 分页元数据)
    后续页: POST getDataList (使用首页元数据中的 condition / comp_id / uuid 等)
    """

    def __init__(self,
                 cookie: str,
                 hexin_v: str,
                 query: str = DEFAULT_QUERY,
                 per_page: int = DEFAULT_PER_PAGE,
                 timeout: int = DEFAULT_TIMEOUT,
                 max_pages: Optional[int] = None,
                 debug_dump_dir: Optional[str] = None):
        self.cookie = cookie.strip()
        self.hexin_v = hexin_v.strip()
        self.query = query
        self.per_page = per_page
        self.timeout = timeout
        self.max_pages = max_pages
        self.debug_dump_dir = debug_dump_dir

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {'http': None, 'https': None}
        self.session.headers.update(self._build_headers())

    def _build_headers(self) -> Dict[str, str]:
        return {
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://www.iwencai.com',
            'Referer': DEFAULT_REFERER,
            'User-Agent': DESKTOP_USER_AGENT,
            'hexin-v': self.hexin_v,
            'Cookie': self.cookie,
        }

    # ---------- Phase 1: get-robot-data ----------

    def _build_robot_data_payload(self) -> Dict[str, str]:
        return {
            'question': self.query,
            'perpage': str(self.per_page),
            'page': '1',
            'secondary_intent': 'stock',
            'log_info': json.dumps({'input_type': 'typewrite'}),
            'source': DEFAULT_SOURCE,
            'version': DEFAULT_VERSION,
            'query_area': '',
            'block_list': '',
            'add_info': json.dumps({
                'urp': {'scene': 1, 'company': 1, 'business': 1},
            }),
        }

    def _fetch_first_page(self) -> Dict[str, Any]:
        payload = self._build_robot_data_payload()
        response = self.session.post(ROBOT_DATA_URL, data=payload,
                                     timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        self._dump_debug('page_1_robot_data', payload, data)

        status = data.get('status_code')
        if status is not None and int(status) != 0:
            raise RuntimeError(
                f"get-robot-data 返回错误: status_code={status}, "
                f"status_msg={data.get('status_msg')}"
            )
        return data

    def _extract_pagination_meta(self, data: Dict[str, Any]) -> Dict[str, str]:
        """从首页响应中提取分页所需的元数据。"""
        comp_data = self._navigate_to_component_data(data)
        meta = comp_data.get('meta', {})
        extra = meta.get('extra', {})

        # comp_id 来自 component 的 cid
        comp = self._navigate_to_component(data)
        comp_id = str(comp.get('cid', ''))

        result = {
            'query': extra.get('query', self.query),
            'condition': extra.get('condition', ''),
            'logid': meta.get('qid', ''),
            'sessionid': meta.get('sessionid', ''),
            'iwc_token': extra.get('token', ''),
            'comp_id': comp_id,
            'uuid': str(meta.get('uuids', '')),
            'user_id': str(meta.get('userid', '')),
            'source': str(meta.get('source', DEFAULT_SOURCE)),
            'urp_sort_way': str(meta.get('urp_sort_way', 'desc')),
            'urp_use_sort': str(meta.get('urp_use_sort', '1')),
            'ret': str(meta.get('ret', 'json_all')),
            'date_range': str(meta.get('time', '')).split(' ')[0],
        }

        # urp_sort_index
        sort_index = meta.get('urp_sort_index', '')
        if sort_index:
            result['urp_sort_index'] = str(sort_index)

        return result

    # ---------- Phase 2: getDataList ----------

    def _build_pagination_payload(self, page: int,
                                  meta: Dict[str, str]) -> Dict[str, str]:
        payload: Dict[str, str] = {
            'query': meta.get('query', self.query),
            'urp_sort_way': meta.get('urp_sort_way', 'desc'),
            'page': str(page),
            'perpage': str(self.per_page),
            'addheaderindexes': '',
            'condition': meta.get('condition', ''),
            'codelist': '',
            'indexnamelimit': '',
            'logid': meta.get('logid', ''),
            'ret': meta.get('ret', 'json_all'),
            'sessionid': meta.get('sessionid', ''),
            'source': meta.get('source', DEFAULT_SOURCE),
            'iwc_token': meta.get('iwc_token', ''),
            'urp_use_sort': meta.get('urp_use_sort', '1'),
            'user_id': meta.get('user_id', ''),
            'query_type': 'stock',
            'comp_id': meta.get('comp_id', ''),
            'business_cat': 'soniu',
            'uuid': meta.get('uuid', ''),
        }

        if meta.get('urp_sort_index'):
            payload['urp_sort_index'] = meta['urp_sort_index']

        date_range = meta.get('date_range', '')
        if date_range:
            payload['date_range[0]'] = date_range

        uuid_val = meta.get('uuid', '')
        if uuid_val:
            payload['uuids[0]'] = uuid_val

        return payload

    def _fetch_page(self, page: int, meta: Dict[str, str]) -> Dict[str, Any]:
        payload = self._build_pagination_payload(page, meta)
        response = self.session.post(PAGINATION_API_URL, data=payload,
                                     timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        self._dump_debug(f'page_{page}_getDataList', payload, data)
        return data

    # ---------- Record extraction ----------

    @staticmethod
    def _navigate_to_component(data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return data['data']['answer'][0]['txt'][0]['content']['components'][0]
        except (KeyError, IndexError, TypeError):
            return {}

    @staticmethod
    def _navigate_to_component_data(data: Dict[str, Any]) -> Dict[str, Any]:
        comp = THSIWenCaiAPIFetcher._navigate_to_component(data)
        return comp.get('data', {})

    @staticmethod
    def _extract_records(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从 API 响应中提取股票记录列表。"""
        # 优先路径 (get-robot-data)
        paths: Sequence[Tuple[Any, ...]] = [
            ('data', 'answer', 0, 'txt', 0, 'content', 'components', 0, 'data', 'datas'),
        ]
        for path in paths:
            current: Any = data
            try:
                for key in path:
                    current = current[key]
            except (KeyError, IndexError, TypeError):
                continue
            if isinstance(current, list):
                return current

        # 通用搜索 (getDataList 返回的格式可能不同)
        def _find(obj: Any, depth: int = 0) -> Optional[List]:
            if depth > 10:
                return None
            if isinstance(obj, dict):
                for key in ('datas', 'data', 'list', 'rows'):
                    val = obj.get(key)
                    if isinstance(val, list) and val and isinstance(val[0], dict):
                        sample_keys = set(val[0].keys())
                        if sample_keys & set(CODE_FIELD_CANDIDATES):
                            return val
                for v in obj.values():
                    r = _find(v, depth + 1)
                    if r is not None:
                        return r
            elif isinstance(obj, list):
                for item in obj:
                    r = _find(item, depth + 1)
                    if r is not None:
                        return r
            return None

        result = _find(data)
        if result is not None:
            return result
        return []

    @staticmethod
    def _extract_total_count(data: Dict[str, Any]) -> int:
        comp_data = THSIWenCaiAPIFetcher._navigate_to_component_data(data)
        extra = comp_data.get('meta', {}).get('extra', {})
        for key in ('row_count', 'code_count'):
            val = extra.get(key)
            if isinstance(val, int) and val > 0:
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)
        return 0

    # ---------- Orchestration ----------

    def fetch_all_records(self) -> List[Dict[str, Any]]:
        # Phase 1: 首页
        print('调用 get-robot-data 获取首页数据...')
        first_page_data = self._fetch_first_page()
        records = self._extract_records(first_page_data)
        if not records:
            raise ValueError(
                '首页未返回任何股票记录。请确认已登录问财且查询正确。'
            )

        total_count = self._extract_total_count(first_page_data)
        print(f'已拉取第 1 页: {len(records)} 行, 总计 {total_count} 行')

        all_records = list(records)

        if self.max_pages is not None and self.max_pages <= 1:
            return self._deduplicate(all_records)

        if len(records) >= self.per_page and total_count > len(records):
            # Phase 2: 提取分页元数据
            meta = self._extract_pagination_meta(first_page_data)
            if not meta.get('condition'):
                print('警告: 未能提取分页 condition，仅返回首页数据')
                return self._deduplicate(all_records)

            total_pages = (total_count + self.per_page - 1) // self.per_page
            if self.max_pages is not None:
                total_pages = min(total_pages, self.max_pages)

            for page in range(2, total_pages + 1):
                data = self._fetch_page(page, meta)
                page_records = self._extract_records(data)
                if not page_records:
                    print(f'第 {page} 页返回空数据，停止翻页')
                    break

                all_records.extend(page_records)
                print(f'已拉取第 {page} 页: {len(page_records)} 行, '
                      f'累计 {len(all_records)} 行')

                if len(page_records) < self.per_page:
                    break

        return self._deduplicate(all_records)

    def _deduplicate(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        unique: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for record in records:
            code = THSSectorParser._normalize_stock_code(
                _first_present_value(record, CODE_FIELD_CANDIDATES)
            )
            if not code or code in seen:
                continue
            seen.add(code)
            unique.append(record)
        print(f'去重后股票行数: {len(unique)}')
        return unique

    def _dump_debug(self, label: str, payload: Dict[str, Any],
                    response_data: Dict[str, Any]):
        if not self.debug_dump_dir:
            return
        os.makedirs(self.debug_dump_dir, exist_ok=True)
        file_path = os.path.join(self.debug_dump_dir, f'iwencai_{label}.json')
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({'request_payload': payload, 'response': response_data},
                      f, ensure_ascii=False, indent=2)
        print(f'调试响应已保存: {file_path}')


class THSSectorParser:
    """同花顺板块数据解析器"""

    def __init__(self, input_file: str = '[iwencai-api]'):
        self.input_file = input_file
        self.output_base_dir = 'output'
        self.stock_to_industry = {}
        self.stock_to_concept = {}
        self.industry_to_stocks = {}
        self.concept_to_stocks = {}

    @staticmethod
    def _normalize_stock_code(raw_value) -> str:
        value = str(raw_value).strip()
        if not value or value.lower() == 'nan':
            return ''

        value = value.split('.')[0]
        digits = re.sub(r'\D', '', value)
        if digits:
            return digits[-6:].zfill(6)
        return value

    @staticmethod
    def _split_industry_names(raw_value: Any) -> List[str]:
        value = str(raw_value).strip()
        if not value or value.lower() == 'nan' or value == '--':
            return []
        parts = [part.strip() for part in value.split('-')]
        return [p for p in parts if p and p != '--']

    @staticmethod
    def _split_concept_names(raw_value: Any) -> List[str]:
        if isinstance(raw_value, list):
            return [str(item).strip() for item in raw_value if str(item).strip()]

        value = str(raw_value).strip()
        if not value or value.lower() == 'nan' or value == '--':
            return []

        if value.startswith('[') and value.endswith(']'):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                pass

        parts = re.split(r'[;；,，/|]+', value)
        return [p.strip() for p in parts if p.strip() and p.strip() != '--']

    def parse_records(self, records: Sequence[Dict[str, Any]]):
        """解析 API 返回的记录列表"""
        print(f'开始解析 API 数据: {len(records)} 行')

        for record in records:
            stock_code = self._normalize_stock_code(
                _first_present_value(record, CODE_FIELD_CANDIDATES)
            )
            if not stock_code:
                continue

            industry_value = _first_present_value(record, INDUSTRY_FIELD_CANDIDATES)
            for industry_name in self._split_industry_names(industry_value):
                self._add_industry_mapping(stock_code, industry_name, industry_name)

            concept_value = _first_present_value(record, CONCEPT_FIELD_CANDIDATES)
            for concept_name in self._split_concept_names(concept_value):
                self._add_concept_mapping(stock_code, concept_name, concept_name)

        print('解析完成:')
        print(f'  - 股票数量: {len(self.stock_to_industry)} (行业), {len(self.stock_to_concept)} (概念)')
        print(f'  - 行业板块数量: {len(self.industry_to_stocks)}')
        print(f'  - 概念板块数量: {len(self.concept_to_stocks)}')

    def _add_industry_mapping(self, stock_code: str, sector_code: str,
                              sector_name: str):
        """添加股票与行业板块的映射关系"""
        if stock_code not in self.stock_to_industry:
            self.stock_to_industry[stock_code] = []
        if sector_code not in self.stock_to_industry[stock_code]:
            self.stock_to_industry[stock_code].append(sector_code)

        if sector_code not in self.industry_to_stocks:
            self.industry_to_stocks[sector_code] = {
                'name': sector_name,
                'stocks': []
            }
        if stock_code not in self.industry_to_stocks[sector_code]['stocks']:
            self.industry_to_stocks[sector_code]['stocks'].append(stock_code)

    def _add_concept_mapping(self, stock_code: str, sector_code: str,
                             sector_name: str):
        """添加股票与概念板块的映射关系"""
        if stock_code not in self.stock_to_concept:
            self.stock_to_concept[stock_code] = []
        if sector_code not in self.stock_to_concept[stock_code]:
            self.stock_to_concept[stock_code].append(sector_code)

        if sector_code not in self.concept_to_stocks:
            self.concept_to_stocks[sector_code] = {
                'name': sector_name,
                'stocks': []
            }
        if stock_code not in self.concept_to_stocks[sector_code]['stocks']:
            self.concept_to_stocks[sector_code]['stocks'].append(stock_code)

    def save_to_json(self):
        """保存数据到 JSON 文件"""
        date_str = datetime.now().strftime('%Y%m%d')

        ths_industry_sector_dir = os.path.join(self.output_base_dir,
                                               'industry_sector_data', 'THS')
        ths_industry_sectors_dir = os.path.join(self.output_base_dir,
                                                'industry_sectors', 'THS')
        ths_concept_sector_dir = os.path.join(self.output_base_dir,
                                              'concept_sector_data', 'THS')
        ths_concept_sectors_dir = os.path.join(self.output_base_dir,
                                               'concept_sectors', 'THS')

        ths_industry_sector_history_dir = os.path.join(ths_industry_sector_dir,
                                                       'history')
        ths_industry_sectors_history_dir = os.path.join(
            ths_industry_sectors_dir, 'history')
        ths_concept_sector_history_dir = os.path.join(ths_concept_sector_dir,
                                                      'history')
        ths_concept_sectors_history_dir = os.path.join(ths_concept_sectors_dir,
                                                       'history')

        os.makedirs(ths_industry_sector_dir, exist_ok=True)
        os.makedirs(ths_industry_sectors_dir, exist_ok=True)
        os.makedirs(ths_concept_sector_dir, exist_ok=True)
        os.makedirs(ths_concept_sectors_dir, exist_ok=True)
        os.makedirs(ths_industry_sector_history_dir, exist_ok=True)
        os.makedirs(ths_industry_sectors_history_dir, exist_ok=True)
        os.makedirs(ths_concept_sector_history_dir, exist_ok=True)
        os.makedirs(ths_concept_sectors_history_dir, exist_ok=True)

        industry_stock_file = os.path.join(ths_industry_sector_dir,
                                           'stock_to_industry_mapping.json')
        with open(industry_stock_file, 'w', encoding='utf-8') as file:
            json.dump(self.stock_to_industry, file, ensure_ascii=False, indent=2)
        print(f'保存股票到行业板块映射: {industry_stock_file}')

        industry_stock_history_file = os.path.join(
            ths_industry_sector_history_dir,
            f'stock_to_industry_mapping_{date_str}.json')
        with open(industry_stock_history_file, 'w', encoding='utf-8') as file:
            json.dump(self.stock_to_industry, file, ensure_ascii=False, indent=2)
        print(f'保存股票到行业板块历史映射: {industry_stock_history_file}')

        industry_sector_history_file = os.path.join(
            ths_industry_sectors_history_dir,
            f'sector_to_stocks_mapping_{date_str}.json')
        industry_sector_latest = os.path.join(
            ths_industry_sectors_dir, 'sector_to_stocks_mapping_latest.json')
        with open(industry_sector_history_file, 'w', encoding='utf-8') as file:
            json.dump(self.industry_to_stocks, file, ensure_ascii=False, indent=2)
        with open(industry_sector_latest, 'w', encoding='utf-8') as file:
            json.dump(self.industry_to_stocks, file, ensure_ascii=False, indent=2)
        print(f'保存行业板块到股票映射: {industry_sector_history_file}')

        concept_stock_file = os.path.join(ths_concept_sector_dir,
                                          'stock_to_concept_mapping.json')
        with open(concept_stock_file, 'w', encoding='utf-8') as file:
            json.dump(self.stock_to_concept, file, ensure_ascii=False, indent=2)
        print(f'保存股票到概念板块映射: {concept_stock_file}')

        concept_stock_history_file = os.path.join(
            ths_concept_sector_history_dir,
            f'stock_to_concept_mapping_{date_str}.json')
        with open(concept_stock_history_file, 'w', encoding='utf-8') as file:
            json.dump(self.stock_to_concept, file, ensure_ascii=False, indent=2)
        print(f'保存股票到概念板块历史映射: {concept_stock_history_file}')

        concept_sector_history_file = os.path.join(
            ths_concept_sectors_history_dir,
            f'sector_to_stocks_mapping_{date_str}.json')
        concept_sector_latest = os.path.join(
            ths_concept_sectors_dir, 'sector_to_stocks_mapping_latest.json')
        with open(concept_sector_history_file, 'w', encoding='utf-8') as file:
            json.dump(self.concept_to_stocks, file, ensure_ascii=False, indent=2)
        with open(concept_sector_latest, 'w', encoding='utf-8') as file:
            json.dump(self.concept_to_stocks, file, ensure_ascii=False, indent=2)
        print(f'保存概念板块到股票映射: {concept_sector_history_file}')

        print('\n统计信息:')
        print(f'  行业板块数量: {len(self.industry_to_stocks)}')
        print(f'  概念板块数量: {len(self.concept_to_stocks)}')
        print(f'  股票总数（行业）: {len(self.stock_to_industry)}')
        print(f'  股票总数（概念）: {len(self.stock_to_concept)}')

    @staticmethod
    def output_files_exist(output_base_dir: str = 'output') -> bool:
        required_files = [
            os.path.join(output_base_dir, 'concept_sector_data', 'THS',
                         'stock_to_concept_mapping.json'),
            os.path.join(output_base_dir, 'industry_sector_data', 'THS',
                         'stock_to_industry_mapping.json'),
            os.path.join(output_base_dir, 'concept_sectors', 'THS',
                         'sector_to_stocks_mapping_latest.json'),
            os.path.join(output_base_dir, 'industry_sectors', 'THS',
                         'sector_to_stocks_mapping_latest.json'),
        ]
        return all(os.path.exists(file_path) and os.path.getsize(file_path) > 0
                   for file_path in required_files)


def run_auto_download_and_parse(
        url: str = DEFAULT_IWENCAI_SECTOR_URL,
        download_dir: str = DEFAULT_IWENCAI_DOWNLOAD_DIR,
        user_data_dir: str = DEFAULT_IWENCAI_USER_DATA_DIR,
        browser_channel: Optional[str] = None,
        headless: bool = False,
        fallback_to_latest: bool = True,
        per_page: int = DEFAULT_PER_PAGE,
        max_pages: Optional[int] = None,
        debug_dump_dir: Optional[str] = None,
        skip_cache: bool = False) -> str:
    """通过 Edge 浏览器会话提取 Cookie，再分页拉取全量数据并保存。"""
    capture = THSIWenCaiSessionCapture(
        url=url,
        user_data_dir=user_data_dir,
        headless=headless,
    )
    captured = capture.capture(skip_cache=skip_cache)

    cookie = captured['cookie_header']
    hexin_v = captured['hexin_v']

    fetcher = THSIWenCaiAPIFetcher(
        cookie=cookie,
        hexin_v=hexin_v,
        per_page=per_page,
        max_pages=max_pages,
        debug_dump_dir=debug_dump_dir,
    )

    try:
        records = fetcher.fetch_all_records()
    except Exception as exc:
        if not skip_cache:
            print(f'API 调用失败 ({exc})，尝试重新获取会话...')
            captured = capture.capture(skip_cache=True)
            fetcher = THSIWenCaiAPIFetcher(
                cookie=captured['cookie_header'],
                hexin_v=captured['hexin_v'],
                per_page=per_page,
                max_pages=max_pages,
                debug_dump_dir=debug_dump_dir,
            )
            records = fetcher.fetch_all_records()
        else:
            raise

    parser = THSSectorParser()
    parser.parse_records(records)
    parser.save_to_json()

    if not parser.output_files_exist(parser.output_base_dir):
        raise RuntimeError('问财板块映射输出文件不完整')

    return '[iwencai-api]'


def refresh_ths_sector_mappings(
        url: str = DEFAULT_IWENCAI_SECTOR_URL,
        download_dir: str = DEFAULT_IWENCAI_DOWNLOAD_DIR,
        user_data_dir: str = DEFAULT_IWENCAI_USER_DATA_DIR,
        browser_channel: Optional[str] = None,
        headless: bool = False,
        fallback_to_latest: bool = True,
        per_page: int = DEFAULT_PER_PAGE,
        max_pages: Optional[int] = None) -> bool:
    try:
        run_auto_download_and_parse(url=url,
                                    download_dir=download_dir,
                                    user_data_dir=user_data_dir,
                                    headless=headless,
                                    fallback_to_latest=fallback_to_latest,
                                    per_page=per_page,
                                    max_pages=max_pages)
        return True
    except Exception as exc:
        print(f'刷新 THS 板块映射失败: {exc}')
        return False


def main():
    parser = argparse.ArgumentParser(description='问财行业/概念 API 拉取与解析工具')
    parser.add_argument('--auto-download',
                        action='store_true',
                        help='打开问财结果页捕获会话后，以 100 条/页分页拉取数据')
    parser.add_argument('--url', default=DEFAULT_IWENCAI_SECTOR_URL)
    parser.add_argument('--download-dir', default=DEFAULT_IWENCAI_DOWNLOAD_DIR)
    parser.add_argument('--user-data-dir',
                        default=DEFAULT_IWENCAI_USER_DATA_DIR)
    parser.add_argument('--headless',
                        action='store_true',
                        help='以无头模式运行浏览器')
    parser.add_argument('--per-page', type=int, default=DEFAULT_PER_PAGE,
                        help='每页拉取的股票数量，默认 100')
    parser.add_argument('--max-pages', type=int,
                        help='限制最大页数（调试用）')
    parser.add_argument('--debug-dump-dir',
                        help='保存 API 原始响应用于调试')
    args = parser.parse_args()

    if args.auto_download:
        run_auto_download_and_parse(url=args.url,
                                    download_dir=args.download_dir,
                                    user_data_dir=args.user_data_dir,
                                    headless=args.headless,
                                    per_page=args.per_page,
                                    max_pages=args.max_pages,
                                    debug_dump_dir=args.debug_dump_dir)
    else:
        print('请指定 --auto-download 参数以从问财 API 拉取数据')
        return 1

    print('\n处理完成！')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
