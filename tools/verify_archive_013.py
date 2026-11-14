"""Read-only CRC32 audit for a completed CurrentSensor segmented archive."""
import argparse
import json
from pathlib import Path
from current_sensor.archive import verify_archive_segments


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path,help='archive directory or session.json')
    args=parser.parse_args()
    result=verify_archive_segments(args.archive)
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 0 if result['crc_ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
