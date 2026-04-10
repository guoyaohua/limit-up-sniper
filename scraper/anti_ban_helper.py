"""
反封禁辅助模块
提供额外的反检测策略
"""

import requests
import time
import random
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class BrowserSimulator:
    """
    浏览器行为模拟器
    模拟真实用户的浏览器行为，包括预请求、页面访问等
    """
    
    def __init__(self):
        self.session = requests.Session()
        self.session.proxies = {'http': None, 'https': None}
        self.session.trust_env = False
        
    def warm_up_session(self, session: requests.Session) -> bool:
        """
        会话预热 - 模拟真实用户访问网站首页
        在开始爬取数据前先访问几个正常页面
        
        Args:
            session: 要预热的会话对象
            
        Returns:
            bool: 预热是否成功
        """
        try:
            logger.info("开始会话预热，模拟真实用户访问...")
            
            # 1. 访问东方财富主页
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Cache-Control': 'max-age=0',
            }
            
            # 访问首页
            response = session.get(
                'https://www.eastmoney.com/',
                headers=headers,
                timeout=30,
                allow_redirects=True
            )
            
            if response.status_code == 200:
                logger.debug("✓ 成功访问东方财富首页")
                time.sleep(random.uniform(2.0, 4.0))
            else:
                logger.warning(f"访问首页返回状态码: {response.status_code}")
            
            # 2. 访问数据中心页面
            response = session.get(
                'https://data.eastmoney.com/',
                headers=headers,
                timeout=30,
                allow_redirects=True
            )
            
            if response.status_code == 200:
                logger.debug("✓ 成功访问数据中心页面")
                time.sleep(random.uniform(2.0, 4.0))
            
            # 3. 访问资金流向页面
            response = session.get(
                'https://data.eastmoney.com/zjlx/',
                headers=headers,
                timeout=30,
                allow_redirects=True
            )
            
            if response.status_code == 200:
                logger.debug("✓ 成功访问资金流向页面")
                time.sleep(random.uniform(1.5, 3.0))
            
            logger.info("会话预热完成，等待片刻后开始正式请求...")
            time.sleep(random.uniform(3.0, 6.0))
            
            return True
            
        except Exception as e:
            logger.error(f"会话预热失败: {e}")
            return False
    
    def simulate_human_behavior(self):
        """
        模拟人类行为 - 随机暂停、鼠标移动等
        """
        # 模拟思考时间
        think_time = random.uniform(1.0, 3.0)
        time.sleep(think_time)
        
        # 10%概率添加较长暂停（模拟查看内容）
        if random.random() < 0.1:
            view_time = random.uniform(5.0, 10.0)
            logger.debug(f"模拟查看内容，暂停 {view_time:.1f} 秒")
            time.sleep(view_time)


class IPRotationHelper:
    """
    IP轮换辅助器
    管理代理IP池（如果有的话）
    """
    
    def __init__(self, proxy_list: Optional[list] = None):
        self.proxy_list = proxy_list or []
        self.current_index = 0
        
    def get_next_proxy(self) -> Optional[dict]:
        """获取下一个代理"""
        if not self.proxy_list:
            return None
        
        proxy = self.proxy_list[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.proxy_list)
        
        return {
            'http': proxy,
            'https': proxy,
        }


class RequestThrottler:
    """
    请求节流器
    实现更智能的请求频率控制
    """
    
    def __init__(self, min_interval: float = 5.0, max_interval: float = 15.0):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.last_request_time = 0
        self.request_count = 0
        self.ban_detected = False
        
    def wait_before_request(self):
        """在请求前等待适当的时间"""
        now = time.time()
        elapsed = now - self.last_request_time
        
        # 计算需要等待的时间
        if self.ban_detected:
            # 检测到封禁，使用更长的间隔
            wait_time = random.uniform(self.max_interval * 2, self.max_interval * 4)
            logger.warning(f"检测到可能的封禁，等待 {wait_time:.1f} 秒...")
        elif self.request_count > 50:
            # 请求次数较多，适当增加间隔
            wait_time = random.uniform(self.max_interval * 0.8, self.max_interval * 1.5)
        else:
            # 正常间隔
            wait_time = random.uniform(self.min_interval, self.max_interval)
        
        # 减去已经过去的时间
        actual_wait = max(0, wait_time - elapsed)
        
        if actual_wait > 0:
            logger.debug(f"请求节流：等待 {actual_wait:.1f} 秒")
            time.sleep(actual_wait)
        
        self.last_request_time = time.time()
        self.request_count += 1
        
    def mark_ban_detected(self):
        """标记检测到封禁"""
        self.ban_detected = True
        logger.error("⚠️ 检测到封禁信号！")
        
    def reset_ban_status(self):
        """重置封禁状态"""
        self.ban_detected = False
        self.request_count = 0


def get_safe_scraper_config():
    """
    获取安全的爬虫配置
    返回一个保守的配置字典，降低被ban风险
    """
    return {
        'interval': random.randint(30, 60),  # 30-60秒间隔
        'max_pages': 2,  # 只爬2页
        'enable_warmup': True,  # 启用预热
        'min_delay': 5.0,  # 最小延迟5秒
        'max_delay': 15.0,  # 最大延迟15秒
        'session_lifetime': 180,  # 会话生命周期3分钟
        'max_requests_per_session': 20,  # 每会话最多20个请求
    }


def check_response_for_ban(response: requests.Response) -> bool:
    """
    检查响应是否表明被封禁
    
    Args:
        response: HTTP响应对象
        
    Returns:
        bool: True表示可能被封禁
    """
    # 检查状态码
    if response.status_code in [403, 429, 503]:
        logger.warning(f"可能被封禁 - 状态码: {response.status_code}")
        return True
    
    # 检查响应内容
    content = response.text.lower()
    ban_keywords = [
        'access denied',
        '访问被拒绝',
        'forbidden',
        'too many requests',
        '请求过于频繁',
        'captcha',
        '验证码',
        'blocked',
        '封禁',
    ]
    
    for keyword in ban_keywords:
        if keyword in content:
            logger.warning(f"检测到封禁关键词: {keyword}")
            return True
    
    return False


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(message)s'
    )
    
    simulator = BrowserSimulator()
    session = requests.Session()
    
    print("开始测试会话预热...")
    success = simulator.warm_up_session(session)
    print(f"预热结果: {'成功' if success else '失败'}")
    
    print("\n推荐的安全配置:")
    config = get_safe_scraper_config()
    for key, value in config.items():
        print(f"  {key}: {value}")