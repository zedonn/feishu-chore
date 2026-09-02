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
电池 = 1px墨色边框 + 2px内衬，灰色(#969696)实心填充=精确百分比，无凸起。

家务进度口径（v7.2，用户定稿·全表轮次）：完成✔数 ÷ 有效任务总数。
  每次渲染前现查任务表（所有写操作之后），勾一个涨一档；
  一轮全部完成 → 系统清空全部✔ → 回到 0% 开新轮。
年份% = 北京时间实时计算：(现在 - 1月1日0点) ÷ 全年秒数，每天自动上涨。

渲染预览（验收用，不碰真实数据）：
系统配置表加一行「渲染预览」，值 = 竖 / 竖短 / 竖方 / 横 → 跑 workflow 会用示例数据
渲染对应版式并推到屏幕，不发飞书消息、不动任务字段，跑完自动把值改回「否」。
预览固定示例值：家务 60% / 今年已过 67%。
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

# ============ 飞书 API 基础 ============
_tenant_token = None
_token_expire = 0


def get_tenant_token():
    global _tenant_token, _token_expire
    if _tenant_token and time.time() < _token_expire - 60:
        return _tenant_token
    if not APP_ID or not APP_SECRET:
        print("错误：未设置 LARK_APP_ID / LARK_APP_SECRET")
        sys.exit(1)
    resp = requests.post(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"获取 token 失败: {data}")
        sys.exit(1)
    _tenant_token = data["tenant_access_token"]
    _token_expire = time.time() + data.get("expire", 7200)
    return _tenant_token


def api_request(method, path, params=None, json_body=None):
    token = get_tenant_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    resp = requests.request(method, f"{API_BASE}{path}", headers=headers, params=params, json=json_body, timeout=30)
    try:
        result = resp.json()
    except Exception:
        print(f"❌ API 非JSON响应 [{method} {path}] HTTP {resp.status_code}: {resp.text[:300]}")
        return {"code": -1, "msg": "非JSON响应"}
    if result.get("code") != 0:
        print(f"❌ API错误 [{method} {path}] code={result.get('code')} msg={result.get('msg')}")
    return result


# ============ 记录操作 ============
def list_records(table_id, view_id=None, page_size=200):
    all_items, page_token = [], None
    while True:
        params = {"page_size": page_size}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        result = api_request("GET", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records", params=params)
        if result.get("code") != 0:
            break
        data = result.get("data", {})
        all_items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return [{"record_id": i["record_id"], "fields": i.get("fields", {})} for i in all_items]


def update_record(table_id, record_id, fields):
    return api_request("PUT", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}",
                       json_body={"fields": fields})


def batch_update_records(table_id, updates):
    if not updates:
        return
    CHUNK = 450
    for start in range(0, len(updates), CHUNK):
        chunk = updates[start:start + CHUNK]
        body = {"records": [{"record_id": r, "fields": f} for r, f in chunk]}
        result = api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_update",
                             json_body=body)
        if result.get("code") == 0:
            continue
        print(f"⚠️ 批量更新被拒，降级逐行定位问题（{len(chunk)}条）")
        for rid, f in chunk:
            r = update_record(table_id, rid, f)
            if r.get("code") != 0:
                print(f"    ❌ 问题行 record_id={rid}，跳过")


def list_fields(table_id):
    result = api_request("GET", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
                         params={"page_size": 100})
    return result.get("data", {}).get("items", []) if result.get("code") == 0 else []


def find_view_id_by_name(table_id, name):
    result = api_request("GET", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/views",
                         params={"page_size": 100})
    if result.get("code") == 0:
        for v in result.get("data", {}).get("items", []):
            if v.get("view_name") == name:
                return v.get("view_id")
    return None


def extract_field_value(fields, field_name):
    """从 fields 字典中提取字段值，兼容多种类型（文字字段会返回片段列表）"""
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


# ============ 配置表读写 ============
def read_config_all():
    cfg = {}
    for r in list_records(CONFIG_TABLE_ID):
        key = extract_field_value(r["fields"], "配置项")
        if key:
            cfg[key] = r
    return cfg


def read_config(cfg, key):
    rec = cfg.get(key)
    if not rec:
        return None
    val = extract_field_value(rec["fields"], "值")
    return val if val != "" else None


def set_config(cfg, key, value):
    rec = cfg.get(key)
    if rec:
        update_record(CONFIG_TABLE_ID, rec["record_id"], {"值": str(value)})
    else:
        api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{CONFIG_TABLE_ID}/records",
                    json_body={"fields": {"配置项": key, "值": str(value)}})
    cfg[key] = {"record_id": rec["record_id"] if rec else None,
                "fields": {"配置项": key, "值": str(value)}}


# ============ 消息发送（邮箱优先，open_id 兜底） ============
def send_text_message(text):
    cfg = read_config_all()
    email = read_config(cfg, "飞书邮箱")
    if email:
        result = api_request("POST", "/im/v1/messages", params={"receive_id_type": "email"},
                             json_body={"receive_id": email, "msg_type": "text",
                                        "content": json.dumps({"text": text}, ensure_ascii=False)})
        if result.get("code") == 0:
            print("推送收件方式: 邮箱")
            return
        print(f"⚠️ 邮箱推送失败，回退 open_id：{result.get('msg')}")
    result = api_request("POST", "/im/v1/messages", params={"receive_id_type": "open_id"},
                         json_body={"receive_id": USER_OPEN_ID, "msg_type": "text",
                                    "content": json.dumps({"text": text}, ensure_ascii=False)})
    if result.get("code") != 0:
        print(f"❌ open_id 推送也失败：{result.get('msg')}")


# ============ 附件下载 ============
def fetch_photo(fields):
    """下载「参考图片」第一张，返回 PIL Image（RGB）或 None"""
    atts = fields.get("参考图片")
    if not atts:
        return None
    att = atts[0]
    url = att.get("url")
    if not url:
        return None
    token = get_tenant_token()
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code != 200:
        print(f"⚠️ 附件下载失败 HTTP {resp.status_code}")
        return None
    try:
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        print(f"⚠️ 附件解析失败: {e}")
        return None


# ============ 渲染 ============
def _load_font(size=18):
    for p in ["NotoSansCJKsc-Regular.otf", "assets/NotoSansCJKsc-Regular.otf",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size, index=2 if p.endswith(".ttc") else 0)
            except Exception:
                continue
    print("⚠️ 未找到中文字体，使用默认字体（显示效果会差）")
    return ImageFont.load_default()


FONT = _load_font(18)
FONT_PLACEHOLDER = _load_font(20)
INK = (30, 30, 30)
LINE = (190, 190, 190)
PLACEHOLDER = (150, 150, 150)  # #969696，v7 电池填充与占位文字同源


def _truncate(text, font, max_w):
    """超宽直接切，不加省略号（用户定稿规则）"""
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text) > max_w:
        text = text[:-1]
    return text


def _wrap_break_all(text, font, max_w, max_lines):
    """逐字换行，最多 max_lines 行，超出直接丢弃（word-break:break_all 等效）"""
    lines, cur = [], ""
    for ch in text:
        if cur and font.getlength(cur + ch) > max_w:
            lines.append(cur)
            if len(lines) == max_lines:
                return lines
            cur = ch
        else:
            cur += ch
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines or [""]


def _draw_row(draw, x, y_top, w, text, font, color=INK, row_h=24):
    """行槽内绘制，墨迹垂直居中于行槽几何中线（textbbox 校准）；row_h 默认 24px"""
    if not text:
        return
    bbox = font.getbbox(text)
    ty = y_top + row_h / 2 - (bbox[3] - bbox[1]) / 2 - bbox[1]
    draw.text((x, ty), text, font=font, fill=color)


def _hline(draw, x1, x2, y):
    draw.line([(x1, y), (x2, y)], fill=LINE, width=1)


def _draw_battery(draw, x, y, w, h, pct):
    """v7 电池进度条：1px 墨色边框 + 2px 内衬，灰色实心填充=精确百分比，无凸起
    pct=0 时为空电池（设计内行为：0% 本来就无填充）"""
    draw.rectangle([x, y, x + w - 1, y + h - 1], outline=INK, fill=(255, 255, 255))
    inner_w = w - 6
    fw = min(inner_w, max(0, round(inner_w * pct / 100)))
    if fw > 0:
        draw.rectangle([x + 3, y + 3, x + 2 + fw, y + h - 4], fill=PLACEHOLDER)


def _year_elapsed_pct():
    """今年已过百分比（北京时间，按秒计算后取整；每天自动上涨约0.27%）"""
    now = datetime.now(BEIJING_TZ)
    start = datetime(now.year, 1, 1, tzinfo=BEIJING_TZ)
    end = datetime(now.year + 1, 1, 1, tzinfo=BEIJING_TZ)
    return round((now - start).total_seconds() / (end - start).total_seconds() * 100)


def _to_grayscale(photo):
    return photo.convert("L").convert("RGB")


def fit_contain(photo, box_w, box_h):
    ratio = min(box_w / photo.width, box_h / photo.height)
    nw, nh = max(1, round(photo.width * ratio)), max(1, round(photo.height * ratio))
    return photo.resize((nw, nh), Image.LANCZOS)


# ---- 横图版式 ----
def render_landscape(img, draw, desc, big, small, photo, chore_pct=0, year_pct=0):
    draw.text((6, _info_y(desc and _truncate(desc, FONT, 214))), "", font=FONT)  # 占位防警告
    _draw_row(draw, 6, 0, 214, _truncate(desc or "", FONT, 214), FONT)
    if big:
        w = FONT.getlength(big)
        _draw_row(draw, 220 + (96 - w) / 2, 0, 96, big, FONT)
    if small:
        w = FONT.getlength(small)
        _draw_row(draw, 316 + 78 - w, 0, 78, small, FONT)
    draw.line([(220, 6), (220, 26)], fill=LINE, width=1)
    draw.line([(316, 6), (316, 26)], fill=LINE, width=1)
    _hline(draw, 0, 400, 32)
    if photo:
        photo = _to_grayscale(photo)
        p = fit_contain(photo, 400, 230)  # v7: 262→230，给底部进度行让位
        img.paste(p, (round((400 - p.width) / 2), 34 + round((230 - p.height) / 2)))
    else:
        ph = "这里是图片"
        w = FONT_PLACEHOLDER.getlength(ph)
        bbox = FONT_PLACEHOLDER.getbbox(ph)
        ty = 34 + 115 - (bbox[3] - bbox[1]) / 2 - bbox[1]
        draw.text(((400 - w) / 2, ty), ph, font=FONT_PLACEHOLDER, fill=PLACEHOLDER)
    # ---- v7 底部进度行（y268 横线，行槽 y269~299）----
    _hline(draw, 0, 400, 268)
    batt_w, batt_h = 110, 20
    _draw_battery(draw, 394 - batt_w, 274, batt_w, batt_h, chore_pct)
    year_text = f"{datetime.now(BEIJING_TZ).year}年已过{year_pct}%"
    tx = 394 - batt_w - 8 - FONT.getlength(year_text)
    _draw_row(draw, tx, 269, 214, year_text, FONT, row_h=30)
    _draw_row(draw, 6, 269, 214, f"家务 {chore_pct}%", FONT, row_h=30)


def _info_y(_):
    return 0  # 横图信息栏行槽 0~32，墨迹中线 16（_draw_row 内 y_top=0, 中心16）


# ---- 竖图版式（v5 全自适应 + v7 锚底进度块）----
def render_portrait(img, draw, desc, big, small, photo, chore_pct=0, year_pct=0):
    H = 300
    # 照片：高300贴满，宽=300×宽高比，左贴边
    pw = max(1, round(photo.width * H / photo.height))
    p = _to_grayscale(photo).resize((pw, H), Image.LANCZOS)
    img.paste(p, (0, 0))
    # 竖线紧贴照片右缘，全高
    draw.line([(pw, 0), (pw, H)], fill=LINE, width=1)
    # 文字列：竖线右 8px 起，右边距 6px
    col_left = pw + 8
    col_right = 400 - 6
    col_w = col_right - col_left
    y = 6
    # 描述 1~3 行自适应
    for ln in _wrap_break_all(desc or "", FONT, col_w, 3):
        _draw_row(draw, col_left, y, col_w, ln, FONT)
        y += 24
    # 线 → 大区域 → 线 → 小区域 → 线（空字段连同行和线一起跳过）
    for text in (big, small):
        if text:
            _hline(draw, col_left, col_right, y)
            y += 1
            _draw_row(draw, col_left, y, col_w, text, FONT)
            y += 24
    _hline(draw, col_left, col_right, y)
    # ---- v7 进度块（锚底：电池钉 y274~294 底距6px；年份超宽折2行则家务行上移）----
    _draw_battery(draw, col_left, 274, col_w, 20, chore_pct)
    year_text = f"{datetime.now(BEIJING_TZ).year}年已过{year_pct}%"
    if FONT.getlength(year_text) <= col_w:
        _draw_row(draw, col_left, 250, col_w, year_text, FONT)
        chore_y = 226
    else:
        lines = _wrap_break_all(year_text, FONT, col_w, 2)  # 2行封顶（数学上够用）
        _draw_row(draw, col_left, 226, col_w, lines[0], FONT)
        if len(lines) > 1:
            _draw_row(draw, col_left, 250, col_w, lines[1], FONT)
        chore_y = 202
    _draw_row(draw, col_left, chore_y, col_w, f"家务 {chore_pct}%", FONT)


def render_eink_image(task, photo=None, chore_pct=0, year_pct=None):
    """task: {描述, 大区域, 小区域}；photo: PIL Image 或 None
    chore_pct: 家务完成百分比(0~100)；year_pct: 今年已过百分比，None=按实时日期计算"""
    if year_pct is None:
        year_pct = _year_elapsed_pct()
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    desc = extract_field_value(task, "具体区域描述").strip()
    big = extract_field_value(task, "大区域").strip()
    small = extract_field_value(task, "小区域").strip()
    if photo and photo.width < photo.height:
        print(f"版式: 竖图自适应（照片 {photo.width}×{photo.height} → 屏上 {round(photo.width*300/photo.height)}×300）")
        render_portrait(img, draw, desc, big, small, photo, chore_pct, year_pct)
    else:
        ratio = f"{photo.width}×{photo.height}" if photo else "无图"
        print(f"版式: 横图（{ratio}）")
        render_landscape(img, draw, desc, big, small, photo, chore_pct, year_pct)
    return img


# ============ funnycoo 相册推送 ============
def push_photo_to_funnycoo(img):
    """上传新图→成功后删旧图（相册只留一张）。失败只警告，不影响主流程。"""
    cfg = read_config_all()
    dev_id = read_config(cfg, "墨水屏设备ID")
    if not dev_id:
        print("⚠️ 配置表无「墨水屏设备ID」，跳过墨水屏推送")
        return False
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    # 1) 上传新图（成功前绝不动旧图；失败=屏幕继续显示旧图，安全降级）
    try:
        resp = requests.post(f"{FUNNYCOO_BASE}/api/upload-photo",
                             files={"file": ("today.png", buf, "image/png")},
                             data={"devId": dev_id}, timeout=30)
        data = resp.json()
        if not (resp.status_code == 200 and data.get("success")):
            print(f"⚠️ funnycoo 上传失败 HTTP {resp.status_code} {data}（屏幕继续显示旧图）")
            return False
        new_id = data["data"]["id"]
        print(f"🖼️ 已推送到墨水屏相册: {new_id}")
    except Exception as e:
        print(f"⚠️ funnycoo 上传异常: {e}（屏幕继续显示旧图）")
        return False
    # 2) 新图就位后，删掉相册里其余旧图（只留新图=固定显示；按ID比对，不猜列表顺序）
    try:
        lst = requests.get(f"{FUNNYCOO_BASE}/api/photo-list/{dev_id}", timeout=15).json()
        for p in lst.get("data", []):
            if p.get("id") and p.get("id") != new_id:
                requests.delete(f"{FUNNYCOO_BASE}/api/delete-photo/{dev_id}/{p['id']}", timeout=15)
                print(f"    🗑️ 已删除相册旧图: {p.get('name', p['id'])}")
    except Exception as e:
        print(f"⚠️ 清理旧图异常（新图不受影响，最多新旧图随机轮播）: {e}")
    return True


# ============ 家务进度（v7.2 全表轮次口径） ============
def global_chore_progress():
    """完成✔数 ÷ 有效任务总数。渲染前现查任务表（此时主流程写操作均已完成）：
    勾一个涨一档；一轮全勾 → 清空✔后查询 → 0% 开新轮。
    返回 (完成数, 总数, 百分比)"""
    records = list_records(TASK_TABLE_ID)
    valid = [r for r in records if is_valid_task(r["fields"])]
    n_total = len(valid)
    n_done = sum(1 for r in valid if r["fields"].get("完成"))
    pct = round(n_done * 100 / n_total) if n_total else 0
    print(f"本轮进度: {n_done}/{n_total} · {pct}%")
    return n_done, n_total, pct


# ============ 渲染预览（验收用） ============
def _make_sample_photo(rw, rh, label):
    """生成示例照片（灰底+标注），用于预览版式"""
    w, h = rw * 60, rh * 60
    ph = Image.new("RGB", (w, h), (228, 228, 228))
    d = ImageDraw.Draw(ph)
    d.rectangle([4, 4, w - 5, h - 5], outline=(180, 180, 180), width=3)
    f = _load_font(48)
    bbox = f.getbbox(label)
    d.text(((w - (bbox[2] - bbox[0])) / 2 - bbox[0], (h - (bbox[3] - bbox[1])) / 2 - bbox[1]),
           label, font=f, fill=(120, 120, 120))
    return ph


PREVIEW_SAMPLES = {
    "竖": ((9, 16), "吧台台面、咖啡机与杯架深度清洁消毒", "厨房", "吧台"),
    "竖短": ((3, 4), "擦拭吧台台面", "厨房", "吧台"),
    "竖方": ((14, 15), "吧台台面、咖啡机与杯架深度清洁消毒", "厨房", "吧台"),
    "横": ((4, 3), "擦电视柜玻璃面并除尘", "客厅", "电视柜"),
}


def run_preview(mode):
    if mode not in PREVIEW_SAMPLES:
        print(f"⚠️ 未知的预览值「{mode}」，可选：{'/'.join(PREVIEW_SAMPLES)}。已跳过预览。")
        return False
    (rw, rh), desc, big, small = PREVIEW_SAMPLES[mode]
    photo = _make_sample_photo(rw, rh, f"示例 {rw}:{rh}")
    img = render_eink_image({"具体区域描述": desc, "大区域": big, "小区域": small}, photo, 60, 67)
    os.makedirs("docs", exist_ok=True)
    img.save("docs/today.png")
    push_photo_to_funnycoo(img)
    return True


# ============ 主流程 ============
def is_valid_task(f):
    """有效任务判定：「小区域」「参考图片」「具体区域描述」任意一个有内容即算有效"""
    if f.get("参考图片"):  # 附件字段：非空列表即有效
        return True
    return bool(extract_field_value(f, "小区域").strip() or extract_field_value(f, "具体区域描述").strip())


def task_name(f):
    small = extract_field_value(f, "小区域")
    desc = extract_field_value(f, "具体区域描述")
    if small:
        return f"{small}（{desc}）" if desc else small
    return desc or "（见表格参考图片）"


def beijing_today():
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def main():
    args = set(sys.argv[1:])
    cfg = read_config_all()

    # ---- 渲染预览分支（优先级最高，不碰真实数据）----
    preview_mode = read_config(cfg, "渲染预览")
    if preview_mode and preview_mode != "否":
        print(f"===== 渲染预览模式：{preview_mode} =====")
        if run_preview(preview_mode):
            set_config(cfg, "渲染预览", "否")
            print("===== 预览完成，「渲染预览」已自动复位为「否」 =====")
            print("屏幕几秒后更新为预览样式（也可按设备按键立即刷新）")
            print("验收后：直接再跑一次 workflow 即可恢复正常显示")
        return

    # ---- 防重复保险 ----
    if "--reset" not in args and "--check-only" not in args:
        if read_config(cfg, "上次推送日期") == beijing_today():
            print("今天已推送过（防重复保险），仅刷新墨水屏图片")
            refresh_eink_only()
            return

    print("===== 拉取任务数据 =====")
    records = list_records(TASK_TABLE_ID, DEFAULT_VIEW_ID)
    valid = [r for r in records if is_valid_task(r["fields"])]
    print(f"共 {len(records)} 行，有效任务 {len(valid)} 条")

    updates = []
    # 序号原位重排
    seq_fields = {f["field_name"]: f for f in list_fields(TASK_TABLE_ID)}
    if "序号" in seq_fields and seq_fields["序号"].get("ui_type") != "AutoNumber":
        for i, r in enumerate(valid, 1):
            if r["fields"].get("序号") != i:
                updates.append((r["record_id"], {"序号": i}))
        if updates:
            print(f"序号重排 {len(updates)} 行")
    else:
        print("序号为自动编号或不存在，跳过重排")

    # 清理空记录残留「是否今日」（全量扫描）
    for r in records:
        if not is_valid_task(r["fields"]) and r["fields"].get("是否今日"):
            updates.append((r["record_id"], {"是否今日": False}))

    todo = [r for r in valid if r["fields"].get("是否今日")]
    done_today = [r for r in todo if r["fields"].get("完成")]

    # 完成记录
    if done_today:
        ensure_log_table_and_write(done_today)

    # 一轮判定提前：全部完成 → 清空「完成」→ 重新拉取（清空后 global_chore_progress 自然回到 0%）
    if todo and len(done_today) == len(todo):
        print("一轮全部完成，清空「完成」标记开启新一轮")
        clear_updates = [(r["record_id"], {"完成": False}) for r in valid if r["fields"].get("完成")]
        updates.extend(clear_updates)
        if updates:
            batch_update_records(TASK_TABLE_ID, updates)
        updates = []
        records = list_records(TASK_TABLE_ID, DEFAULT_VIEW_ID)
        valid = [r for r in records if is_valid_task(r["fields"])]
        todo = [r for r in valid if r["fields"].get("是否今日")]
        done_today = []

    # 移出已完成
    for r in done_today:
        updates.append((r["record_id"], {"是否今日": False}))
    todo = [r for r in todo if r not in done_today]

    # 待办超限清理（按序号）
    todo.sort(key=lambda r: r["fields"].get("序号") or 9999)
    if len(todo) > DAILY_COUNT:
        for r in todo[DAILY_COUNT:]:
            updates.append((r["record_id"], {"是否今日": False}))
        todo = todo[:DAILY_COUNT]

    # 智能补充：从延续任务最后位置按行序往后取，跳过今日和已完成
    lack = DAILY_COUNT - len(todo)
    if lack > 0:
        last_seq = max((r["fields"].get("序号") or 0) for r in todo) if todo else 0
        todo_seqs = {r["fields"].get("序号") for r in todo}
        picked = []
        for r in valid:
            seq = r["fields"].get("序号") or 0
            if r["fields"].get("完成") or seq in todo_seqs or seq <= (last_seq if todo else 0):
                continue
            picked.append(r)
            if len(picked) == lack:
                break
        for r in picked:
            updates.append((r["record_id"], {"是否今日": True}))
            todo.append(r)
        print(f"补充 {len(picked)} 条")
    elif lack == 0 and not args.intersection({"--reset"}):
        pass

    if updates:
        batch_update_records(TASK_TABLE_ID, updates)

    # 按视图顺序排列最终待办
    today_view_id = find_view_id_by_name(TASK_TABLE_ID, TODAY_VIEW_NAME)
    if today_view_id:
        ordered = [r["record_id"] for r in list_records(TASK_TABLE_ID, today_view_id)]
        todo.sort(key=lambda r: ordered.index(r["record_id"]) if r["record_id"] in ordered else 9999)
        print(f"按「{TODAY_VIEW_NAME}」视图顺序排列")
    else:
        print("未找到今日视图，回退按序号排序（已排过）")

    if "--check-only" in args and not picked_any(todo, done_today, lack):
        print("check-only：无变化，静默退出")
        return

    # ---- 渲染 + 推墨水屏（进度在渲染前按全表现查，v7.2 口径）----
    refresh_eink_display(todo)

    # ---- 飞书推送 ----
    if todo:
        lines = [f"{i+1}. {task_name(r['fields'])}" for i, r in enumerate(todo)]
        send_text_message("今日家务任务：\n" + "\n".join(lines))
        set_config(cfg, "上次推送日期", beijing_today())
        print(f"✅ 已推送 {len(todo)} 条任务")
    else:
        print("今日无待办，未发推送")


def picked_any(todo, done_today, lack):
    return lack > 0 or done_today


def refresh_eink_only():
    """防重复触发时：只刷新墨水屏图，不发消息。
    v7.2：进度与主流程同口径（global_chore_progress 现查全表），无需反查补丁"""
    records = list_records(TASK_TABLE_ID, DEFAULT_VIEW_ID)
    todo = [r for r in records if is_valid_task(r["fields"]) and r["fields"].get("是否今日")]
    today_view_id = find_view_id_by_name(TASK_TABLE_ID, TODAY_VIEW_NAME)
    if today_view_id:
        ordered = [r["record_id"] for r in list_records(TASK_TABLE_ID, today_view_id)]
        todo.sort(key=lambda r: ordered.index(r["record_id"]) if r["record_id"] in ordered else 9999)
    refresh_eink_display(todo)


def refresh_eink_display(todo):
    _, _, chore_pct = global_chore_progress()
    target = None
    for r in todo:
        if not r["fields"].get("完成"):
            target = r
            break
    if not target and todo:
        target = todo[0]
    if not target:
        img = render_eink_image({"具体区域描述": "（无任务）", "大区域": "", "小区域": ""}, None, chore_pct)
    else:
        photo = fetch_photo(target["fields"])
        img = render_eink_image(target["fields"], photo, chore_pct)
    os.makedirs("docs", exist_ok=True)
    img.save("docs/today.png")
    push_photo_to_funnycoo(img)


def _find_table_id_by_name(name):
    result = api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables/search", json_body={})
    if result.get("code") == 0:
        for t in result.get("data", {}).get("items", []):
            if t.get("name") == name:
                return t.get("table_id")
    return None


def ensure_log_table_and_write(done_records):
    log_table_id = _find_table_id_by_name(LOG_TABLE_NAME)
    if not log_table_id:
        r = api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables",
                        json_body={"table": {"name": LOG_TABLE_NAME, "default_view_name": "记录",
                                             "fields": [
                                                 {"field_name": "完成日期", "type": 5},
                                                 {"field_name": "大区域", "type": 3},
                                                 {"field_name": "小区域", "type": 3},
                                                 {"field_name": "具体区域描述", "type": 1},
                                             ]}})
        if r.get("code") != 0:
            print(f"⚠️ 创建完成记录表失败: {r.get('msg')}")
            return
        log_table_id = r["data"]["table_id"]
    midnight = int(datetime.now(BEIJING_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    new_records = []
    for r in done_records:
        f = r["fields"]
        big = f.get("大区域")
        small = f.get("小区域")
        if isinstance(big, list):
            big = big[0] if big else ""
        if isinstance(small, list):
            small = small[0] if small else ""
        new_records.append({"fields": {
            "完成日期": midnight,
            "大区域": str(big or ""),
            "小区域": str(small or ""),
            "具体区域描述": extract_field_value(f, "具体区域描述"),
        }})
    if new_records:
        api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables/{log_table_id}/records/batch_create",
                    json_body={"records": new_records})
        print(f"已写入 {len(new_records)} 条完成记录")


if __name__ == "__main__":
    main()
