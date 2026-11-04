"""`loadtest`: the one command teams use.

  loadtest run <definition.yaml> [--quiet] [--platform FILE] ...   run one load test
  loadtest publish application <ad.yaml> --version V              publish an ApplicationDefinition by name
  loadtest publish suite <folder> --version V                     publish a TestSuite by name
  loadtest status                                                 list runs (active / interrupted / finished)
  loadtest cleanup --run-id <id>                                  remove an abandoned run's namespace
"""
import sys

from perf_runner import publication, run


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command, rest = (argv[0], argv[1:]) if argv else ('', [])
    if command == 'run' and rest and not rest[0].startswith('-'):
        return run.main(['run', '--definition', rest[0], *rest[1:]])
    if command in ('run', 'status', 'cleanup'):
        return run.main([command, *rest])
    if command == 'publish':
        return publication.main(rest)
    print(__doc__, file=sys.stderr if command not in ('-h', '--help', 'help') else sys.stdout)
    return 0 if command in ('-h', '--help', 'help') else 2


if __name__ == '__main__':
    raise SystemExit(main())
