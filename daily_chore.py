#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日家务任务推送脚本（滑动窗口 + 墨水屏渲染 + funnycoo 相册推送）

墨水屏版式（2026-09 v7 定稿）：
横图（宽≥高）或无图 → 顶部32px信息栏（描述1行截断|大区域|小区域）+ 横线
  + 照片区230px居中 + 268px横线 + 底行30px：左「家务N%」，右「YYYY年已过N%」+ 110×20电池
竖图（宽<高） → 全自适应：照片高300贴满、左贴边；竖线贴右缘；文字列=
  描述1~3行 → 线 → 大区域 → 线 → 小区域 → 线；
  进度块锚底：电池=列宽×20px（y274~294，底距6px）→ 年份行(y250) → 家务行(y226)；
  年份超列宽自动折2行(y226+y250)，家务行上移至y202（2行封顶）。
电池 = 1px墨色边框 + 2px内衬，灰色(#969696)实心填充，无凸起。
【绑定关系（原型锁定，勿动）】电池填充 = 年份进度；家务%为纯文字，无条。

家务进度口径（v7.2，用户定稿·全表轮次）：完成✔数 ÷ 有效任务总数。
  每次渲染前现查任务表（所有写操作之后），勾一个涨一档；
  一轮全部完成 → 系统清空全部✔ → 回到 0% 开新轮。
年份% = 北京时间实时计算：(现在 - 1月1日0点) ÷ 全年秒数，每天自动上涨。

渲染预览（验收用，不碰真实数据）：
系统配置表加一行「渲染预览」，值 = 竖 / 竖短 / 竖方 / 横 → 跑 workflow 会用示例数据
渲染对应版式并推到屏幕，不发飞书消息、不动任务字段，跑完自动把值改回「否」。
预览固定示例值：家务 60%（纯文字）/ 今年已过 67%（电池约2/3满）。
值 = 否 或无此行 = 正常模式。

环境变量（必填）：LARK_APP_ID / LARK_APP_SECRET（GitHub Secrets）
"""

import io
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from PIL import Image, ImageDraw, ImageFont

# ============ 配置 ============
BASE_TOKEN = os.environ.get("LARK_BASE_TOKEN", "GIgLbeJDUadS17sreyNcX7jknoe")
TASK_TABLE_ID = os.environ.get("LARK_TASK_TABLE_ID", "tblOo1DKyKgs0CV4")
CONFIG_TABLE_ID = os.environ.get("LARK_CONFIG_TABLE_ID", "tbl5WaTKn591sLJ6")
DEFAULT_VIEW_ID = os.environ.get("LARK_DEFAULT_VIEW_ID", "vewATu0DaX")
TODAY_VIEW_NAME = os.environ.get("LARK_TODAY_VIEW_NAME", "今日任务")
USER_OPEN_ID = os.environ.get("LARK_USER_OPEN_ID", "ou_487b71f46f00d88bbaf1862a0ee1639d")
DAILY_COUNT = int(os.environ.get("CHORE_DAILY_COUNT", "5"))
APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
API_BASE = "https://open.feishu.cn/open-apis"
FUNNYCOO_BASE = "https://funnycoo.cn:4001"
BEIJING_TZ = timezone(timedelta(hours=8))
LOG_TABLE_NAME = "完成记录"

# ============ 飞书 API 基础封装 ============
_tenant_token = None
_token_expire = 0


def get_tenant_token():
    """获取 tenant_access_token，带缓存（提前60s过期）"""
    global _tenant_token, _token_expire
    if _tenant_token and time.time() < _token_expire - 60:
        return _tenant_token
    if not APP_ID or not APP_SECRET:
        print("❌ 未设置 LARK_APP_ID / LARK_APP_SECRET 环境变量")
        sys.exit(1)
    url = f"{API_BASE}/auth/v3/tenant_access_token/internal"
    try:
        r = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"❌ 获取 token 网络错误: {e}")
        sys.exit(1)
    if data.get("code") != 0:
        print(f"❌ 获取 token 失败: {data}")
        sys.exit(1)
    _tenant_token = data["tenant_access_token"]
    _token_expire = time.time() + data.get("expire", 7200)
    return _tenant_token


def api_request(method, path, params=None, json_body=None, timeout=30, retries=3):
    """统一的飞书 API 请求，失败时打印详细信息"""
    token = get_tenant_token()
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}

    for attempt in range(retries):
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, params=params, timeout=timeout)
            elif method == "POST":
                r = requests.post(url, headers=headers, params=params, json=json_body, timeout=timeout)
            elif method == "PUT":
                r = requests.put(url, headers=headers, params=params, json=json_body, timeout=timeout)
            elif method == "DELETE":
                r = requests.delete(url, headers=headers, params=params, timeout=timeout)
            else:
                raise ValueError(f"不支持的 HTTP 方法: {method}")
            result = r.json()
        except requests.exceptions.Timeout:
            print(f"⚠️ API 请求超时 (第{attempt+1}次): {method} {path}")
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"code": -1, "msg": "timeout"}
        except requests.exceptions.ConnectionError as e:
            print(f"⚠️ API 连接错误 (第{attempt+1}次): {method} {path}: {e}")
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"code": -1, "msg": f"connection error: {e}"}
        except Exception as e:
            print(f"❌ API 请求异常 [{method} {path}]: {e}")
            return {"code": -1, "msg": str(e)}

        if result.get("code") == 99991663 or result.get("code") == 99991661:
            print(f"⚠️ token 过期/无效 (第{attempt+1}次)，重新获取...")
            _tenant_token = None
            token = get_tenant_token()
            headers["Authorization"] = f"Bearer {token}"
            continue

        if result.get("code") != 0:
            print(f"❌ API错误 [{method} {path}] code={result.get('code')} msg={result.get('msg')}"
                  + (f" 请求体={json.dumps(json_body, ensure_ascii=False)[:300]}" if json_body else ""))
        return result

    return {"code": -1, "msg": "max retries exceeded"}


def list_records(table_id, view_id=None, filter_str=None):
    """遍历读取整张表（自动分页）"""
    records = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        if filter_str:
            params["filter"] = filter_str
        result = api_request("GET", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records", params=params)
        if result.get("code") != 0:
            print(f"❌ 读取记录失败: {result.get('msg')}")
            break
        data = result.get("data", {})
        items = data.get("items", [])
        records.extend(items)
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return records


def batch_update(table_id, updates):
    """批量更新记录（单包最多450条）"""
    if not updates:
        return {"code": 0}
    url = f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_update"
    CHUNK = 450
    last_result = {"code": 0}
    for i in range(0, len(updates), CHUNK):
        chunk = updates[i:i + CHUNK]
        body = {"records": chunk}
        result = api_request("POST", url, json_body=body)
        if result.get("code") != 0:
            print(f"⚠️ 批量更新失败（第{i//CHUNK+1}包），降级逐条更新定位问题行...")
            for u in chunk:
                r = api_request("PUT", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{u['record_id']}",
                                json_body={"fields": u["fields"]})
                if r.get("code") != 0:
                    print(f"   ❌ 问题行 record_id={u['record_id']}，已跳过，其余行不受影响")
        last_result = result
    return last_result

# ============ 配置表读写 ============
def read_config(cfg_records, name, default=None):
    """从配置表记录中读配置项（兼容多种字段名）"""
    for r in cfg_records:
        f = r.get("fields", {})
        k = extract_field_value(f, "配置项")
        if k == name:
            v = extract_field_value(f, "值")
            return v if v is not None else default
    return default


def set_config(cfg_records, name, value):
    """写配置项：有则更新，无则新增"""
    for r in cfg_records:
        f = r.get("fields", {})
        k = extract_field_value(f, "配置项")
        if k == name:
            api_request("PUT", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{CONFIG_TABLE_ID}/records/{r['record_id']}",
                        json_body={"fields": {"值": value}})
            return
    api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{CONFIG_TABLE_ID}/records",
                json_body={"fields": {"配置项": name, "值": value}})


def send_text_message(text):
    """发送飞书文本消息（优先邮箱，失败回退 open_id）"""
    cfg_records = list_records(CONFIG_TABLE_ID)
    email = read_config(cfg_records, "飞书邮箱")
    if email:
        result = api_request("POST", "/contact/v3/users/batch_get_id",
                             params={"user_id_type": "open_id"},
                             json_body={"emails": [email]})
        if result.get("code") == 0:
            users = result.get("data", {}).get("user_list", [])
            if users and users[0].get("user_id"):
                open_id = users[0]["user_id"]
                msg_result = api_request("POST", "/im/v1/messages",
                                         params={"receive_id_type": "open_id"},
                                         json_body={"receive_id": open_id,
                                                    "msg_type": "text",
                                                    "content": json.dumps({"text": text}, ensure_ascii=False)})
                if msg_result.get("code") == 0:
                    print("✅ 已推送飞书私信")
                    return
                print(f"⚠️ 邮箱推送失败，回退 open_id：{msg_result.get('msg')}")
    result = api_request("POST", "/im/v1/messages",
                         params={"receive_id_type": "open_id"},
                         json_body={"receive_id": USER_OPEN_ID,
                                    "msg_type": "text",
                                    "content": json.dumps({"text": text}, ensure_ascii=False)})
    if result.get("code") == 0:
        print("✅ 已推送飞书私信")
    else:
        print(f"❌ open_id 推送也失败：{result.get('msg')}")


# ============ 字段工具 ============
def extract_field_value(fields, field_name):
    """从 fields 字典中提取字段值，兼容多种类型"""
    val = fields.get(field_name)
    if val is None:
        return ""
    if isinstance(val, list):
        if not val:
            return ""
        if isinstance(val[0], dict):
            return val[0].get("text", str(val[0]))
        return str(val[0])
    if isinstance(val, bool):
        return val
    return str(val)


def task_name(f):
    """任务显示名：优先小区域，其次具体区域描述"""
    small = extract_field_value(f, "小区域")
    if small:
        return small
    desc = extract_field_value(f, "具体区域描述")
    if desc:
        return desc
    return "（见表格参考图片）"


# ============ 墨水屏渲染 ============
def _find_font():
    """按优先级找中文字体"""
    candidates = [
        "NotoSansCJKsc-Regular.otf",
        os.path.join("assets", "NotoSansCJKsc-Regular.otf"),
        "NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    print("⚠️ 找不到中文字体文件，尝试从网络下载...")
    urls = [
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 200 and len(r.content) > 1000000:
                with open("NotoSansCJKsc-Regular.otf", "wb") as f:
                    f.write(r.content)
                print("✅ 字体下载完成")
                return "NotoSansCJKsc-Regular.otf"
        except Exception as e:
            print(f"⚠️ 字体下载失败: {e}")
    print("❌ 无法获取中文字体，退出")
    sys.exit(1)


def _load_font(size):
    path = _find_font()
    index = 2 if path.endswith(".ttc") else 0
    return ImageFont.truetype(path, size, index=index)


INK = (30, 30, 30)
LINE = (190, 190, 190)
PLACEHOLDER = (150, 150, 150)

def _truncate(text, font, max_w):
    """把文字限制在 max_w 像素内，超宽直接截断（不加省略号，空间全留给正文）"""
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text) > max_w:
        text = text[:-1]
    return text


def _wrap_break_all(text, font, max_w, max_lines=3):
    """逐字换行：硬切不省略，最多 max_lines 行（第 max_lines 行也硬切）"""
    lines, cur = [], ""
    for ch in text:
        if font.getlength(cur + ch) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


def _draw_row(draw, x, y_top, w, text, font, color=INK, row_h=24):
    """行槽内垂直居中绘制文字：y_top 是行槽顶部，中线 = y_top + row_h/2"""
    if not text:
        return
    bbox = font.getbbox(text)
    ty = y_top + row_h / 2 - (bbox[3] - bbox[1]) / 2 - bbox[1]
    draw.text((x, ty), text, font=font, fill=color)


def _hline(draw, x1, x2, y):
    """1px 灰横线"""
    draw.line([(x1, y), (x2, y)], fill=LINE, width=1)


def _draw_battery(draw, x, y, w, h, pct):
    """电池：1px 墨色边框 + 2px 内衬，灰色实心填充，无凸起"""
    draw.rectangle([x, y, x + w - 1, y + h - 1], outline=INK, fill=(255, 255, 255))
    inner_w = w - 6
    fw = min(inner_w, max(0, round(inner_w * pct / 100)))
    if fw > 0:
        draw.rectangle([x + 3, y + 3, x + 2 + fw, y + h - 4], fill=PLACEHOLDER)


def _year_elapsed_pct():
    """今年已过去的百分比（北京时间）"""
    now = datetime.now(BEIJING_TZ)
    start = datetime(now.year, 1, 1, tzinfo=BEIJING_TZ)
    end = datetime(now.year + 1, 1, 1, tzinfo=BEIJING_TZ)
    return round((now - start).total_seconds() / (end - start).total_seconds() * 100)


def _to_grayscale(photo):
    """转灰度再回 RGB：墨水屏黑白-only（红色暂缓）"""
    return photo.convert("L").convert("RGB")


def fit_contain(photo, box_w, box_h):
    """等比缩放，完整放入盒子（contain），返回新图"""
    pw, ph = photo.size
    scale = min(box_w / pw, box_h / ph)
    nw, nh = max(1, int(pw * scale)), max(1, int(ph * scale))
    return photo.resize((nw, nh), Image.LANCZOS)


def global_chore_progress():
    """全表家务进度（完成✔数 ÷ 有效任务总数）"""
    records = list_records(TASK_TABLE_ID, DEFAULT_VIEW_ID)
    valid = [r for r in records if is_valid_task(r["fields"])]
    if not valid:
        return 0
    done = sum(1 for r in valid if r["fields"].get("完成"))
    return round(done * 100 / len(valid))


def get_task_photo(task):
    """下载任务的参考图片（如有），返回 PIL Image 或 None"""
    f = task.get("fields", {}) if isinstance(task, dict) else {}
    photo_val = f.get("参考图片")
    if not photo_val:
        return None
    try:
        file_token = None
        if isinstance(photo_val, list) and photo_val and isinstance(photo_val[0], dict):
            file_token = photo_val[0].get("file_token")
        if not file_token:
            return None
        token = get_tenant_token()
        url = f"{API_BASE}/drive/v1/medias/{file_token}/download"
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content))
    except Exception as e:
        print(f"⚠️ 参考图片下载失败: {e}")
    return None

def render_landscape(img, draw, desc, big, small, photo, chore_pct=0, year_pct=0):
    """横图/无图版式：顶部信息栏 + 照片区 + 底部进度区"""
    font = _load_font(18)
    font_ph = _load_font(20)
    # 顶部信息栏 32px
    _draw_row(draw, 6, 0, 214, _truncate(desc, font, 214), font)
    if big:
        w = font.getlength(big)
        _draw_row(draw, 220 + (96 - w) / 2, 0, 96, big, font)
    if small:
        w = font.getlength(small)
        _draw_row(draw, 316 + 78 - w, 0, 78, small, font)
    draw.line([(220, 6), (220, 26)], fill=LINE, width=1)
    draw.line([(316, 6), (316, 26)], fill=LINE, width=1)
    _hline(draw, 0, 400, 32)

    if photo is None:
        ph = "这里是图片"
        w = font_ph.getlength(ph)
        bbox = font_ph.getbbox(ph)
        ty = 34 + 115 - (bbox[3] - bbox[1]) / 2 - bbox[1]
        draw.text(((400 - w) / 2, ty), ph, font=font_ph, fill=PLACEHOLDER)
    else:
        _hline(draw, 0, 400, 266)
        gray = _to_grayscale(photo)
        fitted = fit_contain(gray, 388, 226)
        fx = (400 - fitted.size[0]) // 2
        fy = 34 + (226 - fitted.size[1]) // 2
        img.paste(fitted, (fx, fy))

    _hline(draw, 0, 400, 268)
    batt_w, batt_h = 110, 20
    _draw_battery(draw, 394 - batt_w, 274, batt_w, batt_h, year_pct)
    year_text = f"{datetime.now().year}年已过{year_pct}%"
    tx = 394 - batt_w - 8 - font.getlength(year_text)
    _draw_row(draw, tx, 269, 214, year_text, font, row_h=30)
    _draw_row(draw, 6, 269, 214, f"家务 {chore_pct}%", font, row_h=30)


def render_portrait(img, draw, desc, big, small, photo, chore_pct=0, year_pct=0):
    """竖图版式：照片高300贴满左贴边，右侧文字列 + 右下进度块"""
    font = _load_font(18)
    if photo is None:
        render_landscape(img, draw, desc, big, small, None, chore_pct, year_pct)
        return
    gray = _to_grayscale(photo)
    fitted = fit_contain(gray, 400, 300)
    pw, ph = fitted.size
    if pw < 40 or ph < 40:
        render_landscape(img, draw, desc, big, small, photo, chore_pct, year_pct)
        return
    img.paste(fitted, (0, 0))
    text_x = pw + 8
    text_w = 394 - text_x
    if text_w < 100:
        render_landscape(img, draw, desc, big, small, photo, chore_pct, year_pct)
        return
    draw.line([(text_x - 4, 0), (text_x - 4, 300)], fill=LINE, width=1)

    y = 6
    for ln in _wrap_break_all(desc or "", font, text_w - 6, 3):
        _draw_row(draw, text_x, y, text_w - 6, ln, font)
        y += 26
    for txt in (big, small):
        if not txt:
            continue
        _hline(draw, text_x, 394, y + 2)
        y += 8
        _draw_row(draw, text_x, y, text_w - 6, txt, font)
        y += 26
    if y < 208:
        _hline(draw, text_x, 394, y + 2)

    font_sm = _load_font(18)
    _draw_row(draw, text_x, 274 - 2 * 24, text_w - 6, f"家务 {chore_pct}%", font_sm)
    _draw_row(draw, text_x, 274 - 24, text_w - 6, f"{datetime.now().year}年已过{year_pct}%", font_sm)
    _draw_battery(draw, text_x, 274, text_w - 6, 20, year_pct)


def render_eink_image(task, photo=None, chore_pct=0, year_pct=None):
    """渲染 400x300 墨水屏图片。task 为 fields 字典或 None。"""
    if year_pct is None:
        year_pct = _year_elapsed_pct()
    fields = task or {}
    desc = extract_field_value(fields, "具体区域描述")
    if not desc:
        desc = task_name(fields)
    big = extract_field_value(fields, "大区域")
    small = extract_field_value(fields, "小区域")
    if task is None:
        desc = "（无任务）"
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    if photo is not None and photo.size[1] > photo.size[0]:
        render_portrait(img, draw, desc, big, small, photo, chore_pct, year_pct)
    else:
        render_landscape(img, draw, desc, big, small, photo, chore_pct, year_pct)
    return img


# ============ 趣联设备 API ============
def get_funnycoo_devid(cfg_records):
    """从配置表读设备 ID"""
    return read_config(cfg_records, "墨水屏设备ID")


def refresh_eink_display(dev_id):
    """触发设备刷新（取最新相册图）"""
    try:
        r = requests.get(f"{FUNNYCOO_BASE}/api/refresh/{dev_id}", timeout=15)
        print(f"🔄 设备刷新: {r.status_code}")
    except Exception as e:
        print(f"⚠️ 设备刷新失败: {e}")


def push_photo_to_funnycoo(image_bytes, dev_id):
    """上传图片到趣联相册（传新删旧，相册只留一张）"""
    files = {"file": ("today.png", image_bytes, "image/png")}
    data = {"devId": dev_id}
    try:
        r = requests.post(f"{FUNNYCOO_BASE}/api/upload-photo", files=files, data=data, timeout=30)
        result = r.json()
        if result.get("success"):
            new_id = result.get("data", {}).get("id")
            print(f"🖼️ 已推送到墨水屏相册: {new_id}")
            old_list = requests.get(f"{FUNNYCOO_BASE}/api/photo-list/{dev_id}", timeout=15).json()
            for item in (old_list.get("data") or []):
                if item.get("id") != new_id:
                    requests.delete(f"{FUNNYCOO_BASE}/api/delete-photo/{dev_id}/{item['id']}", timeout=15)
                    print(f"    🗑️ 已删除相册旧图: {item['id']}")
            return True
        else:
            print(f"❌ 趣联上传失败: {result}")
            return False
    except Exception as e:
        print(f"❌ 趣联推送异常: {e}")
        return False

def beijing_today():
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


# ============ 主流程 ============
# 序号安全转 int：2026-09-14 实测飞书 API 对「序号」数字字段返回字符串
# ('7'/'25')，字符串比较导致补位全灭（每天"补充 0 条"）、重排每天误判 82 行。
# 统一强转，数字/字符串/空值全兼容。
def _seq(fields):
    v = fields.get("序号")
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


def is_valid_task(f):
    """有效任务：小区域/具体描述/参考图片 任一非空"""
    if f.get("参考图片"):
        return True
    return bool(extract_field_value(f, "小区域").strip()
                or extract_field_value(f, "具体区域描述").strip())


def get_or_create_log_table():
    """获取或创建「完成记录」表"""
    result = api_request("GET", f"/bitable/v1/apps/{BASE_TOKEN}/tables", params={"page_size": 100})
    if result.get("code") == 0:
        for t in result.get("data", {}).get("items", []):
            if t.get("name") == LOG_TABLE_NAME:
                return t["table_id"]
    result = api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables",
                         json_body={"table": {"name": LOG_TABLE_NAME, "default_view_name": "表格视图",
                                              "fields": [{"field_name": "任务", "type": 1},
                                                         {"field_name": "完成时间", "type": 1},
                                                         {"field_name": "推送批次", "type": 1}]}})
    if result.get("code") == 0:
        print(f"✅ 已创建「{LOG_TABLE_NAME}」表")
        return result["data"]["table_id"]
    print(f"⚠️ 创建完成记录表失败: {result.get('msg')}")
    return None


def log_completed_tasks(done_tasks):
    """把完成的任务写入「完成记录」表（不换行拼一条，避免行数膨胀）"""
    if not done_tasks:
        return
    log_table_id = get_or_create_log_table()
    if not log_table_id:
        return
    names = "、".join(task_name(r["fields"]) for r in done_tasks)
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    result = api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{log_table_id}/records",
                         json_body={"fields": {"任务": names, "完成时间": now_str,
                                               "推送批次": beijing_today()}})
    if result.get("code") == 0:
        print(f"📝 完成记录已写入 {len(done_tasks)} 条")
    else:
        print(f"⚠️ 完成记录写入失败: {result.get('msg')}")


def render_preview():
    """渲染预览模式：用示例数据渲染指定版式，推到屏幕，不动真实数据、不发消息"""
    cfg = list_records(CONFIG_TABLE_ID)
    preview = read_config(cfg, "渲染预览", "否") or "否"
    if preview in ("否", ""):
        return False
    print(f"🎨 渲染预览模式: {preview}")
    samples = {
        "竖":    {"具体区域描述": "17字描述预览第二行效果", "大区域": "客厅", "小区域": "沙发"},
        "竖短":  {"具体区域描述": "短描述", "大区域": "厨房", "小区域": "灶台"},
        "竖方":  {"具体区域描述": "14比15的竖方图配17字描述会自动折行到第三行封顶截断", "大区域": "卧室", "小区域": "衣柜"},
        "横":    {"具体区域描述": "横图版式回归对照", "大区域": "卫生间", "小区域": "马桶"},
    }
    task = samples.get(preview, samples["横"])
    chore_pct, year_pct = 60, 67
    if preview == "横":
        img = render_eink_image(task, photo=None, chore_pct=chore_pct, year_pct=year_pct)
    else:
        photo = Image.new("RGB", (9, 16), (200, 200, 200))
        if preview == "竖短":
            photo = Image.new("RGB", (3, 4), (200, 200, 200))
        elif preview == "竖方":
            photo = Image.new("RGB", (14, 15), (200, 200, 200))
        img = render_eink_image(task, photo=photo, chore_pct=chore_pct, year_pct=year_pct)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    dev_id = get_funnycoo_devid(cfg)
    if dev_id:
        push_photo_to_funnycoo(buf.getvalue(), dev_id)
    else:
        print("⚠️ 配置表缺少「墨水屏设备ID」，跳过推送")
    set_config(cfg, "渲染预览", "否")
    print("✅ 预览渲染并推送完成，配置已复位")
    return True

def find_view_id_by_name(table_id, name):
    """按视图名查 view_id"""
    result = api_request("GET", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/views",
                         params={"page_size": 100})
    if result.get("code") == 0:
        for v in result.get("data", {}).get("items", []):
            if v.get("view_name") == name:
                return v.get("view_id")
    return None


def refresh_eink_only():
    """防重复保险：仅重新渲染并推送图片，不动任务数据、不发消息"""
    cfg = list_records(CONFIG_TABLE_ID)
    dev_id = get_funnycoo_devid(cfg)
    if not dev_id:
        print("⚠️ 配置表缺少「墨水屏设备ID」，无法刷新")
        return
    records = list_records(TASK_TABLE_ID, DEFAULT_VIEW_ID)
    valid = [r for r in records if is_valid_task(r["fields"])]
    todo = [r for r in valid if r["fields"].get("是否今日")]
    current = next((r for r in todo if not r["fields"].get("完成")), todo[0] if todo else None)
    chore_pct = global_chore_progress()
    photo = get_task_photo(current) if current else None
    img = render_eink_image(current["fields"] if current else None, photo=photo, chore_pct=chore_pct)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    push_photo_to_funnycoo(buf.getvalue(), dev_id)


def main():
    print("===== 每日家务任务推送 =====")

    # 渲染预览模式（验收用，不碰真实数据）
    if render_preview():
        return

    # 防重复保险
    cfg = list_records(CONFIG_TABLE_ID)
    if read_config(cfg, "上次推送日期") == beijing_today():
        print("今天已推送过（防重复保险），仅刷新墨水屏图片")
        refresh_eink_only()
        return

    print("===== 拉取任务数据 =====")
    records = list_records(TASK_TABLE_ID, DEFAULT_VIEW_ID)
    valid = [r for r in records if is_valid_task(r["fields"])]
    print(f"共 {len(records)} 行，有效任务 {len(valid)} 条")

    # 序号原位重排（保持 Grid 视图行序）
    updates = []
    for i, r in enumerate(valid, 1):
        if _seq(r["fields"]) != i:
            updates.append((r["record_id"], {"序号": i}))
    if updates:
        print(f"序号重排 {len(updates)} 行")

    # 今日任务列表（是否今日=真）
    todo = [r for r in valid if r["fields"].get("是否今日")]
    done_today = [r for r in todo if r["fields"].get("完成")]

    # 全完成判定：全表 82 个全部完成 → 清空完成标记，开新一轮
    # （2026-09-14 修正：原为"今日 5 个全完成就清零"，会把全表勾误清）
    if valid and all(r["fields"].get("完成") for r in valid):
        print("🎉 全表完成，清空完成标记，开新一轮")
        clear_updates = [{"record_id": r["record_id"], "fields": {"完成": False}}
                         for r in valid if r["fields"].get("完成")]
        batch_update(TASK_TABLE_ID, clear_updates)
        records = list_records(TASK_TABLE_ID, DEFAULT_VIEW_ID)
        valid = [r for r in records if is_valid_task(r["fields"])]
        todo = [r for r in valid if r["fields"].get("是否今日")]
        done_today = [r for r in todo if r["fields"].get("完成")]

    # 记录今日完成（写完成记录表）
    if done_today:
        log_completed_tasks(done_today)

    # 移出已完成（是否今日=否）
    for r in done_today:
        updates.append((r["record_id"], {"是否今日": False}))
        todo.remove(r)

    # 待办超限清理（保持最多 DAILY_COUNT 条）
    if len(todo) > DAILY_COUNT:
        todo.sort(key=lambda r: _seq(r["fields"]) or 9999)
        for r in todo[DAILY_COUNT:]:
            updates.append((r["record_id"], {"是否今日": False}))
        todo = todo[:DAILY_COUNT]
        print(f"清理超出 {DAILY_COUNT} 条的待办 {len(updates)} 行" if updates else "")

    # 智能补充：不足 DAILY_COUNT 个时，按 Grid 行序从未完成任务中补齐
    lack = DAILY_COUNT - len(todo)
    if lack > 0:
        last_seq = max((_seq(r["fields"]) for r in todo), default=0) if todo else 0
        todo_seqs = {_seq(r["fields"]) for r in todo}
        picked = []
        for r in valid:
            seq = _seq(r["fields"])
            if r["fields"].get("完成") or seq in todo_seqs or seq <= (last_seq if todo else 0):
                continue
            picked.append(r)
            if len(picked) == lack:
                break
        for r in picked:
            updates.append((r["record_id"], {"是否今日": True}))
            todo.append(r)
        print(f"补充 {len(picked)} 条")

    # 按「今日任务」视图顺序排列
    today_view_id = find_view_id_by_name(TASK_TABLE_ID, TODAY_VIEW_NAME)
    if today_view_id:
        ordered = [r["record_id"] for r in list_records(TASK_TABLE_ID, today_view_id)]
        todo.sort(key=lambda r: ordered.index(r["record_id"]) if r["record_id"] in ordered else 999)
        print("按「今日任务」视图顺序排列")

    # 应用所有更新
    if updates:
        batch_update(TASK_TABLE_ID, updates)

    # 渲染并推送
    chore_pct = global_chore_progress()
    valid_count = len(valid)
    done_count = round(chore_pct * valid_count / 100)
    print(f"本轮进度: {done_count}/{valid_count} · {chore_pct}%")
    current = next((r for r in todo if not r["fields"].get("完成")), todo[0] if todo else None)
    photo = get_task_photo(current) if current else None
    if photo is not None and photo.size[1] > photo.size[0]:
        print("版式: 竖图")
    else:
        print("版式: 横图（无图）" if photo is None else "版式: 横图")
    img = render_eink_image(current["fields"] if current else None, photo=photo, chore_pct=chore_pct)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    with open("docs/today.png", "wb") as f:
        f.write(buf.getvalue())
    dev_id = get_funnycoo_devid(cfg)
    if dev_id:
        push_photo_to_funnycoo(buf.getvalue(), dev_id)
    else:
        print("⚠️ 配置表缺少「墨水屏设备ID」，跳过推送")

    # 记录推送日期（防重复）+ 推送飞书私信
    if todo:
        # 飞书私信推送已按用户要求移除（2026-09-14，有墨水屏不需要私信）
        set_config(cfg, "上次推送日期", beijing_today())
        print(f"✅ 今日 {len(todo)} 条待办")
    else:
        print("今日无待办")


if __name__ == "__main__":
    main()
