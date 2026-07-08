"""
每日游戏行业资讯自动采集 & 飞书推送
用法: python daily_push.py [--dry-run] [--max-cards N]
定时: Windows 任务计划程序, 每天 8:55 运行
"""
import json, os, sys, re, time, hashlib, argparse, subprocess, io
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from collections import defaultdict
import xml.etree.ElementTree as ET

import requests
from openai import OpenAI

# ── 路径 ──
BASE_DIR = Path(__file__).resolve().parent
SENT_PATH = BASE_DIR / "sent_items.json"
CONFIG_PATH = BASE_DIR / "config.json"
TZ = timezone(timedelta(hours=8))

# ── 配置 ──
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CFG = json.load(f)

# Kimi API (可选)
_raw_key = CFG.get("kimi_api_key", "")
if _raw_key.startswith("${") and _raw_key.endswith("}"):
    _env_name = _raw_key[2:-1]
    _raw_key = os.environ.get(_env_name, "")
kimi_client = None
if _raw_key:
    try:
        kimi_client = OpenAI(api_key=_raw_key, base_url=CFG.get("kimi_api_base", "https://api.moonshot.cn/v1"))
    except Exception as e:
        print(f"  [WARN] Kimi API 不可用, 将用关键词排序: {e}")

# 飞书推送目标（支持多人）
FEISHU_USER_IDS = [
    "ou_0778a44bd227886191ea870d8fe6f515",  # 刘湘
    "ou_0ae56b7ba5bb9b53150902375ccbd770",  # 刘辰
]
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
USE_FEISHU_API = bool(FEISHU_APP_ID and FEISHU_APP_SECRET)  # GitHub Actions 模式

if USE_FEISHU_API:
    print("  模式: 飞书API直连 (GitHub Actions)")
else:
    print("  模式: lark-cli (本地)")

_feishu_token = None
_feishu_token_expire = 0

def get_feishu_token():
    """获取飞书 tenant_access_token (带缓存)"""
    global _feishu_token, _feishu_token_expire
    if _feishu_token and time.time() < _feishu_token_expire:
        return _feishu_token
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET
    }, timeout=10)
    data = resp.json()
    if data.get("code") == 0:
        _feishu_token = data["tenant_access_token"]
        _feishu_token_expire = time.time() + data.get("expire", 3600) - 300
        return _feishu_token
    raise Exception(f"飞书token失败: {data}")

# ── RSS 源 ──
RSS_SOURCES = [
    {"name": "GameLook", "url": "http://www.gamelook.com.cn/feed/"},
    {"name": "白鲸出海", "url": "https://www.baijing.cn/feed"},
    {"name": "手游那点事", "url": "http://www.nadianshi.com/feed"},
    {"name": "游戏茶馆", "url": "https://youxichaguan.com/feed"},
]

# ── 关键词 ──
KEYWORDS = [
    "二合", "合成", "merge", "Merge", "合并",
    "柠檬微趣", "点点互动", "Gossip Harbor", "Tasty Travels", "浪漫餐厅",
    "桃源深处有人家", "Whisper Castle", "Just Desserts", "Hotel Legacy",
    "Hollywood Merge", "Flambé", "Seaside Escape",
    "女性向", "出海", "休闲游戏", "模拟经营",
    "AI 3D", "3D生成", "3D大模型", "AI生成", "AI工具",
    "AI动画", "世界模型", "AI资产生成", "AI游戏",
    "AIGC", "大模型", "AI驱动", "AI视频",
    "影眸", "Hyper3D", "VAST", "Tripo", "Meshy", "Seele",
    "游戏葡萄", "光子", "腾讯", "网易",
    "手游", "新游", "开测", "流水", "收入", "月流水",
    "融资", "收购", "独立游戏", "派对游戏",
    "Steam", "畅销", "爆款", "黑马",
]

EXCLUDE_WORDS = [
    "招聘", "校招", "实习", "春招", "秋招", "社招",
    "股票", "股价", "涨停", "跌停", "A股", "港股",
    "区块链", "加密货币", "NFT", "Web3",
]


def load_sent():
    if SENT_PATH.exists():
        with open(SENT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"topics_sent": [], "ai_tools_sent": [], "last_push": "", "push_count": 0}


def save_sent(data):
    with open(SENT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def strip_html(text):
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_first_image(html_text):
    if not html_text:
        return ""
    matches = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_text)
    for src in matches:
        if not any(x in src.lower() for x in ['avatar', 'logo', 'icon', 'gravatar', '1x1', 'pixel']):
            return src
    return ""


def match_keywords(text):
    return [kw for kw in KEYWORDS if kw.lower() in text.lower()]


def is_excluded(text):
    return any(ew in text for ew in EXCLUDE_WORDS)


def is_duplicate(article, sent_data):
    combined = article.get("title", "") + article.get("summary", "")
    for sent_topic in sent_data.get("topics_sent", []):
        sent_words = set(re.findall(r'[一-鿿\w]+', sent_topic.lower()))
        article_words = set(re.findall(r'[一-鿿\w]+', combined.lower()))
        if sent_words and article_words:
            overlap = len(sent_words & article_words) / max(len(sent_words), 1)
            if overlap > 0.6:
                return True
    for tool_name in sent_data.get("ai_tools_sent", []):
        core = tool_name.split("/")[0].strip().lower()
        if len(core) >= 3 and combined.lower().count(core) >= 2:
            return True
    return False


def fetch_rss(source):
    articles = []
    try:
        resp = requests.get(source["url"], headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }, timeout=30, verify=False)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall('.//item')
        if not items:
            items = root.findall('.//{http://www.w3.org/2005/Atom}entry')

        for item in items:
            title_el = item.find('title')
            if title_el is None:
                title_el = item.find('{http://www.w3.org/2005/Atom}title')
            title = title_el.text.strip() if title_el is not None and title_el.text else ""

            link_el = item.find('link')
            if link_el is None:
                link_el = item.find('{http://www.w3.org/2005/Atom}link')
            link = ""
            if link_el is not None:
                link = link_el.text or link_el.get('href', '') or ""

            date_el = item.find('pubDate')
            for tag in ['published', '{http://www.w3.org/2005/Atom}published',
                        '{http://www.w3.org/2005/Atom}updated']:
                if date_el is None:
                    date_el = item.find(tag)
            pub_date = date_el.text.strip() if date_el is not None and date_el.text else ""

            desc_el = item.find('description')
            for tag in ['{http://www.w3.org/2005/Atom}summary',
                        '{http://www.w3.org/2005/Atom}content']:
                if desc_el is None:
                    desc_el = item.find(tag)
            desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

            content_ns = '{http://purl.org/rss/1.0/modules/content/}encoded'
            content_el = item.find(content_ns)
            content = content_el.text.strip() if content_el is not None and content_el.text else ""

            full_html = content or desc
            articles.append({
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "source": source["name"],
                "img_url": extract_first_image(full_html),
                "summary": strip_html(full_html)[:300],
            })
    except Exception as e:
        print(f"  [WARN] {source['name']} RSS 失败: {e}")
    return articles


def is_recent(article, days=3):
    dt = parse_date_str(article.get("pub_date", ""))
    if dt is None:
        return True
    return (datetime.now(timezone.utc) - dt).days < days


def parse_date_str(date_str):
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str).replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ['%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S']:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def upload_image(img_url):
    """下载图片并上传飞书，返回image_key"""
    if not img_url:
        return None
    try:
        resp = requests.get(img_url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.google.com/"
        }, timeout=15, verify=False)
        if resp.status_code != 200 or len(resp.content) < 500:
            return None

        if USE_FEISHU_API:
            # GitHub Actions: 飞书API直连
            token = get_feishu_token()
            url = "https://open.feishu.cn/open-apis/im/v1/images"
            img_bytes = io.BytesIO(resp.content)
            ext = img_url.split('.')[-1].split('?')[0]
            if ext not in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
                ext = 'jpg'
            files = {"image": (f"cover.{ext}", img_bytes, "application/octet-stream")}
            headers = {"Authorization": f"Bearer {token}"}
            r = requests.post(url, files=files, data={"image_type": "message"}, headers=headers, timeout=20)
            data = r.json()
            if data.get("code") == 0:
                return data["data"]["image_key"]
            print(f"  [WARN] 飞书图片上传: {data.get('msg', '')}")
        else:
            # 本地: lark-cli via bash
            ext = img_url.split('.')[-1].split('?')[0]
            if ext not in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
                ext = 'jpg'
            tmp_path = BASE_DIR / f"_tmp_img.{ext}"
            with open(tmp_path, 'wb') as f:
                f.write(resp.content)
            cmd = f"lark-cli im images create --data '{{\"image_type\":\"message\"}}' --file '{tmp_path.as_posix()}' --as bot"
            result = subprocess.run(['bash', '-c', cmd],
                                    capture_output=True, text=True, timeout=20, cwd=str(BASE_DIR))
            tmp_path.unlink(missing_ok=True)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data.get('ok'):
                    return data['data']['image_key']
    except Exception as e:
        print(f"  [WARN] 图片上传失败: {e}")
    return None


def send_card(article, img_key):
    """发飞书卡片消息"""
    title = article.get("title", "")[:100]
    source = article.get("source", "")
    link = article.get("link", "")
    summary = article.get("summary", "")[:250]

    templates = ["blue", "turquoise", "purple", "orange", "red", "yellow", "green"]
    template = templates[hash(title) % len(templates)]

    elements = []
    if img_key:
        elements.append({"tag": "img", "img_key": img_key, "alt": {"tag": "plain_text", "content": title[:50]}})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{source}**\n\n{summary}"}})
    if link:
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "阅读原文"}, "url": link, "type": "primary"}]})
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": source}]})

    card = {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": title}, "template": template}, "elements": elements}
    card_str = json.dumps(card, ensure_ascii=False)

    ok_count = 0
    for uid in FEISHU_USER_IDS:
        try:
            if USE_FEISHU_API:
                token = get_feishu_token()
                url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
                body = {"receive_id": uid, "msg_type": "interactive", "content": card_str}
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                r = requests.post(url, json=body, headers=headers, timeout=20)
                data = r.json()
                if data.get("code") == 0:
                    ok_count += 1
                else:
                    print(f"  [ERROR] 飞书发送({uid[:10]}): {data.get('msg','')}")
            else:
                tmp = BASE_DIR / "_tmp_card.json"
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(card_str)
                bash_cmd = f"lark-cli im +messages-send --user-id {uid} --as bot --msg-type interactive --content \"$(cat '{tmp.as_posix()}')\""
                result = subprocess.run(['bash', '-c', bash_cmd],
                                        capture_output=True, text=True, timeout=20, cwd=str(BASE_DIR))
                tmp.unlink(missing_ok=True)
                if result.returncode == 0 and json.loads(result.stdout).get('ok'):
                    ok_count += 1
                else:
                    print(f"  [ERROR] lark-cli({uid[:10]}): {result.stderr[:100]}")
            time.sleep(1)
        except Exception as e:
            print(f"  [ERROR] 发送失败({uid[:10]}): {e}")
    return ok_count > 0


def ai_filter(articles, sent_data, top_n=8):
    """用Kimi筛选，不可用时退化为关键词排序"""
    if len(articles) <= top_n:
        return articles
    if kimi_client is None:
        for a in articles:
            a["_score"] = len(match_keywords(a["title"] + a.get("summary", "")))
        articles.sort(key=lambda x: x["_score"], reverse=True)
        return articles[:top_n]

    cand = "\n".join(f"[{i}] {a['source']} | {a['title']}" for i, a in enumerate(articles[:20]))
    sent_topics = "\n".join(f"- {t}" for t in sent_data.get("topics_sent", [])[-10:])
    prompt = f"""从候选游戏行业资讯中选{top_n}篇最有价值的。
已发送主题(避免重复):\n{sent_topics}\n\n候选:\n{cand}\n\n只回复编号, 格式: 3,7,12,5"""
    try:
        resp = kimi_client.chat.completions.create(
            model=CFG.get("kimi_model", "kimi-k2.6"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=50)
        indices = [int(x) for x in re.findall(r'\d+', resp.choices[0].message.content)]
        filtered = [articles[i] for i in indices if 0 <= i < len(articles)]
        if filtered:
            return filtered
    except Exception as e:
        print(f"  [WARN] AI筛选失败: {e}")
    for a in articles:
        a["_score"] = len(match_keywords(a["title"] + a.get("summary", "")))
    articles.sort(key=lambda x: x["_score"], reverse=True)
    return articles[:top_n]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-cards", type=int, default=4)
    args = parser.parse_args()

    t0 = datetime.now()
    print(f"=== 每日游戏资讯采集 {t0.strftime('%Y-%m-%d %H:%M')} ===\n")

    sent_data = load_sent()
    print(f"已推送 {sent_data.get('push_count', 0)} 次, 记录 {len(sent_data.get('topics_sent', []))} 个主题\n")

    # 禁用 SSL 警告
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # 1. RSS
    print("[1/5] 抓取RSS...")
    all_articles = []
    for src in RSS_SOURCES:
        arts = fetch_rss(src)
        recent = [a for a in arts if is_recent(a, 3)]
        print(f"  {src['name']}: {len(recent)} 篇(近3天)")
        all_articles.extend(recent)
    print(f"  共 {len(all_articles)} 篇\n")

    if not all_articles:
        print("无新文章")
        return

    # 2. 关键词
    print("[2/5] 关键词过滤...")
    matched = []
    for a in all_articles:
        text = a["title"] + a.get("summary", "")
        if match_keywords(text) and not is_excluded(text) and a["title"]:
            matched.append(a)
    print(f"  匹配 {len(matched)} 篇\n")

    # 3. 去重
    print("[3/5] 去重...")
    fresh = [a for a in matched if not is_duplicate(a, sent_data)]
    print(f"  去重后 {len(fresh)} 篇\n")

    if not fresh:
        print("无新内容")
        return

    # 4. 筛选
    print(f"[4/5] 筛选 {args.max_cards} 篇...")
    selected = ai_filter(fresh, sent_data, args.max_cards)
    for i, a in enumerate(selected):
        print(f"  [{i+1}] {a['source']} | {a['title'][:60]}")
    print()

    if args.dry_run:
        print("[DRY-RUN] 不推送")
        return

    # 5. 推送
    print(f"[5/5] 推送 {len(selected)} 张卡片...")
    new_topics = []
    for i, art in enumerate(selected):
        print(f"  [{i+1}/{len(selected)}] {art['title'][:50]}...")
        img_key = upload_image(art.get("img_url"))
        ok = send_card(art, img_key)
        if ok:
            new_topics.append(art["title"][:30])
            print(f"    成功")
        else:
            if img_key:
                print("    重试无图...")
                ok2 = send_card(art, None)
                if ok2:
                    new_topics.append(art["title"][:30])
                    print("    成功(无图)")
                else:
                    print("    失败")
            else:
                print("    失败")
        time.sleep(1)

    # 更新记录
    sent_data["topics_sent"].extend(new_topics)
    sent_data["topics_sent"] = sent_data["topics_sent"][-200:]
    sent_data["last_push"] = t0.strftime("%Y-%m-%d")
    sent_data["push_count"] = sent_data.get("push_count", 0) + 1
    save_sent(sent_data)

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n=== 完成 ({elapsed:.0f}s) === 推送 {len(new_topics)} 篇")


if __name__ == "__main__":
    main()
