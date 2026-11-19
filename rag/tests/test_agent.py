"""Deterministic safety/contract checks. Scripted tools are not model inference."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent

REGISTRY = agent.load_registry()
ROWS = REGISTRY['rows']
EPISODE = json.loads((ROOT / 'data/sources/episode.json').read_text())
RECEIPTS = {}


def response(action, probabilities=None):
    probabilities = probabilities or {label: float(label == action) for label in agent.ACTIONS}
    confidence = (max(probabilities.values()) - 0.25) / 0.75
    return {'answers': {'next_action': {'type': 'choice', 'choice': action,
            'confidence': confidence, 'probabilities': probabilities}}}


class FixtureRetriever:
    """A filter-aware lookup fixture, with no embedding model or ranking claim."""
    def __init__(self, batches=None):
        self.batches = batches or [['RC01-S2', 'RC01-S3', 'RC01-S4']]
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append({'query': query, **deepcopy(kwargs)})
        ids = self.batches[min(len(self.calls)-1, len(self.batches)-1)]
        hits = []
        for row_id in ids:
            row = next((row for row in ROWS if row['id'] == row_id), None)
            filters = kwargs.get('filters', {})
            if row is not None and (row['equipment'] != filters.get('equipment', row['equipment'])
                    or (row['mode'] and filters.get('process_mode') not in row['mode'])
                    or row['kind'] != filters.get('kind', row['kind'])):
                continue
            hits.append({'id': row_id, 'rank': len(hits)+1})
        return {'hits': hits[:kwargs.get('limit', 3)], 'timing_ms': {},
                'execution': {'backend': 'fixture_lookup_no_embedding', 'actual_model_calls': 0}}


class ScriptedBackend:
    name = 'scripted_contract_fixture'

    def __init__(self, actions):
        self.actions = actions
        self.states = []

    def decide(self, state):
        self.states.append(deepcopy(state))
        item = self.actions[min(len(self.states)-1, len(self.actions)-1)]
        return response(item) if isinstance(item, str) else deepcopy(item)


class AgentContractTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = deepcopy(EPISODE)
        self.rows = deepcopy(ROWS)
        self.runtime = agent.fixture_runtime()

    def run_case(self, name=None, **kwargs):
        packet = agent.run_incident(self.snapshot, self.rows, kwargs.pop('retriever', FixtureRetriever()),
                    runtime=kwargs.pop('runtime', self.runtime), **kwargs)
        json.dumps(packet, allow_nan=False)
        RECEIPTS[name or self.id().split('.')[-1]] = packet
        return packet

    def latest(self, tag='XMEAS(21)'):
        return next(row for row in reversed(self.snapshot['observation_prefix']) if row['tag'] == tag)

    def missing(self):
        result = deepcopy(self.latest())
        self.snapshot['observation_prefix'] = [row for row in self.snapshot['observation_prefix']
                                              if row['tag'] != 'XMEAS(21)']
        return result

    def assert_closed(self, packet):
        self.assertEqual(packet['action'], 'ESCALATE')
        self.assertEqual(packet['guidance_ko'], [])
        self.assertEqual(packet['citations'], [])

    def decisions(self, packet):
        return [step for step in packet['trace'] if 'decision_input' in step]

    def test_compound_ack_and_complete_source_order(self):
        packet = self.run_case()
        self.assertEqual(packet['action'], 'PROVIDE_SOP')
        state = self.decisions(packet)[0]['decision_input']['context']
        self.assertEqual(len(state['active_alarms']), 2)
        temp = next(item for item in state['active_alarms'] if item['alarm_definition_id'] == 'AL-01T')
        self.assertEqual(temp['ack_state'], 'ACKNOWLEDGED')
        self.assertEqual(temp['condition_state'], 'ACTIVE')
        self.assertEqual(len(state['alarm_groups']), 1)
        self.assertEqual(packet['evidence_ids'], [row['id'] for row in ROWS[:5]])
        self.assertEqual(packet['guidance_ko'], [row['ko'] for row in ROWS[:5]])
        self.assertEqual(packet['citations'], [{'id': row['id'], **row['source']} for row in ROWS[:5]])
        self.assertEqual(packet['retrieved_evidence_ids'], ['RC01-S2', 'RC01-S3', 'RC01-S4'])
        self.assertEqual(packet['procedure_expansion']['added_ids'], ['RC01-S1', 'RC01-S5'])
        self.assertEqual([h['id'] for h in packet['trace'][0]['output']['result']['hits']],
                         ['RC01-S2', 'RC01-S3', 'RC01-S4'])
        self.assertEqual(packet['zoro_status'], 'NOT_RUN')

    def test_clear_event_removes_only_its_instance(self):
        event = deepcopy(self.snapshot['published_alarm_events'][-1])
        event.update(alarm_event_id='test-clear-temp', condition_state='CLEARED')
        event['available_key']['event_seq'] = 49
        self.snapshot['published_alarm_events'].append(event)
        packet = self.run_case()
        self.assertEqual(packet['action'], 'PROVIDE_SOP')
        state = self.decisions(packet)[0]['decision_input']['context']
        self.assertEqual([item['alarm_definition_id'] for item in state['active_alarms']], ['AL-01P'])

    def test_clear_all_blocks_forced_provide(self):
        for i, source in enumerate(self.snapshot['published_alarm_events'][1:]):
            event = deepcopy(source)
            event.update(alarm_event_id=f'test-clear-{i}', condition_state='CLEARED')
            event['available_key'] = {'tick': 3, 'event_seq': 49+i}
            self.snapshot['published_alarm_events'].append(event)
        packet = self.run_case(decision_backend=ScriptedBackend(['PROVIDE_SOP']))
        self.assert_closed(packet)
        self.assertIn('NO_ACTIVE_ALARM', packet['gate']['reasons'])

    def test_missing_observation_consumed_by_next_decision(self):
        returned = self.missing()
        backend = ScriptedBackend(['REQUEST_INFO', 'PROVIDE_SOP'])
        packet = self.run_case(decision_backend=backend, observation_batches=[{'observations': [returned]}])
        self.assertEqual(packet['action'], 'PROVIDE_SOP')
        self.assertEqual(len(backend.states), 2)
        next_state = backend.states[1]
        self.assertEqual(next_state['context']['observations']['XMEAS(21)']['observation_id'], returned['observation_id'])
        self.assertEqual(next_state['tool_history'][0]['output']['observations'][0]['value_or_null'], returned['value_or_null'])
        self.assertEqual(packet['backend'], 'scripted_contract_fixture')
        self.assertEqual(packet['zoro_status'], 'NOT_RUN')

    def test_no_observation_is_explicit_and_consumed(self):
        self.missing()
        backend = ScriptedBackend(['REQUEST_INFO', 'REQUEST_INFO'])
        packet = self.run_case(decision_backend=backend)
        self.assert_closed(packet)
        self.assertNotIn('error', packet)
        steps = self.decisions(packet)
        self.assertEqual(steps[0]['tool_output']['status'], 'NO_OBSERVATION')
        self.assertEqual(backend.states[1]['tool_history'][0]['output']['status'], 'NO_OBSERVATION')
        self.assertEqual(steps[1]['proposed_action'], 'REQUEST_INFO')
        self.assertEqual(steps[1]['action'], 'ESCALATE')

    def test_latest_invalid_observation_is_never_backfilled(self):
        for field, value, issue in [('quality', 'BAD', 'BAD_QUALITY'), ('unit', 'psi', 'WRONG_UNIT'),
                                   ('value_or_null', float('nan'), 'NONFINITE_OR_MISSING_VALUE'),
                                   ('value_or_null', float('inf'), 'NONFINITE_OR_MISSING_VALUE'),
                                   ('value_or_null', True, 'NONFINITE_OR_MISSING_VALUE'),
                                   ('sampled_at_sim_h_or_null', 0.0, 'STALE'),
                                   ('sampled_at_sim_h_or_null', 0.04, 'INVALID_SAMPLE_TIME')]:
            with self.subTest(field=field, value=value):
                self.snapshot = deepcopy(EPISODE)
                self.latest()[field] = value
                packet = self.run_case(name=f'invalid-{field}-{value}')
                self.assert_closed(packet)
                step = self.decisions(packet)[0]
                self.assertEqual(step['action'], 'REQUEST_INFO')
                self.assertIn(issue, packet['gate']['invalid_tags'][0]['issues'])
                self.assertEqual(step['decision_input']['context']['observations']['XMEAS(21)']['observation_id'], self.latest()['observation_id'])

    def test_one_tick_age_is_inclusive(self):
        self.latest()['sampled_at_sim_h_or_null'] = 0.02
        self.assertEqual(self.run_case()['action'], 'PROVIDE_SOP')

    def test_forced_provide_cannot_bypass_missing_or_bad_values(self):
        self.missing()
        packet = self.run_case(decision_backend=ScriptedBackend(['PROVIDE_SOP']))
        self.assert_closed(packet)
        self.assertEqual(self.decisions(packet)[0]['proposed_action'], 'PROVIDE_SOP')

    def test_future_records_and_private_metadata_are_not_exposed(self):
        future = deepcopy(self.latest())
        future.update(observation_id='FUTURE_SECRET_ID', value_or_null=123456, available_at_sim_h=0.04)
        future['available_key'] = {'tick': 4, 'event_seq': 1}
        self.snapshot['observation_prefix'].append(future)
        future_event = deepcopy(self.snapshot['published_alarm_events'][-1])
        future_event.update(alarm_event_id='FUTURE_SECRET_EVENT', condition_state='CLEARED', available_at_sim_h=0.04)
        future_event['available_key'] = {'tick': 4, 'event_seq': 2}
        self.snapshot['published_alarm_events'].append(future_event)
        self.snapshot['hidden_fault_id'] = 'PRIVATE_SENTINEL'
        self.runtime['hidden_fault_id'] = 'PRIVATE_SENTINEL'
        self.latest('XMEAS(7)')['hidden_fault_id'] = 'PRIVATE_SENTINEL'
        self.snapshot['published_alarm_events'][0]['priority_reason'] = 'PRIVATE_SENTINEL'
        packet = self.run_case()
        serialized = json.dumps(packet)
        for value in ['FUTURE_SECRET_ID', 'FUTURE_SECRET_EVENT', 'PRIVATE_SENTINEL']:
            self.assertNotIn(value, serialized)
        self.assertEqual(packet['action'], 'PROVIDE_SOP')

    def test_future_tool_return_is_rejected(self):
        observation = self.missing()
        observation['available_at_sim_h'] = 0.04
        observation['available_key'] = {'tick': 4, 'event_seq': 1}
        packet = self.run_case(observation_batches=[{'observations': [observation]}])
        self.assert_closed(packet)
        self.assertIn('not-yet-published', packet['error']['message'])

    def test_later_cutoff_return_is_consumed(self):
        observation = self.missing()
        observation.update(observation_id='tool-new-at-tick4', value_or_null=96.3,
                           available_at_sim_h=0.04, sampled_at_sim_h_or_null=0.04)
        observation['available_key'] = {'tick': 4, 'event_seq': 1}
        packet = self.run_case(observation_batches=[{'observations': [observation],
                              'cutoff_key': {'tick': 4, 'event_seq': 2}, 't_decision_cutoff_sim_h': 0.04}])
        self.assertEqual(packet['action'], 'PROVIDE_SOP')
        self.assertEqual(self.decisions(packet)[1]['decision_input']['context']['cutoff_key']['tick'], 4)

    def test_cutoff_cannot_move_backwards(self):
        observation = self.missing()
        packet = self.run_case(observation_batches=[{'observations': [observation],
                             'cutoff_key': {'tick': 2, 'event_seq': 50}, 't_decision_cutoff_sim_h': 0.02}])
        self.assert_closed(packet)
        self.assertIn('backwards', packet['error']['message'])

    def test_tool_cannot_return_unrequested_tag_or_replace_ids(self):
        for variant in ['unrequested', 'duplicate']:
            with self.subTest(variant=variant):
                self.snapshot = deepcopy(EPISODE)
                self.latest()['quality'] = 'BAD'
                row = deepcopy(self.latest('XMEAS(7)' if variant == 'unrequested' else 'XMEAS(21)'))
                packet = self.run_case(name=f'tool-{variant}', observation_batches=[{'observations': [row]}])
                self.assert_closed(packet)
                self.assertIn('error', packet)

    def test_missing_runtime_and_identity_mismatches(self):
        variants = [('missing', {}), ('model', {**self.runtime, 'model_rev': 'actual-tep-model'}),
                    ('controller', {**self.runtime, 'controller_rev': 'different-controller'}),
                    ('ended', {**self.runtime, 'episode_status': 'FINISHED'}),
                    ('duration', {**self.runtime, 'tick_duration_sim_h': None})]
        for name, runtime in variants:
            with self.subTest(name=name):
                packet = self.run_case(name=f'identity-{name}', runtime=runtime,
                                      decision_backend=ScriptedBackend(['PROVIDE_SOP']))
                self.assert_closed(packet)
                self.assertTrue(packet['gate']['reasons'])

    def test_mode_sensor_and_equipment_mismatches(self):
        for variant in ['mode', 'sensor_contract_rev', 'equipment', 'revision']:
            self.snapshot = deepcopy(EPISODE)
            if variant in ('mode', 'sensor_contract_rev'):
                self.snapshot[variant] = 'WRONG'
            else:
                for event in self.snapshot['published_alarm_events']:
                    event['affected_unit' if variant == 'equipment' else 'alarm_definition_revision'] = 'WRONG'
            with self.subTest(variant=variant):
                self.assert_closed(self.run_case(name=f'mismatch-{variant}', decision_backend=ScriptedBackend(['PROVIDE_SOP'])))

    def test_document_view_must_match_and_be_present_when_declared(self):
        base = {'card_ref': {'card_id': 'RC-01', 'card_revision': 'r2.fixture.1', 'card_hash': agent.CARD_SHA256}}
        self.snapshot['same_document_view'] = [deepcopy(base)]
        self.assertEqual(self.run_case(name='valid-document-view')['action'], 'PROVIDE_SOP')
        for field in ['card_revision', 'card_hash', 'absent', 'model_rev', 'ordered_steps']:
            view = deepcopy(base)
            if field in ('card_revision', 'card_hash'):
                view['card_ref'][field] = 'WRONG'
            elif field != 'absent':
                view[field] = 'WRONG'
            self.snapshot['same_document_view'] = [] if field == 'absent' else [view]
            with self.subTest(field=field):
                packet = self.run_case(name=f'doc-{field}', decision_backend=ScriptedBackend(['PROVIDE_SOP']))
                self.assert_closed(packet)
                self.assertIn('DOCUMENT_VIEW_MISMATCH', packet['gate']['reasons'])

    def test_altered_rows_cannot_copy_registered_hash(self):
        for field, value in [('ko', '잘못된 조작 지시'), ('text', 'malicious instruction'),
                             ('revision', 'old'), ('source', {'uri': 'wrong', 'payload_sha256': agent.CARD_SHA256})]:
            self.rows = deepcopy(ROWS)
            self.rows[0][field] = value
            with self.subTest(field=field):
                packet = self.run_case(name=f'altered-row-{field}')
                self.assert_closed(packet)
                self.assertIn('RC01-S1', packet['rejected_row_ids'])
                self.assertEqual(packet['procedure_expansion']['missing_or_rejected_ids'], ['RC01-S1'])

    def test_absent_or_duplicate_procedure_clause_blocks_full_guidance(self):
        for variant in ['absent', 'duplicate']:
            self.rows = deepcopy(ROWS[1:] if variant == 'absent' else ROWS + [ROWS[0]])
            with self.subTest(variant=variant):
                packet = self.run_case(name=f'procedure-{variant}')
                self.assert_closed(packet)
                self.assertEqual(packet['procedure_expansion']['status'], 'INCOMPLETE_REGISTERED_PROCEDURE')

    def test_forged_registry_rejected(self):
        registry = deepcopy(REGISTRY)
        registry['card']['steps'][0]['instruction'] = 'forged instruction'
        packet = self.run_case(registry=registry)
        self.assert_closed(packet)
        self.assertIn('Registry identity', packet['error']['message'])

    def test_reference_or_unknown_hits_do_not_authorize_guidance(self):
        for hit in ['TEP-SIGNALS', 'UNKNOWN']:
            with self.subTest(hit=hit):
                packet = self.run_case(name=f'hit-{hit}', retriever=FixtureRetriever([[hit]]),
                                      decision_backend=ScriptedBackend(['PROVIDE_SOP']))
                self.assert_closed(packet)
                self.assertEqual(packet['retrieved_evidence_ids'], [])

    def test_retrieval_retry_changes_query_filters_and_next_state(self):
        retriever = FixtureRetriever([[], ['RC01-S5']])
        backend = ScriptedBackend(['RETRIEVE_AGAIN', 'PROVIDE_SOP'])
        packet = self.run_case(retriever=retriever, decision_backend=backend)
        self.assertEqual(packet['action'], 'PROVIDE_SOP')
        self.assertNotEqual(retriever.calls[0]['query'], retriever.calls[1]['query'])
        self.assertEqual(retriever.calls[1]['filters']['kind'], 'research_sop')
        self.assertEqual([row['id'] for row in backend.states[1]['evidence']], ['RC01-S5'])
        self.assertEqual(backend.states[1]['tool_history'][0]['output']['result']['hits'][0]['id'], 'RC01-S5')
        self.assertEqual(packet['retrieved_evidence_ids'], ['RC01-S5'])

    def test_empty_retry_stops(self):
        retriever = FixtureRetriever([[]])
        packet = self.run_case(retriever=retriever)
        self.assert_closed(packet)
        self.assertEqual(len(retriever.calls), 2)
        self.assertEqual([step['action'] for step in self.decisions(packet)], ['RETRIEVE_AGAIN', 'ESCALATE'])

    def test_step_limit_bounds_scripted_backend(self):
        backend = ScriptedBackend(['RETRIEVE_AGAIN'])
        packet = self.run_case(decision_backend=backend, max_steps=2)
        self.assert_closed(packet)
        self.assertEqual(len(backend.states), 2)
        self.assertEqual(packet['trace'][-1]['output']['status'], 'STEP_LIMIT_REACHED')

    def test_bad_model_response_is_preserved_and_closed(self):
        raw = response('PROVIDE_SOP')
        raw['answers']['next_action']['probabilities']['ESCALATE'] = 0.5
        packet = self.run_case(decision_backend=ScriptedBackend([raw]))
        self.assert_closed(packet)
        self.assertEqual(self.decisions(packet)[0]['raw_model_response'], raw)
        self.assertEqual(self.decisions(packet)[0]['tool_output']['status'], 'ERROR')

    def test_typed_distribution_validation(self):
        valid = response('PROVIDE_SOP', {'PROVIDE_SOP': 0.6667, 'REQUEST_INFO': 0.1111,
                                       'RETRIEVE_AGAIN': 0.1111, 'ESCALATE': 0.1111})
        self.assertEqual(agent.parse_zoro_response(valid), 'PROVIDE_SOP')
        variants = []
        for field, value in [('type', 'text'), ('choice', 'ACTUATE'), ('confidence', float('nan')),
                             ('confidence', -1), ('confidence', True)]:
            bad = deepcopy(valid)
            bad['answers']['next_action'][field] = value
            variants.append(bad)
        for value in [float('nan'), float('inf'), -0.1, 1.1, True]:
            bad = deepcopy(valid)
            bad['answers']['next_action']['probabilities']['PROVIDE_SOP'] = value
            variants.append(bad)
        for mode in ['missing', 'extra', 'sum', 'argmax']:
            bad = deepcopy(valid)
            probabilities = bad['answers']['next_action']['probabilities']
            if mode == 'missing': probabilities.pop('ESCALATE')
            elif mode == 'extra': probabilities['ACTUATE'] = 0.0
            elif mode == 'sum': probabilities['PROVIDE_SOP'] = 0.5
            else: bad['answers']['next_action']['choice'] = 'REQUEST_INFO'
            variants.append(bad)
        for index, bad in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ValueError):
                agent.parse_zoro_response(bad)

    def test_http_serialization_for_both_official_envelopes(self):
        captures = []
        def fake_open(request, timeout):
            captures.append({'body': json.loads(request.data), 'method': request.get_method(), 'timeout': timeout})
            return io.BytesIO(json.dumps(response('ESCALATE')).encode())
        for path in ['decide', 'systemone']:
            client = agent.ZoroHTTPBackend(f'http://127.0.0.1:30001/v1/{path}', timeout=2)
            with patch('agent.urlopen', side_effect=fake_open):
                result = client.decide({'public': 'example'})
            self.assertEqual(agent.parse_zoro_response(result), 'ESCALATE')
            body = captures[-1]['body']
            payload = body['request'] if path == 'decide' else body
            self.assertEqual(payload['state'], {'public': 'example'})
            self.assertEqual(set(payload['questions']['next_action']['criteria']), set(agent.ACTIONS))
            self.assertIs(body['reasoning'], False)
            self.assertEqual(captures[-1]['method'], 'POST')
            self.assertEqual(captures[-1]['timeout'], 2)
            if path == 'systemone': self.assertEqual(body['model'], 'trillionlabs/Zoro-1')
        RECEIPTS['http_transport_mock'] = {'status': 'PASS', 'actual_network_calls': 0,
                                         'actual_model_calls': 0, 'requests': captures}

    def test_http_client_rejects_bad_endpoint_and_response(self):
        for endpoint in ['file:///v1/decide', 'http:///v1/decide', 'https://example.invalid/other',
                         'http://user:secret@example.invalid/v1/decide', 'https://example.invalid/v1/decide?token=x']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                agent.ZoroHTTPBackend(endpoint)
        client = agent.ZoroHTTPBackend('http://127.0.0.1:30001/v1/decide')
        for payload in [b'not-json', b'{}', b'x' * (1024*1024+1)]:
            with patch('agent.urlopen', return_value=io.BytesIO(payload)), self.assertRaises(ValueError):
                client.decide({})
        with patch('agent.urlopen', side_effect=TimeoutError('fixture timeout')):
            packet = self.run_case(decision_backend=client)
        self.assert_closed(packet)
        self.assertEqual(packet['zoro_status'], 'ENDPOINT_ATTEMPTED_NO_VALID_RESPONSE')

    def test_retriever_failure_is_explicit(self):
        search = FixtureRetriever()
        with patch.object(search, 'search', side_effect=RuntimeError('fixture retrieval failure')):
            packet = self.run_case(retriever=search)
        self.assert_closed(packet)
        self.assertEqual(packet['error']['message'], 'fixture retrieval failure')

    def test_conflicting_public_records_block_guidance(self):
        duplicate = deepcopy(self.latest())
        duplicate['value_or_null'] = 100.0
        self.snapshot['observation_prefix'].append(duplicate)
        packet = self.run_case()
        self.assert_closed(packet)
        self.assertIn('INVALID_PUBLIC_RECORD', packet['gate']['reasons'])

    def test_same_tag_same_time_ambiguity_blocks_guidance(self):
        duplicate = deepcopy(self.latest())
        duplicate['observation_id'] = 'ambiguous-order'
        duplicate['quality'] = 'BAD'
        self.snapshot['observation_prefix'].append(duplicate)
        packet = self.run_case()
        self.assert_closed(packet)
        self.assertIn('CONFLICTING_OBSERVATION_ORDER', packet['gate']['reasons'])

    def test_unlinked_alarm_evidence_blocks_guidance(self):
        for event in self.snapshot['published_alarm_events']:
            event['source_observation_ids'] = ['not-in-public-prefix']
        packet = self.run_case()
        self.assert_closed(packet)
        self.assertIn('ALARM_OBSERVATIONS_UNVERIFIED', packet['gate']['reasons'])

    def test_input_data_is_not_mutated(self):
        snapshot, rows, runtime = deepcopy(self.snapshot), deepcopy(self.rows), deepcopy(self.runtime)
        self.run_case()
        self.assertEqual(self.snapshot, snapshot)
        self.assertEqual(self.rows, rows)
        self.assertEqual(self.runtime, runtime)


if __name__ == "__main__":
    unittest.main()
