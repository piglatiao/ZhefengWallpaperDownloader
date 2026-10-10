#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
哲风壁纸 原图下载器（本地网页交互界面）
==================================
用法：
  python get_wallpapers.py
直接启动并自动打开本地网页界面（http://127.0.0.1:随机端口/）。
浏览器模式在网页内按下载任务选择（默认无头、不弹窗；可勾选「可见窗口」弹出一个小窗）。
代理通过环境变量 HAO_PROXY 设置（不设则直连）：
  1) 顶部切换设备：手机 / 电脑
  2) 下拉选择分类（实时从站点拉取，共 9 类）
  3) 缩略图网格点击即选中，可翻页、跨页多选、全选本页、清空
  4) 点「下载所选」，右下角面板实时显示下载进度

原理（已逆向验证）：
  1) 分类接口 /link/pc/wallpaper/getTypeAll（AES 解密）拿到分类 typeId
  2) 解密列表 SSR -> 拿到每页壁纸的 wtId / fileId（每页条数与网页一致：电脑 12 / 手机 13）
  3) 本地服务代理缩略图 /link/common/file/getCroppingImg/{fileId} 供网格显示
  4) 点下载 -> 真实浏览器打开详情页 -> Altcha 人机验证（PoW 自动求解）
  5) getCompleteUrl 返回带签名的直链 down.haowallpaper.com/...png?zfsign=...
  6) 下载直链 = 原图（2~27MB，全分辨率）

重要限制（站点规则，非代码 bug）：
  - 游客按 IP 限每日下载次数；登录后解锁每日 10 次。
  - 若返回 305 "访客今日下载次数上限"，说明当日配额已用完，
    需登录（10次/日）或等次日重置后再跑本脚本。
"""
import base64
import atexit
from collections import OrderedDict
import gzip
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket  # pip install websocket-client

ssl._create_default_https_context = ssl._create_unverified_context

BASE = 'https://haowallpaper.com'
TYPE_ID = '35c203f75643ac7803b8f706fa91ef40'  # 魅力｜迷人（默认分类）

_LOCAL_APPDATA = os.environ.get('LOCALAPPDATA')
APP_DATA_DIR = (os.path.join(_LOCAL_APPDATA, 'ZheFengWallpaperDownloader')
                if _LOCAL_APPDATA else os.path.join(os.getcwd(), 'wallpaper_data'))
AUTH_ROOT = os.path.join(APP_DATA_DIR, 'accounts')
AUTH_INDEX_PATH = os.path.join(APP_DATA_DIR, 'accounts.json')
USAGE_PATH = os.path.join(APP_DATA_DIR, 'usage.json')
GUEST_PROFILE = os.path.join(APP_DATA_DIR, 'guest-device')
GUEST_LIMIT = 5
ACCOUNT_LIMIT = 10
AUTH_LOCK = threading.RLock()
USAGE_LOCK = threading.Lock()
AUTH_ACCOUNTS = []
AUTH_LOADED = False
ACTIVE_ACCOUNT_ID = None
AUTH_LOGIN = None


def _ensure_app_data():
    """创建账号和设备用的本地数据目录。"""
    os.makedirs(AUTH_ROOT, exist_ok=True)


def load_auth_store():
    """读取账号索引；索引不保存站点 token，登录态由浏览器配置目录保存。"""
    global AUTH_ACCOUNTS, AUTH_LOADED
    with AUTH_LOCK:
        if AUTH_LOADED:
            return
        records = []
        try:
            with open(AUTH_INDEX_PATH, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            candidates = raw.get('accounts', []) if isinstance(raw, dict) else []
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                account_id = str(item.get('id', ''))
                if not re.fullmatch(r'[0-9a-f]{32}', account_id):
                    continue
                records.append({
                    'id': account_id,
                    'name': str(item.get('name') or '微信账号')[:80],
                    'avatar': str(item.get('avatar') or '')[:500],
                    'created_at': int(item.get('created_at') or 0),
                    'last_used': int(item.get('last_used') or 0),
                })
        except (FileNotFoundError, OSError, ValueError, TypeError):
            records = []
        AUTH_ACCOUNTS = records
        AUTH_LOADED = True


def _save_auth_store_locked():
    """以 UTF-8 原子写入账号索引，避免程序中断留下半个 JSON 文件。"""
    _ensure_app_data()
    temp_path = f'{AUTH_INDEX_PATH}.{uuid.uuid4().hex}.tmp'
    payload = {'version': 1, 'accounts': AUTH_ACCOUNTS}
    try:
        with open(temp_path, 'w', encoding='utf-8', newline='') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(temp_path, AUTH_INDEX_PATH)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _account_record(account_id):
    """根据账号 ID 获取本地账号记录。"""
    load_auth_store()
    with AUTH_LOCK:
        return next((a for a in AUTH_ACCOUNTS if a['id'] == account_id), None)


def _account_profile_dir(account_id):
    """返回账号对应的持久化 Chromium 用户目录。"""
    if not account_id or not re.fullmatch(r'[0-9a-f]{32}', str(account_id)):
        raise ValueError('账号标识无效')
    _ensure_app_data()
    return os.path.join(AUTH_ROOT, f'account_{account_id}')


def _auth_summary(account):
    """生成可以返回给本地网页的账号摘要，不暴露浏览器路径和 token。"""
    if not account:
        return None
    return {
        'id': account['id'],
        'name': account.get('name') or '微信账号',
        'avatar': account.get('avatar') or '',
    }


def _today_key():
    return time.strftime('%Y-%m-%d')


def _load_usage_locked():
    """读取本地游客计数；日期变化时从零开始。"""
    try:
        with open(USAGE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        data = {}
    if data.get('date') != _today_key():
        return {'date': _today_key(), 'guest_count': 0}
    try:
        count = max(0, min(GUEST_LIMIT, int(data.get('guest_count', 0))))
    except (TypeError, ValueError):
        count = 0
    return {'date': _today_key(), 'guest_count': count}


def _save_usage_locked(data):
    """以 UTF-8 原子保存本地游客计数。"""
    _ensure_app_data()
    temp_path = f'{USAGE_PATH}.{uuid.uuid4().hex}.tmp'
    try:
        with open(temp_path, 'w', encoding='utf-8', newline='') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(temp_path, USAGE_PATH)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def guest_usage():
    """返回游客今日已用和剩余数量。"""
    with USAGE_LOCK:
        data = _load_usage_locked()
        return data['guest_count'], max(0, GUEST_LIMIT - data['guest_count'])


def record_guest_download():
    """记录一次已取得下载直链的游客下载。"""
    with USAGE_LOCK:
        data = _load_usage_locked()
        data['guest_count'] = min(GUEST_LIMIT, data['guest_count'] + 1)
        _save_usage_locked(data)


def find_chrome():
    """自动探测 Chrome 可执行文件，返回绝对路径；找不到则抛异常。
    优先级：环境变量 CHROME_BIN > 程序内置 Chromium > 常见安装目录 > PATH。
    """
    env = os.environ.get('CHROME_BIN')
    if env:
        env_path = os.path.abspath(os.path.expandvars(env))
        if os.path.isfile(env_path):
            return env_path

    app_dir = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, 'frozen', False) else __file__))
    resource_dirs = [app_dir]
    meipass = getattr(sys, '_MEIPASS', '')
    if meipass and os.path.abspath(meipass) not in resource_dirs:
        resource_dirs.append(os.path.abspath(meipass))
    bundled_candidates = []
    for resource_dir in resource_dirs:
        bundled_candidates.extend([
            os.path.join(resource_dir, 'browser', 'chrome.exe'),
            os.path.join(resource_dir, 'browser', 'chrome-win64', 'chrome.exe'),
        ])
    for p in bundled_candidates:
        if os.path.isfile(p):
            return p

    candidates = [
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
        r'C:\Program Files\Google\Chrome Beta\Application\chrome.exe',
        r'C:\Program Files\Google\Chrome Dev\Application\chrome.exe',
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',  # 兜底：Edge 同内核可用
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    for name in ('chrome', 'google-chrome', 'chrome.exe', 'msedge'):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        '未找到内置 Chromium、Chrome 或 Edge，请设置环境变量 CHROME_BIN 指向浏览器可执行文件')


CHROME = find_chrome()
TMP = os.path.join(os.getcwd(), '_chrome_crawl')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REMOTE_OPENER = None
REMOTE_RETRIES = 2
REMOTE_RETRY_DELAY = 0.8
REMOTE_MIN_INTERVAL = 0.15
_REMOTE_RATE_LOCK = threading.Lock()
_NEXT_REMOTE_REQUEST = 0.0
_REMOTE_RETRYABLE = (TimeoutError, ConnectionError, socket.timeout)

# ---- AES 解密（列表 / 分类 SSR 数据）----
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
KEY = b'68zhehao2O776519'
IV = b'aa176b7519e84710'


def decrypt(c):
    raw = bytes.fromhex(base64.b64decode(c).hex())
    return re.sub(r'\x00.*$', '', unpad(AES.new(KEY, AES.MODE_CBC, IV).decrypt(raw), 16).decode('utf-8', 'ignore'), flags=re.S)


def open_remote(req, timeout):
    """按启动时选择的网络模式打开远程请求。"""
    _wait_remote_slot()
    if REMOTE_OPENER is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return REMOTE_OPENER.open(req, timeout=timeout)


def _wait_remote_slot():
    """控制远端请求启动间隔，避免短时间内形成突发请求。"""
    global _NEXT_REMOTE_REQUEST
    with _REMOTE_RATE_LOCK:
        now = time.monotonic()
        wait = max(0.0, _NEXT_REMOTE_REQUEST - now)
        _NEXT_REMOTE_REQUEST = max(now, _NEXT_REMOTE_REQUEST) + REMOTE_MIN_INTERVAL
    if wait:
        time.sleep(wait)


def _is_retryable_remote_error(exc):
    """判断远端错误是否适合重试，不重试业务状态码。"""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return reason is not exc and _is_retryable_remote_error(reason)
    return isinstance(exc, _REMOTE_RETRYABLE)


def _remote_error_status(exc):
    """将远端异常转换为本地 API 应返回的状态码。"""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return 404
        if exc.code == 429:
            return 429
        if exc.code in (408, 504):
            return 504
        if exc.code == 503:
            return 503
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if reason is not exc:
            return _remote_error_status(reason)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return 504
    return 502


def _remote_retry_delay(exc, attempt):
    """计算退避时间；远端明确给出 Retry-After 时优先遵守。"""
    delay = REMOTE_RETRY_DELAY * (2 ** attempt)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            retry_after = float(exc.headers.get('Retry-After', ''))
            delay = max(delay, min(retry_after, 30.0))
        except (TypeError, ValueError):
            pass
    return delay


def read_remote(req, timeout, retries=REMOTE_RETRIES):
    """读取远端响应并确保关闭连接；只对临时网络错误退避重试。"""
    for attempt in range(retries + 1):
        try:
            with open_remote(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            if attempt >= retries or not _is_retryable_remote_error(exc):
                raise
            time.sleep(_remote_retry_delay(exc, attempt))
    raise RuntimeError('远程请求失败')


def configure_network(proxy):
    """配置远程请求的代理；空值表示明确直连。"""
    global REMOTE_OPENER
    handler = urllib.request.ProxyHandler(
        {'http': proxy, 'https': proxy} if proxy else {})
    REMOTE_OPENER = urllib.request.build_opener(handler)


def proxy_available(proxy):
    """检查代理监听端口是否可连接，不发起实际下载请求。"""
    parsed = urllib.parse.urlparse(proxy)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5):
            return True
    except OSError:
        return False


# 浏览器窗口模式改为在网页端按下载任务选择（默认无头、不弹窗；可勾选「可见窗口」弹小窗），
# 不再启动时交互式询问。代理仅通过环境变量 HAO_PROXY 设置（不设则直连）。


def get_html(path, timeout=30, retries=REMOTE_RETRIES):
    """读取远端 HTML；timeout 为单次读取超时，retries 为失败重试次数。"""
    req = urllib.request.Request(
        BASE + path,
        headers={'User-Agent': UA, 'Accept': 'text/html,*/*', 'Accept-Encoding': 'gzip'})
    d = read_remote(req, timeout=timeout, retries=retries)
    try:
        d = gzip.decompress(d)
    except Exception:
        pass
    return d.decode('utf-8', 'ignore')


def get_list(kind, page=1, type_id=None):
    """解密某一分类某页的壁纸列表；返回 dict(list/pages/total) 或 None。"""
    cache_key = (kind, page, type_id or TYPE_ID)
    now = time.monotonic()
    with _LIST_CACHE_LOCK:
        cached = _LIST_CACHE.get(cache_key)
        if cached:
            expires, data = cached
            if expires > now:
                _LIST_CACHE.move_to_end(cache_key)
                return data
            _LIST_CACHE.pop(cache_key, None)
    view = 'mobileView' if kind == 'mobile' else 'homeView'
    url = f'/{view}?page={page}&typeId={type_id or TYPE_ID}&sortType=3'
    h = get_html(url, timeout=20, retries=1).replace('\\u002F', '/').replace('\\u002B', '+').replace('\\/', '/')
    for c in re.findall(r'"([A-Za-z0-9+/]{200,}={0,2})"', h):
        try:
            o = json.loads(decrypt(c))
            if isinstance(o, dict) and 'list' in o and 'pages' in o:
                with _LIST_CACHE_LOCK:
                    _LIST_CACHE[cache_key] = (time.monotonic() + _LIST_CACHE_TTL, o)
                    _LIST_CACHE.move_to_end(cache_key)
                    while len(_LIST_CACHE) > _LIST_CACHE_MAX_ITEMS:
                        _LIST_CACHE.popitem(last=False)
                return o
        except Exception:
            continue
    return None


def get_json(path):
    """请求 JSON 接口（带可选代理），返回解析后的 dict。"""
    req = urllib.request.Request(BASE + path,
                                 headers={'User-Agent': UA, 'Accept': 'application/json,*/*',
                                          'Accept-Encoding': 'gzip'})
    d = read_remote(req, timeout=30)
    try:
        d = gzip.decompress(d)
    except Exception:
        pass
    return json.loads(d.decode('utf-8', 'ignore'))


_CATEGORIES = None


def get_categories():
    """拉取分类列表（id=typeId / typeName=显示名），结果缓存复用。"""
    global _CATEGORIES
    if _CATEGORIES is not None:
        return _CATEGORIES
    env = get_json('/link/pc/wallpaper/getTypeAll')
    obj = json.loads(decrypt(env['data']))
    _CATEGORIES = obj.get('1') or []
    return _CATEGORIES


def _item_title(it):
    labels = it.get('labelList') or []
    return '-'.join(labels[:3]) or it.get('wtId', '')


def _sanitize(name, limit=40):
    return re.sub(r'[\\/:*?"<>|｜]', '_', name)[:limit].strip() or 'wallpaper'


def fetch_thumb(file_id):
    """抓取缩略图（服务端代理，规避防盗链），返回 jpeg 字节。"""
    req = urllib.request.Request(
        f'{BASE}/link/common/file/getCroppingImg/{file_id}',
        headers={'User-Agent': UA, 'Referer': BASE + '/', 'Accept-Encoding': 'gzip'})
    d = read_remote(req, timeout=30)
    if d[:2] == b'\x1f\x8b':
        try:
            d = gzip.decompress(d)
        except Exception:
            pass
    return d


class CDP:
    def __init__(s, ws):
        s.ws = websocket.create_connection(ws, timeout=180); s.i = 0; s.ev = []
        s.closed = False

    def send(s, m, p=None):
        s.i += 1
        s.ws.send(json.dumps({'id': s.i, 'method': m, 'params': p or {}}))
        old = s.ws.gettimeout()
        s.ws.settimeout(180)
        try:
            while True:
                try:
                    msg = json.loads(s.ws.recv())
                except websocket.WebSocketTimeoutException:
                    raise RuntimeError('CDP 命令超时未响应（连接可能已断开）')
                except websocket.WebSocketConnectionClosedException:
                    s.closed = True
                    raise
                if msg.get('id') == s.i:
                    return msg
                if 'method' in msg:
                    s.ev.append(msg)
        finally:
            s.ws.settimeout(old)

    def evl(s, e):
        r = s.send('Runtime.evaluate', {'expression': e, 'returnByValue': True})
        res = r.get('result', {})
        if 'exceptionDetails' in res:
            return {'__err__': str(res['exceptionDetails'])[:300]}
        return res.get('result', {}).get('value')

    def pump(s, timeout=0.2):
        """读取 CDP 推送事件，避免事件停留在 WebSocket 缓冲区。"""
        old_timeout = s.ws.gettimeout()
        s.ws.settimeout(timeout)
        try:
            while True:
                try:
                    msg = json.loads(s.ws.recv())
                except websocket.WebSocketTimeoutException:
                    break
                except websocket.WebSocketConnectionClosedException:
                    # 对端（标签页/Chrome）关闭了调试连接，标记后退出读取，由调用方决定重试或放弃
                    s.closed = True
                    break
                if 'method' in msg:
                    s.ev.append(msg)
        finally:
            s.ws.settimeout(old_timeout)


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def download(url, out_dir, suggested_filename=None):
    """下载原图并保留远端返回的原始文件名，返回（文件名，字节数）。"""
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': BASE + '/'})
    for attempt in range(2):
        try:
            with open_remote(req, timeout=180) as response:
                filename = suggested_filename or response.headers.get_filename()
                if not filename:
                    filename = urllib.parse.unquote(
                        urllib.parse.urlparse(url).path.rsplit('/', 1)[-1])
                filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
                if not filename:
                    raise RuntimeError('下载响应未提供有效文件名')
                data = response.read()
            break
        except Exception as exc:
            if attempt >= 1 or not _is_retryable_remote_error(exc):
                raise
            time.sleep(_remote_retry_delay(exc, attempt))
    path = os.path.join(out_dir, filename)
    with open(path, 'wb') as f:
        f.write(data)
    return filename, len(data)


# ---- 浏览器会话：一次下载任务复用同一个浏览器进程 ----
def _wait_page_ready(c, timeout=15):
    """轮询等待页面加载完成（出现底部下载导航），最多 timeout 秒。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if c.evl("document.readyState") == 'complete' and \
               c.evl("!!document.querySelector('.DownButtom .hao-bottom-nav-end__face, .DownButtom .hao-bottom-nav-end')"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


class BrowserSession:
    """单个 Chrome 进程，供一次下载任务的全部壁纸复用；可见模式全程只弹一个窗口。"""

    def __init__(self, headless=True, proxy=None, profile_dir=None):
        self.port = free_port()
        self.browser = None
        self.persistent = profile_dir is not None
        self.prof = os.path.abspath(profile_dir or os.path.join(TMP, f'_job_{self.port}'))
        if self.persistent:
            os.makedirs(self.prof, exist_ok=True)
        elif os.path.isdir(self.prof):
            shutil.rmtree(self.prof)
        args = [CHROME]
        if proxy:
            args.append(f'--proxy-server={proxy}')
        if headless:
            args.append('--headless=new')
        else:
            args.append('--window-size=460,780')  # 可见窗口模式：弹出的小窗尺寸
        args += ['--disable-gpu', '--no-sandbox', '--no-first-run',
                 '--no-default-browser-check',
                 f'--user-data-dir={self.prof}',
                 f'--remote-debugging-port={self.port}',
                 '--remote-allow-origins=*']
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_ready()
        self._deny_downloads()

    def _deny_downloads(self):
        """禁止内置浏览器将站点下载结果写入系统下载目录。"""
        ver = json.loads(DIRECT_OPENER.open(
            f'http://127.0.0.1:{self.port}/json/version', timeout=5).read())
        self.browser = CDP(ver['webSocketDebuggerUrl'])
        try:
            result = self.browser.send('Browser.setDownloadBehavior', {
                'behavior': 'deny', 'eventsEnabled': True})
            if 'error' in result:
                self.browser.send('Browser.setDownloadBehavior', {'behavior': 'deny'})
        except Exception:
            try:
                self.browser.ws.close()
            except Exception:
                pass
            self.browser = None
            raise

    def take_download_filename(self):
        """读取浏览器最近一次下载的站点建议文件名。"""
        if not self.browser:
            return None
        self.browser.pump(timeout=0.05)
        events, self.browser.ev = self.browser.ev, []
        for event in events:
            if event.get('method') == 'Browser.downloadWillBegin':
                name = event.get('params', {}).get('suggestedFilename')
                if name:
                    return name
        return None

    def _wait_ready(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if DIRECT_OPENER.open(f'http://127.0.0.1:{self.port}/json/version', timeout=5).getcode() == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError('浏览器启动失败 / 调试端口未就绪')

    def new_tab(self, url):
        """用 CDP Target.createTarget 开新标签页并导航到 url，返回 (CDP 连接, target_id)。"""
        ver = json.loads(DIRECT_OPENER.open(
            f'http://127.0.0.1:{self.port}/json/version', timeout=5).read())
        bc = CDP(ver['webSocketDebuggerUrl'])  # 浏览器级连接
        try:
            r = bc.send('Target.createTarget', {'url': url})
            target_id = r['result']['targetId']
            # 等待该标签页出现在 /json 列表并取出其调试地址
            ws = None
            for _ in range(50):
                tabs = json.loads(DIRECT_OPENER.open(
                    f'http://127.0.0.1:{self.port}/json', timeout=5).read())
                for t in tabs:
                    if t.get('id') == target_id and t.get('type') == 'page':
                        ws = t.get('webSocketDebuggerUrl')
                        break
                if ws:
                    break
                time.sleep(0.2)
        finally:
            try:
                bc.ws.close()
            except Exception:
                pass
        if not ws:
            raise RuntimeError('无法获取标签页调试地址')
        return CDP(ws), target_id

    def close_tab(self, target_id):
        try:
            ver = json.loads(DIRECT_OPENER.open(
                f'http://127.0.0.1:{self.port}/json/version', timeout=5).read())
            bc = CDP(ver['webSocketDebuggerUrl'])
            try:
                bc.send('Target.closeTarget', {'targetId': target_id})
            finally:
                try:
                    bc.ws.close()
                except Exception:
                    pass
        except Exception:
            pass

    def quit(self):
        if self.persistent:
            try:
                ver = json.loads(DIRECT_OPENER.open(
                    f'http://127.0.0.1:{self.port}/json/version', timeout=5).read())
                bc = CDP(ver['webSocketDebuggerUrl'])
                try:
                    bc.send('Browser.close')
                finally:
                    try:
                        bc.ws.close()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self.proc.wait(timeout=8)
            except Exception:
                pass
        if self.browser:
            try:
                self.browser.ws.close()
            except Exception:
                pass
            self.browser = None
        try:
            if self.proc.poll() is None:
                if os.name == 'nt':
                    subprocess.run(
                        ['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False)
                else:
                    self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill(); self.proc.wait(timeout=5)
            except Exception:
                pass
        if not self.persistent:
            for _ in range(10):
                if not os.path.isdir(self.prof):
                    break
                try:
                    shutil.rmtree(self.prof); break
                except OSError:
                    time.sleep(0.5)


def get_one(sess, kind, wid):
    """在复用浏览器会话里下载单张：开新标签 -> 点下载 -> 过验证 -> 拿直链。"""
    c = None
    target_id = None
    try:
        view = 'mobileViewLook' if kind == 'mobile' else 'homeViewLook'
        c, target_id = sess.new_tab(f'{BASE}/{view}/{wid}')
        c.send('Network.enable')
        c.send('Runtime.enable')
        c.send('Page.setDownloadBehavior', {'behavior': 'deny'})
        if not _wait_page_ready(c, timeout=15):
            # 详情页首屏偶发停留在 loading，刷新一次后重新等待页面渲染。
            try:
                c.send('Page.reload', {'ignoreCache': True})
            except Exception:
                return None, 'PAGE_RELOAD', None
            if not _wait_page_ready(c, timeout=20):
                return None, 'PAGE_TIMEOUT', None

        c.ev.clear()
        # 点下载
        clicked = c.evl("""
          (() => { const el=document.querySelector('.DownButtom .hao-bottom-nav-end__face')||
              document.querySelector('.DownButtom .hao-bottom-nav-end')||
              Array.from(document.querySelectorAll('.hao-bottom-nav-end__face,.hao-bottom-nav-end'))
                .find(x => (x.innerText || '').trim() === '下载');
            if(el){el.click();return 'clicked';} return 'NO_BTN'; })()
        """)
        if clicked != 'clicked':
            return None, str(clicked or 'NO_BTN'), None

        verify_loaded = False
        verify_started = False
        verify_required = False
        suggested_filename = None
        response_events = {}
        finished_requests = set()
        response_error = None
        # 等待下载响应及页面验证组件完成（最多约 60 秒）
        for _ in range(30):
            time.sleep(2)
            c.pump()
            suggested_filename = sess.take_download_filename() or suggested_filename
            events, c.ev = c.ev, []
            done = None
            for e in events:
                method = e.get('method')
                params = e.get('params', {})
                if method == 'Network.responseReceived':
                    response = params.get('response', {})
                    if '/common/file/getCompleteUrl/' in response.get('url', ''):
                        response_events[params.get('requestId')] = response
                elif method == 'Network.loadingFinished':
                    finished_requests.add(params.get('requestId'))

            # responseReceived 只代表收到响应头，等 loadingFinished 后读取响应体更稳定。
            for rid, response in list(response_events.items()):
                if rid not in finished_requests:
                    continue
                try:
                    rb = c.send('Network.getResponseBody', {'requestId': rid})
                    res = rb.get('result', {})
                    body = res.get('body', '')
                    b = base64.b64decode(body) if res.get('base64') else body.encode()
                    payload = json.loads(b.decode('utf-8', 'ignore'))
                    st = response.get('status')
                    direct = payload.get('data')
                    del response_events[rid]
                    response_error = None
                    if st == 401:
                        return None, 'AUTH_EXPIRED', None
                    if st == 200 and isinstance(direct, str) and 'down.haowallpaper.com' in direct:
                        done = direct
                        break
                    if st == 305:
                        msg = str(payload.get('msg', ''))
                        if re.search(r'下载.*(次数|额度|限制|不足|用完|上限)|次数.*(不足|限制|上限)|额度.*(不足|限制|上限)|limit', msg, re.I):
                            return None, 'QUOTA', None
                        if '登录' in msg or '授权' in msg:
                            return None, 'AUTH_EXPIRED', None
                        if '3004' in msg or '错误的请求' in msg:
                            verify_required = True
                        else:
                            return None, f'HTTP_{st}', None
                    elif st != 200:
                        return None, f'HTTP_{st}', None
                except Exception as exc:
                    response_error = str(exc)[:80]
            if done:
                return done, 'ok', suggested_filename

            if verify_required and not verify_loaded:
                result = c.evl("""
                  (() => { const el=document.querySelector('button.hint:not([disabled])');
                    if(el){el.click();return 'hint-clicked';} return 'wait'; })()
                """)
                verify_loaded = result == 'hint-clicked'
            elif verify_loaded and not verify_started:
                result = c.evl("""
                  (() => { const el=document.querySelector('.altcha-custom-trigger:not([disabled])');
                    if(el){el.click();return 'verify-clicked';} return 'wait'; })()
                """)
                verify_started = result == 'verify-clicked'
        if verify_required:
            return None, 'VERIFY_TIMEOUT', None
        if response_error:
            return None, 'URL_READ_ERROR', None
        return None, 'no-url', None
    except websocket.WebSocketConnectionClosedException:
        # 标签页/浏览器调试连接被对端关闭：单张失败，不击垮整个下载任务
        return None, 'CONN_LOST', None
    finally:
        if c:
            try:
                c.ws.close()
            except Exception:
                pass
        if target_id:
            sess.close_tab(target_id)


def _wait_browser_expression(c, expression, timeout=30, interval=0.4):
    """等待浏览器页面上的条件成立，超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = c.evl(expression)
            if value:
                return value
        except Exception:
            pass
        time.sleep(interval)
    return None


def _set_login_state(state, **values):
    """在线程间安全更新扫码登录状态。"""
    with AUTH_LOCK:
        if AUTH_LOGIN is state:
            state.update(values)


def _run_wechat_login(state):
    """打开可见 Chromium 承载微信扫码登录，并等待站点确认登录成功。"""
    session = None
    page = None
    profile_dir = state['profile_dir']
    try:
        session = BrowserSession(headless=False, proxy=PROXY, profile_dir=profile_dir)
        _set_login_state(state, session=session, status='starting', message='正在打开微信登录页面')
        page, target_id = session.new_tab(BASE)
        page.send('Runtime.enable')
        ready = _wait_browser_expression(
            page,
            "document.readyState === 'complete' && !!document.querySelector('.topDiv')",
            timeout=40)
        if not ready:
            raise RuntimeError('登录页面加载超时')

        clicked = page.evl("""
          (() => { const el=document.querySelector('.topDiv');
            if(el){el.click();return 'clicked';} return 'NO_LOGIN_ENTRY'; })()
        """)
        if clicked != 'clicked' or not _wait_browser_expression(page, "!!document.querySelector('.t-1')", 10):
            raise RuntimeError('未找到微信扫码登录入口')
        page.evl("""
          (() => { const el=document.querySelector('.t-1');
            if(el){el.click();return 'clicked';} return 'NO_QR_ENTRY'; })()
        """)
        _set_login_state(state, status='waiting', message='请在弹出的浏览器窗口中使用微信扫码登录')

        deadline = time.time() + 300
        while time.time() < deadline:
            if state['cancel'].is_set():
                _set_login_state(state, status='cancelled', message='登录已取消')
                return
            name = page.evl("""
              (() => { const el=document.querySelector('.top-name');
                return el ? el.textContent.trim() : ''; })()
            """)
            if isinstance(name, str) and name and name != '登录后获取名称和头像':
                avatar = page.evl(
                    "(() => document.querySelector('.topDiv img')?.getAttribute('src') || '')()")
                account = {
                    'id': state['account_id'],
                    'name': name[:80],
                    'avatar': str(avatar or '')[:500],
                    'created_at': int(time.time()),
                    'last_used': int(time.time()),
                }
                global ACTIVE_ACCOUNT_ID
                with AUTH_LOCK:
                    load_auth_store()
                    AUTH_ACCOUNTS.append(account)
                    _save_auth_store_locked()
                    ACTIVE_ACCOUNT_ID = account['id']
                    state.update({
                        'status': 'ready',
                        'message': f'已登录：{account["name"]}',
                        'name': account['name'],
                        'avatar': account['avatar'],
                    })
                return
            time.sleep(1)
        _set_login_state(state, status='failed', message='扫码登录超时，请重新发起登录')
    except Exception as exc:
        if state['cancel'].is_set():
            _set_login_state(state, status='cancelled', message='登录已取消')
        else:
            _set_login_state(state, status='failed', message=str(exc)[:200])
    finally:
        if page:
            try:
                page.ws.close()
            except Exception:
                pass
        if session:
            session.quit()
        with AUTH_LOCK:
            if AUTH_LOGIN is state:
                state['session'] = None
        if state.get('status') in ('failed', 'cancelled') and os.path.isdir(profile_dir):
            try:
                shutil.rmtree(profile_dir)
            except OSError:
                pass


def start_wechat_login():
    """创建一次新的微信扫码登录任务；每个账号使用独立浏览器配置。"""
    global AUTH_LOGIN
    load_auth_store()
    with AUTH_LOCK:
        if AUTH_LOGIN and AUTH_LOGIN.get('status') in ('starting', 'waiting'):
            return AUTH_LOGIN
        account_id = uuid.uuid4().hex
        profile_dir = _account_profile_dir(account_id)
        state = {
            'id': uuid.uuid4().hex[:12],
            'account_id': account_id,
            'profile_dir': profile_dir,
            'status': 'starting',
            'message': '正在准备登录窗口',
            'name': '',
            'avatar': '',
            'cancel': threading.Event(),
            'session': None,
        }
        AUTH_LOGIN = state
    threading.Thread(target=_run_wechat_login, args=(state,), daemon=True).start()
    return state


def cancel_wechat_login():
    """取消待完成的扫码任务。"""
    with AUTH_LOCK:
        if not AUTH_LOGIN or AUTH_LOGIN.get('status') not in ('starting', 'waiting'):
            return False
        state = AUTH_LOGIN
        state['cancel'].set()
        state['message'] = '正在关闭登录窗口'
        session = state.get('session')
    if session:
        session.quit()
    return True


def select_account(account_id):
    """切换当前使用的账号；None 表示游客模式。"""
    global ACTIVE_ACCOUNT_ID
    cancel_wechat_login()
    load_auth_store()
    account = _account_record(account_id) if account_id else None
    if account_id and not account:
        raise ValueError('记忆账号不存在')
    with AUTH_LOCK:
        ACTIVE_ACCOUNT_ID = account['id'] if account else None
        if account:
            account['last_used'] = int(time.time())
            _save_auth_store_locked()
    return _auth_summary(account)


def auth_state_payload():
    """生成登录状态接口数据，不返回任何登录凭据。"""
    load_auth_store()
    with AUTH_LOCK:
        accounts = sorted(AUTH_ACCOUNTS, key=lambda x: x.get('last_used', 0), reverse=True)
        active = _account_record(ACTIVE_ACCOUNT_ID) if ACTIVE_ACCOUNT_ID else None
        login = None
        if AUTH_LOGIN:
            login = {
                'id': AUTH_LOGIN['id'],
                'status': AUTH_LOGIN['status'],
                'message': AUTH_LOGIN.get('message', ''),
                'name': AUTH_LOGIN.get('name', ''),
            }
    used, remaining = guest_usage()
    return {
        'ok': True,
        'mode': 'account' if active else 'guest',
        'active': _auth_summary(active),
        'accounts': [_auth_summary(a) for a in accounts],
        'guest': {'used': used, 'remaining': remaining, 'limit': GUEST_LIMIT},
        'account_limit': ACCOUNT_LIMIT,
        'login': login,
    }


def download_identity(account_id):
    """校验下载身份并返回配置目录和显示名称。"""
    if account_id:
        account = _account_record(account_id)
        if not account:
            raise ValueError('记忆账号不存在，请重新登录')
        return _account_profile_dir(account['id']), account['id'], account['name']
    _ensure_app_data()
    return GUEST_PROFILE, None, '游客'


# ============================ 本地 Web 服务 ============================

PROXY = os.environ.get('HAO_PROXY')  # 设为 http(s)://host:port 走代理；留空直连
JOBS = {}                 # jid -> 进度对象
JOB_LOCK = threading.RLock()
_LIST_CACHE = OrderedDict()  # (kind, page, type_id) -> (过期时间, 列表数据)
_LIST_CACHE_LOCK = threading.Lock()
_LIST_CACHE_TTL = 60
_LIST_CACHE_MAX_ITEMS = 32
_THUMB_CACHE = OrderedDict()  # fileId -> jpeg 字节
_THUMB_CACHE_LOCK = threading.Lock()
_THUMB_FETCH_LIMIT = threading.BoundedSemaphore(6)
_THUMB_CACHE_MAX_ITEMS = 128
_CLIENT_DISCONNECTED = (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError, TimeoutError)
_NO_SWITCH = object()


def cleanup_runtime_cache():
    """清空运行时缓存并删除临时 Chromium 用户目录。"""
    cancel_wechat_login()
    with _LIST_CACHE_LOCK:
        _LIST_CACHE.clear()
    with _THUMB_CACHE_LOCK:
        _THUMB_CACHE.clear()
    if not os.path.isdir(TMP):
        return True
    for _ in range(10):
        try:
            shutil.rmtree(TMP)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.5)
    return False


atexit.register(cleanup_runtime_cache)


def _job_public(job):
    """过滤下载任务内部对象，返回可安全序列化的进度数据。"""
    with JOB_LOCK:
        return {
            'ok': job.get('ok', True),
            'id': job.get('id'),
            'kind': job.get('kind'),
            'total': job.get('total', 0),
            'done': job.get('done', 0),
            'got': job.get('got', 0),
            'results': [dict(r) for r in job.get('results', [])],
            'finished': job.get('finished', False),
            'status': job.get('status', 'running'),
            'out_dir': job.get('out_dir'),
            'account_id': job.get('account_id'),
            'account_name': job.get('account_name', '游客'),
            'next_index': job.get('next_index', 0),
            'error': job.get('error', ''),
        }


def _has_active_job():
    """检查是否已有未结束的下载任务，避免多个任务争用同一浏览器配置。"""
    with JOB_LOCK:
        return any(not job.get('finished') for job in JOBS.values())


def _take_switch_request(job):
    """取出用户请求的账号切换；None 也表示有效的游客切换请求。"""
    with JOB_LOCK:
        if not job.pop('_switch_pending', False):
            return _NO_SWITCH
        return job.pop('_switch_account', None)


def run_download_job(jid, kind, items, headless=True, account_id=None, start_index=0):
    """后台线程：复用浏览器逐个下载，并在单张任务之间切换账号。"""
    job = JOBS[jid]
    out_dir = job['out_dir']
    got = job.get('got', 0)
    sess = None
    index = start_index
    try:
        profile_dir, account_id, account_name = download_identity(account_id)
        sess = BrowserSession(headless, PROXY, profile_dir=profile_dir)
        with JOB_LOCK:
            job['account_id'] = account_id
            job['account_name'] = account_name
        while index < len(items):
            requested_account = _take_switch_request(job)
            if requested_account is not _NO_SWITCH and requested_account != account_id:
                new_profile, new_account_id, new_account_name = download_identity(requested_account)
                if sess:
                    sess.quit()
                    sess = None
                sess = BrowserSession(headless, PROXY, profile_dir=new_profile)
                account_id = new_account_id
                with JOB_LOCK:
                    job['account_id'] = new_account_id
                    job['account_name'] = new_account_name

            with JOB_LOCK:
                job['next_index'] = index
            it = items[index]
            wid = it.get('wtId')
            rec = {'file': '', 'status': 'running', 'msg': ''}
            job['results'].append(rec)

            if account_id is None and guest_usage()[1] <= 0:
                rec['status'] = 'quota'
                rec['msg'] = '游客今日本设备配额已用完'
                with JOB_LOCK:
                    job['status'] = 'quota'
                    job['finished'] = True
                    job['next_index'] = index
                break

            direct, status, suggested_filename = None, None, None
            for _ in range(2):  # 偶发 CDP 断线最多重试 1 次（浏览器会话仍存活）
                direct, status, suggested_filename = get_one(sess, kind, wid)
                if status != 'CONN_LOST':
                    break
            if status == 'QUOTA':
                rec['status'] = 'quota'
                rec['msg'] = '当前账号每日配额已用完，请切换账号后继续'
                with JOB_LOCK:
                    job['status'] = 'quota'
                    job['finished'] = True
                    job['next_index'] = index
                break
            if status == 'AUTH_EXPIRED':
                rec['status'] = 'auth'
                rec['msg'] = '登录已失效，请切换账号或重新扫码登录'
                with JOB_LOCK:
                    job['status'] = 'auth_expired'
                    job['finished'] = True
                    job['next_index'] = index
                break
            if not direct:
                rec['status'] = 'fail'
                rec['msg'] = status
                with JOB_LOCK:
                    job['done'] += 1
                    job['next_index'] = index + 1
                index += 1
                continue
            try:
                if account_id is None:
                    record_guest_download()
                filename, sz = download(direct, out_dir, suggested_filename)
                rec['file'] = filename
                rec['status'] = 'ok'
                rec['msg'] = f'{sz / 1024:.0f} KB'
                got += 1
            except Exception as ex:
                rec['status'] = 'fail'
                rec['msg'] = str(ex)[:80]
            with JOB_LOCK:
                job['done'] += 1
                job['got'] = got
                job['next_index'] = index + 1
            index += 1
            time.sleep(1)
        else:
            with JOB_LOCK:
                job['status'] = 'done'
                job['finished'] = True
                job['next_index'] = len(items)
    except Exception as exc:
        with JOB_LOCK:
            job['status'] = 'error'
            job['error'] = str(exc)[:200]
            job['finished'] = True
            job['next_index'] = index
    finally:
        if sess:
            sess.quit()
        with JOB_LOCK:
            job['got'] = got
            if job['status'] == 'running':
                job['status'] = 'done'
                job['finished'] = True


def switch_download_account(jid, account_id):
    """请求当前任务在下一张壁纸前切换账号，配额中断时直接续传。"""
    profile_dir, selected_id, selected_name = download_identity(account_id)
    del profile_dir
    with JOB_LOCK:
        job = JOBS.get(jid)
        if not job:
            raise ValueError('下载任务不存在')
        if not job.get('finished'):
            job['_switch_account'] = selected_id
            job['_switch_pending'] = True
            return {'status': 'switch_pending', 'account_name': selected_name}
        if job.get('status') not in ('quota', 'auth_expired'):
            raise ValueError('当前任务不能切换账号')
        start_index = int(job.get('next_index', 0))
        if start_index >= job.get('total', 0):
            raise ValueError('当前任务没有待下载内容')
        if job.get('results') and job['results'][-1].get('status') in ('quota', 'auth'):
            job['results'].pop()
        job['status'] = 'running'
        job['finished'] = False
        job['account_id'] = selected_id
        job['account_name'] = selected_name
        items = list(job['_items'])
        kind = job['kind']
        headless = job['headless']
    threading.Thread(
        target=run_download_job,
        args=(jid, kind, items, headless, selected_id, start_index),
        daemon=True).start()
    return {'status': 'resumed', 'account_name': selected_name}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默访问日志

    def _write_body(self, body, ctype, code=200, headers=None):
        """向本地浏览器发送响应；客户端提前断开时安静结束。"""
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECTED:
            return False
        return True

    def _send(self, body, ctype):
        return self._write_body(body, ctype)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        return self._write_body(body, 'application/json; charset=utf-8', code)

    def _send_error(self, code, message=None):
        """发送错误响应；浏览器已断开时不再制造二次异常。"""
        try:
            self.send_error(code, message)
        except _CLIENT_DISCONNECTED:
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/':
            self._send(HTML_PAGE.encode('utf-8'), 'text/html; charset=utf-8')

        elif path == '/favicon.ico':
            self._write_body(b'', 'image/x-icon', 204)

        elif path == '/api/auth/state':
            self._json(auth_state_payload())

        elif path == '/api/categories':
            try:
                cats = [{'id': c.get('id'), 'name': c.get('typeName', '')} for c in get_categories()]
                self._json({'ok': True, 'categories': cats})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, _remote_error_status(e))

        elif path == '/api/list':
            try:
                kind = qs.get('kind', ['home'])[0]
                type_id = qs.get('type_id', [None])[0]
                page = int(qs.get('page', ['1'])[0])
                data = get_list(kind, page, type_id)
                if data is None:
                    raise RuntimeError('远端列表响应解析失败')
                items = [{
                    'wtId': it.get('wtId'),
                    'fileId': it.get('fileId'),
                    'title': _item_title(it),
                    'rw': it.get('rw'),
                    'rh': it.get('rh'),
                    'fileMb': it.get('fileMb'),
                } for it in (data.get('list') or [])]
                self._json({'ok': True, 'page': page, 'pages': data.get('pages', 0),
                            'total': data.get('total', 0), 'items': items})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, _remote_error_status(e))

        elif path == '/thumb':
            fid = qs.get('fileId', [''])[0]
            if not fid:
                self._send_error(400)
                return
            with _THUMB_CACHE_LOCK:
                data = _THUMB_CACHE.get(fid)
                if data is not None:
                    _THUMB_CACHE.move_to_end(fid)
            if data is None:
                try:
                    with _THUMB_FETCH_LIMIT:
                        with _THUMB_CACHE_LOCK:
                            data = _THUMB_CACHE.get(fid)
                            if data is not None:
                                _THUMB_CACHE.move_to_end(fid)
                        if data is None:
                            data = fetch_thumb(fid)
                            if data:
                                with _THUMB_CACHE_LOCK:
                                    _THUMB_CACHE[fid] = data
                                    _THUMB_CACHE.move_to_end(fid)
                                    while len(_THUMB_CACHE) > _THUMB_CACHE_MAX_ITEMS:
                                        _THUMB_CACHE.popitem(last=False)
                except Exception as e:
                    self._send_error(_remote_error_status(e), '缩略图获取失败')
                    return
            if not data:
                self._send_error(404)
                return
            ctype = ('image/webp' if data[:4] == b'RIFF' else
                     'image/png' if data[:4] == b'\x89PNG' else
                     'image/gif' if data[:4] == b'GIF8' else 'image/jpeg')
            self._write_body(data, ctype, headers={'Cache-Control': 'max-age=86400'})

        elif path == '/api/progress':
            jid = qs.get('id', [''])[0]
            job = JOBS.get(jid)
            self._json(_job_public(job) if job else {'ok': False, 'error': 'no such job'})

        else:
            self._send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/auth/start':
            try:
                state = start_wechat_login()
                self._json({'ok': True, 'id': state['id'], 'status': state['status'],
                            'message': state.get('message', '')})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)
            return
        if path == '/api/auth/cancel':
            self._json({'ok': cancel_wechat_login()})
            return

        try:
            n = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(n) or b'{}')
        except Exception as e:
            self._json({'ok': False, 'error': f'请求数据无效：{e}'}, 400)
            return

        if path == '/api/auth/select':
            try:
                account_id = payload.get('account_id') or None
                account = select_account(account_id)
                self._json({'ok': True, 'active': account, 'mode': 'account' if account else 'guest'})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 400)
            return

        if path == '/api/download/switch':
            try:
                account_id = payload.get('account_id') or None
                result = switch_download_account(str(payload.get('id') or ''), account_id)
                self._json({'ok': True, **result})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 400)
            return

        if path != '/api/download':
            self._send_error(404)
            return
        try:
            kind = payload.get('kind', 'home')
            items = payload.get('items') or []
            if not isinstance(items, list) or not items:
                self._json({'ok': False, 'error': '未选择任何壁纸'}, 400)
                return
            if _has_active_job():
                self._json({'ok': False, 'error': '已有下载任务正在进行，请等待完成'}, 409)
                return
            account_id = payload['account_id'] if 'account_id' in payload else ACTIVE_ACCOUNT_ID
            account_id = account_id or None
            _, selected_id, account_name = download_identity(account_id)
            if selected_id is None:
                remaining = guest_usage()[1]
                if len(items) > remaining:
                    self._json({'ok': False,
                                'error': f'游客模式今日还可下载 {remaining} 张，请减少选择或登录账号'}, 400)
                    return
            label = '手机' if kind == 'mobile' else '电脑'
            cat_name = _sanitize(payload.get('cat_name') or '壁纸')
            out_dir = os.path.join(os.getcwd(), label, cat_name)
            os.makedirs(out_dir, exist_ok=True)
            headless = bool(payload.get('headless', True))  # 网页端勾选「可见窗口」时为 False
            jid = uuid.uuid4().hex[:12]
            with JOB_LOCK:
                JOBS[jid] = {
                    'ok': True, 'id': jid, 'kind': kind, 'total': len(items),
                    'done': 0, 'got': 0, 'results': [], 'finished': False,
                    'status': 'running', 'out_dir': out_dir,
                    'account_id': selected_id, 'account_name': account_name,
                    'next_index': 0, '_items': list(items), 'headless': headless,
                }
            threading.Thread(
                target=run_download_job,
                args=(jid, kind, list(items), headless, selected_id), daemon=True).start()
            self._json({'ok': True, 'id': jid, 'out_dir': out_dir})
        except Exception as e:
            code = 400 if isinstance(e, ValueError) else 500
            self._json({'ok': False, 'error': str(e)}, code)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>哲风壁纸下载器</title>
<style>
  :root{--bg:#f4f5f7;--card:#fff;--line:#e6e8eb;--txt:#1f2329;--sub:#8a9099;
        --accent:#2f6bff;--accent-soft:#eaf0ff;--ok:#12b76a;--bad:#f04438;}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt);}
  header{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid var(--line);
         padding:12px 18px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;}
  h1{font-size:16px;margin:0 10px 0 0;font-weight:700;white-space:nowrap}
  .tabs{display:flex;background:var(--bg);border-radius:10px;padding:3px}
  .tabs button{border:0;background:transparent;padding:7px 20px;border-radius:8px;cursor:pointer;font-size:14px;color:var(--sub)}
  .tabs button.on{background:#fff;color:var(--txt);box-shadow:0 1px 3px rgba(0,0,0,.12);font-weight:600}
  select{padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:14px;background:#fff;color:var(--txt);min-width:170px}
  .grow{flex:1}
  .count{font-size:13px;color:var(--sub);white-space:nowrap}
  .auth-area{display:flex;align-items:center}
  .auth-button{display:flex;align-items:center;gap:7px;min-height:36px;padding:6px 10px;border:1px solid var(--line);
        border-radius:9px;background:#fff;color:var(--txt);cursor:pointer;font-size:13px;font-weight:600;max-width:210px}
  .auth-button:hover{border-color:#b7c7ee;background:var(--accent-soft)}
  .auth-button img{width:24px;height:24px;border-radius:50%;object-fit:cover;background:#eceef1}
  .auth-button span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  main{padding:16px 18px 120px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px}
  .card{background:var(--card);border:2px solid transparent;border-radius:12px;overflow:hidden;cursor:pointer;
        box-shadow:0 1px 3px rgba(0,0,0,.06);transition:.15s;position:relative}
  .card:hover{box-shadow:0 6px 18px rgba(0,0,0,.12);transform:translateY(-2px)}
  .card.sel{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  .thumb{width:100%;aspect-ratio:3/4;object-fit:cover;display:block;background:#eceef1}
  .meta{padding:8px 10px}
  .tt{font-size:13px;line-height:1.35;height:2.7em;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .dim{font-size:12px;color:var(--sub);margin-top:4px;display:flex;justify-content:space-between;gap:6px}
  .tick{position:absolute;top:8px;right:8px;width:26px;height:26px;border-radius:50%;background:var(--accent);color:#fff;
        display:none;align-items:center;justify-content:center;font-size:15px;box-shadow:0 2px 6px rgba(0,0,0,.25)}
  .card.sel .tick{display:flex}
  .pager{display:flex;gap:10px;align-items:center;justify-content:center;margin:22px 0}
  .pager button{padding:8px 16px;border:1px solid var(--line);background:#fff;border-radius:8px;cursor:pointer;font-size:14px}
  .pager button:disabled{opacity:.45;cursor:not-allowed}
  .bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid var(--line);
       padding:12px 18px;display:flex;gap:12px;align-items:center;box-shadow:0 -2px 12px rgba(0,0,0,.06)}
  .btn{border:0;border-radius:9px;padding:10px 22px;font-size:14px;cursor:pointer;font-weight:600}
  .btn.primary{background:var(--accent);color:#fff}
  .btn.primary:disabled{background:#c3cee6;cursor:not-allowed}
  .btn.ghost{background:var(--bg);color:var(--txt)}
  .toggle{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--sub);cursor:pointer;user-select:none;margin-right:4px}
  .toggle input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
  .panel{position:fixed;right:18px;bottom:78px;width:380px;max-height:60vh;overflow:auto;background:#fff;
         border:1px solid var(--line);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.16);display:none;padding:12px 14px}
  .panel.on{display:block}
  .panel h3{margin:0 0 8px;font-size:14px}
  .row{display:flex;justify-content:space-between;gap:8px;font-size:13px;padding:5px 0;border-bottom:1px dashed var(--line)}
  .row span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px}
  .st{color:var(--sub);white-space:nowrap} .ok{color:var(--ok);white-space:nowrap} .fail{color:var(--bad);white-space:nowrap}
  .prog{height:8px;background:var(--bg);border-radius:6px;overflow:hidden;margin:8px 0}
  .prog > i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
  .hint{color:var(--sub);font-size:14px;text-align:center;padding:60px}
  .modal-backdrop{position:fixed;inset:0;z-index:30;display:none;align-items:center;justify-content:center;
        padding:16px;background:rgba(31,35,41,.42)}
  .modal-backdrop.on{display:flex}
  .auth-dialog{width:min(470px,100%);max-height:calc(100vh - 32px);overflow:auto;background:#fff;border:1px solid var(--line);
        border-radius:12px;box-shadow:0 18px 50px rgba(31,35,41,.22);padding:20px}
  .auth-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}
  .auth-head h2{font-size:18px;margin:0;font-weight:700}
  .auth-caption{font-size:13px;line-height:1.5;color:var(--sub);margin-top:6px}
  .icon-btn{width:32px;height:32px;border:0;border-radius:8px;background:var(--bg);color:var(--sub);font-size:20px;line-height:1;cursor:pointer}
  .icon-btn:hover{background:var(--accent-soft);color:var(--txt)}
  .auth-section-title{font-size:12px;color:var(--sub);margin:18px 0 8px}
  .auth-list{display:grid;gap:8px;max-height:190px;overflow:auto}
  .auth-account{display:flex;align-items:center;gap:10px;width:100%;padding:10px;border:1px solid var(--line);
        border-radius:9px;background:#fff;color:var(--txt);cursor:pointer;text-align:left}
  .auth-account:hover{border-color:#b7c7ee;background:var(--accent-soft)}
  .auth-account img,.auth-initial{width:34px;height:34px;flex:none;border-radius:50%;object-fit:cover;background:#eaf0ff;color:var(--accent);
        display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:700}
  .auth-account strong{display:block;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .auth-account small{display:block;color:var(--sub);font-size:12px;margin-top:3px}
  .auth-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px}
  .auth-actions .btn{padding:10px 12px}
  .auth-status{display:none;margin-top:14px;padding:10px 12px;border:1px solid #c8d6ff;border-radius:9px;background:var(--accent-soft);color:var(--txt);font-size:13px;line-height:1.5}
  .auth-status.on{display:flex;align-items:center;justify-content:space-between;gap:10px}
  .auth-status button{border:0;background:transparent;color:var(--accent);cursor:pointer;font-size:13px;white-space:nowrap}
  .auth-foot{color:var(--sub);font-size:12px;line-height:1.5;margin-top:14px}
  @media(max-width:640px){
    header{padding:10px 12px;gap:8px}
    h1{width:100%;margin-right:0}
    .auth-area{margin-left:auto}
    .auth-button{max-width:180px}
    .auth-actions{grid-template-columns:1fr}
    .panel{left:12px;right:12px;width:auto}
  }
</style>
</head>
<body>
<header>
  <h1>哲风壁纸下载器</h1>
  <div class="tabs" id="tabs">
    <button data-kind="mobile">手机</button>
    <button data-kind="home" class="on">电脑</button>
  </div>
  <select id="cat"></select>
  <div class="grow"></div>
  <div class="count" id="count">已选 0 张</div>
  <div class="auth-area"><button class="auth-button" id="authbtn" aria-haspopup="dialog">
    <span id="auth-label">选择身份</span>
  </button></div>
</header>
<div class="modal-backdrop on" id="auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-title">
  <section class="auth-dialog">
    <div class="auth-head">
      <div><h2 id="auth-title">选择下载身份</h2><div class="auth-caption" id="auth-caption">请选择游客模式或使用微信扫码登录。</div></div>
      <button class="icon-btn" id="auth-close" aria-label="关闭" title="关闭">×</button>
    </div>
    <div class="auth-section-title">已记忆账号</div>
    <div class="auth-list" id="auth-list"></div>
    <div class="auth-actions">
      <button class="btn ghost" id="guest-mode">游客模式</button>
      <button class="btn primary" id="wechat-login">微信扫码登录</button>
    </div>
    <div class="auth-status" id="auth-status"><span id="auth-status-text"></span><button id="auth-cancel">取消</button></div>
    <div class="auth-foot" id="auth-foot">登录凭据由本机浏览器配置保存，账号索引只记录昵称和头像。</div>
  </section>
</div>
<main>
  <div class="hint" id="hint">加载中…</div>
  <div class="grid" id="grid"></div>
  <div class="pager" id="pager" style="display:none">
    <button id="prev">上一页</button>
    <span class="count" id="pageinfo"></span>
    <button id="next">下一页</button>
  </div>
</main>
<div class="panel" id="panel">
  <h3>下载进度</h3>
  <div class="prog"><i id="pbar"></i></div>
  <div id="plist"></div>
</div>
<div class="bar">
  <div class="grow"></div>
  <button class="btn ghost" id="clear">清空</button>
  <button class="btn ghost" id="selall">全选本页</button>
  <label class="toggle"><input type="checkbox" id="vis"> 可见窗口</label>
  <button class="btn primary" id="dl" disabled>下载所选</button>
</div>
<script>
const state={kind:'home',typeId:'',catName:'',page:1,pages:1,items:[],sel:new Map(),jobId:null,timer:null,loadController:null,loadRequest:0,pageCache:new Map(),pagePending:new Map(),loading:false,authReady:false,contentStarted:false,jobFinished:false,jobRunning:false,jobStatus:'',auth:{mode:'guest',accountId:null,name:'游客',active:null,accounts:[],guest:{used:0,remaining:5,limit:5},accountLimit:10,login:null,loginId:null,loginTimer:null}};
const $=s=>document.querySelector(s);
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(u,opt){
  const r=await fetch(u,opt);
  const text=await r.text();
  let d;
  try{d=JSON.parse(text);}catch(_){throw new Error('服务器响应无效（HTTP '+r.status+'）');}
  if(!r.ok&&(!d||typeof d!=='object'||d.ok===undefined))return {ok:false,error:'请求失败（HTTP '+r.status+'）'};
  return d;
}
function setHint(t){const h=$('#hint');h.textContent=t;h.style.display=t?'block':'none';}
function listKey(kind,typeId,page){return kind+'|'+typeId+'|'+page;}
function listUrl(kind,typeId,page){return '/api/list?kind='+encodeURIComponent(kind)+'&type_id='+encodeURIComponent(typeId)+'&page='+page;}
function prefetchPage(page,kind,typeId){
  const key=listKey(kind,typeId,page);
  if(state.pageCache.has(key)||state.pagePending.has(key))return;
  const pending=api(listUrl(kind,typeId,page)).then(d=>{
    if(d&&d.ok)state.pageCache.set(key,d);
    return d;
  }).catch(()=>null);
  state.pagePending.set(key,pending);
  pending.finally(()=>{if(state.pagePending.get(key)===pending)state.pagePending.delete(key);});
}
function updateBar(){$('#count').textContent=`已选 ${state.sel.size} 张`;$('#dl').disabled=state.sel.size===0||!state.authReady||state.jobRunning;}
function avatarSrc(url){
  const m=String(url||'').match(/\/getCroppingImg\/([^/?#]+)/);
  return m?('/thumb?fileId='+encodeURIComponent(m[1])):'';
}
function updateAuthButton(){
  const a=state.auth.active;
  const label=a?('微信：'+a.name+' · '+state.auth.accountLimit):('游客 · '+state.auth.guest.remaining+'/'+state.auth.guest.limit);
  const b=$('#authbtn');
  const avatar=a?avatarSrc(a.avatar):'';
  b.innerHTML=(avatar?'<img src="'+avatar+'" alt="">':'')+'<span>'+esc(label)+'</span>';
  b.title=a?('当前账号：'+a.name):'当前为游客模式';
}
function setAuthState(d){
  state.auth.mode=d.mode||'guest';state.auth.active=d.active||null;state.auth.accountId=d.active?.id||null;
  state.auth.name=d.active?.name||'游客';state.auth.accounts=d.accounts||[];state.auth.guest=d.guest||state.auth.guest;
  state.auth.accountLimit=d.account_limit||10;state.auth.login=d.login||null;updateAuthButton();renderAuthModal();updateBar();
}
function renderAuthModal(){
  const list=$('#auth-list');
  if(!state.auth.accounts.length){list.innerHTML='<div class="auth-foot">暂无已记忆账号</div>';}
  else list.innerHTML=state.auth.accounts.map(a=>{
    const avatar=avatarSrc(a.avatar);
    const icon=avatar?'<img src="'+avatar+'" alt="">':'<span class="auth-initial">'+esc((a.name||'账').slice(0,1))+'</span>';
    return '<button class="auth-account" data-account-id="'+esc(a.id)+'">'+icon+'<span><strong>'+esc(a.name)+'</strong><small>每日最多 '+state.auth.accountLimit+' 张 · 使用此账号</small></span></button>';
  }).join('');
  $('#guest-mode').textContent='游客模式 · '+state.auth.guest.remaining+'/'+state.auth.guest.limit;
  const login=state.auth.login;
  const pending=login&&login.id===state.authLoginId&&['starting','waiting'].includes(login.status);
  $('#auth-status').classList.toggle('on',!!pending);
  if(pending)$('#auth-status-text').textContent=login.message||'请在弹出的浏览器窗口中扫码登录';
  $('#auth-close').disabled=!state.authReady;
  $('#auth-caption').textContent=state.auth.active?('当前使用：'+state.auth.active.name+'，可切换到其他已记忆账号。'):'请选择游客模式或使用微信扫码登录。';
}
async function refreshAuthState(){const d=await api('/api/auth/state');if(!d.ok)throw new Error(d.error||'登录状态获取失败');setAuthState(d);return d;}
async function ensureContent(){if(state.contentStarted)return;state.contentStarted=true;await loadCats();}
async function selectAuth(accountId){
  const d=await api('/api/auth/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_id:accountId})});
  if(!d.ok){alert('切换身份失败：'+(d.error||''));return;}
  await refreshAuthState();
  const canSwitch=state.jobId&&state.jobStatus!=='done'&&state.jobStatus!=='error';
  if(canSwitch){
    const sw=await api('/api/download/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:state.jobId,account_id:state.auth.accountId})});
    if(!sw.ok)alert('当前任务切换失败：'+(sw.error||''));
  }
  state.authReady=true;$('#auth-modal').classList.remove('on');renderAuthModal();updateBar();await ensureContent();
}
async function startWechatLogin(){
  const d=await api('/api/auth/start',{method:'POST'});
  if(!d.ok){alert('启动扫码登录失败：'+(d.error||''));return;}
  state.authLoginId=d.id;$('#auth-modal').classList.add('on');renderAuthModal();pollWechatLogin();
}
async function pollWechatLogin(){
  if(!state.authLoginId)return;
  try{
    const d=await refreshAuthState(),login=d.login;
    if(login&&login.id===state.authLoginId&&['starting','waiting'].includes(login.status)){
      state.auth.login=login;renderAuthModal();state.auth.loginTimer=setTimeout(pollWechatLogin,800);return;
    }
    if(login&&login.id===state.authLoginId&&login.status==='ready'){
      state.authLoginId=null;state.auth.loginId=null;state.authReady=true;$('#auth-modal').classList.remove('on');renderAuthModal();updateBar();await ensureContent();return;
    }
    state.authLoginId=null;state.auth.loginId=null;renderAuthModal();
  }catch(e){$('#auth-status').classList.add('on');$('#auth-status-text').textContent=e.message||'登录状态获取失败';}
}
async function cancelWechatLogin(){
  if(state.auth.loginTimer)clearTimeout(state.auth.loginTimer);state.auth.loginTimer=null;
  await api('/api/auth/cancel',{method:'POST'});state.authLoginId=null;state.auth.loginId=null;await refreshAuthState();
}

async function loadCats(){
  try{
    const d=await api('/api/categories');
    if(!d.ok){setHint('分类加载失败：'+(d.error||''));return;}
    const sel=$('#cat');sel.innerHTML='';
    d.categories.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.name;o.dataset.name=c.name;sel.appendChild(o);});
    state.typeId=sel.value;state.catName=sel.options[sel.selectedIndex].dataset.name;
    await loadPage(1);
  }catch(e){setHint('分类加载失败：'+(e.message||'网络错误'));}
}
async function loadPage(p){
  if(state.loadController)state.loadController.abort();
  const requestNo=++state.loadRequest;
  const controller=new AbortController();
  const viewKind=state.kind;
  const viewTypeId=state.typeId;
  const pageKey=listKey(viewKind,viewTypeId,p);
  state.page=p;setHint('加载中…');$('#grid').innerHTML='';$('#pager').style.display='none';
  state.loadController=controller;
  state.loading=true;
  $('#prev').disabled=true;$('#next').disabled=true;
  try{
    let d=state.pageCache.get(pageKey);
    if(!d){
      const pending=state.pagePending.get(pageKey);
      d=pending ? await pending : await api(listUrl(viewKind,viewTypeId,p),{signal:controller.signal});
      if(!d)throw new Error('页面加载失败');
      if(d&&d.ok)state.pageCache.set(pageKey,d);
    }
    if(requestNo!==state.loadRequest)return;
    if(!d.ok){setHint('加载失败：'+(d.error||''));return;}
    state.items=d.items||[];state.pages=d.pages||1;
    setHint(state.items.length?'':'该页没有内容');
    const g=$('#grid');
    state.items.forEach(it=>{
      const card=document.createElement('div');
      card.className='card'+(state.sel.has(it.wtId)?' sel':'');
      card.innerHTML='<img class="thumb" loading="lazy" src="/thumb?fileId='+encodeURIComponent(it.fileId)+'" alt="">'+
        '<div class="tick">✓</div>'+
        '<div class="meta"><div class="tt">'+esc(it.title)+'</div>'+
        '<div class="dim"><span>'+(it.rw||'?')+'×'+(it.rh||'?')+'</span><span>'+esc(it.fileMb||'')+'</span></div></div>';
      card.onclick=()=>{if(state.sel.has(it.wtId))state.sel.delete(it.wtId);else state.sel.set(it.wtId,it);card.classList.toggle('sel');updateBar();};
      g.appendChild(card);
    });
    $('#pager').style.display=state.items.length?'flex':'none';
    $('#pageinfo').textContent='第 '+d.page+' / '+d.pages+' 页（本页 '+state.items.length+' 张）';
    updateBar();
  }catch(e){
    if(e.name!=='AbortError'&&requestNo===state.loadRequest)setHint('加载失败：'+(e.message||'网络错误'));
  }finally{
    if(requestNo===state.loadRequest){
      state.loadController=null;
      state.loading=false;
      $('#prev').disabled=state.page<=1;
      $('#next').disabled=state.page>=state.pages;
    }
  }
}

$('#authbtn').onclick=async()=>{if(!state.authReady)return;$('#auth-modal').classList.add('on');try{await refreshAuthState();}catch(e){$('#auth-caption').textContent=e.message||'登录状态获取失败';}};
$('#auth-close').onclick=()=>{if(state.authReady)$('#auth-modal').classList.remove('on');};
$('#guest-mode').onclick=()=>selectAuth(null);
$('#wechat-login').onclick=()=>startWechatLogin();
$('#auth-cancel').onclick=()=>cancelWechatLogin();
$('#auth-list').onclick=e=>{const b=e.target.closest('[data-account-id]');if(b)selectAuth(b.dataset.accountId);};
$('#tabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;
  document.querySelectorAll('#tabs button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  state.kind=b.dataset.kind;state.pageCache.clear();state.sel.clear();updateBar();loadPage(1);};
$('#cat').onchange=e=>{state.typeId=e.target.value;state.catName=e.target.options[e.target.selectedIndex].dataset.name;
  state.pageCache.clear();state.sel.clear();updateBar();loadPage(1);};
$('#prev').onclick=()=>loadPage(Math.max(1,state.page-1));
$('#next').onclick=()=>loadPage(Math.min(state.pages,state.page+1));
$('#next').onmouseenter=()=>{if(!state.loading&&state.page<state.pages)prefetchPage(state.page+1,state.kind,state.typeId);};
$('#next').onpointerdown=()=>{if(!state.loading&&state.page<state.pages)prefetchPage(state.page+1,state.kind,state.typeId);};
$('#clear').onclick=()=>{state.sel.clear();document.querySelectorAll('.card.sel').forEach(c=>c.classList.remove('sel'));updateBar();};
$('#selall').onclick=()=>{const g=$('#grid');state.items.forEach((it,i)=>{state.sel.set(it.wtId,it);if(g.children[i])g.children[i].classList.add('sel');});updateBar();};
$('#dl').onclick=async()=>{
  const items=[...state.sel.values()];
  if(!items.length)return;
  const visible=$('#vis').checked;
  const d=await api('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({kind:state.kind,cat_name:state.catName,items,headless:!visible,account_id:state.auth.accountId})});
  if(!d.ok){alert('启动下载失败：'+(d.error||''));return;}
  state.jobId=d.id;state.jobFinished=false;state.jobRunning=true;state.jobStatus='running';$('#panel').classList.add('on');$('#plist').innerHTML='';$('#pbar').style.width='0';updateBar();
  if(state.timer)clearTimeout(state.timer);poll();
};
async function poll(){
  if(!state.jobId)return;
  const d=await api('/api/progress?id='+encodeURIComponent(state.jobId));
  if(!d.ok)return;
  state.jobFinished=!!d.finished;state.jobRunning=!d.finished;state.jobStatus=d.status||'';updateBar();
  const total=d.total||1,W=Math.round((d.done/total)*100);
  $('#pbar').style.width=W+'%';
  const statusText=d.status==='quota'?' · 当前账号配额用尽':(d.status==='auth_expired'?' · 登录已失效':(d.finished?' · 完成':''));
  let html=`<div class="row"><span>进度</span><span class="st">${d.done}/${d.total}${statusText}</span></div>`;
  html+=d.results.map(r=>`<div class="row"><span>${esc(r.file)}</span>
    <span class="${r.status==='ok'?'ok':(['fail','quota','auth'].includes(r.status)?'fail':'st')}">${r.status==='ok'?'OK':(['fail','quota','auth'].includes(r.status)?'失败':'…')} ${esc(r.msg||'')}</span></div>`).join('');
  if(d.finished&&['quota','auth_expired'].includes(d.status)&&d.next_index<d.total){
    html+='<div class="row"><span>后续下载</span><button class="btn ghost" id="job-switch">选择账号并继续</button></div>';
  }
  if(d.finished&&d.out_dir)html+=`<div class="row"><span>保存目录</span><span class="st">${esc(d.out_dir)}</span></div>`;
  $('#plist').innerHTML=html;
  const switchButton=$('#job-switch');
  if(switchButton)switchButton.onclick=async()=>{state.authReady=true;$('#auth-modal').classList.add('on');try{await refreshAuthState();}catch(e){$('#auth-caption').textContent=e.message||'登录状态获取失败';}};
  if(!d.finished)state.timer=setTimeout(poll,1000);
}
async function boot(){
  try{await refreshAuthState();}
  catch(e){$('#auth-caption').textContent=e.message||'登录状态获取失败';$('#auth-status').classList.add('on');$('#auth-status-text').textContent='无法获取本地登录状态';}
}
boot();
</script>
</body>
</html>
"""


if __name__ == '__main__':
    load_auth_store()
    configure_network(PROXY)  # PROXY 来自环境变量 HAO_PROXY，None 表示直连
    if PROXY:
        print(f'使用代理：{PROXY}')
    else:
        print('网络：直连（如需代理，请设置环境变量 HAO_PROXY，例如 http://127.0.0.1:7897）')

    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = srv.server_address[1]
    url = f'http://127.0.0.1:{port}/'
    print(f'\n网页界面已启动：{url}')
    print('浏览器会自动打开；如需结束程序，请在本终端按 Ctrl+C。')
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已结束。')
    finally:
        srv.server_close()
        cleanup_runtime_cache()
