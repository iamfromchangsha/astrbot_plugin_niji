from astrbot import logger
from astrbot.api.event import (
    MessageEventResult,
    AstrMessageEvent,
    filter,
    EventMessageType
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
    "niji_diary_logger",
    "自动将用户与AI的完整对话记录上传至'你的日记'",
    "iamfromchangsha",
    "1.0.0"
)
class NijiDiaryLoggerPlugin(Star):
    # 完全适配 v4.14.2：仅保留 context 参数，彻底移除 config
    def __init__(self, context: Context):
        super().__init__(context)
        self.session: Optional[aiohttp.ClientSession] = None
        # 对话缓冲区: {user_id: [{"role": "user/assistant", "content": str}, ...]}
        self.conversation_buffer: Dict[str, List[Dict[str, str]]] = {}
        self.scheduler_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """插件初始化"""
        self.session = aiohttp.ClientSession()
        self.scheduler_task = asyncio.create_task(self._daily_scheduler())
        logger.info("NijiDiaryLoggerPlugin (v1.0.0) 已启动，记录完整对话。")

    def _get_beijing_time(self) -> datetime:
        """获取北京时间"""
        return datetime.now(timezone(timedelta(hours=8)))

    def _get_current_date_str(self) -> str:
        """获取当前日期字符串 YYYY-MM-DD"""
        return self._get_beijing_time().strftime("%Y-%m-%d")

    async def is_user_bound(self, user_id: str) -> bool:
        """检查用户是否已绑定账号"""
        return await self.get_kv_data(f"niji_token_{user_id}") is not None

    def _extract_plain_text(self, message: List[Any]) -> str:
        """从消息段中提取纯文本，忽略媒体"""
        parts = []
        for seg in message:
            if isinstance(seg, Plain):
                parts.append(seg.text.strip())
        return " ".join(parts).strip() or "[无文本内容]"

    def _append_to_buffer(self, user_id: str, role: str, content: str):
        """将消息添加到对话缓冲区"""
        if not content:
            return
        if user_id not in self.conversation_buffer:
            self.conversation_buffer[user_id] = []
        self.conversation_buffer[user_id].append({"role": role, "content": content})

    # ========== 登录与日记操作 ==========
    async def _login(self, username: str, password: str) -> Optional[str]:
        """登录你的日记并获取token"""
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
        """获取用户ID和日记列表"""
        url = "https://nijiweb.cn/"
        headers = {"Cookie": f"token={token}"}
        try:
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                html = await resp.text()
            # 提取用户ID
            user_id_match = re.search(r"setUserId\((\d+)\);", html)
            user_id = user_id_match.group(1) if user_id_match else None
            # 提取日记卡片
            cards = []
            for match in re.findall(r"addDiaryCard\((\{[^}]*\})\);", html):
                try:
                    # 替换单引号为双引号，兼容JSON解析
                    card_json = match.replace("'", '"').replace("\\'", "'")
                    cards.append(json.loads(card_json))
                except Exception as e:
                    logger.warning(f"解析日记卡片失败: {e}, 原始数据: {match}")
                    continue
            return user_id, cards
        except Exception as e:
            logger.error(f"获取日记列表失败: {e}")
            return None, []

    async def _get_diary_content(
        self, token: str, owner_id: str, diary_id: str, user_id: str
    ) -> Optional[str]:
        """获取指定日记的内容"""
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
                result = await resp.json()
                return result.get("content", "")
        except Exception as e:
            logger.error(f"获取日记内容失败: {e}")
            return None

    async def _post_diary(
        self, token: str, title: str, content: str, diary_id: Optional[str] = None
    ) -> bool:
        """发布/更新日记"""
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
        """上传指定用户的对话记录到日记"""
        # 获取用户绑定的token
        token = await self.get_kv_data(f"niji_token_{user_id}")
        if not token:
            logger.debug(f"用户 {user_id} 未绑定账号，跳过上传")
            return

        # 获取用户的对话记录
        convs = self.conversation_buffer.get(user_id, [])
        if not convs:
            logger.debug(f"用户 {user_id} 今日无对话记录，跳过上传")
            return

        # 格式化对话记录
        formatted_lines = []
        for idx, turn in enumerate(convs, 1):
            role_name = "👤 我" if turn["role"] == "user" else "🤖 AI"
            formatted_lines.append(f"【{idx}】{role_name}:\n{turn['content']}\n")
        new_content = "\n".join(formatted_lines).strip()

        # 准备日记标题和日期
        current_date = self._get_current_date_str()
        title = f"AI对话记录 - {current_date}"

        # 检查是否已有当天日记
        niji_user_id, cards = await self._get_user_info_and_diaries(token)
        if not niji_user_id:
            logger.error(f"无法获取用户 {user_id} 的日记ID，上传失败")
            return

        existing_diary_id = None
        for card in cards:
            try:
                if (str(card.get("cardUserID")) == niji_user_id and 
                    card.get("createdDate") == current_date):
                    existing_diary_id = card.get("cardDiaryId")
                    break
            except Exception as e:
                logger.warning(f"检查日记失败: {e}")
                continue

        # 拼接内容（如果已有日记则追加）
        final_content = new_content
        if existing_diary_id:
            old_content = await self._get_diary_content(token, niji_user_id, existing_diary_id, niji_user_id)
            if old_content:
                final_content = f"{old_content}\n\n--- 新增对话 ---\n{new_content}"

        # 发布/更新日记
        if await self._post_diary(token, title, final_content, existing_diary_id):
            logger.info(f"成功上传用户 {user_id} 的对话记录到日记")
            # 清空缓冲区
            self.conversation_buffer[user_id] = []
        else:
            logger.error(f"用户 {user_id} 日记上传失败")

    async def _daily_scheduler(self):
        """每日定时上传任务（北京时间23:50）"""
        while True:
            try:
                now = self._get_beijing_time()
                # 计算下次执行时间（当天23:50）
                target_time = now.replace(hour=23, minute=50, second=0, microsecond=0)
                if now > target_time:
                    # 如果已过当天23:50，则执行明天的
                    target_time += timedelta(days=1)
                
                # 计算等待时间
                wait_seconds = (target_time - now).total_seconds()
                logger.info(f"下次日记上传时间: {target_time.strftime('%Y-%m-%d %H:%M:%S')}，等待 {int(wait_seconds)} 秒")
                
                # 等待执行
                await asyncio.sleep(wait_seconds)
                
                # 执行上传
                logger.info("开始执行每日对话记录上传任务")
                try:
                    # 获取所有绑定用户
                    all_keys = await self.get_all_kv_keys()
                    bound_users = [k.replace("niji_token_", "") for k in all_keys if k.startswith("niji_token_")]
                    
                    for user_id in bound_users:
                        await self._upload_user_diary(user_id)
                    
                    logger.info("每日对话记录上传任务执行完成")
                except Exception as e:
                    logger.error(f"执行每日上传任务失败: {e}")
                    
            except asyncio.CancelledError:
                logger.info("定时任务已取消")
                break
            except Exception as e:
                logger.error(f"定时任务出错: {e}")
                # 出错后等待1分钟再重试，避免死循环
                await asyncio.sleep(60)

    # ========== 指令与事件监听 ==========
    @filter.command("login")
    async def login_command(self, event: AstrMessageEvent, username: str, password: str):
        """登录指令：/login 邮箱 密码"""
        user_id = event.get_sender_id()
        logger.info(f"用户 {user_id} 尝试绑定账号: {username}")
        
        # 登录并获取token
        token = await self._login(username, password)
        if token:
            # 保存token
            await self.put_kv_data(f"niji_token_{user_id}", token)
            yield event.plain_result("✅ 绑定成功！您与AI的完整对话将每日23:50自动同步到'你的日记'。")
            logger.info(f"用户 {user_id} 账号绑定成功")
        else:
            yield event.plain_result("❌ 绑定失败，请检查账号密码是否正确。")
            logger.warning(f"用户 {user_id} 账号绑定失败")

    @filter.event_message_type(EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        """监听所有用户消息并记录"""
        user_id = event.get_sender_id()
        
        # 只处理已绑定用户的消息
        if not await self.is_user_bound(user_id):
            return
        
        # 提取纯文本内容
        message_segments = event.get_message()
        text_content = self._extract_plain_text(message_segments)
        
        # 记录用户消息
        if text_content and text_content != "[无文本内容]":
            self._append_to_buffer(user_id, "user", text_content)
            logger.debug(f"记录用户 {user_id} 消息: {text_content[:50]}...")

    async def on_bot_send_message(self, event: AstrMessageEvent, message: List[Any]):
        """监听机器人发送的消息（v4.14.2 兼容方式）"""
        user_id = event.get_sender_id()
        
        # 只处理已绑定用户
        if not await self.is_user_bound(user_id):
            return
        
        # 提取机器人回复的纯文本
        text_content = self._extract_plain_text(message)
        
        # 记录机器人回复
        if text_content and text_content != "[无文本内容]":
            self._append_to_buffer(user_id, "assistant", text_content)
            logger.debug(f"记录机器人回复 {user_id}: {text_content[:50]}...")

    async def terminate(self):
        """插件停止时的清理工作"""
        # 取消定时任务
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
        
        # 关闭HTTP会话
        if self.session:
            await self.session.close()
        
        logger.info("NijiDiaryLoggerPlugin 已停止运行")