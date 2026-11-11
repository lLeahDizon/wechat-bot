"""Verified, reversible service improvements with two terminal outcomes."""
import hashlib
import json
import re
import time

from .core import normalize, unsafe, _PRIVATE_REQUEST, _OBVIOUS_OTHER
from .model import call, ModelFailure

SCHEMA="""
CREATE TABLE IF NOT EXISTS improvements (
 id TEXT PRIMARY KEY,kind TEXT,payload TEXT,summary TEXT,status TEXT,at REAL,updated REAL);
CREATE TABLE IF NOT EXISTS improvement_receipts (
 subject TEXT,fingerprint TEXT,suggestion_id TEXT,at REAL,PRIMARY KEY(subject,fingerprint));
CREATE TABLE IF NOT EXISTS improvement_active (slot TEXT PRIMARY KEY,suggestion_id TEXT,value TEXT);
CREATE TABLE IF NOT EXISTS improvement_decisions (suggestion_id TEXT PRIMARY KEY,reason TEXT,at REAL);
CREATE TABLE IF NOT EXISTS improvement_open (subject TEXT PRIMARY KEY,fingerprint TEXT,at REAL);
CREATE TABLE IF NOT EXISTS improvement_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,suggestion_id TEXT,action TEXT,reason TEXT,previous TEXT,at REAL);
"""
LENGTHS={'concise','standard'}
LAYOUTS={'conclusion_then_steps','conclusion_then_evidence'}
TOKEN_UNITS={'auto'}
TOPICS={'diagnosis_order','evidence','client_help','answer_structure'}
MODES={'verified_faq_style','verified_service'}
TRIGGER=re.compile(r'^(?:/建议|/反馈|我建议|我想反馈|给(?:你|机器人|助手).{0,4}建议|建议|反馈|你(?:刚才|的)?(?:回答|回复|答复).{0,8}(?:太长|太短|不清楚|看不懂|不准确|有误))',re.I)
LIVE=re.compile(r'现在|当前|今天|最近|刚刚|实时|状态|正常吗|挂了吗|是否可用|排队|并发|耗时|延迟|错误率|(?:查|检查|统计).{0,12}(?:用量|消耗|次数|错误)|cpu|内存|磁盘|负载|剩余|余额|用了多少',re.I)
RULE_RISK=re.compile(r'(?:输出|公布|提供|给出|发送|读取|展示|获取).{0,12}(?:密钥|口令|密码|凭据|源站|服务器\s*IP|真实\s*IP|管理(?:入口|地址)|上游.{0,4}(?:账号|身份|地址|线路))|(?:让|要求).{0,8}(?:用户|成员).{0,8}(?:发|提供|上传).{0,8}(?:密钥|凭据|原始日志)|系统提示|system\s*prompt|\b(?:root|sudo|ssh)\b|重启|写入|删除|刷新|新增账号|修改配置|安装|登录|执行.{0,8}(?:命令|代码|sql|shell)|(?:忽略|绕过|关闭|覆盖|扩大).{0,8}(?:规则|校验|限制|检查|权限)|(?:修改|调整|改变|提高|增加).{0,10}(?:模型|预算|额度|告警|监控|窗口|并发|上限|次数)|默认.{0,8}(?:窗口|时长|模型)|自动.{0,8}(?:修复|部署|升级)',re.I)
REASONS={
 'verified':'已通过安全、职责与执行校验', 'already_effective':'相同规则当前已生效',
 'safety':'涉及敏感信息、权限或受保护操作', 'unrelated':'与当前Sub2API职责无关',
 'unsupported':'当前能力无法实际落实该项，未执行任何变更',
 'invalid':'建议未能整理为可安全执行的方案', 'unclear':'补充后仍无法明确可执行方案',
 'verification_unavailable':'本次校验未完成，无法确认可安全生效，可稍后重新完整提议',
 'not_verified':'安全、职责或事实依据未通过独立校验', 'capacity':'当前学习容量已满，未应用该项',
 'revoked':'此规则已由操作员撤销，未重新启用', 'superseded':'已被后续规则替换，当前未生效',
 'source_changed':'所引用的审核答案已变化，原匹配已失效', 'legacy':'旧候选尚未执行，按新规则明确拒绝',
 'clarification_expired':'未在有效期内收到明确补充', 'clarification_interrupted':'澄清过程已中断，请重新完整提议',
 'operator_review':'经操作员核实并实际启用', 'oversize':'建议过长，请概括具体改进点',
}


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def feedback_trigger(text):
    if re.match(r'^建议.{0,15}(?:怎么玩|怎么打|怎么出装|带什么|用什么|选什么)',text) and re.search(r'英雄联盟|lol|英雄|符文|出装',text,re.I):return False
    return bool(TRIGGER.search(text))
def fingerprint(text):return hashlib.sha256(re.sub(r'[\W_]+','',normalize(text)).casefold().encode()).hexdigest()
def faq_hash(facts):return hashlib.sha256(encoded([{'id':x['id'],'answer':x['answer']} for x in facts]).encode()).hexdigest()
def answer_hash(entry):return hashlib.sha256(encoded({'id':entry['id'],'answer':entry['answer']}).encode()).hexdigest()


def candidate(raw,facts):
    if not isinstance(raw,dict):return None
    kind=raw.get('kind')
    if kind=='reply_style':
        style=raw.get('style');allowed={'length':LENGTHS,'layout':LAYOUTS,'token_unit':TOKEN_UNITS}
        if not isinstance(style,dict) or not style or set(style)-set(allowed):return None
        if any(not isinstance(v,str) or v not in allowed[k] for k,v in style.items()):return None
        return {'kind':kind,'style':style}
    if kind=='faq_alias':
        question=raw.get('question');ref=raw.get('faq_ref')
        if not isinstance(question,str) or not 4<=len(question)<=200 or not isinstance(ref,str) or ref not in {x['id'] for x in facts}:return None
        question=normalize(question).strip()
        if unsafe(question) or _PRIVATE_REQUEST.search(question) or _OBVIOUS_OTHER.search(question) or LIVE.search(question):return None
        return {'kind':kind,'question':question,'faq_ref':ref} if re.sub(r'[\W_]+','',question) else None
    if kind=='service_rule':
        topic=raw.get('topic');text=raw.get('text')
        domain=raw.get('domain','service')
        if not isinstance(domain,str) or domain not in {'service','lol'}:return None
        if not isinstance(topic,str) or topic not in TOPICS or not isinstance(text,str) or not 6<=len(text)<=240:return None
        text=normalize(text).strip()
        if unsafe(text) or _PRIVATE_REQUEST.search(text) or _OBVIOUS_OTHER.search(text) or RULE_RISK.search(text):return None
        if not re.search(r'先|优先|给出|说明|附|区分|列|提醒|展示|回答|答复',text):return None
        result={'kind':kind,'topic':topic,'text':text}
        if 'domain' in raw:result['domain']=raw['domain']
        return result
    if kind=='suggestion':
        summary=raw.get('summary')
        if isinstance(summary,str) and 4<=len(summary)<=180 and not unsafe(summary) and not _PRIVATE_REQUEST.search(summary):
            return {'kind':kind,'summary':normalize(summary).strip()}
    if kind=='clarify':
        options=raw.get('options')
        if not isinstance(options,list) or not 2<=len(options)<=3:return None
        if any(not isinstance(x,dict) or x.get('kind') not in {'reply_style','faq_alias','service_rule'} for x in options):return None
        normalized=[candidate(x,facts) for x in options]
        if any(not x or x['kind'] not in {'reply_style','faq_alias','service_rule'} for x in normalized):return None
        if len({encoded(x) for x in normalized})!=len(normalized):return None
        return {'kind':'clarify','options':normalized}
    return None


def _verify(data,proposal,key_file,api_base,model_call):
    request={'intent':'feedback_verify','question':data['question'],'candidate':proposal,'facts':data['facts'],'context':[]}
    try:second,usage=model_call(request,key_file,api_base)
    except ModelFailure:return {'feedback_candidate':proposal,'feedback_verified':False,'feedback_rejection':'verification_unavailable'},{}
    value=second.get('verification');refs={x['id'] for x in data['facts']}
    ok=(second.get('scope')=='service' and isinstance(value,dict) and value.get('approved') is True
        and isinstance(value.get('refs'),list) and bool(value['refs']) and all(isinstance(x,str) and x in refs for x in value['refs']))
    reasons={'reply_style':'safe_style','faq_alias':'matches_trusted_faq','service_rule':'safe_service_rule'}
    ok=bool(ok and value.get('reason')==reasons.get(proposal['kind']))
    if ok and proposal['kind']=='faq_alias':ok=proposal['faq_ref'] in value['refs']
    return {'feedback_candidate':proposal,'feedback_verified':bool(ok),'feedback_rejection':None if ok else 'not_verified'},usage


def review(data,key_file,api_base,model_call=call):
    """Two calls total, or one proposal and one verifier separated by clarification."""
    if data.get('intent')=='feedback_verify_only':
        proposal=candidate(data.get('candidate'),data['facts'])
        if not proposal or proposal['kind'] not in {'reply_style','faq_alias','service_rule'}:return {'feedback_rejection':'invalid'},[]
        result,usage=_verify(data,proposal,key_file,api_base,model_call);return result,[usage]
    try:first,usage=model_call({**data,'intent':'feedback_propose'},key_file,api_base)
    except ModelFailure:return {'feedback_rejection':'verification_unavailable'},[{}]
    usages=[usage]
    if first.get('scope')!='service':
        return {'feedback_rejection':'unrelated' if first.get('scope')=='out_of_scope' else 'safety' if first.get('scope')=='private' else 'invalid'},usages
    raw=first.get('feedback')
    if isinstance(raw,dict) and raw.get('kind')=='faq_alias':
        examples=[a or b for a,b in re.findall(r'[“「]([^”」\n]{4,200})[”」]|"([^"\n]{4,200})"',data['question'])]
        if len(examples)==1:raw={**raw,'question':examples[0]}
    proposal=candidate(raw,data['facts'])
    if not proposal:return {'feedback_rejection':'invalid'},usages
    if proposal['kind']=='clarify':return {'feedback_clarification':proposal},usages
    if proposal['kind']=='suggestion':return {'feedback_candidate':proposal,'feedback_rejection':'unsupported'},usages
    result,usage=_verify(data,proposal,key_file,api_base,model_call);usages.append(usage);return result,usages


def summary_of(proposal):
    if proposal['kind']=='faq_alias':return '补充已审核FAQ的常见问法匹配'
    if proposal['kind']=='service_rule':return proposal['text']
    if proposal['kind']=='suggestion':return proposal['summary']
    if proposal['kind']=='rejection':return '该条改进建议'
    names={'concise':'回答更简洁','standard':'保留必要解释','conclusion_then_steps':'先给结论，再列步骤',
           'conclusion_then_evidence':'先给结论，再列证据','auto':'统计单位自动换算，小数最多两位',
           'million':'旧Token单位偏好已由自动换算取代','tokens':'旧Token单位偏好已由自动换算取代'}
    return '；'.join(names[v] for v in proposal['style'].values())


def slots(payload):
    if payload['kind']=='faq_alias':return {'faq:'+payload['question_hash']:payload}
    if payload['kind']=='reply_style':return {'style:'+k:{'kind':'reply_style','value':v} for k,v in payload['style'].items()}
    if payload['kind']=='service_rule':return {'rule:'+('lol:' if payload.get('domain')=='lol' else '')+payload['topic']:payload}
    return {}


def _record(db,identifier):
    row=db.execute('SELECT kind,payload,summary,status FROM improvements WHERE id=?',(identifier,)).fetchone()
    return {'kind':row[0],'payload':json.loads(row[1]),'summary':row[2],'status':row[3]} if row else None


def _decision(db,identifier,status,reason,now,previous=None):
    db.execute('INSERT OR REPLACE INTO improvement_decisions VALUES (?,?,?)',(identifier,reason,now))
    if previous is not None:
        db.execute('INSERT INTO improvement_events(suggestion_id,action,reason,previous,at) VALUES (?,?,?,?,?)',
                   (identifier,status,reason,encoded(previous),now))


def _faq_valid(payload,facts):
    entry=next((x for x in facts if x['id']==payload.get('faq_ref')),None)
    if not entry:return False
    return payload['answer_hash']==answer_hash(entry) if 'answer_hash' in payload else payload.get('faq_hash')==faq_hash(facts)


def effective_summary(db,identifier,record):
    values=[json.loads(x[0]) for x in db.execute('SELECT value FROM improvement_active WHERE suggestion_id=?',(identifier,))]
    if record['kind']=='reply_style' and values:
        style={slot.split(':',1)[1]:json.loads(value)['value'] for slot,value in db.execute('SELECT slot,value FROM improvement_active WHERE suggestion_id=?',(identifier,))}
        return summary_of({'kind':'reply_style','style':style})
    return record['summary']


def synchronize(db,facts,now):
    with db:
        for identifier,status,payload in db.execute('SELECT id,status,payload FROM improvements').fetchall():
            reason=None
            if status=='pending':reason='legacy'
            elif status=='reverted':reason='revoked'
            elif status=='applied':
                value=json.loads(payload)
                if value.get('kind')=='faq_alias' and not _faq_valid(value,facts):reason='source_changed'
                elif not db.execute('SELECT 1 FROM improvement_active WHERE suggestion_id=?',(identifier,)).fetchone():reason='superseded'
            if reason:
                previous=_record(db,identifier)
                if status!='reverted':db.execute("UPDATE improvements SET status='rejected' WHERE id=?",(identifier,))
                if reason in {'source_changed','revoked'}:db.execute('DELETE FROM improvement_active WHERE suggestion_id=?',(identifier,))
                if not db.execute('SELECT 1 FROM improvement_decisions WHERE suggestion_id=?',(identifier,)).fetchone() or status in {'pending','applied'}:
                    _decision(db,identifier,'rejected',reason,now,previous)


def initialize(db,facts,now,recover=False):
    db.executescript(SCHEMA)
    if recover:
        for subject,fp,_ in db.execute('SELECT subject,fingerprint,at FROM improvement_open').fetchall():reject(db,subject,fp,'clarification_interrupted',now)
        with db:db.execute('DELETE FROM improvement_open')
    synchronize(db,facts,now)


def _payload(proposal,facts):
    payload={**proposal}
    if proposal['kind']=='faq_alias':
        entry=next(x for x in facts if x['id']==proposal['faq_ref']);payload.pop('question')
        payload.update(question_hash=fingerprint(proposal['question']),faq_hash=faq_hash(facts),answer_hash=answer_hash(entry))
    return payload


def save(db,proposal,verified,subject,message_fingerprint,facts,now,mode,rejection=None,explicit_id=None,operator=False):
    payload=_payload(proposal,facts)
    identifier=explicit_id or 'YJ-'+hashlib.sha256(encoded(payload).encode()).hexdigest()[:16]
    if not explicit_id:
        for old_id,text in db.execute('SELECT id,payload FROM improvements WHERE kind=?',(proposal['kind'],)):
            old=json.loads(text)
            if old==payload or (proposal['kind']=='faq_alias' and old.get('question_hash')==payload['question_hash'] and old.get('faq_ref')==payload['faq_ref']):identifier=old_id;break
    previous=_record(db,identifier)
    auto=verified and not rejection and mode in MODES and proposal['kind'] in {'faq_alias','reply_style','service_rule'}
    if proposal['kind']=='service_rule' and mode!='verified_service':auto=False
    reason=rejection or ('verified' if auto else 'unsupported' if proposal['kind']=='suggestion' else 'not_verified')
    old_reason=db.execute('SELECT reason FROM improvement_decisions WHERE suggestion_id=?',(identifier,)).fetchone()
    if previous and (previous['status']=='reverted' or (old_reason and old_reason[0]=='revoked')) and not operator:auto=False;reason='revoked'
    if auto and proposal['kind']=='faq_alias':
        exists=db.execute('SELECT 1 FROM improvement_active WHERE slot=?',('faq:'+payload['question_hash'],)).fetchone()
        if not exists and db.execute("SELECT count(*) FROM improvement_active WHERE slot LIKE 'faq:%'").fetchone()[0]>=100:auto=False;reason='capacity'
    if not auto and previous and previous['status']=='applied' and db.execute('SELECT 1 FROM improvement_active WHERE suggestion_id=?',(identifier,)).fetchone():
        with db:db.execute('INSERT OR REPLACE INTO improvement_receipts VALUES (?,?,?,?)',(subject,message_fingerprint,identifier,now))
        return identifier,'applied',effective_summary(db,identifier,previous),'already_effective'
    status='applied' if auto else 'rejected'
    if operator and auto:reason='operator_review'
    with db:
        db.execute('INSERT INTO improvements VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,payload=excluded.payload,summary=excluded.summary,status=excluded.status,updated=excluded.updated',
                   (identifier,proposal['kind'],encoded(payload),summary_of(proposal),status,now,now))
        if auto:
            for slot,value in slots(payload).items():db.execute('INSERT OR REPLACE INTO improvement_active VALUES (?,?,?)',(slot,identifier,encoded(value)))
        _decision(db,identifier,status,reason,now,previous if previous else {})
        db.execute('INSERT OR REPLACE INTO improvement_receipts VALUES (?,?,?,?)',(subject,message_fingerprint,identifier,now))
    synchronize(db,facts,now)
    return identifier,status,summary_of(proposal),reason


def reject(db,subject,message_fingerprint,reason,now):
    reason=reason if reason in REASONS else 'invalid'
    payload={'kind':'rejection','fingerprint':message_fingerprint,'reason':reason}
    identifier='YJ-'+hashlib.sha256(encoded(payload).encode()).hexdigest()[:16]
    with db:
        db.execute('INSERT INTO improvements VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated',
                   (identifier,'rejection',encoded(payload),'该条改进建议','rejected',now,now))
        _decision(db,identifier,'rejected',reason,now)
        db.execute('INSERT OR REPLACE INTO improvement_receipts VALUES (?,?,?,?)',(subject,message_fingerprint,identifier,now))
    return identifier,'rejected','该条改进建议',reason


def cached(db,subject,message_fingerprint):
    row=db.execute('SELECT i.id,i.kind,i.payload,i.summary,i.status,d.reason FROM improvement_receipts r JOIN improvements i ON i.id=r.suggestion_id LEFT JOIN improvement_decisions d ON d.suggestion_id=i.id WHERE r.subject=? AND r.fingerprint=?',
                   (subject,message_fingerprint)).fetchone()
    if not row:return None
    record={'kind':row[1],'payload':json.loads(row[2]),'summary':row[3],'status':row[4]}
    active=bool(db.execute('SELECT 1 FROM improvement_active WHERE suggestion_id=?',(row[0],)).fetchone())
    return row[0],'applied' if row[4]=='applied' and active else 'rejected',effective_summary(db,row[0],record),row[5] or ('revoked' if row[4]=='reverted' else 'legacy')


def acknowledgement(identifier,status,summary,reason='verified',tone='default',duplicate=False):
    lead='收到啦，' if tone=='gentle' else ''
    if status=='applied':body='已采纳并生效：'+summary+'。'
    else:body='已拒绝：'+summary+'。原因：'+REASONS.get(reason,REASONS['invalid'])+'。'
    return lead+('这条建议之前已处理。' if duplicate else '')+body+'编号 '+identifier+'。'


def active_preferences(db):
    allowed={'length':LENGTHS,'layout':LAYOUTS,'token_unit':TOKEN_UNITS};result={}
    for slot,text in db.execute("SELECT slot,value FROM improvement_active WHERE slot LIKE 'style:%'"):
        value=json.loads(text).get('value');name=slot.split(':',1)[1]
        if name in allowed and isinstance(value,str) and value in allowed[name]:result[name]=value
    return result


def active_guidance(db,domain='service'):
    values=[json.loads(x[0]) for x in db.execute("SELECT value FROM improvement_active WHERE slot LIKE 'rule:%' ORDER BY slot")]
    return [x['text'] for x in values if x.get('domain','service')==domain]


def faq_match(db,text,facts):
    row=db.execute('SELECT value FROM improvement_active WHERE slot=?',('faq:'+fingerprint(text),)).fetchone()
    if not row:return None
    payload=json.loads(row[0])
    if not _faq_valid(payload,facts):return None
    return next(x for x in facts if x['id']==payload['faq_ref'])


def migrate_faq_provenance(db,old_facts,new_facts):
    changed=[];old={x['id']:x for x in old_facts};new={x['id']:x for x in new_facts}
    with db:
        for identifier,text in db.execute("SELECT id,payload FROM improvements WHERE kind='faq_alias'").fetchall():
            p=json.loads(text);ref=p.get('faq_ref')
            if ref in old and ref in new and old[ref]['answer']==new[ref]['answer'] and _faq_valid(p,old_facts):
                p['answer_hash']=answer_hash(new[ref]);db.execute('UPDATE improvements SET payload=? WHERE id=?',(encoded(p),identifier))
                db.execute('UPDATE improvement_active SET value=? WHERE suggestion_id=?',(encoded(p),identifier));changed.append(identifier)
    return changed


def clarification_prompt(proposal,tone='default'):
    lead='想再确认一下，' if tone=='gentle' else '请明确这条建议希望采用哪一种：'
    def label(option):
        if option['kind']=='faq_alias':return '将“'+option['question']+'”匹配到已有审核答案'
        return summary_of(option)
    return lead+'\n'+'\n'.join(f'{i+1}. {label(option)}' for i,option in enumerate(proposal['options']))+'\n请原提议者在24小时内真正@我回复选项编号；这一步尚未应用任何变更。'


def clarification_choice(text,state):
    text=normalize(text).strip();match=re.fullmatch(r'(?:补充[:： ]*)?(?:我选|选择|选|第)?([1-3一二三])(?:个|项)?[。.!！?？\s]*',text)
    if match:
        index={'一':0,'二':1,'三':2}.get(match[1],int(match[1])-1 if match[1].isdigit() else -1)
        return state['options'][index] if 0<=index<len(state['options']) else False
    if re.match(r'^(?:补充|我的意思|意思是|是指|澄清|我说的是)',text):return False
    return None


def listing(db):
    rows=db.execute('SELECT id,kind,payload,summary,status FROM improvements ORDER BY updated DESC LIMIT 5').fetchall()
    result=[]
    for identifier,kind,payload,summary,status in rows:
        active=bool(db.execute('SELECT 1 FROM improvement_active WHERE suggestion_id=?',(identifier,)).fetchone())
        if status=='applied' and active:
            text=effective_summary(db,identifier,{'kind':kind,'summary':summary});result.append(f'{identifier}（已采纳并生效）：{text}')
        else:
            row=db.execute('SELECT reason FROM improvement_decisions WHERE suggestion_id=?',(identifier,)).fetchone()
            reason=row[0] if row else 'revoked' if status=='reverted' else 'legacy'
            result.append(f'{identifier}（已拒绝）：{summary}；{REASONS.get(reason,REASONS["invalid"])}')
    return '最近的建议结果：\n'+'\n'.join(result) if result else '目前还没有建议记录。真正@我后可提出本服务的改进建议。'


def prune(db,now):
    with db:
        db.execute('DELETE FROM improvement_receipts WHERE at<?',(now-7*86400,))
        db.execute('DELETE FROM improvements WHERE id NOT IN (SELECT suggestion_id FROM improvement_active) AND (updated<? OR id NOT IN (SELECT id FROM improvements ORDER BY updated DESC LIMIT 200))',(now-7*86400,))
        db.execute('DELETE FROM improvement_decisions WHERE suggestion_id NOT IN (SELECT id FROM improvements)')
        db.execute('DELETE FROM improvement_events WHERE at<? OR seq NOT IN (SELECT seq FROM improvement_events ORDER BY seq DESC LIMIT 500)',(now-7*86400,))


def rollback(db,identifier,facts=None):
    record=_record(db,identifier)
    if not record:return False
    affected=[x[0] for x in db.execute('SELECT slot FROM improvement_active WHERE suggestion_id=?',(identifier,))]
    with db:
        db.execute("UPDATE improvements SET status='reverted' WHERE id=?",(identifier,));_decision(db,identifier,'rejected','revoked',time.time(),record)
        db.execute('DELETE FROM improvement_active WHERE suggestion_id=?',(identifier,))
        rows=db.execute("SELECT i.id,i.payload FROM improvements i LEFT JOIN improvement_decisions d ON d.suggestion_id=i.id WHERE i.id!=? AND (i.status='applied' OR (i.status='rejected' AND d.reason='superseded')) ORDER BY i.updated DESC",(identifier,)).fetchall()
        for slot in affected:
            for older,text in rows:
                p=json.loads(text);values=slots(p)
                if slot not in values or (p['kind']=='faq_alias' and (facts is None or not _faq_valid(p,facts))):continue
                db.execute('INSERT OR REPLACE INTO improvement_active VALUES (?,?,?)',(slot,older,encoded(values[slot])))
                db.execute("UPDATE improvements SET status='applied' WHERE id=?",(older,));_decision(db,older,'applied','verified',time.time());break
    return True


def operator_inspect(db,identifier,facts):
    record=_record(db,identifier)
    if not record:return None
    p=record['payload'];kind=p.get('kind');valid=False
    if kind in {'reply_style','service_rule'}:valid=candidate(p,facts)==p
    elif kind=='faq_alias':
        valid=(isinstance(p.get('question_hash'),str) and bool(re.fullmatch(r'[a-f0-9]{64}',p['question_hash']))
               and _faq_valid(p,facts))
    trusted=record['status']=='applied'
    for action,reason,previous in db.execute('SELECT action,reason,previous FROM improvement_events WHERE suggestion_id=?',(identifier,)):
        trusted=trusted or (action=='applied' and reason in {'verified','operator_review'}) or json.loads(previous).get('status')=='applied'
    active=bool(db.execute('SELECT 1 FROM improvement_active WHERE suggestion_id=?',(identifier,)).fetchone())
    revision=hashlib.sha256(encoded({'record':record,'active':list(db.execute('SELECT slot,suggestion_id,value FROM improvement_active ORDER BY slot')),
        'facts':faq_hash(facts)}).encode()).hexdigest()
    return {'summary':record['summary'],'status':record['status'],'can_adopt':bool(valid and trusted and not active),
            'can_revert':bool(active and valid),'revision':revision,'already_active':active}


def operator_adopt(db,identifier,facts,revision):
    review=operator_inspect(db,identifier,facts)
    if not review or review['revision']!=revision or not review['can_adopt']:return False
    record=_record(db,identifier);now=time.time()
    with db:
        for slot,value in slots(record['payload']).items():db.execute('INSERT OR REPLACE INTO improvement_active VALUES (?,?,?)',(slot,identifier,encoded(value)))
        db.execute("UPDATE improvements SET status='applied',updated=? WHERE id=?",(now,identifier))
        _decision(db,identifier,'applied','operator_review',now,record)
    synchronize(db,facts,now)
    return True
