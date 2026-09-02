#!/usr/bin/env python3
"""
每日家务任务推送脚本（每天5条滑动窗口版，纯飞书 OpenAPI）

逻辑：
- 每天保持最多5条"是否今日"=true的待办任务
- 智能补充：今天完成几条，明天就从后面补几条，没完成就原样保留
  例：完成1条剩2345 → 明天补6变成23456；全完成 → 明天推678910
- 补充时跳过「完成」=true的任务（已完成的这一轮不再回来）
- 一轮完成判定提前：所有任务都完成时，先清空全部「完成」标记开启新一轮，再补充
- 序号原位重排：不移动任何条目的位置，只按当前行顺序把序号改写成1,2,3...
  （注意：「序号」字段必须是「数字」类型；若是「自动编号」则API无法写入，脚本会提示并跳过）
- 完成记录：检测到打钩完成的任务时，自动写入「完成记录」表（表不存在则自动创建）
- 支持 --check-only 高频检查模式（无新补充时静默退出）
- 支持 --reset 重置模式：清空所有任务的「是否今日」和「完成」标记，
  然后自动从头补充第1~5条（本地调试用，GitHub 上不需要）
- 防重复保险：推送成功后会把当天日期写入配置表「上次推送日期」，
  同一天内再次触发（无论来自哪个闹钟）都直接退出，防止重复推送；
  如需当天强制重推，把配置表里的日期改乱再运行即可
- 自动清理空记录上残留的"是否今日"标记
- 有效任务判定：「小区域」「参考图片」「具体区域描述」任意一个有内容即算有效
  （仅填「大区域」不算；「序号」「是否今日」「完成」由脚本写入，不参与判定）
- 推送显示名依次取：小区域 → 具体区域描述 → （见表格参考图片）
- 所有批量写操作走飞书批量接口（单包最多450条，自动分包），速度从
  "每行1~3秒"降到"全部1~3秒"；批量被拒时自动降级逐行更新，定位问题行
- 墨水屏图片（阶段一新增）：推送流程结束后，把今日任务渲染成 400x300 PNG
  存到 docs/today.png，由 GitHub Actions 提交进仓库、经 GitHub Pages 对外展示；
  "今天已推送过"的早退分支也会刷新图片（方便白天手动测试）；
  图片生成失败只打警告，绝不影响飞书推送

环境变量（必填）：
  LARK_APP_ID      飞书自建应用的 App ID
  LARK_APP_SECRET  飞书自建应用的 App Secret

环境变量（可选，有默认值）：
  LARK_BASE_TOKEN          多维表格 token
  LARK_TASK_TABLE_ID       家务任务表 ID
  LARK_CONFIG_TABLE_ID     系统配置表 ID
  LARK_DEFAULT_VIEW_ID     默认视图 ID
  LARK_USER_OPEN_ID        推送目标用户的 open_id
  CHORE_DAILY_COUNT        每天待办数量，默认5
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

# ============ 配置 ============
BASE_TOKEN = os.environ.get("LARK_BASE_TOKEN", "GIgLbeJDUadS17sreyNcX7jknoe")
TASK_TABLE_ID = os.environ.get("LARK_TASK_TABLE_ID", "tblOo1DKyKgs0CV4")
CONFIG_TABLE_ID = os.environ.get("LARK_CONFIG_TABLE_ID", "tbl5WaTKn591sLJ6")
DEFAULT_VIEW_ID = os.environ.get("LARK_DEFAULT_VIEW_ID", "vewATu0DaX")
USER_OPEN_ID = os.environ.get("LARK_USER_OPEN_ID", "ou_487b71f46f00d88bbaf1862a0ee1639d")
DAILY_COUNT = int(os.environ.get("CHORE_DAILY_COUNT", "5"))

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
API_BASE = "https://open.feishu.cn/open-apis"
BEIJING_TZ = timezone(timedelta(hours=8))
LOG_TABLE_NAME = "完成记录"


# ============ 飞书 API 基础封装 ============
_tenant_token = None
_token_expire = 0


def get_tenant_token():
    """获取 tenant_access_token，带缓存"""
    global _tenant_token, _token_expire
    if _tenant_token and time.time() < _token_expire - 60:
        return _tenant_token
    if not APP_ID or not APP_SECRET:
        print("错误：未设置 LARK_APP_ID / LARK_APP_SECRET 环境变量")
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
    """统一的飞书 API 请求，失败时打印详细信息"""
    token = get_tenant_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    url = f"{API_BASE}{path}"
    resp = requests.request(
        method, url, headers=headers, params=params, json=json_body, timeout=30
    )
    try:
        result = resp.json()
    except Exception:
        print(f"❌ API 返回非 JSON [{method} {path}]")
        print(f"   HTTP状态码: {resp.status_code}")
        print(f"   请求体: {json.dumps(json_body, ensure_ascii=False)[:300] if json_body else '无'}")
        print(f"   响应前500字: {resp.text[:500]}")
        return {"code": -1, "msg": "非JSON响应"}
    if result.get("code") != 0:
        print(f"❌ API错误 [{method} {path}]")
        print(f"   code: {result.get('code')}, msg: {result.get('msg')}")
        print(f"   请求体: {json.dumps(json_body, ensure_ascii=False)[:300] if json_body else '无'}")
    return result


# ============ 记录操作 ============
def list_records(table_id, view_id=None, page_size=200):
    """列出记录，自动分页，返回 [{record_id, fields}, ...]"""
    all_items = []
    page_token = None
    while True:
        params = {"page_size": page_size}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        result = api_request(
            "GET",
            f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records",
            params=params,
        )
        if result.get("code") != 0:
            break
        data = result.get("data", {})
        all_items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return [{"record_id": item["record_id"], "fields": item.get("fields", {})}
            for item in all_items]


def update_record(table_id, record_id, fields):
    """更新记录的字段"""
    return api_request(
        "PUT",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}",
        json_body={"fields": fields},
    )


def batch_update_records(table_id, updates):
    """
    批量更新记录：updates = [(record_id, fields), ...]
    - 单包最多450条，自动分包，一次请求搞定全部（避免逐行跨洋请求）
    - 若整包被飞书拒收（某行数据有问题），自动降级为逐行更新，
      好行照常改，问题行单独报出来，不会被拖垮
    """
    if not updates:
        return
    CHUNK = 450
    for start in range(0, len(updates), CHUNK):
        chunk = updates[start:start + CHUNK]
        body = {"records": [{"record_id": rid, "fields": f} for rid, f in chunk]}
        result = api_request(
            "POST",
            f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_update",
            json_body=body,
        )
        if result.get("code") == 0:
            continue
        print(f"⚠️ 批量更新被拒（{len(chunk)} 条），降级为逐行更新以定位问题行")
        for rid, f in chunk:
            r = update_record(table_id, rid, f)
            if r.get("code") != 0:
                print(f"   ❌ 问题行 record_id={rid}，已跳过，其余行不受影响")


def list_fields(table_id):
    """列出表的所有字段"""
    result = api_request(
        "GET",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
        params={"page_size": 100},
    )
    if result.get("code") == 0:
        return result.get("data", {}).get("items", [])
    return []


def send_text_message(open_id, text):
    """给用户发送文本消息"""
    content = json.dumps({"text": text}, ensure_ascii=False)
    result = api_request(
        "POST",
        "/im/v1/messages",
        params={"receive_id_type": "open_id"},
        json_body={"receive_id": open_id, "msg_type": "text", "content": content},
    )
    return result.get("code") == 0


# ============ 业务逻辑 ============
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


def get_seq(t):
    """安全获取序号，返回int"""
    val = extract_field_value(t["fields"], "序号")
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def is_valid_task(fields):
    """
    有效任务判定：「小区域」「参考图片」「具体区域描述」任意一个有内容即算有效。
    注意：「序号」「是否今日」「完成」由脚本自己写入，绝不可参与判定，
    否则脚本写过的行会永远被当成有效任务。
    """
    for name in ("小区域", "参考图片", "具体区域描述"):
        val = fields.get(name)
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        if isinstance(val, list) and not val:
            continue
        return True
    return False


def get_task_display_name(fields):
    """任务显示名：优先小区域，其次具体区域描述，都没有则提示看参考图片"""
    small = extract_field_value(fields, "小区域")
    if small:
        return small
    desc = extract_field_value(fields, "具体区域描述")
    if desc:
        return desc
    return "（见表格参考图片）"


def get_all_tasks_in_order():
    """通过默认视图获取所有任务，返回顺序 = 表格视觉行顺序"""
    return list_records(TASK_TABLE_ID, view_id=DEFAULT_VIEW_ID)


def get_config():
    """读取系统配置表，返回 {配置项: 值}"""
    records = list_records(CONFIG_TABLE_ID)
    config = {}
    for item in records:
        key = extract_field_value(item["fields"], "配置项")
        val = extract_field_value(item["fields"], "值")
        config[str(key)] = str(val)
    return config


def update_config(key, value):
    """更新系统配置表中的某一项"""
    records = list_records(CONFIG_TABLE_ID)
    for item in records:
        k = extract_field_value(item["fields"], "配置项")
        if str(k) == key:
            update_record(CONFIG_TABLE_ID, item["record_id"], {"值": str(value)})
            return True
    return False


def set_config(key, value):
    """更新配置项；配置表里还没有这一项时，自动创建该行"""
    records = list_records(CONFIG_TABLE_ID)
    for item in records:
        k = extract_field_value(item["fields"], "配置项")
        if str(k) == key:
            update_record(CONFIG_TABLE_ID, item["record_id"], {"值": str(value)})
            return True
    result = api_request(
        "POST",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{CONFIG_TABLE_ID}/records",
        json_body={"fields": {"配置项": key, "值": str(value)}},
    )
    return result.get("code") == 0


def update_task(record_id, fields):
    """更新家务任务记录"""
    return update_record(TASK_TABLE_ID, record_id, fields)


def reset_all_flags(tasks):
    """
    重置模式：清空所有记录（含空记录）的「是否今日」和「完成」标记。
    之后主流程会自动从头补充第1~DAILY_COUNT条。
    """
    updates = []
    for t in tasks:
        u = {}
        if extract_field_value(t["fields"], "是否今日") is True:
            u["是否今日"] = False
        if extract_field_value(t["fields"], "完成") is True:
            u["完成"] = False
        if u:
            updates.append((t["record_id"], u))
    batch_update_records(TASK_TABLE_ID, updates)
    print(f"🔄 重置完成：清除了 {len(updates)} 条记录的标记")


def renumber_by_row_order(tasks):
    """
    序号原位重排：不移动任何条目的位置，
    只按当前行顺序（默认视图顺序）把序号改写成 1,2,3...
    空记录不占序号。
    """
    # 先检查「序号」字段类型：自动编号字段 API 无法写入
    seq_field = None
    for f in list_fields(TASK_TABLE_ID):
        if f.get("field_name") == "序号":
            seq_field = f
            break
    if seq_field and seq_field.get("ui_type") == "AutoNumber":
        print("⚠️ 「序号」是【自动编号】字段，飞书不允许通过 API 修改，已跳过重排")
        print("   如需序号自动重排，请在多维表格中把「序号」字段类型改为【数字】")
        return tasks

    updates = []
    changes = []
    for i, t in enumerate(tasks):
        expected_seq = i + 1
        current_seq = get_seq(t)
        if current_seq != expected_seq:
            updates.append((t["record_id"], {"序号": expected_seq}))
            changes.append(f"{current_seq or '空'}→{expected_seq}")
    if updates:
        batch_update_records(TASK_TABLE_ID, updates)
        print(f"序号原位重排完成：批量更新了 {len(updates)} 条（条目位置不变）")
        print(f"   变化明细: {', '.join(changes)}")
    else:
        print("序号已是连续排列，无需调整")
    return tasks


def get_or_create_log_table():
    """查找「完成记录」表，不存在则自动创建，返回 table_id"""
    result = api_request(
        "GET",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables",
        params={"page_size": 100},
    )
    if result.get("code") == 0:
        for tb in result.get("data", {}).get("items", []):
            if tb.get("name") == LOG_TABLE_NAME:
                return tb.get("table_id")

    # 表不存在，自动创建（字段：完成日期/大区域/小区域/具体区域描述）
    body = {
        "table": {
            "name": LOG_TABLE_NAME,
            "default_view_name": "全部记录",
            "fields": [
                {"field_name": "完成日期", "type": 5, "property": {"date_formatter": "yyyy/MM/dd"}},
                {"field_name": "大区域", "type": 1},
                {"field_name": "小区域", "type": 1},
                {"field_name": "具体区域描述", "type": 1},
            ],
        }
    }
    result = api_request("POST", f"/bitable/v1/apps/{BASE_TOKEN}/tables", json_body=body)
    if result.get("code") == 0:
        table_id = result.get("data", {}).get("table_id")
        print(f"📝 已自动创建「{LOG_TABLE_NAME}」表: {table_id}")
        return table_id
    return None


def log_completed_tasks(done_tasks):
    """把本次检测到完成的任务写入「完成记录」表"""
    if not done_tasks:
        return
    table_id = get_or_create_log_table()
    if not table_id:
        print("⚠️ 完成记录表不可用，跳过记录")
        return
    now = datetime.now(BEIJING_TZ)
    day_start = datetime(now.year, now.month, now.day, tzinfo=BEIJING_TZ)
    date_ts = int(day_start.timestamp() * 1000)  # 日期字段用毫秒时间戳
    records = []
    for t in done_tasks:
        small = extract_field_value(t["fields"], "小区域")
        desc = extract_field_value(t["fields"], "具体区域描述")
        if not small and not desc:
            small = "（见表格参考图片）"  # 只贴了图片的任务，记录里留个说明
        records.append({
            "fields": {
                "完成日期": date_ts,
                "大区域": extract_field_value(t["fields"], "大区域"),
                "小区域": small,
                "具体区域描述": desc,
            }
        })
    result = api_request(
        "POST",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_create",
        json_body={"records": records},
    )
    if result.get("code") == 0:
        print(f"📝 已写入 {len(records)} 条完成记录（{now.strftime('%Y-%m-%d')}）")
    else:
        print("⚠️ 完成记录写入失败")


def send_message(task_info):
    """组装并发送今日任务消息（支持多条滑动窗口）"""
    total_today = task_info.get("total_today", 0)
    new_count = task_info.get("new_count", 0)
    remaining_count = task_info.get("remaining_count", 0)
    tasks = task_info.get("tasks", [])
    new_task_ids = task_info.get("new_task_ids", set())

    lines = [
        "🧹 今日家务任务",
        "━━━━━━━━━━━━━━━",
        f"📋 共 {total_today} 条待办（新增 {new_count} 条，延续 {remaining_count} 条）",
        "",
    ]
    new_tasks = [t for t in tasks if t["record_id"] in new_task_ids]
    if new_tasks:
        lines.append("🆕 新增：")
        for t in new_tasks:
            area = extract_field_value(t["fields"], "大区域")
            name = get_task_display_name(t["fields"])
            lines.append(f"  • [{area}] {name}")
        lines.append("")

    remaining_tasks = [t for t in tasks if t["record_id"] not in new_task_ids]
    if remaining_tasks:
        lines.append("🔄 延续（昨日未完成）：")
        for t in remaining_tasks:
            area = extract_field_value(t["fields"], "大区域")
            name = get_task_display_name(t["fields"])
            lines.append(f"  • [{area}] {name}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━")
    lines.append("完成后请在多维表格中勾选「完成」✅")
    lines.append(f"多维表格链接：https://my.feishu.cn/base/{BASE_TOKEN}")

    message = "\n".join(lines)
    return send_text_message(USER_OPEN_ID, message)


# ============ 墨水屏图片渲染（阶段一新增） ============
# 每天把今日任务画成一张 400x300 的 PNG，存到仓库 docs/today.png，
# 由 GitHub Actions 提交进仓库，再通过 GitHub Pages 对公网开放；
# 墨水屏（中转模式）定时来抓这张图显示。
# 渲染失败只打警告，绝不影响飞书推送主流程。

SCREEN_W, SCREEN_H = 400, 300
EINK_OUTPUT_DIR = "docs"
EINK_OUTPUT_FILE = os.path.join(EINK_OUTPUT_DIR, "today.png")


def _find_font():
    """找中文字体文件（按优先级依次尝试）"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "NotoSansCJKsc-Regular.otf"),
        os.path.join(script_dir, "NotoSansSC-Regular.otf"),
        os.path.join(script_dir, "assets", "NotoSansCJKsc-Regular.otf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _truncate_text(draw, text, font, max_width):
    """按像素宽度截断文字，超长以…结尾"""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def render_today_image(today_tasks):
    """把今日任务渲染成 400x300 PNG，写到 docs/today.png"""
    from PIL import Image, ImageDraw, ImageFont  # 用到才导入，没装 Pillow 不影响推送

    font_path = _find_font()
    if not font_path:
        print("⚠️ 找不到中文字体文件，跳过墨水屏图片生成（不影响飞书推送）")
        print("   请把 NotoSansCJKsc-Regular.otf 上传到仓库根目录后重试")
        return

    # .ttc 是字体集合文件，index=2 取简体中文子字体；普通 .otf 用 index=0
    index = 2 if font_path.endswith(".ttc") else 0

    img = Image.new("RGB", (SCREEN_W, SCREEN_H), "white")
    draw = ImageDraw.Draw(img)

    f_title = ImageFont.truetype(font_path, 28, index=index)
    f_task = ImageFont.truetype(font_path, 22, index=index)
    f_small = ImageFont.truetype(font_path, 15, index=index)

    RED = (190, 30, 30)
    BLACK = (30, 30, 30)
    GRAY = (140, 140, 140)

    # 顶部红色标题条
    draw.rectangle([0, 0, SCREEN_W, 48], fill=RED)
    now = datetime.now(BEIJING_TZ)
    draw.text((14, 9), f"今日家务 {now.month}月{now.day}日", font=f_title, fill="white")

    # 任务列表
    y = 66
    if not today_tasks:
        draw.text((14, y), "今天没有待办任务，好好休息！", font=f_task, fill=BLACK)
    else:
        for i, t in enumerate(today_tasks, 1):
            area = extract_field_value(t["fields"], "大区域")
            name = get_task_display_name(t["fields"])
            line = f"{i}. [{area}] {name}" if area else f"{i}. {name}"
            line = _truncate_text(draw, line, f_task, SCREEN_W - 28)
            draw.text((14, y), line, font=f_task, fill=BLACK)
            y += 38

    # 底部时间戳角标：专门用来排查"趣联服务器是否缓存了旧图"
    draw.text((14, SCREEN_H - 26), "更新于 " + now.strftime("%Y-%m-%d %H:%M"),
              font=f_small, fill=GRAY)

    os.makedirs(EINK_OUTPUT_DIR, exist_ok=True)
    img.save(EINK_OUTPUT_FILE, "PNG")
    print(f"🖼️ 已生成墨水屏图片: {EINK_OUTPUT_FILE}（共 {len(today_tasks)} 条任务）")


def main():
    check_only = "--check-only" in sys.argv
    reset_mode = "--reset" in sys.argv
    mode = "高频检查" if check_only else "每日推送"
    if reset_mode:
        mode += "+重置"
    print(f"=== 每日家务任务推送（{mode}模式，每天{DAILY_COUNT}条滑动窗口）===")

    # 0. 防重复保险：今天已推送过就直接退出
    #    （cron-job.org 主闹钟 + GitHub schedule 备用闹钟可能同一天都触发；
    #    --reset 和 --check-only 模式不受此限制）
    #    阶段一新增：就算跳过推送，也顺手刷新一次墨水屏图片，
    #    以表格当前状态为准 —— 这样白天手动测试也能拿到图
    today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    if not check_only and not reset_mode:
        config = get_config()
        if config.get("上次推送日期") == today_str:
            print(f"✅ 今天（{today_str}）已经推送过了，本次跳过推送")
            print("   如需强制重推：把配置表「上次推送日期」的值改成别的，再重新运行")
            try:
                tasks_now = get_all_tasks_in_order()
                today_now = [t for t in tasks_now
                             if is_valid_task(t["fields"])
                             and extract_field_value(t["fields"], "是否今日") is True]
                today_now.sort(key=get_seq)
                render_today_image(today_now)
            except Exception as e:
                print(f"⚠️ 墨水屏图片生成失败: {e}")
            return

    # 1. 获取所有任务（按默认视图行顺序），过滤空记录
    tasks = get_all_tasks_in_order()
    total = len(tasks)
    valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]
    print(f"当前任务总数: {total}，有效任务: {len(valid_tasks)}")
    if len(valid_tasks) == 0:
        print("没有有效任务，无法推送")
        return

    # 2. 重置模式：清空所有标记，从头开始（之后会自动补第1~5条）
    if reset_mode:
        reset_all_flags(tasks)
        tasks = get_all_tasks_in_order()
        valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]

    # 3. 每日推送模式：序号原位重排（不移动条目位置，只对有效记录编序号）
    if not check_only:
        renumber_by_row_order(valid_tasks)
        tasks = get_all_tasks_in_order()
        valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]

    # 4. 清理残留 + 统计当前待办
    #    注意：必须在【全部记录】里找「是否今日」=true，
    #    否则空记录上的残留标记永远无法清除，会导致「今日任务」视图多出行
    valid_ids = {t["record_id"] for t in valid_tasks}
    all_today = [t for t in tasks if extract_field_value(t["fields"], "是否今日") is True]
    stale_today = [t for t in all_today if t["record_id"] not in valid_ids]
    if stale_today:
        print(f"⚠️ 发现 {len(stale_today)} 条空记录残留「是否今日」标记，自动清理")
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"是否今日": False}) for t in stale_today]
        )
    today_tasks = [t for t in all_today if t["record_id"] in valid_ids]
    print(f"当前待办数: {len(today_tasks)}")

    # 5. 如果待办超过DAILY_COUNT条，清理多余的（历史残留）
    if len(today_tasks) > DAILY_COUNT:
        print(f"⚠️ 待办数超过{DAILY_COUNT}条，清理多余的")
        today_tasks.sort(key=get_seq)
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"是否今日": False}) for t in today_tasks[DAILY_COUNT:]],
        )
        today_tasks = today_tasks[:DAILY_COUNT]

    # 6. 统计已完成的，移出待办（智能补充：完成几条就补几条）
    done_tasks = [t for t in today_tasks if extract_field_value(t["fields"], "完成") is True]
    remaining_tasks = [t for t in today_tasks if extract_field_value(t["fields"], "完成") is not True]
    print(f"已完成: {len(done_tasks)}条，延续: {len(remaining_tasks)}条")

    # 6.1 把完成的任务写入「完成记录」表
    log_completed_tasks(done_tasks)
    if done_tasks:
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"是否今日": False}) for t in done_tasks]
        )

    # 7. 一轮完成判定【提前到补充之前】：
    #    所有有效任务都完成 → 清空全部「完成」标记，开启新一轮
    if all(extract_field_value(t["fields"], "完成") is True for t in valid_tasks):
        print("🎉 完成一轮循环，清空所有任务的完成状态，开启新一轮")
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"完成": False}) for t in valid_tasks]
        )
        # 重新拉取，确保后续补充逻辑读到的是清空后的状态
        tasks = get_all_tasks_in_order()
        valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]

    # 8. 计算需要补充多少条（未完成的一条不补，原样保留到明天）
    need_to_add = DAILY_COUNT - len(remaining_tasks)
    if need_to_add < 0:
        need_to_add = 0
    print(f"需要补充: {need_to_add}条")

    # 9. 按行顺序补充新任务
    #    规则：从延续任务的最后位置往后取，跳过两类：
    #    - 当前待办（today_ids）
    #    - 已完成（「完成」=true）的任务——已完成的这一轮不再回来，防止空转
    new_tasks = []
    if need_to_add > 0:
        today_ids = {t["record_id"] for t in today_tasks}
        # 找到延续任务在 valid_tasks 中的最大行位置
        remaining_ids = {t["record_id"] for t in remaining_tasks}
        if remaining_ids:
            max_index = max(i for i, t in enumerate(valid_tasks) if t["record_id"] in remaining_ids)
        elif today_tasks:
            max_index = max(i for i, t in enumerate(valid_tasks) if t["record_id"] in today_ids)
        else:
            max_index = -1

        candidates = []
        n = len(valid_tasks)
        for offset in range(1, n + 1):
            idx = (max_index + offset) % n
            t = valid_tasks[idx]
            if t["record_id"] in today_ids:
                continue
            if extract_field_value(t["fields"], "完成") is True:
                continue  # 跳过已完成
            candidates.append(t)
            if len(candidates) >= need_to_add:
                break
        new_tasks = candidates[:need_to_add]
        batch_update_records(
            TASK_TABLE_ID,
            [(t["record_id"], {"是否今日": True}) for t in new_tasks]
        )
        print(f"已补充: {len(new_tasks)}条")
        if len(new_tasks) < need_to_add:
            print(f"⚠️ 未完成的候选任务不足，本次只补了 {len(new_tasks)} 条")

    # 10. 重新获取当前待办，发送消息
    tasks = get_all_tasks_in_order()
    valid_tasks = [t for t in tasks if is_valid_task(t["fields"])]
    final_today = [t for t in valid_tasks if extract_field_value(t["fields"], "是否今日") is True]
    final_today.sort(key=get_seq)
    task_info = {
        "total_today": len(final_today),
        "new_count": len(new_tasks),
        "remaining_count": len(remaining_tasks),
        "tasks": final_today,
        "new_task_ids": {t["record_id"] for t in new_tasks},
    }
    print(f"最终待办: {len(final_today)}条（新增{len(new_tasks)}条，延续{len(remaining_tasks)}条）")

    # 10.1 【阶段一新增】渲染墨水屏图片（失败不影响推送）
    try:
        render_today_image(final_today)
    except Exception as e:
        print(f"⚠️ 墨水屏图片生成失败（不影响飞书推送）: {e}")

    # 高频检查模式：如果没有新补充的任务，静默退出
    if check_only and len(new_tasks) == 0:
        print("高频检查：无新补充任务，静默退出")
        return

    success = send_message(task_info)
    print(f"消息推送: {'成功' if success else '失败'}")
    if success and not check_only:
        if set_config("上次推送日期", today_str):
            print(f"📌 已记录推送日期: {today_str}（今天再触发将自动跳过）")
        else:
            print("⚠️ 推送日期写入配置表失败（不影响本次推送）")
    print("=== 执行完成 ===")


if __name__ == "__main__":
    main()
