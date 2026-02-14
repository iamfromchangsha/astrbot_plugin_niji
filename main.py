from astrbot import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import (
    MessageEventResult,
    AstrMessageEvent,
    filter,
)
from astrbot.api.message_components import (
    Image,
    Plain,
    Record,
    Video,
)
from astrbot.api.star import Context, Star, register
import aiohttp
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


@register(
    name="niji_diary_logger",
    author="iamfromchangsha",
    description="自动将用户与AI的完整对话记录上传至'你的日记'",
    version="1.0.0",
)
class NijiDiaryLoggerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        # 升级为对话缓冲区: {user_id: [{"role": "user/assistant", "content": str}, ...]}
        self.conversation_buffer: Dict[str, List[Dict[str, str]]] = {}
        self.scheduler_task: Optional[asyncio.Task] = None

    async def initialize(self):
        self.session = aiohttp.ClientSession()
        self.scheduler_task = asyncio.create_task(self._daily_scheduler())
        logger.info("NijiDiaryLoggerPlugin (v1.0.0) 已启动，记录完整对话。")

    def _get_beijing_time(self) -> datetime:
        return datetime.now(timezone(timedelta(hours=8)))

    def _get_current_date_str(self) -> str:
        return self._get_beijing_time().strftime("%Y-%m-%d")

    async def is_user_bound(self, user_id: str) -> bool:
        return await self.get_kv_data(f"niji_token_{user_id}") is not None

    def _extract_plain_text(self, message: List[Any]) -> str:
        """从消息段中提取纯文本，忽略媒体"""
        parts = []
        for seg in message:
            if isinstance(seg, Plain):
                parts.append(seg.text.strip())
        return " ".join(parts).strip() or "[无文本内容]"

    def _append_to_buffer(self, user_id: str, role: str, content: str):
        if not content:
            return
        if user_id not in self.conversation_buffer:
            self.conversation_buffer[user_id] = []
        self.conversation_buffer[user_id].append({"role": role, "content": content})

    # ========== 登录与日记操作（保持不变）==========
    async def _login(self, username: str, password: str) -> Optional[str]:
        url = "https://nijiweb.cn/api/login/"
        headers = {"content-type": "application/x-www-form-urlencoded"}
        data = {"email": username, "password": password}
        try:
            async with self.session.post(url, headers=headers, data=data) as resp:
                resp.raise_for_status()
                token = resp.cookies.get("token")
                return token.value if token else None
        except Exception as e:
            logger.error(f"登录失败: {e}")
            return None

    async def _get_user_info_and_diaries(self, token: str) -> Tuple[Optional[str], list]:
        url = "https://nijiweb.cn/"
        headers = {"Cookie": f"token={token}"}
        try:
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                html = await resp.text()
            user_id = re.search(r"setUserId\((\d+)\);", html)
            user_id = user_id.group(1) if user_id else None
            cards = []
            for match in re.findall(r"addDiaryCard\((\{[^}]*\})\);", html):
                try:
                    cards.append(json.loads(match.replace("'", '"')))
                except:
                    continue
            return user_id, cards
        except Exception as e:
            logger.error(f"获取日记列表失败: {e}")
            return None, []

    async def _get_diary_content(
        self, token: str, owner_id: str, diary_id: str, user_id: str
    ) -> Optional[str]:
        url = "https://nijiweb.cn/api/"
        headers = {
            "Cookie": f"token={token}",
            "content-type": "application/x-www-form-urlencoded",
        }
        data = {
            "function": "getDiary",
            "ownerId": owner_id,
            "diaryId": diary_id,
            "userId": user_id,
        }
        try:
            async with self.session.post(url, headers=headers, data=data) as resp:
                resp.raise_for_status()
                return (await resp.json()).get("content")
        except Exception as e:
            logger.error(f"获取日记内容失败: {e}")
            return None

    async def _post_diary(
        self, token: str, title: str, content: str, diary_id: Optional[str]
    ) -> bool:
        url = "https://nijiweb.cn/api/"
        headers = {
            "Cookie": f"token={token}",
            "content-type": "application/x-www-form-urlencoded",
        }
        data = {
            "function": "writeDiary",
            "dateText": self._get_current_date_str(),
            "titleText": title,
            "contentText": content,
            "id": diary_id or "",
        }
        try:
            async with self.session.post(url, headers=headers, data=data) as resp:
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"发布日记失败: {e}")
            return False

    async def _upload_user_diary(self, user_id: str):
        token = await self.get_kv_data(f"niji_token_{user_id}")
        if not token:
            return

        convs = self.conversation_buffer.get(user_id, [])
        if not convs:
            logger.debug(f"用户 {user_id} 今日无对话，跳过。")
            return

        # 格式化为可读对话
        formatted_lines = []
        for turn in convs:
            role_name = "👤 User" if turn["role"] == "user" else "🤖 Assistant"
            formatted_lines.append(f"{role_name}:\n{turn['content']}\n")
        new_content = "\n".join(formatted_lines).strip()

        current_date = self._get_current_date_str()
        title = f"AI对话记录 - {current_date}"

        # 检查是否已有当天日记
        _, cards = await self._get_user_info_and_diaries(token)
        existing_id = None
        for card in cards:
            if str(card.get("cardUserID")) == user_id and card.get("createdDate") == current_date:
                existing_id = card.get("cardDiaryId")
                break

        final_content = new_content
        if existing_id:
            old_content = await self._get_diary_content(token, user_id, existing_id, user_id)
            if old_content:
                final_content = old_content + "\n\n---\n" + new_content

        if await self._post_diary(token, title, final_content, existing_id):
            logger.info(f"成功上传用户 {user_id} 的完整对话。")
            self.conversation_buffer[user_id] = []  # 清空
        else:
            logger.error(f"上传失败: {user_id}")

    async def _daily_scheduler(self):
        """每天北京时间 23:50 触发上传"""
        while True:
            try:
                now = self._get_beijing_time()
                # 计算今天 23:50 的时间点
                today_2350 = now.replace(hour=23, minute=50, second=0, microsecond=0)
                
                # 如果已经过了今天的 23:50，则目标是明天的 23:50
                if now > today_2350:
                    next_run = today_2350 + timedelta(days=1)
                else:
                    next_run = today_2350

                sleep_seconds = (next_run - now).total_seconds()
                logger.debug(f"下次日记上传时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}, 等待 {sleep_seconds:.0f} 秒")
                await asyncio.sleep(sleep_seconds)
                
                # 上传时，当前日期就是“要上传的日期”
                all_keys = await self.get_all_kv_keys()
                for k in all_keys:
                    if k.startswith("niji_token_"):
                        uid = k.replace("niji_token_", "")
                        await self._upload_user_diary(uid)
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"调度器错误: {e}")

    # ========== 指令与事件监听 ==========
    @filter.command("login")
    async def login_command(self, event: AstrMessageEvent, username: str, password: str):
        user_id = event.get_sender_id()
        token = await self._login(username, password)
        if token:
            await self.put_kv_data(f"niji_token_{user_id}", token)
            yield event.plain_result("✅ 绑定成功！您与AI的完整对话将每日自动同步到'你的日记'。")
        else:
            yield event.plain_result("❌ 绑定失败，请检查账号密码。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        """记录用户发来的消息"""
        user_id = event.get_sender_id()
        if not await self.is_user_bound(user_id):
            return
        text = self._extract_plain_text(event.get_message())
        self._append_to_buffer(user_id, "user", text)

    @filter.after_event()
    async def on_bot_response(self, event: AstrMessageEvent, result: MessageEventResult):
        """记录Bot的回复"""
        if not isinstance(result, MessageEventResult):
            return
        user_id = event.get_sender_id()
        if not await self.is_user_bound(user_id):
            return
        text = self._extract_plain_text(result.get_message())
        self._append_to_buffer(user_id, "assistant", text)

    async def terminate(self):
        if self.scheduler_task:
            self.scheduler_task.cancel()
        if self.session:
            await self.session.close()
        logger.info("NijiDiaryLoggerPlugin 已停止。")