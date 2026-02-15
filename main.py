import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List

# --- 重要：请根据你当前 AstrBot 版本的实际API调整导入 ---
# 从错误信息看，你的版本可能没有 'from astrbot.api import Star'
# 通常旧版或当前版的插件是直接定义一个函数或类，由框架动态加载
# 我们保留原始的简单函数式结构，并应用之前的修复逻辑 ---
# （注：如果你的版本确实需要继承某个类，请查阅该版本文档，但目前报错显示不需要）

# --- 插件配置 ---
# 你可以直接在这里硬编码，或者通过其他方式（如读取同目录下的 config.json）来加载。
DB_PATH = './niji_diary.db'
DIARY_FILE_PATH = '/home/pi/彩虹日记/日记.json' # 请修改为你实际的路径
COMMAND_PREFIXES = ['!']
ALLOWED_USER_IDS = {'123456789'} # 请修改为你自己的QQ号或其他允许的用户ID
DEBOUNCE_TIME = 5 # 防止用户频繁触发的冷却时间（秒）

# --- 全局变量 ---
conn = None
logger = logging.getLogger(__name__)
last_run_times = {} # 记录每个用户的最后运行时间，用于防抖动
is_syncing = False # 全局同步锁，防止并发执行

def init_db():
    global conn
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False) # 设置 check_same_thread=False 以支持多线程访问
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS diary_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                hash TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        logger.info(f"数据库已连接并初始化: {DB_PATH}")
    except Exception as e:
        logger.error(f"初始化数据库失败: {e}")
        raise # 如果初始化失败，阻止插件继续加载

def get_last_modified_time(file_path: str) -> float:
    return os.path.getmtime(file_path)

def calculate_hash(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()

def read_diary_file() -> List[Dict[str, Any]]:
    if not DIARY_FILE_PATH or not os.path.exists(DIARY_FILE_PATH):
        logger.error(f"日记文件不存在: {DIARY_FILE_PATH}")
        return []
        
    try:
        with open(DIARY_FILE_PATH, 'r', encoding='utf-8') as f:
            raw_data = f.read()
            
        data = json.loads(raw_data)
        
        if isinstance(data, list):
            return data
        else:
            logger.error("日记文件格式错误：根元素不是数组。")
            return []
            
    except json.JSONDecodeError as e:
        logger.error(f"解析日记文件 JSON 失败: {e}")
        return []
    except Exception as e:
        logger.error(f"读取日记文件失败: {e}")
        return []

def upsert_entry(entry_data: Dict[str, Any]):
    global conn
    if conn is None:
        logger.error("数据库未初始化，无法插入数据。")
        return
        
    cursor = conn.cursor()
    content = entry_data.get("content", "")
    date_str = entry_data.get("date", "")

    if not content or not date_str:
        logger.warning(f"跳过无效条目，缺少 content 或 date: {entry_data}")
        return

    new_hash = calculate_hash(content)

    cursor.execute("SELECT hash FROM diary_entries WHERE date = ?", (date_str,))
    row = cursor.fetchone()

    if row:
        existing_hash = row[0]
        if existing_hash != new_hash:
            cursor.execute('''
                UPDATE diary_entries 
                SET content = ?, hash = ?, last_updated = CURRENT_TIMESTAMP 
                WHERE date = ?
            ''', (content, new_hash, date_str))
            conn.commit()
            logger.info(f"更新了日记条目: {date_str}")
        else:
            logger.debug(f"日记条目 {date_str} 内容无变化，跳过。")
    else:
        cursor.execute('''
            INSERT INTO diary_entries (date, content, hash) 
            VALUES (?, ?, ?)
        ''', (date_str, content, new_hash))
        conn.commit()
        logger.info(f"新增了日记条目: {date_str}")

def sync_diary_logic():
    global is_syncing
    if not DIARY_FILE_PATH:
        logger.error("无法同步：未配置 'DIARY_FILE_PATH'。")
        return

    if is_syncing:
        logger.warning("同步已在进行中，本次请求被忽略。")
        return

    is_syncing = True
    start_time = datetime.now()
    logger.info("开始同步虹彩日记...")

    try:
        entries = read_diary_file()
        if not entries:
             logger.warning("未能从文件中读取到有效日记条目。")
             return

        for entry in entries:
            upsert_entry(entry)

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        logger.info(f"日记同步完成。耗时: {duration:.2f} 秒。")

    except Exception as e:
        logger.error(f"同步过程中发生错误: {e}")
    finally:
        is_syncing = False # 确保无论成功与否，锁都会被释放

def main_handler(prompt: str, message_metadata: Dict[str, Any]):
    """
    AstrBot 插件的主入口函数。
    Args:
        prompt (str): 用户发送的原始消息内容。
        message_metadata (Dict): 包含消息来源、发送者ID等信息的元数据字典。
    
    Returns:
        str: 返回给用户的响应消息，如果没有则返回 None。
    """
    global last_run_times, is_syncing
    
    user_id = str(message_metadata.get('sender', {}).get('user_id', '')) # 确保 user_id 是字符串
    group_id = message_metadata.get('group_id') # 如果在群里，会有 group_id

    # 1. 命令匹配
    cmd_found = False
    for prefix in COMMAND_PREFIXES:
        if prompt.startswith(prefix):
            full_cmd = prompt[len(prefix):].split()[0]
            if full_cmd == "sync_diary":
                cmd_found = True
                break

    if not cmd_found:
        return None # 消息不匹配任何命令，返回 None 让框架处理其他插件

    # 2. 权限检查
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return "权限不足，无法执行此操作。"

    # 3. 防抖动检查
    current_time = time.time()
    last_time = last_run_times.get(user_id, 0)
    if current_time - last_time < DEBOUNCE_TIME:
        remaining_time = DEBOUNCE_TIME - (current_time - last_time)
        return f"操作过于频繁，请等待 {remaining_time:.1f} 秒后再试。"

    # 4. 并发控制检查（在更新时间戳之前再次确认）
    if is_syncing:
        return "同步已在进行中，请稍候..."

    # 5. 更新时间戳，允许执行
    last_run_times[user_id] = current_time

    # 6. 执行核心同步逻辑
    try:
        sync_diary_logic()
        return "日记同步完成！"
    except Exception as e:
        logger.error(f"执行同步时发生错误: {e}")
        return f"同步失败: {e}"



try:
    init_db()
except Exception as e:
    # 如果初始化失败，打印错误日志，让框架捕获异常并报告加载失败
    logger.critical(f"插件初始化失败: {e}")
    # 抛出异常以使框架知晓加载失败
    raise
# --- ---