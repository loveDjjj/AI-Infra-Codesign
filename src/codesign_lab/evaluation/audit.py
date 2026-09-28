"""独立审计并保全正式验收证据；不晋升或上传。"""
import argparse
import json
from ..release import audit, retain_release


def execute(identifier):
    record = retain_release(audit(identifier))
    return {field: record.get(field) for field in
            ['id', 'audited', 'eligible', 'reproduction', 'report', 'audit_path']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('identifier')
    args = parser.parse_args()
    print(json.dumps(execute(args.identifier), ensure_ascii=False))


if __name__ == '__main__':
    main()
