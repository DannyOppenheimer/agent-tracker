"""Development launcher; never bundled into the release executable."""
import argparse

from dev_scenarios import SCENARIOS, scenario
from tracker import main as dashboard


def main(argv=None):
    parser = argparse.ArgumentParser(prog='agent-tracker',
        description='Development dashboard with isolated synthetic setups')
    cases = parser.add_mutually_exclusive_group(required=True)
    for number, description in SCENARIOS.items():
        cases.add_argument(f'--test-{number}', action='store_const', const=number,
                           dest='scenario', help=description)
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--json', action='store_true', help='Print synthetic sessions')
    output.add_argument('--usage-json', action='store_true', help='Print synthetic account usage')
    args = parser.parse_args(argv)
    forwarded = ['--json'] if args.json else ['--usage-json'] if args.usage_json else []
    dashboard(forwarded, preview=scenario(args.scenario))


if __name__ == '__main__':
    main()
