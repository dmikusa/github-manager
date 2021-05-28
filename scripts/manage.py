#!/usr/bin/env python3
import sys
import ghm
from subprocess import CalledProcessError


if __name__ == '__main__':
    if not ghm.utils.check_requirements():
        print()
        print("This tool requires you to install `gh`, a Github cli")
        print()
        print("See https://cli.github.com/ for installation instructions")
        print()
        sys.exit(-2)

    parser = ghm.args.parse_args()
    try:
        res = parser.parse_args(sys.argv[1:])
        if not hasattr(res, 'func'):
            parser.print_help()
            sys.exit(-3)
        res.func(res)
    except CalledProcessError as ex:
        print()
        print("Failed:")
        print(f"   Command: {' '.join(ex.cmd)}")
        print(f"   Return : {ex.returncode}")
        print(f"   Output : {ex.output.decode('UTF-8').strip()}")
        print(f"   STDOUT : {ex.stdout.decode('UTF-8').strip()}")
        print(f"   STDERR : {ex.stderr.decode('UTF-8').strip()}")
