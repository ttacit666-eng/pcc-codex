"""Portable, non-secret runtime paths. Never reads authentication material."""
import json
import os
from pathlib import Path
import shutil

BASE = Path(__file__).resolve().parents[1]

def settings():
    path = BASE / 'config/runtime.local.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    return {
        'cli': os.environ.get('PCC_CODEX_CLI') or config.get('cli') or shutil.which('codex') or 'codex',
        'plus_home': str(Path(os.environ.get('PCC_PLUS_HOME') or config.get('plus_home') or Path.home()/'.codex-plus').expanduser().absolute()),
        'controller_home': str(Path(os.environ.get('PCC_CONTROLLER_HOME') or config.get('controller_home') or os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser().absolute()),
    }

def validate_separation(plus_home=None):
    config = settings()
    plus = Path(plus_home or config['plus_home']).absolute()
    controller = Path(config['controller_home']).absolute()
    for target in (plus, controller):
        for part in (target, *target.parents):
            if part.is_symlink() or (part.exists() and getattr(part.lstat(),'st_file_attributes',0)&0x400):
                raise ValueError('Runtime homes must not traverse reparse points')
    plus, controller = plus.resolve(), controller.resolve()
    if plus == controller or plus in controller.parents or controller in plus.parents:
        raise ValueError('Plus and controller homes must be separate, non-nested directories')
    if plus == Path.home() or plus == Path(plus.anchor):
        raise ValueError('Plus home must be a dedicated directory')
    return config

def controller_env():
    env = os.environ.copy()
    env['CODEX_HOME'] = settings()['controller_home']
    return env
