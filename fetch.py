#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取各游戏官方账号（B站/微博/小红书）的最新动态，生成 data.json 供 index.html 展示。
只依赖 Python 标准库，可直接在 GitHub Actions 运行。
数据来源：RSSHub（rsshub.app 或你自建的实例https://rsshub.rssforever.com）。
"""
import json, re, os, sys, html, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

CN_TZ = timezone(timedelta(hours=8))
UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive'
}


def log(msg):
    print(f"[{datetime.now(CN_TZ):%H:%M:%S}] {msg}", flush=True)


def fetch_url(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def localname(tag):
    return tag.split('}')[-1]


def strip_html(s):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    s = html.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


def parse_feed(content):
    """解析 RSS 2.0 / Atom，返回 [{title, link, date, raw_text}]"""
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
    """可选：调用兼容 OpenAI 接口的模型生成摘要。失败时返回 None。"""
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
                                 data=body, headers={**UA, 'Content-Type': 'application/json',
                                                     'Authorization': f'Bearer {key}'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return data['choices'][0]['message']['content'].strip()


def main():
    cfg = json.load(open('games.json', encoding='utf-8'))
    rsshub = cfg.get('rsshub', 'https://rsshub.rssforever.com').rstrip('/')
    per_src = cfg.get('fetch', {}).get('items_per_source', 10)
    ex_chars = cfg.get('fetch', {}).get('excerpt_chars', 300)
    ai_cfg = cfg.get('ai', {})
    ai_budget = 30  # 每次运行最多 AI 摘要 30 条，控制成本

    items, errors = [], []
    for g in cfg['games']:
        feeds = []
        if g.get('bilibili', {}).get('uid'):
            feeds.append(('B站', f"{rsshub}/bilibili/user/video/{g['bilibili']['uid']}"))
        if g.get('weibo', {}).get('uid'):
            feeds.append(('微博', f"{rsshub}/weibo/user/{g['weibo']['uid']}"))
        if g.get('xiaohongshu', {}).get('user_id'):
            feeds.append(('小红书', f"{rsshub}/xiaohongshu/user/{g['xiaohongshu']['user_id']}/notes"))
        for src, url in feeds:
            try:
                entries = parse_feed(fetch_url(url))[:per_src]
                log(f"{g['name']} / {src}: {len(entries)} 条")
                for e in entries:
                    summary = None
                    if ai_budget > 0 and ai_cfg.get('enabled') and len(e['raw']) > 120:
                        summary = ai_summarize(e['raw'][:1200], ai_cfg)
                        ai_budget -= 1 if summary else 0
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
            time.sleep(1.5)  # 礼貌限速

    # 活动事件（来自 games.json 手动维护）
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
