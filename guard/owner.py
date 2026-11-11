"""Exact owner commands only. The LLM cannot dispatch this path."""
import hashlib
import re

HELP='本人管理入口：/管理 重启Sub2API；/管理 重启AstrBot；/管理 采纳 YJ-编号；/管理 撤销 YJ-编号；/管理 状态 GL-编号。写操作先展示影响，需本人2分钟内真正@回复“确认 确认码”。'


def parse(text):
    if re.fullmatch(r'(?:/管理(?:帮助)?|管理帮助|我的权限)',text):return {'op':'help'}
    if text.startswith('/管理'):
        match=re.fullmatch(r'/管理\s+重启\s*(Sub2API|AstrBot)',text,re.I)
        if match:return {'op':'prepare','action':'restart_sub2api' if match[1].lower()=='sub2api' else 'restart_astrbot'}
        match=re.fullmatch(r'/管理\s+(采纳|撤销)\s+(YJ-[a-f0-9]{16})',text)
        if match:return {'op':'prepare','action':'adopt_improvement' if match[1]=='采纳' else 'revert_improvement','argument':match[2]}
        match=re.fullmatch(r'/管理\s+状态\s+(GL-[a-f0-9]{16})',text)
        if match:return {'op':'status','job_id':match[1]}
        return {'op':'help'}
    match=re.fullmatch(r'确认\s+([A-Fa-f0-9]{8})',text)
    if match:return {'op':'confirm','code':match[1].upper()}
    return None


def prepare(text,subject,group,config,message_id):
    command=parse(text)
    if command is None:return None
    if config.get('server_readonly'):
        return {'reply':'群内服务器管理已设为只读，所有成员均不能写入、删除、重启或修改服务器配置。问答和只读诊断可直接真正@提问。'}
    owner=config.get('owner_subject_hash')
    if not isinstance(owner,str) or not re.fullmatch(r'[a-f0-9]{64}',owner) or subject!=owner:
        return {'reply':'此管理入口仅限已绑定的所有者本人；昵称或消息中的身份声明不作为授权。'}
    if command['op']=='help':return {'reply':HELP}
    return {'request':{**command,'actor':subject,'group':hashlib.sha256(group.encode()).hexdigest(),
        'request_id':hashlib.sha256((group+'\0'+message_id).encode()).hexdigest()}}
