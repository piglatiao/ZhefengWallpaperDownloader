#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
哲风壁纸 原图爬取器（魅力｜迷人 分类）
========================================
原理（已逆向验证）：
  1) 解密首页 SSR 列表 -> 拿到每页壁纸的 wtId / fileId
  2) 真实浏览器打开详情页 -> 点下载 -> 通过 Altcha 人机验证（PoW 自动求解）
  3) getCompleteUrl 返回带签名的直链 down.haowallpaper.com/...png?zfsign=...
  4) 下载直链 = 原图（2~27MB，全分辨率）

重要限制（站点规则，非代码 bug）：
  - 游客按 IP 限每日下载次数；登录后解锁每日 10 次。
  - 若返回 305 "访客今日下载次数上限"，说明当日配额已用完，
    需登录（10次/日）或等次日重置后再跑本脚本。

用法：
  python get_wallpapers.py
可选：把已登录浏览器的 Cookie 通过环境变量 HAO_COOKIE 传入，可绕过游客限额。
"""
import base64
import gzip
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import websocket  # pip install websocket-client

ssl._create_default_https_context = ssl._create_unverified_context

BASE = 'https://haowallpaper.com'
TYPE_ID = '35c203f75643ac7803b8f706fa91ef40'  # 魅力｜迷人


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
OUT = os.path.join(os.getcwd(), '哲风壁纸_魅力迷人')
TMP = os.path.join(os.getcwd(), '_chrome_crawl')
os.makedirs(OUT, exist_ok=True)

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REMOTE_OPENER = None

# ---- AES 解密（列表 SSR 数据）----
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
KEY = b'68zhehao2O776519'
IV = b'aa176b7519e84710'


def decrypt(c):
    raw = bytes.fromhex(base64.b64decode(c).hex())
    return re.sub(r'\x00.*$', '', unpad(AES.new(KEY, AES.MODE_CBC, IV).decrypt(raw), 16).decode('utf-8', 'ignore'), flags=re.S)


def open_remote(req, timeout):
    """按启动时选择的网络模式打开远程请求。"""
    if REMOTE_OPENER is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return REMOTE_OPENER.open(req, timeout=timeout)


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


def choose_startup_options():
    """启动时选择浏览器窗口模式和是否使用代理，返回 headless、proxy。"""
    proxies = urllib.request.getproxies()
    system_proxy = proxies.get('https') or proxies.get('http')
    print('\n浏览器窗口模式：')
    print('  1. 当前后台无头模式（默认）')
    print('  2. 可见虚拟窗口模式')
    window_choice = input('请选择 [1/2]：').strip()
    headless = window_choice != '2'

    print('\n网络模式：')
    print('  1. 无代理，直连')
    print(f'  2. 使用已检测代理：{system_proxy}' if system_proxy else '  2. 使用代理')
    network_choice = input('请选择 [1/2]：').strip()
    proxy = None
    if network_choice == '2':
        proxy = os.environ.get('HAO_PROXY') or system_proxy
        if not proxy:
            proxy = input('请输入 HTTP/HTTPS 代理地址（例如 http://127.0.0.1:7897）：').strip()
        if not proxy:
            raise RuntimeError('未提供代理地址，已停止。')
        print(f'请先开启代理服务：{proxy}')
        confirmed = input('代理服务已开启，继续测试连接？[y/N]：').strip().lower()
        if confirmed != 'y':
            raise RuntimeError('未确认代理服务已开启，已停止。')
        if not proxy_available(proxy):
            raise RuntimeError(f'代理无法连接：{proxy}')

    configure_network(proxy)
    mode = '可见虚拟窗口' if not headless else '当前无头模式'
    route = f'代理 {proxy}' if proxy else '无代理直连'
    print(f'已选择：{mode}，{route}')
    return headless, proxy


def get_html(path):
    req = urllib.request.Request(BASE + path, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'})
    d = open_remote(req, timeout=30).read()
    try:
        d = gzip.decompress(d)
    except Exception:
        pass
    return d.decode('utf-8', 'ignore')


def get_list(kind, page=1):
    view = 'mobileView' if kind == 'mobile' else 'homeView'
    url = f'/{view}?page={page}&typeId={TYPE_ID}&sortType=3'
    h = get_html(url).replace('\\u002F', '/').replace('\\u002B', '+').replace('\\/', '/')
    for c in re.findall(r'"([A-Za-z0-9+/]{200,}={0,2})"', h):
        try:
            o = json.loads(decrypt(c))
            if isinstance(o, dict) and 'list' in o and 'pages' in o:
                return o
        except Exception:
            continue
    return None


class CDP:
    def __init__(s, ws):
        s.ws = websocket.create_connection(ws, timeout=180); s.i = 0; s.ev = []

    def send(s, m, p=None):
        s.i += 1
        s.ws.send(json.dumps({'id': s.i, 'method': m, 'params': p or {}}))
        while True:
            msg = json.loads(s.ws.recv())
            if msg.get('id') == s.i:
                return msg
            if 'method' in msg:
                s.ev.append(msg)

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
                if 'method' in msg:
                    s.ev.append(msg)
        finally:
            s.ws.settimeout(old_timeout)


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def download(url, path):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': BASE + '/'})
    d = open_remote(req, timeout=180).read()
    with open(path, 'wb') as f:
        f.write(d)
    return len(d)


def get_one(kind, wid, prof, headless=True, proxy=None):
    """开一个浏览器实例，走完 点下载->过验证->拿直链->返回直链 或 None/限额"""
    port = free_port()
    if os.path.isdir(prof):
        shutil.rmtree(prof)
    chrome_args = [CHROME]
    if proxy:
        chrome_args.append(f'--proxy-server={proxy}')
    if headless:
        chrome_args.append('--headless=new')
    else:
        chrome_args.append('--window-size=1280,900')
    chrome_args.extend([
        '--disable-gpu', '--no-sandbox',
        f'--user-data-dir={prof}', f'--remote-debugging-port={port}',
        '--remote-allow-origins=*', f'{BASE}/mobileViewLook/{wid}',
    ])
    proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = None
    try:
        time.sleep(12)
        tabs = json.loads(DIRECT_OPENER.open(f'http://127.0.0.1:{port}/json', timeout=15).read())
        pg = [t for t in tabs if t.get('type') == 'page' and 'haowallpaper' in t.get('url', '')]
        if not pg:
            return None, 'no-tab'
        c = CDP(pg[0]['webSocketDebuggerUrl'])
        c.send('Network.enable')
        c.send('Runtime.enable')
        time.sleep(1)
        c.ev.clear()
        # 点下载
        clicked = c.evl("""
          (() => { const el=document.querySelector('.hao-bottom-nav-end__face')||document.querySelector('.hao-bottom-nav-end');
            if(el){el.click();return 'clicked';} return 'NO_BTN'; })()
        """)
        if clicked != 'clicked':
            return None, str(clicked or 'NO_BTN')

        verify_loaded = False
        verify_started = False
        verify_required = False
        # 等待下载响应及页面验证组件完成（最多约 60 秒）
        for _ in range(30):
            time.sleep(2)
            c.pump()
            events, c.ev = c.ev, []
            done = None
            for e in events:
                if e.get('method') == 'Network.responseReceived':
                    u = e['params']['response'].get('url', '')
                    if '/common/file/getCompleteUrl/' in u:
                        rid = e['params']['requestId']
                        try:
                            rb = c.send('Network.getResponseBody', {'requestId': rid})
                            res = rb.get('result', {})
                            b = base64.b64decode(res['body']) if res.get('base64') else res['body'].encode()
                            txt = b.decode('utf-8', 'ignore')
                            st = e['params']['response'].get('status')
                            payload = json.loads(txt)
                            direct = payload.get('data')
                            if st == 200 and isinstance(direct, str) and 'down.haowallpaper.com' in direct:
                                done = direct; break
                            if st == 305:
                                msg = str(payload.get('msg', ''))
                                if re.search(r'下载.*(次数|额度|限制|不足|用完|上限)|次数.*(不足|限制|上限)|额度.*(不足|限制|上限)|limit', msg, re.I):
                                    return None, 'QUOTA'
                                if '3004' in msg or '错误的请求' in msg:
                                    verify_required = True
                        except Exception:
                            pass
            if done:
                return done, 'ok'

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
        return None, 'VERIFY_TIMEOUT' if verify_required else 'no-url'
    finally:
        if c:
            try:
                c.ws.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        for _ in range(10):
            if not os.path.isdir(prof):
                break
            try:
                shutil.rmtree(prof)
                break
            except OSError:
                time.sleep(0.5)


def crawl(kind, n=5, headless=True, proxy=None):
    label = '手机' if kind == 'mobile' else '电脑'
    print(f'\n===== {label}壁纸 ×{n} =====')
    got = 0
    page = 1
    seen = set()
    while got < n and page <= 4:
        data = get_list(kind, page)
        if not data:
            page += 1
            continue
        for it in data['list']:
            if got >= n:
                break
            fid, wid = it['fileId'], it['wtId']
            if wid in seen:
                continue
            seen.add(wid)
            labels = it.get('labelList') or []
            title = '-'.join(labels[:3]) or wid
            safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:55]
            fname = f"{label}_{got+1:02d}_{safe}_{it['rw']}x{it['rh']}.jpg"
            print(f'  [{got+1}/{n}] {fname} ...', end=' ', flush=True)
            direct, status = get_one(kind, wid, TMP + f'_{kind}_{got}', headless, proxy)
            if status == 'QUOTA':
                print('当日配额已用完，停止')
                return got
            if not direct:
                print('未拿到直链', status)
                got += 1
                continue
            try:
                sz = download(direct, os.path.join(OUT, fname))
                print(f'OK {sz/1024:.0f} KB')
                got += 1
            except Exception as ex:
                print('下载失败', ex)
            time.sleep(1)
        page += 1
    print(f'  → 完成 {got}/{n}')
    return got


if __name__ == '__main__':
    try:
        headless, proxy = choose_startup_options()
    except (EOFError, RuntimeError, ValueError) as ex:
        print(f'启动配置失败：{ex}')
        sys.exit(1)
    m = crawl('mobile', 5, headless, proxy)
    h = crawl('home', 5, headless, proxy)
    print(f'\n完成。本次下载 手机{m} + 电脑{h} 张，文件位于: {OUT}')
    if m + h < 10:
        print('注意：未凑满 10 张，多为站点「当日游客下载配额」限制。登录后或次日重置再跑即可。')
