"""Supplier adapter regression cases: timing, failure gates and secret boundary."""
import base64
import json
import os
import pickle
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class SupplierCredentialPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real adapter and native DB writer without a network socket."""

    secret = 'synthetic-not-a-live-credential-718bf3'

    async def process(self, payloads, status=200):
        from workbench.engines.supplier_api import StrictSupplierPlugin

        class Content:
            async def iter_any(self):
                if status != 200:
                    raise AssertionError('HTTP error body must not be read')
                for payload in payloads:
                    yield ('data: ' + (payload if isinstance(payload, str) else json.dumps(payload)) + '\n\n').encode()

        class Response:
            headers = {'Content-Type': 'text/event-stream'}
            content = Content()

            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False

        response = Response()
        response.status = status

        class Session:
            def post(self, **kwargs): return response

        plugin = object.__new__(StrictSupplierPlugin)
        plugin.sequence, plugin.first_start = 0, None
        with patch.dict(os.environ, {}, clear=True):
            output = await plugin.process_request(Session(), 'https://not-contacted.invalid/v1/chat/completions',
                                                  {'Authorization': 'Bearer ' + self.secret},
                                                  {'model': 'fixture', 'messages': [], 'stream': True})
        output.finalize(plugin)
        return output

    def assert_safe_native_archive(self, output, expected_usage):
        from evalscope.perf.utils.db_util import create_result_table, insert_benchmark_data
        from workbench.supplier import archive_results

        self.assertEqual(output.generated_text, '')
        self.assertEqual(output.response_messages, expected_usage)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'result'
            root.mkdir()
            database = root / 'benchmark_data.db'
            with sqlite3.connect(database) as connection:
                create_result_table(connection.cursor())
                insert_benchmark_data(connection.cursor(), output)
                encoded = connection.execute('SELECT response_messages FROM result').fetchone()[0]
                if output.success:
                    self.assertEqual(connection.execute('SELECT prompt_tokens, completion_tokens FROM result').fetchone(),
                                     (output.prompt_tokens, output.completion_tokens))
            # Decode only this test's own native data; never unpickle external reports.
            decoded = pickle.loads(base64.b64decode(encoded))
            self.assertEqual(decoded, expected_usage)
            self.assertNotIn(self.secret, json.dumps(decoded))
            self.assertNotIn(self.secret.encode(), database.read_bytes())
            self.assertTrue(archive_results(root, self.secret).is_file())

    async def test_split_echo_and_dictionary_keys_never_enter_native_database(self):
        first = {'choices': [{'delta': {'content': self.secret[:16]}, 'finish_reason': None}],
                 self.secret: {'metadata': self.secret}}
        second = {'choices': [{'delta': {'content': self.secret[16:]}, 'finish_reason': 'stop'}],
                  'metadata': {self.secret: self.secret},
                  'usage': {'prompt_tokens': 17, 'completion_tokens': 42, 'extra': self.secret}}
        output = await self.process([first, second, '[DONE]'])
        self.assertTrue(output.success)
        self.assertGreaterEqual(output.first_chunk_latency, 0)
        self.assertEqual((output.prompt_tokens, output.completion_tokens), (17, 42))
        self.assert_safe_native_archive(output, [{'usage': {'prompt_tokens': 17, 'completion_tokens': 42}}])

    async def test_invalid_usage_values_and_metadata_are_not_retained(self):
        payload = {'choices': [{'delta': {'content': 'valid text'}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': self.secret, 'completion_tokens': True, self.secret: self.secret}}
        output = await self.process([payload, '[DONE]'])
        self.assertTrue(output.success)
        self.assertEqual((output.prompt_tokens, output.completion_tokens), (0, 0))
        self.assert_safe_native_archive(output, [{'usage': {}}])

    async def test_stream_error_echo_is_reduced_to_fixed_error_code(self):
        output = await self.process([{'error': {'message': self.secret, self.secret: self.secret}}])
        self.assertFalse(output.success)
        self.assertEqual(output.error, 'API_STREAM_ERROR')
        self.assert_safe_native_archive(output, [])

    async def test_http_error_body_is_never_read_or_retained(self):
        output = await self.process([{'error': {'message': self.secret}}], status=401)
        self.assertFalse(output.success)
        self.assertEqual(output.error, 'HTTP_401')
        self.assert_safe_native_archive(output, [])


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
