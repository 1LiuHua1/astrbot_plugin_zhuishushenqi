# astrbot_plugin_zhuishushenqi

基于 AstrBot 的 **追书神器免费版** 插件。支持短信验证码登录、极验 GT3 验证码自动过验，登录后可同步 Token 到青龙/呆呆面板环境变量，实现多账号管理与白名单控制。

## ✨ 功能

- **短信验证码登录**：自动调用追书神器接口发送短信，并处理极验 GT3 滑块验证。
- **查询账号信息**：支持查询金币、余额、等级等追书神器账号详情。
- **交互式账号管理**：管理员可查看所有登录账号、删除指定账号。
- **多用户隔离**：每个 QQ 用户管理自己的追书神器账号，Token 互不干扰。
- **白名单权限控制**：管理员可动态添加/移除可使用插件的用户。
- **同步到青龙/呆呆面板**：可将登录后的 Token 同步为面板环境变量 `ZSSQ_TOKEN`，方便脚本调用。

## 📦 安装

### 通过 AstrBot WebUI 安装
1. 登录 AstrBot WebUI 管理面板。
2. 点击左侧菜单「插件」→「插件市场」。
3. 在“从 URL 安装”或“上传文件”中，填入本仓库地址：
   `https://github.com/1LiuHua1/astrbot_plugin_zhuishushenqi`
4. 点击“安装”并等待完成。

### 手动安装（开发者）
1. 将本仓库克隆或下载解压到 AstrBot 的 `data/plugins/` 目录下。
2. 在 AstrBot WebUI 的「插件」→「已安装」页面，找到 `astrbot_plugin_zhuishushenqi`。
3. 点击右侧的“管理”（或“...”）按钮，选择“重载插件”以激活。

## ⚙️ 配置

在插件安装并重载后，进入 WebUI 的「插件」→「已安装」页面，找到本插件，点击“配置”按钮进行设置：

| 配置项 | 说明 | 必填 |
| :--- | :--- | :---: |
| `zssq_api_base` | 追书神器 API 服务器根地址，默认无需修改 | 否 |
| `geetest_appkey` | 极验验证码自动过验服务的 AppKey（用于自动通过滑块） | 是 |
| `qinglong_url` | 青龙/呆呆面板地址，例如 `http://192.168.1.100:5700` | 是 |
| `qinglong_client_id` | 青龙 OpenAPI 的 Client ID | 是 |
| `qinglong_client_secret` | 青龙 OpenAPI 的 Client Secret | 是 |
| `admin_whitelist` | 管理员用户 ID 列表，多个用英文逗号 `,` 分隔，例如 `123456,789012` | 否 |

> **注意**：青龙面板需开启 OpenAPI 功能，并在「开放平台」→「应用」中创建应用以获取 `Client ID` 和 `Client Secret`。

## 🕹️ 使用

在 QQ 聊天窗口向机器人发送以下指令（假设命令前缀为 `/`）：

### 普通用户指令
| 指令 | 说明 |
| :--- | :--- |
| `/zssq_login <手机号>` | 发起短信登录，向指定手机号发送验证码 |
| `/zssq_code <验证码>` | 输入收到的6位短信验证码，完成登录并自动过极验 |
| `/zssq_info` | 查看当前登录账号的昵称、金币、余额、等级 |
| `/zssq_sync` | 将当前登录账号的 Token 同步到青龙/呆呆面板 |

### 管理员指令
| 指令 | 说明 |
| :--- | :--- |
| `/zssq_accounts list` | 查看所有已登录账号的列表 |
| `/zssq_accounts delete <QQ号>` | 删除指定 QQ 号绑定的追书神器账号 |
| `/zssq_whitelist add <QQ号>` | 将用户加入白名单，允许使用本插件 |
| `/zssq_whitelist remove <QQ号>` | 将用户移出白名单 |
| `/zssq_whitelist list` | 查看当前白名单中的所有 QQ 号 |

### 典型流程
1. **管理员**在插件配置中填写极验 AppKey 和青龙面板信息，并将需要使用插件的用户 QQ 加入白名单。
2. **用户**发送 `/zssq_login 13800138000`。
3. 收到短信后，**用户**回复 `/zssq_code 123456`，插件自动处理极验滑块并完成登录。
4. 登录成功后，**用户**可随时发送 `/zssq_sync` 将 Token 同步至青龙面板的环境变量 `ZSSQ_TOKEN`。

## 🗂️ 项目文件结构
