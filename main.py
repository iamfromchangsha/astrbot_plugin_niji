from astrbot import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "diary_uploader",
    "自动上传聊天记录到日记插件",
    "iamfromchangsha",
    "1.0.8"
)
class NijiDiaryLoggerPlugin(Star):
    def __init__(self, context):
        super().__init__(context)
        logger.info("Niji Diary Logger (v1.0.8) 已初始化")

    async def initialize(self):
        logger.info("Niji Diary Logger (v1.0.8) 已启动")

    async def terminate(self):
        logger.info("Niji Diary Logger (v1.0.8) 已停止")
