"""CI-resource guards only. No Docker, network, application settings or database."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
import io
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/container_smoke.py'
spec = importlib.util.spec_from_file_location('shushi_container_smoke', SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class ContainerSmokeGuards(unittest.TestCase):
    def test_workstation_refused_before_any_command_or_configuration_read(self):
        with patch.object(smoke.subprocess, 'run') as run:
            with self.assertRaises(smoke.SmokeError):
                smoke.guard_runner({})
            run.assert_not_called()

    def test_runner_event_must_be_public_and_cannot_contain_runtime_data(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); event = root / 'event.json'
            env = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'Linux',
                   'GITHUB_REPOSITORY': smoke.PUBLIC_REPO, 'GITHUB_WORKSPACE': str(root), 'GITHUB_EVENT_PATH': str(event)}
            event.write_text(json.dumps({'repository': {'private': False, 'full_name': smoke.PUBLIC_REPO}}))
            with patch.object(smoke.platform, 'system', return_value='Linux'):
                smoke.guard_runner(env, root)
                for key in ('DEEPSEEK_API_KEY', 'DOCKER_HOST', 'DATABASE_URL'):
                    with self.assertRaises(smoke.SmokeError):
                        smoke.guard_runner({**env, key: 'must-not-use'}, root)
                for path in ('.env', 'data'):
                    (root / path).touch()
                    with self.assertRaises(smoke.SmokeError): smoke.guard_runner(env, root)
                    (root / path).unlink()
                event.write_text(json.dumps({'repository': {'private': True, 'full_name': smoke.PUBLIC_REPO}}))
                with self.assertRaises(smoke.SmokeError): smoke.guard_runner(env, root)

    def model(self, project, root, caddy):
        return {'name': project, 'volumes': {'db': {'name': project + '_db'}},
                'networks': {'private': {'name': project + '_private'}},
                'services': {'api': {'environment': {'EXTERNAL_CALLS_ENABLED': 'false'}, 'build': {'context': str(root)}},
                             'caddy': {'ports': [{'host_ip': '127.0.0.1'}], 'volumes': [
                                 {'type': 'bind', 'source': str(caddy), 'target': '/etc/caddy/Caddyfile', 'read_only': True}]}}}

    def test_resolved_model_rejects_external_data_remote_ports_and_vendor_keys(self):
        project = 'shushi-ci-' + 'a' * 24
        root = Path('/tmp/isolated-ci'); caddy = Path('/tmp/synthetic/Caddyfile')
        good = self.model(project, root, caddy)
        smoke.validate_model(good, project, root, caddy)
        changes = [lambda m: m.update(name='shushi'),
                   lambda m: m['volumes']['db'].update(external=True),
                   lambda m: m['volumes']['db'].update(name='production_data'),
                   lambda m: m['services']['caddy']['ports'][0].update(host_ip='0.0.0.0'),
                   lambda m: m['services']['caddy']['volumes'][0].update(source='/private/data'),
                   lambda m: m['services']['api']['environment'].update(EXTERNAL_CALLS_ENABLED='true'),
                   lambda m: m['services']['api']['environment'].update(TRANSCRIPTION_API_KEY='do-not-send'),
                   lambda m: m['services']['api']['build'].update(context='/private/source'),
                   lambda m: m['services']['api'].update(container_name='production-api')]
        for change in changes:
            model = copy.deepcopy(good); change(model)
            with self.subTest(change=change), self.assertRaises(smoke.SmokeError):
                smoke.validate_model(model, project, root, caddy)

    def test_cleanup_refuses_caller_project_and_does_not_run_before_ownership(self):
        stack = object.__new__(smoke.Stack)
        stack.owned = False; stack.project = 'production'
        with patch.object(stack, 'compose') as compose:
            stack.cleanup(); compose.assert_not_called()
            stack.owned = True
            with self.assertRaises(smoke.SmokeError): stack.cleanup()
            compose.assert_not_called()

    def test_child_environment_does_not_inherit_credentials_or_compose_selection(self):
        clean = smoke.clean_environment({'PATH': '/usr/bin', 'HOME': '/tmp/ci', 'DEEPSEEK_API_KEY': 'secret',
                                         'COMPOSE_PROJECT_NAME': 'production', 'DOCKER_HOST': 'remote'})
        self.assertEqual(clean['PATH'], '/usr/bin')
        self.assertFalse({'DEEPSEEK_API_KEY', 'COMPOSE_PROJECT_NAME', 'DOCKER_HOST'} & clean.keys())

    def test_successful_checks_do_not_hide_cleanup_failure(self):
        class FakeStack:
            def __init__(self, *_args): pass
            def execute(self): return {'checks': 'passed'}
            def cleanup(self): raise smoke.SmokeError('cleanup failed')
        with patch.object(smoke, 'guard_runner'), patch.object(smoke, 'Stack', FakeStack), redirect_stderr(io.StringIO()):
            self.assertEqual(smoke.main(), 1)


if __name__ == '__main__':
    unittest.main()
