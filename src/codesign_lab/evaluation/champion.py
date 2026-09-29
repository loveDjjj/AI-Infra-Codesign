"""独立生成和校验本地可提交冠军包。"""
import argparse
import json

from ..release import ensure_champion


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('record_id')
    parser.add_argument('--force', action='store_true', help='源码或课程轨迹更新后重建同分冠军')
    args = parser.parse_args()
    print(json.dumps(ensure_champion(args.record_id, force=args.force), ensure_ascii=False))


if __name__ == '__main__':
    main()
