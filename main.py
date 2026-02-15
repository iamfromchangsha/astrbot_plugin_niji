from __future__ import annotations
import asyncio
import aiohttp
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# 尝试导入 StarTools（如果可用）
try:
    from astrbot.api.star import StarTools
    HAS_STARTOOLS = True
except ImportError:
    HAS_STARTOOLS = False


# ---------- 工具函数 ----------
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


def _compare_date_str(date_str1: str, date_str2: str, format_str: str = "%Y-%m-%d") -> Optional[int]:
    """比较两个日期字符串。返回 1, 0, -1。如果任一日期无效，则返回 None."""
    try:
        date1 = datetime.strptime(date_str1, format_str).date()
        date2 = datetime.strptime(date_str2, format_str).date()
        if date1 > date2:
            return 1
        elif date1 < date2:
            return -1
        else:
            return 0
    except ValueError:
        return None


# ---------- 用户数据类 ----------
@dataclass
class NijiUser:
    """存储日记用户凭据和状态"""
    token: str
    user_id: str
    last_uploaded_date: str = ""  # 格式: YYYY-MM-DD

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


# ---------- 主插件类 ----------
@register("NijiDiarySync", "日记同步助手", "自动将聊天记录同步到你的日记网站。", "1.3.0", "https://github.com/your-name/astrbot_plugin_niji")
class NijiDiarySync(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.session: Optional[aiohttp.ClientSession] = None

        # 运行时数据
        self._niji_users: Dict[str, NijiUser] = {}

        # 数据文件路径
        if HAS_STARTOOLS:
            data_dir_path = StarTools.get_data_dir()
            self._data_dir = str(data_dir_path)
            os.makedirs(self._data_dir, exist_ok=True)
        else:
            root = os.getcwd()
            self._data_dir = _ensure_dir(os.path.join(root, "data", "plugin_data", "astrbot_plugin_niji"))

        self._data_file_path = os.path.join(self._data_dir, "niji_users.json")

        # 加载数据
        self._load_data()

        # 初始化 HTTP 会话
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
            },
            timeout=aiohttp.ClientTimeout(total=30)
        )
        # 启动后台任务
        self._upload_task = asyncio.create_task(self._periodic_upload_loop())

        logger.info("[NijiDiarySync] Plugin initialized.")
        # 提醒用户注意数据文件权限（敏感信息存储）
        logger.warning("[NijiDiarySync] User tokens are stored in plain text. Please ensure the data file permissions are secure (e.g., 600).")

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

    # ---------- 数据持久化 ----------
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

    # ---------- 后台循环 ----------
    async def _periodic_upload_loop(self):
        """后台循环，每天检查并上传"""
        while True:
            try:
                now = _now_beijing()
                # 计算到今天23:50的秒数
                target_time = now.replace(hour=23, minute=50, second=0, microsecond=0)
                if now >= target_time:
                    target_time += timedelta(days=1)
                sleep_seconds = (target_time - now).total_seconds()

                logger.info(f"[NijiDiarySync] Next upload check scheduled for {target_time.strftime('%Y-%m-%d %H:%M:%S')}, sleeping for {sleep_seconds}s...")
                await asyncio.sleep(sleep_seconds)

                # 执行上传任务
                await self._perform_daily_upload()
            except asyncio.CancelledError:
                logger.info("[NijiDiarySync] Upload loop was cancelled.")
                break
            except Exception as e:
                logger.error(f"[NijiDiarySync] Error in periodic upload loop: {e}")
                await asyncio.sleep(60)

    async def _perform_daily_upload(self):
        """执行每日上传逻辑（带并发限流）"""
        today_str = _fmt_date(_now_beijing())
        logger.info(f"[NijiDiarySync] Starting daily upload check for {today_str}...")

        # 限制并发数，避免触发网站限流
        semaphore = asyncio.Semaphore(3)
        tasks = []
        for user_id, niji_user in list(self._niji_users.items()):
            task = self._upload_for_single_user(niji_user, user_id, today_str, semaphore)
            tasks.append(task)

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    user_id = list(self._niji_users.keys())[i]
                    logger.error(f"[NijiDiarySync] Error uploading for user {user_id}: {result}")

    async def _upload_for_single_user(self, niji_user: NijiUser, astrbot_user_id: str, today_str: str, semaphore: asyncio.Semaphore):
        """为单个用户执行上传，使用信号量控制并发"""
        async with semaphore:
            try:
                if niji_user.last_uploaded_date != today_str:
                    logger.info(f"[NijiDiarySync] Preparing to upload log for {astrbot_user_id} (last upload: {niji_user.last_uploaded_date})")
                    full_history_text = await self._get_full_conversation_history(astrbot_user_id)
                    if not full_history_text.strip():
                        logger.info(f"[NijiDiarySync] No history to upload for {astrbot_user_id}, skipping.")
                        return
                    success = await self._upload_chat_log(niji_user, today_str, full_history_text)
                    if success:
                        niji_user.last_uploaded_date = today_str
                        self._save_data()
                        logger.info(f"[NijiDiarySync] Successfully uploaded history for {astrbot_user_id}")
                    else:
                        logger.error(f"[NijiDiarySync] Failed to upload history for {astrbot_user_id}")
            except Exception as e:
                logger.error(f"[NijiDiarySync] Error processing user {astrbot_user_id}: {e}")

    # ---------- 获取聊天历史 ----------
    async def _get_full_conversation_history(self, umo: str) -> str:
        """安全获取完整上下文历史"""
        contexts = await self._safe_get_full_contexts(umo)
        if not contexts:
            return ""

        history_lines = []
        for msg in contexts:
            role = msg.get("role", "unknown")
            content_obj = msg.get("content", "")
            content_text = self._extract_content_text({"content": content_obj})
            if content_text:
                display_role = "User" if role == "user" else "Bot" if role == "assistant" else role.capitalize()
                history_lines.append(f"[{display_role}] {content_text}")

        return "\n".join(history_lines)

    async def _safe_get_full_contexts(self, umo: str) -> List[Dict]:
        """安全获取完整上下文，使用多重降级策略确保稳定性"""
        contexts = await self._try_get_from_manager(umo)
        if contexts:
            logger.debug(f"[NijiDiarySync] ✅ Got {len(contexts)} messages")
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
        """简化历史获取逻辑，直接尝试各种方法"""
        if not conversation:
            return []

        # 尝试 get_messages 方法（异步优先）
        if hasattr(conversation, 'get_messages') and callable(conversation.get_messages):
            try:
                if asyncio.iscoroutinefunction(conversation.get_messages):
                    msgs = await conversation.get_messages()
                else:
                    msgs = conversation.get_messages()
                contexts = self._extract_contexts_from_data(msgs)
                if contexts:
                    return contexts
            except Exception as e:
                logger.debug(f"[NijiDiarySync] get_messages failed: {e}")

        # 尝试 messages 属性
        if hasattr(conversation, 'messages'):
            try:
                msgs = conversation.messages
                contexts = self._extract_contexts_from_data(msgs)
                if contexts:
                    return contexts
            except Exception as e:
                logger.debug(f"[NijiDiarySync] messages attribute failed: {e}")

        # 尝试 history 属性
        if hasattr(conversation, 'history'):
            try:
                msgs = conversation.history
                contexts = self._extract_contexts_from_data(msgs)
                if contexts:
                    return contexts
            except Exception as e:
                logger.debug(f"[NijiDiarySync] history attribute failed: {e}")

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

        if isinstance(msgs, dict) and "messages" in msgs:
            msgs = msgs["messages"]

        if not isinstance(msgs, list):
            return []

        normalized = []
        for msg in msgs:
            if not isinstance(msg, dict):
                continue

            role = msg.get("role") or msg.get("speaker") or msg.get("from")
            if role not in ("user", "assistant", "system"):
                continue

            content = self._extract_content_text(msg)

            if content:
                normalized.append({"role": role, "content": content.strip()})

        return normalized

    def _extract_content_text(self, msg: dict) -> str:
        """从消息中提取文本内容，兼容新旧格式"""
        raw_content = msg.get("content")

        if isinstance(raw_content, str):
            return raw_content

        if isinstance(raw_content, list):
            text_parts = []
            for part in raw_content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type", "")
                if part_type == "text" and part.get("text"):
                    text_parts.append(str(part["text"]))
            if text_parts:
                return " ".join(text_parts)

        fallback = msg.get("text") or msg.get("message") or ""
        if isinstance(fallback, str):
            return fallback

        return ""

    # ---------- 日记网站 API ----------
    async def _login(self, username: str, password: str) -> Tuple[Optional[str], Optional[str]]:
        """登录日记网站，返回 (token, user_id) 或 (None, None)"""
        url = "https://nijiweb.cn/api/login/"
        data = {"email": username, "password": password}
        try:
            async with self.session.post(url, data=data) as response:
                if response.status == 200:
                    token = response.cookies.get('token')
                    if token:
                        token_value = token.value
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
                    logger.error(f"[NijiDiarySync] Login failed with status {response.status}.")
                    return None, None
        except aiohttp.ClientError as e:
            logger.error(f"[NijiDiarySync] Login request failed: {e}")
            return None, None
        except Exception as e:
            logger.error(f"[NijiDiarySync] Unexpected error during login: {e}")
            return None, None

    async def _get_userid_and_diarycard(self, token: str) -> Tuple[Optional[str], list]:
        """获取用户ID和日记卡片列表（增加宽松正则）"""
        url = "https://nijiweb.cn/"
        headers = {"Cookie": f"token={token}"}
        try:
            async with self.session.get(url, headers=headers) as response:
                if response.status == 200:
                    html_content = await response.text()
                    # 更宽松的正则，允许空格
                    userid_match = re.search(r"setUserId\s*\(\s*(\d+)\s*\)", html_content)
                    user_id = userid_match.group(1) if userid_match else None
                    if not user_id:
                        logger.warning("[NijiDiarySync] Could not extract user ID from HTML, maybe the site structure changed.")

                    diary_pattern = r"addDiaryCard\s*\(\s*(\{[^}]*\})\s*\)"
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
                    logger.error(f"[NijiDiarySync] Get user info failed with status {response.status}.")
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
                    try:
                        json_resp = await response.json()
                        return json_resp
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
                        logger.error(f"[NijiDiarySync] Get diary response not JSON: {e}. Body: {await response.text()[:500]}")
                        return None
                else:
                    text = await response.text()
                    logger.error(f"[NijiDiarySync] Get diary failed with status {response.status}. Preview: {text[:200]}...")
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
            "id": diary_id or ""
        }
        try:
            async with self.session.post(url, headers=headers, data=data) as response:
                if response.status == 200:
                    try:
                        json_resp = await response.json()
                        success_status = json_resp.get("status") == "success"
                        logger.info(f"[NijiDiarySync] Post diary response JSON: {json_resp}")
                        return success_status
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
                        text = await response.text()
                        logger.error(f"[NijiDiarySync] Post diary response not JSON: {e}. Body: {text[:500]}")
                        return False
                else:
                    text = await response.text()
                    logger.error(f"[NijiDiarySync] Post diary failed with status {response.status}. Preview: {text[:200]}...")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"[NijiDiarySync] Post diary request failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[NijiDiarySync] Unexpected error posting diary: {e}")
            return False

    async def _upload_chat_log(self, niji_user: NijiUser, date_str: str, history_text: str) -> bool:
        """上传聊天记录到日记（始终创建新日记，避免追加）"""
        if not history_text.strip():
            logger.warning(f"[NijiDiarySync] No history text to upload for user {niji_user.user_id} on {date_str}")
            return True

        # 标题添加具体时间，避免同一天多篇日记覆盖
        now = _now_beijing()
        time_str = now.strftime("%H:%M")
        diary_title = f"Chat Log {date_str} {time_str}"

        logger.info(f"[NijiDiarySync] Creating new diary entry for {niji_user.user_id} on {date_str} at {time_str}")
        # 始终不传 diary_id，强制创建新日记
        return await self._post_diary(niji_user.token, diary_title, history_text, None, date_str)

    # ---------- 事件处理器 ----------
    @filter.command("login")
    async def _cmd_login(self, event: AstrMessageEvent):
        """处理 /login 命令"""
        text = (event.message_str or "").strip()
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /login <用户名> <密码>")
            return

        username = parts[1]
        password = parts[2]

        token, user_id = await self._login(username, password)
        if token and user_id:
            astrbot_user_id = event.unified_msg_origin
            self._niji_users[astrbot_user_id] = NijiUser(token=token, user_id=user_id)
            self._save_data()
            yield event.plain_result("✅ 日记账号绑定成功！聊天记录将自动同步。")
            logger.info(f"[NijiDiarySync] User {astrbot_user_id} logged in and bound to diary account {user_id}.")
        else:
            yield event.plain_result("❌ 登录失败，请检查用户名和密码。")

    @filter.command("sync")
    async def _cmd_sync(self, event: AstrMessageEvent):
        """手动触发同步，立即上传今日聊天记录"""
        astrbot_user_id = event.unified_msg_origin
        if astrbot_user_id not in self._niji_users:
            yield event.plain_result("❌ 你尚未绑定日记账号，请先使用 /login 命令绑定。")
            return

        niji_user = self._niji_users[astrbot_user_id]
        today_str = _fmt_date(_now_beijing())

        # 如果今天已上传，询问是否强制再传
        if niji_user.last_uploaded_date == today_str:
            yield event.plain_result("⚠️ 今天已经自动同步过，是否强制重新上传？(输入 /sync force 确认)")
            return

        # 执行上传
        yield event.plain_result("🔄 正在获取聊天记录并上传，请稍候...")
        full_history = await self._get_full_conversation_history(astrbot_user_id)
        if not full_history.strip():
            yield event.plain_result("📭 没有找到可上传的聊天记录。")
            return

        success = await self._upload_chat_log(niji_user, today_str, full_history)
        if success:
            niji_user.last_uploaded_date = today_str
            self._save_data()
            yield event.plain_result("✅ 手动同步成功！")
        else:
            yield event.plain_result("❌ 手动同步失败，请查看日志。")

    @filter.command("sync force")
    async def _cmd_sync_force(self, event: AstrMessageEvent):
        """强制重新上传今日聊天记录"""
        astrbot_user_id = event.unified_msg_origin
        if astrbot_user_id not in self._niji_users:
            yield event.plain_result("❌ 你尚未绑定日记账号。")
            return

        niji_user = self._niji_users[astrbot_user_id]
        today_str = _fmt_date(_now_beijing())

        yield event.plain_result("🔄 强制同步中...")
        full_history = await self._get_full_conversation_history(astrbot_user_id)
        if not full_history.strip():
            yield event.plain_result("📭 没有聊天记录可上传。")
            return

        success = await self._upload_chat_log(niji_user, today_str, full_history)
        if success:
            niji_user.last_uploaded_date = today_str
            self._save_data()
            yield event.plain_result("✅ 强制同步成功！")
        else:
            yield event.plain_result("❌ 强制同步失败。")