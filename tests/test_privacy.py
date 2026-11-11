import importlib.util
import logging
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('bot_privacy', Path(__file__).resolve().parents[1]/'runtime/privacy.py')
privacy=importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class PrivacyTests(unittest.TestCase):
    def test_factory_removes_dynamic_text_arguments_and_exceptions(self):
        try:
            raise ValueError('synthetic-private-value')
        except ValueError:
            import sys
            record=privacy.safe_factory('astrbot.plugin.anything', logging.ERROR,
                '/opt/private/path.py',42,'request %s',('synthetic-private-value',),sys.exc_info())
        self.assertEqual(record.getMessage(),'runtime_event')
        self.assertIsNone(record.exc_info)
        self.assertEqual(record.pathname,'runtime')
        self.assertNotIn('synthetic-private-value',str(record.__dict__))

    def test_only_fixed_guard_metadata_survives(self):
        record=privacy.safe_factory('sub2api_guard',logging.WARNING,'',0,'model_failure code=http_429',(),None)
        self.assertEqual(record.getMessage(),'model_failure code=http_429')
        other=privacy.safe_factory('sub2api_guard',logging.WARNING,'',0,'user said a private thing',(),None)
        self.assertEqual(other.getMessage(),'runtime_event')

    def test_direct_loguru_messages_are_anonymous(self):
        record={'message':'synthetic-private-value','exception':'private exception','extra':{'prompt':'private'}}
        privacy.patch_loguru(record)
        self.assertEqual(record,{'message':'runtime_event','exception':None,'extra':{}})

    def test_extra_fields_are_filtered_after_record_creation(self):
        record=privacy.safe_factory('astrbot',logging.ERROR,'',0,'private',(),None)
        record.private_payload={'user_text':'private'}
        self.assertTrue(privacy.MetadataOnly().filter(record))
        self.assertFalse(hasattr(record,'private_payload'))
        record.category='user_chat'
        self.assertFalse(privacy.MetadataOnly().filter(record))


if __name__=='__main__':unittest.main()
