"""Unsuitable driver parents fail before dispatch, retries, or background spawn."""
from unittest.mock import patch

from bf import api, cli, runner
from bf.store import BfError
from test_bf_core import Base, make_plan
from test_bf_runner import FakeAdapter


class DriverEnvironment(Base):
    legacy_protocol = False

    def require_host(self):
        self.reinit(env={'codex': {'require_host': True}})

    def test_foreground_refused_without_mutating_orders_or_creating_run(self):
        self.require_host()
        before = (self.base / 'orders.json').read_bytes()
        fake = FakeAdapter(self.base, {})
        driver = runner.Runner(self.base, fake)
        with patch('agent_cli.check_execution', side_effect=ValueError('NoNewPrivs!=0')):
            with self.assertRaisesRegex(BfError, 'DRIVER_ENVIRONMENT_BLOCKED'):
                driver.step()
        self.assertEqual((self.base / 'orders.json').read_bytes(), before)
        self.assertEqual(fake.launches, [])
        self.assertFalse((self.base / 'driver.json').exists())
        self.assertFalse((self.base / 'records' / 'runs').exists())

    def test_background_and_ensure_run_refused_before_spawn(self):
        self.require_host()
        before = (self.base / 'orders.json').read_bytes()
        with patch('agent_cli.check_execution', side_effect=ValueError('cannot verify parent NoNewPrivs')):
            with patch('bf.runner.subprocess.Popen') as spawn:
                for command in (None, ['python3', 'driver']):
                    with self.assertRaisesRegex(BfError, 'DRIVER_ENVIRONMENT_BLOCKED'):
                        runner.ensure_running(self.base, command)
                spawn.assert_not_called()
        self.assertEqual((self.base / 'orders.json').read_bytes(), before)
        self.assertFalse((self.base / 'driver.log').exists())

    def test_cli_refused_before_profile_probing(self):
        self.require_host()
        with patch('agent_cli.check_execution', side_effect=ValueError('NoNewPrivs!=0')):
            with patch('bf.adapter.ensure_profiles') as profiles:
                self.assertEqual(cli.main(['--base', str(self.base), 'run', '--background']), 2)
                profiles.assert_not_called()

    def test_authorized_host_passes_and_tasks_without_requirement_skip_check(self):
        with patch('agent_cli.check_execution') as check:
            runner.check_driver_environment(self.st())
            check.assert_not_called()
            self.require_host()
            runner.check_driver_environment(self.st())
            check.assert_called_once_with({'require_host': True})
