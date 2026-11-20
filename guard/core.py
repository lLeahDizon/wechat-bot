"""Deterministic gates, anonymous durable metadata and short in-memory context."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit,parse_qsl

DAY = 86400
PUBLIC_HOSTS = {"api.apiroute.asia", "developers.openai.com", "learn.chatgpt.com", "developer.riotgames.com", "ddragon.leagueoflegends.com", "www.leagueoflegends.com"}
PUBLIC_HOSTS |= {'lolesports.com','lpl.qq.com','lol.qq.com','gol.gg','www.gol.gg','news.17173.com'}
OUT_OF_SCOPE = "这里只回答当前 Sub2API 服务和英雄联盟知识问题，请把问题说回这两项职责。"
PRIVATE = "这项内容涉及内部信息，群内不提供。可以描述报错现象，但请勿发送密钥、账号凭据或原始日志。"
SECRET_INPUT = "消息中疑似包含密钥、地址或个人信息，本次不交给模型。请先撤回并脱敏；如已公开有效密钥，请联系管理员更换。"
FAILED = "这次答疑未能完成。请稍后重试；如持续出现，请提供发生时间和脱敏报错。"
UNSUPPORTED = "这个问题目前没有足够可靠的信息可回答，请补充Sub2API现象，或英雄联盟的英雄、位置和版本。"

_SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S{12,}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_.-]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|Cookie\s*:\s*\S{6,}|Authorization\s*:\s*(?:\S+[ \t]+)?\S{6,}|"
    r"(?:api[_ -]?key|secret|password|passwd|cookie|authorization|token|密钥|密码|口令)\s*[:=]\s*[\"']?[^\s\"']{8,}", re.I
)
_IDENTITY = re.compile(r"wxid_[A-Za-z0-9_-]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)")
_PATH = re.compile(r"/(?:etc|root|home|opt|var|run|proc|Users|tmp|srv)/[^\s`\"']+")
_IPV4 = re.compile(r"(?<![A-Za-z0-9_.])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9_.])")
_IPV6 = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Fa-f0-9]{0,4}:){2,}[A-Fa-f0-9:]{0,39}(?![A-Za-z0-9_])")
_URL = re.compile(r"https?://[^\s<>\"')\]，。；]+", re.I)
_PRIVATE_REQUEST = re.compile(
    r"私钥|管理(?:员)?(?:密码|凭据|密钥)|系统提示词|system\s*prompt|"
    r"(?:显示|提供|发我|给我|告诉我|读取|列出|获取).{0,12}(?:源站|真实\s*IP|服务器\s*IP|SSH.{0,4}地址|上游(?:账号|帐号|身份|凭据)|密码|密钥|cookie|token)|"
    r"(?:源站|真实\s*IP|服务器\s*IP).{0,8}(?:发我|告诉我|给我|是什么|多少)|"
    r"(?:其他|别人|所有|全体|每个).{0,8}(?:账单|余额|密钥|账号)|"
    r"(?:打印|输出|读取|显示|提供|发我).{0,10}(?:环境变量|原始日志)|"
    r"(?:配置文件|日志).{0,6}(?:全文|完整|原文|全部)|"
    r"忽略.{0,12}(?:规则|指令|限制)|<\s*(?:system|developer)\s*>|"
    r"rm\s+-|docker\s+(?:restart|stop|start|exec|rm|update)|sudo\s|"
    r"(?:帮我|请|直接|立即|马上).{0,8}(?:重启|修改配置|删除账号|刷新token|清除错误)|"
    r"(?:重启|修改配置|删除账号|刷新token|清除错误)(?:一下|吧|服务|sub2api)|"
    r"(?:执行|运行).{0,6}(?:shell|sql|脚本)", re.I
)
_OBVIOUS_OTHER = re.compile(
    r"(?:写|编|讲|生成).{0,8}(?:诗|笑话|故事|作文|小说|情书|代码)|"
    r"天气|股票|彩票|算命|星座|相亲攻略|旅游攻略|骂.{0,4}(?:人|他|她)|"
    r"(?:\d+\s*[+×*/]\s*\d+)|(?:翻译|translate).{0,10}", re.I
)
_RELATED = re.compile(
    r"sub\s*2\s*api|本服务|这个服务|这个接口|api|codex|claude|gpt|模型|推理|密钥|key|"
    r"reconnect|断流|重连|连接|超时|timeout|报错|错误|额度|用量|计费|余额|映射|"
    r"配置|代理|clash|stream|401|403|429|502|503|图片生成|视频生成|"
    r"cpu|内存|磁盘|负载|健康|诊断|监控|告警|caddy|postgres|redis|队列|并发|延迟|耗时|"
    r"ttfb|http|日志|服务|容器|docker|网络|丢包|tls|证书|上游|请求|缓存|tokens?", re.I
)
_DYNAMIC = re.compile(r"查询|检查|诊断|监控|告警|状态|cpu|内存|磁盘|负载|redis|postgres|并发|延迟|耗时|用量|tokens?|日志|上游|映射|网络|tls|连接|reconnect|超时|防火墙|SSH|端口|漏洞|入侵|备份|服务器.{0,8}风险",re.I)
_CORRECTION = re.compile(r"补充|纠正|不是|更正|新的|新报错|变了|仍然|还是|不对|但是|另外|现在|刚刚|重试后")
_OTHER_USAGE = re.compile(r'(?:其他|别人|所有|全体|每个).{0,8}用量')
_PRIVATE_DATA = re.compile(
    r'私钥|管理(?:员)?(?:密码|凭据|密钥)|系统提示词|system\s*prompt|'
    r'(?:显示|提供|发我|给我|告诉我|读取|列出|获取).{0,12}(?:源站|真实\s*IP|服务器\s*IP|SSH.{0,4}地址|上游(?:账号|帐号|身份|凭据)|密码|密钥|cookie|token)|'
    r'(?:源站|真实\s*IP|服务器\s*IP).{0,8}(?:发我|告诉我|给我|是什么|多少)|'
    r'(?:其他|别人|所有|全体|每个).{0,8}(?:账单|余额|密钥|账号)|'
    r'(?:打印|输出|读取|显示|提供|发我).{0,10}(?:环境变量|原始日志)|(?:配置文件|日志).{0,6}(?:全文|完整|原文|全部)|'
    r'忽略.{0,12}(?:规则|指令|限制)|<\s*(?:system|developer)\s*>',re.I)
_SERVICE_TOPIC=re.compile(r'sub\s*2\s*api|本服务|这个服务|本服务器|当前服务器|这台服务器|服务器|主机|caddy|postgres|redis|防火墙|SSH登录|端口暴露|'
    r'查(?:询|一下)?.{0,6}(?:cpu|内存|磁盘|负载|容器|日志|上游|并发|延迟|用量)|codex.{0,10}(?:报错|断流|重连|reconnect)|我的.{0,5}(?:key|token|用量)',re.I)
_SERVER_WRITE=re.compile(r'(?:重启|删库|删除.{0,8}(?:账号|文件|目录|数据)|删掉|写入|修改配置|刷新token|清除错误|停止服务|执行.{0,4}(?:shell|sql|脚本))',re.I)
_LOL_FOLLOW=re.compile(r'英雄|对线|出装|符文|团战|这个位置|这场|这两队|BP|^(?:那|继续|详细一点|为什么|怎么说|具体呢)',re.I)
_LOL_TOPIC=re.compile(r'英雄联盟|(?<![A-Za-z0-9])(?:LOL|LPL|LCK|LEC|LCS|LCP|MSI|Worlds)(?![A-Za-z0-9])|世界赛|季中赛|先锋赛',re.I)
_GAME_HINT=re.compile(r'英雄|对线|出装|符文|技能|怎么玩|打野|中单|上单|下路|辅助|团战|阵容',re.I)
_GENERAL_TOPIC=re.compile(r'原理|概念|教程|区别|设计|实现|推荐|评测|选购|怎么买|是什么|讲讲')
_EXPLICIT_SERVICE=re.compile(r'sub\s*2\s*api|本服务|这个服务|本服务器|当前服务器|这台服务器|我的.{0,5}(?:key|token|用量)',re.I)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if c in "\n\t" or (unicodedata.category(c)[0] != "C" and c not in "\u2028\u2029"))


def digest(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


_SAFE_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def safe_failure_code(value):
    if not isinstance(value, str):
        return "other_error"
    value = value.lower()
    return value if _SAFE_FAILURE_CODE.fullmatch(value) else "other_error"


def mask_labels(text,rules):
    text=normalize(text);hidden=set()
    for rule in rules[:8]:
        size=rule.get('length');hashed=rule.get('digest')
        if type(size) is not int or not 1<=size<=32 or not isinstance(hashed,str):continue
        for offset in range(max(0,len(text)-size+1)):
            if hashlib.sha256(text[offset:offset+size].casefold().encode()).hexdigest()==hashed:
                hidden.update(range(offset,offset+size))
    result=[];previous=False
    for index,char in enumerate(text):
        if index in hidden:
            if not previous:result.append('[名称已隐藏]')
            previous=True
        else:result.append(char);previous=False
    return ''.join(result)


def unsafe(text: str, allowed_urls=()) -> bool:
    """Fail closed for secrets/identifiers. Never report the matched value."""
    text = normalize(text)
    allowed_urls = set(allowed_urls or ())
    if _SECRET.search(text) or _IDENTITY.search(text) or _PATH.search(text):
        return True
    for candidate in _IPV4.findall(text) + _IPV6.findall(text):
        try:
            ipaddress.ip_address(candidate)
            return True
        except ValueError:
            continue
    for url in _URL.findall(text):
        if url in allowed_urls:
            continue
        try:
            parts = urlsplit(url)
            if parts.hostname not in PUBLIC_HOSTS or parts.username or parts.password or parts.port not in {None,443} or parts.fragment:
                return True
            if parts.query:
                params=parse_qsl(parts.query,keep_blank_values=True)
                allowed=False
                if parts.hostname in {'lpl.qq.com','lol.qq.com'} and parts.path.lower().endswith(('.html','.shtml')):
                    allowed=bool(params) and len(params)<=5 and all((k.lower() in {'gameid','bmatchid','nid','docid','id','newsid','articleid'} and bool(re.fullmatch(r'\d{1,20}',v))) or (k.lower()=='tabid' and v in {'main','schedule','data','news'}) for k,v in params)
                if not allowed:return True
            if any(x in parts.path.lower() for x in ("/admin", "/login", "/oauth", "/callback")):
                return True
        except ValueError:
            return True
    return False


def redact_question(text):
    if _SECRET.search(text):return None
    text=_URL.sub(lambda m:'[链接已隐藏]' if unsafe(m.group()) else m.group(),text)
    text=_IDENTITY.sub('[身份信息已隐藏]',text)
    text=_PATH.sub('[内部路径已隐藏]',text)
    def address(match):
        try:ipaddress.ip_address(match.group());return '[地址已隐藏]'
        except ValueError:return match.group()
    text=_IPV4.sub(address,text);text=_IPV6.sub(address,text)
    return None if unsafe(text) else text


def without_reference_footer(text):
    return '\n'.join(line for line in text.splitlines()
        if not re.match(r'^\s*(?:[-*]\s*)?(?:(?:参考来源|参考资料|参考链接|资料来源|来源|抓取时间|sources?)\s*[:：]|\[\d+\]\s*[:：]\s*https?://)',line,re.I)).strip()


def public_snapshot(path: Path, now: float) -> dict:
    try:
        source = json.loads(path.read_text())
        stamp = float(source["captured_at"])
        if not now - 120 <= stamp <= now + 10:
            return {"service": "unknown", "fresh": False}
        service = source.get("service")
        if service not in {"healthy", "degraded", "unavailable", "unknown"}:
            raise ValueError
        pressure = source.get("pressure", "unknown")
        if pressure not in {"normal", "elevated", "unknown"}:
            raise ValueError
        return {"service": service, "pressure": pressure, "fresh": True, "captured_at": stamp}
    except (OSError, ValueError, KeyError, TypeError):
        return {"service": "unknown", "fresh": False}


@dataclass(frozen=True)
class Incoming:
    group: str
    sender: str
    message_id: str
    text: str
    mentioned: bool
    timestamp: float
    from_self: bool = False


@dataclass
class Decision:
    reply: str | None = None
    category: str = "ignored"
    model_input: dict | None = None
    subject: str = ""
    fingerprint: str = ""
    status_hash: str = ""
    reference: str = ""
    question: str = ""
    usage: dict | None = None
    learning: str = ""
    tone: str = 'default'
    usage_request: dict | None = None
    evidence_ids: set[str] = field(default_factory=set)
    public_urls: set[str] = field(default_factory=set)
    contains_personal_usage: bool = False
    intent: str = ''
    token_unit: str = 'auto'
    feedback_origin: str = ''
    feedback_question: dict | None = None
    admin_request: dict | None = None
    admin_job: str = ''


class Engine:
    def __init__(self, config: dict, faq: list[dict], state_path: Path, snapshot_path: Path, clock=time.time, league_catalog=None):
        self.config, self.faq, self.clock = config, faq, clock
        self.snapshot_path = snapshot_path
        self.contexts: dict[str, tuple[float, list[tuple[str, str]]]] = {}
        self.last_domains = {}
        self.league_catalog = league_catalog or {}
        self.feedback_clarifications = {}
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state_path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, state TEXT NOT NULL, at REAL NOT NULL, failure_code TEXT);
            CREATE TABLE IF NOT EXISTS questions(subject TEXT, fingerprint TEXT, status_hash TEXT, ref TEXT, at REAL,
              PRIMARY KEY(subject,fingerprint,status_hash));
            CREATE TABLE IF NOT EXISTS irrelevant(subject TEXT PRIMARY KEY, count INTEGER, at REAL);
            CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY, requests INTEGER, input_tokens INTEGER, output_tokens INTEGER);
            CREATE TABLE IF NOT EXISTS knowledge(topic TEXT, note TEXT, at REAL, UNIQUE(topic,note));
        """)
        delivery_columns = {row[1] for row in self.db.execute("PRAGMA table_info(deliveries)")}
        if "failure_code" not in delivery_columns:
            self.db.execute("ALTER TABLE deliveries ADD COLUMN failure_code TEXT")
        from .improvements import initialize
        initialize(self.db,self.faq,self.clock(),recover=True)
        from .reminders import Reminders
        self.reminders=Reminders(self.db,self.clock)
        self.db.commit()

    def purge(self) -> None:
        now = self.clock()
        self.contexts = {k: v for k, v in self.contexts.items() if now - v[0] < DAY}
        self.last_domains = {k:v for k,v in self.last_domains.items() if now-v[0]<DAY}
        self.db.execute("DELETE FROM questions WHERE at < ?", (now - DAY,))
        self.db.execute("DELETE FROM irrelevant WHERE at < ?", (now - DAY,))
        # Technical replay metadata only; no message bodies are stored here.
        self.db.execute("DELETE FROM deliveries WHERE at < ?", (now - 7 * DAY,))
        self.db.execute("DELETE FROM usage WHERE day < date('now','-7 days')")
        from .improvements import prune,synchronize,reject
        for subject,state in list(self.feedback_clarifications.items()):
            if now-state['at']>=DAY:
                reject(self.db,subject,state['fingerprint'],'clarification_expired',now)
                self.db.execute('DELETE FROM improvement_open WHERE subject=?',(subject,))
                del self.feedback_clarifications[subject]
        synchronize(self.db,self.faq,now)
        prune(self.db,now)
        self.db.commit()

    def claim(self, event: Incoming) -> bool:
        key = digest(event.group, event.message_id)
        try:
            self.db.execute("INSERT INTO deliveries(id,state,at) VALUES (?, 'pending', ?)", (key, self.clock()))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_delivery(self, event: Incoming, state: str) -> None:
        if state not in {"replied", "no_reply", "needs_review", "failed"}:
            state = "needs_review"
        self.db.execute("UPDATE deliveries SET state=? WHERE id=?", (state, digest(event.group, event.message_id)))
        self.db.commit()

    def record_model_failure(self, event: Incoming, intent: str, code: str) -> bool:
        if intent != "investment":
            return False
        try:
            cursor = self.db.execute("UPDATE deliveries SET failure_code=? WHERE id=?",
                (safe_failure_code(code), digest(event.group, event.message_id)))
            self.db.commit()
            return cursor.rowcount == 1
        except sqlite3.Error:
            self.db.rollback()
            return False

    def retort(self, subject: str, kind: str, tone='default') -> str:
        if tone=='gentle':
            return ('这个话题超出职责范围啦，我们回到Sub2API或英雄联盟问题，好吗？' if kind=='off_topic' else
                    '这个问题刚才回复过啦，我再把要点放在下面；有新情况的话，补充一下就好。')
        if tone=='sharp':
            return ('提醒过还跑题，别继续原地加戏。把Sub2API或英雄联盟问题说清楚再来。' if kind=='off_topic' else
                    '答案就在上面，先看完再问。反复复制同一个问题，不叫排查，叫复读。')
        bank = self.config["retorts"][kind]
        index = int(subject[:8], 16) % len(bank)
        return bank[index]

    def irrelevant(self, decision: Decision) -> Decision:
        now = self.clock()
        row = self.db.execute("SELECT count,at FROM irrelevant WHERE subject=?", (decision.subject,)).fetchone()
        count = (row[0] if row and now - row[1] < DAY else 0) + 1
        self.db.execute("INSERT OR REPLACE INTO irrelevant VALUES (?,?,?)", (decision.subject, count, now))
        self.db.commit()
        decision.reply = (('这里主要帮你处理本服务的问题哦，换成Sub2API相关问题我再帮你看。' if decision.tone=='gentle' else OUT_OF_SCOPE)
                          if count == 1 else self.retort(decision.subject, "off_topic",decision.tone))
        decision.category = "off_topic"
        decision.question = ""
        return decision

    def prepare(self, event: Incoming) -> Decision:
        now = self.clock()
        self.purge()
        group = self.config.get("group_id", "")
        if not self.config.get("enabled") or not group or group == "UNCONFIGURED":
            return Decision()
        if (event.group != group or not event.mentioned
                or event.from_self or not event.sender or not event.message_id):
            return Decision()
        if not now - 300 <= event.timestamp <= now + 30:
            return Decision()
        text = normalize(event.text).strip()
        acceptance_prefix = self.config.get('acceptance_prefix', '')
        if acceptance_prefix:
            if not isinstance(acceptance_prefix, str) or not text.startswith(acceptance_prefix):
                return Decision()
            text = text[len(acceptance_prefix):].strip()
        if not self.claim(event):
            return Decision()
        decision = Decision(subject=digest(group, event.sender))
        profile=self.config.get('member_profiles',{}).get(decision.subject,{})
        decision.tone=profile.get('tone') if profile.get('tone') in {'sharp','gentle'} else 'default'
        from .owner import prepare as owner_prepare
        owner=owner_prepare(text,decision.subject,group,self.config,event.message_id)
        if owner:
            decision.reply=owner.get('reply');decision.admin_request=owner.get('request');decision.category='owner_operation'
            return decision
        if self.config.get('feedback_enabled'):
            from .improvements import feedback_trigger,clarification_choice,active_preferences
            state=self.feedback_clarifications.get(decision.subject)
            if feedback_trigger(text) or (state and clarification_choice(text,state) is not None):decision.intent='feedback'
            if state and decision.intent=='feedback':decision.feedback_origin=state['fingerprint']
            decision.fingerprint=digest(re.sub(r'[\W_]+','',text).casefold())
            decision.token_unit=active_preferences(self.db).get('token_unit','auto')
        if not text:
            return decision
        if len(text) > 4000:
            if decision.intent=='feedback':return self.reject_feedback(decision,'oversize')
            decision.reply, decision.category = "问题较长，请先概括现象和脱敏报错，分清一个具体问题。", "oversize"
            return decision
        if _SECRET.search(text):
            if decision.intent=='feedback':return self.reject_feedback(decision,'safety')
            decision.reply, decision.category = SECRET_INPUT, "sensitive_input"
            return decision
        general=bool(self.config.get('general_enabled'))
        from .account_quota import privacy_text,RESET_ACTION
        if self.config.get('diagnostics_enabled') and RESET_ACTION.search(text) and re.search(r'账号|帐号|上游|额度|配额',text):
            decision.reply='群内只查询额度与重置机会，不执行额度重置或消耗重置次数。';decision.category='write_disabled'
            return decision
        if general and _SERVER_WRITE.search(text) and _SERVICE_TOPIC.search(text):
            decision.reply='群内服务器管理仅开放只读排查，不执行写入、删除、重启或配置变更。';decision.category='write_disabled'
            return decision
        if (_PRIVATE_DATA if general else _PRIVATE_REQUEST).search(privacy_text(text)):
            if decision.intent=='feedback':return self.reject_feedback(decision,'safety')
            decision.reply, decision.category = ('这部分涉及内部信息，暂时不能在群内提供哦。你可以补充脱敏现象，我会继续帮你看。' if decision.tone=='gentle' else PRIVATE), "private"
            return decision
        if _OTHER_USAGE.search(text):
            aliases=self.config.get('usage_public_aliases',{})
            named=any(name in text for names in aliases.values() for name in names)
            if not self.config.get('diagnostics_enabled') or not named:
                if decision.intent=='feedback':return self.reject_feedback(decision,'safety')
                decision.reply=PRIVATE if not self.config.get('diagnostics_enabled') else '目前只授权查询example_person_a和example_person_b的Token用量，请指明查哪位。'
                decision.category='private' if not self.config.get('diagnostics_enabled') else 'usage_clarification'
                return decision
        web_urls=[]
        if general:
            from .public_web import public_url
            def link(match):
                try:web_urls.append(public_url(match.group()))
                except (ValueError,UnicodeError):return '[链接已隐藏]'
                return '[待核实的公开链接]'
            text=_URL.sub(link,text)
        if unsafe(text):
            redacted=redact_question(text) if self.config.get('diagnostics_enabled') else None
            if redacted is None:
                if decision.intent=='feedback':return self.reject_feedback(decision,'safety')
                decision.reply,decision.category=SECRET_INPUT,'sensitive_input'
                return decision
            text=redacted
        text=mask_labels(text,self.config.get('hidden_label_rules',[]))
        reminder=self.reminders.request(text,decision.subject,digest(group),digest(group,event.message_id),self.config)
        if reminder is not None:
            decision.reply=reminder;decision.category='reminder';decision.intent='reminder'
            return decision
        snapshot = public_snapshot(self.snapshot_path, now)
        decision.status_hash = digest(json.dumps({k: v for k, v in snapshot.items() if k != "captured_at"}, sort_keys=True))
        decision.fingerprint = digest(re.sub(r"[\W_]+", "", text).casefold())
        decision.question = text
        if self.config.get('feedback_enabled'):
            from .improvements import feedback_trigger,cached,acknowledgement,listing,clarification_choice,clarification_prompt,reject,summary_of
            if re.fullmatch(r'(?:查看)?(?:建议列表|反馈列表|改进记录)[?？。!！\s]*',text):
                decision.reply=listing(self.db);decision.category='feedback_list'
                return decision
            state=self.feedback_clarifications.get(decision.subject)
            if state:
                selected=clarification_choice(text,state)
                if selected is False:return self.reject_feedback(decision,'unclear')
                if selected:
                    self.feedback_clarifications.pop(decision.subject,None)
                    self.db.execute('DELETE FROM improvement_open WHERE subject=?',(decision.subject,));self.db.commit()
                    decision.intent='feedback';decision.category='feedback';decision.feedback_origin=state['fingerprint']
                    decision.model_input={'intent':'feedback_verify_only','question':state['question']+'\n原提议者补充：'+text+'\n所选方案：'+summary_of(selected),
                        'candidate':selected,'facts':[{'id':x['id'],'answer':x['answer']} for x in self.faq],
                        'context':[],'diagnostic':False,'tone':decision.tone}
                    return decision
                if feedback_trigger(text):
                    if decision.fingerprint==state['fingerprint']:
                        decision.reply=clarification_prompt(state,decision.tone);decision.category='feedback_clarification'
                        return decision
                    reject(self.db,decision.subject,state['fingerprint'],'clarification_interrupted',now)
                    self.feedback_clarifications.pop(decision.subject,None)
                    self.db.execute('DELETE FROM improvement_open WHERE subject=?',(decision.subject,));self.db.commit()
                    decision.feedback_origin=''
            if feedback_trigger(text):
                if _OBVIOUS_OTHER.search(text) and not general:return self.reject_feedback(decision,'unrelated')
                orphan=self.db.execute('SELECT fingerprint FROM improvement_open WHERE subject=?',(decision.subject,)).fetchone()
                if orphan and decision.subject not in self.feedback_clarifications:
                    reject(self.db,decision.subject,orphan[0],'clarification_interrupted',now)
                    self.db.execute('DELETE FROM improvement_open WHERE subject=?',(decision.subject,));self.db.commit()
                previous=cached(self.db,decision.subject,decision.fingerprint)
                decision.intent='feedback';decision.category='feedback'
                if previous:
                    decision.reply=acknowledgement(*previous,tone=decision.tone,duplicate=True)
                    return decision
                decision.model_input={'question':text,'context':[],
                    'facts':[{'id':x['id'],'answer':x['answer']} for x in self.faq],
                    'intent':'feedback','diagnostic':False,'tone':decision.tone}
                return decision
        if general:
            from .investment import references_other_personal_data
            from .investment import investment_capability_answer, investment_capability_question
            from .investment import related as investment_related
            follow=(self.last_domains.get(decision.subject,(0,''))[1]=='investment'
                and len(text)<120 and bool(re.match(r'^(?:那|继续|具体|还有|另外|风险|估值|为什么|怎么|这些|上述|再看|补充)',text))
                and not _OBVIOUS_OTHER.search(text))
            if investment_related(text) and references_other_personal_data(text,
                    [alias for target,names in self.config.get('usage_public_aliases',{}).items()
                     if target!=profile.get('usage_target') for alias in names if isinstance(alias,str)]):
                decision.intent='investment';decision.reply='为保护其他群成员的个人财务信息，我只使用提问者本人明确提供的持仓资料；请改为分析你自己的情况或公开公司数据。'
                decision.category='investment_privacy';return decision
            if investment_capability_question(text):
                decision.intent='investment';decision.reply=investment_capability_answer()
                decision.category='investment_capability';return decision
            if investment_related(text) or follow:
                return self.investment_question(decision,text,web_urls)
        if self.config.get('lol_enabled'):
            from .league import related,OTHER_MODE
            from .esports import related as esports_related,OTHER_SPORT
            terms=self.league_catalog.get('item_names',[])+self.league_catalog.get('rune_names',[])
            esports=esports_related(text)
            game=related(text,self.league_catalog.get('champions',{}),terms) or esports
            if general:
                from .predictions import teams_from
                esports=bool(esports and (_LOL_TOPIC.search(text) or teams_from(text)))
                game=bool(_LOL_TOPIC.search(text) or esports or game and _GAME_HINT.search(text))
            if self.config.get('feedback_enabled'):
                from .improvements import faq_match
                known=faq_match(self.db,text,self.faq)
                game=game or bool(known and known['id'].startswith('lol_'))
            reserved=re.fullmatch(r'(?:/帮助|帮助|你能做什么|你是谁|使用说明|/状态|状态|服务状态)[?？。!！\s]*',text)
            follow=(self.last_domains.get(decision.subject,(0,''))[1]=='lol' and not _RELATED.search(text) and not _OBVIOUS_OTHER.search(text) and not reserved)
            if general:follow=follow and bool(_LOL_FOLLOW.search(text))
            if OTHER_SPORT.search(text) and not re.search(r'英雄联盟|\blol\b|\blpl\b|\blck\b',text,re.I):follow=False
            if game or follow:
                if general and re.search(r'版本更新|更新了|补丁|更新公告|改动公告',text):return self.general_question(decision,text,web_urls)
                decision.intent='lol'
                history=self.contexts.get(decision.subject+':lol',(0,[]))[1]
                esports=esports or bool(not game and follow and history and esports_related(history[-1][0]))
                decision.status_hash=digest('lol',self.league_catalog.get('version','unknown'),str(int(now//(300 if esports else DAY))))
                if OTHER_MODE.search(text) and not general:
                    decision.reply='目前英雄联盟资料按端游整理；手游、云顶之弈和金铲铲暂无对应版本资料。端游问题可以直接说明英雄、位置和具体困惑。'
                    decision.category='lol_clarification';return decision
                previous=self.db.execute('SELECT ref FROM questions WHERE subject=? AND fingerprint=? AND status_hash=?',
                    (decision.subject,decision.fingerprint,decision.status_hash)).fetchone()
                if previous and not _CORRECTION.search(text):
                    answer=next((a for q,a in reversed(history) if q==text),'这个问题前面已回答；请补充哪个步骤或现象仍不清楚。')
                    decision.reply=(self.retort(decision.subject,'repeat',decision.tone)+'\n'+answer)[:1500]
                    decision.category='repeat';return decision
                for entry in self.faq:
                    if entry['id'].startswith('lol_') and any(re.fullmatch(p,text,re.I) for p in entry.get('direct',[])):
                        decision.reply=entry['answer'];decision.category='faq';decision.reference=entry['id'];return decision
                if self.config.get('feedback_enabled'):
                    from .improvements import faq_match
                    learned=faq_match(self.db,text,self.faq)
                    if learned and learned['id'].startswith('lol_'):
                        decision.reply=learned['answer'];decision.category='faq';decision.reference=learned['id'];return decision
                if general and OTHER_MODE.search(text):return self.general_question(decision,text,web_urls)
                decision.category='model'
                decision.model_input={'domain':'lol','intent':'lol','esports':esports,'question':text,'diagnostic':False,'tone':decision.tone,
                    'context':[{'question':q,'answer':a} for q,a in history[-2:]],
                    'facts':[{'id':x['id'],'answer':x['answer']} for x in self.faq if x['id'].startswith('lol_')]}
                if self.config.get('feedback_enabled'):
                    from .improvements import active_preferences,active_guidance
                    preferences=active_preferences(self.db)
                    decision.model_input['response_preferences']={k:v for k,v in preferences.items() if k in {'length','layout'}}
                    decision.model_input['service_guidance']=active_guidance(self.db,'lol')
                return decision
        if re.fullmatch(r"(?:/帮助|帮助|你能做什么|你是谁|使用说明)[?？。!！\s]*", text):
            decision.reply, decision.category = self.config["help"], "help"
            decision.reference = "scope"
            return decision
        if re.fullmatch(r"(?:现在|当前)?(?:/状态|服务状态|状态|服务正常吗|服务挂了吗|服务器正常吗|服务器挂了吗)[?？。!！\s]*", text):
            labels = {"healthy": "正常", "degraded": "部分异常", "unavailable": "暂不可用", "unknown": "尚不能确认"}
            decision.reply = "服务状态：" + labels[snapshot["service"]]
            if snapshot["fresh"]:
                decision.reply += "。检查时间：" + time.strftime("%m-%d %H:%M:%S", time.localtime(snapshot["captured_at"]))
            else:
                decision.reply += "，当前没有新鲜的诊断快照。"
            decision.reply += "。仅代表基础健康检查，不保证所有上游请求正常；此检查不调用模型。"
            decision.category, decision.reference = "status", "status"
            return decision
        if _OBVIOUS_OTHER.search(text) and not general:
            return self.irrelevant(decision)
        if general:
            from .general import question_review_request
            review_text=question_review_request(text)
            if review_text is not None:
                if not review_text:
                    decision.reply='请把要评价的问题原文粘贴或引用出来；我只点评问题本身，不评价提问者。'
                    decision.category='question_review_clarification';return decision
                for aliases in self.config.get('usage_public_aliases',{}).values():
                    for alias in aliases:
                        if isinstance(alias,str) and alias:review_text=review_text.replace(alias,'[群成员]')
                review_text=mask_labels(review_text,self.config.get('hidden_label_rules',[]))
                decision.intent='question_review';decision.tone='default';decision.category='question_review'
                decision.model_input={'domain':'general','phase':'question_review','question':review_text,
                    'context':[],'facts':[],'web_urls':[],'diagnostic':False,'tone':'default'}
                return decision
        usage=clarification=None
        if self.config.get('diagnostics_enabled'):
            from .account_quota import request_for as quota_request
            quota,quota_error,detailed=quota_request(text,
                follow=self.last_domains.get(decision.subject,(0,''))[1]=='account_quota',
                aliases=self.config.get('usage_public_aliases'),bound_member=bool(profile.get('usage_target')))
            if quota or quota_error:
                decision.intent='account_quota';decision.usage_request=quota;decision.reply=quota_error
                decision.category='usage' if quota else 'usage_clarification'
                if quota and detailed:
                    history=self.contexts.get(decision.subject+':account_quota',(0,[]))[1]
                    decision.model_input={'domain':'account_quota','phase':'account_quota_advice','question':text,
                        'context':[{'question':q,'answer':a} for q,a in history[-2:]],'facts':[],
                        'diagnostic':False,'tone':decision.tone}
                return decision
            from .usage import request_for
            usage,clarification=request_for(text,profile,self.config.get('usage_public_aliases'))
        if usage or clarification:
            decision.usage_request=usage
            decision.reply=clarification
            decision.category='usage' if usage else 'usage_clarification'
            return decision
        row = self.db.execute("SELECT ref FROM questions WHERE subject=? AND fingerprint=? AND status_hash=?",
            (decision.subject, decision.fingerprint, decision.status_hash)).fetchone()
        if row and not _CORRECTION.search(text) and not (self.config.get('diagnostics_enabled') and _DYNAMIC.search(text)):
            previous = next((x["answer"] for x in self.faq if x["id"] == row[0]), "请看前面已给出的答案；有新现象请补充具体变化。")
            decision.reply = self.retort(decision.subject, "repeat",decision.tone) + "\n" + previous
            decision.category, decision.reference = "repeat", row[0]
            return decision
        for entry in self.faq:
            if any(re.fullmatch(pattern, text, re.I) for pattern in entry.get("direct", [])):
                decision.reply, decision.category, decision.reference = entry["answer"], "faq", entry["id"]
                return decision
        if self.config.get('feedback_enabled'):
            from .improvements import faq_match
            learned=faq_match(self.db,text,self.faq)
            if learned:
                decision.reply,decision.category,decision.reference=learned['answer'],'faq',learned['id']
                return decision
        service_follow=(self.last_domains.get(decision.subject,(0,''))[1]=='service' and len(text)<120 and bool(re.match(r'那|还|仍|继续|为什么|补充|纠正|现在还是',text)))
        general_topic=bool(_GENERAL_TOPIC.search(text) and not _EXPLICIT_SERVICE.search(text))
        if general and (not (_SERVICE_TOPIC.search(text) or service_follow) or _OBVIOUS_OTHER.search(text) or general_topic):
            return self.general_question(decision,text,web_urls)
        history = self.contexts.get(decision.subject, (0, []))[1]
        if not _RELATED.search(text) and not history:
            if general:return self.general_question(decision,text,web_urls)
            return self.irrelevant(decision)
        decision.category = "model"
        decision.model_input = {
            "question": text,
            "context": [{"question": q, "answer": a} for q, a in history[-2:]],
            "facts": [{"id": x["id"], "answer": x["answer"]} for x in self.faq if not x['id'].startswith('lol_')],
            "status": snapshot,
            "tone":decision.tone,
            "diagnostic":bool(_DYNAMIC.search(text)),
        }
        if self.config.get('feedback_enabled'):
            from .improvements import active_preferences,active_guidance
            decision.model_input['response_preferences']=active_preferences(self.db)
            decision.model_input['service_guidance']=active_guidance(self.db)
        return decision

    def general_question(self,decision,text,web_urls=()):
        decision.intent='general';decision.category='model'
        if not text or re.fullmatch(r'\d{1,3}|[?？。!！\s]+',text):
            decision.reply='请补充具体问题或必要背景，我再帮你分析。';decision.category='clarification'
            return decision
        decision.status_hash=digest('general',str(int(self.clock()//300)))
        history=self.contexts.get(decision.subject+':general',(0,[]))[1]
        row=self.db.execute('SELECT ref FROM questions WHERE subject=? AND fingerprint=? AND status_hash=?',
            (decision.subject,decision.fingerprint,decision.status_hash)).fetchone()
        previous=next((answer for question,answer in reversed(history) if question==text),None)
        if row and previous and not _CORRECTION.search(text):
            decision.reply=self.retort(decision.subject,'repeat',decision.tone)+'\n'+previous;decision.category='repeat'
            return decision
        decision.model_input={'domain':'general','question':text,'web_urls':list(web_urls)[:2],
            'context':[{'question':q,'answer':a} for q,a in history[-2:]],'facts':[],
            'diagnostic':False,'tone':decision.tone}
        if self.config.get('feedback_enabled'):
            from .improvements import active_preferences
            decision.model_input['response_preferences']={k:v for k,v in active_preferences(self.db).items() if k in {'length','layout'}}
        return decision

    def investment_question(self,decision,text,web_urls=()):
        decision.intent='investment';decision.category='model'
        decision.status_hash=digest('investment',str(int(self.clock()//300)))
        decision.model_input={'domain':'investment','intent':'investment','question':text,
            'web_urls':list(web_urls)[:2],'personal_context':[],'diagnostic':False}
        return decision

    def output_unsafe(self,text,decision):
        if decision.intent in {'general','question_review','investment'}:
            text=_URL.sub(lambda m:'' if m.group() in decision.public_urls else m.group(),text)
        allowed=decision.public_urls if decision.intent=='investment' else ()
        return unsafe(text,allowed)

    def reject_feedback(self,decision,reason):
        from .improvements import reject,acknowledgement
        origin=decision.feedback_origin or decision.fingerprint
        value=reject(self.db,decision.subject,origin,reason,self.clock())
        if decision.fingerprint and decision.fingerprint!=origin:
            self.db.execute('INSERT OR REPLACE INTO improvement_receipts VALUES (?,?,?,?)',
                (decision.subject,decision.fingerprint,value[0],self.clock()))
        self.feedback_clarifications.pop(decision.subject,None)
        self.db.execute('DELETE FROM improvement_open WHERE subject=?',(decision.subject,));self.db.commit()
        decision.reply=acknowledgement(*value,tone=decision.tone);decision.category='feedback_rejected';decision.model_input=None
        return decision

    def feedback_public(self,proposal):
        from .improvements import encoded
        body=encoded(proposal)
        names=[name for values in self.config.get('usage_public_aliases',{}).values() for name in values]
        return (not any(normalize(name).casefold() in normalize(body).casefold() for name in names)
                and mask_labels(body,self.config.get('hidden_label_rules',[]))==body)

    def interpret(self, decision: Decision, result: dict) -> Decision:
        if decision.intent=='feedback':
            from .improvements import candidate,save,acknowledgement,clarification_prompt
            if result.get('feedback_clarification'):
                question=candidate(result['feedback_clarification'],self.faq)
                if not question or not self.feedback_public(question):return self.reject_feedback(decision,'safety')
                decision.feedback_question={**question,'question':decision.question,'fingerprint':decision.fingerprint,'at':self.clock()}
                self.db.execute('INSERT OR REPLACE INTO improvement_open VALUES (?,?,?)',
                    (decision.subject,decision.fingerprint,self.clock()));self.db.commit()
                decision.reply=clarification_prompt(question,decision.tone);decision.category='feedback_clarification'
                return decision
            proposal=candidate(result.get('feedback_candidate'),self.faq)
            if not proposal:return self.reject_feedback(decision,result.get('feedback_rejection','invalid'))
            if not self.feedback_public(proposal):return self.reject_feedback(decision,'safety')
            origin=decision.feedback_origin or decision.fingerprint
            value=save(self.db,proposal,result.get('feedback_verified') is True,decision.subject,origin,
                self.faq,self.clock(),self.config.get('improvement_mode'),rejection=result.get('feedback_rejection'))
            if origin!=decision.fingerprint:
                self.db.execute('INSERT OR REPLACE INTO improvement_receipts VALUES (?,?,?,?)',
                    (decision.subject,decision.fingerprint,value[0],self.clock()));self.db.commit()
            decision.reply=acknowledgement(*value,tone=decision.tone);decision.category='feedback'
            return decision
        if result.get("scope") == "out_of_scope" and decision.intent in {'general','question_review','investment'}:
            decision.reply='这个问题我目前无法可靠回答。请补充证券代码、市场或希望核验的具体问题。' if decision.intent=='investment' else '这个问题我目前无法可靠回答。请补充具体背景、时间或你希望解决的目标。';decision.category='clarification'
            return decision
        if result.get("scope") == "out_of_scope":
            return self.irrelevant(decision)
        if result.get("scope") == "private":
            decision.reply, decision.category = ('这部分涉及内部信息，暂时不能在群内提供哦。你可以补充脱敏现象，我会继续帮你看。' if decision.tone=='gentle' else PRIVATE), "private"
            return decision
        answer, refs = result.get("answer"), result.get("refs")
        permitted = ({x['id'] for x in self.faq if x['id'].startswith('lol_')} if decision.intent=='lol' else {x['id'] for x in self.faq if not x['id'].startswith('lol_')}|{'status'}) | decision.evidence_ids
        if decision.intent in {'general','question_review'}:permitted={'general_knowledge'}|decision.evidence_ids
        if decision.intent=='investment':permitted={'investment_data'}|decision.evidence_ids
        if result.get("scope") != "service" or not isinstance(answer, str) or not isinstance(refs, list) or not refs:
            decision.reply, decision.category = UNSUPPORTED, "ungrounded"
            return decision
        if any(not isinstance(ref, str) or ref not in permitted for ref in refs) or self.output_unsafe(answer,decision):
            decision.reply, decision.category = PRIVATE, "sensitive_output"
            return decision
        max_answer_chars=7800 if decision.intent=='investment' else 1500
        if len(answer) > max_answer_chars:
            decision.reply, decision.category = "答复过长，未发送。请把问题拆成一个具体问题。", "oversize_output"
            return decision
        decision.reply = answer.strip() if decision.intent=='investment' else without_reference_footer(answer)
        if not decision.reply:
            decision.reply,decision.category=UNSUPPORTED,'ungrounded';return decision
        decision.reference = refs[0]
        decision.category = 'question_review' if decision.intent=='question_review' else 'investment' if decision.intent=='investment' else "answer"
        note = result.get("learning")
        # Anonymous knowledge candidates are separate from chat transcripts. They
        # are archived, never automatically promoted to trusted runtime facts.
        identities=[name for names in self.config.get('usage_public_aliases',{}).values() for name in names]
        if (decision.intent not in {'lol','general','question_review','investment'} and not decision.contains_personal_usage and isinstance(note,str) and 0<len(note)<=300
                and not unsafe(note) and not _PRIVATE_REQUEST.search(note) and not any(name in note for name in identities)):
            decision.learning = note
        return decision

    def record_usage(self, usage: dict) -> None:
        def safe_int(value):
            return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000000 else 0
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        self.db.execute("""INSERT INTO usage VALUES (?,1,?,?) ON CONFLICT(day) DO UPDATE SET
            requests=requests+1,input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens""",
            (day, safe_int(usage.get("input_tokens")), safe_int(usage.get("output_tokens"))))
        self.db.commit()

    def delivered(self, event: Incoming, decision: Decision, status: str) -> None:
        self.mark_delivery(event, status)
        if status != "replied" or not decision.reply:
            return
        now = self.clock()
        if decision.category=='feedback_clarification' and decision.feedback_question:
            self.feedback_clarifications[decision.subject]=decision.feedback_question
            self.db.execute('INSERT OR REPLACE INTO improvement_open VALUES (?,?,?)',
                (decision.subject,decision.feedback_question['fingerprint'],now))
        if decision.reference and decision.fingerprint and decision.category not in {"off_topic", "private", "sensitive_input", "sensitive_output", "repeat", "question_review"}:
            self.db.execute("INSERT OR REPLACE INTO questions VALUES (?,?,?,?,?)",
                (decision.subject, decision.fingerprint, decision.status_hash, decision.reference, now))
        if decision.category in {"answer", "faq", "usage"} and decision.question:
            domain=decision.intent if decision.intent in {'lol','general','account_quota'} else 'service'
            key=decision.subject+(':'+domain if domain!='service' else '')
            history = self.contexts.get(key, (0, []))[1]
            self.contexts[key] = (now, (history + [(decision.question, decision.reply)])[-2:])
            self.last_domains[decision.subject]=(now,domain)
        elif decision.category=='investment':
            self.last_domains[decision.subject]=(now,'investment')
        elif decision.category in {'help','status','owner_operation'}:self.last_domains[decision.subject]=(now,'service')
        if decision.learning:
            self.db.execute("INSERT OR IGNORE INTO knowledge VALUES (?,?,?)", (decision.reference, decision.learning, now))
        self.db.commit()
