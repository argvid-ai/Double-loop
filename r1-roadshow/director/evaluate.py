#!/usr/bin/env python3
"""Run reproducible, labeled image smoke cases on the actual board models."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import cv2

from logic import Director
from vision import PersonDetector, VLM


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cases', type=Path, help='JSON list: name, path, expected_scene (optional)')
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    cases = json.loads(args.cases.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    detector, vlm = PersonDetector(config), VLM(config)
    results = []
    try:
        for case in cases:
            path = Path(case['path']).expanduser()
            frame = cv2.imread(str(path))
            if frame is None:
                raise ValueError(f'Cannot read {path}')
            people = detector.detect(frame)
            audit, image = vlm.classify([frame], len(people))
            now = time.monotonic()
            # This isolates semantic classification from the separately tested debounce.
            decision = Director(confirmations=1).decide(audit.get('observation',{}),now,now,
                                                        detector_count=len(people),temporal=False)
            row = {**case, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'vision': audit, 'people': people, 'decision': decision,
                   'classification_match': (audit.get('observation',{}).get('scene') in case['expected_scenes'])
                       if 'expected_scenes' in case else None}
            results.append(row)
            (args.out/(case['name']+'.jpg')).write_bytes(image)
            (args.out/'results.json').write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
            print(json.dumps(row,ensure_ascii=False),flush=True)
    finally:
        detector.close()


if __name__ == '__main__':
    main()
