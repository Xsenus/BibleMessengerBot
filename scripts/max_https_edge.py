"""Own one HTTPS/VPN redirect, with watchdog rollback; never edit Xray or other rules."""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'certs' / 'max-edge'
CONTAINER = 'bible-max-edge'
IMAGE = 'nginx@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94'


def command(*args: str, allow_failure=False, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode and not allow_failure:
        raise RuntimeError(f'{args[0]} operation failed (exit {result.returncode})')
    return result


def owned_container():
    result = command('docker','container','inspect',CONTAINER,allow_failure=True)
    if result.returncode:
        return None
    state = json.loads(result.stdout)[0]
    if state['Config'].get('Labels',{}).get('io.bible-messenger.role') != 'max-edge':
        raise RuntimeError('Refusing to use an unowned gateway container')
    return state


def ready(ip: str):
    try:
        state = owned_container()
        if not state or not state['State']['Running']:
            return False
        with socket.create_connection(('127.0.0.1',29443),timeout=3) as raw:
            with ssl.create_default_context().wrap_socket(raw,server_hostname=ip) as tls:
                tls.sendall(f'GET /health HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n\r\n'.encode())
                response = b''
                while portion := tls.recv(4096):
                    response += portion
                    if len(response)>16384:
                        return False
                if b'200 OK' not in response or b'"platform":"max"' not in response:
                    return False
        with socket.create_connection(('127.0.0.1',443),timeout=3):
            pass
        return True
    except (OSError,ssl.SSLError,RuntimeError):
        return False


def rules(ip: str, interface: str):
    # No OUTPUT rule: backend connections to the original local Xray bypass redirect.
    return (
        ('filter','INPUT',('-i',interface,'-d',ip,'-p','tcp','--dport','29443',
            '-m','conntrack','--ctorigdst',ip,'--ctorigdstport','443',
            '-m','comment','--comment','bible-max-edge','-j','ACCEPT')),
        ('nat','PREROUTING',('-i',interface,'-d',ip,'-p','tcp','--dport','443',
            '-m','comment','--comment','bible-max-edge','-j','REDIRECT','--to-ports','29443')),
    )


def present(table, chain, rule):
    return command('iptables','-w','10','-t',table,'-C',chain,*rule,allow_failure=True).returncode == 0


def route_on(ip, interface):
    for table,chain,rule in rules(ip,interface):
        if not present(table,chain,rule):
            command('iptables','-w','10','-t',table,'-I',chain,'1',*rule)


def route_off(ip, interface):
    for table,chain,rule in reversed(rules(ip,interface)):
        while present(table,chain,rule):
            command('iptables','-w','10','-t',table,'-D',chain,*rule)


def start(ip, interface):
    expected = (ROOT/'deploy/max-edge.nginx.conf.template').read_text().replace('@PUBLIC_IP@',ip)
    config = STATE/'edge.conf'
    existing = owned_container()
    if existing and config.exists() and config.read_text()!=expected:
        raise RuntimeError('Stop the managed gateway before changing its configuration')
    if not config.exists() or config.read_text()!=expected:
        temporary = STATE/'edge.conf.new'
        temporary.write_text(expected)
        temporary.chmod(0o600)
        temporary.replace(config)
    mounts = ('-v',f'{config}:/etc/nginx/nginx.conf:ro',
              '-v',f'{STATE}/letsencrypt:/etc/letsencrypt:ro')
    if existing:
        if not existing['State']['Running']:
            command('docker','start',CONTAINER)
    else:
        for address,port in (('0.0.0.0',29443),('127.0.0.1',28443)):
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                listener.bind((address,port))
        command('docker','run','--rm','--network','none',*mounts,IMAGE,'nginx','-t')
        command('docker','run','-d','--name',CONTAINER,'--label','io.bible-messenger.role=max-edge',
            '--restart','unless-stopped','--network','host','--memory','128m','--cpus','1',
            '--security-opt','no-new-privileges:true','--cap-drop','ALL',
            '--cap-add','SETUID','--cap-add','SETGID','--cap-add','CHOWN',
            '--health-cmd','nginx -t','--health-interval','30s','--health-timeout','5s',
            '--health-retries','3',*mounts,IMAGE)
    for _attempt in range(30):
        if ready(ip):
            break
        time.sleep(2)
    else:
        raise RuntimeError('Gateway or MAX receiver is not ready; public route unchanged')
    (STATE/'routing-authorized').touch(mode=0o600)
    route_on(ip,interface)
    print('MAX HTTPS routing enabled; existing Xray listener unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('start','stop','guard','status'))
    args = parser.parse_args()
    if os.geteuid()!=0:
        raise RuntimeError('Run this infrastructure tool as root')
    ip = str(ipaddress.IPv4Address(os.environ.get('MAX_EDGE_IP','82.38.69.203')))
    interface = os.environ.get('MAX_EDGE_INTERFACE','ens1')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}',interface):
        raise ValueError('Invalid public interface')
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (STATE/'route.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if args.action == 'start':
            try:
                start(ip,interface)
            except Exception:
                route_off(ip,interface)
                (STATE/'routing-authorized').unlink(missing_ok=True)
                raise
        elif args.action == 'stop':
            route_off(ip,interface)
            (STATE/'routing-authorized').unlink(missing_ok=True)
            if owned_container():
                command('docker','stop','--time','15',CONTAINER)
            print('Owned redirect removed; new VPN connections go directly to Xray.')
        elif args.action == 'guard':
            active = command('systemctl','is-active','--quiet','bible-max-edge.service',allow_failure=True)
            if active.returncode or not (STATE/'routing-authorized').exists():
                return
            if ready(ip):
                route_on(ip,interface)
            else:
                route_off(ip,interface)
                raise RuntimeError('Gateway unavailable; owned redirect removed for direct VPN fallback')
        else:
            print(json.dumps({'gateway_ready':ready(ip),'redirect_present':present(*rules(ip,interface)[1]),
                              'public_ip':ip,'public_interface':interface}))


if __name__=='__main__':
    main()
