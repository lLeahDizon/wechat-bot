"""Deterministic, privacy-bounded group usage queries."""
import datetime
import calendar
import re

from .stat_format import decimal_amount,format_money,format_token_quantity,format_tokens,token_scale

ALIASES={'flower':['example_person_a','example_person_a','example_person_a'],
         'gentle':['example_person_b','example_person_b','example_person_b']}
NAMES={'flower':'example_person_a','gentle':'example_person_b'}
SUBJECT_RE=re.compile(r'^(?:flower|gentle|member_[a-f0-9]{16})$')
TOKEN_RE=re.compile(r'(?<![A-Za-z0-9_])tokens?(?![A-Za-z0-9_])|令牌',re.I)
COST_RE=re.compile(r'\$|(?<![A-Za-z0-9_])USD(?![A-Za-z0-9_])|美元|额度|费用|花费|金额|价格|成本|消费',re.I)
USAGE_RE=re.compile(r'用量|消耗|使用量|使用排行|谁用得多',re.I)
RANK_RE=re.compile(r'排行|排名|排行榜|前\s*\d+|谁用得多',re.I)
DIAGNOSTIC_RE=re.compile(r'CPU|内存|磁盘|延迟|报错|日志|映射|诊断|原因',re.I)
EXPLANATION_RE=re.compile(r'如何|怎么|原理|是什么|计算|口径|配置',re.I)
COMMENTARY_RE=re.compile(r'评论|点评|评价|分析|解读',re.I)
COMPARE_ALL_RE=re.compile(r'群成员之间|成员之间|所有成员|全体成员|大家的|每个人的',re.I)
PERIOD_ERROR='请用北京时间指定日期或起止时间；未指定时按今天统计。'
BJT=datetime.timezone(datetime.timedelta(hours=8),'Asia/Shanghai')
MAX_EPOCH=253402300799
DATE_RE=re.compile(r'(?<!\d)(?:(?P<year>\d{4})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})(?:日|号)?|(?P<month_only>\d{1,2})月(?P<day_only>\d{1,2})(?:日|号)?)(?:[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?(?!\d)')
TIME_RE=re.compile(r'(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)')
RANGE_RE=re.compile(r'(?:至|到|~|～|—|–|－|\s-\s)')
DURATION_RE=re.compile(r'(?:最近|近|过去)\s*(\d+|[一二两三四五六七八九十]+)\s*(分钟|分|小时|天|周|星期|个?月|年)')
CN_DIGITS={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}


def _now(now=None):
    if now is None:return datetime.datetime.now(BJT)
    if now.tzinfo is None:return now.replace(tzinfo=BJT)
    return now.astimezone(BJT)


def _number(value):
    if value.isdigit():return int(value)
    if '十' not in value:return CN_DIGITS.get(value)
    tens,ones=value.split('十',1)
    return (CN_DIGITS.get(tens[-1],1) if tens else 1)*10+(CN_DIGITS.get(ones,0) if ones else 0)


def _month_start(value):
    return value.replace(day=1,hour=0,minute=0,second=0,microsecond=0)


def _subtract_months(value,count):
    index=value.year*12+value.month-1-count
    year,month=divmod(index,12)
    day=min(value.day,calendar.monthrange(year,month+1)[1])
    return value.replace(year=year,month=month+1,day=day)


def _date_matches(text,now):
    found=[]
    for match in DATE_RE.finditer(text):
        year=int(match.group('year')) if match.group('year') else now.year
        month=int(match.group('month') or match.group('month_only'))
        day=int(match.group('day') or match.group('day_only'))
        hour=int(match.group('hour') or 0);minute=int(match.group('minute') or 0)
        second=int(match.group('second') or 0)
        try:value=datetime.datetime(year,month,day,hour,minute,second,tzinfo=BJT)
        except ValueError:return None
        found.append((match,value,match.group('hour') is not None))
    return found


def _range(start,end):
    start_epoch=int(start.timestamp());end_epoch=int(end.timestamp())
    if start_epoch<0 or end_epoch>MAX_EPOCH or start_epoch>=end_epoch:
        return None,None,None,None,PERIOD_ERROR
    return 'range',15,start_epoch,end_epoch,None


def _period(text,now=None):
    now=_now(now)
    matches=_date_matches(text,now)
    if matches is None:return None,None,None,None,PERIOD_ERROR
    if len(matches)>2:return None,None,None,None,PERIOD_ERROR
    if len(matches)==2:
        first,second=matches
        between=text[first[0].end():second[0].start()]
        if not RANGE_RE.search(between):return None,None,None,None,PERIOD_ERROR
        end=second[1]+(datetime.timedelta(days=1) if not second[2] else datetime.timedelta())
        return _range(first[1],end)
    times=list(TIME_RE.finditer(text))
    if len(matches)==1:
        match,value,has_time=matches[0]
        if len(times)==2 and RANGE_RE.search(text[times[0].end():times[1].start()]):
            day=value.date()
            start=datetime.datetime.combine(day,datetime.time(int(times[0].group(1)),int(times[0].group(2)),int(times[0].group(3) or 0)),BJT)
            end=datetime.datetime.combine(day,datetime.time(int(times[1].group(1)),int(times[1].group(2)),int(times[1].group(3) or 0)),BJT)
            if end<=start:end+=datetime.timedelta(days=1)
            return _range(start,end)
        if has_time:
            if re.search(r'(?:至今|到现在|到当前|以来)\s*$',text):
                return _range(value,now)
            return None,None,None,None,'精确时间范围请同时提供开始和结束时间。'
        start=value.replace(hour=0,minute=0,second=0,microsecond=0)
        if re.search(r'(?:至今|到现在|到当前|以来|起)\s*$',text):return _range(start,now)
        return _range(start,start+datetime.timedelta(days=1))
    if len(times)==2 and RANGE_RE.search(text[times[0].end():times[1].start()]):
        day=now.date()-datetime.timedelta(days=1) if '昨天' in text else now.date()-datetime.timedelta(days=2) if '前天' in text else now.date()
        start=datetime.datetime.combine(day,datetime.time(int(times[0].group(1)),int(times[0].group(2)),int(times[0].group(3) or 0)),BJT)
        end=datetime.datetime.combine(day,datetime.time(int(times[1].group(1)),int(times[1].group(2)),int(times[1].group(3) or 0)),BJT)
        if end<=start:end+=datetime.timedelta(days=1)
        return _range(start,end)
    if re.search(r'全部时间|所有时间|完整历史|全部历史|历史累计|累计|总计以来|所有记录',text):
        return 'all',15,None,None,None
    if re.search(r'前天',text):
        start=(now-datetime.timedelta(days=2)).replace(hour=0,minute=0,second=0,microsecond=0)
        return _range(start,start+datetime.timedelta(days=1))
    if re.search(r'昨天|昨日',text):
        start=(now-datetime.timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        return _range(start,start+datetime.timedelta(days=1))
    if re.search(r'上周',text):
        start=(now-datetime.timedelta(days=now.weekday()+7)).replace(hour=0,minute=0,second=0,microsecond=0)
        return _range(start,start+datetime.timedelta(days=7))
    if re.search(r'本周',text):
        start=(now-datetime.timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        return _range(start,now)
    if re.search(r'上个月|上月',text):
        current=_month_start(now);start=_subtract_months(current,1)
        return _range(start,current)
    if re.search(r'本月',text):return _range(_month_start(now),now)
    duration=DURATION_RE.search(text)
    if duration:
        amount=_number(duration.group(1));unit=duration.group(2)
        if not amount:return None,None,None,None,PERIOD_ERROR
        if unit in {'分钟','分'}:start=now-datetime.timedelta(minutes=amount)
        elif unit=='小时':start=now-datetime.timedelta(hours=amount)
        elif unit=='天':start=now-datetime.timedelta(days=amount)
        elif unit in {'周','星期'}:start=now-datetime.timedelta(weeks=amount)
        elif unit in {'月','个月'}:start=_subtract_months(now,amount)
        else:start=_subtract_months(now,amount*12)
        if amount==1 and unit=='小时':return 'window',60,None,None,None
        if amount==1 and unit=='天':return 'window',1440,None,None,None
        if amount==15 and unit in {'分钟','分'}:return 'window',15,None,None,None
        if amount==24 and unit=='小时':return 'window',1440,None,None,None
        return _range(start,now)
    return 'today',15,None,None,None


def _targets(text,aliases):
    found=[]
    for target,names in aliases.items():
        if not isinstance(target,str) or not SUBJECT_RE.fullmatch(target):continue
        positions=([text.find(name) for name in names if isinstance(name,str) and name and name in text]
                   if isinstance(names,list) else [])
        if positions:found.append((min(positions),target))
    return [target for _,target in sorted(found)]


def request_for(text,profile,aliases=None):
    aliases=ALIASES if aliases is None else aliases
    if not isinstance(text,str) or not (TOKEN_RE.search(text) or COST_RE.search(text) or USAGE_RE.search(text)):
        return None,None
    if DIAGNOSTIC_RE.search(text) or EXPLANATION_RE.search(text):return None,None

    ranking=bool((RANK_RE.search(text) or COMPARE_ALL_RE.search(text))
                 and (TOKEN_RE.search(text) or COST_RE.search(text) or USAGE_RE.search(text)))
    commentary=bool(COMMENTARY_RE.search(text))
    token_requested=bool(TOKEN_RE.search(text))
    cost_requested=bool(COST_RE.search(text))
    both_requested=bool(re.search(r'一起|同时|两种|都查|全部指标',text))
    if ranking or both_requested:token_requested=cost_requested=True
    if not token_requested and not cost_requested:token_requested=True

    try:period,window,range_start,range_end,error=_period(text)
    except (OverflowError,OSError,ValueError):return None,PERIOD_ERROR
    if error:return None,error
    metrics=[]
    if token_requested:metrics.append('tokens')
    if cost_requested:metrics.append('usd')
    if ranking:target='all'
    else:
        matched=_targets(text,aliases)
        if len(matched)>1:target=matched
        elif matched:target=matched[0]
        else:
            unknown=re.search(r'([\w\u4e00-\u9fff-]{1,32})的(?:token|tokens|用量|额度)',text,re.I)
            if unknown and '我的' not in text:
                return None,'这个群昵称还没有对应的用量权限配置，请联系管理员核实。'
            target=profile.get('usage_target') if isinstance(profile,dict) else None
        if not isinstance(target,(str,list)) or (isinstance(target,str) and not SUBJECT_RE.fullmatch(target)):
            return None,'你的微信成员身份尚未绑定用量Key，请联系管理员配置。'
        if isinstance(target,list) and (not target or len(target)>10 or any(not SUBJECT_RE.fullmatch(x) for x in target)):
            return None,'指定的成员范围无法核实，请使用已配置的群昵称。'

    request={'op':'member_usage','target':target,'metrics':metrics,'period':period,
             'window_minutes':window,'rank':ranking}
    if period=='range':request.update(range_start_epoch=range_start,range_end_epoch=range_end)
    if commentary:request['commentary']=True
    return request,None


def _count(value,unit='auto',scale=None):
    return format_tokens(value,scale)


def _amount(value):
    return format_money(value)[0]


def _token_line(item,unit,include_name=False,names=None,rank=None):
    fields=['ordinary_input_tokens','cache_read_tokens','cache_write_tokens','input_tokens','output_tokens','total_tokens']
    prefix=f'{rank}. ' if rank is not None else ''
    if not item.get('binding_available'):
        return prefix+'当前授权关联不可用，未改查其他Key。'
    if any(type(item.get(key)) is not int or item[key]<0 for key in fields):
        raise ValueError('invalid_token_result')
    if item['input_tokens']!=sum(item[key] for key in fields[:3]) or item['total_tokens']!=item['input_tokens']+item['output_tokens']:
        raise ValueError('invalid_token_totals')
    label=''
    if include_name:
        subject=item.get('subject');label=(names or {}).get(subject,NAMES.get(subject))
        if not isinstance(label,str) or not label:return '排行榜成员显示映射不完整，本次不显示个人数据。'
        label+='：'
    return prefix+label+f'合计 {format_token_quantity(item["total_tokens"])}。'


def _usage_comment(token_items,usd_items,names=None):
    names=names or {}
    notes=[];unit_hints=set()
    for items,field,label in [(token_items,'total_tokens','Token'),(usd_items,'usd_amount','费用')]:
        values=[]
        for item in items:
            if not item.get('binding_available'):continue
            try:value=item[field] if field=='total_tokens' else decimal_amount(item[field])
            except (KeyError,TypeError,ValueError):continue
            values.append((value,names.get(item.get('subject'),NAMES.get(item.get('subject'),'该成员'))))
        if len(values)<2:continue
        low=min(values,key=lambda item:item[0]);high=max(values,key=lambda item:item[0])
        if high[0]==low[0]:
            notes.append(f'{label}用量相同')
            continue
        if field=='total_tokens':
            difference=format_tokens(high[0]-low[0])
            scale,unit=token_scale(high[0]-low[0]);difference+=(' '+unit)
        else:
            difference,unit=format_money(high[0]-low[0]);unit_hints.add(unit)
        notes.append(f'{high[1]}的{label}消耗高于{low[1]}，差约{difference}')
    if not notes:return '当前可用汇总不足以比较成员差异；单个成员的用量也不能说明其工作表现。'
    hint=[]
    for unit,description in [('mUSD','千分之一美元'),('µUSD','百万分之一美元'),('nUSD','十亿分之一美元'),('pUSD','万亿分之一美元'),
                             ('KUSD','千美元'),('MUSD','百万美元'),('BUSD','十亿美元')]:
        if unit in unit_hints:hint.append(f'{unit}为{description}')
    suffix=('（'+ '；'.join(hint)+'）') if hint else ''
    return '数据趋势：'+'；'.join(notes)+suffix+'。这只反映API调用与费用，不代表工作量、效率或工作表现。'


def _period_label(data):
    period=data.get('period')
    if period=='today':return '北京时间今天00:00至今'
    if period=='window':return f'最近{data.get("window_minutes")}分钟'
    if period=='all':return '全部当前保留的使用记录'
    if period!='range':raise ValueError('invalid_usage_period')
    start=data.get('range_start_epoch');end=data.get('range_end_epoch')
    if type(start) is not int or type(end) is not int or start>=end:raise ValueError('invalid_usage_range')
    first=datetime.datetime.fromtimestamp(start,BJT);last=datetime.datetime.fromtimestamp(end,BJT)
    if first.time()==datetime.time() and last.time()==datetime.time():
        final_day=(last-datetime.timedelta(days=1)).date()
        if first.date()==final_day:return '北京时间'+first.strftime('%Y-%m-%d')
        return '北京时间'+first.strftime('%Y-%m-%d')+'至'+final_day.strftime('%Y-%m-%d')
    return '北京时间'+first.strftime('%Y-%m-%d %H:%M:%S')+'至'+last.strftime('%Y-%m-%d %H:%M:%S')


def format_member_usage(result,tone='default',unit='auto',names=None,commentary=False):
    if not isinstance(result,dict) or result.get('state')!='ok':
        return '暂时没有取得可核对的用量，请稍后再查；本次不猜测数字。'
    data=result.get('data',{})
    if not isinstance(data,dict):return '统计结果格式异常，本次不提供数字。'
    try:period=_period_label(data)
    except (OverflowError,OSError,TypeError,ValueError):return '统计范围格式异常，本次不提供数字。'
    ranking=data.get('rank') is True
    token_items=data.get('token_items',[]);usd_items=data.get('usd_items',[])
    if not isinstance(token_items,list) or not isinstance(usd_items,list):
        return '统计结果格式异常，本次不提供数字。'
    if not ranking and not token_items and not usd_items:
        return '当前没有可核对的授权用量数据，请联系管理员确认成员关联。'
    tone_lead='查到啦，' if tone=='gentle' else '按实际计量记录看：' if tone=='sharp' else ''
    lines=[]
    if ranking:
        lines.append(tone_lead+period+'（仅统计已绑定成员，各取前10）')
        lines.append('Token用量排行：')
        if token_items:
            try:
                rendered=[_token_line(item,unit,True,names,i) for i,item in enumerate(token_items[:10],1)]
                if any(line.startswith('排行榜成员显示映射不完整') for line in rendered):return rendered[0]
                lines.extend(rendered)
            except (TypeError,ValueError):return 'Token统计格式异常，本次不提供未经核实的数字。'
        else:lines.append('暂无已记录用量。')
        lines.append('Sub2API美元额度消耗排行：')
        fee_rows=[item for item in usd_items[:10] if item.get('binding_available')]
        if not fee_rows:lines.append('暂无实际扣费记录。')
        for index,item in enumerate(fee_rows,1):
            subject=item.get('subject');label=(names or {}).get(subject,NAMES.get(subject))
            if not isinstance(label,str) or not label:return '排行榜成员显示映射不完整，本次不显示个人数据。'
            try:amount=_amount(item.get('usd_amount'))
            except (TypeError,ValueError):return '美元额度统计格式异常，本次不提供未经核实的数字。'
            lines.append(f'{index}. {label} {amount}')
    else:
        lines.append(tone_lead+period+'：')
        if token_items:
            try:lines.extend(_token_line(item,unit,len(token_items)>1,names) for item in token_items)
            except (TypeError,ValueError):return 'Token统计格式异常，本次不提供未经核实的数字。'
        if usd_items:
            for item in usd_items:
                if not item.get('binding_available'):
                    lines.append('当前授权关联不可用，未改查其他Key。');continue
                try:amount=_amount(item.get('usd_amount'))
                except (TypeError,ValueError):return '美元额度统计格式异常，本次不提供未经核实的数字。'
                subject=item.get('subject');label=(names or {}).get(subject,NAMES.get(subject))
                lines.append((label+'：' if len(usd_items)>1 and label else '')+f'Sub2API费用消耗 {amount}。')
    if commentary:lines.append(_usage_comment(token_items,usd_items,names))
    at=datetime.datetime.fromtimestamp(result['captured_at'],datetime.timezone(datetime.timedelta(hours=8))).strftime('%m-%d %H:%M:%S')
    lines.append('统计截至'+at+('。费用合计来自Sub2API使用记录的实际扣费字段；如显示mUSD/µUSD等则为换算单位。' if usd_items else '；计量记录不等于成功请求次数。'))
    return '\n'.join(lines)


def format_usage(result,tone='default',unit='auto'):
    """Legacy formatter for model-planned service diagnostics."""
    if result.get('state')!='ok':return '暂时没有取得可核对的用量，请稍后再查；本次不猜测数字。'
    data=result.get('data',{});items=data.get('items',[])
    if not items:return '当前没有可核对的授权用量数据，请联系管理员检查关联状态。'
    try:period=_period_label(data)
    except (OverflowError,OSError,TypeError,ValueError):return '统计范围格式异常，本次不提供数字。'
    lines=[('查到啦，' if tone=='gentle' else '按实际计量记录看：' if tone=='sharp' else '')+period+'：']
    for item in items:
        subject=item.get('subject');label=NAMES.get(subject,'授权成员')+'：' if len(items)>1 else ''
        if not item.get('binding_available'):
            lines.append(label+'当前授权关联不可用，未改查其他Key。');continue
        try:
            content=_token_line(item,unit,False)
            lines.append(label+content)
        except (TypeError,ValueError):lines.append(label+'统计格式异常，本次不提供未经核实的数字。')
    at=datetime.datetime.fromtimestamp(result['captured_at'],datetime.timezone(datetime.timedelta(hours=8))).strftime('%m-%d %H:%M:%S')
    lines.append('统计截至'+at+'；计量记录不等于成功请求次数。')
    return '\n'.join(lines)
