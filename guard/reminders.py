"""Local group reminders: fixed calendar rules, member ownership and confirmation."""
import datetime as dt
import hashlib
import json
import math
import re
import secrets
import time

BJT=dt.timezone(dt.timedelta(hours=8))
DAY=86400
GRACE=15*60
IDENTIFIER=r'TX-[A-Fa-f0-9]{8}'
HELP=('群提醒支持北京时间的一次性、每天和每周固定时间。例：明天6点提醒我起床；每天22:30提醒我休息；每周五18点提醒大家收尾。'
      '设置后请本人2分钟内真正@回复“确认提醒 确认码”。可说“我的提醒”“查看提醒 TX-编号”“修改提醒 TX-编号 为 明天7点”“取消提醒 TX-编号”。'
      '每人管理自己的提醒，所有者可用“所有提醒”管理全部。已核实的example_person_b/example_person_a可被真正@；其他接收人称呼按正文保留。'
      '提醒只发送文字；完成或取消后清理内容。掉线恢复仅补发15分钟内提醒，更早的标为错过，重复提醒直接续到下一次。')


class Invalid(ValueError):pass


def stamp(value):return dt.datetime.fromtimestamp(value,BJT).strftime('%Y-%m-%d %H:%M:%S')+' 北京时间'


def number(text):
    if text.isascii() and text.isdigit():return int(text)
    digits={c:i for i,c in enumerate('零一二三四五六七八九')};digits['两']=2
    if text in digits:return digits[text]
    if '十' in text:
        a,b=text.split('十',1)
        if (not a or a in digits) and (not b or b in digits):return (digits[a] if a else 1)*10+(digits[b] if b else 0)
    raise Invalid('请用明确的数字时间，例如06:00、下午6点。')


def next_at(spec,now):
    current=dt.datetime.fromtimestamp(now,BJT)
    if spec['mode']=='once':return None
    date=current.date()
    if spec['mode']=='weekly':date+=dt.timedelta(days=(spec['weekday']-date.weekday())%7)
    candidate=dt.datetime.combine(date,dt.time(spec['hour'],spec['minute']),BJT)
    if candidate.timestamp()<=now:candidate+=dt.timedelta(days=7 if spec['mode']=='weekly' else 1)
    return candidate.timestamp()


def schedule_label(spec):
    if spec['mode']=='once':return '一次性 '+stamp(spec['at'])
    day='每天' if spec['mode']=='daily' else '每周'+'一二三四五六日'[spec['weekday']]
    return day+f" {spec['hour']:02d}:{spec['minute']:02d} 北京时间；首次 "+stamp(spec['at'])


def parse_spec(text,actor,config,now,base=None):
    from .core import normalize,unsafe,mask_labels,digest
    text=normalize(text).strip();current=dt.datetime.fromtimestamp(now,BJT)
    if re.search(r'每隔|每月|每年|工作日|每小时|每分钟|截至|截止到|直到',text):
        raise Invalid('目前支持一次性、每天或每周固定时间；请拆成明确的提醒，不会忽略这些附加条件。')
    target=base.get('target',actor) if base else actor;label=base.get('label','本人') if base else '本人'
    for subject,names in config.get('usage_public_aliases',{}).items():
        for name in sorted(names,key=len,reverse=True):
            if re.search(r'提醒\s*'+re.escape(name),text):
                bound=[h for h,p in config.get('member_profiles',{}).items() if p.get('usage_target')==subject]
                if len(bound)!=1:raise Invalid('接收人尚未唯一绑定，请先用“提醒我”或把称呼写在正文里。')
                target=bound[0];label='example_person_b' if subject=='gentle' else 'example_person_a'
                text=re.sub(r'提醒\s*'+re.escape(name),'提醒',text);break
    if re.search(r'提醒(?:我|自己)',text):target=actor;label='本人';text=re.sub(r'提醒(?:我|自己)','提醒',text)
    if re.search(r'提醒(?:大家|群里|全群)',text):target='';label='群内';text=re.sub(r'提醒(?:大家|群里|全群)','提醒',text)
    if '@所有人' in text or '@all' in text.lower():raise Invalid('提醒不支持@所有人。')
    mode='daily' if re.search(r'每天|每日',text) else 'weekly' if re.search(r'每(?:周|星期)',text) else 'once'
    weekly=re.search(r'每(?:周|星期)([一二三四五六日天]|[1-7](?!\d))',text)
    if mode=='weekly' and not weekly:raise Invalid('每周提醒需要写明星期几，例如每周五18:00。')
    weekday=('一二三四五六日'.index(weekly[1].replace('天','日')) if not weekly[1].isdigit() else int(weekly[1])-1) if weekly else None
    relative=re.search(r'([零一二两三四五六七八九十\d]+)\s*(分钟|小时|天)后',text)
    clock=re.search(r'(?:(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上)\s*)?(?<![\d:])([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})(?::([0-5]\d)(?!\d)|点(?:(半|一刻|三刻)|([零一二两三四五六七八九十\d]{1,3})分?)?)',text)
    absolute=re.search(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?|(?<!\d)(\d{1,2})月(\d{1,2})[日号]',text)
    dayword=re.search(r'后天|明天|明早|今天|今晚',text)
    once_week=re.search(r'(下周|本周|星期|周)([一二三四五六日天])',text) if mode=='once' else None
    if relative and (mode!='once' or clock or absolute or dayword or once_week):raise Invalid('相对时间与固定时间混在一起了，请只保留一种。')
    if relative:
        amount=number(relative[1]);seconds=amount*{'分钟':60,'小时':3600,'天':DAY}[relative[2]]
        if not 60<=seconds<=365*DAY:raise Invalid('一次性相对提醒请设在1分钟至365天内。')
        due=math.ceil(now+seconds);calendar=dt.datetime.fromtimestamp(due,BJT);hour,minute=calendar.hour,calendar.minute
    elif clock:
        hour=number(clock[2]);minute=int(clock[3]) if clock[3] else {'半':30,'一刻':15,'三刻':45}.get(clock[4],number(clock[5]) if clock[5] else 0)
        period=clock[1]
        if period in {'下午','傍晚','晚上','中午'}:
            if hour==12 and period=='晚上':raise Invalid('晚上12点有跨日歧义，请写明日期和00:00。')
            if 1<=hour<12:hour+=12
        elif period=='凌晨' and hour==12:hour=0
        elif period in {'早上','早晨','上午'} and hour>=12:raise Invalid('请用明确的24小时制时间，例如12:00。')
        if not 0<=hour<=23 or not 0<=minute<=59:raise Invalid('时间无效，请使用00:00至23:59。')
        date=current.date()
        try:
            if absolute:
                y,m,d=absolute.groups()[:3] if absolute[1] else (str(current.year),absolute[4],absolute[5])
                date=dt.date(int(y),int(m),int(d))
            elif dayword:date+=dt.timedelta(days={'后天':2,'明天':1,'明早':1,'今天':0,'今晚':0}[dayword[0]])
            elif once_week:
                wd='一二三四五六日'.index(once_week[2].replace('天','日'));delta=wd-date.weekday()
                if once_week[1]=='下周':delta+=7
                elif once_week[1]!='本周' and delta<0:delta+=7
                date+=dt.timedelta(days=delta)
            due=dt.datetime.combine(date,dt.time(hour,minute),BJT).timestamp()
        except ValueError:raise Invalid('日期无效，请检查年、月、日。') from None
        if mode in {'daily','weekly'}:
            if absolute or dayword:raise Invalid('重复提醒先只填写每天/每周及时间，不混用起始日期。')
            due=next_at({'mode':mode,'hour':hour,'minute':minute,'weekday':weekday},now)
        elif due<=now:raise Invalid('这个时间已经过去，请明确写“明天”或完整日期。')
    elif base:
        mode=base['mode'];weekday=base.get('weekday');hour=base['hour'];minute=base['minute'];due=base['at']
        if mode!='once':due=next_at(base,now)
        if relative or dayword or absolute or once_week or re.search(r'每天|每日|每周|每星期',text):raise Invalid('请补全具体时间，例如明天07:00。')
    else:raise Invalid('请补充明确时间和内容，例如“明天6点提醒我起床”。')
    body=text
    for match in [relative,clock,absolute,dayword,once_week,weekly]:
        if match:body=body.replace(match[0],' ',1)
    body=re.sub(r'每天|每日',' ',body)
    body=re.sub(r'^(?:(?:请|帮我|麻烦|给我|设置|新增|添加|创建|一个|一条|定时|闹钟|提醒)\s*)+','',body.strip())
    body=re.sub(r'提醒(?:一下)?(?:我|自己|大家|全群)?','',body).strip(' ：:,，。;；')
    if not body and base:body=base['text']
    body=mask_labels(body,config.get('hidden_label_rules',[]))
    if not body or len(body)>300 or unsafe(body):raise Invalid('提醒内容须为1至300字的脱敏短文本，请勿包含密钥、地址或个人信息。')
    if due>now+366*DAY:raise Invalid('请将首次提醒设在未来一年内。')
    return {'text':body,'target':target,'label':label,'mode':mode,'hour':hour,'minute':minute,'weekday':weekday,'at':due}


class Reminders:
    def __init__(self,db,clock=time.time):
        self.db=db;self.clock=clock
        db.executescript('''CREATE TABLE IF NOT EXISTS reminders(
          id TEXT PRIMARY KEY,actor TEXT NOT NULL,grp TEXT NOT NULL,spec TEXT NOT NULL,
          next_at REAL,state TEXT NOT NULL,version INTEGER NOT NULL,updated REAL NOT NULL,last_result TEXT);
          CREATE TABLE IF NOT EXISTS reminder_proposals(
          id TEXT PRIMARY KEY,actor TEXT NOT NULL,grp TEXT NOT NULL,op TEXT NOT NULL,target TEXT,
          revision INTEGER,spec TEXT,code TEXT UNIQUE,expires REAL,state TEXT,request_id TEXT UNIQUE,
          confirmed_request TEXT);
          CREATE TABLE IF NOT EXISTS reminder_deliveries(
          key TEXT PRIMARY KEY,reminder_id TEXT,version INTEGER,scheduled_at REAL,state TEXT,
          server_id TEXT,send_id TEXT,at REAL,checked_at REAL);
          CREATE INDEX IF NOT EXISTS reminders_due ON reminders(state,next_at);
          ''');db.commit()

    def purge(self):
        now=self.clock()
        with self.db:
            self.db.execute("UPDATE reminder_proposals SET state='expired',spec=NULL,code=NULL WHERE state='pending' AND expires<?",(now,))
            self.db.execute('DELETE FROM reminder_proposals WHERE expires<?',(now-7*DAY,))
            self.db.execute("DELETE FROM reminders WHERE state!='active' AND updated<?",(now-7*DAY,))
            self.db.execute("DELETE FROM reminder_deliveries WHERE state!='submitting' AND at<?",(now-7*DAY,))

    def visible(self,identifier,actor,group,config):
        row=self.db.execute('SELECT id,actor,grp,spec,next_at,state,version,updated,last_result FROM reminders WHERE id=? AND grp=?',(identifier,group)).fetchone()
        if not row:return None
        keys=['id','actor','grp','spec','next_at','state','version','updated','last_result'];value=dict(zip(keys,row))
        if actor!=value['actor'] and actor!=config.get('owner_subject_hash'):return None
        value['spec']=json.loads(value['spec']);return value

    def listing(self,actor,group,config,all_members=False):
        if all_members and actor!=config.get('owner_subject_hash'):return '“所有提醒”仅已绑定的所有者可查看；可说“我的提醒”。'
        sql="SELECT id,spec,next_at,last_result FROM reminders WHERE grp=? AND state='active'";args=[group]
        if not all_members:sql+=' AND actor=?';args.append(actor)
        rows=self.db.execute(sql+' ORDER BY next_at,id LIMIT 11',args).fetchall()
        if not rows:return '当前没有生效中的提醒。'
        lines=['生效中的提醒（北京时间）：']
        for identifier,raw,due,result in rows[:10]:
            spec=json.loads(raw);lines.append(identifier+' '+stamp(due)+' '+spec['label']+'：'+spec['text'][:65]+('（上次未确认送达）' if result=='needs_review' else ''))
        if len(rows)>10:lines.append('只显示最近10条，可按编号查看。')
        return '\n'.join(lines)

    def request(self,text,actor,group,request_id,config):
        self.purge()
        if re.match(r'建议|意见|反馈|优化',text):return None
        direct=re.match(r'(?:(?:请|帮我|麻烦|给我)\s*)?(?:/提醒|提醒|闹钟|定时|设置.{0,4}提醒|添加.{0,4}提醒|新增.{0,4}提醒|创建.{0,4}提醒|查看.{0,4}提醒|取消提醒|删除提醒|修改提醒|更改提醒|确认提醒|我的提醒|所有提醒|全部提醒|怎样设置提醒|怎么设置提醒|如何设置提醒)',text)
        timed=re.search(r'(?:明天|后天|今天|明早|今晚|每天|每日|每周|每星期|下周|本周|20\d{2}|\d{1,2}月|[零一二两三四五六七八九十\d]+(?:点|分钟后|小时后|天后)|\d{1,2}:\d{2}).*提醒',text)
        if not direct and not timed:return None
        if config.get('reminders_enabled') is not True:return '群提醒功能暂未启用。'
        if len(text)>600:return '提醒请求过长，请用简短的时间和内容。'
        if re.fullmatch(r'(?:/提醒(?:帮助)?|提醒帮助|怎样设置提醒|怎么设置提醒|如何设置提醒)',text):return HELP
        if re.fullmatch(r'(?:查看)?(?:我的提醒|提醒列表|所有提醒|全部提醒)',text):return self.listing(actor,group,config,all_members=bool(re.search('所有|全部',text)))
        confirmed=re.fullmatch(r'确认提醒\s+([A-Fa-f0-9]{8})',text)
        if confirmed:return self.confirm(confirmed[1].upper(),actor,group,request_id,config)
        duplicate=self.db.execute('SELECT id,code,state FROM reminder_proposals WHERE request_id=? AND actor=? AND grp=?',(request_id,actor,group)).fetchone()
        if duplicate:return '该提醒请求已处理，请查看“我的提醒”。'
        edit=re.fullmatch(r'(查看|取消|删除|修改|更改)提醒\s*('+IDENTIFIER+r')(?:\s*(?:为|改成|改为)\s*(.+))?',text,re.S)
        op='create';target='';revision=0;base=None;spec=None
        if edit:
            target=edit[2].upper();row=self.visible(target,actor,group,config)
            if not row:return '未找到你可管理的该提醒；仅本人或已绑定所有者可操作。'
            if edit[1]=='查看':
                result={'accepted':'微信已受理','needs_review':'发送结果待核查','missed':'已错过，未补发','submitting':'发送受理中'}.get(row['last_result'],'')
                return target+' '+{'active':'生效中','cancelled':'已取消','done':'已结束'}[row['state']]+'；'+(schedule_label({**row['spec'],'at':row['next_at']})+'；'+row['spec'].get('text','') if row['state']=='active' else '提醒内容已清理。')+('；'+result if result else '')
            op='cancel' if edit[1] in {'取消','删除'} else 'update'
            if row['state']!='active' and not (op=='cancel' and row['state']=='done' and row['last_result']=='submitting'):
                return '该提醒已经结束，不能再次修改或取消。'
            revision=row['version'];base=row['spec'];op='cancel' if edit[1] in {'取消','删除'} else 'update'
            if op=='update' and not edit[3]:return '请写“修改提醒 '+target+' 为 明天07:00”或完整的新时间和内容。'
            text=edit[3] or ''
        try:
            if op!='cancel':spec=parse_spec(text,actor,config,self.clock(),base)
        except Invalid as error:return str(error)
        from .core import unsafe,normalize,mask_labels
        for _ in range(128):
            identifier='TX-'+secrets.token_hex(4).upper();code=secrets.token_hex(4).upper()
            if unsafe(identifier+' '+code) or mask_labels(identifier+' '+code,config.get('hidden_label_rules',[]))!=normalize(identifier+' '+code):continue
            if self.db.execute('SELECT 1 FROM reminder_proposals WHERE id=? OR code=?',(identifier,code)).fetchone():continue
            if self.db.execute('SELECT 1 FROM reminders WHERE id=?',(identifier,)).fetchone():continue
            break
        else:return '暂时无法生成提醒编号，请稍后重试。'
        with self.db:
            self.db.execute("UPDATE reminder_proposals SET state='superseded',spec=NULL,code=NULL WHERE actor=? AND grp=? AND state='pending'",(actor,group))
            self.db.execute('INSERT INTO reminder_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)',
                (identifier,actor,group,op,target,revision,json.dumps(spec,ensure_ascii=False) if spec else None,code,self.clock()+120,'pending',request_id))
        preview=('取消 '+target+'。已提交给微信的消息无法撤回。' if op=='cancel' else ('修改 '+target if op=='update' else '新建 '+identifier)+'：'+schedule_label(spec)+'\n接收人：'+spec['label']+'；内容：'+spec['text'])
        return '提醒预览：'+preview+'\n请本人2分钟内真正@回复：确认提醒 '+code+'。未确认不保存。'

    def confirm(self,code,actor,group,request_id,config):
        row=self.db.execute('SELECT id,actor,grp,op,target,revision,spec,expires,state,confirmed_request FROM reminder_proposals WHERE code=?',(code,)).fetchone()
        if not row or row[1]!=actor or row[2]!=group:return '提醒确认码无效或不属于你，未保存。'
        identifier,_,_,op,target,revision,raw,expires,state,confirmed=row
        if state=='applied':return (target or identifier)+' 这条确认已处理，不会重复创建；可查看“我的提醒”。'
        if state!='pending' or expires<self.clock():return '提醒确认码已过期或被新请求替换，未保存；请重新设置。'
        previous=self.visible(target,actor,group,config) if op!='create' else None
        mutable=previous and (previous['state']=='active' or op=='cancel' and previous['state']=='done' and previous['last_result']=='submitting')
        if op!='create' and (not mutable or previous['version']!=revision):return '提醒已改变或无操作权限，请重新查看并发起修改。'
        spec=json.loads(raw) if raw else None
        if spec and spec['at']<=self.clock():return '首次提醒时间已过去，未保存；请重新设置未来时间。'
        with self.db:
            if op=='create':
                self.db.execute('INSERT INTO reminders VALUES (?,?,?,?,?,\'active\',1,?,NULL)',(identifier,actor,group,raw,spec['at'],self.clock()))
            elif op=='update':
                self.db.execute('UPDATE reminders SET spec=?,next_at=?,version=version+1,updated=? WHERE id=?',(raw,spec['at'],self.clock(),target))
            else:
                self.db.execute("UPDATE reminders SET state='cancelled',spec='{}',next_at=NULL,version=version+1,updated=? WHERE id=?",(self.clock(),target))
            self.db.execute("UPDATE reminder_proposals SET state='applied',spec=NULL,confirmed_request=? WHERE id=?",(request_id,identifier))
        return ((target or identifier)+' 已取消，提醒内容已清理。' if op=='cancel' else (target or identifier)+' 已保存：'+schedule_label(spec)+'；'+spec['label']+'：'+spec['text'])

    def claim_due(self,group,server_id):
        now=self.clock()
        from .core import unsafe
        with self.db:
            row=self.db.execute("SELECT id,actor,spec,next_at,version FROM reminders WHERE grp=? AND state='active' AND next_at<=? ORDER BY next_at,id LIMIT 1",(group,now)).fetchone()
            if not row:return None
            identifier,actor,raw,due,version=row;spec=json.loads(raw)
            key='group-reminder-'+hashlib.sha256((identifier+':'+str(version)+':'+str(due)).encode()).hexdigest()[:32]
            if self.db.execute('SELECT 1 FROM reminder_deliveries WHERE key=?',(key,)).fetchone():return None
            missed=now-due>GRACE or unsafe(spec['text'])
            state='skipped' if missed else 'submitting'
            self.db.execute('INSERT INTO reminder_deliveries VALUES (?,?,?,?,?,?,NULL,?,?)',(key,identifier,version,due,state,server_id,now,now))
            future=next_at(spec,now)
            self.db.execute("UPDATE reminders SET next_at=?,state=?,spec=?,updated=?,last_result=? WHERE id=?",
                (future,'active' if future else 'done',raw if future else '{}',now,'missed' if missed else 'submitting',identifier))
            if missed:return {'skipped':True}
            return {'key':key,'id':identifier,'version':version,'at':due,'actor':actor,'spec':spec,'group':group,'server_id':server_id}

    def current(self,item):
        row=self.db.execute('SELECT state,version,last_result FROM reminders WHERE id=?',(item['id'],)).fetchone()
        return bool(row and row[1]==item['version'] and row[0] in {'active','done'})

    def finish(self,key,state,send_id=None):
        if state not in {'accepted','submitting','needs_review','skipped'}:raise ValueError('invalid_send_state')
        with self.db:
            self.db.execute('UPDATE reminder_deliveries SET state=?,send_id=coalesce(?,send_id),checked_at=? WHERE key=?',(state,send_id,self.clock(),key))
            self.db.execute('UPDATE reminders SET last_result=? WHERE id=(SELECT reminder_id FROM reminder_deliveries WHERE key=?) AND version=(SELECT version FROM reminder_deliveries WHERE key=?)',(state,key,key))

    def pending_receipts(self):
        return [dict(zip(['key','server_id','send_id','at'],x)) for x in self.db.execute("SELECT key,server_id,send_id,at FROM reminder_deliveries WHERE state='submitting' AND checked_at<=? ORDER BY at LIMIT 5",(self.clock()-10,)).fetchall()]
