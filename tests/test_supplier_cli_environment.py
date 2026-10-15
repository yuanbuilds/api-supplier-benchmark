"""Public CLI dotenv policy, using a temporary synthetic .env only."""
import json
import os
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from helpers import ROOT, load_module
from workbench import config


class SupplierCliEnvironmentTests(unittest.TestCase):
    def prepare(self, directory):
        root = Path(directory)
        (root / '.env').write_text('SYNTHETIC_DOTENV_KEY=synthetic-not-a-real-key\n', encoding='utf-8')
        paths = {}
        for label, strict in [('supplier', True), ('legacy', False)]:
            path = root / (label + '.yaml')
            path.write_text(json.dumps({'schema_version': 1, 'name': label,
                                        'test': {'engine': 'evalscope_perf', 'supplier_acceptance': strict}}))
            paths[label] = path
        return root, paths

    def test_direct_runner_skips_dotenv_for_supplier_but_keeps_legacy(self):
        runner = load_module(ROOT / 'scripts/run_benchmark.py', 'supplier_direct_cli_env')
        with tempfile.TemporaryDirectory() as temporary:
            root, paths = self.prepare(temporary)
            for label in ('supplier', 'legacy'):
                seen = []
                def validate(*args, **kwargs):
                    seen.append(os.environ.get('SYNTHETIC_DOTENV_KEY'))
                    return {}, ['Stop before any execution'], []
                args = SimpleNamespace(batch_file=str(paths[label]), confirm_cost=False, result_file=None)
                with patch.dict(os.environ, {}, clear=True), patch.object(config, 'ROOT', root), \
                     patch.object(runner, 'parse_args', return_value=args), \
                     patch.object(runner, 'validate_batch', side_effect=validate), \
                     patch.object(runner, 'write_result_handoff'), \
                     redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(runner.main(), 2)
                self.assertEqual(seen, [None if label == 'supplier' else 'synthetic-not-a-real-key'])

    def test_multi_batch_check_does_not_leak_dotenv_in_either_order(self):
        checker = load_module(ROOT / 'scripts/check_env.py', 'supplier_multi_cli_env')
        with tempfile.TemporaryDirectory() as temporary:
            root, paths = self.prepare(temporary)
            for order in [('legacy', 'supplier'), ('supplier', 'legacy')]:
                seen = []
                def preflight(path):
                    available = 'SYNTHETIC_DOTENV_KEY' in os.environ
                    seen.append((path.stem, available))
                    return {'ready': available, 'errors': [] if available else ['explicit key required'], 'warnings': []}
                with patch.dict(os.environ, {'EXPLICIT_KEY': 'explicit-test-value'}, clear=True), \
                     patch.object(config, 'ROOT', root), patch.object(checker, 'ROOT', root), \
                     patch.object(checker, 'preflight_payload', side_effect=preflight), \
                     patch('sys.argv', ['check_env.py', *[str(paths[name]) for name in order], '--json']), \
                     redirect_stdout(StringIO()):
                    self.assertEqual(checker.main(), 1)
                    self.assertNotIn('SYNTHETIC_DOTENV_KEY', os.environ)
                    self.assertEqual(os.environ['EXPLICIT_KEY'], 'explicit-test-value')
                self.assertEqual(seen, [(name, name == 'legacy') for name in order])

    def test_supplier_only_preflight_never_calls_dotenv_loader(self):
        checker = load_module(ROOT / 'scripts/check_env.py', 'supplier_only_cli_env')
        with tempfile.TemporaryDirectory() as temporary:
            root, paths = self.prepare(temporary)
            with patch.object(checker, 'ROOT', root), patch.object(checker, 'load_dotenv') as dotenv, \
                 patch.object(checker, 'preflight_payload', return_value={'ready':True}), \
                 patch('sys.argv', ['check_env.py', str(paths['supplier']), '--json']), redirect_stdout(StringIO()):
                self.assertEqual(checker.main(), 0)
                dotenv.assert_not_called()

    def test_invalid_batch_is_reported_without_loading_dotenv(self):
        checker = load_module(ROOT / 'scripts/check_env.py', 'supplier_invalid_cli_env')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(checker, 'ROOT', root), patch.object(checker, 'load_dotenv') as dotenv, \
                 patch('sys.argv', ['check_env.py', 'missing.yaml', '--json']), redirect_stdout(StringIO()):
                self.assertEqual(checker.main(), 1)
                dotenv.assert_not_called()


if __name__ == '__main__': unittest.main()
