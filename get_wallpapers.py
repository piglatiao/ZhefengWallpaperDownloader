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
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket  # pip install websocket-client

ssl._create_default_https_context = ssl._create_unverified_context

BASE = 'https://haowallpaper.com'
TYPE_ID = '35c203f75643ac7803b8f706fa91ef40'  # 魅力｜迷人（默认分类）


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


# 浏览器窗口模式改为在网页端按下载任务选择（默认无头、不弹窗；可勾选「可见窗口」弹小窗），
# 不再启动时交互式询问。代理仅通过环境变量 HAO_PROXY 设置（不设则直连）。


def get_html(path):
    req = urllib.request.Request(BASE + path, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'})
    d = open_remote(req, timeout=30).read()
    try:
        d = gzip.decompress(d)
    except Exception:
        pass
    return d.decode('utf-8', 'ignore')


def get_list(kind, page=1, type_id=None):
    """解密某一分类某页的壁纸列表；返回 dict(list/pages/total) 或 None。"""
    view = 'mobileView' if kind == 'mobile' else 'homeView'
    url = f'/{view}?page={page}&typeId={type_id or TYPE_ID}&sortType=3'
    h = get_html(url).replace('\\u002F', '/').replace('\\u002B', '+').replace('\\/', '/')
    for c in re.findall(r'"([A-Za-z0-9+/]{200,}={0,2})"', h):
        try:
            o = json.loads(decrypt(c))
            if isinstance(o, dict) and 'list' in o and 'pages' in o:
                return o
        except Exception:
            continue
    return None


def get_json(path):
    """请求 JSON 接口（带可选代理），返回解析后的 dict。"""
    req = urllib.request.Request(BASE + path,
                                 headers={'User-Agent': UA, 'Accept': 'application/json,*/*'})
    d = open_remote(req, timeout=30).read()
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
        headers={'User-Agent': UA, 'Referer': BASE + '/'})
    d = open_remote(req, timeout=30).read()
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


def download(url, path):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': BASE + '/'})
    d = open_remote(req, timeout=180).read()
    with open(path, 'wb') as f:
        f.write(d)
    return len(d)


# ---- 浏览器会话：一次下载任务复用同一个浏览器进程 ----
def _wait_page_ready(c, timeout=15):
    """轮询等待页面加载完成（出现底部下载导航），最多 timeout 秒。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if c.evl("document.readyState") == 'complete' and \
               c.evl("!!document.querySelector('.hao-bottom-nav-end')"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


class BrowserSession:
    """单个 Chrome 进程，供一次下载任务的全部壁纸复用；可见模式全程只弹一个窗口。"""

    def __init__(self, headless=True, proxy=None):
        self.port = free_port()
        self.prof = os.path.join(TMP, f'_job_{self.port}')
        if os.path.isdir(self.prof):
            shutil.rmtree(self.prof)
        args = [CHROME]
        if proxy:
            args.append(f'--proxy-server={proxy}')
        if headless:
            args.append('--headless=new')
        else:
            args.append('--window-size=460,780')  # 可见窗口模式：弹出的小窗尺寸
        args += ['--disable-gpu', '--no-sandbox',
                 f'--user-data-dir={self.prof}',
                 f'--remote-debugging-port={self.port}',
                 '--remote-allow-origins=*']
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_ready()

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
        try:
            self.proc.terminate(); self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill(); self.proc.wait(timeout=5)
            except Exception:
                pass
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
        c, target_id = sess.new_tab(f'{BASE}/mobileViewLook/{wid}')
        c.send('Network.enable')
        c.send('Runtime.enable')
        _wait_page_ready(c, timeout=15)

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
    except websocket.WebSocketConnectionClosedException:
        # 标签页/浏览器调试连接被对端关闭：单张失败，不击垮整个下载任务
        return None, 'CONN_LOST'
    finally:
        if c:
            try:
                c.ws.close()
            except Exception:
                pass
        if target_id:
            sess.close_tab(target_id)


# ============================ 本地 Web 服务 ============================

PROXY = os.environ.get('HAO_PROXY')  # 设为 http(s)://host:port 走代理；留空直连
JOBS = {}                 # jid -> 进度对象
_THUMB_CACHE = {}         # fileId -> jpeg 字节


def run_download_job(jid, kind, items, headless=True):
    """后台线程：整个任务复用同一个浏览器会话逐个下载。headless=False 时只弹一个可见窗口。"""
    job = JOBS[jid]
    out_dir = job['out_dir']
    label = '手机' if kind == 'mobile' else '电脑'
    got = 0
    sess = None
    try:
        sess = BrowserSession(headless, PROXY)
        for seq, it in enumerate(items, 1):
            wid = it.get('wtId')
            title = _sanitize(it.get('title') or str(wid))
            fname = f"{label}_{seq:02d}_{title}_{it.get('rw', '?')}x{it.get('rh', '?')}.jpg"
            rec = {'file': fname, 'status': 'running', 'msg': ''}
            job['results'].append(rec)
            direct, status = None, None
            for _ in range(2):  # 偶发 CDP 断线最多重试 1 次（浏览器会话仍存活）
                direct, status = get_one(sess, kind, wid)
                if status != 'CONN_LOST':
                    break
            if status == 'QUOTA':
                rec['status'] = 'fail'
                rec['msg'] = '当日配额已用完'
                job['status'] = 'quota'
                break
            if not direct:
                rec['status'] = 'fail'
                rec['msg'] = status
                job['done'] += 1
                continue
            try:
                sz = download(direct, os.path.join(out_dir, fname))
                rec['status'] = 'ok'
                rec['msg'] = f'{sz / 1024:.0f} KB'
                got += 1
            except Exception as ex:
                rec['status'] = 'fail'
                rec['msg'] = str(ex)[:80]
            job['done'] += 1
            time.sleep(1)
    finally:
        if sess:
            sess.quit()
        job['got'] = got
        job['finished'] = True
        if job['status'] == 'running':
            job['status'] = 'done'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默访问日志

    def _send(self, body, ctype):
        try:
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/':
            self._send(HTML_PAGE.encode('utf-8'), 'text/html; charset=utf-8')

        elif path == '/api/categories':
            try:
                cats = [{'id': c.get('id'), 'name': c.get('typeName', '')} for c in get_categories()]
                self._json({'ok': True, 'categories': cats})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)

        elif path == '/api/list':
            try:
                kind = qs.get('kind', ['home'])[0]
                type_id = qs.get('type_id', [None])[0]
                page = int(qs.get('page', ['1'])[0])
                data = get_list(kind, page, type_id) or {}
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
                self._json({'ok': False, 'error': str(e)}, 500)

        elif path == '/thumb':
            fid = qs.get('fileId', [''])[0]
            if not fid:
                self.send_error(400)
                return
            data = _THUMB_CACHE.get(fid)
            if data is None:
                try:
                    data = fetch_thumb(fid)
                    _THUMB_CACHE[fid] = data
                except Exception:
                    data = b''
            if not data:
                self.send_error(404)
                return
            ctype = ('image/webp' if data[:4] == b'RIFF' else
                     'image/png' if data[:4] == b'\x89PNG' else
                     'image/gif' if data[:4] == b'GIF8' else 'image/jpeg')
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'max-age=86400')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif path == '/api/progress':
            jid = qs.get('id', [''])[0]
            job = JOBS.get(jid)
            self._json(job if job else {'ok': False, 'error': 'no such job'})

        else:
            self.send_error(404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != '/api/download':
            self.send_error(404)
            return
        try:
            n = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(n) or b'{}')
            kind = payload.get('kind', 'home')
            items = payload.get('items') or []
            if not items:
                self._json({'ok': False, 'error': '未选择任何壁纸'}, 400)
                return
            label = '手机' if kind == 'mobile' else '电脑'
            cat_name = _sanitize(payload.get('cat_name') or '壁纸')
            out_dir = os.path.join(os.getcwd(), f'哲风壁纸_{label}_{cat_name}')
            os.makedirs(out_dir, exist_ok=True)
            headless = bool(payload.get('headless', True))  # 网页端勾选「可见窗口」时为 False
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {'ok': True, 'id': jid, 'kind': kind, 'total': len(items),
                         'done': 0, 'got': 0, 'results': [], 'finished': False,
                         'status': 'running', 'out_dir': out_dir}
            threading.Thread(target=run_download_job, args=(jid, kind, items, headless), daemon=True).start()
            self._json({'ok': True, 'id': jid, 'out_dir': out_dir})
        except Exception as e:
            self._json({'ok': False, 'error': str(e)}, 500)


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
</header>
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
const state={kind:'home',typeId:'',catName:'',page:1,pages:1,items:[],sel:new Map(),jobId:null,timer:null};
const $=s=>document.querySelector(s);
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(u,opt){const r=await fetch(u,opt);return r.json();}
function setHint(t){const h=$('#hint');h.textContent=t;h.style.display=t?'block':'none';}
function updateBar(){$('#count').textContent=`已选 ${state.sel.size} 张`;$('#dl').disabled=state.sel.size===0;}

async function loadCats(){
  const d=await api('/api/categories');
  if(!d.ok){setHint('分类加载失败：'+(d.error||''));return;}
  const sel=$('#cat');sel.innerHTML='';
  d.categories.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.name;o.dataset.name=c.name;sel.appendChild(o);});
  state.typeId=sel.value;state.catName=sel.options[sel.selectedIndex].dataset.name;
  loadPage(1);
}
async function loadPage(p){
  state.page=p;setHint('加载中…');$('#grid').innerHTML='';$('#pager').style.display='none';
  const d=await api(`/api/list?kind=${state.kind}&type_id=${encodeURIComponent(state.typeId)}&page=${p}`);
  if(!d.ok){setHint('加载失败：'+(d.error||''));return;}
  state.items=d.items||[];state.pages=d.pages||1;
  setHint(state.items.length?'':'该页没有内容');
  const g=$('#grid');
  state.items.forEach(it=>{
    const card=document.createElement('div');
    card.className='card'+(state.sel.has(it.wtId)?' sel':'');
    card.innerHTML=`<img class="thumb" loading="lazy" src="/thumb?fileId=${encodeURIComponent(it.fileId)}" alt="">
      <div class="tick">✓</div>
      <div class="meta"><div class="tt">${esc(it.title)}</div>
      <div class="dim"><span>${it.rw||'?'}×${it.rh||'?'}</span><span>${esc(it.fileMb||'')}</span></div></div>`;
    card.onclick=()=>{if(state.sel.has(it.wtId))state.sel.delete(it.wtId);else state.sel.set(it.wtId,it);card.classList.toggle('sel');updateBar();};
    g.appendChild(card);
  });
  $('#pager').style.display=state.items.length?'flex':'none';
  $('#pageinfo').textContent=`第 ${d.page} / ${d.pages} 页（本页 ${state.items.length} 张）`;
  $('#prev').disabled=p<=1;$('#next').disabled=p>=state.pages;
  updateBar();
}

$('#tabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;
  document.querySelectorAll('#tabs button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  state.kind=b.dataset.kind;state.sel.clear();updateBar();loadPage(1);};
$('#cat').onchange=e=>{state.typeId=e.target.value;state.catName=e.target.options[e.target.selectedIndex].dataset.name;
  state.sel.clear();updateBar();loadPage(1);};
$('#prev').onclick=()=>loadPage(Math.max(1,state.page-1));
$('#next').onclick=()=>loadPage(Math.min(state.pages,state.page+1));
$('#clear').onclick=()=>{state.sel.clear();document.querySelectorAll('.card.sel').forEach(c=>c.classList.remove('sel'));updateBar();};
$('#selall').onclick=()=>{const g=$('#grid');state.items.forEach((it,i)=>{state.sel.set(it.wtId,it);if(g.children[i])g.children[i].classList.add('sel');});updateBar();};
$('#dl').onclick=async()=>{
  const items=[...state.sel.values()];
  if(!items.length)return;
  const visible=$('#vis').checked;
  const d=await api('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({kind:state.kind,cat_name:state.catName,items,headless:!visible})});
  if(!d.ok){alert('启动下载失败：'+(d.error||''));return;}
  state.jobId=d.id;$('#panel').classList.add('on');$('#plist').innerHTML='';$('#pbar').style.width='0';
  if(state.timer)clearTimeout(state.timer);poll();
};
async function poll(){
  if(!state.jobId)return;
  const d=await api('/api/progress?id='+encodeURIComponent(state.jobId));
  if(!d.ok)return;
  const total=d.total||1,W=Math.round((d.done/total)*100);
  $('#pbar').style.width=W+'%';
  let html=`<div class="row"><span>进度</span><span class="st">${d.done}/${d.total}${d.finished?' · 完成':''}${d.status==='quota'?' · 配额用尽':''}</span></div>`;
  html+=d.results.map(r=>`<div class="row"><span>${esc(r.file)}</span>
    <span class="${r.status==='ok'?'ok':(r.status==='fail'?'fail':'st')}">${r.status==='ok'?'OK':(r.status==='fail'?'失败':'…')} ${esc(r.msg||'')}</span></div>`).join('');
  if(d.finished&&d.out_dir)html+=`<div class="row"><span>保存目录</span><span class="st">${esc(d.out_dir)}</span></div>`;
  $('#plist').innerHTML=html;
  if(!d.finished)state.timer=setTimeout(poll,1000);
}
loadCats();
</script>
</body>
</html>
"""


if __name__ == '__main__':
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
