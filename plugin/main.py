import asyncio
import json
import logging
import time
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from diagnostics.client import QueryBudget

from .guard.controller import diagnose
from .guard.core import FAILED, Engine, Incoming, digest, mask_labels, unsafe
from .guard.general import answer as general_answer
from .guard.general import review_question
from .guard.history import (
    build_group_context,
    build_investment_context,
    merge_group_history,
)
from .guard.improvements import review as review_feedback
from .guard.investment import run_isolated as run_investment
from .guard.league import LeagueKnowledge, catalogue
from .guard.model import ModelFailure, call
from .guard.public_web import PublicResearch
from .guard.reminder_delivery import ReminderDelivery
from .guard.usage import format_member_usage

POLICY = Path('/run/bot-config/policy.json')
FAQ = Path('/run/bot-config/faq.json')
SNAPSHOT = Path('/run/bot-status/public.json')
STATE = Path('/AstrBot/data/sub2api_guard/state.sqlite')
LOL_CATALOGUE = Path('/run/bot-config/lol-catalogue.json')
KEY = Path('/run/secrets/sub2api-key')
LOGGER = logging.getLogger('sub2api_guard')


class Sub2APIGuard(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        from privacy import install
        install()
        config = json.loads(POLICY.read_text())
        faq = json.loads(FAQ.read_text())
        # Validate operator-maintained public facts before receiving any events.
        if any(unsafe(entry['answer']) for entry in faq):
            raise RuntimeError('public_faq_validation_failed')
        seed=catalogue(LOL_CATALOGUE)
        self.league=LeagueKnowledge(STATE.parent/'lol-cache',seed)
        self.research=PublicResearch(STATE.parent/'public-web-cache',key_file=KEY)
        self.engine = Engine(config, faq, STATE, SNAPSHOT,league_catalog=seed)
        self.member_locks = {}
        self.processing = asyncio.Semaphore(2)
        self.cleaner = None
        self.reminder_task=None
        self.reminder_delivery=ReminderDelivery(self.engine.reminders)
        self.reminder_state='starting'
        self.reminder_tick_at=0
        self._heartbeat()
        LOGGER.warning('boot')

    def _heartbeat(self):
        (STATE.parent/'heartbeat.json').write_text(json.dumps({'at':time.time(),'guard':'ready',
            'policy_enabled':bool(self.engine.config.get('enabled')),
            'diagnostics_enabled':bool(self.engine.config.get('diagnostics_enabled')),
            'monitor_enabled':bool(self.engine.config.get('monitor_enabled')),
            'feedback_enabled':bool(self.engine.config.get('feedback_enabled')),
            'lol_enabled':bool(self.engine.config.get('lol_enabled')),
            'general_enabled':bool(self.engine.config.get('general_enabled')),
            'server_readonly':bool(self.engine.config.get('server_readonly')),
            'reminders_enabled':bool(self.engine.config.get('reminders_enabled')),
            'reminders_state':self.reminder_state,'reminders_tick_at':self.reminder_tick_at,
            'improvement_mode':self.engine.config.get('improvement_mode')})+'\n')

    async def initialize(self):
        self.cleaner = asyncio.create_task(self._expire_memory())
        self.reminder_task=asyncio.create_task(self._reminder_loop())

    async def _reminder_loop(self):
        while True:
            try:
                policy=json.loads(POLICY.read_text())
                if policy.get('group_id')!=self.engine.config.get('group_id'):
                    self.reminder_state='wrong_group'
                else:
                    adapter=self.context.get_platform_inst(policy['bot_instance_id'])
                    self.reminder_state=await self.reminder_delivery.tick(adapter,policy)
                self.reminder_tick_at=time.time()
            except Exception:
                self.reminder_state='unavailable';self.reminder_tick_at=time.time()
            self._heartbeat()
            await asyncio.sleep(10)

    async def _expire_memory(self):
        while True:
            await asyncio.sleep(60)
            self.engine.purge()
            self._heartbeat()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def guard_message(self, event: AstrMessageEvent):
        if event.get_platform_name() != 'wechat_native':
            return
        event.stop_event()
        if event.get_platform_id() != self.engine.config['bot_instance_id']:
            event.finish_no_reply()
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            event.finish_no_reply()
            return
        mentions=raw.get('mentions',{})
        mentioned=isinstance(mentions,dict) and mentions.get('state')=='known' and mentions.get('self') is True
        if event.get_group_id()!=self.engine.config.get('group_id'):
            event.finish_no_reply()
            return
        if not mentioned:
            event.finish_no_reply()
            return
        subject=digest(event.get_group_id() or '',event.get_sender_id() or '')
        lock=self.member_locks.setdefault(subject,asyncio.Lock())
        async with self.processing,lock:
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                return
            mentions = raw.get('mentions', {})
            mentioned = isinstance(mentions, dict) and mentions.get('state') == 'known' and mentions.get('self') is True
            incoming = Incoming(group=event.get_group_id() or '', sender=event.get_sender_id() or '',
                message_id=str(event.message_obj.message_id or ''), text=event.get_message_str() or '',
                mentioned=mentioned, timestamp=float(event.message_obj.timestamp),
                from_self=event.get_sender_id() == event.get_self_id())
            decision = None
            try:
                decision = self.engine.prepare(incoming)
                if (decision.model_input and decision.intent not in {'feedback','question_review'}):
                    adapter=event.adapter
                    try:
                        history=await adapter.group_history(incoming.group)
                    except Exception:
                        LOGGER.warning('Native 持久群历史读取失败；继续使用可用的实时上下文')
                        history=None
                    if history is None:
                        persisted=()
                        has_older=False
                        history_state='unavailable'
                    else:
                        persisted,has_older=history
                        history_state='partial' if has_older else 'available'
                    cache=getattr(adapter,'summary_cache',None)
                    sequence=event.get_extra('wechat_native_context_sequence')
                    live=cache.snapshot_before(incoming.group,sequence) if cache is not None and isinstance(sequence,int) else ()
                    records=merge_group_history(persisted,live,incoming.group,incoming.message_id,
                        incoming.timestamp,event.get_self_id(),max_records=200)
                    if decision.intent=='investment':
                        own_profile=self.engine.config.get('member_profiles',{}).get(decision.subject,{})
                        own_target=own_profile.get('usage_target') if isinstance(own_profile,dict) else None
                        other_aliases=[alias for target,names in self.engine.config.get('usage_public_aliases',{}).items()
                            if target!=own_target for alias in names if isinstance(alias,str)]
                        decision.model_input['personal_context']=build_investment_context(records,
                            incoming.message_id,incoming.sender,self.engine.config.get('hidden_label_rules',[]),
                            excluded_aliases=other_aliases)
                    else:
                        context=build_group_context(records,incoming.message_id,
                            self.engine.config.get('hidden_label_rules',[]))
                        if not context and history_state=='available':history_state='empty'
                        elif context and history_state=='unavailable':history_state='partial'
                        decision.model_input['group_context']=context
                        decision.model_input['group_context_state']=history_state
                if decision.admin_request:
                    decision.reply='群内服务器管理仅开放只读排查，不执行写入、删除、重启或配置变更。'
                elif decision.usage_request:
                    decision.contains_personal_usage=True
                    evidence=await asyncio.to_thread(QueryBudget().query,decision.usage_request)
                    decision.evidence_ids.add(evidence['id'])
                    decision.reference=evidence['id']
                    decision.reply=format_member_usage(evidence,decision.tone,decision.token_unit,
                        self.engine.config.get('usage_public_names',{}),
                        commentary=decision.usage_request.get('commentary') is True)
                elif decision.model_input:
                    try:
                        if decision.intent=='feedback':
                            result,usages=await asyncio.to_thread(review_feedback,decision.model_input,KEY,self.engine.config['api_base'])
                            for usage in usages:self.engine.record_usage(usage)
                            if result.get('failure'):raise ModelFailure(result['failure'])
                        elif decision.intent=='question_review':
                            result,usages=await asyncio.to_thread(review_question,decision.model_input['question'],KEY,self.engine.config['api_base'])
                            for usage in usages:self.engine.record_usage(usage)
                            if result.get('failure'):raise ModelFailure(result['failure'])
                        elif decision.intent=='general':
                            result,usages,proof=await asyncio.to_thread(general_answer,decision.model_input,KEY,self.engine.config['api_base'],self.research)
                            for usage in usages:self.engine.record_usage(usage)
                            decision.evidence_ids.update(proof)
                            decision.public_urls.update(result.pop('_verified_public_urls',[]))
                            if result.get('failure'):raise ModelFailure(result['failure'])
                        elif decision.intent=='investment':
                            result,usages,proof=await asyncio.to_thread(run_investment,decision.model_input,KEY,self.engine.config['api_base'])
                            for usage in usages:self.engine.record_usage(usage)
                            decision.evidence_ids.update(proof)
                            decision.public_urls.update(result.pop('_verified_public_urls',[]))
                            if result.get('failure'):raise ModelFailure(result['failure'])
                        elif decision.intent=='lol':
                            result,usage=await asyncio.to_thread(self.league.answer,decision.model_input,KEY,self.engine.config['api_base'])
                            self.engine.record_usage(usage);decision.evidence_ids.add('lol_data')
                            self.engine.league_catalog.update(champions=self.league.champions,
                                item_names=list(self.league.terms),rune_names=[],version=self.league.current_version)
                        elif self.engine.config.get('diagnostics_enabled'):
                            result,usages,evidence=await asyncio.to_thread(diagnose,decision.model_input,KEY,self.engine.config['api_base'])
                            for usage in usages:self.engine.record_usage(usage)
                            decision.evidence_ids.update(item['id'] for item in evidence)
                            decision.contains_personal_usage=any(item.get('op')=='token_usage' for item in evidence)
                            if result.get('failure'):raise ModelFailure(result['failure'])
                        else:
                            result,usage=await asyncio.to_thread(call,decision.model_input,KEY,self.engine.config['api_base'])
                            self.engine.record_usage(usage)
                        self.engine.interpret(decision,result)
                    except ModelFailure as error:
                        if decision.intent=='lol' or (decision.intent not in {'feedback','investment'} and not self.engine.config.get('diagnostics_enabled')):self.engine.record_usage({})
                        if decision.intent=='feedback':self.engine.reject_feedback(decision,'verification_unavailable')
                        else:decision.reply, decision.category = FAILED, 'model_failure'
                        LOGGER.warning('model_failure code=' + error.code)
                if not decision.reply:
                    self.engine.mark_delivery(incoming, 'no_reply')
                    event.finish_no_reply()
                    return
                decision.reply=mask_labels(decision.reply,self.engine.config.get('hidden_label_rules',[]))
                if self.engine.output_unsafe(decision.reply,decision):
                    decision.reply = '答复未通过信息检查，本次不发送详情。请联系管理员核实。'
                await event.send(MessageChain().message(decision.reply))
                status = event.adapter.state.message_status(event.message_obj.message_id)
                self.engine.delivered(incoming, decision, status)
                LOGGER.warning('reply_accepted' if status == 'replied' else 'reply_uncertain')
            except Exception:
                # The adapter keeps uncertain outcomes and its existing idempotency
                # key. Do not retry or print the event/exception/raw response.
                self.engine.mark_delivery(incoming, 'needs_review')
                event.finish_needs_review()
                LOGGER.warning('internal_failure')
            finally:
                event.stop_event()

    @filter.on_plugin_error()
    async def suppress_error(self, event: AstrMessageEvent, *args, **kwargs):
        if event.get_platform_name() == 'wechat_native':
            event.stop_event()

    async def terminate(self):
        if self.reminder_task:
            self.reminder_task.cancel()
            try:await self.reminder_task
            except asyncio.CancelledError:pass
        if self.cleaner:
            self.cleaner.cancel()
            try:
                await self.cleaner
            except asyncio.CancelledError:
                pass
        self.engine.contexts.clear()
        self.engine.last_domains.clear()
        self.engine.feedback_clarifications.clear()
        self.engine.db.close()
        LOGGER.warning('stopped')
