"""Default checks for the `loadtest` command routing."""
import unittest
from unittest import mock

from perf_runner import cli


class CliTests(unittest.TestCase):
    def test_run_takes_the_definition_as_its_argument(self):
        with mock.patch('perf_runner.run.main', return_value=0) as run:
            self.assertEqual(cli.main(['run', 'LoadTestDefinitions/checkout.yaml', '--quiet']), 0)
        run.assert_called_once_with(['run', '--definition', 'LoadTestDefinitions/checkout.yaml', '--quiet'])

    def test_status_cleanup_and_publish_are_forwarded(self):
        with mock.patch('perf_runner.run.main', return_value=0) as run:
            cli.main(['status'])
            cli.main(['cleanup', '--run-id', 'lt-x-1'])
        self.assertEqual(run.call_args_list, [mock.call(['status']), mock.call(['cleanup', '--run-id', 'lt-x-1'])])
        with mock.patch('perf_runner.publication.main', return_value=0) as publish:
            cli.main(['publish', 'suite', 'TestSuites/checkout', '--version', '1'])
        publish.assert_called_once_with(['suite', 'TestSuites/checkout', '--version', '1'])

    def test_unknown_command_prints_usage(self):
        with mock.patch('sys.stderr'):
            self.assertEqual(cli.main(['deploy']), 2)
        with mock.patch('sys.stdout'):
            self.assertEqual(cli.main(['--help']), 0)


if __name__ == '__main__':
    unittest.main()
