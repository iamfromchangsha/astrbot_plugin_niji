from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import aiohttp
import re
import json
from datetime import datetime, timezone, timedelta
import asyncio
from typing import Optional, Dict, List

@register("diary_uploader", "豆包", "自动上传聊天记录到日记插件", "1.0.0")
class DiaryUploaderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 存储用户的登录信息，key是用户id，value是{"username": "", "password": "", "token": ""}
        self.user_data: Dict[str, Dict] = {}
        # 聊天记录存储，key是用户id，value是{"date": "yyyy-mm-dd", "records": []}
        self.chat_records: Dict[str, Dict] = {}
        # 启动定时任务，每天23:55上传当天的聊天记录
        self.start_scheduled_task()

    def start_scheduled_task(self):
        # 每天23:55执行上传任务
        async def scheduled_upload():
            while True:
                # 计算下次执行的时间
                now = datetime.now(timezone(timedelta(hours=8)))
                target_time = now.replace(hour=23, minute=55, second=0, microsecond=0)
                if now > target_time:
                    # 今天已经过了23:55，明天执行
                    target_time += timedelta(days=1)
                delta = (target_time - now).total_seconds()
                await asyncio.sleep(delta)
                # 执行上传
                await self.upload_daily_records()
        # 启动任务
        asyncio.create_task(scheduled_upload())

    async def upload_daily_records(self):
        # 遍历所有用户的聊天记录，上传
        current_date = self.get_current_date_str()
        for user_id, record_data in self.chat_records.items():
            if record_data.get("date") == current_date and record_data.get("records"):
                # 获取用户的token
                user_info = self.user_data.get(user_id)
                if not user_info or not user_info.get("token"):
                    continue
                # 拼接聊天记录
                content = "\n\n".join(record_data["records"])
                # 上传到日记
                await self.upload_to_diary(user_info["token"], user_id, content, current_date)
                # 清空当天的记录
                self.chat_records[user_id] = {"date": current_date, "records": []}

    def get_current_date_str(self):
        beijing_tz = timezone(timedelta(hours=8))
        current_time = datetime.now(beijing_tz)
        return current_time.strftime("%Y-%m-%d")

    async def login(self, username: str, password: str) -> Optional[str]:
        url = "https://nijiweb.cn/api/login/"
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
        }
        data = {
            "email": username,
            "password": password
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as response:
                if response.status == 200:
                    # 获取token
                    token = response.cookies.get('token')
                    if token:
                        return token.value
                logger.error(f"登录失败，状态码: {response.status}，响应: {await response.text()}")
                return None

    async def get_userid_and_diarycard(self, token: str) -> tuple[Optional[str], List[Dict]]:
        url = "https://nijiweb.cn/"
        headers = {
            "Cookie": f"token={token}",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    response.raise_for_status()
                    html_content = await response.text()
                    # 提取用户ID
                    userid_pattern = r"setUserId\((\d+)\);"
                    userid_match = re.search(userid_pattern, html_content)
                    user_id = userid_match.group(1) if userid_match else None
                    # 提取日记卡片
                    diary_pattern = r"addDiaryCard\((\{[^}]*\})\);"
                    diary_matches = re.findall(diary_pattern, html_content)
                    diary_cards = []
                    for json_str in diary_matches:
                        clean_json = json_str.replace("'", '"')
                        try:
                            diary_data = json.loads(clean_json)
                            diary_cards.append(diary_data)
                        except json.JSONDecodeError as e:
                            logger.error(f"解析日记卡片失败: {e}，内容: {json_str}")
                    return user_id, diary_cards
        except Exception as e:
            logger.error(f"获取用户信息和日记卡片失败: {e}")
            return None, []

    async def get_diary(self, token: str, ownerid: str, diaryid: str, userid: str) -> Optional[Dict]:
        url = "https://nijiweb.cn/api/"
        headers = {
            "Cookie": f"token={token}",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
        }
        data = {
            "function": "getDiary",
            "ownerId": ownerid,
            "diaryId": diaryid,
            "userId": userid
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as response:
                if response.status == 200:
                    return await response.json()
                logger.error(f"获取日记失败，状态码: {response.status}，响应: {await response.text()}")
                return None

    async def post_diary(self, token: str, title: str, content: str, diary_id: Optional[str], date_text: str) -> bool:
        url = "https://nijiweb.cn/api/"
        headers = {
            "Cookie": f"token={token}",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
        }
        data = {
            "function": "writeDiary",
            "dateText": date_text,
            "titleText": title,
            "contentText": content,
            "id": diary_id or ""
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as response:
                if response.status == 200:
                    logger.info(f"日记上传成功，响应: {await response.text()}")
                    return True
                logger.error(f"日记上传失败，状态码: {response.status}，响应: {await response.text()}")
                return False

    async def upload_to_diary(self, token: str, user_id: str, content: str, date_str: str):
        # 先获取用户的日记卡片，查看当天是否已有日记
        _, diary_cards = await self.get_userid_and_diarycard(token)
        current_date = date_str
        existing_diary_id = None
        for card in diary_cards:
            if str(card.get("cardUserID")) == user_id and card.get("createdDate") == current_date:
                existing_diary_id = card.get("cardDiaryId")
                break
        title = f"当天聊天记录_{current_date}"
        if existing_diary_id:
            # 更新现有日记
            existing_diary = await self.get_diary(token, user_id, existing_diary_id, user_id)
            if existing_diary:
                new_content = existing_diary.get("content", "") + "\n\n" + content
                await self.post_diary(token, title, new_content, existing_diary_id, current_date)
        else:
            # 创建新日记
            await self.post_diary(token, title, content, None, current_date)

    @filter.command("login")
    async def login_command(self, event: AstrMessageEvent):
        # 解析用户输入的账号和密码，格式：/login 用户名 密码
        args = event.message_str.split()
        if len(args) < 3:
            yield event.plain_result("请输入正确的格式：/login 用户名 密码")
            return
        username = args[1]
        password = args[2]
        # 登录
        token = await self.login(username, password)
        if token:
            # 存储用户信息
            user_id = event.get_sender_id()
            self.user_data[user_id] = {
                "username": username,
                "password": password,
                "token": token
            }
            # 初始化聊天记录
            self.chat_records[user_id] = {"date": self.get_current_date_str(), "records": []}
            yield event.plain_result("登录成功，已绑定账号，将自动上传每天的聊天记录到日记。")
        else:
            yield event.plain_result("登录失败，请检查账号密码是否正确。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        # 保存聊天记录
        user_id = event.get_sender_id()
        if user_id not in self.user_data:
            # 用户未绑定，不保存
            return
        current_date = self.get_current_date_str()
        # 检查用户的聊天记录是否是当天的
        record_data = self.chat_records.get(user_id)
        if not record_data or record_data.get("date") != current_date:
            self.chat_records[user_id] = {"date": current_date, "records": []}
        # 记录消息，格式：[时间] 发送者：消息内容
        time_str = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
        sender_name = event.get_sender_name()
        message = event.message_str
        record = f"[{time_str}] {sender_name}：{message}"
        self.chat_records[user_id]["records"].append(record)
        logger.info(f"已保存聊天记录：{record}")

    async def terminate(self):
        # 插件停止时，关闭定时任务
        pass