"""Installed before framework imports and again before message processing.

No user text, provider responses, identifiers or exception strings reach either
the standard logging broker or loguru. Container log retention is disabled too.
"""
import logging
import re

_original_factory = logging.getLogRecordFactory()
_original_add_handler = logging.Logger.addHandler
_SAFE = re.compile(r"^(?:boot|stopped|reply_accepted|reply_uncertain|no_reply|model_failure|internal_failure)(?: code=[a-z0-9_]{1,40})?$")
_STANDARD_FIELDS = set(logging.LogRecord('runtime',30,'',0,'',(),None).__dict__)


class MetadataOnly(logging.Filter):
    def filter(self, record):
        if getattr(record,'category',None)=='user_chat':
            return False
        for key in set(record.__dict__)-_STANDARD_FIELDS:
            record.__dict__.pop(key,None)
        record.exc_info=record.exc_text=record.stack_info=None
        return True


_filter=MetadataOnly()


def add_safe_handler(logger, handler):
    handler.addFilter(_filter)
    return _original_add_handler(logger,handler)


def safe_factory(*args, **kwargs):
    record = _original_factory(*args, **kwargs)
    if record.name != 'sub2api_guard' or not isinstance(record.msg, str) or record.args or not _SAFE.fullmatch(record.msg):
        record.msg = 'runtime_event'
    record.name = 'runtime'
    record.args = ()
    record.exc_info = record.exc_text = record.stack_info = None
    record.pathname = record.filename = record.module = record.funcName = 'runtime'
    record.lineno = 0
    return record


def patch_loguru(record):
    # Standard records have already been made anonymous; direct loguru messages
    # must pass the same strict fixed-event whitelist.
    if not isinstance(record.get('message'), str) or not _SAFE.fullmatch(record['message']):
        record['message'] = 'runtime_event'
    record['exception'] = None
    record['extra'] = {}


def install():
    logging.setLogRecordFactory(safe_factory)
    logging.Logger.addHandler=add_safe_handler
    for logger in [logging.getLogger(),*logging.Logger.manager.loggerDict.values()]:
        if not isinstance(logger,logging.Logger):
            continue
        logger.addFilter(_filter)
        for handler in logger.handlers:
            handler.addFilter(_filter)
    try:
        from loguru import logger
        logger.configure(patcher=patch_loguru)
    except ImportError:
        pass
