#!/usr/bin/env python3
"""
每日家务任务推送脚本（纯飞书 OpenAPI 版，不依赖 lark-cli）

适合部署到 GitHub Actions / 云服务器等任意 Python 环境。

环境变量（必填）：
  LARK_APP_ID      飞书自建应用的 App ID
  LARK_APP_SECRET  飞书自建应用的 App Secret

环境变量（可选，有默认值）：
  LARK_BASE_TOKEN     多维表格 token
  LARK_TASK_TABLE_ID  家务任务表 ID
  LARK_CONFIG_TABLE_ID 系统配置表 ID
  LARK_DEFAULT_VIEW_ID 默认视图 ID
  LARK_AREA_FIELD_ID  大区域字段 ID
  LARK_USER_OPEN_ID   推送目标用户的 open_id

用法：
  python daily_chore.py              # 每日完整推送（按区域重排 + 发消息）
  python daily_chore.py --check-only # 高频检查（仅任务完成时推进并发消息）
"""
import json
import os
import sys
import time
import requests

# ============ 配置 ============
BASE_TOKEN = os.environ.get("LARK_BASE_TOKEN", "GIgLbeJDUadS17sreyNcX7jknoe")
TASK_TABLE_ID = os.environ.get("LARK_TASK_TABLE_ID", "tblOo1DKyKgs0CV4")
CONFIG_TABLE_ID = os.environ.get("LARK_CONFIG_TABLE_ID", "tbl5WaTKn591sLJ6")
DEFAULT_VIEW_ID = os.environ.get("LARK_DEFAULT_VIEW_ID", "vewATu0DaX")
AREA_FIELD_ID = os.environ.get("LARK_AREA_FIELD_ID", "fldYh8CiMt")
USER_OPEN_ID = os.environ.get("LARK_USER_OPEN_ID", "ou_487b71f46f00d88bbaf1862a0ee1639d")

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")

API_BASE = "https://open.feishu.cn/open-apis"

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
    """统一的飞书 API 请求"""
    token = get_tenant_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    url = f"{API_BASE}{path}"
    resp = requests.request(
        method, url, headers=headers, params=params, json=json_body, timeout=30
    )
    result = resp.json()
    if result.get("code") != 0:
        print(f"API 错误 {method} {path}: {result.get('msg')} (code={result.get('code')})")
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
            f"/base/v1/bases/{BASE_TOKEN}/tables/{table_id}/records",
            params=params,
        )
        if result.get("code") != 0:
            break
        data = result.get("data", {})
        all_items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    # 统一格式
    return [{"record_id": item["record_id"], "fields": item.get("fields", {})} for item in all_items]


def update_record(table_id, record_id, fields):
    """更新记录的字段"""
    return api_request(
        "PUT",
        f"/base/v1/bases/{BASE_TOKEN}/tables/{table_id}/records/{record_id}",
        json_body={"fields": fields},
    )


def get_field(table_id, field_id):
    """获取字段详情（含单选选项）"""
    result = api_request(
        "GET",
        f"/base/v1/bases/{BASE_TOKEN}/tables/{table_id}/fields/{field_id}",
    )
    if result.get("code") == 0:
        return result.get("data", {}).get("field", {})
    return {}


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


def update_task(record_id, fields):
    """更新家务任务记录"""
    return update_record(TASK_TABLE_ID, record_id, fields)


def find_current_task(tasks):
    """
    定位当前任务：
    1. 优先找"是否今日"=true 的记录
    2. 找不到时用配置表 record_id 兜底
    3. 还找不到就取第一条
    """
    for t in tasks:
        if extract_field_value(t["fields"], "是否今日") is True:
            return t, "is_today_flag"

    config = get_config()
    fallback_rid = config.get("当前任务ID", "")
    if fallback_rid:
        for t in tasks:
            if t["record_id"] == fallback_rid:
                return t, "config_fallback"

    if tasks:
        return tasks[0], "first_task"
    return None, "no_tasks"


def get_area_order():
    """动态读取「大区域」单选字段的选项顺序"""
    field = get_field(TASK_TABLE_ID, AREA_FIELD_ID)
    options = field.get("options", [])
    return [opt.get("name", "") for opt in options if opt.get("name")]


def reorganize_and_renumber(tasks):
    """
    按「大区域」单选字段的选项顺序重新排列任务，并重排序号为 1,2,3...
    - 相同区域聚在一起，区域顺序跟随单选字段的选项顺序
    - 区域内保持原有相对顺序（稳定排序）
    - 不在选项列表中的区域排到最后
    """
    area_order = get_area_order()
    area_priority = {name: i for i, name in enumerate(area_order)}
    print(f"读取到区域选项顺序: {' → '.join(area_order) if area_order else '(获取失败)'}")

    def sort_key(t):
        area = extract_field_value(t["fields"], "大区域")
        priority = area_priority.get(area, len(area_order) + 1)
        return (priority, area)

    tasks.sort(key=sort_key)

    updated = 0
    for i, t in enumerate(tasks):
        expected_seq = i + 1
        current_seq = t["fields"].get("序号")
        if current_seq != expected_seq:
            update_task(t["record_id"], {"序号": expected_seq})
            updated += 1

    if updated > 0:
        print(f"按区域重排并更新序号：调整了 {updated} 条")
    else:
        print("序号已是按区域排列，无需调整")
    return tasks


def send_message(task_info):
    """组装并发送今日任务消息"""
    pos = task_info.get("position", "")
    total = task_info.get("total", "")
    area = task_info.get("area", "")
    small_area = task_info.get("small_area", "")
    method = task_info.get("method", "")
    has_image = task_info.get("has_image", False)

    lines = [
        "🧹 今日家务任务",
        "━━━━━━━━━━━━━━━",
        f"📋 第 {pos}/{total} 项",
        f"📍 区域：{area}",
        f"🎯 内容：{small_area}",
    ]
    if method:
        lines.append(f"💡 方法：{method}")
    if has_image:
        lines.append("🖼️  参考图片：请在多维表格「今日任务」视图查看")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append("完成后请在多维表格中勾选「完成」✅")
    lines.append(f"多维表格链接：https://my.feishu.cn/base/{BASE_TOKEN}")

    message = "\n".join(lines)
    return send_text_message(USER_OPEN_ID, message)


def main():
    check_only = "--check-only" in sys.argv
    mode = "高频检查" if check_only else "每日推送"
    print(f"=== 每日家务任务推送（{mode}模式）===")

    # 1. 获取所有任务
    tasks = get_all_tasks_in_order()
    total = len(tasks)
    print(f"当前任务总数: {total}")

    if total == 0:
        print("任务表为空，无法推送")
        return

    # 每日推送模式：按区域重排并更新序号
    if not check_only:
        tasks = reorganize_and_renumber(tasks)
        tasks = get_all_tasks_in_order()  # 重排后重新获取

    # 2. 定位当前任务
    current_task, locate_method = find_current_task(tasks)
    if not current_task:
        print("无法定位当前任务")
        return

    current_idx = tasks.index(current_task)
    current_fields = current_task["fields"]
    current_record_id = current_task["record_id"]
    is_done = extract_field_value(current_fields, "完成") is True
    print(f"当前任务: 位置[{current_idx+1}/{total}] 定位方式:{locate_method} 已完成:{is_done}")

    # 3. 高频检查模式：如果未完成，静默退出
    if check_only and not is_done:
        print("当前任务未完成，静默退出（不推送）")
        return

    # 4. 判断是否推进
    today_task = current_task
    if is_done:
        next_idx = (current_idx + 1) % total
        is_new_round = (next_idx == 0)

        if is_new_round:
            print("完成一轮循环，清空所有任务的完成状态")
            for t in tasks:
                if extract_field_value(t["fields"], "完成") is True:
                    update_task(t["record_id"], {"完成": False})

        update_task(current_record_id, {"是否今日": False, "完成": False})

        today_task = tasks[next_idx]
        update_task(today_task["record_id"], {"是否今日": True})
        update_config("当前任务ID", today_task["record_id"])
        print(f"推进到位置[{next_idx+1}/{total}]")
    else:
        print("当前任务未完成，保持不变")
        if locate_method != "is_today_flag":
            update_task(current_record_id, {"是否今日": True})
            update_config("当前任务ID", current_record_id)

    # 5. 组装并发送消息
    today_fields = today_task["fields"]
    today_idx = tasks.index(today_task)
    task_info = {
        "position": today_idx + 1,
        "total": total,
        "area": extract_field_value(today_fields, "大区域"),
        "small_area": extract_field_value(today_fields, "小区域") or "(未填写内容)",
        "method": extract_field_value(today_fields, "清洗方法"),
        "has_image": bool(today_fields.get("参考图片")),
    }
    print(f"今日任务: 位置[{task_info['position']}/{total}] [{task_info['area']}] {task_info['small_area'][:30]}")

    success = send_message(task_info)
    print(f"消息推送: {'成功' if success else '失败'}")
    print("=== 执行完成 ===")


if __name__ == "__main__":
    main()
