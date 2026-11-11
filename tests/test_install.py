import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('install',Path(__file__).resolve().parents[1]/'admin/install.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_reads_framework_config_with_or_without_utf8_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cmd_config.json'
            for encoding in ['utf-8','utf-8-sig']:
                path.write_text(json.dumps({'platform':[{'enable':False}]}),encoding=encoding)
                self.assertFalse(installer.read_json(path)['platform'][0]['enable'])


if __name__=='__main__':unittest.main()
