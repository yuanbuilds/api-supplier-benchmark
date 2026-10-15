"""Public supplier workflow: bounded plans, private inputs and safe result export."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from workbench import supplier


class SupplierOrchestrationTests(unittest.TestCase):
    def config(self):
        return dict(schema_version=1, supplier='fixture', base_url='https://example.com/v1',
                    model='glm-exact', region='fixture-region', reasoning_mode='default')

    def test_full_plan_is_exact_and_does_not_call_provider(self):
        plan = supplier.make_plan(supplier.validate_config(self.config()))
        self.assertEqual(plan['planned_requests'], 706)
        self.assertEqual(plan['output_token_upper_bound'], 2891776)
        self.assertEqual([(p['rpm'], p['count'], p['window_seconds']) for p in plan['phases'][-3:]],
                         [(50,25,30),(100,50,30),(200,600,180)])
        self.assertEqual(plan['retries'], 0)
        self.assertEqual(sum(p['count'] for p in supplier.phases(True)), 207)

    def test_invalid_auth_and_nonfinite_thresholds_rejected(self):
        for change in [dict(api_key='fake'), dict(base_url='http://example.com/v1'),
                       dict(base_url='https://user:password@example.com/v1'),
                       dict(base_url='https://example.com/v1?key=fake'),
                       dict(base_url='https://example.com/v1/chat/completions'),
                       dict(extra_body={'nested':[{'Authorization':'fake'}]}),
                       dict(extra_body={'stream':False}), dict(max_output_tokens=True),
                       dict(thresholds={'ttft_p95_seconds':float('nan')}),
                       dict(thresholds={'min_success_rate':1.01})]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                supplier.validate_config({**self.config(), **change})

    def test_demo_cannot_contact_external_endpoint(self):
        with self.assertRaises(ValueError): supplier.validate_config(self.config(), demo=True)
        c=supplier.validate_config({**self.config(), 'base_url':'http://127.0.0.1:1234/v1'}, demo=True)
        self.assertEqual(c['max_output_tokens'],4096)

    def test_isolated_env_cannot_consume_ambient_private_catalog_or_key(self):
        with patch.dict(os.environ, {'UNRELATED_API_KEY':'do-not-forward','HTTPS_PROXY':'private-proxy',
                                     'WORKBENCH_PROVIDERS_FILE':'private.yaml'}, clear=True):
            env=supplier.isolated_env('dummy-key', Path('/tmp/fixture-providers.yaml'))
        self.assertNotIn('UNRELATED_API_KEY',env)
        self.assertEqual(env['HTTPS_PROXY'],'')
        self.assertEqual(env['WORKBENCH_SKIP_DOTENV'],'1')
        self.assertEqual(env['WORKBENCH_PROVIDERS_FILE'],'/tmp/fixture-providers.yaml')
        self.assertEqual(env['SUPPLIER_API_KEY'],'dummy-key')

    def test_generated_batches_have_only_planned_requests_and_no_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            provider,batches=supplier.prepare_inputs(out,supplier.validate_config(self.config()),supplier.phases())
            for phase in supplier.phases():
                batch=json.loads(batches[phase['name']].read_text())
                self.assertTrue(batch['test']['supplier_acceptance'])
                self.assertFalse(batch['test']['test_connection'])
                self.assertEqual(batch['test']['warmup_num'],0)
                bodies=[json.loads(s) for s in Path(batch['test']['dataset_path']).read_text().splitlines()]
                self.assertEqual(len(bodies),phase['count'])
                self.assertTrue(all(b['stream'] and b['max_tokens']==4096 for b in bodies))
            self.assertEqual(json.loads(provider.read_text())['supplier']['api_key_env'],'SUPPLIER_API_KEY')

    def test_result_zip_is_integral_and_blocks_binary_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'result';out.mkdir()
            (out/'benchmark_data.db').write_bytes(b'\x00binary-dummy-key\xff')
            with self.assertRaisesRegex(ValueError,'阻止生成'):
                supplier.archive_results(out,'dummy-key')
            self.assertFalse((out.parent/'result.zip').exists())
            (out/'benchmark_data.db').write_bytes(b'\x00no-credential\xff')
            (out/'summary.json').write_text('{"complete": true}\n')
            archive=supplier.archive_results(out,'dummy-key')
            with zipfile.ZipFile(archive) as bundle:
                self.assertIsNone(bundle.testzip())
                for line in bundle.read('result/SHA256SUMS.txt').decode().splitlines():
                    expected,path=line.split('  ',1)
                    self.assertEqual(expected,hashlib.sha256(bundle.read('result/'+path)).hexdigest())
                self.assertTrue(all(not n.startswith('/') and '..' not in Path(n).parts for n in bundle.namelist()))


if __name__=='__main__': unittest.main()
