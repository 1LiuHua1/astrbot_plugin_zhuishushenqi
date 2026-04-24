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
    """封装追书神器免费版的登录与信息查询接口。"""

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

    async def request_sms_code(self, phone: str) -> Dict[str, Any]:
        """请求短信验证码。返回 {success, gt, challenge, msg}"""
        await self._ensure_session()
        url = f"{self.api_base}/user/sendsms"
        headers = {
            "User-Agent": "ZhuishuShenqi/5.0.0 (Android 12)",
            "X-Device-Id": self._device_id,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"phone": phone, "type": "login"}
        async with self.session.post(url, data=data, headers=headers) as resp:
            resp.raise_for_status()
            result = await resp.json()
            if result.get("ok"):
                gt = result.get("gt", "")
                challenge = result.get("challenge", "")
                return {"success": True, "gt": gt, "challenge": challenge, "msg": "验证码已发送"}
            return {"success": False, "msg": result.get("msg", "发送失败")}

    async def login_with_sms(self, phone: str, code: str, geetest_validate: str,
                             geetest_challenge: str, geetest_seccode: str) -> Dict[str, Any]:
        """短信登录，传入极验校验结果。返回 {success, token, uid, msg}"""
        await self._ensure_session()
        url = f"{self.api_base}/user/login"
        headers = {
            "User-Agent": "ZhuishuShenqi/5.0.0 (Android 12)",
            "X-Device-Id": self._device_id,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "phone": phone,
            "sms_code": code,
            "geetest_challenge": geetest_challenge,
            "geetest_validate": geetest_validate,
            "geetest_seccode": geetest_seccode,
        }
        async with self.session.post(url, data=data, headers=headers) as resp:
            resp.raise_for_status()
            result = await resp.json()
            if result.get("ok"):
                token = result.get("token", "")
                uid = result.get("user", {}).get("id", "")
                return {"success": True, "token": token, "uid": str(uid), "msg": "登录成功"}
            return {"success": False, "msg": result.get("msg", "登录失败")}

    async def get_account_info(self, token: str) -> Dict[str, Any]:
        """获取账号金币/余额/等级等信息。"""
        await self._ensure_session()
        url = f"{self.api_base}/account/profile"
        headers = {
            "User-Agent": "ZhuishuShenqi/5.0.0 (Android 12)",
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

    def save(self, qq: str, token: str, uid: str):
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
          "1.0.0", "支持短信登录、极验自动过验、青龙同步")
class ZhuishuShenqiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.waiting_for_code: Dict[str, dict] = {}  # qq -> {"phone","gt","challenge"}
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
    # 指令1：短信登录
    # -----------------------------------------------------------------------
    @filter.command("zssq_login")
    async def cmd_login(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if not self._is_whitelisted(user_id):
            yield event.plain_result("您没有使用此命令的权限。")
            return

        phones = event.message_str.strip().split()
        if len(phones) < 2:
            yield event.plain_result("用法：/zssq_login 手机号")
            return
        phone = phones[1]
        if not re.fullmatch(r"\d{11}", phone):
            yield event.plain_result("手机号格式不正确。")
            return

        cfg = _load_config(self.context)
        client = ZSSQClient(cfg["zssq_api_base"])
        try:
            result = await client.request_sms_code(phone)
        finally:
            await client.close()

        if not result["success"]:
            yield event.plain_result(f"发送验证码失败：{result['msg']}")
            return

        self.waiting_for_code[user_id] = {
            "phone": phone,
            "gt": result.get("gt"),
            "challenge": result.get("challenge"),
        }
        yield event.plain_result(
            f"验证码已发送至 {phone}，请在90秒内回复：/zssq_code 验证码"
        )

    # -----------------------------------------------------------------------
    # 指令2：输入验证码完成登录
    # -----------------------------------------------------------------------
    @filter.command("zssq_code")
    async def cmd_code(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        entry = self.waiting_for_code.pop(user_id, None)
        if not entry:
            yield event.plain_result("您还未发起短信登录，请先使用 /zssq_login 手机号。")
            return

        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法：/zssq_code 验证码")
            return
        code = parts[1]

        cfg = _load_config(self.context)
        appkey = cfg.get("geetest_appkey", "")
        if not appkey:
            yield event.plain_result("未配置极验 AppKey，无法自动过验证。")
            return

        try:
            geetest_result = await _get_geetest_response(
                entry["gt"], entry["challenge"], appkey, "https://app.zhuishushenqi.com/login"
            )
        except RuntimeError as e:
            yield event.plain_result(f"极验验证失败：{e}")
            return

        client = ZSSQClient(cfg["zssq_api_base"])
        try:
            login_result = await client.login_with_sms(
                entry["phone"], code,
                geetest_result["validate"],
                geetest_result["challenge"],
                geetest_result["validate"] + "|jordan",
            )
        finally:
            await client.close()

        if not login_result["success"]:
            yield event.plain_result(f"登录失败：{login_result['msg']}")
            return

        self.store.save(user_id, login_result["token"], login_result["uid"])
        yield event.plain_result(f"登录成功！UID: {login_result['uid']}")

    # -----------------------------------------------------------------------
    # 指令3：查询账号信息
    # -----------------------------------------------------------------------
    @filter.command("zssq_info")
    async def cmd_info(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        user = self.store.get(user_id)
        if not user:
            yield event.plain_result("您尚未登录，请先使用 /zssq_login 手机号。")
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
            yield event.plain_result("您尚未登录，请先使用 /zssq_login 手机号。")
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
                lines.append(f"QQ: {qq} | UID: {info['uid']} | 登录时间: {info['updated_at']}")
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
    # 生命周期
    # -----------------------------------------------------------------------
    async def terminate(self):
        logger.info("追书神器插件已卸载。")
