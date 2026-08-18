import re
import sys
import time
import random
import logging
from typing import Dict, Any, Tuple
from functools import wraps
import requests
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("GLaDOS")

# ==================== 配置 ====================
CHECKIN_URL = "https://glados.rocks/api/user/checkin"
STATUS_URL = "https://glados.rocks/api/user/status"
POINTS_URL = "https://glados.rocks/api/user/points"

HEADERS_BASE = {
    "origin": "https://glados.rocks",
    "referer": "https://glados.rocks/console/checkin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    # 注意：使用 requests 的 json= 参数时会自动设置 Content-Type: application/json，
    # 此处无需（也不应）手动设置 content-type，否则与 requests 默认行为重复。
}
PAYLOAD = {"token": "glados.rocks"}
TIMEOUT = (5, 15)  # (连接超时, 读取超时)

MAX_RETRY = 3
RETRY_MIN_WAIT = 2.0
RETRY_MAX_WAIT = 10.0
MIN_DELAY = 1.0
MAX_DELAY = 2.0

COOKIE_MASK_LENGTH = 10

# 前后各显示 10 个字符，因此长度必须 > 2*COOKIE_MASK_LENGTH + 3 = 23 才能安全脱敏，
# 设为 24 可避免 len∈[21,23] 时前后片段重叠导致几乎暴露完整 Cookie（M3）。
COOKIE_MIN_LENGTH = 24

# 重复签到判定关键词（L5：提升为模块级常量，便于维护/国际化）
REPEAT_KEYWORDS = ("repeat", "already", "重复", "已签到", "签到过", "请勿")



# ==================== 工具函数 ====================
def safe_json(resp: requests.Response) -> Dict[str, Any]:
    """安全解析 JSON 响应（用于推送等非关键路径，失败返回空字典）。"""
    try:
        return resp.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        return {}


def require_json(resp: requests.Response) -> Dict[str, Any]:
    """
    严格解析 JSON 响应（用于签到/状态/积分等核心请求路径）。

    - 若响应体不是合法 JSON（如网关 502 的 HTML 错误页、空响应），抛出
      requests.exceptions.RequestException，使调用方 @retry_on_failure 能捕获并重试（M1）。
    - 同时记录原始响应片段（debug），便于排查真实失败原因。
    """
    try:
        return resp.json()
    except ValueError:
        snippet = (resp.text or "<空响应>")[:200]
        logger.debug(
            "非 JSON 响应 (status=%s, content-type=%s): %s",
            resp.status_code,
            resp.headers.get("Content-Type"),
            snippet,
        )
        raise  # requests.exceptions.JSONDecodeError 同时继承 ValueError 和 RequestException，可被 is_retryable 识别


def safe_int_str(val: Any, default: str = "-") -> str:
    """安全将值转为整数字符串，失败时返回默认值"""
    try:
        return str(int(val))
    except (TypeError, ValueError):
        try:
            return str(int(float(val)))
        except (TypeError, ValueError):
            return default


def mask_email(email: str) -> str:
    """
    邮箱脱敏：保留前两个字符和最后一个字符，中间用 *** 替代
    Examples:
        mask_email("test@example.com")   -> "te***t@example.com"
        mask_email("ab@example.com")     -> "***@example.com"
        mask_email("a@example.com")      -> "***@example.com"
        mask_email("unknown")            -> "unknown"
    """
    if not email or email == "unknown" or "@" not in email:
        return email
    try:
        name, domain = email.rsplit("@", 1)
        if not name:
            return email
        if len(name) <= 3:
            masked_name = "***"
        else:
            masked_name = f"{name[:2]}***{name[-1]}"
        return f"{masked_name}@{domain}"
    except Exception:
        return email


def mask_cookie(cookie: str) -> str:
    """Cookie 脱敏（只显示前后各10个字符）。长度不足 COOKIE_MIN_LENGTH 时整体脱敏。"""
    if not cookie or len(cookie) <= COOKIE_MIN_LENGTH:
        return "***"
    return f"{cookie[:COOKIE_MASK_LENGTH]}...{cookie[-COOKIE_MASK_LENGTH:]}"


def parse_earned_points(message: str) -> int:
    """
    从签到成功响应文本中解析本次获得的积分数（H1）。

    GLaDOS 签到接口不返回 points 字段，获得积分数写在 message 中。
    兼容中英文两种文案（与 classify_checkin 的成功判定保持一致）：
      - 英文： "Checkin success, got 1 points"
      - 中文： "已经签到成功，获得 1 点，请明天继续签到哦！"
    解析失败时优雅降级为 0。
    """
    if not message:
        return 0
    # 优先匹配英文 "got N points"（新版 GLaDOS 默认返回此文案）
    m = re.search(r"got\s+(\d+)\s+points?", message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # 兼容旧版中文文案 "获得 N 点"
    m = re.search(r"获得\s*(\d+)\s*点", message)
    return int(m.group(1)) if m else 0


def validate_cookie(cookie: str) -> Tuple[bool, str]:
    """验证 Cookie 是否包含必要字段（按 ; 拆分 key 精确校验，避免子串误判）"""
    if not cookie or not cookie.strip():
        return False, "Cookie 为空"
    cookie = cookie.strip()
    keys = {part.split("=", 1)[0].strip() for part in cookie.split(";") if part.strip()}
    if "koa:sess" not in keys:
        return False, "Cookie 缺少必要字段: koa:sess"
    if "koa:sess.sig" not in keys:
        return False, "Cookie 缺少必要字段: koa:sess.sig"
    return True, ""


def is_retryable(exc: Exception) -> bool:
    """
    判断异常是否可重试（M2）。

    - 网络层异常（超时/连接错误/JSON 解析失败等 RequestException，非 HTTPError）：可重试；
    - HTTPError：仅 5xx 服务端错误可重试，4xx 客户端错误（如 Cookie 失效 401/403）不可重试；
    - 其它异常：不可重试。
    """
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", 0) if resp is not None else 0
        # 429 Too Many Requests 为限流错误，应重试（默认指数退避即可）
        if status == 429:
            return True
        return 500 <= status < 600
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    return False


# ==================== 签到逻辑 ====================
def retry_on_failure(max_retries: int = MAX_RETRY, min_wait: float = RETRY_MIN_WAIT, max_wait: float = RETRY_MAX_WAIT):
    """重试装饰器（指数退避，仅对可重试异常重试）"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RequestException as e:
                    last_exception = e
                    if attempt < max_retries and is_retryable(e):
                        wait_time = min(min_wait * (2 ** attempt), max_wait)
                        logger.warning("第 %d 次尝试失败: %s，%.1f秒后重试...",
                                       attempt + 1, e, wait_time)
                        time.sleep(wait_time)
                        continue
                    break  # 不可重试（如 4xx）直接退出
            raise last_exception  # type: ignore
        return wrapper
    return decorator


@retry_on_failure()
def checkin_request(session: requests.Session, headers: Dict[str, str]) -> Dict[str, Any]:
    """执行签到请求（带重试）"""
    r = session.post(CHECKIN_URL, headers=headers, json=PAYLOAD, timeout=TIMEOUT)
    r.raise_for_status()
    return require_json(r)  # 非 JSON 响应抛异常进入重试（M1）


@retry_on_failure()
def api_get(session: requests.Session, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """查询账号状态/积分（带重试）"""
    r = session.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return require_json(r)  # 非 JSON 响应抛异常进入重试（M1）


def classify_checkin(code: Any, message: str) -> str:
    """
    判断签到结果: ok / repeat / fail
    GLaDOS API: code=0 成功, code=1 已签到, 其他失败
    """
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = -2
    if code == 0:
        return "ok"
    if code == 1:
        return "repeat"          # GLaDOS 契约：code==1 即已签到，无条件（H4 根治）
    msg = (message or "").lower()
    # 使用精确正则匹配代替宽泛的 "got" 子串检查（H3），兼容 point/points
    if re.search(r"got\s+\d+\s+points?", msg):
        return "ok"
    if any(kw in msg for kw in REPEAT_KEYWORDS):
        return "repeat"
    return "fail"


def email_notice(content: str):
    sender_email = os.getenv("SENDER_EMAIL", "")
    auth_code = os.getenv("AUTH_CODE", "")
    receiver_email = os.getenv("TEST_EMAIL", "")
    message = MIMEText(content, 'plain', 'utf-8')
    message['From'] = formataddr(('Q128', sender_email))
    message['To'] = formataddr(('dawningnow', receiver_email))
    message['Subject'] = 'GLaDOS签到' 
    try:
        server = smtplib.SMTP_SSL('smtp.qq.com', 465)
        server.login(sender_email, auth_code)
        server.sendmail(sender_email, [receiver_email], message.as_string())
        server.quit()
        print("邮件发送成功！")
    except Exception as e:
        print(f"邮件发送失败 !")


def checkin_account(
    session: requests.Session,
    cookie: str,
    index: int,
) -> Dict[str, Any]:
    """执行单个账号的签到，返回账号信息字典"""
    session.cookies.clear()  # 清除上一个账号的残留 Cookie，避免串扰
    headers = {**HEADERS_BASE}
    headers["cookie"] = cookie
    email = "unknown"
    days = "-"
    total_points = "-"
    earned = 0
    status = ""
    result = "fail"

    try:
        # 1. 签到
        j = checkin_request(session, headers)
        code = j.get("code", -2)
        message = j.get("message", "")
        
        # H1：GLaDOS 不返回 points 字段，从 message 文本解析本次获得积分
        earned = parse_earned_points(message)
        result = classify_checkin(code, message)

        if result == "ok":
            status = f"✅ 成功 (+{earned}积分)"
        elif result == "repeat":
            status = "🔄 已签到"
        else:
            status = f"❌ 失败({message})"

        # 2. 查询账号状态（剩余天数、邮箱）
        try:
            s = api_get(session, STATUS_URL, headers)
            data = s.get("data") or {}
            email = data.get("email", email)
            if data.get("leftDays") is not None:
                days = f"{safe_int_str(data['leftDays'])} 天"
        except Exception as e:  # noqa: BLE001
            logger.warning("账号 %d 状态查询失败: %s", index, e)

        # 3. 查询总积分（兼容顶层 points 与 data.points 两种返回结构，#1）
        try:
            p = api_get(session, POINTS_URL, headers)
            pts = p.get("points")
            if pts is None:
                pts = (p.get("data") or {}).get("points")
            if pts is not None:
                total_points = f"{safe_int_str(pts)} 积分"
        except requests.exceptions.HTTPError as e:
            resp = getattr(e, "response", None)
            status_code = getattr(resp, "status_code", "?") if resp is not None else "?"
            logger.warning("账号 %d 积分查询失败 (HTTP %s)", index, status_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("账号 %d 积分查询失败: %s", index, e)

    except Exception as e:  # noqa: BLE001
        logger.error("账号 %d 签到异常: %s", index, e)
        status = f"❌ 异常({type(e).__name__})"
        result = "fail"

    return {
        "index": index,
        "email": mask_email(email),
        "status": status,
        "result": result,
        "total_points": total_points,
        "remaining_days": days,
    }


# ==================== 主流程 ====================
def main() -> int:
    # H2：支持 ||| 或换行(\n)或 & 分隔多账号 Cookie；推荐使用 ||| 避免与 Cookie 值冲突
    raw = os.getenv("COOKIES", "")
    cookies = [c.strip() for c in re.split(r"\|\|\||[&\n]", raw) if c.strip()]

    logger.info("检测到 %d 个账号", len(cookies))

    ok = fail = repeat = 0
    lines = []

    with requests.Session() as session:  # H1：使用上下文管理器确保连接释放
        for idx, cookie in enumerate(cookies, 1):
            is_valid, error_msg = validate_cookie(cookie)  # 验证 Cookie 格式，无效则跳过
            if not is_valid:
                logger.warning("账号 %d Cookie 格式异常: %s", idx, error_msg)
                logger.warning("Cookie 片段: %s", mask_cookie(cookie))
                fail += 1
                lines.append(f"{idx}. [无效Cookie] | ❌ 失败({error_msg}) | 总积分:- | 剩余:-")
                if idx < len(cookies):
                    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue

            logger.info("正在处理账号 %d/%d...", idx, len(cookies))
            acc = checkin_account(session, cookie, idx)

            if acc["result"] == "ok":
                ok += 1
            elif acc["result"] == "repeat":
                repeat += 1
            else:
                fail += 1
            
            line = (
                f"{acc['index']}. {acc['email']} | {acc['status']} | "
                f"总积分:{acc['total_points']} | 剩余:{acc['remaining_days']} |"
                f"ok:{ok} | repeat:{repeat} | fail: {fail}"
            )
            lines.append(line)

            # 非最后一个账号时随机延迟
            if idx < len(cookies):
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    content = "\n".join(lines)
    # 将content发给邮箱
    logger.info(content)
    email_notice(content)

    if ok == 0 and repeat == 0 and len(cookies) > 0:
        logger.error("⚠️ 全部 %d 个账号签到失败", len(cookies))
        return 1
    return 0

    
if __name__ == "__main__":
    sys.exit(main())