import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec=importlib.util.spec_from_file_location('native_control',Path(__file__).resolve().parents[1]/'admin/native_control.py')
control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)


class LoginRecoveryTests(unittest.TestCase):
    def test_terminal_login_receipt_remains_readable_on_exit_nine(self):
        receipt={'id':'attempt-1','state':'failed','result':{'code':'login_poll_failed'}}
        result=SimpleNamespace(returncode=9,stdout=json.dumps(receipt),stderr='')
        with patch.object(control.subprocess,'run',return_value=result):
            self.assertEqual(control.cli(['auth','attempts','get','attempt-1'],terminal_operation=True),receipt)

    def test_nonzero_unrelated_response_is_not_accepted_as_receipt(self):
        result=SimpleNamespace(returncode=9,stdout=json.dumps({'id':'different','state':'failed'}),stderr='')
        with patch.object(control.subprocess,'run',return_value=result):
            with self.assertRaises(RuntimeError):
                control.cli(['auth','attempts','get','attempt-1'],terminal_operation=True)

    def test_live_login_cannot_be_replaced_by_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'login-operation.json';path.write_text('{"id":"attempt-1"}')
            with patch.object(control,'LOGIN',path),patch.object(control,'status',return_value={'authenticated':False,'connection':'disconnected'}),patch.object(control,'operation',return_value=({}, {'state':'awaiting_scan'}, {})),patch.object(control,'cli') as cli:
                with self.assertRaisesRegex(RuntimeError,'login_operation_still_active'):
                    control.login_qr(refresh=True)
                self.assertEqual(json.loads(path.read_text())['id'],'attempt-1')
                cli.assert_not_called()

    def test_authenticated_disconnected_account_cannot_be_relogged(self):
        with patch.object(control,'status',return_value={'authenticated':True,'connection':'disconnected'}),patch.object(control,'cli') as cli:
            with self.assertRaisesRegex(RuntimeError,'authenticated_account_requires_review'):
                control.login_qr(refresh=True)
            cli.assert_not_called()

    def test_succeeded_disconnected_login_archives_both_receipts_before_new_qr(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);evidence=root/'admin-evidence';evidence.mkdir()
            qr_dir=root/'native-data/private-qr';qr_dir.mkdir(parents=True)
            account=evidence/'native-account.json';account.write_text('{"id":"account-1"}')
            login=evidence/'login-operation.json';login.write_text('{"id":"attempt-1"}')
            previous=evidence/'login-operation.previous.json';previous.write_text('{"id":"older-attempt"}')
            operations=[({'id':'attempt-1'},{'state':'succeeded'},{}),
                ({'id':'attempt-2'},{'state':'awaiting_scan'},{'qrAvailable':True})]
            calls=[]
            def fake_cli(args,account_id=None,**kwargs):
                calls.append(args)
                if args[:2]==['auth','login']:
                    return {'operation':{'id':'attempt-2'}}
                if args[:2]==['auth','qr']:
                    (qr_dir/Path(args[4]).name).write_bytes(b'fixture-png')
                    return {'qrGeneration':2}
                raise AssertionError('unexpected_native_command')
            with patch.object(control,'ROOT',root),patch.object(control,'ACCOUNT',account),patch.object(control,'LOGIN',login),\
                 patch.object(control,'status',return_value={'authenticated':False,'connection':'disconnected'}),\
                 patch.object(control,'operation',side_effect=operations),patch.object(control,'cli',side_effect=fake_cli),\
                 patch.object(control.os,'chown'):
                result=control.login_qr(refresh=True)
            archived=list(evidence.glob('login-operation.previous-*.json'))
            self.assertTrue(result['qr_available'])
            self.assertEqual(json.loads(login.read_text())['id'],'attempt-2')
            self.assertEqual(json.loads(previous.read_text())['id'],'older-attempt')
            self.assertEqual(json.loads(archived[0].read_text())['id'],'attempt-1')
            self.assertEqual(len(list(qr_dir.glob('login-*.png'))),1)
            self.assertEqual(sum(args[:2]==['auth','login'] for args in calls),1)

    def test_succeeded_operation_with_non_disconnected_state_is_not_refreshed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'login-operation.json';path.write_text('{"id":"attempt-1"}')
            with patch.object(control,'LOGIN',path),patch.object(control,'status',return_value={'authenticated':False,'connection':'online'}),\
                 patch.object(control,'operation',return_value=({}, {'state':'succeeded'}, {})),patch.object(control,'cli') as cli:
                with self.assertRaisesRegex(RuntimeError,'login_operation_still_active'):
                    control.login_qr(refresh=True)
                self.assertEqual(json.loads(path.read_text())['id'],'attempt-1')
                cli.assert_not_called()


if __name__=='__main__':unittest.main()
