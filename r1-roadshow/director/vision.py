"""Local LQ50 VLM requests and optional CIX person-count cross-check."""
import base64
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

import cv2
import numpy as np

from logic import LABELS, PROMPT, observation


def contact_sheet(frames):
    tiles = []
    for index, frame in enumerate(frames):
        height, width = frame.shape[:2]
        scale = min(384 / width, 288 / height)
        tile = np.zeros((312, 384, 3), dtype=np.uint8)
        small = cv2.resize(frame, (round(width * scale), round(height * scale)))
        y, x = 24 + (288-small.shape[0])//2, (384-small.shape[1])//2
        tile[y:y+small.shape[0], x:x+small.shape[1]] = small
        cv2.putText(tile, f't{index}', (8, 17), cv2.FONT_HERSHEY_SIMPLEX, .5, (255,255,255), 1)
        tiles.append(tile)
    sheet = np.hstack(tiles)
    ok, encoded = cv2.imencode('.jpg', sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise RuntimeError('JPEG encoding failed')
    return encoded.tobytes()


class VLM:
    def __init__(self, config):
        self.endpoint = (os.environ.get('R1_VLM_ENDPOINT') or config.get('endpoint') or '').rstrip('/')
        if not self.endpoint.startswith(('http://', 'https://')):
            raise ValueError('Set R1_VLM_ENDPOINT to the VLM service URL before starting the director')
        self.model = config['model']
        self.timeout = config['request_timeout_s']

    def request(self, content, max_tokens):
        body = {'model': self.model, 'temperature': 0, 'seed': 42, 'max_tokens': max_tokens,
                'cache_prompt': False, 'chat_template_kwargs': {'enable_thinking': False},
                'messages': [{'role': 'user', 'content': content}]}
        request = urllib.request.Request(self.endpoint+'/v1/chat/completions',
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            reply = json.load(response)
        return reply['choices'][0], reply.get('usage')

    def classify(self, frames, detector_count, motion_candidates=()):
        image = contact_sheet(frames)
        choices = {0: ['无人'], 1: ['单人'], 2: ['双人交流', '双人独立']}.get(
            detector_count, ['多人讨论', '多人独立'])
        choices += ['遮挡', '不确定']
        if len(frames) > 1:
            choices += list(motion_candidates)
        prompt = PROMPT.format(labels='、'.join(choices))
        inputs = frames if motion_candidates else frames[-1:]
        if motion_candidates:
            prompt += '\n多张图按从早到晚排列，人数不累加。左右指图像中的方向，起立须从坐变站。'
        else:
            prompt += '\n这是最新单帧，不判断行走或起立。'
        started = time.monotonic()
        content = [{'type': 'text', 'text': prompt}]
        hashes = []
        for frame in inputs:
            h, w = frame.shape[:2]
            scale = min(640/w, 640/h)
            small = cv2.resize(frame, (max(1,round(w*scale)), max(1,round(h*scale))))
            ok, encoded = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise RuntimeError('Inference image encoding failed')
            data = encoded.tobytes()
            hashes.append(hashlib.sha256(data).hexdigest())
            content.append({'type':'image_url', 'image_url': {'url':'data:image/jpeg;base64,'+base64.b64encode(data).decode()}})
        choice, usage = self.request(content, 30)
        label = choice['message']['content'].strip().strip('。\n "')
        audit = {'raw_label': label, 'image_sha256': hashlib.sha256(image).hexdigest(),
                 'inference_images_sha256': hashes, 'candidates': choices, 'model': self.model,
                 'usage': usage, 'finish_reason': choice.get('finish_reason'), 'people_source': 'CIX YOLOv8n'}
        try:
            if choice.get('finish_reason') != 'stop' or label not in choices:
                raise ValueError('Classification must be one exact scene label')
            if label == '双人交流':
                # A separate visual question checks interaction rather than count.
                verify_prompt = '图中这两个人是否互相面对，正在对话式互动？两人同向背对相机行走不算交流。只回答是或否。'
                checked, checked_usage = self.request([
                    {'type':'text','text':verify_prompt}, content[-1]],12)
                answer = checked['message']['content'].strip().strip('。\n "')
                audit.update(interaction_verification=answer, interaction_usage=checked_usage)
                if checked.get('finish_reason') != 'stop' or answer not in ('是','否'):
                    raise ValueError('Interaction verification is ambiguous')
                label = '双人交流' if answer == '是' else '双人独立'
                audit['verified_label'] = label
            receipt = f'视觉模型分类：{label}；CIX人员检测：{detector_count}人'
            audit['observation'] = observation(dict(scene=LABELS[label], people=detector_count, evidence=receipt))
        except (ValueError, TypeError, KeyError) as error:
            audit['error'] = str(error)
        audit['latency_s'] = time.monotonic()-started
        return audit, image


def movement_candidates(first_people, last_people, first_shape, last_shape):
    """Only offer dynamic labels when a sole person's boxes support movement."""
    if len(first_people) != 1 or len(last_people) != 1:
        return []
    first = np.asarray(first_people[0]['xyxy']) / np.array([first_shape[1],first_shape[0]]*2)
    last = np.asarray(last_people[0]['xyxy']) / np.array([last_shape[1],last_shape[0]]*2)
    delta_x = (last[0]+last[2]-first[0]-first[2])/2
    labels = []
    if abs(delta_x) >= .04:
        labels.append('向右走' if delta_x > 0 else '向左走')
    if last[3]-last[1] >= 1.18*(first[3]-first[1]) and last[1] < first[1]-.04:
        labels.append('起立')
    return labels


class PersonDetector:
    def __init__(self, config):
        root = Path(config['yolo_dir']).expanduser()
        sys.path.insert(0, str(root))
        from benchmark_video import prepare
        from run_yolo import make_engine, postprocess
        self.prepare, self.postprocess = prepare, postprocess
        self.quiet = open(os.devnull, 'w')
        with redirect_stdout(self.quiet):
            self.forward, self.cleanup = make_engine('npu', root/'models/yolov8n.cix')
        self.confidence = config['yolo_confidence']

    def detect(self, frame):
        with redirect_stdout(self.quiet):
            raw = self.forward(self.prepare(frame))
        return [d for d in self.postprocess(raw, frame.shape, self.confidence) if d['label'] == 'person']

    def close(self):
        self.cleanup()
        self.quiet.close()
