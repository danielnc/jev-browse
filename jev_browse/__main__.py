"""`python3 -m jev_browse <command>` (or `jev-browse <command>` when installed as a package).

  install     add the harness helpers block and link the skill (see install.md)
  uninstall   remove the harness helpers block and the skill links
  doctor      check the install and configuration, and print what is active
  config      print the active settings (--example: a commented config.toml; --markdown: the docs table)
"""

import sys


def config_main(argv, out=print):
    import argparse

    from . import config
    from .doctor import load_harness_env

    ap = argparse.ArgumentParser(prog="python3 -m jev_browse config")
    ap.add_argument("--example", action="store_true", help="print a commented config.toml with every setting")
    ap.add_argument("--markdown", action="store_true", help="print the settings table for docs/configuration.md")
    args = ap.parse_args(argv)
    if args.example:
        out(config.example_toml(), end="")
        return 0
    if args.markdown:
        out(config.markdown_table(), end="")
        return 0
    load_harness_env()
    config.reset_cache()
    out(f"config file: {config.config_path()}")
    for row in config.active():
        out(f"{row['key']:<34} {row['value']!r:<28} ({row['source']}; {row['env']})")
    for key, msg in config.problems():
        out(f"problem: {key}: {msg}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "install":
        from .install import main as install_main

        return install_main(rest)
    if cmd == "uninstall":
        from .install import main as install_main

        return install_main(["--uninstall", *rest]) or install_main(["--unlink-skill", *rest])
    if cmd == "doctor":
        from .doctor import main as doctor_main

        return doctor_main(rest)
    if cmd == "config":
        return config_main(rest)
    print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
