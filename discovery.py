import ipaddress
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

SSH_PORT = 22


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def arp_hosts():
    hosts = set()
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["arp", "-a", "-n"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return hosts
        for line in out.splitlines():
            m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", line)
            if m:
                hosts.add(m.group(1))
        return hosts
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[2] == "0x2":
                    hosts.add(parts[0])
    except OSError:
        pass
    return hosts


def _port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return host
    except OSError:
        return None


def scan_subnet(ip, port=SSH_PORT):
    net = ipaddress.ip_network(f"{ip}/24", strict=False)
    hosts = [str(h) for h in net.hosts()]
    with ThreadPoolExecutor(max_workers=48) as pool:
        found = pool.map(lambda h: _port_open(h, port), hosts)
    return {h for h in found if h}


def discover():
    found = set(arp_hosts())
    ip = local_ip()
    if ip:
        found |= scan_subnet(ip)
    return sorted(found, key=lambda h: [int(o) for o in h.split(".")])