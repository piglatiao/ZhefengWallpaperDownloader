#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
哲风壁纸 终端批量下载器（代理 IP + 无痕浏览器）
====================================================
用法：
  .venv\\Scripts\\python.exe batch_download.py
  .venv\\Scripts\\python.exe batch_download.py -k mobile -q 星空 -n 23
  .venv\\Scripts\\python.exe batch_download.py -k home -c 3 -n 50 -w 10   # -c 为分类序号

交互项（不带参数时逐项询问）：
  1 切换设备：电脑 / 手机
  2 选择分类：实时拉取 9 个分类，并列出每个分类能下的总数
  3 关键词搜索：站内搜索；单字等搜不到时自动转模糊（按标题逐字匹配）
  4 排序：最热 / 最新 / 随机
  5 数量：要下载多少张
  6 开始下载

每次选完分类或搜完词，会立刻打印「命中 N 张 · M 页」和三条示例标题，
主菜单顶部同时显示「可下：N 张」。

下载方式：
  - 全无窗口：Chrome headless=new；每张用一个全新的临时用户目录（无痕），
    结束即删除，不共享 cookie。
  - 多无痕线程：默认 10 个线程并行，每个线程绑一个代理 IP + 一个无痕浏览器，
    先领好最多 5 张再开浏览器，下满 5 张换下一个 IP，避免白占额度。
  - 游客每 IP 每天 5 张：达到 5 张主动换 IP；站点提前返回配额不足也立刻换 IP
    重下当前这张，不丢数量、不算失败。
  - 代理从 txt 读入，一行一个 http://user:password@ip:端口；程序在本地起一个带
    Basic 认证头的转发代理（Chrome 不认 URL 内嵌的代理账号密码），Chrome 和
    直链下载都走它。
  - 启动前先整份预过滤：连不上的直接跳过并记失效；429 限流会等 4 秒复验，
    连两次失败才算真失效，不会误杀。
  - 未用过的代理随机抽取，不从文件头顺着来；文件头那批多半当天已被别人用超。
  - 已用 / 失效 / 配额耗尽记录在 proxy_usage.json，按日期分周期：当天用完即跳过，
    次日自动清零重来；失效代理当天不再重试。
  - 速度：每张之间随机停 5~10 秒，换 IP 再停 3~7 秒；用 -d 4-8 可自行调整。
  - 代理端口本机直连不通时（常见于需要本机客户端出网的代理），自动探测 127.0.0.1
    上的 7897/7890/1080 等端口做上级级联；也可用 --upstream 明确指定。
"""
import argparse
import base64
import json
import os
import random
import re
import select
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

def _init_console():
    """Windows 控制台默认 GBK，中文会显成乱码；先把控制台切到 UTF-8 再把流改成 UTF-8。"""
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


_init_console()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import get_wallpapers as gw  # 复用已逆向验证的列表 / 浏览器 / 直链逻辑

# Chrome 预连接会一次给每个域名开好几条连接，代理受限时容易打满；限流后再提速
gw.BrowserSession.extra_args = (
    '--disable-background-networking',
    '--disable-component-update',
    '--disable-domain-reliability',
    '--disable-sync',
    '--disable-features=NetworkPrediction,PreconnectToSearch,OptimizationHints',
    '--no-pings',
)

gw.DOWNLOAD_WAIT_ROUNDS = 60   # 点下载后最多等 120 秒直链（每轮 2 秒），慢代理留足余量

# 只屏蔽第三方统计 / 广告；站内 CSS、图片不动——屏蔽它们反而让页面水合变慢、成功率下降
gw.BLOCK_PATTERNS = (
    '*res.wx.qq.com*', '*hm.baidu.com*', '*google-analytics*', '*googletagmanager*',
    '*doubleclick*', '*facebook*', '*twitter*', '*youtube*', '*gvt1.com*',
)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'proxy_usage.json')
GUEST_LIMIT = 5          # 游客单 IP 每日额度
WORKERS = 10             # 同时开几个无痕浏览器线程
PAGE_DELAY = (5.0, 10.0) # 每张之间随机停顿秒数（放慢，别把站点和代理压太狠）
PROXY_REST = (3.0, 7.0)  # 换 IP 时额外停顿
MAX_ITEM_TRIES = 3       # 单张最多换几个 IP 重试
ITEM_TIMEOUT = 220       # 单张判死秒数：页面加载 40s + 点下载后等直链 120s + 余量
VERIFY_WORKERS = 8       # 并发验证代理
VERIFY_TIMEOUT = 10
KIND_NAME = {'home': '电脑', 'mobile': '手机'}
SORT_NAME = {1: '最热', 2: '最新', 3: '随机'}


def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


# ============================ 本地认证转发代理 ============================
CHAIN_PROXY = None      # 上级隧道（本机 VPN / 机场客户端），代理端口需经它才能连上时使用
CHAIN_SEM = threading.BoundedSemaphore(32)   # 同时向上一级开的隧道上限，压太狠会把本机客户端打挂
PER_PROXY_CONN = 6      # 单个代理同时允许的连接数（Chrome 预连接会一次开很多）
_chain_state = {'ok': True, 'checked': 0.0}
_chain_lock = threading.Lock()


def _chain_alive(cache=2.0):
    """探一下上级代理端口还在不在；不在就说明本机客户端挂了，别把代理池全判死。"""
    if not CHAIN_PROXY:
        return True
    now = time.time()
    with _chain_lock:
        if now - _chain_state['checked'] < cache:
            return _chain_state['ok']
    cu = urllib.parse.urlparse(CHAIN_PROXY)
    ok = True
    try:
        with socket.create_connection((cu.hostname, cu.port or 80), timeout=3):
            pass
    except OSError:
        ok = False
    with _chain_lock:
        _chain_state['ok'] = ok
        _chain_state['checked'] = time.time()
    return ok


def wait_chain(timeout=90):
    """上级代理被压挂时等它恢复；恢复前不要拿代理去试，否则会把整池代理误判成失效。"""
    if not CHAIN_PROXY:
        return True
    deadline = time.time() + timeout
    waited = 0.0
    while time.time() < deadline:
        if _chain_alive(cache=0.5):
            if waited > 3:
                log(f'上级代理已恢复（等了 {waited:.0f}s）')
            return True
        if waited == 0:
            log('上级代理端口连不上，等它自动恢复…')
        time.sleep(3.0)
        waited += 3.0
    log(f'上级代理 {timeout:.0f}s 内没恢复')
    return False


def _open_via_chain(chain, host, port, timeout=8, tries=2):
    """经上级代理 CONNECT 到目标，返回已建立的隧道 socket。429 会短暂退避后重试。
    超时给得短：Chrome 一页要开十几条连接，卡在一条上整页都会停住，宁可快速失败让它重试。"""
    cu = urllib.parse.urlparse(chain)
    last = None
    for attempt in range(tries):
        if not _chain_alive():
            wait_chain(60)
            continue
        if not CHAIN_SEM.acquire(timeout=10):
            last = '本级隧道排队超时'
            continue
        s = None
        try:
            s = socket.create_connection((cu.hostname, cu.port or 80), timeout=timeout)
            req = f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n'
            if cu.username:
                raw = f'{urllib.parse.unquote(cu.username)}:{urllib.parse.unquote(cu.password or "")}'
                req += 'Proxy-Authorization: Basic ' + base64.b64encode(raw.encode()).decode() + '\r\n'
            s.sendall((req + '\r\n').encode('latin-1'))
            s.settimeout(timeout)
            head = LocalProxy._read_head(s)
            line = head.split(b'\r\n', 1)[0]
            if b' 200' in line:
                sock, s = s, None
                return sock
            last = line.decode('latin-1', 'ignore')[:80] or '上游断开'
            if b'429' in line:
                time.sleep(1.0 * (attempt + 1))
            continue
        except OSError as exc:
            last = exc
            time.sleep(0.3 * (attempt + 1))
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
            CHAIN_SEM.release()
    raise OSError(f'上级代理链路失败 {host}:{port} -> {last}')


def detect_chain_proxy():
    """探测本机常见的本地代理端口，返回可用于级联的 http 代理地址或 None。"""
    for port in (7897, 7890, 7891, 10809, 10808, 1080, 2080, 8888, 8080):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return f'http://127.0.0.1:{port}'
        except OSError:
            continue
    env = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY') or os.environ.get('ALL_PROXY')
    if env and '://' in env:
        return env
    return None


class LocalProxy:
    """把 http://user:pass@host:port 转成 127.0.0.1:port，自动补 Proxy-Authorization。
    配置了 CHAIN_PROXY 时先连上级隧道，再由上级代理连目标代理端口。"""

    def __init__(self, upstream, chain=None):
        u = urllib.parse.urlparse(upstream)
        if not u.hostname or not u.port:
            raise ValueError(f'代理格式不正确: {upstream}')
        self.up_host, self.up_port = u.hostname, u.port
        self.chain = chain if chain is not None else CHAIN_PROXY
        token = ''
        if u.username:
            raw = f'{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or "")}'
            token = 'Basic ' + base64.b64encode(raw.encode()).decode()
        self.auth = token
        self._closed = False
        self._clients = set()
        self._lock = threading.Lock()
        self.fails = 0
        self.throttled = False
        self.slots = threading.BoundedSemaphore(max(1, PER_PROXY_CONN))
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen(128)
        self.port = self.srv.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _connect_upstream(self, host=None, port=None):
        """连到上游代理本身（带级联时走隧道）。"""
        host = host or self.up_host
        port = port or self.up_port
        if self.chain:
            return _open_via_chain(self.chain, host, port)
        return socket.create_connection((host, port), timeout=30)

    def _accept_loop(self):
        while not self._closed:
            try:
                cli, _ = self.srv.accept()
            except OSError:
                return
            with self._lock:
                self._clients.add(cli)
            threading.Thread(target=self._serve, args=(cli,), daemon=True).start()

    def _serve(self, cli):
        """每个客户端连接先排队拿一个槽位，单代理并发压在 PER_PROXY_CONN 以内。"""
        got = self.slots.acquire(timeout=120)
        try:
            if not got or self._closed:
                try:
                    cli.close()
                except OSError:
                    pass
                return
            self._handle(cli)
        finally:
            if got:
                self.slots.release()

    @staticmethod
    def _read_head(sock, limit=65536):
        buf = b''
        while b'\r\n\r\n' not in buf and len(buf) < limit:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return b''
            if not chunk:
                return buf
            buf += chunk
        return buf

    def _handle(self, cli):
        up = None
        try:
            head = self._read_head(cli)
            if not head:
                return
            first = head.split(b'\r\n', 1)[0].decode('latin-1', 'ignore')
            parts = first.split()
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]
            if method == 'CONNECT':
                host, _, port = target.rpartition(':')
                up = self._connect_upstream(self.up_host, self.up_port)
                req = (f'CONNECT {host}:{port} HTTP/1.1\r\n'
                       f'Host: {host}:{port}\r\n')
                if self.auth:
                    req += f'Proxy-Authorization: {self.auth}\r\n'
                up.sendall((req + '\r\n').encode('latin-1'))
                resp = self._read_head(up)
                if b' 200 ' not in resp.split(b'\r\n', 1)[0]:
                    code = resp.split(b'\r\n', 1)[0].decode('latin-1', 'ignore').strip()
                    cli.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
                    self._note_fail(f'{code or "上游断开"}')
                    return
                cli.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                self._pipe(cli, up)
            else:
                # 明文 HTTP：原样转发并补认证头
                u = urllib.parse.urlparse(target)
                up = self._connect_upstream(u.hostname or self.up_host, u.port or 80)
                rest = head.split(b'\r\n', 1)[1] if b'\r\n' in head else b''
                lines = head.split(b'\r\n', 1)[0] + b'\r\n'
                extra = f'Proxy-Authorization: {self.auth}\r\n'.encode() if self.auth else b''
                up.sendall(lines + extra + rest)
                self._pipe(cli, up)
        except Exception:
            self._note_fail('连接失败')
        finally:
            for s in (cli, up):
                if s is None:
                    continue
                try:
                    s.close()
                except OSError:
                    pass
            with self._lock:
                self._clients.discard(cli)

    def _note_fail(self, why):
        """同一代理的连接失败只报前几条，避免 10 个线程刷屏；限流会置位 throttle。"""
        with self._lock:
            self.fails += 1
            n = self.fails
            first = n <= 3
        if '429' in why or 'Not Enough' in why:
            self.throttled = True
        if first:
            log(f'代理 {self.up_host}:{self.up_port} CONNECT 失败 -> {why}（{n}）')

    @staticmethod
    def _pipe(a, b):
        socks = [a, b]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 90)
                if not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            return

    def close(self):
        self._closed = True
        try:
            self.srv.close()
        except OSError:
            pass
        with self._lock:
            for c in list(self._clients):
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()


# ============================ 代理池 / 每日周期 ============================
def proxy_key(raw):
    u = urllib.parse.urlparse(raw)
    if not u.hostname or not u.port:
        return ''
    return f'{u.hostname}:{u.port}'


def load_proxies(path):
    out, seen = [], set()
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if not re.match(r'^(https?|socks5h?)://', line, re.I):
                line = 'http://' + line
            key = proxy_key(line)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(line)
    return out


class UsageState:
    """按日期分周期的已使用记录；换天自动清零。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        today = time.strftime('%Y-%m-%d')
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict) or d.get('date') != today:
            d = {'date': today, 'used': [], 'dead': [], 'quota': []}
        for k in ('used', 'dead', 'quota'):
            if not isinstance(d.get(k), list):
                d[k] = []
        return d

    def _save(self):
        tmp = f'{self.path}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _add(self, bucket, key):
        with self.lock:
            if key not in self.data[bucket]:
                self.data[bucket].append(key)
            self._save()

    def used(self):
        return set(self.data['used']) | set(self.data['quota'])

    def dead(self):
        return set(self.data['dead'])

    def mark_used(self, key):
        self._add('used', key)

    def mark_dead(self, key):
        self._add('dead', key)

    def mark_quota(self, key):
        self._add('quota', key)

    def summary(self):
        with self.lock:
            return self.data['date'], len(self.data['used']), len(self.data['dead']), len(self.data['quota'])


def check_proxy(raw, timeout=VERIFY_TIMEOUT):
    """真实发一次 HTTPS 请求验证代理可用，返回本地代理实例或 None。"""
    lp, _ = _check_proxy_detail(raw, timeout)
    return lp


def _proxy_fail_reason(raw, timeout=VERIFY_TIMEOUT):
    """再探一次，返回失败原因（仅用于日志）。"""
    lp, why = _check_proxy_detail(raw, timeout)
    if lp:
        lp.close()
        return ''
    return why


def _check_proxy_detail(raw, timeout=VERIFY_TIMEOUT):
    lp = None
    try:
        u = urllib.parse.urlparse(raw)
        try:
            if CHAIN_PROXY:
                _open_via_chain(CHAIN_PROXY, u.hostname, u.port, timeout=6, tries=1).close()
            else:
                with socket.create_connection((u.hostname, u.port), timeout=4):
                    pass
        except OSError as exc:
            return None, f'隧道 {str(exc)[:40]}'
        lp = LocalProxy(raw)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': lp.url, 'https': lp.url}))
        ok, why = False, '探测无响应'
        for probe in ('https://www.baidu.com/', gw.BASE + '/'):
            try:
                req = urllib.request.Request(probe, headers={'User-Agent': gw.UA, 'Accept': '*/*'})
                with opener.open(req, timeout=timeout) as r:
                    r.read(1024)
                ok = True
                break
            except urllib.error.HTTPError as exc:
                if exc.code < 500:      # 4xx 说明代理链路是通的
                    ok = True
                    break
                why = f'HTTP {exc.code}'
            except Exception as exc:
                why = str(exc)[:50]
                continue
        if ok:
            return lp, ''
        lp.close()
        return None, why
    except Exception as exc:
        if lp:
            lp.close()
        return None, str(exc)[:50]


class ProxyPool:
    """未使用的代理随机抽取；启动前先全量过滤一遍，连不上的直接跳过。"""

    def __init__(self, entries, state):
        self.entries = list(entries)
        random.shuffle(self.entries)
        self.state = state
        self.ready = []          # 已验证可用、尚未使用的 (key, raw)
        self.lock = threading.Lock()
        self.filtered = False
        self.checked = 0

    def _pick_batch(self, size=VERIFY_WORKERS):
        used, dead = self.state.used(), self.state.dead()
        pool = [e for e in self.entries
                if proxy_key(e) and proxy_key(e) not in used and proxy_key(e) not in dead]
        if not pool:
            return []
        random.shuffle(pool)
        return [(proxy_key(e), e) for e in pool[:size]]

    def prefilter(self, workers=40, timeout=8, keep=None):
        """启动前并发验证整份代理文件：能用的进 ready，用不了的当场记 dead。
        keep 给定时只保证拿到 keep 个可用就够，剩下的继续换下一批，直到扫完或够用。"""
        used, dead = self.state.used(), self.state.dead()
        todo = [e for e in self.entries if proxy_key(e) not in used and proxy_key(e) not in dead]
        random.shuffle(todo)
        total = len(todo)
        if not total:
            self.filtered = True
            return 0
        wait_chain()
        log(f'预过滤代理：待验证 {total} 个（并发 {workers}，单个超时 {timeout}s）')
        t0 = time.time()
        alive = 0
        lock = threading.Lock()
        idx = 0
        stop = threading.Event()
        retries = {}

        wait_chain()

        def run():
            nonlocal idx, alive
            while not stop.is_set():
                with lock:
                    if idx >= len(todo):
                        return
                    raw = todo[idx]
                    idx += 1
                key = proxy_key(raw)
                lp = check_proxy(raw, timeout=timeout)
                fail_note = ''
                if lp is None and _chain_alive():
                    # 429 限流、偶发抖动都会被 check_proxy 判失败；等 4 秒复验一次，
                    # 只有连两次都失败才真当失效，别把一整池好代理误杀。
                    fail_note = _proxy_fail_reason(raw, timeout)
                    time.sleep(4.0)
                    lp = check_proxy(raw, timeout=timeout)
                with lock:
                    self.checked += 1
                    n = self.checked
                if lp is not None:
                    lp.close()
                    with lock:
                        self.ready.append((key, raw))
                        alive += 1
                elif not _chain_alive():
                    # 上级代理掉线：不是代理本身的问题，放回队列稍后重验，不记 dead
                    with lock:
                        retries[key] = retries.get(key, 0) + 1
                    time.sleep(3.0)
                    wait_chain(60)
                    continue
                else:
                    self.state.mark_dead(key)
                if n % 50 == 0 or n == total:
                    el = max(0.1, time.time() - t0)
                    rate = n / el
                    eta = (total - n) / rate if rate else 0
                    log(f'  预过滤 {n}/{total} · 可用 {alive} · 失效 {len(self.state.dead())}'
                        f' · {rate:.1f}/s · 剩余约 {eta:.0f}s')
                if keep and alive >= keep:
                    stop.set()
                    return

        threads = [threading.Thread(target=run, daemon=True) for _ in range(min(workers, total))]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(0.4)
        self.filtered = True
        log(f'预过滤完成：验证 {self.checked} 个，可用 {alive} 个，'
            f'失效 {len(self.state.dead())}，用时 {time.time() - t0:.0f}s')
        return alive

    def _refill(self):
        while True:
            batch = self._pick_batch()
            if not batch:
                return False
            wait_chain()
            with ThreadPoolExecutor(max_workers=min(VERIFY_WORKERS, len(batch))) as ex:
                results = list(ex.map(lambda kv: (kv[0], kv[1], check_proxy(kv[1])), batch))
            alive = 0
            for key, raw, lp in results:
                if lp is not None:
                    lp.close()
                    self.ready.append((key, raw))
                    alive += 1
                elif not _chain_alive():
                    continue            # 上级代理波动，稍后这批还能再验，不记 dead
                else:
                    self.state.mark_dead(key)
            used, dead = len(self.state.used()), len(self.state.dead())
            log(f'代理验证：本批 {len(batch)} 个，可用 {alive} 个（已用 {used} / 失效 {dead} / 共 {len(self.entries)}）')
            if self.ready:
                return True

    def acquire(self):
        """随机取一个未用过且可用的代理，取到即落盘标记已使用。"""
        with self.lock:
            while True:
                random.shuffle(self.ready)
                while self.ready:
                    key, raw = self.ready.pop()
                    if key in self.state.used() or key in self.state.dead():
                        continue
                    self.state.mark_used(key)
                    return key, raw
                if not self._refill():
                    return None, None

    def mark_quota(self, key):
        self.state.mark_quota(key)

    def available(self):
        with self.lock:
            return len(self.ready)

    def stats(self):
        date, used, dead, quota = self.state.summary()
        return date, len(self.entries), used, dead, quota


# ============================ 列表收集 / 模糊搜索 ============================
def probe(kind, type_id=None, search=None, sort_type=3):
    """探测一次列表，返回 (总数, 页数, 前三条标题)。"""
    gw.configure_network(None)
    d = gw.get_list(kind, 1, type_id, search=search, sort_type=sort_type)
    if not d:
        return 0, 0, []
    titles = [i['title'] for i in gw._public_items(d)[:3]]
    return int(d.get('total') or 0), int(d.get('pages') or 0), titles


def _kw_chars(kw):
    return [c for c in kw if not c.isspace() and c not in '-_,，。']


def _fuzzy_hit(title, chars):
    """标题命中数；全字符命中算精确，少一个算近似。"""
    return sum(1 for c in chars if c in title)


def fuzzy_scan(kind, type_id, kw, want, max_pages=12, sort_type=3):
    """站内搜索无结果时，按标题字符做模糊匹配。
    type_id 给定时只扫该分类的前 max_pages 页；没给就轮流扫各分类前几页，够数即停。"""
    gw.configure_network(None)
    chars = _kw_chars(kw)
    if not chars:
        return [], 0
    strict, loose = [], []
    seen = set()
    scanned = 0
    need = max(4, want)
    if type_id:
        targets = [(type_id, max_pages)]
    else:
        cats = list_categories()
        per = max(2, max_pages // max(1, len(cats)))
        targets = [(c['id'], per) for c in cats]
    for tid, cap in targets:
        page, pages = 1, 1
        while page <= pages and page <= cap:
            d = gw.get_list(kind, page, tid, search=None, sort_type=sort_type)
            if not d:
                break
            pages = max(1, int(d.get('pages') or 1))
            scanned += 1
            for it in gw._public_items(d):
                wid = it.get('wtId')
                if not wid or wid in seen:
                    continue
                seen.add(wid)
                score = _fuzzy_hit(it['title'], chars)
                if score == len(chars):
                    strict.append(it)
                elif len(chars) > 1 and score >= len(chars) - 1:
                    loose.append(it)
            if len(strict) >= need:
                break
            page += 1
        if len(strict) >= need:
            break
    out = strict + loose
    random.shuffle(out)
    return out, scanned


def collect_items(kind, type_id, search, mode, want, fuzzy=False):
    """按模式收集去重后的壁纸条目，直到 want 张或用尽页数。"""
    gw.configure_network(None)
    items, seen = [], set()

    def take(lst):
        for it in lst:
            wid = it.get('wtId')
            if not wid or wid in seen:
                continue
            seen.add(wid)
            items.append(it)
            if len(items) >= want:
                return True
        return False

    if fuzzy or (search and probe(kind, type_id, search)[0] == 0):
        # 模糊：站内搜不到就直接按分类翻页做标题匹配
        got, _ = fuzzy_scan(kind, type_id, search, want)
        return got[:want]

    if mode == 3:  # 随机：跨随机页抓取
        first = gw.get_list(kind, 1, type_id, search=search, sort_type=3)
        if not first:
            return items
        pages = max(1, int(first.get('pages') or 1))
        order = list(range(1, pages + 1))
        random.shuffle(order)
        for p in order:
            data = first if p == 1 else gw.get_list(kind, p, type_id, search=search, sort_type=3)
            if not data:
                continue
            batch = gw._public_items(data)
            random.shuffle(batch)
            if take(batch):
                break
            if len(order) > 30 and len(items) >= want:
                break
    else:
        sort_type = 3 if mode == 1 else 1
        page, pages = 1, 1
        while len(items) < want and page <= pages:
            data = gw.get_list(kind, page, type_id, search=search, sort_type=sort_type)
            if not data:
                break
            pages = max(1, int(data.get('pages') or 1))
            if take(gw._public_items(data)):
                break
            page += 1
    return items


# ============================ 下载 ============================
def name_from_item(it, ext='.jpg'):
    """站点文件名形如「【哲风壁纸】宇宙星空-星空.jpg」，用标题前两段还原。
    浏览器没把建议文件名带回来时用它兜底，避免落成 17242989986237824.png 这种编号。"""
    title = (it or {}).get('title') or ''
    parts = [p.strip() for p in title.split('-') if p.strip()]
    if parts:
        base = '-'.join(parts[:2])
    else:
        base = str((it or {}).get('wtId') or 'wallpaper')
    base = re.sub(r'[\\/:*?"<>|｜]', '_', base)[:60].strip() or 'wallpaper'
    return f'【哲风壁纸】{base}{ext}'


def save_file(url, out_dir, suggested, proxy_url, fallback=None):
    """下载直链：代理和直连同时赛跑，谁先连上就用谁，大文件走直连通常更快。"""
    out = {}

    def attempt(tag, opener):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': gw.UA, 'Referer': gw.BASE + '/'})
            with opener.open(req, timeout=180) as r:
                name = suggested or r.headers.get_filename()
                if not name:
                    name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit('/', 1)[-1])
                name = name.replace('\\', '/').rsplit('/', 1)[-1]
                # 直链路径自带的是 17242983011110272.png 这种编号；浏览器没带回
                # 「【哲风壁纸】xxx.jpg」时用标题还原的名字，别让用户看到一串数字
                if fallback and (not name or re.fullmatch(r'\d+\.(png|jpg|jpeg|webp)', name, re.I)):
                    name = fallback
                if not name:
                    name = 'wallpaper.jpg'
                data = r.read()
            out.setdefault('win', (tag, name, data))
        except Exception as exc:
            out.setdefault(tag + '_err', exc)

    openers = [('direct', urllib.request.build_opener(urllib.request.ProxyHandler({})))]
    if proxy_url:
        openers.append(('proxy', urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url}))))
    threads = [threading.Thread(target=attempt, args=(t, o), daemon=True) for t, o in openers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if 'win' in out:
        tag, name, data = out['win']
        path = os.path.join(out_dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return name, len(data), tag
    raise out.get('direct_err') or out.get('proxy_err') or RuntimeError('下载失败')


def fetch_one(sess, kind, wid, lp, hard_timeout=ITEM_TIMEOUT):
    """在会话里过一张详情页拿直链；加硬超时，卡死的浏览器直接判死换 IP，不拖整批。"""
    box = {}

    def run():
        s = sess
        try:
            res = gw.get_one(s, kind, wid)
            if len(res) == 3:
                direct, status, fname = res
            else:                       # 兼容返回 (sess, direct, status, fname) 的实现
                s, direct, status, fname = res
            box['r'] = (s, direct, status, fname)
        except Exception as exc:
            box['err'] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(hard_timeout)
    if t.is_alive():
        # 页面卡住：进程直接杀掉，调用方拿到 ITEM_HANG 后换 IP
        try:
            gw_kill = getattr(sess, 'proc', None)
            if gw_kill is not None:
                gw_kill.kill()
        except Exception:
            pass
        return None, None, 'ITEM_HANG', None
    if 'err' in box:
        raise box['err']
    new_sess, direct, status, fname = box.get('r', (sess, None, 'NO_RESULT', None))
    if status == 'CONN_LOST':
        try:
            new_sess.quit()
        except Exception:
            pass
        new_sess = gw.BrowserSession(headless=True, proxy=lp.url)
        return new_sess, *gw.get_one(new_sess, kind, wid)
    return new_sess, direct, status, fname


def run_batch(items, kind, out_dir, pool, workers=WORKERS):
    """多无痕线程：每个线程一个代理 + 一个全新无痕浏览器，各自下满 GUEST_LIMIT 张再换 IP。
    速度靠 PAGE_DELAY 控制，默认每张间隔 5~10 秒，代理之间再歇 3~7 秒。"""
    total = len(items)
    queue = list(items)
    qlock = threading.Lock()
    stats = {'got': 0, 'done': 0}
    slock = threading.Lock()
    fails = []
    tries = {}
    stop = threading.Event()
    pending = {'n': total}
    t0 = time.time()

    def take_item():
        with qlock:
            return queue.pop(0) if queue else None

    def requeue(it):
        with qlock:
            queue.insert(0, it)

    def requeue_all(items):
        if not items:
            return
        with qlock:
            queue[0:0] = list(items)

    def settle():
        """一张彻底做完（成功或放弃）才减计数；重排不减。"""
        with qlock:
            pending['n'] -= 1
            return pending['n']

    def still_work():
        with qlock:
            return pending['n'] > 0

    def bump(got=0, done=0):
        with slock:
            stats['got'] += got
            stats['done'] += done
            return stats['got'], stats['done']

    def bump_try(wid):
        with slock:
            n = tries.get(wid, 0) + 1
            tries[wid] = n
            return n

    def worker(idx):
        while not stop.is_set() and still_work():
            # 先把想要的张数摘到手，再领代理；剩余不够就少领几个，别白占 IP 额度
            with qlock:
                want_n = min(GUEST_LIMIT, len(queue))
                if want_n <= 0:
                    return
                mine = [queue.pop(0) for _ in range(want_n)]
            key, raw = pool.acquire()
            if not raw:
                log(f'[W{idx}] 代理池已用尽')
                with qlock:
                    queue.extend(mine)
                stop.set()
                return
            lp = sess = None
            log(f'[W{idx}] 代理 {key} 就位，领到 {len(mine)} 张，剩余待下 {pending["n"]} 张')
            try:
                lp = LocalProxy(raw)
                sess = gw.BrowserSession(headless=True, proxy=lp.url)
                while mine and not stop.is_set():
                    it = mine.pop(0)
                    sess, direct, status, fname = fetch_one(sess, kind, it['wtId'], lp)

                    if status == 'ITEM_HANG':
                        log(f'[W{idx}] {it["wtId"]} 页面卡死 {ITEM_TIMEOUT}s，换 IP')
                        sess = None
                        requeue(it)
                        break

                    if status in ('QUOTA', 'AUTH_EXPIRED'):
                        pool.mark_quota(key)
                        log(f'[W{idx}] 代理 {key} 额度耗尽（{status}），换 IP 重下 {it["wtId"]}')
                        requeue(it)
                        break

                    if not direct:
                        n = bump_try(it['wtId'])
                        if n < MAX_ITEM_TRIES and not stop.is_set():
                            log(f'[W{idx}] {it["wtId"]} 失败({status})，换 IP 重试')
                            requeue(it)
                            break
                        fails.append({'wtId': it['wtId'], 'status': status})
                        g, d = bump(done=1)
                        left = settle()
                        log(f'[W{idx}] 放弃 {it["wtId"]} 状态 {status}（{d}/{total}，剩 {left}）')
                        time.sleep(random.uniform(PAGE_DELAY[0], PAGE_DELAY[1]))
                        continue

                    try:
                        name, size, via = save_file(direct, out_dir, fname, lp.url,
                                                    fallback=name_from_item(it))
                        g, d = bump(got=1, done=1)
                        left = settle()
                        log(f'[W{idx}] OK {name} {size / 1048576:.2f}MB ← {key} [{via}]'
                            f'（{d}/{total}，成功 {g}，剩 {left}）')
                    except Exception as exc:
                        fails.append({'wtId': it['wtId'], 'status': f'DL:{exc}'[:120]})
                        g, d = bump(done=1)
                        left = settle()
                        log(f'[W{idx}] 保存失败 {it["wtId"]}: {str(exc)[:80]}'
                            f'（{d}/{total}，剩 {left}）')
                    time.sleep(random.uniform(PAGE_DELAY[0], PAGE_DELAY[1]))
            except KeyboardInterrupt:
                stop.set()
                requeue_all(mine)
                mine = []
                return
            except Exception as exc:
                log(f'[W{idx}] 代理 {key} 会话异常：{str(exc)[:100]}')
                requeue_all(mine)
                mine = []
                time.sleep(random.uniform(2.0, 4.0))
            finally:
                if mine:
                    # 中途换 IP：没轮到的还回队首，别的线程可以接着下
                    requeue_all(mine)
                if sess:
                    try:
                        sess.quit()
                    except Exception:
                        pass
                if lp:
                    lp.close()
            time.sleep(random.uniform(PROXY_REST[0], PROXY_REST[1]))

    threads = [threading.Thread(target=worker, args=(i + 1,), daemon=True)
               for i in range(min(workers, max(1, total)))]
    try:
        for t in threads:
            t.start()
            time.sleep(random.uniform(0.4, 1.2))   # 线程错峰启动
        for t in threads:
            while t.is_alive():
                t.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        log('收到中断，等待线程收尾…')
    got, done = stats['got'], stats['done']
    log(f'批处理结束：成功 {got} / 已处理 {done} / 失败 {len(fails)}，用时 {time.time() - t0:.0f}s')
    return got, fails, done


# ============================ 交互界面 ============================
def choose_device():
    print('\n设备：1 电脑   2 手机')
    while True:
        c = input('选择 [1/2，回车=1]: ').strip() or '1'
        if c in ('1', '2'):
            return 'home' if c == '1' else 'mobile'
        print('请输入 1 或 2')


def list_categories():
    cats = [{'id': c.get('id'), 'name': c.get('typeName', '')} for c in gw.get_categories()]
    return cats


def category_counts(kind, cats, sort_type=3):
    """并发探测每个分类的壁纸总数，返回 {typeId: 总数}。"""
    counts = {}

    def one(c):
        try:
            d = gw.get_list(kind, 1, c['id'], search=None, sort_type=sort_type)
            return c['id'], int((d or {}).get('total') or 0)
        except Exception:
            return c['id'], -1

    with ThreadPoolExecutor(max_workers=min(6, len(cats))) as ex:
        for cid, n in ex.map(one, cats):
            counts[cid] = n
    return counts


def show_hit(kind, type_id, search, sort_type=3, tag=''):
    """打印一个条件实际能搜到多少张，返回总数。"""
    try:
        total, pages, titles = probe(kind, type_id, search, sort_type)
    except Exception as exc:
        print(f'  查询失败：{str(exc)[:80]}')
        return -1
    if total:
        print(f'  {tag}命中 {total} 张 · {pages} 页')
        if titles:
            print('  示例：' + ' | '.join(t[:20] for t in titles))
    else:
        print(f'  {tag}站内搜索无结果')
    return total


def fuzzy_preview(kind, type_id, kw, want=12):
    """模糊检索预览：返回 (条目数, 示例标题)。"""
    try:
        got, scanned = fuzzy_scan(kind, type_id, kw, want)
    except Exception as exc:
        print(f'  模糊检索失败：{str(exc)[:80]}')
        return 0, []
    return len(got), [i['title'][:20] for i in got[:3]]


def choose_category(cats, kind, counts):
    print('\n分类（括号内为可下载总数）：')
    for i, c in enumerate(cats, 1):
        n = counts.get(c['id'])
        label = f'{n} 张' if n and n > 0 else ('—' if n in (0, -1, None) else '')
        print(f'  {i:>2}. {c["name"]}  ({label})')
    print('   s. 改用关键词搜索')
    while True:
        c = input('选择序号 [回车=1，s=搜索]: ').strip()
        if c.lower() == 's':
            kw = input('关键词（支持模糊，多词用空格）: ').strip()
            if kw:
                return None, kw
            continue
        if not c:
            return cats[0], ''
        if c.isdigit() and 1 <= int(c) <= len(cats):
            return cats[int(c) - 1], ''
        # 直接输名字也认
        hit = [x for x in cats if c and c in x['name']]
        if len(hit) == 1:
            return hit[0], ''
        print('输入无效')


def choose_sort():
    print('\n排序：1 最热   2 最新   3 随机')
    while True:
        c = input('选择 [回车=1]: ').strip() or '1'
        if c in ('1', '2', '3'):
            return int(c)
        print('请输入 1/2/3')


def choose_count():
    while True:
        c = input('\n下载数量: ').strip()
        if c.isdigit() and int(c) > 0:
            return int(c)
        print('请输入正整数')


def pick_proxy_file(explicit=None):
    if explicit:
        return explicit
    root = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(root, f) for f in os.listdir(root)
             if re.match(r'^proxies.*\.txt$', f, re.I)]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def apply_condition(defaults, cat=None, kw=''):
    """设置分类或关键词，打印命中数量；返回可用于下载的条目数。"""
    kind, mode = defaults['kind'], defaults['mode']
    sort_type = 3 if mode in (1, 3) else 1
    if cat:
        defaults['type_id'], defaults['search'], defaults['fuzzy'] = cat['id'], '', False
        defaults['label'] = cat['name']
        total = show_hit(kind, cat['id'], None, sort_type, tag=f'[{cat["name"]}] ')
        defaults['hits'] = max(0, total)
        return defaults['hits']
    defaults['type_id'], defaults['search'] = None, kw
    defaults['label'] = f'搜索:{kw}'
    total = show_hit(kind, None, kw, sort_type, tag=f'[{kw}] ')
    if total > 0:
        defaults['fuzzy'] = False
        defaults['hits'] = total
        return total
    n, sample = fuzzy_preview(kind, None, kw)
    head = f'命中 {n} 张' if n else '无匹配'
    tail = f' · 示例：{" | ".join(sample)}' if sample else ''
    print(f'  转模糊检索：{head}{tail}')
    defaults['fuzzy'] = n > 0
    defaults['hits'] = n
    if n:
        print('  已自动开启模糊模式（按标题逐字匹配，从各分类里捞）')
    return n


def interactive_menu(defaults):
    cats = None
    counts = None
    while True:
        date, pool_size, used, dead, quota = defaults['pool'].stats()
        fuzzy_tag = ' · 模糊' if defaults.get('fuzzy') else ''
        print('\n' + '=' * 58)
        print(' 哲风壁纸 批量下载（终端 / 代理 IP / 无痕 / 无窗口）')
        print('=' * 58)
        print(f' 设备：{KIND_NAME[defaults["kind"]]}    分类：{defaults["label"] or "未选择"}{fuzzy_tag}')
        print(f' 排序：{SORT_NAME[defaults["mode"]]}    数量：{defaults["count"]}'
              + (f'    可下：{defaults["hits"]} 张' if defaults.get('hits') else ''))
        print(f' 代理：{os.path.basename(defaults["proxy_file"])} 共 {pool_size} 个')
        print(f' 今日({date})：已用 {used} / 失效 {dead} / 配额耗尽 {quota}')
        print('-' * 58)
        print(' 1 切换设备   2 选择分类   3 关键词搜索')
        print(' 4 设置排序   5 设置数量   6 开始下载   0 退出')
        c = input('选择: ').strip()
        if c == '0':
            return False
        if c == '1':
            defaults['kind'] = choose_device()
            defaults['hits'] = 0
            counts = None
            if defaults.get('label'):
                apply_condition(defaults,
                                cat={'id': defaults['type_id'], 'name': defaults['label']}
                                if defaults.get('type_id') else None,
                                kw=defaults.get('search', ''))
        elif c == '2':
            try:
                cats = cats or list_categories()
                if counts is None:
                    print('  正在统计各分类数量…')
                    counts = category_counts(defaults['kind'], cats)
                cat, kw = choose_category(cats, defaults['kind'], counts)
                apply_condition(defaults, cat=cat, kw=kw)
            except Exception as exc:
                print(f'分类拉取失败：{exc}')
        elif c == '3':
            kw = input('关键词（支持模糊，多词用空格）: ').strip()
            if kw:
                apply_condition(defaults, cat=None, kw=kw)
        elif c == '4':
            defaults['mode'] = choose_sort()
            if defaults.get('label'):
                apply_condition(defaults,
                                cat={'id': defaults['type_id'], 'name': defaults['label']}
                                if defaults.get('type_id') else None,
                                kw=defaults.get('search', ''))
        elif c == '5':
            defaults['count'] = choose_count()
        elif c == '6':
            if not defaults['label']:
                print('请先选择分类或输入关键词')
                continue
            return True
        else:
            print('输入无效')


def main():
    global CHAIN_PROXY, PAGE_DELAY, PER_PROXY_CONN
    ap = argparse.ArgumentParser(description='哲风壁纸终端批量下载器')
    ap.add_argument('-p', '--proxy-file', help='代理 txt 路径，默认取最新的 proxies*.txt')
    ap.add_argument('-k', '--kind', choices=['home', 'mobile'], help='home=电脑 mobile=手机')
    ap.add_argument('-c', '--category', help='分类序号或分类名')
    ap.add_argument('-q', '--search', help='搜索关键词')
    ap.add_argument('-s', '--sort', type=int, choices=[1, 2, 3], help='1最热 2最新 3随机')
    ap.add_argument('-n', '--count', type=int, help='下载数量')
    ap.add_argument('-w', '--workers', type=int, default=WORKERS, help='并发无痕线程数，默认 10')
    ap.add_argument('-d', '--delay', help='每张间隔秒数范围，如 4-8，默认 5-10')
    ap.add_argument('-f', '--fuzzy', action='store_true', help='强制模糊（按标题逐字匹配）')
    ap.add_argument('-o', '--out', default=os.getcwd(), help='保存根目录')
    ap.add_argument('--upstream', help='上级代理，本机代理端口直连不通时用，例如 http://127.0.0.1:7897')
    ap.add_argument('--verify-workers', type=int, default=10, help='预过滤代理的并发数，默认 10')
    ap.add_argument('--verify-timeout', type=float, default=8, help='单个代理验证超时秒数，默认 8')
    ap.add_argument('--per-proxy-conn', type=int, default=PER_PROXY_CONN,
                    help='单个代理允许的并发连接数，默认 6')
    args = ap.parse_args()

    if args.per_proxy_conn:
        PER_PROXY_CONN = max(1, args.per_proxy_conn)
    if args.delay:
        try:
            lo, _, hi = args.delay.partition('-')
            PAGE_DELAY = (float(lo), float(hi or lo))
        except ValueError:
            print('--delay 格式应为 4-8')
            return 2
    workers = max(1, min(30, args.workers or WORKERS))

    global CHAIN_PROXY
    if args.upstream:
        CHAIN_PROXY = args.upstream
    elif os.environ.get('PROXY_CHAIN'):
        CHAIN_PROXY = os.environ['PROXY_CHAIN']
    else:
        probe_u = urllib.parse.urlparse(load_proxies(pick_proxy_file(args.proxy_file))[0])
        try:
            with socket.create_connection((probe_u.hostname, probe_u.port), timeout=4):
                CHAIN_PROXY = None
        except OSError:
            CHAIN_PROXY = detect_chain_proxy()
            if CHAIN_PROXY:
                log(f'目标代理端口直连不通，改用本机上级代理级联：{CHAIN_PROXY}')
            else:
                log('目标代理端口直连不通，也没探测到本机上级代理')
    if CHAIN_PROXY:
        log(f'级联：{CHAIN_PROXY} -> 代理池')

    proxy_file = pick_proxy_file(args.proxy_file)
    if not proxy_file or not os.path.isfile(proxy_file):
        print('未找到代理 txt，请用 -p 指定')
        return 1
    entries = load_proxies(proxy_file)
    if not entries:
        print(f'{proxy_file} 中没有有效代理行')
        return 1
    pool = ProxyPool(entries, UsageState(STATE_FILE))
    log(f'代理文件 {proxy_file}：{len(entries)} 条')

    gw.configure_network(None)

    defaults = {
        'kind': args.kind or 'home',
        'type_id': None,
        'search': args.search or '',
        'label': '',
        'mode': args.sort or 1,
        'count': args.count or 10,
        'fuzzy': bool(args.fuzzy),
        'hits': 0,
        'proxy_file': proxy_file,
        'pool': pool,
    }
    if args.search:
        defaults['label'] = f'搜索:{args.search}'
        show_hit(defaults['kind'], None, args.search, tag=f'[{args.search}] ')
    elif args.category:
        try:
            cats = list_categories()
            if args.category.isdigit() and 1 <= int(args.category) <= len(cats):
                cat = cats[int(args.category) - 1]
            else:
                hit = [c for c in cats if args.category in c['name']]
                cat = hit[0] if hit else None
            if cat:
                defaults['type_id'] = cat['id']
                defaults['label'] = cat['name']
                show_hit(defaults['kind'], cat['id'], None, tag=f'[{cat["name"]}] ')
        except Exception as exc:
            print(f'分类拉取失败：{exc}')

    if not defaults['label']:
        if not interactive_menu(defaults):
            return 0
    else:
        print(f'设备 {KIND_NAME[defaults["kind"]]} / {defaults["label"]} / '
              f'{SORT_NAME[defaults["mode"]]} / {defaults["count"]} 张')

    log(f'收集列表：{KIND_NAME[defaults["kind"]]} / {defaults["label"]} / {SORT_NAME[defaults["mode"]]}'
        + ('（模糊）' if defaults.get('fuzzy') else ''))
    items = collect_items(defaults['kind'], defaults['type_id'], defaults['search'],
                          defaults['mode'], defaults['count'],
                          fuzzy=defaults.get('fuzzy', False))
    if not items:
        log('没有取到壁纸条目')
        return 1

    # 下载前先把整份代理过滤一遍：连不上的当场跳过并记 dead
    need = max(2, (len(items) + GUEST_LIMIT - 1) // GUEST_LIMIT + 2)
    alive = pool.prefilter(workers=args.verify_workers, timeout=args.verify_timeout, keep=need)
    if alive <= 0:
        log('预过滤后没有可用代理，检查上级代理或换一份代理文件')
        return 1
    if alive < need:
        log(f'可用代理只有 {alive} 个，最多再下 {alive * GUEST_LIMIT} 张')
    log(f'可用代理 {alive} 个，够下约 {alive * GUEST_LIMIT} 张')
    log(f'共取到 {len(items)} 张，开始下载（{workers} 个无痕线程 / 每 IP {GUEST_LIMIT} 张 / '
        f'间隔 {PAGE_DELAY[0]:.1f}-{PAGE_DELAY[1]:.1f}s）')

    out_dir = os.path.join(args.out, KIND_NAME[defaults['kind']],
                           gw._sanitize(defaults['label']))
    os.makedirs(out_dir, exist_ok=True)
    log(f'保存目录：{out_dir}')

    t0 = time.time()
    got, fails, done = run_batch(items, defaults['kind'], out_dir, pool, workers)
    log(f'结束：成功 {got} / 尝试 {done} / 失败 {len(fails)}，用时 {time.time() - t0:.0f}s')
    for f in fails[:20]:
        log(f'  失败 {f["wtId"]} {f["status"]}')
    date, total_proxies, used, dead, quota = pool.stats()
    log(f'今日({date}) 代理：已用 {used} / 失效 {dead} / 配额耗尽 {quota}，'
        f'记录文件 {STATE_FILE}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已中断')
        sys.exit(130)
