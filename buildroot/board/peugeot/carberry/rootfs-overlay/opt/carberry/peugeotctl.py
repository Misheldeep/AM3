#!/usr/bin/env python3
"""Console client for Peugeot / CarBerry bridge dev04."""

import argparse
import json
import socket
import sys
import time
from pathlib import Path

SOCKET_PATH = Path('/run/peugeot-bridge.sock')


def jline(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


class Client:
    def __init__(self, path=SOCKET_PATH, timeout=2.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.buf = bytearray()
        self.hello = self.recv_obj(timeout)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, obj):
        self.sock.sendall(jline(obj))

    def recv_obj(self, timeout=None):
        if timeout is not None:
            self.sock.settimeout(timeout)
        while True:
            p = self.buf.find(b'\n')
            if p >= 0:
                raw = bytes(self.buf[:p])
                del self.buf[:p + 1]
                if not raw.strip():
                    continue
                return json.loads(raw.decode('utf-8'))
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError('bridge API closed connection')
            self.buf.extend(data)

    def request(self, obj, timeout=2.0):
        self.send(obj)
        return self.recv_obj(timeout)


def show(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def onoff(text):
    value = str(text).lower()
    if value in ('1', 'on', 'true', 'yes', 'enable', 'enabled'):
        return True
    if value in ('0', 'off', 'false', 'no', 'disable', 'disabled'):
        return False
    raise argparse.ArgumentTypeError('use on/off')


def direction(text):
    value = str(text).strip().lower()
    if value in ('+', 'up', '+0.5', '1', '+1'):
        return 1
    if value in ('-', 'down', '-0.5', '-1'):
        return -1
    raise argparse.ArgumentTypeError('use + or -')


def request_once(req):
    c = Client()
    try:
        response = c.request(req)
        show(response)
        return 0 if response.get('ok') else 1
    finally:
        c.close()


def wait_filter_event(c, enabled, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = deadline - time.monotonic()
        try:
            obj = c.recv_obj(max(0.05, left))
        except socket.timeout:
            continue
        if (obj.get('event') == 'semantic'
                and obj.get('name') == 'can.debug_filters'
                and bool(obj.get('enabled')) == bool(enabled)):
            return True
    return False


def debug_sniff(args):
    c = Client(timeout=3.0)
    restore = not args.leave_debug
    try:
        sub = c.request({
            'cmd': 'subscribe',
            'can': True,
            'semantic': True,
            'channels': args.channels,
            'directions': ['rx'],
        })
        if not sub.get('ok'):
            show(sub)
            return 1

        resp = c.request({'cmd': 'debug.set', 'enabled': True})
        if not resp.get('ok'):
            show(resp)
            return 1

        if not wait_filter_event(c, True, timeout=3.0):
            print('WARNING: debug filter confirmation not seen; continuing', file=sys.stderr)

        print('=== DEBUG SNIFF: catch-all RX enabled; Ctrl+C stops ===')
        while True:
            try:
                obj = c.recv_obj(None)
            except socket.timeout:
                continue
            if obj.get('event') != 'can':
                continue
            mono = obj.get('mono', 0.0)
            ch = obj.get('channel')
            cid = obj.get('id', '')
            data = obj.get('data', '')
            print(f'{mono:14.6f}  RX{ch} {cid}-{data}', flush=True)

    except KeyboardInterrupt:
        print('\nStopping sniffer...', file=sys.stderr)
        return 0
    finally:
        c.close()
        if restore:
            try:
                r = Client(timeout=2.0)
                try:
                    response = r.request({'cmd': 'debug.set', 'enabled': False})
                    if response.get('ok'):
                        print('Production RX filters requested.', file=sys.stderr)
                    else:
                        print('WARNING: could not restore production filters: ' + str(response), file=sys.stderr)
                finally:
                    r.close()
            except Exception as exc:
                print(f'WARNING: could not restore production filters: {exc}', file=sys.stderr)


def main():
    p = argparse.ArgumentParser(prog='peugeotctl')
    sp = p.add_subparsers(dest='command', required=True)

    sp.add_parser('status')

    q = sp.add_parser('control')
    q.add_argument('state', type=onoff)

    q = sp.add_parser('debug')
    q.add_argument('state', type=onoff)

    q = sp.add_parser('debug-sniff')
    q.add_argument('--channels', type=int, nargs='+', choices=(1, 2), default=[1, 2])
    q.add_argument('--leave-debug', action='store_true', help='do not restore production filters on exit')

    q = sp.add_parser('display')
    q.add_argument('button', choices=('UP','DOWN','LEFT','RIGHT','OK','MENU','DARK','EXIT'))

    q = sp.add_parser('steer')
    q.add_argument('button', choices=('VOL_UP','VOL_DOWN','WHEEL_UP','WHEEL_DOWN','NEXT','PREVIOUS','PLAY_PAUSE'))

    q = sp.add_parser('media')
    q.add_argument('button', choices=('PLAY_PAUSE','NEXT','PREVIOUS'))

    q = sp.add_parser('volume')
    q.add_argument('group', choices=('master','sub'))
    q.add_argument('direction', type=direction, metavar='+|-')

    sp.add_parser('mute')
    sp.add_parser('dirac')

    q = sp.add_parser('preset')
    q.add_argument('number', type=int, choices=(1, 2))

    sp.add_parser('get-state-test')

    q = sp.add_parser('display-enable')
    q.add_argument('state', type=onoff)

    q = sp.add_parser('hu')
    q.add_argument('state', type=onoff)

    q = sp.add_parser('source')
    q.add_argument('value')

    args = p.parse_args()

    if args.command == 'debug-sniff':
        return debug_sniff(args)

    if args.command == 'status':
        req = {'cmd': 'status'}
    elif args.command == 'control':
        req = {'cmd': 'control.set', 'enabled': args.state}
    elif args.command == 'debug':
        req = {'cmd': 'debug.set', 'enabled': args.state}
    elif args.command == 'display':
        req = {'cmd': 'display.button', 'button': args.button}
    elif args.command == 'steer':
        req = {'cmd': 'steering.button', 'button': args.button}
    elif args.command == 'media':
        req = {'cmd': 'media.button', 'button': args.button}
    elif args.command == 'volume':
        req = {'cmd': 'harmony.volume', 'group': args.group, 'direction': args.direction}
    elif args.command == 'mute':
        req = {'cmd': 'harmony.mute'}
    elif args.command == 'dirac':
        req = {'cmd': 'harmony.dirac'}
    elif args.command == 'preset':
        req = {'cmd': 'harmony.preset', 'preset': args.number}
    elif args.command == 'get-state-test':
        req = {'cmd': 'harmony.get_state_test'}
    elif args.command == 'display-enable':
        req = {'cmd': 'display.enable', 'enabled': args.state}
    elif args.command == 'hu':
        req = {'cmd': 'hu.enable', 'enabled': args.state}
    elif args.command == 'source':
        req = {'cmd': 'hu.source_raw', 'value': args.value}
    else:
        p.error('unhandled command')
        return 2

    try:
        return request_once(req)
    except Exception as exc:
        print(f'peugeotctl: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
