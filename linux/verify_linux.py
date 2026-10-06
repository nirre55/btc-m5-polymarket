"""Linux-only offline service checks. No keys, auth or orders; temp databases only."""
from pathlib import Path
import json
import signal
import tempfile
import sys
from unittest.mock import patch
import subprocess
import os

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import bot

if os.name!='posix':
    raise SystemExit('Run with Linux Python')

with tempfile.TemporaryDirectory(prefix='btc-m5-check-') as name:
    folder=Path(name)
    (folder/'stop.request').write_text('old stop marker')
    (folder/'HALT').write_text('persistent halt')
    # Exercise real Linux signal handlers and cleanup without network access.
    with patch('bot.Engine.cycle',side_effect=lambda:signal.raise_signal(signal.SIGTERM)):
        bot.run(folder,bot.configuration(mode='prepare'))
    data=json.loads((folder/'status.json').read_text())
    assert data['health']['lifecycle']=='STOPPED' and data['execution_disabled']
    assert data['confirmed_or_simulated_positions']==0 and not bot.running(folder)
    assert not (folder/'stop.request').exists() and (folder/'HALT').exists()
    lock=bot.Lock(folder/'service.lock')
    try:
        try:
            other=bot.Lock(folder/'service.lock')
        except RuntimeError:
            pass
        else:
            other.close()
            raise AssertionError('Linux lock did not exclude second process')
    finally:
        lock.close()
    # Validate unit directives with an installed executable for the local check.
    # The production /opt path and service user are not installed here.
    unit=(ROOT/'linux/btc-m5-polymarket@.service').read_text()
    lines=[]
    for line in unit.splitlines():
        if line.startswith('ExecStart='):
            line=f'ExecStart={sys.executable} -c pass'
        lines.append(line)
    candidate=folder/'btc-m5-polymarket@.service'
    candidate.write_text('\n'.join(lines)+'\n')
    checked=subprocess.run(['systemd-analyze','verify',str(candidate)],capture_output=True,text=True)
    if checked.returncode:
        print(checked.stderr)
        raise SystemExit(checked.returncode)
    print('Linux SIGTERM cleanup, fcntl lock, stale-stop reset, persistent HALT: OK')
    print('systemd unit directives: OK (executable substituted; VPS installation untested)')
