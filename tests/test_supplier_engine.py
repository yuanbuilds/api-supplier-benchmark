"""Supplier adapter regression cases: timing, failure gates and secret boundary."""
import json
import tempfile
import unittest
from pathlib import Path

from workbench.engines.supplier_stream import SSEDecoder, StreamContractError, StrictTextStream
from workbench.engines.evalscope import build_commands, prepare_supplier_config, supplier_worker_environment
from workbench.reports.evalscope import build_result_row


def event(content=None, reasoning=None, finish=None):
    delta = {}
    if content is not None:
        delta['content'] = content
    if reasoning is not None:
        delta['reasoning_content'] = reasoning
    return json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})


class StrictTextStreamTests(unittest.TestCase):
    def test_reasoning_and_answer_have_distinct_nonblank_clocks(self):
        stream = StrictTextStream()
        stream.accept(event(content=''), .01)
        stream.accept(event(content='  '), .02)
        stream.accept(event(reasoning='think'), .04)
        stream.accept(event(content='答案'), .12)
        stream.accept(event(finish='stop'), .13)
        stream.accept('[DONE]', .14)
        stream.validate()
        self.assertEqual(stream.first_generated_seconds, .04)
        self.assertEqual(stream.first_content_seconds, .12)

    def test_body_without_reasoning_starts_both_clocks(self):
        stream = StrictTextStream(); stream.accept(event(content='text', finish='stop'), .2)
        stream.accept('[DONE]', .3); stream.validate()
        self.assertEqual(stream.first_generated_seconds, stream.first_content_seconds)

    def test_incomplete_and_empty_replies_do_not_pass(self):
        cases = [([event(content='text', finish='stop')], 'MISSING_DONE'),
                 ([event(reasoning='think', finish='stop'), '[DONE]'], 'EMPTY_BODY'),
                 ([event(content=' ', finish='stop'), '[DONE]'], 'EMPTY_BODY'),
                 ([event(content='text', finish='length'), '[DONE]'], 'TRUNCATED'),
                 ([event(content='text'), '[DONE]'], 'MISSING_OR_INVALID_FINISH')]
        for chunks, expected in cases:
            with self.subTest(expected=expected):
                stream = StrictTextStream()
                for chunk in chunks: stream.accept(chunk, .1)
                with self.assertRaisesRegex(StreamContractError, expected): stream.validate()

    def test_tools_errors_and_invalid_json_rejected(self):
        for payload in ['{', '{"error":{"message":"private upstream detail"}}',
                        '{"choices":[{"delta":{"tool_calls":[{"function":{"name":"x"}}]}}]}']:
            with self.subTest(payload=payload), self.assertRaises(StreamContractError):
                StrictTextStream().accept(payload, .1)

    def test_fragmented_utf8_crlf_and_sse_comments(self):
        raw = (': ping\r\n\r\ndata: ' + event(content='汉字') + '\r\n\r\ndata: [DONE]\r\n\r\n').encode()
        decoder = SSEDecoder(); parsed = []
        for byte in raw: parsed.extend(decoder.feed(bytes([byte])))
        parsed.extend(decoder.feed(b'', final=True))
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[-1], '[DONE]')

    def test_invalid_utf8_is_not_silently_replaced(self):
        with self.assertRaises(UnicodeDecodeError): SSEDecoder().feed(b'\xff')


class SupplierConfigurationTests(unittest.TestCase):
    def test_minimal_env_does_not_forward_unrelated_keys_or_proxies(self):
        env = supplier_worker_environment({'PATH': '/bin', 'MY_KEY': 'dummy-secret',
                                          'OTHER_API_KEY': 'never', 'HTTPS_PROXY': 'http://private'}, 'MY_KEY')
        self.assertEqual(env['WORKBENCH_SUPPLIER_KEY'], 'dummy-secret')
        self.assertNotIn('MY_KEY', env)
        self.assertNotIn('OTHER_API_KEY', env)
        self.assertNotIn('HTTPS_PROXY', env)
        self.assertEqual(env['WORKBENCH_SKIP_DOTENV'], '1')

    def test_fixed_rpm_compiles_official_trace_without_duration_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'input.jsonl'
            source.write_text(json.dumps({'messages':[{'role':'user','content':'test'}]})+'\n')
            test = {'supplier_acceptance':True,'modality':'text','dataset':'line_by_line',
                    'dataset_path':str(source),'stream':True,'number':[3],'concurrency':[1],
                    'requests_per_minute':200,'duration_seconds':.9,'timeout_seconds':60}
            path = prepare_supplier_config(test, {'model':'glm-exact-id','url':'https://example.com/v1/chat/completions'}, root)
            config = json.loads(path.read_text()); args = config['arguments']
            trace = [json.loads(s) for s in (root/'workload-trace.jsonl').read_text().splitlines()]
            self.assertEqual([r['timestamp'] for r in trace], [0,.3,.6])
            self.assertEqual(len({r['request_id'] for r in trace}), 3)
            self.assertTrue(args['open_loop']); self.assertTrue(args['no_test_connection'])
            self.assertNotIn('duration', args)
            self.assertEqual(args['parallel'], [0]); self.assertEqual(args['warmup_num'], 0)
            self.assertNotIn('api_key', args); self.assertNotIn('tokenizer_path', args)
            self.assertNotIn('ignore_eos', trace[0]['body'])
            self.assertEqual(trace[0]['body']['model'], 'glm-exact-id')

    def test_invalid_rate_count_cannot_generate_false_200rpm_plan(self):
        test = {'supplier_acceptance':True,'modality':'text','dataset':'line_by_line','dataset_path':'x',
                'stream':True,'number':[2],'requests_per_minute':200,'duration_seconds':60}
        with self.assertRaisesRegex(ValueError, 'number must equal'):
            build_commands({'test':test,'targets':{'p':['m']}}, {'p':{'base_url':'https://example.com/v1'}})

    def test_secure_command_argv_has_no_authorization_argument(self):
        test = {'supplier_acceptance':True,'modality':'text','dataset':'line_by_line','dataset_path':'x',
                'stream':True,'number':[1],'concurrency':[1]}
        commands = build_commands({'test':test,'targets':{'p':['m']}},
                                  {'p':{'base_url':'https://example.com/v1','api_key_env':'SUPPLIER_API_KEY'}})
        self.assertNotIn('--headers', commands[0]['command_template'])
        self.assertNotIn('--api-key', commands[0]['command_template'])


class UpgradedEvalScopeParserTests(unittest.TestCase):
    def test_current_summary_ttft_and_missing_percentile_after_all_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'benchmark_summary.json').write_text(json.dumps({'Total Requests':1,'Success Requests':0,'Failed Requests':1,'Avg TTFT (ms)':0}))
            row = build_result_row({'run_id':'r'}, {'provider':'p','model':'m','status':'success','supplier_acceptance':True},root)
            self.assertEqual(row['ttft_avg_ms'],0)
            self.assertEqual(row['failed_requests'],1)
            self.assertEqual(row['actual_requests_per_minute'],'')
            self.assertEqual(row['ttft_p95_ms'],'')


if __name__ == '__main__': unittest.main()
