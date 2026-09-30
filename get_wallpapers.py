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
    优先级：环境变量 CHROME_BIN > 常见安装目录 > PATH(shutil.which)。
    """
    env = os.environ.get('CHROME_BIN')
    if env and os.path.isfile(env):
        return env
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
    raise FileNotFoundError('未找到 Chrome / Edge，请设置环境变量 CHROME_BIN 指向浏览器可执行文件')


CHROME = find_chrome()
OUT = os.path.join(os.getcwd(), '哲风壁纸_魅力迷人')
TMP = os.path.join(os.getcwd(), '_chrome_crawl')
os.makedirs(OUT, exist_ok=True)

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# ---- AES 解密（列表 SSR 数据）----
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
KEY = b'68zhehao2O776519'
IV = b'aa176b7519e84710'


def decrypt(c):
    raw = bytes.fromhex(base64.b64decode(c).hex())
    return re.sub(r'\x00.*$', '', unpad(AES.new(KEY, AES.MODE_CBC, IV).decrypt(raw), 16).decode('utf-8', 'ignore'), flags=re.S)


def get_html(path):
    req = urllib.request.Request(BASE + path, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'})
    d = urllib.request.urlopen(req, timeout=30).read()
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


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def download(url, path):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': BASE + '/'})
    d = urllib.request.urlopen(req, timeout=180).read()
    with open(path, 'wb') as f:
        f.write(d)
    return len(d)


def get_one(kind, wid, prof):
    """开一个浏览器实例，走完 点下载->过验证->拿直链->返回直链 或 None/限额"""
    port = free_port()
    if os.path.isdir(prof):
        shutil.rmtree(prof)
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--disable-gpu', '--no-sandbox',
         f'--user-data-dir={prof}', f'--remote-debugging-port={port}',
         '--remote-allow-origins=*', f'{BASE}/mobileViewLook/{wid}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(12)
        tabs = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=15).read())
        pg = [t for t in tabs if t.get('type') == 'page' and 'haowallpaper' in t.get('url', '')]
        if not pg:
            return None, 'no-tab'
        c = CDP(pg[0]['webSocketDebuggerUrl'])
        c.send('Network.enable')
        c.send('Runtime.enable')
        time.sleep(1)
        c.ev.clear()
        # 点下载
        c.evl("""
          (() => { const el=document.querySelector('.hao-bottom-nav-end__face')||document.querySelector('.hao-bottom-nav-end');
            if(el){el.click();return 'clicked';} return 'NO_BTN'; })()
        """)
        # 等 altcha 挂载并自动求解（最多 ~20s）
        for _ in range(10):
            time.sleep(2)
            done = None
            for e in c.ev:
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
                            if st == 200 and 'down.haowallpaper.com' in txt:
                                done = json.loads(txt)['data']; break
                            if st == 305:
                                try:
                                    msg = json.loads(txt).get('msg', '')
                                except Exception:
                                    msg = ''
                                if '上限' in msg or 'limit' in msg.lower():
                                    return None, 'QUOTA'
                        except Exception:
                            pass
            if done:
                return done, 'ok'
        return None, 'no-url'
    finally:
        proc.terminate()
        try:
            shutil.rmtree(prof)
        except Exception:
            pass


def crawl(kind, n=5):
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
            direct, status = get_one(kind, wid, TMP + f'_{kind}_{got}')
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
    m = crawl('mobile', 5)
    h = crawl('home', 5)
    print(f'\n完成。本次下载 手机{m} + 电脑{h} 张，文件位于: {OUT}')
    if m + h < 10:
        print('注意：未凑满 10 张，多为站点「当日游客下载配额」限制。登录后或次日重置再跑即可。')
