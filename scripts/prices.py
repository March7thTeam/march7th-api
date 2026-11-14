"""Trusted local operator pricing CLI: python -m scripts.prices --help."""
import argparse
import json
import getpass
from contextlib import closing
from pathlib import Path
from app.db import connect, init_db
from app.price_store import seed_release, import_release, activate_release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('seed', help='Print an editable seed release JSON to stdout')
    commands.add_parser('history', help='List immutable releases and activation history')
    add = commands.add_parser('import', help='Validate and import JSON without activating')
    add.add_argument('file', type=Path)
    active = commands.add_parser('activate', help='Activate an already imported release atomically')
    active.add_argument('version')
    args = parser.parse_args()
    if args.command == 'seed':
        print(json.dumps(seed_release(), ensure_ascii=False, indent=2)); return
    init_db()
    with closing(connect()) as conn, conn:
        if args.command == 'history':
            print(json.dumps({'current': [dict(r) for r in conn.execute('SELECT * FROM price_current')],
                'versions': [dict(r) for r in conn.execute('SELECT version,sha256,imported_at,imported_by FROM price_versions ORDER BY imported_at')],
                'activations': [dict(r) for r in conn.execute('SELECT * FROM price_activations ORDER BY id')]}, ensure_ascii=False, indent=2)); return
        conn.execute('BEGIN IMMEDIATE')
        if args.command == 'import':
            import_release(conn, json.loads(args.file.read_text()), actor=getpass.getuser())
        else:
            activate_release(conn, args.version, actor=getpass.getuser())
    print('ok')

if __name__ == '__main__':
    main()
