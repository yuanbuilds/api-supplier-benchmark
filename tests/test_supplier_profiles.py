"""Versioned supplier profiles preserve workloads and private boundaries."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from workbench import supplier
from workbench.reports.supplier import build_report
from test_supplier_report import record, write_phase


class SupplierProfileTests(unittest.TestCase):
    def config(self):
        return supplier.validate_config(dict(schema_version=1, profile=supplier.KIMI_PROFILE,
            supplier='public-fixture', base_url='https://example.com/v1', model='kimi-k3',
            region='fixture-region', reasoning_mode='reasoning_effort=low'))

    def test_kimi_exact_plan_has_no_glm_threshold_or_rpm(self):
        config = self.config()
        plan = supplier.make_plan(config)
        self.assertEqual(plan['planned_requests'], 33)
        self.assertEqual(plan['output_token_upper_bound'], 33792)
        self.assertEqual([(s['name'],s['count'],s['concurrency']) for s in plan['phases']],
            [('connectivity',1,1),('c1-r1',10,1),('c1-r2',10,1),('c4',12,4)])
        self.assertTrue(all(s['rpm'] is None for s in plan['phases']))
        self.assertTrue(all(v is None for v in config['thresholds'].values()))
        self.assertEqual(supplier.make_plan(config, True, True)['planned_requests'], 33)

    def test_profiles_cannot_silently_change_standard(self):
        config = self.config()
        for change in [dict(max_output_tokens=4096),dict(extra_body={'temperature':0}),
                       dict(thresholds={'ttft_p50_seconds':1}),dict(profile='unknown')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                supplier.validate_config({**config,**change})
        with self.assertRaisesRegex(ValueError,'不一致'):
            supplier.validate_config(config, profile=supplier.DEFAULT_PROFILE)
        old = {k:v for k,v in config.items() if k not in ('profile','max_output_tokens','thresholds')}
        self.assertEqual(supplier.validate_config(old)['max_output_tokens'],4096)

    def test_kimi_inputs_keep_originals_and_explicit_effective_parameters(self):
        config = self.config()
        profile = supplier.load_profile(supplier.KIMI_PROFILE)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            _,batches=supplier.prepare_inputs(root,config,profile['phases'])
            hashes=json.loads((root/'inputs/dataset-hashes.json').read_text())
            for stage in profile['phases']:
                batch=json.loads(batches[stage['name']].read_text());test=batch['test']
                self.assertEqual(test['concurrency'],[stage['concurrency']])
                self.assertEqual(test['number'],[stage['count']])
                self.assertEqual(test['warmup_num'],0)
                self.assertFalse(test['test_connection'])
                self.assertEqual(test['timeout_seconds'],60)
                self.assertEqual(test['connect_timeout_seconds'],10)
                actual=[json.loads(l) for l in Path(test['dataset_path']).read_text().splitlines()]
                self.assertEqual(len(actual),stage['count'])
                for body in actual:
                    self.assertEqual(body['max_tokens'],1024)
                    self.assertEqual(body['model'],'kimi-k3')
                    self.assertTrue(body['stream'])
                    for k,v in profile['request_defaults'].items():self.assertEqual(body[k],v)
                if stage.get('dataset'):
                    original=root/'inputs/original'/stage['dataset']
                    self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),stage['original_dataset_sha256'])
                    messages=[json.loads(l)['messages'] for l in original.read_text().splitlines()]
                    self.assertEqual([b['messages'] for b in actual],messages)
                    self.assertNotEqual(hashes[stage['name']+'.jsonl'],hashes['original/'+stage['dataset']])
            self.assertEqual(json.loads(batches['c4'].read_text())['test']['supplier_workload'],'concurrency')

    def test_tampered_profile_dataset_fails_closed(self):
        with patch.object(supplier,'sha256',return_value='wrong'),self.assertRaisesRegex(ValueError,'校验失败'):
            supplier.load_profile(supplier.KIMI_PROFILE)

    def test_plan_cli_does_not_start_request_and_uses_kimi_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'target.json';config.write_text(json.dumps(self.config()))
            with patch.object(supplier,'execute') as execute,redirect_stdout(StringIO()) as output:
                rc=supplier.main(['plan','--profile',supplier.KIMI_PROFILE,'--config',str(config)])
            self.assertEqual(rc,0);execute.assert_not_called()
            self.assertIn('33 次请求',output.getvalue())
            self.assertNotIn('最后 200 RPM',output.getvalue())

    def test_report_is_kimi_measurement_and_keeps_phases_separate(self):
        plan=supplier.make_plan(self.config(),demo=True)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            plan['phase_runs']=[]
            for stage in plan['phases']:
                write_phase(root/stage['name'],[record(i+1,i*.2,ttft=.04+i*.001) for i in range(stage['count'])])
                plan['phase_runs'].append({**stage,'status':'completed','exit_code':0,'run_dir':stage['name']})
            (root/'manifest.json').write_text(json.dumps(plan))
            summary=build_report(root)
            self.assertEqual(summary['assessment'],'measurement_only')
            self.assertEqual(summary['valid_success'],33)
            self.assertEqual(summary['profile'],supplier.KIMI_PROFILE)
            payload=json.loads((root/'report-data.json').read_text())
            self.assertIn('Kimi K3',payload['title'])
            self.assertFalse(any('RPM' in m['label'] for m in payload['metrics']))
            self.assertEqual(len(payload['rows']),4)
            self.assertTrue(all(p['ttft_p50_check'] is None for p in summary['phases']))


if __name__ == '__main__':unittest.main()
