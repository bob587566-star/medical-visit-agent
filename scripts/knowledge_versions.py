"""Local source-library versioning. Never accepts patient files or model actions."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.knowledge_versions import KnowledgeVersions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['snapshot', 'list', 'rollback'])
    parser.add_argument('version', nargs='?')
    args = parser.parse_args()
    repository = KnowledgeVersions(ROOT / 'knowledge' / 'articles.json')
    if args.action == 'snapshot':
        result = repository.snapshot()
    elif args.action == 'list':
        result = repository.versions()
    else:
        if not args.version:
            parser.error('rollback requires a version hash')
        result = repository.rollback(args.version)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
