from __future__ import annotations
import asyncio
import aiohttp
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from astrbot.api import logger
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
@register("NijiDiarySync", "日记同步助手", "自动将聊天记录同步到你的日记网站。", "1.4.0", "https://github.com/your-name/astrbot_plugin_niji")
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
        """为单个用户执行上传（增量），使用信号量控制并发"""
        async with semaphore:
            try:
                # 如果已经上传过今天，跳过
                if niji_user.last_uploaded_date == today_str:
                    return

                logger.info(f"[NijiDiarySync] Preparing to upload log for {astrbot_user_id} (last upload: {niji_user.last_uploaded_date})")

                # 获取自上次上传日期以来的新消息
                since_date = niji_user.last_uploaded_date if niji_user.last_uploaded_date else None
                new_messages_text = await self._get_conversation_history_since(astrbot_user_id, since_date)

                if not new_messages_text.strip():
                    logger.info(f"[NijiDiarySync] No new messages for {astrbot_user_id} since {since_date or 'beginning'}, marking as uploaded.")
                    # 虽然没有新消息，但今天已检查过，更新最后上传日期，避免每天空跑
                    niji_user.last_uploaded_date = today_str
                    self._save_data()
                    return

                success = await self._upload_chat_log(niji_user, today_str, new_messages_text)
                if success:
                    niji_user.last_uploaded_date = today_str
                    self._save_data()
                    logger.info(f"[NijiDiarySync] Successfully uploaded new messages for {astrbot_user_id}")
                else:
                    logger.error(f"[NijiDiarySync] Failed to upload new messages for {astrbot_user_id}")
            except Exception as e:
                logger.error(f"[NijiDiarySync] Error processing user {astrbot_user_id}: {e}")

    # ---------- 获取聊天历史（带日期过滤）----------
    async def _get_conversation_history_since(self, umo: str, since_date: Optional[str] = None) -> str:
        """
        获取自指定日期以来的所有消息文本。
        since_date: 格式 "YYYY-MM-DD"，只返回该日期及之后的消息。如果为 None，返回全部消息。
        """
        contexts = await self._safe_get_full_contexts(umo)
        if not contexts:
            return ""

        since = None
        if since_date:
            try:
                since = datetime.strptime(since_date, "%Y-%m-%d").date()
            except ValueError:
                logger.warning(f"[NijiDiarySync] Invalid since_date format: {since_date}, will not filter.")
                since = None

        history_lines = []
        for msg in contexts:
            # 如果提供了 since 且消息有日期，则检查日期是否满足条件
            if since:
                msg_date = self._extract_message_date(msg)
                if msg_date and msg_date < since:
                    continue  # 早于 since 的消息跳过

            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content:
                display_role = "User" if role == "user" else "Bot" if role == "assistant" else role.capitalize()
                history_lines.append(f"[{display_role}] {content}")

        return "\n".join(history_lines)

    def _extract_message_date(self, msg: dict) -> Optional[datetime.date]:
        """
        从消息字典中提取日期。尝试常见的字段名：timestamp, created_at, time。
        支持时间戳（秒/毫秒）和 ISO 格式字符串。
        """
        # 优先使用规范化后存储的 timestamp 字段
        ts = msg.get("timestamp")
        if ts is None:
            return None

        # 如果是数字，视为时间戳（秒）
        if isinstance(ts, (int, float)):
            # 如果时间戳很大（毫秒级），转换为秒
            if ts > 1e11:  # 毫秒级时间戳（> 100 亿）
                ts = ts / 1000
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).date()
            except (OSError, OverflowError, ValueError):
                return None

        # 如果是字符串，尝试解析为日期时间
        if isinstance(ts, str):
            # 尝试常见格式
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S%z"):
                try:
                    dt = datetime.strptime(ts, fmt)
                    return dt.date()
                except ValueError:
                    continue
            # 如果都不是，尝试从字符串中提取日期部分（例如 "2025-02-16 10:30" -> "2025-02-16"）
            match = re.search(r"(\d{4}-\d{2}-\d{2})", ts)
            if match:
                try:
                    return datetime.strptime(match.group(1), "%Y-%m-%d").date()
                except ValueError:
                    pass
        return None

    # ---------- 获取原始聊天历史（核心逻辑，保持不变但保留时间戳）----------
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
        """标准化消息格式，兼容多种数据源，并保留时间戳"""
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
            if not content:
                continue

            new_msg = {
                "role": role,
                "content": content.strip()
            }

            # 保留时间戳字段（如果有）
            for ts_field in ("timestamp", "created_at", "time"):
                if ts_field in msg:
                    new_msg["timestamp"] = msg[ts_field]
                    break

            normalized.append(new_msg)

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
        """获取用户ID和日记卡片列表（解析HTML中的addDiaryCard调用）"""
        url = "https://nijiweb.cn/"
        headers = {"Cookie": f"token={token}"}
        try:
            async with self.session.get(url, headers=headers) as response:
                if response.status == 200:
                    html_content = await response.text()
                    # 提取用户ID
                    userid_match = re.search(r"setUserId\s*\(\s*(\d+)\s*\)", html_content)
                    user_id = userid_match.group(1) if userid_match else None
                    if not user_id:
                        logger.warning("[NijiDiarySync] Could not extract user ID from HTML, maybe the site structure changed.")

                    # 提取所有日记卡片
                    diary_pattern = r"addDiaryCard\s*\(\s*(\{[^}]*\})\s*\)"
                    diary_matches = re.findall(diary_pattern, html_content)
                    diary_cards = []
                    for json_str in diary_matches:
                        clean_json_str = json_str.replace("'", '"')
                        try:
                            card_data = json.loads(clean_json_str)
                            diary_cards.append(card_data)
                        except json.JSONDecodeError as e:
                            logger.error(f"[NijiDiarySync] Single diary card JSON parse error: {e}, raw: {json_str}")
                            continue
                    return user_id, diary_cards
                else:
                    logger.error(f"[NijiDiarySync] Get user info failed with status {response.status}.")
                    return None, []
        except aiohttp.ClientError as e:
            logger.error(f"[NijiDiarySync] Get user info request failed: {e}")
            return None, []
        except Exception as e:
            logger.error(f"[NijiDiarySync] Unexpected error getting user info: {e}")
            return None, []

    async def _find_diary_id_by_title(self, token: str, title: str) -> Optional[str]:
        """根据标题在日记卡片列表中查找对应的日记ID"""
        _, diary_cards = await self._get_userid_and_diarycard(token)
        for card in diary_cards:
            if card.get("title") == title:
                return card.get("id")
        return None

    async def _post_diary(self, token: str, title: str, content: str, diary_id: Optional[str], date_text: str) -> bool:
        """发布或更新日记（兼容错误的 Content-Type）"""
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
                    text = await response.text()
                    try:
                        json_resp = json.loads(text)
                    except json.JSONDecodeError as e:
                        logger.error(f"[NijiDiarySync] Post diary response is not valid JSON: {e}. Body: {text[:500]}")
                        return False

                    success_status = json_resp.get("status") == "success"
                    logger.info(f"[NijiDiarySync] Post diary response JSON: {json_resp}")
                    return success_status
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
        """
        上传聊天记录到日记（如果当天已有同名日记则更新，否则创建新日记）
        日记标题格式：Chat Log YYYY-MM-DD
        """
        if not history_text.strip():
            logger.warning(f"[NijiDiarySync] No history text to upload for user {niji_user.user_id} on {date_str}")
            return True

        diary_title = f"Chat Log {date_str}"
        # 查找当天是否已有日记
        diary_id = await self._find_diary_id_by_title(niji_user.token, diary_title)
        action = "Updating" if diary_id else "Creating"
        logger.info(f"[NijiDiarySync] {action} diary entry for {niji_user.user_id} on {date_str}")

        return await self._post_diary(niji_user.token, diary_title, history_text, diary_id, date_str)

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
            yield event.plain_result("✅ 日记账号绑定成功！聊天记录将自动同步（每天仅上传新增内容）。")
            logger.info(f"[NijiDiarySync] User {astrbot_user_id} logged in and bound to diary account {user_id}.")
        else:
            yield event.plain_result("❌ 登录失败，请检查用户名和密码。")

    @filter.command("sync")
    async def _cmd_sync(self, event: AstrMessageEvent):
        """手动触发同步，立即上传今日的新聊天记录（增量）"""
        astrbot_user_id = event.unified_msg_origin
        if astrbot_user_id not in self._niji_users:
            yield event.plain_result("❌ 你尚未绑定日记账号，请先使用 /login 命令绑定。")
            return

        niji_user = self._niji_users[astrbot_user_id]
        today_str = _fmt_date(_now_beijing())

        # 如果今天已上传，询问是否强制再传（强制将重新上传全部历史）
        if niji_user.last_uploaded_date == today_str:
            yield event.plain_result("⚠️ 今天已经自动同步过，是否强制重新上传全部历史？(输入 /sync force 确认)")
            return

        # 执行增量上传
        yield event.plain_result("🔄 正在获取新增聊天记录并上传，请稍候...")
        since_date = niji_user.last_uploaded_date if niji_user.last_uploaded_date else None
        new_messages = await self._get_conversation_history_since(astrbot_user_id, since_date)
        if not new_messages.strip():
            yield event.plain_result("📭 没有找到新增的聊天记录（自上次同步以来）。")
            # 仍然更新最后上传日期，避免下次重复询问
            niji_user.last_uploaded_date = today_str
            self._save_data()
            return

        success = await self._upload_chat_log(niji_user, today_str, new_messages)
        if success:
            niji_user.last_uploaded_date = today_str
            self._save_data()
            yield event.plain_result("✅ 手动同步成功！")
        else:
            yield event.plain_result("❌ 手动同步失败，请查看日志。")

    @filter.command("sync force")
    async def _cmd_sync_force(self, event: AstrMessageEvent):
        """强制重新上传今日聊天记录（上传全部历史，忽略增量）"""
        astrbot_user_id = event.unified_msg_origin
        if astrbot_user_id not in self._niji_users:
            yield event.plain_result("❌ 你尚未绑定日记账号。")
            return

        niji_user = self._niji_users[astrbot_user_id]
        today_str = _fmt_date(_now_beijing())

        yield event.plain_result("🔄 强制同步中（上传全部历史）...")
        # 强制上传全部历史（since_date=None）
        full_history = await self._get_conversation_history_since(astrbot_user_id, None)
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