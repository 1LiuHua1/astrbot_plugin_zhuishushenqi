import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from typing import Optional, Dict, Any

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------

def _load_config(context: Context) -> Dict[str, Any]:
    """从插件配置中读取用户自定义参数，若无则回退为默认值。"""
    cfg = context.get_config() or {}
    return {
        "zssq_api_base": cfg.get("zssq_api_base", "https://goldcoinnew.zhuishushenqi.com"),
        "geetest_appkey": cfg.get("geetest_appkey", ""),
        "qinglong_url": cfg.get("qinglong_url", ""),
        "qinglong_client_id": cfg.get("qinglong_client_id", ""),
        "qinglong_client_secret": cfg.get("qinglong_client_secret", ""),
        "admin_whitelist": cfg.get("admin_whitelist", ""),
    }

async def _get_geetest_response(gt: str, challenge: str, appkey: str, referer: str) -> Dict[str, Any]:
    """调用第三方极验识别API（易云/类似接口）"""
    url = "http://api.z-fp.com/start_handle"
    params = {
        "username": "your_username",
        "appkey": appkey,
        "gt": gt,
        "challenge": challenge,
        "referer": referer,
        "handle_method": "three_on",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            result = await resp.json()
            if result.get("code") == 200:
                return result["data"]
            else:
                logger.error(f"极验识别失败: {result}")
                raise RuntimeError(f"极验识别失败: {result.get('msg', '未知错误')}")

# ---------------------------------------------------------------------------
# 追书神器 API 客户端
# ---------------------------------------------------------------------------

class ZSSQClient:
    """封装追书神器免费版的接口，优先通过 /account/profile 检测登录态并获取 Token。"""

    def __init__(self, api_base: str = "https://goldcoinnew.zhuishushenqi.com"):
        self.api_base = api_base
        self.session: Optional[aiohttp.ClientSession] = None
        self._device_id = str(uuid.uuid4()).replace("-", "")

    async def _ensure_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()

    async def close(self):
        if self.session:
            await self.session.close()

    # ---------- 主要使用的登录态检查方法 ----------
    async def check_auth(self, token: Optional[str] = None) -> Dict[str, Any]:
        """
        通过访问 /account/profile 检测当前客户端是否具备有效登录态。
        如果提供了 token，则使用该 token 请求；否则不带 token 直接访问，
        尝试触发服务器下发或默认行为。

        返回 {success, token, uid, msg}
        """
        await self._ensure_session()
        url = f"{self.api_base}/account/profile"
        headers = {
            "User-Agent": "ZhuiShu/5.0.1 (iPhone; iOS 16.0; Scale/3.00)",
            "X-Device-Id": self._device_id,
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with self.session.get(url, headers=headers, allow_redirects=True) as resp:
            status = resp.status
            resp_text = await resp.text()
            logger.info(f"/account/profile 响应码: {status}, 内容: {resp_text[:200]}...")
            if status == 200:
                try:
                    data = json.loads(resp_text)
                except Exception:
                    return {"success": False, "msg": f"返回非 JSON 数据: {resp_text[:100]}"}
                # 追书神器正常返回数据格式可能为 {"ok": true, "data": {...}} 或直接包含用户信息
                if data.get("ok"):
                    # 有些情况下这个接口会直接返回用户数据，但没有 token
                    # 如果有 user 字段，视为登录成功，但没有 token 时可能需要从别处获取
                    user_info = data.get("data") or data.get("user")
                    if user_info:
                        uid = str(user_info.get("id", ""))
                        # 此处无法直接获取 token，需要用户手动提供。提示登录成功但需要手动输入token
                        return {"success": True, "uid": uid, "msg": "检测到有效登录态，但自动获取 token 较复杂，请手动输入 token 或使用 /zssq_token 设置"}
                    # 如果没有用户信息但 ok 为真，也是一种成功
                    return {"success": True, "msg": "登录态检测通过"}
                # 返回的 ok 为 false，通常说明未登录
                return {"success": False, "msg": data.get("msg", "未登录或 token 无效")}
            else:
                return {"success": False, "msg": f"服务器返回 {status}"}

    # ---------- 旧的短信发送方法，已保留但不再使用 ----------
    async def request_sms_code(self, phone: str) -> Dict[str, Any]:
        """（已废弃）请求短信验证码，此接口可能会 404。"""
        await self._ensure_session()
        url = f"{self.api_base}/user/sendsms"
        headers = {
            "User-Agent": "ZhuiShu/5.0.1 (iPhone; iOS 16.0; Scale/3.00)",
            "X-Device-Id": self._device_id,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"phone": phone, "type": "login"}
        async with self.session.post(url, data=data, headers=headers) as resp:
            resp_text = await resp.text()
            logger.info(f"短信接口响应码: {resp.status}, 内容: {resp_text[:200]}")
            if resp.status == 404:
                return {"success": False, "msg": "接口不存在 (404)，请更换登录方式。"}
            try:
                result = json.loads(resp_text)
            except Exception:
                return {"success": False, "msg": f"非 JSON 响应: {resp_text[:100]}"}
            if result.get("ok"):
                return {"success": True, "gt": result.get("gt", ""), "challenge": result.get("challenge", ""), "msg": "验证码已发送"}
            return {"success": False, "msg": result.get("msg", "发送失败")}

    # ---------- 查询账号信息 ----------
    async def get_account_info(self, token: str) -> Dict[str, Any]:
        """获取账号金币/余额/等级等信息。"""
        await self._ensure_session()
        url = f"{self.api_base}/account/profile"
        headers = {
            "User-Agent": "ZhuiShu/5.0.1 (iPhone; iOS 16.0; Scale/3.00)",
            "X-Device-Id": self._device_id,
            "Authorization": f"Bearer {token}",
        }
        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            result = await resp.json()
            if result.get("ok"):
                profile = result.get("data", {})
                return {
                    "success": True,
                    "nickname": profile.get("nickname", ""),
                    "coin": profile.get("coin", 0),
                    "balance": profile.get("balance", 0.0),
                    "level": profile.get("level", 0),
                }
            return {"success": False, "msg": result.get("msg", "获取失败")}

# ---------------------------------------------------------------------------
# 青龙面板 API 客户端
# ---------------------------------------------------------------------------

class QingLongClient:
    """用于将 token 同步到青龙/呆呆面板环境变量。"""

    def __init__(self, base_url: str, client_id: str, client_secret: str):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        url = f"{self.base_url}/open/auth/token"
        params = {"client_id": self.client_id, "client_secret": self.client_secret}
        async with self._session.get(url, params=params) as resp:
            data = await resp.json()
            if data.get("code") == 200:
                self._token = data["data"]["token"]
                return self._token
            raise RuntimeError(f"青龙认证失败: {data.get('message', '未知错误')}")

    async def sync_env(self, name: str, value: str, remarks: str = "") -> Dict[str, Any]:
        """创建或追加环境变量。"""
        await self._ensure_session()
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        # 先尝试查找已存在的变量
        search_url = f"{self.base_url}/open/envs"
        params = {"searchValue": name}
        async with self._session.get(search_url, params=params, headers=headers) as resp:
            search_data = await resp.json()
        existing = None
        if search_data.get("code") == 200:
            for env in search_data.get("data", []):
                if env.get("name") == name:
                    existing = env
                    break
        if existing:
            # 追加值（使用 & 分隔）
            old_value = existing.get("value", "")
            new_value = old_value + "&" + value if old_value else value
            update_url = f"{self.base_url}/open/envs"
            body = {"id": existing["_id"], "name": name, "value": new_value, "remarks": remarks}
            async with self._session.put(update_url, json=body, headers=headers) as resp:
                result = await resp.json()
                if result.get("code") == 200:
                    return {"success": True, "msg": f"环境变量 {name} 已更新"}
                return {"success": False, "msg": result.get("message", "更新失败")}
        else:
            create_url = f"{self.base_url}/open/envs"
            body = [{"name": name, "value": value, "remarks": remarks}]
            async with self._session.post(create_url, json=body, headers=headers) as resp:
                result = await resp.json()
                if result.get("code") == 200:
                    return {"success": True, "msg": f"环境变量 {name} 已创建"}
                return {"success": False, "msg": result.get("message", "创建失败")}

# ---------------------------------------------------------------------------
# 多用户存储
# ---------------------------------------------------------------------------

class UserStore:
    """简单的 JSON 文件存储，按 QQ 号隔离用户 token。"""
    def __init__(self, path: str = "data/zssq_users.json"):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def _read(self) -> Dict[str, Dict[str, Any]]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: Dict[str, Dict[str, Any]]):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def save(self, qq: str, token: str, uid: str = ""):
        data = self._read()
        data[qq] = {"token": token, "uid": uid, "updated_at": int(time.time())}
        self._write(data)

    def get(self, qq: str) -> Optional[Dict[str, Any]]:
        return self._read().get(qq)

    def remove(self, qq: str) -> bool:
        data = self._read()
        if qq in data:
            del data[qq]
            self._write(data)
            return True
        return False

    def list_all(self) -> Dict[str, Dict[str, Any]]:
        return self._read()

# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------

@register("astrbot_plugin_zhuishushenqi", "1LiuHua1", "追书神器免费版插件",
          "1.0.1", "支持短信登录、极验自动过验、青龙同步")
class ZhuishuShenqiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = UserStore()

    # -----------------------------------------------------------------------
    # 白名单/权限辅助
    # -----------------------------------------------------------------------
    def _is_admin(self, user_id: str) -> bool:
        cfg = _load_config(self.context)
        whitelist = [x.strip() for x in cfg.get("admin_whitelist", "").split(",") if x.strip()]
        return str(user_id) in whitelist

    def _is_whitelisted(self, user_id: str) -> bool:
        return True  # 所有用户均可使用基础功能

    # -----------------------------------------------------------------------
    # 指令1：登录（新版策略）
    # -----------------------------------------------------------------------
    @filter.command("zssq_login")
    async def cmd_login(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if not self._is_whitelisted(user_id):
            yield event.plain_result("您没有使用此命令的权限。")
            return

        # 尝试通过 /account/profile 检测登录态（无需手机号）
        cfg = _load_config(self.context)
        client = ZSSQClient(cfg["zssq_api_base"])
        try:
            result = await client.check_auth()
        except Exception as e:
            logger.error(f"登录检查异常: {e}")
            yield event.plain_result(f"网络请求异常：{e}")
            return
        finally:
            await client.close()

        if result.get("success"):
            # 检测到有效登录态，但缺少 token，提示手动设置
            uid = result.get("uid", "未知")
            yield event.plain_result(
                f"检测到您的设备已登录追书神器（UID: {uid}），但自动获取 token 较复杂。\n"
                "请手动抓取 Authorization 头中的 token，然后使用 /zssq_token <你的token> 来保存。\n"
                "抓包教程抓取 App 中 goldcoinnew.zhuishushenqi.com 域名的请求头。"
            )
        else:
            # 未检测到登录态，提示用户手动登录 App 后再抓包
            yield event.plain_result(
                "未能检测到有效登录态，建议在手机上登录追书神器 App 后，通过抓包获取 token。\n"
                "获取后使用 /zssq_token <token> 命令保存。详细步骤请参考插件文档。"
            )

    # -----------------------------------------------------------------------
    # 指令2：手动设置 Token（替代原有的短信登录流程）
    # -----------------------------------------------------------------------
    @filter.command("zssq_token")
    async def cmd_set_token(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：/zssq_token <你的token>")
            return
        token = parts[1].strip()
        if not token:
            yield event.plain_result("token 不能为空")
            return
        # 简单校验 token 是否有效（可选：用 check_auth(token) 验证）
        cfg = _load_config(self.context)
        client = ZSSQClient(cfg["zssq_api_base"])
        valid = False
        uid = ""
        try:
            check = await client.check_auth(token=token)
            if check.get("success"):
                valid = True
                uid = check.get("uid", "")
        except Exception:
            pass
        finally:
            await client.close()

        if valid:
            self.store.save(user_id, token, uid)
            yield event.plain_result(f"Token 验证通过，已保存。UID: {uid}")
        else:
            yield event.plain_result("提供的 token 似乎无效，请检查后重试。")

    # -----------------------------------------------------------------------
    # 指令3：查询账号信息
    # -----------------------------------------------------------------------
    @filter.command("zssq_info")
    async def cmd_info(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        user = self.store.get(user_id)
        if not user:
            yield event.plain_result("您尚未设置 token，请使用 /zssq_token 设置。")
            return

        cfg = _load_config(self.context)
        client = ZSSQClient(cfg["zssq_api_base"])
        try:
            info = await client.get_account_info(user["token"])
        finally:
            await client.close()

        if not info["success"]:
            yield event.plain_result(f"查询失败：{info['msg']}")
            return

        msg = (
            f"昵称：{info['nickname']}\n"
            f"金币：{info['coin']}\n"
            f"余额：{info['balance']}\n"
            f"等级：{info['level']}"
        )
        yield event.plain_result(msg)

    # -----------------------------------------------------------------------
    # 指令4：同步到青龙面板
    # -----------------------------------------------------------------------
    @filter.command("zssq_sync")
    async def cmd_sync(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        user = self.store.get(user_id)
        if not user:
            yield event.plain_result("您尚未设置 token，请使用 /zssq_token 设置。")
            return

        cfg = _load_config(self.context)
        ql_url = cfg.get("qinglong_url", "")
        ql_client_id = cfg.get("qinglong_client_id", "")
        ql_client_secret = cfg.get("qinglong_client_secret", "")
        if not ql_url or not ql_client_id or not ql_client_secret:
            yield event.plain_result("青龙面板未配置，请在插件配置中填写相关参数。")
            return

        ql = QingLongClient(ql_url, ql_client_id, ql_client_secret)
        try:
            result = await ql.sync_env("ZSSQ_TOKEN", user["token"], f"追书神器账号{user_id}")
        finally:
            await ql.close()

        if result["success"]:
            yield event.plain_result(f"同步成功：{result['msg']}")
        else:
            yield event.plain_result(f"同步失败：{result['msg']}")

    # -----------------------------------------------------------------------
    # 指令5：账号管理（查看、删除）
    # -----------------------------------------------------------------------
    @filter.command("zssq_accounts")
    async def cmd_accounts(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if not self._is_admin(user_id):
            yield event.plain_result("仅管理员可用此命令。")
            return

        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法：/zssq_accounts list|delete QQ号")
            return

        action = parts[1].lower()
        if action == "list":
            all_users = self.store.list_all()
            if not all_users:
                yield event.plain_result("当前无任何登录账号。")
                return
            lines = []
            for qq, info in all_users.items():
                lines.append(f"QQ: {qq} | UID: {info.get('uid', '未知')} | 更新时间: {info['updated_at']}")
            yield event.plain_result("\n".join(lines))

        elif action == "delete":
            if len(parts) < 3:
                yield event.plain_result("请指定要删除的QQ号。")
                return
            target_qq = parts[2]
            if self.store.remove(target_qq):
                yield event.plain_result(f"已删除账号 {target_qq}。")
            else:
                yield event.plain_result("未找到该账号。")
        else:
            yield event.plain_result("未知操作，支持 list / delete。")

    # -----------------------------------------------------------------------
    # 指令6：白名单管理（仅管理员）
    # -----------------------------------------------------------------------
    @filter.command("zssq_whitelist")
    async def cmd_whitelist(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if not self._is_admin(user_id):
            yield event.plain_result("仅管理员可用此命令。")
            return

        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法：/zssq_whitelist add|remove|list QQ号")
            return

        action = parts[1].lower()
        cfg = _load_config(self.context)

        if action == "list":
            whitelist = cfg.get("admin_whitelist", "")
            yield event.plain_result(f"当前白名单: {whitelist if whitelist else '无'}")
        elif action in ("add", "remove"):
            if len(parts) < 3:
                yield event.plain_result("请指定QQ号。")
                return
            target_qq = parts[2]
            current = [x.strip() for x in cfg.get("admin_whitelist", "").split(",") if x.strip()]
            if action == "add":
                if target_qq not in current:
                    current.append(target_qq)
            else:
                if target_qq in current:
                    current.remove(target_qq)
                else:
                    yield event.plain_result("该QQ不在白名单中。")
                    return
            new_whitelist = ",".join(current)
            self.context.save_config({"admin_whitelist": new_whitelist})
            yield event.plain_result(f"白名单已更新: {new_whitelist}")
        else:
            yield event.plain_result("未知操作，支持 add / remove / list。")

    # -----------------------------------------------------------------------
    # 指令7：帮助
    # -----------------------------------------------------------------------
    @filter.command("zssq_help")
    async def cmd_help(self, event: AstrMessageEvent):
        help_text = (
            "追书神器插件帮助\n"
            "========\n"
            "/zssq_login - 检测登录态并提示获取 token\n"
            "/zssq_token <token> - 手动设置 token\n"
            "/zssq_info - 查看账号信息\n"
            "/zssq_sync - 同步 token 到青龙\n"
            "/zssq_accounts list|delete - 账号管理（管理员）\n"
            "/zssq_whitelist add|remove|list - 白名单管理（管理员）\n"
            "/zssq_help - 显示本帮助\n"
            "\n"
            "获得 token 的方法：\n"
            "1. 手机安装追书神器 App 并登录\n"
            "2. 电脑安装抓包软件（如 Charles），手机设置代理\n"
            "3. 操作 App 任意功能，筛选 goldcoinnew.zhuishushenqi.com 的请求\n"
            "4. 复制请求头中 Authorization: Bearer 后面的字符串，即为 token"
        )
        yield event.plain_result(help_text)

    # -----------------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------------
    async def terminate(self):
        logger.info("追书神器插件已卸载。")
