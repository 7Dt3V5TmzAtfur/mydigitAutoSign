#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 17 7 * * *
# new Env('数码之家签到')
"""
数码之家（www.mydigit.cn）每日自动签到 —— 青龙面板单文件版

功能:
    1. 环境变量配置，密码不落盘，可多账号
    2. 会话缓存复用，减少登录次数（缓存失效自动重登）
    3. 请求重试 + 单账号异常隔离，一个账号失败不影响其他账号
    4. 兼容青龙内置 notify 通知模块

青龙使用:
    1. 将本文件放入青龙脚本目录，或在面板「脚本管理」中新建并粘贴。
    2. 在「环境变量」中添加账号，二选一：

       A. 多账号（推荐）
          MYDIGIT_ACCOUNTS = 用户名#密码#安全提问ID#答案
          多个账号用 & 或换行分隔，例如：
          user1#pass1#0&user2#pass2#2#答案2

       B. 单账号
          MYDIGIT_USERNAME = 用户名
          MYDIGIT_PASSWORD = 密码
          MYDIGIT_QUESTION_ID = 安全提问ID（默认 0，未设置）
          MYDIGIT_ANSWER = 安全提问答案（默认空）

    3. 可选变量:
          MYDIGIT_USE_CACHE = true/false   是否复用会话缓存，默认 true
          MYDIGIT_DELAY     = 2            登录后等待秒数，默认 2
          MYDIGIT_NOTIFY    = true/false   是否推送通知，默认 true
          MYDIGIT_RETRY     = 3            请求重试次数，默认 3

安全提问 ID:
    0 未设置 | 1 母亲的名字 | 2 爷爷的名字 | 3 父亲出生的城市
    4 一位老师的名字 | 5 个人计算机型号 | 6 最喜欢的餐馆 | 7 驾照后四位

退出码:
    0 全部账号签到成功或今日已签到
    1 存在失败账号，或配置缺失

依赖: requests（青龙镜像已内置）
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:  # pragma: no cover
    print('[错误] 缺少依赖 requests，请先执行：pip install requests')
    sys.exit(1)


# ============================== 常量配置 ==============================

BASE_URL = 'https://www.mydigit.cn'
PORTAL_URL = f'{BASE_URL}/'
SIGN_PAGE = f'{BASE_URL}/k_misign-sign.html'
LOGIN_ENTRY = f'{BASE_URL}/member.php?mod=logging&action=login&handlekey=login'
LOGIN_AJAX = f'{LOGIN_ENTRY}&infloat=yes&inajax=1&ajaxtarget=fwin_content_login'

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)
TIMEOUT = (10, 20)          # (连接超时, 读取超时)
CACHE_TTL = 7 * 24 * 3600   # 会话缓存最长保留 7 天
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.mydigit_cache')

# 安全提问中文名 -> ID
QUESTION_MAP = {
    '未设置': '0', '无': '0',
    '母亲的名字': '1',
    '爷爷的名字': '2',
    '父亲出生的城市': '3',
    '您其中一位老师的名字': '4',
    '一位老师的名字': '4',
    '您个人计算机的型号': '5',
    '个人计算机的型号': '5',
    '您最喜欢的餐馆名称': '6',
    '最喜欢的餐馆名称': '6',
    '驾驶执照最后四位数字': '7',
    '驾照后四位': '7',
}

LOGIN_OK_WORDS = ('欢迎您回来', '登录成功', '现在将转入')
LOGIN_FAIL_WORDS = ('登录失败', '密码错误', '用户名或密码错误', '安全提问', '验证码', '请填写')

SIGN_DONE_WORDS = ('已经签到', '已签到', '今天已签', '明天再来', '已签过')
SIGN_OK_WORDS = ('签到成功', '恭喜', '获得', '奖励', '签到完成')


def _env_bool(name, default=True):
    value = (os.environ.get(name) or '').strip().lower()
    if not value:
        return default
    return value not in ('0', 'false', 'no', 'off')


def _env_int(name, default):
    value = (os.environ.get(name) or '').strip()
    try:
        return int(value) if value else default
    except ValueError:
        return default


USE_CACHE = _env_bool('MYDIGIT_USE_CACHE', True)
ENABLE_NOTIFY = _env_bool('MYDIGIT_NOTIFY', True)
DELAY_SECONDS = max(_env_int('MYDIGIT_DELAY', 2), 0)
RETRY_TIMES = max(_env_int('MYDIGIT_RETRY', 3), 1)


# ============================== 通用工具 ==============================

def log(message):
    print(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {message}', flush=True)


def clean_text(raw):
    """剥离 XML/CDATA/HTML 标签，得到可直接展示的文本。"""
    if not raw:
        return ''
    text = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', raw, flags=re.S)
    text = re.sub(r'<script[\s\S]*?</script>', ' ', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[\r\n\t]+', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def hidden_value(html, name):
    """提取隐藏域的 value，兼容 name/value 属性顺序颠倒。"""
    if not html:
        return None
    patterns = (
        rf'name=["\']{name}["\'][^>]*?value=["\']([^"\']*)["\']',
        rf'value=["\']([^"\']*)["\'][^>]*?name=["\']{name}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match and match.group(1):
            return match.group(1)
    return None


def normalize_question_id(value):
    value = (value or '0').strip()
    if value in QUESTION_MAP:
        return QUESTION_MAP[value]
    if value.isdigit() and 0 <= int(value) <= 7:
        return value
    log(f'  未知的安全提问「{value}」，按 0（未设置）处理')
    return '0'


class Account:
    def __init__(self, username, password, question_id='0', answer=''):
        self.username = username
        self.password = password
        self.question_id = normalize_question_id(question_id)
        self.answer = answer or ''

    def __repr__(self):
        return f'<Account {self.username}>'


class SignError(Exception):
    """业务级异常：配置、登录或签到失败。"""


# ============================== 账号解析 ==============================

def load_accounts():
    """从环境变量读取账号列表，支持多账号与单账号两种写法。"""
    accounts = []
    raw = (os.environ.get('MYDIGIT_ACCOUNTS') or '').strip()

    if raw:
        raw = raw.replace('\\n', '\n')
        for line in re.split(r'[&\n\r]+', raw):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [item.strip() for item in line.split('#', 3)]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                log(f'[警告] 账号配置格式不正确，已跳过：{line.split("#")[0]}')
                continue
            question_id = parts[2] if len(parts) > 2 and parts[2] else \
                (os.environ.get('MYDIGIT_QUESTION_ID') or '0')
            answer = parts[3] if len(parts) > 3 else \
                (os.environ.get('MYDIGIT_ANSWER') or '')
            accounts.append(Account(parts[0], parts[1], question_id, answer))
    else:
        username = (os.environ.get('MYDIGIT_USERNAME') or '').strip()
        password = (os.environ.get('MYDIGIT_PASSWORD') or '').strip()
        if username and password:
            accounts.append(Account(
                username, password,
                os.environ.get('MYDIGIT_QUESTION_ID') or '0',
                os.environ.get('MYDIGIT_ANSWER') or '',
            ))

    # 按用户名去重，避免重复签到
    unique, seen = [], set()
    for account in accounts:
        if account.username in seen:
            log(f'[警告] 重复账号已忽略：{account.username}')
            continue
        seen.add(account.username)
        unique.append(account)
    return unique


# ============================== 签到客户端 ==============================

class MyDigitClient:
    def __init__(self, account):
        self.account = account
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Referer': PORTAL_URL,
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })

    # ---------- 基础请求 ----------

    def _request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', TIMEOUT)
        last_error = None
        for attempt in range(1, RETRY_TIMES + 1):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code >= 500:
                    raise requests.HTTPError(f'HTTP {response.status_code}')
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < RETRY_TIMES:
                    wait = attempt * 2
                    log(f'  请求异常（{attempt}/{RETRY_TIMES}）：{exc}，{wait}s 后重试')
                    time.sleep(wait)
        raise SignError(f'请求失败：{url} -> {last_error}')

    def _get(self, url, **kwargs):
        return self._request('GET', url, **kwargs)

    # ---------- 会话缓存 ----------

    def _cache_path(self):
        digest = hashlib.md5(self.account.username.encode('utf-8')).hexdigest()[:16]
        return os.path.join(CACHE_DIR, f'session_{digest}.json')

    def _export_cookies(self):
        """保留 domain/path，避免恢复时同名 cookie 叠加。"""
        return [
            {
                'name': cookie.name,
                'value': cookie.value,
                'domain': cookie.domain,
                'path': cookie.path,
            }
            for cookie in self.session.cookies
            if cookie.name
        ]

    def _import_cookies(self, cookies):
        if isinstance(cookies, dict):  # 兼容旧版 name:value 缓存
            self.session.cookies.update(cookies)
            return
        for item in cookies:
            if not isinstance(item, dict) or not item.get('name'):
                continue
            self.session.cookies.set(
                item['name'],
                item.get('value', ''),
                domain=item.get('domain') or '',
                path=item.get('path') or '/',
            )

    def load_cache(self):
        if not USE_CACHE:
            return False
        path = self._cache_path()
        try:
            with open(path, 'r', encoding='utf-8') as file:
                data = json.load(file)
        except (OSError, ValueError):
            return False

        if data.get('username') != self.account.username:
            return False
        saved_at = int(data.get('saved_at') or 0)
        if time.time() - saved_at > CACHE_TTL:
            log('  会话缓存已过期')
            return False

        cookies = data.get('cookies')
        if not cookies:
            return False
        self._import_cookies(cookies)
        saved_time = datetime.fromtimestamp(saved_at).strftime('%Y-%m-%d %H:%M')
        log(f'  命中会话缓存（{saved_time}）')
        return True

    def save_cache(self):
        if not USE_CACHE:
            return
        data = {
            'username': self.account.username,
            'cookies': self._export_cookies(),
            'saved_at': int(time.time()),
        }
        path = self._cache_path()
        temp = f'{path}.tmp'
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(temp, 'w', encoding='utf-8') as file:
                json.dump(data, file, ensure_ascii=False)
            os.replace(temp, path)
            log('  登录会话已缓存')
        except OSError as exc:
            log(f'  会话缓存写入失败（不影响签到）：{exc}')

    def clear_cache(self):
        try:
            os.remove(self._cache_path())
            log('  已清除失效的会话缓存')
        except OSError:
            pass

    # ---------- 登录态 ----------

    def is_logged_in(self):
        """Discuz 登录后页头含退出链接；游客访问签到页虽返回 200 但不含该链接。"""
        try:
            response = self._get(SIGN_PAGE)
        except SignError as exc:
            log(f'  登录态检测失败：{exc}')
            return False

        if 'action=login' in response.url or 'mod=logging' in response.url:
            return False

        text = response.text
        logged_markers = ('action=logout', '欢迎您回来')
        if not any(marker in text for marker in logged_markers):
            return False
        return hidden_value(text, 'formhash') is not None

    # ---------- 登录 ----------

    def login(self):
        log('  开始登录流程')

        self._get(PORTAL_URL)
        time.sleep(1)

        form_html = self._get(LOGIN_AJAX).text
        loginhash = hidden_value(form_html, 'loginhash')
        formhash = hidden_value(form_html, 'formhash')
        if not loginhash:
            match = re.search(r'loginhash=([A-Za-z0-9]+)', form_html)
            loginhash = match.group(1) if match else None
        if not loginhash or not formhash:
            raise SignError('未能获取 loginhash/formhash，站点登录页结构可能已变更')

        submit_url = (
            f'{BASE_URL}/member.php?mod=logging&action=login&loginsubmit=yes'
            f'&handlekey=login&loginhash={loginhash}&inajax=1'
        )
        payload = {
            'formhash': formhash,
            'referer': PORTAL_URL,
            'username': self.account.username,
            'password': self.account.password,
            'questionid': self.account.question_id,
            'answer': self.account.answer,
            'cookietime': '2592000',
        }
        response = self._request('POST', submit_url, data=payload)
        message = clean_text(response.text)

        if any(word in message for word in LOGIN_FAIL_WORDS) and \
                not any(word in message for word in LOGIN_OK_WORDS):
            raise SignError(f'登录被拒绝：{message[:120] or "无返回内容"}')

        time.sleep(DELAY_SECONDS)

        if not self.is_logged_in():
            raise SignError('登录后校验失败，请检查账号密码、安全提问配置')
        log('  登录成功')
        self.save_cache()

    # ---------- 签到 ----------

    def sign(self):
        log('  进入签到页')
        page = self._get(SIGN_PAGE).text
        formhash = hidden_value(page, 'formhash')
        if not formhash:
            return 'expired', '签到页未找到 formhash，登录态可能已失效'

        sign_url = (
            f'{BASE_URL}/plugin.php?id=k_misign:sign&operation=qiandao'
            f'&formhash={formhash}&format=empty'
        )
        response = self._get(sign_url)
        message = clean_text(response.text) or '签到接口无返回文本'

        if any(word in message for word in SIGN_DONE_WORDS):
            return 'done', message
        if any(word in message for word in SIGN_OK_WORDS):
            return 'success', message
        if '请先登录' in message or '未登录' in message:
            return 'expired', message
        return 'unknown', message

    # ---------- 编排 ----------

    def run(self):
        """返回 (状态, 说明)，状态 ∈ success / done / failed。"""
        if self.load_cache() and self.is_logged_in():
            log('  缓存会话有效，免登录')
        else:
            self.clear_cache()
            self.login()

        status, message = self.sign()
        if status == 'expired':
            log('  登录态失效，重新登录后重试签到')
            self.clear_cache()
            self.login()
            status, message = self.sign()

        if status in ('success', 'done'):
            return status, message
        if status == 'expired':
            return 'failed', f'签到失败：{message}'
        return 'failed', f'签到结果未识别：{message[:160]}'


# ============================== 通知与主流程 ==============================

def push_notification(title, content):
    if not ENABLE_NOTIFY:
        return
    try:
        from notify import send  # 青龙内置通知模块
    except ImportError:
        log('未检测到 notify 模块，跳过通知推送')
        return
    try:
        send(title, content)
        log('通知已推送')
    except Exception as exc:  # noqa: BLE001 通知失败不应影响任务结果
        log(f'通知推送失败：{exc}')


def main():
    log('=' * 52)
    log('数码之家自动签到 · 青龙单文件版')
    log('=' * 52)

    accounts = load_accounts()
    if not accounts:
        log('未读取到账号配置。请设置环境变量 MYDIGIT_ACCOUNTS（用户名#密码#提问ID#答案），')
        log('或设置 MYDIGIT_USERNAME 与 MYDIGIT_PASSWORD。')
        return 1

    log(f'共 {len(accounts)} 个账号，开始执行')
    results = []

    for index, account in enumerate(accounts, 1):
        log('-' * 52)
        log(f'[{index}/{len(accounts)}] {account.username}')
        try:
            status, message = MyDigitClient(account).run()
        except SignError as exc:
            status, message = 'failed', str(exc)
        except Exception as exc:  # noqa: BLE001 单账号异常隔离
            status, message = 'failed', f'未预期异常：{exc!r}'

        label = {'success': '成功', 'done': '已签到', 'failed': '失败'}[status]
        log(f'  结果：{label} | {message[:200]}')
        results.append((account.username, label, message))

    log('=' * 52)
    log('执行汇总')
    lines = []
    for index, (username, label, message) in enumerate(results, 1):
        line = f'{index}. {username}  [{label}]  {message[:160]}'
        log(line)
        lines.append(line)

    failed = [item for item in results if item[1] == '失败']
    summary = f'成功 {len(results) - len(failed)} 个，失败 {len(failed)} 个'
    log(summary)

    push_notification('数码之家签到', '\n'.join(lines + ['', summary]))
    log('=' * 52)
    return 0 if not failed else 1


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    sys.exit(main())
