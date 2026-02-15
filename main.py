import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List

# --- 假设这些是从 astrbot.api 或类似模块导入的 ---
# 请根据实际 AstrBot 文档替换正确的导入路径
from astrbot.api import Star, MessageResult # type: ignore
from astrbot.api.interfaces import EventFilter, MessageType # type: ignore
# ---


class NijiDiarySync(Star):
    """
    用于同步虹彩日记 (Niji Diary) 数据到本地 SQLite 数据库的 AstrBot 插件。
    继承 Star 类以完全集成到框架中。
    """

    def __init__(self, context: Dict[str, Any], runtime_context: Dict[str, Any]):
        """
        初始化插件。
        Args:
            context: 插件上下文，通常包含配置等信息。
            runtime_context: 运行时上下文，可能包含机器人实例等。
        """
        super().__init__()
        self.logger = logging.getLogger("plugin.NijiDiarySync")
        
        # --- 配置加载 ---
        config = context.get("config", {})
        self.db_path = config.get("db_path", "./niji_diary.db")
        self.command_prefixes = config.get("command_prefixes", ["!"])
        self.allowed_user_ids = set(config.get("allowed_user_ids", []))
        self.debounce_time = config.get("debounce_time", 5) # 防抖动时间，单位秒
        self.diary_file_path = config.get("diary_file_path", "") # 虹彩日记文件路径
        if not self.diary_file_path:
             self.logger.warning("警告: 未在配置中找到 'diary_file_path'。同步功能将不可用。")

        # --- 状态管理 ---
        self.last_run_times: Dict[str, float] = {} # 记录每个用户的最后运行时间，用于防抖动
        self.is_syncing = False # 全局同步锁，防止并发执行

        # --- 数据库初始化 ---
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._create_table_if_not_exists()
            self.logger.info(f"数据库已连接: {self.db_path}")
        except Exception as e:
            self.logger.error(f"初始化数据库失败: {e}")
            raise

        # --- 事件过滤器设置 ---
        # 创建一个过滤器，匹配文本类型的消息
        self.text_message_filter = EventFilter().types(MessageType.TEXT)

        # 注册命令处理器
        # 注意: API 可能是 register_command 或类似名称，请根据 AstrBot 文档调整
        try:
            # 假设 AstrBot 支持通过 self.register_cmd 方式注册
            # 如果不支持，请移除或修改此部分，并在 on_message 中手动处理命令
            self.register_cmd(
                name="sync_diary",
                description="同步虹彩日记数据",
                handler=self._handle_sync_command,
                filters=[self.text_message_filter]
            )
            self.logger.info("命令处理器 'sync_diary' 已注册。")
        except AttributeError:
            # 如果框架不直接支持 register_cmd，我们就在 on_message 中处理
            self.logger.warning("AstrBot 框架似乎不支持 register_cmd，将在 on_message 中处理命令。")
        except Exception as e:
            self.logger.error(f"注册命令处理器失败: {e}")

    def _create_table_if_not_exists(self):
        """创建日记条目表（如果不存在）。"""
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS diary_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                hash TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()

    def _get_last_modified_time(self, file_path: str) -> float:
        """获取文件的最后修改时间戳。"""
        return os.path.getmtime(file_path)

    def _calculate_hash(self, data: str) -> str:
        """计算字符串的 SHA256 哈希值。"""
        return hashlib.sha256(data.encode()).hexdigest()

    def _read_diary_file(self) -> List[Dict[str, Any]]:
        """读取并解析 JSON 格式的日记文件。"""
        if not self.diary_file_path or not os.path.exists(self.diary_file_path):
            self.logger.error(f"日记文件不存在: {self.diary_file_path}")
            return []
            
        try:
            with open(self.diary_file_path, 'r', encoding='utf-8') as f:
                raw_data = f.read()
                
            # 尝试解析为 JSON
            data = json.loads(raw_data)
            
            # 假设日记数据结构是一个列表，其中包含日期和内容
            # 例如: [{"date": "2023-10-27", "content": "..."}, ...]
            if isinstance(data, list):
                return data
            else:
                self.logger.error("日记文件格式错误：根元素不是数组。")
                return []
                
        except json.JSONDecodeError as e:
            self.logger.error(f"解析日记文件 JSON 失败: {e}")
            return []
        except Exception as e:
            self.logger.error(f"读取日记文件失败: {e}")
            return []

    def _upsert_entry(self, entry_data: Dict[str, Any]):
        """更新或插入一条日记记录到数据库。"""
        cursor = self.conn.cursor()
        content = entry_data.get("content", "")
        date_str = entry_data.get("date", "")

        if not content or not date_str:
            self.logger.warning(f"跳过无效条目，缺少 content 或 date: {entry_data}")
            return

        new_hash = self._calculate_hash(content)

        # 检查是否已存在该日期的记录
        cursor.execute("SELECT hash FROM diary_entries WHERE date = ?", (date_str,))
        row = cursor.fetchone()

        if row:
            existing_hash = row[0]
            if existing_hash != new_hash:
                # 内容已更改，更新记录
                cursor.execute('''
                    UPDATE diary_entries 
                    SET content = ?, hash = ?, last_updated = CURRENT_TIMESTAMP 
                    WHERE date = ?
                ''', (content, new_hash, date_str))
                self.conn.commit()
                self.logger.info(f"更新了日记条目: {date_str}")
            else:
                # 内容未变，不作操作
                pass
        else:
            # 新增记录
            cursor.execute('''
                INSERT INTO diary_entries (date, content, hash) 
                VALUES (?, ?, ?)
            ''', (date_str, content, new_hash))
            self.conn.commit()
            self.logger.info(f"新增了日记条目: {date_str}")

    def _sync_diary_logic(self):
        """核心同步逻辑。"""
        if not self.diary_file_path:
            self.logger.error("无法同步：未配置 'diary_file_path'。")
            return

        start_time = datetime.now()
        self.logger.info("开始同步虹彩日记...")

        try:
            entries = self._read_diary_file()
            if not entries:
                 self.logger.warning("未能从文件中读取到有效日记条目。")
                 return

            for entry in entries:
                self._upsert_entry(entry)

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            self.logger.info(f"日记同步完成。耗时: {duration:.2f} 秒。")

        except Exception as e:
            self.logger.error(f"同步过程中发生错误: {e}")

    def _handle_sync_command(self, message_result: MessageResult):
        """
        处理 !sync_diary 命令的回调函数。
        此函数仅在 AstrBot 支持 register_cmd 时被调用。
        """
        user_id = message_result.message_event.sender.user_id
        
        # 权限检查
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            message_result.reply("权限不足，无法执行此操作。")
            return

        # 防抖动检查
        current_time = time.time() # type: ignore
        last_time = self.last_run_times.get(user_id, 0)
        if current_time - last_time < self.debounce_time:
            remaining_time = self.debounce_time - (current_time - last_time)
            message_result.reply(f"操作过于频繁，请等待 {remaining_time:.1f} 秒后再试。")
            return

        # 并发控制
        if self.is_syncing:
            message_result.reply("同步已在进行中，请稍候...")
            return

        self.is_syncing = True
        self.last_run_times[user_id] = current_time

        try:
            self._sync_diary_logic()
            message_result.reply("日记同步完成！")
        finally:
            self.is_syncing = False


    def on_message(self, message_result: MessageResult):
        """
        AstrBot 框架的主要消息入口点。
        如果框架不支持 register_cmd，则在此处处理命令。
        同时可以处理其他非命令逻辑。
        """
        # 应用过滤器，只处理文本消息
        if not self.text_message_filter.match(message_result.message_event):
            return # 不匹配，忽略

        # 1. 尝试处理命令（如果框架未自动处理）
        msg_content = message_result.message_event.raw_message.strip()
        
        # 查找命令前缀
        command_found = False
        for prefix in self.command_prefixes:
            if msg_content.startswith(prefix):
                command_found = True
                full_command = msg_content[len(prefix):].split(maxsplit=1)[0]
                
                if full_command == "sync_diary":
                    user_id = message_result.message_event.sender.user_id
                    
                    # 权限检查
                    if self.allowed_user_ids and user_id not in self.allowed_user_ids:
                        message_result.reply("权限不足，无法执行此操作。")
                        return

                    # 防抖动检查
                    import time # 导入放在这里，避免顶层导入问题
                    current_time = time.time()
                    last_time = self.last_run_times.get(user_id, 0)
                    if current_time - last_time < self.debounce_time:
                        remaining_time = self.debounce_time - (current_time - last_time)
                        message_result.reply(f"操作过于频繁，请等待 {remaining_time:.1f} 秒后再试。")
                        return

                    # 并发控制
                    if self.is_syncing:
                        message_result.reply("同步已在进行中，请稍候...")
                        return

                    self.is_syncing = True
                    self.last_run_times[user_id] = current_time

                    try:
                        self._sync_diary_logic()
                        message_result.reply("日记同步完成！")
                    finally:
                        self.is_syncing = False
                
                break # 找到匹配的前缀，不再继续循环

        # 2. 处理其他非命令逻辑（如果有的话）
        # if not command_found:
        #     # 例如，可以监听特定关键词触发同步等
        #     pass


    def terminate(self):
        """
        插件卸载或机器人关闭时调用此方法。
        用于清理资源，如关闭数据库连接。
        """
        self.logger.info("正在终止 NijiDiarySync 插件...")
        if hasattr(self, 'conn') and self.conn:
            try:
                self.conn.close()
                self.logger.info("数据库连接已关闭。")
            except Exception as e:
                self.logger.error(f"关闭数据库连接时出错: {e}")
        self.logger.info("NijiDiarySync 插件终止完成。")

# --- 插件元信息 ---
# plugin_name = "NijiDiarySync"
# plugin_version = "1.0.0"
# plugin_author = "YourName"
# plugin_description = "同步虹彩日记 (Niji Diary) 到本地 SQLite 数据库。"
# --- ---