# 📖 Niji Diary Sync

自动将你与 AI 的聊天记录同步到 [你的日记](https://nijiweb.cn) 网站，支持每日自动上传和手动触发同步，打造属于你的私人 AI 对话存档。


![统计访问](http://moe.xn--estn41aqtae4v.xyz/@astrbot_plugin_niji?name=astrbot_plugin_niji&theme=random&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)
## ✨ 核心功能

- **自动同步**：每天北京时间 23:50 自动上传当日聊天记录
- **手动同步**：支持使用 `/sync` 命令手动触发上传
- **增量上传**：智能检测新增消息，仅上传未同步的内容
- **多用户支持**：支持多用户独立记录，互不干扰
- **数据安全**：仅保存文本内容，不上传图片/视频等媒体文件
- **防覆盖机制**：如果当天已有日记，新内容会更新到该日记中

## 📦 版本要求

- **AstrBot ≥ v4.9.2**
- **Python ≥ 3.8**
- 网络可访问 `https://nijiweb.cn`

## ⚙️ 安装方法

1. 将本插件文件夹放入 AstrBot 的插件目录：
   ```
   astrbot/plugins/astrbot_plugin_niji/
   ```

2. 确保目录结构如下：
   ```
   astrbot_plugin_niji/
   ├── main.py
   ├── requirements.txt
   ├── metadata.yaml
   └── readme.md
   ```

3. 重启 AstrBot，插件将自动加载并安装依赖。

## 🔑 使用步骤

### 1. 绑定账号

在任意支持的平台（如 QQ、微信、Telegram 等）向 Bot 发送指令：

```
/login 你的邮箱 你的密码
```

**示例：**
```
/login user@example.com mypassword123
```

- 若返回 `✅ 日记账号绑定成功！...`，表示已启用自动同步
- 若返回 `❌ 登录失败...`，请检查账号密码是否正确

> 🔐 安全说明：密码仅用于登录获取 token，不会被存储。token 会保存在本地数据文件中。

### 2. 开始聊天

绑定后，你与 AI 的所有对话（包括后续提问和 AI 回答）都会被自动记录。

### 3. 同步方式

#### 自动同步
- 插件会在 **每天北京时间 23:50** 自动将当日新增聊天记录上传至 [你的日记](https://nijiweb.cn)
- 日记标题格式：`Chat Log YYYY-MM-DD`

#### 手动同步
- 发送指令 `/sync` 可立即上传今日的新增聊天记录
- 发送指令 `/sync force` 可强制重新上传今日全部聊天记录

### 4. 查看记录

登录 [你的日记](https://nijiweb.cn) 网站，在日记列表中查看标题为 `Chat Log YYYY-MM-DD` 的条目。

## 📝 注意事项

- **仅记录文本**：图片、语音、表情包等非文本消息会被忽略
- **数据存储**：用户凭据和同步状态会保存在本地数据文件中
- **上传失败处理**：若网络异常导致上传失败，数据会保留并在下次同步时重试
- **多设备使用**：如果在多个平台使用同一个"你的日记"账号，建议只在一个平台绑定，避免重复记录

## ❓ 常见问题

### Q：为什么日记日期和聊天日期不一致？
A：本插件在 **每天北京时间 23:50** 上传，确保日记日期与聊天日期一致。

### Q：如何手动触发上传？
A：发送 `/sync` 命令即可手动触发今日新增内容的上传。

### Q：如何强制重新上传全部历史？
A：发送 `/sync force` 命令可强制重新上传今日全部聊天记录。

### Q：绑定失败怎么办？
A：请检查：
1. 邮箱和密码是否正确
2. 网络是否可以访问 `https://nijiweb.cn`
3. AstrBot 版本是否 ≥ v4.9.2

## 🛠 依赖说明

本插件仅依赖：
- `aiohttp>=3.9.0`（用于 HTTP 请求）

AstrBot 核心模块由主程序提供，无需额外安装。

## 📜 许可证

MIT License — 自由使用、修改、分发。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request 来帮助改进这个插件！

---

> 💬 作者：iamfromchangsha  
> 🌐 项目地址：https://github.com/iamfromchangsha/astrbot_plugin_niji  
> 🕒 最后更新：2026年2月
