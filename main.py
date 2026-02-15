from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
import aiohttp
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone


@register(
    "diary_uploader",
    "自动上传聊天记录到日记插件",
    "iamfromchangsha",
    "1.0.9"
)
class NijiDiaryLoggerPlugin(Star):
    def __init__(self, context):
        super().__init__(context)
        self.session = None
        self.conversation_buffer = {}
        self.scheduler_task = None
        logger.info("Niji Diary Logger (v1.0.9) 已初始化")

    async def initialize(self):
        self.session = aiohttp.ClientSession()
        self.scheduler_task = asyncio.create_task(self._daily_scheduler())
        logger.info("Niji Diary Logger (v1.0.9) 已启动，开始记录对话。")

    def _get_beijing_time(self):
        return datetime.now(timezone(timedelta(hours=8)))

    def _get_current_date_str(self):
        return self._get_beijing_time().strftime("%Y-%m-%d")

    async def is_user_bound(self, user_id):
        """检查用户是否已绑定"""
        return await self.get_kv_data(f"niji_token_{user_id}") is not None

    def _extract_plain_text(self, message):
        """从消息段中提取纯文本，忽略媒体"""
        parts = []
        for seg in message:
            if isinstance(seg, Plain):
                parts.append(seg.text.strip())
        return " ".join(parts).strip()

    def _append_to_buffer(self, user_id, role, content):
        """将消息追加到缓冲区"""
        if not content:
            return
        current_date = self._get_current_date_str()
        if user_id not in self.conversation_buffer:
            self.conversation_buffer[user_id] = {}
        if current_date not in self.conversation_buffer[user_id]:
            self.conversation_buffer[user_id][current_date] = []
        self.conversation_buffer[user_id][current_date].append({"role": role, "content": content})

    # ========== 「你的日记」API 操作 ==========
    async def _login(self, username, password):
        url = "https://nijiweb.cn/api/login/"
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
        }
        data = {"email": username, "password": password}
        try:
            async with self.session.post(url, headers=headers, data=data) as resp:
                resp.raise_for_status()
                token = resp.cookies.get("token")
                return token.value if token else None
        except Exception as e:
            logger.error(f"登录失败: {e}")
            return None

    async def _get_user_info_and_diaries(self, token):
        url = "https://nijiweb.cn/"
        headers = {"Cookie": f"token={token}"}
        try:
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                html = await resp.text()
            user_id = re.search(r"setUserId$(\d+)$;", html)
            user_id = user_id.group(1) if user_id else None
            cards = []
            for match in re.findall(r"addDiaryCard$(\{[^}]*\})$;", html):
                try:
                    cards.append(json.loads(match.replace("'", '"')))
                except:
                    continue
            return user_id, cards
        except Exception as e:
            logger.error(f"获取日记列表失败: {e}")
            return None, []

    async def _get_diary_content(self, token, owner_id, diary_id, user_id):
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

    async def _post_diary(self, token, title, content, diary_id):
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

    async def _upload_user_diary(self, user_id):
        token = await self.get_kv_data(f"niji_token_{user_id}")
        if not token:
            return

        current_date = self._get_current_date_str()
        daily_records = self.conversation_buffer.get(user_id, {}).get(current_date, [])
        if not daily_records:
            logger.debug(f"用户 {user_id} 今日无对话，跳过上传。")
            return

        # 格式化为可读对话
        formatted_lines = []
        for record in daily_records:
            prefix = "👤 " if record["role"] == "user" else "🤖 "
            formatted_lines.append(f"{prefix}{record['content']}")
        new_content = "\n".join(formatted_lines).strip()

        title = f"AI对话记录 - {current_date}"

        # 检查是否已有当天日记
        _, cards = await self._get_user_info_and_diaries(token)
        existing_id = None
        for card in cards:
            if card.get("createdDate") == current_date:
                existing_id = card.get("cardDiaryId")
                break

        final_content = new_content
        if existing_id:
            old_content = await self._get_diary_content(token, user_id, existing_id, user_id)
            if old_content:
                final_content = old_content + "\n\n---\n" + new_content

        if await self._post_diary(token, title, final_content, existing_id):
            logger.info(f"成功上传用户 {user_id} 的对话记录。")
            # 清空当日记录
            if user_id in self.conversation_buffer and current_date in self.conversation_buffer[user_id]:
                self.conversation_buffer[user_id][current_date] = []
        else:
            logger.error(f"上传失败: {user_id}，数据将保留至次日重试。")

    async def _daily_scheduler(self):
        """每天北京时间 23:50 触发上传"""
        while True:
            try:
                now = self._get_beijing_time()
                today_2350 = now.replace(hour=23, minute=50, second=0, microsecond=0)
                if now > today_2350:
                    next_run = today_2350 + timedelta(days=1)
                else:
                    next_run = today_2350

                sleep_seconds = (next_run - now).total_seconds()
                logger.info(f"下次上传时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                await asyncio.sleep(sleep_seconds)

                all_keys = await self.get_all_kv_keys()
                for k in all_keys:
                    if k.startswith("niji_token_"):
                        uid = k.replace("niji_token_", "")
                        await self._upload_user_diary(uid)

            except asyncio.CancelledError:
                logger.info("日记上传调度器已取消。")
                break
            except Exception as e:
                logger.error(f"调度器错误: {e}")
                # 避免异常导致无限重启，休眠后再继续
                await asyncio.sleep(60)

    # ========== 指令与事件监听 ==========
    @filter.command("login")
    async def login_command(self, event: AstrMessageEvent, username: str, password: str):
        user_id = event.get_sender_id()
        token = await self._login(username, password)
        if token:
            await self.put_kv_data(f"niji_token_{user_id}", token)
            # 初始化用户缓冲区
            if user_id not in self.conversation_buffer:
                self.conversation_buffer[user_id] = {}
            yield event.plain_result(
                "✅ 绑定成功！\n"
                "📌 已开始记录您与AI的完整对话。\n"
                "⏰ 每日 23:50 自动同步至'你的日记'。"
            )
        else:
            yield event.plain_result("❌ 绑定失败，请检查账号密码。")

    @filter.command("logout")
    async def logout_command(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        await self.put_kv_data(f"niji_token_{user_id}", None)
        # 清理缓冲区
        if user_id in self.conversation_buffer:
            del self.conversation_buffer[user_id]
        yield event.plain_result("✅ 解绑成功！\n" "已停止记录对话并删除绑定信息。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """记录所有消息"""
        user_id = event.get_sender_id()
        if not await self.is_user_bound(user_id):
            return
        
        # 提取文本内容
        text = self._extract_plain_text(event.get_message())
        if not text:
            return

        # 避免记录指令本身
        if event.message_str.startswith("/"):
            return

        # 确定消息类型
        if hasattr(event, 'message_type') and event.message_type == 'response':
            role = "ai"
        else:
            role = "user"

        # 添加到缓冲区
        self._append_to_buffer(user_id, role, text)

    async def terminate(self):
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()
        logger.info("Niji Diary Logger 已停止。")