# main.py
from __future__ import annotations
import asyncio
import aiohttp
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

# 尝试导入 StarTools（如果可用）
try:
    from astrbot.api.star import StarTools
    HAS_STARTOOLS = True
except ImportError:
    HAS_STARTOOLS = False

# 尝试导入新的Message模型（新版本astrbot）
try:
    from astrbot.core.agent.message import (
        AssistantMessageSegment, UserMessageSegment, TextPart,
    )
    HAS_NEW_MESSAGE_API = True
except ImportError:
    HAS_NEW_MESSAGE_API = False

# 工具函数
def _ensure_dir(p: str) -> str:
    """确保目录存在，不存在则创建"""
    os.makedirs(p, exist_ok=True)
    return p

def _now_beijing() -> datetime:
    """获取北京时间"""
    beijing_tz = timezone(timedelta(hours=8))
    return datetime.now(beijing_tz)

def _fmt_date(dt: datetime) -> str:
    """格式化日期为 YYYY-MM-DD"""
    return dt.strftime("%Y-%m-%d")

def _compare_date_str(date_str1: str, date_str2: str, format_str: str = "%Y-%m-%d") -> int:
    """比较两个日期字符串"""
    try:
        date1 = datetime.strptime(date_str1, format_str).date()
        date2 = datetime.strptime(date_str2, format_str).date()
        if date1 > date2:
            return 1
        elif date1 < date2:
            return -1
        else:
            return 0
    except ValueError as e:
        logger.error(f"日期格式错误: {e}")
        return 0 # 错误时返回相等，避免逻辑中断

@dataclass
class NijiUser:
    """存储日记用户凭据和状态"""
    token: str
    user_id: str
    last_uploaded_date: str = "" # 格式: YYYY-MM-DD
    # 移除 chat_log，因为我们不再手动拼接，而是获取完整的上下文历史

    def to_dict(self):
        return {
            "token": self.token,
            "user_id": self.user_id,
            "last_uploaded_date": self.last_uploaded_date,
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            token=data.get("token", ""),
            user_id=data.get("user_id", ""),
            last_uploaded_date=data.get("last_uploaded_date", ""),
        )

# 主插件类
@register("NijiDiarySync", "日记同步助手", "自动将聊天记录同步到你的日记网站。", "1.1.0", "https://github.com/your-name/astrbot_plugin_niji")
class NijiDiarySync(Star):
    def __init__(self, context: Context): # 移除了 config 参数
        super().__init__(context)
        # self.cfg: AstrBotConfig = config # 移除这一行，因为我们不再接收 config
        self.session: Optional[aiohttp.ClientSession] = None

        # 运行时数据
        self._niji_users: Dict[str, NijiUser] = {}

        # 数据文件路径
        if HAS_STARTOOLS:
            data_dir_path = StarTools.get_data_dir() / "astrbot_plugin_niji"
            self._data_dir = str(data_dir_path)
            os.makedirs(self._data_dir, exist_ok=True)
        else:
            root = os.getcwd() # 使用当前工作目录作为根目录
            self._data_dir = _ensure_dir(os.path.join(root, "data", "plugin_data", "astrbot_plugin_niji"))

        self._data_file_path = os.path.join(self._data_dir, "niji_users.json")

        # 加载数据
        self._load_data()

        # 启动后台任务
        self._upload_task = asyncio.create_task(self._periodic_upload_loop())

        # 初始化 aiohttp session
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
            },
            timeout=aiohttp.ClientTimeout(total=30)
        )

        logger.info("[NijiDiarySync] Plugin initialized.")

    async def terminate(self):
        """插件销毁时关闭 aiohttp session 和任务"""
        if self._upload_task and not self._upload_task.done():
            self._upload_task.cancel()
            try:
                await self._upload_task
            except asyncio.CancelledError:
                pass

        if self.session:
            await self.session.close()
        
        self._save_data()
        logger.info("[NijiDiarySync] Plugin terminated.")

    def _load_data(self):
        """从文件加载用户数据"""
        if not os.path.exists(self._data_file_path):
            return
        try:
            with open(self._data_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for user_id_str, user_dict in data.items():
                self._niji_users[user_id_str] = NijiUser.from_dict(user_dict)
            logger.info(f"[NijiDiarySync] Loaded data for {len(self._niji_users)} users.")
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.error(f"[NijiDiarySync] Failed to load data: {e}")
        except (IOError, OSError) as e:
            logger.error(f"[NijiDiarySync] Failed to read data file: {e}")

    def _save_data(self):
        """保存用户数据到文件"""
        try:
            data = {uid: user.to_dict() for uid, user in self._niji_users.items()}
            with open(self._data_file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except (IOError, OSError) as e:
            logger.error(f"[NijiDiarySync] Failed to write data file: {e}")
        except (TypeError, ValueError) as e:
            logger.error(f"[NijiDiarySync] Failed to serialize data: {e}")

    async def _periodic_upload_loop(self):
        """后台循环，每天检查并上传"""
        while True:
            now = _now_beijing()
            # 计算到今天23:50的秒数
            target_time = now.replace(hour=23, minute=50, second=0, microsecond=0)
            if now >= target_time: # 如果已经过了今天的上传时间，就计算到明天的
                target_time += timedelta(days=1)
            sleep_seconds = (target_time - now).total_seconds()
            
            logger.info(f"[NijiDiarySync] Next upload check scheduled for {target_time.strftime('%Y-%m-%d %H:%M:%S')}, sleeping for {sleep_seconds}s...")
            await asyncio.sleep(sleep_seconds)
            
            # 执行上传任务
            await self._perform_daily_upload()

    async def _perform_daily_upload(self):
        """执行每日上传逻辑"""
        today_str = _fmt_date(_now_beijing())
        logger.info(f"[NijiDiarySync] Starting daily upload check for {today_str}...")
        
        for user_id, niji_user in list(self._niji_users.items()):
            if niji_user.last_uploaded_date != today_str:
                logger.info(f"[NijiDiarySync] Preparing to upload log for {user_id} (last upload: {niji_user.last_uploaded_date})")
                
                # 获取完整的对话历史
                full_history_text = await self._get_full_conversation_history(user_id)
                
                if not full_history_text.strip():
                    logger.info(f"[NijiDiarySync] No history to upload for {user_id}, skipping.")
                    niji_user.last_uploaded_date = today_str # 仍标记为已处理，避免重复检查空历史
                    continue
                
                success = await self._upload_chat_log(niji_user, today_str, full_history_text)
                if success:
                    niji_user.last_uploaded_date = today_str
                    self._save_data()
                    logger.info(f"[NijiDiarySync] Successfully uploaded history for {user_id}")
                else:
                    logger.error(f"[NijiDiarySync] Failed to upload history for {user_id}")

    async def _get_full_conversation_history(self, umo: str) -> str:
        """
        安全获取完整上下文历史，模仿 Conversa 插件中的 _safe_get_full_contexts 方法
        """
        contexts = await self._safe_get_full_contexts(umo)
        if not contexts:
            return ""

        history_lines = []
        for msg in contexts:
            role = msg.get("role", "unknown")
            content_obj = msg.get("content", "")
            
            # 提取内容文本，兼容 Conversa 中的 _extract_content_text 逻辑
            content_text = self._extract_content_text({"content": content_obj})
            
            if content_text:
                display_role = "User" if role == "user" else "Bot" if role == "assistant" else role.capitalize()
                history_lines.append(f"[{display_role}] {content_text}")
        
        return "\n".join(history_lines)

    # --- 以下方法直接复制自 Conversa 插件，保持一致 ---
    async def _safe_get_full_contexts(self, umo: str) -> List[Dict]:
        """安全获取完整上下文，使用多重降级策略确保稳定性"""
        contexts = []
        # 策略1：通过conversation_manager获取
        contexts = await self._try_get_from_manager(umo)
        if contexts:
            logger.debug(f"[NijiDiarySync] ✅ Strategy 1 success: Got {len(contexts)} messages")
            return contexts

        logger.warning(f"[NijiDiarySync] ⚠️ Could not fetch history for {umo}, returning empty.")
        return []

    async def _try_get_from_manager(self, umo: str) -> List[Dict]:
        """尝试通过conversation_manager获取历史"""
        try:
            if not hasattr(self.context, "conversation_manager"):
                return []

            conv_mgr = self.context.conversation_manager
            conversation_id = await conv_mgr.get_curr_conversation_id(umo)
            if not conversation_id:
                return []

            conversation = await conv_mgr.get_conversation(umo, conversation_id)
            return await self._try_get_from_conversation(conversation)
        except Exception as e:
            logger.debug(f"[NijiDiarySync] Manager fetch failed: {e}")
            return []

    async def _try_get_from_conversation(self, conversation) -> List[Dict]:
        """尝试从conversation对象获取历史"""
        if not conversation:
            return []

        # 尝试多种数据源
        sources = [
            ("messages", lambda: getattr(conversation, "messages", None)),
            ("get_messages", lambda: self._safe_call(getattr(conversation, "get_messages", None))),
            ("history", lambda: getattr(conversation, "history", None))
        ]

        for source_name, getter in sources:
            try:
                data = getter() # Conversa 中这里也用了同步调用
                if data:
                    contexts = self._extract_contexts_from_data(data)
                    if contexts:
                        logger.debug(f"[NijiDiarySync] Got {len(contexts)} messages from {source_name}")
                        return contexts
            except Exception as e:
                logger.debug(f"[NijiDiarySync] {source_name} fetch failed: {e}")

        return []

    def _extract_contexts_from_data(self, data) -> List[Dict]:
        """从各种数据格式中提取上下文"""
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return self._normalize_messages(parsed)
            except json.JSONDecodeError:
                return []
        elif isinstance(data, list):
            return self._normalize_messages(data)
        elif hasattr(data, '__iter__'):
            try:
                return self._normalize_messages(list(data))
            except Exception:
                return []
        return []

    def _normalize_messages(self, msgs) -> List[Dict]:
        """标准化消息格式，兼容多种数据源"""
        if not msgs:
            return []

        # 处理嵌套结构
        if isinstance(msgs, dict) and "messages" in msgs:
            msgs = msgs["messages"]

        if not isinstance(msgs, list):
            return []

        normalized = []
        for msg in msgs:
            if not isinstance(msg, dict):
                continue

            # 提取角色
            role = msg.get("role") or msg.get("speaker") or msg.get("from")
            if role not in ("user", "assistant", "system"):
                continue

            # 提取内容（兼容新旧格式）
            content = self._extract_content_text(msg)

            # 验证并添加
            if content:
                normalized.append({"role": role, "content": content.strip()})

        return normalized

    def _extract_content_text(self, msg: dict) -> str:
        """从消息中提取文本内容，兼容新旧格式"""
        raw_content = msg.get("content")

        # 情况1：字符串格式（旧格式，直接返回）
        if isinstance(raw_content, str):
            return raw_content

        # 情况2：列表格式（新格式 ContentPart 列表）
        if isinstance(raw_content, list):
            text_parts = []
            for part in raw_content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type", "")
                # 提取 TextPart 的文本
                if part_type == "text" and part.get("text"):
                    text_parts.append(str(part["text"]))
                # 也可以选择性提取 ThinkPart（思考内容），但通常不包含在对话历史中
                # elif part_type == "think" and part.get("think"):
                #     text_parts.append(f"[思考: {part['think']}]")

            if text_parts:
                return " ".join(text_parts)

        # 情况3：尝试其他备选字段（向后兼容）
        fallback = msg.get("text") or msg.get("message") or ""
        if isinstance(fallback, str):
            return fallback

        return ""

    async def _safe_call(self, func, *args, **kwargs):
        """安全调用可能是异步的函数"""
        try:
            if func and asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            elif func:
                return func(*args, **kwargs)
        except Exception:
            return None
    # --- Conversa 方法结束 ---

    async def _login(self, username: str, password: str) -> Tuple[Optional[str], Optional[str]]:
        """登录日记网站，返回 (token, user_id) 或 (None, None)"""
        url = "https://nijiweb.cn/api/login/"
        data = {
            "email": username,
            "password": password
        }
        try:
            async with self.session.post(url, data=data) as response:
                if response.status == 200:
                    # 尝试从响应 cookies 中获取 token
                    token = response.cookies.get('token')
                    if token:
                        token_value = token.value
                        # 登录成功后获取用户ID
                        user_id, _ = await self._get_userid_and_diarycard(token_value)
                        if user_id:
                            return token_value, user_id
                        else:
                            logger.error("[NijiDiarySync] Login successful but failed to get user ID.")
                            return None, None
                    else:
                        logger.error("[NijiDiarySync] Login successful but no token found in response.")
                        return None, None
                else:
                    text = await response.text()
                    logger.error(f"[NijiDiarySync] Login failed with status {response.status}: {text}")
                    return None, None
        except aiohttp.ClientError as e:
            logger.error(f"[NijiDiarySync] Login request failed: {e}")
            return None, None
        except Exception as e:
            logger.error(f"[NijiDiarySync] Unexpected error during login: {e}")
            return None, None

    async def _get_userid_and_diarycard(self, token: str) -> Tuple[Optional[str], list]:
        """获取用户ID和日记卡片列表"""
        url = "https://nijiweb.cn/"
        headers = {"Cookie": f"token={token}"}
        try:
            async with self.session.get(url, headers=headers) as response:
                if response.status == 200:
                    html_content = await response.text()
                    
                    # 提取用户ID
                    userid_match = re.search(r"setUserId\((\d+)\);", html_content)
                    user_id = userid_match.group(1) if userid_match else None
                    
                    # 提取日记卡片
                    diary_pattern = r"addDiaryCard\((\{[^}]*\})\);"
                    diary_matches = re.findall(diary_pattern, html_content)
                    diary_cards_data = []
                    for json_str in diary_matches:
                        clean_json_str = json_str.replace("'", '"')
                        try:
                            diary_data = json.loads(clean_json_str)
                            diary_cards_data.append(diary_data)
                        except json.JSONDecodeError as e:
                            logger.error(f"[NijiDiarySync] Single diary card JSON parse error: {e}, raw: {json_str}")
                            continue
                    return user_id, diary_cards_data
                else:
                    text = await response.text()
                    logger.error(f"[NijiDiarySync] Get user info failed with status {response.status}: {text}")
                    return None, []
        except aiohttp.ClientError as e:
            logger.error(f"[NijiDiarySync] Get user info request failed: {e}")
            return None, []
        except Exception as e:
            logger.error(f"[NijiDiarySync] Unexpected error getting user info: {e}")
            return None, []

    async def _get_diary(self, token: str, owner_id: str, diary_id: str, user_id: str) -> Optional[dict]:
        """获取指定日记内容"""
        url = "https://nijiweb.cn/api/"
        headers = {"Cookie": f"token={token}"}
        data = {
            "function": "getDiary",
            "ownerId": owner_id,
            "diaryId": diary_id,
            "userId": user_id
        }
        try:
            async with self.session.post(url, headers=headers, data=data) as response:
                if response.status == 200:
                    json_resp = await response.json()
                    return json_resp
                else:
                    text = await response.text()
                    logger.error(f"[NijiDiarySync] Get diary failed with status {response.status}: {text}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"[NijiDiarySync] Get diary request failed: {e}")
            return None
        except Exception as e:
            logger.error(f"[NijiDiarySync] Unexpected error getting diary: {e}")
            return None

    async def _post_diary(self, token: str, title: str, content: str, diary_id: Optional[str], date_text: str) -> bool:
        """发布或更新日记"""
        url = "https://nijiweb.cn/api/"
        headers = {"Cookie": f"token={token}"}
        data = {
            "function": "writeDiary",
            "dateText": date_text,
            "titleText": title,
            "contentText": content,
            "id": diary_id or "" # 传空字符串表示新建
        }
        try:
            async with self.session.post(url, headers=headers, data=data) as response:
                if response.status == 200:
                    text = await response.text()
                    logger.info(f"[NijiDiarySync] Post diary response: {text}")
                    # 根据 API 返回判断成功（这里假设返回 JSON 或包含 success 关键字）
                    # 请根据实际 API 响应调整判断逻辑
                    # 示例：如果返回 {"status": "success"} 或包含 "success"
                    try:
                        json_resp = await response.json()
                        return json_resp.get("status") == "success"
                    except json.JSONDecodeError:
                        # 如果不是 JSON，可以根据文本内容判断
                        return "success" in text.lower() or "ok" in text.lower()
                else:
                    text = await response.text()
                    logger.error(f"[NijiDiarySync] Post diary failed with status {response.status}: {text}")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"[NijiDiarySync] Post diary request failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[NijiDiarySync] Unexpected error posting diary: {e}")
            return False

    async def _upload_chat_log(self, niji_user: NijiUser, date_str: str, history_text: str) -> bool:
        """上传聊天记录到日记"""
        if not history_text.strip():
            logger.warning(f"[NijiDiarySync] No history text to upload for user {niji_user.user_id} on {date_str}")
            return True # 认为成功，因为没有内容需要上传

        # 1. 获取当天的日记ID（如果存在）
        _, diary_cards = await self._get_userid_and_diarycard(niji_user.token)
        existing_diary_id = None
        for diary_card in diary_cards:
            if str(diary_card.get("cardUserID")) == str(niji_user.user_id) and \
               str(_compare_date_str(date_str, diary_card.get("createdDate"))) == '0':
                existing_diary_id = diary_card.get("cardDiaryId")
                break

        new_content = history_text
        diary_title = f"Chat Log {date_str}"

        if existing_diary_id:
            # 更新现有日记
            logger.info(f"[NijiDiarySync] Updating existing diary {existing_diary_id} for {niji_user.user_id}")
            existing_diary_data = await self._get_diary(niji_user.token, niji_user.user_id, existing_diary_id, niji_user.user_id)
            if existing_diary_data and "content" in existing_diary_data:
                original_content = existing_diary_data["content"]
                new_content = original_content + "\n\n--- New Chat Log ---\n\n" + history_text
            else:
                logger.warning(f"[NijiDiarySync] Could not fetch existing diary content for update, using new log only.")
            return await self._post_diary(niji_user.token, diary_title, new_content, existing_diary_id, date_str)
        else:
            # 创建新日记
            logger.info(f"[NijiDiarySync] Creating new diary entry for {niji_user.user_id} on {date_str}")
            return await self._post_diary(niji_user.token, diary_title, new_content, None, date_str)

    # --- 事件处理器 ---

    @filter.command("login")
    async def _cmd_login(self, event: AstrMessageEvent):
        """处理 /login 命令"""
        text = (event.message_str or "").strip()
        parts = text.split(maxsplit=2) # /login username password
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /login <用户名> <密码>")
            return

        username = parts[1]
        password = parts[2]

        token, user_id = await self._login(username, password)
        if token and user_id:
            # 存储用户信息
            astrbot_user_id = event.unified_msg_origin
            self._niji_users[astrbot_user_id] = NijiUser(token=token, user_id=user_id)
            self._save_data()
            yield event.plain_result("✅ 日记账号绑定成功！聊天记录将自动同步。")
            logger.info(f"[NijiDiarySync] User {astrbot_user_id} logged in and bound to diary account {user_id}.")
        else:
            yield event.plain_result("❌ 登录失败，请检查用户名和密码。")

    # 注意：这里不再监听消息事件来累积日志，因为我们将获取完整的对话历史
    # @filter.event_message_type(filter.EventMessageType.MESSAGE)
    # async def _on_user_message(self, event: AstrMessageEvent):
    #     # 此方法不再需要，因为不再手动累积日志
    #     pass