#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取各游戏官方账号（B站/微博/小红书）的最新动态，生成 data.json 供 index.html 展示。
只依赖 Python 标准库，可直接在 GitHub Actions 运行。

B站：直接调用官方 API（WBI 签名），不经过 RSSHub，稳定且不受公共实例限制。
微博/小红书：走 RSSHub（公共实例或你在 games.json 里填的自建实例地址）。
"""
import json, re, os, sys, html, time, hashlib, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

CN_TZ = timezone(timedelta(hours=8))
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
HEADERS = {'User-Agent': UA, 'Referer': 'https://www.bilibili.com'}

# B站 WBI 签名用的混淆表
MIXIN_KEY_ENC_TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,
                     14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,
                     56,59,6,63,57,62,11,36,20,34,44,52]
_mixin_key = None


def log(msg):
    print(f"[{datetime.now(CN_TZ):%H:%M:%S}] {msg}", flush=True)


_cookies = {}


def http_get(url, timeout=25, headers=None):
    h = dict(headers or HEADERS)
    if 'bilibili.com' in url and _cookies:
        h['Cookie'] = '; '.join(f'{k}={v}' for k, v in _cookies.items())
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def refresh_bili_cookie():
    """获取 buvid3/buvid4 匿名 cookie，绕开 B站 对机房 IP 的匿名请求拦截"""
    try:
        spi = json.loads(http_get('https://api.bilibili.com/x/frontend/finger/spi',
                                  headers={'User-Agent': UA, 'Referer': 'https://www.bilibili.com/'}))
        if spi.get('code') == 0:
            _cookies['buvid3'] = spi['data']['b_3']
            _cookies['buvid4'] = spi['data']['b_4']
            log('已获取 B站 匿名 cookie')
    except Exception as ex:
        log(f'获取 B站 cookie 失败（继续尝试无 cookie 请求）：{ex}')


def get_mixin_key():
    """从 B站 nav 接口获取 WBI 混淆密钥（每次运行只取一次）"""
    global _mixin_key
    if _mixin_key:
        return _mixin_key
    nav = json.loads(http_get('https://api.bilibili.com/x/web-interface/nav'))
    img = nav['data']['wbi_img']['img_url'].rsplit('/', 1)[-1].split('.')[0]
    sub = nav['data']['wbi_img']['sub_url'].rsplit('/', 1)[-1].split('.')[0]
    raw = img + sub
    _mixin_key = ''.join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    return _mixin_key


def wbi_sign(params):
    params = dict(params)
    params['wts'] = int(time.time())
    params = {k: ''.join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(sorted(params.items()))
    w_rid = hashlib.md5((query + get_mixin_key()).encode()).hexdigest()
    return f"{query}&w_rid={w_rid}"


def fetch_bilibili_direct(uid, limit):
    """直接调 B站官方 API 获取 UP 主最近视频"""
    api = 'https://api.bilibili.com/x/space/wbi/arc/search'
    params = {'mid': uid, 'ps': min(limit * 2, 30), 'pn': 1, 'order': 'pubdate',
              'platform': 'web', 'dm_img_list': '[]', 'dm_img_str': 'no webgl',
              'dm_cover_img_str': 'no webgl', 'dm_img_inter': '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'}
    data = json.loads(http_get(f"{api}?{wbi_sign(params)}"))
    if data.get('code') != 0:
        raise RuntimeError(f"B站 API 返回 code={data.get('code')}: {data.get('message')}")
    out = []
    for v in data['data']['list']['vlist'][:limit]:
        out.append({
            'title': v.get('title', ''),
            'link': f"https://www.bilibili.com/video/{v.get('bvid', '')}",
            'date': datetime.fromtimestamp(v.get('created', 0), CN_TZ).isoformat(),
            'raw': v.get('description', '') or '',
        })
    return out


def localname(tag):
    return tag.split('}')[-1]


def strip_html(s):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    s = html.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


def parse_feed(content):
    """解析 RSS 2.0 / Atom，返回 [{title, link, date, raw}]"""
    root = ET.fromstring(content)
    items = []
    channel = root if localname(root.tag) == 'channel' else root
    for item in channel.iter():
        if localname(item.tag) not in ('item', 'entry'):
            continue
        d = {}
        for child in item:
            name = localname(child.tag)
            if name in ('title', 'link', 'summary', 'description', 'encoded', 'pubDate', 'updated', 'published'):
                if name == 'link' and child.get('href'):
                    d[name] = child.get('href')
                elif child.text:
                    d.setdefault(name, child.text.strip())
        if 'pubDate' in d:
            try:
                dt = parsedate_to_datetime(d['pubDate'])
            except Exception:
                dt = None
        elif 'updated' in d or 'published' in d:
            try:
                dt = datetime.fromisoformat((d.get('updated') or d.get('published')).replace('Z', '+00:00'))
            except Exception:
                dt = None
        else:
            dt = None
        raw = strip_html(d.get('description') or d.get('summary') or d.get('encoded') or '')
        items.append({
            'title': strip_html(d.get('title', '')),
            'link': d.get('link', ''),
            'date': dt.astimezone(CN_TZ).isoformat() if dt else None,
            'raw': raw,
        })
    return items


def classify(title):
    if any(k in title for k in ['公告', '维护', '更新说明', '补偿', '声明', '调整', '修复', '制作组通讯', '更新公告']):
        return '公告'
    if any(k in title for k in ['新角色', '角色PV', '角色 PV', '立绘', '新干员', '新时装', '时装', '服装', '皮肤', '新皮肤', '新衣装', '卡池', '上新']):
        return '角色'
    if any(k in title for k in ['活动', '版本', 'PV', '预告']):
        return '活动'
    return '其他'


def excerpt(text, n):
    return text[:n] + ('…' if len(text) > n else '')


def ai_summarize(text, cfg):
    key = os.environ.get(cfg.get('api_key_env', 'AI_API_KEY'), '')
    if not cfg.get('enabled') or not key:
        return None
    body = json.dumps({
        'model': cfg.get('model', 'deepseek-chat'),
        'messages': [
            {'role': 'system', 'content': '你是游戏资讯摘要助手。把给定资讯压缩成不超过80字的中文摘要，保留关键信息（角色名、活动名、时间、奖励），不要评价。'},
            {'role': 'user', 'content': text[:1500]},
        ],
        'max_tokens': 200, 'temperature': 0.3,
    }).encode()
    req = urllib.request.Request(cfg['base_url'].rstrip('/') + '/chat/completions',
                                 data=body, headers={'User-Agent': UA, 'Content-Type': 'application/json',
                                                     'Authorization': f'Bearer {key}'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return data['choices'][0]['message']['content'].strip()


def main():
    cfg = json.load(open('games.json', encoding='utf-8'))
    rsshub = cfg.get('rsshub', 'https://rsshub.app').rstrip('/')
    per_src = cfg.get('fetch', {}).get('items_per_source', 10)
    ex_chars = cfg.get('fetch', {}).get('excerpt_chars', 300)
    ai_cfg = cfg.get('ai', {})
    ai_budget = 30

    items, errors = [], []
    refresh_bili_cookie()
    for g in cfg['games']:
        feeds = []  # (来源名, 抓取函数)
        if g.get('bilibili', {}).get('uid'):
            feeds.append(('B站', 'bili'))
        if g.get('weibo', {}).get('uid'):
            feeds.append(('微博', f"{rsshub}/weibo/user/{g['weibo']['uid']}"))
        if g.get('xiaohongshu', {}).get('user_id'):
            feeds.append(('小红书', f"{rsshub}/xiaohongshu/user/{g['xiaohongshu']['user_id']}/notes"))

        for src, kind in feeds:
            try:
                if kind == 'bili':
                    try:
                        entries = fetch_bilibili_direct(g['bilibili']['uid'], per_src)
                    except Exception as ex1:
                        log(f"{g['name']} / B站直连失败（{ex1}），改用 RSSHub…")
                        errors.append(f"{g['name']}/B站直连: {ex1}")
                        entries = parse_feed(http_get(f"{rsshub}/bilibili/user/video/{g['bilibili']['uid']}"))[:per_src]
                else:
                    entries = parse_feed(http_get(kind))[:per_src]
                log(f"{g['name']} / {src}: {len(entries)} 条")
                for e in entries:
                    summary = None
                    if ai_budget > 0 and ai_cfg.get('enabled') and len(e['raw']) > 120:
                        summary = ai_summarize(e['raw'][:1200], ai_cfg)
                        if summary:
                            ai_budget -= 1
                        time.sleep(1)
                    items.append({
                        'game': g['id'], 'game_name': g['name'], 'color': g.get('color', '#888'),
                        'source': src, 'title': e['title'], 'link': e['link'],
                        'date': e['date'], 'cat': classify(e['title']),
                        'summary': summary or excerpt(e['raw'], ex_chars),
                    })
            except Exception as ex:
                errors.append(f"{g['name']}/{src}: {ex}")
                log(f"⚠ {g['name']} / {src} 抓取失败：{ex}")
            time.sleep(1.5)

    events = []
    for g in cfg['games']:
        for ev in g.get('events', []):
            events.append({'game': g['id'], 'game_name': g['name'], 'color': g.get('color', '#888'), **ev})

    items.sort(key=lambda x: x.get('date') or '', reverse=True)
    out = {
        'updated_at': datetime.now(CN_TZ).isoformat(timespec='seconds'),
        'games': [{'id': g['id'], 'name': g['name'], 'color': g.get('color', '#888')} for g in cfg['games']],
        'items': items, 'events': events, 'errors': errors,
    }
    json.dump(out, open('data.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    log(f"完成：{len(items)} 条资讯，{len(events)} 个活动，{len(errors)} 个源失败")


if __name__ == '__main__':
    main()
