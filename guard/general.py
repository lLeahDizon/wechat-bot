"""General answers with optional public research and at most two model calls."""
import re
from .model import call,ModelFailure
from .public_web import query_text,public_url

CURRENT=re.compile(r'今天|明天|昨天|目前|现在|最新|实时|最近|新闻|天气|价格|政策|法规|法律|药物|用药|症状|疾病|股票|基金|投资|网址|官网|链接|查一下|查查|上网|查询|搜索|查资料|202[6-9]|20[3-9]\d')
QUESTION_REVIEW=re.compile(r'(?:评价|点评|评论|评估|分析).{0,24}(?:问题|提问|问法)|(?:问题|提问|问法).{0,24}(?:评价|点评|评论|评估|分析)',re.I)
QUOTED_QUESTION=re.compile(r'[“「『"](.+?)[”」』"]',re.S)


def question_review_request(text):
    if not isinstance(text,str) or not QUESTION_REVIEW.search(text):return None
    quoted=QUOTED_QUESTION.search(text)
    if quoted and quoted.group(1).strip():return quoted.group(1).strip()
    prefixed=re.search(r'(?:问题|提问|问法)\s*[:：]\s*(.+)$',text,re.S)
    if prefixed and prefixed.group(1).strip():return prefixed.group(1).strip()
    return ''


def review_question(question,key_file,api_base,model_call=call):
    data={'domain':'general','phase':'question_review','question':question,
          'context':[],'facts':[],'web_urls':[],'diagnostic':False,'tone':'default'}
    try:result,usage=model_call(data,key_file,api_base)
    except ModelFailure as error:return {'failure':error.code},[{}]
    return result,[usage]


def answer(data,key_file,api_base,research,model_call=call):
    usages=[];required=bool(CURRENT.search(data['question']) or data.get('web_urls'))
    plan={**data,'domain':'general','phase':'general_plan','requires_research':required,
          'allowed_research':{'max_queries':2,'max_pages':3,'public_https_only':True,'max_model_calls':2}}
    try:first,usage=model_call(plan,key_file,api_base);usages.append(usage)
    except ModelFailure as error:return {'failure':error.code},[{}],[]
    first.pop('_verified_public_urls',None)
    queries=first.get('web_queries') or []
    if not isinstance(queries,list):queries=[]
    if required and not queries:
        # Do not export an entire chat/history or server evidence to a search
        # engine; only the already-redacted current question can be a fallback.
        queries=[re.sub(r'\[待核实的公开链接\]','',data['question']).strip()[:180]]
    safe=[]
    for query in queries[:2]:
        try:safe.append(query_text(query))
        except ValueError:pass
    urls=[]
    for value in data.get('web_urls',[])[:2]:
        try:urls.append(public_url(value))
        except (ValueError,UnicodeError):pass
    if not safe and not urls and not required:
        return first,usages,[]
    try:proof=research.collect(safe,urls)
    except Exception:proof={'kind':'public_research','state':'unavailable','sources':[],'search_results':[]}
    if not proof.get('sources'):
        # Search snippets alone do not verify changing facts. A deterministic
        # failure keeps the bot from confidently inventing a current answer.
        return {'scope':'service','answer':'这次未取得足够可靠的网页正文，暂不能核实这个问题。可以补充具体时间、地区或公开资料链接，我再查。',
                'refs':['web_data'],'queries':[],'learning':''},usages,['web_data']
    final={**data,'domain':'general','phase':'general_answer','web_data':proof,'requires_research':required}
    try:result,usage=model_call(final,key_file,api_base);usages.append(usage)
    except ModelFailure as error:return {'failure':error.code},usages+[{}],['web_data']
    result['_verified_public_urls']=list(dict.fromkeys(url for source in proof['sources']
        for url in [source.get('url'),source.get('final_url')] if isinstance(url,str)))
    return result,usages,['web_data']
